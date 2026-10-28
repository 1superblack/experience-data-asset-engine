"""Windows 控制台编码回归：CLI 的中文输出不能依赖外部 UTF-8 环境变量。

真实场景：用户装完包，在中文 Windows 的 cmd / PowerShell 里直接敲
``experience-data-asset-engine doctor --vault .``，此时 ``PYTHONUTF8`` 与
``PYTHONIOENCODING`` 都没设，Python 会按控制台代码页（cp936）写 stdout/stderr，
UTF-8 终端读出来就是乱码。修复在 ``cli.main()`` 里：入口处把 stdout/stderr
reconfigure 成 UTF-8。

所以这个测试**必须清空这两个变量**，否则测的是"用户已经手动修好了"的场景，
什么都没验证到。为了让清理是显式的、可审计的，这里用 ``_env_without_utf8()``
统一构造环境；``tests/test_package.py`` 的护栏认得这个函数名，其余子进程调用
仍必须走 ``utf8_env()``。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "cli.py"

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="编码问题只在 Windows 控制台代码页（cp936）下复现",
)


def _env_without_utf8() -> dict[str, str]:
    """复制当前环境，并**故意删掉**两个 UTF-8 变量，模拟用户没设过的普通 shell。"""
    env = os.environ.copy()
    env.pop("PYTHONUTF8", None)
    env.pop("PYTHONIOENCODING", None)
    return env


def test_doctor_prints_readable_chinese_without_utf8_env(tmp_path: Path) -> None:
    """未设任何 UTF-8 变量时，stderr 里的中文必须仍可读，不能是乱码。"""
    plain_dir = tmp_path / "not-a-vault"
    plain_dir.mkdir()

    proc = subprocess.run(
        [sys.executable, str(CLI), "doctor", "--vault", str(plain_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_env_without_utf8(),
        check=False,
    )

    combined = proc.stdout + proc.stderr
    assert proc.returncode == 1, combined
    assert "错误:" in proc.stderr, f"stderr 缺少中文错误前缀：{proc.stderr!r}"
    assert "\ufffd" not in combined, f"输出出现替换字符（说明按非 UTF-8 编码写出）：{combined!r}"
    assert "未初始化" in proc.stderr, f"错误正文丢失：{proc.stderr!r}"


def test_version_output_is_ascii_safe(tmp_path: Path) -> None:
    """``--version`` 只含 ASCII，任何代码页下都不该坏——作为对照基线。"""
    proc = subprocess.run(
        [sys.executable, str(CLI), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_env_without_utf8(),
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "experience-data-asset-engine 1.0.0"
