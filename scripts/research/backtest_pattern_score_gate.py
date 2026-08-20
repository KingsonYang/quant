from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from quant.backtest_common import load_all_trade_dates, load_trade_dates_in_range
from quant.config.loader import load_strategy_config
from quant.pattern_selection import select_pattern_candidates_in_range_with_connection


DEFAULT_STRATEGY = "limit_up_pullback_ma5_reclaim"
DEFAULT_START = "2022-12-29"
DEFAULT_END = "2026-05-06"


def _date(value: str) -> date:
    return date.fromisoformat(value)


def _ints(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def _floats(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def _apply_score_gate(candidates: list[Any], *, min_score: float, min_gap: float) -> list[Any]:
    if not candidates:
        return candidates
    first_score = candidates[0].composite_score
    if first_score is None or first_score < min_score:
        return []
    if min_gap > 0 and len(candidates) >= 2:
        second_score = candidates[1].composite_score
        if second_score is None or first_score - second_score < min_gap:
            return []
    return candidates


def _load_price_rows(
    connection: duckdb.DuckDBPyConnection,
    *,
    symbols: set[str],
    start_trade_date: date,
    end_trade_date: date,
) -> dict[tuple[str, date], dict[str, float]]:
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


def _run_combo(
    *,
    rows: list[dict[str, Any]],
    detail_rows: list[dict[str, Any]],
    write_details: bool,
    min_score: float,
    score_gap: float,
    holding_period: int,
    top_n: int,
    trade_dates: list[date],
    all_trade_dates: list[date],
    trade_date_index: dict[date, int],
    selection_map: dict[date, list[Any]],
    price_cache: dict[tuple[str, date], dict[str, float]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    returns: list[float] = []
    max_high_returns: list[float] = []
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
            return_pct = None
            max_high_return_pct = None
            incomplete_reason = ""
            buy_price = None
            sell_price = None
            if buy_date is None:
                incomplete_reason = "missing_next_trade_date"
            elif sell_date is None:
                incomplete_reason = "insufficient_holding_window"
            else:
                buy_bar = price_cache.get((candidate.symbol, buy_date))
                sell_bar = price_cache.get((candidate.symbol, sell_date))
                if buy_bar is None:
                    incomplete_reason = "missing_buy_bar"
                elif sell_bar is None:
                    incomplete_reason = "missing_sell_bar"
                else:
                    buy_price = buy_bar["open"]
                    sell_price = sell_bar["close"]
                    return_pct = (sell_price - buy_price) * 100 / buy_price
                    returns.append(return_pct)
                    highs = []
                    for index in range(buy_index, sell_index + 1):
                        bar = price_cache.get((candidate.symbol, all_trade_dates[index]))
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
                        "symbol": candidate.symbol,
                        "name": candidate.name,
                        "selection_date": selection_date,
                        "d0_date": candidate.d0_date,
                        "composite_score": candidate.composite_score,
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
        f"min_score={min_score:g} gap={score_gap:g} hp={holding_period} top_n={top_n} "
        f"completed={completed_count} win_rate={win_rate_pct:.2f}% "
        f"avg_return={average_return_pct:.4f}% high_ge_5={high_ge_5_rate:.2f}%"
    )
    return rows, detail_rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", type=Path, default=Path("data/quant.duckdb"))
    parser.add_argument("--strategies-dir", type=Path, default=Path("strategies"))
    parser.add_argument("--strategy", default=DEFAULT_STRATEGY)
    parser.add_argument("--start-date", type=_date, default=_date(DEFAULT_START))
    parser.add_argument("--end-date", type=_date, default=_date(DEFAULT_END))
    parser.add_argument("--holding-periods", type=_ints, default=_ints("1,2,3,4,5"))
    parser.add_argument("--top-ns", type=_ints, default=_ints("1"))
    parser.add_argument("--min-scores", type=_floats, default=_floats("0"))
    parser.add_argument("--score-gaps", type=_floats, default=_floats("0"))
    parser.add_argument("--write-details", action="store_true")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/research/pattern_score_gate_backtest"),
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    loaded = load_strategy_config(args.strategy, args.strategies_dir)
    max_holding_period = max(args.holding_periods)
    max_top_n = max(args.top_ns)
    rows: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []

    with duckdb.connect(str(args.db_path), read_only=True) as connection:
        trade_dates = load_trade_dates_in_range(connection, args.start_date, args.end_date)
        all_trade_dates = load_all_trade_dates(connection)
        trade_date_index = {trade_day: index for index, trade_day in enumerate(all_trade_dates)}
        raw_selection_map = select_pattern_candidates_in_range_with_connection(
            connection,
            args.start_date,
            args.end_date,
            loaded.config.selection,
        )
        relevant_symbols = {
            candidate.symbol
            for candidates in raw_selection_map.values()
            for candidate in candidates[:max_top_n]
        }
        final_sell_index = max(
            (
                min(trade_date_index[trade_day] + 1 + max_holding_period, len(all_trade_dates) - 1)
                for trade_day, candidates in raw_selection_map.items()
                if candidates
            ),
            default=trade_date_index[args.end_date],
        )
        price_cache = _load_price_rows(
            connection,
            symbols=relevant_symbols,
            start_trade_date=trade_dates[0],
            end_trade_date=all_trade_dates[final_sell_index],
        )

    for min_score in args.min_scores:
        for score_gap in args.score_gaps:
            gated_selection_map = {
                selection_date: _apply_score_gate(
                    candidates,
                    min_score=min_score,
                    min_gap=score_gap,
                )
                for selection_date, candidates in raw_selection_map.items()
            }
            for holding_period in args.holding_periods:
                for top_n in args.top_ns:
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
        ["average_return_pct", "win_rate_pct", "high_ge_5_rate_pct", "completed_count"],
        ascending=[False, False, False, False],
    )
    summary_df.to_csv(args.output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    if args.write_details:
        pd.DataFrame(detail_rows).to_csv(args.output_dir / "details.csv", index=False, encoding="utf-8-sig")

    print("\n## Ranked Summary")
    print(summary_df.head(80).to_string(index=False, float_format=lambda value: f"{value:.4f}"))
    print(f"\nWrote CSV files to {args.output_dir}")


if __name__ == "__main__":
    main()
