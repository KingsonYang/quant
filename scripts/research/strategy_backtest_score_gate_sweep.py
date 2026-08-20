from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from quant.config.loader import load_strategy_config
from quant.strategy_runtime import run_loaded_strategy_backtest


def _date(value: str) -> date:
    return date.fromisoformat(value)


def _ints(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def _decimals(value: str) -> list[Decimal | None]:
    result: list[Decimal | None] = []
    for item in value.split(","):
        stripped = item.strip()
        if not stripped:
            continue
        if stripped.lower() in {"none", "null"}:
            result.append(None)
        else:
            result.append(Decimal(stripped))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", type=Path, default=Path("data/quant.duckdb"))
    parser.add_argument("--strategies-dir", type=Path, default=Path("strategies"))
    parser.add_argument("--strategy", default="limit_up_pullback_ma5_reclaim")
    parser.add_argument("--start-date", type=_date, required=True)
    parser.add_argument("--end-date", type=_date, required=True)
    parser.add_argument("--holding-periods", type=_ints, default=_ints("3,5"))
    parser.add_argument("--min-scores", type=_decimals, default=_decimals("none,70,75,80"))
    parser.add_argument("--score-gaps", type=_decimals, default=_decimals("none,3,5"))
    parser.add_argument("--selection-top-n", type=int, default=1)
    parser.add_argument("--output-dir", type=Path, default=Path("results/research/strategy_score_gate_sweep"))
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    loaded = load_strategy_config(args.strategy, args.strategies_dir)
    if loaded.config.strategy_backtest is None:
        raise SystemExit(f"Strategy {args.strategy} does not define strategy_backtest.")

    defaults = loaded.config.strategy_backtest
    original_top_n = defaults.selection_top_n
    original_min_score = defaults.min_composite_score
    original_gap = defaults.min_score_gap

    rows: list[dict[str, Any]] = []
    for min_score in args.min_scores:
        for score_gap in args.score_gaps:
            for holding_period in args.holding_periods:
                defaults.selection_top_n = args.selection_top_n
                defaults.min_composite_score = min_score
                defaults.min_score_gap = score_gap
                report = run_loaded_strategy_backtest(
                    db_path=args.db_path,
                    loaded=loaded,
                    start_date=args.start_date,
                    end_date=args.end_date,
                    holding_period=holding_period,
                )
                summary = report.summary
                closed = [trade for trade in report.trades if trade.status == "closed"]
                positive = sum(1 for trade in closed if trade.return_pct is not None and trade.return_pct > 0)
                win_rate = positive * 100 / len(closed) if closed else 0.0
                avg_return = (
                    sum(float(trade.return_pct) for trade in closed if trade.return_pct is not None) / len(closed)
                    if closed
                    else 0.0
                )
                rows.append(
                    {
                        "holding_period": holding_period,
                        "selection_top_n": args.selection_top_n,
                        "min_composite_score": str(min_score) if min_score is not None else "",
                        "min_score_gap": str(score_gap) if score_gap is not None else "",
                        "closed_trade_count": summary.closed_trade_count,
                        "open_position_count": summary.open_position_count,
                        "total_trade_count": summary.total_trade_count,
                        "final_assets": float(summary.current_total_assets),
                        "total_return_pct": float(summary.total_return_pct),
                        "realized_profit": float(summary.realized_profit),
                        "win_rate_pct": win_rate,
                        "avg_trade_return_pct": avg_return,
                    }
                )
                print(
                    f"hp={holding_period} min_score={min_score} gap={score_gap} "
                    f"trades={summary.closed_trade_count} assets={summary.current_total_assets} "
                    f"return={summary.total_return_pct}% win_rate={win_rate:.2f}%"
                )

    defaults.selection_top_n = original_top_n
    defaults.min_composite_score = original_min_score
    defaults.min_score_gap = original_gap

    frame = pd.DataFrame(rows).sort_values(
        ["final_assets", "win_rate_pct", "closed_trade_count"],
        ascending=[False, False, False],
    )
    frame.to_csv(args.output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    print("\n## Ranked Summary")
    print(frame.to_string(index=False, float_format=lambda value: f"{value:.4f}"))
    print(f"\nWrote CSV files to {args.output_dir}")


if __name__ == "__main__":
    main()
