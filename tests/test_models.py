from __future__ import annotations

import json
from pathlib import Path

import pytest

from experience_data_asset_engine.adapters.normalized_json import load_normalized_json
from experience_data_asset_engine.errors import InputError, SchemaError
from experience_data_asset_engine.models import (
    NOISE_COUNTERS,
    STATUS_VALUES,
    VOLATILITY_VALUES,
    DistilledNote,
    Message,
    NoiseReport,
    NormalizedSession,
    parse_iso_datetime,
    state_key,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def valid_note_payload() -> dict:
    return json.loads((FIXTURES / "distilled-note.json").read_text(encoding="utf-8"))


def session_payload() -> dict:
    return json.loads((FIXTURES / "session.json").read_text(encoding="utf-8"))


# --- 协议常量 ---------------------------------------------------------------


def test_protocol_constants():
    assert STATUS_VALUES == ("有效", "待复核", "已过时", "已被取代")
    assert VOLATILITY_VALUES == ("stable", "evolving", "volatile", "progress")
    assert set(NOISE_COUNTERS) == {
        "input_messages",
        "kept_messages",
        "system_noise",
        "tool_noise",
        "duplicate_topics",
        "dead_ends",
        "redactions",
    }


# --- Message / NormalizedSession -------------------------------------------


def test_message_defaults():
    message = Message(role="user", text="hello")
    assert message.kind == "message"
    assert message.created_at is None


def test_message_rejects_unknown_role():
    with pytest.raises(SchemaError, match="role"):
        Message(role="hacker", text="x")


def test_message_rejects_unknown_kind():
    with pytest.raises(SchemaError, match="kind"):
        Message(role="user", text="x", kind="weird")


def test_message_rejects_bad_timestamp():
    with pytest.raises(SchemaError, match="created_at"):
        Message(role="user", text="x", created_at="2026/10/27 10:00")


def test_parse_iso_accepts_z_suffix():
    parsed = parse_iso_datetime("2026-10-27T02:00:00Z")
    assert parsed.utcoffset().total_seconds() == 0


def test_load_normalized_json(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "codex",
                "session_id": "s1",
                "title": "Test",
                "created_at": "2026-10-27T10:00:00+08:00",
                "updated_at": "2026-10-27T11:00:00+08:00",
                "messages": [{"role": "user", "text": "hello", "kind": "message"}],
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    assert load_normalized_json(path).messages[0].text == "hello"


def test_load_normalized_json_fixture():
    session = load_normalized_json(FIXTURES / "session.json")
    assert session.source == "codex"
    assert session.session_id == "s1"
    assert session.model == "demo-model"
    assert session.cwd == "/path/to/project"
    assert session.metadata == {"fixture": True}
    assert [message.role for message in session.messages] == ["user", "assistant"]


def test_session_requires_source_and_session_id():
    payload = session_payload()
    payload.pop("source")
    payload.pop("session_id")
    with pytest.raises(SchemaError) as exc:
        NormalizedSession.from_dict(payload)
    assert "source" in str(exc.value)
    assert "session_id" in str(exc.value)


def test_session_rejects_unsupported_schema_version():
    payload = session_payload()
    payload["schema_version"] = 2
    with pytest.raises(SchemaError, match="schema_version"):
        NormalizedSession.from_dict(payload)


def test_session_rejects_missing_messages():
    payload = session_payload()
    payload.pop("messages")
    with pytest.raises(SchemaError, match="messages"):
        NormalizedSession.from_dict(payload)


def test_session_rejects_messages_not_list():
    payload = session_payload()
    payload["messages"] = {"role": "user"}
    with pytest.raises(SchemaError, match="messages"):
        NormalizedSession.from_dict(payload)


def test_session_rejects_bad_message_item():
    payload = session_payload()
    payload["messages"] = [{"role": "user"}]
    with pytest.raises(SchemaError, match="text"):
        NormalizedSession.from_dict(payload)


def test_session_requires_timestamps():
    payload = session_payload()
    payload.pop("created_at")
    payload.pop("updated_at")
    with pytest.raises(SchemaError) as exc:
        NormalizedSession.from_dict(payload)
    assert "created_at" in str(exc.value)
    assert "updated_at" in str(exc.value)


def test_session_rejects_bad_timestamp_format():
    payload = session_payload()
    payload["updated_at"] = "yesterday"
    with pytest.raises(SchemaError, match="updated_at"):
        NormalizedSession.from_dict(payload)


def test_session_rejects_non_mapping_metadata():
    payload = session_payload()
    payload["metadata"] = ["nope"]
    with pytest.raises(SchemaError, match="metadata"):
        NormalizedSession.from_dict(payload)


def test_session_is_immutable():
    session = NormalizedSession.from_dict(session_payload())
    with pytest.raises(Exception):
        session.session_id = "other"  # type: ignore[misc]


def test_load_normalized_json_missing_file(tmp_path):
    with pytest.raises(InputError, match="不存在"):
        load_normalized_json(tmp_path / "nope.json")


def test_load_normalized_json_broken_json(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"schema_version": 1,', encoding="utf-8")
    with pytest.raises(SchemaError, match="第 1 行"):
        load_normalized_json(path)


def test_load_normalized_json_rejects_array(tmp_path):
    path = tmp_path / "array.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(SchemaError, match="对象"):
        load_normalized_json(path)


def test_load_normalized_json_tolerates_bom(tmp_path):
    path = tmp_path / "bom.json"
    payload = json.dumps(session_payload(), ensure_ascii=False)
    path.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
    assert load_normalized_json(path).session_id == "s1"


def test_session_to_dict_roundtrip():
    session = load_normalized_json(FIXTURES / "session.json")
    assert NormalizedSession.from_dict(session.to_dict()).to_dict() == session.to_dict()


def test_session_with_messages_keeps_header():
    session = load_normalized_json(FIXTURES / "session.json")
    trimmed = session.with_messages(session.messages[:1])
    assert trimmed.session_id == session.session_id
    assert len(trimmed.messages) == 1


# --- DistilledNote ---------------------------------------------------------


def test_distilled_note_rejects_summary_only():
    with pytest.raises(ValueError, match="decisions"):
        DistilledNote.from_dict({"title": "x", "summary": "only summary"})


def test_distilled_note_accepts_fixture():
    note = DistilledNote.from_dict(valid_note_payload())
    assert note.title == "会话归档协议与稳定字段"
    assert note.decisions[0].decision.startswith("先定")
    assert note.decisions[0].rejected[0].startswith("直接按各客户端格式")
    assert note.noise_report.input_messages == 210
    assert note.noise_report.kept_messages == 83
    assert note.content_volatility == "evolving"
    assert note.status == "有效"
    assert note.review_after == "2027-01-25"
    assert note.schema_version == 1


def test_distilled_note_to_dict_roundtrip():
    note = DistilledNote.from_dict(valid_note_payload())
    again = DistilledNote.from_dict(note.to_dict())
    assert again.to_dict() == note.to_dict()


def test_distilled_note_rejects_unknown_status():
    payload = valid_note_payload()
    payload["status"] = "大概有效"
    with pytest.raises(SchemaError, match="status"):
        DistilledNote.from_dict(payload)


def test_distilled_note_rejects_unknown_volatility():
    payload = valid_note_payload()
    payload["content_volatility"] = "sometimes"
    with pytest.raises(SchemaError, match="content_volatility"):
        DistilledNote.from_dict(payload)


@pytest.mark.parametrize("value", ["2027/01/25", "2027-13-01", "", "next quarter"])
def test_distilled_note_rejects_bad_review_after(value):
    payload = valid_note_payload()
    payload["review_after"] = value
    with pytest.raises(SchemaError, match="review_after"):
        DistilledNote.from_dict(payload)


@pytest.mark.parametrize("title", ["a/b", "a\\b", "a:b", ".hidden", "..", "trailing "])
def test_distilled_note_rejects_unsafe_title(title):
    payload = valid_note_payload()
    payload["title"] = title
    with pytest.raises(SchemaError, match="标题"):
        DistilledNote.from_dict(payload)


def test_distilled_note_rejects_windows_reserved_title():
    payload = valid_note_payload()
    payload["title"] = "CON"
    with pytest.raises(SchemaError, match="保留名"):
        DistilledNote.from_dict(payload)


def test_distilled_note_rejects_too_long_title():
    payload = valid_note_payload()
    payload["title"] = "长" * 121
    with pytest.raises(SchemaError, match="标题"):
        DistilledNote.from_dict(payload)


def test_distilled_note_requires_decision_reason():
    payload = valid_note_payload()
    payload["decisions"] = [{"decision": "只写结论"}]
    with pytest.raises(SchemaError, match="reason"):
        DistilledNote.from_dict(payload)


def test_distilled_note_rejects_empty_decisions():
    payload = valid_note_payload()
    payload["decisions"] = []
    with pytest.raises(SchemaError, match="decisions"):
        DistilledNote.from_dict(payload)


def test_distilled_note_rejects_blank_rejected_item():
    payload = valid_note_payload()
    payload["decisions"] = [{"decision": "A", "reason": "B", "rejected": ["  "]}]
    with pytest.raises(SchemaError, match="rejected"):
        DistilledNote.from_dict(payload)


def test_distilled_note_allows_empty_optional_lists():
    payload = valid_note_payload()
    payload["risks"] = []
    payload["todo"] = []
    note = DistilledNote.from_dict(payload)
    assert note.risks == ()


def test_noise_report_requires_all_counters():
    payload = valid_note_payload()
    payload["noise_report"] = {"input_messages": 10}
    with pytest.raises(SchemaError) as exc:
        DistilledNote.from_dict(payload)
    assert "kept_messages" in str(exc.value)


def test_noise_report_rejects_negative_values():
    payload = valid_note_payload()
    payload["noise_report"]["redactions"] = -1
    with pytest.raises(SchemaError, match="redactions"):
        DistilledNote.from_dict(payload)


def test_noise_report_rejects_kept_greater_than_input():
    payload = valid_note_payload()
    payload["noise_report"]["kept_messages"] = 999
    with pytest.raises(SchemaError, match="kept_messages"):
        DistilledNote.from_dict(payload)


def test_noise_report_standalone_validation():
    report = NoiseReport(input_messages=5, kept_messages=2, system_noise=1, tool_noise=1, duplicate_topics=0, dead_ends=0, redactions=1)
    assert report.input_messages == 5


def test_distilled_note_rejects_empty_required_text():
    payload = valid_note_payload()
    payload["background"] = "   "
    with pytest.raises(SchemaError, match="background"):
        DistilledNote.from_dict(payload)


def test_distilled_note_rejects_non_mapping():
    with pytest.raises(SchemaError, match="对象"):
        DistilledNote.from_dict(["not", "a", "dict"])  # type: ignore[arg-type]


# --- session_id 作为标识而不是路径 -------------------------------------------


@pytest.mark.parametrize(
    "session_id",
    [
        "a/b",
        "a\\b",
        "../../outside",
        "..",
        ".",
        "/etc/passwd",
        "C:\\Windows\\system32",
        "a\nb",
        "a\tb",
        "a\x00b",
        "a\x1bb",
        "",
        "   ",
        "C:evil",
    ],
)
def test_session_rejects_unsafe_session_id(session_id):
    payload = session_payload()
    payload["session_id"] = session_id
    with pytest.raises(SchemaError, match="session_id"):
        NormalizedSession.from_dict(payload)


def test_session_rejects_overlong_session_id():
    payload = session_payload()
    payload["session_id"] = "x" * 201
    with pytest.raises(SchemaError, match="200"):
        NormalizedSession.from_dict(payload)


def test_session_accepts_opaque_session_id():
    payload = session_payload()
    payload["session_id"] = "01JC8ZK4T9XQ7M3V6B2N8R5D1F"
    assert NormalizedSession.from_dict(payload).session_id == "01JC8ZK4T9XQ7M3V6B2N8R5D1F"


def test_state_key_is_stable_and_path_safe():
    session = load_normalized_json(FIXTURES / "session.json")
    key = session.state_key
    assert key == state_key("codex", "s1")
    assert len(key) == 64
    assert all(char in "0123456789abcdef" for char in key)
    assert "/" not in key and "\\" not in key and ".." not in key
    assert state_key("codex", "s1") == state_key("codex", "s1")
    assert state_key("codex", "s1") != state_key("claude", "s1")


def test_state_key_derives_from_source_and_session_id():
    first = state_key("codex", "../../outside")
    second = state_key("codex", "../../outside")
    assert first == second
    assert "outside" not in first


# --- project 作为归档目录名 ---------------------------------------------------


@pytest.mark.parametrize(
    "project",
    [
        "数据资产引擎",
        "示例工程 二期",
        "2026 Q1 - 项目A",
        "项目（一期）",
        "obsidian-vault",
        "A.B.C",
    ],
)
def test_distilled_note_accepts_safe_project(project):
    payload = valid_note_payload()
    payload["project"] = project
    assert DistilledNote.from_dict(payload).project == project


@pytest.mark.parametrize(
    "project",
    [
        "..",
        ".",
        "../outside",
        "a/b",
        "a\\b",
        "/abs",
        "C:\\Windows",
        ".hidden",
        "CON",
        "com1",
        "trailing ",
        "trailing.",
        "",
        "   ",
        "a\nb",
        "a\tb",
        "a\x00b",
        "a:b",
        "a|b",
        "长" * 81,
    ],
)
def test_distilled_note_rejects_unsafe_project(project):
    payload = valid_note_payload()
    payload["project"] = project
    with pytest.raises(SchemaError, match="project"):
        DistilledNote.from_dict(payload)


# --- title 的写入安全 --------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "第一行\n第二行",
        "制表\t符",
        "空字符\x00",
        "回车\r符",
        "转义\x1b符",
        "结尾点.",
        "结尾空格 ",
    ],
)
def test_distilled_note_rejects_control_or_trailing_title(title):
    payload = valid_note_payload()
    payload["title"] = title
    with pytest.raises(SchemaError, match="标题"):
        DistilledNote.from_dict(payload)


# --- 被否决方案 -------------------------------------------------------------


def test_distilled_note_rejects_when_every_rejected_is_empty():
    payload = valid_note_payload()
    payload["decisions"] = [
        {"decision": "方案 A", "reason": "理由 A", "rejected": []},
        {"decision": "方案 B", "reason": "理由 B"},
    ]
    with pytest.raises(SchemaError, match="rejected"):
        DistilledNote.from_dict(payload)


def test_distilled_note_accepts_one_non_empty_rejected():
    payload = valid_note_payload()
    payload["decisions"] = [
        {"decision": "方案 A", "reason": "理由 A", "rejected": []},
        {"decision": "方案 B", "reason": "理由 B", "rejected": ["方案 C：成本过高"]},
    ]
    assert DistilledNote.from_dict(payload).decisions[1].rejected == ("方案 C：成本过高",)
