from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Any

import duckdb
import pandas as pd

from quant.backtest_common import load_all_trade_dates, load_trade_dates_in_range
from quant.config.loader import load_strategy_config
from quant.pattern_selection import select_pattern_candidates_in_range_with_connection


DEFAULT_STRATEGY = "shandian_1hao_erci_baoliang_qidong"
DEFAULT_START = "2022-12-29"
DEFAULT_END = "2026-05-06"
LAUNCH_THRESHOLD_PCT = 5.0


@dataclass(frozen=True)
class Sample:
    symbol: str
    name: str
    selection_date: date
    d0_date: date
    first_launch_day: int | None
    max_high_ret_5d: float | None
    max_close_ret_5d: float | None
    d0_change_pct: float | None
    d0_volume_ratio_prev5: float | None
    signal_change_pct: float | None
    signal_turnover: float | None
    signal_volume_vs_prev1: float | None
    signal_volume_ratio_prev5: float | None
    signal_prev2_change_sum: float | None
    signal_close_vs_d0_low_pct: float | None
    signal_close_vs_d0_close_pct: float | None
    d0_to_signal_trade_days: int
    platform_min_low_vs_d0_low_pct: float | None
    platform_min_close_vs_d0_low_pct: float | None
    platform_avg_volume_vs_d0: float | None
    platform_weak_day_ratio: float | None
    pre_launch_min_low_drawdown_pct: float | None
    pre_launch_avg_volume_vs_signal: float | None
    pre_launch_ma5_hold_ratio: float | None
    launch_open_gap_vs_prev_close_pct: float | None
    launch_change_pct: float | None
    launch_volume_vs_signal: float | None


def _date(value: str) -> date:
    return date.fromisoformat(value)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _pct(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return (numerator - denominator) * 100.0 / denominator


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _avg(values: list[float | None]) -> float | None:
    actual = [value for value in values if value is not None]
    if not actual:
        return None
    return sum(actual) / len(actual)


def _load_bars(
    connection: duckdb.DuckDBPyConnection,
    symbols: set[str],
    start_date: date,
    end_date: date,
) -> dict[tuple[str, date], dict[str, Any]]:
    if not symbols:
        return {}
    symbol_list = sorted(symbols)
    placeholders = ", ".join("?" for _ in symbol_list)
    rows = connection.execute(
        f"""
        SELECT
          s.symbol,
          s.name,
          d.trade_date,
          d.open_price,
          d.high_price,
          d.low_price,
          d.close_price,
          d.volume,
          d.change_pct,
          d.turnover_ratio,
          d.ma5_price,
          avg(d.volume) OVER (
            PARTITION BY d.security_id
            ORDER BY d.trade_date
            ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
          ) AS prev5_avg_volume,
          lag(d.volume, 1) OVER (PARTITION BY d.security_id ORDER BY d.trade_date) AS prev1_volume,
          lag(d.change_pct, 1) OVER (PARTITION BY d.security_id ORDER BY d.trade_date) AS prev1_change_pct,
          lag(d.change_pct, 2) OVER (PARTITION BY d.security_id ORDER BY d.trade_date) AS prev2_change_pct
        FROM daily_bar d
        JOIN security_master s ON s.security_id = d.security_id
        WHERE s.symbol IN ({placeholders})
          AND d.trade_date BETWEEN ? AND ?
        ORDER BY s.symbol, d.trade_date
        """,
        [*symbol_list, start_date, end_date],
    ).fetchall()
    return {
        (row[0], row[2]): {
            "symbol": row[0],
            "name": row[1],
            "trade_date": row[2],
            "open": _to_float(row[3]),
            "high": _to_float(row[4]),
            "low": _to_float(row[5]),
            "close": _to_float(row[6]),
            "volume": _to_float(row[7]),
            "change_pct": _to_float(row[8]),
            "turnover_ratio": _to_float(row[9]),
            "ma5": _to_float(row[10]),
            "prev5_avg_volume": _to_float(row[11]),
            "prev1_volume": _to_float(row[12]),
            "prev1_change_pct": _to_float(row[13]),
            "prev2_change_pct": _to_float(row[14]),
        }
        for row in rows
    }


def _build_samples(
    *,
    connection: duckdb.DuckDBPyConnection,
    strategy_name: str,
    strategies_dir: Path,
    start_date: date,
    end_date: date,
    top_n: int | None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    loaded = load_strategy_config(strategy_name, strategies_dir)
    default_top_n = loaded.config.backtest_selection.selection_top_n if loaded.config.backtest_selection else None
    effective_top_n = top_n if top_n is not None else default_top_n

    all_trade_dates = load_all_trade_dates(connection)
    trade_index = {trade_day: index for index, trade_day in enumerate(all_trade_dates)}
    trade_dates = load_trade_dates_in_range(connection, start_date, end_date)
    grouped = select_pattern_candidates_in_range_with_connection(
        connection,
        start_date,
        end_date,
        loaded.config.selection,
    )
    selected = []
    for trade_day in trade_dates:
        candidates = grouped.get(trade_day, [])
        if effective_top_n is not None:
            candidates = candidates[:effective_top_n]
        selected.extend(candidates)

    symbols = {candidate.symbol for candidate in selected}
    earliest_d0 = min((candidate.d0_date for candidate in selected if candidate.d0_date), default=start_date)
    max_index = min(trade_index[end_date] + 5, len(all_trade_dates) - 1)
    bars = _load_bars(connection, symbols, earliest_d0, all_trade_dates[max_index])

    samples: list[Sample] = []
    day_rows: list[dict[str, Any]] = []
    for candidate in selected:
        if candidate.d0_date is None:
            continue
        signal = bars.get((candidate.symbol, candidate.selection_date))
        d0 = bars.get((candidate.symbol, candidate.d0_date))
        if signal is None or d0 is None or signal["close"] in (None, 0):
            continue

        signal_index = trade_index[candidate.selection_date]
        d0_index = trade_index[candidate.d0_date]
        future_dates = all_trade_dates[signal_index + 1: signal_index + 6]
        future_bars = [bars.get((candidate.symbol, future_date)) for future_date in future_dates]
        first_launch_day = None
        running_max_high = None
        running_max_close = None
        for day_no, bar in enumerate(future_bars, start=1):
            if bar is None:
                continue
            high_ret = _pct(bar["high"], signal["close"])
            close_ret = _pct(bar["close"], signal["close"])
            if high_ret is not None:
                running_max_high = high_ret if running_max_high is None else max(running_max_high, high_ret)
                if first_launch_day is None and high_ret >= LAUNCH_THRESHOLD_PCT:
                    first_launch_day = day_no
            if close_ret is not None:
                running_max_close = close_ret if running_max_close is None else max(running_max_close, close_ret)
            day_rows.append(
                {
                    "symbol": candidate.symbol,
                    "name": candidate.name,
                    "selection_date": candidate.selection_date,
                    "d0_date": candidate.d0_date,
                    "day_no": day_no,
                    "trade_date": bar["trade_date"],
                    "exact_high_ret_pct": high_ret,
                    "exact_close_ret_pct": close_ret,
                    "cum_high_ret_pct": running_max_high,
                    "cum_close_ret_pct": running_max_close,
                    "is_exact_close_positive": close_ret is not None and close_ret > 0,
                    "is_exact_high_ge_3": high_ret is not None and high_ret >= 3.0,
                    "is_exact_high_ge_5": high_ret is not None and high_ret >= 5.0,
                    "is_exact_high_ge_7_5": high_ret is not None and high_ret >= 7.5,
                    "is_exact_high_ge_10": high_ret is not None and high_ret >= 10.0,
                    "is_cum_high_ge_5": running_max_high is not None and running_max_high >= 5.0,
                }
            )

        platform_bars = [
            bars.get((candidate.symbol, trade_day))
            for trade_day in all_trade_dates[d0_index + 1: signal_index]
        ]
        platform_bars = [bar for bar in platform_bars if bar is not None]
        platform_lows = [bar["low"] for bar in platform_bars if bar["low"] is not None]
        platform_closes = [bar["close"] for bar in platform_bars if bar["close"] is not None]
        platform_volumes = [bar["volume"] for bar in platform_bars if bar["volume"] is not None]
        weak_days = [
            bar for bar in platform_bars
            if (
                bar["close"] is not None
                and bar["open"] is not None
                and bar["change_pct"] is not None
                and (bar["close"] <= bar["open"] or bar["change_pct"] <= 1.5)
            )
        ]

        pre_launch_bars = []
        launch_bar = None
        if first_launch_day is not None:
            launch_bar = future_bars[first_launch_day - 1]
            pre_launch_bars = [bar for bar in future_bars[: first_launch_day - 1] if bar is not None]
        pre_launch_lows = [bar["low"] for bar in pre_launch_bars if bar["low"] is not None]
        pre_launch_volumes = [bar["volume"] for bar in pre_launch_bars if bar["volume"] is not None]
        pre_launch_ma5_holds = [
            1.0 if bar["close"] is not None and bar["ma5"] is not None and bar["close"] >= bar["ma5"] else 0.0
            for bar in pre_launch_bars
            if bar["close"] is not None and bar["ma5"] is not None
        ]
        prev_close = None
        if first_launch_day is not None and launch_bar is not None:
            prev_date = all_trade_dates[signal_index + first_launch_day - 1]
            prev_bar = bars.get((candidate.symbol, prev_date))
            prev_close = prev_bar["close"] if prev_bar is not None else None

        samples.append(
            Sample(
                symbol=candidate.symbol,
                name=candidate.name,
                selection_date=candidate.selection_date,
                d0_date=candidate.d0_date,
                first_launch_day=first_launch_day,
                max_high_ret_5d=running_max_high,
                max_close_ret_5d=running_max_close,
                d0_change_pct=d0["change_pct"],
                d0_volume_ratio_prev5=_safe_ratio(d0["volume"], d0["prev5_avg_volume"]),
                signal_change_pct=signal["change_pct"],
                signal_turnover=signal["turnover_ratio"],
                signal_volume_vs_prev1=_safe_ratio(signal["volume"], signal["prev1_volume"]),
                signal_volume_ratio_prev5=_safe_ratio(signal["volume"], signal["prev5_avg_volume"]),
                signal_prev2_change_sum=(
                    signal["prev1_change_pct"] + signal["prev2_change_pct"]
                    if signal["prev1_change_pct"] is not None and signal["prev2_change_pct"] is not None
                    else None
                ),
                signal_close_vs_d0_low_pct=_pct(signal["close"], d0["low"]),
                signal_close_vs_d0_close_pct=_pct(signal["close"], d0["close"]),
                d0_to_signal_trade_days=signal_index - d0_index,
                platform_min_low_vs_d0_low_pct=(
                    _pct(min(platform_lows), d0["low"]) if platform_lows else None
                ),
                platform_min_close_vs_d0_low_pct=(
                    _pct(min(platform_closes), d0["low"]) if platform_closes else None
                ),
                platform_avg_volume_vs_d0=(
                    mean(platform_volumes) / d0["volume"]
                    if platform_volumes and d0["volume"]
                    else None
                ),
                platform_weak_day_ratio=(
                    len(weak_days) / len(platform_bars) if platform_bars else None
                ),
                pre_launch_min_low_drawdown_pct=(
                    _pct(min(pre_launch_lows), signal["close"]) if pre_launch_lows else None
                ),
                pre_launch_avg_volume_vs_signal=(
                    mean(pre_launch_volumes) / signal["volume"]
                    if pre_launch_volumes and signal["volume"]
                    else None
                ),
                pre_launch_ma5_hold_ratio=_avg(pre_launch_ma5_holds),
                launch_open_gap_vs_prev_close_pct=(
                    _pct(launch_bar["open"], prev_close)
                    if launch_bar is not None and prev_close
                    else None
                ),
                launch_change_pct=launch_bar["change_pct"] if launch_bar is not None else None,
                launch_volume_vs_signal=(
                    launch_bar["volume"] / signal["volume"]
                    if launch_bar is not None and launch_bar["volume"] is not None and signal["volume"]
                    else None
                ),
            )
        )

    return pd.DataFrame(asdict(sample) for sample in samples), pd.DataFrame(day_rows)


def _summarize_days(day_df: pd.DataFrame) -> pd.DataFrame:
    if day_df.empty:
        return pd.DataFrame()
    return (
        day_df.groupby("day_no")
        .agg(
            samples=("symbol", "count"),
            exact_close_pos_rate=("is_exact_close_positive", "mean"),
            exact_close_avg_ret=("exact_close_ret_pct", "mean"),
            exact_high_avg_ret=("exact_high_ret_pct", "mean"),
            exact_high_ge_3_rate=("is_exact_high_ge_3", "mean"),
            exact_high_ge_5_rate=("is_exact_high_ge_5", "mean"),
            exact_high_ge_7_5_rate=("is_exact_high_ge_7_5", "mean"),
            exact_high_ge_10_rate=("is_exact_high_ge_10", "mean"),
            cum_high_ge_5_rate=("is_cum_high_ge_5", "mean"),
            cum_high_avg_ret=("cum_high_ret_pct", "mean"),
        )
        .reset_index()
    )


def _feature_compare(sample_df: pd.DataFrame) -> pd.DataFrame:
    if sample_df.empty:
        return pd.DataFrame()
    sample_df = sample_df.copy()
    sample_df["launched_5d"] = sample_df["first_launch_day"].notna()
    features = [
        "d0_change_pct",
        "d0_volume_ratio_prev5",
        "signal_change_pct",
        "signal_turnover",
        "signal_volume_vs_prev1",
        "signal_volume_ratio_prev5",
        "signal_prev2_change_sum",
        "signal_close_vs_d0_low_pct",
        "signal_close_vs_d0_close_pct",
        "d0_to_signal_trade_days",
        "platform_min_low_vs_d0_low_pct",
        "platform_min_close_vs_d0_low_pct",
        "platform_avg_volume_vs_d0",
        "platform_weak_day_ratio",
        "pre_launch_min_low_drawdown_pct",
        "pre_launch_avg_volume_vs_signal",
        "pre_launch_ma5_hold_ratio",
        "launch_open_gap_vs_prev_close_pct",
        "launch_change_pct",
        "launch_volume_vs_signal",
    ]
    rows = []
    for feature in features:
        launched = sample_df.loc[sample_df["launched_5d"], feature].dropna()
        others = sample_df.loc[~sample_df["launched_5d"], feature].dropna()
        rows.append(
            {
                "feature": feature,
                "launched_count": int(launched.count()),
                "launched_median": float(launched.median()) if not launched.empty else None,
                "launched_mean": float(launched.mean()) if not launched.empty else None,
                "not_launched_count": int(others.count()),
                "not_launched_median": float(others.median()) if not others.empty else None,
                "not_launched_mean": float(others.mean()) if not others.empty else None,
            }
        )
    return pd.DataFrame(rows)


def _quantile_bins(sample_df: pd.DataFrame) -> pd.DataFrame:
    if sample_df.empty:
        return pd.DataFrame()
    sample_df = sample_df.copy()
    sample_df["launched_5d"] = sample_df["first_launch_day"].notna()
    features = [
        "signal_change_pct",
        "signal_turnover",
        "signal_volume_vs_prev1",
        "signal_volume_ratio_prev5",
        "signal_prev2_change_sum",
        "d0_to_signal_trade_days",
        "platform_avg_volume_vs_d0",
        "platform_weak_day_ratio",
    ]
    rows = []
    for feature in features:
        series = sample_df[feature].dropna()
        if series.nunique() < 3:
            continue
        try:
            bins = pd.qcut(sample_df[feature], q=4, duplicates="drop")
        except ValueError:
            continue
        grouped = sample_df.assign(bin=bins).dropna(subset=["bin"]).groupby("bin", observed=True)
        for bin_value, group in grouped:
            rows.append(
                {
                    "feature": feature,
                    "bin": str(bin_value),
                    "samples": int(len(group)),
                    "launch_rate_5d": float(group["launched_5d"].mean()),
                    "avg_max_high_5d": float(group["max_high_ret_5d"].mean()),
                    "median_max_high_5d": float(group["max_high_ret_5d"].median()),
                }
            )
    return pd.DataFrame(rows)


def _write(path: Path, df: pd.DataFrame) -> None:
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _print_table(title: str, df: pd.DataFrame, rows: int = 80) -> None:
    print(f"\n## {title}")
    if df.empty:
        print("(empty)")
        return
    print(df.head(rows).to_string(index=False, float_format=lambda value: f"{value:.4f}"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", type=Path, default=Path("data/quant.duckdb"))
    parser.add_argument("--strategies-dir", type=Path, default=Path("strategies"))
    parser.add_argument("--strategy", default=DEFAULT_STRATEGY)
    parser.add_argument("--start-date", type=_date, default=_date(DEFAULT_START))
    parser.add_argument("--end-date", type=_date, default=_date(DEFAULT_END))
    parser.add_argument("--top-n", type=int, default=None)
    parser.add_argument("--output-dir", type=Path, default=Path("results/research/shandian_1hao_erci_baoliang_qidong"))
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(args.db_path), read_only=True) as connection:
        sample_df, day_df = _build_samples(
            connection=connection,
            strategy_name=args.strategy,
            strategies_dir=args.strategies_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            top_n=args.top_n,
        )

    day_summary = _summarize_days(day_df)
    feature_summary = _feature_compare(sample_df)
    bin_summary = _quantile_bins(sample_df)
    launch_distribution = (
        sample_df.assign(first_launch_day=sample_df["first_launch_day"].fillna(0).astype(int))
        .groupby("first_launch_day")
        .agg(samples=("symbol", "count"), avg_max_high_5d=("max_high_ret_5d", "mean"))
        .reset_index()
        .replace({"first_launch_day": {0: "no_launch"}})
    )

    _write(args.output_dir / "samples.csv", sample_df)
    _write(args.output_dir / "day_returns.csv", day_df)
    _write(args.output_dir / "day_summary.csv", day_summary)
    _write(args.output_dir / "feature_compare.csv", feature_summary)
    _write(args.output_dir / "feature_bins.csv", bin_summary)
    _write(args.output_dir / "launch_distribution.csv", launch_distribution)

    launched = sample_df["first_launch_day"].notna() if not sample_df.empty else pd.Series(dtype=bool)
    print(f"strategy={args.strategy}")
    print(f"window={args.start_date}..{args.end_date}")
    print(f"samples={len(sample_df)}")
    if len(sample_df):
        print(f"launch_threshold={LAUNCH_THRESHOLD_PCT:.1f}% high vs signal close")
        print(f"launch_rate_5d={launched.mean() * 100:.2f}%")
        print(f"avg_max_high_5d={sample_df['max_high_ret_5d'].mean():.4f}%")
        print(f"median_max_high_5d={sample_df['max_high_ret_5d'].median():.4f}%")
        print(f"avg_max_close_5d={sample_df['max_close_ret_5d'].mean():.4f}%")
        print(f"median_max_close_5d={sample_df['max_close_ret_5d'].median():.4f}%")
    _print_table("Day Summary", day_summary)
    _print_table("Launch Distribution", launch_distribution)
    _print_table("Feature Compare", feature_summary)
    _print_table("Feature Bins", bin_summary, rows=200)
    print(f"\nWrote CSV files to {args.output_dir}")


if __name__ == "__main__":
    main()
