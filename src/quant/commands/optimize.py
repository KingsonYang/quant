from __future__ import annotations

from datetime import date
from pathlib import Path

import typer

from quant.config import DEFAULT_STRATEGIES_DIR, StrategyConfigError, load_strategy_config
from quant.db.init_db import DEFAULT_DB_PATH
from quant.optimization import OptimizationError, optimize_loaded_strategy
from quant.optimization.runner import (
    OPTIMIZATION_METHOD_GRID,
    OPTIMIZATION_METHOD_RANDOM,
    OPTIMIZATION_TARGET_SELECTION,
    OPTIMIZATION_TARGET_STRATEGY,
    default_optimization_results_dir,
)
from quant.optimization.search_space import SearchSpaceError


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


def optimize_strategy_command(
    strategy_name: str = typer.Argument(..., help="Strategy name, matching the YAML file stem."),
    start_date: str = typer.Option(
        ...,
        "--start-date",
        help="Optimization backtest start date in YYYY-MM-DD format.",
    ),
    end_date: str = typer.Option(
        ...,
        "--end-date",
        help="Optimization backtest end date in YYYY-MM-DD format.",
    ),
    method: str = typer.Option(
        OPTIMIZATION_METHOD_GRID,
        "--method",
        help="Optimization search method: grid or random.",
    ),
    target: str = typer.Option(
        OPTIMIZATION_TARGET_STRATEGY,
        "--target",
        help="Optimization target: strategy_backtest or selection_backtest.",
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
    max_trials: int | None = typer.Option(
        None,
        "--max-trials",
        help="Maximum number of random trials. Required for --method random.",
    ),
    seed: int | None = typer.Option(
        None,
        "--seed",
        help="Optional random seed for reproducible random search.",
    ),
    top_n: int = typer.Option(
        5,
        "--top-n",
        min=1,
        help="Number of top optimized strategies to materialize.",
    ),
    results_dir: Path | None = typer.Option(
        None,
        "--results-dir",
        help="Optional directory for optimization outputs.",
    ),
) -> None:
    """Optimize one existing YAML strategy."""
    loaded = _load_strategy(strategy_name, strategies_dir)
    method = method.strip().lower()
    target = target.strip().lower()
    if method not in {OPTIMIZATION_METHOD_GRID, OPTIMIZATION_METHOD_RANDOM}:
        raise typer.BadParameter("method must be one of: grid, random.")
    if target not in {OPTIMIZATION_TARGET_STRATEGY, OPTIMIZATION_TARGET_SELECTION}:
        raise typer.BadParameter(
            "target must be one of: strategy_backtest, selection_backtest."
        )
    if method == OPTIMIZATION_METHOD_RANDOM and (max_trials is None or max_trials < 1):
        raise typer.BadParameter("--max-trials is required for --method random.")
    start = _parse_trade_date("start_date", start_date)
    end = _parse_trade_date("end_date", end_date)
    output_dir = (
        results_dir
        if results_dir is not None
        else default_optimization_results_dir(strategy_name, target, start, end)
    )
    try:
        result = optimize_loaded_strategy(
            db_path=db_path,
            loaded=loaded,
            method=method,
            target=target,
            start_date=start,
            end_date=end,
            top_n=top_n,
            results_dir=output_dir,
            max_trials=max_trials,
            seed=seed,
            progress_callback=typer.echo,
        )
    except (OptimizationError, SearchSpaceError, StrategyConfigError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Strategy: {result.strategy_name}")
    typer.echo(f"Target: {result.target}")
    typer.echo(f"Method: {result.method}")
    typer.echo(f"Trials: {result.trial_count}")
    typer.echo(f"Best score ({result.score_version}): {result.best_score}")
    typer.echo(f"Best run_id: {result.best_run_id}")
    typer.echo(f"Results directory: {result.results_dir.resolve()}")
    typer.echo("Top trials:")
    for rank, trial in enumerate(result.top_trials, start=1):
        typer.echo(
            "\t".join(
                [
                    f"rank={rank}",
                    f"trial={trial.trial_index}",
                    f"run_id={trial.run_id}",
                    f"score={trial.score}",
                    f"generated={trial.generated_strategy_path}",
                ]
            )
        )


def register(root_app: typer.Typer) -> None:
    root_app.command(
        "optimize",
        help="Optimize one existing YAML strategy.",
    )(optimize_strategy_command)
