from __future__ import annotations

import json
from pathlib import Path

import typer

from quant.db.init_db import DEFAULT_DB_PATH
from quant.experiments import ExperimentStore


app = typer.Typer(help="Inspect saved backtest experiments.")


@app.command("list")
def list_experiments_command(
    strategy: str | None = typer.Option(
        None,
        "--strategy",
        help="Optional strategy name filter.",
    ),
    run_kind: str | None = typer.Option(
        None,
        "--run-kind",
        help="Optional run kind filter: selection_backtest or strategy_backtest.",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        min=1,
        help="Maximum number of runs to display.",
    ),
    db_path: Path = typer.Option(
        DEFAULT_DB_PATH,
        "--db-path",
        help="Target DuckDB file path.",
    ),
) -> None:
    """List saved experiment runs."""
    items = ExperimentStore(db_path).list_runs(
        strategy_name=strategy,
        run_kind=run_kind,
        limit=limit,
    )
    if not items:
        typer.echo("No experiment runs found.")
        return
    for item in items:
        typer.echo(
            "\t".join(
                [
                    str(item.run_id),
                    item.strategy_key,
                    item.run_kind,
                    f"{item.start_date}->{item.end_date}",
                    f"score={item.score}",
                    item.run_status,
                    str(item.created_at),
                ]
            )
        )


@app.command("show")
def show_experiment_command(
    run_id: int = typer.Argument(..., help="Experiment run identifier."),
    db_path: Path = typer.Option(
        DEFAULT_DB_PATH,
        "--db-path",
        help="Target DuckDB file path.",
    ),
) -> None:
    """Show one saved experiment run."""
    try:
        detail = ExperimentStore(db_path).load_run_detail(run_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Run ID: {detail.run_id}")
    typer.echo(f"Strategy: {detail.strategy_key}")
    typer.echo(f"Display name: {detail.strategy_name}")
    typer.echo(f"Run kind: {detail.run_kind}")
    typer.echo(f"Run name: {detail.run_name}")
    typer.echo(f"Date range: {detail.start_date} -> {detail.end_date}")
    typer.echo(f"Status: {detail.run_status}")
    typer.echo(f"Score ({detail.score_version}): {detail.score}")
    typer.echo(f"Created at: {detail.created_at}")
    typer.echo("Metrics:")
    for metric_name, metric_value in sorted(detail.metrics.items()):
        typer.echo(f"{metric_name}: {metric_value}")
    if detail.latest_equity is not None:
        typer.echo("Latest equity:")
        for key, value in detail.latest_equity.items():
            typer.echo(f"{key}: {value}")
    if detail.trade_overview is not None:
        typer.echo("Trade overview:")
        for key, value in detail.trade_overview.items():
            typer.echo(f"{key}: {value}")
    typer.echo("Artifacts:")
    for artifact in detail.artifacts:
        typer.echo(f"{artifact.artifact_name}\t{artifact.artifact_type}\t{artifact.relative_path}")
    typer.echo("Parameter snapshot:")
    typer.echo(json.dumps(detail.parameter_snapshot, ensure_ascii=False, indent=2))


@app.command("compare")
def compare_experiments_command(
    run_id_1: int = typer.Argument(..., help="Left experiment run identifier."),
    run_id_2: int = typer.Argument(..., help="Right experiment run identifier."),
    db_path: Path = typer.Option(
        DEFAULT_DB_PATH,
        "--db-path",
        help="Target DuckDB file path.",
    ),
) -> None:
    """Compare the common metrics of two experiment runs."""
    store = ExperimentStore(db_path)
    try:
        left = store.load_run_detail(run_id_1)
        right = store.load_run_detail(run_id_2)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Left: {left.run_id} {left.strategy_key} {left.run_kind}")
    typer.echo(f"Right: {right.run_id} {right.strategy_key} {right.run_kind}")
    common_metrics = sorted(set(left.metrics) & set(right.metrics))
    if not common_metrics:
        typer.echo("No common metrics found.")
        return
    for metric_name in common_metrics:
        typer.echo(
            "\t".join(
                [
                    metric_name,
                    str(left.metrics.get(metric_name)),
                    str(right.metrics.get(metric_name)),
                ]
            )
        )
