"""命令行入口（Gate 4 全链路）。

退出码契约（与 ``references/config.md`` 一致）：
    0 成功
    1 校验或配置失败（含输入错误）
    2 需要用户补充输入（缺参数、uninstall 未加 --yes）
    3 需要蒸馏（未配置模型，只生成了 staging）
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

from . import __version__
from .adapters.codex_jsonl import load_codex_session
from .adapters.markdown import load_markdown_session
from .adapters.normalized_json import load_normalized_json
from .archive_flow import ArchivePlan, prepare_archive
from .config import (
    CONFIG_FILENAME,
    DEFAULT_ARCHIVE_REL,
    STATE_DIRNAME,
    AppConfig,
    doctor,
    ensure_inside_vault,
    has_obsidian_dir,
    init_config,
    load_config,
    read_json_file,
    resolve_safely,
    summarize_diagnostics,
)
from .distill import validate_distilled_note
from .errors import ConfigError, DistillerError, InputError
from .models import STATUS_VALUES, DistilledNote, NoiseReport, NormalizedSession
from .validator import (
    iter_due_notes,
    scan_notes,
    set_note_status,
    validate_scan,
    validate_vault,
)
from .writer import write_note

PROG = "experience-data-asset-engine"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INPUT_REQUIRED = 2
EXIT_DISTILLATION_REQUIRED = 3

_SUPPORTED_INPUT_SUFFIXES = (".json", ".jsonl", ".jsonl.zst", ".md")


# --- 参数 -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="把 AI 对话与项目过程蒸馏为可复用、可追溯的经验数据资产（Obsidian 笔记）。",
        epilog="退出码：0 成功 / 1 校验或配置失败 / 2 需要用户补充输入 / 3 需要蒸馏。",
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

    archive_parser = subparsers.add_parser("archive", help="归档一次会话（降噪 → 蒸馏 → 校验 → 写入）")
    archive_parser.add_argument(
        "--input", metavar="<path>", help="会话文件：.json / .jsonl / .jsonl.zst / .md"
    )
    archive_parser.add_argument(
        "--distilled", metavar="<path>", help="已蒸馏好的 DistilledNote JSON（给了就不调用模型）"
    )
    archive_parser.add_argument(
        "--dry-run", action="store_true", help="只演算：不写 Markdown / state / audit / staging"
    )
    archive_parser.add_argument(
        "--include-stale", action="store_true", help="统计时包含「已过时 / 已被取代」的笔记"
    )
    archive_parser.add_argument("--vault", metavar="<path>", help="vault 根目录")

    validate_parser = subparsers.add_parser("validate", help="校验 vault 内笔记（只读）")
    validate_parser.add_argument("--vault", metavar="<path>", help="vault 根目录")
    validate_parser.add_argument(
        "--include-stale", action="store_true", help="统计时包含「已过时 / 已被取代」的笔记"
    )

    status_parser = subparsers.add_parser("status", help="统计笔记状态（只读）")
    status_parser.add_argument("--vault", metavar="<path>", help="vault 根目录")

    recheck_parser = subparsers.add_parser("recheck", help="到期复核：只列候选，或手改单篇状态")
    recheck_parser.add_argument("--due", action="store_true", help="列出到期候选（只列不改）")
    recheck_parser.add_argument(
        "--note", metavar="<path>", help="要改状态的笔记路径（必须在 vault 内）"
    )
    recheck_parser.add_argument(
        "--status", metavar="<状态>", help=f"目标状态：{' / '.join(STATUS_VALUES)}"
    )
    recheck_parser.add_argument(
        "--include-stale", action="store_true", help="列到期候选时包含过期笔记"
    )
    recheck_parser.add_argument("--vault", metavar="<path>", help="vault 根目录")

    uninstall_parser = subparsers.add_parser(
        "uninstall", help="只删除工具自身状态目录（绝不删 Markdown）"
    )
    uninstall_parser.add_argument("--vault", metavar="<path>", help="vault 根目录")
    uninstall_parser.add_argument(
        "--yes", action="store_true", help="确认删除；不加这个参数时零删除"
    )

    return parser


# --- 小工具 -----------------------------------------------------------------


def _err(message: str) -> None:
    print(f"错误: {message}", file=sys.stderr)


def _vault_of(args: argparse.Namespace) -> Path | None:
    raw = getattr(args, "vault", None)
    return Path(raw).expanduser() if raw else None


def _config(args: argparse.Namespace) -> AppConfig:
    return load_config(_vault_of(args))


def _display(config: AppConfig, path: Path) -> str:
    """尽量显示 vault 内相对路径，避免把宿主绝对路径刷进日志。"""
    try:
        return Path(path).resolve().relative_to(config.vault).as_posix()
    except (ValueError, OSError):
        return str(path)


def _noise_line(report: NoiseReport) -> str:
    return (
        f"保留 {report.kept_messages}/{report.input_messages}"
        f"｜系统 {report.system_noise}｜工具 {report.tool_noise}"
        f"｜重复 {report.duplicate_topics}｜死路 {report.dead_ends}"
        f"｜脱敏 {report.redactions}"
    )


def _load_session(path: Path) -> NormalizedSession:
    name = path.name.lower()
    if name.endswith(".jsonl") or name.endswith(".jsonl.zst"):
        return load_codex_session(path)
    if name.endswith(".json"):
        return load_normalized_json(path)
    if name.endswith(".md"):
        return load_markdown_session(path)
    raise InputError(
        f"无法识别的输入后缀: {path.name}；只接受 {' / '.join(_SUPPORTED_INPUT_SUFFIXES)}"
    )


def _load_distilled(path: Path) -> DistilledNote:
    if not path.exists():
        raise InputError(f"蒸馏结果文件不存在: {path}")
    return validate_distilled_note(read_json_file(path))


def _print_write_result(
    config: AppConfig,
    note: DistilledNote,
    result,
    *,
    distilled: bool = False,
) -> None:
    print(f"action: {result.action}")
    print(f"path: {_display(config, result.path)}")
    print(f"content_hash: {result.content_hash}")
    print(f"noise: {_noise_line(note.noise_report)}")
    if distilled:
        print("来源: --distilled（未调用模型）")


def _print_vault_line(config: AppConfig, include_stale: bool) -> None:
    scan = scan_notes(config, include_stale=include_stale)
    mode = "含" if include_stale else "不含"
    print(f"vault: 检索可见 {len(scan.searched)} 篇（{mode}过期），跳过 {scan.skipped} 篇")


# --- 子命令 -----------------------------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    if not args.vault:
        _err(f"init 需要 --vault <path>（例如 {PROG} init --vault /path/to/vault）")
        return EXIT_INPUT_REQUIRED

    vault = Path(args.vault).expanduser()
    config_path = vault / STATE_DIRNAME / CONFIG_FILENAME
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
    config = _config(args)
    diagnostics = doctor(config)
    for item in diagnostics:
        print(f"{item.level.upper():<5} {item.code:<22} {item.message}")
        if item.hint:
            print(f"{'':<29}{item.hint}")
    print(summarize_diagnostics(diagnostics))
    return EXIT_FAILURE if any(item.is_error for item in diagnostics) else EXIT_OK


def _cmd_archive(args: argparse.Namespace) -> int:
    if not args.input:
        _err("archive 需要 --input <path>（会话文件）")
        return EXIT_INPUT_REQUIRED

    config = _config(args)
    session = _load_session(Path(args.input).expanduser())
    distilled_note = _load_distilled(Path(args.distilled).expanduser()) if args.distilled else None

    plan = prepare_archive(
        session,
        config,
        distilled_note=distilled_note,
        allow_side_effects=not args.dry_run,
    )

    if args.dry_run:
        return _print_plan(config, plan, args)

    if plan.staging_required:
        _err("需要蒸馏：未配置模型，已生成 staging 请求，未写入任何正式笔记")
        _print_staging(config, plan)
        return EXIT_DISTILLATION_REQUIRED

    if plan.note is None:  # 防御：非 staging 分支一定带笔记
        raise ConfigError(f"内部状态异常：mode={plan.mode} 却没有蒸馏结果")

    result = write_note(plan.note, plan.session, config)
    _print_write_result(config, plan.note, result, distilled=plan.distilled)
    _print_vault_line(config, args.include_stale)
    return EXIT_OK


def _print_staging(config: AppConfig, plan: ArchivePlan) -> None:
    if plan.staging_path is not None:
        print(f"staging: {_display(config, plan.staging_path)}", file=sys.stderr)
    if plan.schema_path is not None:
        print(f"schema: {_display(config, plan.schema_path)}", file=sys.stderr)
    print("下一步: 让 Agent 按 schema 产出 DistilledNote，再用 --distilled 归档", file=sys.stderr)


def _print_plan(config: AppConfig, plan: ArchivePlan, args: argparse.Namespace) -> int:
    """--dry-run：只读演算，一个字节都不写。"""
    print("dry-run: 不写入任何文件（不写 Markdown / state / audit / staging，也不创建目录）")
    print(f"input: {Path(args.input).name}")
    print(f"session: {plan.session.title}（{plan.session.source}:{plan.session.session_id}）")
    print(f"noise: {_noise_line(plan.report)}")
    print(f"will_keep: {plan.report.kept_messages} 条消息进入蒸馏")

    if plan.note is not None:
        # dry-run 与真跑用同一份 note（同样的覆盖、同样的哈希），不会出现两套统计
        result = write_note(plan.note, plan.session, config, dry_run=True)
        print(f"预计 action: {result.action}")
        print(f"预计 path: {_display(config, result.path)}")
        print(f"content_hash: {result.content_hash}")
        if plan.distilled:
            print("distilled: 使用 --distilled，不会调用模型")
    elif plan.provider_planned:
        print(f"预计 action: 调用模型 {config.provider} → 校验 → 写入")
        print("预计 path: 需蒸馏完成后才能确定")
        print("provider: dry-run 不会发起任何模型请求")
    else:
        print("预计 action: 生成 staging 请求（需要蒸馏），不会写正式笔记")
        print("预计退出码: 3（真跑需要蒸馏；dry-run 自身返回 0）")
    return EXIT_OK


def _cmd_validate(args: argparse.Namespace) -> int:
    config = _config(args)
    report = validate_vault(config, include_stale=args.include_stale)

    print(f"valid: {len(report.valid)}")
    print(f"missing_fields: {len(report.missing_fields)}")
    print(f"invalid_status: {len(report.invalid_status)}")
    print(f"broken_links: {len(report.broken_links)}")
    print(f"skipped: {report.skipped}")
    for issue in report.issues:
        print(f"  [{issue.code}] {_display(config, issue.path)}: {issue.message}")
    print(report.summary())
    return EXIT_OK if report.ok else EXIT_FAILURE


def _cmd_status(args: argparse.Namespace) -> int:
    config = _config(args)
    scan = scan_notes(config, include_stale=True)

    counts = {status: 0 for status in STATUS_VALUES}
    pending = 0
    unparseable = 0
    other = 0
    for record in scan.records:
        if record.is_pending:
            pending += 1
            continue
        if not record.ok:
            unparseable += 1
            continue
        status = record.status
        if status in counts:
            counts[status] += 1
        else:
            other += 1

    # 复用同一次扫描：status 不再为了拿校验结论把 vault 读第二遍
    report = validate_scan(config, scan, include_stale=True)
    for status in STATUS_VALUES:
        print(f"{status}: {counts[status]}")
    print(f"pending: {pending}")
    print(f"无法解析: {unparseable}")
    print(f"其他/非法状态: {other}")
    print(f"断链数: {len(report.broken_links)}")
    print(f"缺字段数: {len(report.missing_fields)}")
    print(f"skipped: {report.skipped}")
    print(f"可检索: {len(scan.searched)}")
    return EXIT_OK


def _cmd_recheck(args: argparse.Namespace) -> int:
    if args.due and (args.note or args.status):
        _err("--due 与 --note/--status 不能同时使用")
        return EXIT_INPUT_REQUIRED
    if not args.due and (not args.note or not args.status):
        _err("recheck 需要 --due，或 --note <path> 与 --status <状态> 同时给出")
        return EXIT_INPUT_REQUIRED

    config = _config(args)

    if args.due:
        due = list(iter_due_notes(config, date.today(), include_stale=args.include_stale))
        print(f"到期候选: {len(due)} 篇（只列不改：未修改任何笔记或其状态）")
        for path in due:
            print(f"  {_display(config, path)}")
        return EXIT_OK

    target = set_note_status(config, Path(args.note).expanduser(), args.status)
    print(f"status: {args.status}")
    print(f"path: {_display(config, target)}")
    return EXIT_OK


def _cmd_uninstall(args: argparse.Namespace) -> int:
    if not args.yes:
        _err("uninstall 需要 --yes 确认；未确认时不会删除任何东西")
        return EXIT_INPUT_REQUIRED

    # 1) 目标必须是真正的 Obsidian vault（缺 .obsidian 直接拒绝，普通目录连看都不看）
    vault_arg = _vault_of(args)
    if vault_arg is not None:
        vault = resolve_safely(vault_arg)
    else:
        vault = resolve_safely(load_config().vault)
    if not has_obsidian_dir(vault):
        raise ConfigError(
            f"目标不是 Obsidian vault（缺少 .obsidian），拒绝执行 uninstall: {vault}"
        )

    # 2) 状态目录必须是普通目录，不能是符号链接
    state_dir = vault / STATE_DIRNAME
    if state_dir.is_symlink():
        raise ConfigError(f"状态目录是符号链接，拒绝删除: {state_dir}")
    target = ensure_inside_vault(vault, state_dir)

    if not target.exists():
        print(f"状态目录不存在，无需卸载: {target}")
        return EXIT_OK
    if not target.is_dir():
        raise ConfigError(f"状态目录不是目录，拒绝删除: {target}")

    # 3) 必须存在本工具的有效配置：没有 config、配置损坏、越界都拒绝删
    config = load_config(vault)

    files = sorted(item for item in target.rglob("*") if item.is_file())
    print(f"将删除状态目录: {_display(config, target)}")
    print(f"将删除 {len(files)} 个文件:")
    for item in files:
        print(f"  - {_display(config, item)}")

    shutil.rmtree(target)
    print(f"已删除: {_display(config, target)}")
    print("未删除任何 Markdown 笔记")
    return EXIT_OK


HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "init": _cmd_init,
    "doctor": _cmd_doctor,
    "archive": _cmd_archive,
    "validate": _cmd_validate,
    "status": _cmd_status,
    "recheck": _cmd_recheck,
    "uninstall": _cmd_uninstall,
}


def main(argv: Sequence[str] | None = None) -> int:
    # Windows 控制台默认代码页可能不是 UTF-8（中文 Windows 是 GBK/cp936）。
    # 用户在普通 cmd/PowerShell 里直接跑 `python scripts/cli.py init ...`，
    # 中文输出会按 cp936 写出，被 UTF-8 终端读成乱码。统一改成 UTF-8 输出。
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                try:
                    reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    # stdout 被替换（如 pytest capsys）时不阻塞主流程
                    pass

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
        _err(str(exc))
        return EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
