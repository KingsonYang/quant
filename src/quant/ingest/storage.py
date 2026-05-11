from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

import duckdb

from quant.ingest.baidu import normalize_symbol
from quant.ingest.models import DailyBarRecord, EventRecord, RawRequestRecord, RawResponseRecord, SecuritySeed


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DuckDBIngestionStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._next_security_id: int | None = None
        self._next_raw_request_id: int | None = None
        self._next_raw_response_id: int | None = None
        self._next_event_id: int | None = None

    def connect(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self.db_path))

    def _get_next_id(self, connection: duckdb.DuckDBPyConnection, table: str, column: str) -> int:
        attr_name = f"_next_{column}"
        cached = getattr(self, attr_name, None)
        if cached is None:
            cached = int(
                connection.execute(
                    f"SELECT COALESCE(MAX({column}), 0) + 1 FROM {table}"
                ).fetchone()[0]
            )
        setattr(self, attr_name, cached + 1)
        return cached

    def ensure_security(self, connection: duckdb.DuckDBPyConnection, seed: SecuritySeed) -> int:
        existing = connection.execute(
            "SELECT security_id, name FROM security_master WHERE symbol = ?",
            [seed.symbol],
        ).fetchone()
        if existing:
            security_id = int(existing[0])
            current_name = existing[1]
            if seed.name and current_name != seed.name:
                connection.execute(
                    """
                    UPDATE security_master
                    SET name = ?, updated_at = ?
                    WHERE security_id = ?
                    """,
                    [seed.name, _utcnow(), security_id],
                )
            return security_id

        security_id = self._get_next_id(connection, "security_master", "security_id")
        now = _utcnow()
        connection.execute(
            """
            INSERT INTO security_master (
                security_id, symbol, exchange, code, name, market, board, list_status,
                currency, data_source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                security_id,
                seed.symbol,
                seed.exchange,
                seed.code,
                seed.name,
                seed.market,
                seed.board,
                seed.list_status,
                "CNY",
                seed.data_source,
                now,
                now,
            ],
        )
        return security_id

    def insert_raw_request(self, connection: duckdb.DuckDBPyConnection, record: RawRequestRecord) -> int:
        raw_request_id = self._get_next_id(connection, "raw_market_request", "raw_request_id")
        connection.execute(
            """
            INSERT INTO raw_market_request (
                raw_request_id, security_id, source_name, endpoint, http_method,
                query_params, requested_at, request_status, error_message, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                raw_request_id,
                record.security_id,
                record.source_name,
                record.endpoint,
                record.http_method,
                record.query_params,
                record.requested_at,
                record.request_status,
                record.error_message,
                _utcnow(),
            ],
        )
        return raw_request_id

    def insert_raw_response(
        self,
        connection: duckdb.DuckDBPyConnection,
        raw_request_id: int,
        record: RawResponseRecord,
    ) -> int:
        raw_response_id = self._get_next_id(connection, "raw_market_response", "raw_response_id")
        connection.execute(
            """
            INSERT INTO raw_market_response (
                raw_response_id, raw_request_id, security_id, fetched_at, response_format,
                response_body, parse_status, parser_version, checksum, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                raw_response_id,
                raw_request_id,
                record.security_id,
                record.fetched_at,
                record.response_format,
                record.response_body,
                record.parse_status,
                record.parser_version,
                record.checksum,
                _utcnow(),
            ],
        )
        return raw_response_id

    def get_max_trade_date(
        self,
        connection: duckdb.DuckDBPyConnection,
        security_id: int,
    ) -> date | None:
        existing = connection.execute(
            "SELECT MAX(trade_date) FROM daily_bar WHERE security_id = ?",
            [security_id],
        ).fetchone()
        if existing is None or existing[0] is None:
            return None
        return existing[0]

    def insert_daily_bars_incremental(
        self,
        connection: duckdb.DuckDBPyConnection,
        bars: Iterable[DailyBarRecord],
    ) -> int:
        bar_list = list(bars)
        if not bar_list:
            return 0

        security_id = bar_list[0].security_id
        max_trade_date = self.get_max_trade_date(connection, security_id)
        incremental_bars = (
            bar_list
            if max_trade_date is None
            else [bar for bar in bar_list if bar.trade_date > max_trade_date]
        )
        if not incremental_bars:
            return 0

        ingested_at = _utcnow()
        params = [
            (
                bar.security_id,
                bar.trade_date,
                bar.ts_epoch,
                bar.open_price,
                bar.high_price,
                bar.low_price,
                bar.close_price,
                bar.pre_close_price,
                bar.volume,
                bar.amount,
                bar.change_amount,
                bar.change_pct,
                bar.turnover_ratio,
                bar.ma5_price,
                bar.ma10_price,
                bar.ma20_price,
                bar.ma5_volume,
                bar.ma10_volume,
                bar.ma20_volume,
                bar.source_name,
                bar.parser_version,
                ingested_at,
            )
            for bar in incremental_bars
        ]
        connection.executemany(
            """
            INSERT INTO daily_bar (
                security_id, trade_date, ts_epoch, open_price, high_price, low_price,
                close_price, pre_close_price, volume, amount, change_amount,
                change_pct, turnover_ratio, ma5_price, ma10_price, ma20_price,
                ma5_volume, ma10_volume, ma20_volume, source_name, parser_version,
                ingested_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
        return len(incremental_bars)

    def replace_daily_bars_for_trade_dates(
        self,
        connection: duckdb.DuckDBPyConnection,
        bars: Iterable[DailyBarRecord],
    ) -> int:
        """Delete existing daily_bar rows for the given (security_id, trade_date) pairs, then insert bars."""
        bar_list = list(bars)
        if not bar_list:
            return 0

        security_id = bar_list[0].security_id
        if any(bar.security_id != security_id for bar in bar_list):
            raise ValueError("replace_daily_bars_for_trade_dates requires bars for a single security_id")

        trade_dates = sorted({bar.trade_date for bar in bar_list})
        placeholders = ", ".join(["?"] * len(trade_dates))
        connection.execute(
            f"DELETE FROM daily_bar WHERE security_id = ? AND trade_date IN ({placeholders})",
            [security_id, *trade_dates],
        )

        ingested_at = _utcnow()
        params = [
            (
                bar.security_id,
                bar.trade_date,
                bar.ts_epoch,
                bar.open_price,
                bar.high_price,
                bar.low_price,
                bar.close_price,
                bar.pre_close_price,
                bar.volume,
                bar.amount,
                bar.change_amount,
                bar.change_pct,
                bar.turnover_ratio,
                bar.ma5_price,
                bar.ma10_price,
                bar.ma20_price,
                bar.ma5_volume,
                bar.ma10_volume,
                bar.ma20_volume,
                bar.source_name,
                bar.parser_version,
                ingested_at,
            )
            for bar in bar_list
        ]
        connection.executemany(
            """
            INSERT INTO daily_bar (
                security_id, trade_date, ts_epoch, open_price, high_price, low_price,
                close_price, pre_close_price, volume, amount, change_amount,
                change_pct, turnover_ratio, ma5_price, ma10_price, ma20_price,
                ma5_volume, ma10_volume, ma20_volume, source_name, parser_version,
                ingested_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
        return len(bar_list)

    def upsert_daily_bars(self, connection: duckdb.DuckDBPyConnection, bars: Iterable[DailyBarRecord]) -> int:
        bar_list = list(bars)
        if not bar_list:
            return 0

        security_id = bar_list[0].security_id
        ingested_at = _utcnow()
        connection.execute("DELETE FROM daily_bar WHERE security_id = ?", [security_id])
        params = [
            (
                bar.security_id,
                bar.trade_date,
                bar.ts_epoch,
                bar.open_price,
                bar.high_price,
                bar.low_price,
                bar.close_price,
                bar.pre_close_price,
                bar.volume,
                bar.amount,
                bar.change_amount,
                bar.change_pct,
                bar.turnover_ratio,
                bar.ma5_price,
                bar.ma10_price,
                bar.ma20_price,
                bar.ma5_volume,
                bar.ma10_volume,
                bar.ma20_volume,
                bar.source_name,
                bar.parser_version,
                ingested_at,
            )
            for bar in bar_list
        ]
        connection.executemany(
            """
            INSERT INTO daily_bar (
                security_id, trade_date, ts_epoch, open_price, high_price, low_price,
                close_price, pre_close_price, volume, amount, change_amount,
                change_pct, turnover_ratio, ma5_price, ma10_price, ma20_price,
                ma5_volume, ma10_volume, ma20_volume, source_name, parser_version,
                ingested_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
        return len(bar_list)

    def upsert_events(self, connection: duckdb.DuckDBPyConnection, events: Iterable[EventRecord]) -> int:
        event_list = list(events)
        if not event_list:
            return 0

        security_id = event_list[0].security_id
        connection.execute("DELETE FROM event_record WHERE security_id = ?", [security_id])
        created_at = _utcnow()
        params = []
        for event in event_list:
            event_id = self._get_next_id(connection, "event_record", "event_id")
            params.append(
                (
                    event_id,
                    event.security_id,
                    event.event_date,
                    event.event_type,
                    event.event_title,
                    event.event_content,
                    event.event_payload,
                    event.source_name,
                    created_at,
                )
            )
        connection.executemany(
            """
            INSERT INTO event_record (
                event_id, security_id, event_date, event_type, event_title,
                event_content, event_payload, source_name, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
        return len(event_list)


def build_security_seed(symbol: str, name: str | None = None) -> SecuritySeed:
    normalized, exchange, code = normalize_symbol(symbol)
    return SecuritySeed(
        symbol=normalized,
        exchange=exchange,
        code=code,
        name=name or normalized,
    )
