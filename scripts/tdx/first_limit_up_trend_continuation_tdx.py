from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from tqcenter import tq


BOARD_CODE = "FLUTC"
BOARD_NAME = "首板趋势延续"
COUNT_BARS = 80
BATCH_SIZE = 200
SHOW_BOARD = True


@dataclass(frozen=True)
class Params:
    limit_up_min_pct: float = 9.5
    limit_up_close_high_min_ratio: float = 0.995
    volume_ratio_prev5_min: float = 0.2
    volume_ratio_prev5_max: float = 4.0
    turnover_min: float = 0.5
    turnover_max: float = 60.0
    amount_min_yi: float = 0.05
    require_first_board: bool = True
    require_break_prev20_high: bool = False
    require_ma_bull_stack: bool = False
    prev10_change_sum_min: float = -20.0
    prev10_change_sum_max: float = 80.0


@dataclass
class Candidate:
    code: str
    name: str
    signal_date: str
    composite_score: float
    open_price: float
    close_price: float
    ma5: float
    volume_ratio_prev5: float
    volume_ratio_prev20: float
    amount_yi: float
    turnover: float
    prev5_change_sum: float
    prev10_change_sum: float
    break_prev20_high: int
    ma_bull_stack: int


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    if not math.isfinite(value):
        return low
    return min(max(value, low), high)


def batched(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def normalize_stock_item(item: Any) -> tuple[str, str]:
    if isinstance(item, str):
        return item, ""
    if not isinstance(item, dict):
        return "", ""
    code = (
        item.get("Code")
        or item.get("code")
        or item.get("StockCode")
        or item.get("stock_code")
        or item.get("代码")
        or ""
    )
    name = (
        item.get("Name")
        or item.get("name")
        or item.get("StockName")
        or item.get("stock_name")
        or item.get("名称")
        or ""
    )
    return str(code).strip(), str(name).strip()


def is_excluded_board(code: str) -> bool:
    raw = code.split(".", 1)[0]
    suffix = code.rsplit(".", 1)[-1].upper() if "." in code else ""
    return (
        suffix == "BJ"
        or raw.startswith(("300", "301"))
        or raw.startswith(("688", "689"))
        or raw.startswith(("8", "4", "92"))
    )


def is_st_name(name: str) -> bool:
    if not name:
        return False
    normalized = str(name).upper().replace(" ", "")
    return "ST" in normalized or "*ST" in normalized or "SST" in normalized or "退" in str(name) or "退市" in str(name)


def normalize_stock_items(items: list[Any]) -> tuple[list[str], dict[str, str]]:
    codes: list[str] = []
    code_to_name: dict[str, str] = {}
    for item in items:
        code, name = normalize_stock_item(item)
        if not code:
            continue
        codes.append(code)
        code_to_name[code] = name

    seen: set[str] = set()
    unique_codes: list[str] = []
    for code in codes:
        if code not in seen:
            seen.add(code)
            unique_codes.append(code)
    return unique_codes, code_to_name


def get_all_a_stock_items() -> list[Any]:
    for market in ("5", 5, None):
        try:
            return tq.get_stock_list(market, list_type=1)
        except Exception:
            continue
    return tq.get_stock_list("5", list_type=0)


def load_stock_pool() -> tuple[list[str], dict[str, str]]:
    raw_items = get_all_a_stock_items()
    codes, code_to_name = normalize_stock_items(raw_items)

    filtered: list[str] = []
    for code in codes:
        name = code_to_name.get(code, "")
        if is_excluded_board(code) or is_st_name(name):
            continue
        filtered.append(code)
    return filtered, code_to_name


def series_value(data: dict[str, pd.DataFrame], field_names: tuple[str, ...], code: str) -> pd.Series | None:
    lower_map = {key.lower(): key for key in data.keys()}
    for field in field_names:
        key = lower_map.get(field.lower())
        if key and code in data[key].columns:
            return pd.to_numeric(data[key][code], errors="coerce")
    return None


def build_bars(data: dict[str, pd.DataFrame], code: str) -> pd.DataFrame | None:
    close = series_value(data, ("Close",), code)
    open_ = series_value(data, ("Open",), code)
    high = series_value(data, ("High",), code)
    low = series_value(data, ("Low",), code)
    volume = series_value(data, ("Volume",), code)
    amount = series_value(data, ("Amount",), code)
    if close is None or open_ is None or high is None or low is None or volume is None:
        return None

    bars = pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount if amount is not None else np.nan,
        }
    ).dropna(subset=["open", "high", "low", "close", "volume"])
    if len(bars) < 35:
        return None

    turnover = series_value(
        data,
        (
            "TurnoverRatio",
            "TurnoverRate",
            "Turnover",
            "Hsl",
            "HSL",
            "换手率",
        ),
        code,
    )
    if turnover is not None:
        bars["turnover"] = pd.to_numeric(turnover, errors="coerce").reindex(bars.index).fillna(8.0)
    else:
        bars["turnover"] = 8.0

    bars = bars.sort_index()
    bars["change_pct"] = bars["close"].pct_change() * 100.0
    bars["ma5"] = bars["close"].rolling(5).mean()
    bars["ma10"] = bars["close"].rolling(10).mean()
    bars["ma20"] = bars["close"].rolling(20).mean()
    bars["prev5_avg_volume"] = bars["volume"].shift(1).rolling(5).mean()
    bars["prev20_avg_volume"] = bars["volume"].shift(1).rolling(20).mean()
    bars["prev5_change_sum"] = bars["change_pct"].shift(1).rolling(5).sum()
    bars["prev10_change_sum"] = bars["change_pct"].shift(1).rolling(10).sum()
    bars["prev20_high"] = bars["high"].shift(1).rolling(20).max()
    return bars.dropna(subset=["change_pct"])


def amount_to_yi(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    # TDX documentation says daily Amount is in ten-thousand yuan.
    return value / 10000.0


def is_limit_up(row: pd.Series, params: Params) -> bool:
    return bool(
        row["change_pct"] >= params.limit_up_min_pct
        and row["high"] > 0
        and row["close"] >= row["high"] * params.limit_up_close_high_min_ratio
        and row["close"] > row["open"]
    )


def select_one(code: str, name: str, bars: pd.DataFrame, params: Params) -> Candidate | None:
    signal = bars.iloc[-1]
    previous = bars.iloc[-2] if len(bars) >= 2 else None
    if previous is None:
        return None

    if not is_limit_up(signal, params):
        return None
    if params.require_first_board and is_limit_up(previous, params):
        return None

    volume_ratio_prev5 = signal["volume"] / signal["prev5_avg_volume"] if signal["prev5_avg_volume"] > 0 else np.nan
    volume_ratio_prev20 = signal["volume"] / signal["prev20_avg_volume"] if signal["prev20_avg_volume"] > 0 else np.nan
    amount_yi = amount_to_yi(float(signal.get("amount", np.nan)))
    turnover = float(signal.get("turnover", 8.0) or 8.0)
    prev5_change_sum = float(signal.get("prev5_change_sum", np.nan))
    prev10_change_sum = float(signal.get("prev10_change_sum", np.nan))
    prev20_high = float(signal.get("prev20_high", np.nan))
    ma5 = float(signal.get("ma5", np.nan))
    ma10 = float(signal.get("ma10", np.nan))
    ma20 = float(signal.get("ma20", np.nan))

    if not (
        math.isfinite(volume_ratio_prev5)
        and params.volume_ratio_prev5_min <= volume_ratio_prev5 <= params.volume_ratio_prev5_max
        and params.turnover_min <= turnover <= params.turnover_max
        and amount_yi >= params.amount_min_yi
        and math.isfinite(prev10_change_sum)
        and params.prev10_change_sum_min <= prev10_change_sum <= params.prev10_change_sum_max
    ):
        return None

    break_prev20_high = int(math.isfinite(prev20_high) and signal["close"] >= prev20_high)
    ma_bull_stack = int(math.isfinite(ma5) and math.isfinite(ma10) and math.isfinite(ma20) and ma5 > ma10 > ma20)
    if params.require_break_prev20_high and not break_prev20_high:
        return None
    if params.require_ma_bull_stack and not ma_bull_stack:
        return None

    close_vs_ma20_pct = (signal["close"] - ma20) / ma20 * 100.0 if ma20 > 0 else 0.0
    ma5_vs_ma10_pct = (ma5 - ma10) / ma10 * 100.0 if ma10 > 0 else 0.0
    ma10_vs_ma20_pct = (ma10 - ma20) / ma20 * 100.0 if ma20 > 0 else 0.0
    close_vs_prev20_high_pct = (signal["close"] - prev20_high) / prev20_high * 100.0 if prev20_high > 0 else 0.0

    trend_quality = (
        clamp(close_vs_ma20_pct / 20.0 * 100.0) * 0.35
        + clamp(ma5_vs_ma10_pct / 5.0 * 100.0) * 0.25
        + clamp(ma10_vs_ma20_pct / 5.0 * 100.0) * 0.25
        + ma_bull_stack * 100.0 * 0.15
    )
    volume_health = (
        (100.0 - min(abs((volume_ratio_prev5 or 0.0) - 1.1) / 1.1 * 100.0, 100.0)) * 0.65
        + (100.0 - min(abs((volume_ratio_prev20 or 0.0) - 1.2) / 1.2 * 100.0, 100.0)) * 0.35
    )
    breakout_quality = break_prev20_high * 65.0 + clamp(close_vs_prev20_high_pct / 8.0 * 100.0) * 0.35
    preheat_quality = clamp(prev5_change_sum / 8.0 * 100.0) * 0.45 + clamp(prev10_change_sum / 15.0 * 100.0) * 0.55
    turnover_activity = 100.0 - min(abs(turnover - 8.0) / 8.0 * 100.0, 100.0)
    overheating_penalty = 0.0
    if volume_ratio_prev5 > 2.5:
        overheating_penalty += min((volume_ratio_prev5 - 2.5) / 2.0 * 18.0, 18.0)
    if turnover > 25.0:
        overheating_penalty += min((turnover - 25.0) / 20.0 * 12.0, 12.0)
    if prev10_change_sum > 35.0:
        overheating_penalty += min((prev10_change_sum - 35.0) / 35.0 * 10.0, 10.0)

    composite_score = (
        trend_quality * 0.30
        + volume_health * 0.25
        + breakout_quality * 0.20
        + preheat_quality * 0.15
        + turnover_activity * 0.10
        - overheating_penalty
    )

    return Candidate(
        code=code,
        name=name,
        signal_date=bars.index[-1].strftime("%Y%m%d"),
        composite_score=float(composite_score),
        open_price=float(signal["open"]),
        close_price=float(signal["close"]),
        ma5=ma5,
        volume_ratio_prev5=float(volume_ratio_prev5),
        volume_ratio_prev20=float(volume_ratio_prev20),
        amount_yi=float(amount_yi),
        turnover=float(turnover),
        prev5_change_sum=float(prev5_change_sum),
        prev10_change_sum=float(prev10_change_sum),
        break_prev20_high=break_prev20_high,
        ma_bull_stack=ma_bull_stack,
    )


def scan_candidates(codes: list[str], code_to_name: dict[str, str], end_time: str, params: Params) -> list[Candidate]:
    candidates: list[Candidate] = []
    for index, group in enumerate(batched(codes, BATCH_SIZE), start=1):
        print(f"scan batch {index}: {len(group)} stocks")
        data = tq.get_market_data(
            field_list=[],
            stock_list=group,
            period="1d",
            end_time=end_time,
            count=COUNT_BARS,
            dividend_type="front",
            fill_data=True,
        )
        for code in group:
            bars = build_bars(data, code)
            if bars is None:
                continue
            candidate = select_one(code, code_to_name.get(code, ""), bars, params)
            if candidate is not None:
                candidates.append(candidate)
    return sorted(candidates, key=lambda item: (-item.composite_score, item.code))


def write_sector(candidates: list[Candidate], board_code: str, board_name: str, show: bool) -> None:
    tq.create_sector(block_code=board_code, block_name=board_name)
    tq.clear_sector(block_code=board_code)
    tq.send_user_block(block_code=board_code, stocks=[item.code for item in candidates], show=show)


def write_csv(candidates: list[Candidate], output: Path) -> None:
    rows = [
        {
            "rank": rank,
            "code": item.code,
            "name": item.name,
            "signal_date": item.signal_date,
            "composite_score": round(item.composite_score, 4),
            "open": round(item.open_price, 4),
            "close": round(item.close_price, 4),
            "ma5": round(item.ma5, 4),
            "volume_ratio_prev5": round(item.volume_ratio_prev5, 4),
            "volume_ratio_prev20": round(item.volume_ratio_prev20, 4),
            "amount_yi": round(item.amount_yi, 4),
            "turnover": round(item.turnover, 4),
            "prev5_change_sum": round(item.prev5_change_sum, 4),
            "prev10_change_sum": round(item.prev10_change_sum, 4),
            "break_prev20_high": item.break_prev20_high,
            "ma_bull_stack": item.ma_bull_stack,
        }
        for rank, item in enumerate(candidates, start=1)
    ]
    pd.DataFrame(rows).to_csv(output, index=False, encoding="utf-8-sig")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TDX first limit-up trend continuation selector")
    parser.add_argument("--date", default="", help="Signal date, YYYYMMDD. Empty means latest daily bar.")
    parser.add_argument("--board-code", default=BOARD_CODE, help="TDX custom sector short code.")
    parser.add_argument("--board-name", default=BOARD_NAME, help="TDX custom sector name.")
    parser.add_argument("--no-show", action="store_true", help="Do not show sector after writing.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    end_time = args.date or datetime.now().strftime("%Y%m%d")
    output = Path(__file__).with_name(f"{args.board_code}_{end_time}_selection.csv")

    tq.initialize(__file__)
    try:
        params = Params()
        codes, code_to_name = load_stock_pool()
        print(f"filtered stock pool: {len(codes)} stocks")
        candidates = scan_candidates(codes, code_to_name, end_time, params)
        write_sector(candidates, args.board_code, args.board_name, not args.no_show)
        write_csv(candidates, output)

        message = (
            f"{args.board_name}|date {end_time}|selected {len(candidates)}|"
            f"sector {args.board_code}|csv {output.name}"
        )
        print(message)
        tq.send_message(message)
    finally:
        tq.close()


if __name__ == "__main__":
    main()
