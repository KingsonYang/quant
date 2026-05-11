from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import duckdb


RETURN_PCT_QUANTIZE = Decimal("0.0001")


def safe_decode(data: bytes) -> str:
    """Auto-detect encoding for Chinese Windows console output."""
    if not data:
        return ""
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def quantize_pct(value: Decimal) -> Decimal:
    return value.quantize(RETURN_PCT_QUANTIZE, rounding=ROUND_HALF_UP)


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path))


def validate_trade_date(
    connection: duckdb.DuckDBPyConnection,
    target_date: date,
    error_type: type[ValueError],
) -> None:
    row = connection.execute(
        """
        SELECT 1
        FROM trading_calendar
        WHERE market = 'ab' AND trade_date = ? AND is_trading_day = TRUE
        """,
        [target_date],
    ).fetchone()
    if row is None:
        raise error_type(
            f"Trade date {target_date.isoformat()} is not available in trading_calendar."
        )


def load_trade_dates_in_range(
    connection: duckdb.DuckDBPyConnection,
    start_date: date,
    end_date: date,
) -> list[date]:
    rows = connection.execute(
        """
        SELECT trade_date
        FROM trading_calendar
        WHERE market = 'ab'
          AND is_trading_day = TRUE
          AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        [start_date, end_date],
    ).fetchall()
    return [row[0] for row in rows]


def load_all_trade_dates(
    connection: duckdb.DuckDBPyConnection,
) -> list[date]:
    rows = connection.execute(
        """
        SELECT trade_date
        FROM trading_calendar
        WHERE market = 'ab' AND is_trading_day = TRUE
        ORDER BY trade_date
        """
    ).fetchall()
    return [row[0] for row in rows]


def load_price_cache(
    connection: duckdb.DuckDBPyConnection,
    symbols: set[str],
    start_trade_date: date,
    end_trade_date: date,
) -> dict[tuple[str, date], tuple[Decimal | None, Decimal | None]]:
    if not symbols:
        return {}

    symbol_list = sorted(symbols)
    placeholders = ", ".join("?" for _ in symbol_list)
    rows = connection.execute(
        f"""
        SELECT
            s.symbol,
            d.trade_date,
            d.open_price,
            d.close_price
        FROM daily_bar AS d
        JOIN security_master AS s
            ON d.security_id = s.security_id
        WHERE s.symbol IN ({placeholders})
          AND d.trade_date BETWEEN ? AND ?
        """,
        [*symbol_list, start_trade_date, end_trade_date],
    ).fetchall()
    return {(row[0], row[1]): (row[2], row[3]) for row in rows}
