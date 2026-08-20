"""
组合回测器 v1
每天运行所有策略，评分排序取TOP1，用止损系统执行
对比：单策略 vs 多策略组合
"""
import sys
sys.path.insert(0, 'D:/SoftWare/tdx/PYPlugins/user')
import os
os.environ['TDXPATH'] = 'D:/SoftWare/tdx/TdxW.exe'

import duckdb
import pandas as pd
import numpy as np
import time
from signal_scorer import SignalScorer, get_strategy_params

DB_PATH = "D:/Files/workspace-ai/quant/data/quant.duckdb"
OUTPUT_DIR = "D:/Files/workspace-ai/quant/data/backtest_v5"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 交易成本
COMMISSION_RATE = 0.0003
STAMP_TAX = 0.001
SLIPPAGE_PCT = 0.001
TOTAL_COST_PCT = (COMMISSION_RATE * 2 + STAMP_TAX + SLIPPAGE_PCT * 2)
STOP_LOSS_PCT = -0.03
TEST_START = "2026-01-01"
TEST_END = "2026-06-01"


# ═══════════════════════════════════════════════════════════
# SQL 构建器（使用最优参数）
# ═══════════════════════════════════════════════════════════

def build_old_duck_signal_query(params):
    return f"""
    WITH base AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
        FROM daily_bar WHERE trade_date >= '{TEST_START}' AND trade_date <= '{TEST_END}'
    ),
    kline AS (
        SELECT *,
            AVG(close_price) OVER w5 as ma5, AVG(close_price) OVER w10 as ma10,
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
                ROWS BETWEEN {params['had_below_window'] + 1} PRECEDING AND 1 PRECEDING) as had_below_recently
        FROM kline
    ),
    signals AS (
        SELECT * FROM kline_lag
        WHERE ma5 > ma10 AND ma5_prev <= ma10_prev
          AND ma5 > ma5_prev * (1 + {params['ma_strict']})
          AND close_price > ma20
          AND volume > vol_ma5 * {params['volume_mult']}
          AND had_below_recently = 1
          AND amount >= {params['amount_filter_yi'] * 1e8}
    ),
    all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, change_pct FROM base)
    SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
        e.trade_date as entry_date, e.open_price as entry_price, e.change_pct as entry_change_pct,
        x.trade_date as exit_date, x.close_price as exit_price,
        'old_duck' as strategy_name
    FROM signals s
    LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
    LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {params['holding_days'] + 1}
    WHERE e.trade_date IS NOT NULL AND e.open_price > 0
      AND COALESCE(e.change_pct, 0) < 9.5 AND x.trade_date IS NOT NULL
    """


def build_520_signal_query(params):
    trend_cond = "AND ma5 > ma10 AND ma10 > ma20" if params['need_trend'] else ""
    return f"""
    WITH base AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
        FROM daily_bar WHERE trade_date >= '{TEST_START}' AND trade_date <= '{TEST_END}'
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
        WHERE ma5 > ma5_prev * (1 + {params['ma_strict']})
          AND volume > vol_ma5 * {params['volume_mult']}
          AND amount >= {params['amount_filter_yi'] * 1e8} {trend_cond}
    ),
    all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, change_pct FROM base)
    SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
        e.trade_date as entry_date, e.open_price as entry_price, e.change_pct as entry_change_pct,
        x.trade_date as exit_date, x.close_price as exit_price,
        '520' as strategy_name
    FROM signals s
    LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
    LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {params['holding_days'] + 1}
    WHERE e.trade_date IS NOT NULL AND e.open_price > 0
      AND COALESCE(e.change_pct, 0) < 9.5 AND x.trade_date IS NOT NULL
    """


def build_vol_surge_signal_query(params):
    return f"""
    WITH base AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
        FROM daily_bar WHERE trade_date >= '{TEST_START}' AND trade_date <= '{TEST_END}'
    ),
    kline AS (
        SELECT *, LAG(volume, 1) OVER (PARTITION BY security_id ORDER BY day_idx) as vol_1,
            LAG(volume, 2) OVER (PARTITION BY security_id ORDER BY day_idx) as vol_2,
            AVG(close_price) OVER (PARTITION BY security_id ORDER BY day_idx ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as ma5
        FROM base
    ),
    signals AS (
        SELECT * FROM kline
        WHERE change_pct >= {params['change_min']} AND change_pct <= {params['change_max']}
          AND vol_1 IS NOT NULL AND vol_1 < volume * {params['pullback1']}
          AND vol_2 IS NOT NULL AND vol_2 < volume * {params['pullback2']}
          AND close_price > ma5
          AND volume > (SELECT AVG(volume) FROM kline k2 WHERE k2.security_id = kline.security_id AND k2.day_idx BETWEEN kline.day_idx - 5 AND kline.day_idx - 1) * {params['vol_mult']}
          AND amount >= {params['amount_filter_yi'] * 1e8}
    ),
    all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, change_pct FROM base)
    SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
        e.trade_date as entry_date, e.open_price as entry_price, e.change_pct as entry_change_pct,
        x.trade_date as exit_date, x.close_price as exit_price,
        'vol_surge' as strategy_name
    FROM signals s
    LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
    LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {params['holding_days'] + 1}
    WHERE e.trade_date IS NOT NULL AND e.open_price > 0
      AND COALESCE(e.change_pct, 0) < 9.5 AND x.trade_date IS NOT NULL
    """


# ═══════════════════════════════════════════════════════════
# 止损应用
# ═══════════════════════════════════════════════════════════

def apply_stop_loss(conn, trades_df):
    """应用止损，返回含pnl的DataFrame"""
    if trades_df.empty:
        return trades_df

    conn.execute("DROP VIEW IF EXISTS _trades_v2")
    conn.execute("DROP TABLE IF EXISTS _trades_v2")
    conn.register('_trades_v2', trades_df)

    query = f"""
    WITH daily AS (
        SELECT security_id, trade_date, low_price
        FROM daily_bar
        WHERE trade_date >= '{TEST_START}' AND trade_date <= '{TEST_END}'
    ),
    ranges AS (
        SELECT t.security_id, t.signal_date, t.entry_date, t.entry_price,
            t.exit_date, t.exit_price, t.entry_change_pct, t.strategy_name,
            (SELECT MIN(d.low_price) FROM daily d
             WHERE d.security_id = t.security_id
               AND d.trade_date > t.entry_date
               AND d.trade_date <= t.exit_date) as min_low_in_holding
        FROM _trades_v2 t
    )
    SELECT * FROM ranges
    """
    result = conn.execute(query).fetchdf()

    if result.empty:
        return result

    result['raw_pnl_pct'] = (result['exit_price'] - result['entry_price']) / result['entry_price'] * 100
    stop_triggered = result['min_low_in_holding'].notna() & (
        result['min_low_in_holding'] <= result['entry_price'] * (1 + STOP_LOSS_PCT)
    )
    result.loc[stop_triggered, 'exit_price_eff'] = result.loc[stop_triggered, 'entry_price'] * (1 + STOP_LOSS_PCT)
    result.loc[~stop_triggered, 'exit_price_eff'] = result.loc[~stop_triggered, 'exit_price']
    result['pnl_pct'] = (result['exit_price_eff'] - result['entry_price']) / result['entry_price'] * 100
    result['pnl_pct'] -= TOTAL_COST_PCT * 100
    result['stop_triggered'] = stop_triggered

    return result


# ═══════════════════════════════════════════════════════════
# 统计分析
# ═══════════════════════════════════════════════════════════

def calc_stats(trades, label=""):
    if trades is None or len(trades) == 0:
        return {'label': label, 'count': 0, 'win_rate': 0, 'avg_win': 0,
                'avg_loss': 0, 'pl_ratio': 0, 'expected': 0, 'total_pnl': 0}

    pnls = trades['pnl_pct'].values
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    count = len(pnls)
    win_rate = len(wins) / count * 100 if count > 0 else 0
    avg_win = float(np.mean(wins)) if len(wins) > 0 else 0
    avg_loss = float(np.mean(losses)) if len(losses) > 0 else 0
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
# 主程序
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    t_start = time.time()
    conn = duckdb.connect(DB_PATH)
    scorer = SignalScorer(conn)
    params = get_strategy_params()

    # 1. 收集所有策略信号
    print("=" * 70)
    print("📡 收集各策略信号...")
    print("=" * 70)

    all_signals = []

    for name, p in [('old_duck', params['old_duck']),
                     ('520', params['520']),
                     ('vol_surge', params['vol_surge'])]:
        if name == 'old_duck':
            q = build_old_duck_signal_query(p)
        elif name == '520':
            q = build_520_signal_query(p)
        else:
            q = build_vol_surge_signal_query(p)

        df = conn.execute(q).fetchdf()
        print(f"  {name}: {len(df)} 条信号")
        all_signals.append(df)

    signals_df = pd.concat(all_signals, ignore_index=True)
    print(f"\n  总信号数: {len(signals_df)}")

    # 2. 评分排序
    print("\n" + "=" * 70)
    print("🏆 评分 + 每日选TOP1...")
    print("=" * 70)
    scored = scorer.score(signals_df)

    # 3. 每日只取评分最高的那个
    # 先按日期分组，每只股票可能同日有多策略信号，取score最高的
    scored['_entry_date'] = pd.to_datetime(scored['entry_date'])
    daily_top1 = scored.loc[
        scored.groupby(['_entry_date', 'security_id'])['score'].idxmax()
    ]

    # 同一日如果还有冲突（同一策略同一日多股），取score最高的
    daily_top1 = daily_top1.loc[
        daily_top1.groupby('_entry_date')['score'].idxmax()
    ]

    print(f"  每日TOP1后: {len(daily_top1)} 条信号")

    # 4. 应用止损
    trades_with_pnl = apply_stop_loss(conn, daily_top1)

    # 5. 单策略回测（对比）
    print("\n" + "=" * 70)
    print("📊 策略对比（测试集 2026-01 至 2026-06）")
    print("=" * 70)

    results = []
    for strat in ['old_duck', '520', 'vol_surge']:
        strat_trades = apply_stop_loss(conn, signals_df[signals_df['strategy_name'] == strat])
        stats = calc_stats(strat_trades, label=strat)
        results.append(stats)
        print(f"\n  【{strat}】")
        print(f"    信号数: {stats['count']}")
        print(f"    胜率: {stats['win_rate']}%")
        print(f"    均盈: {stats['avg_win']}%")
        print(f"    均亏: {stats['avg_loss']}%")
        print(f"    盈亏比: {stats['pl_ratio']}")
        print(f"    期望值: {stats['expected']}%")

    # 6. 组合TOP1回测
    combo_stats = calc_stats(trades_with_pnl, label="组合TOP1")
    print(f"\n  【组合TOP1】")
    print(f"    信号数: {combo_stats['count']}")
    print(f"    胜率: {combo_stats['win_rate']}%")
    print(f"    均盈: {combo_stats['avg_win']}%")
    print(f"    均亏: {combo_stats['avg_loss']}%")
    print(f"    盈亏比: {combo_stats['pl_ratio']}")
    print(f"    期望值: {combo_stats['expected']}%")
    results.append(combo_stats)

    # 7. 保存结果
    trades_with_pnl.to_csv(f"{OUTPUT_DIR}/combined_signals.csv", index=False, encoding='utf-8-sig')
    df_results = pd.DataFrame(results)
    df_results.to_csv(f"{OUTPUT_DIR}/final_summary.csv", index=False, encoding='utf-8-sig')

    # 8. 显示组合里各策略占比
    print("\n" + "=" * 70)
    print("📈 组合TOP1的策略分布")
    print("=" * 70)
    strat_dist = trades_with_pnl['strategy_name'].value_counts()
    for strat, cnt in strat_dist.items():
        print(f"  {strat}: {cnt} ({cnt/len(trades_with_pnl)*100:.1f}%)")

    conn.close()
    print(f"\n⏱️ 耗时: {time.time() - t_start:.0f}秒")
    print(f"📁 结果保存: {OUTPUT_DIR}/")
