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


@dataclass(frozen=True)
class PerturbationTrial:
    trial_index: int
    parameter_path: str
    base_value: Any
    perturbation_value: Any
    overrides: list[ParameterOverride]


def build_perturbation_trials(
    loaded: LoadedStrategyConfig,
    *,
    max_perturbations: int,
    seed: int | None,
) -> list[PerturbationTrial]:
    if max_perturbations < 1:
        raise SearchSpaceError("max_perturbations must be at least 1.")
    search_space = validate_search_space(loaded)
    candidates: list[tuple[str, Any, Any]] = []
    for path, values in search_space.items():
        base_value = _resolve_path_value(loaded, path)
        for value in values:
            if value != base_value:
                candidates.append((path, base_value, value))
    rng = random.Random(seed)
    rng.shuffle(candidates)
    selected = candidates[:max_perturbations]
    return [
        PerturbationTrial(
            trial_index=index,
            parameter_path=path,
            base_value=base_value,
            perturbation_value=value,
            overrides=[ParameterOverride(path=path, value=value)],
        )
        for index, (path, base_value, value) in enumerate(selected, start=1)
    ]


def _resolve_path_value(loaded: LoadedStrategyConfig, path: str) -> Any:
    current: Any = loaded.config
    segments = path.split(".")
    if len(segments) == 4 and segments[0] == "selection" and segments[1] == "rules" and segments[3] == "value":
        return loaded.config.selection.rules[int(segments[2])].value
    for segment in segments:
        current = getattr(current, segment)
    return current
