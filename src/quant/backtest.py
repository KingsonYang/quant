from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Callable

import duckdb

from quant.backtest_common import (
    connect,
    load_all_trade_dates,
    load_price_cache,
    load_trade_dates_in_range,
    quantize_pct,
    validate_trade_date,
)
from quant.selection import SelectionRule, _select_stocks_with_connection


DEFAULT_SELECTION_SORT_BY = "symbol"
DEFAULT_SELECTION_SORT_ORDER = "ASC"


class StockSelectionBacktestError(ValueError):
    """Raised when stock selection backtest input is invalid."""


@dataclass(frozen=True)
class StockSelectionBacktestRequest:
    start_date: date
    end_date: date
    holding_period: int
    rules: list[SelectionRule]
    logic: str = "AND"
    selection_sort_by: str = DEFAULT_SELECTION_SORT_BY
    selection_sort_order: str = DEFAULT_SELECTION_SORT_ORDER
    selection_top_n: int | None = None


@dataclass(frozen=True)
class ReturnDistributionBucket:
    label: str
    count: int


@dataclass(frozen=True)
class StockSelectionBacktestDetail:
    symbol: str
    name: str
    selection_date: date
    buy_date: date | None
    sell_date: date | None
    buy_price: Decimal | None
    sell_price: Decimal | None
    holding_period: int
    return_pct: Decimal | None
    status: str
    incomplete_reason: str | None


@dataclass(frozen=True)
class StockSelectionBacktestSummary:
    start_date: date
    end_date: date
    holding_period: int
    trade_days: int
    rule_count: int
    selection_sort_by: str
    selection_sort_order: str
    selection_top_n: int | None
    hit_count: int
    completed_count: int
    incomplete_count: int
    positive_count: int
    negative_count: int
    flat_count: int
    win_rate_pct: Decimal | None
    average_return_pct: Decimal | None
    return_distribution: list[ReturnDistributionBucket]


@dataclass(frozen=True)
class StockSelectionBacktestReport:
    request: StockSelectionBacktestRequest
    summary: StockSelectionBacktestSummary
    details: list[StockSelectionBacktestDetail]


@dataclass(frozen=True)
class StockSelectionBacktestProgress:
    current_trade_day: int
    total_trade_days: int
    selection_date: date


def _quantize_pct(value: Decimal) -> Decimal:
    return quantize_pct(value)


def _validate_request(
    connection: duckdb.DuckDBPyConnection,
    request: StockSelectionBacktestRequest,
) -> list[date]:
    if not request.rules:
        raise StockSelectionBacktestError("At least one selection rule is required.")
    if request.holding_period < 1:
        raise StockSelectionBacktestError("Holding period must be at least 1 trading day.")
    if request.selection_top_n is not None and request.selection_top_n < 1:
        raise StockSelectionBacktestError("selection_top_n must be at least 1 when provided.")
    if request.start_date > request.end_date:
        raise StockSelectionBacktestError("Backtest start_date must be on or before end_date.")

    validate_trade_date(connection, request.start_date, StockSelectionBacktestError)
    validate_trade_date(connection, request.end_date, StockSelectionBacktestError)

    trade_dates = load_trade_dates_in_range(connection, request.start_date, request.end_date)
    if not trade_dates:
        raise StockSelectionBacktestError("No trading dates found in the requested backtest range.")
    return trade_dates


def _build_distribution(completed_returns: list[Decimal]) -> list[ReturnDistributionBucket]:
    buckets = [
        ("小于-7.5%", lambda value: value < Decimal("-7.5")),
        ("-7.5%到-3.5%", lambda value: Decimal("-7.5") <= value < Decimal("-3.5")),
        ("-3.5%到0%", lambda value: Decimal("-3.5") <= value < Decimal("0")),
        ("0%到3.5%", lambda value: Decimal("0") <= value < Decimal("3.5")),
        ("3.5%到7.5%", lambda value: Decimal("3.5") <= value < Decimal("7.5")),
        ("大于等于7.5%", lambda value: value >= Decimal("7.5")),
    ]
    return [
        ReturnDistributionBucket(
            label=label,
            count=sum(1 for value in completed_returns if predicate(value)),
        )
        for label, predicate in buckets
    ]


def _build_summary(
    request: StockSelectionBacktestRequest,
    trade_days: int,
    details: list[StockSelectionBacktestDetail],
) -> StockSelectionBacktestSummary:
    completed = [detail for detail in details if detail.status == "completed"]
    completed_returns = [detail.return_pct for detail in completed if detail.return_pct is not None]
    positive_count = sum(1 for value in completed_returns if value > 0)
    negative_count = sum(1 for value in completed_returns if value < 0)
    flat_count = sum(1 for value in completed_returns if value == 0)

    average_return_pct = None
    win_rate_pct = None
    if completed_returns:
        average_return_pct = _quantize_pct(
            sum(completed_returns, start=Decimal("0")) / Decimal(len(completed_returns))
        )
        win_rate_pct = _quantize_pct(
            Decimal(positive_count) * Decimal("100") / Decimal(len(completed_returns))
        )

    return StockSelectionBacktestSummary(
        start_date=request.start_date,
        end_date=request.end_date,
        holding_period=request.holding_period,
        trade_days=trade_days,
        rule_count=len(request.rules),
        selection_sort_by=request.selection_sort_by,
        selection_sort_order=request.selection_sort_order,
        selection_top_n=request.selection_top_n,
        hit_count=len(details),
        completed_count=len(completed),
        incomplete_count=len(details) - len(completed),
        positive_count=positive_count,
        negative_count=negative_count,
        flat_count=flat_count,
        win_rate_pct=win_rate_pct,
        average_return_pct=average_return_pct,
        return_distribution=_build_distribution(completed_returns),
    )


def run_stock_selection_backtest(
    db_path: Path,
    request: StockSelectionBacktestRequest,
    progress_callback: Callable[[StockSelectionBacktestProgress], None] | None = None,
) -> StockSelectionBacktestReport:
    with connect(db_path) as connection:
        trade_dates = _validate_request(connection, request)
        all_trade_dates = load_all_trade_dates(connection)
        trade_date_index = {trade_date: index for index, trade_date in enumerate(all_trade_dates)}
        selection_batches: list[tuple[date, list]] = []

        for current_trade_day, selection_date in enumerate(trade_dates, start=1):
            if progress_callback is not None:
                progress_callback(
                    StockSelectionBacktestProgress(
                        current_trade_day=current_trade_day,
                        total_trade_days=len(trade_dates),
                        selection_date=selection_date,
                    )
                )
            selection_summary = _select_stocks_with_connection(
                connection=connection,
                trade_date=selection_date,
                rules=request.rules,
                logic=request.logic,
                sort_by=request.selection_sort_by,
                sort_order=request.selection_sort_order,
            )
            selection_results = selection_summary.results
            if request.selection_top_n is not None:
                selection_results = selection_results[: request.selection_top_n]
            selection_batches.append((selection_date, selection_results))

        relevant_symbols = {
            result.symbol
            for _, selection_results in selection_batches
            for result in selection_results
        }
        final_sell_index = max(
            (
                min(trade_date_index[selection_date] + 1 + request.holding_period, len(all_trade_dates) - 1)
                for selection_date, selection_results in selection_batches
                if selection_results
            ),
            default=0,
        )
        price_cache = load_price_cache(
            connection=connection,
            symbols=relevant_symbols,
            start_trade_date=trade_dates[0],
            end_trade_date=all_trade_dates[final_sell_index],
        )
        details: list[StockSelectionBacktestDetail] = []

        for selection_date, selection_results in selection_batches:
            selection_index = trade_date_index[selection_date]
            buy_index = selection_index + 1
            sell_index = buy_index + request.holding_period

            buy_date = all_trade_dates[buy_index] if buy_index < len(all_trade_dates) else None
            sell_date = all_trade_dates[sell_index] if sell_index < len(all_trade_dates) else None

            for result in selection_results:
                if buy_date is None:
                    details.append(
                        StockSelectionBacktestDetail(
                            symbol=result.symbol,
                            name=result.name,
                            selection_date=selection_date,
                            buy_date=None,
                            sell_date=None,
                            buy_price=None,
                            sell_price=None,
                            holding_period=request.holding_period,
                            return_pct=None,
                            status="incomplete",
                            incomplete_reason="missing_next_trade_date",
                        )
                    )
                    continue

                buy_price, _ = price_cache.get((result.symbol, buy_date), (None, None))
                if buy_price is None:
                    details.append(
                        StockSelectionBacktestDetail(
                            symbol=result.symbol,
                            name=result.name,
                            selection_date=selection_date,
                            buy_date=buy_date,
                            sell_date=None,
                            buy_price=None,
                            sell_price=None,
                            holding_period=request.holding_period,
                            return_pct=None,
                            status="incomplete",
                            incomplete_reason="missing_buy_bar",
                        )
                    )
                    continue

                if sell_date is None:
                    details.append(
                        StockSelectionBacktestDetail(
                            symbol=result.symbol,
                            name=result.name,
                            selection_date=selection_date,
                            buy_date=buy_date,
                            sell_date=None,
                            buy_price=buy_price,
                            sell_price=None,
                            holding_period=request.holding_period,
                            return_pct=None,
                            status="incomplete",
                            incomplete_reason="insufficient_holding_window",
                        )
                    )
                    continue

                _, sell_price = price_cache.get((result.symbol, sell_date), (None, None))
                if sell_price is None:
                    details.append(
                        StockSelectionBacktestDetail(
                            symbol=result.symbol,
                            name=result.name,
                            selection_date=selection_date,
                            buy_date=buy_date,
                            sell_date=sell_date,
                            buy_price=buy_price,
                            sell_price=None,
                            holding_period=request.holding_period,
                            return_pct=None,
                            status="incomplete",
                            incomplete_reason="missing_sell_bar",
                        )
                    )
                    continue

                return_pct = _quantize_pct((sell_price - buy_price) * Decimal("100") / buy_price)
                details.append(
                    StockSelectionBacktestDetail(
                        symbol=result.symbol,
                        name=result.name,
                        selection_date=selection_date,
                        buy_date=buy_date,
                        sell_date=sell_date,
                        buy_price=buy_price,
                        sell_price=sell_price,
                        holding_period=request.holding_period,
                        return_pct=return_pct,
                        status="completed",
                        incomplete_reason=None,
                    )
                )

    summary = _build_summary(request, trade_days=len(trade_dates), details=details)
    return StockSelectionBacktestReport(
        request=request,
        summary=summary,
        details=details,
    )
