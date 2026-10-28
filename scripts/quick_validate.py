#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Skill 元数据校验入口（可移植，不依赖第三方库）。

用法::

    python scripts/quick_validate.py [SKILL_DIR]
    python scripts/quick_validate.py            # 默认校验当前目录
    python scripts/quick_validate.py dist/unzip # 校验解压后的分发包

``SKILL_DIR`` 只要求包含一个 ``SKILL.md``，因此仓库根目录和解压后的 zip 根目录
都能直接校验——这正是「打包前后用同一把尺子」的意思。

退出码契约（对外的硬承诺，``tests/test_quick_validate.py`` 逐条锁定）：

* ``0`` 校验通过
* ``1`` 元数据不合规——文件读得出来，但 frontmatter 结构、YAML 子集或字段规则非法
* ``2`` 读不出 ``SKILL.md``——不存在、不是普通文件、无权限、以及无法按 UTF-8 解码

读与判的边界就是 ``1`` / ``2`` 的边界：

* 取不到文件（路径不存在、无权限、I/O 失败）→ ``2``：环境问题，修路径或权限；
* 文件取到了、但内容不成立（**非 UTF-8 编码**、frontmatter 非法、字段不合规）→ ``1``：
  内容问题，改元数据。

编码问题刻意算 ``1`` 而不是 ``2``：文件本来就在，只是写坏了。任何情况下都不允许
冒 traceback。

输出契约：所有**动态值**（路径、displayName、报错里回显的原文）都经
``_ascii()`` 转义成 ``\\uXXXX`` 形式，因此 stdout/stderr 恒为纯 ASCII，
可以在任意代码页的终端里安全显示、比较和粘贴；固定提示文案本身也是英文 ASCII。

YAML 子集（刻意窄，遇到不支持的写法直接报错而不是猜）：

* 顶层 ``key: value`` 标量；
* 单层嵌套映射，子键缩进**恰好 2 个空格**；
* 不支持多行标量（``|`` / ``>``）、序列（``- `` 开头）、注释以外的自由格式；
* 重复键、缩进跳变、Tab 缩进都是错误。

代价是要自己维护这个小解析器，收益是校验器在只装了 ``pytest`` 的环境里也能跑
（CI 的 dev-only job 就没有 PyYAML）。所有解析错误都带行号和字段名。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: 平台对 name / description / displayName 的常见上限。
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_DISPLAY_NAME_LENGTH = 64

#: 退出码。调用方按这三个值分支，不要按输出字符串匹配。
EXIT_OK = 0
EXIT_INVALID = 1
EXIT_UNREADABLE = 2

#: 子键缩进只允许这一种宽度。
NESTED_INDENT = 2

HYPHEN_CASE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

REQUIRED_TOP_KEYS = ("name", "description", "license")
REQUIRED_METADATA_KEYS = ("slug", "displayName", "version")

USAGE = """usage: quick_validate.py [SKILL_DIR]

Validate the YAML frontmatter of SKILL.md inside SKILL_DIR (default: cwd).

exit codes:
  0  metadata is valid
  1  metadata is invalid (readable file, bad frontmatter / YAML subset / fields)
  2  SKILL.md cannot be read (missing, not a file, no permission, not UTF-8)

output is pure ASCII: dynamic values are escaped as \\uXXXX.
"""


class FrontmatterError(ValueError):
    """frontmatter 结构不合法（缺失、未闭合、写法不支持）。"""


def _ascii(value: object) -> str:
    """把任意值转成纯 ASCII 文本，非 ASCII 用 ``\\uXXXX`` / ``\\UXXXXXXXX`` 转义。"""
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


def split_frontmatter(text: str) -> str:
    """取出 ``---`` 之间的 frontmatter 原文。"""
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise FrontmatterError("line 1: SKILL.md must start with a '---' frontmatter block")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index])
    raise FrontmatterError("line 1: opening '---' is never closed by a second '---'")


def parse_frontmatter(block: str, first_line_no: int = 2) -> dict[str, object]:
    """解析本仓库用到的 YAML 子集：顶层标量 + 一层嵌套映射（子键缩进 2 空格）。

    ``first_line_no`` 是 ``block`` 第一行在 ``SKILL.md`` 里的真实行号，
    让报错能直接指向文件里的位置。

    不用 PyYAML 是为了让校验器在只装了 ``pytest`` 的环境里也能跑
    （CI 的 dev-only job 就没有 PyYAML）。代价是子集很窄：不支持多行标量与
    序列，缩进只认 2 空格，重复键直接报错——宁可炸，也不静默解析成错的值。
    """
    data: dict[str, object] = {}
    current_map: dict[str, str] | None = None
    current_map_name: str | None = None

    for offset, raw in enumerate(block.splitlines()):
        lineno = first_line_no + offset
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue

        stripped = raw.lstrip()
        indent_str = raw[: len(raw) - len(stripped)]
        if "\t" in indent_str:
            raise FrontmatterError(
                f"line {lineno}: tab indentation is not supported, use {NESTED_INDENT} spaces"
            )
        indent = len(indent_str)
        line = stripped

        if line == "-" or line.startswith("- "):
            raise FrontmatterError(f"line {lineno}: YAML sequences are not supported: {line!r}")
        if ":" not in line:
            raise FrontmatterError(f"line {lineno}: expected 'key: value': {line!r}")

        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if not key:
            raise FrontmatterError(f"line {lineno}: empty key before ':'")
        if value in ("|", ">", "|-", ">-"):
            raise FrontmatterError(f"line {lineno}: multi-line scalar is not supported: {key!r}")
        if value == "-" or value.startswith("- "):
            raise FrontmatterError(
                f"line {lineno}: inline YAML sequences are not supported: {key!r}"
            )

        if indent:
            if indent != NESTED_INDENT:
                raise FrontmatterError(
                    f"line {lineno}: {key!r} is indented by {indent} spaces; "
                    f"this subset only allows {NESTED_INDENT} spaces per level"
                )
            if current_map is None:
                raise FrontmatterError(
                    f"line {lineno}: unexpected indented key {key!r} (no parent mapping)"
                )
            if key in current_map:
                raise FrontmatterError(f"line {lineno}: duplicate key {current_map_name}.{key}")
            current_map[key] = _clean(value)
            continue

        if key in data:
            raise FrontmatterError(f"line {lineno}: duplicate key {key}")
        if value == "":
            current_map = {}
            current_map_name = key
            data[key] = current_map
            continue
        current_map, current_map_name = None, None
        data[key] = _clean(value)

    return data


def _clean(value: str) -> str:
    """去掉包裹的成对引号；**保留**值内部的空格，交给字段规则判断。"""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def validate(skill_dir: Path) -> tuple[list[str], dict[str, str]]:
    """返回 (问题列表, 关键字段摘要)。问题为空即通过。

    读取阶段的失败（不存在、无权限、解码失败）原样抛出，由 :func:`main` 映射成
    退出码 ``2``；内容阶段的失败会变成问题列表，由 :func:`main` 映射成 ``1``。
    """
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

    # 首尾空格必须用**未 strip** 的原始值判断，否则这条规则永远不成立。
    display_raw = str(metadata.get("displayName", ""))
    display_name = display_raw.strip()
    if display_name:
        if len(display_name) > MAX_DISPLAY_NAME_LENGTH:
            problems.append(
                f"metadata.displayName: longer than {MAX_DISPLAY_NAME_LENGTH} "
                f"characters ({len(display_name)})"
            )
        if display_raw != display_name:
            problems.append(
                "metadata.displayName: has leading or trailing whitespace "
                f"({display_raw!r}); trim it in SKILL.md"
            )

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
    """命令行入口：只返回 ``EXIT_OK`` / ``EXIT_INVALID`` / ``EXIT_UNREADABLE``。

    任何读取失败都不允许冒出 traceback——那些是契约的一部分，不是意外。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return EXIT_OK

    skill_dir = Path(args[0]) if args else Path(".")
    try:
        problems, summary = validate(skill_dir)
    except FileNotFoundError as exc:
        print(f"ERROR: {_ascii(exc)} (exit {EXIT_UNREADABLE})")
        return EXIT_UNREADABLE
    except FrontmatterError as exc:
        print(f"FAIL frontmatter: {_ascii(exc)}")
        print("FAILED: 1 problem(s)")
        return EXIT_INVALID
    except UnicodeDecodeError as exc:
        # 文件在、但字节不是合法 UTF-8：这是内容问题（1），不是环境问题（2）。
        print(f"FAIL encoding: SKILL.md is not valid UTF-8: {_ascii(exc)}")
        print("FAILED: 1 problem(s)")
        return EXIT_INVALID
    except (PermissionError, OSError) as exc:
        print(f"ERROR: cannot read SKILL.md: {_ascii(exc)} (exit {EXIT_UNREADABLE})")
        return EXIT_UNREADABLE

    print(f"skill_dir: {_ascii(skill_dir.resolve())}")
    for key, value in summary.items():
        print(f"OK   {key}={_ascii(value)}")

    if problems:
        for item in problems:
            print(f"FAIL {_ascii(item)}")
        print(f"FAILED: {len(problems)} problem(s)")
        return EXIT_INVALID

    print("PASS: skill metadata is valid")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
