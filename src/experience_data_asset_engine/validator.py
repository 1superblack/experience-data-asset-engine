"""vault 校验与默认过期过滤（Task 9）。

读取端和写入端是同一份契约的两头，所以这里**复用** ``writer.FRONTMATTER_FIELDS``
与 ``writer.parse_frontmatter``，不另写一套 frontmatter 规则——两套规则一定会漂移。

三条硬约束：

1. **只读**：``validate_vault`` / ``iter_notes`` / ``iter_due_notes`` 不修改、不移动、
   不删除任何 Markdown，也不改 ``状态``。到期只进复核候选，绝不自动判定过期。
2. **不静默**：缺字段、非法 YAML、非法状态、非法日期、``review_after`` 格式错误、
   取代关系断链/单向，全部进结构化报告。解析不了的文件不会被当成 ``valid``。
3. **默认过滤过期**：``有效`` 与 ``待复核`` 可检索；``已过时`` 与 ``已被取代``
   默认排除，``include_stale=True`` 才恢复，并如实报告被跳过的数量。

报告结构按 Task 9 指令给出的四个桶划分，具体问题类型由 ``Issue.code`` 区分：

- ``missing_fields``：``missing_field`` / ``empty_field`` / ``bad_field_type`` /
  ``invalid_date`` / ``unparseable``
- ``invalid_status``：``invalid_status`` / ``invalid_volatility``
- ``broken_links``：``broken_link`` / ``one_way_link`` / ``self_link`` / ``malformed_link_field``
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

from .config import STATE_DIRNAME, AppConfig, ensure_inside_vault, resolve_safely
from .errors import ConfigError, DistillerError, InputError
from .lifecycle import is_due, is_searchable
from .models import STATUS_VALUES, VOLATILITY_LABELS
from .writer import FRONTMATTER_FIELDS, parse_frontmatter

#: 与写入端完全一致的必填字段（顺序无关，只用于存在性与类型检查）。
REQUIRED_FIELDS: Final = FRONTMATTER_FIELDS

#: 必须是「非空标量」的字段：空字符串等同于没写。
NON_EMPTY_FIELDS: Final = (
    "项目",
    "主题",
    "状态",
    "内容时效",
    "created",
    "updated",
    "valid_from",
    "review_after",
    "来源",
    "会话ID",
    "内容哈希",
    "生成器",
    "状态键",
)

#: 必须是列表的字段。
LIST_FIELDS: Final = ("tags", "取代自", "取代为")

#: 必须是 ``YYYY-MM-DD`` 的字段。
DATE_FIELDS: Final = ("created", "updated", "valid_from", "review_after")

#: 双向取代关系的两个字段。
LINK_FIELDS: Final = ("取代自", "取代为")

#: 不参与扫描的目录：Obsidian 自身、版本库、回收站、本工具状态目录。
SKIP_DIRS: Final = frozenset({".obsidian", ".git", ".trash", STATE_DIRNAME})

_STRICT_DATE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: 检索排序权重：``待复核`` 可检索但排在 ``有效`` 之后（优先级降低，不是判死）。
_STATUS_RANK: Final = {"有效": 0, "待复核": 1}


# --- 结构类型 ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Issue:
    """一条结构化问题。``code`` 是稳定的机器可读标签，``message`` 给人看。"""

    path: Path
    code: str
    message: str
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "code": self.code,
            "message": self.message,
            "field": self.field,
        }


@dataclass(frozen=True, slots=True)
class NoteRecord:
    """一篇笔记的读取结果。``fields`` 为 None 表示没解析成功。"""

    path: Path
    fields: Mapping[str, Any] | None = None
    error: Issue | None = None

    @property
    def ok(self) -> bool:
        return self.fields is not None and self.error is None

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def status(self) -> str | None:
        if not self.ok:
            return None
        value = self.fields.get("状态")
        return value if isinstance(value, str) else None

    def links(self, field: str) -> tuple[str, ...]:
        if not self.ok:
            return ()
        return _link_targets(self.fields.get(field))


@dataclass(frozen=True, slots=True)
class NoteScan:
    """一次扫描的完整结果：扫描到的、可检索的、跳过的、可解析的。"""

    records: tuple[NoteRecord, ...]
    searched: tuple[NoteRecord, ...]
    skipped: int
    include_stale: bool

    @property
    def unparseable(self) -> tuple[NoteRecord, ...]:
        return tuple(record for record in self.records if not record.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": len(self.records),
            "searched": len(self.searched),
            "skipped": self.skipped,
            "unparseable": len(self.unparseable),
            "include_stale": self.include_stale,
        }


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """vault 校验结论。

    ``valid`` 是「确认可用」的笔记，**绝不含**解析失败的文件；
    其余按 Task 9 指令的三个问题桶分类，``Issue.code`` 再细分具体类型。
    """

    valid: tuple[Path, ...] = ()
    missing_fields: tuple[Issue, ...] = ()
    invalid_status: tuple[Issue, ...] = ()
    broken_links: tuple[Issue, ...] = ()
    skipped: int = 0

    @property
    def issues(self) -> tuple[Issue, ...]:
        return self.missing_fields + self.invalid_status + self.broken_links

    @property
    def ok(self) -> bool:
        return not self.issues

    def summary(self) -> str:
        return (
            f"可用 {len(self.valid)} 篇，缺字段/日期/解析问题 {len(self.missing_fields)} 处，"
            f"非法状态/时效 {len(self.invalid_status)} 处，"
            f"取代链接问题 {len(self.broken_links)} 处，"
            f"默认检索跳过 {self.skipped} 篇（过期、已取代、非法状态或无法解析）"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": [str(path) for path in self.valid],
            "missing_fields": [issue.to_dict() for issue in self.missing_fields],
            "invalid_status": [issue.to_dict() for issue in self.invalid_status],
            "broken_links": [issue.to_dict() for issue in self.broken_links],
            "skipped": self.skipped,
            "ok": self.ok,
        }


# --- 扫描 -------------------------------------------------------------------


def _scan_root(config: AppConfig) -> Path:
    """扫描范围是 vault 本体（排除隐藏目录与状态目录），不是只有归档目录。"""
    return resolve_safely(config.vault)


def _iter_markdown(root: Path) -> Iterator[Path]:
    """遍历 vault 下的 Markdown（不进入隐藏目录、状态目录与回收站）。"""
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.md")):
        try:
            relative = path.relative_to(root)
        except ValueError:  # pragma: no cover - rglob 结果必然在 root 下
            continue
        if any(part in SKIP_DIRS for part in relative.parts[:-1]):
            continue
        if not path.is_file():
            continue
        yield path


def _read_record(config: AppConfig, path: Path) -> NoteRecord:
    """读一篇笔记。越界、读不了、解析失败都返回带 error 的记录，不抛异常。"""
    try:
        ensure_inside_vault(config.vault, path)
    except ConfigError as exc:
        return NoteRecord(
            path,
            None,
            Issue(path, "unparseable", f"笔记解析后位于 vault 之外，已跳过：{exc}", "frontmatter"),
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return NoteRecord(
            path, None, Issue(path, "unparseable", f"无法读取笔记：{exc}", "frontmatter")
        )
    try:
        fields, _ = parse_frontmatter(text)
    except DistillerError as exc:
        return NoteRecord(
            path, None, Issue(path, "unparseable", f"frontmatter 无法解析：{exc}", "frontmatter")
        )
    return NoteRecord(path, fields, None)


def _split_records(
    records: Sequence[NoteRecord], include_stale: bool
) -> tuple[tuple[NoteRecord, ...], int]:
    """按检索口径切分：返回 (可检索记录, 跳过数量)。

    ``skipped`` 同时统计「过期/被取代」「非法状态」与「解析失败」——
    两者都不该出现在检索结果里，但必须如实计数，不能静默消失。
    """
    searched: list[NoteRecord] = []
    skipped = 0
    for record in records:
        status = record.status
        if status is None:
            # 解析失败 / 缺状态：无法确认是否现行，宁可不返回，也不当成可用笔记
            skipped += 1
            continue
        try:
            keep = is_searchable(status, include_stale)
        except InputError:
            # 非法状态无法判断是否现行：不返回，但在报告里作为 invalid_status 出现
            skipped += 1
            continue
        if not keep:
            skipped += 1
            continue
        searched.append(record)
    searched.sort(key=lambda item: (_STATUS_RANK.get(item.status or "", 9), str(item.path)))
    return tuple(searched), skipped


def scan_notes(config: AppConfig, include_stale: bool = False) -> NoteScan:
    """扫描 vault，返回结构化结果（只读）。

    ``include_stale`` 只影响 ``searched``：为 False 时排除 ``已过时`` / ``已被取代``，
    并在 ``skipped`` 中如实计数。
    """
    root = _scan_root(config)
    records = tuple(_read_record(config, path) for path in _iter_markdown(root))
    searched, skipped = _split_records(records, include_stale)
    return NoteScan(
        records=records,
        searched=searched,
        skipped=skipped,
        include_stale=include_stale,
    )


def iter_notes(config: AppConfig, include_stale: bool = False) -> Iterator[Path]:
    """按默认策略产出可检索笔记路径。

    默认只给 ``有效`` 与 ``待复核``（后者排序靠后）；``include_stale=True`` 给全部状态。
    解析失败的文件无论哪种模式都不返回——不能用「猜它还有效」蒙过去。
    需要跳过数量时用 ``scan_notes(config, include_stale).skipped``。
    """
    for record in scan_notes(config, include_stale).searched:
        yield record.path


# --- 校验 -------------------------------------------------------------------


def _link_targets(raw: Any) -> tuple[str, ...]:
    """把 ``取代自`` / ``取代为`` 的值整理成链接目标名列表。

    兼容 ``[[笔记]]``、``[[笔记|别名]]``、带 ``.md`` 后缀、以及单值字符串。
    非字符串元素直接忽略——具体类型问题由 ``malformed_link_field`` 上报。
    """
    if raw is None:
        return ()
    items: Sequence[Any] = (raw,) if isinstance(raw, str) else raw if isinstance(raw, list) else ()
    targets: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text.startswith("[[") and text.endswith("]]"):
            text = text[2:-2].strip()
        text = text.split("|", 1)[0].strip()
        if text.casefold().endswith(".md"):
            text = text[:-3]
        if text and text not in targets:
            targets.append(text)
    return tuple(targets)


def _index_keys(record: NoteRecord, root: Path) -> tuple[str, ...]:
    keys = {record.stem.casefold()}
    try:
        relative = record.path.relative_to(root).with_suffix("").as_posix()
    except ValueError:  # pragma: no cover - 记录都来自 root
        relative = record.stem
    keys.add(relative.casefold())
    return tuple(sorted(keys))


def _check_fields(record: NoteRecord) -> tuple[list[Issue], list[Issue]]:
    """返回 (缺字段桶, 非法状态桶)。"""
    fields = record.fields or {}
    missing: list[Issue] = []
    status_issues: list[Issue] = []

    for name in REQUIRED_FIELDS:
        if name not in fields:
            missing.append(Issue(record.path, "missing_field", f"缺少必填字段 {name}", name))
    for name in NON_EMPTY_FIELDS:
        if name not in fields:
            continue
        value = fields[name]
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(Issue(record.path, "empty_field", f"字段 {name} 为空", name))
    for name in LIST_FIELDS:
        if name in fields and not isinstance(fields[name], list):
            missing.append(Issue(record.path, "bad_field_type", f"字段 {name} 必须是列表", name))

    status = fields.get("状态")
    if status is not None and status not in STATUS_VALUES:
        status_issues.append(
            Issue(
                record.path,
                "invalid_status",
                f"状态 {status!r} 非法，只能是 {'/'.join(STATUS_VALUES)} 之一",
                "状态",
            )
        )
    volatility = fields.get("内容时效")
    labels = tuple(VOLATILITY_LABELS.values())
    if volatility is not None and volatility not in labels:
        status_issues.append(
            Issue(
                record.path,
                "invalid_volatility",
                f"内容时效 {volatility!r} 非法，只能是 {'/'.join(labels)} 之一",
                "内容时效",
            )
        )

    for name in DATE_FIELDS:
        value = fields.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not _STRICT_DATE.match(value.strip()):
            missing.append(
                Issue(record.path, "invalid_date", f"字段 {name} 不是 YYYY-MM-DD：{value!r}", name)
            )
            continue
        try:
            date.fromisoformat(value.strip())
        except ValueError:
            missing.append(
                Issue(record.path, "invalid_date", f"字段 {name} 不是合法日期：{value!r}", name)
            )

    return missing, status_issues


def _check_links(records: Sequence[NoteRecord], root: Path) -> list[Issue]:
    """检查 ``取代自`` / ``取代为`` 的断链、单向与自指。

    双向关系要求「A 写取代自 B」时「B 也写取代为 A」，两边都指向对方才算闭合。
    """
    index: dict[str, NoteRecord] = {}
    for record in records:
        if not record.ok:
            continue
        for key in _index_keys(record, root):
            index.setdefault(key, record)

    issues: list[Issue] = []
    for record in records:
        if not record.ok:
            continue
        for field in LINK_FIELDS:
            raw = (record.fields or {}).get(field)
            if raw is not None and not isinstance(raw, (str, list)):
                issues.append(
                    Issue(
                        record.path,
                        "malformed_link_field",
                        f"字段 {field} 必须是列表或字符串",
                        field,
                    )
                )
                continue
            opposite = "取代为" if field == "取代自" else "取代自"
            for target in record.links(field):
                target_record = index.get(target.casefold())
                if target_record is None:
                    issues.append(
                        Issue(
                            record.path,
                            "broken_link",
                            f"{field} 指向的笔记在 vault 内不存在：{target}",
                            field,
                        )
                    )
                    continue
                if target_record.path == record.path:
                    issues.append(
                        Issue(record.path, "self_link", f"{field} 指向自己：{target}", field)
                    )
                    continue
                back_keys = {item.casefold() for item in _link_targets((target_record.fields or {}).get(opposite))}
                forward_keys = {key.casefold() for key in _index_keys(record, root)}
                if not (back_keys & forward_keys):
                    issues.append(
                        Issue(
                            record.path,
                            "one_way_link",
                            f"{field} 指向 {target_record.stem}，"
                            f"但对方没有写 {opposite} 指回本篇（单向关系）",
                            field,
                        )
                    )
    return issues


def validate_vault(config: AppConfig, include_stale: bool = False) -> ValidationReport:
    """校验 vault 内全部笔记（只读）。

    校验**始终覆盖全部笔记**，包括 ``已过时`` / ``已被取代``——否则过期笔记里的坏字段
    永远不会被发现。``include_stale`` 只影响报告里的 ``skipped`` 口径，
    与 ``iter_notes`` 保持一致，避免「校验看全部」被误读成「检索不过滤」。
    """
    root = _scan_root(config)
    scan = scan_notes(config, include_stale=True)

    missing: list[Issue] = []
    invalid_status: list[Issue] = []
    field_ok: list[Path] = []

    for record in scan.records:
        if not record.ok:
            missing.append(record.error or Issue(record.path, "unparseable", "无法解析"))
            continue
        record_missing, record_status = _check_fields(record)
        missing.extend(record_missing)
        invalid_status.extend(record_status)
        if not record_missing and not record_status:
            field_ok.append(record.path)

    broken = _check_links(scan.records, root)
    link_bad = {issue.path for issue in broken}
    valid = [path for path in field_ok if path not in link_bad]
    _, skipped = _split_records(scan.records, include_stale)

    return ValidationReport(
        valid=tuple(valid),
        missing_fields=tuple(missing),
        invalid_status=tuple(invalid_status),
        broken_links=tuple(broken),
        skipped=skipped,
    )


# --- 复核候选（只列不改） ----------------------------------------------------


def iter_due_notes(
    config: AppConfig, today: date, include_stale: bool = False
) -> Iterator[Path]:
    """产出「到期该复核」的笔记路径。

    只列不改：不改 ``状态``、不改 frontmatter、不移动文件。
    到期是提请人看一眼的信号，不是自动判死。
    """
    if not isinstance(today, date):
        raise InputError("today 必须是一个明确的 date，不使用宿主隐式当前时间")
    for record in scan_notes(config, include_stale).searched:
        if not record.ok:
            continue
        review_after = (record.fields or {}).get("review_after")
        if not isinstance(review_after, str) or not review_after.strip():
            continue
        if is_due(review_after, today):
            yield record.path
