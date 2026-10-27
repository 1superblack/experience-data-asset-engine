"""Task 9：vault 校验与默认过期过滤。

样本统一来自 ``tests/fixtures/vault-basic/``（合成 vault，无真实数据）。
每个「只读」用例都用目录字节快照来证明——不是靠嘴上说不改。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from experience_data_asset_engine.config import AppConfig, init_config
from experience_data_asset_engine.errors import InputError
from experience_data_asset_engine.models import VOLATILITY_LABELS
from experience_data_asset_engine.validator import (
    Issue,
    ValidationReport,
    iter_due_notes,
    iter_notes,
    scan_notes,
    validate_vault,
)
from experience_data_asset_engine.writer import GENERATOR, render_frontmatter

PROJECT = "体验数据资产引擎"

#: fixture 里「默认不可检索」的笔记：过期、被取代、非法状态、无法解析、
#: 以及字段校验不过关（缺必填字段 / 非法日期）的——「可检索」不得比「可校验」宽松。
DEFAULT_HIDDEN = {
    "已过时结论",
    "已被取代结论",
    "旧方案C",
    "wiki旧笔记",
    "无回写笔记",
    "非法状态笔记",
    "坏YAML笔记",
    "缺字段笔记",
    "非法日期笔记",
}


# --- 辅助 -------------------------------------------------------------------


def put_note(config: AppConfig, title: str, text: str, project: str = PROJECT) -> Path:
    path = config.vault / "04-对话存档" / project / f"{title}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def note_text(
    title: str,
    *,
    project: str = PROJECT,
    status: str = "有效",
    volatility: str = "演进",
    created: str = "2026-10-27",
    review_after: str = "2027-01-25",
    replaces: tuple[str, ...] = (),
    replaced_by: tuple[str, ...] = (),
) -> str:
    fields = {
        "tags": ["对话存档", project, volatility],
        "项目": project,
        "主题": title,
        "状态": status,
        "内容时效": volatility,
        "created": created,
        "updated": created,
        "valid_from": created,
        "review_after": review_after,
        "取代自": list(replaces),
        "取代为": list(replaced_by),
        "来源": "codex",
        "会话ID": f"sess-{title}",
        "内容哈希": "a" * 64,
        "生成器": GENERATOR,
        "状态键": "b" * 64,
    }
    return render_frontmatter(fields) + f"\n\n# {title}\n"


def snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def make_file_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - 依赖宿主权限
        pytest.skip("当前环境不支持创建文件符号链接")


def stems(paths) -> set[str]:
    return {Path(path).stem for path in paths}


def codes(issues) -> set[str]:
    return {issue.code for issue in issues}


@pytest.fixture
def config(basic_vault: Path) -> AppConfig:
    return init_config(basic_vault)


# --- 默认过滤（规则 4 / 5） --------------------------------------------------


def test_iter_notes_defaults_to_valid_and_pending_only(config: AppConfig):
    found = stems(iter_notes(config))

    assert "现行结论" in found
    assert "待复核结论" in found
    assert DEFAULT_HIDDEN.isdisjoint(found)


def test_iter_notes_include_stale_returns_every_parsable_note(config: AppConfig):
    default = list(iter_notes(config))
    full = list(iter_notes(config, include_stale=True))

    assert len(full) > len(default)
    assert {"已过时结论", "已被取代结论"} <= stems(full)
    # 解析失败的仍然不返回：不能靠「猜它还有效」蒙过去
    assert "坏YAML笔记" not in stems(full)


def test_scan_reports_skipped_count(config: AppConfig):
    scan = scan_notes(config)
    assert scan.skipped == len(scan.records) - len(scan.searched)
    assert scan.skipped > 0

    full = scan_notes(config, include_stale=True)
    searched = {record.path for record in full.searched}
    blocked = {record.path.stem for record in full.records if record.path not in searched}

    # include_stale=True 也不是「全放行」：解析失败、非法状态、缺字段、非法日期仍然不进
    assert blocked == {"坏YAML笔记", "非法状态笔记", "缺字段笔记", "非法日期笔记"}
    assert full.skipped == len(blocked)


def test_missing_field_note_is_not_searchable(config: AppConfig):
    """只写了「状态: 有效」、缺其余必填字段的笔记，不得进默认检索。"""
    path = config.vault / "04-对话存档" / PROJECT / "缺字段笔记.md"

    assert path.exists(), "fixture 应包含该样本"
    assert "缺字段笔记" not in stems(iter_notes(config))
    assert "缺字段笔记" not in stems(iter_notes(config, include_stale=True))
    # 但它不能静默消失：校验报告里必须看得见
    issues = [issue for issue in validate_vault(config).missing_fields if issue.path == path]
    assert {issue.field for issue in issues} == {"内容哈希", "状态键"}


def test_invalid_date_note_is_not_searchable(config: AppConfig):
    path = config.vault / "04-对话存档" / PROJECT / "非法日期笔记.md"

    assert "非法日期笔记" not in stems(iter_notes(config))
    assert "非法日期笔记" not in stems(iter_notes(config, include_stale=True))
    assert [issue for issue in validate_vault(config).missing_fields if issue.path == path]


def test_validation_report_carries_skipped_count(config: AppConfig):
    assert validate_vault(config).skipped == scan_notes(config).skipped


def test_pending_review_is_searchable_but_ranked_later(config: AppConfig):
    order = [path.stem for path in iter_notes(config)]
    assert "待复核结论" in order
    assert order.index("待复核结论") > order.index("现行结论")


def test_validate_covers_notes_outside_archive_dir(config: AppConfig):
    """校验范围是 vault，不是只有归档目录——归档目录外的合法笔记也应进 valid。"""
    assert "参考索引" in stems(validate_vault(config).valid)


# --- 字段类问题（规则 1 / 2） ------------------------------------------------


def test_missing_fields_are_reported(config: AppConfig):
    report = validate_vault(config)
    issues = [issue for issue in report.missing_fields if issue.path.stem == "缺字段笔记"]

    assert {issue.field for issue in issues} == {"内容哈希", "状态键"}
    assert codes(issues) == {"missing_field"}
    assert "缺字段笔记" not in stems(report.valid)


def test_invalid_date_goes_to_missing_fields_bucket(config: AppConfig):
    report = validate_vault(config)
    issues = [issue for issue in report.missing_fields if issue.path.stem == "非法日期笔记"]

    assert issues
    assert codes(issues) == {"invalid_date"}
    assert issues[0].field in {"created", "updated", "valid_from"}
    assert "非法日期笔记" not in stems(report.valid)


def test_unparseable_note_is_reported_and_never_valid(config: AppConfig):
    report = validate_vault(config)
    issues = [issue for issue in report.missing_fields if issue.path.stem == "坏YAML笔记"]

    assert codes(issues) == {"unparseable"}
    assert "坏YAML笔记" not in stems(report.valid)


# --- 状态类问题（规则 1） ----------------------------------------------------


def test_invalid_status_is_reported(config: AppConfig):
    report = validate_vault(config)
    issues = [issue for issue in report.invalid_status if issue.path.stem == "非法状态笔记"]

    assert codes(issues) == {"invalid_status"}
    assert issues[0].field == "状态"
    assert "非法状态笔记" not in stems(report.valid)
    # 非法状态既不进检索结果，也不静默消失：它出现在报告里
    assert "非法状态笔记" not in stems(iter_notes(config))


def test_invalid_volatility_is_reported(config: AppConfig):
    put_note(config, "非法时效笔记", note_text("非法时效笔记", volatility="半稳定"))

    report = validate_vault(config)
    issues = [issue for issue in report.invalid_status if issue.path.stem == "非法时效笔记"]

    assert codes(issues) == {"invalid_volatility"}
    assert "半稳定" in issues[0].message


@pytest.mark.parametrize("label", list(VOLATILITY_LABELS.values()))
def test_every_declared_volatility_label_is_accepted(config: AppConfig, label: str):
    title = f"时效标签 {label}"
    put_note(config, title, note_text(title, volatility=label))

    report = validate_vault(config)

    assert not [
        issue
        for issue in report.invalid_status
        if issue.path.stem == title and issue.code == "invalid_volatility"
    ]
    assert title in stems(report.valid)


# --- 取代关系（规则 3） ------------------------------------------------------


def test_bidirectional_links_are_accepted(config: AppConfig):
    offending = stems(
        issue.path
        for issue in validate_vault(config).broken_links
        if issue.code != "unparseable"
    )
    assert "新方案C" not in offending
    assert "wiki新笔记" not in offending


def test_wiki_link_and_md_suffix_both_resolve(config: AppConfig):
    """``[[笔记]]`` 与 ``笔记.md`` 两种写法都要能解析成同一篇笔记。"""
    report = validate_vault(config)
    for stem in ("wiki新笔记", "wiki旧笔记"):
        assert stem in stems(report.valid), f"{stem} 的取代关系应闭合"


def test_broken_link_is_reported(config: AppConfig):
    report = validate_vault(config)
    issues = [issue for issue in report.broken_links if issue.path.stem == "断链笔记"]

    assert codes(issues) == {"broken_link"}
    assert "查无此笔记" in issues[0].message
    assert "断链笔记" not in stems(report.valid)


def test_one_way_link_is_reported(config: AppConfig):
    report = validate_vault(config)
    issues = [issue for issue in report.broken_links if issue.path.stem == "单向笔记"]

    assert codes(issues) == {"one_way_link"}
    assert "无回写笔记" in issues[0].message


def test_self_link_is_reported(config: AppConfig):
    report = validate_vault(config)
    issues = [issue for issue in report.broken_links if issue.path.stem == "自指笔记"]

    assert codes(issues) == {"self_link"}


def test_malformed_link_field_is_reported(config: AppConfig):
    text = note_text("坏链接字段").replace("取代自: []", "取代自: 42")
    put_note(config, "坏链接字段", text)

    report = validate_vault(config)

    assert "malformed_link_field" in codes(
        issue for issue in report.broken_links if issue.path.stem == "坏链接字段"
    )


# --- 报告形状 ---------------------------------------------------------------


def test_report_has_exactly_the_contract_buckets(config: AppConfig):
    report = validate_vault(config)
    assert set(ValidationReport.__dataclass_fields__) == {
        "valid",
        "missing_fields",
        "invalid_status",
        "broken_links",
        "skipped",
    }
    assert report.ok is False
    payload = report.to_dict()
    assert set(payload) == {
        "valid",
        "missing_fields",
        "invalid_status",
        "broken_links",
        "skipped",
        "ok",
    }
    assert "跳过" in report.summary()


def test_issue_is_structured():
    issue = Issue(Path("x.md"), "missing_field", "缺少必填字段 内容哈希", "内容哈希")
    assert issue.to_dict() == {
        "path": "x.md",
        "code": "missing_field",
        "message": "缺少必填字段 内容哈希",
        "field": "内容哈希",
    }


def test_empty_vault_validates_clean(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    clean = init_config(vault)

    report = validate_vault(clean)

    assert report.ok is True
    assert report.valid == ()
    assert report.skipped == 0
    assert list(iter_notes(clean)) == []


# --- 只读（规则 6） ----------------------------------------------------------


def test_validate_vault_is_read_only(config: AppConfig):
    before = snapshot(config.vault)
    validate_vault(config)
    assert snapshot(config.vault) == before


def test_iter_notes_is_read_only(config: AppConfig):
    before = snapshot(config.vault)
    list(iter_notes(config))
    list(iter_notes(config, include_stale=True))
    assert snapshot(config.vault) == before


# --- 到期只列不改（规则 7） --------------------------------------------------


def test_due_notes_only_lists_and_never_changes_status(config: AppConfig):
    due_path = config.vault / "04-对话存档" / PROJECT / "待复核结论.md"
    before = snapshot(config.vault)

    due = list(iter_due_notes(config, date(2026, 10, 27)))

    assert due_path in due
    assert snapshot(config.vault) == before
    assert "状态: 待复核" in due_path.read_text(encoding="utf-8")


def test_due_notes_respect_stale_filter(config: AppConfig):
    default = stems(iter_due_notes(config, date(2026, 10, 27)))
    full = stems(iter_due_notes(config, date(2026, 10, 27), include_stale=True))

    assert "已过时结论" not in default
    assert "已过时结论" in full


def test_due_notes_reject_implicit_today(config: AppConfig):
    with pytest.raises(InputError, match="today"):
        list(iter_due_notes(config, "2026-10-27"))  # type: ignore[arg-type]


def test_due_notes_skip_broken_review_after_instead_of_raising(config: AppConfig):
    """单篇 review_after 坏了：跳过它，其余照常产出，不中断整个复核列表。"""
    broken = put_note(
        config,
        "坏复核日期笔记",
        note_text("坏复核日期笔记", review_after="下个月"),
    )
    healthy_due = config.vault / "04-对话存档" / PROJECT / "待复核结论.md"

    due = list(iter_due_notes(config, date(2026, 10, 27)))

    assert broken not in due, "坏日期的笔记不得进到期候选"
    assert healthy_due in due, "同批其它到期笔记必须照常产出，不能被一篇坏笔记拖停"
    # 它也不能静默消失：validator 要报 invalid_date
    issues = [issue for issue in validate_vault(config).missing_fields if issue.path == broken]
    assert codes(issues) == {"invalid_date"}


# --- 待合并候选（pending） ---------------------------------------------------


def test_pending_candidate_is_not_reported_as_bad_frontmatter(config: AppConfig):
    """pending 是完整 Markdown 候选，不是坏 YAML，也不该混进 missing_fields。"""
    pending = put_note(
        config,
        "冲突笔记.pending-abcdef12",
        note_text("冲突笔记"),
    )

    report = validate_vault(config)

    assert not [issue for issue in report.issues if issue.path == pending]
    assert pending not in report.valid, "候选不是正式笔记，不该算 valid"
    assert "冲突笔记.pending-abcdef12" not in stems(iter_notes(config))
    assert "冲突笔记.pending-abcdef12" not in stems(iter_notes(config, include_stale=True))
    # 计数上仍然可见
    assert scan_notes(config).skipped > 0


def test_legacy_json_pending_is_skipped_not_reported(config: AppConfig):
    """旧版 JSON 指针 pending 同样只跳过、不进 missing_fields。"""
    pending = put_note(
        config,
        "旧候选.pending-12345678",
        '{\n  "待合并到": "旧候选.md"\n}\n',
    )

    report = validate_vault(config)

    assert not [issue for issue in report.issues if issue.path == pending]
    assert pending not in report.valid


# --- 越界 -------------------------------------------------------------------


def test_symlinked_note_pointing_outside_vault_is_reported(config: AppConfig, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_note = outside / "外部笔记.md"
    outside_note.write_text(note_text("外部笔记"), encoding="utf-8")
    make_file_symlink(config.vault / "04-对话存档" / PROJECT / "逃逸.md", outside_note)

    report = validate_vault(config)

    assert "unparseable" in codes(
        issue for issue in report.missing_fields if issue.path.name == "逃逸.md"
    )
    assert "逃逸" not in stems(report.valid)
    assert outside_note.read_text(encoding="utf-8") == note_text("外部笔记")
