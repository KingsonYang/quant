from __future__ import annotations

import random

from quant.optimization.search_space import ParameterOverride


def build_random_trials(
    search_space: dict[str, list[str | int | float | object]],
    *,
    max_trials: int,
    seed: int | None,
) -> list[list[ParameterOverride]]:
    rng = random.Random(seed)
    ordered_items = sorted(search_space.items(), key=lambda item: item[0])
    trials: list[list[ParameterOverride]] = []
    for _ in range(max_trials):
        trials.append(
            [
                ParameterOverride(path=path, value=rng.choice(candidates))
                for path, candidates in ordered_items
            ]
        )
    return trials
