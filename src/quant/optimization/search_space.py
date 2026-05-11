from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import re
from typing import Any

from pydantic import ValidationError
import yaml

from quant.config.loader import LoadedStrategyConfig
from quant.config.models import StrategyConfig
from quant.experiments.store import _serialize_for_json
from quant.pattern_selection import SUPPORTED_PATTERN_SORT_FIELDS
from quant.selection import FIELD_DEFINITIONS, SORT_DEFINITIONS, SUPPORTED_SORT_ORDERS


ALLOWED_PARAMETER_PATHS = {
    "backtest_selection.holding_period",
    "backtest_selection.selection_top_n",
    "selection.sort_by",
    "selection.sort_order",
    "strategy_backtest.holding_period",
    "strategy_backtest.max_positions",
    "strategy_backtest.max_daily_buys",
    "strategy_backtest.position_size_pct",
    "strategy_backtest.take_profit_pct",
    "strategy_backtest.stop_loss_pct",
    "strategy_backtest.selection_top_n",
    "selection.params.k0_volume_multiple",
    "selection.params.k1_volume_multiple",
    "selection.params.platform_max_trade_days",
    "selection.params.d0_change_min_pct",
    "selection.params.d1_change_min_pct",
    "selection.params.d1_prev2_max_change_pct",
    "selection.params.limit_up_min_pct",
    "selection.params.limit_up_close_high_min_ratio",
    "selection.params.limit_up_volume_multiple",
    "selection.params.pullback_min_trade_days",
    "selection.params.pullback_max_trade_days",
    "selection.params.min_pullback_weak_days",
    "selection.params.weak_day_max_change_pct",
    "selection.params.support_low_tolerance_pct",
    "selection.params.close_support_tolerance_pct",
    "selection.params.must_touch_low_zone_pct",
    "selection.params.pullback_avg_volume_max_ratio",
    "selection.params.pullback_max_daily_change_pct",
    "selection.params.pullback_min_daily_change_pct",
    "selection.params.recent_ma5_touch_tolerance_pct",
    "selection.params.signal_min_change_pct",
    "selection.params.signal_reclaim_low_tolerance_pct",
    "selection.params.signal_max_distance_from_t0_low_pct",
    "selection.params.signal_volume_ratio_min",
    "selection.params.signal_volume_ratio_max",
    "selection.params.deep_limit_up_volume_multiple",
    "selection.params.deep_support_low_tolerance_pct",
    "selection.params.deep_close_support_tolerance_pct",
    "selection.params.deep_pullback_avg_volume_max_ratio",
    "selection.params.deep_pullback_max_daily_change_pct",
    "selection.params.deep_pullback_min_daily_change_pct",
    "selection.params.deep_signal_reclaim_low_tolerance_pct",
}

RULE_VALUE_PATH_PATTERN = re.compile(r"^selection\.rules\.(\d+)\.value$")


class SearchSpaceError(ValueError):
    """Raised when optimization search space config is invalid."""


@dataclass(frozen=True)
class ParameterOverride:
    path: str
    value: str | int | float | Decimal


def validate_search_space(loaded: LoadedStrategyConfig) -> dict[str, list[str | int | float | Decimal]]:
    optimization = loaded.config.optimization
    if optimization is None or not optimization.enabled:
        raise SearchSpaceError(
            f"Strategy '{loaded.name}' does not define an enabled optimization block."
        )
    validated: dict[str, list[str | int | float | Decimal]] = {}
    for path, candidates in optimization.search_space.items():
        validated[path] = _validate_parameter_path(loaded.config, path, list(candidates))
    return dict(sorted(validated.items()))


def _validate_parameter_path(
    config: StrategyConfig,
    path: str,
    candidates: list[str | int | float | Decimal],
) -> list[str | int | float | Decimal]:
    if path in ALLOWED_PARAMETER_PATHS:
        _assert_path_exists(config, path)
        return _validate_exact_path_candidates(config, path, candidates)
    if RULE_VALUE_PATH_PATTERN.match(path):
        return _validate_rule_value_path_candidates(config, path, candidates)
    raise SearchSpaceError(
        f"Unsupported optimization parameter path '{path}'."
    )


def _assert_path_exists(config: StrategyConfig, path: str) -> None:
    current: Any = config
    for segment in path.split("."):
        if current is None or not hasattr(current, segment):
            raise SearchSpaceError(
                f"Optimization parameter path '{path}' is not available for strategy '{config.strategy.name}'."
            )
        current = getattr(current, segment)


def _validate_exact_path_candidates(
    config: StrategyConfig,
    path: str,
    candidates: list[str | int | float | Decimal],
) -> list[str | int | float | Decimal]:
    if path == "selection.sort_by":
        supported = _supported_sort_fields(config)
        for candidate in candidates:
            if not isinstance(candidate, str) or candidate not in supported:
                supported_text = ", ".join(sorted(supported))
                raise SearchSpaceError(
                    f"Unsupported optimization sort field '{candidate}' for path '{path}'. "
                    f"Supported fields: {supported_text}."
                )
    if path == "selection.sort_order":
        normalized: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, str) or candidate.upper() not in SUPPORTED_SORT_ORDERS:
                raise SearchSpaceError(
                    f"Unsupported optimization sort order '{candidate}' for path '{path}'. "
                    "Supported sort orders: ASC, DESC."
                )
            normalized.append(candidate.upper())
        return normalized
    return candidates


def _supported_sort_fields(config: StrategyConfig) -> set[str]:
    selection_type = getattr(config.selection, "type", "rules")
    if selection_type == "pattern_sql":
        return set(SUPPORTED_PATTERN_SORT_FIELDS)
    return set(SORT_DEFINITIONS)


def _validate_rule_value_path_candidates(
    config: StrategyConfig,
    path: str,
    candidates: list[str | int | float | Decimal],
) -> list[str | int | float | Decimal]:
    selection_type = getattr(config.selection, "type", "rules")
    if selection_type != "rules" or not hasattr(config.selection, "rules"):
        raise SearchSpaceError(
            f"Optimization parameter path '{path}' is only available for rules selection strategies."
        )
    match = RULE_VALUE_PATH_PATTERN.match(path)
    if match is None:
        raise SearchSpaceError(f"Unsupported optimization parameter path '{path}'.")
    rule_index = int(match.group(1))
    rules = config.selection.rules
    if rule_index >= len(rules):
        raise SearchSpaceError(
            f"Optimization rule index {rule_index} is out of range for strategy "
            f"'{config.strategy.name}' with {len(rules)} rule(s)."
        )
    rule = rules[rule_index]
    field_definition = FIELD_DEFINITIONS.get(rule.field)
    if field_definition is None:
        raise SearchSpaceError(
            f"Cannot optimize rule value for unsupported field '{rule.field}'."
        )
    field_type = field_definition["type"]
    for candidate in candidates:
        if field_type == "number" and not _is_numeric_candidate(candidate):
            raise SearchSpaceError(
                f"Optimization parameter path '{path}' requires numeric candidates "
                f"because rule field '{rule.field}' is numeric."
            )
        if field_type == "string" and not isinstance(candidate, str):
            raise SearchSpaceError(
                f"Optimization parameter path '{path}' requires string candidates "
                f"because rule field '{rule.field}' is string."
            )
    return candidates


def _is_numeric_candidate(value: object) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def apply_parameter_overrides(
    loaded: LoadedStrategyConfig,
    overrides: list[ParameterOverride],
) -> LoadedStrategyConfig:
    cloned = loaded.config.model_copy(deep=True)
    for override in overrides:
        _apply_path_value(cloned, override.path, override.value)
    try:
        validated_config = StrategyConfig.model_validate(cloned.model_dump(mode="python"))
    except ValidationError as exc:
        raise SearchSpaceError(
            f"Applied optimization overrides produced an invalid strategy config for "
            f"'{loaded.name}': {exc}"
        ) from exc
    return LoadedStrategyConfig(
        name=loaded.name,
        path=loaded.path,
        config=validated_config,
    )


def _apply_path_value(config: StrategyConfig, path: str, value: Any) -> None:
    match = RULE_VALUE_PATH_PATTERN.match(path)
    if match is not None:
        config.selection.rules[int(match.group(1))].value = value
        return
    segments = path.split(".")
    current: Any = config
    for segment in segments[:-1]:
        current = getattr(current, segment)
    field_name = segments[-1]
    current_value = getattr(current, field_name)
    coerced_value = value
    if isinstance(current_value, Decimal) and not isinstance(value, Decimal):
        coerced_value = Decimal(str(value))
    setattr(current, field_name, coerced_value)


def build_override_snapshot(overrides: list[ParameterOverride]) -> dict[str, Any]:
    return {
        override.path: _serialize_for_json(override.value)
        for override in overrides
    }


def serialize_strategy_config_yaml(config: StrategyConfig) -> str:
    payload = _normalize_for_yaml(config.model_dump(mode="python"))
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def _normalize_for_yaml(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _normalize_for_yaml(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_for_yaml(item) for item in value]
    return value


def build_generated_strategy_name(base_strategy: str, target: str, rank: int) -> str:
    return f"{base_strategy}__{target}__rank_{rank}"


def write_generated_strategy(
    *,
    base_loaded: LoadedStrategyConfig,
    optimized_loaded: LoadedStrategyConfig,
    target: str,
    rank: int,
    generated_dir: Path,
) -> Path:
    generated_dir.mkdir(parents=True, exist_ok=True)
    config = optimized_loaded.config.model_copy(deep=True)
    generated_name = build_generated_strategy_name(base_loaded.name, target, rank)
    config.strategy.name = generated_name
    display_name = base_loaded.config.strategy.display_name or base_loaded.name
    config.strategy.display_name = f"{display_name} Top{rank}"
    base_description = base_loaded.config.strategy.description or ""
    suffix = "generated by quant optimize"
    config.strategy.description = (
        f"{base_description} | {suffix}" if base_description else suffix
    )
    output_path = generated_dir / f"{generated_name}.yml"
    output_path.write_text(serialize_strategy_config_yaml(config), encoding="utf-8")
    return output_path
