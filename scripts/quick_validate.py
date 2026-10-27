#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Skill 元数据校验入口（可移植，不依赖第三方库）。

用法::

    python scripts/quick_validate.py [SKILL_DIR]
    python scripts/quick_validate.py            # 默认校验当前目录
    python scripts/quick_validate.py dist/unzip # 校验解压后的分发包

``SKILL_DIR`` 只要求包含一个 ``SKILL.md``，因此仓库根目录和解压后的 zip 根目录
都能直接校验——这正是「打包前后用同一把尺子」的意思。

退出码：

* ``0`` 校验通过
* ``1`` 校验不通过（元数据不合规）
* ``2`` 找不到 / 读不出 ``SKILL.md``

输出全部使用 ASCII，避免不同代码页下的显示差异；失败时逐条打印
``FAIL <字段>: <原因>``，可直接粘进 issue。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: 平台对 name / description / displayName 的常见上限。
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_DISPLAY_NAME_LENGTH = 64

HYPHEN_CASE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

REQUIRED_TOP_KEYS = ("name", "description", "license")
REQUIRED_METADATA_KEYS = ("slug", "displayName", "version")


class FrontmatterError(ValueError):
    """frontmatter 结构不合法（缺失、未闭合、写法不支持）。"""


def split_frontmatter(text: str) -> str:
    """取出 ``---`` 之间的 frontmatter 原文。"""
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise FrontmatterError("SKILL.md must start with a '---' frontmatter block")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index])
    raise FrontmatterError("frontmatter block is not closed by a second '---'")


def parse_frontmatter(block: str) -> dict[str, object]:
    """解析本仓库用到的 YAML 子集：顶层标量 + 一层嵌套映射。

    不用 PyYAML 是为了让校验器在只装了 ``pytest`` 的环境里也能跑
    （CI 的 dev-only job 就没有 PyYAML）。代价是不支持多行标量与列表，
    遇到时明确报错，而不是静默解析成错的值。
    """
    data: dict[str, object] = {}
    current_map: dict[str, str] | None = None

    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indented = raw[:1] in (" ", "\t")
        line = raw.strip()

        if indented:
            if current_map is None:
                raise FrontmatterError(f"unexpected indented line: {line!r}")
            if ":" not in line:
                raise FrontmatterError(f"expected 'key: value' in nested block: {line!r}")
            key, value = line.split(":", 1)
            current_map[key.strip()] = _clean(value)
            continue

        if ":" not in line:
            raise FrontmatterError(f"expected 'key: value': {line!r}")
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if value == "":
            current_map = {}
            data[key] = current_map
            continue
        if value in ("|", ">", "|-", ">-"):
            raise FrontmatterError(f"multi-line scalar is not supported: {key!r}")
        current_map = None
        data[key] = _clean(value)

    return data


def _clean(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def validate(skill_dir: Path) -> tuple[list[str], dict[str, str]]:
    """返回 (问题列表, 关键字段摘要)。问题为空即通过。"""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise FileNotFoundError(f"SKILL.md not found in {skill_dir}")

    text = skill_md.read_text(encoding="utf-8")
    block = split_frontmatter(text)
    data = parse_frontmatter(block)

    problems: list[str] = []

    for key in REQUIRED_TOP_KEYS:
        if not str(data.get(key, "")).strip():
            problems.append(f"{key}: required but missing or empty")

    name = str(data.get("name", "")).strip()
    if name:
        if not HYPHEN_CASE.match(name):
            problems.append(f"name: {name!r} is not hyphen-case (e.g. 'my-skill-name')")
        if len(name) > MAX_NAME_LENGTH:
            problems.append(f"name: longer than {MAX_NAME_LENGTH} characters ({len(name)})")

    description = str(data.get("description", "")).strip()
    if description and len(description) > MAX_DESCRIPTION_LENGTH:
        problems.append(
            f"description: longer than platform limit {MAX_DESCRIPTION_LENGTH} "
            f"characters ({len(description)})"
        )

    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        problems.append("metadata: required nested block is missing")
        metadata = {}
    for key in REQUIRED_METADATA_KEYS:
        if not str(metadata.get(key, "")).strip():
            problems.append(f"metadata.{key}: required but missing or empty")

    slug = str(metadata.get("slug", "")).strip()
    if slug:
        if not HYPHEN_CASE.match(slug):
            problems.append(f"metadata.slug: {slug!r} is not hyphen-case")
        if name and slug != name:
            problems.append(f"metadata.slug: {slug!r} does not match name {name!r}")

    display_name = str(metadata.get("displayName", "")).strip()
    if display_name:
        if len(display_name) > MAX_DISPLAY_NAME_LENGTH:
            problems.append(
                f"metadata.displayName: longer than {MAX_DISPLAY_NAME_LENGTH} "
                f"characters ({len(display_name)})"
            )
        if display_name != display_name.strip():
            problems.append("metadata.displayName: has leading or trailing spaces")

    version = str(metadata.get("version", "")).strip()
    if version and not SEMVER.match(version):
        problems.append(f"metadata.version: {version!r} is not MAJOR.MINOR.PATCH")

    summary = {
        "name": name,
        "slug": slug,
        "displayName": display_name,
        "version": version,
        "license": str(data.get("license", "")).strip(),
        "description_length": str(len(description)),
    }
    return problems, summary


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    skill_dir = Path(args[0]) if args else Path(".")
    try:
        problems, summary = validate(skill_dir)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        return 2
    except (FrontmatterError, OSError) as exc:
        print(f"FAIL frontmatter: {exc}")
        print("FAILED: 1 problem(s)")
        return 1

    print(f"skill_dir: {skill_dir.resolve()}")
    for key, value in summary.items():
        print(f"OK   {key}={value}")

    if problems:
        for item in problems:
            print(f"FAIL {item}")
        print(f"FAILED: {len(problems)} problem(s)")
        return 1

    print("PASS: skill metadata is valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
