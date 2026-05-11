from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import typer

from quant.backtest import StockSelectionBacktestRequest, run_stock_selection_backtest
from quant.commands.common import (
    LEGACY_EXCEPTIONS,
    LEGACY_HELP_SUFFIX,
    echo_selection_backtest_report,
    echo_selection_summary,
    echo_strategy_backtest_report,
    resolve_selection_rules,
)
from quant.db.init_db import DEFAULT_DB_PATH
from quant.selection import select_stocks
from quant.strategy_backtest import StrategyBacktestRequest, run_strategy_backtest
from quant.web import serve as serve_web


def register(app: typer.Typer) -> None:
    @app.command("web")
    def web_command(
        host: str = typer.Option(
            "127.0.0.1",
            "--host",
            help=f"Host address for the legacy backtest web app. {LEGACY_HELP_SUFFIX}",
        ),
        port: int = typer.Option(
            8000,
            "--port",
            min=1,
            max=65535,
            help=f"Port for the legacy backtest web app. {LEGACY_HELP_SUFFIX}",
        ),
        db_path: Path = typer.Option(
            DEFAULT_DB_PATH,
            "--db-path",
            help=f"Target DuckDB file path used by the legacy web app. {LEGACY_HELP_SUFFIX}",
        ),
    ) -> None:
        """Serve the legacy backtest web pages. Prefer CLI strategy workflows."""
        serve_web(host=host, port=port, default_db_path=db_path)

    @app.command("select-stocks")
    def select_stocks_command(
        trade_date: str = typer.Option(
            ...,
            "--trade-date",
            help=f"Target trading date in YYYY-MM-DD format. {LEGACY_HELP_SUFFIX}",
        ),
        rules_json: str | None = typer.Option(
            None,
            "--rules-json",
            help="JSON array of selection rules.",
        ),
        rules_file: Path | None = typer.Option(
            None,
            "--rules-file",
            help="Path to a JSON file containing the selection rules array.",
        ),
        logic: str = typer.Option(
            "AND",
            "--logic",
            help="Rule combination logic. Only AND is supported in v1.",
        ),
        sort_by: str = typer.Option(
            "symbol",
            "--sort-by",
            help="Sort field for result ordering.",
        ),
        sort_order: str = typer.Option(
            "ASC",
            "--sort-order",
            help="Sort order, ASC or DESC.",
        ),
        db_path: Path = typer.Option(
            DEFAULT_DB_PATH,
            "--db-path",
            help="Target DuckDB file path.",
        ),
    ) -> None:
        """Legacy selection command. Prefer `quant run select <strategy>`."""
        try:
            summary = select_stocks(
                db_path=db_path,
                trade_date=date.fromisoformat(trade_date),
                rules=resolve_selection_rules(
                    rules_json,
                    rules_file,
                    error_type=ValueError,
                ),
                logic=logic,
                sort_by=sort_by,
                sort_order=sort_order,
            )
        except LEGACY_EXCEPTIONS as exc:
            raise typer.BadParameter(str(exc)) from exc

        echo_selection_summary(summary)

    @app.command("backtest-selection")
    def backtest_selection_command(
        start_date: str = typer.Option(
            ...,
            "--start-date",
            help=f"Backtest start trading date in YYYY-MM-DD format. {LEGACY_HELP_SUFFIX}",
        ),
        end_date: str = typer.Option(
            ...,
            "--end-date",
            help="Backtest end trading date in YYYY-MM-DD format.",
        ),
        holding_period: int = typer.Option(
            ...,
            "--holding-period",
            min=1,
            help="Holding period measured in trading days after the buy date.",
        ),
        rules_json: str | None = typer.Option(
            None,
            "--rules-json",
            help="JSON array of selection rules.",
        ),
        rules_file: Path | None = typer.Option(
            None,
            "--rules-file",
            help="Path to a JSON file containing the selection rules array.",
        ),
        logic: str = typer.Option(
            "AND",
            "--logic",
            help="Rule combination logic. Only AND is supported in v1.",
        ),
        selection_sort_by: str = typer.Option(
            "symbol",
            "--selection-sort-by",
            help="Daily stock selection sort field applied before backtest detail generation.",
        ),
        selection_sort_order: str = typer.Option(
            "ASC",
            "--selection-sort-order",
            help="Daily stock selection sort order, ASC or DESC.",
        ),
        selection_top_n: int | None = typer.Option(
            None,
            "--selection-top-n",
            min=1,
            help="Only include the top N stocks from each daily selection result in the backtest.",
        ),
        show_progress: bool = typer.Option(
            False,
            "--show-progress",
            help="Print per-trading-day progress while the backtest is running.",
        ),
        db_path: Path = typer.Option(
            DEFAULT_DB_PATH,
            "--db-path",
            help="Target DuckDB file path.",
        ),
    ) -> None:
        """Legacy selection backtest. Prefer `quant run backtest-selection <strategy>`."""
        try:
            report = run_stock_selection_backtest(
                db_path=db_path,
                request=StockSelectionBacktestRequest(
                    start_date=date.fromisoformat(start_date),
                    end_date=date.fromisoformat(end_date),
                    holding_period=holding_period,
                    rules=resolve_selection_rules(
                        rules_json,
                        rules_file,
                        error_type=ValueError,
                    ),
                    logic=logic,
                    selection_sort_by=selection_sort_by,
                    selection_sort_order=selection_sort_order,
                    selection_top_n=selection_top_n,
                ),
                progress_callback=(
                    (lambda progress: typer.echo(
                        f"[{progress.current_trade_day}/{progress.total_trade_days}] selection_date={progress.selection_date}"
                    ))
                    if show_progress
                    else None
                ),
            )
        except LEGACY_EXCEPTIONS as exc:
            raise typer.BadParameter(str(exc)) from exc

        echo_selection_backtest_report(report)

    @app.command("backtest-strategy")
    def backtest_strategy_command(
        start_date: str = typer.Option(
            ...,
            "--start-date",
            help=f"Strategy backtest start trading date in YYYY-MM-DD format. {LEGACY_HELP_SUFFIX}",
        ),
        end_date: str = typer.Option(
            ...,
            "--end-date",
            help="Strategy backtest end trading date in YYYY-MM-DD format.",
        ),
        holding_period: int = typer.Option(
            ...,
            "--holding-period",
            min=1,
            help="Maximum holding period measured in trading days after the buy date.",
        ),
        max_positions: int = typer.Option(
            ...,
            "--max-positions",
            min=1,
            help="Maximum number of stocks that can be held at the same time.",
        ),
        max_daily_buys: int = typer.Option(
            ...,
            "--max-daily-buys",
            min=1,
            help="Maximum number of new positions opened in a single trading day.",
        ),
        initial_capital: str = typer.Option(
            ...,
            "--initial-capital",
            help="Initial capital for the strategy account.",
        ),
        position_size_pct: str = typer.Option(
            ...,
            "--position-size-pct",
            help="Maximum capital percentage allowed for a single position, for example 50 means 50%%.",
        ),
        take_profit_pct: str = typer.Option(
            ...,
            "--take-profit-pct",
            help="Take profit threshold in percentage points, for example 8 means 8%%.",
        ),
        stop_loss_pct: str = typer.Option(
            ...,
            "--stop-loss-pct",
            help="Stop loss threshold in percentage points, for example -5 means -5%%.",
        ),
        rules_json: str | None = typer.Option(
            None,
            "--rules-json",
            help="JSON array of selection rules.",
        ),
        rules_file: Path | None = typer.Option(
            None,
            "--rules-file",
            help="Path to a JSON file containing the selection rules array.",
        ),
        logic: str = typer.Option(
            "AND",
            "--logic",
            help="Rule combination logic. Only AND is supported in v1.",
        ),
        selection_sort_by: str = typer.Option(
            "symbol",
            "--selection-sort-by",
            help="Daily stock selection sort field applied before strategy execution.",
        ),
        selection_sort_order: str = typer.Option(
            "ASC",
            "--selection-sort-order",
            help="Daily stock selection sort order, ASC or DESC.",
        ),
        selection_top_n: int | None = typer.Option(
            None,
            "--selection-top-n",
            min=1,
            help="Only include the top N stocks from each daily selection result in the strategy backtest.",
        ),
        show_progress: bool = typer.Option(
            False,
            "--show-progress",
            help="Print per-trading-day progress while the strategy backtest is running.",
        ),
        db_path: Path = typer.Option(
            DEFAULT_DB_PATH,
            "--db-path",
            help="Target DuckDB file path.",
        ),
    ) -> None:
        """Legacy strategy backtest. Prefer `quant run backtest-strategy <strategy>`."""
        try:
            report = run_strategy_backtest(
                db_path=db_path,
                request=StrategyBacktestRequest(
                    start_date=date.fromisoformat(start_date),
                    end_date=date.fromisoformat(end_date),
                    holding_period=holding_period,
                    rules=resolve_selection_rules(
                        rules_json,
                        rules_file,
                        error_type=ValueError,
                    ),
                    max_positions=max_positions,
                    max_daily_buys=max_daily_buys,
                    initial_capital=Decimal(initial_capital),
                    position_size_pct=Decimal(position_size_pct),
                    take_profit_pct=Decimal(take_profit_pct),
                    stop_loss_pct=Decimal(stop_loss_pct),
                    logic=logic,
                    selection_sort_by=selection_sort_by,
                    selection_sort_order=selection_sort_order,
                    selection_top_n=selection_top_n,
                ),
                progress_callback=(
                    (lambda progress: typer.echo(
                        f"[{progress.current_trade_day}/{progress.total_trade_days}] trade_date={progress.trade_date}"
                    ))
                    if show_progress
                    else None
                ),
            )
        except LEGACY_EXCEPTIONS as exc:
            raise typer.BadParameter(str(exc)) from exc

        echo_strategy_backtest_report(report)
