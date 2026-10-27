"""vault 配置、路径边界与 ``doctor`` 诊断。

设计要点：

- 配置固定写入 ``<vault>/.obsidian-knowledge-distiller/config.json``，跟着 vault 走。
- 配置里只存**vault 内相对路径**，不写机器绝对路径，便于 vault 搬家与同步。
- 任何路径都必须落在 vault 内；越界一律抛 ``ConfigError``（失败关闭）。
- 凭据只从环境变量读取，永不落盘。
"""

from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from . import __version__
from .errors import ConfigError, InputError, SchemaError

DEFAULT_ARCHIVE_REL = "04-对话存档"
STATE_DIRNAME = ".obsidian-knowledge-distiller"
CONFIG_FILENAME = "config.json"
CONFIG_SCHEMA_VERSION = 1
DEFAULT_API_KEY_ENV = "OKD_API_KEY"
MIN_PYTHON = (3, 11)

ENV_VAULT = "OKD_VAULT"
ENV_CONFIG = "OKD_CONFIG"
ENV_PROVIDER = "OKD_PROVIDER"
ENV_MODEL = "OKD_MODEL"
ENV_BASE_URL = "OKD_BASE_URL"
ENV_API_KEY_ENV = "OKD_API_KEY_ENV"

DiagnosticLevel = Literal["ok", "warn", "error"]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """一条自检结论。level 只有 ok / warn / error 三档。"""

    code: str
    level: DiagnosticLevel
    message: str
    hint: str | None = None

    @property
    def is_error(self) -> bool:
        return self.level == "error"


@dataclass(frozen=True, slots=True)
class AppConfig:
    """一次运行的完整配置快照。字段与 references/config.md 一一对应。"""

    vault: Path
    archive_dir: Path
    state_dir: Path
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_env: str = DEFAULT_API_KEY_ENV
    schema_version: int = CONFIG_SCHEMA_VERSION

    @property
    def config_path(self) -> Path:
        return self.state_dir / CONFIG_FILENAME

    @property
    def state_path(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def audit_dir(self) -> Path:
        return self.state_dir / "audit"

    @property
    def staging_dir(self) -> Path:
        return self.state_dir / "staging"

    @property
    def archive_rel(self) -> str:
        return self.archive_dir.relative_to(self.vault).as_posix()

    def api_key(self) -> str | None:
        """凭据只从环境变量读，永不来自配置文件。"""
        value = os.environ.get(self.api_key_env)
        return value if value and value.strip() else None


# --- 路径边界 ---------------------------------------------------------------


def ensure_inside_vault(base: Path, target: Path) -> Path:
    """确认 target 解析后仍在 base 内，返回解析后的绝对路径。

    符号链接会先被解析，因此「通过符号链接跳到 vault 外」同样会被拒绝。
    """
    base_resolved = Path(base).resolve()
    target_resolved = Path(target).resolve()
    if target_resolved.is_relative_to(base_resolved):
        return target_resolved
    raise ConfigError(f"拒绝访问 vault 之外的路径（outside vault）: {target}")


def _archive_dir_from_rel(vault: Path, archive_rel: str) -> Path:
    raw = (archive_rel or "").strip()
    if not raw:
        raise ConfigError("归档目录不能为空")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise ConfigError("归档目录必须是 vault 内的相对路径，不能使用绝对路径")
    if ".." in candidate.parts:
        raise ConfigError("归档目录不能包含 ..，必须位于 vault 内")
    if candidate.parts and candidate.parts[0].startswith("."):
        raise ConfigError(
            f"归档目录不能是隐藏目录（{candidate.parts[0]}），Obsidian 默认不展示隐藏目录"
        )
    resolved = ensure_inside_vault(vault, vault / candidate)
    if resolved == Path(vault).resolve():
        raise ConfigError("归档目录不能是 vault 根目录，请指定子目录")
    return resolved


# --- 读写工具 ---------------------------------------------------------------


def read_json_file(path: Path) -> Any:
    """读 JSON：缺失或不可读抛 InputError，语法错误抛 SchemaError（带行号）。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise InputError(f"文件不存在: {path}") from None
    except OSError as exc:
        raise InputError(f"无法读取文件: {path}（{exc}）") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaError(
            f"JSON 解析失败: {path} 第 {exc.lineno} 行第 {exc.colno} 列: {exc.msg}"
        ) from None


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> Path:
    """临时文件 + 原子替换；失败时清理临时文件，不留半截文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-", suffix=".json")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def _ensure_dir(directory: Path, label: str) -> Path:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"无法创建{label}: {directory}（{exc}）") from exc
    if not directory.is_dir():
        raise ConfigError(f"{label}不可用（同名文件已存在）: {directory}")
    return directory


def _now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _probe_writable(directory: Path) -> str | None:
    """在目录内写一个探针文件再删掉，返回失败原因（成功返回 None）。"""
    try:
        handle, name = tempfile.mkstemp(prefix=".okd-probe-", suffix=".tmp", dir=str(directory))
    except OSError as exc:
        return str(exc)
    os.close(handle)
    Path(name).unlink(missing_ok=True)
    return None


# --- init / load ------------------------------------------------------------


def init_config(vault: Path, archive_rel: str = DEFAULT_ARCHIVE_REL) -> AppConfig:
    """初始化 vault 配置。已存在配置时不覆盖，直接返回既有配置。"""
    vault_path = Path(vault).expanduser().resolve()
    if not vault_path.is_dir():
        raise ConfigError(f"vault 目录不存在或不是目录: {vault_path}")
    archive_dir = _archive_dir_from_rel(vault_path, archive_rel)
    state_dir = vault_path / STATE_DIRNAME
    _ensure_dir(state_dir, "状态目录")

    config_path = state_dir / CONFIG_FILENAME
    if config_path.exists():
        return load_config(vault_path)

    _ensure_dir(archive_dir, "归档目录")
    payload: dict[str, Any] = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "archive_dir": archive_dir.relative_to(vault_path).as_posix(),
        "provider": None,
        "model": None,
        "base_url": None,
        "api_key_env": DEFAULT_API_KEY_ENV,
        "created_at": _now_iso(),
        "tool_version": __version__,
    }
    write_json_atomic(config_path, payload)
    return load_config(vault_path)


def _locate_config(vault: Path | None) -> Path:
    if vault is not None:
        vault_path = Path(vault).expanduser().resolve()
        if not vault_path.is_dir():
            raise ConfigError(f"vault 目录不存在或不是目录: {vault_path}")
        config_path = vault_path / STATE_DIRNAME / CONFIG_FILENAME
        if not config_path.is_file():
            raise ConfigError(
                f"未初始化：找不到配置 {config_path}，请先运行 init --vault <vault>"
            )
        return config_path

    env_config = os.environ.get(ENV_CONFIG)
    if env_config and env_config.strip():
        candidate = Path(env_config).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"{ENV_CONFIG} 指向的配置文件不存在: {candidate}")
        return candidate.resolve()

    env_vault = os.environ.get(ENV_VAULT)
    if env_vault and env_vault.strip():
        return _locate_config(Path(env_vault))

    current = Path.cwd()
    for parent in (current, *current.parents):
        candidate = parent / STATE_DIRNAME / CONFIG_FILENAME
        if candidate.is_file():
            return candidate.resolve()

    raise ConfigError(
        "未初始化：当前目录及上层都没有 "
        f"{STATE_DIRNAME}/{CONFIG_FILENAME}，请先运行 init --vault <vault>"
    )


def _optional_str(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"配置字段 {field} 必须是字符串或 null")
    stripped = value.strip()
    return stripped or None


def _env_override(env_name: str, fallback: str | None) -> str | None:
    value = os.environ.get(env_name)
    if value is not None and value.strip():
        return value.strip()
    return fallback


def _read_config(config_path: Path) -> AppConfig:
    if config_path.parent.name != STATE_DIRNAME:
        raise ConfigError(
            "配置文件必须位于 <vault>/"
            f"{STATE_DIRNAME}/{CONFIG_FILENAME}，当前路径不符合约定: {config_path}"
        )
    payload = read_json_file(config_path)
    if not isinstance(payload, dict):
        raise ConfigError(f"配置文件必须是 JSON 对象: {config_path}")

    version = payload.get("schema_version")
    if version != CONFIG_SCHEMA_VERSION:
        raise ConfigError(
            f"不支持的 schema_version: {version!r}（当前支持 {CONFIG_SCHEMA_VERSION}）"
        )

    archive_rel = payload.get("archive_dir")
    if not isinstance(archive_rel, str) or not archive_rel.strip():
        raise ConfigError("配置缺少 archive_dir，请重新运行 init")

    vault_path = config_path.parent.parent.resolve()
    archive_dir = _archive_dir_from_rel(vault_path, archive_rel)
    state_dir = config_path.parent.resolve()

    provider = _env_override(ENV_PROVIDER, _optional_str(payload.get("provider"), "provider"))
    model = _env_override(ENV_MODEL, _optional_str(payload.get("model"), "model"))
    base_url = _env_override(ENV_BASE_URL, _optional_str(payload.get("base_url"), "base_url"))
    api_key_env = _env_override(
        ENV_API_KEY_ENV, _optional_str(payload.get("api_key_env"), "api_key_env")
    ) or DEFAULT_API_KEY_ENV

    return AppConfig(
        vault=vault_path,
        archive_dir=archive_dir,
        state_dir=state_dir,
        provider=provider,
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
    )


def load_config(vault: Path | None = None) -> AppConfig:
    """定位并读取配置。

    定位顺序：显式 ``vault`` 参数 -> ``OKD_CONFIG`` -> ``OKD_VAULT`` ->
    从当前目录逐层向上找 ``.obsidian-knowledge-distiller/config.json``。
    """
    return _read_config(_locate_config(vault))


# --- doctor -----------------------------------------------------------------


def zstandard_version() -> str | None:
    """返回可用的 zstandard 版本，未安装时返回 None。"""
    try:
        import zstandard  # noqa: PLC0415
    except ImportError:
        return None
    return getattr(zstandard, "__version__", "unknown")


def _diag_python() -> Diagnostic:
    current = platform.python_version()
    if sys.version_info[:2] >= MIN_PYTHON:
        return Diagnostic(
            "python.version",
            "ok",
            f"Python {current} (要求 >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})",
        )
    return Diagnostic(
        "python.version",
        "error",
        f"Python {current} 过低，要求 >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
        hint="请升级 Python 后重试",
    )


def _diag_vault(config: AppConfig) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if config.vault.is_dir():
        diagnostics.append(Diagnostic("vault.exists", "ok", f"vault 目录可用: {config.vault}"))
    else:
        diagnostics.append(
            Diagnostic(
                "vault.exists",
                "error",
                f"vault 目录不存在: {config.vault}",
                hint="检查路径是否写错，或运行 init --vault <vault> 重新配置",
            )
        )
        return diagnostics

    if (config.vault / ".obsidian").is_dir():
        diagnostics.append(Diagnostic("vault.obsidian", "ok", "检测到 .obsidian 目录"))
    else:
        diagnostics.append(
            Diagnostic(
                "vault.obsidian",
                "error",
                "未找到 .obsidian 目录：目标不是 Obsidian vault",
                hint="确认传入的是 vault 根目录，而不是其中某个子目录",
            )
        )
    return diagnostics


def _diag_archive(config: AppConfig) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    try:
        ensure_inside_vault(config.vault, config.archive_dir)
    except ConfigError as exc:
        diagnostics.append(Diagnostic("archive.inside_vault", "error", str(exc)))
    else:
        diagnostics.append(
            Diagnostic("archive.inside_vault", "ok", f"归档目录位于 vault 内: {config.archive_rel}")
        )

    if config.archive_dir.is_dir():
        diagnostics.append(
            Diagnostic("archive.exists", "ok", f"归档目录存在: {config.archive_rel}")
        )
        failure = _probe_writable(config.archive_dir)
        if failure is None:
            diagnostics.append(Diagnostic("archive.writable", "ok", "归档目录可写"))
        else:
            diagnostics.append(
                Diagnostic("archive.writable", "error", f"归档目录不可写（{failure}）")
            )
    elif config.archive_dir.exists():
        message = f"归档路径被占用但不是目录: {config.archive_dir}"
        diagnostics.append(Diagnostic("archive.exists", "error", message))
        diagnostics.append(Diagnostic("archive.writable", "error", message))
    else:
        diagnostics.append(
            Diagnostic(
                "archive.exists",
                "warn",
                f"归档目录还不存在，首次归档时创建: {config.archive_rel}",
            )
        )
        diagnostics.append(
            Diagnostic("archive.writable", "ok", "父目录可写，归档时会自动创建目录")
        )
    return diagnostics


def _diag_state(config: AppConfig) -> Diagnostic:
    if not config.state_dir.is_dir():
        return Diagnostic(
            "state.writable",
            "error",
            f"状态目录缺失或不是目录: {config.state_dir}",
            hint="重新运行 init --vault <vault> 可恢复（不会覆盖已有配置）",
        )
    failure = _probe_writable(config.state_dir)
    if failure is not None:
        return Diagnostic("state.writable", "error", f"状态目录不可写（{failure}）")
    return Diagnostic("state.writable", "ok", "状态目录可写")


def _diag_model(config: AppConfig) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if not config.provider:
        diagnostics.append(
            Diagnostic(
                "model.provider",
                "warn",
                "未配置模型：archive 只会生成 staging 请求，等待 Agent 完成蒸馏，不会把原文当笔记写入",
                hint=f"如需脚本直连，设置 {ENV_PROVIDER}/{ENV_MODEL}/{ENV_BASE_URL}",
            )
        )
        return diagnostics

    model_label = config.model or "(未指定模型)"
    diagnostics.append(
        Diagnostic("model.provider", "ok", f"模型提供方: {config.provider} / {model_label}")
    )
    if config.api_key():
        diagnostics.append(
            Diagnostic(
                "model.credentials",
                "ok",
                f"已从环境变量 {config.api_key_env} 读取凭据（不落盘）",
            )
        )
    else:
        diagnostics.append(
            Diagnostic(
                "model.credentials",
                "warn",
                f"未检测到环境变量 {config.api_key_env}，脚本直连模型时会失败",
                hint="也可以不配置模型，改为让 Agent 直接产出蒸馏 JSON",
            )
        )
    return diagnostics


def _diag_codex_dependency() -> Diagnostic:
    version = zstandard_version()
    if version is None:
        return Diagnostic(
            "codex.zstandard",
            "warn",
            "未安装 zstandard：读取 .jsonl.zst 会失败关闭（不会降级读原文）",
            hint="pip install 'obsidian-knowledge-distiller[codex]'",
        )
    return Diagnostic(
        "codex.zstandard",
        "ok",
        f"zstandard {version} 可用（支持 Codex .jsonl.zst）",
    )


def doctor(config: AppConfig) -> list[Diagnostic]:
    """只读自检：不修改任何笔记，只在状态目录/归档目录写一个临时探针文件。"""
    diagnostics: list[Diagnostic] = [_diag_python()]
    diagnostics.extend(_diag_vault(config))
    diagnostics.extend(_diag_archive(config))
    diagnostics.append(_diag_state(config))
    diagnostics.extend(_diag_model(config))
    diagnostics.append(_diag_codex_dependency())
    return diagnostics


def summarize_diagnostics(diagnostics: list[Diagnostic]) -> str:
    """一行汇总，供 CLI 与测试复用。"""
    ok = sum(1 for item in diagnostics if item.level == "ok")
    warn = sum(1 for item in diagnostics if item.level == "warn")
    error = sum(1 for item in diagnostics if item.level == "error")
    return f"诊断汇总: {ok} 项通过, {warn} 项警告, {error} 项错误"
