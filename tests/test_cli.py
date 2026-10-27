from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from experience_data_asset_engine import __version__
from experience_data_asset_engine.cli import (
    EXIT_FAILURE,
    EXIT_INPUT_REQUIRED,
    EXIT_NOT_IMPLEMENTED,
    EXIT_OK,
    main,
)
from experience_data_asset_engine.config import CONFIG_FILENAME, STATE_DIRNAME

ROOT = Path(__file__).resolve().parents[1]


def test_version(capsys):
    assert main(["--version"]) == 0
    assert "0.1.0" in capsys.readouterr().out


def test_package_version_matches_release():
    assert __version__ == "0.1.0"


def test_no_command_prints_help_and_asks_for_input(capsys):
    assert main([]) == EXIT_INPUT_REQUIRED
    assert "init" in capsys.readouterr().out


def test_unknown_command_returns_input_required(capsys):
    assert main(["nope"]) == EXIT_INPUT_REQUIRED
    assert "invalid choice" in capsys.readouterr().err


def test_help_exits_zero(capsys):
    assert main(["--help"]) == EXIT_OK
    assert "doctor" in capsys.readouterr().out


def test_init_requires_vault_argument(capsys):
    assert main(["init"]) == EXIT_INPUT_REQUIRED
    assert "--vault" in capsys.readouterr().err


def test_init_writes_config_and_reports_path(sample_vault, capsys):
    assert main(["init", "--vault", str(sample_vault)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "配置已写入" in out
    assert (sample_vault / STATE_DIRNAME / CONFIG_FILENAME).exists()


def test_init_twice_reports_not_overwritten(sample_vault, capsys):
    assert main(["init", "--vault", str(sample_vault)]) == EXIT_OK
    capsys.readouterr()
    assert main(["init", "--vault", str(sample_vault)]) == EXIT_OK
    assert "未覆盖" in capsys.readouterr().out


def test_init_honours_archive_dir_option(sample_vault, capsys):
    assert main(["init", "--vault", str(sample_vault), "--archive-dir", "99-其他"]) == EXIT_OK
    assert (sample_vault / "99-其他").is_dir()
    assert "99-其他" in capsys.readouterr().out


def test_init_with_missing_vault_returns_failure(tmp_path, capsys):
    assert main(["init", "--vault", str(tmp_path / "missing")]) == EXIT_FAILURE
    assert "错误" in capsys.readouterr().err


def test_doctor_reports_health(sample_vault, capsys):
    main(["init", "--vault", str(sample_vault)])
    capsys.readouterr()
    assert main(["doctor", "--vault", str(sample_vault)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "诊断汇总" in out
    assert "OK" in out


def test_doctor_without_config_returns_failure(sample_vault, capsys):
    assert main(["doctor", "--vault", str(sample_vault)]) == EXIT_FAILURE
    assert "init" in capsys.readouterr().err


def test_doctor_uses_env_vault(sample_vault, monkeypatch, capsys):
    main(["init", "--vault", str(sample_vault)])
    capsys.readouterr()
    monkeypatch.setenv("EDAE_VAULT", str(sample_vault))
    assert main(["doctor"]) == EXIT_OK
    assert "诊断汇总" in capsys.readouterr().out


def test_doctor_prints_no_api_key(sample_vault, monkeypatch, capsys):
    main(["init", "--vault", str(sample_vault)])
    capsys.readouterr()
    monkeypatch.setenv("EDAE_API_KEY", "sk-cli-must-not-leak")
    assert main(["doctor", "--vault", str(sample_vault)]) == EXIT_OK
    captured = capsys.readouterr()
    assert "sk-cli-must-not-leak" not in captured.out + captured.err


@pytest.mark.parametrize("command", ["archive", "validate", "status", "recheck", "uninstall"])
def test_unimplemented_commands_fail_closed(command, capsys):
    assert main([command]) == EXIT_NOT_IMPLEMENTED
    assert "尚未实现" in capsys.readouterr().err


def test_scripts_entrypoint_runs_from_repo_root():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "cli.py"), "--version"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == EXIT_OK, result.stderr
    assert "0.1.0" in result.stdout


def test_module_entrypoint_runs():
    env = dict(PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "experience_data_asset_engine", "--version"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert result.returncode == EXIT_OK, result.stderr
    assert "0.1.0" in result.stdout


def test_module_main_guard_exits_with_code(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["experience-data-asset-engine", "--version"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("experience_data_asset_engine", run_name="__main__")
    assert exc.value.code == EXIT_OK
    assert "0.1.0" in capsys.readouterr().out
