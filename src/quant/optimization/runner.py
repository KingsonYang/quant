from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from quant.config.loader import DEFAULT_STRATEGIES_DIR, LoadedStrategyConfig
from quant.experiments import ExperimentStore
from quant.experiments.store import _serialize_for_json
from quant.optimization.grid_search import build_grid_trials
from quant.optimization.random_search import build_random_trials
from quant.optimization.search_space import (
    ParameterOverride,
    apply_parameter_overrides,
    build_override_snapshot,
    validate_search_space,
    write_generated_strategy,
)
from quant.strategy_runtime import (
    run_loaded_strategy_backtest,
    run_loaded_strategy_selection_backtest,
)


OPTIMIZATION_TARGET_STRATEGY = "strategy_backtest"
OPTIMIZATION_TARGET_SELECTION = "selection_backtest"
OPTIMIZATION_METHOD_GRID = "grid"
OPTIMIZATION_METHOD_RANDOM = "random"


class OptimizationError(ValueError):
    """Raised when strategy optimization cannot be executed."""


@dataclass(frozen=True)
class OptimizationTrialSummary:
    trial_index: int
    run_id: int
    method: str
    target: str
    score: float
    score_version: str
    parameter_overrides: dict[str, object]
    generated_strategy_path: str | None = None


@dataclass(frozen=True)
class OptimizationResult:
    strategy_name: str
    target: str
    method: str
    trial_count: int
    best_run_id: int
    best_score: float
    score_version: str
    results_dir: Path
    top_trials: list[OptimizationTrialSummary]


def optimize_loaded_strategy(
    *,
    db_path: Path,
    loaded: LoadedStrategyConfig,
    method: str,
    target: str,
    start_date: date,
    end_date: date,
    top_n: int,
    results_dir: Path,
    max_trials: int | None = None,
    seed: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
    generated_dir: Path = DEFAULT_STRATEGIES_DIR / "generated",
) -> OptimizationResult:
    search_space = validate_search_space(loaded)
    trials = _build_trials(
        method=method,
        search_space=search_space,
        max_trials=max_trials,
        seed=seed,
    )
    if not trials:
        raise OptimizationError("Optimization search space produced zero trials.")
    results_dir.mkdir(parents=True, exist_ok=True)
    experiment_store = ExperimentStore(db_path)
    trial_summaries: list[OptimizationTrialSummary] = []
    for index, overrides in enumerate(trials, start=1):
        if progress_callback is not None:
            progress_callback(f"[{index}/{len(trials)}] running optimization trial")
        optimized_loaded = apply_parameter_overrides(loaded, overrides)
        override_snapshot = build_override_snapshot(overrides)
        run_name = f"{loaded.name}__optimize__{target}__trial_{index}"
        if target == OPTIMIZATION_TARGET_STRATEGY:
            if optimized_loaded.config.strategy_backtest is None:
                raise OptimizationError(
                    f"Strategy '{loaded.name}' does not define strategy_backtest defaults."
                )
            report = run_loaded_strategy_backtest(
                db_path=db_path,
                loaded=optimized_loaded,
                start_date=start_date,
                end_date=end_date,
            )
            saved = experiment_store.save_strategy_backtest(
                optimized_loaded,
                report,
                run_name=run_name,
                metadata={
                    "optimization_method": method,
                    "optimization_target": target,
                    "optimization_trial_index": index,
                    "optimization_base_strategy": loaded.name,
                    "parameter_overrides": override_snapshot,
                },
            )
        elif target == OPTIMIZATION_TARGET_SELECTION:
            if optimized_loaded.config.backtest_selection is None:
                raise OptimizationError(
                    f"Strategy '{loaded.name}' does not define backtest_selection defaults."
                )
            report = run_loaded_strategy_selection_backtest(
                db_path=db_path,
                loaded=optimized_loaded,
                start_date=start_date,
                end_date=end_date,
            )
            saved = experiment_store.save_selection_backtest(
                optimized_loaded,
                report,
                run_name=run_name,
                metadata={
                    "optimization_method": method,
                    "optimization_target": target,
                    "optimization_trial_index": index,
                    "optimization_base_strategy": loaded.name,
                    "parameter_overrides": override_snapshot,
                },
            )
        else:
            raise OptimizationError(f"Unsupported optimization target '{target}'.")
        trial_summaries.append(
            OptimizationTrialSummary(
                trial_index=index,
                run_id=saved.run_id,
                method=method,
                target=target,
                score=saved.score,
                score_version=saved.score_version,
                parameter_overrides=override_snapshot,
            )
        )
    ranked_trials = sorted(
        trial_summaries,
        key=lambda trial: (-trial.score, trial.trial_index),
    )
    top_trials = ranked_trials[:top_n]
    generated_paths: list[Path] = []
    for rank, trial in enumerate(top_trials, start=1):
        optimized_loaded = apply_parameter_overrides(
            loaded,
            [
                ParameterOverride(path=path, value=value)
                for path, value in trial.parameter_overrides.items()
            ],
        )
        generated_path = write_generated_strategy(
            base_loaded=loaded,
            optimized_loaded=optimized_loaded,
            target=target,
            rank=rank,
            generated_dir=generated_dir,
        )
        generated_paths.append(generated_path)
        top_trials[rank - 1] = OptimizationTrialSummary(
            trial_index=trial.trial_index,
            run_id=trial.run_id,
            method=trial.method,
            target=trial.target,
            score=trial.score,
            score_version=trial.score_version,
            parameter_overrides=trial.parameter_overrides,
            generated_strategy_path=str(generated_path),
        )
    _write_trials_csv(results_dir / "trials.csv", trial_summaries)
    (results_dir / "top_trials.json").write_text(
        json.dumps([_serialize_for_json(item) for item in top_trials], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary_payload = {
        "strategy_name": loaded.name,
        "strategy_path": str(loaded.path.resolve()),
        "target": target,
        "method": method,
        "trial_count": len(trial_summaries),
        "best_run_id": top_trials[0].run_id,
        "best_score": top_trials[0].score,
        "score_version": top_trials[0].score_version,
        "generated_strategy_paths": [str(path) for path in generated_paths],
        "top_trials": [_serialize_for_json(item) for item in top_trials],
    }
    (results_dir / "summary.json").write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return OptimizationResult(
        strategy_name=loaded.name,
        target=target,
        method=method,
        trial_count=len(trial_summaries),
        best_run_id=top_trials[0].run_id,
        best_score=top_trials[0].score,
        score_version=top_trials[0].score_version,
        results_dir=results_dir,
        top_trials=top_trials,
    )


def default_optimization_results_dir(
    strategy_name: str,
    target: str,
    start_date: date,
    end_date: date,
) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return (
        Path("results")
        / "optimization"
        / f"{strategy_name}_{target}_{start_date.isoformat()}_{end_date.isoformat()}_{timestamp}"
    )


def _build_trials(
    *,
    method: str,
    search_space: dict[str, list[str | int | float | object]],
    max_trials: int | None,
    seed: int | None,
) -> list[list[ParameterOverride]]:
    if method == OPTIMIZATION_METHOD_GRID:
        return build_grid_trials(search_space)
    if method == OPTIMIZATION_METHOD_RANDOM:
        if max_trials is None or max_trials < 1:
            raise OptimizationError("--max-trials must be provided and be at least 1 for random optimization.")
        return build_random_trials(search_space, max_trials=max_trials, seed=seed)
    raise OptimizationError(f"Unsupported optimization method '{method}'.")


def _write_trials_csv(path: Path, trials: list[OptimizationTrialSummary]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "trial_index",
                "run_id",
                "method",
                "target",
                "score",
                "score_version",
                "parameter_overrides_json",
            ],
        )
        writer.writeheader()
        for trial in trials:
            writer.writerow(
                {
                    "trial_index": trial.trial_index,
                    "run_id": trial.run_id,
                    "method": trial.method,
                    "target": trial.target,
                    "score": trial.score,
                    "score_version": trial.score_version,
                    "parameter_overrides_json": json.dumps(
                        trial.parameter_overrides,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                }
            )
