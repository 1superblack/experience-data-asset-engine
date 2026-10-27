"""uninstall 的安全边界（Gate 4 复审）。

核心：uninstall 只允许删「自己的、配置有效的、位于真实 vault 内」的状态目录。
普通目录、无配置、坏配置一律拒绝，且文件树不变。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from experience_data_asset_engine.cli import EXIT_FAILURE, EXIT_INPUT_REQUIRED, EXIT_OK, main
from experience_data_asset_engine.config import (
    CONFIG_FILENAME,
    DEFAULT_ARCHIVE_REL,
    STATE_DIRNAME,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def run(capsys: pytest.CaptureFixture, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def init_vault(capsys: pytest.CaptureFixture, vault: Path) -> Path:
    assert run(capsys, "init", "--vault", str(vault))[0] == EXIT_OK
    return vault


# --- 拒绝场景 ---------------------------------------------------------------


def test_uninstall_refuses_plain_directory_even_with_state_dir(tmp_path: Path, capsys):
    """普通目录里就算有一个同名状态目录，也不许删——它可能只是别人的同名文件夹。"""
    plain = tmp_path / "plain-dir"
    state = plain / STATE_DIRNAME
    state.mkdir(parents=True)
    (state / CONFIG_FILENAME).write_text(
        json.dumps({"schema_version": 1, "archive_dir": DEFAULT_ARCHIVE_REL}),
        encoding="utf-8",
    )
    (state / "state.json").write_text('{"schema_version": 1, "entries": {}}', encoding="utf-8")
    (plain / "手写笔记.md").write_text("我的笔记\n", encoding="utf-8")
    before = snapshot(plain)

    code, out, err = run(capsys, "uninstall", "--vault", str(plain), "--yes")

    assert code == EXIT_FAILURE
    assert "Obsidian" in err
    assert out == "", "拒绝时不该输出「将删除」计划"
    assert snapshot(plain) == before
    assert state.is_dir()


def test_uninstall_refuses_state_dir_without_config(sample_vault: Path, capsys):
    init_vault(capsys, sample_vault)
    config_path = sample_vault / STATE_DIRNAME / CONFIG_FILENAME
    config_path.unlink()
    before = snapshot(sample_vault)

    code, _, err = run(capsys, "uninstall", "--vault", str(sample_vault), "--yes")

    assert code == EXIT_FAILURE
    assert "未初始化" in err
    assert snapshot(sample_vault) == before
    assert (sample_vault / STATE_DIRNAME).is_dir()


def test_uninstall_refuses_corrupted_config(sample_vault: Path, capsys):
    init_vault(capsys, sample_vault)
    config_path = sample_vault / STATE_DIRNAME / CONFIG_FILENAME
    config_path.write_text("{ 这不是 JSON", encoding="utf-8")
    before = snapshot(sample_vault)

    code, _, err = run(capsys, "uninstall", "--vault", str(sample_vault), "--yes")

    assert code == EXIT_FAILURE
    assert "JSON 解析失败" in err
    assert snapshot(sample_vault) == before


def test_uninstall_refuses_config_outside_vault(sample_vault: Path, tmp_path: Path, capsys):
    """配置里 archive_dir 指到 vault 外：load_config 会拒绝，uninstall 也不许删。"""
    init_vault(capsys, sample_vault)
    config_path = sample_vault / STATE_DIRNAME / CONFIG_FILENAME
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["archive_dir"] = "../outside"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    before = snapshot(sample_vault)

    code, _, err = run(capsys, "uninstall", "--vault", str(sample_vault), "--yes")

    assert code == EXIT_FAILURE
    assert "vault 内" in err
    assert snapshot(sample_vault) == before


def test_uninstall_still_requires_yes(sample_vault: Path, capsys):
    init_vault(capsys, sample_vault)
    before = snapshot(sample_vault)

    code, _, err = run(capsys, "uninstall", "--vault", str(sample_vault))

    assert code == EXIT_INPUT_REQUIRED
    assert "--yes" in err
    assert snapshot(sample_vault) == before


# --- 正常场景 ---------------------------------------------------------------


def test_uninstall_reports_files_before_deleting(sample_vault: Path, capsys):
    init_vault(capsys, sample_vault)

    code, out, _ = run(capsys, "uninstall", "--vault", str(sample_vault), "--yes")

    assert code == EXIT_OK
    lines = out.splitlines()
    plan_index = next(i for i, line in enumerate(lines) if line.startswith("将删除状态目录"))
    delete_index = next(i for i, line in enumerate(lines) if line.startswith("已删除"))
    assert plan_index < delete_index, "必须先报计划再删"
    assert any("个文件" in line for line in lines[:delete_index])
    assert any(line.strip().startswith("- ") for line in lines[:delete_index])
    assert not (sample_vault / STATE_DIRNAME).exists()


def test_uninstall_keeps_markdown_bytes(sample_vault: Path, capsys):
    init_vault(capsys, sample_vault)
    note = sample_vault / DEFAULT_ARCHIVE_REL / "手写" / "我的笔记.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\n状态: 有效\n---\n\n手写正文\n", encoding="utf-8")
    before = note.read_bytes()

    assert run(capsys, "uninstall", "--vault", str(sample_vault), "--yes")[0] == EXIT_OK

    assert note.read_bytes() == before
    assert not (sample_vault / STATE_DIRNAME).exists()


def test_uninstall_without_state_dir_is_noop(sample_vault: Path, capsys):
    init_vault(capsys, sample_vault)
    shutil.rmtree(sample_vault / STATE_DIRNAME)

    code, out, _ = run(capsys, "uninstall", "--vault", str(sample_vault), "--yes")

    assert code == EXIT_OK
    assert "无需卸载" in out


def test_uninstall_without_state_dir_but_plain_dir_still_refused(tmp_path: Path, capsys):
    """状态目录不存在也不能放宽 vault 校验：普通目录一律拒绝。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "笔记.md").write_text("手写\n", encoding="utf-8")
    before = snapshot(plain)

    code, _, err = run(capsys, "uninstall", "--vault", str(plain), "--yes")

    assert code == EXIT_FAILURE
    assert "Obsidian" in err
    assert snapshot(plain) == before
