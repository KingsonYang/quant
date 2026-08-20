"""Feature extraction from daily_bar for pool-level scoring (v2).

Given a list of PatternSelectionCandidate objects and a DuckDB connection,
computes additional features from historical K-line data that are not
available in the pattern selection SQL output.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np

_hs300_cache = None


def _get_hs300_features(connection):
    global _hs300_cache
    if _hs300_cache is not None:
        return _hs300_cache
    import pandas as pd
    hs300 = connection.execute(
        "SELECT trade_date, close_price FROM hs300_bar ORDER BY trade_date",
    ).fetchdf()
    if hs300.empty:
        _hs300_cache = pd.DataFrame()
        return _hs300_cache
    hs300["trade_date"] = hs300["trade_date"].astype(str)
    hs300 = hs300.sort_values("trade_date").reset_index(drop=True)
    hs300["hs_mom_20d"] = hs300["close_price"].pct_change(20)
    hs300["mkt_ret_5d"] = hs300["close_price"].pct_change(5)
    hs300["mkt_ret_10d"] = hs300["close_price"].pct_change(10)
    hs300["mkt_ret_20d"] = hs300["close_price"].pct_change(20)
    hs300["mkt_above_ma5"] = (hs300["close_price"] > hs300["close_price"].rolling(5).mean()).astype(float)
    hs300["mkt_above_ma20"] = (hs300["close_price"] > hs300["close_price"].rolling(20).mean()).astype(float)
    hs300["mkt_vol_10d"] = hs300["close_price"].pct_change().rolling(10).std()
    hs300["mkt_vol_20d"] = hs300["close_price"].pct_change().rolling(20).std()
    _md = hs300["close_price"].diff()
    _mg = _md.clip(lower=0).rolling(14).mean()
    _ml = (-_md.clip(upper=0)).rolling(14).mean()
    hs300["mkt_rsi_14"] = 100 - 100 / (1 + _mg / (_ml + 1e-10))
    hs300["mkt_consec_up"] = (hs300["close_price"].pct_change() > 0).rolling(5).sum()
    hs300["mkt_consec_down"] = (hs300["close_price"].pct_change() <= 0).rolling(5).sum()
    hs300 = hs300.fillna(0)
    _hs300_cache = hs300
    return hs300



def _safe_float(val: Any) -> float:
    if val is None:
        return np.nan
    try:
        f = float(val)
        return f if np.isfinite(f) else np.nan
    except (TypeError, ValueError):
        return np.nan


def extract_daily_bar_features(
    connection,
    candidates: list,
    lookback_days: int = 90,
) -> dict[int, dict[str, float]]:
    """Extract features from daily_bar for each candidate.

    Returns {candidate_index: {feature_name: value}}.
    Features are computed from data up to and including the selection_date.
    """
    if not candidates:
        return {}

    # Collect unique (symbol, selection_date, d0_date) triples
    # and resolve security_ids
    symbols = list({c.symbol for c in candidates})
    if not symbols:
        return {}

    sec_rows = connection.execute(
        "SELECT symbol, security_id FROM security_master WHERE symbol = ANY(?)",
        [symbols],
    ).fetchall()
    sym_to_sid = {r[0]: r[1] for r in sec_rows}

    # Determine date range for batch query
    all_dates = [c.selection_date for c in candidates]
    min_date = min(all_dates)
    max_date = max(all_dates)
    # Extend lookback before earliest date
    from datetime import timedelta
    fetch_start = min_date - timedelta(days=lookback_days * 2)  # calendar days

    sec_ids = list(set(sym_to_sid.values()))
    if not sec_ids:
        return {}

    # Batch fetch all daily_bar data
    bars = connection.execute(
        "SELECT security_id, trade_date, open_price, high_price, low_price, "
        "close_price, pre_close_price, volume, amount, change_pct, turnover_ratio, "
        "ma5_price, ma10_price, ma20_price, ma5_volume, ma10_volume, ma20_volume "
        "FROM daily_bar WHERE security_id = ANY(?) AND trade_date >= ? "
        "ORDER BY security_id, trade_date",
        [sec_ids, fetch_start],
    ).fetchdf()

    if bars.empty:
        return {}

    bars["trade_date"] = bars["trade_date"].astype(str)

    # Group by security_id for efficient lookup
    bars_by_sid: dict[int, Any] = {}
    for sid, group in bars.groupby("security_id"):
        bars_by_sid[sid] = group.sort_values("trade_date")

    result: dict[int, dict[str, float]] = {}

    for idx, cand in enumerate(candidates):
        sid = sym_to_sid.get(cand.symbol)
        if sid is None or sid not in bars_by_sid:
            continue

        stock_bars = bars_by_sid[sid]
        sel_date_str = str(cand.selection_date)
        d0_date_str = str(cand.d0_date) if cand.d0_date else None

        # Filter to data up to and including selection_date
        sel_data = stock_bars[stock_bars["trade_date"] <= sel_date_str]
        if len(sel_data) < 20:
            continue

        sel_row = sel_data[sel_data["trade_date"] == sel_date_str]
        if sel_row.empty:
            continue
        sel_row = sel_row.iloc[0]

        # D0 data
        d0_row = None
        if d0_date_str:
            d0_data = stock_bars[stock_bars["trade_date"] <= d0_date_str]
            if not d0_data.empty:
                d0_row = d0_data.iloc[-1]

        features: dict[str, float] = {}

        closes = sel_data["close_price"].values.astype(float)
        vols = sel_data["volume"].values.astype(float)
        close_f = _safe_float(sel_row["close_price"])
        pre_close = _safe_float(sel_row["pre_close_price"])

        # --- 1. Price Momentum ---
        if len(closes) > 5:
            features["mom_5d"] = (closes[-1] / closes[-6] - 1) * 100
        if len(closes) > 10:
            features["mom_10d"] = (closes[-1] / closes[-11] - 1) * 100
        if len(closes) > 20:
            features["mom_20d"] = (closes[-1] / closes[-21] - 1) * 100
        if len(closes) > 30:
            features["mom_30d"] = (closes[-1] / closes[-31] - 1) * 100

        # --- 2. Price Positioning ---
        ma5 = _safe_float(sel_row["ma5_price"])
        ma10 = _safe_float(sel_row["ma10_price"])
        ma20 = _safe_float(sel_row["ma20_price"])
        if not np.isnan(ma5) and ma5 > 0:
            features["close_vs_ma5"] = (close_f / ma5 - 1) * 100
        if not np.isnan(ma10) and ma10 > 0:
            features["close_vs_ma10"] = (close_f / ma10 - 1) * 100
        if not np.isnan(ma20) and ma20 > 0:
            features["close_vs_ma20"] = (close_f / ma20 - 1) * 100

        # --- 3. Range Positioning (20-day) ---
        if len(closes) >= 20:
            high_20 = float(np.max(closes[-20:]))
            low_20 = float(np.min(closes[-20:]))
            if high_20 > low_20:
                features["range_pos_20d"] = (close_f - low_20) / (high_20 - low_20)
            else:
                features["range_pos_20d"] = 0.5

        # --- 4. Volume Characteristics ---
        if len(vols) > 5 and np.mean(vols[-6:-1]) > 0:
            features["vol_ratio_ma5"] = vols[-1] / np.mean(vols[-6:-1])
        if len(vols) > 10 and np.mean(vols[-11:-1]) > 0:
            features["vol_ratio_ma10"] = vols[-1] / np.mean(vols[-11:-1])
        if len(vols) > 20 and np.mean(vols[-21:-1]) > 0:
            features["vol_ratio_ma20"] = vols[-1] / np.mean(vols[-21:-1])

        features["d1_turnover"] = _safe_float(sel_row["turnover_ratio"])

        if len(vols) >= 10:
            recent_vol = np.mean(vols[-5:])
            prev_vol = np.mean(vols[-10:-5])
            if prev_vol > 0:
                features["vol_trend_5d"] = recent_vol / prev_vol

        # --- 5. Volatility ---
        if len(closes) > 10:
            rets = np.diff(closes[-11:]) / closes[-11:-1] * 100
            features["volatility_10d"] = float(np.std(rets))
        if len(closes) > 20:
            rets20 = np.diff(closes[-21:]) / closes[-21:-1] * 100
            features["volatility_20d"] = float(np.std(rets20))

        # Amplitude on signal day
        high_f = _safe_float(sel_row["high_price"])
        low_f = _safe_float(sel_row["low_price"])
        if not np.isnan(pre_close) and pre_close > 0:
            features["d1_amplitude"] = (high_f - low_f) / pre_close * 100

        # --- 6. Signal Day Change ---
        features["d1_change_pct"] = _safe_float(sel_row["change_pct"])
        if d0_row is not None:
            features["d0_change_pct"] = _safe_float(d0_row["change_pct"])

        # --- 7. D0-to-D1 close change ---
        if d0_row is not None:
            d0_close = _safe_float(d0_row["close_price"])
            if not np.isnan(d0_close) and d0_close > 0:
                features["d1_vs_d0_close"] = (close_f / d0_close - 1) * 100

        # --- 8. Gap on signal day ---
        open_f = _safe_float(sel_row["open_price"])
        if not np.isnan(pre_close) and pre_close > 0:
            features["d1_gap"] = (open_f / pre_close - 1) * 100

        # --- 9. Candle body (signal day) ---
        if not np.isnan(pre_close) and pre_close > 0:
            body = abs(close_f - open_f)
            features["d1_body_pct"] = body / pre_close * 100
            features["d1_upper_shadow"] = (
                high_f - max(close_f, open_f)
            ) / pre_close * 100
            features["d1_lower_shadow"] = (
                min(close_f, open_f) - low_f
            ) / pre_close * 100

        result[idx] = features

    return result


def _compute_rsi(close_prices: np.ndarray, period: int = 14) -> float:
    if len(close_prices) < period + 1:
        return 50.0
    diffs = np.diff(close_prices)
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss < 1e-10:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _compute_macd(close_prices: np.ndarray) -> tuple[float, float, float]:
    n = len(close_prices)
    if n < 35:
        return 0.0, 0.0, 0.0
    # Incremental EMA computation (O(n) instead of O(n^2))
    mult12 = 2.0 / 13.0
    mult26 = 2.0 / 27.0
    ema12 = float(close_prices[0])
    ema26 = float(close_prices[0])
    macd_series = np.zeros(n)
    for i in range(1, n):
        ema12 = close_prices[i] * mult12 + ema12 * (1 - mult12)
        ema26 = close_prices[i] * mult26 + ema26 * (1 - mult26)
        macd_series[i] = ema12 - ema26
    macd = float(macd_series[-1])
    # Signal = EMA9 of MACD series
    mult9 = 2.0 / 10.0
    signal = float(macd_series[0])
    for i in range(1, n):
        signal = macd_series[i] * mult9 + signal * (1 - mult9)
    hist = macd - signal
    return macd, signal, hist


def _ema(values: np.ndarray, period: int) -> float:
    if len(values) < period:
        return float(np.mean(values)) if len(values) > 0 else 0.0
    multiplier = 2.0 / (period + 1)
    ema = float(np.mean(values[:period]))
    for v in values[period:]:
        ema = v * multiplier + ema * (1 - multiplier)
    return ema


def _compute_bb_pos(close_prices: np.ndarray, period: int = 20) -> float:
    if len(close_prices) < period:
        return 0.5
    window = close_prices[-period:]
    sma = np.mean(window)
    std = np.std(window)
    if std < 1e-10:
        return 0.5
    upper = sma + 2 * std
    lower = sma - 2 * std
    return float((close_prices[-1] - lower) / (upper - lower))


def _compute_atr_ratio(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14
) -> float:
    if len(closes) < period + 1:
        return 0.0
    trs = []
    for i in range(1, period + 1):
        idx = len(closes) - period - 1 + i
        tr = max(
            highs[idx] - lows[idx],
            abs(highs[idx] - closes[idx - 1]),
            abs(lows[idx] - closes[idx - 1]),
        )
        trs.append(tr)
    atr = np.mean(trs)
    if closes[-1] < 1e-10:
        return 0.0
    return float(atr / closes[-1])


def _compute_price_accel(close_prices: np.ndarray) -> float:
    if len(close_prices) < 10:
        return 0.0
    r5 = close_prices[-1] / close_prices[-6] - 1
    r10 = close_prices[-1] / close_prices[-11] - 1
    return float(r5 - r10 / 2)


def _compute_price_position(close_prices: np.ndarray, period: int = 60) -> float:
    if len(close_prices) < period:
        period = len(close_prices)
    window = close_prices[-period:]
    hi = float(np.max(window))
    lo = float(np.min(window))
    if hi - lo < 1e-10:
        return 0.5
    return float((close_prices[-1] - lo) / (hi - lo))


def extract_pre_signal_features(
    connection,
    candidates: list,
    lookback_days: int = 130,
) -> dict[int, dict[str, float]]:
    """Extract pre-signal features (pre_*, d0_*) and market features for IC scoring.

    Pre-signal features are computed from daily_bar data up to and including d0_date.
    Market features are computed from hs300_bar up to d0_date.

    Returns {candidate_index: {feature_name: value}}.
    """
    if not candidates:
        return {}

    symbols = list({c.symbol for c in candidates})
    if not symbols:
        return {}

    sec_rows = connection.execute(
        "SELECT symbol, security_id FROM security_master WHERE symbol = ANY(?)",
        [symbols],
    ).fetchall()
    sym_to_sid = {r[0]: r[1] for r in sec_rows}

    from datetime import timedelta

    all_d0_dates = [c.d0_date for c in candidates if c.d0_date]
    if not all_d0_dates:
        return {}
    min_d0 = min(all_d0_dates)
    fetch_start = min_d0 - timedelta(days=lookback_days * 2)

    sec_ids = list(set(sym_to_sid.values()))
    if not sec_ids:
        return {}

    bars = connection.execute(
        "SELECT security_id, trade_date, open_price, high_price, low_price, "
        "close_price, pre_close_price, volume, amount, change_pct, turnover_ratio, "
        "ma5_price, ma10_price, ma20_price, ma5_volume, ma10_volume, ma20_volume "
        "FROM daily_bar WHERE security_id = ANY(?) AND trade_date >= ? "
        "ORDER BY security_id, trade_date",
        [sec_ids, fetch_start],
    ).fetchdf()

    if bars.empty:
        return {}

    bars["trade_date"] = bars["trade_date"].astype(str)
    bars_by_sid: dict[int, Any] = {}
    for sid, group in bars.groupby("security_id"):
        bars_by_sid[sid] = group.sort_values("trade_date")

    # Use cached HS300 features (avoid re-computing per call)
    hs300 = _get_hs300_features(connection)

    result: dict[int, dict[str, float]] = {}

    for idx, cand in enumerate(candidates):
        sid = sym_to_sid.get(cand.symbol)
        if sid is None or sid not in bars_by_sid:
            continue

        d0_date_str = str(cand.d0_date) if cand.d0_date else None
        if d0_date_str is None:
            continue

        stock_bars = bars_by_sid[sid]
        d0_data = stock_bars[stock_bars["trade_date"] <= d0_date_str]
        if len(d0_data) < 30:
            continue

        d0_row = d0_data.iloc[-1]
        closes = d0_data["close_price"].values.astype(float)
        highs = d0_data["high_price"].values.astype(float)
        lows = d0_data["low_price"].values.astype(float)
        vols = d0_data["volume"].values.astype(float)
        changes = d0_data["change_pct"].values.astype(float)
        turnovers = d0_data["turnover_ratio"].values.astype(float)
        amounts = d0_data["amount"].values.astype(float)

        d0_close = closes[-1]
        d0_open = _safe_float(d0_row["open_price"])
        d0_high = _safe_float(d0_row["high_price"])
        d0_low = _safe_float(d0_row["low_price"])
        d0_prec = _safe_float(d0_row["pre_close_price"])
        d0_vol = vols[-1]
        d0_turn = turnovers[-1]

        feats: dict[str, float] = {}

        # --- Momentum ---
        def _safe_ratio(numerator, denominator):
            if abs(denominator) < 1e-10:
                return 0.0
            return float(numerator / denominator - 1) * 100

        feats["pre_mom_5d"] = _safe_ratio(d0_close, closes[-6]) if len(closes) > 5 else 0.0
        feats["pre_mom_10d"] = _safe_ratio(d0_close, closes[-11]) if len(closes) > 10 else 0.0
        feats["pre_mom_20d"] = _safe_ratio(d0_close, closes[-21]) if len(closes) > 20 else 0.0
        feats["pre_mom_40d"] = _safe_ratio(d0_close, closes[-41]) if len(closes) > 40 else 0.0
        feats["pre_mom_60d"] = _safe_ratio(d0_close, closes[-61]) if len(closes) > 60 else 0.0

        # --- Volume ---
        v5 = float(np.mean(vols[-5:])) if len(vols) >= 5 else 0.0
        v20 = float(np.mean(vols[-20:])) if len(vols) >= 20 else 1.0
        v2140 = float(np.mean(vols[-40:-20])) if len(vols) >= 40 else 1.0
        feats["pre_vol_trend"] = v5 / (v20 + 1) - 1
        feats["pre_vol_ratio_20_40"] = v20 / (v2140 + 1) - 1
        feats["pre_vol_20d"] = float(np.std(changes[-20:])) if len(changes) >= 20 else 0.0
        feats["pre_vol_60d"] = float(np.std(changes[-60:])) if len(changes) >= 60 else 0.0
        feats["pre_d0_vol_surge"] = d0_vol / (v20 + 1) - 1

        # --- Range ---
        h60 = float(np.max(highs[-60:])) if len(highs) >= 60 else float(np.max(highs))
        l60 = float(np.min(lows[-60:])) if len(lows) >= 60 else float(np.min(lows))
        h5 = float(np.max(highs[-5:])) if len(highs) >= 5 else float(np.max(highs))
        l5 = float(np.min(lows[-5:])) if len(lows) >= 5 else float(np.min(lows))
        feats["pre_range_pos"] = float(np.clip((d0_close - l60) / (h60 - l60 + 0.001), -1, 2))
        feats["pre_range_5d"] = (h5 - l5) / (d0_close + 0.001) * 100
        feats["pre_range_20d"] = (h60 - l60) / (d0_close + 0.001) * 100

        # --- Turnover ---
        t5 = float(np.mean(turnovers[-5:])) if len(turnovers) >= 5 else 0.0
        t20 = float(np.mean(turnovers[-20:])) if len(turnovers) >= 20 else 0.001
        feats["pre_turn_trend"] = t5 / (t20 + 0.001) - 1
        feats["pre_d0_turn_vs_avg"] = d0_turn / (t20 + 0.001) - 1

        # --- Return stats ---
        feats["pre_ret_avg_10d"] = float(np.mean(changes[-10:])) if len(changes) >= 10 else 0.0
        feats["pre_min_ret_20d"] = float(np.min(changes[-20:])) if len(changes) >= 20 else 0.0
        feats["pre_max_ret_20d"] = float(np.max(changes[-20:])) if len(changes) >= 20 else 0.0
        feats["pre_up_ratio_5d"] = float(np.sum(changes[-5:] > 0) / 5.0) if len(changes) >= 5 else 0.0

        # --- Acceleration ---
        feats["pre_mom_accel"] = feats["pre_mom_5d"] - feats["pre_mom_20d"] / 4

        # --- MA deviation ---
        d0_ma5 = _safe_float(d0_row["ma5_price"])
        d0_ma20 = _safe_float(d0_row["ma20_price"])
        if not np.isnan(d0_ma5) and d0_ma5 > 0:
            feats["pre_d0_vs_ma5"] = (d0_close / d0_ma5 - 1) * 100
        else:
            feats["pre_d0_vs_ma5"] = 0.0
        if not np.isnan(d0_ma20) and d0_ma20 > 0:
            feats["pre_d0_vs_ma20"] = (d0_close / d0_ma20 - 1) * 100
        else:
            feats["pre_d0_vs_ma20"] = 0.0

        # --- Volume concentration ---
        maxv20 = float(np.max(vols[-20:])) if len(vols) >= 20 else float(np.max(vols))
        feats["pre_vol_concentration"] = maxv20 / (v20 + 1) - 1

        # --- Liquidity ---
        amt5 = float(np.mean(amounts[-5:])) if len(amounts) >= 5 else 0.0
        amt20 = float(np.mean(amounts[-20:])) if len(amounts) >= 20 else 0.0
        feats["pre_liq_5d"] = float(np.log1p(amt5))
        feats["pre_liq_20d"] = float(np.log1p(amt20))

        # --- D0 candle ---
        feats["d0_gap"] = (d0_open / (d0_prec + 0.001) - 1) * 100 if d0_prec > 0 else 0.0
        feats["d0_body"] = (d0_close - d0_open) / (abs(d0_open) + 0.001) * 100
        feats["d0_amplitude"] = (d0_high - d0_low) / (d0_close + 0.001) * 100
        feats["d0_upper_shadow"] = (d0_high - max(d0_open, d0_close)) / (d0_close + 0.001) * 100
        feats["d0_lower_shadow"] = (min(d0_open, d0_close) - d0_low) / (d0_close + 0.001) * 100

        # --- Technical indicators ---
        feats["rsi_14"] = _compute_rsi(closes, 14)
        macd, macd_sig, macd_hist = _compute_macd(closes)
        feats["macd"] = macd
        feats["macd_signal"] = macd_sig
        feats["macd_hist"] = macd_hist
        feats["consec_up"] = float(np.sum(changes[-5:] > 0)) if len(changes) >= 5 else 0.0
        feats["consec_down"] = float(np.sum(changes[-5:] <= 0)) if len(changes) >= 5 else 0.0
        feats["bb_pos"] = _compute_bb_pos(closes, 20)
        feats["atr_ratio"] = _compute_atr_ratio(highs, lows, closes, 14)
        feats["vol_trend"] = v5 / (v20 + 1) - 1
        feats["price_accel"] = _compute_price_accel(closes)
        feats["price_position"] = _compute_price_position(closes, 60)

        # --- Market features (from HS300, aligned to d0_date) ---
        if not hs300.empty:
            hs_row = hs300[hs300["trade_date"] <= d0_date_str]
            if not hs_row.empty:
                hr = hs_row.iloc[-1]
                feats["hs_mom_20d"] = _safe_float(hr["hs_mom_20d"])
                feats["mkt_ret_5d"] = _safe_float(hr["mkt_ret_5d"])
                feats["mkt_ret_10d"] = _safe_float(hr["mkt_ret_10d"])
                feats["mkt_ret_20d"] = _safe_float(hr["mkt_ret_20d"])
                feats["mkt_above_ma5"] = _safe_float(hr["mkt_above_ma5"])
                feats["mkt_above_ma20"] = _safe_float(hr["mkt_above_ma20"])
                feats["mkt_vol_10d"] = _safe_float(hr["mkt_vol_10d"])
                feats["mkt_vol_20d"] = _safe_float(hr["mkt_vol_20d"])
                feats["mkt_rsi_14"] = _safe_float(hr["mkt_rsi_14"])
                feats["mkt_consec_up"] = _safe_float(hr["mkt_consec_up"])
                feats["mkt_consec_down"] = _safe_float(hr["mkt_consec_down"])
            else:
                for c in ["hs_mom_20d", "mkt_ret_5d", "mkt_ret_10d", "mkt_ret_20d",
                          "mkt_above_ma5", "mkt_above_ma20", "mkt_vol_10d", "mkt_vol_20d",
                          "mkt_rsi_14", "mkt_consec_up", "mkt_consec_down"]:
                    feats[c] = 0.0
        else:
            for c in ["hs_mom_20d", "mkt_ret_5d", "mkt_ret_10d", "mkt_ret_20d",
                      "mkt_above_ma5", "mkt_above_ma20", "mkt_vol_10d", "mkt_vol_20d",
                      "mkt_rsi_14", "mkt_consec_up", "mkt_consec_down"]:
                feats[c] = 0.0

        # --- Relative strength (stock vs HS300) ---
        feats["rel_str_5d"] = feats["pre_mom_5d"] - feats["hs_mom_20d"] * 25
        feats["rel_str_20d"] = feats["pre_mom_20d"] - feats["hs_mom_20d"] * 100

        # --- Interaction features ---
        feats["int_mom5_vol20"] = feats["pre_mom_5d"] * feats["pre_vol_20d"]
        feats["int_mom5_rangepos"] = feats["pre_mom_5d"] * feats["pre_range_pos"]
        feats["int_mom5_upratio"] = feats["pre_mom_5d"] * feats["pre_up_ratio_5d"]
        feats["int_momaccel_vol20"] = feats["pre_mom_accel"] * feats["pre_vol_20d"]
        feats["int_relstr5_vol20"] = feats["rel_str_5d"] * feats["pre_vol_20d"]
        feats["int_mom5_momaccel"] = feats["pre_mom_5d"] * feats["pre_mom_accel"]
        feats["int_d0ma5_vol20"] = feats["pre_d0_vs_ma5"] * feats["pre_vol_20d"]
        feats["int_d0gap_vol20"] = feats["d0_gap"] * feats["pre_vol_20d"]
        feats["int_d0body_vol20"] = feats["d0_body"] * feats["pre_vol_20d"]

        result[idx] = feats

    return result

def extract_all_ic_features(
    connection,
    candidates: list,
) -> tuple[dict[int, dict[str, float]], dict[int, dict[str, float]]]:
    """Combined extraction of KL + pre-signal features in a single daily_bar query.

    Returns (kl_features, pre_features) where each is {idx: {feat: val}}.
    """
    if not candidates:
        return {}, {}
    symbols = list({c.symbol for c in candidates})
    if not symbols:
        return {}, {}
    sec_rows = connection.execute(
        "SELECT symbol, security_id FROM security_master WHERE symbol = ANY(?)",
        [symbols],
    ).fetchall()
    sym_to_sid = {r[0]: r[1] for r in sec_rows}
    from datetime import timedelta
    all_d0 = [c.d0_date for c in candidates if c.d0_date]
    all_sel = [c.selection_date for c in candidates]
    min_date = min(all_d0 + all_sel)
    fetch_start = min_date - timedelta(days=260)
    sec_ids = list(set(sym_to_sid.values()))
    if not sec_ids:
        return {}, {}
    bars = connection.execute(
        "SELECT security_id, trade_date, open_price, high_price, low_price, "
        "close_price, pre_close_price, volume, amount, change_pct, turnover_ratio, "
        "ma5_price, ma10_price, ma20_price, ma5_volume, ma10_volume, ma20_volume "
        "FROM daily_bar WHERE security_id = ANY(?) AND trade_date >= ? "
        "ORDER BY security_id, trade_date",
        [sec_ids, fetch_start],
    ).fetchdf()
    if bars.empty:
        return {}, {}
    bars["trade_date"] = bars["trade_date"].astype(str)
    bars_by_sid = {}
    for sid, group in bars.groupby("security_id"):
        bars_by_sid[sid] = group.sort_values("trade_date")
    hs300 = _get_hs300_features(connection)
    kl_result = {}
    pre_result = {}
    for idx, cand in enumerate(candidates):
        sid = sym_to_sid.get(cand.symbol)
        if sid is None or sid not in bars_by_sid:
            continue
        stock_bars = bars_by_sid[sid]
        sel_date_str = str(cand.selection_date)
        d0_date_str = str(cand.d0_date) if cand.d0_date else None
        # KL features (up to selection_date)
        sel_data = stock_bars[stock_bars["trade_date"] <= sel_date_str]
        if len(sel_data) < 20:
            continue
        sel_row = sel_data.iloc[-1]
        closes = sel_data["close_price"].values.astype(float)
        vols = sel_data["volume"].values.astype(float)
        close_f = _safe_float(sel_row["close_price"])
        pre_close = _safe_float(sel_row["pre_close_price"])
        kl = {}
        if len(closes) > 5:
            kl["mom_5d"] = (closes[-1] / closes[-6] - 1) * 100
        if len(closes) > 10:
            kl["mom_10d"] = (closes[-1] / closes[-11] - 1) * 100
        if len(closes) > 20:
            kl["mom_20d"] = (closes[-1] / closes[-21] - 1) * 100
        if len(closes) > 30:
            kl["mom_30d"] = (closes[-1] / closes[-31] - 1) * 100
        ma5 = _safe_float(sel_row["ma5_price"])
        ma10 = _safe_float(sel_row["ma10_price"])
        ma20 = _safe_float(sel_row["ma20_price"])
        if not np.isnan(ma5) and ma5 > 0:
            kl["close_vs_ma5"] = (close_f / ma5 - 1) * 100
        if not np.isnan(ma10) and ma10 > 0:
            kl["close_vs_ma10"] = (close_f / ma10 - 1) * 100
        if not np.isnan(ma20) and ma20 > 0:
            kl["close_vs_ma20"] = (close_f / ma20 - 1) * 100
        if len(closes) >= 20:
            hi20 = float(np.max(closes[-20:]))
            lo20 = float(np.min(closes[-20:]))
            kl["range_pos_20d"] = (close_f - lo20) / (hi20 - lo20) if hi20 > lo20 else 0.5
        if len(vols) > 5 and np.mean(vols[-6:-1]) > 0:
            kl["vol_ratio_ma5"] = vols[-1] / np.mean(vols[-6:-1])
        if len(vols) > 10 and np.mean(vols[-11:-1]) > 0:
            kl["vol_ratio_ma10"] = vols[-1] / np.mean(vols[-11:-1])
        if len(vols) > 20 and np.mean(vols[-21:-1]) > 0:
            kl["vol_ratio_ma20"] = vols[-1] / np.mean(vols[-21:-1])
        kl["d1_turnover"] = _safe_float(sel_row["turnover_ratio"])
        if len(vols) >= 10:
            rv = np.mean(vols[-5:])
            pv = np.mean(vols[-10:-5])
            if pv > 0:
                kl["vol_trend_5d"] = rv / pv
        if len(closes) > 10:
            rets = np.diff(closes[-11:]) / closes[-11:-1] * 100
            kl["volatility_10d"] = float(np.std(rets))
        if len(closes) > 20:
            rets20 = np.diff(closes[-21:]) / closes[-21:-1] * 100
            kl["volatility_20d"] = float(np.std(rets20))
        high_f = _safe_float(sel_row["high_price"])
        low_f = _safe_float(sel_row["low_price"])
        if not np.isnan(pre_close) and pre_close > 0:
            kl["d1_amplitude"] = (high_f - low_f) / pre_close * 100
        kl["d1_change_pct"] = _safe_float(sel_row["change_pct"])
        d0_row = None
        if d0_date_str:
            d0_data = stock_bars[stock_bars["trade_date"] <= d0_date_str]
            if not d0_data.empty:
                d0_row = d0_data.iloc[-1]
                kl["d0_change_pct"] = _safe_float(d0_row["change_pct"])
        if d0_row is not None:
            d0_close = _safe_float(d0_row["close_price"])
            if not np.isnan(d0_close) and d0_close > 0:
                kl["d1_vs_d0_close"] = (close_f / d0_close - 1) * 100
        open_f = _safe_float(sel_row["open_price"])
        if not np.isnan(pre_close) and pre_close > 0:
            kl["d1_gap"] = (open_f / pre_close - 1) * 100
            body = abs(close_f - open_f)
            kl["d1_body_pct"] = body / pre_close * 100
            kl["d1_upper_shadow"] = (high_f - max(close_f, open_f)) / pre_close * 100
            kl["d1_lower_shadow"] = (min(close_f, open_f) - low_f) / pre_close * 100
        kl_result[idx] = kl
        # Pre-signal features (up to d0_date)
        if d0_date_str is None:
            continue
        d0_data = stock_bars[stock_bars["trade_date"] <= d0_date_str]
        if len(d0_data) < 30:
            continue
        d0_row = d0_data.iloc[-1]
        dc = d0_data["close_price"].values.astype(float)
        dh = d0_data["high_price"].values.astype(float)
        dl = d0_data["low_price"].values.astype(float)
        dv = d0_data["volume"].values.astype(float)
        dch = d0_data["change_pct"].values.astype(float)
        dtr = d0_data["turnover_ratio"].values.astype(float)
        dam = d0_data["amount"].values.astype(float)
        d0_close = dc[-1]
        d0_open = _safe_float(d0_row["open_price"])
        d0_high = _safe_float(d0_row["high_price"])
        d0_low = _safe_float(d0_row["low_price"])
        d0_prec = _safe_float(d0_row["pre_close_price"])
        d0_vol = dv[-1]
        d0_turn = dtr[-1]
        pf = {}
        def _sr(num, den):
            return float(num / den - 1) * 100 if abs(den) > 1e-10 else 0.0
        pf["pre_mom_5d"] = _sr(d0_close, dc[-6]) if len(dc) > 5 else 0.0
        pf["pre_mom_10d"] = _sr(d0_close, dc[-11]) if len(dc) > 10 else 0.0
        pf["pre_mom_20d"] = _sr(d0_close, dc[-21]) if len(dc) > 20 else 0.0
        pf["pre_mom_40d"] = _sr(d0_close, dc[-41]) if len(dc) > 40 else 0.0
        pf["pre_mom_60d"] = _sr(d0_close, dc[-61]) if len(dc) > 60 else 0.0
        v5 = float(np.mean(dv[-5:])) if len(dv) >= 5 else 0.0
        v20 = float(np.mean(dv[-20:])) if len(dv) >= 20 else 1.0
        v2140 = float(np.mean(dv[-40:-20])) if len(dv) >= 40 else 1.0
        pf["pre_vol_trend"] = v5 / (v20 + 1) - 1
        pf["pre_vol_ratio_20_40"] = v20 / (v2140 + 1) - 1
        pf["pre_vol_20d"] = float(np.std(dch[-20:])) if len(dch) >= 20 else 0.0
        pf["pre_vol_60d"] = float(np.std(dch[-60:])) if len(dch) >= 60 else 0.0
        pf["pre_d0_vol_surge"] = d0_vol / (v20 + 1) - 1
        h60 = float(np.max(dh[-60:])) if len(dh) >= 60 else float(np.max(dh))
        l60 = float(np.min(dl[-60:])) if len(dl) >= 60 else float(np.min(dl))
        h5 = float(np.max(dh[-5:])) if len(dh) >= 5 else float(np.max(dh))
        l5 = float(np.min(dl[-5:])) if len(dl) >= 5 else float(np.min(dl))
        pf["pre_range_pos"] = float(np.clip((d0_close - l60) / (h60 - l60 + 0.001), -1, 2))
        pf["pre_range_5d"] = (h5 - l5) / (d0_close + 0.001) * 100
        pf["pre_range_20d"] = (h60 - l60) / (d0_close + 0.001) * 100
        t5 = float(np.mean(dtr[-5:])) if len(dtr) >= 5 else 0.0
        t20 = float(np.mean(dtr[-20:])) if len(dtr) >= 20 else 0.001
        pf["pre_turn_trend"] = t5 / (t20 + 0.001) - 1
        pf["pre_d0_turn_vs_avg"] = d0_turn / (t20 + 0.001) - 1
        pf["pre_ret_avg_10d"] = float(np.mean(dch[-10:])) if len(dch) >= 10 else 0.0
        pf["pre_min_ret_20d"] = float(np.min(dch[-20:])) if len(dch) >= 20 else 0.0
        pf["pre_max_ret_20d"] = float(np.max(dch[-20:])) if len(dch) >= 20 else 0.0
        pf["pre_up_ratio_5d"] = float(np.sum(dch[-5:] > 0) / 5.0) if len(dch) >= 5 else 0.0
        pf["pre_mom_accel"] = pf["pre_mom_5d"] - pf["pre_mom_20d"] / 4
        d0_ma5 = _safe_float(d0_row["ma5_price"])
        d0_ma20 = _safe_float(d0_row["ma20_price"])
        pf["pre_d0_vs_ma5"] = (d0_close / d0_ma5 - 1) * 100 if not np.isnan(d0_ma5) and d0_ma5 > 0 else 0.0
        pf["pre_d0_vs_ma20"] = (d0_close / d0_ma20 - 1) * 100 if not np.isnan(d0_ma20) and d0_ma20 > 0 else 0.0
        maxv20 = float(np.max(dv[-20:])) if len(dv) >= 20 else float(np.max(dv))
        pf["pre_vol_concentration"] = maxv20 / (v20 + 1) - 1
        amt5 = float(np.mean(dam[-5:])) if len(dam) >= 5 else 0.0
        amt20 = float(np.mean(dam[-20:])) if len(dam) >= 20 else 0.0
        pf["pre_liq_5d"] = float(np.log1p(amt5))
        pf["pre_liq_20d"] = float(np.log1p(amt20))
        pf["d0_gap"] = (d0_open / (d0_prec + 0.001) - 1) * 100 if d0_prec > 0 else 0.0
        pf["d0_body"] = (d0_close - d0_open) / (abs(d0_open) + 0.001) * 100
        pf["d0_amplitude"] = (d0_high - d0_low) / (d0_close + 0.001) * 100
        pf["d0_upper_shadow"] = (d0_high - max(d0_open, d0_close)) / (d0_close + 0.001) * 100
        pf["d0_lower_shadow"] = (min(d0_open, d0_close) - d0_low) / (d0_close + 0.001) * 100
        pf["rsi_14"] = _compute_rsi(dc, 14)
        macd, macd_sig, macd_hist = _compute_macd(dc)
        pf["macd"] = macd
        pf["macd_signal"] = macd_sig
        pf["macd_hist"] = macd_hist
        pf["consec_up"] = float(np.sum(dch[-5:] > 0)) if len(dch) >= 5 else 0.0
        pf["consec_down"] = float(np.sum(dch[-5:] <= 0)) if len(dch) >= 5 else 0.0
        pf["bb_pos"] = _compute_bb_pos(dc, 20)
        pf["atr_ratio"] = _compute_atr_ratio(dh, dl, dc, 14)
        pf["vol_trend"] = v5 / (v20 + 1) - 1
        pf["price_accel"] = _compute_price_accel(dc)
        pf["price_position"] = _compute_price_position(dc, 60)
        if not hs300.empty:
            hr = hs300[hs300["trade_date"] <= d0_date_str]
            if not hr.empty:
                hr = hr.iloc[-1]
                for mc in ["hs_mom_20d", "mkt_ret_5d", "mkt_ret_10d", "mkt_ret_20d",
                           "mkt_above_ma5", "mkt_above_ma20", "mkt_vol_10d", "mkt_vol_20d",
                           "mkt_rsi_14", "mkt_consec_up", "mkt_consec_down"]:
                    pf[mc] = _safe_float(hr[mc])
            else:
                for mc in ["hs_mom_20d", "mkt_ret_5d", "mkt_ret_10d", "mkt_ret_20d",
                           "mkt_above_ma5", "mkt_above_ma20", "mkt_vol_10d", "mkt_vol_20d",
                           "mkt_rsi_14", "mkt_consec_up", "mkt_consec_down"]:
                    pf[mc] = 0.0
        else:
            for mc in ["hs_mom_20d", "mkt_ret_5d", "mkt_ret_10d", "mkt_ret_20d",
                       "mkt_above_ma5", "mkt_above_ma20", "mkt_vol_10d", "mkt_vol_20d",
                       "mkt_rsi_14", "mkt_consec_up", "mkt_consec_down"]:
                pf[mc] = 0.0
        pf["rel_str_5d"] = pf["pre_mom_5d"] - pf["hs_mom_20d"] * 25
        pf["rel_str_20d"] = pf["pre_mom_20d"] - pf["hs_mom_20d"] * 100
        pf["int_mom5_vol20"] = pf["pre_mom_5d"] * pf["pre_vol_20d"]
        pf["int_mom5_rangepos"] = pf["pre_mom_5d"] * pf["pre_range_pos"]
        pf["int_mom5_upratio"] = pf["pre_mom_5d"] * pf["pre_up_ratio_5d"]
        pf["int_momaccel_vol20"] = pf["pre_mom_accel"] * pf["pre_vol_20d"]
        pf["int_relstr5_vol20"] = pf["rel_str_5d"] * pf["pre_vol_20d"]
        pf["int_mom5_momaccel"] = pf["pre_mom_5d"] * pf["pre_mom_accel"]
        pf["int_d0ma5_vol20"] = pf["pre_d0_vs_ma5"] * pf["pre_vol_20d"]
        pf["int_d0gap_vol20"] = pf["d0_gap"] * pf["pre_vol_20d"]
        pf["int_d0body_vol20"] = pf["d0_body"] * pf["pre_vol_20d"]
        pre_result[idx] = pf
    return kl_result, pre_result
