from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import duckdb

from quant.ingest.tdxquant import DEFAULT_TQCENTER_PATH


@dataclass(frozen=True)
class DataStatus:
    daily_bar_max_date: date | None
    daily_bar_security_count: int
    calendar_max_date: date | None
    calendar_lagging: bool
    tdx_available: bool
    tdx_message: str

def load_stock_kline(db_path: Path, symbol: str, center_date: date) -> dict:
    start = center_date - timedelta(days=31)
    end = center_date + timedelta(days=31)
    with duckdb.connect(str(db_path), read_only=True) as connection:
        rows = connection.execute("""SELECT d.trade_date, COALESCE(d.open_qfq,d.open_price), COALESCE(d.high_qfq,d.high_price), COALESCE(d.low_qfq,d.low_price), COALESCE(d.close_qfq,d.close_price), d.volume, s.name FROM daily_bar d JOIN security_master s ON s.security_id=d.security_id WHERE s.symbol=? AND d.trade_date BETWEEN ? AND ? ORDER BY d.trade_date""", [symbol, start, end]).fetchall()
    return {"symbol": symbol, "name": rows[0][6] if rows else None, "center_date": center_date.isoformat(), "requested_start": start.isoformat(), "requested_end": end.isoformat(), "data": [{"trade_date": r[0].isoformat(), "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]} for r in rows]}


def probe_tdxquant(tqcenter_path: Path | None = None) -> tuple[bool, str]:
    path = tqcenter_path or DEFAULT_TQCENTER_PATH
    try:
        import importlib.util

        if not path.exists():
            return False, f"未找到 tqcenter.py：{path}。请先开通行情终端。"
        spec = importlib.util.spec_from_file_location("tqcenter", str(path))
        if spec is None or spec.loader is None:
            return False, f"无法加载 tqcenter：{path}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tq = module.tq
        tq.initialize(str(Path(__file__).resolve()))
        return True, "通达信连接可用"
    except Exception as exc:
        return False, f"通达信不可用：{exc}"


def load_data_status(
    db_path: Path,
    *,
    tdx_probe: Callable[[], tuple[bool, str]] | None = None,
) -> DataStatus:
    with duckdb.connect(str(db_path)) as connection:
        bar_row = connection.execute(
            "SELECT MAX(trade_date), COUNT(DISTINCT security_id) FROM daily_bar"
        ).fetchone()
        calendar_row = connection.execute(
            """
            SELECT MAX(trade_date)
            FROM trading_calendar
            WHERE market = 'ab'
            """
        ).fetchone()
    daily_bar_max_date = bar_row[0] if bar_row else None
    daily_bar_security_count = int(bar_row[1] or 0) if bar_row else 0
    calendar_max_date = calendar_row[0] if calendar_row else None
    calendar_lagging = (
        daily_bar_max_date is not None
        and calendar_max_date is not None
        and calendar_max_date < daily_bar_max_date
    )
    probe = tdx_probe or probe_tdxquant
    tdx_available, tdx_message = probe()
    return DataStatus(
        daily_bar_max_date=daily_bar_max_date,
        daily_bar_security_count=daily_bar_security_count,
        calendar_max_date=calendar_max_date,
        calendar_lagging=calendar_lagging,
        tdx_available=tdx_available,
        tdx_message=tdx_message,
    )
