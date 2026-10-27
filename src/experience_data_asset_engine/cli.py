"""命令行入口。

退出码契约（与 references/config.md 一致）：
    0 成功
    1 校验或配置失败
    2 需要用户补充输入
    3 需要蒸馏（未配置模型）
    4 子命令尚未实现（Gate 1 只交付骨架）
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from . import __version__
from .config import (
    DEFAULT_ARCHIVE_REL,
    CONFIG_FILENAME,
    STATE_DIRNAME,
    doctor,
    init_config,
    load_config,
    summarize_diagnostics,
)
from .errors import DistillerError

PROG = "experience-data-asset-engine"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INPUT_REQUIRED = 2
EXIT_DISTILLATION_REQUIRED = 3
EXIT_NOT_IMPLEMENTED = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="把 AI 对话蒸馏为可追溯、带生命周期管理的 Obsidian 知识笔记。",
        epilog="退出码：0 成功 / 1 校验或配置失败 / 2 需要用户补充输入 / 3 需要蒸馏 / 4 尚未实现。",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{PROG} {__version__}",
        help="打印版本号后退出",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    init_parser = subparsers.add_parser("init", help="写入 vault 配置（幂等，不覆盖已有配置）")
    init_parser.add_argument("--vault", metavar="<path>", help="Obsidian vault 根目录")
    init_parser.add_argument(
        "--archive-dir",
        dest="archive_dir",
        metavar="<relative-path>",
        default=DEFAULT_ARCHIVE_REL,
        help=f"vault 内的归档子目录（相对路径），默认 {DEFAULT_ARCHIVE_REL}",
    )

    doctor_parser = subparsers.add_parser("doctor", help="环境自检与诊断")
    doctor_parser.add_argument("--vault", metavar="<path>", help="vault 根目录，省略则按环境变量定位")

    archive_parser = subparsers.add_parser("archive", help="归档一次会话（尚未实现）")
    archive_parser.add_argument("--input", metavar="<path>", help="会话文件（尚未实现）")
    archive_parser.add_argument("--distilled", metavar="<path>", help="蒸馏结果 JSON（尚未实现）")
    archive_parser.add_argument("--dry-run", action="store_true", help="只演算不写入（尚未实现）")
    archive_parser.add_argument(
        "--include-stale", action="store_true", help="包含已过时/已被取代（尚未实现）"
    )

    validate_parser = subparsers.add_parser("validate", help="校验 vault 笔记（尚未实现）")
    validate_parser.add_argument("--vault", metavar="<path>", help="vault 根目录（尚未实现）")

    subparsers.add_parser("status", help="统计笔记状态（尚未实现）")

    recheck_parser = subparsers.add_parser("recheck", help="到期复核（尚未实现）")
    recheck_parser.add_argument("--due", action="store_true", help="列出到期笔记（尚未实现）")
    recheck_parser.add_argument("--note", metavar="<path>", help="指定笔记（尚未实现）")
    recheck_parser.add_argument("--status", metavar="<status>", help="目标状态（尚未实现）")

    subparsers.add_parser("uninstall", help="只删除工具自身状态目录（尚未实现）")

    return parser


def _cmd_init(args: argparse.Namespace) -> int:
    if not args.vault:
        print(
            f"错误: init 需要 --vault <path>（例如 {PROG} init --vault /path/to/vault）",
            file=sys.stderr,
        )
        return EXIT_INPUT_REQUIRED

    vault = Path(args.vault).expanduser()
    config_path = vault.resolve() / STATE_DIRNAME / CONFIG_FILENAME
    existed = config_path.exists()
    config = init_config(vault, archive_rel=args.archive_dir)
    if existed:
        print(f"配置已存在，未覆盖: {config.config_path}")
    else:
        print(f"配置已写入: {config.config_path}")
    print(f"vault: {config.vault}")
    print(f"归档目录: {config.archive_dir}")
    return EXIT_OK


def _cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config(Path(args.vault).expanduser() if args.vault else None)
    diagnostics = doctor(config)
    for item in diagnostics:
        print(f"{item.level.upper():<5} {item.code:<22} {item.message}")
        if item.hint:
            print(f"{'':<29}{item.hint}")
    print(summarize_diagnostics(diagnostics))
    return EXIT_FAILURE if any(item.is_error for item in diagnostics) else EXIT_OK


def _cmd_not_implemented(args: argparse.Namespace) -> int:
    print(
        f"{args.command} 尚未实现：{__version__} 只交付骨架、vault 配置、"
        "数据协议与 Codex 输入适配。",
        file=sys.stderr,
    )
    print("请勿向用户声称该操作已完成。", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "init": _cmd_init,
    "doctor": _cmd_doctor,
    "archive": _cmd_not_implemented,
    "validate": _cmd_not_implemented,
    "status": _cmd_not_implemented,
    "recheck": _cmd_not_implemented,
    "uninstall": _cmd_not_implemented,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse 用 SystemExit 表达 --version / 参数错误
        code = exc.code
        return EXIT_OK if code is None else int(code)

    command = getattr(args, "command", None)
    if not command:
        parser.print_help()
        return EXIT_INPUT_REQUIRED

    try:
        return HANDLERS[command](args)
    except DistillerError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
