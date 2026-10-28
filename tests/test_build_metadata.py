"""构建元数据回归：SPDX license 与 setuptools 下限必须成对存在。

背景（真踩过）：``license = { text = "MIT" }`` 换成 SPDX 字符串 ``license = "MIT"``
之后，setuptools 76 及更早版本会直接解析失败——

    setuptools.errors.InvalidConfigError:
    License classifiers have been superseded by license expressions.

实测边界：68.2.2 失败、76.1.0 失败、77.0.3 成功。所以 ``build-system.requires``
的下限必须是 77；写成 ``>=68`` 会让任何用旧 setuptools 的构建环境（含 CI 缓存、
用户本地 wheel）直接装不上。

这里直接读 ``pyproject.toml`` 断言，而不是跑一次真实构建：构建慢且依赖网络，
而这三条都是纯静态事实——写错就是写错，不需要"构建成功"才成立。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
LICENSE_FILE = ROOT / "LICENSE"
COMMERCIAL_LICENSE_FILE = ROOT / "COMMERCIAL-LICENSE.md"

#: SPDX 字符串 license 需要的最低 setuptools 版本。
MIN_SETUPTOOLS_FOR_SPDX = 77


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _requires_versions() -> list[str]:
    return [str(item) for item in _pyproject()["build-system"]["requires"]]


def test_license_is_spdx_string() -> None:
    """``license`` 必须是 SPDX 字符串，不能回退成旧表形式。"""
    license_field = _pyproject()["project"]["license"]
    assert license_field == "AGPL-3.0-only", (
        f"license 应为 SPDX 字符串 'AGPL-3.0-only'，实际 {license_field!r}"
    )


def test_license_files_include_open_source_and_commercial_notices() -> None:
    """分发包必须同时带 AGPL 正文与商业授权说明。"""
    files = set(_pyproject()["project"]["license-files"])
    assert files == {"LICENSE", "COMMERCIAL-LICENSE.md"}


def test_setuptools_floor_is_77() -> None:
    """构建下限必须 >= 77，否则旧 setuptools 解析 SPDX license 会失败。"""
    required = " ".join(_requires_versions())
    assert f"setuptools>={MIN_SETUPTOOLS_FOR_SPDX}" in required, (
        f"build-system.requires 必须锁定 setuptools>={MIN_SETUPTOOLS_FOR_SPDX}，实际 {required!r}"
    )


def test_no_legacy_license_classifier() -> None:
    """``License ::`` classifier 与 SPDX 字符串互斥，不能重新加回来。"""
    classifiers = [str(item) for item in _pyproject()["project"].get("classifiers", [])]
    offenders = [item for item in classifiers if item.startswith("License ::")]
    assert not offenders, f"classifiers 里仍有旧式 License 分类：{offenders}"


def test_license_files_are_present_and_match_the_declared_model() -> None:
    """LICENSE 必须是完整 AGPL 正文，商业授权边界必须单独成文。"""
    license_text = LICENSE_FILE.read_text(encoding="utf-8")
    commercial_text = COMMERCIAL_LICENSE_FILE.read_text(encoding="utf-8")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in license_text
    assert "Version 3, 19 November 2007" in license_text
    assert "AGPL-3.0-only" in commercial_text
    assert "书面商业许可协议" in commercial_text
