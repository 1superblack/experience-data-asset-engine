"""Task 8：安全写入、幂等与原子替换。

这里的断言都指向「磁盘上到底发生了什么」——不看返回值就下结论的用例一律不写。
每个会写盘的用例都在临时 vault 内运行，绝不触碰真实 vault。
"""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from experience_data_asset_engine import writer
from experience_data_asset_engine.config import AppConfig, init_config
from experience_data_asset_engine.errors import ConfigError, SchemaError
from experience_data_asset_engine.models import (
    SCHEMA_VERSION,
    Decision,
    DistilledNote,
    Message,
    NoiseReport,
    NormalizedSession,
)
from experience_data_asset_engine.writer import (
    ACTION_CREATED,
    ACTION_PENDING,
    ACTION_SKIPPED,
    ACTION_UPDATED,
    GENERATOR,
    content_hash_of,
    parse_frontmatter,
    render_frontmatter,
    render_note,
    safe_component,
    state_key_of,
    write_note,
)

SECRET = "sk-a1b2c3d4e5f6g7h8"


# --- 构造器 -----------------------------------------------------------------


def make_session(
    session_id: str = "sess-0001",
    source: str = "codex",
    title: str = "Gate 2 复审",
) -> NormalizedSession:
    return NormalizedSession(
        schema_version=SCHEMA_VERSION,
        source=source,
        session_id=session_id,
        title=title,
        created_at="2026-10-01T09:00:00+08:00",
        updated_at="2026-10-02T10:00:00+08:00",
        messages=(Message(role="user", text="继续"),),
    )


def make_note(**overrides) -> DistilledNote:
    defaults: dict = {
        "title": "Gate2 隐私旁路修复",
        "project": "体验数据资产引擎",
        "summary": "三轮修复后 Gate 2 关闭。",
        "background": "复审发现标题、截断私钥、source 三处旁路。",
        "diagnosis": ("标题未脱敏", "截断私钥被保留"),
        "decisions": (
            Decision(
                decision="source 改为受控标识符",
                reason="自由文本会让密钥与路径直接进 staging",
                rejected=("用正则过滤文本",),
            ),
        ),
        "evidence": ("388 passed", "覆盖率 92%"),
        "reusable": ("脱敏要覆盖标题，不只是正文",),
        "risks": ("上游模型仍可能把密钥抄进笔记",),
        "todo": ("推进 Task 9 validator",),
        "noise_report": NoiseReport(
            input_messages=10,
            kept_messages=4,
            system_noise=2,
            tool_noise=1,
            duplicate_topics=1,
            dead_ends=1,
            redactions=1,
        ),
        "content_volatility": "evolving",
        "status": "有效",
        "review_after": "2027-01-25",
    }
    defaults.update(overrides)
    return DistilledNote(**defaults)


def snapshot(root: Path) -> dict[str, bytes]:
    """目录下所有文件的相对路径 -> 字节，用于断言「一个字节都没动」。"""
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def make_dir_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - 依赖宿主权限
        pytest.skip("当前环境不支持创建目录符号链接")


def make_file_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - 依赖宿主权限
        pytest.skip("当前环境不支持创建文件符号链接")


@pytest.fixture
def config(sample_vault: Path) -> AppConfig:
    return init_config(sample_vault)


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    """固定基准日，避免 created/updated 随风飘。"""
    fixed = date(2026, 10, 27)
    monkeypatch.setattr(writer, "_today", lambda: fixed)
    return fixed


def note_path(config: AppConfig, note: DistilledNote) -> Path:
    return config.archive_dir / note.project / f"{note.title}.md"


def read_audit(config: AppConfig) -> list[dict]:
    log = config.audit_dir / "write-audit.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


# --- frontmatter 往返 -------------------------------------------------------


def test_frontmatter_round_trips_all_fields():
    fields = {
        "tags": ["对话存档", "体验数据资产引擎", "演进"],
        "项目": "体验数据资产引擎",
        "主题": "Gate2 隐私旁路修复",
        "状态": "有效",
        "内容时效": "演进",
        "created": "2026-10-27",
        "updated": "2026-10-27",
        "valid_from": "2026-10-27",
        "review_after": "2027-01-25",
        "取代自": [],
        "取代为": [],
        "来源": "codex",
        "会话ID": "sess-0001",
        "内容哈希": "0" * 64,
        "生成器": GENERATOR,
        "状态键": "1" * 64,
    }
    parsed, body = parse_frontmatter(render_frontmatter(fields) + "\n\n正文")
    assert parsed == fields
    assert body.strip() == "正文"


def test_frontmatter_list_item_with_comma_round_trips():
    """项目名带逗号时不能被列表切分撕开。"""
    fields = {
        "tags": ["对话存档", "A, B 项目", "演进"],
        "项目": "A, B 项目",
        "主题": "含逗号的清单, 第 2 项",
        "状态": "有效",
        "内容时效": "演进",
        "created": "2026-10-27",
        "updated": "2026-10-27",
        "valid_from": "2026-10-27",
        "review_after": "2027-01-25",
        "取代自": [],
        "取代为": [],
        "来源": "codex",
        "会话ID": "sess-0001",
        "内容哈希": "0" * 64,
        "生成器": GENERATOR,
        "状态键": "1" * 64,
    }
    parsed, _ = parse_frontmatter(render_frontmatter(fields))
    assert parsed["tags"] == ["对话存档", "A, B 项目", "演进"]
    assert parsed["主题"] == "含逗号的清单, 第 2 项"


def test_frontmatter_quotes_and_escapes_survive():
    fields = {
        "tags": [],
        "项目": '引号"与\\反斜杠',
        "主题": "第一行\n第二行",
        "状态": "有效",
        "内容时效": "演进",
        "created": "2026-10-27",
        "updated": "2026-10-27",
        "valid_from": "2026-10-27",
        "review_after": "2027-01-25",
        "取代自": [],
        "取代为": [],
        "来源": "codex",
        "会话ID": "sess-0001",
        "内容哈希": "0" * 64,
        "生成器": GENERATOR,
        "状态键": "1" * 64,
    }
    parsed, _ = parse_frontmatter(render_frontmatter(fields))
    assert parsed["项目"] == '引号"与\\反斜杠'
    assert parsed["主题"] == "第一行\n第二行"


def test_frontmatter_missing_field_is_rejected():
    with pytest.raises(SchemaError, match="缺少必填字段"):
        render_frontmatter({"tags": []})


@pytest.mark.parametrize(
    "text",
    [
        "没有分隔线",
        "---\n项目: x\n",  # 缺结束线
        "---\n不是键值对\n---\n",
        "---\n项目: x\n项目: y\n---\n",  # 重复键
    ],
)
def test_parse_frontmatter_rejects_malformed(text):
    with pytest.raises(SchemaError):
        parse_frontmatter(text)


# --- 名称安全 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["CON", "nul", "COM1", "lpt9", "a:b", "a/b", "a\\b", "a|b", "a?b", ".hidden", "尾点.", ""],
)
def test_safe_component_rejects_unsafe_names(name):
    with pytest.raises(SchemaError):
        safe_component(name)


def test_safe_component_strips_surrounding_whitespace():
    """首尾空白先被 strip，再判断「不能以空格结尾」——不是静默放过。"""
    assert safe_component("  归档  ") == "归档"


@pytest.mark.parametrize("name", ["Gate2 修复", "v1.2 计划", "kebab-case", "under_score", "2026 预算"])
def test_safe_component_accepts_normal_names(name):
    assert safe_component(name) == name


def test_safe_component_redacts_before_returning():
    assert safe_component(f"密钥 {SECRET}") == "密钥 [已脱敏]"


def test_safe_component_redaction_does_not_bypass_path_check():
    """脱敏后的结果仍要过路径校验，不能靠「反正会被脱敏」把 / 带进路径。"""
    with pytest.raises(SchemaError, match="非法字符"):
        safe_component(f"{SECRET}/../../etc/passwd")


# --- 指纹与幂等键 -----------------------------------------------------------


def test_content_hash_ignores_time_and_is_stable():
    note = make_note()
    session = make_session()
    first = content_hash_of(note, session)
    assert first == content_hash_of(note, session)
    assert len(first) == 64


def test_content_hash_changes_with_note_or_session():
    note = make_note()
    base = content_hash_of(note, make_session())
    assert content_hash_of(make_note(summary="换了摘要"), make_session()) != base
    assert content_hash_of(note, make_session(session_id="sess-0002")) != base


def test_state_key_matches_session_state_key():
    session = make_session()
    key = state_key_of(session)
    assert key == session.state_key
    assert len(key) == 64 and key.isalnum()


# --- 写入：首次 / 跳过 / 更新 ------------------------------------------------


def test_first_write_creates_note(config: AppConfig, today: date):
    note = make_note()
    session = make_session()

    result = write_note(note, session, config)

    assert result.action == ACTION_CREATED
    assert result.dry_run is False
    assert result.path == note_path(config, note)
    text = result.path.read_text(encoding="utf-8")
    assert "\r\n" not in text

    fields, body = parse_frontmatter(text)
    assert fields["生成器"] == GENERATOR
    assert fields["状态键"] == session.state_key
    assert fields["主题"] == note.title
    assert fields["来源"] == "codex"
    assert fields["created"] == "2026-10-27"
    assert fields["updated"] == "2026-10-27"
    assert fields["review_after"] == "2027-01-25"
    assert fields["内容哈希"] == result.content_hash
    assert "# Gate2 隐私旁路修复" in body
    assert "## 决策" in body

    state = json.loads(config.state_path.read_text(encoding="utf-8"))
    assert state["entries"][session.state_key]["path"].endswith(".md")
    assert state["entries"][session.state_key]["content_hash"] == result.content_hash

    audit = read_audit(config)
    assert len(audit) == 1
    assert audit[0]["action"] == ACTION_CREATED


def test_second_write_is_skipped_and_touches_nothing(config: AppConfig, today: date):
    note = make_note()
    session = make_session()
    first = write_note(note, session, config)
    before = snapshot(config.vault)
    audit_before = read_audit(config)

    second = write_note(note, session, config)

    assert second.action == ACTION_SKIPPED
    assert second.path == first.path
    assert snapshot(config.vault) == before
    assert read_audit(config) == audit_before


def test_same_session_update_keeps_created_and_bumps_updated(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
):
    session = make_session()
    monkeypatch.setattr(writer, "_today", lambda: date(2026, 10, 27))
    write_note(make_note(), session, config)

    monkeypatch.setattr(writer, "_today", lambda: date(2026, 11, 5))
    updated_note = make_note(summary="补了 Task 9 的结论。")
    result = write_note(updated_note, session, config)

    assert result.action == ACTION_UPDATED
    fields, body = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert fields["created"] == "2026-10-27"
    assert fields["updated"] == "2026-11-05"
    assert fields["valid_from"] == "2026-10-27"
    assert "补了 Task 9 的结论。" in body

    actions = [entry["action"] for entry in read_audit(config)]
    assert actions == [ACTION_CREATED, ACTION_UPDATED]


# --- 不覆盖：手写笔记 / pending ----------------------------------------------


def test_handwritten_note_is_never_overwritten(config: AppConfig, today: date):
    note = make_note()
    target = note_path(config, note)
    target.parent.mkdir(parents=True)
    target.write_text("这是我手写的笔记，别动。\n", encoding="utf-8")

    result = write_note(note, make_session(), config)

    assert result.action == ACTION_PENDING
    assert target.read_text(encoding="utf-8") == "这是我手写的笔记，别动。\n"
    assert result.path.name == f"{note.title}.pending-{result.content_hash[:8]}.md"

    # pending 必须是可解析的完整 Markdown，不是 JSON 指针
    fields, body = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert fields["内容哈希"] == result.content_hash
    assert fields["生成器"] == GENERATOR
    assert "## 摘要" in body and "## 决策" in body
    # 标注待合并到的目标，且只写相对路径
    assert "待合并到" in body
    assert f"{note.project}/{target.name}" in body
    assert "C:/" not in body and "C:\\" not in body
    assert state_not_written(config)


def state_not_written(config: AppConfig) -> bool:
    """pending 不动 state.json：没写进去的东西不该被记成已归档。"""
    return not config.state_path.exists()


def test_pending_is_idempotent_across_repeated_calls(config: AppConfig, today: date):
    """同一份冲突内容重复归档：命中同一个 pending，零新增、零改动。"""
    note = make_note()
    target = note_path(config, note)
    target.parent.mkdir(parents=True)
    target.write_text("手写内容\n", encoding="utf-8")

    first = write_note(note, make_session(), config)
    before = snapshot(config.vault)
    second = write_note(note, make_session(), config)
    after = snapshot(config.vault)

    assert first.action == ACTION_PENDING
    assert second.action == ACTION_PENDING
    assert second.path == first.path
    assert before == after, "第二次归档不得新增或改动任何文件"
    assert len(list(target.parent.glob("*.pending-*.md"))) == 1


def test_pending_does_not_grow_pending_2_or_3(config: AppConfig, today: date):
    """连续三次归档同一冲突内容：仍然只有一个 pending 文件。"""
    note = make_note()
    target = note_path(config, note)
    target.parent.mkdir(parents=True)
    target.write_text("手写内容\n", encoding="utf-8")

    for _ in range(3):
        write_note(note, make_session(), config)

    pendings = sorted(target.parent.glob("*.pending-*.md"))
    assert len(pendings) == 1
    assert not list(target.parent.glob("*.pending-2*"))
    assert not list(target.parent.glob("*.pending-3*"))


def test_foreign_pending_with_same_name_stops_instead_of_overwriting(
    config: AppConfig, today: date
):
    """同名 pending 已被别人占用：停下报错，绝不覆盖、也不静默返回错的文件。"""
    note = make_note()
    session = make_session()
    target = note_path(config, note)
    target.parent.mkdir(parents=True)
    target.write_text("手写内容\n", encoding="utf-8")
    digest = content_hash_of(note, session)
    foreign = target.with_name(f"{note.title}.pending-{digest[:8]}.md")
    foreign.write_text("别人写的候选\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="同名 pending"):
        write_note(note, session, config)
    assert foreign.read_text(encoding="utf-8") == "别人写的候选\n"


def test_other_session_same_title_goes_pending(config: AppConfig, today: date):
    note = make_note()
    write_note(note, make_session(session_id="sess-0001"), config)
    original = note_path(config, note).read_text(encoding="utf-8")

    result = write_note(note, make_session(session_id="sess-0002"), config)

    assert result.action == ACTION_PENDING
    assert note_path(config, note).read_text(encoding="utf-8") == original


# --- 同一会话只有一篇正式笔记 -----------------------------------------------


def test_same_session_title_change_does_not_create_second_note(
    config: AppConfig, today: date
):
    """同会话改标题：正式笔记仍只有一篇，旧笔记不动，新内容落 pending 等确认。"""
    session = make_session()
    created = write_note(make_note(), session, config)
    assert created.action == ACTION_CREATED
    original_text = created.path.read_text(encoding="utf-8")

    result = write_note(make_note(title="Gate2 隐私旁路修复（改名）"), session, config)

    assert result.action == ACTION_PENDING, "路径变化不得再 created 第二篇正式笔记"
    official = [p for p in config.vault.rglob("*.md") if ".pending" not in p.name]
    assert official == [created.path], f"正式笔记应仍只有一篇，实际 {official}"
    assert created.path.read_text(encoding="utf-8") == original_text, "旧笔记不得被移动或改写"

    fields, body = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert fields["状态键"] == state_key_of(session)
    assert "待合并到" in body
    # 旧 state path 不能被悄悄遗弃：pending 必须指回它
    assert created.path.relative_to(config.vault).as_posix() in body
    assert "标题或项目名变化" in body


def test_same_session_project_change_does_not_create_second_note(
    config: AppConfig, today: date
):
    """同会话改项目名同样不得落到第二个目录里。"""
    session = make_session()
    created = write_note(make_note(), session, config)

    result = write_note(make_note(project="另一个项目"), session, config)

    assert result.action == ACTION_PENDING
    official = [p for p in config.vault.rglob("*.md") if ".pending" not in p.name]
    assert official == [created.path]


def test_moved_state_path_with_same_hash_is_skipped(config: AppConfig, today: date):
    """笔记被人工改名、state 同步更新后：同会话再归档应跳过，不再写第二篇。"""
    session = make_session()
    note = make_note()
    created = write_note(note, session, config)

    # 模拟用户手工改名，并把 state 指到新位置
    other = created.path.with_name("历史位置.md")
    created.path.rename(other)
    state = json.loads(config.state_path.read_text(encoding="utf-8"))
    state["entries"][state_key_of(session)]["path"] = other.relative_to(
        config.vault
    ).as_posix()
    config.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    result = write_note(note, session, config)

    assert result.action == ACTION_SKIPPED
    assert result.path == other
    assert not note_path(config, note).exists(), "不得在目标位置再写一篇"


# --- 边界：vault 外与符号链接 ------------------------------------------------


def test_archive_dir_outside_vault_is_rejected(config: AppConfig, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    broken = AppConfig(vault=config.vault, archive_dir=outside, state_dir=config.state_dir)

    with pytest.raises(ConfigError, match="outside vault"):
        write_note(make_note(), make_session(), broken)
    assert snapshot(outside) == {}


def test_project_dir_symlink_escape_is_rejected(config: AppConfig, tmp_path: Path):
    note = make_note()
    outside = tmp_path / "outside"
    outside.mkdir()
    make_dir_symlink(config.archive_dir / note.project, outside)

    with pytest.raises(ConfigError, match="outside vault"):
        write_note(note, make_session(), config)
    assert snapshot(outside) == {}


def test_target_file_symlink_escape_is_rejected(config: AppConfig, tmp_path: Path):
    note = make_note()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "hijack.md"
    outside_file.write_text("vault 外的文件\n", encoding="utf-8")
    project_dir = config.archive_dir / note.project
    project_dir.mkdir(parents=True)
    make_file_symlink(project_dir / f"{note.title}.md", outside_file)

    with pytest.raises(ConfigError, match="outside vault"):
        write_note(note, make_session(), config)
    assert outside_file.read_text(encoding="utf-8") == "vault 外的文件\n"


def test_state_dir_symlink_escape_is_rejected(config: AppConfig, tmp_path: Path):
    outside = tmp_path / "outside-state"
    outside.mkdir()
    shutil.rmtree(config.state_dir)
    make_dir_symlink(config.state_dir, outside)

    with pytest.raises(ConfigError, match="outside vault"):
        write_note(make_note(), make_session(), config)
    assert snapshot(outside) == {}


# --- dry-run ----------------------------------------------------------------


def test_dry_run_writes_nothing(config: AppConfig, today: date):
    note = make_note()
    before = snapshot(config.vault)

    result = write_note(note, make_session(), config, dry_run=True)

    assert result.action == ACTION_CREATED
    assert result.dry_run is True
    assert snapshot(config.vault) == before
    assert not config.state_path.exists()
    assert not config.audit_dir.exists()
    assert not note_path(config, note).exists()


def test_dry_run_reports_update_without_touching_file(config: AppConfig, today: date):
    session = make_session()
    write_note(make_note(), session, config)
    before = snapshot(config.vault)

    result = write_note(make_note(summary="新摘要"), session, config, dry_run=True)

    assert result.action == ACTION_UPDATED
    assert result.dry_run is True
    assert snapshot(config.vault) == before


# --- 敏感值不落盘 -----------------------------------------------------------


def test_secrets_never_reach_markdown_state_or_audit(config: AppConfig, today: date):
    note = make_note(
        title=f"密钥 {SECRET} 归档",
        summary=f"这次用的是 {SECRET} 这个 key。",
        background="Cookie: sessionid=abcdef1234567890\nAuthorization: Bearer abcdefghijklmnop",
        evidence=(f"用 {SECRET} 调用成功",),
    )

    result = write_note(note, make_session(), config)

    md = result.path.read_text(encoding="utf-8")
    assert SECRET not in md
    assert "abcdef1234567890" not in md
    assert "abcdefghijklmnop" not in md
    assert "[已脱敏]" in md

    state_text = config.state_path.read_text(encoding="utf-8")
    audit_text = (config.audit_dir / "write-audit.jsonl").read_text(encoding="utf-8")
    for blob in (state_text, audit_text):
        assert SECRET not in blob
        assert "abcdef1234567890" not in blob

    # 文件名与 frontmatter 里的标题必须是同一份脱敏结果，不能一份脱敏一份原文
    fields, _ = parse_frontmatter(md)
    assert fields["主题"] == result.path.stem == "密钥 [已脱敏] 归档"


def test_render_note_defaults_to_safe_title_and_project():
    note = make_note(project="A, B 项目", title=f"标题 {SECRET}")
    text = render_note(note, make_session(), "0" * 64, "2026-10-27", "2026-10-27")
    fields, body = parse_frontmatter(text)
    assert fields["主题"] == safe_component(note.title)
    assert fields["项目"] == "A, B 项目"
    assert body.splitlines()[0] == f"# {safe_component(note.title)}"


# --- 日期口径：必须用本地日期 -----------------------------------------------


def test_today_uses_local_date_not_utc(monkeypatch: pytest.MonkeyPatch):
    """``_today()`` 用本地时区：UTC 日期在东八区 00:00-08:00 会写成前一天。"""

    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return datetime(2026, 10, 27, 7, 30)  # 本地：27 日早上 7:30
            return datetime(2026, 10, 26, 23, 30, tzinfo=timezone.utc)  # UTC 仍是 26 日

    monkeypatch.setattr(writer, "datetime", FakeDateTime)
    assert writer._today() == date(2026, 10, 27)


def test_write_uses_local_date_for_created(config: AppConfig, monkeypatch: pytest.MonkeyPatch):
    """落盘日期同样走本地日期，不受 UTC 偏移影响。"""

    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return datetime(2026, 10, 27, 0, 30)  # 本地刚过零点
            return datetime(2026, 10, 26, 16, 30, tzinfo=timezone.utc)  # UTC 还是 26 日

    monkeypatch.setattr(writer, "datetime", FakeDateTime)
    result = write_note(make_note(), make_session(), config)
    fields, _ = parse_frontmatter(result.path.read_text(encoding="utf-8"))

    assert fields["created"] == "2026-10-27"
    assert fields["updated"] == "2026-10-27"


# --- Windows 文件名与原子替换 ------------------------------------------------


def test_atomic_write_leaves_no_temp_file(tmp_path: Path):
    target = tmp_path / "note.md"
    writer.write_text_atomic(target, "内容\n")
    assert target.read_text(encoding="utf-8") == "内容\n"
    assert list(tmp_path.glob(".edae-*")) == []


def test_atomic_write_keeps_original_when_replace_fails(tmp_path: Path, monkeypatch):
    target = tmp_path / "note.md"
    target.write_text("旧内容\n", encoding="utf-8")

    def boom(src, dst):  # noqa: ANN001
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(writer.os, "replace", boom)
    with pytest.raises(OSError, match="模拟磁盘写入失败"):
        writer.write_text_atomic(target, "新内容\n")

    assert target.read_text(encoding="utf-8") == "旧内容\n"
    assert list(tmp_path.glob(".edae-*")) == []


def test_write_rejects_unsafe_project_name(config: AppConfig, today: date):
    with pytest.raises(SchemaError, match="非法字符"):
        write_note(make_note(project="a/b"), make_session(), config)
    assert snapshot(config.archive_dir) == {}


# --- 降噪统计章节（Gate 4 复审） ---------------------------------------------


def expected_noise_block(note: DistilledNote) -> list[str]:
    report = note.noise_report
    return [
        "## 降噪统计",
        f"过滤消息 {report.kept_messages}/{report.input_messages}",
        f"系统噪音 {report.system_noise}",
        f"工具噪音 {report.tool_noise}",
        f"重复主题 {report.duplicate_topics}",
        f"无效死路 {report.dead_ends}",
        f"脱敏 {report.redactions}",
    ]


def test_render_body_contains_noise_section():
    note = make_note()
    body = writer.render_body(note)

    for line in expected_noise_block(note):
        assert line in body, f"正文缺少: {line}"


def test_written_note_body_contains_real_noise_counts(config: AppConfig, today: date):
    note = make_note()
    result = write_note(note, make_session(), config)

    _fields, body = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    for line in expected_noise_block(note):
        assert line in body, f"落盘笔记正文缺少: {line}"
    assert "过滤消息 4/10" in body, "必须用 note.noise_report 的真实计数"


def test_noise_section_reflects_report_changes(config: AppConfig, today: date):
    """换一份 noise_report，正文数字必须跟着变——证明不是写死模板。"""
    report = NoiseReport(
        input_messages=200,
        kept_messages=45,
        system_noise=55,
        tool_noise=50,
        duplicate_topics=30,
        dead_ends=20,
        redactions=4,
    )
    note = make_note(noise_report=report)
    result = write_note(note, make_session(), config)

    body = parse_frontmatter(result.path.read_text(encoding="utf-8"))[1]
    assert "过滤消息 45/200" in body
    assert "系统噪音 55" in body
    assert "工具噪音 50" in body
    assert "重复主题 30" in body
    assert "无效死路 20" in body
    assert "脱敏 4" in body


def test_pending_candidate_contains_same_noise_section(config: AppConfig, today: date):
    """pending 候选也必须带同一份统计，否则合并时会把审计信息丢掉。"""
    note = make_note()
    text = writer.render_pending(
        note,
        make_session(),
        "a" * 64,
        today.isoformat(),
        "04-对话存档/项目/目标.md",
        "目标笔记不是本工具生成",
    )

    _fields, body = parse_frontmatter(text)
    for line in expected_noise_block(note):
        assert line in body, f"pending 正文缺少: {line}"


