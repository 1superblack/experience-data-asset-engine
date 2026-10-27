"""Task 11：CLI 全链路（init / archive / validate / status / recheck / uninstall）。

全部在 ``tmp_path`` 的临时 vault 上跑，输入是合成 fixture，不涉及真实数据与网络。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from experience_data_asset_engine.cli import (
    EXIT_DISTILLATION_REQUIRED,
    EXIT_FAILURE,
    EXIT_INPUT_REQUIRED,
    EXIT_OK,
    main,
)
from experience_data_asset_engine.config import STATE_DIRNAME
from experience_data_asset_engine.writer import parse_frontmatter

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANUAL_MD = FIXTURES / "manual-session.md"
SESSION_JSON = FIXTURES / "session.json"
NOISY_JSON = FIXTURES / "noisy-session.json"
DISTILLED = FIXTURES / "distilled-e2e.json"
ARCHIVE_REL = "04-对话存档"


def run(capsys: pytest.CaptureFixture, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def snapshot_with_dirs(root: Path) -> dict[str, object]:
    """连目录一起记账：证明 dry-run 连一个空目录都没建。"""
    return {
        path.relative_to(root).as_posix(): ("dir" if path.is_dir() else path.read_bytes())
        for path in sorted(root.rglob("*"))
    }


@pytest.fixture
def vault(sample_vault: Path, capsys: pytest.CaptureFixture) -> Path:
    code, _, _ = run(capsys, "init", "--vault", str(sample_vault))
    assert code == EXIT_OK
    return sample_vault


def archived_notes(vault: Path) -> list[Path]:
    return sorted((vault / ARCHIVE_REL).rglob("*.md"))


def archive_once(capsys, vault, *extra: str):
    return run(
        capsys,
        "archive",
        "--input",
        str(MANUAL_MD),
        "--distilled",
        str(DISTILLED),
        "--vault",
        str(vault),
        *extra,
    )


# --- 主链路 -----------------------------------------------------------------


def test_full_flow_init_archive_validate_status_recheck(vault: Path, capsys):
    code, out, err = archive_once(capsys, vault)
    assert code == EXIT_OK, err
    assert "action: created" in out
    assert "content_hash:" in out
    assert "noise: 保留 6/6" in out
    assert "来源: --distilled" in out

    notes = archived_notes(vault)
    assert len(notes) == 1
    assert notes[0].name == "E2E 归档样本.md"
    assert notes[0].parent.name == "E2E工程"

    code, out, _ = run(capsys, "validate", "--vault", str(vault))
    assert code == EXIT_OK
    assert "valid: 1" in out
    assert "missing_fields: 0" in out
    assert "broken_links: 0" in out
    assert "skipped: 0" in out

    code, out, _ = run(capsys, "status", "--vault", str(vault))
    assert code == EXIT_OK
    assert "有效: 1" in out
    assert "待复核: 0" in out
    assert "已过时: 0" in out
    assert "已被取代: 0" in out
    assert "pending: 0" in out
    assert "断链数: 0" in out
    assert "缺字段数: 0" in out


def test_second_archive_is_skipped(vault: Path, capsys):
    assert archive_once(capsys, vault)[0] == EXIT_OK
    code, out, _ = archive_once(capsys, vault)

    assert code == EXIT_OK
    assert "action: skipped" in out
    assert len(archived_notes(vault)) == 1


def test_archive_accepts_json_session(vault: Path, capsys):
    code, out, err = run(
        capsys,
        "archive",
        "--input",
        str(SESSION_JSON),
        "--distilled",
        str(DISTILLED),
        "--vault",
        str(vault),
    )
    assert code == EXIT_OK, err
    assert "action: created" in out


def test_archive_rejects_unknown_suffix(vault: Path, tmp_path: Path, capsys):
    odd = tmp_path / "session.txt"
    odd.write_text("hello", encoding="utf-8")

    code, _, err = run(
        capsys,
        "archive",
        "--input",
        str(odd),
        "--distilled",
        str(DISTILLED),
        "--vault",
        str(vault),
    )

    assert code == EXIT_FAILURE
    assert "无法识别的输入后缀" in err


def test_archive_missing_input_file_fails(vault: Path, tmp_path: Path, capsys):
    code, _, err = run(
        capsys,
        "archive",
        "--input",
        str(tmp_path / "nope.md"),
        "--distilled",
        str(DISTILLED),
        "--vault",
        str(vault),
    )
    assert code == EXIT_FAILURE
    assert "不存在" in err


def test_archive_invalid_distilled_fails_closed(vault: Path, tmp_path: Path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"title": "x", "summary": "只有摘要"}), encoding="utf-8")

    code, _, err = run(
        capsys,
        "archive",
        "--input",
        str(MANUAL_MD),
        "--distilled",
        str(bad),
        "--vault",
        str(vault),
    )

    assert code == EXIT_FAILURE
    assert "decisions" in err
    assert archived_notes(vault) == []


# --- 无 provider：只写 staging -------------------------------------------------


def test_archive_without_provider_writes_staging_only(vault: Path, capsys):
    before = tree(vault)
    code, out, err = run(capsys, "archive", "--input", str(MANUAL_MD), "--vault", str(vault))

    assert code == EXIT_DISTILLATION_REQUIRED
    assert "需要蒸馏" in err
    staging_files = sorted((vault / STATE_DIRNAME / "staging").glob("*"))
    assert len(staging_files) == 2
    assert {item.suffix for item in staging_files} == {".md", ".json"}
    assert archived_notes(vault) == [], "无 provider 时不许写正式笔记"
    assert out.strip() == "", "退出码 3 时 stdout 不该有成功输出"
    after = tree(vault)
    markdown_outside_state = [
        name for name in after if name.endswith(".md") and not name.startswith(STATE_DIRNAME)
    ]
    assert markdown_outside_state == []
    assert set(before) < set(after), "只应新增状态目录下的 staging 文件"


# --- --distilled 跳过 provider -------------------------------------------------


def test_distilled_skips_provider_without_network(vault: Path, capsys, monkeypatch):
    # 指向必然连不上的地址：若真的调用了模型，就会以 DistillationError 退出 1
    monkeypatch.setenv("EDAE_PROVIDER", "openai-compatible")
    monkeypatch.setenv("EDAE_MODEL", "demo-model")
    monkeypatch.setenv("EDAE_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")

    code, out, err = archive_once(capsys, vault)

    assert code == EXIT_OK, err
    assert "来源: --distilled（未调用模型）" in out
    assert "network" not in err.lower()


# --- dry-run 零写入 -----------------------------------------------------------


def test_dry_run_writes_nothing(vault: Path, capsys):
    before = snapshot_with_dirs(vault)

    code, out, err = archive_once(capsys, vault, "--dry-run")

    assert code == EXIT_OK, err
    assert "dry-run" in out
    assert "预计 action: created" in out
    assert "预计 path: 04-对话存档/E2E工程/E2E 归档样本.md" in out
    assert snapshot_with_dirs(vault) == before, "dry-run 不许新增任何文件或目录"
    assert not (vault / STATE_DIRNAME / "staging").exists()
    assert not (vault / ARCHIVE_REL / "E2E工程").exists()


def test_dry_run_without_provider_does_not_create_staging(vault: Path, capsys):
    before = snapshot_with_dirs(vault)
    code, out, _ = run(
        capsys, "archive", "--input", str(MANUAL_MD), "--vault", str(vault), "--dry-run"
    )

    assert code == EXIT_OK
    assert "预计退出码: 3" in out
    assert snapshot_with_dirs(vault) == before


def test_dry_run_shows_noise_summary(vault: Path, capsys):
    _, out, _ = run(
        capsys,
        "archive",
        "--input",
        str(NOISY_JSON),
        "--distilled",
        str(DISTILLED),
        "--vault",
        str(vault),
        "--dry-run",
    )
    assert "noise: 保留 3/10" in out
    assert "系统 3" in out
    assert "工具 2" in out
    assert "脱敏 1" in out


# --- recheck -----------------------------------------------------------------


def test_recheck_due_lists_without_changing_anything(vault: Path, capsys):
    assert archive_once(capsys, vault)[0] == EXIT_OK
    note = archived_notes(vault)[0]
    before = note.read_bytes()

    code, out, _ = run(capsys, "recheck", "--due", "--vault", str(vault))

    assert code == EXIT_OK
    assert "到期候选: 1 篇" in out
    assert "E2E 归档样本.md" in out
    assert "只列不改" in out
    assert note.read_bytes() == before


def test_recheck_status_update_is_surgical(vault: Path, capsys):
    assert archive_once(capsys, vault)[0] == EXIT_OK
    note = archived_notes(vault)[0]
    before_text = note.read_text(encoding="utf-8")
    fields_before, body_before = parse_frontmatter(before_text)

    code, out, _ = run(
        capsys,
        "recheck",
        "--note",
        str(note),
        "--status",
        "已过时",
        "--vault",
        str(vault),
    )

    assert code == EXIT_OK
    assert "status: 已过时" in out
    after_text = note.read_text(encoding="utf-8")
    fields_after, body_after = parse_frontmatter(after_text)

    assert fields_after["状态"] == "已过时"
    assert body_after == body_before, "正文必须原样保留"
    fields_after.pop("状态")
    fields_before.pop("状态")
    assert fields_after == fields_before, "除状态外的 frontmatter 字段必须不变"

    assert before_text.count("\n") == after_text.count("\n")
    changed = [
        (left, right)
        for left, right in zip(before_text.splitlines(), after_text.splitlines())
        if left != right
    ]
    assert changed == [("状态: 有效", "状态: 已过时")]


def test_recheck_rejects_path_outside_vault(vault: Path, tmp_path: Path, capsys):
    outside = tmp_path / "outside.md"
    outside.write_text("---\n状态: 有效\n---\n", encoding="utf-8")

    code, _, err = run(
        capsys, "recheck", "--note", str(outside), "--status", "已过时", "--vault", str(vault)
    )

    assert code == EXIT_FAILURE
    assert "outside vault" in err
    assert outside.read_text(encoding="utf-8") == "---\n状态: 有效\n---\n"


def test_recheck_rejects_pending_candidate(vault: Path, capsys):
    assert archive_once(capsys, vault)[0] == EXIT_OK
    note = archived_notes(vault)[0]
    pending = note.with_name("E2E 归档样本.pending-abcdef12.md")
    pending.write_text(note.read_text(encoding="utf-8"), encoding="utf-8")

    code, _, err = run(
        capsys, "recheck", "--note", str(pending), "--status", "已过时", "--vault", str(vault)
    )

    assert code == EXIT_FAILURE
    assert "pending" in err


def test_recheck_rejects_foreign_note(vault: Path, capsys):
    foreign = vault / ARCHIVE_REL / "手写" / "我的笔记.md"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("---\n状态: 有效\n---\n\n手写内容\n", encoding="utf-8")

    code, _, err = run(
        capsys, "recheck", "--note", str(foreign), "--status", "已过时", "--vault", str(vault)
    )

    assert code == EXIT_FAILURE
    assert "不是本工具生成" in err
    assert "状态: 有效" in foreign.read_text(encoding="utf-8")


def test_recheck_rejects_invalid_status(vault: Path, capsys):
    assert archive_once(capsys, vault)[0] == EXIT_OK
    note = archived_notes(vault)[0]

    code, _, err = run(
        capsys, "recheck", "--note", str(note), "--status", "大概有效", "--vault", str(vault)
    )

    assert code == EXIT_FAILURE
    assert "非法状态" in err


def test_recheck_requires_status_or_due(vault: Path, capsys):
    assert run(capsys, "recheck", "--vault", str(vault))[0] == EXIT_INPUT_REQUIRED
    assert run(capsys, "recheck", "--note", "x.md", "--vault", str(vault))[0] == EXIT_INPUT_REQUIRED
    assert (
        run(capsys, "recheck", "--due", "--status", "有效", "--vault", str(vault))[0]
        == EXIT_INPUT_REQUIRED
    )


# --- uninstall ---------------------------------------------------------------


def test_uninstall_without_yes_deletes_nothing(vault: Path, capsys):
    assert archive_once(capsys, vault)[0] == EXIT_OK
    before = tree(vault)

    code, _, err = run(capsys, "uninstall", "--vault", str(vault))

    assert code == EXIT_INPUT_REQUIRED
    assert "--yes" in err
    assert tree(vault) == before
    assert (vault / STATE_DIRNAME).is_dir()


def test_uninstall_yes_removes_only_state_dir(vault: Path, capsys):
    assert archive_once(capsys, vault)[0] == EXIT_OK
    note = archived_notes(vault)[0]
    markdown_before = note.read_bytes()

    code, out, _ = run(capsys, "uninstall", "--vault", str(vault), "--yes")

    assert code == EXIT_OK
    assert "将删除状态目录" in out, "删除前必须先报出目标"
    assert "将删除" in out and "个文件" in out, "删除前必须先报出文件数量"
    assert "未删除任何 Markdown" in out
    assert not (vault / STATE_DIRNAME).exists()
    assert note.read_bytes() == markdown_before
    assert len(archived_notes(vault)) == 1


def test_uninstall_when_state_dir_missing_is_idempotent(vault: Path, capsys):
    shutil.rmtree(vault / STATE_DIRNAME)

    code, out, _ = run(capsys, "uninstall", "--vault", str(vault), "--yes")
    assert code == EXIT_OK
    assert "无需卸载" in out


# --- 输出不含敏感内容 ---------------------------------------------------------


def test_archive_output_never_contains_secret(vault: Path, capsys):
    code, out, err = run(
        capsys,
        "archive",
        "--input",
        str(NOISY_JSON),
        "--distilled",
        str(DISTILLED),
        "--vault",
        str(vault),
    )
    assert code == EXIT_OK, err
    combined = out + err
    assert "abcdef1234567890" not in combined
    assert "sk-" not in combined
    assert "需替换" not in combined
    # 真实降噪统计：noisy-session.json 是 10 条输入、保留 3 条
    assert "noise: 保留 3/10" in out
    assert "系统 3" in out and "工具 2" in out and "脱敏 1" in out


def test_dry_run_reports_real_redaction_count_for_noisy_input(vault: Path, capsys):
    _, out, _ = run(
        capsys, "archive", "--input", str(NOISY_JSON), "--vault", str(vault), "--dry-run"
    )
    assert "脱敏 1" in out
    assert "abcdef1234567890" not in out


# --- 有 provider：完整链路走一遍本地假服务 -------------------------------------


def archive_noisy(capsys, vault, *extra: str):
    return run(
        capsys,
        "archive",
        "--input",
        str(NOISY_JSON),
        "--distilled",
        str(DISTILLED),
        "--vault",
        str(vault),
        *extra,
    )


def noise_lines(note_path: Path) -> list[str]:
    body = parse_frontmatter(note_path.read_text(encoding="utf-8"))[1]
    return [line for line in body.splitlines() if line.startswith(("过滤消息", "系统噪音", "工具噪音", "重复主题", "无效死路", "脱敏"))]


def test_distilled_uses_real_noise_stats_in_stdout(vault: Path, capsys):
    code, out, err = archive_noisy(capsys, vault)

    assert code == EXIT_OK, err
    assert "noise: 保留 3/10" in out, "必须显示真实降噪结果，而不是 distilled JSON 自带的 6/6"
    assert "系统 3" in out and "工具 2" in out and "重复 1" in out
    assert "死路 1" in out and "脱敏 1" in out


def test_distilled_writes_real_noise_stats_into_note_body(vault: Path, capsys):
    assert archive_noisy(capsys, vault)[0] == EXIT_OK
    note = archived_notes(vault)[0]

    lines = noise_lines(note)

    assert "过滤消息 3/10" in lines
    assert "系统噪音 3" in lines
    assert "工具噪音 2" in lines
    assert "重复主题 1" in lines
    assert "无效死路 1" in lines
    assert "脱敏 1" in lines


def test_distilled_supplied_stats_are_discarded(vault: Path, capsys):
    supplied = json.loads(DISTILLED.read_text(encoding="utf-8"))
    assert supplied["noise_report"]["input_messages"] == 6, "fixture 自身声称 6/6"

    assert archive_noisy(capsys, vault)[0] == EXIT_OK
    text = archived_notes(vault)[0].read_text(encoding="utf-8")

    assert "6/6" not in text, "传入 JSON 自带的统计不得保留"
    assert "过滤消息 3/10" in text


def test_dry_run_and_real_write_share_stats_and_hash(vault: Path, capsys):
    code, dry_out, err = archive_noisy(capsys, vault, "--dry-run")
    assert code == EXIT_OK, err
    dry_hash = dry_out.split("content_hash: ")[1].splitlines()[0].strip()
    assert "noise: 保留 3/10" in dry_out
    assert not archived_notes(vault), "dry-run 不得写文件"

    code, real_out, err = archive_noisy(capsys, vault)
    assert code == EXIT_OK, err
    real_hash = real_out.split("content_hash: ")[1].splitlines()[0].strip()

    assert dry_hash == real_hash, "dry-run 与实际写入必须同一口径（同一 content_hash）"
    assert "noise: 保留 3/10" in real_out
    assert "过滤消息 3/10" in archived_notes(vault)[0].read_text(encoding="utf-8")


# --- 只读命令不写盘 / status 只扫一次 (Gate 4 复审 C) -------------------------


def test_validate_is_read_only(vault: Path, capsys):
    assert archive_once(capsys, vault)[0] == EXIT_OK
    before = snapshot_with_dirs(vault)

    code, out, _ = run(capsys, "validate", "--vault", str(vault))

    assert code == EXIT_OK
    assert "valid: 1" in out
    assert snapshot_with_dirs(vault) == before, "validate 必须零写入"


def test_status_is_read_only(vault: Path, capsys):
    assert archive_once(capsys, vault)[0] == EXIT_OK
    before = snapshot_with_dirs(vault)

    code, out, _ = run(capsys, "status", "--vault", str(vault))

    assert code == EXIT_OK
    assert "有效: 1" in out
    assert snapshot_with_dirs(vault) == before, "status 必须零写入"


def test_status_scans_vault_once(vault: Path, capsys, monkeypatch):
    """status 既要状态分布又要校验结论，只能读一次盘。"""
    from experience_data_asset_engine import cli as cli_module
    from experience_data_asset_engine.validator import scan_notes as real_scan

    calls: list[object] = []
    captured: dict[str, object] = {}

    def counting_scan(config, include_stale=False):
        result = real_scan(config, include_stale=include_stale)
        calls.append(result)
        return result

    def spy_validate_scan(config, scan, include_stale=False):
        captured["scan"] = scan
        from experience_data_asset_engine.validator import validate_scan as real_validate

        return real_validate(config, scan, include_stale=include_stale)

    monkeypatch.setattr(cli_module, "scan_notes", counting_scan)
    monkeypatch.setattr(cli_module, "validate_scan", spy_validate_scan)

    assert run(capsys, "status", "--vault", str(vault))[0] == EXIT_OK

    assert len(calls) == 1, f"status 扫描了 {len(calls)} 次，应只扫 1 次"
    assert captured["scan"] is calls[0], "校验必须复用同一份扫描结果"


# --- pending 冲突：手写笔记不被覆盖，候选带真实统计 ---------------------------


def test_conflict_writes_pending_with_real_stats(vault: Path, capsys):
    target = vault / ARCHIVE_REL / "E2E工程" / "E2E 归档样本.md"
    target.parent.mkdir(parents=True)
    handwritten = "---\n状态: 有效\n---\n\n# 我自己写的\n\n别覆盖我。\n"
    target.write_text(handwritten, encoding="utf-8")

    code, out, err = archive_noisy(capsys, vault)

    assert code == EXIT_OK, err
    assert "action: pending" in out
    assert target.read_text(encoding="utf-8") == handwritten, "手写笔记不得被改动"

    pendings = sorted(target.parent.glob("*.pending-*.md"))
    assert len(pendings) == 1
    pending_text = pendings[0].read_text(encoding="utf-8")
    assert "过滤消息 3/10" in pending_text
    assert "脱敏 1" in pending_text
    assert "6/6" not in pending_text


# --- 只读命令不写盘 / status 只扫一次 (Gate 4 复审 C) -------------------------


# --- pending 冲突：手写笔记不被覆盖，候选带真实统计 ---------------------------


