"""Pool-level IC-weighted scoring (V87: two-stage return+safety blend).

V87: Two-stage scoring -- 70% return IC score + 30% safety (downside IC) score.
V14 backward compatibility: single-stage IC-weighted rank composite.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

SCORE_VERSION = "ic_v87"

_model_cache: dict[str, Any] | None = None


def _find_model_file() -> Path | None:
    model_dir = Path(__file__).resolve().parent / "models"
    candidates = [
        model_dir / "ic_model_v87.json",
        model_dir / "ic_model_v14.json",
        Path(__file__).resolve().parent.parent.parent.parent / "results" / "ic_model_v87.json",
        Path.cwd() / "results" / "ic_model_v87.json",
        Path(__file__).resolve().parent.parent.parent.parent / "results" / "ic_model_v14.json",
        Path.cwd() / "results" / "ic_model_v14.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _load_model() -> dict[str, Any]:
    global _model_cache
    if _model_cache is not None:
        return _model_cache
    p = _find_model_file()
    if p is None:
        raise FileNotFoundError("ic_model_v87.json or ic_model_v14.json not found in results/")
    with open(p) as fh:
        _model_cache = json.load(fh)
    return _model_cache


def _safe_float(val: Any) -> float:
    if val is None:
        return 0.0
    try:
        f = float(val)
        return f if np.isfinite(f) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _pct_rank(values: list[float]) -> list[float]:
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank / n
        i = j + 1
    return ranks


KL_FROM_CANDIDATE = [
    "f_signal_change", "f_signal_turnover", "f_signal_vol_ratio_prev5",
    "f_signal_vol_vs_prev1", "f_d0_vol_ratio_prev5",
    "f_signal_vs_d0_close_pct", "f_platform_min_low_vs_d0_low_pct",
    "f_platform_avg_vol_vs_d0", "f_platform_weak_day_ratio",
    "f_between_days", "composite_score",
]


def _gather_all_features(
    candidates: list,
    extra_features: dict[int, dict[str, float]],
    pre_features: dict[int, dict[str, float]],
) -> list[dict[str, float]]:
    """Merge candidate attributes, daily_bar KL features, and pre-signal features."""
    result = []
    for i in range(len(candidates)):
        feats: dict[str, float] = {}
        for f in KL_FROM_CANDIDATE:
            feats[f] = _safe_float(getattr(candidates[i], f, None))
        ex = extra_features.get(i, {})
        for f in [
            "mom_5d", "mom_10d", "mom_20d", "mom_30d",
            "close_vs_ma5", "close_vs_ma10", "close_vs_ma20",
            "range_pos_20d", "vol_ratio_ma5", "vol_ratio_ma10", "vol_ratio_ma20",
            "d1_turnover", "vol_trend_5d", "volatility_10d", "volatility_20d",
            "d1_amplitude", "d1_change_pct", "d0_change_pct", "d1_vs_d0_close",
            "d1_gap", "d1_body_pct", "d1_upper_shadow", "d1_lower_shadow",
        ]:
            feats[f] = _safe_float(ex.get(f))
        pf = pre_features.get(i, {})
        for f, v in pf.items():
            feats[f] = _safe_float(v)
        result.append(feats)
    return result


def _add_engineered_features(all_feats: list[dict[str, float]]) -> None:
    """Add engineered features computable from base features at runtime."""
    n = len(all_feats)
    if n == 0:
        return
    for i in range(n):
        f = all_feats[i]
        vr5 = f.get("vol_ratio_ma5", 0.0)
        vr20 = f.get("vol_ratio_ma20", 1.0)
        f["vol_accel"] = vr5 / vr20 if abs(vr20) > 1e-10 else 1.0
        f["mom_accel_5_20"] = f.get("mom_5d", 0.0) - f.get("mom_20d", 0.0)
        f["atr_proxy"] = f.get("pre_range_20d", 0.0) * f.get("volatility_20d", 0.0)
        f["price_trend"] = (
            f.get("close_vs_ma5", 0.0) + f.get("close_vs_ma10", 0.0) + f.get("close_vs_ma20", 0.0)
        ) / 3.0
        vol10 = f.get("volatility_10d", 1.0)
        f["mom_quality"] = f.get("mom_5d", 0.0) / vol10 if abs(vol10) > 1e-10 else 0.0
        f["body_x_amp"] = f.get("d0_body", 0.0) * f.get("d0_amplitude", 0.0)
        f["mom_x_vol"] = f.get("mom_5d", 0.0) * f.get("vol_ratio_ma5", 0.0)
        f["int_mktret20_mom20"] = f.get("mkt_ret_20d", 0.0) * f.get("mom_20d", 0.0)
        f["int_mktret5_mom5"] = f.get("mkt_ret_5d", 0.0) * f.get("mom_5d", 0.0)


def _add_pool_features(all_feats: list[dict[str, float]]) -> None:
    """Add pool-level features (same-day cross-sectional statistics)."""
    n = len(all_feats)
    if n == 0:
        return
    n_cand = float(n)
    avg_mom5 = float(np.mean([all_feats[i].get("pre_mom_5d", 0.0) for i in range(n)]))
    avg_mom20 = float(np.mean([all_feats[i].get("pre_mom_20d", 0.0) for i in range(n)]))
    avg_vol20 = float(np.mean([all_feats[i].get("pre_vol_20d", 0.0) for i in range(n)]))
    for i in range(n):
        all_feats[i]["pool_n_cand"] = n_cand
        all_feats[i]["pool_avg_mom5"] = avg_mom5
        all_feats[i]["pool_avg_mom20"] = avg_mom20
        all_feats[i]["pool_avg_vol20"] = avg_vol20
        all_feats[i]["pool_size_log"] = float(np.log1p(n_cand))


def _add_nl_transforms(all_feats: list[dict[str, float]], nl_bases: list[str]) -> None:
    """Add nonlinear transforms (_pos, _neg, _sq) for specified bases."""
    n = len(all_feats)
    if n == 0 or not nl_bases:
        return
    for base in nl_bases:
        if n > 0 and base not in all_feats[0]:
            continue
        vals = [all_feats[i].get(base, 0.0) for i in range(n)]
        for i in range(n):
            v = vals[i]
            all_feats[i][base + "_pos"] = max(v, 0.0)
            all_feats[i][base + "_neg"] = max(-v, 0.0)
            all_feats[i][base + "_sq"] = min(v * v, 100.0)


def _add_daymeta_features(all_feats: list[dict[str, float]], daymeta_bases: list[str]) -> None:
    """Add day-level cross-sectional statistics (mean, std, spread, z-score)."""
    n = len(all_feats)
    if n < 2 or not daymeta_bases:
        return
    for base in daymeta_bases:
        if base not in all_feats[0]:
            continue
        vals = np.array([all_feats[i].get(base, 0.0) for i in range(n)], dtype=np.float64)
        dm = float(np.mean(vals))
        ds = float(np.std(vals))
        dsp = float(np.max(vals) - np.min(vals))
        ds_safe = ds if ds > 1e-10 else 1.0
        for i in range(n):
            all_feats[i]["day_mean_" + base] = dm
            all_feats[i]["day_std_" + base] = ds
            all_feats[i]["day_spread_" + base] = dsp
            all_feats[i]["rel_z_" + base] = float((vals[i] - dm) / ds_safe)


def _add_derived_features(
    all_feats: list[dict[str, float]],
    nl_bases: list[str],
    int_pairs: list[list[str]],
) -> None:
    """Add nonlinear transforms and interaction terms in-place (V14 compat)."""
    _add_nl_transforms(all_feats, nl_bases)
    n = len(all_feats)
    if n == 0:
        return
    for pair in int_pairs:
        f1, f2 = pair[0], pair[1]
        if f1 not in all_feats[0] or f2 not in all_feats[0]:
            continue
        key = f"int_{f1}_{f2}"
        for i in range(n):
            v1 = all_feats[i].get(f1, 0.0)
            v2 = all_feats[i].get(f2, 0.0)
            all_feats[i][key] = max(-100.0, min(100.0, v1 * v2))


def _compute_weighted_score(
    all_feats: list[dict[str, float]],
    feature_list: list[str],
    ic_weights: dict[str, float],
    power: float,
) -> list[float]:
    """Compute IC-weighted rank composite score for a feature set."""
    n = len(all_feats)
    if n == 0:
        return []
    totals = [0.0] * n
    total_weight = 0.0
    for feat in feature_list:
        weight = ic_weights.get(feat, 0.0)
        if abs(weight) < 0.005:
            continue
        vals = [all_feats[i].get(feat, 0.0) for i in range(n)]
        if len(set(vals)) < 2:
            continue
        ranks = _pct_rank(vals)
        if weight < 0:
            ranks = [1.0 - r for r in ranks]
        abs_w = abs(weight) ** power
        for i in range(n):
            totals[i] += ranks[i] * abs_w
        total_weight += abs_w
    if total_weight > 0:
        return [totals[i] / total_weight for i in range(n)]
    return [0.5] * n


def compute_pool_score_ic(
    candidates: list,
    extra_features: dict[int, dict[str, float]] | None = None,
    pre_features: dict[int, dict[str, float]] | None = None,
) -> list[float]:
    """Score candidates using IC-weighted rank composite.

    V87: Two-stage scoring (return IC + safety/downside IC, blended with alpha).
    V14: Single-stage IC-weighted rank composite (backward compatible).

    Returns:
        List of scores. Higher is better.
    """
    n = len(candidates)
    if n == 0:
        return []
    if n == 1:
        return [0.5]

    extra_features = extra_features or {}
    pre_features = pre_features or {}

    model = _load_model()
    is_v87 = "return_features" in model and "safety_features" in model

    # Gather all base features
    all_feats = _gather_all_features(candidates, extra_features, pre_features)

    # Add engineered features
    _add_engineered_features(all_feats)

    # Add pool features
    _add_pool_features(all_feats)

    if is_v87:
        # V87: Two-stage scoring
        alpha = model.get("alpha", 0.70)
        power = model.get("power", 1.5)
        safety_power = model.get("safety_power", 1.5)
        nl_bases = model.get("nl_bases", [])
        daymeta_bases = model.get("daymeta_bases", [])

        # Add NL transforms and daymeta
        _add_nl_transforms(all_feats, nl_bases)
        _add_daymeta_features(all_feats, daymeta_bases)

        # Stage 1: Return score
        ret_fl = model["return_feature_list"]
        ret_w = model["return_features"]
        ret_raw = _compute_weighted_score(all_feats, ret_fl, ret_w, power)
        ret_ranked = _pct_rank(ret_raw)

        # Stage 2: Safety score (downside IC, inverted)
        saf_fl = model["safety_feature_list"]
        saf_w = model["safety_features"]
        saf_raw = _compute_weighted_score(all_feats, saf_fl, saf_w, safety_power)
        saf_ranked = _pct_rank(saf_raw)
        saf_inverted = [1.0 - r for r in saf_ranked]

        # Blend
        combined = [alpha * ret_ranked[i] + (1.0 - alpha) * saf_inverted[i] for i in range(n)]
        return combined
    else:
        # V14: Single-stage scoring (backward compatible)
        ic_weights = model["ic_weights"]
        feature_list = model["feature_list"]
        nl_bases = model.get("nl_bases", [])
        int_pairs = model.get("int_pairs", [])

        _add_derived_features(all_feats, nl_bases, int_pairs)

        raw = _compute_weighted_score(all_feats, feature_list, ic_weights, 1.0)
        return _pct_rank(raw)
