from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from quant.backtest import (
    ReturnDistributionBucket,
    StockSelectionBacktestDetail,
    StockSelectionBacktestProgress,
    StockSelectionBacktestReport,
    StockSelectionBacktestRequest,
    StockSelectionBacktestSummary,
)
from quant.backtest_common import (
    connect,
    load_all_trade_dates,
    load_low_price_cache,
    load_low_qfq_cache,
    load_price_cache,
    load_qfq_price_cache,
    load_trade_dates_in_range,
    quantize_pct,
    validate_trade_date,
)
from quant.config.loader import LoadedStrategyConfig
from quant.config.models import PatternSelectionConfig, RulesSelectionConfig
from quant.market_regime import load_market_regime_states
from quant.pattern_selection import (
    PatternSelectionCandidate,
    PatternSelectionError,
    select_pattern_candidates_in_range_with_connection,
    select_pattern_candidates_with_connection,
)
from quant.selection import (
    _select_stocks_in_range_with_connection,
    _select_stocks_with_connection,
)
from quant.strategy_backtest import (
    MONEY_QUANTIZE,
    StrategyBacktestEquityPoint,
    StrategyBacktestProgress,
    StrategyBacktestReport,
    StrategyBacktestRequest,
    StrategyBacktestSummary,
    StrategyBacktestTrade,
)
from quant.strategy_matrix import StrategyHoldingPeriodSummary, format_strategy_holding_period_table


class StrategyRuntimeError(ValueError):
    """Raised when a YAML strategy cannot be executed."""


@dataclass(frozen=True)
class StrategySelectionCandidate:
    symbol: str
    name: str
    selection_date: date
    d0_date: date | None = None
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
    false_break_occurred: bool | None = None
    false_break_reclaim_days_actual: int | None = None
    pool_score_v3: float | None = None


@dataclass(frozen=True)
class StrategySelectionView:
    strategy_name: str
    display_name: str | None
    selection_type: str
    trade_date: date
    sort_by: str
    sort_order: str
    candidate_count: int
    candidates: list[StrategySelectionCandidate]


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


def strategy_label(loaded: LoadedStrategyConfig) -> str:
    return loaded.config.strategy.display_name or loaded.name


def _rule_count(loaded: LoadedStrategyConfig) -> int:
    if isinstance(loaded.config.selection, RulesSelectionConfig):
        return len(loaded.config.selection.rules)
    return 1


def _selection_sort(loaded: LoadedStrategyConfig) -> tuple[str, str]:
    selection = loaded.config.selection
    return selection.sort_by, selection.sort_order


def _selection_type_name(loaded: LoadedStrategyConfig) -> str:
    selection = loaded.config.selection
    if isinstance(selection, RulesSelectionConfig):
        return "rules"
    return selection.pattern_id


def _convert_pattern_candidates(
    candidates: list[PatternSelectionCandidate],
) -> list[StrategySelectionCandidate]:
    return [
        StrategySelectionCandidate(
            symbol=candidate.symbol,
            name=candidate.name,
            selection_date=candidate.selection_date,
            d0_date=candidate.d0_date,
            composite_score=candidate.composite_score,
            branch_type=candidate.branch_type,
            board_count=candidate.board_count,
            first_limit_date=candidate.first_limit_date,
            first_limit_open=candidate.first_limit_open,
            first_limit_close=candidate.first_limit_close,
            last_limit_date=candidate.last_limit_date,
            last_limit_open=candidate.last_limit_open,
            last_limit_close=candidate.last_limit_close,
            signal_open=candidate.signal_open,
            signal_close=candidate.signal_close,
            signal_ma5=candidate.signal_ma5,
           false_break_occurred=candidate.false_break_occurred,
           false_break_reclaim_days_actual=candidate.false_break_reclaim_days_actual,
            pool_score_v3=candidate.pool_score_v3,
       )
       for candidate in candidates
   ]


def _select_candidates_with_connection(
    connection,
    trade_date: date,
    loaded: LoadedStrategyConfig,
) -> list[StrategySelectionCandidate]:
    selection = loaded.config.selection
    if isinstance(selection, RulesSelectionConfig):
        summary = _select_stocks_with_connection(
            connection=connection,
            trade_date=trade_date,
            rules=selection.to_selection_rules(),
            logic=selection.logic,
            sort_by=selection.sort_by,
            sort_order=selection.sort_order,
        )
        return [
            StrategySelectionCandidate(
                symbol=row.symbol,
                name=row.name,
                selection_date=row.trade_date,
            )
            for row in summary.results
        ]
    if isinstance(selection, PatternSelectionConfig):
        return _convert_pattern_candidates(
            select_pattern_candidates_with_connection(connection, trade_date, selection)
        )
    raise StrategyRuntimeError("Unsupported strategy selection configuration.")


def _select_candidates_in_range_with_connection(
    connection,
    start_date: date,
    end_date: date,
    loaded: LoadedStrategyConfig,
) -> dict[date, list[StrategySelectionCandidate]]:
    selection = loaded.config.selection
    if isinstance(selection, RulesSelectionConfig):
        grouped = _select_stocks_in_range_with_connection(
            connection=connection,
            start_date=start_date,
            end_date=end_date,
            rules=selection.to_selection_rules(),
            logic=selection.logic,
            sort_by=selection.sort_by,
            sort_order=selection.sort_order,
        )
        return {
            trade_date: [
                StrategySelectionCandidate(
                    symbol=row.symbol,
                    name=row.name,
                    selection_date=row.trade_date,
                )
                for row in rows
            ]
            for trade_date, rows in grouped.items()
        }
    if isinstance(selection, PatternSelectionConfig):
        grouped = select_pattern_candidates_in_range_with_connection(
            connection,
            start_date,
            end_date,
            selection,
        )
        return {
            trade_date: _convert_pattern_candidates(rows)
            for trade_date, rows in grouped.items()
        }
    raise StrategyRuntimeError("Unsupported strategy selection configuration.")


def _apply_strategy_score_gate(
    candidates: list[StrategySelectionCandidate],
    *,
    min_composite_score: Decimal | None,
    min_score_gap: Decimal | None,
) -> list[StrategySelectionCandidate]:
    if not candidates:
        return candidates
    if min_composite_score is None and min_score_gap is None:
        return candidates

    first = candidates[0]
    if min_composite_score is not None:
        if first.composite_score is None:
            return []
        if Decimal(str(first.composite_score)) < min_composite_score:
            return []

    if min_score_gap is not None and len(candidates) >= 2:
        second = candidates[1]
        if first.composite_score is None or second.composite_score is None:
            return []
        score_gap = Decimal(str(first.composite_score)) - Decimal(str(second.composite_score))
        if score_gap < min_score_gap:
            return []

    return candidates


def _build_recent_signal_filter_passes(
    *,
    selection_map: dict[date, list[StrategySelectionCandidate]],
    trade_dates: list[date],
    all_trade_dates: list[date],
    trade_date_index: dict[date, int],
    price_cache: dict[tuple[str, date], tuple[Decimal | None, Decimal | None]],
    holding_period: int,
    recent_signal_window: int,
    recent_signal_min_count: int,
    min_recent_signal_win_rate: Decimal | None,
    min_recent_signal_avg_return: Decimal | None,
) -> dict[date, bool]:
    if min_recent_signal_win_rate is None and min_recent_signal_avg_return is None:
        return {trade_date: True for trade_date in trade_dates}

    outcomes: list[tuple[date, date, Decimal]] = []
    for selection_date, candidates in selection_map.items():
        selection_index = trade_date_index.get(selection_date)
        if selection_index is None:
            continue
        buy_index = selection_index + 1
        sell_index = buy_index + holding_period
        if sell_index >= len(all_trade_dates):
            continue
        buy_date = all_trade_dates[buy_index]
        sell_date = all_trade_dates[sell_index]
        for candidate in candidates:
            buy_price, _ = _lookup_price(price_cache, candidate.symbol, buy_date)
            _, sell_price = _lookup_price(price_cache, candidate.symbol, sell_date)
            if buy_price is None or sell_price is None or buy_price <= 0:
                continue
            return_pct = quantize_pct((sell_price - buy_price) * Decimal("100") / buy_price)
            outcomes.append((sell_date, selection_date, return_pct))
    outcomes.sort(key=lambda item: (item[0], item[1]))

    passes: dict[date, bool] = {}
    completed_returns: list[Decimal] = []
    outcome_index = 0
    for trade_date in trade_dates:
        while outcome_index < len(outcomes) and outcomes[outcome_index][0] < trade_date:
            completed_returns.append(outcomes[outcome_index][2])
            outcome_index += 1

        recent_returns = completed_returns[-recent_signal_window:]
        if len(recent_returns) < recent_signal_min_count:
            passes[trade_date] = True
            continue

        passed = True
        if recent_returns:
            win_rate = quantize_pct(
                Decimal(sum(1 for value in recent_returns if value > 0))
                * Decimal("100")
                / Decimal(len(recent_returns))
            )
            avg_return = quantize_pct(
                sum(recent_returns, start=Decimal("0")) / Decimal(len(recent_returns))
            )
        else:
            win_rate = Decimal("0")
            avg_return = Decimal("0")

        if min_recent_signal_win_rate is not None and win_rate < min_recent_signal_win_rate:
            passed = False
        if min_recent_signal_avg_return is not None and avg_return < min_recent_signal_avg_return:
            passed = False
        passes[trade_date] = passed

    return passes


def run_strategy_selection(
    db_path: Path,
    trade_date: date,
    loaded: LoadedStrategyConfig,
) -> StrategySelectionView:
    with connect(db_path) as connection:
        candidates = _select_candidates_with_connection(connection, trade_date, loaded)
    sort_by, sort_order = _selection_sort(loaded)
    return StrategySelectionView(
        strategy_name=loaded.name,
        display_name=loaded.config.strategy.display_name,
        selection_type=_selection_type_name(loaded),
        trade_date=trade_date,
        sort_by=sort_by,
        sort_order=sort_order,
        candidate_count=len(candidates),
        candidates=candidates,
    )


def _validate_selection_backtest_inputs(
    *,
    connection,
    start_date: date,
    end_date: date,
    holding_period: int,
    selection_top_n: int | None,
) -> list[date]:
    if holding_period < 1:
        raise StrategyRuntimeError("Holding period must be at least 1 trading day.")
    if selection_top_n is not None and selection_top_n < 1:
        raise StrategyRuntimeError("selection_top_n must be at least 1 when provided.")
    if start_date > end_date:
        raise StrategyRuntimeError("Backtest start_date must be on or before end_date.")

    validate_trade_date(connection, start_date, StrategyRuntimeError)
    validate_trade_date(connection, end_date, StrategyRuntimeError)
    trade_dates = load_trade_dates_in_range(connection, start_date, end_date)
    if not trade_dates:
        raise StrategyRuntimeError("No trading dates found in the requested backtest range.")
    return trade_dates


def _build_distribution(completed_returns: list[Decimal]) -> list[ReturnDistributionBucket]:
    buckets = [
        ("< -7.5%", lambda value: value < Decimal("-7.5")),
        ("-7.5% to -3.5%", lambda value: Decimal("-7.5") <= value < Decimal("-3.5")),
        ("-3.5% to 0%", lambda value: Decimal("-3.5") <= value < Decimal("0")),
        ("0% to 3.5%", lambda value: Decimal("0") <= value < Decimal("3.5")),
        ("3.5% to 7.5%", lambda value: Decimal("3.5") <= value < Decimal("7.5")),
        (">= 7.5%", lambda value: value >= Decimal("7.5")),
    ]
    return [
        ReturnDistributionBucket(
            label=label,
            count=sum(1 for value in completed_returns if predicate(value)),
        )
        for label, predicate in buckets
    ]


def _build_selection_backtest_summary(
    *,
    request: StockSelectionBacktestRequest,
    trade_days: int,
    details: list[StockSelectionBacktestDetail],
    rule_count: int,
) -> StockSelectionBacktestSummary:
    completed = [detail for detail in details if detail.status == "completed"]
    completed_returns = [detail.return_pct for detail in completed if detail.return_pct is not None]
    positive_count = sum(1 for value in completed_returns if value > 0)
    negative_count = sum(1 for value in completed_returns if value < 0)
    flat_count = sum(1 for value in completed_returns if value == 0)

    average_return_pct = None
    win_rate_pct = None
    if completed_returns:
        average_return_pct = quantize_pct(
            sum(completed_returns, start=Decimal("0")) / Decimal(len(completed_returns))
        )
        win_rate_pct = quantize_pct(
            Decimal(positive_count) * Decimal("100") / Decimal(len(completed_returns))
        )

    return StockSelectionBacktestSummary(
        start_date=request.start_date,
        end_date=request.end_date,
        holding_period=request.holding_period,
        trade_days=trade_days,
        rule_count=rule_count,
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


def run_loaded_strategy_selection_backtest(
    *,
    db_path: Path,
    loaded: LoadedStrategyConfig,
    start_date: date,
    end_date: date,
    holding_period: int | None = None,
    selection_top_n: int | None = None,
    progress_callback: Callable[[StockSelectionBacktestProgress], None] | None = None,
) -> StockSelectionBacktestReport:
    if loaded.config.backtest_selection is None and holding_period is None:
        raise StrategyRuntimeError(
            f"Strategy '{loaded.name}' does not define backtest_selection defaults."
        )
    defaults = loaded.config.backtest_selection
    effective_holding_period = (
        holding_period
        if holding_period is not None
        else defaults.holding_period
    )
    effective_selection_top_n = (
        selection_top_n
        if selection_top_n is not None
        else defaults.selection_top_n
    )
    sort_by, sort_order = _selection_sort(loaded)
    request = StockSelectionBacktestRequest(
        start_date=start_date,
        end_date=end_date,
        holding_period=effective_holding_period,
        rules=[],
        selection_sort_by=sort_by,
        selection_sort_order=sort_order,
        selection_top_n=effective_selection_top_n,
    )

    with connect(db_path) as connection:
        trade_dates = _validate_selection_backtest_inputs(
            connection=connection,
            start_date=start_date,
            end_date=end_date,
            holding_period=effective_holding_period,
            selection_top_n=effective_selection_top_n,
        )
        all_trade_dates = load_all_trade_dates(connection)
        trade_date_index = {trade_day: index for index, trade_day in enumerate(all_trade_dates)}
        raw_selection_map = _select_candidates_in_range_with_connection(
            connection=connection,
            start_date=start_date,
            end_date=end_date,
            loaded=loaded,
        )
        selection_batches: list[tuple[date, list[StrategySelectionCandidate]]] = []

        for current_trade_day, selection_date in enumerate(trade_dates, start=1):
            if progress_callback is not None:
                progress_callback(
                    StockSelectionBacktestProgress(
                        current_trade_day=current_trade_day,
                        total_trade_days=len(trade_dates),
                        selection_date=selection_date,
                    )
                )
            candidates = raw_selection_map.get(selection_date, [])
            if effective_selection_top_n is not None:
                candidates = candidates[:effective_selection_top_n]
            selection_batches.append((selection_date, candidates))

        relevant_symbols = {
            candidate.symbol
            for _, candidates in selection_batches
            for candidate in candidates
        }
        final_sell_index = max(
            (
                min(trade_date_index[selection_date] + 1 + effective_holding_period, len(all_trade_dates) - 1)
                for selection_date, candidates in selection_batches
                if candidates
            ),
            default=0,
        )
        price_cache = load_qfq_price_cache(
            connection=connection,
            symbols=relevant_symbols,
            start_trade_date=trade_dates[0],
            end_trade_date=all_trade_dates[final_sell_index],
        )
        details: list[StockSelectionBacktestDetail] = []

        for selection_date, candidates in selection_batches:
            selection_index = trade_date_index[selection_date]
            buy_index = selection_index + 1
            sell_index = buy_index + effective_holding_period
            buy_date = all_trade_dates[buy_index] if buy_index < len(all_trade_dates) else None
            sell_date = all_trade_dates[sell_index] if sell_index < len(all_trade_dates) else None

            for candidate in candidates:
                if buy_date is None:
                    details.append(
                        StockSelectionBacktestDetail(
                            symbol=candidate.symbol,
                            name=candidate.name,
                            selection_date=selection_date,
                            buy_date=None,
                            sell_date=None,
                            buy_price=None,
                            sell_price=None,
                            holding_period=effective_holding_period,
                            return_pct=None,
                            status="incomplete",
                            incomplete_reason="missing_next_trade_date",
                        )
                    )
                    continue

                buy_price, _ = price_cache.get((candidate.symbol, buy_date), (None, None))
                if buy_price is None:
                    details.append(
                        StockSelectionBacktestDetail(
                            symbol=candidate.symbol,
                            name=candidate.name,
                            selection_date=selection_date,
                            buy_date=buy_date,
                            sell_date=None,
                            buy_price=None,
                            sell_price=None,
                            holding_period=effective_holding_period,
                            return_pct=None,
                            status="incomplete",
                            incomplete_reason="missing_buy_bar",
                        )
                    )
                    continue

                if sell_date is None:
                    details.append(
                        StockSelectionBacktestDetail(
                            symbol=candidate.symbol,
                            name=candidate.name,
                            selection_date=selection_date,
                            buy_date=buy_date,
                            sell_date=None,
                            buy_price=buy_price,
                            sell_price=None,
                            holding_period=effective_holding_period,
                            return_pct=None,
                            status="incomplete",
                            incomplete_reason="insufficient_holding_window",
                        )
                    )
                    continue

                _, sell_price = price_cache.get((candidate.symbol, sell_date), (None, None))
                if sell_price is None:
                    details.append(
                        StockSelectionBacktestDetail(
                            symbol=candidate.symbol,
                            name=candidate.name,
                            selection_date=selection_date,
                            buy_date=buy_date,
                            sell_date=sell_date,
                            buy_price=buy_price,
                            sell_price=None,
                            holding_period=effective_holding_period,
                            return_pct=None,
                            status="incomplete",
                            incomplete_reason="missing_sell_bar",
                        )
                    )
                    continue

                return_pct = quantize_pct((sell_price - buy_price) * Decimal("100") / buy_price)
                details.append(
                    StockSelectionBacktestDetail(
                        symbol=candidate.symbol,
                        name=candidate.name,
                        selection_date=selection_date,
                        buy_date=buy_date,
                        sell_date=sell_date,
                        buy_price=buy_price,
                        sell_price=sell_price,
                        holding_period=effective_holding_period,
                        return_pct=return_pct,
                        status="completed",
                        incomplete_reason=None,
                    )
                )

    summary = _build_selection_backtest_summary(
        request=request,
        trade_days=len(trade_dates),
        details=details,
        rule_count=_rule_count(loaded),
    )
    return StockSelectionBacktestReport(
        request=request,
        summary=summary,
        details=details,
    )


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTIZE)


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
    if trade_days < 1 or initial_capital <= 0:
        return None
    ratio = float(ending_assets / initial_capital)
    annualized = (ratio ** (252 / trade_days) - 1) * 100
    return quantize_pct(Decimal(str(annualized)))


def _validate_strategy_backtest_inputs(
    *,
    connection,
    request: StrategyBacktestRequest,
) -> list[date]:
    if request.start_date > request.end_date:
        raise StrategyRuntimeError("Backtest start_date must be on or before end_date.")
    if request.holding_period < 1:
        raise StrategyRuntimeError("Holding period must be at least 1 trading day.")
    if request.max_positions < 1:
        raise StrategyRuntimeError("max_positions must be at least 1.")
    if request.max_daily_buys < 1:
        raise StrategyRuntimeError("max_daily_buys must be at least 1.")
    if request.initial_capital <= 0:
        raise StrategyRuntimeError("initial_capital must be greater than 0.")
    if request.position_size_pct <= 0 or request.position_size_pct > 100:
        raise StrategyRuntimeError(
            "position_size_pct must be greater than 0 and less than or equal to 100."
        )
    if request.take_profit_pct <= 0:
        raise StrategyRuntimeError("take_profit_pct must be greater than 0.")
    if request.stop_loss_pct >= 0:
        raise StrategyRuntimeError("stop_loss_pct must be less than 0.")
    if request.selection_top_n is not None and request.selection_top_n < 1:
        raise StrategyRuntimeError("selection_top_n must be at least 1 when provided.")
    if request.min_composite_score is not None and request.min_composite_score < 0:
        raise StrategyRuntimeError("min_composite_score must be non-negative when provided.")
    if request.min_score_gap is not None and request.min_score_gap < 0:
        raise StrategyRuntimeError("min_score_gap must be non-negative when provided.")

    validate_trade_date(connection, request.start_date, StrategyRuntimeError)
    validate_trade_date(connection, request.end_date, StrategyRuntimeError)
    trade_dates = load_trade_dates_in_range(connection, request.start_date, request.end_date)
    if not trade_dates:
        raise StrategyRuntimeError("No trading dates found in the requested backtest range.")
    return trade_dates


def _build_strategy_backtest_summary(
    *,
    request: StrategyBacktestRequest,
    trade_days: int,
    trades: list[StrategyBacktestTrade],
    current_cash_balance: Decimal,
    current_market_value: Decimal,
    rule_count: int,
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
            (trade.sell_amount or Decimal("0")) - trade.buy_amount
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
        rule_count=rule_count,
        selection_sort_by=request.selection_sort_by,
        selection_sort_order=request.selection_sort_order,
        selection_top_n=request.selection_top_n,
        min_composite_score=request.min_composite_score,
        min_score_gap=request.min_score_gap,
        market_filter_enabled=request.market_filter_enabled,
        intraday_stop_loss_enabled=request.intraday_stop_loss_enabled,
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


def run_loaded_strategy_backtest(
    *,
    db_path: Path,
    loaded: LoadedStrategyConfig,
    start_date: date,
    end_date: date,
    holding_period: int | None = None,
    progress_callback: Callable[[StrategyBacktestProgress], None] | None = None,
) -> StrategyBacktestReport:
    if loaded.config.strategy_backtest is None:
        raise StrategyRuntimeError(
            f"Strategy '{loaded.name}' does not define strategy_backtest defaults."
        )
    defaults = loaded.config.strategy_backtest
    sort_by, sort_order = _selection_sort(loaded)
    request = StrategyBacktestRequest(
        start_date=start_date,
        end_date=end_date,
        holding_period=holding_period if holding_period is not None else defaults.holding_period,
        rules=[],
        max_positions=defaults.max_positions,
        max_daily_buys=defaults.max_daily_buys,
        initial_capital=defaults.initial_capital,
        position_size_pct=defaults.position_size_pct,
        take_profit_pct=defaults.take_profit_pct,
        stop_loss_pct=defaults.stop_loss_pct,
        selection_sort_by=sort_by,
        selection_sort_order=sort_order,
        selection_top_n=defaults.selection_top_n,
        min_composite_score=defaults.min_composite_score,
        min_score_gap=defaults.min_score_gap,
        market_filter_enabled=bool(defaults.market_filter and defaults.market_filter.enabled),
        intraday_stop_loss_enabled=defaults.intraday_stop_loss_enabled,
    )

    with connect(db_path) as connection:
        trade_dates = _validate_strategy_backtest_inputs(connection=connection, request=request)
        all_trade_dates = load_all_trade_dates(connection)
        trade_date_index = {trade_day: index for index, trade_day in enumerate(all_trade_dates)}
        raw_selection_map = _select_candidates_in_range_with_connection(
            connection=connection,
            start_date=request.start_date,
            end_date=request.end_date,
            loaded=loaded,
        )
        candidate_selection_map: dict[date, list[StrategySelectionCandidate]] = {}
        for selection_date, selection_results in raw_selection_map.items():
            gated_results = _apply_strategy_score_gate(
                selection_results,
                min_composite_score=request.min_composite_score,
                min_score_gap=request.min_score_gap,
            )
            if request.selection_top_n is None:
                candidate_selection_map[selection_date] = gated_results
            else:
                candidate_selection_map[selection_date] = gated_results[: request.selection_top_n]

        relevant_symbols = {
            candidate.symbol
            for selection_results in candidate_selection_map.values()
            for candidate in selection_results
        }
        price_cache = load_qfq_price_cache(
            connection=connection,
            symbols=relevant_symbols,
            start_trade_date=trade_dates[0],
            end_trade_date=trade_dates[-1],
        )
        low_price_cache = (
            load_low_qfq_cache(
                connection=connection,
                symbols=relevant_symbols,
                start_trade_date=trade_dates[0],
                end_trade_date=trade_dates[-1],
            )
            if request.intraday_stop_loss_enabled
            else {}
        )
        market_regime_states = (
            load_market_regime_states(
                connection,
                start_date=request.start_date,
                end_date=request.end_date,
                config=defaults.market_filter,
            )
            if defaults.market_filter is not None and defaults.market_filter.enabled
            else {}
        )
        recent_signal_passes = (
            _build_recent_signal_filter_passes(
                selection_map=candidate_selection_map,
                trade_dates=trade_dates,
                all_trade_dates=all_trade_dates,
                trade_date_index=trade_date_index,
                price_cache=price_cache,
                holding_period=request.holding_period,
                recent_signal_window=defaults.market_filter.recent_signal_window,
                recent_signal_min_count=defaults.market_filter.recent_signal_min_count,
                min_recent_signal_win_rate=defaults.market_filter.min_recent_signal_win_rate,
                min_recent_signal_avg_return=defaults.market_filter.min_recent_signal_avg_return,
            )
            if defaults.market_filter is not None and defaults.market_filter.enabled
            else {}
        )
        selection_map: dict[date, list[StrategySelectionCandidate]] = {}
        for selection_date, candidates in candidate_selection_map.items():
            if market_regime_states and not market_regime_states.get(selection_date, None):
                selection_map[selection_date] = []
                continue
            if market_regime_states and not market_regime_states[selection_date].passed:
                selection_map[selection_date] = []
                continue
            if recent_signal_passes and not recent_signal_passes.get(selection_date, True):
                selection_map[selection_date] = []
                continue
            selection_map[selection_date] = candidates

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

                open_price, _ = _lookup_price(price_cache, position.symbol, current_date)

                holding_days = current_trade_index - position.buy_trade_index
                sell_amount = close_price * Decimal(position.shares)
                return_pct = quantize_pct(
                    (sell_amount - position.buy_amount) * Decimal("100") / position.buy_amount
                )

                exit_reason: str | None = None
                effective_sell_price = close_price
                effective_return_pct = return_pct

                intraday_stop_triggered = False
                if request.intraday_stop_loss_enabled:
                    low_price = low_price_cache.get((position.symbol, current_date))
                    if low_price is not None and position.buy_price > 0:
                        low_return_pct = (
                            (low_price - position.buy_price) * Decimal("100") / position.buy_price
                        )
                        if low_return_pct <= request.stop_loss_pct:
                            intraday_stop_triggered = True

                if intraday_stop_triggered:
                    # Conservative fill: the stop-loss threshold price, but if the day
                    # gapped below it (open < threshold) fill at the worse open price.
                    # Never assume a fill better than reality -> avoids backtest inflation.
                    stop_multiplier = Decimal("1") + request.stop_loss_pct / Decimal("100")
                    threshold_price = position.buy_price * stop_multiplier
                    if open_price is not None and open_price < threshold_price:
                        effective_sell_price = open_price
                    else:
                        effective_sell_price = threshold_price
                    effective_return_pct = quantize_pct(
                        (effective_sell_price - position.buy_price)
                        * Decimal("100")
                        / position.buy_price
                    )
                    exit_reason = "intraday_stop_loss"
                elif return_pct >= request.take_profit_pct:
                    exit_reason = "take_profit"
                elif return_pct <= request.stop_loss_pct:
                    exit_reason = "stop_loss"
                elif holding_days >= request.holding_period:
                    exit_reason = "holding_period"

                if exit_reason is None:
                    remaining_positions.append(position)
                    continue

                sell_amount = effective_sell_price * Decimal(position.shares)
                cash += sell_amount
                trades.append(
                    StrategyBacktestTrade(
                        symbol=position.symbol,
                        name=position.name,
                        selection_date=position.selection_date,
                        buy_date=position.buy_date,
                        sell_date=current_date,
                        buy_price=position.buy_price,
                        sell_price=effective_sell_price,
                        shares=position.shares,
                        lots=position.shares // 100,
                        buy_amount=_quantize_money(position.buy_amount),
                        sell_amount=_quantize_money(sell_amount),
                        holding_days=holding_days,
                        return_pct=effective_return_pct,
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
                max_position_amount = current_total_assets * request.position_size_pct / Decimal("100")
                lot_cost = buy_price * Decimal("100")
                max_lots_by_cash = int(cash // lot_cost)
                max_lots_by_position = int(max_position_amount // lot_cost)
                lots = min(max_lots_by_cash, max_lots_by_position)
                if lots < 1:
                    continue

                shares = lots * 100
                buy_amount = buy_price * Decimal(shares)
                cash -= buy_amount
                current_market_value += buy_price * Decimal(shares)
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
    summary = _build_strategy_backtest_summary(
        request=request,
        trade_days=len(trade_dates),
        trades=trades,
        current_cash_balance=cash,
        current_market_value=current_market_value,
        rule_count=_rule_count(loaded),
    )
    return StrategyBacktestReport(
        request=request,
        summary=summary,
        trades=trades,
        equity_curve=equity_curve,
    )


def _win_rate_pct(report: StrategyBacktestReport) -> Decimal | None:
    closed_trades = [trade for trade in report.trades if trade.status == "closed"]
    if not closed_trades:
        return None
    positive_trades = sum(
        1
        for trade in closed_trades
        if trade.return_pct is not None and trade.return_pct > 0
    )
    return (
        Decimal(positive_trades) * Decimal("100") / Decimal(len(closed_trades))
    ).quantize(Decimal("0.0001"))


def _summary_from_report(
    strategy_name: str,
    report: StrategyBacktestReport,
) -> StrategyHoldingPeriodSummary:
    return StrategyHoldingPeriodSummary(
        strategy_name=strategy_name,
        holding_period=report.request.holding_period,
        start_date=report.summary.start_date,
        end_date=report.summary.end_date,
        closed_trade_count=report.summary.closed_trade_count,
        open_position_count=report.summary.open_position_count,
        total_trade_count=report.summary.total_trade_count,
        realized_profit=report.summary.realized_profit,
        unrealized_profit=report.summary.unrealized_profit,
        current_total_assets=report.summary.current_total_assets,
        total_return_pct=report.summary.total_return_pct,
        annualized_return_pct=report.summary.annualized_return_pct,
        win_rate_pct=_win_rate_pct(report),
    )


def _serialize_scalar(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _serialize_summary(summary: StrategyHoldingPeriodSummary) -> dict[str, str | int | None]:
    return {
        key: _serialize_scalar(value)
        for key, value in asdict(summary).items()
    }


def run_loaded_strategy_holding_period_matrix(
    *,
    db_path: Path,
    loaded: LoadedStrategyConfig,
    start_date: date,
    end_date: date,
    holding_periods: list[int],
    results_dir: Path,
    progress_callback: Callable[[StrategyBacktestProgress], None] | None = None,
    experiment_callback: Callable[[int, StrategyBacktestReport], Any] | None = None,
) -> list[StrategyHoldingPeriodSummary]:
    if loaded.config.strategy_backtest is None:
        raise StrategyRuntimeError(
            f"Strategy '{loaded.name}' does not define strategy_backtest defaults."
        )
    results_dir.mkdir(parents=True, exist_ok=True)
    summaries: list[StrategyHoldingPeriodSummary] = []
    for holding_period in holding_periods:
        report = run_loaded_strategy_backtest(
            db_path=db_path,
            loaded=loaded,
            start_date=start_date,
            end_date=end_date,
            holding_period=holding_period,
            progress_callback=progress_callback,
        )
        experiment_result = (
            experiment_callback(holding_period, report)
            if experiment_callback is not None
            else None
        )
        summary = _summary_from_report(loaded.name, report)
        summary_path = results_dir / f"holding_period_{holding_period}.json"
        payload = {
            "summary": _serialize_summary(summary),
            "request": {
                "strategy_name": loaded.name,
                "holding_period": holding_period,
            },
        }
        if experiment_result is not None:
            payload["experiment"] = {
                "run_id": getattr(experiment_result, "run_id", None),
                "score": getattr(experiment_result, "score", None),
                "score_version": getattr(experiment_result, "score_version", None),
            }
        summary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        summaries.append(summary)
    return summaries


def format_loaded_strategy_holding_period_table(
    summaries: list[StrategyHoldingPeriodSummary],
) -> str:
    return format_strategy_holding_period_table(summaries)


__all__ = [
    "PatternSelectionError",
    "StrategyRuntimeError",
    "StrategySelectionCandidate",
    "StrategySelectionView",
    "format_loaded_strategy_holding_period_table",
    "run_loaded_strategy_backtest",
    "run_loaded_strategy_holding_period_matrix",
    "run_loaded_strategy_selection_backtest",
    "run_strategy_selection",
    "strategy_label",
]
