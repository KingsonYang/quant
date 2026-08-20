from __future__ import annotations

import argparse
import csv
import random
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import duckdb

from quant.backtest_common import load_all_trade_dates, load_price_cache, load_trade_dates_in_range
from quant.config.loader import load_strategy_config
from quant.pattern_selection import select_pattern_candidates_in_range_with_connection


DEFAULT_STRATEGY = "limit_up_pullback_ma5_reclaim"
DEFAULT_START = "2022-12-29"
DEFAULT_END = "2026-05-06"
DEFAULT_RECENT_START = "2025-12-31"
MONEY_QUANTIZE = Decimal("0.01")
PCT_QUANTIZE = Decimal("0.0001")


@dataclass(frozen=True)
class TrialParams:
    holding_period: int
    max_positions: int
    max_daily_buys: int
    position_size_pct: Decimal
    take_profit_pct: Decimal
    stop_loss_pct: Decimal
    selection_top_n: int


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


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


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


def _run_backtest(
    *,
    params: TrialParams,
    trade_dates: list[date],
    all_trade_dates: list[date],
    trade_date_index: dict[date, int],
    selection_map: dict[date, list[Any]],
    price_cache: dict[tuple[str, date], tuple[Decimal | None, Decimal | None]],
    initial_capital: Decimal,
) -> dict[str, Any]:
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

            exit_reason: str | None = None
            if return_pct >= params.take_profit_pct:
                exit_reason = "take_profit"
            elif return_pct <= params.stop_loss_pct:
                exit_reason = "stop_loss"
            elif holding_days >= params.holding_period:
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
        daily_candidates = selection_map.get(selection_date, [])[: params.selection_top_n]
        daily_buys = 0
        current_market_value = sum(
            (_position_market_value(position, current_date, price_cache) for position in open_positions),
            start=Decimal("0"),
        )

        for candidate in daily_candidates:
            if daily_buys >= params.max_daily_buys:
                break
            if len(open_positions) >= params.max_positions:
                break
            if any(position.symbol == candidate.symbol for position in open_positions):
                continue

            buy_price, _ = price_cache.get((candidate.symbol, current_date), (None, None))
            if buy_price is None:
                continue

            current_total_assets = cash + current_market_value
            max_position_amount = current_total_assets * params.position_size_pct / Decimal("100")
            lot_cost = buy_price * Decimal("100")
            max_lots_by_cash = int(cash // lot_cost)
            max_lots_by_position = int(max_position_amount // lot_cost)
            lots = min(max_lots_by_cash, max_lots_by_position)
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
    current_market_value = sum(
        (_position_market_value(position, final_trade_date, price_cache) for position in open_positions),
        start=Decimal("0"),
    )
    final_assets = cash + current_market_value
    total_return_pct = _pct((final_assets - initial_capital) * Decimal("100") / initial_capital)
    closed_trade_count = len(closed_returns)
    win_rate_pct = (
        _pct(Decimal(sum(1 for value in closed_returns if value > 0)) * Decimal("100") / Decimal(closed_trade_count))
        if closed_trade_count
        else None
    )
    avg_trade_return_pct = (
        _pct(sum(closed_returns, start=Decimal("0")) / Decimal(closed_trade_count))
        if closed_trade_count
        else None
    )

    return {
        "holding_period": params.holding_period,
        "max_positions": params.max_positions,
        "max_daily_buys": params.max_daily_buys,
        "position_size_pct": str(params.position_size_pct),
        "take_profit_pct": str(params.take_profit_pct),
        "stop_loss_pct": str(params.stop_loss_pct),
        "selection_top_n": params.selection_top_n,
        "final_assets": str(_money(final_assets)),
        "total_return_pct": str(total_return_pct),
        "realized_profit": str(_money(realized_profit)),
        "cash": str(_money(cash)),
        "market_value": str(_money(current_market_value)),
        "buy_count": buy_count,
        "closed_trade_count": closed_trade_count,
        "open_position_count": len(open_positions),
        "win_rate_pct": str(win_rate_pct) if win_rate_pct is not None else "",
        "avg_trade_return_pct": str(avg_trade_return_pct) if avg_trade_return_pct is not None else "",
        "exit_take_profit": exit_take_profit,
        "exit_stop_loss": exit_stop_loss,
        "exit_holding_period": exit_holding_period,
    }


def _decimal_choices(values: list[str]) -> list[Decimal]:
    return [Decimal(value) for value in values]


def _build_random_trials(count: int, seed: int) -> list[TrialParams]:
    rng = random.Random(seed)
    holding_periods = list(range(1, 11))
    max_positions = [1, 2, 3, 5]
    max_daily_buys = [1, 2, 3]
    position_size_pcts = _decimal_choices(["20", "33", "50", "70", "100"])
    take_profit_pcts = _decimal_choices(["8", "10", "12", "15", "20", "25"])
    stop_loss_pcts = _decimal_choices(["-3", "-5", "-7", "-10"])
    selection_top_ns = [1, 2, 3, 5, 8, 10]

    trials: list[TrialParams] = []
    seen: set[tuple[Any, ...]] = set()
    max_unique = (
        len(holding_periods)
        * len(max_positions)
        * len(max_daily_buys)
        * len(position_size_pcts)
        * len(take_profit_pcts)
        * len(stop_loss_pcts)
        * len(selection_top_ns)
    )
    target = min(count, max_unique)
    while len(trials) < target:
        trial = TrialParams(
            holding_period=rng.choice(holding_periods),
            max_positions=rng.choice(max_positions),
            max_daily_buys=rng.choice(max_daily_buys),
            position_size_pct=rng.choice(position_size_pcts),
            take_profit_pct=rng.choice(take_profit_pcts),
            stop_loss_pct=rng.choice(stop_loss_pcts),
            selection_top_n=rng.choice(selection_top_ns),
        )
        key = (
            trial.holding_period,
            trial.max_positions,
            trial.max_daily_buys,
            trial.position_size_pct,
            trial.take_profit_pct,
            trial.stop_loss_pct,
            trial.selection_top_n,
        )
        if key in seen:
            continue
        seen.add(key)
        trials.append(trial)
    return trials


def _prepare_context(
    *,
    db_path: Path,
    strategy_name: str,
    strategies_dir: Path,
    start_date: date,
    end_date: date,
    max_selection_top_n: int,
) -> dict[str, Any]:
    with duckdb.connect(str(db_path), read_only=True) as connection:
        loaded = load_strategy_config(strategy_name, strategies_dir)
        trade_dates = load_trade_dates_in_range(connection, start_date, end_date)
        all_trade_dates = load_all_trade_dates(connection)
        trade_date_index = {trade_day: index for index, trade_day in enumerate(all_trade_dates)}
        raw_selection_map = select_pattern_candidates_in_range_with_connection(
            connection,
            start_date,
            end_date,
            loaded.config.selection,
        )
        selection_map = {
            trade_day: raw_selection_map.get(trade_day, [])[:max_selection_top_n]
            for trade_day in trade_dates
        }
        relevant_symbols = {
            candidate.symbol
            for candidates in selection_map.values()
            for candidate in candidates
        }
        price_cache = load_price_cache(
            connection=connection,
            symbols=relevant_symbols,
            start_trade_date=trade_dates[0],
            end_trade_date=trade_dates[-1],
        )
    return {
        "trade_dates": trade_dates,
        "all_trade_dates": all_trade_dates,
        "trade_date_index": trade_date_index,
        "selection_map": selection_map,
        "price_cache": price_cache,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", type=Path, default=Path("data/quant.duckdb"))
    parser.add_argument("--strategies-dir", type=Path, default=Path("strategies"))
    parser.add_argument("--strategy", default=DEFAULT_STRATEGY)
    parser.add_argument("--start-date", type=_parse_date, default=_parse_date(DEFAULT_START))
    parser.add_argument("--end-date", type=_parse_date, default=_parse_date(DEFAULT_END))
    parser.add_argument("--recent-start-date", type=_parse_date, default=_parse_date(DEFAULT_RECENT_START))
    parser.add_argument("--trials", type=int, default=500)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--initial-capital", type=Decimal, default=Decimal("100000"))
    parser.add_argument("--output-dir", type=Path, default=Path("results/research/limit_up_pullback_ma5_reclaim_strategy_random"))
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    trials = _build_random_trials(args.trials, args.seed)

    long_context = _prepare_context(
        db_path=args.db_path,
        strategy_name=args.strategy,
        strategies_dir=args.strategies_dir,
        start_date=args.start_date,
        end_date=args.end_date,
        max_selection_top_n=10,
    )
    long_rows = [
        _run_backtest(
            params=trial,
            initial_capital=args.initial_capital,
            **long_context,
        )
        for trial in trials
    ]
    long_rows.sort(key=lambda row: Decimal(row["final_assets"]), reverse=True)

    recent_context = _prepare_context(
        db_path=args.db_path,
        strategy_name=args.strategy,
        strategies_dir=args.strategies_dir,
        start_date=args.recent_start_date,
        end_date=args.end_date,
        max_selection_top_n=10,
    )
    recent_by_params = {}
    for trial in trials:
        result = _run_backtest(
            params=trial,
            initial_capital=args.initial_capital,
            **recent_context,
        )
        key = (
            result["holding_period"],
            result["max_positions"],
            result["max_daily_buys"],
            result["position_size_pct"],
            result["take_profit_pct"],
            result["stop_loss_pct"],
            result["selection_top_n"],
        )
        recent_by_params[key] = result

    combined_rows = []
    for row in long_rows:
        key = (
            row["holding_period"],
            row["max_positions"],
            row["max_daily_buys"],
            row["position_size_pct"],
            row["take_profit_pct"],
            row["stop_loss_pct"],
            row["selection_top_n"],
        )
        recent = recent_by_params[key]
        combined = dict(row)
        combined["recent_final_assets"] = recent["final_assets"]
        combined["recent_total_return_pct"] = recent["total_return_pct"]
        combined["recent_closed_trade_count"] = recent["closed_trade_count"]
        combined["recent_win_rate_pct"] = recent["win_rate_pct"]
        combined_rows.append(combined)

    _write_csv(args.output_dir / "trials_ranked_by_long_final_assets.csv", combined_rows)

    print(f"strategy={args.strategy}")
    print(f"long_window={args.start_date}..{args.end_date}")
    print(f"recent_window={args.recent_start_date}..{args.end_date}")
    print(f"trials={len(trials)}, seed={args.seed}")
    print(f"output={args.output_dir}")
    print("TOP_BY_LONG_FINAL_ASSETS")
    header = (
        "rank hp pos buys size tp sl top final_assets total_ret closed win_rate "
        "recent_assets recent_ret"
    )
    print(header)
    for rank, row in enumerate(combined_rows[:20], start=1):
        print(
            f"{rank:>4d} "
            f"{row['holding_period']:>2d} "
            f"{row['max_positions']:>3d} "
            f"{row['max_daily_buys']:>4d} "
            f"{row['position_size_pct']:>4s} "
            f"{row['take_profit_pct']:>3s} "
            f"{row['stop_loss_pct']:>4s} "
            f"{row['selection_top_n']:>3d} "
            f"{row['final_assets']:>12s} "
            f"{row['total_return_pct']:>9s} "
            f"{row['closed_trade_count']:>6d} "
            f"{row['win_rate_pct']:>8s} "
            f"{row['recent_final_assets']:>13s} "
            f"{row['recent_total_return_pct']:>10s}"
        )


if __name__ == "__main__":
    main()
