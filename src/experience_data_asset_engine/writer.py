"""Obsidian 安全写入与幂等（Task 8）。

写入是整套工具唯一会改动用户磁盘的动作，因此这里的规则比其它模块更硬：

1. **只写 vault 内**：``<archive_dir>/<project>/<title>.md``，落盘前所有路径都用
   ``resolve_safely`` 解析并确认仍在 vault 与 archive_dir 之内；符号链接逃逸一律拒绝。
2. **不覆盖别人的笔记**：目标文件存在但不是我写的（没有生成器标记或状态键不匹配），
   只写 ``<title>.pending-<hash>.md`` 候选（完整 Markdown，可合并），绝不覆盖；
   同一份冲突内容重复归档命中同一个 pending，不产生 ``.pending-2`` / ``.pending-3``。
3. **同一会话只有一篇正式笔记**：``state_key`` 已记录过别的路径时，不再 created 第二篇，
   改为落 pending 并标出「待合并到」的旧路径，由人确认；工具不移动、不删除任何笔记。
3. **原子替换**：先写同目录临时文件再 ``os.replace``，不留半截 Markdown。
4. **幂等**：同一 ``source + session_id`` 内容没变就是 ``skipped``，文件字节一个都不动。
5. **dry-run 零写入**：不建目录、不写 Markdown、不写 state、不写 audit。
6. **写入边界再脱敏**：不信任上游模型，Markdown 落盘前再过一遍 ``redact_sensitive``。
7. **不删不移**：不删除、不移动任何用户笔记，也不自动清理旧版本。

frontmatter 的渲染与解析都放在本模块，作为「落盘格式」的唯一口径；
``validator.py`` 直接复用 ``parse_frontmatter``，避免两套格式定义漂移。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Final

from .config import AppConfig, ensure_inside_vault, read_json_file, write_json_atomic
from .errors import ConfigError, SchemaError
from .models import DistilledNote, NoiseReport, NormalizedSession
from .noise import redact_sensitive

GENERATOR: Final = "experience-data-asset-engine"
FRONTMATTER_FENCE: Final = "---"
PENDING_SUFFIX: Final = ".pending"
#: 待合并候选的**精确**文件名：``<stem>.pending-<8位小写十六进制>.md``，
#: 以及旧版 ``<stem>.pending.md``。用精确匹配而不是 ``".pending" in stem``，
#: 否则 ``release.pending-notes.md`` 这种普通笔记会被误判成候选。
_PENDING_NAME_RE: Final = re.compile(r"^.+\.pending(?:-[0-9a-f]{8})?\.md$")
STATE_SCHEMA_VERSION: Final = 1

#: 写入动作。
ACTION_CREATED: Final = "created"
ACTION_UPDATED: Final = "updated"
ACTION_SKIPPED: Final = "skipped"
ACTION_PENDING: Final = "pending"

#: frontmatter 必填字段（顺序即渲染顺序）。
FRONTMATTER_FIELDS: Final = (
    "tags",
    "项目",
    "主题",
    "状态",
    "内容时效",
    "created",
    "updated",
    "valid_from",
    "review_after",
    "取代自",
    "取代为",
    "来源",
    "会话ID",
    "内容哈希",
    "生成器",
    "状态键",
)

#: Obsidian / Windows 都不接受的文件名字符。
_ILLEGAL_NAME_CHARS: Final = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED: Final = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


# --- 结果类型 ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WriteResult:
    """一次写入的结论。``action`` 只会是 created / updated / skipped / pending。"""

    path: Path
    action: str
    content_hash: str
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "action": self.action,
            "content_hash": self.content_hash,
            "dry_run": self.dry_run,
        }


# --- 时间与哈希 -------------------------------------------------------------


def _today() -> date:
    """当前日期，**本地时区**。

    不能用 ``datetime.now(timezone.utc).date()``：在东八区每天 00:00-08:00
    会被算成前一天，``created`` / ``updated`` 就比真实归档日早一天，
    笔记里记的日期与用户日历对不上。测试通过 monkeypatch 本函数固定基准日。
    """
    return datetime.now().date()


def content_hash_of(note: DistilledNote, session: NormalizedSession) -> str:
    """内容指纹：笔记内容 + 来源会话标识。

    只用于判断「同一份内容是否已经写过」，因此**不包含**写入时间，
    否则每次写入都会算出不同哈希，幂等就失效了。
    """
    payload = {
        "note": note.to_dict(),
        "source": session.source,
        "session_id": session.session_id,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def state_key_of(session: NormalizedSession) -> str:
    """幂等键：与 ``NormalizedSession.state_key`` 同源，不把 session_id 当路径。"""
    return session.state_key


# --- 名称安全 ---------------------------------------------------------------


def _scrub(text: str) -> str:
    """落盘前的最后一道脱敏。

    上游蒸馏模型产出的正文同样不可信：它可能把会话里的密钥、Cookie、
    Authorization 头原样抄进笔记。写入端不假设上游已清洗，逐字段再过一遍。
    """
    if not isinstance(text, str):
        raise SchemaError("待脱敏内容必须是字符串")
    redacted, _ = redact_sensitive(text)
    return redacted


def safe_component(name: str, label: str = "名称") -> str:
    """把一段文本变成安全的单级文件/目录名。

    先脱敏，再按路径规则校验：脱敏不能成为绕开路径校验的后门。
    """
    if not isinstance(name, str):
        raise SchemaError(f"{label} 必须是字符串")
    candidate = _scrub(name).strip()
    if not candidate:
        raise SchemaError(f"{label} 脱敏后为空，拒绝写入")
    if _ILLEGAL_NAME_CHARS.search(candidate):
        raise SchemaError(f"{label} 含非法字符（不能用于文件名）：{candidate!r}")
    if candidate.startswith("."):
        raise SchemaError(f"{label} 不能以点开头（会生成隐藏文件）：{candidate!r}")
    if candidate.endswith(".") or candidate.endswith(" "):
        raise SchemaError(f"{label} 不能以点或空格结尾：{candidate!r}")
    if candidate.split(".")[0].upper() in _WINDOWS_RESERVED:
        raise SchemaError(f"{label} 不能使用 Windows 保留名：{candidate!r}")
    return candidate


# --- frontmatter 渲染 -------------------------------------------------------

_UNSAFE_YAML_START: Final = re.compile(r"^[\s\-?:,\[\]{}#&*!|>'\"%@`]")
#: 逗号必须算「需要加引号」：``[a, b]`` 形式下未加引号的逗号会被拆成两个元素。
_UNSAFE_YAML_CONTAINS: Final = re.compile(r"[:,#\[\]{}\n\r\t]")

_YAML_ESCAPES: Final = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "r": "\r"}
#: 看起来像数字的字符串必须加引号，否则回到解析端会被当成 int。
#: 哈希是全十六进制串，纯数字的哈希（如 ``"1"*64``）不引号就会丢类型，
#: 进而让「内容没变」误判成「内容变了」。
_NUMERIC_LIKE: Final = re.compile(r"^-?\d+(\.\d+)?$")


def _yaml_escape(text: str) -> str:
    out: list[str] = []
    for char in text:
        if char in {'"', "\\"}:
            out.append("\\" + char)
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif char == "\r":
            out.append("\\r")
        else:
            out.append(char)
    return "".join(out)


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "":
        return '""'
    needs_quote = bool(
        _UNSAFE_YAML_START.search(text)
        or _UNSAFE_YAML_CONTAINS.search(text)
        or text.strip() != text
        or _NUMERIC_LIKE.match(text)
        or text.lower() in {"null", "true", "false", "yes", "no", "~"}
    )
    if not needs_quote:
        return text
    return f'"{_yaml_escape(text)}"'


def _yaml_list(values: Sequence[str]) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(_yaml_scalar(item) for item in values) + "]"


def render_frontmatter(fields: Mapping[str, Any]) -> str:
    """按固定字段顺序渲染 frontmatter（含首尾分隔线）。"""
    lines = [FRONTMATTER_FENCE]
    for key in FRONTMATTER_FIELDS:
        if key not in fields:
            raise SchemaError(f"frontmatter 缺少必填字段 {key}")
        value = fields[key]
        rendered = _yaml_list(value) if isinstance(value, (list, tuple)) else _yaml_scalar(value)
        lines.append(f"{key}: {rendered}")
    lines.append(FRONTMATTER_FENCE)
    return "\n".join(lines)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析本工具渲染的 frontmatter，返回 (字段字典, 正文)。

    只支持本项目实际写出的 YAML 子集：``key: 标量`` 与 ``key: [a, b]``。
    解析失败抛 ``SchemaError``——绝不把解析不了的文件当成合法笔记。
    """
    if not isinstance(text, str):
        raise SchemaError("笔记内容必须是字符串")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith(FRONTMATTER_FENCE + "\n"):
        raise SchemaError("缺少 frontmatter 起始分隔线 ---")
    lines = normalized.split("\n")
    fields: dict[str, Any] = {}
    closing = None
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == FRONTMATTER_FENCE:
            closing = index
            break
        if not line.strip():
            continue
        if ":" not in line:
            raise SchemaError(f"frontmatter 第 {index + 1} 行不是 key: value 形式")
        key, _, raw = line.partition(":")
        key = key.strip()
        if not key:
            raise SchemaError(f"frontmatter 第 {index + 1} 行缺少键名")
        if key in fields:
            raise SchemaError(f"frontmatter 出现重复键 {key}")
        fields[key] = _parse_scalar(raw.strip())
    if closing is None:
        raise SchemaError("frontmatter 缺少结束分隔线 ---")
    body = "\n".join(lines[closing + 1 :])
    # frontmatter 与正文之间按惯例空一行；这不属于正文内容，去掉以免每次往返多一个空行。
    return fields, body.lstrip("\n")


def _split_list_items(inner: str) -> list[str]:
    """按逗号切分列表项，但引号内的逗号不切（项目名可能带逗号）。"""
    items: list[str] = []
    buffer: list[str] = []
    in_quotes = False
    escaped = False
    for char in inner:
        if escaped:
            buffer.append(char)
            escaped = False
            continue
        if in_quotes and char == "\\":
            buffer.append(char)
            escaped = True
            continue
        if char == '"':
            in_quotes = not in_quotes
            buffer.append(char)
            continue
        if char == "," and not in_quotes:
            items.append("".join(buffer).strip())
            buffer = []
            continue
        buffer.append(char)
    items.append("".join(buffer).strip())
    return [item for item in items if item]


def _unquote(raw: str) -> str:
    """按渲染时的转义顺序反向还原（逐字符扫描，不用 replace 链）。"""
    if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
        return raw
    out: list[str] = []
    index = 1
    end = len(raw) - 1
    while index < end:
        char = raw[index]
        if char == "\\" and index + 1 < end:
            nxt = raw[index + 1]
            out.append(_YAML_ESCAPES.get(nxt, nxt))
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _parse_scalar(raw: str) -> Any:
    if raw == "" or raw == "null" or raw == "~":
        return None
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in _split_list_items(inner)]
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return _unquote(raw)
    if raw.lower() in {"true", "false"}:
        return raw.lower() == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


# --- 正文渲染 ---------------------------------------------------------------


def _bullets(items: Sequence[str], empty: str = "无") -> str:
    if not items:
        return empty
    return "\n".join(f"- {_scrub(item)}" for item in items)


def _decisions_block(note: DistilledNote) -> str:
    blocks: list[str] = []
    for index, decision in enumerate(note.decisions, start=1):
        blocks.append(f"### {index}. {_scrub(decision.decision)}")
        blocks.append("")
        blocks.append(f"理由：{_scrub(decision.reason)}")
        if decision.rejected:
            blocks.append("")
            blocks.append("被否决方案：")
            blocks.append("")
            blocks.append(_bullets(decision.rejected))
        blocks.append("")
    return "\n".join(blocks).rstrip()


def _noise_section(report: NoiseReport) -> str:
    """降噪统计章节：七个计数里可读的六个（kept/input 合并成一行）。

    计数只来自 ``note.noise_report``——也就是管道 ``filter_noise`` 的返回值，
    不从模型响应或 CLI 临时变量重新拼，避免「笔记里一套、日志里另一套」。
    """
    return "\n".join(
        [
            f"过滤消息 {report.kept_messages}/{report.input_messages}",
            f"系统噪音 {report.system_noise}",
            f"工具噪音 {report.tool_noise}",
            f"重复主题 {report.duplicate_topics}",
            f"无效死路 {report.dead_ends}",
            f"脱敏 {report.redactions}",
        ]
    )


def render_body(note: DistilledNote, title: str | None = None) -> str:
    """渲染笔记正文（不含 frontmatter）。

    ``title`` 由调用方传入已经过 ``safe_component`` 的同一条字符串，
    保证正文标题与文件名不会出现一份脱敏、一份原文的错位。
    """
    heading = title if title is not None else safe_component(note.title, "标题")
    sections = [
        ("摘要", _scrub(note.summary)),
        ("背景", _scrub(note.background)),
        ("诊断", _bullets(note.diagnosis)),
        ("决策", _decisions_block(note)),
        ("证据", _bullets(note.evidence)),
        ("可复用", _bullets(note.reusable)),
        ("风险", _bullets(note.risks)),
        ("待办", _bullets(note.todo)),
        ("降噪统计", _noise_section(note.noise_report)),
    ]
    parts = [f"# {heading}", ""]
    for section_heading, content in sections:
        parts.append(f"## {section_heading}")
        parts.append("")
        parts.append(content if content else "无")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def render_note(
    note: DistilledNote,
    session: NormalizedSession,
    content_hash: str,
    created: str,
    updated: str,
    title: str | None = None,
    project: str | None = None,
) -> str:
    """渲染完整 Markdown（frontmatter + 正文）。

    ``title`` / ``project`` 不传时内部按 ``safe_component`` 规范化，
    保证与写入路径使用的名称逐字一致。
    """
    safe_title = title if title is not None else safe_component(note.title, "标题")
    safe_project = project if project is not None else safe_component(note.project, "项目名")
    fields = {
        "tags": ["对话存档", safe_project, note.volatility_label],
        "项目": safe_project,
        "主题": safe_title,
        "状态": note.status,
        "内容时效": note.volatility_label,
        "created": created,
        "updated": updated,
        "valid_from": created,
        "review_after": note.review_after,
        "取代自": [],
        "取代为": [],
        "来源": session.source,
        "会话ID": session.session_id,
        "内容哈希": content_hash,
        "生成器": GENERATOR,
        "状态键": state_key_of(session),
    }
    return render_frontmatter(fields) + "\n\n" + render_body(note, safe_title)


# --- state / audit ----------------------------------------------------------


def _load_state(config: AppConfig) -> dict[str, Any]:
    ensure_inside_vault(config.vault, config.state_dir)
    ensure_inside_vault(config.vault, config.state_path)
    path = config.state_path
    if not path.exists():
        return {"schema_version": STATE_SCHEMA_VERSION, "entries": {}}
    try:
        payload = read_json_file(path)
    except ConfigError as exc:
        raise ConfigError(f"state.json 无法读取，为避免覆盖历史记录已停止：{exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("state.json 结构损坏（顶层不是对象），已停止写入")
    entries = payload.get("entries")
    if entries is None:
        payload["entries"] = {}
    elif not isinstance(entries, dict):
        raise ConfigError("state.json 的 entries 不是对象，已停止写入")
    payload.setdefault("schema_version", STATE_SCHEMA_VERSION)
    return payload


def _append_audit(config: AppConfig, record: Mapping[str, Any]) -> Path:
    """追加一行审计。只记路径、哈希、动作、计数，不记对话原文与凭据。"""
    audit_dir = config.audit_dir
    ensure_inside_vault(config.vault, audit_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)
    ensure_inside_vault(config.vault, audit_dir)
    log_path = audit_dir / "write-audit.jsonl"
    ensure_inside_vault(config.vault, log_path)
    line = json.dumps(dict(record), ensure_ascii=False, sort_keys=True)
    with open(log_path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return log_path


# --- 原子写入 ---------------------------------------------------------------


def write_text_atomic(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".edae-", suffix=".md.tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return path


def _is_ours(existing: Path, state_key: str) -> bool:
    """目标文件是否由本工具写入且归属同一 state_key。"""
    try:
        text = existing.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    try:
        fields, _ = parse_frontmatter(text)
    except SchemaError:
        return False
    return fields.get("生成器") == GENERATOR and fields.get("状态键") == state_key


def _pending_path(target: Path, content_hash: str) -> Path:
    """pending 候选的**唯一**路径：``<stem>.pending-<hash8>.md``。

    用内容哈希命名而不是 ``.pending-2`` / ``.pending-3`` 递增：
    同一份冲突内容重复归档时必须命中同一文件，第二次调用零新增。
    不同内容的冲突各自一个文件，这是它们本来就该分开。
    """
    suffix = target.suffix or ".md"
    return target.with_name(f"{target.stem}{PENDING_SUFFIX}-{content_hash[:8]}{suffix}")


def is_pending_filename(name: str) -> bool:
    """文件名是否是写入端留下的待合并候选（精确匹配）。

    只认两种形态：

    - ``<stem>.pending-<8位小写十六进制>.md``（当前格式）
    - ``<stem>.pending.md``（旧版格式）

    不能用 ``".pending" in stem`` 这种模糊判断：``release.pending-notes.md``
    是用户的普通笔记，被误判成候选后会在校验与检索里被静默跳过。
    """
    return bool(_PENDING_NAME_RE.match(name))


# --- 主入口 -----------------------------------------------------------------


def write_note(
    note: DistilledNote,
    session: NormalizedSession,
    config: AppConfig,
    dry_run: bool = False,
) -> WriteResult:
    """把蒸馏笔记写进 vault。返回实际发生的动作，绝不静默成功。

    「同一来源会话只有一篇正式笔记」是硬契约：若该 ``state_key`` 此前已写入过
    另一个路径（用户改了标题或项目名），本次**不会**再 created 第二篇，
    而是落一份指向旧笔记的 pending 候选等人工确认。
    """
    project = safe_component(note.project, "项目名")
    title = safe_component(note.title, "标题")

    archive_dir = ensure_inside_vault(config.vault, config.archive_dir)
    target = archive_dir / project / f"{title}.md"
    ensure_inside_vault(config.vault, target)
    ensure_inside_vault(archive_dir, target)

    content_hash = content_hash_of(note, session)
    state_key = state_key_of(session)
    today = _today().isoformat()

    state = _load_state(config)
    previous = _previous_path(config, state, state_key)

    # 同会话此前已归档到别的路径，且那篇还在：不新增正式笔记
    if previous is not None and previous != target and previous.exists():
        if _hash_of(previous) == content_hash:
            return WriteResult(previous, ACTION_SKIPPED, content_hash, dry_run)
        return _write_pending(
            target,
            previous,
            content_hash,
            note,
            session,
            config,
            dry_run,
            PENDING_REASON_MOVED,
        )

    if target.exists():
        if not _is_ours(target, state_key):
            return _write_pending(
                target,
                target,
                content_hash,
                note,
                session,
                config,
                dry_run,
                PENDING_REASON_OCCUPIED,
            )
        if _hash_of(target) == content_hash:
            return WriteResult(target, ACTION_SKIPPED, content_hash, dry_run)
        created = _created_of(target, today)
        text = render_note(note, session, content_hash, created, today, title, project)
        if dry_run:
            return WriteResult(target, ACTION_UPDATED, content_hash, True)
        write_text_atomic(target, text)
        _record_success(
            config, state, state_key, target, note, session, content_hash, today, ACTION_UPDATED
        )
        return WriteResult(target, ACTION_UPDATED, content_hash, False)

    text = render_note(note, session, content_hash, today, today, title, project)
    if dry_run:
        return WriteResult(target, ACTION_CREATED, content_hash, True)
    write_text_atomic(target, text)
    _record_success(
        config, state, state_key, target, note, session, content_hash, today, ACTION_CREATED
    )
    return WriteResult(target, ACTION_CREATED, content_hash, False)


def _previous_path(
    config: AppConfig, state: Mapping[str, Any], state_key: str
) -> Path | None:
    """state.json 里该 state_key 上次落盘的路径（越界则视为没有）。"""
    entries = state.get("entries")
    if not isinstance(entries, Mapping):
        return None
    entry = entries.get(state_key)
    if not isinstance(entry, Mapping):
        return None
    raw = entry.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return ensure_inside_vault(config.vault, config.vault / raw)
    except ConfigError:
        return None


def _hash_of(existing: Path) -> str | None:
    """读取文件里记录的 ``内容哈希``；读不了就返回 None（当作内容已变）。"""
    try:
        fields, _ = parse_frontmatter(existing.read_text(encoding="utf-8"))
    except (SchemaError, OSError, UnicodeDecodeError):
        return None
    value = fields.get("内容哈希")
    return value if isinstance(value, str) and value else None


def _created_of(existing: Path, fallback: str) -> str:
    try:
        fields, _ = parse_frontmatter(existing.read_text(encoding="utf-8"))
    except (SchemaError, OSError, UnicodeDecodeError):
        return fallback
    created = fields.get("created")
    return created if isinstance(created, str) and created else fallback


def _write_pending(
    target: Path,
    blocked_by: Path,
    content_hash: str,
    note: DistilledNote,
    session: NormalizedSession,
    config: AppConfig,
    dry_run: bool,
    reason: str,
) -> WriteResult:
    """目标位置不可写正式笔记：只落一份**可合并的 Markdown 候选**，绝不覆盖原文件。

    pending 不再写 JSON 指针——JSON 混在 ``.md`` 后缀里既不是笔记也不是数据，
    validator 会把它判成坏 frontmatter。现在 pending 就是一篇完整笔记：
    完整 frontmatter + 完整正文 + 顶部标注「待合并到 + 为什么」。
    """
    ensure_inside_vault(config.vault, config.archive_dir)
    chosen = _pending_path(target, content_hash)
    ensure_inside_vault(config.vault, chosen)
    ensure_inside_vault(config.archive_dir, chosen)

    if chosen.exists():
        # 同一份冲突内容重复归档：返回既有 pending，零新增、零覆盖
        if _pending_matches(chosen, content_hash, state_key_of(session)):
            return WriteResult(chosen, ACTION_PENDING, content_hash, dry_run)
        # 同名但内容不是本次归档（手工文件或哈希前缀冲突）：停下来，不猜、不覆盖
        raise ConfigError(
            "同名 pending 已存在且内容不是本次归档（可能是手工文件或哈希前缀冲突），"
            f"为避免混淆已停止写入：{chosen}"
        )

    if dry_run:
        return WriteResult(chosen, ACTION_PENDING, content_hash, True)

    today = _today().isoformat()
    try:
        blocked_display = blocked_by.relative_to(config.vault).as_posix()
    except ValueError:
        blocked_display = blocked_by.name
    text = render_pending(
        note,
        session,
        content_hash,
        today,
        blocked_display,
        reason,
        safe_component(note.title, "标题"),
        safe_component(note.project, "项目名"),
    )
    write_text_atomic(chosen, text)
    _append_audit(
        config,
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "action": ACTION_PENDING,
            "path": str(chosen),
            "content_hash": content_hash,
            "source": session.source,
            "状态键": state_key_of(session),
            "blocked_by": str(blocked_by),
            "reason": reason,
        },
    )
    return WriteResult(chosen, ACTION_PENDING, content_hash, False)


PENDING_REASON_OCCUPIED: Final = "目标笔记已存在且不是本工具生成，原笔记未被覆盖。"
PENDING_REASON_MOVED: Final = "同一来源会话此前已归档到另一路径（标题或项目名变化），未自动移动原笔记。"


def _pending_matches(path: Path, content_hash: str, state_key: str) -> bool:
    """既有 pending 是否就是本次这份内容（内容哈希与状态键都对得上）。"""
    try:
        fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    except (SchemaError, OSError, UnicodeDecodeError):
        return False
    return fields.get("内容哈希") == content_hash and fields.get("状态键") == state_key


def render_pending(
    note: DistilledNote,
    session: NormalizedSession,
    content_hash: str,
    today: str,
    blocked_display: str,
    reason: str,
    title: str | None = None,
    project: str | None = None,
) -> str:
    """渲染 pending 候选：完整 Markdown，顶部标出「待合并到」与原因。

    ``blocked_display`` 必须是 **相对 vault 的路径**：笔记会进 Obsidian，
    里面写绝对路径等于把本机目录结构抄进知识库。
    """
    full = render_note(note, session, content_hash, today, today, title, project)
    head, _, body = full.partition("\n\n")
    marker = "\n".join(
        [
            f"> **待合并到**：`{blocked_display}`",
            f"> **原因**：{reason}",
            "> **处理**：确认无误后把本文件内容并入目标笔记，再删除本文件；"
            "本工具不会自动合并、移动或删除任何笔记。",
        ]
    )
    return head + "\n\n" + marker + "\n\n" + body


def _record_success(
    config: AppConfig,
    state: dict[str, Any],
    state_key: str,
    target: Path,
    note: DistilledNote,
    session: NormalizedSession,
    content_hash: str,
    today: str,
    action: str,
) -> None:
    """落 state 与审计。

    ``action`` 由调用方传入真实的写入动作——这里无法从 state 反推，
    早期版本用 ``state_key not in {}`` 判断，恒为 created，是错的。
    """
    entries = state.setdefault("entries", {})
    safe_project = _scrub(note.project)
    safe_title = _scrub(note.title)
    entries[state_key] = {
        "path": target.relative_to(config.vault).as_posix(),
        "content_hash": content_hash,
        "source": session.source,
        "project": safe_project,
        "title": safe_title,
        "status": note.status,
        "content_volatility": note.content_volatility,
        "review_after": note.review_after,
        "updated": today,
    }
    ensure_inside_vault(config.vault, config.state_path)
    write_json_atomic(config.state_path, state)
    _append_audit(
        config,
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "path": str(target),
            "content_hash": content_hash,
            "source": session.source,
            "状态键": state_key,
            "project": safe_project,
            "status": note.status,
        },
    )


