from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd


DEFAULT_START = "2022-12-29"
DEFAULT_END = "2026-05-06"
LIMIT_UP_MIN_PCT = 9.5
LIMIT_UP_CLOSE_HIGH_RATIO = 0.995


def _date(value: str) -> str:
    return value


def _write(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _print_table(title: str, frame: pd.DataFrame, *, rows: int = 80) -> None:
    print(f"\n## {title}")
    if frame.empty:
        print("(empty)")
        return
    print(frame.head(rows).to_string(index=False, float_format=lambda value: f"{value:.4f}"))


def _load_first_limit_samples(
    connection: duckdb.DuckDBPyConnection,
    *,
    start_date: str,
    end_date: str,
    limit_up_min_pct: float,
    close_high_ratio: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    samples = connection.execute(
        """
        WITH bars AS (
          SELECT
            s.symbol,
            s.name,
            d.security_id,
            d.trade_date,
            d.open_price,
            d.high_price,
            d.low_price,
            d.close_price,
            d.pre_close_price,
            d.volume,
            d.amount,
            d.change_pct,
            d.turnover_ratio,
            d.ma5_price,
            d.ma10_price,
            d.ma20_price,
            lag(d.close_price, 1) OVER w AS prev1_close,
            lag(d.change_pct, 1) OVER w AS prev1_change_pct,
            lag(d.change_pct, 2) OVER w AS prev2_change_pct,
            avg(d.volume) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS prev5_avg_volume,
            avg(d.volume) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 10 PRECEDING AND 1 PRECEDING
            ) AS prev10_avg_volume,
            avg(d.volume) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
            ) AS prev20_avg_volume,
            sum(d.change_pct) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS prev5_change_sum,
            sum(d.change_pct) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 10 PRECEDING AND 1 PRECEDING
            ) AS prev10_change_sum,
            max(d.high_price) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING
            ) AS prev20_high,
            avg(d.turnover_ratio) OVER (
              PARTITION BY d.security_id
              ORDER BY d.trade_date
              ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS prev5_avg_turnover
          FROM daily_bar d
          JOIN security_master s ON s.security_id = d.security_id
          WINDOW w AS (PARTITION BY d.security_id ORDER BY d.trade_date)
        ),
        marked AS (
          SELECT
            *,
            CASE
              WHEN change_pct >= ?
               AND close_price >= high_price * ?
               AND close_price > open_price
              THEN 1 ELSE 0
            END AS is_limit_up
          FROM bars
        ),
        first_boards AS (
          SELECT
            *,
            lag(is_limit_up, 1) OVER (PARTITION BY security_id ORDER BY trade_date) AS prev_is_limit_up
          FROM marked
        ),
        picked AS (
          SELECT *
          FROM first_boards
          WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
            AND is_limit_up = 1
            AND COALESCE(prev_is_limit_up, 0) = 0
            AND prev5_avg_volume > 0
            AND prev10_avg_volume > 0
            AND prev20_avg_volume > 0
        )
        SELECT
          symbol,
          name,
          trade_date AS first_limit_date,
          change_pct AS first_limit_change_pct,
          turnover_ratio AS first_limit_turnover,
          amount / 100000000.0 AS first_limit_amount_yi,
          volume / prev5_avg_volume AS volume_ratio_prev5,
          volume / prev10_avg_volume AS volume_ratio_prev10,
          volume / prev20_avg_volume AS volume_ratio_prev20,
          CASE WHEN pre_close_price > 0 THEN (open_price - pre_close_price) / pre_close_price * 100 END AS open_gap_pct,
          CASE WHEN pre_close_price > 0 THEN (high_price - low_price) / pre_close_price * 100 END AS intraday_amp_pct,
          CASE WHEN high_price > 0 THEN close_price / high_price END AS close_high_ratio,
          CASE WHEN ma5_price > 0 THEN (close_price - ma5_price) / ma5_price * 100 END AS close_vs_ma5_pct,
          CASE WHEN ma10_price > 0 THEN (close_price - ma10_price) / ma10_price * 100 END AS close_vs_ma10_pct,
          CASE WHEN ma20_price > 0 THEN (close_price - ma20_price) / ma20_price * 100 END AS close_vs_ma20_pct,
          CASE WHEN ma10_price > 0 THEN (ma5_price - ma10_price) / ma10_price * 100 END AS ma5_vs_ma10_pct,
          CASE WHEN ma20_price > 0 THEN (ma10_price - ma20_price) / ma20_price * 100 END AS ma10_vs_ma20_pct,
          CASE WHEN ma5_price > ma10_price AND ma10_price > ma20_price THEN 1 ELSE 0 END AS ma_bull_stack,
          prev1_change_pct,
          prev2_change_pct,
          prev5_change_sum,
          prev10_change_sum,
          CASE WHEN prev20_high > 0 THEN (close_price - prev20_high) / prev20_high * 100 END AS close_vs_prev20_high_pct,
          CASE WHEN close_price >= prev20_high THEN 1 ELSE 0 END AS break_prev20_high,
          CASE WHEN prev5_avg_turnover > 0 THEN turnover_ratio / prev5_avg_turnover END AS turnover_ratio_prev5
        FROM picked
        ORDER BY first_limit_date, symbol
        """,
        [limit_up_min_pct, close_high_ratio, start_date, end_date],
    ).df()

    if samples.empty:
        return samples, pd.DataFrame()

    symbols = sorted(samples["symbol"].unique().tolist())
    placeholders = ", ".join("?" for _ in symbols)
    min_date = samples["first_limit_date"].min()
    max_date = samples["first_limit_date"].max()
    future_rows = connection.execute(
        f"""
        WITH all_dates AS (
          SELECT trade_date, row_number() OVER (ORDER BY trade_date) - 1 AS trade_index
          FROM trading_calendar
          WHERE market = 'ab' AND is_trading_day
        ),
        bars AS (
          SELECT
            s.symbol,
            s.name,
            d.trade_date,
            ad.trade_index,
            d.open_price,
            d.high_price,
            d.low_price,
            d.close_price,
            d.volume,
            d.change_pct,
            d.turnover_ratio
          FROM daily_bar d
          JOIN security_master s ON s.security_id = d.security_id
          JOIN all_dates ad ON ad.trade_date = d.trade_date
          WHERE s.symbol IN ({placeholders})
        ),
        anchors AS (
          SELECT
            b.symbol,
            b.trade_date AS first_limit_date,
            b.close_price AS first_limit_close,
            b.trade_index AS first_limit_index
          FROM bars b
          WHERE b.trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
        )
        SELECT
          a.symbol,
          a.first_limit_date,
          fb.trade_date,
          fb.trade_index - a.first_limit_index AS day_no,
          CASE WHEN a.first_limit_close > 0 THEN (fb.high_price - a.first_limit_close) / a.first_limit_close * 100 END AS exact_high_ret_pct,
          CASE WHEN a.first_limit_close > 0 THEN (fb.close_price - a.first_limit_close) / a.first_limit_close * 100 END AS exact_close_ret_pct,
          fb.change_pct,
          fb.turnover_ratio,
          CASE WHEN a.first_limit_close > 0 THEN (fb.low_price - a.first_limit_close) / a.first_limit_close * 100 END AS exact_low_ret_pct
        FROM anchors a
        JOIN bars fb
          ON fb.symbol = a.symbol
         AND fb.trade_index > a.first_limit_index
         AND fb.trade_index <= a.first_limit_index + 5
        ORDER BY a.first_limit_date, a.symbol, day_no
        """,
        [*symbols, min_date, max_date],
    ).df()

    sample_keys = samples[["symbol", "first_limit_date"]].drop_duplicates()
    future_rows = future_rows.merge(sample_keys, on=["symbol", "first_limit_date"], how="inner")
    future_rows["cum_high_ret_pct"] = future_rows.groupby(["symbol", "first_limit_date"])[
        "exact_high_ret_pct"
    ].cummax()
    future_rows["cum_close_ret_pct"] = future_rows.groupby(["symbol", "first_limit_date"])[
        "exact_close_ret_pct"
    ].cummax()

    max_future = (
        future_rows.groupby(["symbol", "first_limit_date"])
        .agg(
            future_days_available=("day_no", "count"),
            max_high_ret_5d=("exact_high_ret_pct", "max"),
            max_close_ret_5d=("exact_close_ret_pct", "max"),
            min_low_ret_5d=("exact_low_ret_pct", "min"),
            day1_high_ret=("exact_high_ret_pct", lambda series: _nth(series, 1)),
            day2_high_ret=("exact_high_ret_pct", lambda series: _nth(series, 2)),
            day3_high_ret=("exact_high_ret_pct", lambda series: _nth(series, 3)),
            day4_high_ret=("exact_high_ret_pct", lambda series: _nth(series, 4)),
            day5_high_ret=("exact_high_ret_pct", lambda series: _nth(series, 5)),
        )
        .reset_index()
    )
    samples = samples.merge(max_future, on=["symbol", "first_limit_date"], how="left")
    samples["launched_5d"] = samples["max_high_ret_5d"] >= 5.0
    samples["strong_5d"] = samples["max_high_ret_5d"] >= 10.0
    samples["failed_5d"] = samples["max_high_ret_5d"] < 3.0
    return samples, future_rows


def _nth(series: pd.Series, day_no: int) -> float | None:
    if len(series) < day_no:
        return None
    value = series.iloc[day_no - 1]
    return None if pd.isna(value) else float(value)


def _summarize_days(future_df: pd.DataFrame) -> pd.DataFrame:
    if future_df.empty:
        return pd.DataFrame()
    frame = future_df.copy()
    frame["exact_close_positive"] = frame["exact_close_ret_pct"] > 0
    frame["exact_high_ge_3"] = frame["exact_high_ret_pct"] >= 3.0
    frame["exact_high_ge_5"] = frame["exact_high_ret_pct"] >= 5.0
    frame["exact_high_ge_10"] = frame["exact_high_ret_pct"] >= 10.0
    frame["cum_high_ge_5"] = frame["cum_high_ret_pct"] >= 5.0
    frame["cum_high_ge_10"] = frame["cum_high_ret_pct"] >= 10.0
    return (
        frame.groupby("day_no")
        .agg(
            samples=("symbol", "count"),
            exact_close_pos_rate=("exact_close_positive", "mean"),
            exact_high_avg_ret=("exact_high_ret_pct", "mean"),
            exact_close_avg_ret=("exact_close_ret_pct", "mean"),
            exact_high_ge_3_rate=("exact_high_ge_3", "mean"),
            exact_high_ge_5_rate=("exact_high_ge_5", "mean"),
            exact_high_ge_10_rate=("exact_high_ge_10", "mean"),
            cum_high_ge_5_rate=("cum_high_ge_5", "mean"),
            cum_high_ge_10_rate=("cum_high_ge_10", "mean"),
            cum_high_avg_ret=("cum_high_ret_pct", "mean"),
        )
        .reset_index()
    )


def _feature_compare(samples: pd.DataFrame) -> pd.DataFrame:
    if samples.empty:
        return pd.DataFrame()
    features = _feature_columns(samples)
    rows: list[dict[str, Any]] = []
    for feature in features:
        strong = samples.loc[samples["strong_5d"], feature].dropna()
        failed = samples.loc[samples["failed_5d"], feature].dropna()
        launched = samples.loc[samples["launched_5d"], feature].dropna()
        not_launched = samples.loc[~samples["launched_5d"], feature].dropna()
        rows.append(
            {
                "feature": feature,
                "strong_count": int(strong.count()),
                "strong_median": float(strong.median()) if not strong.empty else None,
                "failed_count": int(failed.count()),
                "failed_median": float(failed.median()) if not failed.empty else None,
                "launched_median": float(launched.median()) if not launched.empty else None,
                "not_launched_median": float(not_launched.median()) if not not_launched.empty else None,
            }
        )
    return pd.DataFrame(rows)


def _feature_bins(samples: pd.DataFrame) -> pd.DataFrame:
    if samples.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for feature in _feature_columns(samples):
        series = samples[feature].dropna()
        if series.nunique() < 3:
            continue
        try:
            bins = pd.qcut(samples[feature], q=4, duplicates="drop")
        except ValueError:
            continue
        grouped = samples.assign(bin=bins).dropna(subset=["bin"]).groupby("bin", observed=True)
        for bin_value, group in grouped:
            rows.append(
                {
                    "feature": feature,
                    "bin": str(bin_value),
                    "samples": int(len(group)),
                    "launch_rate_5d": float(group["launched_5d"].mean()),
                    "strong_rate_5d": float(group["strong_5d"].mean()),
                    "failed_rate_5d": float(group["failed_5d"].mean()),
                    "avg_max_high_5d": float(group["max_high_ret_5d"].mean()),
                    "median_max_high_5d": float(group["max_high_ret_5d"].median()),
                }
            )
    return pd.DataFrame(rows)


def _feature_columns(samples: pd.DataFrame) -> list[str]:
    candidates = [
        "first_limit_change_pct",
        "first_limit_turnover",
        "first_limit_amount_yi",
        "volume_ratio_prev5",
        "volume_ratio_prev10",
        "volume_ratio_prev20",
        "open_gap_pct",
        "intraday_amp_pct",
        "close_vs_ma5_pct",
        "close_vs_ma10_pct",
        "close_vs_ma20_pct",
        "ma5_vs_ma10_pct",
        "ma10_vs_ma20_pct",
        "ma_bull_stack",
        "prev1_change_pct",
        "prev2_change_pct",
        "prev5_change_sum",
        "prev10_change_sum",
        "close_vs_prev20_high_pct",
        "break_prev20_high",
        "turnover_ratio_prev5",
    ]
    return [feature for feature in candidates if feature in samples.columns]


def _top_bins(bin_df: pd.DataFrame, *, min_samples: int) -> pd.DataFrame:
    if bin_df.empty:
        return pd.DataFrame()
    return (
        bin_df.loc[bin_df["samples"] >= min_samples]
        .sort_values(["strong_rate_5d", "avg_max_high_5d"], ascending=[False, False])
        .head(30)
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", type=Path, default=Path("data/quant.duckdb"))
    parser.add_argument("--start-date", type=_date, default=DEFAULT_START)
    parser.add_argument("--end-date", type=_date, default=DEFAULT_END)
    parser.add_argument("--limit-up-min-pct", type=float, default=LIMIT_UP_MIN_PCT)
    parser.add_argument("--close-high-ratio", type=float, default=LIMIT_UP_CLOSE_HIGH_RATIO)
    parser.add_argument("--min-bin-samples", type=int, default=80)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/research/first_limit_up_causes"),
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(args.db_path), read_only=True) as connection:
        samples, future_df = _load_first_limit_samples(
            connection,
            start_date=args.start_date,
            end_date=args.end_date,
            limit_up_min_pct=args.limit_up_min_pct,
            close_high_ratio=args.close_high_ratio,
        )

    day_summary = _summarize_days(future_df)
    feature_compare = _feature_compare(samples)
    feature_bins = _feature_bins(samples)
    top_bins = _top_bins(feature_bins, min_samples=args.min_bin_samples)

    _write(args.output_dir / "samples.csv", samples)
    _write(args.output_dir / "future_returns.csv", future_df)
    _write(args.output_dir / "day_summary.csv", day_summary)
    _write(args.output_dir / "feature_compare.csv", feature_compare)
    _write(args.output_dir / "feature_bins.csv", feature_bins)
    _write(args.output_dir / "top_bins.csv", top_bins)

    print("study=first_limit_up_causes")
    print(f"window={args.start_date}..{args.end_date}")
    print(f"samples={len(samples)}")
    if not samples.empty:
        print(f"launch_rate_5d={(samples['launched_5d'].mean() * 100):.2f}%")
        print(f"strong_rate_5d={(samples['strong_5d'].mean() * 100):.2f}%")
        print(f"failed_rate_5d={(samples['failed_5d'].mean() * 100):.2f}%")
        print(f"avg_max_high_5d={samples['max_high_ret_5d'].mean():.4f}%")
        print(f"median_max_high_5d={samples['max_high_ret_5d'].median():.4f}%")
    _print_table("Day Summary", day_summary)
    _print_table("Feature Compare", feature_compare)
    _print_table("Top Bins", top_bins, rows=30)
    print(f"\nWrote CSV files to {args.output_dir}")


if __name__ == "__main__":
    main()
