from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Callable

import httpx

from quant.ingest.baidu import FetchResult, fetch_baidu_kline, normalize_symbol
from quant.ingest.models import BatchIngestionSummary, IngestionSummary
from quant.ingest.parser import parse_baidu_payload, parse_market_data_rows, parse_report_events
from quant.ingest.storage import DuckDBIngestionStore, build_security_seed
from quant.ingest.symbols import load_symbol_name_pairs


def _ingest_baidu_symbol_with_resources(
    symbol: str,
    company_name: str | None,
    store: DuckDBIngestionStore,
    connection,
    client: httpx.Client | None = None,
    incremental: bool = False,
    only_trade_dates: frozenset[date] | None = None,
) -> IngestionSummary:
    normalized_symbol, _, _ = normalize_symbol(symbol)
    fetch_result: FetchResult = fetch_baidu_kline(normalized_symbol, client=client)

    security_id = store.ensure_security(
        connection,
        build_security_seed(normalized_symbol, company_name),
    )

    request_record = replace(fetch_result.request, security_id=security_id)
    raw_request_id = store.insert_raw_request(connection, request_record)

    payload = parse_baidu_payload(fetch_result.response.response_body)
    bars = parse_market_data_rows(payload, security_id=security_id)
    events = parse_report_events(payload, security_id=security_id)

    response_record = replace(
        fetch_result.response,
        security_id=security_id,
        parse_status="success",
    )
    store.insert_raw_response(connection, raw_request_id, response_record)
    if only_trade_dates is not None:
        filtered_bars = [bar for bar in bars if bar.trade_date in only_trade_dates]
        daily_bars_written = store.replace_daily_bars_for_trade_dates(connection, filtered_bars)
        events_written = 0
    elif incremental:
        daily_bars_written = store.insert_daily_bars_incremental(connection, bars)
        events_written = store.upsert_events(connection, events)
    else:
        daily_bars_written = store.upsert_daily_bars(connection, bars)
        events_written = store.upsert_events(connection, events)

    return IngestionSummary(
        symbol=normalized_symbol,
        security_id=security_id,
        raw_request_written=1,
        raw_response_written=1,
        daily_bars_written=daily_bars_written,
        events_written=events_written,
    )


def ingest_baidu_symbol(
    symbol: str,
    db_path: Path,
    company_name: str | None = None,
    only_trade_dates: frozenset[date] | None = None,
) -> IngestionSummary:
    store = DuckDBIngestionStore(db_path)
    with store.connect() as connection:
        with httpx.Client(timeout=15.0) as client:
            return _ingest_baidu_symbol_with_resources(
                symbol=symbol,
                company_name=company_name,
                store=store,
                connection=connection,
                client=client,
                only_trade_dates=only_trade_dates,
            )


def ingest_baidu_symbol_incremental(
    symbol: str,
    db_path: Path,
    company_name: str | None = None,
    only_trade_dates: frozenset[date] | None = None,
) -> IngestionSummary:
    store = DuckDBIngestionStore(db_path)
    with store.connect() as connection:
        with httpx.Client(timeout=15.0) as client:
            return _ingest_baidu_symbol_with_resources(
                symbol=symbol,
                company_name=company_name,
                store=store,
                connection=connection,
                client=client,
                incremental=True,
                only_trade_dates=only_trade_dates,
            )


def ingest_baidu_from_symbol_file(
    symbols_file: Path,
    db_path: Path,
    limit: int | None = None,
    continue_on_error: bool = True,
    progress_callback: Callable[[str], None] | None = None,
    incremental: bool = False,
    only_trade_dates: frozenset[date] | None = None,
) -> BatchIngestionSummary:
    symbol_name_pairs = load_symbol_name_pairs(symbols_file)
    if limit is not None:
        symbol_name_pairs = symbol_name_pairs[:limit]

    succeeded_symbols = 0
    failed_symbols = 0
    raw_requests_written = 0
    raw_responses_written = 0
    daily_bars_written = 0
    events_written = 0
    total_symbols = len(symbol_name_pairs)

    store = DuckDBIngestionStore(db_path)
    with store.connect() as connection:
        with httpx.Client(timeout=15.0) as client:
            for index, (symbol, company_name) in enumerate(symbol_name_pairs, start=1):
                if progress_callback is not None:
                    progress_callback(
                        f"[{index}/{total_symbols}] importing {symbol} {company_name}"
                    )
                try:
                    summary = _ingest_baidu_symbol_with_resources(
                        symbol=symbol,
                        company_name=company_name,
                        store=store,
                        connection=connection,
                        client=client,
                        incremental=incremental,
                        only_trade_dates=only_trade_dates,
                    )
                except Exception as exc:
                    failed_symbols += 1
                    if progress_callback is not None:
                        progress_callback(
                            f"[{index}/{total_symbols}] failed {symbol}: {exc}"
                        )
                    if not continue_on_error:
                        raise
                    continue

                succeeded_symbols += 1
                raw_requests_written += summary.raw_request_written
                raw_responses_written += summary.raw_response_written
                daily_bars_written += summary.daily_bars_written
                events_written += summary.events_written
                if progress_callback is not None:
                    progress_callback(
                        f"[{index}/{total_symbols}] done {symbol} "
                        f"bars={summary.daily_bars_written} events={summary.events_written} "
                        f"ok={succeeded_symbols} fail={failed_symbols}"
                    )

    return BatchIngestionSummary(
        total_symbols=total_symbols,
        succeeded_symbols=succeeded_symbols,
        failed_symbols=failed_symbols,
        raw_requests_written=raw_requests_written,
        raw_responses_written=raw_responses_written,
        daily_bars_written=daily_bars_written,
        events_written=events_written,
    )


def ingest_baidu_incremental_from_symbol_file(
    symbols_file: Path,
    db_path: Path,
    limit: int | None = None,
    continue_on_error: bool = True,
    progress_callback: Callable[[str], None] | None = None,
    only_trade_dates: frozenset[date] | None = None,
) -> BatchIngestionSummary:
    return ingest_baidu_from_symbol_file(
        symbols_file=symbols_file,
        db_path=db_path,
        limit=limit,
        continue_on_error=continue_on_error,
        progress_callback=progress_callback,
        incremental=True,
        only_trade_dates=only_trade_dates,
    )
