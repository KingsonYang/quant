from __future__ import annotations

from pathlib import Path

import typer

from quant.config import (
    DEFAULT_STRATEGIES_DIR,
    StrategyConfigError,
    discover_strategy_configs,
    load_strategy_config,
)


app = typer.Typer(help="Manage YAML strategy definitions.")


@app.command("list")
def list_strategies_command(
    strategies_dir: Path = typer.Option(
        DEFAULT_STRATEGIES_DIR,
        "--strategies-dir",
        help="Directory containing strategy YAML files.",
    ),
) -> None:
    """List available strategies."""
    try:
        strategies = discover_strategy_configs(strategies_dir)
    except StrategyConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Strategies directory: {strategies_dir.resolve()}")
    for strategy in strategies:
        display_name = strategy.config.strategy.display_name or ""
        description = strategy.config.strategy.description or ""
        typer.echo(f"{strategy.name}\t{display_name}\t{strategy.path}\t{description}")


@app.command("show")
def show_strategy_command(
    strategy_name: str = typer.Argument(..., help="Strategy name, matching the YAML file stem."),
    strategies_dir: Path = typer.Option(
        DEFAULT_STRATEGIES_DIR,
        "--strategies-dir",
        help="Directory containing strategy YAML files.",
    ),
) -> None:
    """Show one strategy file."""
    try:
        loaded = load_strategy_config(strategy_name, strategies_dir)
    except StrategyConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Strategy: {loaded.name}")
    typer.echo(f"Path: {loaded.path.resolve()}")
    if loaded.config.strategy.display_name:
        typer.echo(f"Display name: {loaded.config.strategy.display_name}")
    if loaded.config.strategy.description:
        typer.echo(f"Description: {loaded.config.strategy.description}")
    typer.echo("---")
    typer.echo(loaded.path.read_text(encoding="utf-8"))


@app.command("validate")
def validate_strategy_command(
    strategy_name: str | None = typer.Argument(
        None,
        help="Optional strategy name. When omitted, validate all top-level strategy files.",
    ),
    strategies_dir: Path = typer.Option(
        DEFAULT_STRATEGIES_DIR,
        "--strategies-dir",
        help="Directory containing strategy YAML files.",
    ),
) -> None:
    """Validate one strategy or all strategy files."""
    try:
        if strategy_name is None:
            strategies = discover_strategy_configs(strategies_dir)
            typer.echo(f"Validated {len(strategies)} strategy file(s).")
            for strategy in strategies:
                typer.echo(f"OK\t{strategy.name}\t{strategy.path}")
        else:
            loaded = load_strategy_config(strategy_name, strategies_dir)
            typer.echo(f"OK\t{loaded.name}\t{loaded.path}")
    except StrategyConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc
