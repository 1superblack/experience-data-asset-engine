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
from collections.abc import Sequence

from . import __version__

PROG = "obsidian-knowledge-distiller"

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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse 用 SystemExit 表达 --version / 参数错误
        code = exc.code
        return EXIT_OK if code is None else int(code)
    parser.print_help()
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
