from __future__ import annotations

from pathlib import Path

import typer

from quant.strategy_matrix import (
    format_strategy_holding_period_table,
    load_strategy_holding_period_summaries,
    write_strategy_holding_period_summary_csv,
)


app = typer.Typer(help="Summarize generated analysis outputs.")


@app.command("strategy-holding-periods")
def summarize_strategy_holding_periods_command(
    results_dir: Path = typer.Option(
        ...,
        "--results-dir",
        help="Directory containing holding_period_<N>.json files.",
    ),
    out_csv: Path | None = typer.Option(
        None,
        "--out-csv",
        help="Optional output path for a CSV summary.",
    ),
) -> None:
    """Summarize matrix strategy backtest runs."""
    try:
        summaries = load_strategy_holding_period_summaries(results_dir)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(format_strategy_holding_period_table(summaries))
    if out_csv is not None:
        write_strategy_holding_period_summary_csv(summaries, out_csv)
        typer.echo(f"Wrote summary CSV: {out_csv.resolve()}")
