from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import typer

from quant.db.init_db import DEFAULT_DB_PATH
from quant.ingest.service import (
    ingest_baidu_from_symbol_file,
    ingest_baidu_incremental_from_symbol_file,
    ingest_baidu_symbol,
    ingest_baidu_symbol_incremental,
)
from quant.ingest.ths import ThsIngestionError, ingest_ths_xls
from quant.ingest.tdxquant import backfill_prices, ingest_tdxquant


DEFAULT_SYMBOLS_FILE = Path("docs") / "股票代码.md"


def _optional_trade_date_filter(trade_date: str | None) -> frozenset[date] | None:
    if trade_date is None:
        return None
    return frozenset({date.fromisoformat(trade_date)})


def _optional_date(value: str | None) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(value)


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

    @app.command("ingest-ths-xls")
    def ingest_ths_xls_command(
        xls_file: Path = typer.Argument(
            ...,
            help="THS exported pseudo-xls TSV file named YYYYMMDD.xls.",
        ),
        db_path: Path = typer.Option(
            DEFAULT_DB_PATH,
            "--db-path",
            help="Target DuckDB file path.",
        ),
        quiet: bool = typer.Option(
            False,
            "--quiet",
            help="Only print the final summary.",
        ),
        derive_date: str | None = typer.Option(
            None,
            "--derive-date",
            help="Refresh daily_bar MA fields for this YYYY-MM-DD only; defaults to the xls file date.",
        ),
        derive_start_date: str | None = typer.Option(
            None,
            "--derive-start-date",
            help="Refresh daily_bar MA fields from this YYYY-MM-DD.",
        ),
        derive_end_date: str | None = typer.Option(
            None,
            "--derive-end-date",
            help="Refresh daily_bar MA fields through this YYYY-MM-DD.",
        ),
    ) -> None:
        """Import a THS exported YYYYMMDD.xls snapshot and fill missing daily_bar rows."""
        if derive_date is not None and (derive_start_date is not None or derive_end_date is not None):
            raise typer.BadParameter("--derive-date cannot be combined with --derive-start-date/--derive-end-date.")
        if derive_date is not None:
            derived_start = _optional_date(derive_date)
            derived_end = derived_start
        else:
            derived_start = _optional_date(derive_start_date)
            derived_end = _optional_date(derive_end_date)
        try:
            summary = ingest_ths_xls(
                xls_file=xls_file,
                db_path=db_path,
                derived_start_date=derived_start,
                derived_end_date=derived_end,
            )
        except ThsIngestionError as exc:
            raise typer.BadParameter(str(exc)) from exc
        if not quiet:
            typer.echo(f"THS file: {summary.source_file}")
            typer.echo(f"Trade date: {summary.trade_date}")
            typer.echo(
                f"Derived MA refresh range: {summary.derived_start_date} -> {summary.derived_end_date}"
            )
        typer.echo(f"Parsed rows: {summary.parsed_rows}")
        typer.echo(f"Snapshot rows written: {summary.snapshot_rows_written}")
        typer.echo(f"Securities created: {summary.securities_created}")
        typer.echo(f"Daily bars written: {summary.daily_bars_written}")
        typer.echo(f"Derived MA rows refreshed: {summary.derived_rows_refreshed}")
        typer.echo(f"Invalid rows: {summary.invalid_rows}")
        typer.echo(f"Skipped daily bar rows: {summary.skipped_daily_bar_rows}")
        typer.echo(f"Calendar rows written: {summary.calendar_rows_written}")
        typer.echo(f"Calendar rows updated: {summary.calendar_rows_updated}")

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

    @app.command("ingest-tdxquant")
    def ingest_tdxquant_command(
        db_path: Path = typer.Option(
            DEFAULT_DB_PATH,
            "--db-path",
            help="Target DuckDB file path.",
        ),
        days: int = typer.Option(
            5,
            "--days",
            min=1,
            help="Load the most recent N days when --start/--end are not provided.",
        ),
        start: str | None = typer.Option(
            None,
            "--start",
            help="Start date YYYY-MM-DD.",
        ),
        end: str | None = typer.Option(
            None,
            "--end",
            help="End date YYYY-MM-DD.",
        ),
        stock: str | None = typer.Option(
            None,
            "--stock",
            help="Restrict to a single symbol such as 600000.SH.",
        ),
        test_only: bool = typer.Option(
            False,
            "--test-only",
            help="Fetch and print without writing to daily_bar.",
        ),
        include_all: bool = typer.Option(
            False,
            "--include-all",
            help="除沪深主板外，一并拉取创业板和科创板。默认只拉通达信的上证主板和深证主板。",
        ),
        tqcenter_path: Path | None = typer.Option(
            None,
            "--tqcenter-path",
            help="tqcenter.py 路径。默认读 QUANT_TQCENTER_PATH 或 config/local.yml。",
        ),
        target_table: str = typer.Option(
            "daily_bar",
            "--target-table",
            help='写入目标表，默认 daily_bar；可指定临时表用于数据源对比。',
        ),
        skip_hsl: bool = typer.Option(
            False,
            "--skip-hsl/--fetch-hsl",
            help="Skip HSL (turnover rate) fetch for speed. Default: fetch (ingest is small range).",
        ),
        batch_size: int = typer.Option(
            50,
            "--batch-size",
            min=1,
            max=500,
            help="Number of stocks per batch API call. Default: 50.",
        ),
    ) -> None:
        """Incremental ingest from TDXQuant; writes back to daily_bar as a supplement to baidu/ths."""
        if start is not None and end is not None:
            start_date = start
            end_date = end
        else:
            end_dt = date.today()
            start_dt = end_dt - timedelta(days=days)
            end_date = end_dt.isoformat()
            start_date = start_dt.isoformat()

        from quant.ingest.tdxquant import TdxQuantIngestionService
        service = TdxQuantIngestionService(
            db_path=db_path,
            tqcenter_path=tqcenter_path,
            target_table=target_table,
        )
        summary = service.ingest(
            start_date=start_date,
            end_date=end_date,
            stock=stock,
            only_main_board=not include_all,
            test_only=test_only,
            skip_hsl=skip_hsl,
            batch_size=batch_size,
        )

        typer.echo(f"TDXQuant range: {summary.start_date} ~ {summary.end_date}")
        typer.echo(f"Target table: {target_table}")
        typer.echo(f"Stocks processed: {summary.stock_count}")
        typer.echo(f"Fetched rows: {summary.fetched}")
        typer.echo(f"Inserted rows: {summary.inserted}")
        typer.echo(f"Skipped (baidu/ths already covered): {summary.skipped_existing}")
        if summary.fetched > 0 and summary.inserted == 0 and not test_only:
            typer.echo("Note: all fetched rows were already covered by baidu/ths.")
        typer.echo(f"Skipped empty: {summary.skipped_empty}")
        typer.echo(f"Errors: {summary.errors}")
        typer.echo(f"source_name='tdxquant', target table: {target_table} (no overwrite of baidu/ths)")

    @app.command("backfill-prices")
    def backfill_prices_command(
        db_path: Path = typer.Option(
            DEFAULT_DB_PATH,
            "--db-path",
            help="Target DuckDB file path.",
        ),
        stock: str | None = typer.Option(
            None,
            "--stock",
            help="Restrict to a single symbol such as 600000.SH.",
        ),
        include_all: bool = typer.Option(
            False,
            "--include-all",
            help="除沪深主板外，一并拉取创业板和科创板。默认只拉通达信的上证主板和深证主板。",
        ),
        tqcenter_path: Path | None = typer.Option(
            None,
            "--tqcenter-path",
            help="tqcenter.py 路径。默认读 QUANT_TQCENTER_PATH 或 config/local.yml。",
        ),
        skip_hsl: bool = typer.Option(
            True,
            "--skip-hsl/--fetch-hsl",
            help="Skip HSL (turnover rate) fetch during backfill for speed. Default: skip.",
        ),
        batch_size: int = typer.Option(
            50,
            "--batch-size",
            min=1,
            max=500,
            help="Number of stocks per batch API call. Default: 50.",
        ),
    ) -> None:
        """Full backfill from TDXQuant: replaces ALL daily_bar data with unadjusted + QFQ prices."""
        summary = backfill_prices(
            db_path=db_path,
            stock=stock,
            only_main_board=not include_all,
            tqcenter_path=tqcenter_path,
            skip_hsl=skip_hsl,
            batch_size=batch_size,
        )
        typer.echo(f"Backfill: {summary.stock_count} stocks processed")
        typer.echo(f"Fetched rows: {summary.fetched}")
        typer.echo(f"Inserted rows: {summary.inserted}")
        typer.echo(f"Skipped empty: {summary.skipped_empty}")
        typer.echo(f"Errors: {summary.errors}")

    @app.command("compact-db")
    def compact_db_command(
        db_path: Path = typer.Option(
            DEFAULT_DB_PATH,
            "--db-path",
            help="Target DuckDB file path.",
        ),
    ) -> None:
        """Compact DuckDB file: reclaim disk space after DELETE (runs VACUUM)."""
        import os
        size_before = os.path.getsize(db_path)
        typer.echo(f"File size before: {size_before / 1024 / 1024:.1f} MB")
        conn = duckdb.connect(str(db_path))
        typer.echo("Running VACUUM...")
        conn.execute("VACUUM")
        conn.close()
        size_after = os.path.getsize(db_path)
        typer.echo(f"File size after:  {size_after / 1024 / 1024:.1f} MB")
        typer.echo(f"Reclaimed: {(size_before - size_after) / 1024 / 1024:.1f} MB")
