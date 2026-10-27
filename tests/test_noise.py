from __future__ import annotations

import json
from pathlib import Path

import pytest

from experience_data_asset_engine.adapters.normalized_json import load_normalized_json
from experience_data_asset_engine.models import Message, NormalizedSession
from experience_data_asset_engine.noise import (
    MAX_KEEP_CHARS,
    REDACTED,
    TRUNCATION_MARKER,
    filter_noise,
    redact_sensitive,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_session(messages: list[dict], session_id: str = "s-noise") -> NormalizedSession:
    return NormalizedSession.from_dict(
        {
            "schema_version": 1,
            "source": "manual",
            "session_id": session_id,
            "title": "降噪测试",
            "created_at": "2026-10-27T09:00:00+08:00",
            "updated_at": "2026-10-27T09:10:00+08:00",
            "messages": messages,
            "metadata": {},
        }
    )


def msg(role: str, text: str, kind: str = "message") -> dict:
    return {"role": role, "text": text, "kind": kind}


def dropped(session: NormalizedSession) -> dict[str, int]:
    _, report = filter_noise(session)
    return report.to_dict()


# --- 只读语义 ---------------------------------------------------------------


def test_filter_does_not_mutate_input():
    session = make_session(
        [
            msg("user", "<app-context>env</app-context>"),
            msg("user", "真正的需求：把降噪拆成四层。"),
            msg("tool", "x" * 800, kind="tool_result"),
        ]
    )
    before = [(message.role, message.text, message.kind) for message in session.messages]

    cleaned, _ = filter_noise(session)

    after = [(message.role, message.text, message.kind) for message in session.messages]
    assert before == after
    assert cleaned is not session
    assert cleaned.messages is not session.messages
    assert len(cleaned.messages) == 1


def test_filter_is_deterministic_and_idempotent_on_clean_input():
    session = make_session([msg("user", "把统计口径写进文档。")])
    first, first_report = filter_noise(session)
    second, second_report = filter_noise(first)
    assert first.to_dict() == second.to_dict()
    assert first_report.kept_messages == 1
    assert second_report.kept_messages == 1
    assert second_report.system_noise == 0


def test_empty_session_yields_zero_report():
    cleaned, report = filter_noise(make_session([]))
    assert cleaned.messages == ()
    assert report.to_dict() == {
        "input_messages": 0,
        "kept_messages": 0,
        "system_noise": 0,
        "tool_noise": 0,
        "duplicate_topics": 0,
        "dead_ends": 0,
        "redactions": 0,
    }


def test_counter_invariant_holds():
    session = load_normalized_json(FIXTURES / "noisy-session.json")
    cleaned, report = filter_noise(session)
    total = (
        report.kept_messages
        + report.system_noise
        + report.tool_noise
        + report.duplicate_topics
        + report.dead_ends
    )
    assert total == report.input_messages == len(session.messages)
    assert report.kept_messages == len(cleaned.messages)


# --- 第一层：输入噪音 -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "<app-context>\nworkspace: /path/to/project\n</app-context>",
        "<environment_context> win32 </environment_context>",
        "# Skills instructions\n- 先读记忆再动手。",
        "Permission instructions: 允许读取项目目录。",
        "This is the system prompt. 你是执行助手。",
    ],
)
def test_injection_messages_are_dropped(text):
    report = dropped(make_session([msg("user", text)]))
    assert report["system_noise"] == 1
    assert report["kept_messages"] == 0


def test_plain_mention_of_tag_name_is_kept():
    """只是把 app-context 当名词讨论，不是注入，不该被杀。"""
    session = make_session([msg("user", "把 app-context 这类环境注入过滤掉，别写进笔记。")])
    cleaned, report = filter_noise(session)
    assert report.system_noise == 0
    assert len(cleaned.messages) == 1


def test_system_role_is_dropped():
    report = dropped(make_session([msg("system", "你是一个严谨的工程师。")]))
    assert report["system_noise"] == 1


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_blank_messages_are_dropped(text):
    report = dropped(make_session([msg("assistant", text)]))
    assert report["system_noise"] == 1


@pytest.mark.parametrize("text", ["好", "好的。", "嗯", "收到", "ok", "OK.", "行。"])
def test_acknowledgements_are_dropped(text):
    report = dropped(make_session([msg("user", text)]))
    assert report["system_noise"] == 1


def test_sentence_starting_with_ack_word_is_kept():
    session = make_session([msg("user", "好的，那我们把截断阈值调到 4000 字符，顺便补个测试。")])
    _, report = filter_noise(session)
    assert report.system_noise == 0
    assert report.kept_messages == 1


def test_consecutive_echo_counts_as_input_noise():
    text = "把四层降噪写进 noise.py，并补上正向与负向测试。"
    report = dropped(make_session([msg("user", text), msg("user", text)]))
    assert report["system_noise"] == 1
    assert report["duplicate_topics"] == 0
    assert report["kept_messages"] == 1


# --- 第二层：执行噪音 -------------------------------------------------------


@pytest.mark.parametrize("kind", ["tool_call", "tool_result"])
def test_tool_kinds_are_dropped(kind):
    report = dropped(make_session([msg("assistant", "{\"tool\": \"shell\"}", kind=kind)]))
    assert report["tool_noise"] == 1
    assert report["kept_messages"] == 0


def test_tool_role_is_dropped():
    report = dropped(make_session([msg("tool", "total 12\ndrwxr-xr-x 5 demo demo 4096")]))
    assert report["tool_noise"] == 1


def test_long_tool_output_is_dropped():
    text = "Traceback (most recent call last):\n" + "\n".join(
        f"line-{index:04d} " + "x" * 80 for index in range(20)
    )
    assert len(text) > 600
    report = dropped(make_session([msg("user", text)]))
    assert report["tool_noise"] == 1


def test_short_traceback_paste_is_kept():
    """短报错是人写的有效信息，不是工具噪声。"""
    text = "Traceback (most recent call last):\n  File \"x.py\", line 1\nValueError: bad"
    assert len(text) < 600
    session = make_session([msg("user", text)])
    cleaned, report = filter_noise(session)
    assert report.tool_noise == 0
    assert len(cleaned.messages) == 1


# --- 第三层：知识噪音 -------------------------------------------------------


def test_repeated_topic_far_apart_is_dropped():
    topic = "降噪统计的七个计数器必须与真实过滤结果一致，否则报告不可信。"
    session = make_session(
        [
            msg("user", topic),
            msg("assistant", "同意，先把计数口径写成不变式，再写实现。"),
            msg("user", topic),
        ]
    )
    _, report = filter_noise(session)
    assert report.duplicate_topics == 1
    assert report.kept_messages == 2


def test_near_duplicate_same_prefix_and_similar_length_is_dropped():
    first = "把 staging 文件名改成 state_key 派生值，原始 session_id 绝对不能进路径。" + "（第一版）"
    second = "把 staging 文件名改成 state_key 派生值，原始 session_id 绝对不能进路径。" + "（第二版）"
    session = make_session([msg("user", first), msg("assistant", "记下了。"), msg("user", second)])
    _, report = filter_noise(session)
    assert report.duplicate_topics == 1


def test_similar_prefix_with_very_different_length_is_kept():
    first = "把 staging 文件名改成 state_key 派生值，原始 session_id 绝对不能进路径。"
    second = (
        "把 staging 文件名改成 state_key 派生值，原始 session_id 绝对不能进路径，"
        "并且 staging 目录本身也要做符号链接逃逸校验，避免写到 vault 外面去。"
    )
    session = make_session([msg("user", first), msg("assistant", "记下了。"), msg("user", second)])
    _, report = filter_noise(session)
    assert report.duplicate_topics == 0
    assert report.kept_messages == 3


@pytest.mark.parametrize(
    "text",
    ["失败了", "还是不行", "报错，看不懂", "又失败了", "没用", "这条死路走不通", "跑不起来"],
)
def test_dead_end_short_messages_are_dropped(text):
    report = dropped(make_session([msg("assistant", text)]))
    assert report["dead_ends"] == 1
    assert report["kept_messages"] == 0


def test_failure_with_conclusion_is_kept():
    text = "失败是因为缓存没清，改成先清缓存就好了。"
    _, report = filter_noise(make_session([msg("assistant", text)]))
    assert report.dead_ends == 0
    assert report.kept_messages == 1


def test_long_failure_report_is_kept():
    text = "失败了，不过我把失败前后的日志对比了一遍，发现是并发写入导致的顺序错乱。"
    _, report = filter_noise(make_session([msg("assistant", text)]))
    assert report.dead_ends == 0


# --- 第四层：安全噪音 -------------------------------------------------------


def test_redacts_common_secret_patterns():
    text, count = redact_sensitive("key sk-abcdefghijklmnop token=supersecret")
    assert "sk-abcdefghijklmnop" not in text
    assert "supersecret" not in text
    assert count == 2


def test_redacts_private_key_block():
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA1234567890abcdef\n"
        "-----END RSA PRIVATE KEY-----"
    )
    redacted, count = redact_sensitive(text)
    assert "MIIEowIBAAKCAQEA" not in redacted
    assert redacted == REDACTED
    assert count == 1


@pytest.mark.parametrize(
    "text, secret",
    [
        ("Authorization: Bearer abcdef1234567890", "abcdef1234567890"),
        ("cookie: session=demo-session-value-1234", "demo-session-value-1234"),
        ("password=Hunter2Hunter2", "Hunter2Hunter2"),
        ("access_token=abcd1234efgh5678", "abcd1234efgh5678"),
        ("api_key=sk-live-1234567890abcdef", "sk-live-1234567890abcdef"),
    ],
)
def test_redacts_header_and_credential_shapes(text, secret):
    redacted, count = redact_sensitive(text)
    assert secret not in redacted
    assert count >= 1


def test_redaction_count_matches_actual_replacements():
    text = "先记两个：password=alpha123 和 token=beta456789"
    redacted, count = redact_sensitive(text)
    assert count == 2
    assert "alpha123" not in redacted
    assert "beta456789" not in redacted


def test_plain_text_is_not_redacted():
    text = "把 token 这个字段名写进文档，但不要写具体值。"
    redacted, count = redact_sensitive(text)
    assert redacted == text
    assert count == 0


def test_no_secret_survives_in_kept_messages():
    session = load_normalized_json(FIXTURES / "noisy-session.json")
    cleaned, report = filter_noise(session)
    joined = "\n".join(message.text for message in cleaned.messages)
    assert "abcdef1234567890" not in joined
    assert report.redactions == 1
    assert REDACTED in joined


def test_long_message_truncated_after_redaction():
    secret = "sk-abcdefghijklmnop1234"
    text = f"先记一下 {secret}。" + "正文" * 3000
    cleaned, report = filter_noise(make_session([msg("user", text)]))
    kept = cleaned.messages[0].text
    assert secret not in kept
    assert REDACTED in kept
    assert kept.endswith(TRUNCATION_MARKER)
    assert len(kept) <= MAX_KEEP_CHARS + len(TRUNCATION_MARKER)
    assert report.redactions == 1


# --- 200 条样本 -------------------------------------------------------------


def test_200_message_sample_statistics():
    session = load_normalized_json(FIXTURES / "noisy-session-200.json")
    cleaned, report = filter_noise(session)

    assert report.input_messages == 200
    assert report.kept_messages == 45
    assert report.system_noise == 55
    assert report.tool_noise == 50
    assert report.duplicate_topics == 30
    assert report.dead_ends == 20
    assert report.redactions == 4
    assert (
        report.kept_messages
        + report.system_noise
        + report.tool_noise
        + report.duplicate_topics
        + report.dead_ends
        == 200
    )


def test_200_message_sample_keeps_no_bulk_noise():
    session = load_normalized_json(FIXTURES / "noisy-session-200.json")
    cleaned, _ = filter_noise(session)

    joined = "\n".join(message.text for message in cleaned.messages)
    assert "<app-context>" not in joined
    assert "Traceback (most recent call last)" not in joined
    assert "sk-" not in joined
    assert "Hunter2Hunter2" not in joined
    assert all(len(message.text) <= MAX_KEEP_CHARS + len(TRUNCATION_MARKER) for message in cleaned.messages)

    input_chars = sum(len(message.text) for message in session.messages)
    kept_chars = sum(len(message.text) for message in cleaned.messages)
    assert input_chars > 40_000
    assert kept_chars < 3_000, f"保留内容仍然过大: {kept_chars}"
    assert kept_chars / input_chars < 0.1


def test_200_message_sample_is_synthetic():
    payload = json.loads((FIXTURES / "noisy-session-200.json").read_text(encoding="utf-8"))
    assert payload["metadata"]["synthetic"] is True
    assert len(payload["messages"]) == 200


def test_kept_messages_preserve_relative_order():
    session = make_session(
        [
            msg("user", "第一件事：先写协议。"),
            msg("user", "好"),
            msg("assistant", "第二件事：再写实现。"),
            msg("user", "第三件事：最后补测试。"),
        ]
    )
    cleaned, _ = filter_noise(session)
    assert [message.text for message in cleaned.messages] == [
        "第一件事：先写协议。",
        "第二件事：再写实现。",
        "第三件事：最后补测试。",
    ]
    assert [message.role for message in cleaned.messages] == ["user", "assistant", "user"]


def test_message_objects_are_replaced_not_reused_for_redaction():
    """脱敏后必须是新 Message 对象，原对象文本不变。"""
    secret = "token=abcdef123456"
    original = Message(role="user", text=secret)
    session = make_session([{"role": "user", "text": secret}])
    cleaned, _ = filter_noise(session)
    assert original.text == secret
    assert cleaned.messages[0].text != secret
    assert REDACTED in cleaned.messages[0].text
