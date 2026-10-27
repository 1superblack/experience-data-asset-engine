"""生命周期：复核日期、可检索性与内容时效分级（Task 7）。

三条硬约束：

1. **到期不等于失效**：``review_after`` 只把笔记放进 ``recheck --due`` 候选，
   绝不自动改 ``status``、绝不删除任何文件。
2. **日期计算必须有明确基准日**：所有函数接收 ``base_date``，
   不读宿主时区的隐式 today（否则同一份笔记在不同机器上算出不同日期）。
3. **时效分级只做保守的确定性判断**：证据不足时一律 ``evolving``，
   不为了「看起来更聪明」去猜 ``stable`` 或 ``volatile``。

口径写在 ``references/expiry-policy.md``。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta

from .errors import InputError
from .models import STATUS_VALUES, VOLATILITY_VALUES

#: 各时效等级的复核周期（天）。可在 config 里按项目覆盖。
DEFAULT_REVIEW_DAYS: Mapping[str, int] = {
    "stable": 365,
    "evolving": 90,
    "volatile": 30,
    "progress": 14,
}

#: 判断不出时效时的兜底等级。保守取值：90 天，不选 365。
DEFAULT_VOLATILITY = "evolving"

#: 默认可检索的状态。``已过时`` / ``已被取代`` 默认排除，需显式 --include-stale。
SEARCHABLE_STATUSES = frozenset({"有效", "待复核"})

#: 默认排除、需要 ``include_stale`` 才返回的状态。
STALE_STATUSES = frozenset({"已过时", "已被取代"})

# --- 时效分级的确定性关键词 -------------------------------------------------

#: 进度类：出现即基本能确定是「走到哪一步」的记录。
_PROGRESS_HINTS = (
    "进度",
    "进展",
    "当前状态",
    "已完成",
    "已跑完",
    "本轮",
    "这一轮",
    "第一轮",
    "第二轮",
    "第三轮",
    "今日",
    "本周",
    "今天",
)

#: 易变类：结论随时可能被推翻、还在试的阶段。
_VOLATILE_HINTS = (
    "临时方案",
    "尚未确定",
    "待定",
    "实验中",
    "试运行",
    "可能变化",
    "不稳定",
    "还在试",
    "先这样",
    "待验证",
)

#: 稳定类：制度、标准、红线、可长期复用的判断原则。
_STABLE_HINTS = (
    "规范",
    "标准",
    "原则",
    "契约",
    "协议",
    "红线",
    "方法论",
    "长期有效",
    "不可协商",
    "硬性要求",
)

#: stable / volatile 需要至少命中这么多个**不同**关键词，避免单个词误判。
_MIN_HINTS_FOR_HARD_GRADE = 2


def _review_days(volatility: str, policy: Mapping[str, int] | None) -> int:
    table = dict(DEFAULT_REVIEW_DAYS)
    if policy:
        for key, value in policy.items():
            if key not in VOLATILITY_VALUES:
                raise InputError(f"复核策略里出现未知时效等级 {key!r}，只接受 {VOLATILITY_VALUES}")
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise InputError(f"复核周期必须是正整数天数，收到 {key}={value!r}")
            table[key] = value
    if volatility not in VOLATILITY_VALUES:
        raise InputError(f"未知的内容时效 {volatility!r}，只接受 {VOLATILITY_VALUES}")
    return table[volatility]


def compute_review_after(
    base_date: date,
    volatility: str,
    policy: Mapping[str, int] | None = None,
) -> date:
    """按基准日算复核日期：``base_date + 该时效等级的周期``。

    只用 ``date + timedelta``，跨月、跨年、闰年都由标准库处理；
    ``base_date`` 必须显式传入，函数内部不读 today。
    """
    if not isinstance(base_date, date):
        raise InputError(f"base_date 必须是 datetime.date，收到 {type(base_date).__name__}")
    days = _review_days(volatility, policy)
    return base_date + timedelta(days=days)


def is_searchable(status: str, include_stale: bool = False) -> bool:
    """该状态的笔记是否应当被检索到。

    ``有效`` / ``待复核`` 默认可检索；``已过时`` / ``已被取代`` 默认不检索，
    ``include_stale=True`` 时恢复。未知状态直接报错——把不认识的状态当成
    「可以检索」或「不可以检索」都是在替用户做决定。
    """
    if status not in STATUS_VALUES:
        raise InputError(f"未知的状态 {status!r}，只接受 {STATUS_VALUES}")
    if status in SEARCHABLE_STATUSES:
        return True
    # 走到这里只可能是 STALE_STATUSES 里的状态（上面已校验过成员资格）
    return bool(include_stale)


def is_due(review_after: str | date, today: date) -> bool:
    """``review_after`` 是否已到期（用于 ``recheck --due`` 候选列表）。

    只做判断，不改任何状态。
    """
    if isinstance(review_after, date):
        due_date = review_after
    else:
        try:
            due_date = date.fromisoformat(str(review_after))
        except ValueError as exc:
            raise InputError(f"review_after 不是合法日期 YYYY-MM-DD：{review_after!r}") from exc
    if not isinstance(today, date):
        raise InputError(f"today 必须是 datetime.date，收到 {type(today).__name__}")
    return due_date <= today


def _count_hints(texts: Iterable[str], hints: tuple[str, ...]) -> int:
    """统计命中的**不同**关键词个数（同一个词重复出现只算一次）。"""
    blob = "\n".join(text for text in texts if isinstance(text, str)).lower()
    return sum(1 for hint in hints if hint.lower() in blob)


def classify_volatility(
    project: str = "",
    decisions: Iterable[Mapping[str, object]] | None = None,
    title: str = "",
) -> str:
    """保守推断内容时效。

    只看标题、项目名和决策文本里的**确定性关键词**，不做语义理解：

    - 命中 >= 2 个不同「进度」词，且不少于其它两类 → ``progress``
    - 否则命中 >= 2 个不同「易变」词 → ``volatile``
    - 否则命中 >= 2 个不同「稳定」词 → ``stable``
    - 其余一律 ``evolving``（证据不足不猜）
    """
    texts: list[str] = [title or "", project or ""]
    for decision in decisions or ():
        if isinstance(decision, Mapping):
            for key in ("decision", "reason"):
                value = decision.get(key)
                if isinstance(value, str):
                    texts.append(value)
            rejected = decision.get("rejected")
            if isinstance(rejected, (list, tuple)):
                texts.extend(item for item in rejected if isinstance(item, str))

    progress_hits = _count_hints(texts, _PROGRESS_HINTS)
    volatile_hits = _count_hints(texts, _VOLATILE_HINTS)
    stable_hits = _count_hints(texts, _STABLE_HINTS)

    if progress_hits >= _MIN_HINTS_FOR_HARD_GRADE and progress_hits >= max(
        volatile_hits, stable_hits
    ):
        return "progress"
    if volatile_hits >= _MIN_HINTS_FOR_HARD_GRADE and volatile_hits > stable_hits:
        return "volatile"
    if stable_hits >= _MIN_HINTS_FOR_HARD_GRADE and stable_hits > volatile_hits:
        return "stable"
    return DEFAULT_VOLATILITY
