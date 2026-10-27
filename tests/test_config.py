from __future__ import annotations

import dataclasses
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from experience_data_asset_engine import config as config_module
from experience_data_asset_engine.config import (
    CONFIG_FILENAME,
    DEFAULT_ARCHIVE_REL,
    STATE_DIRNAME,
    AppConfig,
    ConfigError,
    doctor,
    init_config,
    load_config,
)


def by_code(diagnostics, code: str):
    matches = [item for item in diagnostics if item.code == code]
    assert matches, f"诊断里缺少 {code}：{[item.code for item in diagnostics]}"
    return matches[0]


def make_config(tmp_path: Path, *, archive_exists: bool = True) -> AppConfig:
    """直接构造 AppConfig，用于只测 doctor 的场景。"""
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    state_dir = vault / STATE_DIRNAME
    state_dir.mkdir(parents=True, exist_ok=True)
    archive_dir = vault / DEFAULT_ARCHIVE_REL
    if archive_exists:
        archive_dir.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        vault=vault.resolve(),
        archive_dir=archive_dir.resolve(),
        state_dir=state_dir.resolve(),
    )


# --- init -----------------------------------------------------------------


def test_init_config_creates_vault_state(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    config = init_config(vault)
    assert config.vault == vault.resolve()
    assert config.archive_dir == (vault / "04-对话存档").resolve()
    assert (vault / ".experience-data-asset-engine" / "config.json").exists()


def test_init_creates_archive_directory(sample_vault):
    config = init_config(sample_vault)
    assert config.archive_dir.is_dir()
    assert config.state_dir.is_dir()


def test_init_does_not_overwrite_existing_config(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    init_config(vault)
    path = vault / ".experience-data-asset-engine" / "config.json"
    before = path.read_text(encoding="utf-8")
    init_config(vault, archive_rel="99-其他")
    assert path.read_text(encoding="utf-8") == before


def test_init_returns_existing_config_on_second_call(sample_vault):
    first = init_config(sample_vault)
    second = init_config(sample_vault, archive_rel="99-其他")
    assert second == first
    assert second.archive_dir == first.archive_dir


def test_init_rejects_missing_vault_directory(tmp_path):
    with pytest.raises(ConfigError, match="vault"):
        init_config(tmp_path / "nope")


def test_init_rejects_vault_that_is_a_file(tmp_path):
    target = tmp_path / "vault-file"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match="vault"):
        init_config(target)


def test_init_rejects_archive_dir_escaping_vault(sample_vault):
    with pytest.raises(ConfigError, match="vault"):
        init_config(sample_vault, archive_rel="../escape")


def test_init_rejects_absolute_archive_dir(sample_vault, tmp_path):
    with pytest.raises(ConfigError, match="相对"):
        init_config(sample_vault, archive_rel=str(tmp_path / "abs"))


def test_init_rejects_hidden_archive_dir(sample_vault):
    with pytest.raises(ConfigError, match="隐藏"):
        init_config(sample_vault, archive_rel=".obsidian/notes")


def test_init_rejects_state_dir_as_archive_dir(sample_vault):
    with pytest.raises(ConfigError, match="隐藏"):
        init_config(sample_vault, archive_rel=STATE_DIRNAME)


def test_init_writes_relative_archive_dir_only(sample_vault):
    config = init_config(sample_vault)
    payload = json.loads(config.config_path.read_text(encoding="utf-8"))
    assert payload["archive_dir"] == DEFAULT_ARCHIVE_REL
    assert str(sample_vault) not in config.config_path.read_text(encoding="utf-8")


def test_init_never_persists_api_key(sample_vault, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-shouldneverbewritten")
    config = init_config(sample_vault)
    assert "sk-shouldneverbewritten" not in config.config_path.read_text(encoding="utf-8")


# --- load -----------------------------------------------------------------


def test_load_config_roundtrip(sample_vault):
    created = init_config(sample_vault)
    loaded = load_config(sample_vault)
    assert loaded == created


def test_load_config_from_env_vault(sample_vault, monkeypatch):
    created = init_config(sample_vault)
    monkeypatch.setenv("EDAE_VAULT", str(sample_vault))
    assert load_config() == created


def test_load_config_from_env_config_path(sample_vault, monkeypatch):
    created = init_config(sample_vault)
    monkeypatch.setenv("EDAE_CONFIG", str(created.config_path))
    assert load_config() == created


def test_load_config_without_config_raises(sample_vault):
    with pytest.raises(ConfigError, match="init"):
        load_config(sample_vault)


def test_load_config_without_any_hint_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="init"):
        load_config()


def test_load_config_rejects_unsupported_schema_version(sample_vault):
    config = init_config(sample_vault)
    payload = json.loads(config.config_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 99
    config.config_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ConfigError, match="schema_version"):
        load_config(sample_vault)


def test_load_config_rejects_tampered_archive_escape(sample_vault):
    config = init_config(sample_vault)
    payload = json.loads(config.config_path.read_text(encoding="utf-8"))
    payload["archive_dir"] = "../../escape"
    config.config_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ConfigError, match="vault"):
        load_config(sample_vault)


def test_load_config_rejects_broken_json(sample_vault):
    config = init_config(sample_vault)
    config.config_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON"):
        load_config(sample_vault)


def test_load_config_env_overrides_model_settings(sample_vault, monkeypatch):
    init_config(sample_vault)
    monkeypatch.setenv("EDAE_PROVIDER", "openai-compatible")
    monkeypatch.setenv("EDAE_MODEL", "demo-model")
    monkeypatch.setenv("EDAE_BASE_URL", "https://example.invalid/v1")
    loaded = load_config(sample_vault)
    assert loaded.provider == "openai-compatible"
    assert loaded.model == "demo-model"
    assert loaded.base_url == "https://example.invalid/v1"


# --- doctor ---------------------------------------------------------------


def test_doctor_healthy_vault_has_no_error(sample_vault):
    diagnostics = doctor(init_config(sample_vault))
    assert [item for item in diagnostics if item.level == "error"] == []
    assert by_code(diagnostics, "vault.exists").level == "ok"
    assert by_code(diagnostics, "archive.writable").level == "ok"
    assert by_code(diagnostics, "state.writable").level == "ok"


def test_doctor_reports_python_version(tmp_path):
    item = by_code(doctor(make_config(tmp_path)), "python.version")
    assert item.level == "ok"
    assert "3.11" in item.message


def test_doctor_reports_missing_obsidian_dir(tmp_path):
    config = make_config(tmp_path)
    (config.vault / ".obsidian").rmdir()
    item = by_code(doctor(config), "vault.obsidian")
    assert item.level == "error"


def test_doctor_reports_missing_vault(tmp_path):
    config = make_config(tmp_path)
    shutil.rmtree(config.vault)
    item = by_code(doctor(config), "vault.exists")
    assert item.level == "error"


def test_doctor_warns_when_archive_dir_missing(tmp_path):
    item = by_code(doctor(make_config(tmp_path, archive_exists=False)), "archive.exists")
    assert item.level == "warn"


def snapshot_tree(root: Path) -> dict[str, str | None]:
    """记录目录树：相对路径 -> 内容（目录为 None）。用于证明零写入。"""
    result: dict[str, str | None] = {}
    for path in sorted(Path(root).rglob("*")):
        rel = path.relative_to(root).as_posix()
        result[rel] = None if path.is_dir() else path.read_text(encoding="utf-8", errors="replace")
    return result


def test_init_rejects_plain_directory_without_side_effects(tmp_path):
    plain = tmp_path / "plain"
    (plain / "子目录").mkdir(parents=True)
    (plain / "note.md").write_text("handwritten", encoding="utf-8")
    before = snapshot_tree(plain)

    with pytest.raises(ConfigError, match="Obsidian"):
        init_config(plain)

    assert snapshot_tree(plain) == before
    assert not (plain / STATE_DIRNAME).exists()
    assert not (plain / DEFAULT_ARCHIVE_REL).exists()


def test_init_rejects_plain_directory_even_with_custom_archive_dir(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ConfigError, match="Obsidian"):
        init_config(plain, archive_rel="99-其他")
    assert list(plain.iterdir()) == []


def test_cli_init_rejects_plain_directory_without_writing(tmp_path, capsys):
    from experience_data_asset_engine.cli import main

    plain = tmp_path / "plain"
    plain.mkdir()
    assert main(["init", "--vault", str(plain)]) == 1
    assert "Obsidian" in capsys.readouterr().err
    assert list(plain.iterdir()) == []


def test_doctor_warns_when_archive_dir_missing_but_parent_writable(tmp_path):
    config = make_config(tmp_path, archive_exists=False)
    item = by_code(doctor(config), "archive.writable")
    assert item.level == "warn", "归档目录不存在时不得报 ok（假绿灯）"
    assert "父目录" in item.message


def test_doctor_errors_when_archive_parent_is_a_file(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    state_dir = vault / STATE_DIRNAME
    state_dir.mkdir()
    blocker = vault / "blocked"
    blocker.write_text("i am a file", encoding="utf-8")
    config = AppConfig(
        vault=vault.resolve(),
        archive_dir=(blocker / DEFAULT_ARCHIVE_REL).resolve(),
        state_dir=state_dir.resolve(),
    )
    item = by_code(doctor(config), "archive.writable")
    assert item.level == "error"
    assert "父" in item.message


def test_doctor_errors_when_parent_probe_fails(tmp_path, monkeypatch):
    config = make_config(tmp_path, archive_exists=False)
    monkeypatch.setattr(config_module, "_probe_writable", lambda directory: "permission denied")
    item = by_code(doctor(config), "archive.writable")
    assert item.level == "error"


# --- 状态目录符号链接逃逸 -----------------------------------------------------


def make_dir_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - 依赖宿主权限
        pytest.skip("当前环境不支持创建目录符号链接")


def test_init_rejects_state_dir_symlink_escape(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    make_dir_symlink(vault / STATE_DIRNAME, outside)
    before = snapshot_tree(outside)

    with pytest.raises(ConfigError, match="outside vault"):
        init_config(vault)

    assert snapshot_tree(outside) == before
    assert not (outside / CONFIG_FILENAME).exists()


def test_init_rejects_config_file_symlink_escape(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    state_dir = vault / STATE_DIRNAME
    state_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / CONFIG_FILENAME
    outside_file.write_text('{"schema_version": 1, "archive_dir": "x"}', encoding="utf-8")
    before = snapshot_tree(outside)
    (state_dir / CONFIG_FILENAME).symlink_to(outside_file)

    with pytest.raises(ConfigError, match="outside vault"):
        init_config(vault)

    assert snapshot_tree(outside) == before


def test_load_config_rejects_state_dir_symlink_escape(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    outside_root = tmp_path / "outside"
    outside_state = outside_root / STATE_DIRNAME
    outside_state.mkdir(parents=True)
    (outside_state / CONFIG_FILENAME).write_text(
        json.dumps({"schema_version": 1, "archive_dir": DEFAULT_ARCHIVE_REL}),
        encoding="utf-8",
    )
    make_dir_symlink(vault / STATE_DIRNAME, outside_state)

    with pytest.raises(ConfigError, match="outside vault"):
        load_config(vault)


def test_init_still_works_with_state_dir_inside_vault(tmp_path, sample_vault):
    config = init_config(sample_vault)
    assert config.config_path.is_file()
    assert config.config_path.resolve().is_relative_to(sample_vault.resolve())


# --- doctor 越界时不崩溃、不写探针 ---------------------------------------------


def test_doctor_reports_outside_archive_without_crashing(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    state_dir = vault / STATE_DIRNAME
    state_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    config = AppConfig(
        vault=vault.resolve(),
        archive_dir=outside.resolve(),
        state_dir=state_dir.resolve(),
    )

    diagnostics = doctor(config)

    assert by_code(diagnostics, "archive.inside_vault").level == "error"
    assert "archive.exists" not in {item.code for item in diagnostics}


def test_doctor_does_not_probe_when_archive_outside_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    state_dir = vault / STATE_DIRNAME
    state_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    config = AppConfig(
        vault=vault.resolve(),
        archive_dir=outside.resolve(),
        state_dir=state_dir.resolve(),
    )
    calls: list[Path] = []
    monkeypatch.setattr(
        config_module, "_probe_writable", lambda directory: calls.append(directory) or None
    )

    doctor(config)

    assert calls == []


def test_doctor_does_not_probe_when_vault_missing(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    shutil.rmtree(config.vault)
    calls: list[Path] = []
    monkeypatch.setattr(
        config_module, "_probe_writable", lambda directory: calls.append(directory) or None
    )

    doctor(config)

    assert calls == []


def test_doctor_does_not_probe_when_obsidian_missing(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    (config.vault / ".obsidian").rmdir()
    calls: list[Path] = []
    monkeypatch.setattr(
        config_module, "_probe_writable", lambda directory: calls.append(directory) or None
    )

    diagnostics = doctor(config)

    assert calls == []
    assert by_code(diagnostics, "vault.obsidian").level == "error"


def test_doctor_resolves_inside_archive_normally(tmp_path):
    config = make_config(tmp_path)
    diagnostics = doctor(config)
    assert by_code(diagnostics, "archive.inside_vault").level == "ok"
    assert by_code(diagnostics, "archive.writable").level == "ok"


# --- 探针文件命名 ------------------------------------------------------------


def test_probe_file_uses_edae_prefix(tmp_path, monkeypatch):
    captured: dict[str, str | None] = {}
    real_mkstemp = tempfile.mkstemp

    def fake_mkstemp(*args, **kwargs):
        captured["prefix"] = kwargs.get("prefix")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(config_module.tempfile, "mkstemp", fake_mkstemp)
    assert config_module._probe_writable(tmp_path) is None
    assert captured["prefix"] == ".edae-probe-"
    assert list(tmp_path.iterdir()) == []


def test_archive_rel_raises_config_error_when_outside_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    config = AppConfig(
        vault=vault.resolve(),
        archive_dir=outside.resolve(),
        state_dir=(vault / STATE_DIRNAME).resolve(),
    )
    with pytest.raises(ConfigError, match="outside vault"):
        config.archive_rel


def test_ensure_inside_vault_wraps_unresolvable_path(tmp_path, monkeypatch):
    """损坏的符号链接 / 链接环会让 resolve() 抛 OSError，必须转成 ConfigError。"""

    def boom(self, *args, **kwargs):
        raise OSError("too many levels of symbolic links")

    monkeypatch.setattr(Path, "resolve", boom)
    with pytest.raises(ConfigError, match="outside vault"):
        config_module.ensure_inside_vault(tmp_path, tmp_path / "x.md")


# --- 符号链接环 ---------------------------------------------------------------


def make_symlink_loop(link: Path) -> None:
    """创建自引用符号链接（指向自身的文件名），用于触发链接环。"""
    try:
        link.symlink_to(link.name, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - 依赖宿主权限
        pytest.skip("当前环境不支持创建目录符号链接")


def test_init_rejects_symlink_loop_in_state_dir(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    make_symlink_loop(vault / STATE_DIRNAME)

    with pytest.raises(ConfigError, match="outside vault"):
        init_config(vault)

    assert {path.name for path in vault.iterdir()} == {".obsidian", STATE_DIRNAME}
    assert not (vault / DEFAULT_ARCHIVE_REL).exists()


def test_init_rejects_symlink_loop_in_vault_path(tmp_path):
    loop_vault = tmp_path / "loop-vault"
    make_symlink_loop(loop_vault)

    with pytest.raises(ConfigError, match="outside vault"):
        init_config(loop_vault)


def test_init_rejects_two_link_cycle(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    make_symlink_loop(vault / STATE_DIRNAME)
    # 再把环改成双跳：STATE -> other -> STATE
    (vault / STATE_DIRNAME).unlink()
    (vault / STATE_DIRNAME).symlink_to("other-dir", target_is_directory=True)
    (vault / "other-dir").symlink_to(STATE_DIRNAME, target_is_directory=True)

    with pytest.raises(ConfigError, match="outside vault"):
        init_config(vault)


def test_load_config_rejects_symlink_loop_in_state_dir(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    make_symlink_loop(vault / STATE_DIRNAME)

    with pytest.raises(ConfigError, match="outside vault"):
        load_config(vault)


def test_cli_init_symlink_loop_returns_1_without_traceback(tmp_path, capsys):
    from experience_data_asset_engine.cli import main

    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    make_symlink_loop(vault / STATE_DIRNAME)

    assert main(["init", "--vault", str(vault)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert captured.err.startswith("错误: ")
    assert "outside vault" in captured.err


def test_cli_doctor_symlink_loop_returns_1_without_traceback(tmp_path, capsys):
    from experience_data_asset_engine.cli import main

    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    make_symlink_loop(vault / STATE_DIRNAME)

    assert main(["doctor", "--vault", str(vault)]) == 1

    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "outside vault" in captured.err


def test_doctor_errors_when_archive_path_is_a_file(tmp_path):
    config = make_config(tmp_path, archive_exists=False)
    config.archive_dir.write_text("i am a file", encoding="utf-8")
    diagnostics = doctor(config)
    assert by_code(diagnostics, "archive.exists").level == "error"
    assert by_code(diagnostics, "archive.writable").level == "error"


def test_doctor_errors_when_state_dir_missing(tmp_path):
    config = make_config(tmp_path)
    config.state_dir.rmdir()
    assert by_code(doctor(config), "state.writable").level == "error"


def test_doctor_warns_without_model_provider(tmp_path):
    item = by_code(doctor(make_config(tmp_path)), "model.provider")
    assert item.level == "warn"
    assert "staging" in item.message or "蒸馏" in item.message


def test_doctor_warns_when_provider_has_no_credentials(tmp_path):
    config = dataclasses.replace(
        make_config(tmp_path),
        provider="openai-compatible",
        model="demo-model",
        base_url="https://example.invalid/v1",
    )
    diagnostics = doctor(config)
    assert by_code(diagnostics, "model.provider").level == "ok"
    assert by_code(diagnostics, "model.credentials").level == "warn"


def test_doctor_ok_when_provider_and_api_key_present(tmp_path, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-test-only")
    config = dataclasses.replace(
        make_config(tmp_path),
        provider="openai-compatible",
        model="demo-model",
        base_url="https://example.invalid/v1",
    )
    assert by_code(doctor(config), "model.credentials").level == "ok"


def test_doctor_reports_zstandard_status(tmp_path):
    item = by_code(doctor(make_config(tmp_path)), "codex.zstandard")
    assert item.level in {"ok", "warn"}
    assert "zstandard" in item.message


def test_doctor_warns_when_zstandard_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "zstandard_version", lambda: None)
    item = by_code(doctor(make_config(tmp_path)), "codex.zstandard")
    assert item.level == "warn"
    assert "zstandard" in item.message


def test_doctor_never_prints_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("EDAE_API_KEY", "sk-mustnotleak")
    diagnostics = doctor(make_config(tmp_path))
    assert all("sk-mustnotleak" not in item.message for item in diagnostics)
