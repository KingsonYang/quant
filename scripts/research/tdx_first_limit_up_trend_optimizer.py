from __future__ import annotations

import argparse
import math
import random
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd


DEFAULT_START = date(2024, 1, 2)
DEFAULT_OUTPUT_DIR = Path("results/research/first_limit_up_trend_continuation_tdx")
DEFAULT_TDX_USER_DIR = Path("d:/SoftWare/tdx/PYPlugins/user")
DEFAULT_DB_PATH = Path("data/quant.duckdb")
NEUTRAL_TURNOVER = 8.0


@dataclass(frozen=True)
class SelectionParams:
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


@dataclass(frozen=True)
class ExecutionParams:
    holding_period: int = 2
    take_profit_pct: float = 10.0
    stop_loss_pct: float = -5.0
    selection_top_n: int = 1
    min_composite_score: float = 80.0
    min_score_gap: float = 3.0
    initial_capital: float = 10000.0
    position_size_pct: float = 100.0


@dataclass(frozen=True)
class Candidate:
    symbol: str
    name: str
    selection_date: date
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


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    if not math.isfinite(value):
        return low
    return min(max(value, low), high)


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def ensure_tdx_import(tdx_user_dir: Path):
    sys.path.insert(0, str(tdx_user_dir))
    from tqcenter import tq  # type: ignore

    return tq


def normalize_stock_item(item: Any) -> tuple[str, str]:
    if isinstance(item, str):
        return item.strip(), ""
    if not isinstance(item, dict):
        return "", ""
    code = item.get("Code") or item.get("code") or item.get("StockCode") or item.get("stock_code") or ""
    name = item.get("Name") or item.get("name") or item.get("StockName") or item.get("stock_name") or ""
    return str(code).strip(), str(name).strip()


def to_tdx_symbol(symbol: str) -> str:
    if len(symbol) == 8 and symbol[:2] in {"SZ", "SH", "BJ"}:
        return f"{symbol[2:]}.{symbol[:2]}"
    return symbol


def to_db_symbol(symbol: str) -> str:
    if "." in symbol:
        code, suffix = symbol.rsplit(".", 1)
        if suffix in {"SZ", "SH", "BJ"}:
            return f"{suffix}{code}"
    return symbol


def is_st_name(name: str) -> bool:
    if not name:
        return False
    normalized = str(name).upper().replace(" ", "")
    return "ST" in normalized or "*ST" in normalized or "SST" in normalized or "退" in str(name) or "退市" in str(name)


def is_hs_main_board_code(code: str) -> bool:
    return (code.endswith(".SH") and code[:3] in {"600", "601", "603", "605"}) or (
        code.endswith(".SZ") and code[:3] in {"000", "001", "002", "003"}
    )


def load_tdx_universe(tq) -> tuple[list[str], dict[str, str]]:
    for market in ("5", 5, None):
        try:
            items = tq.get_stock_list(market, list_type=1)
            break
        except Exception:
            items = []
    else:
        items = tq.get_stock_list("5", list_type=0)

    names: dict[str, str] = {}
    codes: list[str] = []
    for item in items:
        code, name = normalize_stock_item(item)
        if not code:
            continue
        if not is_hs_main_board_code(code) or is_st_name(name):
            continue
        if code not in names:
            codes.append(code)
        names[code] = name
    return codes, names


def frame_series(data: dict[str, pd.DataFrame], code: str, field_names: tuple[str, ...]) -> pd.Series | None:
    lower_map = {field.lower(): field for field in data.keys()}
    for field_name in field_names:
        key = lower_map.get(field_name.lower())
        if key and isinstance(data[key], pd.DataFrame) and code in data[key].columns:
            return pd.to_numeric(data[key][code], errors="coerce")
    return None


def amount_to_yi(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return value / 10000.0


def build_derived_bars(data: dict[str, pd.DataFrame], code: str) -> tuple[pd.DataFrame | None, bool]:
    close = frame_series(data, code, ("Close",))
    open_ = frame_series(data, code, ("Open",))
    high = frame_series(data, code, ("High",))
    low = frame_series(data, code, ("Low",))
    volume = frame_series(data, code, ("Volume",))
    amount = frame_series(data, code, ("Amount",))
    if close is None or open_ is None or high is None or low is None or volume is None:
        return None, False

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
        return None, False

    turnover = frame_series(
        data,
        code,
        ("TurnoverRatio", "TurnoverRate", "Turnover", "Hsl", "HSL", "换手率"),
    )
    turnover_available = turnover is not None and turnover.notna().any()
    bars["turnover"] = (
        pd.to_numeric(turnover, errors="coerce").reindex(bars.index).fillna(NEUTRAL_TURNOVER)
        if turnover_available
        else NEUTRAL_TURNOVER
    )

    bars = bars.sort_index()
    bars.index = pd.to_datetime(bars.index)
    bars["trade_date"] = bars.index.date
    bars["change_pct"] = bars["close"].pct_change() * 100.0
    bars["ma5"] = bars["close"].rolling(5).mean()
    bars["ma10"] = bars["close"].rolling(10).mean()
    bars["ma20"] = bars["close"].rolling(20).mean()
    bars["prev5_avg_volume"] = bars["volume"].shift(1).rolling(5).mean()
    bars["prev20_avg_volume"] = bars["volume"].shift(1).rolling(20).mean()
    bars["prev5_change_sum"] = bars["change_pct"].shift(1).rolling(5).sum()
    bars["prev10_change_sum"] = bars["change_pct"].shift(1).rolling(10).sum()
    bars["prev20_high"] = bars["high"].shift(1).rolling(20).max()
    return bars.dropna(subset=["change_pct"]), turnover_available


def fetch_tdx_bars(
    tq,
    *,
    codes: list[str],
    names: dict[str, str],
    start_date: date,
    end_date: date,
    batch_size: int,
) -> tuple[dict[str, pd.DataFrame], list[date], bool]:
    bars_by_symbol: dict[str, pd.DataFrame] = {}
    any_turnover_available = False
    fetch_start = (start_date - timedelta(days=90)).strftime("%Y%m%d")
    fetch_end = end_date.strftime("%Y%m%d")
    batches = chunked(codes, batch_size)
    for batch_index, batch in enumerate(batches, start=1):
        print(f"[TDX] fetch batch {batch_index}/{len(batches)}: {batch[0]}..{batch[-1]} ({len(batch)})")
        data = tq.get_market_data(
            field_list=[],
            stock_list=batch,
            period="1d",
            start_time=fetch_start,
            end_time=fetch_end,
            count=-1,
            dividend_type="front",
            fill_data=True,
        )
        for code in batch:
            bars, turnover_available = build_derived_bars(data, code)
            if bars is None:
                continue
            bars_by_symbol[code] = bars
            any_turnover_available = any_turnover_available or turnover_available
    print(f"[TDX] usable stocks={len(bars_by_symbol)} turnover_available={any_turnover_available}")
    trade_dates = sorted(
        {
            trade_date
            for bars in bars_by_symbol.values()
            for trade_date in bars["trade_date"].tolist()
            if start_date <= trade_date <= end_date
        }
    )
    return bars_by_symbol, trade_dates, any_turnover_available


def is_limit_up(row: pd.Series, params: SelectionParams) -> bool:
    return bool(
        row["change_pct"] >= params.limit_up_min_pct
        and row["high"] > 0
        and row["close"] >= row["high"] * params.limit_up_close_high_min_ratio
        and row["close"] > row["open"]
    )


def candidate_from_row(
    *,
    symbol: str,
    name: str,
    signal: pd.Series,
    params: SelectionParams,
) -> Candidate | None:
    volume_ratio_prev5 = signal["volume"] / signal["prev5_avg_volume"] if signal["prev5_avg_volume"] > 0 else np.nan
    volume_ratio_prev20 = signal["volume"] / signal["prev20_avg_volume"] if signal["prev20_avg_volume"] > 0 else np.nan
    amount_yi = amount_to_yi(float(signal.get("amount", np.nan)))
    turnover = float(signal.get("turnover", NEUTRAL_TURNOVER) or NEUTRAL_TURNOVER)
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
        (100.0 - min(abs(volume_ratio_prev5 - 1.1) / 1.1 * 100.0, 100.0)) * 0.65
        + (100.0 - min(abs(volume_ratio_prev20 - 1.2) / 1.2 * 100.0, 100.0)) * 0.35
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
        symbol=symbol,
        name=name,
        selection_date=signal["trade_date"],
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


def build_signal_frame(
    bars_by_symbol: dict[str, pd.DataFrame],
    names: dict[str, str],
    *,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for symbol, bars in bars_by_symbol.items():
        rows = bars[(bars["trade_date"] >= start_date) & (bars["trade_date"] <= end_date)].copy()
        if rows.empty:
            continue
        rows["symbol"] = symbol
        rows["name"] = names.get(symbol, "")
        rows["selection_date"] = rows["trade_date"]
        rows["open_price"] = rows["open"]
        rows["close_price"] = rows["close"]
        rows["close_high_ratio"] = np.where(rows["high"] > 0, rows["close"] / rows["high"], np.nan)
        rows["close_gt_open"] = rows["close"] > rows["open"]
        rows["prev_change_pct_for_limit"] = bars["change_pct"].shift(1).reindex(rows.index)
        close_high_ratio_all = pd.Series(
            np.where(bars["high"] > 0, bars["close"] / bars["high"], np.nan),
            index=bars.index,
        )
        rows["prev_close_high_ratio"] = close_high_ratio_all.shift(1).reindex(rows.index)
        rows["prev_close_gt_open"] = (bars["close"] > bars["open"]).shift(1).reindex(rows.index).fillna(False)
        rows["volume_ratio_prev5"] = np.where(
            rows["prev5_avg_volume"] > 0,
            rows["volume"] / rows["prev5_avg_volume"],
            np.nan,
        )
        rows["volume_ratio_prev20"] = np.where(
            rows["prev20_avg_volume"] > 0,
            rows["volume"] / rows["prev20_avg_volume"],
            np.nan,
        )
        rows["amount_yi"] = rows["amount"].map(lambda value: amount_to_yi(float(value)))
        rows["break_prev20_high"] = (rows["prev20_high"] > 0) & (rows["close"] >= rows["prev20_high"])
        rows["ma_bull_stack"] = (rows["ma5"] > rows["ma10"]) & (rows["ma10"] > rows["ma20"])
        rows["close_vs_ma20_pct"] = np.where(
            rows["ma20"] > 0,
            (rows["close"] - rows["ma20"]) / rows["ma20"] * 100.0,
            0.0,
        )
        rows["ma5_vs_ma10_pct"] = np.where(
            rows["ma10"] > 0,
            (rows["ma5"] - rows["ma10"]) / rows["ma10"] * 100.0,
            0.0,
        )
        rows["ma10_vs_ma20_pct"] = np.where(
            rows["ma20"] > 0,
            (rows["ma10"] - rows["ma20"]) / rows["ma20"] * 100.0,
            0.0,
        )
        rows["close_vs_prev20_high_pct"] = np.where(
            rows["prev20_high"] > 0,
            (rows["close"] - rows["prev20_high"]) / rows["prev20_high"] * 100.0,
            0.0,
        )
        frames.append(
            rows[
                [
                    "symbol",
                    "name",
                    "selection_date",
                    "open_price",
                    "close_price",
                    "ma5",
                    "change_pct",
                    "close_high_ratio",
                    "close_gt_open",
                    "prev_change_pct_for_limit",
                    "prev_close_high_ratio",
                    "prev_close_gt_open",
                    "volume_ratio_prev5",
                    "volume_ratio_prev20",
                    "amount_yi",
                    "turnover",
                    "prev5_change_sum",
                    "prev10_change_sum",
                    "break_prev20_high",
                    "ma_bull_stack",
                    "close_vs_ma20_pct",
                    "ma5_vs_ma10_pct",
                    "ma10_vs_ma20_pct",
                    "close_vs_prev20_high_pct",
                ]
            ]
        )
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def select_candidates_from_frame(signal_frame: pd.DataFrame, params: SelectionParams) -> dict[date, list[Candidate]]:
    if signal_frame.empty:
        return {}
    frame = signal_frame
    is_limit = (
        (frame["change_pct"] >= params.limit_up_min_pct)
        & (frame["close_high_ratio"] >= params.limit_up_close_high_min_ratio)
        & (frame["close_gt_open"])
    )
    prev_is_limit = (
        (frame["prev_change_pct_for_limit"] >= params.limit_up_min_pct)
        & (frame["prev_close_high_ratio"] >= params.limit_up_close_high_min_ratio)
        & (frame["prev_close_gt_open"])
    )
    mask = (
        is_limit
        & frame["volume_ratio_prev5"].between(params.volume_ratio_prev5_min, params.volume_ratio_prev5_max)
        & frame["turnover"].between(params.turnover_min, params.turnover_max)
        & (frame["amount_yi"] >= params.amount_min_yi)
        & frame["prev10_change_sum"].between(params.prev10_change_sum_min, params.prev10_change_sum_max)
    )
    if params.require_first_board:
        mask &= ~prev_is_limit
    if params.require_break_prev20_high:
        mask &= frame["break_prev20_high"]
    if params.require_ma_bull_stack:
        mask &= frame["ma_bull_stack"]

    selected = frame.loc[mask].copy()
    if selected.empty:
        return {}

    trend_quality = (
        np.clip(selected["close_vs_ma20_pct"].fillna(0.0) / 20.0 * 100.0, 0, 100) * 0.35
        + np.clip(selected["ma5_vs_ma10_pct"].fillna(0.0) / 5.0 * 100.0, 0, 100) * 0.25
        + np.clip(selected["ma10_vs_ma20_pct"].fillna(0.0) / 5.0 * 100.0, 0, 100) * 0.25
        + selected["ma_bull_stack"].astype(float) * 100.0 * 0.15
    )
    volume_health = (
        (100.0 - np.minimum(abs(selected["volume_ratio_prev5"].fillna(0.0) - 1.1) / 1.1 * 100.0, 100.0)) * 0.65
        + (100.0 - np.minimum(abs(selected["volume_ratio_prev20"].fillna(0.0) - 1.2) / 1.2 * 100.0, 100.0)) * 0.35
    )
    breakout_quality = (
        selected["break_prev20_high"].astype(float) * 65.0
        + np.clip(selected["close_vs_prev20_high_pct"].fillna(0.0) / 8.0 * 100.0, 0, 100) * 0.35
    )
    preheat_quality = (
        np.clip(selected["prev5_change_sum"].fillna(0.0) / 8.0 * 100.0, 0, 100) * 0.45
        + np.clip(selected["prev10_change_sum"].fillna(0.0) / 15.0 * 100.0, 0, 100) * 0.55
    )
    turnover_activity = 100.0 - np.minimum(abs(selected["turnover"].fillna(NEUTRAL_TURNOVER) - 8.0) / 8.0 * 100.0, 100.0)
    overheating_penalty = (
        np.where(selected["volume_ratio_prev5"] > 2.5, np.minimum((selected["volume_ratio_prev5"] - 2.5) / 2.0 * 18.0, 18.0), 0.0)
        + np.where(selected["turnover"] > 25.0, np.minimum((selected["turnover"] - 25.0) / 20.0 * 12.0, 12.0), 0.0)
        + np.where(selected["prev10_change_sum"] > 35.0, np.minimum((selected["prev10_change_sum"] - 35.0) / 35.0 * 10.0, 10.0), 0.0)
    )
    selected["composite_score"] = (
        trend_quality * 0.30
        + volume_health * 0.25
        + breakout_quality * 0.20
        + preheat_quality * 0.15
        + turnover_activity * 0.10
        - overheating_penalty
    )
    selected = selected.sort_values(["selection_date", "composite_score", "symbol"], ascending=[True, False, True])

    grouped: dict[date, list[Candidate]] = {}
    for row in selected.itertuples(index=False):
        candidate = Candidate(
            symbol=row.symbol,
            name=row.name,
            selection_date=row.selection_date,
            composite_score=float(row.composite_score),
            open_price=float(row.open_price),
            close_price=float(row.close_price),
            ma5=float(row.ma5),
            volume_ratio_prev5=float(row.volume_ratio_prev5),
            volume_ratio_prev20=float(row.volume_ratio_prev20),
            amount_yi=float(row.amount_yi),
            turnover=float(row.turnover),
            prev5_change_sum=float(row.prev5_change_sum),
            prev10_change_sum=float(row.prev10_change_sum),
            break_prev20_high=int(row.break_prev20_high),
            ma_bull_stack=int(row.ma_bull_stack),
        )
        grouped.setdefault(candidate.selection_date, []).append(candidate)
    return grouped


def select_candidates(
    bars_by_symbol: dict[str, pd.DataFrame],
    names: dict[str, str],
    *,
    start_date: date,
    end_date: date,
    params: SelectionParams,
) -> dict[date, list[Candidate]]:
    grouped: dict[date, list[Candidate]] = {}
    for symbol, bars in bars_by_symbol.items():
        rows = bars[(bars["trade_date"] >= start_date) & (bars["trade_date"] <= end_date)]
        for index_position in range(len(rows)):
            signal = rows.iloc[index_position]
            source_position = bars.index.get_loc(rows.index[index_position])
            if source_position == 0:
                continue
            previous = bars.iloc[source_position - 1]
            if not is_limit_up(signal, params):
                continue
            if params.require_first_board and is_limit_up(previous, params):
                continue
            candidate = candidate_from_row(symbol=symbol, name=names.get(symbol, ""), signal=signal, params=params)
            if candidate is not None:
                grouped.setdefault(candidate.selection_date, []).append(candidate)
    for trade_date, candidates in grouped.items():
        candidates.sort(key=lambda item: (-item.composite_score, item.symbol))
    return grouped


def price_row(bars_by_symbol: dict[str, pd.DataFrame], symbol: str, trade_date: date) -> dict[str, float] | None:
    bars = bars_by_symbol.get(symbol)
    if bars is None:
        return None
    rows = bars[bars["trade_date"] == trade_date]
    if rows.empty:
        return None
    row = rows.iloc[-1]
    return {"open": float(row["open"]), "high": float(row["high"]), "close": float(row["close"])}


def apply_score_gate(candidates: list[Candidate], params: ExecutionParams) -> list[Candidate]:
    if not candidates:
        return []
    if candidates[0].composite_score < params.min_composite_score:
        return []
    if params.min_score_gap > 0 and len(candidates) >= 2:
        if candidates[0].composite_score - candidates[1].composite_score < params.min_score_gap:
            return []
    return candidates


def run_signal_backtest(
    *,
    selection_map: dict[date, list[Candidate]],
    bars_by_symbol: dict[str, pd.DataFrame],
    trade_dates: list[date],
    params: ExecutionParams,
) -> dict[str, float]:
    returns: list[float] = []
    max_high_returns: list[float] = []
    hit_count = 0
    incomplete_count = 0
    trade_index = {trade_date: index for index, trade_date in enumerate(trade_dates)}
    for selection_date in trade_dates:
        gated = apply_score_gate(selection_map.get(selection_date, []), params)[: params.selection_top_n]
        if not gated:
            continue
        selection_index = trade_index[selection_date]
        buy_index = selection_index + 1
        sell_index = buy_index + params.holding_period
        if buy_index >= len(trade_dates) or sell_index >= len(trade_dates):
            incomplete_count += len(gated)
            continue
        buy_date = trade_dates[buy_index]
        sell_date = trade_dates[sell_index]
        for candidate in gated:
            hit_count += 1
            buy_bar = price_row(bars_by_symbol, candidate.symbol, buy_date)
            sell_bar = price_row(bars_by_symbol, candidate.symbol, sell_date)
            if buy_bar is None or sell_bar is None or buy_bar["open"] <= 0:
                incomplete_count += 1
                continue
            buy_price = buy_bar["open"]
            sell_price = sell_bar["close"]
            returns.append((sell_price - buy_price) * 100 / buy_price)
            highs = []
            for index in range(buy_index, sell_index + 1):
                bar = price_row(bars_by_symbol, candidate.symbol, trade_dates[index])
                if bar is not None:
                    highs.append(bar["high"])
            if highs:
                max_high_returns.append((max(highs) - buy_price) * 100 / buy_price)
    completed = len(returns)
    wins = sum(1 for value in returns if value > 0)
    return {
        "hit_count": hit_count,
        "completed_count": completed,
        "incomplete_count": incomplete_count,
        "win_rate_pct": wins * 100 / completed if completed else 0.0,
        "average_return_pct": sum(returns) / completed if completed else 0.0,
        "high_ge_3_rate_pct": sum(1 for value in max_high_returns if value >= 3) * 100 / len(max_high_returns)
        if max_high_returns
        else 0.0,
        "high_ge_5_rate_pct": sum(1 for value in max_high_returns if value >= 5) * 100 / len(max_high_returns)
        if max_high_returns
        else 0.0,
        "high_ge_10_rate_pct": sum(1 for value in max_high_returns if value >= 10) * 100 / len(max_high_returns)
        if max_high_returns
        else 0.0,
        "average_max_high_return_pct": sum(max_high_returns) / len(max_high_returns) if max_high_returns else 0.0,
    }


@dataclass
class OpenPosition:
    symbol: str
    name: str
    selection_date: date
    buy_date: date
    buy_index: int
    buy_price: float
    shares: int
    buy_amount: float
    max_high_return_pct: float = 0.0


def run_small_account_backtest(
    *,
    selection_map: dict[date, list[Candidate]],
    bars_by_symbol: dict[str, pd.DataFrame],
    trade_dates: list[date],
    params: ExecutionParams,
) -> tuple[dict[str, float], list[dict[str, Any]], list[dict[str, Any]]]:
    cash = params.initial_capital
    open_position: OpenPosition | None = None
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    trade_index = {trade_date: index for index, trade_date in enumerate(trade_dates)}

    for current_index, current_date in enumerate(trade_dates):
        if open_position is not None:
            bar = price_row(bars_by_symbol, open_position.symbol, current_date)
            if bar is not None and current_index > open_position.buy_index:
                high_return = (bar["high"] - open_position.buy_price) * 100 / open_position.buy_price
                open_position.max_high_return_pct = max(open_position.max_high_return_pct, high_return)
                close_return = (bar["close"] - open_position.buy_price) * 100 / open_position.buy_price
                holding_days = current_index - open_position.buy_index
                exit_reason = ""
                if close_return >= params.take_profit_pct:
                    exit_reason = "take_profit"
                elif close_return <= params.stop_loss_pct:
                    exit_reason = "stop_loss"
                elif holding_days >= params.holding_period:
                    exit_reason = "holding_period"
                if exit_reason:
                    sell_amount = bar["close"] * open_position.shares
                    cash += sell_amount
                    trades.append(
                        {
                            "symbol": open_position.symbol,
                            "name": open_position.name,
                            "selection_date": open_position.selection_date,
                            "buy_date": open_position.buy_date,
                            "sell_date": current_date,
                            "buy_price": open_position.buy_price,
                            "sell_price": bar["close"],
                            "shares": open_position.shares,
                            "return_pct": close_return,
                            "max_high_return_pct": open_position.max_high_return_pct,
                            "exit_reason": exit_reason,
                            "status": "closed",
                        }
                    )
                    open_position = None

        previous_date = trade_dates[current_index - 1] if current_index > 0 else None
        if open_position is None and previous_date is not None:
            candidates = apply_score_gate(selection_map.get(previous_date, []), params)[: params.selection_top_n]
            for candidate in candidates:
                bar = price_row(bars_by_symbol, candidate.symbol, current_date)
                if bar is None or bar["open"] <= 0:
                    continue
                budget = cash * params.position_size_pct / 100.0
                shares = int(budget // (bar["open"] * 100)) * 100
                if shares < 100:
                    continue
                buy_amount = shares * bar["open"]
                cash -= buy_amount
                open_position = OpenPosition(
                    symbol=candidate.symbol,
                    name=candidate.name,
                    selection_date=previous_date,
                    buy_date=current_date,
                    buy_index=current_index,
                    buy_price=bar["open"],
                    shares=shares,
                    buy_amount=buy_amount,
                )
                break

        market_value = 0.0
        if open_position is not None:
            bar = price_row(bars_by_symbol, open_position.symbol, current_date)
            market_value = open_position.shares * (bar["close"] if bar is not None else open_position.buy_price)
        equity_curve.append({"trade_date": current_date, "cash": cash, "market_value": market_value, "assets": cash + market_value})

    if open_position is not None:
        last_date = trade_dates[-1]
        bar = price_row(bars_by_symbol, open_position.symbol, last_date)
        sell_price = bar["close"] if bar is not None else open_position.buy_price
        trades.append(
            {
                "symbol": open_position.symbol,
                "name": open_position.name,
                "selection_date": open_position.selection_date,
                "buy_date": open_position.buy_date,
                "sell_date": None,
                "buy_price": open_position.buy_price,
                "sell_price": sell_price,
                "shares": open_position.shares,
                "return_pct": (sell_price - open_position.buy_price) * 100 / open_position.buy_price,
                "max_high_return_pct": open_position.max_high_return_pct,
                "exit_reason": "",
                "status": "open",
            }
        )

    closed = [trade for trade in trades if trade["status"] == "closed"]
    returns = [trade["return_pct"] for trade in closed]
    final_assets = equity_curve[-1]["assets"] if equity_curve else params.initial_capital
    peak = params.initial_capital
    max_drawdown_pct = 0.0
    for point in equity_curve:
        peak = max(peak, point["assets"])
        if peak > 0:
            max_drawdown_pct = max(max_drawdown_pct, (peak - point["assets"]) * 100 / peak)
    total_return_pct = (final_assets - params.initial_capital) * 100 / params.initial_capital
    win_rate_pct = sum(1 for value in returns if value > 0) * 100 / len(returns) if returns else 0.0
    summary = {
        "initial_capital": params.initial_capital,
        "final_assets": final_assets,
        "total_return_pct": total_return_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "closed_trade_count": len(closed),
        "total_trade_count": len(trades),
        "win_rate_pct": win_rate_pct,
        "average_return_pct": sum(returns) / len(returns) if returns else 0.0,
        "high_ge_5_rate_pct": sum(1 for trade in closed if trade["max_high_return_pct"] >= 5) * 100 / len(closed)
        if closed
        else 0.0,
    }
    return summary, trades, equity_curve


def objective_score(summary: dict[str, float]) -> float:
    closed = summary["closed_trade_count"]
    penalty = 100.0 if closed < 5 else 0.0
    return (
        summary["total_return_pct"]
        - 0.35 * summary["max_drawdown_pct"]
        + 0.08 * summary["win_rate_pct"]
        + min(closed, 80) * 0.05
        - penalty
    )


def random_selection_params(rng: random.Random) -> SelectionParams:
    min_change = rng.choice([-20.0, -10.0, 0.0, 3.0])
    max_change = rng.choice([30.0, 50.0, 80.0])
    if min_change >= max_change:
        min_change, max_change = -20.0, 80.0
    return SelectionParams(
        limit_up_close_high_min_ratio=rng.choice([0.99, 0.993, 0.995, 0.998]),
        volume_ratio_prev5_min=rng.choice([0.2, 0.5, 0.8]),
        volume_ratio_prev5_max=rng.choice([1.5, 2.0, 2.5, 3.0, 4.0]),
        require_break_prev20_high=rng.choice([False, True]),
        require_ma_bull_stack=rng.choice([False, True]),
        prev10_change_sum_min=min_change,
        prev10_change_sum_max=max_change,
    )


def random_execution_params(rng: random.Random, initial_capital: float) -> ExecutionParams:
    return ExecutionParams(
        holding_period=rng.choice([1, 2, 3, 4, 5]),
        take_profit_pct=rng.choice([6.0, 8.0, 10.0, 15.0]),
        stop_loss_pct=rng.choice([-8.0, -5.0, -3.0]),
        selection_top_n=rng.choice([1, 2, 3, 5]),
        min_composite_score=rng.choice([0.0, 60.0, 70.0, 75.0, 80.0, 85.0, 90.0]),
        min_score_gap=rng.choice([0.0, 3.0, 5.0, 8.0, 10.0]),
        initial_capital=initial_capital,
    )


def selection_key(params: SelectionParams) -> tuple[Any, ...]:
    return tuple(asdict(params).items())


def load_original_candidates(db_path: Path, start_date: date, end_date: date) -> dict[date, list[Candidate]]:
    if not db_path.exists():
        return {}
    params = SelectionParams()
    with duckdb.connect(str(db_path)) as connection:
        rows = connection.execute(
            """
            WITH bars AS (
              SELECT
                s.symbol, s.name, d.security_id, d.trade_date, d.open_price, d.high_price,
                d.close_price, d.volume, d.amount, d.change_pct, d.turnover_ratio,
                d.ma5_price, d.ma10_price, d.ma20_price,
                avg(d.volume) OVER (PARTITION BY d.security_id ORDER BY d.trade_date ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS prev5_avg_volume,
                avg(d.volume) OVER (PARTITION BY d.security_id ORDER BY d.trade_date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS prev20_avg_volume,
                sum(d.change_pct) OVER (PARTITION BY d.security_id ORDER BY d.trade_date ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS prev5_change_sum,
                sum(d.change_pct) OVER (PARTITION BY d.security_id ORDER BY d.trade_date ROWS BETWEEN 10 PRECEDING AND 1 PRECEDING) AS prev10_change_sum,
                max(d.high_price) OVER (PARTITION BY d.security_id ORDER BY d.trade_date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS prev20_high
              FROM daily_bar d
              JOIN security_master s ON s.security_id = d.security_id
            ),
            marked AS (
              SELECT *,
                CASE WHEN change_pct >= ? AND high_price > 0 AND close_price >= high_price * ? AND close_price > open_price THEN 1 ELSE 0 END AS is_limit_up,
                CASE WHEN prev5_avg_volume > 0 THEN volume / prev5_avg_volume ELSE NULL END AS volume_ratio_prev5,
                CASE WHEN prev20_avg_volume > 0 THEN volume / prev20_avg_volume ELSE NULL END AS volume_ratio_prev20,
                CASE WHEN amount IS NOT NULL THEN amount / 100000000.0 ELSE NULL END AS amount_yi,
                CASE WHEN ma20_price > 0 THEN (close_price - ma20_price) / ma20_price * 100 ELSE NULL END AS close_vs_ma20_pct,
                CASE WHEN ma10_price > 0 THEN (ma5_price - ma10_price) / ma10_price * 100 ELSE NULL END AS ma5_vs_ma10_pct,
                CASE WHEN ma20_price > 0 THEN (ma10_price - ma20_price) / ma20_price * 100 ELSE NULL END AS ma10_vs_ma20_pct,
                CASE WHEN prev20_high > 0 THEN (close_price - prev20_high) / prev20_high * 100 ELSE NULL END AS close_vs_prev20_high_pct,
                CASE WHEN close_price >= prev20_high THEN 1 ELSE 0 END AS break_prev20_high,
                CASE WHEN ma5_price > ma10_price AND ma10_price > ma20_price THEN 1 ELSE 0 END AS ma_bull_stack
              FROM bars
            ),
            first_boards AS (
              SELECT *, lag(is_limit_up, 1) OVER (PARTITION BY security_id ORDER BY trade_date) AS prev_is_limit_up
              FROM marked
            ),
            picked AS (
              SELECT *
              FROM first_boards
              WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
                AND is_limit_up = 1
                AND (? = false OR COALESCE(prev_is_limit_up, 0) = 0)
                AND volume_ratio_prev5 BETWEEN ? AND ?
                AND turnover_ratio BETWEEN ? AND ?
                AND amount_yi >= ?
                AND (? = false OR break_prev20_high = 1)
                AND (? = false OR ma_bull_stack = 1)
                AND prev10_change_sum BETWEEN ? AND ?
            ),
            score_calc AS (
              SELECT *,
                (
                  LEAST(GREATEST(COALESCE(close_vs_ma20_pct, 0) / 20.0 * 100, 0), 100) * 0.35
                  + LEAST(GREATEST(COALESCE(ma5_vs_ma10_pct, 0) / 5.0 * 100, 0), 100) * 0.25
                  + LEAST(GREATEST(COALESCE(ma10_vs_ma20_pct, 0) / 5.0 * 100, 0), 100) * 0.25
                  + ma_bull_stack * 100 * 0.15
                ) AS trend_quality,
                (
                  (100 - LEAST(ABS(COALESCE(volume_ratio_prev5, 0) - 1.1) / 1.1 * 100, 100)) * 0.65
                  + (100 - LEAST(ABS(COALESCE(volume_ratio_prev20, 0) - 1.2) / 1.2 * 100, 100)) * 0.35
                ) AS volume_health,
                (
                  break_prev20_high * 65
                  + LEAST(GREATEST(COALESCE(close_vs_prev20_high_pct, 0) / 8.0 * 100, 0), 100) * 0.35
                ) AS breakout_quality,
                (
                  LEAST(GREATEST(COALESCE(prev5_change_sum, 0) / 8.0 * 100, 0), 100) * 0.45
                  + LEAST(GREATEST(COALESCE(prev10_change_sum, 0) / 15.0 * 100, 0), 100) * 0.55
                ) AS preheat_quality,
                (100 - LEAST(ABS(COALESCE(turnover_ratio, 0) - 8.0) / 8.0 * 100, 100)) AS turnover_activity,
                (
                  CASE WHEN COALESCE(volume_ratio_prev5, 0) > 2.5 THEN LEAST((volume_ratio_prev5 - 2.5) / 2.0 * 18, 18) ELSE 0 END
                  + CASE WHEN COALESCE(turnover_ratio, 0) > 25 THEN LEAST((turnover_ratio - 25.0) / 20.0 * 12, 12) ELSE 0 END
                  + CASE WHEN COALESCE(prev10_change_sum, 0) > 35 THEN LEAST((prev10_change_sum - 35.0) / 35.0 * 10, 10) ELSE 0 END
                ) AS overheating_penalty
              FROM picked
            )
            SELECT trade_date, symbol, name,
              trend_quality * 0.30 + volume_health * 0.25 + breakout_quality * 0.20 + preheat_quality * 0.15 + turnover_activity * 0.10 - overheating_penalty AS composite_score,
              open_price, close_price, ma5_price, volume_ratio_prev5, volume_ratio_prev20, amount_yi, turnover_ratio,
              prev5_change_sum, prev10_change_sum, break_prev20_high, ma_bull_stack
            FROM score_calc
            ORDER BY trade_date, composite_score DESC, symbol ASC
            """,
            [
                params.limit_up_min_pct,
                params.limit_up_close_high_min_ratio,
                start_date,
                end_date,
                params.require_first_board,
                params.volume_ratio_prev5_min,
                params.volume_ratio_prev5_max,
                params.turnover_min,
                params.turnover_max,
                params.amount_min_yi,
                params.require_break_prev20_high,
                params.require_ma_bull_stack,
                params.prev10_change_sum_min,
                params.prev10_change_sum_max,
            ],
        ).fetchall()
    grouped: dict[date, list[Candidate]] = {}
    for row in rows:
        candidate = Candidate(
            selection_date=row[0],
            symbol=to_tdx_symbol(row[1]),
            name=row[2],
            composite_score=float(row[3]),
            open_price=float(row[4]),
            close_price=float(row[5]),
            ma5=float(row[6]) if row[6] is not None else 0.0,
            volume_ratio_prev5=float(row[7]) if row[7] is not None else 0.0,
            volume_ratio_prev20=float(row[8]) if row[8] is not None else 0.0,
            amount_yi=float(row[9]) if row[9] is not None else 0.0,
            turnover=float(row[10]) if row[10] is not None else NEUTRAL_TURNOVER,
            prev5_change_sum=float(row[11]) if row[11] is not None else 0.0,
            prev10_change_sum=float(row[12]) if row[12] is not None else 0.0,
            break_prev20_high=int(row[13]) if row[13] is not None else 0,
            ma_bull_stack=int(row[14]) if row[14] is not None else 0,
        )
        grouped.setdefault(candidate.selection_date, []).append(candidate)
    return grouped


def load_original_bars(db_path: Path, symbols: set[str], start_date: date, end_date: date) -> dict[str, pd.DataFrame]:
    if not db_path.exists() or not symbols:
        return {}
    db_symbols = sorted({to_db_symbol(symbol) for symbol in symbols})
    placeholders = ", ".join("?" for _ in db_symbols)
    with duckdb.connect(str(db_path)) as connection:
        rows = connection.execute(
            f"""
            SELECT s.symbol, d.trade_date, d.open_price, d.high_price, d.low_price, d.close_price
            FROM daily_bar d
            JOIN security_master s ON s.security_id = d.security_id
            WHERE s.symbol IN ({placeholders})
              AND d.trade_date BETWEEN ? AND ?
            ORDER BY s.symbol, d.trade_date
            """,
            [*db_symbols, start_date, end_date],
        ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for symbol, trade_date, open_price, high_price, low_price, close_price in rows:
        symbol = to_tdx_symbol(symbol)
        grouped.setdefault(symbol, []).append(
            {
                "trade_date": trade_date,
                "open": float(open_price),
                "high": float(high_price),
                "low": float(low_price),
                "close": float(close_price),
            }
        )
    return {symbol: pd.DataFrame(items) for symbol, items in grouped.items()}


def compare_candidates(
    tdx_map: dict[date, list[Candidate]],
    original_map: dict[date, list[Candidate]],
    trade_dates: list[date],
) -> pd.DataFrame:
    rows = []
    for trade_date in trade_dates:
        tdx_candidates = tdx_map.get(trade_date, [])
        original_candidates = original_map.get(trade_date, [])
        tdx_top5 = {candidate.symbol for candidate in tdx_candidates[:5]}
        original_top5 = {candidate.symbol for candidate in original_candidates[:5]}
        common = tdx_top5 & original_top5
        original_scores = {candidate.symbol: candidate.composite_score for candidate in original_candidates}
        score_diffs = [
            candidate.composite_score - original_scores[candidate.symbol]
            for candidate in tdx_candidates[:5]
            if candidate.symbol in original_scores
        ]
        rows.append(
            {
                "trade_date": trade_date,
                "tdx_count": len(tdx_candidates),
                "original_count": len(original_candidates),
                "tdx_top1": tdx_candidates[0].symbol if tdx_candidates else "",
                "original_top1": original_candidates[0].symbol if original_candidates else "",
                "top1_match": bool(tdx_candidates and original_candidates and tdx_candidates[0].symbol == original_candidates[0].symbol),
                "top5_overlap_count": len(common),
                "top5_overlap_rate_pct": len(common) * 100 / 5 if tdx_candidates or original_candidates else 0.0,
                "top5_common_score_diff_avg": sum(score_diffs) / len(score_diffs) if score_diffs else 0.0,
                "tdx_top1_score": tdx_candidates[0].composite_score if tdx_candidates else None,
                "original_top1_score": original_candidates[0].composite_score if original_candidates else None,
            }
        )
    return pd.DataFrame(rows)


def write_recommended_params(path: Path, selection_params: SelectionParams, execution_params: ExecutionParams, summary: dict[str, float]) -> None:
    lines = [
        "selection:",
        "  params:",
        *[f"    {key}: {value}" for key, value in asdict(selection_params).items()],
        "strategy_backtest:",
        *[f"  {key}: {value}" for key, value in asdict(execution_params).items()],
        "tdx_result:",
        *[f"  {key}: {value}" for key, value in summary.items()],
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def candidate_rows(selection_map: dict[date, list[Candidate]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trade_date, candidates in sorted(selection_map.items()):
        for rank, candidate in enumerate(candidates, start=1):
            row = asdict(candidate)
            row["rank"] = rank
            row["selection_date"] = trade_date
            rows.append(row)
    return rows


def run() -> None:
    parser = argparse.ArgumentParser(description="TDX first-limit-up trend continuation research and optimizer")
    parser.add_argument("--tdx-user-dir", type=Path, default=DEFAULT_TDX_USER_DIR)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--start-date", type=parse_date, default=DEFAULT_START)
    parser.add_argument("--end-date", type=parse_date, default=date.today())
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--batch-size", type=int, default=160)
    parser.add_argument("--max-trials", type=int, default=200)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--skip-original-compare", action="store_true")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    tq = ensure_tdx_import(args.tdx_user_dir)
    tq.initialize(__file__)
    try:
        codes, names = load_tdx_universe(tq)
        print(f"[TDX] filtered universe={len(codes)}")
        bars_by_symbol, trade_dates, turnover_available = fetch_tdx_bars(
            tq,
            codes=codes,
            names=names,
            start_date=args.start_date,
            end_date=args.end_date,
            batch_size=args.batch_size,
        )
    finally:
        tq.close()

    if not trade_dates:
        raise RuntimeError("No TDX trade dates loaded. Check TDX client and local daily data.")

    signal_frame = build_signal_frame(
        bars_by_symbol,
        names,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    if signal_frame.empty:
        raise RuntimeError("No TDX signal rows built from loaded bars.")
    print(f"[TDX] signal rows={len(signal_frame)}")

    default_selection = SelectionParams()
    default_execution = ExecutionParams()
    tdx_default_map = select_candidates_from_frame(signal_frame, default_selection)
    pd.DataFrame(candidate_rows(tdx_default_map)).to_csv(
        args.output_dir / "tdx_default_candidates.csv",
        index=False,
        encoding="utf-8-sig",
    )

    default_signal_summary = run_signal_backtest(
        selection_map=tdx_default_map,
        bars_by_symbol=bars_by_symbol,
        trade_dates=trade_dates,
        params=default_execution,
    )
    default_account_summary, default_trades, default_equity = run_small_account_backtest(
        selection_map=tdx_default_map,
        bars_by_symbol=bars_by_symbol,
        trade_dates=trade_dates,
        params=default_execution,
    )

    comparison_rows = [
        {"source": "tdx_default_signal", **default_signal_summary},
        {"source": "tdx_default_small_account", **default_account_summary},
    ]

    if not args.skip_original_compare:
        original_map = load_original_candidates(args.db_path, args.start_date, args.end_date)
        compare_candidates(tdx_default_map, original_map, trade_dates).to_csv(
            args.output_dir / "tdx_vs_original_candidates.csv",
            index=False,
            encoding="utf-8-sig",
        )
        original_symbols = {
            candidate.symbol
            for candidates in original_map.values()
            for candidate in candidates[:5]
        }
        original_bars = load_original_bars(
            args.db_path,
            original_symbols,
            trade_dates[0],
            trade_dates[-1],
        )
        original_signal_summary = run_signal_backtest(
            selection_map=original_map,
            bars_by_symbol=original_bars,
            trade_dates=trade_dates,
            params=default_execution,
        )
        original_account_summary, _, _ = run_small_account_backtest(
            selection_map=original_map,
            bars_by_symbol=original_bars,
            trade_dates=trade_dates,
            params=default_execution,
        )
        comparison_rows.extend(
            [
                {"source": "original_default_signal", **original_signal_summary},
                {"source": "original_default_small_account", **original_account_summary},
            ]
        )

    pd.DataFrame(comparison_rows).to_csv(
        args.output_dir / "tdx_vs_original_backtest.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(default_trades).to_csv(args.output_dir / "default_tdx_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(default_equity).to_csv(args.output_dir / "default_tdx_equity.csv", index=False, encoding="utf-8-sig")

    rng = random.Random(args.seed)
    selection_cache: dict[tuple[Any, ...], dict[date, list[Candidate]]] = {selection_key(default_selection): tdx_default_map}
    trial_rows: list[dict[str, Any]] = []
    for trial in range(1, args.max_trials + 1):
        selection_params = random_selection_params(rng)
        execution_params = random_execution_params(rng, initial_capital=10000.0)
        key = selection_key(selection_params)
        if key not in selection_cache:
            selection_cache[key] = select_candidates_from_frame(signal_frame, selection_params)
        summary, _, _ = run_small_account_backtest(
            selection_map=selection_cache[key],
            bars_by_symbol=bars_by_symbol,
            trade_dates=trade_dates,
            params=execution_params,
        )
        trial_row = {
            "trial": trial,
            "objective_score": objective_score(summary),
            "turnover_available": turnover_available,
            **{f"selection.{key}": value for key, value in asdict(selection_params).items()},
            **{f"execution.{key}": value for key, value in asdict(execution_params).items()},
            **summary,
        }
        trial_rows.append(trial_row)
        if trial % 20 == 0 or trial == args.max_trials:
            print(f"[OPT] completed {trial}/{args.max_trials}")

    trials_df = pd.DataFrame(trial_rows).sort_values(
        ["objective_score", "total_return_pct", "closed_trade_count"],
        ascending=[False, False, False],
    )
    trials_df.to_csv(args.output_dir / "random_trials.csv", index=False, encoding="utf-8-sig")

    period_specs = [
        ("2024", date(2024, 1, 2), date(2024, 12, 31)),
        ("2025", date(2025, 1, 2), date(2025, 12, 31)),
        ("2026", date(2026, 1, 2), trade_dates[-1]),
        ("full", args.start_date, trade_dates[-1]),
    ]
    cross_rows: list[dict[str, Any]] = []
    top_trials = trials_df.head(10).to_dict("records")
    for ranked, row in enumerate(top_trials, start=1):
        selection_params = SelectionParams(
            limit_up_min_pct=float(row["selection.limit_up_min_pct"]),
            limit_up_close_high_min_ratio=float(row["selection.limit_up_close_high_min_ratio"]),
            volume_ratio_prev5_min=float(row["selection.volume_ratio_prev5_min"]),
            volume_ratio_prev5_max=float(row["selection.volume_ratio_prev5_max"]),
            turnover_min=float(row["selection.turnover_min"]),
            turnover_max=float(row["selection.turnover_max"]),
            amount_min_yi=float(row["selection.amount_min_yi"]),
            require_first_board=bool(row["selection.require_first_board"]),
            require_break_prev20_high=bool(row["selection.require_break_prev20_high"]),
            require_ma_bull_stack=bool(row["selection.require_ma_bull_stack"]),
            prev10_change_sum_min=float(row["selection.prev10_change_sum_min"]),
            prev10_change_sum_max=float(row["selection.prev10_change_sum_max"]),
        )
        execution_params = ExecutionParams(
            holding_period=int(row["execution.holding_period"]),
            take_profit_pct=float(row["execution.take_profit_pct"]),
            stop_loss_pct=float(row["execution.stop_loss_pct"]),
            selection_top_n=int(row["execution.selection_top_n"]),
            min_composite_score=float(row["execution.min_composite_score"]),
            min_score_gap=float(row["execution.min_score_gap"]),
            initial_capital=10000.0,
        )
        key = selection_key(selection_params)
        selection_map = selection_cache[key]
        for label, period_start, period_end in period_specs:
            period_dates = [trade_date for trade_date in trade_dates if period_start <= trade_date <= period_end]
            if not period_dates:
                continue
            summary, _, _ = run_small_account_backtest(
                selection_map=selection_map,
                bars_by_symbol=bars_by_symbol,
                trade_dates=period_dates,
                params=execution_params,
            )
            cross_rows.append({"rank": ranked, "trial": row["trial"], "period": label, **summary})

    cross_df = pd.DataFrame(cross_rows)
    cross_df.to_csv(
        args.output_dir / "top_trials_cross_period.csv",
        index=False,
        encoding="utf-8-sig",
    )

    recommended_trial = int(trials_df.iloc[0]["trial"])
    recommendation_mode = "highest_objective"
    if not cross_df.empty:
        robust_trials: list[int] = []
        for trial, group in cross_df.groupby("trial"):
            periods = set(group["period"].tolist())
            if not {"2024", "2025", "2026", "full"}.issubset(periods):
                continue
            if (group["total_return_pct"] > 0).all() and group.loc[group["period"] == "full", "max_drawdown_pct"].iloc[0] <= 50:
                robust_trials.append(int(trial))
        if robust_trials:
            robust_df = trials_df[trials_df["trial"].isin(robust_trials)]
            recommended_trial = int(robust_df.iloc[0]["trial"])
            recommendation_mode = "robust_cross_period"

    best = trials_df[trials_df["trial"] == recommended_trial].iloc[0].to_dict()
    best_selection = SelectionParams(
        limit_up_min_pct=float(best["selection.limit_up_min_pct"]),
        limit_up_close_high_min_ratio=float(best["selection.limit_up_close_high_min_ratio"]),
        volume_ratio_prev5_min=float(best["selection.volume_ratio_prev5_min"]),
        volume_ratio_prev5_max=float(best["selection.volume_ratio_prev5_max"]),
        turnover_min=float(best["selection.turnover_min"]),
        turnover_max=float(best["selection.turnover_max"]),
        amount_min_yi=float(best["selection.amount_min_yi"]),
        require_first_board=bool(best["selection.require_first_board"]),
        require_break_prev20_high=bool(best["selection.require_break_prev20_high"]),
        require_ma_bull_stack=bool(best["selection.require_ma_bull_stack"]),
        prev10_change_sum_min=float(best["selection.prev10_change_sum_min"]),
        prev10_change_sum_max=float(best["selection.prev10_change_sum_max"]),
    )
    best_execution = ExecutionParams(
        holding_period=int(best["execution.holding_period"]),
        take_profit_pct=float(best["execution.take_profit_pct"]),
        stop_loss_pct=float(best["execution.stop_loss_pct"]),
        selection_top_n=int(best["execution.selection_top_n"]),
        min_composite_score=float(best["execution.min_composite_score"]),
        min_score_gap=float(best["execution.min_score_gap"]),
        initial_capital=10000.0,
    )
    best["recommendation_mode"] = recommendation_mode
    write_recommended_params(args.output_dir / "recommended_params.yml", best_selection, best_execution, best)

    caveats = [
        f"period: {trade_dates[0]} to {trade_dates[-1]}",
        f"tdx_turnover_available: {turnover_available}",
        "if turnover is unavailable, neutral turnover 8.0 is used for filter and score",
        "small-account backtest uses next-day open buys, close-based exits, and 100-share lots",
    ]
    (args.output_dir / "README.txt").write_text("\n".join(caveats) + "\n", encoding="utf-8")
    print(f"[DONE] wrote outputs to {args.output_dir}")


if __name__ == "__main__":
    run()
