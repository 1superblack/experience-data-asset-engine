"""Codex 会话适配器：``.jsonl`` 与 ``.jsonl.zst``。

Codex 的 rollout 文件是逐行 JSON 记录，混合了会话元信息、模型上下文、UI 事件和工具
输出。本适配器只做一件事：把对话消息抽成 ``NormalizedSession``，其余记录分类计数。

提取规则（唯一合法解释）：

- ``session_meta``：提供会话 ID、cwd、模型、起始时间。缺失即失败关闭。
- ``response_item`` + ``payload.type == "message"``：模型上下文里的对话消息。
- ``event_msg`` + ``payload.type`` 为 ``user_message`` / ``agent_message``：UI 事件流。
- ``task_complete``：只标记完成时间，不产生消息（避免和最后一条回复重复）。
- 工具调用与工具输出：**只计数，不进入消息列表**。
- 其它记录类型：按类型计数，不猜测其语义。

消息流选择：有 ``response_item`` 消息就用它；否则退回 ``event_msg``；两者都没有就失败关闭。
这样同一份文件里两套流并存时不会重复计数。

``.jsonl.zst`` 需要可选依赖 ``zstandard``。缺依赖、解压失败、被截断、非 UTF-8、超限
一律失败关闭——绝不降级为「按原文读」。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..errors import InputError, OptionalDependencyError, SchemaError, SessionFormatError
from ..models import Message, NormalizedSession, parse_iso_datetime

try:  # 可选依赖：只有读 .jsonl.zst 时才需要
    import zstandard
except ImportError:  # pragma: no cover - 由 test_missing_zstandard_fails_closed 覆盖
    zstandard = None  # type: ignore[assignment]

MAX_INPUT_BYTES = 64 * 1024 * 1024
READ_CHUNK = 64 * 1024
MAX_TITLE_CHARS = 40

SUPPORTED_SUFFIXES = (".jsonl", ".jsonl.zst")

TOOL_ITEM_TYPES = frozenset(
    {
        "function_call",
        "function_call_output",
        "local_shell_call",
        "local_shell_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "web_search_call",
        "reasoning",
    }
)

TOOL_EVENT_TYPES = frozenset(
    {
        "exec_command_begin",
        "exec_command_end",
        "patch_apply_begin",
        "patch_apply_end",
        "mcp_tool_call_begin",
        "mcp_tool_call_end",
        "web_search_begin",
        "web_search_end",
    }
)

MESSAGE_EVENT_ROLES = {"user_message": "user", "agent_message": "assistant"}
ROLE_MAP = {"user": "user", "assistant": "assistant", "system": "system", "developer": "system"}


def load_codex_session(path: Path) -> NormalizedSession:
    """读取一个 Codex 会话文件，返回标准化会话。"""
    source = Path(path)
    _check_suffix(source)
    text = _read_text(source)
    records = _parse_records(text, source)
    return _build_session(records, source)


# --- 读取 -------------------------------------------------------------------


def _check_suffix(path: Path) -> None:
    name = path.name.lower()
    if name.endswith(SUPPORTED_SUFFIXES):
        return
    raise SessionFormatError(
        f"不支持的文件后缀: {path.name}（只接受 .jsonl 或 .jsonl.zst）"
    )


def _read_text(path: Path) -> str:
    if not path.exists():
        raise InputError(f"文件不存在: {path}")
    if not path.is_file():
        raise InputError(f"输入不是文件: {path}")

    data = _decompress(path) if path.name.lower().endswith(".jsonl.zst") else _read_bytes(path)
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SessionFormatError(
            f"输入不是 UTF-8 编码: {path}（偏移 {exc.start} 字节）"
        ) from None


def _read_bytes(path: Path) -> bytes:
    try:
        size = path.stat().st_size
        if size > MAX_INPUT_BYTES:
            raise SessionFormatError(
                f"输入过大（{size} 字节 > 上限 {MAX_INPUT_BYTES} 字节）: {path}"
            )
        return path.read_bytes()
    except OSError as exc:
        raise InputError(f"无法读取文件: {path}（{exc}）") from exc


def _decompress(path: Path) -> bytes:
    if zstandard is None:
        raise OptionalDependencyError(
            "读取 .jsonl.zst 需要可选依赖 zstandard；"
            "请执行 pip install 'obsidian-knowledge-distiller[codex]'。"
            "不会降级为其它处理方式。"
        )
    try:
        with path.open("rb") as raw:
            reader = zstandard.ZstdDecompressor().stream_reader(raw, read_size=READ_CHUNK)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = reader.read(READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_INPUT_BYTES:
                    raise SessionFormatError(
                        f"解压后输入过大（超过 {MAX_INPUT_BYTES} 字节）: {path}"
                    )
                chunks.append(chunk)
    except zstandard.ZstdError as exc:
        raise SessionFormatError(
            f"zst 解压失败（文件损坏或被截断）: {path}（{exc}）"
        ) from exc
    except OSError as exc:
        raise InputError(f"无法读取文件: {path}（{exc}）") from exc
    return b"".join(chunks)


# --- 解析 -------------------------------------------------------------------


def _parse_records(text: str, path: Path) -> list[tuple[int, dict[str, Any]]]:
    records: list[tuple[int, dict[str, Any]]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SessionFormatError(
                f"{path} 第 {lineno} 行不是合法 JSON: {exc.msg}（第 {exc.colno} 列）"
            ) from None
        if not isinstance(record, dict):
            raise SessionFormatError(
                f"{path} 第 {lineno} 行必须是 JSON 对象，收到 {type(record).__name__}"
            )
        records.append((lineno, record))
    if not records:
        raise SessionFormatError(f"输入为空：没有可解析的记录: {path}")
    return records


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _normalize_role(role: Any) -> str | None:
    if not isinstance(role, str):
        return None
    return ROLE_MAP.get(role.strip().lower())


def _short_title(text: str, limit: int = MAX_TITLE_CHARS) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "..."


def _bump(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def _timestamp(record: dict[str, Any], lineno: int) -> tuple[datetime, str] | None:
    raw = record.get("timestamp")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise SessionFormatError(f"第 {lineno} 行 timestamp 必须是字符串")
    try:
        return parse_iso_datetime(raw, f"第 {lineno} 行 timestamp"), raw
    except SchemaError as exc:
        raise SessionFormatError(str(exc)) from None


def _build_session(
    records: list[tuple[int, dict[str, Any]]], path: Path
) -> NormalizedSession:
    meta: dict[str, Any] | None = None
    meta_timestamp: str | None = None
    item_messages: list[tuple[str, str, str | None]] = []
    event_messages: list[tuple[str, str, str | None]] = []
    ignored: dict[str, int] = {}
    tool_events = 0
    task_complete = False
    model: str | None = None
    cwd: str | None = None
    first_timestamp: str | None = None
    last_timestamp: str | None = None

    for lineno, record in records:
        stamp = _timestamp(record, lineno)
        raw_timestamp = stamp[1] if stamp else None
        if raw_timestamp is not None:
            first_timestamp = first_timestamp or raw_timestamp
            last_timestamp = raw_timestamp

        record_type = record.get("type")
        payload = record.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise SessionFormatError(f"第 {lineno} 行 payload 必须是 JSON 对象")

        if record_type == "session_meta":
            if meta is None:
                meta = payload
                payload_stamp = payload.get("timestamp")
                if payload_stamp is not None:
                    if not isinstance(payload_stamp, str):
                        raise SessionFormatError("session_meta.timestamp 必须是字符串")
                    try:
                        parse_iso_datetime(payload_stamp, "session_meta.timestamp")
                    except SchemaError as exc:
                        raise SessionFormatError(str(exc)) from None
                    meta_timestamp = payload_stamp
                else:
                    meta_timestamp = raw_timestamp
            cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else cwd
            model = payload.get("model") if isinstance(payload.get("model"), str) else model
        elif record_type == "response_item":
            item_type = payload.get("type")
            if item_type == "message":
                role = _normalize_role(payload.get("role"))
                if role is None:
                    _bump(ignored, f"response_item:role:{payload.get('role')}")
                else:
                    item_messages.append((role, _content_text(payload.get("content")), raw_timestamp))
            elif item_type in TOOL_ITEM_TYPES:
                tool_events += 1
            else:
                _bump(ignored, f"response_item:{item_type}")
        elif record_type == "event_msg":
            event_type = payload.get("type")
            if event_type in MESSAGE_EVENT_ROLES:
                text = payload.get("message")
                if isinstance(text, str):
                    event_messages.append(
                        (MESSAGE_EVENT_ROLES[event_type], text, raw_timestamp)
                    )
                else:
                    _bump(ignored, f"event_msg:{event_type}:no-message")
            elif event_type == "task_complete":
                task_complete = True
            elif event_type in TOOL_EVENT_TYPES:
                tool_events += 1
            else:
                _bump(ignored, f"event_msg:{event_type}")
        elif record_type == "turn_context":
            model = payload.get("model") if isinstance(payload.get("model"), str) else model
            cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else cwd
        else:
            _bump(ignored, str(record_type))

    if meta is None:
        raise SessionFormatError("缺少 session_meta 记录：无法确定会话 ID 与来源")
    session_id = meta.get("id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise SessionFormatError("session_meta 缺少 id：无法为会话生成稳定幂等键")

    if item_messages:
        stream = "response_item"
        chosen = item_messages
    elif event_messages:
        stream = "event_msg"
        chosen = event_messages
    else:
        raise SessionFormatError(
            "没有提取到任何对话消息：输入可能只含工具调用或系统事件，"
            "无法蒸馏为知识笔记"
        )

    created_at = meta_timestamp or first_timestamp
    updated_at = last_timestamp or created_at
    if created_at is None:
        raise SessionFormatError("缺少时间戳：无法确定会话时间范围")

    title = meta.get("title") if isinstance(meta.get("title"), str) else None
    if not title or not title.strip():
        first_user = next((text for role, text, _ in chosen if role == "user" and text.strip()), "")
        title = _short_title(first_user) or session_id.strip()

    metadata: dict[str, Any] = {
        "message_stream": stream,
        "tool_events": tool_events,
        "task_complete": task_complete,
        "record_count": len(records),
    }
    if item_messages and event_messages:
        metadata["event_messages_dropped"] = len(event_messages)
    if ignored:
        metadata["ignored_records"] = dict(sorted(ignored.items()))

    messages = tuple(
        Message(role=role, text=text, created_at=created_at_stamp)
        for role, text, created_at_stamp in chosen
    )

    try:
        return NormalizedSession.from_dict(
            {
                "schema_version": 1,
                "source": "codex",
                "session_id": session_id.strip(),
                "title": title.strip(),
                "created_at": created_at,
                "updated_at": updated_at,
                "model": model,
                "cwd": cwd,
                "messages": [message.to_dict() for message in messages],
                "metadata": metadata,
            }
        )
    except SchemaError as exc:
        raise SessionFormatError(f"会话结构不符合协议: {exc}") from None
