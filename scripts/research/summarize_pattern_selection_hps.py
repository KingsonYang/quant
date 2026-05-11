#!/usr/bin/env python3
"""
Summarize pattern-selection backtest CSVs produced by backtest_pattern_selection_hps.py.

Default input glob: results/pattern_selection_hp_*.csv

For each holding period (hp), aggregates completed trades' return_pct and prints:
- counts (rows, completed, incomplete, incomplete reasons breakdown)
- min / max / mean / median / stdev
- percentiles p10 / p25 / p75 / p90
- win rate (return > 0), flat rate (== 0)
- bucket counts (including 0-10% as requested)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable


FILENAME_HP_RE = re.compile(r"pattern_selection_hp_(\d+)\.csv$", re.IGNORECASE)


def _ensure_utf8_console() -> None:
    """Best-effort: make Chinese headers render correctly on Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except Exception:
                pass


@dataclass(frozen=True)
class HpSummary:
    hp: int
    file: str
    rows_total: int
    completed: int
    incomplete: int
    incomplete_reasons: dict[str, int]
    invalid_completed_rows: int

    ret_min: Decimal | None
    ret_max: Decimal | None
    ret_mean: Decimal | None
    ret_median: Decimal | None
    ret_stdev: Decimal | None
    ret_p10: Decimal | None
    ret_p25: Decimal | None
    ret_p75: Decimal | None
    ret_p90: Decimal | None

    win_rate_pct: Decimal | None
    flat_rate_pct: Decimal | None

    bucket_lt_m10: int
    bucket_m10_m5: int
    bucket_m5_0: int
    bucket_0_5: int
    bucket_5_10: int
    bucket_10_15: int
    bucket_15_20: int
    bucket_gt_20: int

    count_ge_10pct: int
    count_le_m10pct: int


def _parse_decimal(value: str) -> Decimal | None:
    value = value.strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except Exception:
        return None


def _quantile_nearest_rank(sorted_values: list[Decimal], q: float) -> Decimal | None:
    """Nearest-rank quantile, q in (0,1)."""
    if not sorted_values:
        return None
    if q <= 0:
        return sorted_values[0]
    if q >= 1:
        return sorted_values[-1]
    pos = int(math.ceil(q * len(sorted_values))) - 1
    pos = max(0, min(pos, len(sorted_values) - 1))
    return sorted_values[pos]


def _stdev(values: list[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    floats = [float(v) for v in values]
    return Decimal(str(statistics.pstdev(floats)))


def _mean(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, start=Decimal("0")) / Decimal(len(values))


def _rate_pct(numer: int, denom: int) -> Decimal | None:
    if denom <= 0:
        return None
    return (Decimal(numer) * Decimal("100") / Decimal(denom)).quantize(Decimal("0.0001"))


def summarize_one_csv(path: Path) -> HpSummary:
    m = FILENAME_HP_RE.search(path.name)
    if not m:
        raise ValueError(f"Unrecognized filename (expected pattern_selection_hp_<N>.csv): {path}")
    hp = int(m.group(1))

    rows_total = 0
    completed = 0
    incomplete = 0
    reasons: Counter[str] = Counter()
    invalid_completed_rows = 0

    returns: list[Decimal] = []

    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {
            "symbol",
            "name",
            "selection_date",
            "d0_date",
            "buy_date",
            "sell_date",
            "buy_open",
            "sell_close",
            "return_pct",
            "status",
            "incomplete_reason",
        }
        if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
            missing = sorted(required - set(reader.fieldnames or []))
            raise ValueError(f"{path}: missing columns: {missing}")

        for row in reader:
            rows_total += 1
            status = (row.get("status") or "").strip()
            if status == "completed":
                completed += 1
                rp = _parse_decimal(row.get("return_pct") or "")
                if rp is None:
                    invalid_completed_rows += 1
                    continue
                returns.append(rp)
            elif status == "incomplete":
                incomplete += 1
                rsn = (row.get("incomplete_reason") or "").strip() or "unknown"
                reasons[rsn] += 1
            else:
                # unexpected status; count as incomplete-ish
                incomplete += 1
                reasons[f"unexpected_status:{status or 'empty'}"] += 1

    sorted_returns = sorted(returns)

    ret_min = sorted_returns[0] if sorted_returns else None
    ret_max = sorted_returns[-1] if sorted_returns else None
    ret_mean = _mean(returns)
    ret_median = statistics.median(returns) if returns else None
    if ret_median is not None and not isinstance(ret_median, Decimal):
        ret_median = Decimal(str(ret_median))
    ret_stdev = _stdev(returns)
    ret_p10 = _quantile_nearest_rank(sorted_returns, 0.10)
    ret_p25 = _quantile_nearest_rank(sorted_returns, 0.25)
    ret_p75 = _quantile_nearest_rank(sorted_returns, 0.75)
    ret_p90 = _quantile_nearest_rank(sorted_returns, 0.90)

    n = len(returns)
    wins = sum(1 for v in returns if v > 0)
    flats = sum(1 for v in returns if v == 0)
    win_rate_pct = _rate_pct(wins, n)
    flat_rate_pct = _rate_pct(flats, n)

    def bucket(v: Decimal) -> str:
        if v < Decimal("-10"):
            return "lt_m10"
        if v < Decimal("-5"):
            return "m10_m5"
        if v < Decimal("0"):
            return "m5_0"
        if v < Decimal("5"):
            return "0_5"
        if v < Decimal("10"):
            return "5_10"
        if v < Decimal("15"):
            return "10_15"
        if v <= Decimal("20"):
            return "15_20"
        return "gt_20"

    bc = Counter(bucket(v) for v in returns)

    count_ge_10pct = sum(1 for v in returns if v >= Decimal("10"))
    count_le_m10pct = sum(1 for v in returns if v <= Decimal("-10"))

    return HpSummary(
        hp=hp,
        file=str(path.as_posix()),
        rows_total=rows_total,
        completed=completed,
        incomplete=incomplete,
        incomplete_reasons=dict(reasons),
        invalid_completed_rows=invalid_completed_rows,
        ret_min=ret_min,
        ret_max=ret_max,
        ret_mean=ret_mean.quantize(Decimal("0.0001")) if ret_mean is not None else None,
        ret_median=ret_median.quantize(Decimal("0.0001")) if ret_median is not None else None,
        ret_stdev=ret_stdev.quantize(Decimal("0.0001")) if ret_stdev is not None else None,
        ret_p10=ret_p10.quantize(Decimal("0.0001")) if ret_p10 is not None else None,
        ret_p25=ret_p25.quantize(Decimal("0.0001")) if ret_p25 is not None else None,
        ret_p75=ret_p75.quantize(Decimal("0.0001")) if ret_p75 is not None else None,
        ret_p90=ret_p90.quantize(Decimal("0.0001")) if ret_p90 is not None else None,
        win_rate_pct=win_rate_pct,
        flat_rate_pct=flat_rate_pct,
        bucket_lt_m10=int(bc["lt_m10"]),
        bucket_m10_m5=int(bc["m10_m5"]),
        bucket_m5_0=int(bc["m5_0"]),
        bucket_0_5=int(bc["0_5"]),
        bucket_5_10=int(bc["5_10"]),
        bucket_10_15=int(bc["10_15"]),
        bucket_15_20=int(bc["15_20"]),
        bucket_gt_20=int(bc["gt_20"]),
        count_ge_10pct=count_ge_10pct,
        count_le_m10pct=count_le_m10pct,
    )


def _discover_files(results_dir: Path, glob_pat: str, only_hp: Iterable[int] | None) -> list[Path]:
    files = sorted(results_dir.glob(glob_pat), key=lambda p: p.name.lower())
    out: list[Path] = []
    for p in files:
        if not p.is_file():
            continue
        m = FILENAME_HP_RE.search(p.name)
        if not m:
            continue
        hp = int(m.group(1))
        if only_hp is not None and hp not in set(only_hp):
            continue
        out.append(p)
    return out


def _print_table(summaries: list[HpSummary]) -> None:
    headers = [
        "持有期",
        "总行数",
        "有效完成",
        "异常完成",
        "未完成",
        "最小收益(%)",
        "P10收益(%)",
        "P25收益(%)",
        "中位数收益(%)",
        "平均收益(%)",
        "P75收益(%)",
        "P90收益(%)",
        "最大收益(%)",
        "收益标准差(%)",
        "胜率(%)",
        "零收益占比(%)",
        "0~5%个数",
        "5~10%个数",
        "0~10%个数",
        ">=10%个数",
        "<=-10%个数",
    ]

    def fmt_pct(v: Decimal | None) -> str:
        if v is None:
            return ""
        return str(v)

    rows: list[list[str]] = []
    for s in summaries:
        sum_0_10 = s.bucket_0_5 + s.bucket_5_10
        rows.append(
            [
                str(s.hp),
                str(s.rows_total),
                str(s.completed - s.invalid_completed_rows),
                str(s.invalid_completed_rows),
                str(s.incomplete),
                fmt_pct(s.ret_min),
                fmt_pct(s.ret_p10),
                fmt_pct(s.ret_p25),
                fmt_pct(s.ret_median),
                fmt_pct(s.ret_mean),
                fmt_pct(s.ret_p75),
                fmt_pct(s.ret_p90),
                fmt_pct(s.ret_max),
                fmt_pct(s.ret_stdev),
                fmt_pct(s.win_rate_pct),
                fmt_pct(s.flat_rate_pct),
                str(s.bucket_0_5),
                str(s.bucket_5_10),
                str(sum_0_10),
                str(s.count_ge_10pct),
                str(s.count_le_m10pct),
            ]
        )

    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))

    def line(cells: list[str]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    print(line(headers))
    print(line(["-" * w for w in widths]))
    for r in rows:
        print(line(r))


def main() -> int:
    _ensure_utf8_console()
    parser = argparse.ArgumentParser(description="Summarize pattern_selection_hp_*.csv backtest outputs.")
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=Path("results"),
        help="Directory containing pattern_selection_hp_*.csv (default: results/)",
    )
    parser.add_argument(
        "--glob",
        default="pattern_selection_hp_*.csv",
        help="Glob pattern under results-dir (default: pattern_selection_hp_*.csv)",
    )
    parser.add_argument(
        "--only-hp",
        default="",
        help="Comma-separated holding periods to include, e.g. 5,10,20,30 (default: all matched files)",
    )
    parser.add_argument(
        "--out-csv",
        type=Path,
        default=None,
        help="Optional path to write a summary CSV (one row per hp).",
    )
    parser.add_argument(
        "--out-json",
        type=Path,
        default=None,
        help="Optional path to write full summaries as JSON (includes incomplete_reasons).",
    )
    parser.add_argument(
        "--print-reasons",
        action="store_true",
        help="Print incomplete_reason breakdown per hp after the main table.",
    )
    args = parser.parse_args()

    only_hp: set[int] | None = None
    if args.only_hp.strip():
        only_hp = {int(x.strip()) for x in args.only_hp.split(",") if x.strip()}

    files = _discover_files(args.results_dir, args.glob, only_hp)
    if not files:
        print(f"No files matched: {args.results_dir / args.glob}")
        return 2

    summaries = [summarize_one_csv(p) for p in files]
    summaries.sort(key=lambda s: s.hp)

    _print_table(summaries)

    if args.print_reasons:
        for s in summaries:
            if not s.incomplete_reasons:
                continue
            print(f"\n[hp={s.hp}] incomplete_reasons:")
            for k, v in sorted(s.incomplete_reasons.items(), key=lambda kv: (-kv[1], kv[0])):
                print(f"  {k}: {v}")

    if args.out_csv is not None:
        args.out_csv.parent.mkdir(parents=True, exist_ok=True)
        flat_fields = [
            "持有期",
            "文件",
            "总行数",
            "完成行数",
            "有效完成笔数",
            "异常完成行数",
            "未完成行数",
            "最小收益(%)",
            "P10收益(%)",
            "P25收益(%)",
            "中位数收益(%)",
            "平均收益(%)",
            "P75收益(%)",
            "P90收益(%)",
            "最大收益(%)",
            "收益标准差(%)",
            "胜率(%)",
            "零收益占比(%)",
            "<-10%个数",
            "-10~-5%个数",
            "-5~0%个数",
            "0~5%个数",
            "5~10%个数",
            "10~15%个数",
            "15~20%个数",
            ">20%个数",
            "0~10%个数",
            ">=10%个数",
            "<=-10%个数",
        ]
        with args.out_csv.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=flat_fields)
            w.writeheader()
            for s in summaries:
                valid_n = s.completed - s.invalid_completed_rows
                w.writerow(
                    {
                        "持有期": s.hp,
                        "文件": s.file,
                        "总行数": s.rows_total,
                        "完成行数": s.completed,
                        "有效完成笔数": valid_n,
                        "异常完成行数": s.invalid_completed_rows,
                        "未完成行数": s.incomplete,
                        "最小收益(%)": s.ret_min,
                        "P10收益(%)": s.ret_p10,
                        "P25收益(%)": s.ret_p25,
                        "中位数收益(%)": s.ret_median,
                        "平均收益(%)": s.ret_mean,
                        "P75收益(%)": s.ret_p75,
                        "P90收益(%)": s.ret_p90,
                        "最大收益(%)": s.ret_max,
                        "收益标准差(%)": s.ret_stdev,
                        "胜率(%)": s.win_rate_pct,
                        "零收益占比(%)": s.flat_rate_pct,
                        "<-10%个数": s.bucket_lt_m10,
                        "-10~-5%个数": s.bucket_m10_m5,
                        "-5~0%个数": s.bucket_m5_0,
                        "0~5%个数": s.bucket_0_5,
                        "5~10%个数": s.bucket_5_10,
                        "10~15%个数": s.bucket_10_15,
                        "15~20%个数": s.bucket_15_20,
                        ">20%个数": s.bucket_gt_20,
                        "0~10%个数": s.bucket_0_5 + s.bucket_5_10,
                        ">=10%个数": s.count_ge_10pct,
                        "<=-10%个数": s.count_le_m10pct,
                    }
                )
        print(f"\nWrote summary CSV: {args.out_csv}")

    if args.out_json is not None:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(s) for s in summaries]
        args.out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote summary JSON: {args.out_json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
