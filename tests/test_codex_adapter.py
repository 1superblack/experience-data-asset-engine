from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from obsidian_knowledge_distiller.adapters import codex_jsonl
from obsidian_knowledge_distiller.adapters.codex_jsonl import load_codex_session
from obsidian_knowledge_distiller.errors import (
    InputError,
    OptionalDependencyError,
    SessionFormatError,
)

try:
    import zstandard
except ImportError:  # 可选依赖，没装也能跑大部分测试
    zstandard = None  # type: ignore[assignment]

needs_zstd = pytest.mark.skipif(
    zstandard is None, reason="需要 zstandard 才能现场生成 .zst 样本"
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PLAIN = FIXTURES / "codex-session.jsonl"
ARCHIVED = FIXTURES / "codex-session.jsonl.zst"


# --- 正常路径 ---------------------------------------------------------------


def test_load_codex_jsonl_session():
    session = load_codex_session(PLAIN)
    assert session.session_id == "019abc"
    assert [m.role for m in session.messages] == ["user", "assistant"]
    assert session.messages[0].text == "把这次会话整理成知识笔记"


@needs_zstd
def test_load_codex_zst_session(tmp_path):
    out = tmp_path / "codex-session.jsonl.zst"
    with PLAIN.open("rb") as source, out.open("wb") as target:
        with zstandard.ZstdCompressor().stream_writer(target) as writer:
            writer.write(source.read())
    assert load_codex_session(out).session_id == "019abc"


def test_load_codex_stored_zst_fixture():
    session = load_codex_session(ARCHIVED)
    assert session.session_id == "019abc"
    assert [m.role for m in session.messages] == ["user", "assistant"]


def test_session_header_fields():
    session = load_codex_session(PLAIN)
    assert session.source == "codex"
    assert session.model == "demo-model"
    assert session.cwd == "/path/to/project"
    assert session.created_at.startswith("2026-10-27T02:00:00")
    assert session.updated_at.startswith("2026-10-27T02:00:40")
    assert session.metadata["message_stream"] == "response_item"
    assert session.metadata["task_complete"] is True


def test_event_stream_fixture_falls_back_to_event_messages():
    session = load_codex_session(FIXTURES / "codex-session-event.jsonl")
    assert session.session_id == "019def"
    assert [m.role for m in session.messages] == ["user", "assistant"]
    assert session.metadata["message_stream"] == "event_msg"


def test_tool_calls_are_counted_not_emitted():
    session = load_codex_session(PLAIN)
    assert session.metadata["tool_events"] == 3
    joined = "\n".join(message.text for message in session.messages)
    assert "function_call_output" not in joined
    assert "call_1" not in joined


def test_unknown_records_are_counted():
    session = load_codex_session(PLAIN)
    assert session.metadata["ignored_records"]["event_msg:token_count"] == 1


def test_title_derived_from_first_user_message():
    session = load_codex_session(PLAIN)
    assert session.title.startswith("把这次会话整理成知识笔记")


def test_session_survives_roundtrip():
    session = load_codex_session(PLAIN)
    assert session.to_dict()["session_id"] == "019abc"


def test_uppercase_zst_extension_is_recognized(tmp_path):
    target = tmp_path / "SESSION.JSONL.ZST"
    shutil.copyfile(ARCHIVED, target)
    assert load_codex_session(target).session_id == "019abc"


# --- 失败关闭 ---------------------------------------------------------------


def test_missing_file_raises_input_error(tmp_path):
    with pytest.raises(InputError, match="不存在"):
        load_codex_session(tmp_path / "nope.jsonl")


def test_directory_input_raises_input_error(tmp_path):
    target = tmp_path / "session.jsonl"
    target.mkdir()
    with pytest.raises(InputError, match="不是文件"):
        load_codex_session(target)


def test_empty_file_fails_closed(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(SessionFormatError, match="为空"):
        load_codex_session(path)


def test_blank_lines_are_ignored(tmp_path):
    path = tmp_path / "blank.jsonl"
    path.write_text("\n\n" + PLAIN.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    assert load_codex_session(path).session_id == "019abc"


def test_missing_session_meta_fails_closed(tmp_path):
    path = tmp_path / "no-meta.jsonl"
    lines = [
        line
        for line in PLAIN.read_text(encoding="utf-8").splitlines()
        if '"session_meta"' not in line
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(SessionFormatError, match="session_meta"):
        load_codex_session(path)


def test_session_meta_without_id_fails_closed(tmp_path):
    path = tmp_path / "no-id.jsonl"
    path.write_text(
        '{"timestamp":"2026-10-27T02:00:00Z","type":"session_meta","payload":{"cwd":"/x"}}\n'
        '{"timestamp":"2026-10-27T02:00:01Z","type":"response_item",'
        '"payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}}\n',
        encoding="utf-8",
    )
    with pytest.raises(SessionFormatError, match="缺少 id"):
        load_codex_session(path)


def test_no_messages_fails_closed(tmp_path):
    path = tmp_path / "no-messages.jsonl"
    path.write_text(
        '{"timestamp":"2026-10-27T02:00:00Z","type":"session_meta","payload":{"id":"019xyz"}}\n'
        '{"timestamp":"2026-10-27T02:00:01Z","type":"event_msg","payload":{"type":"token_count"}}\n',
        encoding="utf-8",
    )
    with pytest.raises(SessionFormatError, match="消息"):
        load_codex_session(path)


def test_invalid_json_line_reports_line_number(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text(
        '{"timestamp":"2026-10-27T02:00:00Z","type":"session_meta","payload":{"id":"019xyz"}}\n'
        '{"type": "response_item",\n',
        encoding="utf-8",
    )
    with pytest.raises(SessionFormatError, match="第 2 行"):
        load_codex_session(path)


def test_non_object_line_fails_closed(tmp_path):
    path = tmp_path / "array-line.jsonl"
    path.write_text('["nope"]\n', encoding="utf-8")
    with pytest.raises(SessionFormatError, match="JSON 对象"):
        load_codex_session(path)


def test_non_object_payload_fails_closed(tmp_path):
    path = tmp_path / "bad-payload.jsonl"
    path.write_text(
        '{"timestamp":"2026-10-27T02:00:00Z","type":"session_meta","payload":{"id":"019xyz"}}\n'
        '{"timestamp":"2026-10-27T02:00:01Z","type":"event_msg","payload":[]}\n',
        encoding="utf-8",
    )
    with pytest.raises(SessionFormatError, match="payload"):
        load_codex_session(path)


def test_invalid_timestamp_fails_closed(tmp_path):
    path = tmp_path / "bad-time.jsonl"
    path.write_text(
        '{"timestamp":"yesterday","type":"session_meta","payload":{"id":"019xyz"}}\n'
        '{"timestamp":"2026-10-27T02:00:01Z","type":"response_item",'
        '"payload":{"type":"message","role":"user","content":[{"type":"text","text":"hi"}]}}\n',
        encoding="utf-8",
    )
    with pytest.raises(SessionFormatError, match="时间"):
        load_codex_session(path)


def test_non_string_timestamp_fails_closed(tmp_path):
    path = tmp_path / "numeric-time.jsonl"
    path.write_text('{"timestamp":123,"type":"session_meta","payload":{"id":"019xyz"}}\n', encoding="utf-8")
    with pytest.raises(SessionFormatError, match="timestamp"):
        load_codex_session(path)


def test_non_utf8_fails_closed(tmp_path):
    path = tmp_path / "latin1.jsonl"
    path.write_bytes(
        b'{"timestamp":"2026-10-27T02:00:00Z","type":"session_meta","payload":{"id":"\xff\xfe"}}\n'
    )
    with pytest.raises(SessionFormatError, match="UTF-8"):
        load_codex_session(path)


def test_oversized_input_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_jsonl, "MAX_INPUT_BYTES", 32)
    with pytest.raises(SessionFormatError, match="过大"):
        load_codex_session(PLAIN)


def test_missing_zstandard_fails_closed(monkeypatch):
    monkeypatch.setattr(codex_jsonl, "zstandard", None)
    with pytest.raises(OptionalDependencyError, match="zstandard"):
        load_codex_session(ARCHIVED)


@needs_zstd
def test_corrupt_zst_fails_closed(tmp_path):
    path = tmp_path / "broken.jsonl.zst"
    path.write_bytes(b"not a zstd frame at all")
    with pytest.raises(SessionFormatError, match="zst"):
        load_codex_session(path)


@needs_zstd
def test_truncated_zst_fails_closed(tmp_path):
    data = ARCHIVED.read_bytes()
    path = tmp_path / "truncated.jsonl.zst"
    path.write_bytes(data[: len(data) // 2])
    with pytest.raises(SessionFormatError, match="zst"):
        load_codex_session(path)


def test_unsupported_suffix_fails_closed(tmp_path):
    path = tmp_path / "session.txt"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(SessionFormatError, match="后缀"):
        load_codex_session(path)
