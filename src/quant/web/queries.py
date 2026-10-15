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

def load_stock_kline(
    db_path: Path,
    symbol: str,
    center_date: date,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    if start_date is not None and end_date is not None:
        start = start_date - timedelta(days=35)
        end = end_date + timedelta(days=35)
    else:
        start = center_date - timedelta(days=45)
        end = center_date + timedelta(days=45)
    with duckdb.connect(str(db_path), read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT d.trade_date,
                   COALESCE(d.open_qfq, d.open_price),
                   COALESCE(d.high_qfq, d.high_price),
                   COALESCE(d.low_qfq, d.low_price),
                   COALESCE(d.close_qfq, d.close_price),
                   d.volume,
                   s.name,
                   d.ma5_price,
                   d.ma10_price,
                   d.ma20_price,
                   d.turnover_ratio,
                   d.change_pct
            FROM daily_bar d
            JOIN security_master s ON s.security_id = d.security_id
            WHERE s.symbol = ? AND d.trade_date BETWEEN ? AND ?
            ORDER BY d.trade_date
            """,
            [symbol, start, end],
        ).fetchall()
    return {
        "symbol": symbol,
        "name": rows[0][6] if rows else None,
        "center_date": center_date.isoformat(),
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "data": [
            {
                "trade_date": r[0].isoformat(),
                "open": float(r[1]) if r[1] is not None else None,
                "high": float(r[2]) if r[2] is not None else None,
                "low": float(r[3]) if r[3] is not None else None,
                "close": float(r[4]) if r[4] is not None else None,
                "volume": int(r[5]) if r[5] is not None else 0,
                "ma5": float(r[7]) if r[7] is not None else None,
                "ma10": float(r[8]) if r[8] is not None else None,
                "ma20": float(r[9]) if r[9] is not None else None,
                "turnover_ratio": float(r[10]) if r[10] is not None else None,
                "change_pct": float(r[11]) if r[11] is not None else None,
            }
            for r in rows
        ],
    }


def load_benchmark_series(db_path: Path, start_date: date, end_date: date) -> list[dict]:
    with duckdb.connect(str(db_path), read_only=True) as connection:
        tables = [r[0] for r in connection.execute("SHOW TABLES").fetchall()]
        if "hs300_bar" not in tables:
            return []
        rows = connection.execute(
            """
            SELECT trade_date, close_price
            FROM hs300_bar
            WHERE trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            [start_date, end_date],
        ).fetchall()
    return [
        {"trade_date": r[0].isoformat(), "close": float(r[1])}
        for r in rows
        if r[1] is not None
    ]


def load_market_sentiment(db_path: Path, trade_date: date) -> dict:
    with duckdb.connect(str(db_path), read_only=True) as connection:
        row = connection.execute(
            """
            SELECT count(*),
                   sum(case when change_pct > 0 then 1 else 0 end),
                   sum(case when change_pct < 0 then 1 else 0 end),
                   sum(case when change_pct == 0 then 1 else 0 end),
                   sum(case when change_pct >= 9.8 then 1 else 0 end),
                   sum(case when change_pct <= -9.8 then 1 else 0 end),
                   avg(change_pct)
            FROM daily_bar
            WHERE trade_date = ?
            """,
            [trade_date],
        ).fetchone()
    if not row or not row[0]:
        return {"trade_date": trade_date.isoformat(), "has_data": False}
    total, up, down, flat, limit_up, limit_down, avg_pct = row
    up_cnt = int(up or 0)
    down_cnt = int(down or 0)
    total_cnt = int(total or 0)
    up_ratio = round((up_cnt / total_cnt * 100), 1) if total_cnt else 0.0
    if up_ratio >= 65:
        sentiment = "强势多头"
    elif up_ratio >= 55:
        sentiment = "偏多震荡"
    elif up_ratio >= 45:
        sentiment = "多空平衡"
    elif up_ratio >= 35:
        sentiment = "偏空调整"
    else:
        sentiment = "冰点防守" 
    return {
        "trade_date": trade_date.isoformat(),
        "has_data": True,
        "total_count": total_cnt,
        "up_count": up_cnt,
        "down_count": down_cnt,
        "flat_count": int(flat or 0),
        "limit_up_count": int(limit_up or 0),
        "limit_down_count": int(limit_down or 0),
        "up_ratio": up_ratio,
        "avg_change_pct": round(float(avg_pct or 0), 2),
        "sentiment": sentiment,
    }


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
