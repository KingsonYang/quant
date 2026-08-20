from __future__ import annotations

from datetime import date
from pathlib import Path

import typer

from quant.backtest import StockSelectionBacktestError, StockSelectionBacktestReport
from quant.selection import SelectionSummary, StockSelectionError, load_selection_rules_file, parse_selection_rules
from quant.strategy_backtest import StrategyBacktestError, StrategyBacktestReport


def parse_iso_date(label: str, value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{label} must be in YYYY-MM-DD format.") from exc


def resolve_selection_rules(
    rules_json: str | None,
    rules_file: Path | None,
    *,
    error_type: type[ValueError],
) -> list:
    if (rules_json is None) == (rules_file is None):
        raise error_type("Provide exactly one of --rules-json or --rules-file.")
    return (
        parse_selection_rules(rules_json)
        if rules_json is not None
        else load_selection_rules_file(rules_file)
    )


def echo_selection_summary(summary: SelectionSummary) -> None:
    typer.echo(f"Trade date: {summary.trade_date}")
    typer.echo(f"Logic: {summary.logic}")
    typer.echo(f"Rules: {summary.rule_count}")
    typer.echo(f"Sort by: {summary.sort_by} {summary.sort_order}")
    typer.echo(f"Matched securities: {summary.result_count}")
    for row in summary.results:
        typer.echo(
            f"{row.symbol}\t{row.name}\tclose={row.close_price}\tvolume={row.volume}\tamount={row.amount}\t"
            f"change_pct={row.change_pct}\tturnover_ratio={row.turnover_ratio}\t"
            f"prev_5d_avg_volume={row.prev_5d_avg_volume}\t"
            f"prev_5d_sum_volume={row.prev_5d_sum_volume}\t"
            f"last_5d_avg_volume={row.last_5d_avg_volume}\t"
            f"last_5d_sum_volume={row.last_5d_sum_volume}\t"
            f"volume_plus_prev_5d_sum={row.volume_plus_prev_5d_sum}\t"
            f"volume_plus_last_5d_sum={row.volume_plus_last_5d_sum}\t"
            f"prev_5d_avg_amount={row.prev_5d_avg_amount}\t"
            f"prev_5d_sum_amount={row.prev_5d_sum_amount}\t"
            f"last_5d_avg_amount={row.last_5d_avg_amount}\t"
            f"last_5d_sum_amount={row.last_5d_sum_amount}\t"
            f"amount_plus_prev_5d_sum={row.amount_plus_prev_5d_sum}\t"
            f"amount_plus_last_5d_sum={row.amount_plus_last_5d_sum}"
        )


def echo_selection_backtest_report(report: StockSelectionBacktestReport) -> None:
    typer.echo(f"Backtest range: {report.summary.start_date} -> {report.summary.end_date}")
    typer.echo(f"Holding period: {report.summary.holding_period}")
    typer.echo(
        f"Selection sort: {report.summary.selection_sort_by} {report.summary.selection_sort_order}"
    )
    typer.echo(f"Selection top N: {report.summary.selection_top_n}")
    typer.echo(f"Trade days: {report.summary.trade_days}")
    typer.echo(f"Rule count: {report.summary.rule_count}")
    typer.echo(f"Total hits: {report.summary.hit_count}")
    typer.echo(f"Completed trades: {report.summary.completed_count}")
    typer.echo(f"Incomplete trades: {report.summary.incomplete_count}")
    typer.echo(f"Positive trades: {report.summary.positive_count}")
    typer.echo(f"Negative trades: {report.summary.negative_count}")
    typer.echo(f"Flat trades: {report.summary.flat_count}")
    typer.echo(f"Win rate (%): {report.summary.win_rate_pct}")
    typer.echo(f"Average return (%): {report.summary.average_return_pct}")
    typer.echo("Return distribution:")
    for bucket in report.summary.return_distribution:
        typer.echo(f"{bucket.label}: {bucket.count}")

    for detail in report.details:
        typer.echo(
            f"{detail.symbol}\t{detail.name}\tselection_date={detail.selection_date}\t"
            f"buy_date={detail.buy_date}\tsell_date={detail.sell_date}\t"
            f"buy_price={detail.buy_price}\tsell_price={detail.sell_price}\t"
            f"return_pct={detail.return_pct}\tstatus={detail.status}\t"
            f"incomplete_reason={detail.incomplete_reason}"
        )


def echo_strategy_backtest_report(report: StrategyBacktestReport) -> None:
    typer.echo(f"Strategy backtest range: {report.summary.start_date} -> {report.summary.end_date}")
    typer.echo(f"Holding period: {report.summary.holding_period}")
    typer.echo(f"Selection sort: {report.summary.selection_sort_by} {report.summary.selection_sort_order}")
    typer.echo(f"Selection top N: {report.summary.selection_top_n}")
    typer.echo(f"Min composite score: {report.summary.min_composite_score}")
    typer.echo(f"Min score gap: {report.summary.min_score_gap}")
    typer.echo(f"Market filter enabled: {report.summary.market_filter_enabled}")
    typer.echo(f"Intraday stop loss enabled: {report.summary.intraday_stop_loss_enabled}")
    typer.echo(f"Max positions: {report.summary.max_positions}")
    typer.echo(f"Max daily buys: {report.summary.max_daily_buys}")
    typer.echo(f"Initial capital: {report.summary.initial_capital}")
    typer.echo(f"Position size (%): {report.summary.position_size_pct}")
    typer.echo(f"Take profit (%): {report.summary.take_profit_pct}")
    typer.echo(f"Stop loss (%): {report.summary.stop_loss_pct}")
    typer.echo(f"Trade days: {report.summary.trade_days}")
    typer.echo(f"Closed trades: {report.summary.closed_trade_count}")
    typer.echo(f"Open positions: {report.summary.open_position_count}")
    typer.echo(f"Current cash balance: {report.summary.current_cash_balance}")
    typer.echo(f"Current market value: {report.summary.current_market_value}")
    typer.echo(f"Current total assets: {report.summary.current_total_assets}")
    typer.echo(f"Realized profit: {report.summary.realized_profit}")
    typer.echo(f"Unrealized profit: {report.summary.unrealized_profit}")
    typer.echo(f"Total return (%): {report.summary.total_return_pct}")
    typer.echo(f"Annualized return (%): {report.summary.annualized_return_pct}")
    typer.echo(f"Transaction costs included: {report.summary.transaction_costs_included}")

    for trade in report.trades:
        typer.echo(
            f"{trade.symbol}\t{trade.name}\tselection_date={trade.selection_date}\t"
            f"buy_date={trade.buy_date}\tsell_date={trade.sell_date}\t"
            f"buy_price={trade.buy_price}\tsell_price={trade.sell_price}\t"
            f"shares={trade.shares}\tlots={trade.lots}\tbuy_amount={trade.buy_amount}\t"
            f"sell_amount={trade.sell_amount}\tholding_days={trade.holding_days}\t"
            f"return_pct={trade.return_pct}\tstatus={trade.status}\texit_reason={trade.exit_reason}"
        )


LEGACY_HELP_SUFFIX = "Legacy command. Prefer `quant run ...` with a YAML strategy."


LEGACY_EXCEPTIONS = (
    ValueError,
    StockSelectionError,
    StockSelectionBacktestError,
    StrategyBacktestError,
)
