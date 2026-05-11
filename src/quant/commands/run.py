from __future__ import annotations

from datetime import date
from pathlib import Path

import typer

from quant.commands.common import (
    echo_selection_backtest_report,
    echo_strategy_backtest_report,
)
from quant.config import DEFAULT_STRATEGIES_DIR, StrategyConfigError, load_strategy_config
from quant.db.init_db import DEFAULT_DB_PATH
from quant.experiments import ExperimentStore
from quant.pattern_selection import PatternSelectionError
from quant.strategy_runtime import (
    StrategyRuntimeError,
    format_loaded_strategy_holding_period_table,
    run_loaded_strategy_backtest,
    run_loaded_strategy_holding_period_matrix,
    run_loaded_strategy_selection_backtest,
    run_strategy_selection,
)


app = typer.Typer(help="Run selection and backtests using YAML strategies.")
matrix_app = typer.Typer(help="Run batch backtests derived from one YAML strategy.")
app.add_typer(matrix_app, name="matrix")


def _parse_trade_date(label: str, value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{label} must be in YYYY-MM-DD format.") from exc


def _load_run_strategy(strategy_name: str, strategies_dir: Path):
    try:
        return load_strategy_config(strategy_name, strategies_dir)
    except StrategyConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _echo_saved_experiment(saved) -> None:
    typer.echo(f"Experiment run_id: {saved.run_id}")
    typer.echo(f"Score ({saved.score_version}): {saved.score}")
    typer.echo(f"Artifacts directory: {saved.artifact_dir.resolve()}")


@app.command("select")
def run_select_command(
    strategy_name: str = typer.Argument(..., help="Strategy name, matching the YAML file stem."),
    trade_date: str = typer.Option(
        ...,
        "--trade-date",
        help="Target trading date in YYYY-MM-DD format.",
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
) -> None:
    """Run stock selection using a YAML strategy."""
    loaded = _load_run_strategy(strategy_name, strategies_dir)
    try:
        summary = run_strategy_selection(
            db_path=db_path,
            trade_date=_parse_trade_date("trade_date", trade_date),
            loaded=loaded,
        )
    except (ValueError, PatternSelectionError, StrategyRuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Strategy: {summary.strategy_name}")
    if summary.display_name:
        typer.echo(f"Display name: {summary.display_name}")
    typer.echo(f"Selection type: {summary.selection_type}")
    typer.echo(f"Trade date: {summary.trade_date}")
    typer.echo(f"Sort by: {summary.sort_by} {summary.sort_order}")
    typer.echo(f"Matched securities: {summary.candidate_count}")
    for candidate in summary.candidates:
        parts = [candidate.symbol, candidate.name]
        if candidate.d0_date is not None:
            parts.append(f"d0_date={candidate.d0_date}")
        if candidate.composite_score is not None:
            parts.append(f"score={candidate.composite_score:.2f}")
        typer.echo("\t".join(parts))


@app.command("backtest-selection")
def run_backtest_selection_command(
    strategy_name: str = typer.Argument(..., help="Strategy name, matching the YAML file stem."),
    start_date: str = typer.Option(
        ...,
        "--start-date",
        help="Backtest start trading date in YYYY-MM-DD format.",
    ),
    end_date: str = typer.Option(
        ...,
        "--end-date",
        help="Backtest end trading date in YYYY-MM-DD format.",
    ),
    db_path: Path = typer.Option(
        DEFAULT_DB_PATH,
        "--db-path",
        help="Target DuckDB file path.",
    ),
    show_progress: bool = typer.Option(
        False,
        "--show-progress",
        help="Print per-trading-day progress while the backtest is running.",
    ),
    run_name: str | None = typer.Option(
        None,
        "--run-name",
        help="Optional name for the saved experiment run.",
    ),
    no_save_experiment: bool = typer.Option(
        False,
        "--no-save-experiment",
        help="Skip automatic experiment persistence for this run.",
    ),
    strategies_dir: Path = typer.Option(
        DEFAULT_STRATEGIES_DIR,
        "--strategies-dir",
        help="Directory containing strategy YAML files.",
    ),
) -> None:
    """Run selection backtest using a YAML strategy."""
    loaded = _load_run_strategy(strategy_name, strategies_dir)
    if loaded.config.backtest_selection is None:
        raise typer.BadParameter(
            f"does not define backtest_selection defaults. Strategy: {strategy_name}."
        )

    try:
        report = run_loaded_strategy_selection_backtest(
            db_path=db_path,
            loaded=loaded,
            start_date=_parse_trade_date("start_date", start_date),
            end_date=_parse_trade_date("end_date", end_date),
            progress_callback=(
                (lambda progress: typer.echo(
                    f"[{progress.current_trade_day}/{progress.total_trade_days}] selection_date={progress.selection_date}"
                ))
                if show_progress
                else None
            ),
        )
    except (ValueError, PatternSelectionError, StrategyRuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    echo_selection_backtest_report(report)
    if not no_save_experiment:
        saved = ExperimentStore(db_path).save_selection_backtest(
            loaded,
            report,
            run_name=run_name,
        )
        _echo_saved_experiment(saved)


@app.command("backtest-strategy")
def run_backtest_strategy_command(
    strategy_name: str = typer.Argument(..., help="Strategy name, matching the YAML file stem."),
    start_date: str = typer.Option(
        ...,
        "--start-date",
        help="Strategy backtest start trading date in YYYY-MM-DD format.",
    ),
    end_date: str = typer.Option(
        ...,
        "--end-date",
        help="Strategy backtest end trading date in YYYY-MM-DD format.",
    ),
    db_path: Path = typer.Option(
        DEFAULT_DB_PATH,
        "--db-path",
        help="Target DuckDB file path.",
    ),
    show_progress: bool = typer.Option(
        False,
        "--show-progress",
        help="Print per-trading-day progress while the strategy backtest is running.",
    ),
    run_name: str | None = typer.Option(
        None,
        "--run-name",
        help="Optional name for the saved experiment run.",
    ),
    no_save_experiment: bool = typer.Option(
        False,
        "--no-save-experiment",
        help="Skip automatic experiment persistence for this run.",
    ),
    strategies_dir: Path = typer.Option(
        DEFAULT_STRATEGIES_DIR,
        "--strategies-dir",
        help="Directory containing strategy YAML files.",
    ),
) -> None:
    """Run strategy backtest using a YAML strategy."""
    loaded = _load_run_strategy(strategy_name, strategies_dir)
    if loaded.config.strategy_backtest is None:
        raise typer.BadParameter(
            f"does not define strategy_backtest defaults. Strategy: {strategy_name}."
        )
    try:
        report = run_loaded_strategy_backtest(
            db_path=db_path,
            loaded=loaded,
            start_date=_parse_trade_date("start_date", start_date),
            end_date=_parse_trade_date("end_date", end_date),
            progress_callback=(
                (lambda progress: typer.echo(
                    f"[{progress.current_trade_day}/{progress.total_trade_days}] trade_date={progress.trade_date}"
                ))
                if show_progress
                else None
            ),
        )
    except (ValueError, PatternSelectionError, StrategyRuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    echo_strategy_backtest_report(report)
    if not no_save_experiment:
        saved = ExperimentStore(db_path).save_strategy_backtest(
            loaded,
            report,
            run_name=run_name,
        )
        _echo_saved_experiment(saved)


@matrix_app.command("strategy-holding-periods")
def run_strategy_holding_periods_matrix_command(
    strategy_name: str = typer.Argument(..., help="Strategy name, matching the YAML file stem."),
    start_date: str = typer.Option(
        ...,
        "--start-date",
        help="Strategy backtest start trading date in YYYY-MM-DD format.",
    ),
    end_date: str = typer.Option(
        ...,
        "--end-date",
        help="Strategy backtest end trading date in YYYY-MM-DD format.",
    ),
    holding_periods: str = typer.Option(
        ...,
        "--holding-periods",
        help="Comma-separated holding periods, for example 1,2,3.",
    ),
    db_path: Path = typer.Option(
        DEFAULT_DB_PATH,
        "--db-path",
        help="Target DuckDB file path.",
    ),
    results_dir: Path | None = typer.Option(
        None,
        "--results-dir",
        help="Optional output directory for per-holding-period JSON summaries.",
    ),
    show_progress: bool = typer.Option(
        False,
        "--show-progress",
        help="Print per-trading-day progress while each backtest is running.",
    ),
    no_save_experiment: bool = typer.Option(
        False,
        "--no-save-experiment",
        help="Skip automatic experiment persistence for each holding-period run.",
    ),
    strategies_dir: Path = typer.Option(
        DEFAULT_STRATEGIES_DIR,
        "--strategies-dir",
        help="Directory containing strategy YAML files.",
    ),
) -> None:
    """Run strategy backtests across multiple holding periods."""
    loaded = _load_run_strategy(strategy_name, strategies_dir)
    if loaded.config.strategy_backtest is None:
        raise typer.BadParameter(
            f"does not define strategy_backtest defaults. Strategy: {strategy_name}."
        )
    try:
        parsed_holding_periods = [
            int(value.strip())
            for value in holding_periods.split(",")
            if value.strip()
        ]
    except ValueError as exc:
        raise typer.BadParameter("holding_periods must be a comma-separated integer list.") from exc
    if not parsed_holding_periods:
        raise typer.BadParameter("Provide at least one holding period.")

    start = _parse_trade_date("start_date", start_date)
    end = _parse_trade_date("end_date", end_date)
    output_dir = (
        results_dir
        if results_dir is not None
        else Path("results")
        / "strategy_holding_periods"
        / f"{strategy_name}_{start.isoformat()}_{end.isoformat()}"
    )
    experiment_store = ExperimentStore(db_path)
    try:
        summaries = run_loaded_strategy_holding_period_matrix(
            db_path=db_path,
            loaded=loaded,
            start_date=start,
            end_date=end,
            holding_periods=parsed_holding_periods,
            results_dir=output_dir,
            experiment_callback=(
                None
                if no_save_experiment
                else (
                    lambda holding_period, report: experiment_store.save_strategy_backtest(
                        loaded,
                        report,
                        run_name=(
                            f"{strategy_name}_strategy_backtest_hp_{holding_period}_"
                            f"{start.isoformat()}_{end.isoformat()}"
                        ),
                    )
                )
            ),
            progress_callback=(
                (lambda progress: typer.echo(
                    f"[{progress.current_trade_day}/{progress.total_trade_days}] trade_date={progress.trade_date}"
                ))
                if show_progress
                else None
            ),
        )
    except (ValueError, PatternSelectionError, StrategyRuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Results directory: {output_dir.resolve()}")
    typer.echo(format_loaded_strategy_holding_period_table(summaries))
