from __future__ import annotations

from datetime import date
from pathlib import Path

import typer

from quant.config import DEFAULT_STRATEGIES_DIR, StrategyConfigError, load_strategy_config
from quant.db.init_db import DEFAULT_DB_PATH
from quant.discovery import (
    DISCOVERY_TARGET_SELECTION,
    DiscoveryError,
    default_discovery_results_dir,
    discover_filters,
)


app = typer.Typer(help="Discover candidate filters and research rules.")


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


@app.command("filters")
def discover_filters_command(
    strategy_name: str = typer.Argument(..., help="Strategy name, matching the YAML file stem."),
    start_date: str = typer.Option(
        ...,
        "--start-date",
        help="Discovery start date in YYYY-MM-DD format.",
    ),
    end_date: str = typer.Option(
        ...,
        "--end-date",
        help="Discovery end date in YYYY-MM-DD format.",
    ),
    target: str = typer.Option(
        DISCOVERY_TARGET_SELECTION,
        "--target",
        help="Discovery target. First version supports selection_backtest.",
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
        help="Directory for generated discovered strategy YAML files.",
    ),
    top_rules: int = typer.Option(
        5,
        "--top-rules",
        min=1,
        help="Number of top filter rules to materialize as strategies.",
    ),
    bins: int = typer.Option(
        5,
        "--bins",
        min=1,
        help="Number of equal-frequency bins per feature.",
    ),
    min_samples: int = typer.Option(
        50,
        "--min-samples",
        min=1,
        help="Minimum samples required for a bin to become a candidate filter.",
    ),
    seed: int | None = typer.Option(
        None,
        "--seed",
        help="Reserved for reproducible future discovery methods.",
    ),
    results_dir: Path | None = typer.Option(
        None,
        "--results-dir",
        help="Optional directory for discovery outputs.",
    ),
) -> None:
    """Discover single-feature filters for a YAML rules strategy."""
    del seed
    loaded = _load_strategy(strategy_name, strategies_dir)
    normalized_target = target.strip().lower()
    start = _parse_trade_date("start_date", start_date)
    end = _parse_trade_date("end_date", end_date)
    output_dir = (
        results_dir
        if results_dir is not None
        else default_discovery_results_dir(strategy_name, normalized_target, start, end)
    )
    try:
        result = discover_filters(
            db_path=db_path,
            loaded=loaded,
            target=normalized_target,
            start_date=start,
            end_date=end,
            top_rules=top_rules,
            bins=bins,
            min_samples=min_samples,
            results_dir=output_dir,
            generated_dir=generated_dir,
        )
    except (DiscoveryError, StrategyConfigError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Strategy: {result.strategy_name}")
    typer.echo(f"Target: {result.target}")
    typer.echo(f"Samples: {result.completed_sample_count}/{result.sample_count} completed")
    typer.echo(f"Baseline win rate (%): {result.baseline.win_rate_pct}")
    typer.echo(f"Baseline average return (%): {result.baseline.average_return_pct}")
    typer.echo(f"Results directory: {result.results_dir.resolve()}")
    typer.echo("Top discovered filters:")
    for rule, generated_path in zip(result.top_rules, result.generated_strategy_paths, strict=True):
        typer.echo(
            "\t".join(
                [
                    f"rank={rule.rank}",
                    f"feature={rule.feature}",
                    f"range=[{rule.lower_bound}, {rule.upper_bound}]",
                    f"samples={rule.sample_count}",
                    f"win_rate={rule.win_rate_pct}",
                    f"avg_return={rule.average_return_pct}",
                    f"score={rule.score}",
                    f"generated={generated_path}",
                ]
            )
        )


def register(root_app: typer.Typer) -> None:
    root_app.add_typer(app, name="discover")
