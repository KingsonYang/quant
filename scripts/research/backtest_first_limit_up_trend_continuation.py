from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pandas as pd

from quant.backtest_common import connect, load_all_trade_dates, load_trade_dates_in_range
from quant.config.loader import load_strategy_config


DEFAULT_STRATEGY = "first_limit_up_trend_continuation"
DEFAULT_START = "2022-12-29"
DEFAULT_END = "2026-05-06"


def _date(value: str) -> date:
    return date.fromisoformat(value)


def _ints(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def _floats(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def _apply_score_gate(candidates: list[dict], *, min_score: float, min_gap: float) -> list[dict]:
    if not candidates:
        return candidates
    first_score = candidates[0]["composite_score"]
    if first_score is None or first_score < min_score:
        return []
    if min_gap > 0 and len(candidates) >= 2:
        second_score = candidates[1]["composite_score"]
        if second_score is None or first_score - second_score < min_gap:
            return []
    return candidates


def _load_candidates_bulk(connection, *, start_date: date, end_date: date, params) -> dict[date, list[dict]]:
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
          trade_date,
          symbol,
          name,
          (
            trend_quality * 0.30
            + volume_health * 0.25
            + breakout_quality * 0.20
            + preheat_quality * 0.15
            + turnover_activity * 0.10
            - overheating_penalty
          ) AS composite_score
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
    grouped: dict[date, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(
            {
                "selection_date": row[0],
                "symbol": row[1],
                "name": row[2],
                "composite_score": float(row[3]) if row[3] is not None else None,
            }
        )
    return grouped


def _load_price_rows(connection, *, symbols: set[str], start_trade_date: date, end_trade_date: date) -> dict:
    if not symbols:
        return {}
    symbol_list = sorted(symbols)
    placeholders = ", ".join("?" for _ in symbol_list)
    rows = connection.execute(
        f"""
        SELECT
          s.symbol,
          d.trade_date,
          d.open_price,
          d.high_price,
          d.close_price
        FROM daily_bar d
        JOIN security_master s ON s.security_id = d.security_id
        WHERE s.symbol IN ({placeholders})
          AND d.trade_date BETWEEN ? AND ?
        """,
        [*symbol_list, start_trade_date, end_trade_date],
    ).fetchall()
    return {
        (row[0], row[1]): {
            "open": float(row[2]),
            "high": float(row[3]),
            "close": float(row[4]),
        }
        for row in rows
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", type=Path, default=Path("data/quant.duckdb"))
    parser.add_argument("--strategies-dir", type=Path, default=Path("strategies"))
    parser.add_argument("--strategy", default=DEFAULT_STRATEGY)
    parser.add_argument("--start-date", type=_date, default=_date(DEFAULT_START))
    parser.add_argument("--end-date", type=_date, default=_date(DEFAULT_END))
    parser.add_argument("--holding-periods", type=_ints, default=_ints("1,2,3,4,5"))
    parser.add_argument("--top-ns", type=_ints, default=_ints("1,2,3,5,10"))
    parser.add_argument("--min-scores", type=_floats, default=_floats("0"))
    parser.add_argument("--score-gaps", type=_floats, default=_floats("0"))
    parser.add_argument("--write-details", action="store_true")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/research/first_limit_up_trend_continuation_backtest"),
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    loaded = load_strategy_config(args.strategy, args.strategies_dir)
    max_holding_period = max(args.holding_periods)
    max_top_n = max(args.top_ns)
    rows = []
    detail_rows = []

    with connect(args.db_path) as connection:
        trade_dates = load_trade_dates_in_range(connection, args.start_date, args.end_date)
        all_trade_dates = load_all_trade_dates(connection)
        trade_date_index = {trade_day: index for index, trade_day in enumerate(all_trade_dates)}
        selection_map = _load_candidates_bulk(
            connection,
            start_date=args.start_date,
            end_date=args.end_date,
            params=loaded.config.selection.params,
        )
        relevant_symbols = {
            candidate["symbol"]
            for candidates in selection_map.values()
            for candidate in candidates[:max_top_n]
        }
        final_sell_index = max(
            (
                min(trade_date_index[trade_day] + 1 + max_holding_period, len(all_trade_dates) - 1)
                for trade_day, candidates in selection_map.items()
                if candidates
            ),
            default=trade_date_index[args.end_date],
        )
        price_cache = _load_price_rows(
            connection=connection,
            symbols=relevant_symbols,
            start_trade_date=trade_dates[0],
            end_trade_date=all_trade_dates[final_sell_index],
        )

    for min_score in args.min_scores:
        for score_gap in args.score_gaps:
            for holding_period in args.holding_periods:
                for top_n in args.top_ns:
                    gated_selection_map = {
                        selection_date: _apply_score_gate(
                            candidates,
                            min_score=min_score,
                            min_gap=score_gap,
                        )
                        for selection_date, candidates in selection_map.items()
                    }
                    rows, detail_rows = _run_combo(
                        rows=rows,
                        detail_rows=detail_rows,
                        write_details=args.write_details,
                        min_score=min_score,
                        score_gap=score_gap,
                        holding_period=holding_period,
                        top_n=top_n,
                        trade_dates=trade_dates,
                        all_trade_dates=all_trade_dates,
                        trade_date_index=trade_date_index,
                        selection_map=gated_selection_map,
                        price_cache=price_cache,
                    )

    summary_df = pd.DataFrame(rows).sort_values(
        ["high_ge_5_rate_pct", "win_rate_pct", "average_return_pct", "completed_count"],
        ascending=[False, False, False, False],
    )
    summary_df.to_csv(args.output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    if args.write_details:
        pd.DataFrame(detail_rows).to_csv(args.output_dir / "details.csv", index=False, encoding="utf-8-sig")

    print("\n## Ranked Summary")
    print(summary_df.head(80).to_string(index=False, float_format=lambda value: f"{value:.4f}"))
    print(f"\nWrote CSV files to {args.output_dir}")


def _run_combo(
    *,
    rows: list[dict],
    detail_rows: list[dict],
    write_details: bool,
    min_score: float,
    score_gap: float,
    holding_period: int,
    top_n: int,
    trade_dates: list[date],
    all_trade_dates: list[date],
    trade_date_index: dict[date, int],
    selection_map: dict[date, list[dict]],
    price_cache: dict,
) -> tuple[list[dict], list[dict]]:
            returns = []
            max_high_returns = []
            hit_count = 0
            incomplete_count = 0
            for selection_date in trade_dates:
                selection_index = trade_date_index[selection_date]
                buy_index = selection_index + 1
                sell_index = buy_index + holding_period
                buy_date = all_trade_dates[buy_index] if buy_index < len(all_trade_dates) else None
                sell_date = all_trade_dates[sell_index] if sell_index < len(all_trade_dates) else None
                for candidate in selection_map.get(selection_date, [])[:top_n]:
                    hit_count += 1
                    incomplete_reason = ""
                    return_pct = None
                    max_high_return_pct = None
                    buy_price = None
                    sell_price = None
                    if buy_date is None:
                        incomplete_reason = "missing_next_trade_date"
                    elif sell_date is None:
                        incomplete_reason = "insufficient_holding_window"
                    else:
                        buy_bar = price_cache.get((candidate["symbol"], buy_date))
                        sell_bar = price_cache.get((candidate["symbol"], sell_date))
                        if buy_bar is None:
                            incomplete_reason = "missing_buy_bar"
                        elif sell_bar is None:
                            incomplete_reason = "missing_sell_bar"
                        else:
                            buy_price = buy_bar["open"]
                            sell_price = sell_bar["close"]
                            return_pct = float((sell_price - buy_price) * 100 / buy_price)
                            returns.append(return_pct)
                            highs = []
                            for index in range(buy_index, sell_index + 1):
                                bar = price_cache.get((candidate["symbol"], all_trade_dates[index]))
                                if bar is not None:
                                    highs.append(bar["high"])
                            if highs:
                                max_high_return_pct = (max(highs) - buy_price) * 100 / buy_price
                                max_high_returns.append(max_high_return_pct)
                    if return_pct is None:
                        incomplete_count += 1
                    if write_details:
                        detail_rows.append(
                            {
                                "min_score": min_score,
                                "score_gap": score_gap,
                                "holding_period": holding_period,
                                "selection_top_n": top_n,
                                "symbol": candidate["symbol"],
                                "name": candidate["name"],
                                "composite_score": candidate["composite_score"],
                                "selection_date": selection_date,
                                "buy_date": buy_date,
                                "sell_date": sell_date,
                                "buy_price": buy_price,
                                "sell_price": sell_price,
                                "return_pct": return_pct,
                                "max_high_return_pct": max_high_return_pct,
                                "status": "completed" if return_pct is not None else "incomplete",
                                "incomplete_reason": incomplete_reason,
                            }
                        )
            completed_count = len(returns)
            positive_count = sum(1 for value in returns if value > 0)
            negative_count = sum(1 for value in returns if value < 0)
            flat_count = completed_count - positive_count - negative_count
            win_rate_pct = positive_count * 100 / completed_count if completed_count else 0.0
            average_return_pct = sum(returns) / completed_count if completed_count else 0.0
            high_ge_3_rate = (
                sum(1 for value in max_high_returns if value >= 3) * 100 / len(max_high_returns)
                if max_high_returns
                else 0.0
            )
            high_ge_5_rate = (
                sum(1 for value in max_high_returns if value >= 5) * 100 / len(max_high_returns)
                if max_high_returns
                else 0.0
            )
            high_ge_10_rate = (
                sum(1 for value in max_high_returns if value >= 10) * 100 / len(max_high_returns)
                if max_high_returns
                else 0.0
            )
            average_max_high_return_pct = (
                sum(max_high_returns) / len(max_high_returns)
                if max_high_returns
                else 0.0
            )
            rows.append(
                {
                    "min_score": min_score,
                    "score_gap": score_gap,
                    "holding_period": holding_period,
                    "selection_top_n": top_n,
                    "hit_count": hit_count,
                    "completed_count": completed_count,
                    "incomplete_count": incomplete_count,
                    "positive_count": positive_count,
                    "negative_count": negative_count,
                    "flat_count": flat_count,
                    "win_rate_pct": win_rate_pct,
                    "average_return_pct": average_return_pct,
                    "high_ge_3_rate_pct": high_ge_3_rate,
                    "high_ge_5_rate_pct": high_ge_5_rate,
                    "high_ge_10_rate_pct": high_ge_10_rate,
                    "average_max_high_return_pct": average_max_high_return_pct,
                }
            )
            print(
                f"min_score={min_score:g} gap={score_gap:g} "
                f"hp={holding_period} top_n={top_n} "
                f"completed={completed_count} "
                f"win_rate={win_rate_pct:.2f}% "
                f"avg_return={average_return_pct:.4f}% "
                f"high_ge_5={high_ge_5_rate:.2f}%"
            )
            return rows, detail_rows


if __name__ == "__main__":
    main()
