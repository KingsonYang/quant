from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from quant.discovery.features import CandidateFeatureSample, completed_samples


class DiscoveryBinningError(ValueError):
    """Raised when discovery bins cannot be built."""


@dataclass(frozen=True)
class BaselineStats:
    sample_count: int
    win_rate_pct: float
    average_return_pct: float
    median_return_pct: float
    worst_return_pct: float


@dataclass(frozen=True)
class FeatureBin:
    feature: str
    bin_index: int
    lower_bound: float
    upper_bound: float
    sample_count: int
    positive_count: int
    negative_count: int
    flat_count: int
    win_rate_pct: float
    average_return_pct: float
    median_return_pct: float
    worst_return_pct: float
    win_rate_lift_pct: float
    average_return_lift_pct: float
    score: float
    eligible: bool


@dataclass(frozen=True)
class FilterRuleCandidate:
    rank: int
    feature: str
    lower_bound: float
    upper_bound: float
    sample_count: int
    win_rate_pct: float
    average_return_pct: float
    median_return_pct: float
    worst_return_pct: float
    win_rate_lift_pct: float
    average_return_lift_pct: float
    score: float


def build_feature_bins(
    *,
    samples: list[CandidateFeatureSample],
    feature_fields: list[str],
    bins: int,
    min_samples: int,
) -> tuple[BaselineStats, list[FeatureBin]]:
    if bins < 1:
        raise DiscoveryBinningError("bins must be at least 1.")
    if min_samples < 1:
        raise DiscoveryBinningError("min_samples must be at least 1.")
    completed = completed_samples(samples)
    if not completed:
        raise DiscoveryBinningError("No completed candidate samples are available for discovery.")
    baseline = _baseline_stats(completed)
    feature_bins: list[FeatureBin] = []
    for feature in feature_fields:
        values = [
            (sample.features.get(feature), sample)
            for sample in completed
            if sample.features.get(feature) is not None
        ]
        if len(values) < min_samples:
            continue
        values.sort(key=lambda item: (item[0], item[1].selection_date, item[1].symbol))
        for index, bin_samples in enumerate(_split_equal_frequency(values, bins), start=1):
            if not bin_samples:
                continue
            feature_values = [float(value) for value, _ in bin_samples]
            stats_samples = [sample for _, sample in bin_samples]
            stats = _return_stats(stats_samples)
            win_rate_lift = round(stats["win_rate_pct"] - baseline.win_rate_pct, 4)
            average_return_lift = round(stats["average_return_pct"] - baseline.average_return_pct, 4)
            eligible = (
                len(stats_samples) >= min_samples
                and win_rate_lift > 0
                and average_return_lift > 0
            )
            score = _score_bin(
                win_rate_lift_pct=win_rate_lift,
                average_return_lift_pct=average_return_lift,
                sample_count=len(stats_samples),
                min_samples=min_samples,
            )
            feature_bins.append(
                FeatureBin(
                    feature=feature,
                    bin_index=index,
                    lower_bound=round(min(feature_values), 6),
                    upper_bound=round(max(feature_values), 6),
                    sample_count=len(stats_samples),
                    positive_count=stats["positive_count"],
                    negative_count=stats["negative_count"],
                    flat_count=stats["flat_count"],
                    win_rate_pct=stats["win_rate_pct"],
                    average_return_pct=stats["average_return_pct"],
                    median_return_pct=stats["median_return_pct"],
                    worst_return_pct=stats["worst_return_pct"],
                    win_rate_lift_pct=win_rate_lift,
                    average_return_lift_pct=average_return_lift,
                    score=score,
                    eligible=eligible,
                )
            )
    return baseline, feature_bins


def select_top_filter_rules(
    feature_bins: list[FeatureBin],
    *,
    top_rules: int,
) -> list[FilterRuleCandidate]:
    if top_rules < 1:
        raise DiscoveryBinningError("top_rules must be at least 1.")
    ranked = sorted(
        [item for item in feature_bins if item.eligible],
        key=lambda item: (-item.score, -item.sample_count, item.feature, item.bin_index),
    )
    return [
        FilterRuleCandidate(
            rank=rank,
            feature=item.feature,
            lower_bound=item.lower_bound,
            upper_bound=item.upper_bound,
            sample_count=item.sample_count,
            win_rate_pct=item.win_rate_pct,
            average_return_pct=item.average_return_pct,
            median_return_pct=item.median_return_pct,
            worst_return_pct=item.worst_return_pct,
            win_rate_lift_pct=item.win_rate_lift_pct,
            average_return_lift_pct=item.average_return_lift_pct,
            score=item.score,
        )
        for rank, item in enumerate(ranked[:top_rules], start=1)
    ]


def _baseline_stats(samples: list[CandidateFeatureSample]) -> BaselineStats:
    stats = _return_stats(samples)
    return BaselineStats(
        sample_count=len(samples),
        win_rate_pct=stats["win_rate_pct"],
        average_return_pct=stats["average_return_pct"],
        median_return_pct=stats["median_return_pct"],
        worst_return_pct=stats["worst_return_pct"],
    )


def _return_stats(samples: list[CandidateFeatureSample]) -> dict[str, float | int]:
    returns = [float(sample.return_pct) for sample in samples if sample.return_pct is not None]
    positive_count = sum(1 for value in returns if value > 0)
    negative_count = sum(1 for value in returns if value < 0)
    flat_count = sum(1 for value in returns if value == 0)
    return {
        "positive_count": positive_count,
        "negative_count": negative_count,
        "flat_count": flat_count,
        "win_rate_pct": round(positive_count * 100 / len(returns), 4),
        "average_return_pct": round(sum(returns) / len(returns), 4),
        "median_return_pct": round(float(median(returns)), 4),
        "worst_return_pct": round(min(returns), 4),
    }


def _split_equal_frequency(
    values: list[tuple[float | None, CandidateFeatureSample]],
    bins: int,
) -> list[list[tuple[float | None, CandidateFeatureSample]]]:
    count = len(values)
    actual_bins = min(bins, count)
    chunks: list[list[tuple[float | None, CandidateFeatureSample]]] = []
    previous_end = 0
    for index in range(actual_bins):
        end = round((index + 1) * count / actual_bins)
        if end <= previous_end:
            end = previous_end + 1
        chunks.append(values[previous_end:end])
        previous_end = end
    return chunks


def _score_bin(
    *,
    win_rate_lift_pct: float,
    average_return_lift_pct: float,
    sample_count: int,
    min_samples: int,
) -> float:
    sample_weight = min(sample_count / min_samples, 3.0)
    return round(win_rate_lift_pct * 0.6 + average_return_lift_pct * 2.0 + sample_weight, 4)
