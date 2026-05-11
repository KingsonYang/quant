from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from pydantic import ValidationError

from quant.config.loader import DEFAULT_STRATEGIES_DIR, LoadedStrategyConfig
from quant.config.models import PatternSelectionConfig, RulesSelectionConfig, StrategyConfig, StrategyRuleConfig
from quant.discovery.binning import (
    BaselineStats,
    FeatureBin,
    FilterRuleCandidate,
    build_feature_bins,
    select_top_filter_rules,
)
from quant.discovery.features import (
    DISCOVERY_FEATURE_FIELDS,
    CandidateFeatureSample,
    build_candidate_feature_samples,
    completed_samples,
)
from quant.experiments.store import _serialize_for_json
from quant.optimization.search_space import serialize_strategy_config_yaml


DISCOVERY_TARGET_SELECTION = "selection_backtest"


class DiscoveryError(ValueError):
    """Raised when filter discovery cannot be executed."""


@dataclass(frozen=True)
class DiscoveryResult:
    strategy_name: str
    target: str
    results_dir: Path
    baseline: BaselineStats
    sample_count: int
    completed_sample_count: int
    top_rules: list[FilterRuleCandidate]
    generated_strategy_paths: list[Path]


def discover_filters(
    *,
    db_path: Path,
    loaded: LoadedStrategyConfig,
    target: str,
    start_date: date,
    end_date: date,
    top_rules: int,
    bins: int,
    min_samples: int,
    results_dir: Path,
    generated_dir: Path = DEFAULT_STRATEGIES_DIR / "generated",
) -> DiscoveryResult:
    normalized_target = target.strip().lower()
    if normalized_target != DISCOVERY_TARGET_SELECTION:
        raise DiscoveryError("discover filters currently supports --target selection_backtest only.")
    if top_rules < 1:
        raise DiscoveryError("top_rules must be at least 1.")
    if bins < 1:
        raise DiscoveryError("bins must be at least 1.")
    if min_samples < 1:
        raise DiscoveryError("min_samples must be at least 1.")
    if not isinstance(loaded.config.selection, (RulesSelectionConfig, PatternSelectionConfig)):
        raise DiscoveryError("discover filters supports rules and pattern_sql selection strategies only.")

    results_dir.mkdir(parents=True, exist_ok=True)
    samples = build_candidate_feature_samples(
        db_path=db_path,
        loaded=loaded,
        start_date=start_date,
        end_date=end_date,
    )
    completed = completed_samples(samples)
    if len(completed) < min_samples:
        _write_candidate_features_csv(results_dir / "candidate_features.csv", samples)
        raise DiscoveryError(
            f"Not enough completed candidate samples for discovery: {len(completed)} < {min_samples}."
        )

    baseline, feature_bins = build_feature_bins(
        samples=samples,
        feature_fields=DISCOVERY_FEATURE_FIELDS,
        bins=bins,
        min_samples=min_samples,
    )
    top = select_top_filter_rules(feature_bins, top_rules=top_rules)
    _write_candidate_features_csv(results_dir / "candidate_features.csv", samples)
    _write_feature_bins_csv(results_dir / "feature_bins.csv", feature_bins)
    (results_dir / "candidate_rules.json").write_text(
        json.dumps([_serialize_for_json(asdict(item)) for item in top], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    generated_paths: list[Path] = []
    if not top:
        _write_summary_json(
            results_dir / "summary.json",
            loaded=loaded,
            target=normalized_target,
            start_date=start_date,
            end_date=end_date,
            baseline=baseline,
            sample_count=len(samples),
            completed_sample_count=len(completed),
            feature_bins=feature_bins,
            top_rules=top,
            generated_paths=generated_paths,
        )
        raise DiscoveryError("No eligible filter rule improved both win rate and average return.")

    for rule in top:
        generated_paths.append(
            write_discovered_strategy(
                base_loaded=loaded,
                rule=rule,
                generated_dir=generated_dir,
            )
        )
    _write_summary_json(
        results_dir / "summary.json",
        loaded=loaded,
        target=normalized_target,
        start_date=start_date,
        end_date=end_date,
        baseline=baseline,
        sample_count=len(samples),
        completed_sample_count=len(completed),
        feature_bins=feature_bins,
        top_rules=top,
        generated_paths=generated_paths,
    )
    return DiscoveryResult(
        strategy_name=loaded.name,
        target=normalized_target,
        results_dir=results_dir,
        baseline=baseline,
        sample_count=len(samples),
        completed_sample_count=len(completed),
        top_rules=top,
        generated_strategy_paths=generated_paths,
    )


def default_discovery_results_dir(
    strategy_name: str,
    target: str,
    start_date: date,
    end_date: date,
) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return (
        Path("results")
        / "discovery"
        / f"{strategy_name}_{target}_{start_date.isoformat()}_{end_date.isoformat()}_{timestamp}"
    )


def build_discovered_strategy_name(base_strategy: str, rank: int) -> str:
    return f"{base_strategy}__discover_filter_rank_{rank}"


def write_discovered_strategy(
    *,
    base_loaded: LoadedStrategyConfig,
    rule: FilterRuleCandidate,
    generated_dir: Path,
) -> Path:
    generated_dir.mkdir(parents=True, exist_ok=True)
    config = base_loaded.config.model_copy(deep=True)
    generated_name = build_discovered_strategy_name(base_loaded.name, rule.rank)
    config.strategy.name = generated_name
    display_name = base_loaded.config.strategy.display_name or base_loaded.name
    config.strategy.display_name = f"{display_name} Discover Filter {rule.rank}"
    base_description = base_loaded.config.strategy.description or ""
    suffix = "generated by quant discover filters"
    config.strategy.description = (
        f"{base_description} | {suffix}" if base_description else suffix
    )
    if isinstance(config.selection, RulesSelectionConfig):
        config.selection.rules.extend(_rules_from_candidate(rule))
    elif isinstance(config.selection, PatternSelectionConfig):
        config.selection.filter_rules.extend(_rules_from_candidate(rule))
    else:
        raise DiscoveryError("Discovered filter strategies can only be generated for rules or pattern_sql strategies.")
    try:
        validated = StrategyConfig.model_validate(config.model_dump(mode="python"))
    except ValidationError as exc:
        raise DiscoveryError(f"Generated discovered strategy is invalid: {exc}") from exc
    output_path = generated_dir / f"{generated_name}.yml"
    output_path.write_text(serialize_strategy_config_yaml(validated), encoding="utf-8")
    return output_path


def _rules_from_candidate(rule: FilterRuleCandidate) -> list[StrategyRuleConfig]:
    if rule.lower_bound == rule.upper_bound:
        return [
            StrategyRuleConfig(
                field=rule.feature,
                operator="eq",
                value=rule.lower_bound,
            )
        ]
    return [
        StrategyRuleConfig(
            field=rule.feature,
            operator="gte",
            value=rule.lower_bound,
        ),
        StrategyRuleConfig(
            field=rule.feature,
            operator="lte",
            value=rule.upper_bound,
        ),
    ]


def _write_candidate_features_csv(path: Path, samples: list[CandidateFeatureSample]) -> None:
    fieldnames = [
        "symbol",
        "name",
        "selection_date",
        "d0_date",
        "buy_date",
        "sell_date",
        "return_pct",
        "is_positive",
        "status",
        "incomplete_reason",
        *DISCOVERY_FEATURE_FIELDS,
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for sample in samples:
            row = {
                "symbol": sample.symbol,
                "name": sample.name,
                "selection_date": sample.selection_date.isoformat(),
                "d0_date": sample.d0_date.isoformat() if sample.d0_date is not None else "",
                "buy_date": sample.buy_date.isoformat() if sample.buy_date is not None else "",
                "sell_date": sample.sell_date.isoformat() if sample.sell_date is not None else "",
                "return_pct": "" if sample.return_pct is None else sample.return_pct,
                "is_positive": "" if sample.is_positive is None else sample.is_positive,
                "status": sample.status,
                "incomplete_reason": sample.incomplete_reason or "",
            }
            row.update({field: sample.features.get(field) for field in DISCOVERY_FEATURE_FIELDS})
            writer.writerow(row)


def _write_feature_bins_csv(path: Path, feature_bins: list[FeatureBin]) -> None:
    fieldnames = [
        "feature",
        "bin_index",
        "lower_bound",
        "upper_bound",
        "sample_count",
        "positive_count",
        "negative_count",
        "flat_count",
        "win_rate_pct",
        "average_return_pct",
        "median_return_pct",
        "worst_return_pct",
        "win_rate_lift_pct",
        "average_return_lift_pct",
        "score",
        "eligible",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in feature_bins:
            writer.writerow(_serialize_for_json(asdict(item)))


def _write_summary_json(
    path: Path,
    *,
    loaded: LoadedStrategyConfig,
    target: str,
    start_date: date,
    end_date: date,
    baseline: BaselineStats,
    sample_count: int,
    completed_sample_count: int,
    feature_bins: list[FeatureBin],
    top_rules: list[FilterRuleCandidate],
    generated_paths: list[Path],
) -> None:
    payload = {
        "strategy_name": loaded.name,
        "strategy_path": str(loaded.path.resolve()),
        "target": target,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "sample_count": sample_count,
        "completed_sample_count": completed_sample_count,
        "baseline": _serialize_for_json(asdict(baseline)),
        "feature_bin_count": len(feature_bins),
        "eligible_rule_count": sum(1 for item in feature_bins if item.eligible),
        "top_rules": [_serialize_for_json(asdict(item)) for item in top_rules],
        "generated_strategy_paths": [str(path) for path in generated_paths],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
