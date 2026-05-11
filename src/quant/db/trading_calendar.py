from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb


DEFAULT_MARKET = "ab"


@dataclass(frozen=True)
class TradingCalendarBuildSummary:
    mode: str
    market: str
    scanned_trade_dates: int
    calendar_rows_written: int
    calendar_rows_updated: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path))


def _load_daily_bar_trade_dates(connection: duckdb.DuckDBPyConnection) -> list[date]:
    rows = connection.execute(
        """
        SELECT DISTINCT trade_date
        FROM daily_bar
        ORDER BY trade_date
        """
    ).fetchall()
    return [row[0] for row in rows]


def _load_calendar_trade_dates(
    connection: duckdb.DuckDBPyConnection,
    market: str,
) -> list[date]:
    rows = connection.execute(
        """
        SELECT trade_date
        FROM trading_calendar
        WHERE market = ?
        ORDER BY trade_date
        """,
        [market],
    ).fetchall()
    return [row[0] for row in rows]


def _next_calendar_id(connection: duckdb.DuckDBPyConnection) -> int:
    return int(
        connection.execute(
            "SELECT COALESCE(MAX(calendar_id), 0) + 1 FROM trading_calendar"
        ).fetchone()[0]
    )


def _insert_calendar_rows(
    connection: duckdb.DuckDBPyConnection,
    trade_dates: list[date],
    market: str,
) -> int:
    if not trade_dates:
        return 0

    now = _utcnow()
    next_id = _next_calendar_id(connection)
    params = [
        (
            next_id + offset,
            market,
            trade_date,
            True,
            None,
            None,
            now,
            now,
        )
        for offset, trade_date in enumerate(trade_dates)
    ]

    connection.executemany(
        """
        INSERT INTO trading_calendar (
            calendar_id, market, trade_date, is_trading_day,
            prev_trade_date, next_trade_date, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        params,
    )
    return len(params)


def _refresh_market_links(
    connection: duckdb.DuckDBPyConnection,
    market: str,
) -> int:
    trade_dates = _load_calendar_trade_dates(connection, market)
    if not trade_dates:
        return 0

    now = _utcnow()
    params = []
    for index, trade_date in enumerate(trade_dates):
        prev_trade_date = trade_dates[index - 1] if index > 0 else None
        next_trade_date = trade_dates[index + 1] if index < len(trade_dates) - 1 else None
        params.append((prev_trade_date, next_trade_date, now, market, trade_date))

    connection.executemany(
        """
        UPDATE trading_calendar
        SET prev_trade_date = ?, next_trade_date = ?, updated_at = ?
        WHERE market = ? AND trade_date = ?
        """,
        params,
    )
    return len(params)


def build_trading_calendar(
    db_path: Path,
    market: str = DEFAULT_MARKET,
) -> TradingCalendarBuildSummary:
    with _connect(db_path) as connection:
        trade_dates = _load_daily_bar_trade_dates(connection)
        connection.execute("DELETE FROM trading_calendar WHERE market = ?", [market])
        written = _insert_calendar_rows(connection, trade_dates, market)
        updated = _refresh_market_links(connection, market)

    return TradingCalendarBuildSummary(
        mode="full",
        market=market,
        scanned_trade_dates=len(trade_dates),
        calendar_rows_written=written,
        calendar_rows_updated=updated,
    )


def build_trading_calendar_incremental(
    db_path: Path,
    market: str = DEFAULT_MARKET,
) -> TradingCalendarBuildSummary:
    with _connect(db_path) as connection:
        trade_dates = _load_daily_bar_trade_dates(connection)
        existing_dates = set(_load_calendar_trade_dates(connection, market))
        missing_dates = [trade_date for trade_date in trade_dates if trade_date not in existing_dates]
        written = _insert_calendar_rows(connection, missing_dates, market)
        updated = _refresh_market_links(connection, market) if written else 0

    return TradingCalendarBuildSummary(
        mode="incremental",
        market=market,
        scanned_trade_dates=len(trade_dates),
        calendar_rows_written=written,
        calendar_rows_updated=updated,
    )
