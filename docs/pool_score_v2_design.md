# Pool-Level Scoring System V2: Design, Implementation & Validation

## 1. Problem Statement

Strategy `shandian_1hao_erci_baoliang_qidong` selects ~20 stocks per day, but the current `composite_score` (v1) has **negative** predictive power (IC = -0.051). Random pick from the pool breaks even (~0%), while the best stock averages +10.87%/trade (HP=1). The problem is purely about **selection within the daily pool**, not pool construction.

## 2. Available Data Sources

### 2.1 Quant Project DuckDB (`data/quant.duckdb`)

| Table | Coverage | Key Columns |
|-------|----------|-------------|
| `daily_bar` | 2013-2026, 5.7M rows, 3043 securities | OHLCV, MA5/10/20 (price+volume), turnover_ratio, change_pct |
| `ths_daily_snapshot` | 2026-05-28 to 2026-07-14 (31 days only) | Limit-up data, money flow, multi-period returns, market cap, institutional trend |
| `hs300_bar` | 2019-2026, 1797 rows | Index OHLCV for market context |
| `market_regime` | 1 row only | MA20/MA60 direction, ADX, regime type |
| `security_master` | 3043 securities | symbol, security_id, board, list_date |

**Backtest coverage**: `daily_bar` has 100% match rate for all pool entries (2022-2026).

### 2.2 TDX Quant API (for live trading, not backtest)

| API | Key Data |
|-----|----------|
| `get_more_info` | Multi-period returns, turnover, money flow (Zjl, FCAmo), limit-up info, market cap, PE/PB/Beta, open auction data |
| `get_gpjy_value` | GP01 股东人数, GP03 融资融券, GP06 陆股通, GP14 涨停数据, GP22 封成比, GP24 首封时间, GP38-40 涨停统计 |
| `get_financial_data` | 400+ financial fields: ROE, revenue growth, profit margins, cash flow |
| `get_bkjy_value` | Sector PE/PB/PS, 涨停数, 资金流入 |
| `get_scjy_value` | Market-level: 涨停家数, 连板数, 涨跌家数, 期指净持仓 |
| `get_gp_one_data` | Consensus target price, institutional holdings, performance forecasts |

### 2.3 Pattern Selection CSVs
20 files (HP=1 to HP=20), each with: symbol, selection_date, d0_date, buy/sell dates, return_pct, 10 existing f_ features, composite_score.

### 2.4 Enriched CSV
`pattern_selection_hp_1_enriched.csv` adds: tdx_Board, tdx_rs_hyname (industry), tdx_ActiveCap_wan, tdx_IPO, idx_env, _cap_bucket.

## 3. Feature Engineering (24 Features from 5 Angles)

### Angle 1: Price Momentum (from daily_bar)
- `mom_5d`, `mom_10d`, `mom_20d`, `mom_30d` — multi-period returns

### Angle 2: Price Positioning (from daily_bar)
- `close_vs_ma5`, `close_vs_ma10`, `close_vs_ma20` — distance from moving averages
- `range_pos_20d` — position within 20-day high-low range (0=bottom, 1=top)

### Angle 3: Volume Characteristics (from daily_bar)
- `vol_ratio_ma5`, `vol_ratio_ma10`, `vol_ratio_ma20` — volume vs MA
- `d1_turnover`, `vol_trend_5d` — turnover and volume trend

### Angle 4: Volatility & Candle Pattern (from daily_bar)
- `volatility_10d`, `volatility_20d`, `d1_amplitude`
- `d1_gap`, `d1_body_pct`, `d1_upper_shadow`, `d1_lower_shadow`

### Angle 5: Existing Pattern Features (from SQL)
- `f_signal_change`, `f_signal_turnover`, `f_signal_vol_ratio_prev5`, `f_signal_vol_vs_prev1`
- `f_d0_vol_ratio_prev5`, `f_signal_vs_d0_close_pct`, `f_platform_avg_vol_vs_d0`
- `f_platform_weak_day_ratio`, `f_platform_min_low_vs_d0_low_pct`, `f_between_days`
- `composite_score` (v1, used inverted)
- `d1_change_pct`, `d0_change_pct`, `d1_vs_d0_close`

## 4. IC Analysis Results

### Top Positive IC Features (higher = better)
| Feature | Train IC | Test IC | Consistency |
|---------|----------|---------|-------------|
| d1_upper_shadow | +0.066 | +0.061 | Very stable |
| range_pos_20d | +0.045 | +0.012 | Stable |
| vol_ratio_ma20 | +0.035 | -0.015 | Weakens |
| f_platform_weak_day_ratio | +0.033 | +0.001 | Weakens |
| d1_gap | +0.031 | +0.010 | Stable |
| mom_20d | +0.021 | +0.020 | Very stable |

### Top Negative IC Features (lower = better, inverted)
| Feature | Train IC | Test IC | Consistency |
|---------|----------|---------|-------------|
| composite_score | -0.054 | -0.045 | Very stable |
| f_signal_vs_d0_close_pct | -0.053 | -0.037 | Very stable |
| f_between_days | -0.033 | -0.031 | Very stable |
| f_platform_avg_vol_vs_d0 | -0.025 | -0.039 | Improves |
| d1_change_pct | -0.020 | -0.034 | Improves |

### Composite Score IC
| Score | Full IC | Train IC | Test IC |
|-------|---------|----------|---------|
| V2 Score | **+0.056** | +0.067 | +0.035 |
| V1 composite_score | -0.051 | -0.054 | -0.045 |

## 5. Validation Results

### HP=1, Test Period (2025-2026)
| Method | Top-N | Avg Return | Win Rate | vs Random |
|--------|-------|-----------|----------|-----------|
| V2 Score | 1 | +0.354% | 44.8% | +0.107% |
| V2 Score | 2 | +0.186% | 46.1% | -0.061% |
| V1 Score | 1 | +0.295% | 49.9% | +0.048% |
| Random | - | +0.247% | - | - |
| Best pick | - | +11.149% | - | - |

### All 20 Holding Periods (Test Period, Top-1)
| HP | V2 Return | V1 Return | Random | V2 Edge |
|----|----------|-----------|--------|---------|
| 1 | +0.354% | +0.295% | +0.247% | +0.107% |
| 5 | -0.044% | -0.391% | -0.052% | +0.008% |
| 10 | +0.262% | +0.679% | +0.224% | +0.038% |
| 15 | +1.025% | +1.002% | +0.509% | +0.516% |
| 20 | +2.155% | +1.707% | +0.786% | +1.369% |

V2 beats random in **14 of 20** holding periods. Strongest edge at HP 17-20 (+1.0% to +1.5% per trade).

### Year-by-Year (HP=1, Top-1)
| Year | V2 Return | V1 Return | Random | V2 Edge |
|------|----------|-----------|--------|---------|
| 2022 | +0.119% | -1.467% | -0.038% | +0.156% |
| 2023 | +0.113% | -0.344% | -0.104% | +0.216% |
| 2024 | +0.032% | +0.382% | -0.074% | +0.107% |
| 2025 | +0.424% | +0.236% | +0.245% | +0.179% |
| 2026 | +0.225% | +0.404% | +0.252% | -0.027% |

V2 is **positive every year** (V1 was deeply negative in 2022-2023).

## 6. Implementation

### Files Created
- `src/quant/evaluation/pool_features.py` — Feature extraction from daily_bar
- `src/quant/evaluation/pool_scoring.py` — V2 scoring with 24 IC-weighted features

### Integration
Modified `src/quant/pattern_selection.py`:
1. Added `pool_score_v2` to `SUPPORTED_PATTERN_SORT_FIELDS`
2. Added `pool_score_v2` field to `PatternSelectionCandidate`
3. Added `pool_score_v2` branch in `_sort_candidates`
4. Added V2 score computation in `select_pattern_candidates_with_connection`

### Usage
Set `sort_by: pool_score_v2` in strategy YAML:
```yaml
selection:
  sort_by: pool_score_v2
  sort_order: DESC
```

## 7. Live Trading Extension Plan

### Phase 1: ths_daily_snapshot Integration (Immediate)
When `ths_daily_snapshot` data is available (last 31 days), use its richer features:
- `limit_seal_ratio`, `limit_open_count`, `first_limit_time` → limit-up quality
- `main_net_volume`, `inner_outer_ratio` → money flow
- `institutional_trend` → institutional behavior
- `open_change_pct`, `auction_turnover_ratio` → open auction quality

### Phase 2: TDX API Integration (Medium-term)
For live trading, fetch real-time features via TDX API:
- `get_more_info`: ZAFPre5/10/20/30/60, Zjl, FCAmo, FCb, OpenAmo, LastZTHzNum
- `get_gpjy_value`: GP14 (涨停数据), GP22 (封成比), GP24 (首封时间), GP39 (次日红盘率)
- `get_bkjy_value`: sector PE/PB percentile, sector 资金流入
- `get_scjy_value`: market 涨停数, 连板数, 涨跌家数

### Phase 3: Model Enhancement (Long-term)
- Logistic regression / gradient boosting (currently IC-weighted rank composite)
- Feature interaction effects (momentum × volume)
- Time-varying weights (rolling IC)
- Regime-conditional scoring (different weights in bull/bear markets)
- Financial quality features (ROE, revenue growth from `get_financial_data`)

## 8. Limitations & Risks

1. **Small edge**: V2 Top-1 beats random by ~0.1%/trade (HP=1). After transaction costs (~0.1-0.15% round-trip), the edge is marginal for 1-day holding. Longer holding periods (HP 15-20) show stronger edges (+0.5% to +1.4%).
2. **IC decay**: Train IC=0.067 decays to Test IC=0.035, suggesting some overfitting.
3. **Feature instability**: Some features (vol_ratio_ma20, d0_change_pct) flip sign between train and test. The consistently stable features are: d1_upper_shadow, mom_20d, composite_score (inverted), f_signal_vs_d0_close_pct (inverted), f_between_days (inverted).
4. **Limited data for live features**: `ths_daily_snapshot` only covers 31 days. TDX API features need to be fetched and cached for live use.
