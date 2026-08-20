from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from quant.backtest_common import load_all_trade_dates, load_price_cache, load_trade_dates_in_range
from quant.config.loader import load_strategy_config
from quant.pattern_selection import select_pattern_candidates_in_range_with_connection


MONEY_QUANTIZE = Decimal("0.01")
PCT_QUANTIZE = Decimal("0.0001")


@dataclass
class OpenPosition:
    symbol: str
    name: str
    selection_date: date
    buy_date: date
    buy_trade_index: int
    buy_price: Decimal
    shares: int
    buy_amount: Decimal


def _date(value: str) -> date:
    return date.fromisoformat(value)


def _ints(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def _decimals(value: str) -> list[Decimal | None]:
    result: list[Decimal | None] = []
    for item in value.split(","):
        stripped = item.strip()
        if not stripped:
            continue
        result.append(None if stripped.lower() in {"none", "null"} else Decimal(stripped))
    return result


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTIZE, rounding=ROUND_HALF_UP)


def _pct(value: Decimal) -> Decimal:
    return value.quantize(PCT_QUANTIZE, rounding=ROUND_HALF_UP)


def _position_market_value(
    position: OpenPosition,
    trade_date: date,
    price_cache: dict[tuple[str, date], tuple[Decimal | None, Decimal | None]],
) -> Decimal:
    _, close_price = price_cache.get((position.symbol, trade_date), (None, None))
    if close_price is None:
        return position.buy_amount
    return close_price * Decimal(position.shares)


def _apply_score_gate(candidates: list[Any], min_score: Decimal | None, score_gap: Decimal | None) -> list[Any]:
    if not candidates:
        return candidates
    first_score = candidates[0].composite_score
    if min_score is not None:
        if first_score is None or Decimal(str(first_score)) < min_score:
            return []
    if score_gap is not None and score_gap > 0 and len(candidates) >= 2:
        second_score = candidates[1].composite_score
        if first_score is None or second_score is None:
            return []
        if Decimal(str(first_score)) - Decimal(str(second_score)) < score_gap:
            return []
    return candidates


def _run_backtest(
    *,
    raw_selection_map: dict[date, list[Any]],
    trade_dates: list[date],
    all_trade_dates: list[date],
    trade_date_index: dict[date, int],
    price_cache: dict[tuple[str, date], tuple[Decimal | None, Decimal | None]],
    holding_period: int,
    max_positions: int,
    max_daily_buys: int,
    initial_capital: Decimal,
    position_size_pct: Decimal,
    take_profit_pct: Decimal,
    stop_loss_pct: Decimal,
    selection_top_n: int,
    min_score: Decimal | None,
    score_gap: Decimal | None,
) -> dict[str, Any]:
    selection_map = {
        selection_date: _apply_score_gate(candidates, min_score, score_gap)[:selection_top_n]
        for selection_date, candidates in raw_selection_map.items()
    }
    cash = initial_capital
    open_positions: list[OpenPosition] = []
    closed_returns: list[Decimal] = []
    realized_profit = Decimal("0")
    exit_take_profit = 0
    exit_stop_loss = 0
    exit_holding_period = 0
    buy_count = 0

    for current_trade_day, current_date in enumerate(trade_dates, start=1):
        current_trade_index = trade_date_index[current_date]
        remaining_positions: list[OpenPosition] = []
        for position in open_positions:
            if position.buy_trade_index >= current_trade_index:
                remaining_positions.append(position)
                continue
            _, close_price = price_cache.get((position.symbol, current_date), (None, None))
            if close_price is None:
                remaining_positions.append(position)
                continue

            holding_days = current_trade_index - position.buy_trade_index
            sell_amount = close_price * Decimal(position.shares)
            return_pct = _pct((sell_amount - position.buy_amount) * Decimal("100") / position.buy_amount)
            exit_reason = None
            if return_pct >= take_profit_pct:
                exit_reason = "take_profit"
            elif return_pct <= stop_loss_pct:
                exit_reason = "stop_loss"
            elif holding_days >= holding_period:
                exit_reason = "holding_period"

            if exit_reason is None:
                remaining_positions.append(position)
                continue

            cash += sell_amount
            realized_profit += sell_amount - position.buy_amount
            closed_returns.append(return_pct)
            if exit_reason == "take_profit":
                exit_take_profit += 1
            elif exit_reason == "stop_loss":
                exit_stop_loss += 1
            else:
                exit_holding_period += 1
        open_positions = remaining_positions

        if current_trade_day == 1:
            continue
        selection_date = trade_dates[current_trade_day - 2]
        daily_candidates = selection_map.get(selection_date, [])
        daily_buys = 0
        current_market_value = sum(
            (_position_market_value(position, current_date, price_cache) for position in open_positions),
            start=Decimal("0"),
        )

        for candidate in daily_candidates:
            if daily_buys >= max_daily_buys or len(open_positions) >= max_positions:
                break
            if any(position.symbol == candidate.symbol for position in open_positions):
                continue
            buy_price, _ = price_cache.get((candidate.symbol, current_date), (None, None))
            if buy_price is None:
                continue
            current_total_assets = cash + current_market_value
            max_position_amount = current_total_assets * position_size_pct / Decimal("100")
            lot_cost = buy_price * Decimal("100")
            lots = min(int(cash // lot_cost), int(max_position_amount // lot_cost))
            if lots < 1:
                continue
            shares = lots * 100
            buy_amount = buy_price * Decimal(shares)
            cash -= buy_amount
            current_market_value += buy_amount
            open_positions.append(
                OpenPosition(
                    symbol=candidate.symbol,
                    name=candidate.name,
                    selection_date=selection_date,
                    buy_date=current_date,
                    buy_trade_index=current_trade_index,
                    buy_price=buy_price,
                    shares=shares,
                    buy_amount=buy_amount,
                )
            )
            daily_buys += 1
            buy_count += 1

    final_trade_date = trade_dates[-1]
    market_value = sum(
        (_position_market_value(position, final_trade_date, price_cache) for position in open_positions),
        start=Decimal("0"),
    )
    final_assets = cash + market_value
    closed_count = len(closed_returns)
    win_rate = (
        _pct(Decimal(sum(1 for value in closed_returns if value > 0)) * Decimal("100") / Decimal(closed_count))
        if closed_count
        else None
    )
    avg_return = (
        _pct(sum(closed_returns, start=Decimal("0")) / Decimal(closed_count))
        if closed_count
        else None
    )
    return {
        "holding_period": holding_period,
        "selection_top_n": selection_top_n,
        "min_composite_score": str(min_score) if min_score is not None else "",
        "min_score_gap": str(score_gap) if score_gap is not None else "",
        "final_assets": str(_money(final_assets)),
        "total_return_pct": str(_pct((final_assets - initial_capital) * Decimal("100") / initial_capital)),
        "realized_profit": str(_money(realized_profit)),
        "cash": str(_money(cash)),
        "market_value": str(_money(market_value)),
        "buy_count": buy_count,
        "closed_trade_count": closed_count,
        "open_position_count": len(open_positions),
        "win_rate_pct": str(win_rate) if win_rate is not None else "",
        "avg_trade_return_pct": str(avg_return) if avg_return is not None else "",
        "exit_take_profit": exit_take_profit,
        "exit_stop_loss": exit_stop_loss,
        "exit_holding_period": exit_holding_period,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", type=Path, default=Path("data/quant.duckdb"))
    parser.add_argument("--strategies-dir", type=Path, default=Path("strategies"))
    parser.add_argument("--strategy", default="limit_up_pullback_ma5_reclaim")
    parser.add_argument("--start-date", type=_date, required=True)
    parser.add_argument("--end-date", type=_date, required=True)
    parser.add_argument("--holding-periods", type=_ints, default=_ints("3,5"))
    parser.add_argument("--min-scores", type=_decimals, default=_decimals("none,70,75"))
    parser.add_argument("--score-gaps", type=_decimals, default=_decimals("none,5"))
    parser.add_argument("--selection-top-n", type=int, default=1)
    parser.add_argument("--output-dir", type=Path, default=Path("results/research/fast_strategy_score_gate_sweep"))
    args = parser.parse_args()

    loaded = load_strategy_config(args.strategy, args.strategies_dir)
    defaults = loaded.config.strategy_backtest
    if defaults is None:
        raise SystemExit(f"Strategy {args.strategy} does not define strategy_backtest.")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    max_holding = max(args.holding_periods)
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
            for candidate in candidates[:args.selection_top_n]
        }
        final_index = min(trade_date_index[args.end_date] + 1 + max_holding, len(all_trade_dates) - 1)
        price_cache = load_price_cache(
            connection=connection,
            symbols=relevant_symbols,
            start_trade_date=trade_dates[0],
            end_trade_date=all_trade_dates[final_index],
        )

    rows = []
    for min_score in args.min_scores:
        for score_gap in args.score_gaps:
            for holding_period in args.holding_periods:
                result = _run_backtest(
                    raw_selection_map=raw_selection_map,
                    trade_dates=trade_dates,
                    all_trade_dates=all_trade_dates,
                    trade_date_index=trade_date_index,
                    price_cache=price_cache,
                    holding_period=holding_period,
                    max_positions=defaults.max_positions,
                    max_daily_buys=defaults.max_daily_buys,
                    initial_capital=defaults.initial_capital,
                    position_size_pct=defaults.position_size_pct,
                    take_profit_pct=defaults.take_profit_pct,
                    stop_loss_pct=defaults.stop_loss_pct,
                    selection_top_n=args.selection_top_n,
                    min_score=min_score,
                    score_gap=score_gap,
                )
                rows.append(result)
                print(
                    f"hp={holding_period} min_score={min_score} gap={score_gap} "
                    f"assets={result['final_assets']} return={result['total_return_pct']}% "
                    f"trades={result['closed_trade_count']}"
                )

    frame = pd.DataFrame(rows)
    frame["_final_assets_num"] = frame["final_assets"].astype(float)
    frame = frame.sort_values("_final_assets_num", ascending=False).drop(columns=["_final_assets_num"])
    frame.to_csv(args.output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    print("\n## Ranked Summary")
    print(frame.to_string(index=False))
    print(f"\nWrote CSV files to {args.output_dir}")


if __name__ == "__main__":
    main()
