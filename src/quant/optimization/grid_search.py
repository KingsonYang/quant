from __future__ import annotations

from itertools import product

from quant.optimization.search_space import ParameterOverride


def build_grid_trials(
    search_space: dict[str, list[str | int | float | object]],
) -> list[list[ParameterOverride]]:
    ordered_items = sorted(search_space.items(), key=lambda item: item[0])
    if not ordered_items:
        return []
    trials: list[list[ParameterOverride]] = []
    for values in product(*(candidates for _, candidates in ordered_items)):
        trials.append(
            [
                ParameterOverride(path=path, value=value)
                for (path, _), value in zip(ordered_items, values, strict=True)
            ]
        )
    return trials
