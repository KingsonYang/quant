from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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
