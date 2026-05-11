#!/bin/bash
# Experimental wrapper around `quant run matrix strategy-holding-periods`

STRATEGY_NAME="amount_ratio_prev_5d_near_2x"
START_DATE="2025-01-02"
END_DATE="2026-04-15"
HOLDING_PERIODS=$(seq -s, 1 30)
RESULTS_DIR="results/strategy_holding_periods/${STRATEGY_NAME}_${START_DATE}_${END_DATE}"

python -m quant \
  run matrix strategy-holding-periods "$STRATEGY_NAME" \
  --start-date "$START_DATE" \
  --end-date "$END_DATE" \
  --holding-periods "$HOLDING_PERIODS" \
  --results-dir "$RESULTS_DIR" \
  --show-progress
