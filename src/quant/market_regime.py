from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from quant.config.models import MarketRegimeFilterConfig


@dataclass(frozen=True)
class MarketRegimeState:
    trade_date: date
    total_count: int
    up_ratio: Decimal
    above_ma5_ratio: Decimal
    first_limit_count: int
    first_limit_count_avg: Decimal
    down_gt5_count: int
    limit_down_count: int
    passed: bool


def load_market_regime_states(
    connection,
    *,
    start_date: date,
    end_date: date,
    config: MarketRegimeFilterConfig,
) -> dict[date, MarketRegimeState]:
    lookback_preceding = max(config.lookback_days - 1, 0)
    rows = connection.execute(
        f"""
        WITH base AS (
          SELECT
            d.security_id,
            d.trade_date,
            d.change_pct,
            d.close_price,
            d.high_price,
            d.low_price,
            d.ma5_price,
            CASE
              WHEN d.change_pct >= ?
               AND d.high_price > 0
               AND d.close_price >= d.high_price * ?
               AND d.close_price > d.open_price
              THEN 1 ELSE 0
            END AS is_limit_up
          FROM daily_bar d
          WHERE d.trade_date <= CAST(? AS DATE)
        ),
        marked AS (
          SELECT
            *,
            lag(is_limit_up, 1) OVER (
              PARTITION BY security_id
              ORDER BY trade_date
            ) AS prev_is_limit_up
          FROM base
        ),
        daily_stats AS (
          SELECT
            trade_date,
            count(*) AS total_count,
            sum(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) AS up_count,
            sum(
              CASE
                WHEN ma5_price IS NOT NULL AND ma5_price > 0 AND close_price >= ma5_price
                THEN 1 ELSE 0
              END
            ) AS above_ma5_count,
            sum(CASE WHEN change_pct <= -5 THEN 1 ELSE 0 END) AS down_gt5_count,
            sum(
              CASE
                WHEN change_pct <= -9.5
                 AND low_price > 0
                 AND close_price <= low_price * 1.005
                THEN 1 ELSE 0
              END
            ) AS limit_down_count,
            sum(is_limit_up) AS limit_up_count,
            sum(
              CASE
                WHEN is_limit_up = 1 AND COALESCE(prev_is_limit_up, 0) = 0
                THEN 1 ELSE 0
              END
            ) AS first_limit_count
          FROM marked
          GROUP BY trade_date
        ),
        rolling AS (
          SELECT
            *,
            avg(first_limit_count) OVER (
              ORDER BY trade_date
              ROWS BETWEEN {lookback_preceding} PRECEDING AND CURRENT ROW
            ) AS first_limit_count_avg
          FROM daily_stats
        )
        SELECT
          trade_date,
          total_count,
          CASE WHEN total_count > 0 THEN up_count * 100.0 / total_count ELSE 0 END AS up_ratio,
          CASE WHEN total_count > 0 THEN above_ma5_count * 100.0 / total_count ELSE 0 END AS above_ma5_ratio,
          first_limit_count,
          first_limit_count_avg,
          down_gt5_count,
          limit_down_count
        FROM rolling
        WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
        ORDER BY trade_date
        """,
        [
            config.limit_up_min_pct,
            config.limit_up_close_high_min_ratio,
            end_date,
            start_date,
            end_date,
        ],
    ).fetchall()

    states: dict[date, MarketRegimeState] = {}
    for row in rows:
        trade_date = row[0]
        total_count = int(row[1] or 0)
        up_ratio = Decimal(str(row[2] or 0))
        above_ma5_ratio = Decimal(str(row[3] or 0))
        first_limit_count = int(row[4] or 0)
        first_limit_count_avg = Decimal(str(row[5] or 0))
        down_gt5_count = int(row[6] or 0)
        limit_down_count = int(row[7] or 0)
        passed = _passes_filter(
            config=config,
            up_ratio=up_ratio,
            above_ma5_ratio=above_ma5_ratio,
            first_limit_count_avg=first_limit_count_avg,
            down_gt5_count=down_gt5_count,
            limit_down_count=limit_down_count,
        )
        states[trade_date] = MarketRegimeState(
            trade_date=trade_date,
            total_count=total_count,
            up_ratio=up_ratio,
            above_ma5_ratio=above_ma5_ratio,
            first_limit_count=first_limit_count,
            first_limit_count_avg=first_limit_count_avg,
            down_gt5_count=down_gt5_count,
            limit_down_count=limit_down_count,
            passed=passed,
        )
    return states


def _passes_filter(
    *,
    config: MarketRegimeFilterConfig,
    up_ratio: Decimal,
    above_ma5_ratio: Decimal,
    first_limit_count_avg: Decimal,
    down_gt5_count: int,
    limit_down_count: int,
) -> bool:
    if config.min_first_limit_count_avg is not None:
        if first_limit_count_avg < config.min_first_limit_count_avg:
            return False
    if config.min_up_ratio is not None:
        if up_ratio < config.min_up_ratio:
            return False
    if config.min_above_ma5_ratio is not None:
        if above_ma5_ratio < config.min_above_ma5_ratio:
            return False
    if config.max_down_gt5_count is not None:
        if down_gt5_count > config.max_down_gt5_count:
            return False
    if config.max_limit_down_count is not None:
        if limit_down_count > config.max_limit_down_count:
            return False
    return True
