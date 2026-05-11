from __future__ import annotations

from datetime import date
from pathlib import Path

import typer

from quant.config import DEFAULT_STRATEGIES_DIR, StrategyConfigError, load_strategy_config
from quant.db.init_db import DEFAULT_DB_PATH
from quant.evolution import (
    EVOLUTION_TARGET_SELECTION,
    EVOLUTION_TARGET_STRATEGY,
    EvolutionError,
    evolve_loaded_strategy,
)
from quant.evolution.runner import default_evolution_results_dir
from quant.optimization.search_space import SearchSpaceError


EVOLUTION_TARGETS = {
    EVOLUTION_TARGET_STRATEGY,
    EVOLUTION_TARGET_SELECTION,
}


def _parse_trade_date(label: str, value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{label} must be in YYYY-MM-DD format.") from exc


def _load_strategy(strategy_name: str, strategies_dir: Path):
    try:
        return load_strategy_config(strategy_name, strategies_dir)
    except StrategyConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc


def evolve_strategy_command(
    strategy_name: str = typer.Argument(..., help="Strategy name, matching the YAML file stem."),
    start_date: str = typer.Option(
        ...,
        "--start-date",
        help="Evolution backtest start trading date in YYYY-MM-DD format.",
    ),
    end_date: str = typer.Option(
        ...,
        "--end-date",
        help="Evolution backtest end trading date in YYYY-MM-DD format.",
    ),
    target: str = typer.Option(
        EVOLUTION_TARGET_STRATEGY,
        "--target",
        help="Evolution target: strategy_backtest or selection_backtest.",
    ),
    generation: int = typer.Option(
        1,
        "--generation",
        min=1,
        help="Generation number for naming outputs.",
    ),
    max_mutations: int = typer.Option(
        20,
        "--max-mutations",
        min=1,
        help="Maximum number of single-parameter mutation candidates.",
    ),
    top_n: int = typer.Option(
        5,
        "--top-n",
        min=1,
        help="Number of surviving strategies to materialize.",
    ),
    seed: int | None = typer.Option(
        None,
        "--seed",
        help="Optional random seed for mutation sampling.",
    ),
    save_experiments: bool = typer.Option(
        True,
        "--save-experiments/--no-save-experiments",
        help="Save evolution candidate backtests to the experiment store.",
    ),
    db_path: Path = typer.Option(
        DEFAULT_DB_PATH,
        "--db-path",
        help="Target DuckDB file path.",
    ),
    strategies_dir: Path = typer.Option(
        DEFAULT_STRATEGIES_DIR,
        "--strategies-dir",
        help="Directory containing strategy YAML files.",
    ),
    generated_dir: Path = typer.Option(
        DEFAULT_STRATEGIES_DIR / "generated",
        "--generated-dir",
        help="Directory for evolved strategy YAML files.",
    ),
    results_dir: Path | None = typer.Option(
        None,
        "--results-dir",
        help="Optional directory for evolution outputs.",
    ),
) -> None:
    """Evolve one YAML strategy by safely mutating known optimization parameters."""
    loaded = _load_strategy(strategy_name, strategies_dir)
    normalized_target = target.strip().lower()
    if normalized_target not in EVOLUTION_TARGETS:
        raise typer.BadParameter("target must be one of: strategy_backtest, selection_backtest.")
    start = _parse_trade_date("start_date", start_date)
    end = _parse_trade_date("end_date", end_date)
    output_dir = (
        results_dir
        if results_dir is not None
        else default_evolution_results_dir(strategy_name, normalized_target, generation, start, end)
    )
    try:
        result = evolve_loaded_strategy(
            db_path=db_path,
            loaded=loaded,
            target=normalized_target,
            start_date=start,
            end_date=end,
            generation=generation,
            max_mutations=max_mutations,
            top_n=top_n,
            seed=seed,
            results_dir=output_dir,
            generated_dir=generated_dir,
            save_experiments=save_experiments,
            progress_callback=typer.echo,
        )
    except (EvolutionError, SearchSpaceError, StrategyConfigError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Strategy: {result.strategy_name}")
    typer.echo(f"Target: {result.target}")
    typer.echo(f"Generation: {result.generation}")
    typer.echo(f"Candidates: {result.candidate_count}")
    typer.echo(f"Best score ({result.score_version}): {result.best_score}")
    typer.echo(f"Best run_id: {result.best_run_id}")
    typer.echo(f"Results directory: {result.results_dir.resolve()}")
    typer.echo("Survivors:")
    for rank, individual in enumerate(result.top_individuals, start=1):
        typer.echo(
            "\t".join(
                [
                    f"rank={rank}",
                    f"trial={individual.trial_index}",
                    f"run_id={individual.run_id}",
                    f"score={individual.score}",
                    f"generated={individual.generated_strategy_path}",
                ]
            )
        )


def register(root_app: typer.Typer) -> None:
    root_app.command(
        "evolve",
        help="Evolve one YAML strategy by mutating known optimization parameters.",
    )(evolve_strategy_command)
