from __future__ import annotations

from quant.evolution.population import EvolutionIndividual


def select_top_individuals(
    individuals: list[EvolutionIndividual],
    *,
    top_n: int,
) -> list[EvolutionIndividual]:
    if top_n < 1:
        raise ValueError("top_n must be at least 1.")
    return sorted(
        individuals,
        key=lambda item: (-item.score, item.trial_index),
    )[:top_n]
