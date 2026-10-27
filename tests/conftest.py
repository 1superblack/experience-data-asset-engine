"""测试公共配置：路径注入、环境隔离、临时 vault。

测试不得触碰真实 vault、真实对话或真实凭据，全部使用 tmp_path 下的隔离目录与
tests/fixtures 中的合成样本。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


_ENV_PREFIX = "OKD_"


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch):
    """清除宿主环境中的本工具变量，避免测试读到真实配置。"""
    import os

    for name in list(os.environ):
        if name.startswith(_ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def sample_vault(tmp_path: Path) -> Path:
    """一个最小的 Obsidian vault 目录（含 .obsidian）。"""
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    return vault
