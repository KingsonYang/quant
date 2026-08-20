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


BOARD_CODE = "LUPB5"
BOARD_NAME = "涨停回踩MA5收复"
COUNT_BARS = 140
BATCH_SIZE = 200
SHOW_BOARD = True


@dataclass(frozen=True)
class Params:
    limit_up_min_pct: float = 9.5
    limit_up_close_high_min_ratio: float = 0.995
    limit_up_volume_multiple: float = 1.0
    pullback_min_trade_days: int = 3
    pullback_max_trade_days: int = 15
    min_pullback_weak_days: int = 2
    weak_day_max_change_pct: float = 1.5
    support_low_tolerance_pct: float = 6.0
    close_support_tolerance_pct: float = 2.5
    must_touch_low_zone_pct: float = 8.0
    pullback_avg_volume_max_ratio: float = 1.8
    pullback_max_daily_change_pct: float = 9.0
    pullback_min_daily_change_pct: float = -6.0
    recent_ma5_touch_tolerance_pct: float = 1.0
    signal_min_change_pct: float = 0.2
    signal_reclaim_low_tolerance_pct: float = 2.0
    signal_max_distance_from_t0_low_pct: float = 12.0
    signal_volume_ratio_min: float = 0.25
    signal_volume_ratio_max: float = 2.5
    deep_pullback_enabled: bool = True
    deep_limit_up_volume_multiple: float = 0.6
    deep_support_low_tolerance_pct: float = 18.0
    deep_close_support_tolerance_pct: float = 15.0
    deep_pullback_avg_volume_max_ratio: float = 2.0
    deep_pullback_max_daily_change_pct: float = 12.0
    deep_pullback_min_daily_change_pct: float = -10.0
    deep_signal_reclaim_low_tolerance_pct: float = 5.0
    score_weight_pullback_depth: float = 0.25
    score_weight_reclaim_strength: float = 0.30
    score_weight_volume_shrink: float = 0.20
    score_weight_weak_day_ratio: float = 0.15
    score_weight_turnover: float = 0.10


@dataclass
class Candidate:
    code: str
    name: str
    signal_date: str
    d0_date: str
    sub_pullback_depth: float
    sub_reclaim_strength: float
    sub_volume_shrink: float
    sub_weak_day_ratio: float
    raw_turnover: float
    composite_score: float = 0.0


def pct_up(value: float) -> float:
    return 1.0 + value / 100.0


def pct_down(value: float) -> float:
    return 1.0 - value / 100.0


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
    if close is None or open_ is None or high is None or low is None or volume is None:
        return None

    bars = pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    ).dropna()
    if len(bars) < 25:
        return None

    turnover = series_value(data, ("TurnoverRatio", "TurnoverRate", "Turnover"), code)
    if turnover is not None:
        bars["turnover"] = pd.to_numeric(turnover, errors="coerce").reindex(bars.index).fillna(0.0)
    else:
        bars["turnover"] = 0.0

    bars = bars.sort_index()
    bars["change_pct"] = bars["close"].pct_change() * 100.0
    bars["ma5"] = bars["close"].rolling(5).mean()
    bars["prev5_avg_vol"] = bars["volume"].shift(1).rolling(5).mean()
    return bars.dropna(subset=["change_pct"])


def select_one(code: str, name: str, bars: pd.DataFrame, params: Params) -> Candidate | None:
    signal = bars.iloc[-1]
    if not np.isfinite(signal["ma5"]) or not np.isfinite(signal["prev5_avg_vol"]):
        return None
    if not (
        signal["close"] >= signal["ma5"]
        and signal["close"] > signal["open"]
        and signal["change_pct"] >= params.signal_min_change_pct
        and signal["volume"] >= params.signal_volume_ratio_min * signal["prev5_avg_vol"]
        and signal["volume"] <= params.signal_volume_ratio_max * signal["prev5_avg_vol"]
    ):
        return None

    prev3 = bars.iloc[-4:-1]
    touched_ma5 = (
        len(prev3) == 3
        and ((prev3["close"] <= prev3["ma5"] * pct_up(params.recent_ma5_touch_tolerance_pct)).any())
    )
    if not touched_ma5:
        return None

    support_low_factor = pct_down(params.support_low_tolerance_pct)
    close_support_factor = pct_down(params.close_support_tolerance_pct)
    touch_low_factor = pct_up(params.must_touch_low_zone_pct)
    signal_reclaim_low_factor = pct_down(params.signal_reclaim_low_tolerance_pct)
    signal_max_distance_factor = pct_up(params.signal_max_distance_from_t0_low_pct)
    deep_support_low_factor = pct_down(params.deep_support_low_tolerance_pct)
    deep_close_support_factor = pct_down(params.deep_close_support_tolerance_pct)
    deep_signal_reclaim_low_factor = pct_down(params.deep_signal_reclaim_low_tolerance_pct)
    anchor_volume_multiple = params.limit_up_volume_multiple
    if params.deep_pullback_enabled:
        anchor_volume_multiple = min(anchor_volume_multiple, params.deep_limit_up_volume_multiple)

    valid_anchors: list[Candidate] = []
    signal_pos = len(bars) - 1
    for pos in range(max(0, signal_pos - params.pullback_max_trade_days - 10), signal_pos):
        t0 = bars.iloc[pos]
        if not (
            t0["change_pct"] >= params.limit_up_min_pct
            and t0["high"] > 0
            and t0["close"] >= params.limit_up_close_high_min_ratio * t0["high"]
            and t0["close"] > t0["open"]
            and np.isfinite(t0["prev5_avg_vol"])
            and t0["volume"] >= anchor_volume_multiple * t0["prev5_avg_vol"]
        ):
            continue

        between = bars.iloc[pos + 1 : signal_pos]
        between_days = len(between)
        if between_days < params.pullback_min_trade_days or between_days > params.pullback_max_trade_days:
            continue

        min_low = float(between["low"].min())
        min_close = float(between["close"].min())
        max_change = float(between["change_pct"].max())
        min_change = float(between["change_pct"].min())
        avg_volume = float(between["volume"].mean())
        weak_days = int(((between["close"] <= between["open"]) | (between["change_pct"] <= params.weak_day_max_change_pct)).sum())
        if weak_days < params.min_pullback_weak_days:
            continue

        normal_branch = (
            min_low >= t0["low"] * support_low_factor
            and min_close >= t0["low"] * close_support_factor
            and max_change <= params.pullback_max_daily_change_pct
            and min_change >= params.pullback_min_daily_change_pct
            and avg_volume <= t0["volume"] * params.pullback_avg_volume_max_ratio
            and signal["close"] >= t0["low"] * signal_reclaim_low_factor
        )
        deep_branch = (
            params.deep_pullback_enabled
            and min_low < t0["low"] * support_low_factor
            and min_low >= t0["low"] * deep_support_low_factor
            and min_close >= t0["low"] * deep_close_support_factor
            and max_change <= params.deep_pullback_max_daily_change_pct
            and min_change >= params.deep_pullback_min_daily_change_pct
            and avg_volume <= t0["volume"] * params.deep_pullback_avg_volume_max_ratio
            and signal["close"] >= t0["low"] * deep_signal_reclaim_low_factor
        )
        if not (normal_branch or deep_branch):
            continue
        if not (
            min_low <= t0["low"] * touch_low_factor
            and signal["close"] <= t0["low"] * signal_max_distance_factor
            and signal["close"] < t0["close"]
        ):
            continue

        valid_anchors.append(
            Candidate(
                code=code,
                name=name,
                signal_date=bars.index[-1].strftime("%Y%m%d"),
                d0_date=bars.index[pos].strftime("%Y%m%d"),
                sub_pullback_depth=min((t0["low"] - min_low) / t0["low"] * 100.0, 100.0),
                sub_reclaim_strength=min((signal["close"] - t0["low"]) / t0["low"] * 100.0, 100.0),
                sub_volume_shrink=min(avg_volume / t0["volume"] * 100.0, 100.0),
                sub_weak_day_ratio=weak_days * 100.0 / between_days,
                raw_turnover=float(signal.get("turnover", 0.0) or 0.0),
            )
        )

    if not valid_anchors:
        return None
    return sorted(valid_anchors, key=lambda item: item.d0_date, reverse=True)[0]


def normalize_scores(candidates: list[Candidate], params: Params) -> list[Candidate]:
    def norm(value: float, values: list[float]) -> float:
        lo = min(values)
        hi = max(values)
        if not math.isfinite(value) or hi <= lo:
            return 50.0
        return (value - lo) / (hi - lo) * 100.0

    pd_values = [c.sub_pullback_depth for c in candidates]
    rs_values = [c.sub_reclaim_strength for c in candidates]
    vs_values = [c.sub_volume_shrink for c in candidates]
    wr_values = [c.sub_weak_day_ratio for c in candidates]
    to_values = [c.raw_turnover for c in candidates]

    for item in candidates:
        item.composite_score = (
            norm(item.sub_pullback_depth, pd_values) * params.score_weight_pullback_depth
            + norm(item.sub_reclaim_strength, rs_values) * params.score_weight_reclaim_strength
            + norm(item.sub_volume_shrink, vs_values) * params.score_weight_volume_shrink
            + norm(item.sub_weak_day_ratio, wr_values) * params.score_weight_weak_day_ratio
            + norm(item.raw_turnover, to_values) * params.score_weight_turnover
        )
    return sorted(candidates, key=lambda item: (-item.composite_score, item.code))


def scan_candidates(codes: list[str], code_to_name: dict[str, str], end_time: str, params: Params) -> list[Candidate]:
    candidates: list[Candidate] = []
    fields = ["Open", "High", "Low", "Close", "Volume"]
    for index, group in enumerate(batched(codes, BATCH_SIZE), start=1):
        print(f"扫描批次 {index}: {len(group)} 只")
        data = tq.get_market_data(
            field_list=fields,
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
    return normalize_scores(candidates, params) if candidates else []


def write_sector(candidates: list[Candidate], board_code: str, board_name: str, show: bool) -> None:
    tq.create_sector(block_code=board_code, block_name=board_name)
    tq.clear_sector(block_code=board_code)
    stocks = [item.code for item in candidates]
    if stocks:
        tq.send_user_block(block_code=board_code, stocks=stocks, show=show)
    else:
        tq.send_user_block(block_code=board_code, stocks=[], show=show)


def write_csv(candidates: list[Candidate], output: Path) -> None:
    rows = [
        {
            "rank": rank,
            "code": item.code,
            "name": item.name,
            "signal_date": item.signal_date,
            "d0_date": item.d0_date,
            "composite_score": round(item.composite_score, 4),
            "sub_pullback_depth": round(item.sub_pullback_depth, 4),
            "sub_reclaim_strength": round(item.sub_reclaim_strength, 4),
            "sub_volume_shrink": round(item.sub_volume_shrink, 4),
            "sub_weak_day_ratio": round(item.sub_weak_day_ratio, 4),
            "raw_turnover": round(item.raw_turnover, 4),
        }
        for rank, item in enumerate(candidates, start=1)
    ]
    pd.DataFrame(rows).to_csv(output, index=False, encoding="utf-8-sig")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TDX 涨停回踩 MA5 收复选股并写入自定义板块")
    parser.add_argument("--date", default="", help="信号日/结束日，格式 YYYYMMDD；为空则使用最新日线")
    parser.add_argument("--board-code", default=BOARD_CODE, help="自定义板块简称")
    parser.add_argument("--board-name", default=BOARD_NAME, help="自定义板块名称")
    parser.add_argument("--no-show", action="store_true", help="写入后不弹出/显示板块")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    end_time = args.date or datetime.now().strftime("%Y%m%d")
    output = Path(__file__).with_name(f"{args.board_code}_{end_time}_selection.csv")

    tq.initialize(__file__)
    try:
        params = Params()
        codes, code_to_name = load_stock_pool()
        print(f"过滤后股票池: {len(codes)} 只")
        candidates = scan_candidates(codes, code_to_name, end_time, params)
        write_sector(candidates, args.board_code, args.board_name, not args.no_show)
        write_csv(candidates, output)

        msg = f"{args.board_name}|信号日 {end_time}|选出 {len(candidates)} 只|板块 {args.board_code}|结果 {output.name}"
        print(msg)
        tq.send_message(msg)
    finally:
        tq.close()


if __name__ == "__main__":
    main()
