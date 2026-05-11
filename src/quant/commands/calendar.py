from __future__ import annotations

from pathlib import Path

import typer

from quant.db.init_db import DEFAULT_DB_PATH, DEFAULT_SCHEMA_PATH, init_duckdb
from quant.db.trading_calendar import build_trading_calendar, build_trading_calendar_incremental


def register(app: typer.Typer) -> None:
    @app.command("init-db")
    def init_db_command(
        db_path: Path = typer.Option(
            DEFAULT_DB_PATH,
            "--db-path",
            help="Target DuckDB file path.",
        ),
        schema_path: Path = typer.Option(
            DEFAULT_SCHEMA_PATH,
            "--schema-path",
            help="Schema SQL file path.",
        ),
    ) -> None:
        """Initialize the local DuckDB database from schema_v1.sql."""
        result = init_duckdb(db_path=db_path, schema_path=schema_path)
        typer.echo(f"Initialized DuckDB: {result.db_path}")
        typer.echo(f"Applied schema: {result.schema_path}")
        typer.echo(f"Statements executed: {result.statements_executed}")

    @app.command("build-trading-calendar")
    def build_trading_calendar_command(
        db_path: Path = typer.Option(
            DEFAULT_DB_PATH,
            "--db-path",
            help="Target DuckDB file path.",
        ),
    ) -> None:
        """Rebuild trading_calendar from distinct daily_bar trade dates."""
        summary = build_trading_calendar(db_path=db_path)
        typer.echo(f"Mode: {summary.mode}")
        typer.echo(f"Market: {summary.market}")
        typer.echo(f"Scanned trade dates: {summary.scanned_trade_dates}")
        typer.echo(f"Calendar rows written: {summary.calendar_rows_written}")
        typer.echo(f"Calendar rows updated: {summary.calendar_rows_updated}")

    @app.command("build-trading-calendar-incremental")
    def build_trading_calendar_incremental_command(
        db_path: Path = typer.Option(
            DEFAULT_DB_PATH,
            "--db-path",
            help="Target DuckDB file path.",
        ),
    ) -> None:
        """Append missing trading dates from daily_bar into trading_calendar."""
        summary = build_trading_calendar_incremental(db_path=db_path)
        typer.echo(f"Mode: {summary.mode}")
        typer.echo(f"Market: {summary.market}")
        typer.echo(f"Scanned trade dates: {summary.scanned_trade_dates}")
        typer.echo(f"Calendar rows written: {summary.calendar_rows_written}")
        typer.echo(f"Calendar rows updated: {summary.calendar_rows_updated}")
