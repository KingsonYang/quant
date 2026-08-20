"""
参数优化引擎 v5
- 对每个策略做网格搜索
- 评估指标：胜率、均盈、均亏、盈亏比、期望值、信号数
- 防过拟合：训练集/测试集分离
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
import json
from datetime import datetime

# 强制 stdout 立即刷新
import functools
print = functools.partial(print, flush=True)

DB_PATH = "D:/Files/workspace-ai/quant/data/quant.duckdb"
OUTPUT_DIR = "D:/Files/workspace-ai/quant/data/param_results"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ═══════════════════════════════════════════════════════════
# 公共参数
# ═══════════════════════════════════════════════════════════

COMMISSION_RATE = 0.0003
STAMP_TAX = 0.001
SLIPPAGE_PCT = 0.001
TOTAL_COST_PCT = (COMMISSION_RATE * 2 + STAMP_TAX + SLIPPAGE_PCT * 2)
STOP_LOSS_PCT = -0.03
TRAIN_START = "2024-01-01"
TRAIN_END = "2025-12-31"
TEST_START = "2026-01-01"
TEST_END = "2026-06-01"
HOLDING_DAYS_LIST = [3, 5]

# ═══════════════════════════════════════════════════════════
# 股票池查询
# ═══════════════════════════════════════════════════════════

def get_stock_pool(conn, max_stocks=3000):
    pool = conn.execute("""
        SELECT DISTINCT s.security_id
        FROM security_master s
        INNER JOIN (
            SELECT security_id, COUNT(*) as bar_count
            FROM daily_bar
            WHERE trade_date >= '2024-01-01'
            GROUP BY security_id
        ) b ON s.security_id = b.security_id
        WHERE s.list_status = 'listed'
          AND s.symbol NOT LIKE 'SZ300%'
          AND s.symbol NOT LIKE 'SH688%'
          AND s.name NOT LIKE '%ST%'
          AND s.name NOT LIKE '%*%'
          AND b.bar_count > 200
        ORDER BY s.security_id
        LIMIT ?
    """, [max_stocks]).fetchall()
    return [int(r[0]) for r in pool]


# ═══════════════════════════════════════════════════════════
# 可参数化的策略 SQL 生成器
# ═══════════════════════════════════════════════════════════

def build_old_duck_query(holding_days, ma_strict, volume_mult,
                         had_below_window, amount_filter_yi):
    """老鸭头策略 SQL（参数化）"""
    return f"""
    WITH base AS (
        SELECT *,
            ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
        FROM daily_bar
        WHERE trade_date >= '{TRAIN_START}' AND trade_date <= '{TEST_END}'
    ),
    kline AS (
        SELECT *,
            AVG(close_price) OVER w5 as ma5,
            AVG(close_price) OVER w10 as ma10,
            AVG(close_price) OVER w20 as ma20,
            AVG(volume) OVER w5 as vol_ma5
        FROM base
        WINDOW
            w5 AS (PARTITION BY security_id ORDER BY day_idx ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
            w10 AS (PARTITION BY security_id ORDER BY day_idx ROWS BETWEEN 9 PRECEDING AND CURRENT ROW),
            w20 AS (PARTITION BY security_id ORDER BY day_idx ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
    ),
    kline_lag AS (
        SELECT *,
            LAG(ma5, 1) OVER (PARTITION BY security_id ORDER BY day_idx) as ma5_prev,
            LAG(ma10, 1) OVER (PARTITION BY security_id ORDER BY day_idx) as ma10_prev,
            MAX(CASE WHEN ma5 < ma10 THEN 1 ELSE 0 END) OVER (
                PARTITION BY security_id ORDER BY day_idx
                ROWS BETWEEN {had_below_window + 1} PRECEDING AND 1 PRECEDING
            ) as had_below_recently
        FROM kline
    ),
    signals AS (
        SELECT *
        FROM kline_lag
        WHERE ma5 > ma10
          AND ma5_prev <= ma10_prev
          AND ma5 > ma5_prev * (1 + {ma_strict})
          AND close_price > ma20
          AND volume > vol_ma5 * {volume_mult}
          AND had_below_recently = 1
          AND amount >= {amount_filter_yi * 1e8}
    ),
    all_bars AS (
        SELECT security_id, day_idx, trade_date, open_price, close_price, change_pct
        FROM base
    )
    SELECT
        s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
        e.trade_date as entry_date, e.open_price as entry_price, e.change_pct as entry_change_pct,
        x.trade_date as exit_date, x.close_price as exit_price
    FROM signals s
    LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
    LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {holding_days + 1}
    WHERE e.trade_date IS NOT NULL
      AND e.open_price > 0
      AND COALESCE(e.change_pct, 0) < 9.5
      AND x.trade_date IS NOT NULL
    """


def build_520_query(holding_days, ma_strict, volume_mult, amount_filter_yi, need_trend):
    """520战法 SQL（参数化）"""
    trend_cond = "AND ma5 > ma10 AND ma10 > ma20" if need_trend else ""
    return f"""
    WITH base AS (
        SELECT *,
            ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
        FROM daily_bar
        WHERE trade_date >= '{TRAIN_START}' AND trade_date <= '{TEST_END}'
    ),
    kline AS (
        SELECT *,
            AVG(close_price) OVER w5 as ma5,
            AVG(close_price) OVER w10 as ma10,
            AVG(close_price) OVER w20 as ma20,
            AVG(volume) OVER w5 as vol_ma5
        FROM base
        WINDOW
            w5 AS (PARTITION BY security_id ORDER BY day_idx ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
            w10 AS (PARTITION BY security_id ORDER BY day_idx ROWS BETWEEN 9 PRECEDING AND CURRENT ROW),
            w20 AS (PARTITION BY security_id ORDER BY day_idx ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
    ),
    kline_with_lag AS (
        SELECT *,
            LAG(close_price, 1) OVER (PARTITION BY security_id ORDER BY day_idx) as ma5_prev
        FROM kline
    ),
    signals AS (
        SELECT *
        FROM kline_with_lag
        WHERE ma5 > ma5_prev * (1 + {ma_strict})
          AND volume > vol_ma5 * {volume_mult}
          AND amount >= {amount_filter_yi * 1e8}
          {trend_cond}
    ),
    all_bars AS (
        SELECT security_id, day_idx, trade_date, open_price, close_price, change_pct
        FROM base
    )
    SELECT
        s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
        e.trade_date as entry_date, e.open_price as entry_price, e.change_pct as entry_change_pct,
        x.trade_date as exit_date, x.close_price as exit_price
    FROM signals s
    LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
    LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {holding_days + 1}
    WHERE e.trade_date IS NOT NULL
      AND e.open_price > 0
      AND COALESCE(e.change_pct, 0) < 9.5
      AND x.trade_date IS NOT NULL
    """


def build_vol_surge_query(holding_days, vol_mult, pullback1, pullback2,
                          change_min, change_max, amount_filter_yi):
    """倍量回调 SQL（参数化）"""
    return f"""
    WITH base AS (
        SELECT *,
            ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
        FROM daily_bar
        WHERE trade_date >= '{TRAIN_START}' AND trade_date <= '{TEST_END}'
    ),
    kline AS (
        SELECT *,
            LAG(volume, 1) OVER (PARTITION BY security_id ORDER BY day_idx) as vol_1,
            LAG(volume, 2) OVER (PARTITION BY security_id ORDER BY day_idx) as vol_2,
            AVG(close_price) OVER (PARTITION BY security_id ORDER BY day_idx ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as ma5
        FROM base
    ),
    signals AS (
        SELECT *
        FROM kline
        WHERE change_pct >= {change_min} AND change_pct <= {change_max}
          AND vol_1 IS NOT NULL AND vol_1 < volume * {pullback1}
          AND vol_2 IS NOT NULL AND vol_2 < volume * {pullback2}
          AND close_price > ma5
          AND volume > (SELECT AVG(volume) FROM kline k2 WHERE k2.security_id = kline.security_id AND k2.day_idx BETWEEN kline.day_idx - 5 AND kline.day_idx - 1) * {vol_mult}
          AND amount >= {amount_filter_yi * 1e8}
    ),
    all_bars AS (
        SELECT security_id, day_idx, trade_date, open_price, close_price, change_pct
        FROM base
    )
    SELECT
        s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
        e.trade_date as entry_date, e.open_price as entry_price, e.change_pct as entry_change_pct,
        x.trade_date as exit_date, x.close_price as exit_price
    FROM signals s
    LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
    LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {holding_days + 1}
    WHERE e.trade_date IS NOT NULL
      AND e.open_price > 0
      AND COALESCE(e.change_pct, 0) < 9.5
      AND x.trade_date IS NOT NULL
    """


# ═══════════════════════════════════════════════════════════
# 通用：跑一次回测 + 统计
# ═══════════════════════════════════════════════════════════

def run_one_backtest(conn, query, start_date_filter, end_date_filter):
    """跑一次回测，返回 DataFrame（带止损）"""
    trades = conn.execute(query).fetchdf()
    if trades.empty:
        return trades
    # 时间段过滤
    trades = trades[
        (trades['signal_date'] >= pd.Timestamp(start_date_filter)) &
        (trades['signal_date'] <= pd.Timestamp(end_date_filter))
    ]
    if trades.empty:
        return trades
    return trades


def apply_stop_loss_and_pnl(conn, trades_df):
    """对交易应用止损 + 计算 PnL"""
    if trades_df.empty:
        return trades_df

    conn.execute("DROP VIEW IF EXISTS _trades")
    conn.execute("DROP TABLE IF EXISTS _trades")
    conn.register('_trades', trades_df)

    query = f"""
    WITH daily AS (
        SELECT security_id, trade_date, low_price, close_price
        FROM daily_bar
        WHERE trade_date >= '{TRAIN_START}' AND trade_date <= '{TEST_END}'
    ),
    ranges AS (
        SELECT
            t.security_id, t.signal_date, t.entry_date, t.entry_price,
            t.exit_date, t.exit_price, t.entry_change_pct,
            (SELECT MIN(d.low_price) FROM daily d
             WHERE d.security_id = t.security_id
               AND d.trade_date > t.entry_date
               AND d.trade_date <= t.exit_date) as min_low_in_holding
        FROM _trades t
    )
    SELECT * FROM ranges
    """
    result = conn.execute(query).fetchdf()

    if result.empty:
        return result

    # 计算收益（含止损）
    result['raw_pnl_pct'] = (result['exit_price'] - result['entry_price']) / result['entry_price'] * 100
    stop_triggered = result['min_low_in_holding'].notna() & (
        result['min_low_in_holding'] <= result['entry_price'] * (1 + STOP_LOSS_PCT)
    )
    result.loc[stop_triggered, 'exit_price_eff'] = result.loc[stop_triggered, 'entry_price'] * (1 + STOP_LOSS_PCT)
    result.loc[~stop_triggered, 'exit_price_eff'] = result.loc[~stop_triggered, 'exit_price']
    result['pnl_pct'] = (result['exit_price_eff'] - result['entry_price']) / result['entry_price'] * 100
    result['pnl_pct'] -= TOTAL_COST_PCT * 100
    result['pnl_amount'] = 10000 * result['pnl_pct'] / 100

    return result


def calc_stats(trades, label=""):
    if trades is None or len(trades) == 0:
        return {'label': label, 'count': 0, 'win_rate': 0, 'avg_win': 0,
                'avg_loss': 0, 'pl_ratio': 0, 'expected': 0, 'total_pnl': 0}

    pnls = trades['pnl_pct'].values
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    count = len(pnls)
    win_rate = len(wins) / count * 100
    avg_win = float(np.mean(wins)) if len(wins) else 0
    avg_loss = float(np.mean(losses)) if len(losses) else 0
    pl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    expected = (win_rate / 100 * avg_win) - ((1 - win_rate / 100) * abs(avg_loss))
    total = float(np.sum(pnls))

    return {
        'label': label, 'count': count,
        'win_rate': round(win_rate, 1),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'pl_ratio': round(pl_ratio, 2),
        'expected': round(expected, 2),
        'total_pnl': round(total, 2),
    }


# ═══════════════════════════════════════════════════════════
# 参数网格搜索
# ═══════════════════════════════════════════════════════════

def grid_search_old_duck(conn):
    """老鸭头参数搜索"""
    # 参数空间
    param_grid = {
        'holding_days': [3, 5],
        'ma_strict': [0.001, 0.002, 0.005],   # MA5 上穿严格度
        'volume_mult': [1.0, 1.2, 1.5],        # 放量倍数
        'had_below_window': [5, 10],           # 找死叉窗口
        'amount_filter_yi': [1.0, 2.0],        # 成交额亿元
    }

    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combinations = list(itertools.product(*values))

    print(f"\n{'='*70}")
    print(f"🐤 老鸭头参数搜索：{len(combinations)} 个组合")
    print(f"{'='*70}")

    results = []
    t_start = time.time()

    for i, combo in enumerate(combinations):
        params = dict(zip(keys, combo))
        # 构建 SQL
        query = build_old_duck_query(
            holding_days=params['holding_days'],
            ma_strict=params['ma_strict'],
            volume_mult=params['volume_mult'],
            had_below_window=params['had_below_window'],
            amount_filter_yi=params['amount_filter_yi'],
        )
        try:
            # 训练集评估
            train_trades = run_one_backtest(conn, query, TRAIN_START, TRAIN_END)
            train_trades = apply_stop_loss_and_pnl(conn, train_trades)
            train_stats = calc_stats(train_trades, label="train")

            # 测试集评估
            test_trades = run_one_backtest(conn, query, TEST_START, TEST_END)
            test_trades = apply_stop_loss_and_pnl(conn, test_trades)
            test_stats = calc_stats(test_trades, label="test")

            results.append({
                **params,
                'train_count': train_stats['count'],
                'train_win_rate': train_stats['win_rate'],
                'train_avg_win': train_stats['avg_win'],
                'train_avg_loss': train_stats['avg_loss'],
                'train_pl_ratio': train_stats['pl_ratio'],
                'train_expected': train_stats['expected'],
                'test_count': test_stats['count'],
                'test_win_rate': test_stats['win_rate'],
                'test_avg_win': test_stats['avg_win'],
                'test_avg_loss': test_stats['avg_loss'],
                'test_pl_ratio': test_stats['pl_ratio'],
                'test_expected': test_stats['expected'],
                # 防过拟合：训练和测试差异
                'expected_drop': train_stats['expected'] - test_stats['expected'],
            })
        except Exception as e:
            print(f"  [{i+1}/{len(combinations)}] 参数{params} 失败: {e}")
            continue

        if (i + 1) % 10 == 0:
            elapsed = time.time() - t_start
            print(f"  [{i+1}/{len(combinations)}] 耗时 {elapsed:.0f}秒")

    df_results = pd.DataFrame(results)
    print(f"\n✅ 老鸭头参数搜索完成：{len(df_results)} 个有效组合")
    print(f"⏱️ 总耗时: {time.time() - t_start:.0f}秒")

    return df_results


def grid_search_520(conn):
    """520战法参数搜索"""
    param_grid = {
        'holding_days': [3, 5],
        'ma_strict': [0.001, 0.002, 0.005],
        'volume_mult': [1.0, 1.2, 1.5],
        'amount_filter_yi': [1.0, 2.0],
        'need_trend': [True, False],  # 是否要求多头排列
    }

    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combinations = list(itertools.product(*values))

    print(f"\n{'='*70}")
    print(f"📊 520战法参数搜索：{len(combinations)} 个组合")
    print(f"{'='*70}")

    results = []
    t_start = time.time()

    for i, combo in enumerate(combinations):
        params = dict(zip(keys, combo))
        query = build_520_query(
            holding_days=params['holding_days'],
            ma_strict=params['ma_strict'],
            volume_mult=params['volume_mult'],
            amount_filter_yi=params['amount_filter_yi'],
            need_trend=params['need_trend'],
        )
        try:
            train_trades = run_one_backtest(conn, query, TRAIN_START, TRAIN_END)
            train_trades = apply_stop_loss_and_pnl(conn, train_trades)
            train_stats = calc_stats(train_trades)

            test_trades = run_one_backtest(conn, query, TEST_START, TEST_END)
            test_trades = apply_stop_loss_and_pnl(conn, test_trades)
            test_stats = calc_stats(test_trades)

            results.append({
                **params,
                'train_count': train_stats['count'],
                'train_win_rate': train_stats['win_rate'],
                'train_expected': train_stats['expected'],
                'test_count': test_stats['count'],
                'test_win_rate': test_stats['win_rate'],
                'test_expected': test_stats['expected'],
                'expected_drop': train_stats['expected'] - test_stats['expected'],
            })
        except Exception as e:
            continue

        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(combinations)}] 耗时 {time.time() - t_start:.0f}秒")

    df_results = pd.DataFrame(results)
    print(f"\n✅ 520战法参数搜索完成：{len(df_results)} 个有效组合")
    return df_results


def grid_search_vol_surge(conn):
    """倍量回调参数搜索"""
    param_grid = {
        'holding_days': [3, 5],
        'vol_mult': [1.5, 2.0, 2.5],
        'pullback1': [0.6, 0.8],
        'pullback2': [0.4, 0.6],
        'change_min': [2.0, 3.0, 5.0],
        'change_max': [8.0, 10.0],
        'amount_filter_yi': [1.0, 2.0],
    }

    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combinations = list(itertools.product(*values))

    print(f"\n{'='*70}")
    print(f"💥 倍量回调参数搜索：{len(combinations)} 个组合")
    print(f"{'='*70}")

    results = []
    t_start = time.time()

    for i, combo in enumerate(combinations):
        params = dict(zip(keys, combo))
        query = build_vol_surge_query(
            holding_days=params['holding_days'],
            vol_mult=params['vol_mult'],
            pullback1=params['pullback1'],
            pullback2=params['pullback2'],
            change_min=params['change_min'],
            change_max=params['change_max'],
            amount_filter_yi=params['amount_filter_yi'],
        )
        try:
            train_trades = run_one_backtest(conn, query, TRAIN_START, TRAIN_END)
            train_trades = apply_stop_loss_and_pnl(conn, train_trades)
            train_stats = calc_stats(train_trades)

            test_trades = run_one_backtest(conn, query, TEST_START, TEST_END)
            test_trades = apply_stop_loss_and_pnl(conn, test_trades)
            test_stats = calc_stats(test_trades)

            results.append({
                **params,
                'train_count': train_stats['count'],
                'train_win_rate': train_stats['win_rate'],
                'train_expected': train_stats['expected'],
                'test_count': test_stats['count'],
                'test_win_rate': test_stats['win_rate'],
                'test_expected': test_stats['expected'],
                'expected_drop': train_stats['expected'] - test_stats['expected'],
            })
        except Exception as e:
            continue

        if (i + 1) % 20 == 0:
            print(f"  [{i+1}/{len(combinations)}] 耗时 {time.time() - t_start:.0f}秒")

    df_results = pd.DataFrame(results)
    print(f"\n✅ 倍量回调参数搜索完成：{len(df_results)} 个有效组合")
    return df_results


# ═══════════════════════════════════════════════════════════
# 主程序
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    t_overall = time.time()
    conn = duckdb.connect(DB_PATH)

    pool = get_stock_pool(conn)
    print(f"📋 全A股池：{len(pool)} 只")

    # 1. 老鸭头
    print("\n" + "="*70)
    print("🐤 阶段 1/3: 老鸭头参数搜索")
    print("="*70)
    df_old_duck = grid_search_old_duck(conn)
    df_old_duck.to_csv(f"{OUTPUT_DIR}/old_duck_params.csv", index=False, encoding='utf-8-sig')

    # 显示 Top 5
    print("\n老鸭头 TOP 5（按 train_expected 排序）:")
    top5 = df_old_duck.sort_values('train_expected', ascending=False).head(5)
    print(top5[['holding_days', 'ma_strict', 'volume_mult', 'had_below_window',
                'amount_filter_yi', 'train_count', 'train_expected',
                'test_count', 'test_expected', 'expected_drop']].to_string(index=False))

    # 2. 520战法
    print("\n" + "="*70)
    print("📊 阶段 2/3: 520战法参数搜索")
    print("="*70)
    df_520 = grid_search_520(conn)
    df_520.to_csv(f"{OUTPUT_DIR}/520_params.csv", index=False, encoding='utf-8-sig')

    print("\n520战法 TOP 5:")
    top5 = df_520.sort_values('train_expected', ascending=False).head(5)
    print(top5[['holding_days', 'ma_strict', 'volume_mult', 'amount_filter_yi',
                'need_trend', 'train_count', 'train_expected',
                'test_count', 'test_expected', 'expected_drop']].to_string(index=False))

    # 3. 倍量回调
    print("\n" + "="*70)
    print("💥 阶段 3/3: 倍量回调参数搜索")
    print("="*70)
    df_vol = grid_search_vol_surge(conn)
    df_vol.to_csv(f"{OUTPUT_DIR}/vol_surge_params.csv", index=False, encoding='utf-8-sig')

    print("\n倍量回调 TOP 5:")
    top5 = df_vol.sort_values('train_expected', ascending=False).head(5)
    print(top5[['holding_days', 'vol_mult', 'pullback1', 'pullback2',
                'change_min', 'change_max', 'amount_filter_yi',
                'train_count', 'train_expected', 'test_count', 'test_expected']].to_string(index=False))

    conn.close()
    print(f"\n🎉 全部完成！总耗时: {time.time() - t_overall:.0f}秒")
    print(f"📁 结果保存: {OUTPUT_DIR}/")
