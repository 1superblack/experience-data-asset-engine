"""数据协议：标准化会话与蒸馏笔记。

两份协议是整套工具唯一的契约面：

- ``NormalizedSession``：来源无关的会话中间格式，所有适配器（Codex、Markdown 等）
  都先落到这里。
- ``DistilledNote``：写笔记的唯一合法输入。缺 ``decisions`` 直接拒绝——只写摘要不算
  蒸馏结果。

字段定义与 ``references/output-schema.md`` 一一对应，改字段必须同时升
``schema_version``。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .errors import SchemaError

SCHEMA_VERSION = 1

MESSAGE_ROLES = ("user", "assistant", "system", "tool")
MESSAGE_KINDS = ("message", "tool_call", "tool_result", "system", "summary")

STATUS_VALUES = ("有效", "待复核", "已过时", "已被取代")
STATUS_VALID = STATUS_VALUES[0]

VOLATILITY_VALUES = ("stable", "evolving", "volatile", "progress")
VOLATILITY_LABELS = {
    "stable": "稳定",
    "evolving": "演进",
    "volatile": "易变",
    "progress": "进度",
}

NOISE_COUNTERS = (
    "input_messages",
    "kept_messages",
    "system_noise",
    "tool_noise",
    "duplicate_topics",
    "dead_ends",
    "redactions",
)

MAX_TITLE_LENGTH = 120
_UNSAFE_TITLE_CHARS = set('<>:"/\\|?*')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def parse_iso_datetime(value: str, field_name: str = "时间") -> datetime:
    """解析 ISO 8601 字符串；失败抛 SchemaError（带字段名）。"""
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{field_name} 必须是非空 ISO 8601 字符串")
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise SchemaError(f"{field_name} 不是合法的 ISO 8601 时间: {value!r}") from None


def _require_object(data: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise SchemaError(f"{label} 必须是 JSON 对象，收到 {type(data).__name__}")
    return data


def _text_field(
    data: Mapping[str, Any],
    key: str,
    issues: list[str],
    *,
    required: bool = True,
    label: str | None = None,
) -> str | None:
    name = label or key
    value = data.get(key)
    if value is None:
        if required:
            issues.append(f"缺少必填字段 {name}")
        return None
    if not isinstance(value, str):
        issues.append(f"{name} 必须是字符串")
        return None
    stripped = value.strip()
    if not stripped:
        issues.append(f"{name} 不能为空")
        return None
    return stripped


def _string_list(
    data: Mapping[str, Any],
    key: str,
    issues: list[str],
    *,
    required: bool = True,
) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        if required:
            issues.append(f"缺少必填字段 {key}（字符串数组）")
        return ()
    if not isinstance(value, list):
        issues.append(f"{key} 必须是字符串数组")
        return ()
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            issues.append(f"{key}[{index}] 必须是字符串")
            continue
        stripped = item.strip()
        if not stripped:
            issues.append(f"{key}[{index}] 不能为空")
            continue
        items.append(stripped)
    return tuple(items)


def _raise_issues(prefix: str, issues: list[str]) -> None:
    if issues:
        raise SchemaError(f"{prefix}: " + "；".join(issues))


# --- 会话 -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    text: str
    created_at: str | None = None
    kind: str = "message"

    def __post_init__(self) -> None:
        issues: list[str] = []
        if self.role not in MESSAGE_ROLES:
            issues.append(f"role 必须是 {'/'.join(MESSAGE_ROLES)} 之一，收到 {self.role!r}")
        if self.kind not in MESSAGE_KINDS:
            issues.append(f"kind 必须是 {'/'.join(MESSAGE_KINDS)} 之一，收到 {self.kind!r}")
        if not isinstance(self.text, str):
            issues.append("text 必须是字符串")
        if self.created_at is not None:
            parse_iso_datetime(self.created_at, "created_at")
        _raise_issues("消息不符合协议", issues)

    @classmethod
    def from_dict(cls, data: Any, index: int = 0) -> "Message":
        label = f"messages[{index}]"
        payload = _require_object(data, label)
        issues: list[str] = []
        text = payload.get("text")
        if not isinstance(text, str):
            issues.append(f"{label}.text 必须存在且为字符串")
        role = payload.get("role")
        if role not in MESSAGE_ROLES:
            issues.append(f"{label}.role 必须是 {'/'.join(MESSAGE_ROLES)} 之一")
        kind = payload.get("kind", "message")
        if kind not in MESSAGE_KINDS:
            issues.append(f"{label}.kind 必须是 {'/'.join(MESSAGE_KINDS)} 之一")
        created_at = _text_field(payload, "created_at", issues, required=False, label=f"{label}.created_at")
        if created_at is not None:
            try:
                parse_iso_datetime(created_at, f"{label}.created_at")
            except SchemaError as exc:
                issues.append(str(exc))
        _raise_issues("消息不符合协议", issues)
        return cls(role=role, text=text, created_at=created_at, kind=kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "text": self.text,
            "created_at": self.created_at,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class NormalizedSession:
    schema_version: int
    source: str
    session_id: str
    title: str
    created_at: str
    updated_at: str
    messages: tuple[Message, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    model: str | None = None
    cwd: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> "NormalizedSession":
        payload = _require_object(data, "标准化会话")
        issues: list[str] = []

        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            issues.append(f"schema_version 必须是 {SCHEMA_VERSION}，收到 {version!r}")

        source = _text_field(payload, "source", issues)
        session_id = _text_field(payload, "session_id", issues)
        title = _text_field(payload, "title", issues)
        model = _text_field(payload, "model", issues, required=False)
        cwd = _text_field(payload, "cwd", issues, required=False)

        created_at = _text_field(payload, "created_at", issues)
        updated_at = _text_field(payload, "updated_at", issues)
        for key, value in (("created_at", created_at), ("updated_at", updated_at)):
            if value is None:
                continue
            try:
                parse_iso_datetime(value, key)
            except SchemaError as exc:
                issues.append(str(exc))

        messages: tuple[Message, ...] = ()
        raw_messages = payload.get("messages")
        if raw_messages is None:
            issues.append("缺少必填字段 messages（数组）")
        elif not isinstance(raw_messages, list):
            issues.append("messages 必须是数组")
        else:
            built: list[Message] = []
            for index, item in enumerate(raw_messages):
                try:
                    built.append(Message.from_dict(item, index))
                except SchemaError as exc:
                    issues.append(str(exc))
            messages = tuple(built)

        metadata = payload.get("metadata", {})
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, Mapping):
            issues.append("metadata 必须是 JSON 对象")

        _raise_issues("标准化会话不符合协议", issues)
        return cls(
            schema_version=SCHEMA_VERSION,
            source=source or "",
            session_id=session_id or "",
            title=title or "",
            created_at=created_at or "",
            updated_at=updated_at or "",
            messages=messages,
            metadata=dict(metadata),
            model=model,
            cwd=cwd,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "session_id": self.session_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "model": self.model,
            "cwd": self.cwd,
            "messages": [message.to_dict() for message in self.messages],
            "metadata": dict(self.metadata),
        }

    def with_messages(self, messages: tuple[Message, ...]) -> "NormalizedSession":
        """派生一个只替换消息列表的会话（降噪阶段使用）。"""
        return NormalizedSession(
            schema_version=self.schema_version,
            source=self.source,
            session_id=self.session_id,
            title=self.title,
            created_at=self.created_at,
            updated_at=self.updated_at,
            messages=messages,
            metadata=dict(self.metadata),
            model=self.model,
            cwd=self.cwd,
        )


# --- 蒸馏笔记 ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NoiseReport:
    input_messages: int
    kept_messages: int
    system_noise: int
    tool_noise: int
    duplicate_topics: int
    dead_ends: int
    redactions: int

    def __post_init__(self) -> None:
        issues: list[str] = []
        for name in NOISE_COUNTERS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                issues.append(f"noise_report.{name} 必须是整数")
            elif value < 0:
                issues.append(f"noise_report.{name} 不能为负数")
        if not issues and self.kept_messages > self.input_messages:
            issues.append(
                "noise_report.kept_messages 不能大于 input_messages"
                f"（{self.kept_messages} > {self.input_messages}）"
            )
        _raise_issues("降噪统计不符合协议", issues)

    @classmethod
    def from_dict(cls, data: Any) -> "NoiseReport":
        payload = _require_object(data, "noise_report")
        issues: list[str] = []
        values: dict[str, int] = {}
        for name in NOISE_COUNTERS:
            value = payload.get(name)
            if value is None:
                issues.append(f"noise_report 缺少字段 {name}")
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                issues.append(f"noise_report.{name} 必须是整数")
                continue
            values[name] = value
        _raise_issues("降噪统计不符合协议", issues)
        return cls(**values)

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in NOISE_COUNTERS}


@dataclass(frozen=True, slots=True)
class Decision:
    decision: str
    reason: str
    rejected: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Any, index: int = 0) -> "Decision":
        label = f"decisions[{index}]"
        payload = _require_object(data, label)
        issues: list[str] = []
        decision = _text_field(payload, "decision", issues, label=f"{label}.decision")
        reason = _text_field(payload, "reason", issues, label=f"{label}.reason")
        rejected = _string_list(payload, "rejected", issues, required=False)
        _raise_issues("决策不符合协议", issues)
        return cls(decision=decision or "", reason=reason or "", rejected=rejected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "rejected": list(self.rejected),
        }


def _validate_title(value: Any, issues: list[str]) -> str | None:
    if not isinstance(value, str):
        issues.append("标题 title 必须是字符串")
        return None
    if not value or not value.strip():
        issues.append("标题 title 不能为空")
        return None
    if value != value.strip():
        issues.append("标题 title 首尾不能有空白")
        return None
    if len(value) > MAX_TITLE_LENGTH:
        issues.append(f"标题 title 长度不能超过 {MAX_TITLE_LENGTH} 个字符")
        return None
    bad_chars = sorted({char for char in value if char in _UNSAFE_TITLE_CHARS})
    if bad_chars:
        issues.append(f"标题 title 不能包含文件名非法字符: {' '.join(bad_chars)}")
        return None
    if value.startswith("."):
        issues.append("标题 title 不能以点开头（会生成隐藏文件）")
        return None
    if value.split(".")[0].upper() in _WINDOWS_RESERVED:
        issues.append(f"标题 title 不能使用 Windows 保留名: {value}")
        return None
    return value


def _validate_review_after(value: Any, issues: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        issues.append("review_after 必须是 YYYY-MM-DD 日期字符串")
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        issues.append(f"review_after 不是合法日期（YYYY-MM-DD）: {value!r}")
        return None


@dataclass(frozen=True, slots=True)
class DistilledNote:
    title: str
    project: str
    summary: str
    background: str
    diagnosis: tuple[str, ...]
    decisions: tuple[Decision, ...]
    evidence: tuple[str, ...]
    reusable: tuple[str, ...]
    risks: tuple[str, ...]
    todo: tuple[str, ...]
    noise_report: NoiseReport
    content_volatility: str
    status: str
    review_after: str
    schema_version: int = SCHEMA_VERSION

    @property
    def volatility_label(self) -> str:
        return VOLATILITY_LABELS[self.content_volatility]

    @classmethod
    def from_dict(cls, data: Any) -> "DistilledNote":
        payload = _require_object(data, "蒸馏笔记")
        issues: list[str] = []

        version = payload.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            issues.append(f"schema_version 必须是 {SCHEMA_VERSION}，收到 {version!r}")

        title = _validate_title(payload.get("title"), issues)
        project = _text_field(payload, "project", issues)
        summary = _text_field(payload, "summary", issues)
        background = _text_field(payload, "background", issues)
        diagnosis = _string_list(payload, "diagnosis", issues)
        evidence = _string_list(payload, "evidence", issues)
        reusable = _string_list(payload, "reusable", issues)
        risks = _string_list(payload, "risks", issues)
        todo = _string_list(payload, "todo", issues)

        decisions: tuple[Decision, ...] = ()
        raw_decisions = payload.get("decisions")
        if raw_decisions is None:
            issues.append("缺少必填字段 decisions（至少 1 条决策与理由）")
        elif not isinstance(raw_decisions, list):
            issues.append("decisions 必须是数组")
        elif not raw_decisions:
            issues.append("decisions 不能为空：只写摘要不算蒸馏结果")
        else:
            built: list[Decision] = []
            for index, item in enumerate(raw_decisions):
                try:
                    built.append(Decision.from_dict(item, index))
                except SchemaError as exc:
                    issues.append(str(exc))
            decisions = tuple(built)

        noise_report: NoiseReport | None = None
        raw_noise = payload.get("noise_report")
        if raw_noise is None:
            issues.append("缺少必填字段 noise_report（降噪统计）")
        else:
            try:
                noise_report = NoiseReport.from_dict(raw_noise)
            except SchemaError as exc:
                issues.append(str(exc))

        volatility = payload.get("content_volatility")
        if volatility not in VOLATILITY_VALUES:
            issues.append(
                f"content_volatility 必须是 {'/'.join(VOLATILITY_VALUES)} 之一，收到 {volatility!r}"
            )
            volatility = None

        status = payload.get("status")
        if status not in STATUS_VALUES:
            issues.append(f"status 必须是 {'/'.join(STATUS_VALUES)} 之一，收到 {status!r}")
            status = None

        review_after = _validate_review_after(payload.get("review_after"), issues)

        _raise_issues("蒸馏笔记不符合协议", issues)
        return cls(
            title=title or "",
            project=project or "",
            summary=summary or "",
            background=background or "",
            diagnosis=diagnosis,
            decisions=decisions,
            evidence=evidence,
            reusable=reusable,
            risks=risks,
            todo=todo,
            noise_report=noise_report,
            content_volatility=volatility,
            status=status,
            review_after=review_after or "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "title": self.title,
            "project": self.project,
            "summary": self.summary,
            "background": self.background,
            "diagnosis": list(self.diagnosis),
            "decisions": [decision.to_dict() for decision in self.decisions],
            "evidence": list(self.evidence),
            "reusable": list(self.reusable),
            "risks": list(self.risks),
            "todo": list(self.todo),
            "noise_report": self.noise_report.to_dict(),
            "content_volatility": self.content_volatility,
            "status": self.status,
            "review_after": self.review_after,
        }
