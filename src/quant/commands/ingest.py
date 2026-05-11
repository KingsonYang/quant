from __future__ import annotations

from datetime import date
from pathlib import Path

import typer

from quant.db.init_db import DEFAULT_DB_PATH
from quant.ingest.service import (
    ingest_baidu_from_symbol_file,
    ingest_baidu_incremental_from_symbol_file,
    ingest_baidu_symbol,
    ingest_baidu_symbol_incremental,
)


DEFAULT_SYMBOLS_FILE = Path("docs") / "股票代码.md"


def _optional_trade_date_filter(trade_date: str | None) -> frozenset[date] | None:
    if trade_date is None:
        return None
    return frozenset({date.fromisoformat(trade_date)})


def register(app: typer.Typer) -> None:
    @app.command("ingest-baidu")
    def ingest_baidu_command(
        symbol: str = typer.Argument(..., help="A-share symbol such as SZ000001."),
        db_path: Path = typer.Option(
            DEFAULT_DB_PATH,
            "--db-path",
            help="Target DuckDB file path.",
        ),
        company_name: str | None = typer.Option(
            None,
            "--company-name",
            help="Optional company name used when seeding security_master.",
        ),
        trade_date: str | None = typer.Option(
            None,
            "--trade-date",
            help="Replace daily_bar for this YYYY-MM-DD only; leaves other dates untouched; skips event_record.",
        ),
    ) -> None:
        """Fetch, parse, normalize, and ingest a single Baidu JSONP payload."""
        summary = ingest_baidu_symbol(
            symbol=symbol,
            db_path=db_path,
            company_name=company_name,
            only_trade_dates=_optional_trade_date_filter(trade_date),
        )
        typer.echo(f"Ingested symbol: {summary.symbol}")
        typer.echo(f"Security ID: {summary.security_id}")
        typer.echo(f"Raw requests written: {summary.raw_request_written}")
        typer.echo(f"Raw responses written: {summary.raw_response_written}")
        typer.echo(f"Daily bars written: {summary.daily_bars_written}")
        typer.echo(f"Events written: {summary.events_written}")

    @app.command("ingest-baidu-incremental")
    def ingest_baidu_incremental_command(
        symbol: str = typer.Argument(..., help="A-share symbol such as SZ000001."),
        db_path: Path = typer.Option(
            DEFAULT_DB_PATH,
            "--db-path",
            help="Target DuckDB file path.",
        ),
        company_name: str | None = typer.Option(
            None,
            "--company-name",
            help="Optional company name used when seeding security_master.",
        ),
        trade_date: str | None = typer.Option(
            None,
            "--trade-date",
            help="Replace daily_bar for this YYYY-MM-DD only; ignores incremental append mode.",
        ),
    ) -> None:
        """Fetch and append only bars newer than the latest stored trade date for one symbol."""
        summary = ingest_baidu_symbol_incremental(
            symbol=symbol,
            db_path=db_path,
            company_name=company_name,
            only_trade_dates=_optional_trade_date_filter(trade_date),
        )
        typer.echo(f"Incrementally ingested symbol: {summary.symbol}")
        typer.echo(f"Security ID: {summary.security_id}")
        typer.echo(f"Raw requests written: {summary.raw_request_written}")
        typer.echo(f"Raw responses written: {summary.raw_response_written}")
        typer.echo(f"Daily bars written: {summary.daily_bars_written}")
        typer.echo(f"Events written: {summary.events_written}")

    @app.command("ingest-baidu-all")
    def ingest_baidu_all_command(
        symbols_file: Path = typer.Option(
            DEFAULT_SYMBOLS_FILE,
            "--symbols-file",
            help="Markdown file containing stock symbols and company names.",
        ),
        db_path: Path = typer.Option(
            DEFAULT_DB_PATH,
            "--db-path",
            help="Target DuckDB file path.",
        ),
        limit: int | None = typer.Option(
            None,
            "--limit",
            min=1,
            help="Optional cap on how many symbols to import for a single run.",
        ),
        fail_fast: bool = typer.Option(
            False,
            "--fail-fast",
            help="Stop immediately when one symbol import fails.",
        ),
        quiet: bool = typer.Option(
            False,
            "--quiet",
            help="Disable per-symbol progress logs.",
        ),
        trade_date: str | None = typer.Option(
            None,
            "--trade-date",
            help="Replace daily_bar for this YYYY-MM-DD only for each symbol; skips event_record.",
        ),
    ) -> None:
        """Import all symbols from a markdown stock list file."""
        summary = ingest_baidu_from_symbol_file(
            symbols_file=symbols_file,
            db_path=db_path,
            limit=limit,
            continue_on_error=not fail_fast,
            progress_callback=None if quiet else typer.echo,
            only_trade_dates=_optional_trade_date_filter(trade_date),
        )
        typer.echo(f"Symbols processed: {summary.total_symbols}")
        typer.echo(f"Succeeded: {summary.succeeded_symbols}")
        typer.echo(f"Failed: {summary.failed_symbols}")
        typer.echo(f"Raw requests written: {summary.raw_requests_written}")
        typer.echo(f"Raw responses written: {summary.raw_responses_written}")
        typer.echo(f"Daily bars written: {summary.daily_bars_written}")
        typer.echo(f"Events written: {summary.events_written}")

    @app.command("ingest-baidu-all-incremental")
    def ingest_baidu_all_incremental_command(
        symbols_file: Path = typer.Option(
            DEFAULT_SYMBOLS_FILE,
            "--symbols-file",
            help="Markdown file containing stock symbols and company names.",
        ),
        db_path: Path = typer.Option(
            DEFAULT_DB_PATH,
            "--db-path",
            help="Target DuckDB file path.",
        ),
        limit: int | None = typer.Option(
            None,
            "--limit",
            min=1,
            help="Optional cap on how many symbols to import for a single run.",
        ),
        fail_fast: bool = typer.Option(
            False,
            "--fail-fast",
            help="Stop immediately when one symbol import fails.",
        ),
        quiet: bool = typer.Option(
            False,
            "--quiet",
            help="Disable per-symbol progress logs.",
        ),
        trade_date: str | None = typer.Option(
            None,
            "--trade-date",
            help="Replace daily_bar for this YYYY-MM-DD only for each symbol; skips event_record.",
        ),
    ) -> None:
        """Import all symbols by appending only bars newer than each symbol's max trade_date."""
        summary = ingest_baidu_incremental_from_symbol_file(
            symbols_file=symbols_file,
            db_path=db_path,
            limit=limit,
            continue_on_error=not fail_fast,
            progress_callback=None if quiet else typer.echo,
            only_trade_dates=_optional_trade_date_filter(trade_date),
        )
        typer.echo(f"Symbols processed: {summary.total_symbols}")
        typer.echo(f"Succeeded: {summary.succeeded_symbols}")
        typer.echo(f"Failed: {summary.failed_symbols}")
        typer.echo(f"Raw requests written: {summary.raw_requests_written}")
        typer.echo(f"Raw responses written: {summary.raw_responses_written}")
        typer.echo(f"Daily bars written: {summary.daily_bars_written}")
        typer.echo(f"Events written: {summary.events_written}")
