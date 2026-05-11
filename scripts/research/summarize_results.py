#!/usr/bin/env python3
"""Experimental wrapper around `quant summarize strategy-holding-periods`."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


STRATEGY_NAME = "amount_ratio_prev_5d_near_2x"
START_DATE = "2025-01-02"
END_DATE = "2026-04-15"
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
        "summarize",
        "strategy-holding-periods",
        "--results-dir",
        str(RESULTS_DIR),
    ]
    print("Running experimental summary wrapper:")
    print(" ".join(command))
    subprocess.run(command, check=False)


if __name__ == "__main__":
    main()
