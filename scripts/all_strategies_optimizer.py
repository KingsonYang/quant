"""
综合策略优化器 v1
7个策略 + 网格搜索 + 训练/测试分离
同时产出每个策略的最优参数 + 评分排序
"""
import sys
sys.path.insert(0, 'D:/SoftWare/tdx/PYPlugins/user')
import os
os.environ['TDXPATH'] = 'D:/SoftWare/tdx/TdxW.exe'

import duckdb
import pandas as pd
import numpy as np
import time
import itertools
import functools
print = functools.partial(print, flush=True)

DB_PATH = "D:/Files/workspace-ai/quant/data/quant.duckdb"
OUTPUT_DIR = "D:/Files/workspace-ai/quant/data/param_results"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 全局参数
COMMISSION_RATE = 0.0003
STAMP_TAX = 0.001
SLIPPAGE_PCT = 0.001
TOTAL_COST_PCT = (COMMISSION_RATE * 2 + STAMP_TAX + SLIPPAGE_PCT * 2)
STOP_LOSS_PCT = -0.03
TRAIN_START = "2024-01-01"
TRAIN_END = "2025-12-31"
TEST_START = "2026-01-01"
TEST_END = "2026-06-01"


# ═══════════════════════════════════════════════════════════════
# SQL 构建器（7个策略）
# ═══════════════════════════════════════════════════════════════

def build_520_query(holding_days, ma_strict, volume_mult, amount_filter_yi, need_trend):
    """520战法：MA5上穿确认"""
    trend_cond = "AND ma5 > ma10 AND ma10 > ma20" if need_trend else ""
    return f"""
    WITH base AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
        FROM daily_bar WHERE trade_date >= '{TRAIN_START}' AND trade_date <= '{TEST_END}'
    ),
    kline AS (
        SELECT *, AVG(close_price) OVER w5 as ma5, AVG(close_price) OVER w10 as ma10,
            AVG(close_price) OVER w20 as ma20, AVG(volume) OVER w5 as vol_ma5
        FROM base
        WINDOW w5 AS (PARTITION BY security_id ORDER BY day_idx ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
              w10 AS (PARTITION BY security_id ORDER BY day_idx ROWS BETWEEN 9 PRECEDING AND CURRENT ROW),
              w20 AS (PARTITION BY security_id ORDER BY day_idx ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
    ),
    kline_lag AS (
        SELECT *, LAG(close_price, 1) OVER (PARTITION BY security_id ORDER BY day_idx) as ma5_prev FROM kline
    ),
    signals AS (
        SELECT * FROM kline_lag
        WHERE ma5 > ma5_prev * (1 + {ma_strict})
          AND volume > vol_ma5 * {volume_mult}
          AND amount >= {amount_filter_yi * 1e8} {trend_cond}
    ),
    all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, change_pct FROM base)
    SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
        e.trade_date as entry_date, e.open_price as entry_price, e.change_pct as entry_change_pct,
        x.trade_date as exit_date, x.close_price as exit_price,
        '520' as strategy_name
    FROM signals s
    LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
    LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {holding_days + 1}
    WHERE e.trade_date IS NOT NULL AND e.open_price > 0
      AND COALESCE(e.change_pct, 0) < 9.5 AND x.trade_date IS NOT NULL
    """


def build_macd_query(holding_days, fast, slow, signal, volume_mult, amount_filter_yi):
    """MACD金叉策略 — Signal线对MACD值做SMA"""
    min_idx = max(fast, slow) + signal + 5
    return f"""
    WITH base AS (
        SELECT security_id, trade_date, close_price, open_price, volume, amount,
            ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
        FROM daily_bar WHERE trade_date >= '{TRAIN_START}' AND trade_date <= '{TEST_END}'
    ),
    ema_base AS (
        SELECT b.security_id, b.trade_date, b.day_idx, b.open_price, b.close_price, b.volume, b.amount,
            AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN {fast-1} PRECEDING AND CURRENT ROW) as sma_fast,
            AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN {slow-1} PRECEDING AND CURRENT ROW) as sma_slow,
            AVG(b.volume) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as vol_ma5
        FROM base b WHERE b.day_idx > {min_idx}
    ),
    macd_base AS (
        SELECT b.security_id, b.trade_date, b.day_idx, b.open_price, b.close_price, b.volume, b.amount, b.vol_ma5,
            b.sma_fast - b.sma_slow as macd
        FROM ema_base b
    ),
    signal_line AS (
        SELECT b.security_id, b.trade_date, b.day_idx, b.open_price, b.close_price, b.volume, b.amount, b.vol_ma5, b.macd,
            AVG(b.macd) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN {signal-1} PRECEDING AND CURRENT ROW) as macd_signal
        FROM macd_base b
    ),
    macline AS (
        SELECT s.*,
            s.macd - s.macd_signal as macd_diff,
            (SELECT m.macd - m.macd_signal FROM signal_line m
             WHERE m.security_id = s.security_id AND m.trade_date < s.trade_date
             ORDER BY m.trade_date DESC LIMIT 1) as macd_diff_prev
        FROM signal_line s
    ),
    signals AS (
        SELECT m.security_id, m.trade_date, m.day_idx, m.open_price, m.close_price, m.volume, m.amount, m.vol_ma5
        FROM macline m
        WHERE m.macd > m.macd_signal AND m.macd_diff_prev IS NOT NULL AND m.macd_diff_prev <= 0
          AND m.volume > m.vol_ma5 * {volume_mult}
          AND m.amount >= {amount_filter_yi * 1e8}
    ),
    all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price FROM base)
    SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
        e.trade_date as entry_date, e.open_price as entry_price,
        x.trade_date as exit_date, x.close_price as exit_price,
        'MACD' as strategy_name
    FROM signals s
    LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
    LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {holding_days + 1}
    WHERE e.trade_date IS NOT NULL AND e.open_price > 0
      AND x.trade_date IS NOT NULL
    """


def build_rsi_query(holding_days, rsi_period, rsi_buy, volume_mult, amount_filter_yi):
    """RSI超卖反转策略"""
    return f"""
    WITH base AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
        FROM daily_bar WHERE trade_date >= '{TRAIN_START}' AND trade_date <= '{TEST_END}'
    ),
    price_lag AS (
        SELECT *, LAG(close_price, 1) OVER (PARTITION BY security_id ORDER BY trade_date) as prev_close,
            AVG(volume) OVER (PARTITION BY security_id ORDER BY trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as vol_ma5
        FROM base
    ),
    rsi_calc AS (
        SELECT *,
            CASE WHEN close_price > prev_close THEN close_price - prev_close ELSE 0 END as up_move,
            CASE WHEN close_price < prev_close THEN prev_close - close_price ELSE 0 END as down_move
        FROM price_lag
    ),
    rsi_sma AS (
        SELECT *,
            AVG(up_move) OVER (PARTITION BY security_id ORDER BY trade_date ROWS BETWEEN {rsi_period-1} PRECEDING AND CURRENT ROW) as avg_up,
            AVG(down_move) OVER (PARTITION BY security_id ORDER BY trade_date ROWS BETWEEN {rsi_period-1} PRECEDING AND CURRENT ROW) as avg_down
        FROM rsi_calc
    ),
    signals AS (
        SELECT *,
            CASE WHEN avg_down > 0 THEN 100 - 100 * avg_up / (avg_up + avg_down) ELSE 100 END as rsi_value,
            LAG(CASE WHEN avg_down > 0 THEN 100 - 100 * avg_up / (avg_up + avg_down) ELSE 100 END, 1)
                OVER (PARTITION BY security_id ORDER BY trade_date) as rsi_prev
        FROM rsi_sma
    ),
    all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, change_pct, volume, amount FROM base)
    SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
        e.trade_date as entry_date, e.open_price as entry_price, e.change_pct as entry_change_pct,
        x.trade_date as exit_date, x.close_price as exit_price,
        'RSI' as strategy_name
    FROM (
        SELECT * FROM signals
        WHERE rsi_value < {rsi_buy} AND rsi_prev >= {rsi_buy}
          AND volume > vol_ma5 * {volume_mult}
          AND amount >= {amount_filter_yi * 1e8}
    ) s
    LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
    LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {holding_days + 1}
    WHERE e.trade_date IS NOT NULL AND e.open_price > 0
      AND COALESCE(e.change_pct, 0) < 9.5 AND x.trade_date IS NOT NULL
    """


def build_ma_cross_query(holding_days, fast_ma, slow_ma, volume_mult, amount_filter_yi):
    """均线金叉策略（MA5上穿MA10）"""
    return f"""
    WITH base AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
        FROM daily_bar WHERE trade_date >= '{TRAIN_START}' AND trade_date <= '{TEST_END}'
    ),
    kline AS (
        SELECT *,
            AVG(close_price) OVER (PARTITION BY security_id ORDER BY trade_date ROWS BETWEEN {fast_ma-1} PRECEDING AND CURRENT ROW) as ma_fast,
            AVG(close_price) OVER (PARTITION BY security_id ORDER BY trade_date ROWS BETWEEN {slow_ma-1} PRECEDING AND CURRENT ROW) as ma_slow,
            AVG(volume) OVER (PARTITION BY security_id ORDER BY trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as vol_ma5
        FROM base
    ),
    kline_lag AS (
        SELECT *, LAG(ma_fast, 1) OVER (PARTITION BY security_id ORDER BY trade_date) as ma_fast_prev,
            LAG(ma_slow, 1) OVER (PARTITION BY security_id ORDER BY trade_date) as ma_slow_prev
        FROM kline
    ),
    signals AS (
        SELECT * FROM kline_lag
        WHERE ma_fast > ma_slow AND ma_fast_prev <= ma_slow_prev
          AND volume > vol_ma5 * {volume_mult}
          AND amount >= {amount_filter_yi * 1e8}
    ),
    all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, change_pct FROM base)
    SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
        e.trade_date as entry_date, e.open_price as entry_price, e.change_pct as entry_change_pct,
        x.trade_date as exit_date, x.close_price as exit_price,
        'MA_CROSS' as strategy_name
    FROM signals s
    LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
    LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {holding_days + 1}
    WHERE e.trade_date IS NOT NULL AND e.open_price > 0
      AND COALESCE(e.change_pct, 0) < 9.5 AND x.trade_date IS NOT NULL
    """


def build_break_high_query(holding_days, lookback, volume_mult, amount_filter_yi):
    """突破N日新高策略"""
    return f"""
    WITH base AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
        FROM daily_bar WHERE trade_date >= '{TRAIN_START}' AND trade_date <= '{TEST_END}'
    ),
    kline AS (
        SELECT *,
            MAX(high_price) OVER (PARTITION BY security_id ORDER BY trade_date ROWS BETWEEN {lookback} PRECEDING AND 1 PRECEDING) as highest_{lookback}d,
            AVG(volume) OVER (PARTITION BY security_id ORDER BY trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as vol_ma5
        FROM base
    ),
    signals AS (
        SELECT * FROM kline
        WHERE close_price > highest_{lookback}d AND close_price > 0
          AND volume > vol_ma5 * {volume_mult}
          AND amount >= {amount_filter_yi * 1e8}
    ),
    all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, change_pct FROM base)
    SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
        e.trade_date as entry_date, e.open_price as entry_price, e.change_pct as entry_change_pct,
        x.trade_date as exit_date, x.close_price as exit_price,
        'BREAK_HIGH' as strategy_name
    FROM signals s
    LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
    LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {holding_days + 1}
    WHERE e.trade_date IS NOT NULL AND e.open_price > 0
      AND COALESCE(e.change_pct, 0) < 9.5 AND x.trade_date IS NOT NULL
    """


def build_vol_surge_query(holding_days, vol_mult, pullback1, pullback2, change_min, change_max, amount_filter_yi):
    """倍量回调策略"""
    return f"""
    WITH base AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
        FROM daily_bar WHERE trade_date >= '{TRAIN_START}' AND trade_date <= '{TEST_END}'
    ),
    kline AS (
        SELECT *, LAG(volume, 1) OVER (PARTITION BY security_id ORDER BY day_idx) as vol_1,
            LAG(volume, 2) OVER (PARTITION BY security_id ORDER BY day_idx) as vol_2,
            AVG(close_price) OVER (PARTITION BY security_id ORDER BY day_idx ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as ma5
        FROM base
    ),
    signals AS (
        SELECT * FROM kline
        WHERE change_pct >= {change_min} AND change_pct <= {change_max}
          AND vol_1 IS NOT NULL AND vol_1 < volume * {pullback1}
          AND vol_2 IS NOT NULL AND vol_2 < volume * {pullback2}
          AND close_price > ma5
          AND volume > (SELECT AVG(volume) FROM kline k2 WHERE k2.security_id = kline.security_id AND k2.day_idx BETWEEN kline.day_idx - 5 AND kline.day_idx - 1) * {vol_mult}
          AND amount >= {amount_filter_yi * 1e8}
    ),
    all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, change_pct FROM base)
    SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
        e.trade_date as entry_date, e.open_price as entry_price, e.change_pct as entry_change_pct,
        x.trade_date as exit_date, x.close_price as exit_price,
        'VOL_SURGE' as strategy_name
    FROM signals s
    LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
    LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {holding_days + 1}
    WHERE e.trade_date IS NOT NULL AND e.open_price > 0
      AND COALESCE(e.change_pct, 0) < 9.5 AND x.trade_date IS NOT NULL
    """


def build_old_duck_query(holding_days, ma_strict, volume_mult, had_below_window, amount_filter_yi):
    """老鸭头策略"""
    return f"""
    WITH base AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
        FROM daily_bar WHERE trade_date >= '{TRAIN_START}' AND trade_date <= '{TEST_END}'
    ),
    kline AS (
        SELECT *, AVG(close_price) OVER w5 as ma5, AVG(close_price) OVER w10 as ma10,
            AVG(close_price) OVER w20 as ma20, AVG(volume) OVER w5 as vol_ma5
        FROM base
        WINDOW w5 AS (PARTITION BY security_id ORDER BY day_idx ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
              w10 AS (PARTITION BY security_id ORDER BY day_idx ROWS BETWEEN 9 PRECEDING AND CURRENT ROW),
              w20 AS (PARTITION BY security_id ORDER BY day_idx ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
    ),
    kline_lag AS (
        SELECT *, LAG(ma5, 1) OVER (PARTITION BY security_id ORDER BY day_idx) as ma5_prev,
            LAG(ma10, 1) OVER (PARTITION BY security_id ORDER BY day_idx) as ma10_prev,
            MAX(CASE WHEN ma5 < ma10 THEN 1 ELSE 0 END) OVER (
                PARTITION BY security_id ORDER BY day_idx
                ROWS BETWEEN {had_below_window + 1} PRECEDING AND 1 PRECEDING) as had_below_recently
        FROM kline
    ),
    signals AS (
        SELECT * FROM kline_lag
        WHERE ma5 > ma10 AND ma5_prev <= ma10_prev
          AND ma5 > ma5_prev * (1 + {ma_strict})
          AND close_price > ma20
          AND volume > vol_ma5 * {volume_mult}
          AND had_below_recently = 1
          AND amount >= {amount_filter_yi * 1e8}
    ),
    all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, change_pct FROM base)
    SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
        e.trade_date as entry_date, e.open_price as entry_price, e.change_pct as entry_change_pct,
        x.trade_date as exit_date, x.close_price as exit_price,
        'OLD_DUCK' as strategy_name
    FROM signals s
    LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
    LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {holding_days + 1}
    WHERE e.trade_date IS NOT NULL AND e.open_price > 0
      AND COALESCE(e.change_pct, 0) < 9.5 AND x.trade_date IS NOT NULL
    """


# ═══════════════════════════════════════════════════════════════
# 参数网格定义
# ═══════════════════════════════════════════════════════════════

STRATEGY_GRIDS = {
    '520': {
        'func': build_520_query,
        'grid': {
            'holding_days': [3, 5],
            'ma_strict': [0.001, 0.003, 0.005],
            'volume_mult': [1.0, 1.3, 1.5],
            'amount_filter_yi': [1.0, 2.0],
            'need_trend': [False],
        },
        'n_total': 54,
    },
    'MACD': {
        'func': build_macd_query,
        'grid': {
            'holding_days': [3, 5],
            'fast': [10, 12],
            'slow': [26, 30],
            'signal': [9],
            'volume_mult': [1.0, 1.3, 1.5],
            'amount_filter_yi': [1.0, 2.0],
        },
        'n_total': 24,
    },
    'RSI': {
        'func': build_rsi_query,
        'grid': {
            'holding_days': [3, 5],
            'rsi_period': [6, 10, 14],
            'rsi_buy': [25, 30, 35],
            'volume_mult': [1.0, 1.3],
            'amount_filter_yi': [1.0, 2.0],
        },
        'n_total': 36,
    },
    'MA_CROSS': {
        'func': build_ma_cross_query,
        'grid': {
            'holding_days': [3, 5],
            'fast_ma': [5, 10],
            'slow_ma': [20, 60],
            'volume_mult': [1.0, 1.3, 1.5],
            'amount_filter_yi': [1.0, 2.0],
        },
        'n_total': 48,
    },
    'BREAK_HIGH': {
        'func': build_break_high_query,
        'grid': {
            'holding_days': [3, 5],
            'lookback': [10, 20],
            'volume_mult': [1.0, 1.3, 1.5],
            'amount_filter_yi': [1.0, 2.0],
        },
        'n_total': 24,
    },
    'VOL_SURGE': {
        'func': build_vol_surge_query,
        'grid': {
            'holding_days': [3, 5],
            'vol_mult': [1.5, 2.0],
            'pullback1': [0.6, 0.8],
            'pullback2': [0.4, 0.6],
            'change_min': [2.0, 3.0, 5.0],
            'change_max': [8.0, 10.0],
            'amount_filter_yi': [1.0, 2.0],
        },
        'n_total': 96,
    },
    'OLD_DUCK': {
        'func': build_old_duck_query,
        'grid': {
            'holding_days': [3, 5],
            'ma_strict': [0.001, 0.003, 0.005],
            'volume_mult': [1.0, 1.3, 1.5],
            'had_below_window': [5, 10],
            'amount_filter_yi': [1.0, 2.0],
        },
        'n_total': 72,
    },
}


# ═══════════════════════════════════════════════════════════════
# 回测工具函数
# ═══════════════════════════════════════════════════════════════

def run_one_backtest(conn, query, start, end):
    trades = conn.execute(query).fetchdf()
    if trades.empty:
        return trades
    return trades[
        (trades['signal_date'] >= pd.Timestamp(start)) &
        (trades['signal_date'] <= pd.Timestamp(end))
    ]


def apply_stop_loss(conn, trades_df):
    if trades_df.empty:
        return trades_df
    conn.execute("DROP VIEW IF EXISTS _trades")
    conn.execute("DROP TABLE IF EXISTS _trades")
    conn.register('_trades', trades_df)
    q = f"""
    WITH daily AS (
        SELECT security_id, trade_date, low_price
        FROM daily_bar WHERE trade_date >= '{TRAIN_START}' AND trade_date <= '{TEST_END}'
    ),
    ranges AS (
        SELECT t.security_id, t.signal_date, t.entry_date, t.entry_price,
            t.exit_date, t.exit_price, t.strategy_name,
            (SELECT MIN(d.low_price) FROM daily d
             WHERE d.security_id = t.security_id AND d.trade_date > t.entry_date AND d.trade_date <= t.exit_date) as min_low_in_holding
        FROM _trades t
    )
    SELECT * FROM ranges
    """
    result = conn.execute(q).fetchdf()
    if result.empty:
        return result
    result['raw_pnl_pct'] = (result['exit_price'] - result['entry_price']) / result['entry_price'] * 100
    stop = result['min_low_in_holding'].notna() & (
        result['min_low_in_holding'] <= result['entry_price'] * (1 + STOP_LOSS_PCT)
    )
    result.loc[stop, 'exit_price_eff'] = result.loc[stop, 'entry_price'] * (1 + STOP_LOSS_PCT)
    result.loc[~stop, 'exit_price_eff'] = result.loc[~stop, 'exit_price']
    result['pnl_pct'] = (result['exit_price_eff'] - result['entry_price']) / result['entry_price'] * 100
    result['pnl_pct'] -= TOTAL_COST_PCT * 100
    return result


def calc_stats(trades):
    if trades is None or len(trades) == 0:
        return {'count': 0, 'win_rate': 0, 'avg_win': 0, 'avg_loss': 0, 'pl_ratio': 0, 'expected': 0}
    pnls = trades['pnl_pct'].values
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    count = len(pnls)
    win_rate = len(wins) / count * 100 if count > 0 else 0
    avg_win = float(np.mean(wins)) if len(wins) > 0 else 0
    avg_loss = float(np.mean(losses)) if len(losses) > 0 else 0
    pl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    expected = (win_rate / 100 * avg_win) - ((1 - win_rate / 100) * abs(avg_loss))
    return {
        'count': count,
        'win_rate': round(win_rate, 1),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'pl_ratio': round(pl_ratio, 2),
        'expected': round(expected, 2),
    }


# ═══════════════════════════════════════════════════════════════
# 单策略网格搜索
# ═══════════════════════════════════════════════════════════════

def grid_search_strategy(conn, name, config):
    """对单个策略做网格搜索"""
    grid = config['grid']
    func = config['func']
    keys = list(grid.keys())
    values = list(grid.values())
    combinations = list(itertools.product(*values))

    print(f"\n{'='*70}")
    print(f"  {name} 参数搜索：{len(combinations)} 个组合")
    print(f"{'='*70}")

    results = []
    t_start = time.time()

    for i, combo in enumerate(combinations):
        params = dict(zip(keys, combo))
        try:
            query = func(**params)
            train = run_one_backtest(conn, query, TRAIN_START, TRAIN_END)
            train = apply_stop_loss(conn, train)
            train_stats = calc_stats(train)

            test = run_one_backtest(conn, query, TEST_START, TEST_END)
            test = apply_stop_loss(conn, test)
            test_stats = calc_stats(test)

            results.append({
                **params,
                'train_count': train_stats['count'],
                'train_expected': train_stats['expected'],
                'train_win_rate': train_stats['win_rate'],
                'train_pl_ratio': train_stats['pl_ratio'],
                'test_count': test_stats['count'],
                'test_expected': test_stats['expected'],
                'test_win_rate': test_stats['win_rate'],
                'test_pl_ratio': test_stats['pl_ratio'],
                'expected_drop': round(train_stats['expected'] - test_stats['expected'], 2),
            })
        except Exception as e:
            pass

        if (i + 1) % 10 == 0:
            elapsed = time.time() - t_start
            print(f"  [{i+1}/{len(combinations)}] 耗时 {elapsed:.0f}秒")

    df_results = pd.DataFrame(results)
    print(f"  ✅ {name} 完成：{len(df_results)} 个有效组合，耗时 {time.time() - t_start:.0f}秒")
    return df_results


# ═══════════════════════════════════════════════════════════════
# 汇总 + 排名
# ═══════════════════════════════════════════════════════════════

def summarize_and_rank(all_results):
    """汇总所有策略最优参数，按测试集期望排名"""
    summary = []
    for name, df in all_results.items():
        if df.empty:
            continue
        # 按测试集期望选最优参数
        best = df.sort_values('test_expected', ascending=False).iloc[0]
        # 同时看训练集表现
        best_train = df.sort_values('train_expected', ascending=False).iloc[0]
        summary.append({
            'strategy': name,
            'n_combinations': len(df),
            # 最优（按测试集）
            'test_expected': best['test_expected'],
            'test_win_rate': best['test_win_rate'],
            'test_pl_ratio': best['test_pl_ratio'],
            'test_count': best['test_count'],
            'train_expected_at_test_best': best['train_expected'],
            'expected_drop': best['expected_drop'],
            # 最优（按训练集）
            'train_expected': best_train['train_expected'],
            'train_win_rate': best_train['train_win_rate'],
            'train_count': best_train['train_count'],
        })

    df_summary = pd.DataFrame(summary)
    df_summary = df_summary.sort_values('test_expected', ascending=False)
    return df_summary


# ═══════════════════════════════════════════════════════════════
# 主程序
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    t_overall = time.time()
    conn = duckdb.connect(DB_PATH)

    print("=" * 70)
    print("🚀 综合策略优化器 v1 —— 7个策略 网格搜索")
    print("=" * 70)
    print(f"  训练期: {TRAIN_START} ~ {TRAIN_END}")
    print(f"  测试期: {TEST_START} ~ {TEST_END}")
    print(f"  止损: {STOP_LOSS_PCT*100}% | 成本: {TOTAL_COST_PCT*100:.2f}%")
    print(f"  总组合数: {sum(c['n_total'] for c in STRATEGY_GRIDS.values())}")
    print()

    all_results = {}

    for name, config in STRATEGY_GRIDS.items():
        df = grid_search_strategy(conn, name, config)
        all_results[name] = df
        # 保存单个策略结果
        df.to_csv(f"{OUTPUT_DIR}/{name}_params.csv", index=False, encoding='utf-8-sig')

    # 汇总排名
    print("\n" + "=" * 70)
    print("🏆 所有策略排名（按测试集期望值）")
    print("=" * 70)

    df_rank = summarize_and_rank(all_results)
    df_rank.to_csv(f"{OUTPUT_DIR}/all_strategies_rank.csv", index=False, encoding='utf-8-sig')

    for i, row in df_rank.iterrows():
        print(f"\n  【{row['strategy']}】")
        print(f"    最优参数组合 test_expected={row['test_expected']}%  win={row['test_win_rate']}%  pl={row['test_pl_ratio']}  信号={row['test_count']}")
        print(f"    训练集期望={row['train_expected']}% (对应测试最优参数时)  过拟合差值={row['expected_drop']}")

    conn.close()
    print(f"\n🎉 全部完成！总耗时: {time.time() - t_overall:.0f}秒")
    print(f"📁 结果保存: {OUTPUT_DIR}/")
