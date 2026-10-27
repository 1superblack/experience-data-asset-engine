"""测试公共配置：路径注入、环境隔离、临时 vault。

测试不得触碰真实 vault、真实对话或真实凭据，全部使用 tmp_path 下的隔离目录与
tests/fixtures 中的合成样本。
"""

from __future__ import annotations

import shutil
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


_ENV_PREFIX = "EDAE_"


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch):
    """清除宿主环境中的本工具变量，避免测试读到真实配置。"""
    import os

    for name in list(os.environ):
        if name.startswith(_ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture(autouse=True)
def isolate_from_host_proxy(monkeypatch: pytest.MonkeyPatch):
    """让测试不依赖宿主的 HTTP_PROXY / HTTPS_PROXY。

    宿主（CI 容器、企业网络、沙箱）常会注入代理变量，urllib 会因此把所有请求送进代理：
    指向 loopback 的用例会被代理改写结果（例如「连接被拒」变成代理返回的 502），
    于是同一份代码在不同机器上时绿时红。这里统一把全局 opener 换成一个
    空 ``ProxyHandler`` 的直连 opener，让 loopback 请求真的走本机；
    monkeypatch 负责在用例结束后还原，不污染其它用例。

    这是测试侧隔离，不是放宽断言——被验证的映射逻辑一点没改。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    monkeypatch.setattr(urllib.request, "_opener", opener)
    yield


@pytest.fixture
def sample_vault(tmp_path: Path) -> Path:
    """一个最小的 Obsidian vault 目录（含 .obsidian）。"""
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    return vault


@pytest.fixture
def basic_vault(tmp_path: Path) -> Path:
    """把 ``tests/fixtures/vault-basic`` 复制成临时 vault，并补上 ``.obsidian``。

    fixture 目录里只存 Markdown：``.obsidian`` 是空目录，git 不跟踪空目录，
    也没必要为它存一个占位文件，所以在测试里补。
    复制而不是直接指向 fixture，是为了让用例可以在副本上随意加笔记而不污染样本。
    """
    target = tmp_path / "vault"
    shutil.copytree(FIXTURES / "vault-basic", target)
    (target / ".obsidian").mkdir(exist_ok=True)
    return target
