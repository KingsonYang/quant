#!/usr/bin/env python3

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path


STRATEGY_NAME = "amount_ratio_prev_5d_near_2x"


def parse_date(date_str: str):
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def daterange(start_date, end_date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def safe_decode(data: bytes) -> str:
    if not data:
        return ""
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def run_command_for_date(trade_date: str):
    cmd = [
        "quant",
        "run",
        "select",
        STRATEGY_NAME,
        "--trade-date",
        trade_date,
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode, safe_decode(result.stdout), safe_decode(result.stderr)


def main():
    start_str = input("请输入开始日期（YYYY-MM-DD）: ").strip()
    end_str = input("请输入结束日期（YYYY-MM-DD）: ").strip()

    try:
        start_date = parse_date(start_str)
        end_date = parse_date(end_str)
    except ValueError:
        print("日期格式错误，请使用 YYYY-MM-DD")
        sys.exit(1)

    if start_date > end_date:
        print("开始日期不能大于结束日期")
        sys.exit(1)

    output_path = Path("results") / "select_stocks_by_date_range.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8-sig") as handle:
        handle.write(f"执行时间区间：{start_date} ~ {end_date}\n")
        handle.write("=" * 80 + "\n\n")

        for current_date in daterange(start_date, end_date):
            trade_date = current_date.strftime("%Y-%m-%d")
            print(f"正在执行: {trade_date}")
            returncode, stdout, stderr = run_command_for_date(trade_date)

            handle.write(f"[trade-date] {trade_date}\n")
            handle.write(f"[returncode] {returncode}\n")
            handle.write("[stdout]\n")
            handle.write(stdout if stdout else "(无输出)\n")
            handle.write("\n[stderr]\n")
            handle.write(stderr if stderr else "(无输出)\n")
            handle.write("\n" + "-" * 80 + "\n\n")

    print(f"执行完成，结果已写入: {output_path}")


if __name__ == "__main__":
    main()
