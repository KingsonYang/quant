---
name: strategy-backtest-optimizer
description: Standard workflow for this quant project to implement, score, validate, backtest, randomly optimize, and cross-period analyze a stock selection strategy. Use when the user asks to create a new strategy, improve an existing strategy, run strategy backtests, search for optimal parameters, compare default versus optimized combinations, or decide whether a strategy is stable, short-term, overfit, conservative, or aggressive.
---

# Strategy Backtest Optimizer

Use this skill to run the project's repeatable strategy research workflow. Keep the process disciplined: define the signal, implement deterministic selection, add `composite_score`, test, run realistic 1w-capital backtests, optimize, then judge robustness across market periods.

## Workflow

1. **Clarify the strategy edge**
   - Identify signal day, buy day, sell rules, intended holding period, and what market behavior the strategy is trying to capture.
   - Check whether current daily data can support it: OHLC, `change_pct`, `volume`, `amount`, `turnover_ratio`, `ma5_price`, `ma10_price`, `ma20_price`.
   - If the idea depends on unavailable data such as intraday tape, order book, theme, industry, or capital flow, either reject that part or build a daily-data approximation and state the limitation.

2. **Implement hard selection conditions first**
   - Put the strategy's non-negotiable rules into a `pattern_sql` implementation in `src/quant/pattern_selection.py`.
   - Add a params model in `src/quant/config/models.py`.
   - Add optimization whitelist entries in `src/quant/optimization/search_space.py` for every parameter intended to be tuned.
   - Create or update the strategy YAML under `strategies/`.

3. **Add `composite_score` for ranking**
   - Always score if daily candidates can exceed one stock.
   - Use score for ranking only; do not let it replace core strategy validity.
   - Typical score dimensions:
     - location quality: lower or cleaner position is better
     - shape quality: tighter platform, stable pullback, stronger support
     - volume health: moderate expansion is better than extreme exhaustion
     - breakout or reclaim strength: clears key price without excessive overheating
     - MA structure: price above MA5/MA10/MA20 and short MAs improving
     - turnover health: active but not frantic
     - overheating penalty: recent surge, extreme turnover, extreme volume, or excessive chase distance
   - Default YAML ordering:

```yaml
selection:
  sort_by: composite_score
  sort_order: DESC
```

4. **Use small-capital realistic defaults**
   - Default to 1w capital unless the user specifies otherwise.
   - Prefer single-position, top-1 execution because small capital cannot buy every candidate.

```yaml
strategy_backtest:
  initial_capital: 10000
  max_positions: 1
  max_daily_buys: 1
  position_size_pct: 100
  selection_top_n: 1
```

5. **Validate before analysis**
   - Run:

```powershell
python -m quant strategy validate <strategy_name>
python -m pytest tests\test_pattern_strategy.py tests\test_strategy_config.py -q
```

   - Add focused tests for:
     - strategy YAML validation
     - candidate selection returns `composite_score`
     - `sort_by: composite_score DESC` ranking
     - score gates such as `min_composite_score` and `min_score_gap` if used

6. **Run fixed default backtests**
   - Run these periods before optimization:

```powershell
python -m quant run backtest-strategy <strategy_name> --start-date 2015-12-29 --end-date 2026-05-25 --no-save-experiment
python -m quant run backtest-strategy <strategy_name> --start-date 2015-12-29 --end-date 2023-12-29 --no-save-experiment
python -m quant run backtest-strategy <strategy_name> --start-date 2024-01-02 --end-date 2026-05-25 --no-save-experiment
python -m quant run backtest-strategy <strategy_name> --start-date 2025-01-02 --end-date 2026-05-25 --no-save-experiment
```

   - Do not run multiple DuckDB backtests against the same database concurrently; this project can hit file-lock errors on Windows. Run strategy backtests serially.

7. **Random optimize with realistic search space**
   - Include core strategy parameters and execution gates:
     - `holding_period`
     - `take_profit_pct`
     - `stop_loss_pct`
     - `min_composite_score`
     - `min_score_gap`
     - strategy-specific windows, volume thresholds, amount thresholds, turnover ranges, pullback or breakout thresholds
   - Keep `initial_capital` fixed at 10000 unless the user explicitly asks to compare capital sizes.
   - Standard command:

```powershell
python -m quant optimize <strategy_name> --target strategy_backtest --method random --max-trials 80 --seed 7 --start-date 2015-12-29 --end-date 2026-05-25 --top-n 10
```

8. **Cross-period verify top trials**
   - Re-run the top trials across long, old, recent-2y, and recent-1y periods.
   - Prefer writing CSVs under `results/research/<strategy_name>/`:
     - `fixed_backtest_summary.csv`
     - `random_trials_ranked_by_final_assets.csv`
     - `top_trial_period_comparison.csv`
     - `yearly_metrics.csv`
   - If no reusable script exists, create a focused script under `scripts/research/` for reproducibility.

## Decision Rules

Choose recommendations by robustness, not only final assets.

- **Default/conservative**: moderate return, enough trades, controlled drawdown, old period does not collapse, recent period still works.
- **Aggressive**: highest return but high drawdown, unstable old period, or dependence on recent market style.
- **Observe only**: good-looking win rate with too few trades.
- **Reject or pause**: long-period loss, severe drawdown, or obvious overfitting.

Red flags:

- Fewer than 5 closed trades across a long test is not enough evidence.
- 100% win rate with 1-3 trades is not reliable.
- Highest-return trial with very large drawdown should not become default.
- A strategy that only works in recent years needs a market regime filter before being treated as stable.

## Final Response Shape

Report:

- files changed
- validation commands and results
- default fixed-period results
- random optimization top result
- recommended conservative parameters
- optional aggressive parameters
- output file paths
- caveats: sample size, drawdown, market-period dependence, unavailable data assumptions

Keep the conclusion practical: tell the user what to run next and which combination is safest to observe.
