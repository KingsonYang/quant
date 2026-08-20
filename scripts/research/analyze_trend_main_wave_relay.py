from __future__ import annotations

import argparse
import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
from statistics import mean

import duckdb

from quant.config import load_strategy_config
from quant.config.models import PatternSelectionConfig
from quant.db.init_db import DEFAULT_DB_PATH
from quant.pattern_selection import select_pattern_candidates_in_range_with_connection
from quant.strategy_runtime import run_loaded_strategy_backtest


DEFAULT_RESULTS_DIR = Path("results/research/trend_main_wave_relay")
PERIODS = [
    ("long", date(2015, 12, 29), date(2026, 6, 1)),
    ("old", date(2015, 12, 29), date(2023, 12, 29)),
    ("recent_2y", date(2024, 1, 2), date(2026, 6, 1)),
    ("recent_1y", date(2025, 1, 2), date(2026, 6, 1)),
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze trend_main_wave_relay_v1 signals.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--strategies-dir", type=Path, default=Path("strategies"))
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2015, 12, 29))
    parser.add_argument("--end-date", type=date.fromisoformat, default=date(2026, 6, 1))
    return parser.parse_args()


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _candidate_rows(connection: duckdb.DuckDBPyConnection, args: argparse.Namespace) -> list[dict]:
    loaded = load_strategy_config("trend_main_wave_relay", args.strategies_dir)
    if not isinstance(loaded.config.selection, PatternSelectionConfig):
        raise TypeError("trend_main_wave_relay must use pattern_sql selection.")
    grouped = select_pattern_candidates_in_range_with_connection(
        connection,
        args.start_date,
        args.end_date,
        loaded.config.selection,
    )
    candidates = [
        {
            "symbol": candidate.symbol,
            "selection_date": selection_date,
            "composite_score": candidate.composite_score,
        }
        for selection_date, daily in grouped.items()
        for candidate in daily
    ]
    if not candidates:
        return []

    connection.execute(
        "CREATE OR REPLACE TEMP TABLE trend_main_wave_candidates(symbol VARCHAR, selection_date DATE, composite_score DOUBLE)"
    )
    connection.executemany(
        "INSERT INTO trend_main_wave_candidates VALUES (?, ?, ?)",
        [
            (
                row["symbol"],
                row["selection_date"],
                row["composite_score"],
            )
            for row in candidates
        ],
    )
    rows = connection.execute(
        """
        WITH bars AS (
          SELECT
            s.symbol,
            s.name,
            d.trade_date,
            d.open_price,
            d.high_price,
            d.low_price,
            d.close_price,
            d.volume,
            d.amount,
            d.change_pct,
            d.turnover_ratio,
            d.ma5_price,
            d.ma10_price,
            d.ma20_price,
            avg(d.volume) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS prev5_avg_volume,
            sum(d.change_pct) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
            ) AS prev20_change_sum,
            max(d.high_price) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 1 FOLLOWING AND 5 FOLLOWING
            ) AS future_5d_high,
            max(d.high_price) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 1 FOLLOWING AND 10 FOLLOWING
            ) AS future_10d_high,
            max(d.high_price) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 1 FOLLOWING AND 20 FOLLOWING
            ) AS future_20d_high
          FROM daily_bar d
          JOIN security_master s ON s.security_id = d.security_id
        )
        SELECT
          c.symbol,
          b.name,
          c.selection_date,
          c.composite_score,
          b.close_price,
          b.change_pct,
          b.turnover_ratio,
          CASE WHEN b.amount IS NOT NULL THEN b.amount / 100000000.0 ELSE NULL END AS amount_yi,
          CASE WHEN b.prev5_avg_volume > 0 THEN b.volume / b.prev5_avg_volume ELSE NULL END AS volume_ratio_prev5,
          b.prev20_change_sum,
          CASE WHEN b.ma10_price > 0 THEN (b.close_price - b.ma10_price) / b.ma10_price * 100 ELSE NULL END AS distance_from_ma10_pct,
          CASE WHEN b.close_price > 0 AND b.future_5d_high IS NOT NULL THEN (b.future_5d_high - b.close_price) / b.close_price * 100 ELSE NULL END AS future_5d_max_return_pct,
          CASE WHEN b.close_price > 0 AND b.future_10d_high IS NOT NULL THEN (b.future_10d_high - b.close_price) / b.close_price * 100 ELSE NULL END AS future_10d_max_return_pct,
          CASE WHEN b.close_price > 0 AND b.future_20d_high IS NOT NULL THEN (b.future_20d_high - b.close_price) / b.close_price * 100 ELSE NULL END AS future_20d_max_return_pct
        FROM trend_main_wave_candidates c
        JOIN bars b ON b.symbol = c.symbol AND b.trade_date = c.selection_date
        ORDER BY c.selection_date, c.composite_score DESC, c.symbol
        """
    ).fetchall()
    columns = [item[0] for item in connection.description]
    return [dict(zip(columns, row, strict=True)) for row in rows]


def _numeric(row: dict, key: str) -> float | None:
    value = row.get(key)
    if value is None:
        return None
    return float(value)


def _avg(rows: list[dict], key: str) -> str:
    values = [_numeric(row, key) for row in rows]
    clean = [value for value in values if value is not None]
    if not clean:
        return ""
    return f"{mean(clean):.4f}"


def _feature_summary(rows: list[dict]) -> list[dict]:
    summary = []
    for horizon in (5, 10, 20):
        return_key = f"future_{horizon}d_max_return_pct"
        for label, predicate in [
            ("all", lambda value: value is not None),
            (">=20", lambda value: value is not None and value >= 20),
            (">=30", lambda value: value is not None and value >= 30),
            (">=50", lambda value: value is not None and value >= 50),
            ("<5", lambda value: value is not None and value < 5),
        ]:
            bucket = [row for row in rows if predicate(_numeric(row, return_key))]
            summary.append(
                {
                    "horizon": horizon,
                    "bucket": label,
                    "count": len(bucket),
                    "avg_score": _avg(bucket, "composite_score"),
                    "avg_future_max_return_pct": _avg(bucket, return_key),
                    "avg_prev20_change_sum": _avg(bucket, "prev20_change_sum"),
                    "avg_volume_ratio_prev5": _avg(bucket, "volume_ratio_prev5"),
                    "avg_turnover_ratio": _avg(bucket, "turnover_ratio"),
                    "avg_distance_from_ma10_pct": _avg(bucket, "distance_from_ma10_pct"),
                }
            )
    return summary


def _winner_loser_compare(rows: list[dict]) -> list[dict]:
    result = []
    for label, predicate in [
        ("future_20d_ge_20", lambda value: value is not None and value >= 20),
        ("future_20d_ge_30", lambda value: value is not None and value >= 30),
        ("future_20d_ge_50", lambda value: value is not None and value >= 50),
        ("future_20d_lt_5", lambda value: value is not None and value < 5),
    ]:
        bucket = [row for row in rows if predicate(_numeric(row, "future_20d_max_return_pct"))]
        result.append(
            {
                "group": label,
                "count": len(bucket),
                "avg_score": _avg(bucket, "composite_score"),
                "avg_prev20_change_sum": _avg(bucket, "prev20_change_sum"),
                "avg_volume_ratio_prev5": _avg(bucket, "volume_ratio_prev5"),
                "avg_turnover_ratio": _avg(bucket, "turnover_ratio"),
                "avg_distance_from_ma10_pct": _avg(bucket, "distance_from_ma10_pct"),
                "avg_future_20d_max_return_pct": _avg(bucket, "future_20d_max_return_pct"),
            }
        )
    return result


def _fixed_backtests(args: argparse.Namespace) -> list[dict]:
    loaded = load_strategy_config("trend_main_wave_relay", args.strategies_dir)
    rows = []
    for label, start_date, end_date in PERIODS:
        report = run_loaded_strategy_backtest(
            db_path=args.db_path,
            loaded=loaded,
            start_date=start_date,
            end_date=end_date,
        )
        closed = [trade for trade in report.trades if trade.status == "closed"]
        wins = [
            trade
            for trade in closed
            if trade.return_pct is not None and trade.return_pct > Decimal("0")
        ]
        rows.append(
            {
                "period": label,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "final_assets": str(report.summary.current_total_assets),
                "total_return_pct": str(report.summary.total_return_pct),
                "annualized_return_pct": str(report.summary.annualized_return_pct or ""),
                "closed_trades": report.summary.closed_trade_count,
                "win_rate_pct": (
                    f"{(Decimal(len(wins)) * Decimal('100') / Decimal(len(closed))):.4f}"
                    if closed
                    else ""
                ),
            }
        )
    return rows


def _yearly_metrics(args: argparse.Namespace) -> list[dict]:
    loaded = load_strategy_config("trend_main_wave_relay", args.strategies_dir)
    rows = []
    for year in range(args.start_date.year, args.end_date.year + 1):
        natural_start = max(date(year, 1, 1), args.start_date)
        natural_end = min(date(year, 12, 31), args.end_date)
        with duckdb.connect(str(args.db_path), read_only=True) as connection:
            bounds = connection.execute(
                """
                SELECT MIN(trade_date), MAX(trade_date)
                FROM trading_calendar
                WHERE trade_date BETWEEN ? AND ?
                  AND is_trading_day = TRUE
                """,
                [natural_start, natural_end],
            ).fetchone()
        if bounds is None or bounds[0] is None or bounds[1] is None:
            continue
        start = bounds[0]
        end = bounds[1]
        try:
            report = run_loaded_strategy_backtest(
                db_path=args.db_path,
                loaded=loaded,
                start_date=start,
                end_date=end,
            )
        except Exception:
            continue
        closed = [trade for trade in report.trades if trade.status == "closed"]
        wins = [
            trade
            for trade in closed
            if trade.return_pct is not None and trade.return_pct > Decimal("0")
        ]
        rows.append(
            {
                "year": year,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "final_assets": str(report.summary.current_total_assets),
                "total_return_pct": str(report.summary.total_return_pct),
                "closed_trades": report.summary.closed_trade_count,
                "win_rate_pct": (
                    f"{(Decimal(len(wins)) * Decimal('100') / Decimal(len(closed))):.4f}"
                    if closed
                    else ""
                ),
            }
        )
    return rows


def main() -> None:
    args = _parse_args()
    args.results_dir.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(args.db_path)) as connection:
        rows = _candidate_rows(connection, args)
    _write_csv(args.results_dir / "samples.csv", rows[:1000])
    _write_csv(args.results_dir / "feature_summary.csv", _feature_summary(rows))
    _write_csv(args.results_dir / "winner_loser_compare.csv", _winner_loser_compare(rows))
    _write_csv(args.results_dir / "fixed_backtest_summary.csv", _fixed_backtests(args))
    _write_csv(args.results_dir / "yearly_metrics.csv", _yearly_metrics(args))
    recommendation = [
        "# trend_main_wave_relay analysis",
        "",
        f"signals: {len(rows)}",
        "",
        "Use fixed_backtest_summary.csv for default-period robustness, "
        "feature_summary.csv for future 5/10/20 day max-run potential, and "
        "winner_loser_compare.csv to compare strong versus weak follow-through samples.",
    ]
    (args.results_dir / "recommendation.md").write_text("\n".join(recommendation) + "\n", encoding="utf-8")
    print(f"Wrote analysis outputs to {args.results_dir.resolve()}")


if __name__ == "__main__":
    main()
