from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvolutionIndividual:
    trial_index: int
    run_id: int | None
    score: float
    score_version: str
    parameter_overrides: dict[str, Any]
    generated_strategy_path: str | None = None


@dataclass(frozen=True)
class EvolutionResult:
    strategy_name: str
    target: str
    generation: int
    candidate_count: int
    best_score: float
    best_run_id: int | None
    score_version: str
    results_dir: Path
    top_individuals: list[EvolutionIndividual]
