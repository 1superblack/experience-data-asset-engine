"""Gate 4 复审：``recheck --note --status`` 改状态时必须同步 ``updated``。

四条口径，每条都用字节/字段快照证明，不靠「读一遍看着对」：

1. ``状态`` 真的变化 → ``updated`` 打成**当前本地日期**（不是 UTC，不是归档日）；
2. 最小改动：只有 ``状态`` 与 ``updated`` 两行变，``created``、正文、其它字段、
   原文件的 CRLF/LF 风格全部保留；
3. ``状态`` 没变 → **一个字节都不写**（``updated`` 是 2020 年的旧值也不动）；
4. 四条拒绝路径（非法状态 / pending / 手写笔记 / 越界）照旧失败关闭，且文件未变。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from experience_data_asset_engine import validator
from experience_data_asset_engine.config import AppConfig, init_config
from experience_data_asset_engine.errors import ConfigError, InputError
from experience_data_asset_engine.validator import set_note_status
from experience_data_asset_engine.writer import (
    GENERATOR,
    parse_frontmatter,
    render_frontmatter,
)

PROJECT = "复核测试项目"
ARCHIVE = Path("04-对话存档") / PROJECT

#: 正文里故意放一行长得像 frontmatter 的内容：改状态时正文必须一字不动。
BODY = "\n".join(
    [
        "# 复核样本",
        "",
        "updated: 2020-01-01",
        "状态: 已过时",
        "",
        "正文第二行，必须在改状态后逐字节保留。",
        "",
    ]
)


def vault_config(tmp_path: Path) -> AppConfig:
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    return init_config(vault)


def fields_for(
    *,
    status: str = "有效",
    updated: str = "2020-01-01",
    created: str = "2020-01-01",
) -> dict[str, object]:
    return {
        "tags": ["对话存档", PROJECT, "演进"],
        "项目": PROJECT,
        "主题": "复核样本",
        "状态": status,
        "内容时效": "演进",
        "created": created,
        "updated": updated,
        "valid_from": created,
        "review_after": "2027-01-25",
        "取代自": [],
        "取代为": [],
        "来源": "codex",
        "会话ID": "sess-recheck-0001",
        "内容哈希": "a" * 64,
        "生成器": GENERATOR,
        "状态键": "b" * 64,
    }


def note_text(newline: str = "\n", **overrides: str) -> str:
    text = render_frontmatter(fields_for(**overrides)) + "\n" + BODY
    return text.replace("\n", newline)


def put_note(config: AppConfig, text: str, name: str = "复核样本.md") -> Path:
    path = config.vault / ARCHIVE / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def tree_snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    """整个文件树的 (字节, mtime_ns) 快照：零写入必须是「连时间戳都不变」。"""
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def changed_lines(before: str, after: str) -> list[tuple[str, str]]:
    left = before.splitlines()
    right = after.splitlines()
    assert len(left) == len(right), "改状态不得增加或删除行"
    return [(a, b) for a, b in zip(left, right) if a != b]


def parse(text: str) -> tuple[dict[str, object], str]:
    return parse_frontmatter(text)


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return vault_config(tmp_path)


# --- 规则 1：状态变化 → updated 打成当前本地日期 -----------------------------


def test_status_change_stamps_updated_with_local_today(config: AppConfig):
    path = put_note(config, note_text())
    today_before = datetime.now().date().isoformat()

    set_note_status(config, path, "已过时")

    updated = str(parse(path.read_bytes().decode("utf-8"))[0]["updated"])
    # 本地日期：既不能是 2020-01-01 的旧值，也不能是 UTC 换算出来的前一天
    assert updated != "2020-01-01"
    assert updated in {today_before, datetime.now().date().isoformat()}


def test_updated_comes_from_local_clock_not_utc(config: AppConfig, monkeypatch):
    """把「今天」钉死成本地日期：证明 updated 走的是本地时钟这一条路径。"""
    path = put_note(config, note_text())
    monkeypatch.setattr(validator, "_local_today", lambda: date(2026, 10, 27))

    set_note_status(config, path, "待复核")

    text = path.read_text(encoding="utf-8")
    assert "updated: 2026-10-27" in text
    assert "状态: 待复核" in text


# --- 规则 2：最小改动 -------------------------------------------------------


def test_only_status_and_updated_lines_change(config: AppConfig):
    path = put_note(config, note_text())
    before = path.read_bytes().decode("utf-8")
    fields_before, body_before = parse(before)

    set_note_status(config, path, "已过时")

    after = path.read_bytes().decode("utf-8")
    fields_after, body_after = parse(after)
    today = fields_after["updated"]

    assert body_after == body_before, "正文必须逐字节保留（正文里的假 updated 也不能动）"
    assert fields_after["created"] == "2020-01-01", "created 不得被改写"
    expected_fields = dict(fields_before, 状态="已过时", updated=today)
    assert fields_after == expected_fields, "除 状态 / updated 外不得有任何字段变化"

    diff = changed_lines(before, after)
    assert [old for old, _ in diff] == ["状态: 有效", "updated: 2020-01-01"]
    assert [new for _, new in diff] == ["状态: 已过时", f"updated: {today}"]


def test_crlf_newlines_are_preserved(config: AppConfig):
    path = put_note(config, note_text(newline="\r\n"))
    before = path.read_bytes()
    assert before.count(b"\r\n") > 10

    set_note_status(config, path, "已过时")

    after = path.read_bytes()
    assert "状态: 已过时\r\n".encode("utf-8") in after
    # 没有任何一行被写成 LF：去掉 CRLF 后不该再剩孤立的 \n
    assert after.replace(b"\r\n", b"").count(b"\n") == 0
    assert changed_lines(
        before.decode("utf-8").replace("\r\n", "\n"),
        after.decode("utf-8").replace("\r\n", "\n"),
    )[0][0] == "状态: 有效"


def test_lf_newlines_are_preserved(config: AppConfig):
    path = put_note(config, note_text(newline="\n"))

    set_note_status(config, path, "已过时")

    assert path.read_bytes().count(b"\r\n") == 0


# --- 规则 3：状态未变 → 零写入 ----------------------------------------------


def test_unchanged_status_writes_nothing_at_all(config: AppConfig):
    path = put_note(config, note_text(status="有效", updated="2020-01-01"))
    before = tree_snapshot(config.vault)

    set_note_status(config, path, "有效")

    assert tree_snapshot(config.vault) == before, "状态没变就必须零写入（字节与 mtime 都不变）"
    assert "updated: 2020-01-01" in path.read_bytes().decode("utf-8"), "旧 updated 保持原样"
    # 没有留下临时文件
    assert not list(config.vault.rglob(".edae-*"))


def test_repeat_call_is_idempotent_after_first_change(config: AppConfig):
    path = put_note(config, note_text())
    set_note_status(config, path, "已过时")
    settled = tree_snapshot(config.vault)

    set_note_status(config, path, "已过时")

    assert tree_snapshot(config.vault) == settled


# --- 规则 4：失败关闭 -------------------------------------------------------


def test_invalid_status_is_rejected_and_file_untouched(config: AppConfig):
    path = put_note(config, note_text())
    before = path.read_bytes()

    with pytest.raises(InputError, match="非法状态"):
        set_note_status(config, path, "大概有效")

    assert path.read_bytes() == before


def test_pending_candidate_is_rejected(config: AppConfig):
    path = put_note(config, note_text(), name="复核样本.pending-abcdef12.md")
    before = path.read_bytes()

    with pytest.raises(InputError, match="pending"):
        set_note_status(config, path, "已过时")

    assert path.read_bytes() == before


def test_legacy_pending_candidate_is_rejected(config: AppConfig):
    path = put_note(config, note_text(), name="复核样本.pending.md")
    before = path.read_bytes()

    with pytest.raises(InputError, match="pending"):
        set_note_status(config, path, "已过时")

    assert path.read_bytes() == before


def test_handwritten_note_is_rejected(config: AppConfig):
    path = config.vault / ARCHIVE / "手写笔记.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("---\n状态: 有效\n---\n\n手写内容\n".encode())
    before = path.read_bytes()

    with pytest.raises(InputError, match="不是本工具生成"):
        set_note_status(config, path, "已过时")

    assert path.read_bytes() == before


def test_path_outside_vault_is_rejected(config: AppConfig, tmp_path: Path):
    outside = tmp_path / "vault-外面.md"
    outside.write_bytes(note_text().encode("utf-8"))
    before = outside.read_bytes()

    with pytest.raises(ConfigError, match="outside vault"):
        set_note_status(config, outside, "已过时")

    assert outside.read_bytes() == before


def test_missing_note_is_rejected(config: AppConfig):
    missing = config.vault / ARCHIVE / "不存在.md"

    with pytest.raises(InputError, match="不存在"):
        set_note_status(config, missing, "已过时")

    assert not missing.exists()


def test_note_without_updated_line_is_rejected_rather_than_half_written(config: AppConfig):
    """缺 ``updated`` 行时失败关闭：不写状态，也不偷偷补一行。

    状态变了却无法记录「什么时候变的」，比拒绝更糟；工具自己写的笔记永远有
    ``updated``，所以这条只会在用户手删了该行时触发。
    """
    text = note_text().replace("updated: 2020-01-01\n", "")
    path = put_note(config, text)
    before = path.read_bytes()

    with pytest.raises(InputError, match="updated"):
        set_note_status(config, path, "已过时")

    assert path.read_bytes() == before
