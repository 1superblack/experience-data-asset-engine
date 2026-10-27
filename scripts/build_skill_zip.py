#!/usr/bin/env python3
"""把仓库打包成 SkillHub 可用的 zip。

三条硬约束：

1. **根目录直挂 ``SKILL.md``**：解压后 ``SKILL.md`` 就在第一层，不套一层
   ``repo-name/`` 子目录——SkillHub 只认根目录的 ``SKILL.md``。
2. **可复现**：同一份源码两次打包，zip 的 SHA256 必须一致。所以条目按路径排序、
   ``ZipInfo.date_time`` 固定为 1980-01-01、``external_attr`` 固定为 0o644、
   压缩级别固定。任何一项随时间变化都会让哈希漂移。
3. **出厂前自检**：打包后回读 zip，只要检出被排除的路径、疑似凭据或个人绝对路径，
   就以非零码退出——宁可打不出包，也不发一个带隐私的包。

用法::

    python scripts/build_skill_zip.py --output dist/experience-data-asset-engine.zip
    python scripts/build_skill_zip.py --output dist/x.zip --list
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath

# --- 打包白名单（相对仓库根，正斜杠） ---------------------------------------

ROOT_FILES = (
    "SKILL.md",
    "LICENSE",
    "README.md",
    "CHANGELOG.md",
    "SECURITY.md",
)
ROOT_DIRS = (
    "scripts",
    "src",
    "references",
    "agents",
)

# --- 排除规则 ---------------------------------------------------------------
# 目录名整段匹配；文件名按 fnmatch 匹配（不含目录分隔符）。
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".github",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "htmlcov",
        "dist",
        "build",
        "tests",
        ".experience-data-asset-engine",
        ".idea",
        ".vscode",
    }
)
EXCLUDED_FILE_PATTERNS = (
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    "config.local.json",
    "*.pyc",
    "*.pyo",
    "*.tmp",
    "*.swp",
    "*.swo",
    "*.pending.md",
    ".coverage",
    ".coverage.*",
)

# --- 自检规则 ---------------------------------------------------------------

CREDENTIAL_PATTERNS = (
    re.compile(rb"sk-[A-Za-z0-9]{16,}"),
    re.compile(rb"BEGIN [A-Z ]*PRIVATE KEY"),
)
# 只按「形状」匹配，不把本机账号名、盘符写死进规则——否则脚本自己就会命中自己。
# 具体的账号名断言放在 tests/test_package.py 里对产物做校验。
PERSONAL_PATH_PATTERNS = (
    re.compile(rb"[A-Za-z]:\\+Users\\+[^\\\s\"'<>|]+"),
    re.compile(rb"[A-Za-z]:\\+obsidian", re.IGNORECASE),
    re.compile(rb"/home/[A-Za-z0-9._-]+/"),
    re.compile(rb"/Users/[A-Za-z0-9._-]+/"),
)

# 1980-01-01 00:00:00 是 zip 格式能表示的最早时间，用常量保证哈希稳定。
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
FIXED_FILE_MODE = 0o644
COMPRESS_LEVEL = 9


class PackageError(RuntimeError):
    """打包自检失败（不能发布）。"""


def is_excluded(rel: PurePosixPath) -> bool:
    """判断相对路径是否应被排除。"""
    for part in rel.parts[:-1]:
        if part in EXCLUDED_DIR_NAMES:
            return True
    if rel.parts[-1] in EXCLUDED_DIR_NAMES:
        return True
    return any(fnmatch.fnmatch(rel.parts[-1], pattern) for pattern in EXCLUDED_FILE_PATTERNS)


def collect_entries(repo_root: Path) -> list[PurePosixPath]:
    """收集要入包的文件，按 posix 路径排序（保证跨平台顺序一致）。"""
    entries: list[PurePosixPath] = []
    missing: list[str] = []

    for name in ROOT_FILES:
        path = repo_root / name
        if path.is_file():
            entries.append(PurePosixPath(name))
        elif name == "SKILL.md" or name in ("LICENSE", "README.md"):
            # 这三个是 SkillHub 的必需项，缺了直接失败，不静默跳过。
            missing.append(name)

    for dirname in ROOT_DIRS:
        base = repo_root / dirname
        if not base.is_dir():
            missing.append(dirname + "/")
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = PurePosixPath(path.relative_to(repo_root).as_posix())
            if is_excluded(rel):
                continue
            entries.append(rel)

    if missing:
        raise PackageError("缺少必需条目：" + "、".join(missing))

    entries.sort(key=lambda item: item.as_posix())
    return entries


def write_zip(repo_root: Path, output: Path, entries: list[PurePosixPath]) -> None:
    """按固定元信息写 zip，保证同源同哈希。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=COMPRESS_LEVEL
    ) as archive:
        for rel in entries:
            data = (repo_root / rel).read_bytes()
            info = zipfile.ZipInfo(rel.as_posix(), date_time=FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # 固定为 Unix，避免打包机系统差异影响字节
            info.external_attr = FIXED_FILE_MODE << 16
            archive.writestr(info, data)


def verify_zip(archive_path: Path) -> list[str]:
    """回读 zip 自检；返回问题清单（空表示通过）。"""
    problems: list[str] = []
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if "SKILL.md" not in names:
            problems.append("根目录缺少 SKILL.md")
        for name in names:
            rel = PurePosixPath(name)
            if is_excluded(rel):
                problems.append(f"包含被排除的路径：{name}")
            depth = len(rel.parts)
            head = rel.parts[0]
            if depth > 1 and head not in ROOT_DIRS:
                problems.append(f"存在未授权顶层目录：{head}/（来自 {name}）")
            data = archive.read(name)
            for pattern in CREDENTIAL_PATTERNS:
                if pattern.search(data):
                    problems.append(f"疑似凭据：{name} 命中 {pattern.pattern!r}")
            for pattern in PERSONAL_PATH_PATTERNS:
                if pattern.search(data):
                    problems.append(f"疑似个人路径：{name} 命中 {pattern.pattern!r}")
        if names != sorted(names):
            problems.append("zip 条目未按路径排序")
    return problems


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(
    repo_root: Path,
    output: Path,
    *,
    verify: bool = True,
) -> tuple[Path, str, list[PurePosixPath]]:
    """打包并（可选）自检，返回 (zip 路径, SHA256, 条目清单)。"""
    entries = collect_entries(repo_root)
    write_zip(repo_root, output, entries)
    if verify:
        problems = verify_zip(output)
        if problems:
            raise PackageError("打包自检未通过：\n" + "\n".join(f"  - {item}" for item in problems))
    return output, sha256_of(output), entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把仓库打包成 SkillHub 可用的 zip（可复现）")
    parser.add_argument(
        "--output",
        default="dist/experience-data-asset-engine.zip",
        help="输出 zip 路径（默认 dist/experience-data-asset-engine.zip）",
    )
    parser.add_argument(
        "--repo",
        default=str(Path(__file__).resolve().parents[1]),
        help="仓库根目录（默认取本脚本的上一级）",
    )
    parser.add_argument("--list", action="store_true", help="额外列出包内每个文件")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo).resolve()
    output = Path(args.output)
    if not output.is_absolute():
        output = (Path.cwd() / output).resolve()

    try:
        archive_path, digest, entries = build(repo_root, output)
    except PackageError as exc:
        print(f"[build_skill_zip] 失败：{exc}", file=sys.stderr)
        return 1

    print(f"zip: {archive_path.as_posix()}")
    print(f"sha256: {digest}")
    print(f"files: {len(entries)}")
    if args.list:
        for rel in entries:
            print(f"  {rel.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
