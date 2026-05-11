#!/usr/bin/env python3
"""Experimental wrapper around `quant run matrix strategy-holding-periods`."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


STRATEGY_NAME = "amount_ratio_prev_5d_near_2x"
START_DATE = "2025-01-02"
END_DATE = "2026-04-15"
HOLDING_PERIODS = ",".join(str(value) for value in range(1, 31))
RESULTS_DIR = (
    Path("results")
    / "strategy_holding_periods"
    / f"{STRATEGY_NAME}_{START_DATE}_{END_DATE}"
)


def main() -> None:
    command = [
        sys.executable,
        "-m",
        "quant",
        "run",
        "matrix",
        "strategy-holding-periods",
        STRATEGY_NAME,
        "--start-date",
        START_DATE,
        "--end-date",
        END_DATE,
        "--holding-periods",
        HOLDING_PERIODS,
        "--results-dir",
        str(RESULTS_DIR),
        "--show-progress",
    ]
    print("Running experimental matrix wrapper:")
    print(" ".join(command))
    subprocess.run(command, check=False)
    print("\nTo summarize results, run:")
    print(
        f"{sys.executable} -m quant summarize strategy-holding-periods --results-dir {RESULTS_DIR}"
    )


if __name__ == "__main__":
    main()
