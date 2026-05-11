from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from quant.config.loader import DEFAULT_STRATEGIES_DIR, LoadedStrategyConfig
from quant.evaluation.metrics import build_selection_backtest_metrics, build_strategy_backtest_metrics
from quant.evaluation.scoring import score_selection_backtest, score_strategy_backtest
from quant.evolution.mutation import build_mutation_trials
from quant.evolution.population import EvolutionIndividual, EvolutionResult
from quant.evolution.survivor_selection import select_top_individuals
from quant.experiments import ExperimentStore
from quant.experiments.models import SavedExperiment
from quant.experiments.store import _serialize_for_json
from quant.optimization.search_space import (
    ParameterOverride,
    SearchSpaceError,
    apply_parameter_overrides,
    build_override_snapshot,
    serialize_strategy_config_yaml,
)
from quant.pattern_selection import PatternSelectionError
from quant.strategy_runtime import (
    StrategyRuntimeError,
    run_loaded_strategy_backtest,
    run_loaded_strategy_selection_backtest,
)


EVOLUTION_TARGET_STRATEGY = "strategy_backtest"
EVOLUTION_TARGET_SELECTION = "selection_backtest"


class EvolutionError(ValueError):
    """Raised when strategy evolution cannot be executed."""


def evolve_loaded_strategy(
    *,
    db_path: Path,
    loaded: LoadedStrategyConfig,
    target: str,
    start_date: date,
    end_date: date,
    generation: int,
    max_mutations: int,
    top_n: int,
    seed: int | None,
    results_dir: Path,
    generated_dir: Path = DEFAULT_STRATEGIES_DIR / "generated",
    save_experiments: bool = True,
    progress_callback: Callable[[str], None] | None = None,
) -> EvolutionResult:
    if target not in {EVOLUTION_TARGET_STRATEGY, EVOLUTION_TARGET_SELECTION}:
        raise EvolutionError("target must be one of: strategy_backtest, selection_backtest.")
    if generation < 1:
        raise EvolutionError("generation must be at least 1.")
    if max_mutations < 1:
        raise EvolutionError("max_mutations must be at least 1.")
    if top_n < 1:
        raise EvolutionError("top_n must be at least 1.")
    _assert_target_defaults(loaded, target)
    trials = build_mutation_trials(loaded, max_mutations=max_mutations, seed=seed)
    results_dir.mkdir(parents=True, exist_ok=True)
    experiment_store = ExperimentStore(db_path) if save_experiments else None
    individuals: list[EvolutionIndividual] = []
    for trial in trials:
        if progress_callback is not None:
            progress_callback(f"[{trial.trial_index}/{len(trials)}] running evolution mutation")
        mutated_loaded = apply_parameter_overrides(loaded, trial.overrides)
        run = _run_evolution_backtest(
            db_path=db_path,
            loaded=mutated_loaded,
            target=target,
            start_date=start_date,
            end_date=end_date,
            experiment_store=experiment_store,
            run_name=f"{loaded.name}__evolve__{target}__gen_{generation}__trial_{trial.trial_index}",
            metadata={
                "entrypoint": "quant evolve",
                "evolution_base_strategy": loaded.name,
                "evolution_generation": generation,
                "evolution_trial_index": trial.trial_index,
                "evolution_target": target,
                "evolution_operation": "mutation",
                "parameter_overrides": build_override_snapshot(trial.overrides),
            },
        )
        individuals.append(
            EvolutionIndividual(
                trial_index=trial.trial_index,
                run_id=run.run_id,
                score=run.score,
                score_version=run.score_version,
                parameter_overrides=build_override_snapshot(trial.overrides),
            )
        )
    survivors = select_top_individuals(individuals, top_n=top_n)
    generated_paths: list[Path] = []
    updated_survivors: list[EvolutionIndividual] = []
    for rank, individual in enumerate(survivors, start=1):
        evolved_loaded = apply_parameter_overrides(
            loaded,
            [
                ParameterOverride(path=path, value=value)
                for path, value in individual.parameter_overrides.items()
            ],
        )
        path = write_evolved_strategy(
            base_loaded=loaded,
            evolved_loaded=evolved_loaded,
            target=target,
            generation=generation,
            rank=rank,
            generated_dir=generated_dir,
        )
        generated_paths.append(path)
        updated_survivors.append(
            replace(individual, generated_strategy_path=str(path))
        )
    _write_candidates_csv(results_dir / "candidates.csv", individuals)
    (results_dir / "survivors.json").write_text(
        json.dumps([_serialize_for_json(item) for item in updated_survivors], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary = {
        "strategy_name": loaded.name,
        "strategy_path": str(loaded.path.resolve()),
        "target": target,
        "generation": generation,
        "candidate_count": len(individuals),
        "best_run_id": updated_survivors[0].run_id,
        "best_score": updated_survivors[0].score,
        "score_version": updated_survivors[0].score_version,
        "generated_strategy_paths": [str(path) for path in generated_paths],
        "top_individuals": [_serialize_for_json(item) for item in updated_survivors],
    }
    (results_dir / "summary.json").write_text(
        json.dumps(_serialize_for_json(summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return EvolutionResult(
        strategy_name=loaded.name,
        target=target,
        generation=generation,
        candidate_count=len(individuals),
        best_score=updated_survivors[0].score,
        best_run_id=updated_survivors[0].run_id,
        score_version=updated_survivors[0].score_version,
        results_dir=results_dir,
        top_individuals=updated_survivors,
    )


def default_evolution_results_dir(
    strategy_name: str,
    target: str,
    generation: int,
    start_date: date,
    end_date: date,
) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return (
        Path("results")
        / "evolution"
        / f"{strategy_name}_{target}_gen_{generation}_{start_date.isoformat()}_{end_date.isoformat()}_{timestamp}"
    )


def build_evolved_strategy_name(base_strategy: str, target: str, generation: int, rank: int) -> str:
    return f"{base_strategy}__evolve_gen_{generation}__{target}__rank_{rank}"


def write_evolved_strategy(
    *,
    base_loaded: LoadedStrategyConfig,
    evolved_loaded: LoadedStrategyConfig,
    target: str,
    generation: int,
    rank: int,
    generated_dir: Path,
) -> Path:
    generated_dir.mkdir(parents=True, exist_ok=True)
    config = evolved_loaded.config.model_copy(deep=True)
    evolved_name = build_evolved_strategy_name(base_loaded.name, target, generation, rank)
    config.strategy.name = evolved_name
    display_name = base_loaded.config.strategy.display_name or base_loaded.name
    config.strategy.display_name = f"{display_name} Gen{generation} Top{rank}"
    base_description = base_loaded.config.strategy.description or ""
    suffix = "generated by quant evolve"
    config.strategy.description = (
        f"{base_description} | {suffix}" if base_description else suffix
    )
    output_path = generated_dir / f"{evolved_name}.yml"
    output_path.write_text(serialize_strategy_config_yaml(config), encoding="utf-8")
    return output_path


def _assert_target_defaults(loaded: LoadedStrategyConfig, target: str) -> None:
    if target == EVOLUTION_TARGET_STRATEGY and loaded.config.strategy_backtest is None:
        raise EvolutionError(f"Strategy '{loaded.name}' does not define strategy_backtest defaults.")
    if target == EVOLUTION_TARGET_SELECTION and loaded.config.backtest_selection is None:
        raise EvolutionError(f"Strategy '{loaded.name}' does not define backtest_selection defaults.")


def _run_evolution_backtest(
    *,
    db_path: Path,
    loaded: LoadedStrategyConfig,
    target: str,
    start_date: date,
    end_date: date,
    experiment_store: ExperimentStore | None,
    run_name: str,
    metadata: dict[str, Any],
) -> EvolutionIndividual:
    try:
        if target == EVOLUTION_TARGET_SELECTION:
            report = run_loaded_strategy_selection_backtest(
                db_path=db_path,
                loaded=loaded,
                start_date=start_date,
                end_date=end_date,
            )
            metrics = build_selection_backtest_metrics(report)
            score = score_selection_backtest(metrics)
            saved = (
                experiment_store.save_selection_backtest(
                    loaded,
                    report,
                    run_name=run_name,
                    metadata=metadata,
                )
                if experiment_store is not None
                else None
            )
        else:
            report = run_loaded_strategy_backtest(
                db_path=db_path,
                loaded=loaded,
                start_date=start_date,
                end_date=end_date,
            )
            metrics = build_strategy_backtest_metrics(report)
            score = score_strategy_backtest(metrics)
            saved = (
                experiment_store.save_strategy_backtest(
                    loaded,
                    report,
                    run_name=run_name,
                    metadata=metadata,
                )
                if experiment_store is not None
                else None
            )
    except (PatternSelectionError, StrategyRuntimeError, ValueError, SearchSpaceError) as exc:
        raise EvolutionError(str(exc)) from exc
    return EvolutionIndividual(
        trial_index=0,
        run_id=_saved_run_id(saved),
        score=score.score,
        score_version=score.score_version,
        parameter_overrides={},
    )


def _saved_run_id(saved: SavedExperiment | None) -> int | None:
    return saved.run_id if saved is not None else None


def _write_candidates_csv(path: Path, individuals: list[EvolutionIndividual]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "trial_index",
                "run_id",
                "score",
                "score_version",
                "parameter_overrides_json",
            ],
        )
        writer.writeheader()
        for individual in individuals:
            writer.writerow(
                {
                    "trial_index": individual.trial_index,
                    "run_id": individual.run_id,
                    "score": individual.score,
                    "score_version": individual.score_version,
                    "parameter_overrides_json": json.dumps(
                        individual.parameter_overrides,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                }
            )
