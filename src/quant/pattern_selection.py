from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from decimal import Decimal

from quant.backtest_common import load_trade_dates_in_range, validate_trade_date

from quant.config.models import (
    FirstLimitUpTrendContinuationParams,
    LimitUpFirstPullbackMa5ReclaimParams,
    LowPositionVolumeBreakoutParams,
    LimitUpPullbackMa5ReclaimParams,
    MainForceAccumulationParams,
    PatternSelectionConfig,
    SecondVolumeLaunchParams,
    AllTimeHighParams,
    TrendMainWaveRelayParams,
)
from quant.selection import (
    StockSelectionError,
    filter_selection_rows_by_rules,
    load_selection_rows_for_symbol_dates_with_connection,
)


SUPPORTED_PATTERN_SORT_ORDERS = {"ASC", "DESC"}
SUPPORTED_PATTERN_SORT_FIELDS = {"symbol", "name", "d0_date", "composite_score", "anti_hype_score", "pool_score_v2", "pool_score_v3", "pool_score_ic", "branch_type", "board_count", "first_limit_date", "last_limit_date"}

# 反热度分: 5个已OOS验证方向的买入时可观测特征(check_tradeable.py, k1=1.5池)
# 方向: 越安静/低换手/不追高/洗盘充分 => 未来越好; 数值=日内百分位秩合成, 越高越"反热度"
_ANTI_HYPE_FEATURES = (
    ("f_signal_change", -1.0),
    ("f_signal_turnover", -1.0),
    ("f_signal_vs_d0_close_pct", -1.0),
    ("f_platform_avg_vol_vs_d0", -1.0),
    ("f_platform_weak_day_ratio", 1.0),
)


class PatternSelectionError(ValueError):
    """Raised when a pattern-based selection request is invalid."""


@dataclass(frozen=True)
class PatternSelectionCandidate:
    symbol: str
    name: str
    selection_date: date
    d0_date: date | None
    composite_score: float | None = None
    branch_type: str | None = None
    board_count: int | None = None
    first_limit_date: date | None = None
    first_limit_open: float | None = None
    first_limit_close: float | None = None
    last_limit_date: date | None = None
    last_limit_open: float | None = None
    last_limit_close: float | None = None
    signal_open: float | None = None
    signal_close: float | None = None
    signal_ma5: float | None = None
    min_close_after_last_limit: float | None = None
    min_low_after_last_limit: float | None = None
    max_high_after_last_limit: float | None = None
    false_break_occurred: bool | None = None
    false_break_reclaim_days_actual: int | None = None
    # 买入时可观测特征 (仅 second_volume_launch 填充, 供选股池排序研究)
    f_signal_change: float | None = None
    f_signal_turnover: float | None = None
    f_signal_vol_ratio_prev5: float | None = None
    f_signal_vol_vs_prev1: float | None = None
    f_d0_vol_ratio_prev5: float | None = None
    f_signal_vs_d0_close_pct: float | None = None
    f_platform_min_low_vs_d0_low_pct: float | None = None
    f_platform_avg_vol_vs_d0: float | None = None
    f_platform_weak_day_ratio: float | None = None
    f_between_days: int | None = None
    pool_score_v2: float | None = None
    pool_score_v3: float | None = None
    pool_score_ic: float | None = None


PATTERN_SQL_TEMPLATE = """
WITH bars AS (
  SELECT
    d.security_id, d.trade_date,
    COALESCE(d.open_qfq, d.open_price) AS open_price,
    COALESCE(d.high_qfq, d.high_price) AS high_price,
    COALESCE(d.low_qfq, d.low_price) AS low_price,
    COALESCE(d.close_qfq, d.close_price) AS close_price,
    d.volume, d.change_pct, d.turnover_ratio,
    row_number() OVER (PARTITION BY d.security_id ORDER BY d.trade_date DESC) AS rn_desc,
    avg(d.volume) OVER (
      PARTITION BY d.security_id
      ORDER BY d.trade_date
      ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
    ) AS prev5_avg_vol,
    lag(d.volume, 1) OVER (PARTITION BY d.security_id ORDER BY d.trade_date) AS prev1_volume,
    lag(d.change_pct, 1) OVER (PARTITION BY d.security_id ORDER BY d.trade_date) AS prev1_change_pct,
    lag(d.change_pct, 2) OVER (PARTITION BY d.security_id ORDER BY d.trade_date) AS prev2_change_pct,
    min(d.low_price) OVER (
      PARTITION BY d.security_id
      ORDER BY d.trade_date
      ROWS BETWEEN {low_position_window} PRECEDING AND 1 PRECEDING
    ) AS lp_win_low,
    max(d.high_price) OVER (
      PARTITION BY d.security_id
      ORDER BY d.trade_date
      ROWS BETWEEN {low_position_window} PRECEDING AND 1 PRECEDING
    ) AS lp_win_high
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
    d0.close_price AS d0_close,
    d0.change_pct AS d0_change,
    d0.prev5_avg_vol AS d0_prev5_avg_vol,
    d0.volume     AS d0_vol,
    min(b.low_price) AS min_low_between,
    min(b.close_price) AS min_close_between,
    avg(b.volume) AS avg_volume_between,
    sum(
      CASE
        WHEN b.close_price <= b.open_price OR b.change_pct <= 1.5 THEN 1
        ELSE 0
      END
    ) AS weak_days,
    count(*) AS between_days
  FROM d0_candidates d0
  JOIN last_window b
    ON b.security_id = d0.security_id
   AND b.trade_date > d0.trade_date
   AND b.trade_date < CAST(? AS DATE)
  GROUP BY d0.security_id, d0.trade_date, d0.low_price, d0.close_price, d0.change_pct, d0.prev5_avg_vol, d0.volume
),
picked AS (
  SELECT
    bl.security_id,
    bl.d0_date,
    bl.d0_low,
    bl.d0_close,
    bl.d0_change,
    bl.d0_prev5_avg_vol,
    bl.d0_vol,
    bl.between_days,
    bl.min_low_between,
    bl.min_close_between,
    bl.avg_volume_between,
    bl.weak_days,
    d1.close_price AS signal_close,
    d1.change_pct AS signal_change,
    d1.turnover_ratio AS signal_turnover,
    d1.volume AS signal_volume,
    d1.prev1_volume AS signal_prev1_volume,
    d1.prev5_avg_vol AS signal_prev5_avg_vol,
    d1.prev1_change_pct AS signal_prev1_change_pct,
    d1.prev2_change_pct AS signal_prev2_change_pct
  FROM between_lows bl
  JOIN d1 ON d1.security_id = bl.security_id
  WHERE bl.between_days <= ?
    AND bl.min_low_between >= bl.d0_low * {platform_min_low_ratio}
    AND d1.change_pct > ?
    AND d1.prev1_volume IS NOT NULL
    AND d1.volume >= ? * d1.prev1_volume
    AND d1.prev1_change_pct IS NOT NULL
    AND d1.prev2_change_pct IS NOT NULL
    AND (d1.prev1_change_pct + d1.prev2_change_pct) < ?
    {low_position_filter}
),
score_calc AS (
  SELECT
    p.*,
    CASE WHEN p.d0_prev5_avg_vol > 0 THEN p.d0_vol / p.d0_prev5_avg_vol ELSE NULL END AS d0_volume_ratio_prev5,
    CASE WHEN p.signal_prev1_volume > 0 THEN p.signal_volume / p.signal_prev1_volume ELSE NULL END AS signal_volume_vs_prev1,
    CASE WHEN p.signal_prev5_avg_vol > 0 THEN p.signal_volume / p.signal_prev5_avg_vol ELSE NULL END AS signal_volume_ratio_prev5,
    CASE WHEN p.d0_low > 0 THEN (p.signal_close - p.d0_low) / p.d0_low * 100 ELSE NULL END AS signal_close_vs_d0_low_pct,
    CASE WHEN p.d0_close > 0 THEN (p.signal_close - p.d0_close) / p.d0_close * 100 ELSE NULL END AS signal_close_vs_d0_close_pct,
    CASE WHEN p.d0_low > 0 THEN (p.min_low_between - p.d0_low) / p.d0_low * 100 ELSE NULL END AS platform_min_low_vs_d0_low_pct,
    CASE WHEN p.d0_low > 0 THEN (p.min_close_between - p.d0_low) / p.d0_low * 100 ELSE NULL END AS platform_min_close_vs_d0_low_pct,
    CASE WHEN p.d0_vol > 0 THEN p.avg_volume_between / p.d0_vol ELSE NULL END AS platform_avg_volume_vs_d0,
    CASE WHEN p.between_days > 0 THEN p.weak_days * 1.0 / p.between_days ELSE NULL END AS platform_weak_day_ratio
  FROM picked p
),
score_components AS (
  SELECT
    sc.*,
    (
      LEAST(GREATEST((sc.signal_change - 5.0) / 5.0 * 100, 0), 100) * 0.45
      + LEAST(GREATEST(COALESCE(sc.signal_close_vs_d0_close_pct, 0) / 25.0 * 100, 0), 100) * 0.35
      + LEAST(GREATEST(COALESCE(sc.signal_close_vs_d0_low_pct, 0) / 35.0 * 100, 0), 100) * 0.20
    ) AS signal_strength_raw,
    (
      (100 - LEAST(ABS(COALESCE(sc.signal_volume_vs_prev1, 0) - 1.6) / 1.6 * 100, 100)) * 0.40
      + (100 - LEAST(ABS(COALESCE(sc.signal_volume_ratio_prev5, 0) - 1.4) / 1.4 * 100, 100)) * 0.40
      + (100 - LEAST(ABS(COALESCE(sc.platform_avg_volume_vs_d0, 0) - 0.9) / 0.9 * 100, 100)) * 0.20
    ) AS volume_health_raw,
    (
      (100 - LEAST(ABS(sc.between_days - 20.0) / 20.0 * 100, 100)) * 0.35
      + (100 - LEAST(COALESCE(sc.platform_weak_day_ratio, 1) * 100, 100)) * 0.35
      + LEAST(GREATEST(COALESCE(sc.platform_min_close_vs_d0_low_pct, 0) / 10.0 * 100, 0), 100) * 0.30
    ) AS platform_quality_raw,
    (
      LEAST(GREATEST((sc.d0_change - 5.0) / 5.0 * 100, 0), 100) * 0.55
      + LEAST(GREATEST(COALESCE(sc.d0_volume_ratio_prev5, 0) / 4.0 * 100, 0), 100) * 0.45
    ) AS d0_launch_quality_raw,
    LEAST(GREATEST(COALESCE(sc.signal_turnover, 0) / 15.0 * 100, 0), 100) AS turnover_strength_raw,
    (
      CASE
        WHEN COALESCE(sc.signal_volume_ratio_prev5, 0) > 2.6
          THEN LEAST((sc.signal_volume_ratio_prev5 - 2.6) / 2.0 * 15, 15)
        ELSE 0
      END
      + CASE
        WHEN COALESCE(sc.signal_volume_vs_prev1, 0) > 3.0
          THEN LEAST((sc.signal_volume_vs_prev1 - 3.0) / 2.0 * 10, 10)
        ELSE 0
      END
      + CASE
        WHEN COALESCE(sc.platform_weak_day_ratio, 0) > 0.8
          THEN LEAST((sc.platform_weak_day_ratio - 0.8) / 0.2 * 10, 10)
        ELSE 0
      END
      + CASE
        WHEN sc.between_days > 30
          THEN LEAST((sc.between_days - 30.0) / 10.0 * 5, 5)
        ELSE 0
      END
    ) AS overheating_penalty
  FROM score_calc sc
),
scored AS (
  SELECT
    s.symbol,
    s.name,
    sc.d0_date,
    (
      (CASE WHEN (mx.mx_signal - mn.mn_signal) > 0
        THEN (sc.signal_strength_raw - mn.mn_signal) / (mx.mx_signal - mn.mn_signal) * 100
        ELSE 50 END) * 0.35
      + (CASE WHEN (mx.mx_volume - mn.mn_volume) > 0
        THEN (sc.volume_health_raw - mn.mn_volume) / (mx.mx_volume - mn.mn_volume) * 100
        ELSE 50 END) * 0.25
      + (CASE WHEN (mx.mx_platform - mn.mn_platform) > 0
        THEN (sc.platform_quality_raw - mn.mn_platform) / (mx.mx_platform - mn.mn_platform) * 100
        ELSE 50 END) * 0.20
      + (CASE WHEN (mx.mx_d0 - mn.mn_d0) > 0
        THEN (sc.d0_launch_quality_raw - mn.mn_d0) / (mx.mx_d0 - mn.mn_d0) * 100
        ELSE 50 END) * 0.15
      + (CASE WHEN (mx.mx_turnover - mn.mn_turnover) > 0
        THEN (sc.turnover_strength_raw - mn.mn_turnover) / (mx.mx_turnover - mn.mn_turnover) * 100
        ELSE 50 END) * 0.05
      - sc.overheating_penalty
    ) AS composite_score,
    sc.signal_change AS f_signal_change,
    sc.signal_turnover AS f_signal_turnover,
    sc.signal_volume_ratio_prev5 AS f_signal_vol_ratio_prev5,
    sc.signal_volume_vs_prev1 AS f_signal_vol_vs_prev1,
    sc.d0_volume_ratio_prev5 AS f_d0_vol_ratio_prev5,
    sc.signal_close_vs_d0_close_pct AS f_signal_vs_d0_close_pct,
    sc.platform_min_low_vs_d0_low_pct AS f_platform_min_low_vs_d0_low_pct,
    sc.platform_avg_volume_vs_d0 AS f_platform_avg_vol_vs_d0,
    sc.platform_weak_day_ratio AS f_platform_weak_day_ratio,
    sc.between_days AS f_between_days
  FROM score_components sc
  JOIN security_master s ON s.security_id = sc.security_id
  CROSS JOIN (
    SELECT
      MAX(signal_strength_raw) AS mx_signal,
      MAX(volume_health_raw) AS mx_volume,
      MAX(platform_quality_raw) AS mx_platform,
      MAX(d0_launch_quality_raw) AS mx_d0,
      MAX(turnover_strength_raw) AS mx_turnover
    FROM score_components
  ) mx
  CROSS JOIN (
    SELECT
      MIN(signal_strength_raw) AS mn_signal,
      MIN(volume_health_raw) AS mn_volume,
      MIN(platform_quality_raw) AS mn_platform,
      MIN(d0_launch_quality_raw) AS mn_d0,
      MIN(turnover_strength_raw) AS mn_turnover
    FROM score_components
  ) mn
)
SELECT symbol, name, d0_date, composite_score,
  f_signal_change, f_signal_turnover, f_signal_vol_ratio_prev5,
  f_signal_vol_vs_prev1, f_d0_vol_ratio_prev5, f_signal_vs_d0_close_pct,
  f_platform_min_low_vs_d0_low_pct, f_platform_avg_vol_vs_d0,
  f_platform_weak_day_ratio, f_between_days
FROM scored
ORDER BY composite_score DESC, symbol ASC
"""


def _pattern_sql(params: SecondVolumeLaunchParams) -> str:
    if params.max_d0_position_pct is not None:
        low_position_filter = (
            f"AND d1.lp_win_high IS NOT NULL "
            f"AND d1.lp_win_low IS NOT NULL "
            f"AND d1.lp_win_high > d1.lp_win_low "
            f"AND (bl.d0_close - d1.lp_win_low) / (d1.lp_win_high - d1.lp_win_low) * 100 <= {params.max_d0_position_pct}"
        )
    else:
        low_position_filter = ""
    sql = PATTERN_SQL_TEMPLATE.format(
        lookback_calendar_days=params.lookback_calendar_days,
        low_position_window=params.low_position_window,
        platform_min_low_ratio=float(params.platform_min_low_ratio),
        low_position_filter=low_position_filter,
    )
    if params.enable_v2_filter:
        # V2 过滤 (3 条从大涨票规律分析中提炼):
        # 规律 1: f_signal_vs_d0_close_pct >= 15% (D1 收盘相对 D0 收盘的涨幅,巨涨组 18.12% vs 不涨组 15.66%)
        # 规律 3: composite_score >= 60 (score>70 桶巨涨率 8.3% vs <50 桶 5.2%)
        # 规律 6: f_signal_vol_ratio_prev5 <= 2.5 (D1 量/前5日均量,大涨组均值 2.0)
        # 在最终 SELECT 加 WHERE,所有字段都可用
        sql = sql.replace(
            "FROM scored\nORDER BY composite_score DESC, symbol ASC",
            "FROM scored\nWHERE composite_score >= 60\n  AND f_signal_vs_d0_close_pct >= 15\n  AND f_signal_vol_ratio_prev5 <= 2.5\nORDER BY composite_score DESC, symbol ASC",
        )
    return sql


LIMIT_UP_PULLBACK_MA5_RECLAIM_SQL_TEMPLATE = """
WITH bars AS (
  SELECT
    d.security_id, d.trade_date,
    COALESCE(d.open_qfq, d.open_price) AS open_price,
    COALESCE(d.high_qfq, d.high_price) AS high_price,
    COALESCE(d.low_qfq, d.low_price) AS low_price,
    COALESCE(d.close_qfq, d.close_price) AS close_price,
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


def _pct_rank(values: list[float]) -> list[float]:
    """日内百分位秩(平均法处理并列), 对齐 pandas rank(pct=True), 取值 (0,1]。"""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1-based 平均秩
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank / n
        i = j + 1
    return ranks


def _anti_hype_scores(candidates: list[PatternSelectionCandidate]) -> list[float]:
    """当日候选的反热度分: 每个可用特征取日内百分位秩×方向, 跨特征平均。
    缺失特征取中性 0.5; 当日无任何可用特征则全部 -inf(排序退化为稳定序)。"""
    n = len(candidates)
    totals = [0.0] * n
    feats_used = 0
    for feat, sign in _ANTI_HYPE_FEATURES:
        vals = [getattr(c, feat) for c in candidates]
        present_idx = [i for i in range(n) if vals[i] is not None]
        present_vals = [vals[i] for i in present_idx]
        if len(present_idx) < 3 or len(set(present_vals)) < 2:
            continue
        pr_present = _pct_rank(present_vals)
        pr = [0.5] * n
        for pos, i in enumerate(present_idx):
            pr[i] = pr_present[pos]
        for i in range(n):
            totals[i] += sign * pr[i]
        feats_used += 1
    if feats_used == 0:
        return [float("-inf")] * n
    return [totals[i] / feats_used for i in range(n)]


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
            key=lambda candidate: (candidate.symbol, candidate.d0_date or candidate.selection_date),
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
    if validated_sort_by == "anti_hype_score":
        scores = _anti_hype_scores(candidates)
        order = sorted(
            range(len(candidates)),
            key=lambda i: (candidates[i].symbol, candidates[i].d0_date or candidates[i].selection_date),
        )
        order = sorted(order, key=lambda i: scores[i], reverse=reverse_primary)
        return [candidates[i] for i in order]
    if validated_sort_by == "pool_score_v2":
        sorted_candidates = sorted(
            candidates,
            key=lambda candidate: (candidate.symbol, candidate.d0_date or candidate.selection_date),
        )
        return sorted(
            sorted_candidates,
           key=lambda candidate: (
               candidate.pool_score_v2
               if candidate.pool_score_v2 is not None
               else float("-inf")
           ),
           reverse=reverse_primary,
       )
    if validated_sort_by == "pool_score_v3":
        sorted_candidates = sorted(
            candidates,
            key=lambda candidate: (candidate.symbol, candidate.d0_date or candidate.selection_date),
        )
        return sorted(
            sorted_candidates,
            key=lambda candidate: (
                candidate.pool_score_v3
                if candidate.pool_score_v3 is not None
                else float("-inf")
            ),
            reverse=reverse_primary,
        )
    if validated_sort_by == "pool_score_ic":
        sorted_candidates = sorted(
            candidates,
            key=lambda candidate: (candidate.symbol, candidate.d0_date or candidate.selection_date),
        )
        return sorted(
            sorted_candidates,
            key=lambda candidate: (
                candidate.pool_score_ic
                if candidate.pool_score_ic is not None
                else float("-inf")
            ),
            reverse=reverse_primary,
        )
    if validated_sort_by == "branch_type":
        sorted_candidates = sorted(
            candidates,
            key=lambda candidate: (candidate.symbol, candidate.d0_date or candidate.selection_date),
        )
        return sorted(
            sorted_candidates,
            key=lambda candidate: candidate.branch_type or "",
            reverse=reverse_primary,
        )
    if validated_sort_by == "board_count":
        sorted_candidates = sorted(
            candidates,
            key=lambda candidate: (candidate.symbol, candidate.d0_date or candidate.selection_date),
        )
        return sorted(
            sorted_candidates,
            key=lambda candidate: candidate.board_count or 0,
            reverse=reverse_primary,
        )
    if validated_sort_by in {"first_limit_date", "last_limit_date"}:
        sorted_candidates = sorted(
            candidates,
            key=lambda candidate: (candidate.symbol, candidate.name),
        )
        return sorted(
            sorted_candidates,
            key=lambda candidate: (
                getattr(candidate, validated_sort_by) or candidate.d0_date or candidate.selection_date
            ),
            reverse=reverse_primary,
        )

    sorted_candidates = sorted(
        candidates,
        key=lambda candidate: (candidate.symbol, candidate.name),
    )
    return sorted(
        sorted_candidates,
        key=lambda candidate: candidate.d0_date or candidate.selection_date,
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
            composite_score=float(row[3]) if row[3] is not None else None,
            f_signal_change=_to_float(row[4]),
            f_signal_turnover=_to_float(row[5]),
            f_signal_vol_ratio_prev5=_to_float(row[6]),
            f_signal_vol_vs_prev1=_to_float(row[7]),
            f_d0_vol_ratio_prev5=_to_float(row[8]),
            f_signal_vs_d0_close_pct=_to_float(row[9]),
            f_platform_min_low_vs_d0_low_pct=_to_float(row[10]),
            f_platform_avg_vol_vs_d0=_to_float(row[11]),
            f_platform_weak_day_ratio=_to_float(row[12]),
            f_between_days=int(row[13]) if row[13] is not None else None,
        )
        for row in rows
    ]

def _select_all_time_high_for_date(connection, selection_date: date, params: AllTimeHighParams) -> list[PatternSelectionCandidate]:
    rows = connection.execute("""WITH b AS (SELECT d.security_id,d.trade_date,COALESCE(d.high_qfq,d.high_price) high_price,s.symbol,s.name,s.board,s.list_status,s.list_date FROM daily_bar d JOIN security_master s ON s.security_id=d.security_id WHERE d.trade_date<=?) SELECT symbol,name,trade_date,high_price,prior_high FROM (SELECT *,MAX(high_price) OVER(PARTITION BY security_id ORDER BY trade_date ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) prior_high FROM b) x WHERE trade_date=? AND prior_high IS NOT NULL AND high_price>=prior_high AND (?=false OR board IN ('主板','main','MAIN') OR board IS NULL) AND list_status='listed' AND (?=false OR (name NOT LIKE '%ST%' AND name NOT LIKE '%退%')) AND (list_date IS NULL OR date_diff('day',list_date,?)>=?)""", [selection_date, selection_date, params.require_main_board, params.exclude_st, selection_date, params.min_listed_days]).fetchall()
    return [PatternSelectionCandidate(symbol=r[0], name=r[1], selection_date=selection_date, d0_date=r[2], composite_score=float((r[3]/r[4]-1)*100)) for r in rows]


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


BRANCH_SINGLE_DIRECT = "single_direct_pullback_reclaim"
BRANCH_SINGLE_PLATFORM = "single_platform_then_pullback_reclaim"
BRANCH_FALSE_BREAK = "false_break_first_open_reclaim"
BRANCH_MULTI_LIMIT = "multi_limit_pullback_reclaim"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _is_effective_limit_up(row: dict[str, Any], params: LimitUpFirstPullbackMa5ReclaimParams) -> bool:
    prev5_avg_vol = row.get("prev5_avg_vol")
    high_price = row.get("high_price")
    close_price = row.get("close_price")
    open_price = row.get("open_price")
    volume = row.get("volume")
    change_pct = row.get("change_pct")
    if (
        prev5_avg_vol is None
        or high_price is None
        or close_price is None
        or open_price is None
        or volume is None
        or change_pct is None
    ):
        return False
    if prev5_avg_vol <= 0 or high_price <= 0:
        return False
    return (
        change_pct >= float(params.limit_up_min_pct)
        and close_price >= high_price * float(params.limit_up_close_high_min_ratio)
        and close_price > open_price
        and volume >= prev5_avg_vol * float(params.limit_up_volume_multiple)
    )


def _false_break_reclaim_days(
    between_rows: list[dict[str, Any]],
    *,
    first_limit_open: float,
    max_reclaim_days: int,
) -> int | None:
    first_break_index = next(
        (idx for idx, row in enumerate(between_rows) if row["low_price"] < first_limit_open),
        None,
    )
    if first_break_index is None:
        return 0
    last_index = min(len(between_rows) - 1, first_break_index + max_reclaim_days)
    for idx in range(first_break_index, last_index + 1):
        if between_rows[idx]["close_price"] >= first_limit_open:
            return idx - first_break_index
    return None


def _classify_first_pullback_branch(
    *,
    board_count: int,
    false_break_occurred: bool,
    max_high_after_last_limit: float,
    last_limit_close: float,
) -> str:
    if board_count >= 2:
        return BRANCH_MULTI_LIMIT
    if false_break_occurred:
        return BRANCH_FALSE_BREAK
    if max_high_after_last_limit > last_limit_close:
        return BRANCH_SINGLE_PLATFORM
    return BRANCH_SINGLE_DIRECT


def _pullback_confirmed(
    between_rows: list[dict[str, Any]],
    *,
    last_limit_close: float,
    mode: str,
) -> bool:
    below_last_limit_close = any(row["close_price"] < last_limit_close for row in between_rows)
    ma5_touch = any(
        row.get("ma5_price") is not None
        and (row["low_price"] <= row["ma5_price"] or row["close_price"] <= row["ma5_price"])
        for row in between_rows
    )
    if mode == "below_last_limit_close":
        return below_last_limit_close
    if mode == "ma5_touch":
        return ma5_touch
    return below_last_limit_close or ma5_touch


def _select_limit_up_first_pullback_ma5_reclaim_for_date(
    connection,
    selection_date: date,
    params: LimitUpFirstPullbackMa5ReclaimParams,
) -> list[PatternSelectionCandidate]:
    start_date = selection_date - timedelta(days=params.lookback_calendar_days)
    raw_rows = connection.execute(
        """
        SELECT
          s.symbol,
          s.name,
          d.security_id,
          d.trade_date,
          d.open_price,
          d.high_price,
          d.low_price,
          d.close_price,
          d.volume,
          d.change_pct,
          d.ma5_price,
          d.turnover_ratio
        FROM daily_bar d
        JOIN security_master s ON s.security_id = d.security_id
        WHERE d.trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
        ORDER BY s.symbol, d.trade_date
        """,
        [start_date, selection_date],
    ).fetchall()

    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in raw_rows:
        item = {
            "symbol": row[0],
            "name": row[1],
            "security_id": row[2],
            "trade_date": row[3],
            "open_price": _to_float(row[4]),
            "high_price": _to_float(row[5]),
            "low_price": _to_float(row[6]),
            "close_price": _to_float(row[7]),
            "volume": _to_float(row[8]),
            "change_pct": _to_float(row[9]),
            "ma5_price": _to_float(row[10]),
            "turnover_ratio": _to_float(row[11]),
            "prev5_avg_vol": None,
            "is_limit_up": False,
        }
        grouped.setdefault(item["security_id"], []).append(item)

    enabled_branches = set(params.enabled_branches)
    candidates: list[PatternSelectionCandidate] = []
    signal_max_factor = float(_pct_up_factor(params.signal_max_above_last_limit_close_pct))
    false_break_factor = float(_pct_down_factor(params.false_break_tolerance_pct))
    platform_high_factor = float(_pct_up_factor(params.platform_high_limit_pct))

    for rows in grouped.values():
        for idx, row in enumerate(rows):
            prev_volumes = [r["volume"] for r in rows[max(0, idx - 5):idx] if r["volume"] is not None]
            if len(prev_volumes) == 5:
                row["prev5_avg_vol"] = sum(prev_volumes) / 5
        for row in rows:
            row["is_limit_up"] = _is_effective_limit_up(row, params)

        signal_idx = next((idx for idx, row in enumerate(rows) if row["trade_date"] == selection_date), None)
        if signal_idx is None:
            continue
        signal = rows[signal_idx]
        if signal["ma5_price"] is None:
            continue
        if params.signal_open_below_ma5 and signal["open_price"] > signal["ma5_price"]:
            continue
        if params.signal_close_above_ma5 and signal["close_price"] < signal["ma5_price"]:
            continue
        if params.signal_require_bullish_candle and signal["close_price"] <= signal["open_price"]:
            continue
        if signal["change_pct"] is not None and signal["change_pct"] < float(params.signal_min_change_pct):
            continue

        limit_indices = [idx for idx in range(signal_idx) if rows[idx]["is_limit_up"]]
        if not limit_indices:
            continue
        last_idx = limit_indices[-1]
        last_limit = rows[last_idx]

        board_indices = [last_idx]
        cursor = last_idx - 1
        while cursor >= 0 and rows[cursor]["is_limit_up"] and len(board_indices) < 4:
            board_indices.append(cursor)
            cursor -= 1
        board_indices = sorted(board_indices)
        if len(board_indices) > 3:
            continue
        board_count = len(board_indices)
        if board_count < params.min_board_count or board_count > params.max_board_count:
            continue
        first_limit = rows[board_indices[0]]

        between_rows = rows[last_idx + 1:signal_idx]
        between_days = len(between_rows)
        if not (params.pullback_min_trade_days <= between_days <= params.pullback_max_trade_days):
            continue
        if not between_rows:
            continue
        if signal["close_price"] > last_limit["close_price"] * signal_max_factor:
            continue
        if not _pullback_confirmed(
            between_rows,
            last_limit_close=last_limit["close_price"],
            mode=params.pullback_confirm_mode,
        ):
            continue

        min_close_after = min(row["close_price"] for row in between_rows)
        min_low_after = min(row["low_price"] for row in between_rows)
        max_high_after = max(row["high_price"] for row in between_rows)
        first_limit_open = first_limit["open_price"]
        false_break_occurred = min_low_after < first_limit_open
        standard_support = min_close_after >= first_limit_open
        reclaim_days = _false_break_reclaim_days(
            between_rows,
            first_limit_open=first_limit_open,
            max_reclaim_days=params.false_break_reclaim_days,
        )
        false_break_support = (
            params.enable_false_break
            and false_break_occurred
            and min_low_after >= first_limit_open * false_break_factor
            and reclaim_days is not None
            and reclaim_days <= params.false_break_reclaim_days
        )
        if false_break_occurred:
            if not false_break_support:
                continue
        elif not standard_support:
            continue

        if board_count == 1 and max_high_after > last_limit["close_price"] * platform_high_factor:
            continue

        branch_type = _classify_first_pullback_branch(
            board_count=board_count,
            false_break_occurred=false_break_occurred,
            max_high_after_last_limit=max_high_after,
            last_limit_close=last_limit["close_price"],
        )
        if branch_type not in enabled_branches:
            continue
        if branch_type == BRANCH_SINGLE_DIRECT and not params.enable_direct_pullback:
            continue
        if branch_type == BRANCH_SINGLE_PLATFORM and not params.enable_high_platform:
            continue
        if branch_type == BRANCH_FALSE_BREAK and not params.enable_false_break:
            continue

        reclaim_score_penalty = (reclaim_days or 0) * 5
        ma5_reclaim_strength = (
            (signal["close_price"] - signal["ma5_price"]) / signal["ma5_price"] * 100
            if signal["ma5_price"]
            else 0
        )
        distance_to_last_close = (
            (last_limit["close_price"] - signal["close_price"]) / last_limit["close_price"] * 100
            if last_limit["close_price"]
            else 0
        )
        composite_score = (
            50
            + board_count * 10
            + ma5_reclaim_strength * 3
            - max(distance_to_last_close, 0) * 0.5
            - reclaim_score_penalty
        )

        candidates.append(
            PatternSelectionCandidate(
                symbol=signal["symbol"],
                name=signal["name"],
                selection_date=selection_date,
                d0_date=last_limit["trade_date"],
                composite_score=float(composite_score),
                branch_type=branch_type,
                board_count=board_count,
                first_limit_date=first_limit["trade_date"],
                first_limit_open=first_limit["open_price"],
                first_limit_close=first_limit["close_price"],
                last_limit_date=last_limit["trade_date"],
                last_limit_open=last_limit["open_price"],
                last_limit_close=last_limit["close_price"],
                signal_open=signal["open_price"],
                signal_close=signal["close_price"],
                signal_ma5=signal["ma5_price"],
                min_close_after_last_limit=min_close_after,
                min_low_after_last_limit=min_low_after,
                max_high_after_last_limit=max_high_after,
                false_break_occurred=false_break_occurred,
                false_break_reclaim_days_actual=reclaim_days,
            )
        )

    return candidates


def _select_first_limit_up_trend_continuation_for_date(
    connection,
    selection_date: date,
    params: FirstLimitUpTrendContinuationParams,
) -> list[PatternSelectionCandidate]:
    rows = connection.execute(
        """
        WITH bars AS (
          SELECT
            s.symbol,
            s.name,
            d.security_id,
            d.trade_date,
            d.open_price,
            d.high_price,
            d.low_price,
            d.close_price,
            d.pre_close_price,
            d.volume,
            d.amount,
            d.change_pct,
            d.turnover_ratio,
            d.ma5_price,
            d.ma10_price,
            d.ma20_price,
            avg(d.volume) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS prev5_avg_volume,
            avg(d.volume) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
            ) AS prev20_avg_volume,
            sum(d.change_pct) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS prev5_change_sum,
            sum(d.change_pct) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 10 PRECEDING AND 1 PRECEDING
            ) AS prev10_change_sum,
            max(d.high_price) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
            ) AS prev20_high
          FROM daily_bar d
          JOIN security_master s ON s.security_id = d.security_id
        ),
        marked AS (
          SELECT
            *,
            CASE
              WHEN change_pct >= ?
               AND high_price > 0
               AND close_price >= high_price * ?
               AND close_price > open_price
              THEN 1 ELSE 0
            END AS is_limit_up,
            CASE WHEN prev5_avg_volume > 0 THEN volume / prev5_avg_volume ELSE NULL END AS volume_ratio_prev5,
            CASE WHEN prev20_avg_volume > 0 THEN volume / prev20_avg_volume ELSE NULL END AS volume_ratio_prev20,
            CASE WHEN amount IS NOT NULL THEN amount / 100000000.0 ELSE NULL END AS amount_yi,
            CASE WHEN ma5_price > 0 THEN (close_price - ma5_price) / ma5_price * 100 ELSE NULL END AS close_vs_ma5_pct,
            CASE WHEN ma10_price > 0 THEN (close_price - ma10_price) / ma10_price * 100 ELSE NULL END AS close_vs_ma10_pct,
            CASE WHEN ma20_price > 0 THEN (close_price - ma20_price) / ma20_price * 100 ELSE NULL END AS close_vs_ma20_pct,
            CASE WHEN ma10_price > 0 THEN (ma5_price - ma10_price) / ma10_price * 100 ELSE NULL END AS ma5_vs_ma10_pct,
            CASE WHEN ma20_price > 0 THEN (ma10_price - ma20_price) / ma20_price * 100 ELSE NULL END AS ma10_vs_ma20_pct,
            CASE WHEN prev20_high > 0 THEN (close_price - prev20_high) / prev20_high * 100 ELSE NULL END AS close_vs_prev20_high_pct,
            CASE WHEN close_price >= prev20_high THEN 1 ELSE 0 END AS break_prev20_high,
            CASE WHEN ma5_price > ma10_price AND ma10_price > ma20_price THEN 1 ELSE 0 END AS ma_bull_stack
          FROM bars
        ),
        first_boards AS (
          SELECT
            *,
            lag(is_limit_up, 1) OVER (PARTITION BY security_id ORDER BY trade_date) AS prev_is_limit_up
          FROM marked
        ),
        picked AS (
          SELECT *
          FROM first_boards
          WHERE trade_date = CAST(? AS DATE)
            AND is_limit_up = 1
            AND (? = false OR COALESCE(prev_is_limit_up, 0) = 0)
            AND volume_ratio_prev5 BETWEEN ? AND ?
            AND turnover_ratio BETWEEN ? AND ?
            AND amount_yi >= ?
            AND (? = false OR break_prev20_high = 1)
            AND (? = false OR ma_bull_stack = 1)
            AND prev10_change_sum BETWEEN ? AND ?
        ),
        score_calc AS (
          SELECT
            *,
            (
              LEAST(GREATEST(COALESCE(close_vs_ma20_pct, 0) / 20.0 * 100, 0), 100) * 0.35
              + LEAST(GREATEST(COALESCE(ma5_vs_ma10_pct, 0) / 5.0 * 100, 0), 100) * 0.25
              + LEAST(GREATEST(COALESCE(ma10_vs_ma20_pct, 0) / 5.0 * 100, 0), 100) * 0.25
              + ma_bull_stack * 100 * 0.15
            ) AS trend_quality,
            (
              (100 - LEAST(ABS(COALESCE(volume_ratio_prev5, 0) - 1.1) / 1.1 * 100, 100)) * 0.65
              + (100 - LEAST(ABS(COALESCE(volume_ratio_prev20, 0) - 1.2) / 1.2 * 100, 100)) * 0.35
            ) AS volume_health,
            (
              break_prev20_high * 65
              + LEAST(GREATEST(COALESCE(close_vs_prev20_high_pct, 0) / 8.0 * 100, 0), 100) * 0.35
            ) AS breakout_quality,
            (
              LEAST(GREATEST(COALESCE(prev5_change_sum, 0) / 8.0 * 100, 0), 100) * 0.45
              + LEAST(GREATEST(COALESCE(prev10_change_sum, 0) / 15.0 * 100, 0), 100) * 0.55
            ) AS preheat_quality,
            (100 - LEAST(ABS(COALESCE(turnover_ratio, 0) - 8.0) / 8.0 * 100, 100)) AS turnover_activity,
            (
              CASE
                WHEN COALESCE(volume_ratio_prev5, 0) > 2.5
                  THEN LEAST((volume_ratio_prev5 - 2.5) / 2.0 * 18, 18)
                ELSE 0
              END
              + CASE
                WHEN COALESCE(turnover_ratio, 0) > 25
                  THEN LEAST((turnover_ratio - 25.0) / 20.0 * 12, 12)
                ELSE 0
              END
              + CASE
                WHEN COALESCE(prev10_change_sum, 0) > 35
                  THEN LEAST((prev10_change_sum - 35.0) / 35.0 * 10, 10)
                ELSE 0
              END
            ) AS overheating_penalty
          FROM picked
        )
        SELECT
          symbol,
          name,
          trade_date,
          (
            trend_quality * 0.30
            + volume_health * 0.25
            + breakout_quality * 0.20
            + preheat_quality * 0.15
            + turnover_activity * 0.10
            - overheating_penalty
          ) AS composite_score,
          open_price,
          close_price,
          ma5_price
        FROM score_calc
        ORDER BY composite_score DESC, symbol ASC
        """,
        [
            params.limit_up_min_pct,
            params.limit_up_close_high_min_ratio,
            selection_date,
            params.require_first_board,
            params.volume_ratio_prev5_min,
            params.volume_ratio_prev5_max,
            params.turnover_min,
            params.turnover_max,
            params.amount_min_yi,
            params.require_break_prev20_high,
            params.require_ma_bull_stack,
            params.prev10_change_sum_min,
            params.prev10_change_sum_max,
        ],
    ).fetchall()
    return [
        PatternSelectionCandidate(
            symbol=row[0],
            name=row[1],
            selection_date=selection_date,
            d0_date=row[2],
            composite_score=float(row[3]) if row[3] is not None else None,
            branch_type="first_limit_up_trend_continuation",
            board_count=1,
            first_limit_date=row[2],
            first_limit_open=float(row[4]) if row[4] is not None else None,
            first_limit_close=float(row[5]) if row[5] is not None else None,
            last_limit_date=row[2],
            signal_open=float(row[4]) if row[4] is not None else None,
            signal_close=float(row[5]) if row[5] is not None else None,
            signal_ma5=float(row[6]) if row[6] is not None else None,
        )
        for row in rows
    ]


def _select_first_limit_up_trend_continuation_in_range(
    connection,
    start_date: date,
    end_date: date,
    params: FirstLimitUpTrendContinuationParams,
) -> dict[date, list[PatternSelectionCandidate]]:
    rows = connection.execute(
        """
        WITH bars AS (
          SELECT
            s.symbol,
            s.name,
            d.security_id,
            d.trade_date,
            d.open_price,
            d.high_price,
            d.low_price,
            d.close_price,
            d.pre_close_price,
            d.volume,
            d.amount,
            d.change_pct,
            d.turnover_ratio,
            d.ma5_price,
            d.ma10_price,
            d.ma20_price,
            avg(d.volume) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS prev5_avg_volume,
            avg(d.volume) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
            ) AS prev20_avg_volume,
            sum(d.change_pct) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS prev5_change_sum,
            sum(d.change_pct) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 10 PRECEDING AND 1 PRECEDING
            ) AS prev10_change_sum,
            max(d.high_price) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
            ) AS prev20_high
          FROM daily_bar d
          JOIN security_master s ON s.security_id = d.security_id
        ),
        marked AS (
          SELECT
            *,
            CASE
              WHEN change_pct >= ?
               AND high_price > 0
               AND close_price >= high_price * ?
               AND close_price > open_price
              THEN 1 ELSE 0
            END AS is_limit_up,
            CASE WHEN prev5_avg_volume > 0 THEN volume / prev5_avg_volume ELSE NULL END AS volume_ratio_prev5,
            CASE WHEN prev20_avg_volume > 0 THEN volume / prev20_avg_volume ELSE NULL END AS volume_ratio_prev20,
            CASE WHEN amount IS NOT NULL THEN amount / 100000000.0 ELSE NULL END AS amount_yi,
            CASE WHEN ma5_price > 0 THEN (close_price - ma5_price) / ma5_price * 100 ELSE NULL END AS close_vs_ma5_pct,
            CASE WHEN ma10_price > 0 THEN (close_price - ma10_price) / ma10_price * 100 ELSE NULL END AS close_vs_ma10_pct,
            CASE WHEN ma20_price > 0 THEN (close_price - ma20_price) / ma20_price * 100 ELSE NULL END AS close_vs_ma20_pct,
            CASE WHEN ma10_price > 0 THEN (ma5_price - ma10_price) / ma10_price * 100 ELSE NULL END AS ma5_vs_ma10_pct,
            CASE WHEN ma20_price > 0 THEN (ma10_price - ma20_price) / ma20_price * 100 ELSE NULL END AS ma10_vs_ma20_pct,
            CASE WHEN prev20_high > 0 THEN (close_price - prev20_high) / prev20_high * 100 ELSE NULL END AS close_vs_prev20_high_pct,
            CASE WHEN close_price >= prev20_high THEN 1 ELSE 0 END AS break_prev20_high,
            CASE WHEN ma5_price > ma10_price AND ma10_price > ma20_price THEN 1 ELSE 0 END AS ma_bull_stack
          FROM bars
        ),
        first_boards AS (
          SELECT
            *,
            lag(is_limit_up, 1) OVER (PARTITION BY security_id ORDER BY trade_date) AS prev_is_limit_up
          FROM marked
        ),
        picked AS (
          SELECT *
          FROM first_boards
          WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
            AND is_limit_up = 1
            AND (? = false OR COALESCE(prev_is_limit_up, 0) = 0)
            AND volume_ratio_prev5 BETWEEN ? AND ?
            AND turnover_ratio BETWEEN ? AND ?
            AND amount_yi >= ?
            AND (? = false OR break_prev20_high = 1)
            AND (? = false OR ma_bull_stack = 1)
            AND prev10_change_sum BETWEEN ? AND ?
        ),
        score_calc AS (
          SELECT
            *,
            (
              LEAST(GREATEST(COALESCE(close_vs_ma20_pct, 0) / 20.0 * 100, 0), 100) * 0.35
              + LEAST(GREATEST(COALESCE(ma5_vs_ma10_pct, 0) / 5.0 * 100, 0), 100) * 0.25
              + LEAST(GREATEST(COALESCE(ma10_vs_ma20_pct, 0) / 5.0 * 100, 0), 100) * 0.25
              + ma_bull_stack * 100 * 0.15
            ) AS trend_quality,
            (
              (100 - LEAST(ABS(COALESCE(volume_ratio_prev5, 0) - 1.1) / 1.1 * 100, 100)) * 0.65
              + (100 - LEAST(ABS(COALESCE(volume_ratio_prev20, 0) - 1.2) / 1.2 * 100, 100)) * 0.35
            ) AS volume_health,
            (
              break_prev20_high * 65
              + LEAST(GREATEST(COALESCE(close_vs_prev20_high_pct, 0) / 8.0 * 100, 0), 100) * 0.35
            ) AS breakout_quality,
            (
              LEAST(GREATEST(COALESCE(prev5_change_sum, 0) / 8.0 * 100, 0), 100) * 0.45
              + LEAST(GREATEST(COALESCE(prev10_change_sum, 0) / 15.0 * 100, 0), 100) * 0.55
            ) AS preheat_quality,
            (100 - LEAST(ABS(COALESCE(turnover_ratio, 0) - 8.0) / 8.0 * 100, 100)) AS turnover_activity,
            (
              CASE
                WHEN COALESCE(volume_ratio_prev5, 0) > 2.5
                  THEN LEAST((volume_ratio_prev5 - 2.5) / 2.0 * 18, 18)
                ELSE 0
              END
              + CASE
                WHEN COALESCE(turnover_ratio, 0) > 25
                  THEN LEAST((turnover_ratio - 25.0) / 20.0 * 12, 12)
                ELSE 0
              END
              + CASE
                WHEN COALESCE(prev10_change_sum, 0) > 35
                  THEN LEAST((prev10_change_sum - 35.0) / 35.0 * 10, 10)
                ELSE 0
              END
            ) AS overheating_penalty
          FROM picked
        )
        SELECT
          symbol,
          name,
          trade_date,
          (
            trend_quality * 0.30
            + volume_health * 0.25
            + breakout_quality * 0.20
            + preheat_quality * 0.15
            + turnover_activity * 0.10
            - overheating_penalty
          ) AS composite_score,
          open_price,
          close_price,
          ma5_price
        FROM score_calc
        ORDER BY trade_date, composite_score DESC, symbol ASC
        """,
        [
            params.limit_up_min_pct,
            params.limit_up_close_high_min_ratio,
            start_date,
            end_date,
            params.require_first_board,
            params.volume_ratio_prev5_min,
            params.volume_ratio_prev5_max,
            params.turnover_min,
            params.turnover_max,
            params.amount_min_yi,
            params.require_break_prev20_high,
            params.require_ma_bull_stack,
            params.prev10_change_sum_min,
            params.prev10_change_sum_max,
        ],
    ).fetchall()
    grouped: dict[date, list[PatternSelectionCandidate]] = {}
    for row in rows:
        selection_date = row[2]
        grouped.setdefault(selection_date, []).append(
            PatternSelectionCandidate(
                symbol=row[0],
                name=row[1],
                selection_date=selection_date,
                d0_date=selection_date,
                composite_score=float(row[3]) if row[3] is not None else None,
                branch_type="first_limit_up_trend_continuation",
                board_count=1,
                first_limit_date=selection_date,
                first_limit_open=float(row[4]) if row[4] is not None else None,
                first_limit_close=float(row[5]) if row[5] is not None else None,
                last_limit_date=selection_date,
                signal_open=float(row[4]) if row[4] is not None else None,
                signal_close=float(row[5]) if row[5] is not None else None,
                signal_ma5=float(row[6]) if row[6] is not None else None,
            )
        )
    return grouped


def _select_low_position_volume_breakout_in_range(
    connection,
    start_date: date,
    end_date: date,
    params: LowPositionVolumeBreakoutParams,
) -> dict[date, list[PatternSelectionCandidate]]:
    lookback = max(
        params.low_position_window,
        params.platform_window,
        params.breakout_window,
        20,
    )
    rows = connection.execute(
        f"""
        WITH bars AS (
          SELECT
            s.symbol,
            s.name,
            d.security_id,
            d.trade_date,
            d.open_price,
            d.high_price,
            d.low_price,
            d.close_price,
            d.volume,
            d.amount,
            d.change_pct,
            d.turnover_ratio,
            d.ma5_price,
            d.ma10_price,
            d.ma20_price,
            avg(d.volume) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS prev5_avg_volume,
            avg(d.amount) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS prev5_avg_amount,
            sum(d.change_pct) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 10 PRECEDING AND 1 PRECEDING
            ) AS prev10_change_sum,
            min(d.low_price) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN {params.low_position_window} PRECEDING AND 1 PRECEDING
            ) AS low_window_low,
            max(d.high_price) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN {params.low_position_window} PRECEDING AND 1 PRECEDING
            ) AS low_window_high,
            min(d.low_price) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN {params.platform_window} PRECEDING AND 1 PRECEDING
            ) AS platform_low,
            max(d.high_price) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN {params.platform_window} PRECEDING AND 1 PRECEDING
            ) AS platform_high,
            max(d.high_price) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN {params.breakout_window} PRECEDING AND 1 PRECEDING
            ) AS breakout_high
          FROM daily_bar d
          JOIN security_master s ON s.security_id = d.security_id
          WHERE d.trade_date <= CAST(? AS DATE)
        ),
        features AS (
          SELECT
            *,
            CASE
              WHEN prev5_avg_volume > 0 THEN volume / prev5_avg_volume
              ELSE NULL
            END AS volume_ratio_prev5,
            CASE
              WHEN amount IS NOT NULL THEN amount / 100000000.0
              ELSE NULL
            END AS amount_yi,
            CASE
              WHEN low_window_high > low_window_low AND low_window_low > 0
              THEN (close_price - low_window_low) / (low_window_high - low_window_low) * 100
              ELSE NULL
            END AS close_position_pct,
            CASE
              WHEN platform_low > 0
              THEN (platform_high - platform_low) / platform_low * 100
              ELSE NULL
            END AS platform_amplitude_pct,
            CASE
              WHEN breakout_high > 0
              THEN (close_price - breakout_high) / breakout_high * 100
              ELSE NULL
            END AS breakout_pct,
            CASE
              WHEN ma10_price > 0 THEN (ma5_price - ma10_price) / ma10_price * 100
              ELSE NULL
            END AS ma5_vs_ma10_pct,
            CASE
              WHEN ma20_price > 0 THEN (ma10_price - ma20_price) / ma20_price * 100
              ELSE NULL
            END AS ma10_vs_ma20_pct
          FROM bars
        ),
        picked AS (
          SELECT *
          FROM features
          WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
            AND close_price > open_price
            AND close_price >= ma5_price
            AND close_price >= ma10_price
            AND close_price >= ma20_price
            AND close_price > breakout_high
            AND breakout_pct BETWEEN ? AND ?
            AND volume_ratio_prev5 >= ?
            AND amount_yi >= ?
            AND turnover_ratio BETWEEN ? AND ?
            AND COALESCE(prev10_change_sum, 0) <= ?
            AND close_position_pct <= ?
            AND platform_amplitude_pct <= ?
        ),
        score_calc AS (
          SELECT
            *,
            (100 - LEAST(GREATEST(COALESCE(close_position_pct, 100), 0), 100)) AS low_position_score,
            (100 - LEAST(GREATEST(COALESCE(platform_amplitude_pct, 100) / ? * 100, 0), 100)) AS platform_score,
            LEAST(GREATEST((COALESCE(volume_ratio_prev5, 0) - 1.0) / 3.0 * 100, 0), 100) AS volume_score,
            LEAST(GREATEST(COALESCE(breakout_pct, 0) / ? * 100, 0), 100) AS breakout_score,
            (
              LEAST(GREATEST(COALESCE(ma5_vs_ma10_pct, 0) / 5.0 * 100, 0), 100) * 0.50
              + LEAST(GREATEST(COALESCE(ma10_vs_ma20_pct, 0) / 5.0 * 100, 0), 100) * 0.50
            ) AS ma_trend_score,
            (100 - LEAST(ABS(COALESCE(turnover_ratio, 0) - 6.0) / 6.0 * 100, 100)) AS turnover_health_score,
            (
              CASE
                WHEN COALESCE(prev10_change_sum, 0) > 18
                THEN LEAST((prev10_change_sum - 18.0) / 15.0 * 15, 15)
                ELSE 0
              END
              + CASE
                WHEN COALESCE(breakout_pct, 0) > 8
                THEN LEAST((breakout_pct - 8.0) / 8.0 * 10, 10)
                ELSE 0
              END
              + CASE
                WHEN COALESCE(turnover_ratio, 0) > 15
                THEN LEAST((turnover_ratio - 15.0) / 15.0 * 10, 10)
                ELSE 0
              END
            ) AS overheating_penalty
          FROM picked
        )
        SELECT
          symbol,
          name,
          trade_date,
          (
            low_position_score * 0.22
            + platform_score * 0.18
            + volume_score * 0.22
            + breakout_score * 0.18
            + ma_trend_score * 0.12
            + turnover_health_score * 0.08
            - overheating_penalty
          ) AS composite_score,
          open_price,
          close_price,
          ma5_price
        FROM score_calc
        ORDER BY trade_date, composite_score DESC, symbol ASC
        """,
        [
            end_date,
            start_date,
            end_date,
            params.min_breakout_pct,
            params.max_breakout_pct,
            params.volume_ratio_min,
            params.amount_min_yi,
            params.turnover_min,
            params.turnover_max,
            params.recent_10d_change_max,
            params.max_close_position_pct,
            params.platform_amplitude_max_pct,
            params.platform_amplitude_max_pct,
            params.max_breakout_pct,
        ],
    ).fetchall()

    grouped: dict[date, list[PatternSelectionCandidate]] = {}
    for row in rows:
        selection_date = row[2]
        grouped.setdefault(selection_date, []).append(
            PatternSelectionCandidate(
                symbol=row[0],
                name=row[1],
                selection_date=selection_date,
                d0_date=selection_date,
                composite_score=float(row[3]) if row[3] is not None else None,
                branch_type="low_position_volume_breakout",
                signal_open=float(row[4]) if row[4] is not None else None,
                signal_close=float(row[5]) if row[5] is not None else None,
                signal_ma5=float(row[6]) if row[6] is not None else None,
            )
        )
    return grouped


def _select_low_position_volume_breakout_for_date(
    connection,
    selection_date: date,
    params: LowPositionVolumeBreakoutParams,
) -> list[PatternSelectionCandidate]:
    return _select_low_position_volume_breakout_in_range(
        connection=connection,
        start_date=selection_date,
        end_date=selection_date,
        params=params,
    ).get(selection_date, [])


def _select_trend_main_wave_relay_in_range(
    connection,
    start_date: date,
    end_date: date,
    params: TrendMainWaveRelayParams,
) -> dict[date, list[PatternSelectionCandidate]]:
    ma10_touch_factor = float(_pct_up_factor(params.ma10_touch_tolerance_pct))
    ma20_support_factor = float(_pct_down_factor(params.ma20_support_tolerance_pct))
    rows = connection.execute(
        f"""
        WITH bars AS (
          SELECT
            s.symbol,
            s.name,
            d.security_id,
            d.trade_date,
            d.open_price,
            d.high_price,
            d.low_price,
            d.close_price,
            d.volume,
            d.amount,
            d.change_pct,
            d.turnover_ratio,
            d.ma5_price,
            d.ma10_price,
            d.ma20_price,
            avg(d.volume) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS prev5_avg_volume,
            avg(d.volume) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
            ) AS prev20_avg_volume,
            sum(d.change_pct) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS prev5_change_sum,
            sum(d.change_pct) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
            ) AS prev20_change_sum,
            min(d.low_price) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 60 PRECEDING AND 1 PRECEDING
            ) AS prev60_low,
            lag(d.ma20_price, {params.trend_slope_lookback}) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
            ) AS ma20_slope_base,
            max(
              CASE
                WHEN d.ma10_price > 0 AND d.low_price <= d.ma10_price * ?
                THEN 1 ELSE 0
              END
            ) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN {params.pullback_window} PRECEDING AND 1 PRECEDING
            ) AS touched_ma10_recently,
            min(
              CASE
                WHEN d.ma20_price > 0 THEN d.low_price / d.ma20_price
                ELSE NULL
              END
            ) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN {params.pullback_window} PRECEDING AND 1 PRECEDING
            ) AS min_low_vs_ma20_recent
          FROM daily_bar d
          JOIN security_master s ON s.security_id = d.security_id
          WHERE d.trade_date <= CAST(? AS DATE)
        ),
        features AS (
          SELECT
            *,
            CASE WHEN prev5_avg_volume > 0 THEN volume / prev5_avg_volume ELSE NULL END AS volume_ratio_prev5,
            CASE WHEN prev20_avg_volume > 0 THEN volume / prev20_avg_volume ELSE NULL END AS volume_ratio_prev20,
            CASE WHEN amount IS NOT NULL THEN amount / 100000000.0 ELSE NULL END AS amount_yi,
            CASE WHEN ma20_slope_base > 0 THEN (ma20_price - ma20_slope_base) / ma20_slope_base * 100 ELSE NULL END AS ma20_slope_pct,
            CASE WHEN prev60_low > 0 THEN (close_price - prev60_low) / prev60_low * 100 ELSE NULL END AS position_from_60d_low_pct,
            CASE WHEN ma10_price > 0 THEN (close_price - ma10_price) / ma10_price * 100 ELSE NULL END AS distance_from_ma10_pct,
            CASE WHEN ma20_price > 0 THEN (close_price - ma20_price) / ma20_price * 100 ELSE NULL END AS close_vs_ma20_pct,
            CASE WHEN ma10_price > 0 THEN (ma5_price - ma10_price) / ma10_price * 100 ELSE NULL END AS ma5_vs_ma10_pct,
            CASE WHEN ma20_price > 0 THEN (ma10_price - ma20_price) / ma20_price * 100 ELSE NULL END AS ma10_vs_ma20_pct,
            CASE WHEN high_price > low_price THEN (close_price - low_price) / (high_price - low_price) * 100 ELSE NULL END AS close_high_position_pct,
            CASE WHEN open_price > 0 THEN (close_price - open_price) / open_price * 100 ELSE NULL END AS body_pct,
            CASE WHEN close_price > 0 THEN (high_price - close_price) / close_price * 100 ELSE NULL END AS upper_shadow_pct
          FROM bars
        ),
        picked AS (
          SELECT *
          FROM features
          WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
            AND ma5_price IS NOT NULL
            AND ma10_price IS NOT NULL
            AND ma20_price IS NOT NULL
            AND ma5_price >= ma10_price
            AND ma10_price >= ma20_price
            AND close_price > ma20_price
            AND COALESCE(ma20_slope_pct, -100) >= ?
            AND touched_ma10_recently = 1
            AND COALESCE(min_low_vs_ma20_recent, 0) >= ?
            AND close_price >= ma5_price
            AND close_price > open_price
            AND change_pct BETWEEN ? AND ?
            AND volume_ratio_prev5 BETWEEN ? AND ?
            AND amount_yi >= ?
            AND turnover_ratio BETWEEN ? AND ?
            AND prev20_change_sum BETWEEN ? AND ?
            AND position_from_60d_low_pct BETWEEN ? AND ?
            AND distance_from_ma10_pct BETWEEN 0 AND ?
            AND COALESCE(upper_shadow_pct, 100) <= ?
        ),
        score_calc AS (
          SELECT
            *,
            (
              LEAST(GREATEST(COALESCE(ma20_slope_pct, 0) / 8.0 * 100, 0), 100) * 0.30
              + LEAST(GREATEST(COALESCE(ma5_vs_ma10_pct, 0) / 6.0 * 100, 0), 100) * 0.25
              + LEAST(GREATEST(COALESCE(ma10_vs_ma20_pct, 0) / 8.0 * 100, 0), 100) * 0.25
              + LEAST(GREATEST(COALESCE(close_vs_ma20_pct, 0) / 25.0 * 100, 0), 100) * 0.20
            ) AS trend_quality,
            (
              LEAST(GREATEST((COALESCE(min_low_vs_ma20_recent, 0) - ?) / (1.08 - ?) * 100, 0), 100) * 0.45
              + (100 - LEAST(GREATEST(COALESCE(distance_from_ma10_pct, 100) / ? * 100, 0), 100)) * 0.40
              + LEAST(GREATEST(COALESCE(prev5_change_sum, 0) / 8.0 * 100, 0), 100) * 0.15
            ) AS pullback_quality,
            (
              LEAST(GREATEST((change_pct - ?) / (? - ?) * 100, 0), 100) * 0.35
              + LEAST(GREATEST(COALESCE(close_high_position_pct, 0), 0), 100) * 0.40
              + LEAST(GREATEST(COALESCE(body_pct, 0) / 6.0 * 100, 0), 100) * 0.25
            ) AS restart_strength,
            (
              (100 - LEAST(ABS(COALESCE(volume_ratio_prev5, 0) - 1.35) / 1.35 * 100, 100)) * 0.65
              + (100 - LEAST(ABS(COALESCE(volume_ratio_prev20, 0) - 1.25) / 1.25 * 100, 100)) * 0.35
            ) AS volume_health,
            (100 - LEAST(ABS(COALESCE(turnover_ratio, 0) - 7.0) / 7.0 * 100, 100)) AS turnover_health,
            (
              CASE
                WHEN COALESCE(prev20_change_sum, 0) > 45
                THEN LEAST((prev20_change_sum - 45.0) / 25.0 * 12, 12)
                ELSE 0
              END
              + CASE
                WHEN COALESCE(distance_from_ma10_pct, 0) > 8
                THEN LEAST((distance_from_ma10_pct - 8.0) / 8.0 * 10, 10)
                ELSE 0
              END
              + CASE
                WHEN COALESCE(turnover_ratio, 0) > 15
                THEN LEAST((turnover_ratio - 15.0) / 15.0 * 10, 10)
                ELSE 0
              END
              + CASE
                WHEN COALESCE(volume_ratio_prev5, 0) > 2.0
                THEN LEAST((volume_ratio_prev5 - 2.0) / 1.5 * 10, 10)
                ELSE 0
              END
              + CASE
                WHEN COALESCE(upper_shadow_pct, 0) > 3
                THEN LEAST((upper_shadow_pct - 3.0) / 6.0 * 8, 8)
                ELSE 0
              END
            ) AS risk_penalty
          FROM picked
        )
        SELECT
          symbol,
          name,
          trade_date,
          (
            trend_quality * 0.30
            + pullback_quality * 0.25
            + restart_strength * 0.25
            + volume_health * 0.15
            + turnover_health * 0.05
            - risk_penalty
          ) AS composite_score,
          open_price,
          close_price,
          ma5_price
        FROM score_calc
        ORDER BY trade_date, composite_score DESC, symbol ASC
        """,
        [
            ma10_touch_factor,
            end_date,
            start_date,
            end_date,
            params.ma20_slope_min_pct,
            ma20_support_factor,
            params.signal_min_change_pct,
            params.signal_max_change_pct,
            params.signal_volume_ratio_min,
            params.signal_volume_ratio_max,
            params.amount_min_yi,
            params.turnover_min,
            params.turnover_max,
            params.recent_20d_change_min,
            params.recent_20d_change_max,
            params.position_from_60d_low_min_pct,
            params.position_from_60d_low_max_pct,
            params.max_distance_from_ma10_pct,
            params.max_upper_shadow_pct,
            ma20_support_factor,
            ma20_support_factor,
            params.max_distance_from_ma10_pct,
            params.signal_min_change_pct,
            params.signal_max_change_pct,
            params.signal_min_change_pct,
        ],
    ).fetchall()

    grouped: dict[date, list[PatternSelectionCandidate]] = {}
    for row in rows:
        selection_date = row[2]
        grouped.setdefault(selection_date, []).append(
            PatternSelectionCandidate(
                symbol=row[0],
                name=row[1],
                selection_date=selection_date,
                d0_date=selection_date,
                composite_score=float(row[3]) if row[3] is not None else None,
                branch_type="trend_main_wave_relay",
                signal_open=float(row[4]) if row[4] is not None else None,
                signal_close=float(row[5]) if row[5] is not None else None,
                signal_ma5=float(row[6]) if row[6] is not None else None,
            )
        )
    return grouped


def _select_trend_main_wave_relay_for_date(
    connection,
    selection_date: date,
    params: TrendMainWaveRelayParams,
) -> list[PatternSelectionCandidate]:
    return _select_trend_main_wave_relay_in_range(
        connection=connection,
        start_date=selection_date,
        end_date=selection_date,
        params=params,
    ).get(selection_date, [])


def _select_main_force_accumulation_in_range(
    connection,
    start_date: date,
    end_date: date,
    params: MainForceAccumulationParams,
) -> dict[date, list[PatternSelectionCandidate]]:
    rows = connection.execute(
        f"""
        WITH bars AS (
          SELECT
            s.symbol,
            s.name,
            d.security_id,
            d.trade_date,
            d.open_price,
            d.high_price,
            d.low_price,
            d.close_price,
            d.volume,
            d.amount,
            d.change_pct,
            avg(d.volume) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN {params.consolidation_window} PRECEDING AND 1 PRECEDING
            ) AS short_avg_volume,
            avg(d.volume) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN {params.low_position_window} PRECEDING AND 1 PRECEDING
            ) AS long_avg_volume,
            max(d.high_price) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN {params.consolidation_window} PRECEDING AND 1 PRECEDING
            ) AS platform_high,
            min(d.low_price) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN {params.low_position_window} PRECEDING AND 1 PRECEDING
            ) AS low_window_low,
            max(d.high_price) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN {params.low_position_window} PRECEDING AND 1 PRECEDING
            ) AS low_window_high
          FROM daily_bar d
          JOIN security_master s ON s.security_id = d.security_id
          WHERE d.trade_date <= CAST(? AS DATE)
        ),
        features AS (
          SELECT
            *,
            CASE
              WHEN short_avg_volume > 0 THEN volume / short_avg_volume
              ELSE NULL
            END AS signal_volume_ratio,
            CASE
              WHEN long_avg_volume > 0 THEN short_avg_volume / long_avg_volume
              ELSE NULL
            END AS volume_shrink_ratio,
            CASE
              WHEN amount IS NOT NULL THEN amount / 100000000.0
              ELSE NULL
            END AS amount_yi,
            CASE
              WHEN low_window_high > low_window_low AND low_window_low > 0
              THEN (close_price - low_window_low) / (low_window_high - low_window_low) * 100
              ELSE NULL
            END AS close_position_pct
          FROM bars
        ),
        picked AS (
          SELECT *
          FROM features
          WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
            AND close_price > open_price
            AND change_pct BETWEEN ? AND ?
            AND close_position_pct <= ?
            AND volume_shrink_ratio <= ?
            AND signal_volume_ratio BETWEEN ? AND ?
            AND amount_yi >= ?
            {"AND close_price > platform_high" if params.require_platform_breakout else ""}
        )
        SELECT
          symbol,
          name,
          trade_date,
          (
            (100 - LEAST(GREATEST(COALESCE(close_position_pct, 100), 0), 100)) * 0.40
            + LEAST(GREATEST((1 - COALESCE(volume_shrink_ratio, 1)) * 100, 0), 100) * 0.30
            + LEAST(GREATEST((COALESCE(signal_volume_ratio, 1) - 1.0) / 3.0 * 100, 0), 100) * 0.30
          ) AS composite_score,
          open_price,
          close_price,
          close_price AS signal_ma5
        FROM picked
        ORDER BY trade_date, composite_score DESC, symbol ASC
        """,
        [
            end_date,
            start_date,
            end_date,
            params.min_change_pct,
            params.max_change_pct,
            params.max_close_position_pct,
            params.volume_shrink_ratio_max,
            params.signal_volume_ratio_min,
            params.signal_volume_ratio_max,
            params.amount_min_yi,
        ],
    ).fetchall()

    grouped: dict[date, list[PatternSelectionCandidate]] = {}
    for row in rows:
        selection_date = row[2]
        grouped.setdefault(selection_date, []).append(
            PatternSelectionCandidate(
                symbol=row[0],
                name=row[1],
                selection_date=selection_date,
                d0_date=selection_date,
                composite_score=float(row[3]) if row[3] is not None else None,
                branch_type="main_force_accumulation",
                signal_open=float(row[4]) if row[4] is not None else None,
                signal_close=float(row[5]) if row[5] is not None else None,
                signal_ma5=float(row[6]) if row[6] is not None else None,
            )
        )
    return grouped


def _select_main_force_accumulation_for_date(
    connection,
    selection_date: date,
    params: MainForceAccumulationParams,
) -> list[PatternSelectionCandidate]:
    return _select_main_force_accumulation_in_range(
        connection=connection,
        start_date=selection_date,
        end_date=selection_date,
        params=params,
    ).get(selection_date, [])


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
    elif selection_config.pattern_id == "all_time_high_v1":
        candidates = _select_all_time_high_for_date(connection, selection_date, selection_config.params)
    elif selection_config.pattern_id == "limit_up_pullback_ma5_reclaim_v1":
        candidates = _select_limit_up_pullback_ma5_reclaim_for_date(
            connection=connection,
            selection_date=selection_date,
            params=selection_config.params,
        )
    elif selection_config.pattern_id == "limit_up_first_pullback_ma5_reclaim_v1":
        candidates = _select_limit_up_first_pullback_ma5_reclaim_for_date(
            connection=connection,
            selection_date=selection_date,
            params=selection_config.params,
        )
    elif selection_config.pattern_id == "first_limit_up_trend_continuation_v1":
        candidates = _select_first_limit_up_trend_continuation_for_date(
            connection=connection,
            selection_date=selection_date,
            params=selection_config.params,
        )
    elif selection_config.pattern_id == "low_position_volume_breakout_v1":
        candidates = _select_low_position_volume_breakout_for_date(
            connection=connection,
            selection_date=selection_date,
            params=selection_config.params,
        )
    elif selection_config.pattern_id == "trend_main_wave_relay_v1":
        candidates = _select_trend_main_wave_relay_for_date(
            connection=connection,
            selection_date=selection_date,
            params=selection_config.params,
        )
    elif selection_config.pattern_id == "main_force_accumulation_v1":
        candidates = _select_main_force_accumulation_for_date(
            connection=connection,
            selection_date=selection_date,
            params=selection_config.params,
        )
    else:
        raise PatternSelectionError(
            f"Unsupported pattern_id: {selection_config.pattern_id}."
        )
    if selection_config.sort_by == "pool_score_v2" and candidates:
        from quant.evaluation.pool_features import extract_daily_bar_features
        from quant.evaluation.pool_scoring import compute_pool_score_v2
        from dataclasses import replace as dc_replace

        extra_feats = extract_daily_bar_features(connection, candidates)
        v2_scores = compute_pool_score_v2(candidates, extra_feats)
        candidates = [
            dc_replace(c, pool_score_v2=v2_scores[i])
            for i, c in enumerate(candidates)
        ]

    if selection_config.sort_by == "pool_score_v3" and candidates:
        from quant.evaluation.pool_features import extract_daily_bar_features
        from quant.evaluation.pool_score_v3 import compute_pool_score_v3_hybrid
        from dataclasses import replace as dc_replace

        extra_feats = extract_daily_bar_features(connection, candidates)
        v3_scores = compute_pool_score_v3_hybrid(candidates, extra_feats)
        candidates = [
            dc_replace(c, pool_score_v3=v3_scores[i])
            for i, c in enumerate(candidates)
        ]

    if selection_config.sort_by == "pool_score_ic" and candidates:
        from quant.evaluation.pool_features import extract_all_ic_features
        from quant.evaluation.pool_score_ic import compute_pool_score_ic
        from dataclasses import replace as dc_replace

        extra_feats, pre_feats = extract_all_ic_features(connection, candidates)
        ic_scores = compute_pool_score_ic(candidates, extra_feats, pre_feats)
        candidates = [
            dc_replace(c, pool_score_ic=ic_scores[i])
            for i, c in enumerate(candidates)
        ]

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
    if selection_config.pattern_id == "first_limit_up_trend_continuation_v1":
        grouped = _select_first_limit_up_trend_continuation_in_range(
            connection=connection,
            start_date=start_date,
            end_date=end_date,
            params=selection_config.params,
        )
        return {
            trade_date: _apply_filter_rules(
                connection,
                _sort_candidates(
                    grouped.get(trade_date, []),
                    sort_by=selection_config.sort_by,
                    sort_order=selection_config.sort_order,
                ),
                selection_config,
            )
            for trade_date in trade_dates
        }
    if selection_config.pattern_id == "low_position_volume_breakout_v1":
        grouped = _select_low_position_volume_breakout_in_range(
            connection=connection,
            start_date=start_date,
            end_date=end_date,
            params=selection_config.params,
        )
        return {
            trade_date: _apply_filter_rules(
                connection,
                _sort_candidates(
                    grouped.get(trade_date, []),
                    sort_by=selection_config.sort_by,
                    sort_order=selection_config.sort_order,
                ),
                selection_config,
            )
            for trade_date in trade_dates
        }
    if selection_config.pattern_id == "trend_main_wave_relay_v1":
        grouped = _select_trend_main_wave_relay_in_range(
            connection=connection,
            start_date=start_date,
            end_date=end_date,
            params=selection_config.params,
        )
        return {
            trade_date: _apply_filter_rules(
                connection,
                _sort_candidates(
                    grouped.get(trade_date, []),
                    sort_by=selection_config.sort_by,
                    sort_order=selection_config.sort_order,
                ),
                selection_config,
            )
            for trade_date in trade_dates
        }
    if selection_config.pattern_id == "main_force_accumulation_v1":
        grouped = _select_main_force_accumulation_in_range(
            connection=connection,
            start_date=start_date,
            end_date=end_date,
            params=selection_config.params,
        )
        return {
            trade_date: _apply_filter_rules(
                connection,
                _sort_candidates(
                    grouped.get(trade_date, []),
                    sort_by=selection_config.sort_by,
                    sort_order=selection_config.sort_order,
                ),
                selection_config,
            )
            for trade_date in trade_dates
        }
    return {
        trade_date: select_pattern_candidates_with_connection(
            connection,
            trade_date,
            selection_config,
        )
        for trade_date in trade_dates
    }
