"""Task 10：Markdown 手动导入适配器。

样本全部合成，不读真实会话。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from experience_data_asset_engine.adapters import markdown as markdown_module
from experience_data_asset_engine.adapters.markdown import (
    MAX_MARKDOWN_BYTES,
    load_markdown_session,
)
from experience_data_asset_engine.errors import InputError, SchemaError, SessionFormatError

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE = FIXTURES / "manual-session.md"
FIXED_NOW = datetime(2026, 10, 27, 9, 30, tzinfo=timezone.utc)


def write(path: Path, text: str, *, encoding: str = "utf-8", newline: str = "\n") -> Path:
    path.write_text(text.replace("\n", newline), encoding=encoding)
    return path


def basic(title: str = "示例会话", user: str = "需求", assistant: str = "回答") -> str:
    return f"# {title}\n\n## 用户\n{user}\n\n## 助手\n{assistant}\n"


# --- 正向 -------------------------------------------------------------------


def test_loads_fixture_session():
    session = load_markdown_session(SAMPLE, now=FIXED_NOW)

    assert session.title == "手动整理的会话：降噪阈值要不要放宽"
    assert session.source == "manual"
    assert [message.role for message in session.messages[:2]] == ["user", "assistant"]
    assert len(session.messages) == 6
    assert [message.role for message in session.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert session.messages[0].text.startswith("我把 200 条合成样本跑了一遍")
    assert session.created_at == "2026-10-27T09:30:00+00:00"


def test_source_defaults_to_manual(tmp_path: Path):
    path = write(tmp_path / "session.md", basic())
    assert load_markdown_session(path, now=FIXED_NOW).source == "manual"


@pytest.mark.parametrize("alias", ["用户", "User", "user", "HUMAN", "  Human  "])
def test_user_aliases(tmp_path: Path, alias: str):
    path = write(tmp_path / "s.md", f"# 标题\n\n## {alias}\n正文\n")
    session = load_markdown_session(path, now=FIXED_NOW)
    assert session.messages[0].role == "user"


@pytest.mark.parametrize("alias", ["助手", "Assistant", "assistant", "Codex", "Claude", "AI"])
def test_assistant_aliases(tmp_path: Path, alias: str):
    path = write(tmp_path / "s.md", f"# 标题\n\n## 用户\n问\n\n## {alias}\n答\n")
    session = load_markdown_session(path, now=FIXED_NOW)
    assert [message.role for message in session.messages] == ["user", "assistant"]


def test_title_whitespace_is_tolerated(tmp_path: Path):
    path = write(tmp_path / "s.md", "#   会话标题  \n\n## 用户\n正文\n")
    assert load_markdown_session(path, now=FIXED_NOW).title == "会话标题"


def test_bom_and_crlf_are_supported(tmp_path: Path):
    path = tmp_path / "s.md"
    path.write_bytes(b"\xef\xbb\xbf" + basic().replace("\n", "\r\n").encode("utf-8"))

    session = load_markdown_session(path, now=FIXED_NOW)

    assert session.title == "示例会话"
    assert session.messages[0].text == "需求"


def test_code_block_headings_are_not_roles(tmp_path: Path):
    text = (
        "# 标题\n\n## 用户\n看这段代码：\n\n```markdown\n## 助手\n伪标题\n```\n\n## 助手\n真的回答\n"
    )
    path = write(tmp_path / "s.md", text)

    session = load_markdown_session(path, now=FIXED_NOW)

    assert [message.role for message in session.messages] == ["user", "assistant"]
    assert "## 助手\n伪标题" in session.messages[0].text, "围栏内容要原样保留在用户段落里"


def test_tilde_fence_is_respected(tmp_path: Path):
    text = "# 标题\n\n## 用户\n~~~\n## 助手\n伪标题\n~~~\n\n## 助手\n回答\n"
    path = write(tmp_path / "s.md", text)
    session = load_markdown_session(path, now=FIXED_NOW)
    assert [message.role for message in session.messages] == ["user", "assistant"]


def test_session_id_is_content_derived_and_stable(tmp_path: Path):
    first = write(tmp_path / "a.md", basic())
    second = write(tmp_path / "b.md", basic())

    left = load_markdown_session(first, now=FIXED_NOW)
    right = load_markdown_session(second, now=FIXED_NOW)

    expected = hashlib.sha256(first.read_bytes()).hexdigest()[:16]
    assert left.session_id == f"md-{expected}"
    assert left.session_id == right.session_id, "同内容不同路径应得到同一会话 ID"
    assert left.state_key == right.state_key


def test_mtime_is_used_when_now_is_omitted(tmp_path: Path):
    path = write(tmp_path / "s.md", basic())
    session = load_markdown_session(path)
    assert session.created_at.startswith(
        datetime.fromtimestamp(path.stat().st_mtime).astimezone().strftime("%Y-%m-%dT%H:%M")
    )


def test_input_file_is_not_modified(tmp_path: Path):
    path = write(tmp_path / "s.md", basic())
    before = path.read_bytes()

    load_markdown_session(path, now=FIXED_NOW)

    assert path.read_bytes() == before


def test_markdown_is_not_written_anywhere(tmp_path: Path):
    root = tmp_path / "work"
    root.mkdir()
    path = write(root / "s.md", basic())

    load_markdown_session(path, now=FIXED_NOW)

    assert sorted(item.name for item in root.iterdir()) == ["s.md"]


# --- 反向：结构问题一律失败关闭 -------------------------------------------------


def test_unknown_role_reports_line_number(tmp_path: Path):
    path = write(tmp_path / "s.md", "# 标题\n\n## 观察者\n内容\n")

    with pytest.raises(SessionFormatError) as err:
        load_markdown_session(path, now=FIXED_NOW)

    assert "第 3 行" in str(err.value)
    assert "观察者" in str(err.value)
    assert "不会把正文当成 assistant" in str(err.value)


def test_missing_title_fails(tmp_path: Path):
    path = write(tmp_path / "s.md", "## 用户\n只有正文\n")
    with pytest.raises(SessionFormatError, match="缺少一级标题"):
        load_markdown_session(path, now=FIXED_NOW)


def test_no_role_sections_fails(tmp_path: Path):
    path = write(tmp_path / "s.md", "# 只有标题\n")
    with pytest.raises(SessionFormatError, match="没有任何角色段落"):
        load_markdown_session(path, now=FIXED_NOW)


def test_empty_file_fails(tmp_path: Path):
    path = write(tmp_path / "s.md", "")
    with pytest.raises(SessionFormatError):
        load_markdown_session(path, now=FIXED_NOW)


def test_empty_section_fails(tmp_path: Path):
    path = write(tmp_path / "s.md", "# 标题\n\n## 用户\n\n## 助手\n回答\n")
    with pytest.raises(SessionFormatError, match="内容为空"):
        load_markdown_session(path, now=FIXED_NOW)


def test_text_before_first_role_fails(tmp_path: Path):
    path = write(tmp_path / "s.md", "# 标题\n\n游离正文\n\n## 用户\n问\n")
    with pytest.raises(SessionFormatError, match="第 3 行"):
        load_markdown_session(path, now=FIXED_NOW)


def test_second_h1_fails(tmp_path: Path):
    path = write(tmp_path / "s.md", "# 标题\n\n## 用户\n问\n\n# 又一个标题\n")
    with pytest.raises(SessionFormatError, match="第二个一级标题"):
        load_markdown_session(path, now=FIXED_NOW)


def test_h1_after_sections_fails(tmp_path: Path):
    path = write(tmp_path / "s.md", "## 用户\n问\n\n# 迟到的标题\n")
    with pytest.raises(SessionFormatError, match="之前"):
        load_markdown_session(path, now=FIXED_NOW)


def test_missing_file_fails(tmp_path: Path):
    with pytest.raises(InputError, match="不存在"):
        load_markdown_session(tmp_path / "nope.md", now=FIXED_NOW)


def test_directory_input_fails(tmp_path: Path):
    target = tmp_path / "dir.md"
    target.mkdir()
    with pytest.raises(InputError, match="不是文件"):
        load_markdown_session(target, now=FIXED_NOW)


def test_non_utf8_fails(tmp_path: Path):
    path = tmp_path / "s.md"
    path.write_bytes(b"# \xff\xfe\n\n## user\nx\n")
    with pytest.raises(SessionFormatError, match="UTF-8"):
        load_markdown_session(path, now=FIXED_NOW)


def test_oversized_input_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(markdown_module, "MAX_MARKDOWN_BYTES", 64)
    path = write(tmp_path / "s.md", basic(user="长" * 200))

    with pytest.raises(SessionFormatError, match="过大"):
        load_markdown_session(path, now=FIXED_NOW)


def test_default_size_limit_is_reasonable():
    assert MAX_MARKDOWN_BYTES == 2 * 1024 * 1024


@pytest.mark.parametrize("source", ["Manual Import", "手工", "", "manual import", "a" * 41])
def test_invalid_source_is_rejected(tmp_path: Path, source: str):
    path = write(tmp_path / "s.md", basic())
    with pytest.raises(SchemaError, match="source"):
        load_markdown_session(path, source=source, now=FIXED_NOW)


def test_source_with_hyphen_is_accepted(tmp_path: Path):
    path = write(tmp_path / "s.md", basic())
    assert load_markdown_session(path, source="manual-import", now=FIXED_NOW).source == (
        "manual-import"
    )


def test_pending_suffix_file_can_still_be_imported(tmp_path: Path):
    """导入只管解析，不管文件名语义：pending 文件也能当输入（但不会被当成笔记）。"""
    path = write(tmp_path / "s.pending-abcdef12.md", basic())
    assert load_markdown_session(path, now=FIXED_NOW).title == "示例会话"


# --- 代码围栏状态机（Gate 4 最终修复） ----------------------------------------

BT = chr(96)      # 反引号
TL = chr(126)     # 波浪号
NL = chr(10)      # 换行（避免在测试源码里写转义）


def fence_times(char: str, count: int) -> str:
    return char * count


def test_four_backtick_fence_keeps_three_backtick_line_inside(tmp_path: Path):
    """四反引号块里出现三反引号：不是关闭，后续内容仍在块内。"""
    text = NL.join(
        [
            "# 标题",
            "",
            "## 用户",
            fence_times(BT, 4) + "markdown",
            fence_times(BT, 3),
            "## 助手",
            "这不是角色",
            fence_times(BT, 3),
            fence_times(BT, 4),
            "",
            "## 助手",
            "真正回答",
            "",
        ]
    )
    session = load_markdown_session(write(tmp_path / "s.md", text), now=FIXED_NOW)

    assert [m.role for m in session.messages] == ["user", "assistant"]
    body = session.messages[0].text
    assert fence_times(BT, 4) + "markdown" in body
    assert "这不是角色" in body, "三反引号行之后的内容仍属于用户代码块"
    assert fence_times(BT, 4) in body
    assert session.messages[1].text == "真正回答"


def test_four_backtick_fence_is_not_closed_by_three_backticks(tmp_path: Path):
    """四反引号开启、三反引号结尾：不得提前关闭，`## 助手` 仍是正文。"""
    text = NL.join(
        [
            "# 标题",
            "## 用户",
            fence_times(BT, 4),
            "内容 A",
            fence_times(BT, 3),
            "## 助手",
            "内容 B",
            fence_times(BT, 4),
            "",
        ]
    )
    session = load_markdown_session(write(tmp_path / "s.md", text), now=FIXED_NOW)

    assert len(session.messages) == 1, "围栏内不应产生 assistant 消息"
    assert session.messages[0].role == "user"
    assert "## 助手" in session.messages[0].text
    assert "内容 B" in session.messages[0].text


def test_closing_fence_may_be_longer_than_opening(tmp_path: Path):
    """关闭用更长的同字符围栏是允许的（CommonMark 行为）。"""
    text = NL.join(
        ["# 标题", "## 用户", fence_times(BT, 3), "块内", fence_times(BT, 5), "", "## 助手", "答", ""]
    )
    session = load_markdown_session(write(tmp_path / "s.md", text), now=FIXED_NOW)

    assert [m.role for m in session.messages] == ["user", "assistant"]
    assert "块内" in session.messages[0].text


def test_tilde_fence_nesting_ignores_backticks(tmp_path: Path):
    """波浪号围栏里出现反引号围栏：都不关闭，直到波浪号关闭行。"""
    text = NL.join(
        [
            "# 标题",
            "## 用户",
            fence_times(TL, 4),
            fence_times(BT, 3) + "python",
            "## 助手",
            "伪标题",
            fence_times(BT, 3),
            fence_times(TL, 4),
            "",
            "## 助手",
            "真实回答",
            "",
        ]
    )
    session = load_markdown_session(write(tmp_path / "s.md", text), now=FIXED_NOW)

    assert [m.role for m in session.messages] == ["user", "assistant"]
    assert "伪标题" in session.messages[0].text
    assert session.messages[1].text == "真实回答"


def test_all_heading_shapes_inside_fence_are_body(tmp_path: Path):
    """围栏内 `## 用户` / `## 助手` / 未知二级标题都不是标题。"""
    text = NL.join(
        [
            "# 标题",
            "## 用户",
            fence_times(BT, 4),
            "## 用户",
            "## 助手",
            "## Unknown",
            fence_times(BT, 4),
            "",
            "## 助手",
            "答",
            "",
        ]
    )
    session = load_markdown_session(write(tmp_path / "s.md", text), now=FIXED_NOW)

    assert [m.role for m in session.messages] == ["user", "assistant"]
    body = session.messages[0].text
    assert "## 用户" in body and "## 助手" in body and "## Unknown" in body


def test_unclosed_fence_fails_closed(tmp_path: Path):
    """未闭合围栏到 EOF：失败关闭，不静默当正文。"""
    text = NL.join(["# 标题", "## 用户", fence_times(BT, 4), "没有闭合", ""])
    path = write(tmp_path / "s.md", text)

    with pytest.raises(SessionFormatError) as err:
        load_markdown_session(path, now=FIXED_NOW)

    message = str(err.value)
    assert "没有闭合" in message or "未关闭" in message
    assert "第 3 行" in message, "要报出围栏开启行号"


def test_fence_before_first_role_fails_closed(tmp_path: Path):
    """标题与第一条角色标题之间出现代码块，同样算「标题前正文」。"""
    text = NL.join(
        ["# 标题", "", fence_times(BT, 3), "游离代码", fence_times(BT, 3), "", "## 用户", "问", ""]
    )
    path = write(tmp_path / "s.md", text)

    with pytest.raises(SessionFormatError, match="第 3 行"):
        load_markdown_session(path, now=FIXED_NOW)


def test_fence_body_keeps_original_lines(tmp_path: Path):
    """围栏内容逐行原样保留（含缩进与语言标注）。"""
    text = NL.join(
        [
            "# 标题",
            "## 用户",
            "   " + fence_times(BT, 4) + "python",
            "    def foo():",
            "        return 1",
            "   " + fence_times(BT, 4),
            "",
        ]
    )
    session = load_markdown_session(write(tmp_path / "s.md", text), now=FIXED_NOW)

    body = session.messages[0].text
    # 段落首尾空白会被 strip，因此第一行的前导缩进不保留；其余行逐字保留
    assert fence_times(BT, 4) + "python" in body
    assert "    def foo():" in body, "代码行内部缩进必须原样保留"
    assert "        return 1" in body
    assert "   " + fence_times(BT, 4) in body, "关闭围栏的缩进也要保留"


# --- 闭合围栏的尾随文本边界（Gate 4 复审终项） -------------------------------


def test_info_string_line_inside_fence_is_content(tmp_path: Path):
    """```python 在 ``` 块里是内容行，不能当成 closing fence。"""
    text = NL.join(
        [
            "# 标题",
            "## 用户",
            fence_times(BT, 3),
            "print(1)",
            fence_times(BT, 3) + "python",
            "## 助手",
            "伪标题",
            fence_times(BT, 3),
            "",
            "## 助手",
            "真正回答",
            "",
        ]
    )
    session = load_markdown_session(write(tmp_path / "s.md", text), now=FIXED_NOW)

    assert [m.role for m in session.messages] == ["user", "assistant"]
    body = session.messages[0].text
    assert fence_times(BT, 3) + "python" in body, "带标注的行要原样留在代码块里"
    assert "伪标题" in body, "该行之后的内容仍属于用户段落"
    assert session.messages[1].text == "真正回答"


def test_heading_after_info_string_line_stays_user_body(tmp_path: Path):
    """围栏内 ```python 之后的 `## 助手` 仍是正文。"""
    text = NL.join(
        [
            "# 标题",
            "## 用户",
            fence_times(BT, 3),
            fence_times(BT, 3) + "python",
            "## 助手",
            fence_times(BT, 3),
            "",
        ]
    )
    session = load_markdown_session(write(tmp_path / "s.md", text), now=FIXED_NOW)

    assert len(session.messages) == 1
    assert "## 助手" in session.messages[0].text


def test_info_string_can_still_open_a_fence(tmp_path: Path):
    """```python 作为真正的 opening 仍然开启代码块，之后的 `## 助手` 是角色。"""
    text = NL.join(
        [
            "# 标题",
            "## 用户",
            fence_times(BT, 3) + "python",
            "print(1)",
            fence_times(BT, 3),
            "",
            "## 助手",
            "答",
            "",
        ]
    )
    session = load_markdown_session(write(tmp_path / "s.md", text), now=FIXED_NOW)

    assert [m.role for m in session.messages] == ["user", "assistant"]
    assert fence_times(BT, 3) + "python" in session.messages[0].text
    assert session.messages[1].text == "答"


def test_closing_fence_allows_trailing_spaces_and_tabs(tmp_path: Path):
    """关闭行只允许空格/制表符尾随。"""
    text = NL.join(
        [
            "# 标题",
            "## 用户",
            fence_times(BT, 3),
            "内容",
            fence_times(BT, 3) + " \t ",
            "",
            "## 助手",
            "答",
            "",
        ]
    )
    session = load_markdown_session(write(tmp_path / "s.md", text), now=FIXED_NOW)

    assert [m.role for m in session.messages] == ["user", "assistant"]
    assert session.messages[1].text == "答"


def test_longer_marker_with_trailing_text_does_not_close(tmp_path: Path):
    """字符与长度都满足、但带尾随文本，仍然不得关闭。"""
    text = NL.join(
        [
            "# 标题",
            "## 用户",
            fence_times(BT, 3),
            fence_times(BT, 4) + "python",
            "## 助手",
            "伪标题",
            fence_times(BT, 3),
            "",
            "## 助手",
            "真回答",
            "",
        ]
    )
    session = load_markdown_session(write(tmp_path / "s.md", text), now=FIXED_NOW)

    assert [m.role for m in session.messages] == ["user", "assistant"]
    assert "伪标题" in session.messages[0].text
    assert session.messages[1].text == "真回答"


def test_unclosed_after_info_string_still_fails(tmp_path: Path):
    """带标注的 opening 到 EOF 未闭合，同样失败关闭。"""
    text = NL.join(["# 标题", "## 用户", fence_times(BT, 3) + "python", "print(1)", ""])
    path = write(tmp_path / "s.md", text)

    with pytest.raises(SessionFormatError, match="没有闭合"):
        load_markdown_session(path, now=FIXED_NOW)


# --- 闭合围栏的尾随文本边界（Gate 4 复审终项） -------------------------------


