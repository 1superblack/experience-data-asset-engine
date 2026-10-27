"""四层降噪与隐私脱敏。

设计约束（与 ``references/noise-policy.md`` 一致）：

1. 只读：绝不修改传入的 ``NormalizedSession``，返回新对象；原始输入文件不动。
2. 可审计：每条丢弃消息只进一个计数器，满足
   ``input_messages == kept + system_noise + tool_noise + duplicate_topics + dead_ends``。
3. 失败关闭：宁可少留，也不把系统上下文、工具大输出、凭据送进蒸馏。
4. 脱敏按**实际替换次数**计数，并按规则顺序执行，避免同一段文本被重复计数。

四层：

- 输入噪音（system_noise）：系统角色、环境注入标记、空白、纯确认、连续回显。
- 执行噪音（tool_noise）：工具角色、工具调用/输出、长日志式内容。
- 知识噪音（duplicate_topics / dead_ends）：重复主题、显式失败短消息。
- 安全噪音（redactions）：密钥、令牌、口令、Cookie、私钥，统一替换为 ``[已脱敏]``。

语义级冲突（「这个结论后来被推翻了」）**不在本层判断**——那需要理解对话，交给 Agent 蒸馏阶段。
这里只做确定性识别，口径写在 noise-policy.md。
"""

from __future__ import annotations

import re
from dataclasses import replace
from difflib import SequenceMatcher
from typing import Final

from .models import Message, NoiseReport, NormalizedSession

REDACTED: Final = "[已脱敏]"

#: 保留消息的最大长度。超长内容会截断——这不是过滤，是防止单条消息撑爆提示词。
MAX_KEEP_CHARS: Final = 4000
TRUNCATION_MARKER: Final = "\n…[已截断]"

#: 死路识别只看很短的消息（<= 30 字符），避免把「失败了，不过我发现是 X」这类有效复盘误杀。
MAX_DEAD_END_CHARS: Final = 30

# --- 输入噪音 ---------------------------------------------------------------

_INJECTION_MARKERS: Final = (
    "<app-context>",
    "</app-context>",
    "<environment_context>",
    "<user_info>",
    "<system-reminder>",
    "<permissions>",
)

_INJECTION_START_RE: Final = re.compile(
    r"(?i)^\s*(?:#{1,6}\s*)?(?:skills?\s+instructions?|permission\s+instructions?|system\s+prompt)\b"
)
_INJECTION_INLINE_RE: Final = re.compile(
    r"(?i)\b(this is the (?:system|developer) prompt|following skills? are available)\b"
)

_ACK_RE: Final = re.compile(
    r"(?i)^[\s\"'“”]*(?:好(?:的|滴|吧)?|嗯+|哦+|行|可以|收到|明白|了解|继续|谢谢|感谢|"
    r"ok(?:ay)?|yes|no|sure|thanks|continue|go on|got it)[\s。．.!！?？~～,，\"'“”]*$"
)

# --- 执行噪音 ---------------------------------------------------------------

_TOOL_OUTPUT_MARKERS: Final = (
    re.compile(r"(?m)^\s*Traceback \(most recent call last\):"),
    re.compile(r"(?m)^\s*exit code: -?\d+\s*$"),
    re.compile(r"(?m)^\s*\$ \S"),
    re.compile(r"(?s)^\s*```[a-zA-Z0-9_-]*\n.*\n```\s*$"),
    re.compile(r"(?i)\bstdout\b[\s\S]{0,40}\bstderr\b"),
)
_TOOL_OUTPUT_MIN_CHARS: Final = 600

# --- 知识噪音 ---------------------------------------------------------------

_DEAD_END_RE: Final = re.compile(
    r"(?i)^\s*(?:失败|报错|还是不行|仍然失败|又失败|不行|没用|无效|白试|放弃|"
    r"跑不起来|不生效|没效果|走不通|"
    r"(?:这条|这个|那种)?死路|(?:一点)?用都没有|"
    r"试了.{0,20}(?:不行|失败|报错|没用)|尝试.{0,20}(?:不行|失败|报错)|"
    r"failed|doesn'?t work|no luck)"
)
_CONCLUSION_RE: Final = re.compile(
    r"(?:因为|所以|原因|结论|改成|改为|修复|解决|最后|于是|不过|但是|其实|发现|换成)"
)

_DUPLICATE_MIN_CHARS: Final = 40
_DUPLICATE_LENGTH_RATIO: Final = 0.9
_DUPLICATE_SIMILARITY: Final = 0.9
_DUPLICATE_COMPARE_CHARS: Final = 400

# --- 安全噪音 ---------------------------------------------------------------

_REDACTION_RULES: Final = (
    # PEM 私钥块必须最先处理，否则内部的 base64 会被后续规则切碎
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.S,
        ),
        REDACTED,
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"), REDACTED),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{8,}"), f"Bearer {REDACTED}"),
    (re.compile(r"(?i)\bcookie\s*:\s*[^\r\n]+"), f"Cookie: {REDACTED}"),
    (re.compile(r"(?i)\bauthorization\s*:\s*[^\r\n]+"), f"Authorization: {REDACTED}"),
    (
        re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*[\"']?[^\s\"',;]{3,}[\"']?"),
        rf"\1={REDACTED}",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|"
            r"client[_-]?secret|secret[_-]?key)\s*[:=]\s*[\"']?[A-Za-z0-9._-]{6,}[\"']?"
        ),
        rf"\1={REDACTED}",
    ),
)


def redact_sensitive(text: str) -> tuple[str, int]:
    """把敏感值替换为 ``[已脱敏]``，返回 (新文本, 实际替换次数)。

    计数按 ``subn`` 的真实替换次数累加，不按「命中了几条规则」虚报。
    """
    if not isinstance(text, str):
        return "", 0
    total = 0
    for pattern, replacement in _REDACTION_RULES:
        text, count = pattern.subn(replacement, text)
        total += count
    return text, total


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _is_injected(text: str) -> bool:
    stripped = text.strip()
    if any(marker in stripped for marker in _INJECTION_MARKERS):
        return True
    if _INJECTION_START_RE.match(stripped):
        return True
    return bool(_INJECTION_INLINE_RE.search(stripped))


def _is_acknowledgement(text: str) -> bool:
    return bool(_ACK_RE.match(text))


def _looks_like_tool_output(text: str) -> bool:
    if len(text) < _TOOL_OUTPUT_MIN_CHARS:
        return False
    return any(pattern.search(text) for pattern in _TOOL_OUTPUT_MARKERS)


def _is_dead_end(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_DEAD_END_CHARS:
        return False
    if _CONCLUSION_RE.search(stripped):
        return False
    return bool(_DEAD_END_RE.match(stripped))


def _is_duplicate(normalized: str, kept_norms: list[str]) -> bool:
    """确定性重复判定：完全相同，或长度接近且相似度 >= 0.9。

    相似度用 ``difflib`` 而不是「前缀相同」——前缀规则在短文本上会被尾部差异骗过去
    （「…进路径（第一版）」和「…进路径（第二版）」前缀长度正好覆盖全文）。
    """
    if not normalized:
        return False
    for previous in kept_norms:
        if previous == normalized:
            return True
        if len(normalized) < _DUPLICATE_MIN_CHARS or len(previous) < _DUPLICATE_MIN_CHARS:
            continue
        shorter, longer = sorted((normalized, previous), key=len)
        if len(shorter) / len(longer) < _DUPLICATE_LENGTH_RATIO:
            continue
        ratio = SequenceMatcher(
            None,
            shorter[:_DUPLICATE_COMPARE_CHARS],
            longer[:_DUPLICATE_COMPARE_CHARS],
        ).ratio()
        if ratio >= _DUPLICATE_SIMILARITY:
            return True
    return False


def _clamp(text: str) -> str:
    if len(text) <= MAX_KEEP_CHARS:
        return text
    return text[:MAX_KEEP_CHARS] + TRUNCATION_MARKER


def _is_input_noise(message: Message, text: str, normalized: str, previous: str | None) -> bool:
    if message.role == "system" or message.kind == "system":
        return True
    if not normalized or _is_injected(text) or _is_acknowledgement(text):
        return True
    return previous is not None and normalized == previous


def _is_execution_noise(message: Message, text: str) -> bool:
    if message.role == "tool" or message.kind in {"tool_call", "tool_result"}:
        return True
    return _looks_like_tool_output(text)


def filter_noise(session: NormalizedSession) -> tuple[NormalizedSession, NoiseReport]:
    """四层降噪。返回 (新会话, 统计)，原会话保持不变。"""
    messages = session.messages
    kept: list[Message] = []
    kept_norms: list[str] = []
    system_noise = tool_noise = duplicate_topics = dead_ends = redactions = 0
    previous_norm: str | None = None

    for message in messages:
        text = message.text if isinstance(message.text, str) else ""
        normalized = _normalize(text)

        if _is_input_noise(message, text, normalized, previous_norm):
            system_noise += 1
        elif _is_execution_noise(message, text):
            tool_noise += 1
        elif _is_duplicate(normalized, kept_norms):
            duplicate_topics += 1
        elif _is_dead_end(text):
            dead_ends += 1
        else:
            redacted, count = redact_sensitive(text)
            redactions += count
            kept.append(replace(message, text=_clamp(redacted)))
            kept_norms.append(normalized)

        previous_norm = normalized

    report = NoiseReport(
        input_messages=len(messages),
        kept_messages=len(kept),
        system_noise=system_noise,
        tool_noise=tool_noise,
        duplicate_topics=duplicate_topics,
        dead_ends=dead_ends,
        redactions=redactions,
    )
    return session.with_messages(tuple(kept)), report
