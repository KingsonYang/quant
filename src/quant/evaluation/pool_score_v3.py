"""Pool-level scoring system v3: V20-style IC-weighted rank composite."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

SCORE_VERSION = "v3"

NF_FEATURES = [
    "mom_5d", "mom_10d", "mom_20d", "mom_30d",
    "close_vs_ma5", "close_vs_ma10", "close_vs_ma20",
    "range_pos_20d", "vol_ratio_ma5", "vol_ratio_ma10", "vol_ratio_ma20",
    "d1_turnover", "vol_trend_5d", "volatility_10d", "volatility_20d",
    "d1_amplitude", "d1_change_pct", "d0_change_pct", "d1_vs_d0_close",
    "d1_gap", "d1_body_pct", "d1_upper_shadow", "d1_lower_shadow",
]

EF_FEATURES = [
    "f_signal_change", "f_signal_turnover", "f_signal_vol_ratio_prev5",
    "f_signal_vol_vs_prev1", "f_d0_vol_ratio_prev5",
    "f_signal_vs_d0_close_pct", "f_platform_min_low_vs_d0_low_pct",
    "f_platform_avg_vol_vs_d0", "f_platform_weak_day_ratio",
    "f_between_days", "composite_score",
]

NL_BASE = [
    "mom_30d", "d1_change_pct", "d1_lower_shadow", "d1_vs_d0_close",
    "d1_body_pct", "d1_upper_shadow", "composite_score",
    "d1_amplitude", "mom_5d", "close_vs_ma5", "close_vs_ma10",
]

IX_PAIRS = [
    ("mom_5d", "vol_ratio_ma5"),
    ("mom_30d", "close_vs_ma20"),
    ("d1_change_pct", "vol_ratio_ma5"),
    ("composite_score", "mom_5d"),
    ("composite_score", "vol_ratio_ma5"),
    ("close_vs_ma5", "close_vs_ma20"),
    ("mom_5d", "mom_30d"),
    ("d1_body_pct", "d1_turnover"),
    ("d1_amplitude", "d1_turnover"),
    ("composite_score", "d1_change_pct"),
]

_ic_weights = None
_feature_list = None
_v20_lookup = None


def _find_file(name, subdir="results"):
    candidates = [
        Path(__file__).resolve().parent.parent.parent.parent / subdir / name,
        Path.cwd() / subdir / name,
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _get_ic_weights():
    global _ic_weights, _feature_list
    if _ic_weights is not None:
        return _ic_weights, _feature_list
    p = _find_file("v20_ic_weights.json")
    if p is None:
        raise FileNotFoundError("v20_ic_weights.json not found.")
    with open(p) as fh:
        data = json.load(fh)
    _ic_weights = data["ic_weights"]
    _feature_list = data["feature_list"]
    return _ic_weights, _feature_list


def _get_v20_lookup():
    global _v20_lookup
    if _v20_lookup is not None:
        return _v20_lookup
    import csv
    p = _find_file("v20_all_scores.csv")
    lookup = {}
    if p is not None:
        with open(p, newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                key = (row["symbol"], row["selection_date"])
                lookup[key] = float(row["v20_ens_score"])
    _v20_lookup = lookup
    return lookup


def _safe_float(val):
    if val is None:
        return 0.0
    try:
        f = float(val)
        return f if np.isfinite(f) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _pct_rank(values):
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


def _gather_features(candidates, extra_features):
    extra_features = extra_features or {}
    result = []
    for i in range(len(candidates)):
        feats = {}
        ex = extra_features.get(i, {})
        for f in NF_FEATURES:
            feats[f] = _safe_float(ex.get(f))
        for f in EF_FEATURES:
            feats[f] = _safe_float(getattr(candidates[i], f, None))
        for f in NL_BASE:
            v = feats.get(f, 0.0)
            feats[f + "_sq"] = min(v * v, 100.0)
            feats[f + "_pos"] = max(v, 0.0)
            feats[f + "_neg"] = max(-v, 0.0)
        for f1, f2 in IX_PAIRS:
            v1 = feats.get(f1, 0.0)
            v2 = feats.get(f2, 0.0)
            prod = v1 * v2
            feats["ix_" + f1 + "_x_" + f2] = max(-100.0, min(100.0, prod))
        result.append(feats)
    return result


def compute_pool_score_v3(candidates, extra_features=None):
    n = len(candidates)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    ic_weights, feature_list = _get_ic_weights()
    all_feats = _gather_features(candidates, extra_features)
    totals = [0.0] * n
    total_weight = 0.0
    for feat in feature_list:
        weight = ic_weights.get(feat, 0.0)
        if abs(weight) <= 0.005:
            continue
        vals = [all_feats[i].get(feat, 0.0) for i in range(n)]
        if len(set(vals)) < 2:
            continue
        ranks = _pct_rank(vals)
        if weight < 0:
            ranks = [1.0 - r for r in ranks]
        abs_w = abs(weight)
        for i in range(n):
            totals[i] += ranks[i] * abs_w
        total_weight += abs_w
    if total_weight > 0:
        raw = [totals[i] / total_weight for i in range(n)]
    else:
        raw = [0.5] * n
    return _pct_rank(raw)


def lookup_v20_scores(candidates):
    lookup = _get_v20_lookup()
    if not lookup:
        return [None] * len(candidates)
    scores = []
    for cand in candidates:
        key = (cand.symbol, str(cand.selection_date))
        scores.append(lookup.get(key))
    return scores


def compute_pool_score_v3_hybrid(candidates, extra_features=None):
    n = len(candidates)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    v20_scores = lookup_v20_scores(candidates)
    if all(s is not None for s in v20_scores):
        return v20_scores
    return compute_pool_score_v3(candidates, extra_features)

