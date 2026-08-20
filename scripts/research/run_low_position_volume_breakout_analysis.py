from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant.config import load_strategy_config
from quant.evaluation.metrics import (
    build_strategy_backtest_metrics,
    build_strategy_yearly_metrics,
)
from quant.optimization.random_search import build_random_trials
from quant.optimization.search_space import (
    ParameterOverride,
    apply_parameter_overrides,
    build_override_snapshot,
    validate_search_space,
)
from quant.strategy_runtime import run_loaded_strategy_backtest


DEFAULT_PERIODS = [
    ("long", date(2015, 12, 29), date(2026, 5, 25)),
    ("old", date(2015, 12, 29), date(2023, 12, 29)),
    ("recent_2y", date(2024, 1, 2), date(2026, 5, 25)),
    ("recent_1y", date(2025, 1, 2), date(2026, 5, 25)),
]


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _summary_row(label: str, start_date: date, end_date: date, report) -> dict[str, Any]:
    metrics = build_strategy_backtest_metrics(report)
    summary = report.summary
    row: dict[str, Any] = {
        "period": label,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "trade_days": summary.trade_days,
        "initial_capital": _scalar(summary.initial_capital),
        "final_assets": _scalar(summary.current_total_assets),
        "cash": _scalar(summary.current_cash_balance),
        "market_value": _scalar(summary.current_market_value),
        "realized_profit": _scalar(summary.realized_profit),
        "total_return_pct": _scalar(summary.total_return_pct),
        "annualized_return_pct": _scalar(summary.annualized_return_pct),
        "closed_trades": summary.closed_trade_count,
        "open_positions": summary.open_position_count,
    }
    row.update(metrics)
    return row


def run_fixed_periods(*, db_path: Path, strategy_name: str, strategies_dir: Path, results_dir: Path) -> None:
    loaded = load_strategy_config(strategy_name, strategies_dir)
    summary_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []
    for label, start_date, end_date in DEFAULT_PERIODS:
        report = run_loaded_strategy_backtest(
            db_path=db_path,
            loaded=loaded,
            start_date=start_date,
            end_date=end_date,
        )
        summary_rows.append(_summary_row(label, start_date, end_date, report))
        for year, metrics in build_strategy_yearly_metrics(report).items():
            yearly_rows.append(
                {
                    "period": label,
                    "year": year,
                    **metrics,
                }
            )

    _write_csv(results_dir / "fixed_backtest_summary.csv", summary_rows)
    _write_csv(results_dir / "yearly_metrics.csv", yearly_rows)


def run_random_search(
    *,
    db_path: Path,
    strategy_name: str,
    strategies_dir: Path,
    results_dir: Path,
    max_trials: int,
    seed: int,
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    loaded = load_strategy_config(strategy_name, strategies_dir)
    search_space = validate_search_space(loaded)
    trials = build_random_trials(search_space, max_trials=max_trials, seed=seed)
    rows: list[dict[str, Any]] = []
    for index, overrides in enumerate(trials, start=1):
        optimized_loaded = apply_parameter_overrides(loaded, overrides)
        report = run_loaded_strategy_backtest(
            db_path=db_path,
            loaded=optimized_loaded,
            start_date=start_date,
            end_date=end_date,
        )
        metrics = build_strategy_backtest_metrics(report)
        snapshot = build_override_snapshot(overrides)
        row: dict[str, Any] = {
            "trial_index": index,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "final_assets": _scalar(report.summary.current_total_assets),
            "total_return_pct": _scalar(report.summary.total_return_pct),
            "annualized_return_pct": _scalar(report.summary.annualized_return_pct),
            "closed_trades": report.summary.closed_trade_count,
            "win_rate_pct": metrics.get("win_rate_pct"),
            "max_drawdown_pct": metrics.get("max_drawdown_pct"),
            "average_trade_return_pct": metrics.get("average_trade_return_pct"),
            "best_trade_return_pct": metrics.get("best_trade_return_pct"),
            "worst_trade_return_pct": metrics.get("worst_trade_return_pct"),
            "profit_factor": metrics.get("profit_factor"),
            "parameter_overrides_json": json.dumps(
                snapshot,
                ensure_ascii=False,
                sort_keys=True,
                default=_json_default,
            ),
        }
        row.update({f"param.{key}": _scalar(value) for key, value in snapshot.items()})
        rows.append(row)

    ranked = sorted(
        rows,
        key=lambda row: (
            -float(row["final_assets"]),
            -int(row["closed_trades"]),
            row["trial_index"],
        ),
    )
    _write_csv(results_dir / "random_trials_ranked_by_final_assets.csv", ranked)
    top_payload = ranked[:10]
    (results_dir / "top10_random_trials.json").write_text(
        json.dumps(top_payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return ranked


def _overrides_from_row(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("parameter_overrides_json", "{}")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("parameter_overrides_json must be a JSON object.")
    return parsed


def run_top_trial_period_comparison(
    *,
    db_path: Path,
    strategy_name: str,
    strategies_dir: Path,
    results_dir: Path,
    top_rows: list[dict[str, Any]],
    top_count: int,
) -> None:
    loaded = load_strategy_config(strategy_name, strategies_dir)
    comparison_rows: list[dict[str, Any]] = []
    yearly_rows: list[dict[str, Any]] = []
    for rank, row in enumerate(top_rows[:top_count], start=1):
        overrides = _overrides_from_row(row)
        optimized_loaded = apply_parameter_overrides(
            loaded,
            [
                ParameterOverride(path=path, value=value)
                for path, value in overrides.items()
            ],
        )
        for label, start_date, end_date in DEFAULT_PERIODS:
            report = run_loaded_strategy_backtest(
                db_path=db_path,
                loaded=optimized_loaded,
                start_date=start_date,
                end_date=end_date,
            )
            summary = _summary_row(label, start_date, end_date, report)
            summary["rank"] = rank
            summary["source_trial_index"] = row.get("trial_index")
            summary["parameter_overrides_json"] = json.dumps(
                overrides,
                ensure_ascii=False,
                sort_keys=True,
                default=_json_default,
            )
            comparison_rows.append(summary)
            for year, metrics in build_strategy_yearly_metrics(report).items():
                yearly_rows.append(
                    {
                        "rank": rank,
                        "source_trial_index": row.get("trial_index"),
                        "period": label,
                        "year": year,
                        **metrics,
                    }
                )

    _write_csv(results_dir / "top_trial_period_comparison.csv", comparison_rows)
    _write_csv(results_dir / "top_trial_yearly_metrics.csv", yearly_rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", type=Path, default=Path("data/quant.duckdb"))
    parser.add_argument("--strategies-dir", type=Path, default=Path("strategies"))
    parser.add_argument("--strategy-name", default="low_position_volume_breakout")
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=Path("results/research/low_position_volume_breakout"),
    )
    parser.add_argument("--max-trials", type=int, default=80)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--random-start-date", type=date.fromisoformat, default=date(2015, 12, 29))
    parser.add_argument("--random-end-date", type=date.fromisoformat, default=date(2026, 5, 25))
    parser.add_argument("--skip-fixed", action="store_true")
    parser.add_argument("--skip-random", action="store_true")
    parser.add_argument("--top-compare-count", type=int, default=3)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.results_dir.mkdir(parents=True, exist_ok=True)
    if not args.skip_fixed:
        run_fixed_periods(
            db_path=args.db_path,
            strategy_name=args.strategy_name,
            strategies_dir=args.strategies_dir,
            results_dir=args.results_dir,
        )
    ranked: list[dict[str, Any]]
    if args.skip_random:
        top_json_path = args.results_dir / "top10_random_trials.json"
        ranked = json.loads(top_json_path.read_text(encoding="utf-8"))
    else:
        ranked = run_random_search(
            db_path=args.db_path,
            strategy_name=args.strategy_name,
            strategies_dir=args.strategies_dir,
            results_dir=args.results_dir,
            max_trials=args.max_trials,
            seed=args.seed,
            start_date=args.random_start_date,
            end_date=args.random_end_date,
        )
    if args.top_compare_count > 0:
        run_top_trial_period_comparison(
            db_path=args.db_path,
            strategy_name=args.strategy_name,
            strategies_dir=args.strategies_dir,
            results_dir=args.results_dir,
            top_rows=ranked,
            top_count=args.top_compare_count,
        )
    print(f"Wrote analysis files to {args.results_dir}")


if __name__ == "__main__":
    main()
