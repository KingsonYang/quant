from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from quant.backtest_common import load_trade_dates_in_range, validate_trade_date

from quant.config.models import (
    LimitUpPullbackMa5ReclaimParams,
    PatternSelectionConfig,
    SecondVolumeLaunchParams,
)
from quant.selection import (
    StockSelectionError,
    filter_selection_rows_by_rules,
    load_selection_rows_for_symbol_dates_with_connection,
)


SUPPORTED_PATTERN_SORT_ORDERS = {"ASC", "DESC"}
SUPPORTED_PATTERN_SORT_FIELDS = {"symbol", "name", "d0_date", "composite_score"}


class PatternSelectionError(ValueError):
    """Raised when a pattern-based selection request is invalid."""


@dataclass(frozen=True)
class PatternSelectionCandidate:
    symbol: str
    name: str
    selection_date: date
    d0_date: date
    composite_score: float | None = None


PATTERN_SQL_TEMPLATE = """
WITH bars AS (
  SELECT
    d.security_id, d.trade_date,
    d.open_price, d.high_price, d.low_price, d.close_price,
    d.volume, d.change_pct,
    row_number() OVER (PARTITION BY d.security_id ORDER BY d.trade_date DESC) AS rn_desc,
    avg(d.volume) OVER (
      PARTITION BY d.security_id
      ORDER BY d.trade_date
      ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
    ) AS prev5_avg_vol,
    lag(d.volume, 1) OVER (PARTITION BY d.security_id ORDER BY d.trade_date) AS prev1_volume,
    lag(d.change_pct, 1) OVER (PARTITION BY d.security_id ORDER BY d.trade_date) AS prev1_change_pct,
    lag(d.change_pct, 2) OVER (PARTITION BY d.security_id ORDER BY d.trade_date) AS prev2_change_pct
  FROM daily_bar d
  WHERE d.trade_date BETWEEN (CAST(? AS DATE) - INTERVAL '{lookback_calendar_days} days') AND CAST(? AS DATE)
),
d1 AS (
  SELECT * FROM bars WHERE trade_date = CAST(? AS DATE)
),
last_window AS (
  SELECT b.*
  FROM bars b
  JOIN d1 ON d1.security_id = b.security_id
  WHERE b.rn_desc <= 31
),
d0_candidates AS (
  SELECT *
  FROM last_window
  WHERE trade_date < CAST(? AS DATE)
    AND change_pct > ?
    AND prev5_avg_vol IS NOT NULL
    AND volume >= ? * prev5_avg_vol
    AND close_price > open_price
),
between_lows AS (
  SELECT
    d0.security_id,
    d0.trade_date AS d0_date,
    d0.low_price  AS d0_low,
    d0.change_pct AS d0_change,
    d0.volume     AS d0_vol,
    min(b.low_price) AS min_low_between,
    count(*) AS between_days
  FROM d0_candidates d0
  JOIN last_window b
    ON b.security_id = d0.security_id
   AND b.trade_date > d0.trade_date
   AND b.trade_date < CAST(? AS DATE)
  GROUP BY d0.security_id, d0.trade_date, d0.low_price, d0.change_pct, d0.volume
),
picked AS (
  SELECT
    bl.security_id,
    bl.d0_date,
    bl.d0_low,
    bl.d0_change,
    bl.d0_vol,
    bl.between_days,
    bl.min_low_between
  FROM between_lows bl
  JOIN d1 ON d1.security_id = bl.security_id
  WHERE bl.between_days <= ?
    AND bl.min_low_between > bl.d0_low
    AND d1.change_pct > ?
    AND d1.prev1_volume IS NOT NULL
    AND d1.volume >= ? * d1.prev1_volume
    AND d1.prev1_change_pct IS NOT NULL
    AND d1.prev2_change_pct IS NOT NULL
    AND (d1.prev1_change_pct + d1.prev2_change_pct) < ?
)
SELECT s.symbol, s.name, p.d0_date
FROM picked p
JOIN security_master s ON s.security_id = p.security_id
ORDER BY s.symbol
"""


def _pattern_sql(params: SecondVolumeLaunchParams) -> str:
    return PATTERN_SQL_TEMPLATE.format(
        lookback_calendar_days=params.lookback_calendar_days,
    )


LIMIT_UP_PULLBACK_MA5_RECLAIM_SQL_TEMPLATE = """
WITH bars AS (
  SELECT
    d.security_id, d.trade_date,
    d.open_price, d.high_price, d.low_price, d.close_price,
    d.volume, d.change_pct, d.ma5_price, d.turnover_ratio,
    row_number() OVER (PARTITION BY d.security_id ORDER BY d.trade_date DESC) AS rn_desc,
    avg(d.volume) OVER (
      PARTITION BY d.security_id
      ORDER BY d.trade_date
      ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
    ) AS prev5_avg_vol
  FROM daily_bar d
  WHERE d.trade_date BETWEEN (CAST(? AS DATE) - INTERVAL '{lookback_calendar_days} days') AND CAST(? AS DATE)
),
signal AS (
  SELECT *
  FROM bars
  WHERE trade_date = CAST(? AS DATE)
    AND ma5_price IS NOT NULL
    AND close_price >= ma5_price
    AND close_price > open_price
    AND change_pct >= ?
    AND prev5_avg_vol IS NOT NULL
    AND volume >= ? * prev5_avg_vol
    AND volume <= ? * prev5_avg_vol
),
signal_prev3 AS (
  SELECT
    s.security_id,
    max(
      CASE
        WHEN b.ma5_price IS NOT NULL AND b.close_price <= b.ma5_price * ? THEN 1
        ELSE 0
      END
    ) AS touched_ma5_recently
  FROM signal s
  JOIN bars b
    ON b.security_id = s.security_id
   AND b.rn_desc BETWEEN 2 AND 4
  GROUP BY s.security_id
),
t0_candidates AS (
  SELECT b.*
  FROM bars b
  JOIN signal s ON s.security_id = b.security_id
  WHERE b.trade_date < CAST(? AS DATE)
    AND b.change_pct >= ?
    AND b.high_price > 0
    AND b.close_price >= ? * b.high_price
    AND b.close_price > b.open_price
    AND b.prev5_avg_vol IS NOT NULL
    AND b.volume >= ? * b.prev5_avg_vol
),
pullbacks AS (
  SELECT
    t0.security_id,
    t0.trade_date AS d0_date,
    t0.low_price  AS d0_low,
    t0.close_price AS d0_close,
    t0.volume     AS d0_volume,
    count(*) AS between_days,
    min(b.low_price) AS min_low_between,
    min(b.close_price) AS min_close_between,
    max(b.change_pct) AS max_change_between,
    min(b.change_pct) AS min_change_between,
    avg(b.volume) AS avg_volume_between,
    sum(
      CASE
        WHEN b.close_price <= b.open_price OR b.change_pct <= ? THEN 1
        ELSE 0
      END
    ) AS weak_days
  FROM t0_candidates t0
  JOIN bars b
    ON b.security_id = t0.security_id
   AND b.trade_date > t0.trade_date
   AND b.trade_date < CAST(? AS DATE)
  GROUP BY t0.security_id, t0.trade_date, t0.low_price, t0.close_price, t0.volume
),
score_calc AS (
  SELECT
    p.security_id,
    p.d0_date,
    p.d0_low,
    p.d0_close,
    p.between_days,
    p.weak_days,
    p.avg_volume_between,
    p.d0_volume,
    -- sub-scores raw values
    CASE WHEN p.d0_low > 0
      THEN LEAST((p.d0_low - p.min_low_between) / p.d0_low * 100, 100)
      ELSE 0 END AS sub_pullback_depth,
    CASE WHEN p.d0_low > 0
      THEN LEAST((s.close_price - p.d0_low) / p.d0_low * 100, 100)
      ELSE 0 END AS sub_reclaim_strength,
    CASE WHEN p.d0_volume > 0
      THEN LEAST(p.avg_volume_between / p.d0_volume * 100, 100)
      ELSE 100 END AS sub_volume_shrink,
    CASE WHEN p.between_days > 0
      THEN p.weak_days * 100.0 / p.between_days
      ELSE 0 END AS sub_weak_day_ratio,
    COALESCE(s.turnover_ratio, 0) AS raw_turnover,
    s.close_price,
    row_number() OVER (PARTITION BY p.security_id ORDER BY p.d0_date DESC) AS anchor_rank
  FROM pullbacks p
  JOIN signal s ON s.security_id = p.security_id
  JOIN signal_prev3 sp ON sp.security_id = p.security_id
  WHERE p.between_days BETWEEN ? AND ?
    AND p.weak_days >= ?
    AND (
      (
        p.min_low_between >= p.d0_low * ?
        AND p.min_close_between >= p.d0_low * ?
        AND p.max_change_between <= ?
        AND p.min_change_between >= ?
        AND p.avg_volume_between <= p.d0_volume * ?
        AND s.close_price >= p.d0_low * ?
      )
      OR (
        ? = TRUE
        AND p.min_low_between < p.d0_low * ?
        AND p.min_low_between >= p.d0_low * ?
        AND p.min_close_between >= p.d0_low * ?
        AND p.max_change_between <= ?
        AND p.min_change_between >= ?
        AND p.avg_volume_between <= p.d0_volume * ?
        AND s.close_price >= p.d0_low * ?
      )
    )
    AND p.min_low_between <= p.d0_low * ?
    AND s.close_price <= p.d0_low * ?
    AND s.close_price < p.d0_close
    AND sp.touched_ma5_recently = 1
),
scored AS (
  SELECT
    sc.security_id,
    sc.d0_date,
    sc.sub_pullback_depth,
    sc.sub_reclaim_strength,
    sc.sub_volume_shrink,
    sc.sub_weak_day_ratio,
    sc.raw_turnover,
    sm.symbol,
    sm.name,
    -- composite score: min-max normalize each to 0-100, then weighted sum
    (CASE WHEN (mx.mx_pd - mn.mn_pd) > 0
      THEN (sc.sub_pullback_depth - mn.mn_pd) / (mx.mx_pd - mn.mn_pd) * 100
      ELSE 50 END) * {w_pullback_depth}
    + (CASE WHEN (mx.mx_rs - mn.mn_rs) > 0
      THEN (sc.sub_reclaim_strength - mn.mn_rs) / (mx.mx_rs - mn.mn_rs) * 100
      ELSE 50 END) * {w_reclaim}
    + (CASE WHEN (mx.mx_vs - mn.mn_vs) > 0
      THEN (sc.sub_volume_shrink - mn.mn_vs) / (mx.mx_vs - mn.mn_vs) * 100
      ELSE 50 END) * {w_volume_shrink}
    + (CASE WHEN (mx.mx_wr - mn.mn_wr) > 0
      THEN (sc.sub_weak_day_ratio - mn.mn_wr) / (mx.mx_wr - mn.mn_wr) * 100
      ELSE 50 END) * {w_weak_day}
    + (CASE WHEN (mx.mx_to - mn.mn_to) > 0
      THEN (sc.raw_turnover - mn.mn_to) / (mx.mx_to - mn.mn_to) * 100
      ELSE 50 END) * {w_turnover}
    AS composite_score
  FROM score_calc sc
  JOIN security_master sm ON sm.security_id = sc.security_id
  CROSS JOIN (SELECT MAX(sub_pullback_depth) AS mx_pd, MAX(sub_reclaim_strength) AS mx_rs,
                      MAX(sub_volume_shrink) AS mx_vs, MAX(sub_weak_day_ratio) AS mx_wr,
                      MAX(raw_turnover) AS mx_to FROM score_calc) mx
  CROSS JOIN (SELECT MIN(sub_pullback_depth) AS mn_pd, MIN(sub_reclaim_strength) AS mn_rs,
                      MIN(sub_volume_shrink) AS mn_vs, MIN(sub_weak_day_ratio) AS mn_wr,
                      MIN(raw_turnover) AS mn_to FROM score_calc) mn
  WHERE sc.anchor_rank = 1
)
SELECT symbol, name, d0_date, composite_score
FROM scored
ORDER BY composite_score DESC, symbol ASC
"""


def _limit_up_pullback_ma5_reclaim_sql(params: LimitUpPullbackMa5ReclaimParams) -> str:
    return LIMIT_UP_PULLBACK_MA5_RECLAIM_SQL_TEMPLATE.format(
        lookback_calendar_days=params.lookback_calendar_days,
        w_pullback_depth=float(params.score_weight_pullback_depth),
        w_reclaim=float(params.score_weight_reclaim_strength),
        w_volume_shrink=float(params.score_weight_volume_shrink),
        w_weak_day=float(params.score_weight_weak_day_ratio),
        w_turnover=float(params.score_weight_turnover),
    )


def _pct_up_factor(value: Decimal) -> Decimal:
    return Decimal("1") + (value / Decimal("100"))


def _pct_down_factor(value: Decimal) -> Decimal:
    return Decimal("1") - (value / Decimal("100"))


def _validate_sort(sort_by: str, sort_order: str) -> tuple[str, str]:
    if sort_by not in SUPPORTED_PATTERN_SORT_FIELDS:
        supported = ", ".join(sorted(SUPPORTED_PATTERN_SORT_FIELDS))
        raise PatternSelectionError(
            f"Unsupported pattern sort field: {sort_by}. Supported fields: {supported}."
        )
    normalized_order = sort_order.upper()
    if normalized_order not in SUPPORTED_PATTERN_SORT_ORDERS:
        raise PatternSelectionError(
            f"Unsupported pattern sort order: {sort_order}. Supported orders: ASC, DESC."
        )
    return sort_by, normalized_order


def _sort_candidates(
    candidates: list[PatternSelectionCandidate],
    *,
    sort_by: str,
    sort_order: str,
) -> list[PatternSelectionCandidate]:
    validated_sort_by, validated_sort_order = _validate_sort(sort_by, sort_order)
    reverse_primary = validated_sort_order == "DESC"

    if validated_sort_by == "symbol":
        sorted_candidates = sorted(
            candidates,
            key=lambda candidate: (candidate.name, candidate.d0_date),
        )
        return sorted(
            sorted_candidates,
            key=lambda candidate: candidate.symbol,
            reverse=reverse_primary,
        )
    if validated_sort_by == "name":
        sorted_candidates = sorted(
            candidates,
            key=lambda candidate: (candidate.symbol, candidate.d0_date),
        )
        return sorted(
            sorted_candidates,
            key=lambda candidate: candidate.name,
            reverse=reverse_primary,
        )
    if validated_sort_by == "composite_score":
        sorted_candidates = sorted(
            candidates,
            key=lambda candidate: (candidate.symbol, candidate.d0_date),
        )
        return sorted(
            sorted_candidates,
            key=lambda candidate: (
                candidate.composite_score
                if candidate.composite_score is not None
                else float("-inf")
            ),
            reverse=reverse_primary,
        )

    sorted_candidates = sorted(
        candidates,
        key=lambda candidate: (candidate.symbol, candidate.name),
    )
    return sorted(
        sorted_candidates,
        key=lambda candidate: candidate.d0_date,
        reverse=reverse_primary,
    )


def _apply_filter_rules(
    connection,
    candidates: list[PatternSelectionCandidate],
    selection_config: PatternSelectionConfig,
) -> list[PatternSelectionCandidate]:
    if not candidates or not selection_config.filter_rules:
        return candidates
    feature_rows = load_selection_rows_for_symbol_dates_with_connection(
        connection,
        {
            (candidate.symbol, candidate.selection_date)
            for candidate in candidates
        },
        end_date=max(candidate.selection_date for candidate in candidates),
    )
    ordered_rows = [
        feature_rows[(candidate.symbol, candidate.selection_date)]
        for candidate in candidates
        if (candidate.symbol, candidate.selection_date) in feature_rows
    ]
    try:
        matched_rows = filter_selection_rows_by_rules(
            ordered_rows,
            [rule.to_selection_rule() for rule in selection_config.filter_rules],
        )
    except StockSelectionError as exc:
        raise PatternSelectionError(str(exc)) from exc
    matched_keys = {
        (row.symbol, row.trade_date)
        for row in matched_rows
    }
    return [
        candidate
        for candidate in candidates
        if (candidate.symbol, candidate.selection_date) in matched_keys
    ]


def _select_second_volume_launch_for_date(
    connection,
    selection_date: date,
    params: SecondVolumeLaunchParams,
) -> list[PatternSelectionCandidate]:
    rows = connection.execute(
        _pattern_sql(params),
        [
            selection_date,
            selection_date,
            selection_date,
            selection_date,
            params.d0_change_min_pct,
            params.k0_volume_multiple,
            selection_date,
            params.platform_max_trade_days,
            params.d1_change_min_pct,
            params.k1_volume_multiple,
            params.d1_prev2_max_change_pct,
        ],
    ).fetchall()
    return [
        PatternSelectionCandidate(
            symbol=row[0],
            name=row[1],
            selection_date=selection_date,
            d0_date=row[2],
        )
        for row in rows
    ]


def _select_limit_up_pullback_ma5_reclaim_for_date(
    connection,
    selection_date: date,
    params: LimitUpPullbackMa5ReclaimParams,
) -> list[PatternSelectionCandidate]:
    support_low_factor = _pct_down_factor(params.support_low_tolerance_pct)
    close_support_factor = _pct_down_factor(params.close_support_tolerance_pct)
    touch_low_factor = _pct_up_factor(params.must_touch_low_zone_pct)
    recent_ma5_touch_factor = _pct_up_factor(params.recent_ma5_touch_tolerance_pct)
    signal_reclaim_low_factor = _pct_down_factor(params.signal_reclaim_low_tolerance_pct)
    signal_max_distance_factor = _pct_up_factor(params.signal_max_distance_from_t0_low_pct)
    deep_support_low_factor = _pct_down_factor(params.deep_support_low_tolerance_pct)
    deep_close_support_factor = _pct_down_factor(params.deep_close_support_tolerance_pct)
    deep_signal_reclaim_low_factor = _pct_down_factor(params.deep_signal_reclaim_low_tolerance_pct)
    anchor_volume_multiple = params.limit_up_volume_multiple
    if params.deep_pullback_enabled:
        anchor_volume_multiple = min(anchor_volume_multiple, params.deep_limit_up_volume_multiple)
    rows = connection.execute(
        _limit_up_pullback_ma5_reclaim_sql(params),
        [
            selection_date,
            selection_date,
            selection_date,
            params.signal_min_change_pct,
            params.signal_volume_ratio_min,
            params.signal_volume_ratio_max,
            recent_ma5_touch_factor,
            selection_date,
            params.limit_up_min_pct,
            params.limit_up_close_high_min_ratio,
            anchor_volume_multiple,
            params.weak_day_max_change_pct,
            selection_date,
            params.pullback_min_trade_days,
            params.pullback_max_trade_days,
            params.min_pullback_weak_days,
            support_low_factor,
            close_support_factor,
            params.pullback_max_daily_change_pct,
            params.pullback_min_daily_change_pct,
            params.pullback_avg_volume_max_ratio,
            signal_reclaim_low_factor,
            params.deep_pullback_enabled,
            support_low_factor,
            deep_support_low_factor,
            deep_close_support_factor,
            params.deep_pullback_max_daily_change_pct,
            params.deep_pullback_min_daily_change_pct,
            params.deep_pullback_avg_volume_max_ratio,
            deep_signal_reclaim_low_factor,
            touch_low_factor,
            signal_max_distance_factor,
        ],
    ).fetchall()
    return [
        PatternSelectionCandidate(
            symbol=row[0],
            name=row[1],
            selection_date=selection_date,
            d0_date=row[2],
            composite_score=float(row[3]) if row[3] is not None else None,
        )
        for row in rows
    ]


def select_pattern_candidates_with_connection(
    connection,
    selection_date: date,
    selection_config: PatternSelectionConfig,
) -> list[PatternSelectionCandidate]:
    validate_trade_date(connection, selection_date, PatternSelectionError)

    if selection_config.pattern_id == "second_volume_launch_v1":
        candidates = _select_second_volume_launch_for_date(
            connection=connection,
            selection_date=selection_date,
            params=selection_config.params,
        )
    elif selection_config.pattern_id == "limit_up_pullback_ma5_reclaim_v1":
        candidates = _select_limit_up_pullback_ma5_reclaim_for_date(
            connection=connection,
            selection_date=selection_date,
            params=selection_config.params,
        )
    else:
        raise PatternSelectionError(
            f"Unsupported pattern_id: {selection_config.pattern_id}."
        )
    sorted_candidates = _sort_candidates(
        candidates,
        sort_by=selection_config.sort_by,
        sort_order=selection_config.sort_order,
    )
    return _apply_filter_rules(connection, sorted_candidates, selection_config)


def select_pattern_candidates_in_range_with_connection(
    connection,
    start_date: date,
    end_date: date,
    selection_config: PatternSelectionConfig,
) -> dict[date, list[PatternSelectionCandidate]]:
    validate_trade_date(connection, start_date, PatternSelectionError)
    validate_trade_date(connection, end_date, PatternSelectionError)
    trade_dates = load_trade_dates_in_range(connection, start_date, end_date)
    return {
        trade_date: select_pattern_candidates_with_connection(
            connection,
            trade_date,
            selection_config,
        )
        for trade_date in trade_dates
    }
