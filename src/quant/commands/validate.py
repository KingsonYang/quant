from __future__ import annotations

from datetime import date
from pathlib import Path

import typer

from quant.config import DEFAULT_STRATEGIES_DIR, StrategyConfigError, load_strategy_config
from quant.db.init_db import DEFAULT_DB_PATH
from quant.optimization.search_space import SearchSpaceError
from quant.validation import (
    VALIDATION_MODE_ALL,
    VALIDATION_MODE_PERTURBATION,
    VALIDATION_MODE_SPLIT,
    VALIDATION_MODE_WALK_FORWARD,
    VALIDATION_TARGET_SELECTION,
    VALIDATION_TARGET_STRATEGY,
    ValidationError,
    validate_loaded_strategy,
)
from quant.validation.runner import default_validation_results_dir
from quant.validation.splits import SplitError


VALIDATION_MODES = {
    VALIDATION_MODE_ALL,
    VALIDATION_MODE_SPLIT,
    VALIDATION_MODE_WALK_FORWARD,
    VALIDATION_MODE_PERTURBATION,
}
VALIDATION_TARGETS = {
    VALIDATION_TARGET_STRATEGY,
    VALIDATION_TARGET_SELECTION,
}


def _parse_trade_date(label: str, value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{label} must be in YYYY-MM-DD format.") from exc


def _parse_split_ratios(value: str) -> tuple[int, int, int]:
    try:
        parts = [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise typer.BadParameter("--split-ratios must contain three comma-separated integers.") from exc
    if len(parts) != 3 or any(part <= 0 for part in parts):
        raise typer.BadParameter("--split-ratios must contain three positive integers.")
    return parts[0], parts[1], parts[2]


def _load_strategy(strategy_name: str, strategies_dir: Path):
    try:
        return load_strategy_config(strategy_name, strategies_dir)
    except StrategyConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc


def validate_strategy_command(
    strategy_name: str = typer.Argument(..., help="Strategy name, matching the YAML file stem."),
    start_date: str = typer.Option(
        ...,
        "--start-date",
        help="Validation start trading date in YYYY-MM-DD format.",
    ),
    end_date: str = typer.Option(
        ...,
        "--end-date",
        help="Validation end trading date in YYYY-MM-DD format.",
    ),
    target: str = typer.Option(
        VALIDATION_TARGET_STRATEGY,
        "--target",
        help="Validation target: strategy_backtest or selection_backtest.",
    ),
    mode: str = typer.Option(
        VALIDATION_MODE_ALL,
        "--mode",
        help="Validation mode: all, split, walk-forward, or perturbation.",
    ),
    split_ratios: str = typer.Option(
        "60,20,20",
        "--split-ratios",
        help="Train/validation/test ratios, for example 60,20,20.",
    ),
    walk_folds: int = typer.Option(
        3,
        "--walk-folds",
        min=1,
        help="Number of expanding walk-forward folds.",
    ),
    max_perturbations: int = typer.Option(
        20,
        "--max-perturbations",
        min=1,
        help="Maximum number of single-parameter perturbation trials.",
    ),
    seed: int | None = typer.Option(
        None,
        "--seed",
        help="Optional random seed for perturbation sampling.",
    ),
    save_experiments: bool = typer.Option(
        True,
        "--save-experiments/--no-save-experiments",
        help="Save validation sub-runs to the experiment store.",
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
    results_dir: Path | None = typer.Option(
        None,
        "--results-dir",
        help="Optional directory for validation outputs.",
    ),
) -> None:
    """Validate one YAML strategy across time splits and perturbations."""
    loaded = _load_strategy(strategy_name, strategies_dir)
    normalized_target = target.strip().lower()
    normalized_mode = mode.strip().lower()
    if normalized_target not in VALIDATION_TARGETS:
        raise typer.BadParameter("target must be one of: strategy_backtest, selection_backtest.")
    if normalized_mode not in VALIDATION_MODES:
        raise typer.BadParameter("mode must be one of: all, split, walk-forward, perturbation.")
    start = _parse_trade_date("start_date", start_date)
    end = _parse_trade_date("end_date", end_date)
    output_dir = (
        results_dir
        if results_dir is not None
        else default_validation_results_dir(strategy_name, normalized_target, start, end)
    )
    try:
        result = validate_loaded_strategy(
            db_path=db_path,
            loaded=loaded,
            target=normalized_target,
            mode=normalized_mode,
            start_date=start,
            end_date=end,
            split_ratios=_parse_split_ratios(split_ratios),
            walk_folds=walk_folds,
            max_perturbations=max_perturbations,
            seed=seed,
            save_experiments=save_experiments,
            results_dir=output_dir,
            progress_callback=typer.echo,
        )
    except (ValidationError, SplitError, SearchSpaceError, StrategyConfigError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Strategy: {result.strategy_name}")
    typer.echo(f"Target: {result.target}")
    typer.echo(f"Mode: {result.mode}")
    typer.echo(f"Train score: {result.train_score}")
    typer.echo(f"Validation score: {result.validation_score}")
    typer.echo(f"Test score: {result.test_score}")
    typer.echo(f"Stability score: {result.stability_score}")
    typer.echo(f"Overfit penalty: {result.overfit_penalty}")
    typer.echo(f"Results directory: {result.results_dir.resolve()}")


def register(root_app: typer.Typer) -> None:
    root_app.command(
        "validate",
        help="Validate one YAML strategy across time splits and perturbations.",
    )(validate_strategy_command)
