from __future__ import annotations

from dataclasses import dataclass
import random
from typing import Any

from quant.config.loader import LoadedStrategyConfig
from quant.optimization.search_space import (
    ParameterOverride,
    SearchSpaceError,
    validate_search_space,
)
from quant.validation.robustness import _resolve_path_value


@dataclass(frozen=True)
class MutationTrial:
    trial_index: int
    parameter_path: str
    base_value: Any
    mutation_value: Any
    overrides: list[ParameterOverride]


def build_mutation_trials(
    loaded: LoadedStrategyConfig,
    *,
    max_mutations: int,
    seed: int | None,
) -> list[MutationTrial]:
    if max_mutations < 1:
        raise SearchSpaceError("max_mutations must be at least 1.")
    search_space = validate_search_space(loaded)
    mutation_candidates: list[tuple[str, Any, Any]] = []
    for path, candidates in search_space.items():
        base_value = _resolve_path_value(loaded, path)
        for candidate in candidates:
            if candidate != base_value:
                mutation_candidates.append((path, base_value, candidate))
    if not mutation_candidates:
        raise SearchSpaceError(
            f"Strategy '{loaded.name}' has no mutation candidates different from current parameters."
        )
    rng = random.Random(seed)
    rng.shuffle(mutation_candidates)
    selected = mutation_candidates[:max_mutations]
    return [
        MutationTrial(
            trial_index=index,
            parameter_path=path,
            base_value=base_value,
            mutation_value=mutation_value,
            overrides=[ParameterOverride(path=path, value=mutation_value)],
        )
        for index, (path, base_value, mutation_value) in enumerate(selected, start=1)
    ]
