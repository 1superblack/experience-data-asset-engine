#!/usr/bin/env python3
"""把仓库打包成 SkillHub 可用的 zip。

三条硬约束：

1. **根目录直挂 ``SKILL.md``**：解压后 ``SKILL.md`` 就在第一层，不套一层
   ``repo-name/`` 子目录——SkillHub 只认根目录的 ``SKILL.md``。
2. **可复现**：对**同一份文件内容**（CF 行尾差异不算内容差异，见下）在**同一台机器、
   同一 zlib 实现**下两次打包，zip 的 SHA256 必须一致。所以条目按路径排序、
   ``ZipInfo.date_time`` 固定为 1980-01-01、``external_attr`` 固定为 0o644、压缩级别固定。
   注意两道边界：
   - **行尾归一**：文本条目入包前统一把 ``\r\n`` 折成 ``\n``。否则同一个 commit、同一台机器，
     只要某个 worktree 被工具改写过行尾（``core.autocrlf`` 或整树复制），打出来的包就不同哈希——
     那是「检出差异」冒充「内容差异」，会让人误以为源码变了。归一只针对文本后缀，二进制原样入包。
   - **zlib**：deflate 的输出字节由底层 zlib/zlib-ng 版本决定，跨平台或跨压缩库版本
     不保证同哈希。因此这个 SHA256 只能当「同一检出下的本机校验值」，不要写进发布文档当
     跨机承诺；对外发布的校验值应由 CI（固定 ubuntu + LF 检出）产出。
3. **出厂前自检**：打包后回读 zip，只要检出被排除的路径、疑似凭据或个人绝对路径，
   就以非零码退出——宁可打不出包，也不发一个带隐私的包。
   自检必须发生在**临时文件**上：先写 ``<output>.<随机>.tmp``，回读通过才
   ``os.replace`` 到最终路径。失败时只删临时文件，最终路径（可能是一份旧的合法 zip）
   一个字节都不动。否则「自检失败」的结果恰恰是磁盘上留着一个带凭据的 zip。
4. **不收符号链接**：根级必需文件、``ROOT_DIRS`` 与递归子项全部检查符号链接
   （含 Windows junction / reparse point），并用 ``resolve(strict=True)`` 确认真实路径
   仍在仓库根内。命中即失败关闭——``references/leak.txt -> ~/.ssh/id_rsa`` 这种条目
   一旦入包，等于把仓库外的文件直接发出去。

用法::

    python scripts/build_skill_zip.py --output dist/experience-data-asset-engine.zip
    python scripts/build_skill_zip.py --output dist/x.zip --list
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

# --- 打包白名单（相对仓库根，正斜杠） ---------------------------------------

ROOT_FILES = (
    "SKILL.md",
    "LICENSE",
    "COMMERCIAL-LICENSE.md",
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
# 目录名也需要 fnmatch：``pip install -e`` 会留下 ``*.egg-info``（内含 SOURCES.txt、
# PKG-INFO 这类带本机绝对路径的构建元数据），名字随包名变化，写不进上面的固定集合。
EXCLUDED_DIR_PATTERNS = (
    "*.egg-info",
    "*.dist-info",
    "*.egg-link",
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

#: 入包前需要把 CRLF 折成 LF 的文本后缀。二进制（如图片、``.zst``）不在其中，原样入包。
TEXT_SUFFIXES = frozenset(
    {".md", ".py", ".yaml", ".yml", ".toml", ".json", ".txt", ".cfg", ".ini", ".sh", ".rst"}
)


class PackageError(RuntimeError):
    """打包自检失败（不能发布）。"""


#: Windows 上重解析点（symlink / junction / mount point）共有的文件属性位。
#: ``Path.is_symlink()`` 与 ``os.path.islink()`` 依赖 lstat 的 ``S_ISLNK``，
#: 在部分版本上对 junction 不成立；这一位是唯一可靠的兜底。
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


def _is_link(path: Path) -> bool:
    """是否为符号链接、junction 或其它重解析点（三重判定，任一命中即算）。"""
    try:
        if path.is_symlink() or os.path.islink(path):
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _assert_packable(repo_root: Path, path: Path, label: str) -> None:
    """条目必须是仓库内的真实文件/目录：不是链接，且真实路径没逃出仓库根。

    两道检查各管一件事：

    - 链接：``references/leak.txt -> ~/.ssh/id_rsa`` 读起来和普通文件没区别，
      光靠 ``is_file()`` 会把它原样打进包。
    - 路径逃逸：即便不是链接（或链接被别的层解开），``resolve(strict=True)``
      给出了真实位置，只要不在仓库根之内就拒绝——这是「读到的字节属于仓库」的最后一道保证。
    """
    if _is_link(path):
        raise PackageError(f"拒绝打包符号链接条目：{label}（链接可能指向仓库之外）")
    try:
        real = path.resolve(strict=True)
    except OSError as exc:
        raise PackageError(f"无法解析条目真实路径：{label}（{exc}）") from exc
    if not real.is_relative_to(repo_root):
        raise PackageError(f"条目真实路径逃出仓库根目录：{label} -> {real}")


def is_excluded(rel: PurePosixPath) -> bool:
    """判断相对路径是否应被排除（目录按名字或模式，文件按模式）。"""
    for part in rel.parts[:-1]:
        if part in EXCLUDED_DIR_NAMES:
            return True
        if any(fnmatch.fnmatch(part, pattern) for pattern in EXCLUDED_DIR_PATTERNS):
            return True
    if rel.parts[-1] in EXCLUDED_DIR_NAMES:
        return True
    return any(fnmatch.fnmatch(rel.parts[-1], pattern) for pattern in EXCLUDED_FILE_PATTERNS)


def collect_entries(repo_root: Path) -> list[PurePosixPath]:
    """收集要入包的文件，按 posix 路径排序（保证跨平台顺序一致）。

    每个入包前经过的条目（含根级必需文件、``ROOT_DIRS`` 本身与递归子项）都先过
    :func:`_assert_packable`：符号链接一律失败关闭，不做「链接指向仓库内就放行」的
    例外——那需要额外的真实路径比对，而且没有真实需求支撑。
    """
    root = Path(repo_root).resolve()
    entries: list[PurePosixPath] = []
    missing: list[str] = []

    for name in ROOT_FILES:
        path = root / name
        if not path.exists() and not _is_link(path):
            if name in ("SKILL.md", "LICENSE", "COMMERCIAL-LICENSE.md", "README.md"):
                # 这些是分发与许可的必需项，缺了直接失败，不静默跳过。
                missing.append(name)
            continue
        # 悬空链接也会走到这里：exists() 为 False 但 _is_link 为 True，照样失败关闭。
        _assert_packable(root, path, name)
        if path.is_file():
            entries.append(PurePosixPath(name))

    for dirname in ROOT_DIRS:
        base = root / dirname
        if not base.exists() and not _is_link(base):
            missing.append(dirname + "/")
            continue
        # 目录本身也可能是链接（``references -> ../outside``），必须先拦。
        _assert_packable(root, base, dirname + "/")
        if not base.is_dir():
            missing.append(dirname + "/")
            continue
        for path in base.rglob("*"):
            label = path.relative_to(root).as_posix()
            _assert_packable(root, path, label)
            if not path.is_file():
                continue
            rel = PurePosixPath(label)
            if is_excluded(rel):
                continue
            entries.append(rel)

    if missing:
        raise PackageError("缺少必需条目：" + "、".join(missing))

    entries.sort(key=lambda item: item.as_posix())
    return entries


def normalize_entry(rel: PurePosixPath, data: bytes) -> bytes:
    """把文本条目的 CRLF 折成 LF，让产物与「检出时的行尾」解耦。

    为什么必须做：同一个 commit，在 ``core.autocrlf=true`` 或整树复制过的 worktree 里被检出
    就是 CRLF，内容一个字没改，但 ``read_bytes()`` 读到的字节不同——打出来的包 SHA256 也就
    不同。那是检出差异冒充内容差异，会让人误判「源码变了」。
    无后缀文件（如 ``LICENSE``）按「不含 NUL 即文本」处理，避免漏掉根级纯文本。
    """
    suffix = rel.suffix.lower()
    is_text = suffix in TEXT_SUFFIXES or (not suffix and b"\x00" not in data)
    if not is_text:
        return data
    return data.replace(b"\r\n", b"\n")


def write_zip(repo_root: Path, output: Path, entries: list[PurePosixPath]) -> None:
    """按固定元信息把条目写进 ``output``（原子性由 :func:`build` 负责）。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=COMPRESS_LEVEL
    ) as archive:
        for rel in entries:
            data = normalize_entry(rel, (repo_root / rel).read_bytes())
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
) -> tuple[Path, str, list[PurePosixPath]]:
    """打包并自检，返回 (zip 路径, SHA256, 条目清单)。

    顺序是「写临时文件 -> 回读自检 -> 原子替换」，不是「写最终文件 -> 自检」。
    差别在失败时的磁盘状态：前者什么都不留（旧的合法 zip 也不会被动），
    后者会让一个带凭据的 zip 正好躺在最终路径上——而「自检失败」的消息
    往往被刷过去，产物被人工上传出去。

    **自检不可关闭**：这里刻意没有 ``verify`` 开关。一个能跳过隐私/凭据检查的
    布尔参数，迟早会被某个调用方传 ``False`` 来「先跑通再说」，而它跳过的正是
    发布前唯一还会拦一次的东西。
    """
    root = Path(repo_root).resolve()
    final = Path(output)
    entries = collect_entries(root)

    final.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp 已经保证名字唯一（前缀里带上目标名，便于失败后人工辨认）。
    descriptor, tmp_name = tempfile.mkstemp(
        dir=str(final.parent), prefix=f".{final.name}.", suffix=".tmp"
    )
    os.close(descriptor)
    tmp_path = Path(tmp_name)
    try:
        write_zip(root, tmp_path, entries)
        # mkstemp 建的是 0600；恢复「普通可读」的权限，否则换个用户跑发布步骤就读不到产物。
        os.chmod(tmp_path, FIXED_FILE_MODE)
        problems = verify_zip(tmp_path)
        if problems:
            raise PackageError(
                "打包自检未通过（校验失败，未产出最终文件）：\n"
                + "\n".join(f"  - {item}" for item in problems)
            )
        os.replace(tmp_path, final)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return final, sha256_of(final), entries


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
        # 自检失败是可预期的业务结果，不是程序崩溃：只给结论，不打 traceback。
        print(f"[build_skill_zip] 失败：{exc}", file=sys.stderr)
        return 1
    except (OSError, FileExistsError, zipfile.BadZipFile) as exc:
        # 预期内的环境/输入问题：父路径是文件、目录只读、目标被占用、zip 损坏。
        # 同样不打 traceback——调用方（CI、发布脚本）需要的是可读结论与退出码。
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
