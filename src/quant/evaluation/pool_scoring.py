"""Pool-level scoring system v2: IC-weighted rank composite.

Extends the anti_hype_score concept with 24 features (existing f_ features
 + daily_bar features), weighted by their train-period rank IC.

Key design:
- For each feature, compute within-pool percentile rank (0-1)
- For negative-IC features, invert the rank (1 - rank)
- Weight by |train IC|, sum to get composite score
- Train period: 2022-01-04 to 2024-12-31
- IC computed via within-pool Spearman rank correlation
"""
from __future__ import annotations

import numpy as np


SCORE_VERSION = "v2"

# Feature weights from train-period (2022-2024) within-pool rank IC.
# Positive weight = higher feature value is better.
# Negative weight = lower feature value is better (rank inverted).
FEATURE_WEIGHTS: dict[str, float] = {
    # Positive IC features (higher = better)
    "d1_upper_shadow": 0.0659,
    "range_pos_20d": 0.0445,
    "vol_ratio_ma20": 0.0346,
    "f_platform_weak_day_ratio": 0.0327,
    "d1_gap": 0.0306,
    "d0_change_pct": 0.0293,
    "vol_ratio_ma10": 0.0253,
    "vol_ratio_ma5": 0.0251,
    "f_signal_vol_ratio_prev5": 0.0251,
    "mom_10d": 0.0234,
    "close_vs_ma20": 0.0230,
    "f_signal_vol_vs_prev1": 0.0227,
    "mom_20d": 0.0211,
    "volatility_20d": 0.0167,
    # Negative IC features (lower = better, rank inverted)
    "mom_30d": -0.0158,
    "d1_change_pct": -0.0203,
    "f_signal_change": -0.0203,
    "f_platform_min_low_vs_d0_low_pct": -0.0235,
    "f_platform_avg_vol_vs_d0": -0.0253,
    "f_d0_vol_ratio_prev5": -0.0283,
    "d1_vs_d0_close": -0.0323,
    "f_between_days": -0.0330,
    "f_signal_vs_d0_close_pct": -0.0532,
    "composite_score": -0.0539,
}


def _pct_rank(values: list[float]) -> list[float]:
    """Percentile rank (average method), aligned with pandas rank(pct=True)."""
    n = len(values)
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


def compute_pool_score_v2(
    candidates: list,
    extra_features: dict[int, dict[str, float]] | None = None,
) -> list[float]:
    """Compute v2 composite score for a pool of candidates.

    Args:
        candidates: List of PatternSelectionCandidate objects with f_ features.
        extra_features: Optional dict {index: {feature: value}} from daily_bar.

    Returns:
        List of scores (0-1 range, higher = better) parallel to candidates.
    """
    n = len(candidates)
    if n == 0:
        return []
    if n == 1:
        return [0.5]

    extra_features = extra_features or {}
    totals = [0.0] * n
    total_weight = 0.0
    feats_used = 0

    for feat, weight in FEATURE_WEIGHTS.items():
        # Gather values from candidate attributes or extra features
        vals: list[float | None] = []
        for i in range(n):
            if i in extra_features and feat in extra_features[i]:
                vals.append(extra_features[i][feat])
            else:
                attr_val = getattr(candidates[i], feat, None)
                vals.append(attr_val)

        # Check how many are present
        present_idx = [i for i in range(n) if vals[i] is not None]
        present_vals = [vals[i] for i in present_idx if np.isfinite(float(vals[i]))]
        present_idx = [i for i in present_idx if vals[i] is not None and np.isfinite(float(vals[i]))]

        if len(present_idx) < 3 or len(set(present_vals)) < 2:
            continue

        pr_present = _pct_rank([float(vals[i]) for i in present_idx])
        pr = [0.5] * n
        for pos, i in enumerate(present_idx):
            pr[i] = pr_present[pos]

        # Invert for negative-weight features
        if weight < 0:
            pr = [1.0 - p for p in pr]

        abs_w = abs(weight)
        for i in range(n):
            totals[i] += pr[i] * abs_w
        total_weight += abs_w
        feats_used += 1

    if total_weight > 0:
        return [totals[i] / total_weight for i in range(n)]
    return [0.5] * n
