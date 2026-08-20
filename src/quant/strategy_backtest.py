from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Callable

from quant.backtest_common import (
    connect,
    load_all_trade_dates,
    load_price_cache,
    load_trade_dates_in_range,
    quantize_pct,
    validate_trade_date,
)
from quant.selection import (
    SelectionRule,
    SelectionResultRow,
    _select_stocks_in_range_with_connection,
)


MONEY_QUANTIZE = Decimal("0.01")


class StrategyBacktestError(ValueError):
    """Raised when strategy backtest input is invalid."""


@dataclass(frozen=True)
class StrategyBacktestRequest:
    start_date: date
    end_date: date
    holding_period: int
    rules: list[SelectionRule]
    max_positions: int
    max_daily_buys: int
    initial_capital: Decimal
    position_size_pct: Decimal
    take_profit_pct: Decimal
    stop_loss_pct: Decimal
    logic: str = "AND"
    selection_sort_by: str = "symbol"
    selection_sort_order: str = "ASC"
    selection_top_n: int | None = None
    min_composite_score: Decimal | None = None
    min_score_gap: Decimal | None = None
    market_filter_enabled: bool = False
    intraday_stop_loss_enabled: bool = False


@dataclass(frozen=True)
class StrategyBacktestTrade:
    symbol: str
    name: str
    selection_date: date
    buy_date: date
    sell_date: date | None
    buy_price: Decimal
    sell_price: Decimal | None
    shares: int
    lots: int
    buy_amount: Decimal
    sell_amount: Decimal | None
    holding_days: int
    return_pct: Decimal | None
    status: str
    exit_reason: str | None


@dataclass(frozen=True)
class StrategyBacktestSummary:
    start_date: date
    end_date: date
    holding_period: int
    trade_days: int
    rule_count: int
    selection_sort_by: str
    selection_sort_order: str
    selection_top_n: int | None
    min_composite_score: Decimal | None
    min_score_gap: Decimal | None
    max_positions: int
    max_daily_buys: int
    initial_capital: Decimal
    position_size_pct: Decimal
    take_profit_pct: Decimal
    stop_loss_pct: Decimal
    closed_trade_count: int
    open_position_count: int
    total_trade_count: int
    current_cash_balance: Decimal
    current_market_value: Decimal
    current_total_assets: Decimal
    realized_profit: Decimal
    unrealized_profit: Decimal
    total_return_pct: Decimal
    annualized_return_pct: Decimal | None
    transaction_costs_included: bool
    market_filter_enabled: bool = False
    intraday_stop_loss_enabled: bool = False


@dataclass(frozen=True)
class StrategyBacktestReport:
    request: StrategyBacktestRequest
    summary: StrategyBacktestSummary
    trades: list[StrategyBacktestTrade]
    equity_curve: list["StrategyBacktestEquityPoint"]


@dataclass(frozen=True)
class StrategyBacktestProgress:
    current_trade_day: int
    total_trade_days: int
    trade_date: date


@dataclass(frozen=True)
class StrategyBacktestEquityPoint:
    trade_date: date
    cash_balance: Decimal
    market_value: Decimal
    total_assets: Decimal


@dataclass
class _OpenPosition:
    symbol: str
    name: str
    selection_date: date
    buy_date: date
    buy_trade_index: int
    buy_price: Decimal
    shares: int
    buy_amount: Decimal


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTIZE)


def _validate_request(
    connection,
    request: StrategyBacktestRequest,
) -> list[date]:
    if not request.rules:
        raise StrategyBacktestError("At least one selection rule is required.")
    if request.start_date > request.end_date:
        raise StrategyBacktestError("Backtest start_date must be on or before end_date.")
    if request.holding_period < 1:
        raise StrategyBacktestError("Holding period must be at least 1 trading day.")
    if request.max_positions < 1:
        raise StrategyBacktestError("max_positions must be at least 1.")
    if request.max_daily_buys < 1:
        raise StrategyBacktestError("max_daily_buys must be at least 1.")
    if request.initial_capital <= 0:
        raise StrategyBacktestError("initial_capital must be greater than 0.")
    if request.position_size_pct <= 0 or request.position_size_pct > 100:
        raise StrategyBacktestError("position_size_pct must be greater than 0 and less than or equal to 100.")
    if request.take_profit_pct <= 0:
        raise StrategyBacktestError("take_profit_pct must be greater than 0.")
    if request.stop_loss_pct >= 0:
        raise StrategyBacktestError("stop_loss_pct must be less than 0.")
    if request.selection_top_n is not None and request.selection_top_n < 1:
        raise StrategyBacktestError("selection_top_n must be at least 1 when provided.")
    if request.min_composite_score is not None and request.min_composite_score < 0:
        raise StrategyBacktestError("min_composite_score must be non-negative when provided.")
    if request.min_score_gap is not None and request.min_score_gap < 0:
        raise StrategyBacktestError("min_score_gap must be non-negative when provided.")

    validate_trade_date(connection, request.start_date, StrategyBacktestError)
    validate_trade_date(connection, request.end_date, StrategyBacktestError)
    trade_dates = load_trade_dates_in_range(connection, request.start_date, request.end_date)
    if not trade_dates:
        raise StrategyBacktestError("No trading dates found in the requested backtest range.")
    return trade_dates


def _lookup_price(
    price_cache: dict[tuple[str, date], tuple[Decimal | None, Decimal | None]],
    symbol: str,
    trade_date: date,
) -> tuple[Decimal | None, Decimal | None]:
    return price_cache.get((symbol, trade_date), (None, None))


def _position_market_value(
    position: _OpenPosition,
    trade_date: date,
    price_cache: dict[tuple[str, date], tuple[Decimal | None, Decimal | None]],
) -> Decimal:
    _, close_price = _lookup_price(price_cache, position.symbol, trade_date)
    if close_price is None:
        return position.buy_amount
    return close_price * Decimal(position.shares)


def _annualized_return_pct(
    initial_capital: Decimal,
    ending_assets: Decimal,
    trade_days: int,
) -> Decimal | None:
    if trade_days < 1:
        return None
    if initial_capital <= 0:
        return None
    ratio = float(ending_assets / initial_capital)
    annualized = (ratio ** (252 / trade_days) - 1) * 100
    return quantize_pct(Decimal(str(annualized)))


def _build_summary(
    request: StrategyBacktestRequest,
    trade_days: int,
    trades: list[StrategyBacktestTrade],
    current_cash_balance: Decimal,
    current_market_value: Decimal,
) -> StrategyBacktestSummary:
    closed_trade_count = sum(1 for trade in trades if trade.status == "closed")
    open_position_count = sum(1 for trade in trades if trade.status == "open")
    realized_profit = sum(
        (
            (trade.sell_amount or Decimal("0")) - trade.buy_amount
            for trade in trades
            if trade.status == "closed"
        ),
        start=Decimal("0"),
    )
    unrealized_profit = sum(
        (
            ((trade.sell_amount or Decimal("0")) - trade.buy_amount)
            for trade in trades
            if trade.status == "open"
        ),
        start=Decimal("0"),
    )
    current_total_assets = current_cash_balance + current_market_value
    total_return_pct = quantize_pct(
        (current_total_assets - request.initial_capital) * Decimal("100") / request.initial_capital
    )

    return StrategyBacktestSummary(
        start_date=request.start_date,
        end_date=request.end_date,
        holding_period=request.holding_period,
        trade_days=trade_days,
        rule_count=len(request.rules),
        selection_sort_by=request.selection_sort_by,
        selection_sort_order=request.selection_sort_order,
        selection_top_n=request.selection_top_n,
        min_composite_score=request.min_composite_score,
        min_score_gap=request.min_score_gap,
        market_filter_enabled=request.market_filter_enabled,
        max_positions=request.max_positions,
        max_daily_buys=request.max_daily_buys,
        initial_capital=_quantize_money(request.initial_capital),
        position_size_pct=quantize_pct(request.position_size_pct),
        take_profit_pct=quantize_pct(request.take_profit_pct),
        stop_loss_pct=quantize_pct(request.stop_loss_pct),
        closed_trade_count=closed_trade_count,
        open_position_count=open_position_count,
        total_trade_count=len(trades),
        current_cash_balance=_quantize_money(current_cash_balance),
        current_market_value=_quantize_money(current_market_value),
        current_total_assets=_quantize_money(current_total_assets),
        realized_profit=_quantize_money(realized_profit),
        unrealized_profit=_quantize_money(unrealized_profit),
        total_return_pct=total_return_pct,
        annualized_return_pct=_annualized_return_pct(
            initial_capital=request.initial_capital,
            ending_assets=current_total_assets,
            trade_days=trade_days,
        ),
        transaction_costs_included=False,
    )


def run_strategy_backtest(
    db_path: Path,
    request: StrategyBacktestRequest,
    progress_callback: Callable[[StrategyBacktestProgress], None] | None = None,
) -> StrategyBacktestReport:
    with connect(db_path) as connection:
        trade_dates = _validate_request(connection, request)
        all_trade_dates = load_all_trade_dates(connection)
        trade_date_index = {trade_date: index for index, trade_date in enumerate(all_trade_dates)}
        raw_selection_map = _select_stocks_in_range_with_connection(
            connection=connection,
            start_date=request.start_date,
            end_date=request.end_date,
            rules=request.rules,
            logic=request.logic,
            sort_by=request.selection_sort_by,
            sort_order=request.selection_sort_order,
        )
        selection_map: dict[date, list[SelectionResultRow]] = {}
        for selection_date, selection_results in raw_selection_map.items():
            if request.selection_top_n is None:
                selection_map[selection_date] = selection_results
            else:
                selection_map[selection_date] = selection_results[: request.selection_top_n]

        relevant_symbols = {
            result.symbol
            for selection_results in selection_map.values()
            for result in selection_results
        }
        price_cache = load_price_cache(
            connection=connection,
            symbols=relevant_symbols,
            start_trade_date=trade_dates[0],
            end_trade_date=trade_dates[-1],
        )

        cash = request.initial_capital
        open_positions: list[_OpenPosition] = []
        trades: list[StrategyBacktestTrade] = []
        equity_curve: list[StrategyBacktestEquityPoint] = []

        for current_trade_day, current_date in enumerate(trade_dates, start=1):
            if progress_callback is not None:
                progress_callback(
                    StrategyBacktestProgress(
                        current_trade_day=current_trade_day,
                        total_trade_days=len(trade_dates),
                        trade_date=current_date,
                    )
                )

            current_trade_index = trade_date_index[current_date]
            remaining_positions: list[_OpenPosition] = []
            for position in open_positions:
                if position.buy_trade_index >= current_trade_index:
                    remaining_positions.append(position)
                    continue

                _, close_price = _lookup_price(price_cache, position.symbol, current_date)
                if close_price is None:
                    remaining_positions.append(position)
                    continue

                holding_days = current_trade_index - position.buy_trade_index
                sell_amount = close_price * Decimal(position.shares)
                return_pct = quantize_pct(
                    (sell_amount - position.buy_amount) * Decimal("100") / position.buy_amount
                )

                exit_reason: str | None = None
                if return_pct >= request.take_profit_pct:
                    exit_reason = "take_profit"
                elif return_pct <= request.stop_loss_pct:
                    exit_reason = "stop_loss"
                elif holding_days >= request.holding_period:
                    exit_reason = "holding_period"

                if exit_reason is None:
                    remaining_positions.append(position)
                    continue

                cash += sell_amount
                trades.append(
                    StrategyBacktestTrade(
                        symbol=position.symbol,
                        name=position.name,
                        selection_date=position.selection_date,
                        buy_date=position.buy_date,
                        sell_date=current_date,
                        buy_price=position.buy_price,
                        sell_price=close_price,
                        shares=position.shares,
                        lots=position.shares // 100,
                        buy_amount=_quantize_money(position.buy_amount),
                        sell_amount=_quantize_money(sell_amount),
                        holding_days=holding_days,
                        return_pct=return_pct,
                        status="closed",
                        exit_reason=exit_reason,
                    )
                )

            open_positions = remaining_positions

            if current_trade_day == 1:
                continue

            selection_date = trade_dates[current_trade_day - 2]
            daily_candidates = selection_map.get(selection_date, [])
            daily_buys = 0

            # Calculate current market value once before the candidate loop
            current_market_value = sum(
                (
                    _position_market_value(position, current_date, price_cache)
                    for position in open_positions
                ),
                start=Decimal("0"),
            )

            for candidate in daily_candidates:
                if daily_buys >= request.max_daily_buys:
                    break
                if len(open_positions) >= request.max_positions:
                    break
                if any(position.symbol == candidate.symbol for position in open_positions):
                    continue

                buy_price, _ = _lookup_price(price_cache, candidate.symbol, current_date)
                if buy_price is None:
                    continue

                current_total_assets = cash + current_market_value
                max_position_amount = (
                    current_total_assets * request.position_size_pct / Decimal("100")
                )
                lot_cost = buy_price * Decimal("100")
                max_lots_by_cash = int(cash // lot_cost)
                max_lots_by_position = int(max_position_amount // lot_cost)
                lots = min(max_lots_by_cash, max_lots_by_position)

                if lots < 1:
                    continue

                shares = lots * 100
                buy_amount = buy_price * Decimal(shares)
                cash -= buy_amount
                new_position_market_value = buy_price * Decimal(shares)
                current_market_value += new_position_market_value
                open_positions.append(
                    _OpenPosition(
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

            equity_curve.append(
                StrategyBacktestEquityPoint(
                    trade_date=current_date,
                    cash_balance=_quantize_money(cash),
                    market_value=_quantize_money(current_market_value),
                    total_assets=_quantize_money(cash + current_market_value),
                )
            )

        final_trade_date = trade_dates[-1]
        current_market_value = Decimal("0")
        for position in open_positions:
            market_value = _position_market_value(position, final_trade_date, price_cache)
            current_market_value += market_value
            holding_days = trade_date_index[final_trade_date] - position.buy_trade_index
            return_pct = quantize_pct(
                (market_value - position.buy_amount) * Decimal("100") / position.buy_amount
            )
            _, final_close_price = _lookup_price(price_cache, position.symbol, final_trade_date)
            trades.append(
                StrategyBacktestTrade(
                    symbol=position.symbol,
                    name=position.name,
                    selection_date=position.selection_date,
                    buy_date=position.buy_date,
                    sell_date=None,
                    buy_price=position.buy_price,
                    sell_price=final_close_price,
                    shares=position.shares,
                    lots=position.shares // 100,
                    buy_amount=_quantize_money(position.buy_amount),
                    sell_amount=_quantize_money(market_value),
                    holding_days=holding_days,
                    return_pct=return_pct,
                    status="open",
                    exit_reason="open_at_end",
                )
            )

    trades.sort(key=lambda trade: (trade.buy_date, trade.symbol, trade.sell_date or trade.buy_date))
    summary = _build_summary(
        request=request,
        trade_days=len(trade_dates),
        trades=trades,
        current_cash_balance=cash,
        current_market_value=current_market_value,
    )
    return StrategyBacktestReport(
        request=request,
        summary=summary,
        trades=trades,
        equity_curve=equity_curve,
    )
