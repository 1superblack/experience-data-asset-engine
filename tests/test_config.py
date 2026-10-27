from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

import pytest

from obsidian_knowledge_distiller import config as config_module
from obsidian_knowledge_distiller.config import (
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
    assert (vault / ".obsidian-knowledge-distiller" / "config.json").exists()


def test_init_creates_archive_directory(sample_vault):
    config = init_config(sample_vault)
    assert config.archive_dir.is_dir()
    assert config.state_dir.is_dir()


def test_init_does_not_overwrite_existing_config(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    init_config(vault)
    path = vault / ".obsidian-knowledge-distiller" / "config.json"
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
    monkeypatch.setenv("OKD_API_KEY", "sk-shouldneverbewritten")
    config = init_config(sample_vault)
    assert "sk-shouldneverbewritten" not in config.config_path.read_text(encoding="utf-8")


# --- load -----------------------------------------------------------------


def test_load_config_roundtrip(sample_vault):
    created = init_config(sample_vault)
    loaded = load_config(sample_vault)
    assert loaded == created


def test_load_config_from_env_vault(sample_vault, monkeypatch):
    created = init_config(sample_vault)
    monkeypatch.setenv("OKD_VAULT", str(sample_vault))
    assert load_config() == created


def test_load_config_from_env_config_path(sample_vault, monkeypatch):
    created = init_config(sample_vault)
    monkeypatch.setenv("OKD_CONFIG", str(created.config_path))
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
    monkeypatch.setenv("OKD_PROVIDER", "openai-compatible")
    monkeypatch.setenv("OKD_MODEL", "demo-model")
    monkeypatch.setenv("OKD_BASE_URL", "https://example.invalid/v1")
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
    monkeypatch.setenv("OKD_API_KEY", "sk-test-only")
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
    monkeypatch.setenv("OKD_API_KEY", "sk-mustnotleak")
    diagnostics = doctor(make_config(tmp_path))
    assert all("sk-mustnotleak" not in item.message for item in diagnostics)
