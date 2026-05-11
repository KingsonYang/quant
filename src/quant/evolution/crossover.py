from __future__ import annotations

from dataclasses import dataclass
import random
from typing import Any

from quant.config.loader import LoadedStrategyConfig
from quant.optimization.search_space import (
    ParameterOverride,
    SearchSpaceError,
    apply_parameter_overrides,
    validate_search_space,
)
from quant.validation.robustness import _resolve_path_value


@dataclass(frozen=True)
class CrossoverResult:
    child: LoadedStrategyConfig
    inherited_overrides: list[ParameterOverride]


def build_crossover_child(
    base: LoadedStrategyConfig,
    donor: LoadedStrategyConfig,
    *,
    seed: int | None,
) -> CrossoverResult:
    if base.config.selection.type != donor.config.selection.type:
        raise SearchSpaceError("Crossover requires strategies with the same selection type.")
    base_space = validate_search_space(base)
    donor_space = validate_search_space(donor)
    common_paths = sorted(set(base_space) & set(donor_space))
    if not common_paths:
        raise SearchSpaceError("Crossover requires at least one common optimization parameter path.")
    rng = random.Random(seed)
    overrides: list[ParameterOverride] = []
    for path in common_paths:
        base_value = _resolve_path_value(base, path)
        donor_value = _resolve_path_value(donor, path)
        chosen_value: Any = rng.choice([base_value, donor_value])
        if chosen_value != base_value:
            overrides.append(ParameterOverride(path=path, value=donor_value))
    if not overrides:
        raise SearchSpaceError("Crossover produced no parameter values different from the base strategy.")
    return CrossoverResult(
        child=apply_parameter_overrides(base, overrides),
        inherited_overrides=overrides,
    )
