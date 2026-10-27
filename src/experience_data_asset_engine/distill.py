"""蒸馏协议与模型无关执行器。

两条路径，都不允许「把原文当笔记」：

1. **无 provider**：只写 ``staging/<state_key>.request.md`` 与 ``.schema.json``，抛
   ``DistillationRequired``，由 Agent 或人工补完蒸馏。归档目录一个字节都不动。
2. **有 provider**：走 OpenAI-compatible ``POST {base_url}/chat/completions``，返回内容必须
   通过 ``DistilledNote`` 全量校验；任何失败（HTTP、超时、截断、非 JSON、缺字段、摘要冒充、
   被否决方案全空）都失败关闭。

另外两条硬约束：

- 提示词只包含**降噪后**的对话；``noise_report`` 由管道用真实统计覆盖，不采信模型自报。
- 凭据只从环境变量读，不进请求日志、不进异常消息、不落盘。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any, Final

from .config import AppConfig, ensure_inside_vault, write_json_atomic
from .errors import ConfigError, DistillationError, DistillerError, SchemaError
from .models import (
    MAX_PROJECT_LENGTH,
    MAX_TITLE_LENGTH,
    NOISE_COUNTERS,
    SCHEMA_VERSION,
    STATUS_VALUES,
    VOLATILITY_VALUES,
    DistilledNote,
    NoiseReport,
    NormalizedSession,
)
from .noise import filter_noise

REQUEST_TIMEOUT_SECONDS: Final = 60.0
MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024
MAX_PROMPT_CONVERSATION_CHARS: Final = 40_000

DISTILL_CONTRACT: Final = """\
你是一个知识蒸馏器，不是摘要器。把对话压缩成一份可复用、可追溯的经验数据资产。

硬性要求：
1. 只输出一个 JSON 对象，不要输出解释、不要输出 Markdown 围栏之外的任何文字。
2. 必须包含字段：summary、background、diagnosis、decisions、evidence、reusable、
   risks、todo、noise_report、content_volatility、status、review_after，
   以及 title、project、schema_version。
3. 每个 decision 必须有非空 reason（为什么这么选）。
4. 所有 decisions 汇总后，至少要有 1 条有效的 rejected（被否决方案：本来还能怎么做、为什么没做）。
   如果当时确实只有一条路，就写「无备选：<实质原因>」——原因不少于 8 个有效字符，
   且不能用「原因未知」「不适用」「暂无」「略」「TODO」这类占位话术；
   宁可承认没有备选，也不要为了过校验编造被否决方案。
5. summary 是一句话结论，不能替代其它字段；只写摘要的输出会被判为不合格。
6. evidence 只放可复核的证据或短引用，禁止整段粘贴对话原文。
7. content_volatility 取 stable / evolving / volatile / progress 之一。
8. status 取 有效 / 待复核 / 已过时 / 已被取代 之一。
9. review_after 用 YYYY-MM-DD。
10. noise_report 的七个数字必须原样复制下面给出的统计，不要自己编。
"""

_JSON_FENCE_RE: Final = re.compile(r"^\s*```(?:json|JSON)?\s*\n(.*?)\n```\s*$", re.S)


class DistillationRequired(DistillerError):
    """未配置模型：蒸馏需要 Agent 或人工介入。

    ``staging_path`` 指向生成好的请求文件，``schema_path`` 指向协议样例。
    CLI 层应把它映射成退出码 3（需要蒸馏），而不是普通失败。
    """

    exit_code = 3

    def __init__(self, staging_path: Path, schema_path: Path | None = None) -> None:
        super().__init__(f"需要蒸馏：{staging_path}")
        self.staging_path = Path(staging_path)
        self.schema_path = Path(schema_path) if schema_path is not None else None


def note_json_schema() -> dict[str, Any]:
    """``DistilledNote`` 的 JSON Schema，给 staging 与外部 Agent 复用。"""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "DistilledNote",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "title",
            "project",
            "summary",
            "background",
            "diagnosis",
            "decisions",
            "evidence",
            "reusable",
            "risks",
            "todo",
            "noise_report",
            "content_volatility",
            "status",
            "review_after",
        ],
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "title": {"type": "string", "maxLength": MAX_TITLE_LENGTH},
            "project": {"type": "string", "maxLength": MAX_PROJECT_LENGTH},
            "summary": {"type": "string"},
            "background": {"type": "string"},
            "diagnosis": {"type": "array", "items": {"type": "string"}},
            "decisions": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "所有 decisions 汇总后至少要有 1 条有效 rejected；"
                    "无备选时写「无备选：<实质原因>」，原因 >= 8 个有效字符且不得是占位话术"
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["decision", "reason"],
                    "properties": {
                        "decision": {"type": "string"},
                        "reason": {"type": "string"},
                        "rejected": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "evidence": {"type": "array", "items": {"type": "string"}},
            "reusable": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "todo": {"type": "array", "items": {"type": "string"}},
            "noise_report": {
                "type": "object",
                "additionalProperties": False,
                "required": list(NOISE_COUNTERS),
                "properties": {name: {"type": "integer", "minimum": 0} for name in NOISE_COUNTERS},
            },
            "content_volatility": {"enum": list(VOLATILITY_VALUES)},
            "status": {"enum": list(STATUS_VALUES)},
            "review_after": {"type": "string", "format": "date"},
        },
    }


def validate_distilled_note(data: Any) -> DistilledNote:
    """把外部 JSON 校验成 ``DistilledNote``。任何不合格都抛 ``SchemaError``。"""
    return DistilledNote.from_dict(data)


def prepare_distillation(session: NormalizedSession, config: AppConfig) -> DistilledNote:
    """降噪 + 蒸馏。无 provider 时只写 staging 并抛 ``DistillationRequired``。"""
    cleaned, report = filter_noise(session)

    if not config.provider:
        request_path, schema_path = _write_staging(cleaned, report, config)
        raise DistillationRequired(request_path, schema_path)

    api_key = config.api_key()
    if not api_key:
        raise ConfigError(
            f"已配置模型提供方 {config.provider}，但环境变量 {config.api_key_env} 为空；"
            "不会生成任何伪成功结果，也不会把原文写进归档目录"
        )
    if not config.base_url:
        raise ConfigError("已配置模型提供方，但缺少 base_url；请设置 EDAE_BASE_URL")

    content = _request_completion(cleaned, report, config, api_key)
    note = validate_distilled_note(_loads_json(content))
    # 统计只能由管道产生：模型自报的数字一律覆盖
    return replace(note, noise_report=report)


# --- staging ----------------------------------------------------------------


def _format_conversation(session: NormalizedSession) -> str:
    blocks: list[str] = []
    used = 0
    for message in session.messages:
        block = f"### {message.role}\n{message.text}\n"
        if used + len(block) > MAX_PROMPT_CONVERSATION_CHARS:
            blocks.append("…[对话其余部分因长度上限省略]")
            break
        blocks.append(block)
        used += len(block)
    return "\n".join(blocks).strip()


def _noise_lines(report: NoiseReport) -> str:
    payload = report.to_dict()
    return "\n".join(f"- {name}: {value}" for name, value in payload.items())


def _build_messages(session: NormalizedSession, report: NoiseReport) -> list[dict[str, str]]:
    user_content = (
        f"会话标题：{session.title}\n"
        f"来源：{session.source}\n\n"
        f"降噪统计（必须原样写进 noise_report）：\n{_noise_lines(report)}\n\n"
        f"过滤后的对话：\n\n{_format_conversation(session)}"
    )
    return [
        {"role": "system", "content": DISTILL_CONTRACT},
        {"role": "user", "content": user_content},
    ]


def _write_text_atomic(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)
    return path


def _write_staging(
    session: NormalizedSession, report: NoiseReport, config: AppConfig
) -> tuple[Path, Path]:
    staging_dir = config.staging_dir
    ensure_inside_vault(config.vault, staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    ensure_inside_vault(config.vault, staging_dir)

    key = session.state_key
    request_path = staging_dir / f"{key}.request.md"
    schema_path = staging_dir / f"{key}.schema.json"

    body = "\n".join(
        [
            "# 蒸馏请求",
            "",
            f"- 来源: {session.source}",
            f"- 会话ID: {session.session_id}",
            f"- state_key: {key}",
            f"- 标题: {session.title}",
            f"- 消息: 保留 {report.kept_messages} / 输入 {report.input_messages}",
            "- 用途: distillation prompt（契约 + 统计 + 过滤后对话）",
            "",
            "## 降噪统计",
            "",
            _noise_lines(report),
            "",
            "## 蒸馏契约",
            "",
            DISTILL_CONTRACT.strip(),
            "",
            "## 过滤后的对话",
            "",
            _format_conversation(session),
            "",
            "## 输出",
            "",
            f"把蒸馏结果写成 JSON，保存到 `{staging_dir.name}/{key}.distilled.json`，"
            "字段与 `" + schema_path.name + "` 一致。不要把原文粘回任何笔记。",
            "",
        ]
    )
    _write_text_atomic(request_path, body)
    write_json_atomic(schema_path, note_json_schema())
    return request_path, schema_path


# --- provider ---------------------------------------------------------------


def _request_completion(
    session: NormalizedSession, report: NoiseReport, config: AppConfig, api_key: str
) -> str:
    url = f"{config.base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": config.model,
        "messages": _build_messages(session, report),
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise DistillationError(
            f"模型接口返回 HTTP {exc.code}，已失败关闭（不降级为原文）"
        ) from None
    except urllib.error.URLError as exc:
        raise DistillationError(
            f"调用模型失败（网络或超时），已失败关闭: {type(exc.reason).__name__}"
        ) from None
    except (TimeoutError, OSError) as exc:
        raise DistillationError(f"调用模型失败（网络或超时），已失败关闭: {type(exc).__name__}") from None

    if len(raw) > MAX_RESPONSE_BYTES:
        raise DistillationError("模型响应超过大小上限，已失败关闭")

    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DistillationError("模型响应不是合法 JSON，已失败关闭") from None

    return _extract_content(envelope)


def _extract_content(envelope: Any) -> str:
    if not isinstance(envelope, dict):
        raise DistillationError("模型响应结构异常（顶层不是对象），已失败关闭")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        raise DistillationError("模型响应缺少 choices，已失败关闭")
    first = choices[0]
    if not isinstance(first, dict):
        raise DistillationError("模型响应结构异常（choice 不是对象），已失败关闭")
    if first.get("finish_reason") == "length":
        raise DistillationError("模型响应被截断（finish_reason=length），已失败关闭")
    message = first.get("message")
    if not isinstance(message, dict):
        raise DistillationError("模型响应缺少 message，已失败关闭")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise DistillationError("模型响应内容为空，已失败关闭")
    return content


def _loads_json(content: str) -> dict[str, Any]:
    text = content.strip()
    fenced = _JSON_FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DistillationError(
            f"模型输出不是合法 JSON，已失败关闭（第 {exc.lineno} 行第 {exc.colno} 列）"
        ) from None
    if not isinstance(data, dict):
        raise SchemaError("蒸馏笔记必须是 JSON 对象")
    return data
