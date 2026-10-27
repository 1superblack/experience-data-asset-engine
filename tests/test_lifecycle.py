from __future__ import annotations

from datetime import date

import pytest

from experience_data_asset_engine.errors import InputError
from experience_data_asset_engine.lifecycle import (
    DEFAULT_REVIEW_DAYS,
    classify_volatility,
    compute_review_after,
    is_due,
    is_searchable,
)


# --- 复核周期 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "volatility, days",
    [("stable", 365), ("evolving", 90), ("volatile", 30), ("progress", 14)],
)
def test_default_periods_match_policy_table(volatility, days):
    assert DEFAULT_REVIEW_DAYS[volatility] == days
    assert (compute_review_after(date(2026, 1, 1), volatility) - date(2026, 1, 1)).days == days


def test_review_after_crosses_month_boundary():
    assert compute_review_after(date(2026, 1, 20), "progress") == date(2026, 2, 3)
    assert compute_review_after(date(2026, 1, 31), "volatile") == date(2026, 3, 2)


def test_review_after_crosses_year_boundary():
    assert compute_review_after(date(2026, 12, 15), "evolving") == date(2027, 3, 15)
    assert compute_review_after(date(2026, 12, 31), "stable") == date(2027, 12, 31)


def test_review_after_handles_leap_year():
    # 2024 是闰年，2 月 29 存在：+14 天应落到 3 月 14 日
    assert compute_review_after(date(2024, 2, 29), "progress") == date(2024, 3, 14)
    # 2023-12-15 +90 天跨过闰日的 2 月 29 日
    assert compute_review_after(date(2023, 12, 15), "evolving") == date(2024, 3, 14)
    # 平年 2 月 28 日 +14 天 → 3 月 14 日；闰年同日 +14 天 → 3 月 14 日（闰日已在 2/29）
    assert compute_review_after(date(2026, 2, 28), "progress") == date(2026, 3, 14)
    assert compute_review_after(date(2024, 2, 28), "progress") == date(2024, 3, 13)


def test_policy_override_is_applied():
    assert compute_review_after(date(2026, 1, 1), "evolving", {"evolving": 7}) == date(2026, 1, 8)
    # 只覆盖一项，其余仍走默认
    assert compute_review_after(date(2026, 1, 1), "stable", {"evolving": 7}) == date(2027, 1, 1)


def test_unknown_volatility_is_rejected():
    with pytest.raises(InputError, match="未知的内容时效"):
        compute_review_after(date(2026, 1, 1), "半稳定")


@pytest.mark.parametrize(
    "policy",
    [{"mystery": 10}, {"stable": 0}, {"stable": -5}, {"stable": True}, {"stable": "30"}],
)
def test_illegal_policy_is_rejected(policy):
    with pytest.raises(InputError):
        compute_review_after(date(2026, 1, 1), "stable", policy)


def test_non_date_base_is_rejected():
    with pytest.raises(InputError, match="base_date"):
        compute_review_after("2026-01-01", "stable")  # type: ignore[arg-type]


# --- 可检索性 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "status, include_stale, expected",
    [
        ("有效", False, True),
        ("有效", True, True),
        ("待复核", False, True),
        ("待复核", True, True),
        ("已过时", False, False),
        ("已过时", True, True),
        ("已被取代", False, False),
        ("已被取代", True, True),
    ],
)
def test_is_searchable_matrix(status, include_stale, expected):
    assert is_searchable(status, include_stale) is expected


def test_unknown_status_is_rejected_rather_than_guessed():
    with pytest.raises(InputError, match="未知的状态"):
        is_searchable("大概还有效")


# --- 到期判断（只列不改） ----------------------------------------------------


def test_is_due_accepts_iso_string_and_date():
    assert is_due("2026-10-01", date(2026, 10, 27)) is True
    assert is_due("2026-10-27", date(2026, 10, 27)) is True
    assert is_due("2026-10-28", date(2026, 10, 27)) is False
    assert is_due(date(2026, 10, 1), date(2026, 10, 27)) is True


def test_is_due_rejects_malformed_date():
    with pytest.raises(InputError, match="review_after"):
        is_due("2026/10/01", date(2026, 10, 27))


def test_is_due_rejects_non_date_today():
    with pytest.raises(InputError, match="today"):
        is_due("2026-10-01", "2026-10-27")  # type: ignore[arg-type]


# --- 时效分级：保守、确定性 --------------------------------------------------


def test_defaults_to_evolving_when_evidence_is_thin():
    assert classify_volatility() == "evolving"
    assert classify_volatility(title="一次普通的技术讨论") == "evolving"
    # 只命中一个「标准」不足以判 stable
    assert classify_volatility(title="对齐了接口标准") == "evolving"


def test_progress_wins_when_progress_signals_dominate():
    decisions = [
        {"decision": "本轮已完成 Gate 2", "reason": "测试全绿", "rejected": ["先生成报告"]},
        {"decision": "今日继续 Task 7", "reason": "进度对齐"},
    ]
    assert classify_volatility(title="当前状态：跑到第二轮", decisions=decisions) == "progress"


def test_volatile_needs_two_distinct_hints():
    one = [{"decision": "用了临时方案", "reason": "时间紧"}]
    assert classify_volatility(title="缓存层", decisions=one) == "evolving"

    two = [
        {"decision": "临时方案先上线", "reason": "尚未确定正式实现"},
    ]
    assert classify_volatility(title="缓存层", decisions=two) == "volatile"


def test_stable_needs_two_distinct_hints():
    one = [{"decision": "写进规范", "reason": "统一口径"}]
    assert classify_volatility(title="命名规则", decisions=one) == "evolving"

    two = [
        {"decision": "列为硬性要求", "reason": "这是数据安全红线"},
    ]
    assert classify_volatility(title="命名规则", decisions=two) == "stable"


def test_repeated_same_hint_does_not_count_twice():
    """同一个词重复出现算一次，不能靠复读把等级刷上去。"""
    repeated = [{"decision": "规范规范规范规范", "reason": "规范"}]
    assert classify_volatility(title="规范 规范 规范", decisions=repeated) == "evolving"


def test_volatile_and_stable_tie_falls_back_to_evolving():
    tie = [{"decision": "临时方案，但也要写成硬性要求", "reason": "矛盾表述"}]
    assert classify_volatility(title="边界情况", decisions=tie) == "evolving"


def test_classify_accepts_non_mapping_decisions_gracefully():
    assert classify_volatility(title="进度", decisions=["不是字典"]) == "evolving"
