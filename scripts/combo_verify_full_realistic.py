"""
多策略组合回测 - 支持参数化和移动止损
用法:
  python scripts/combo_verify_full_realistic.py --start 2020-01-01 --end 2026-06-03
  python scripts/combo_verify_full_realistic.py --ma-slow 20 --holding-days 8 --stop-pct 0.98 --use-trailing-stop
  python scripts/combo_verify_full_realistic.py --start 2022-01-01 --end 2024-01-01 --output-suffix熊市
"""
import duckdb
import pandas as pd
import numpy as np
import argparse
import datetime

DB_PATH = 'data/quant.duckdb'

# ── 参数解析 ──
parser = argparse.ArgumentParser()
parser.add_argument('--start',   default='2020-01-01', help='回测开始日期')
parser.add_argument('--end',     default='2026-06-03', help='回测结束日期')
parser.add_argument('--holding-days', type=int, default=5,  help='持仓天数（交易日）')
parser.add_argument('--ma-slow', type=int, default=60,     help='MA慢线周期（默认60，测试MA20用20）')
parser.add_argument('--stop-pct', type=float, default=0.97, help='止损线（默认0.97=-3%%）')
parser.add_argument('--use-trailing-stop', action='store_true', help='是否用移动止损（每日检查，提前止损）')
parser.add_argument('--output-suffix', default='',            help='输出文件后缀')
parser.add_argument('--capital',   type=float, default=100000.0, help='初始资金')
args = parser.parse_args()

INITIAL_CAPITAL  = args.capital
TEST_START       = args.start
TEST_END         = args.end
HOLDING_DAYS     = args.holding_days   # 持仓天数（交易日）
MA_SLOW          = args.ma_slow       # MA慢线周期
STOP_PCT         = args.stop_pct      # 止损线
USE_TRAILING     = args.use_trailing_stop
SUFFIX          = args.output_suffix

COST_PCT = 0.56   # 佣金0.03%×2 + 印花税0.1% + 滑点0.1%×2
MAX_POSITIONS = 2

conn = duckdb.connect(DB_PATH)
conn.execute("SET enable_progress_bar=false")

print(f"参数: MA_SLOW={MA_SLOW} HOLDING={HOLDING_DAYS} STOP={STOP_PCT} TRAILING={USE_TRAILING}")
print(f"区间: {TEST_START} ~ {TEST_END}")
print(f"{'='*60}\n")

# ── 动态SQL生成：根据MA慢线周期 ──
ma_rows = MA_SLOW - 1   # ROWS BETWEEN N PRECEDING = N+1窗口

# ── MA_CROSS ──
q_ma = f"""
WITH base AS (
    SELECT security_id, trade_date, close_price, open_price, low_price, volume, amount, change_pct,
        ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
    FROM daily_bar
    WHERE trade_date >= ? AND trade_date <= ?
),
price_ma AS (
    SELECT b.security_id, b.trade_date, b.day_idx, b.close_price, b.volume, b.amount, b.change_pct,
        AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as ma5,
        AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN {ma_rows} PRECEDING AND CURRENT ROW) as ma_slow,
        AVG(b.volume) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as vol_ma5
    FROM base b WHERE b.day_idx > {MA_SLOW + 1}
),
signals AS (
    SELECT m.security_id, m.trade_date, m.day_idx,
        m.ma5 / m.ma_slow - 1 as ma_gap
    FROM price_ma m
    WHERE m.ma5 > m.ma_slow
      AND (SELECT m2.ma5 FROM price_ma m2 WHERE m2.security_id = m.security_id AND m2.day_idx < m.day_idx ORDER BY m2.day_idx DESC LIMIT 1) <=
          (SELECT m2.ma_slow FROM price_ma m2 WHERE m2.security_id = m.security_id AND m2.day_idx < m.day_idx ORDER BY m2.day_idx DESC LIMIT 1)
      AND m.volume > m.vol_ma5 * 1.5
      AND m.amount >= 100000000
      AND ABS(m.change_pct) < 9.8
),
all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, low_price, change_pct FROM base)
SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
    e.trade_date as entry_date, e.open_price as entry_price,
    x.trade_date as exit_date, x.close_price as exit_price, x.low_price as exit_low,
    e.change_pct as entry_change,
    s.ma_gap as raw_score
FROM signals s
LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {HOLDING_DAYS + 1}
WHERE e.trade_date IS NOT NULL AND e.open_price > 0 AND x.trade_date IS NOT NULL
"""
ma_raw = conn.execute(q_ma, [TEST_START, TEST_END]).fetchdf()
ma_raw['strategy'] = 'MA_CROSS'
print(f"MA_CROSS({MA_SLOW}): {len(ma_raw)}笔")

# ── MACD ──
q_macd = f"""
WITH base AS (
    SELECT security_id, trade_date, close_price, open_price, low_price, volume, amount, change_pct,
        ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
    FROM daily_bar
    WHERE trade_date >= ? AND trade_date <= ?
),
ema_base AS (
    SELECT b.security_id, b.trade_date, b.day_idx, b.close_price, b.volume, b.amount, b.change_pct,
        AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 11 PRECEDING AND CURRENT ROW) as sma_fast,
        AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 25 PRECEDING AND CURRENT ROW) as sma_slow,
        AVG(b.volume) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as vol_ma5
    FROM base b WHERE b.day_idx > 40
),
macd_base AS (
    SELECT b.security_id, b.trade_date, b.day_idx, b.volume, b.amount, b.vol_ma5, b.change_pct,
        b.sma_fast - b.sma_slow as macd
    FROM ema_base b
),
signal_line AS (
    SELECT b.security_id, b.trade_date, b.day_idx, b.volume, b.amount, b.vol_ma5, b.macd, b.change_pct,
        AVG(b.macd) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 8 PRECEDING AND CURRENT ROW) as macd_signal
    FROM macd_base b
),
macline AS (
    SELECT s.security_id, s.trade_date, s.day_idx, s.volume, s.amount, s.vol_ma5, s.change_pct,
        (SELECT m.macd - m.macd_signal FROM signal_line m
         WHERE m.security_id = s.security_id AND m.day_idx < s.day_idx
         ORDER BY m.day_idx DESC LIMIT 1) as macd_diff_prev
    FROM signal_line s
    WHERE s.macd > s.macd_signal
),
signals AS (
    SELECT m.security_id, m.trade_date, m.day_idx,
        ABS(m.macd_diff_prev) as raw_score
    FROM macline m
    WHERE m.macd_diff_prev IS NOT NULL AND m.macd_diff_prev <= 0
      AND m.volume > m.vol_ma5 * 1.3 AND m.amount >= 200000000
      AND ABS(m.change_pct) < 9.8
),
all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, low_price, change_pct FROM base)
SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
    e.trade_date as entry_date, e.open_price as entry_price,
    x.trade_date as exit_date, x.close_price as exit_price, x.low_price as exit_low,
    e.change_pct as entry_change,
    s.raw_score as raw_score
FROM signals s
LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {HOLDING_DAYS + 1}
WHERE e.trade_date IS NOT NULL AND e.open_price > 0 AND x.trade_date IS NOT NULL
"""
macd_raw = conn.execute(q_macd, [TEST_START, TEST_END]).fetchdf()
macd_raw['strategy'] = 'MACD'
print(f"MACD: {len(macd_raw)}笔")

# ── VOL_SURGE ──
q_vol = f"""
WITH base AS (
    SELECT security_id, trade_date, close_price, open_price, low_price, volume, amount, change_pct,
        ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
    FROM daily_bar
    WHERE trade_date >= ? AND trade_date <= ?
),
price_vol AS (
    SELECT b.security_id, b.trade_date, b.day_idx, b.close_price, b.open_price, b.volume, b.amount, b.change_pct,
        AVG(b.volume) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as vol_ma20,
        (b.close_price - b.open_price) / b.open_price * 100 as pct_change
    FROM base b WHERE b.day_idx > 20
),
signals AS (
    SELECT v.security_id, v.trade_date, v.day_idx,
        v.pct_change as raw_score
    FROM price_vol v
    WHERE v.volume >= v.vol_ma20 * 1.5
      AND v.amount >= 100000000
      AND v.pct_change BETWEEN 3.0 AND 8.0
      AND (SELECT MIN(v2.close_price) / v.close_price - 1
           FROM price_vol v2
           WHERE v2.security_id = v.security_id AND v2.day_idx < v.day_idx AND v2.day_idx >= v.day_idx - 5) <= -0.04
      AND ABS(v.change_pct) < 9.8
),
all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, low_price, change_pct FROM base)
SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
    e.trade_date as entry_date, e.open_price as entry_price,
    x.trade_date as exit_date, x.close_price as exit_price, x.low_price as exit_low,
    e.change_pct as entry_change,
    s.raw_score as raw_score
FROM signals s
LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {HOLDING_DAYS + 1}
WHERE e.trade_date IS NOT NULL AND e.open_price > 0 AND x.trade_date IS NOT NULL
"""
vol_raw = conn.execute(q_vol, [TEST_START, TEST_END]).fetchdf()
vol_raw['strategy'] = 'VOL_SURGE'
print(f"VOL_SURGE: {len(vol_raw)}笔")

conn.close()

# ── 合并+百分位化 ──
all_trades = pd.concat([ma_raw, macd_raw, vol_raw], ignore_index=True)
all_trades['signal_date'] = pd.to_datetime(all_trades['signal_date'])
all_trades['entry_date']  = pd.to_datetime(all_trades['entry_date'])
all_trades['exit_date']   = pd.to_datetime(all_trades['exit_date'])
all_trades['raw_score']   = all_trades['raw_score'].astype(float)

# 百分位化
for strat in ['MA_CROSS', 'MACD', 'VOL_SURGE']:
    mask = all_trades['strategy'] == strat
    scores = all_trades.loc[mask, 'raw_score']
    if len(scores) > 0:
        all_trades.loc[mask, 'score'] = scores.rank(pct=True, ascending=True)

all_trades = all_trades.sort_values('score', ascending=False)
all_trades = all_trades.drop_duplicates(subset=['signal_date', 'security_id'], keep='first')
all_trades = all_trades.sort_values(['entry_date', 'score'], ascending=[True, False]).reset_index(drop=True)
print(f"合并去重后: {len(all_trades)}笔\n")

# ── 构建全量日K数据（用于日内止损） ──
print("加载日K数据（用于移动止损）…")
conn2 = duckdb.connect(DB_PATH)
conn2.execute("SET enable_progress_bar=false")
# daily_bar没有day_idx列，需要重建
all_bars_df = conn2.execute(f"""
    SELECT security_id, trade_date,
        ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx,
        open_price, close_price, low_price, change_pct
    FROM daily_bar
    WHERE trade_date >= ? AND trade_date <= ?
""", [TEST_START, TEST_END]).fetchdf()
conn2.close()
all_bars_df['trade_date'] = pd.to_datetime(all_bars_df['trade_date'])

# ── 模拟资金曲线（支持移动止损） ──
capital = INITIAL_CAPITAL
active_positions = {}
capital_curve = []
stats = []

# 构建信号→exit_idx映射
all_trades['exit_idx'] = all_trades['sig_idx'] + HOLDING_DAYS + 1

unique_days = sorted(all_trades['entry_date'].unique())

for day in unique_days:
    # 结算到期/止损持仓
    expired_secs = [s for s, info in active_positions.items() if info['exit_date'] <= day]
    for sec in expired_secs:
        info = active_positions[sec]
        trade_row = all_trades[(all_trades['security_id'] == sec) &
                               (all_trades['entry_date'] == info['entry_date'])].iloc[0]
        entry     = float(trade_row['entry_price'])
        exit_price = float(trade_row['exit_price'])
        low       = float(trade_row.get('exit_low', 0))

        # 固定止损检测（以exit_low）
        if low > 0 and low <= entry * STOP_PCT:
            exit_price = entry * STOP_PCT

        pnl_pct   = (exit_price - entry) / entry * 100 - COST_PCT
        pnl_money = info['shares'] * pnl_pct / 100 * entry
        capital  += pnl_money

        stats.append({
            'security_id': sec,
            'entry_date': info['entry_date'], 'exit_date': info['exit_date'],
            'strategy': info['strategy'],
            'pnl_pct': pnl_pct, 'capital_after': capital,
            'stopped': info.get('stopped', False), 'exit_reason': info.get('exit_reason', 'hold')
        })
        del active_positions[sec]

    # 移动止损检查（持仓期间每日检测）
    if USE_TRAILING and active_positions:
        for sec, info in list(active_positions.items()):
            entry_date = info['entry_date']
            sig_idx    = info['sig_idx']
            exit_idx   = info['exit_idx']
            entry      = info['entry_price']
            stop_target = entry * STOP_PCT

            # 找持仓期间的K线（从entry_idx+1到当前day_idx）
            current_idx = all_trades[all_trades['entry_date'] == day].iloc[0]['sig_idx'] if len(all_trades[all_trades['entry_date'] == day]) > 0 else None

            if current_idx is None:
                continue

            # 获取该持仓从entry到当前的所有日K
            bars = all_bars_df[(all_bars_df['security_id'] == sec) &
                               (all_bars_df['day_idx'] > sig_idx) &
                               (all_bars_df['day_idx'] <= current_idx + 1)]

            stopped_this_day = False
            for _, bar in bars.iterrows():
                bar_low   = float(bar['low_price'])
                bar_close = float(bar['close_price'])
                bar_date  = bar['trade_date']

                # 触及止损：日内低点创新低 or 收盘跌破止损线
                if bar_low <= stop_target:
                    # 以次日开盘价止损
                    next_bar = all_bars_df[(all_bars_df['security_id'] == sec) &
                                          (all_bars_df['day_idx'] == bar['day_idx'] + 1)]
                    if len(next_bar) > 0:
                        exit_px = float(next_bar.iloc[0]['open_price'])
                        pnl_pct = (exit_px - entry) / entry * 100 - COST_PCT
                        capital += info['shares'] * pnl_pct / 100 * entry
                        stats.append({
                            'security_id': sec,
                            'entry_date': entry_date, 'exit_date': bar_date,
                            'strategy': info['strategy'],
                            'pnl_pct': pnl_pct, 'capital_after': capital,
                            'stopped': True, 'exit_reason': 'trailing_stop'
                        })
                        del active_positions[sec]
                        stopped_this_day = True
                        break
                # 跟踪止损：最高点回落超过一定幅度也出局（保留利润）
                # 当前简化版暂不实现

    # 新开仓
    current_count   = len(active_positions)
    if current_count < MAX_POSITIONS:
        available_slots = MAX_POSITIONS - current_count
        day_signals     = all_trades[all_trades['entry_date'] == day].copy()
        day_signals     = day_signals[~day_signals['security_id'].isin(active_positions.keys())]

        slots_used = 0
        for _, row in day_signals.iterrows():
            if slots_used >= available_slots:
                break
            sec        = row['security_id']
            entry_price = float(row['entry_price'])
            if entry_price <= 0:
                continue

            per_pos_cap = capital / MAX_POSITIONS
            shares      = int(per_pos_cap / entry_price)
            if shares <= 0:
                continue

            active_positions[sec] = {
                'exit_date': row['exit_date'],
                'exit_idx':  int(row['exit_idx']),
                'sig_idx':   int(row['sig_idx']),
                'shares':    shares,
                'entry_price': entry_price,
                'entry_date': day,
                'strategy':  row['strategy'],
                'stopped':   False,
                'exit_reason': 'hold'
            }
            slots_used += 1

    capital_curve.append({'date': day, 'capital': capital, 'positions': len(active_positions)})

# 剩余持仓结算
if active_positions:
    for sec, info in list(active_positions.items()):
        trade_row = all_trades[(all_trades['security_id'] == sec) &
                               (all_trades['entry_date'] == info['entry_date'])].iloc[0]
        entry     = float(trade_row['entry_price'])
        exit_price = float(trade_row['exit_price'])
        low       = float(trade_row.get('exit_low', 0))

        if low > 0 and low <= entry * STOP_PCT:
            exit_price = entry * STOP_PCT

        pnl_pct   = (exit_price - entry) / entry * 100 - COST_PCT
        capital  += info['shares'] * pnl_pct / 100 * entry
        stats.append({
            'security_id': sec,
            'entry_date': info['entry_date'], 'exit_date': info['exit_date'],
            'strategy': info['strategy'],
            'pnl_pct': pnl_pct, 'capital_after': capital,
            'stopped': info.get('stopped', False),
            'exit_reason': 'final'
        })

# ── 结果 ──
df_stats  = pd.DataFrame(stats)
df_curve  = pd.DataFrame(capital_curve)

print(f"\n{'='*60}")
print(f"  最终资金: {capital:,.0f} 元")
print(f"  初始资金: {INITIAL_CAPITAL:,.0f} 元")
print(f"  总收益: {(capital/INITIAL_CAPITAL-1)*100:.1f}%")

years = (pd.Timestamp(TEST_END) - pd.Timestamp(TEST_START)).days / 365.25
ann_return = ((capital / INITIAL_CAPITAL) ** (1 / years) - 1) * 100
print(f"  投资时长: {years:.1f} 年")
print(f"  年化复利: {ann_return:.1f}%")

df_curve['peak'] = df_curve['capital'].cummax()
df_curve['drawdown'] = (df_curve['capital'] / df_curve['peak'] - 1) * 100
max_dd = df_curve['drawdown'].min()
max_dd_date = df_curve.loc[df_curve['drawdown'].idxmin(), 'date']
print(f"  最大回撤: {max_dd:.1f}% (发生在 {str(max_dd_date)[:10]})")

n_trades = len(df_stats)
if n_trades > 0:
    wins = df_stats[df_stats['pnl_pct'] > 0]
    wr   = len(wins) / n_trades * 100
    avg_win  = wins['pnl_pct'].mean() if len(wins) > 0 else 0
    avg_loss = df_stats[df_stats['pnl_pct'] <= 0]['pnl_pct'].mean()
    pl  = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    avg_pct = df_stats['pnl_pct'].mean()
    print(f"\n  总交易笔数: {n_trades}")
    print(f"  胜率: {wr:.1f}%")
    print(f"  盈亏比: {pl:.2f}")
    print(f"  平均期望: {avg_pct:.3f}%/笔")
    print(f"  止损率: {(df_stats['stopped']==True).sum()/n_trades*100:.1f}%")
    print(f"\n  各策略贡献:")
    for strat in ['MA_CROSS', 'MACD', 'VOL_SURGE']:
        sub = df_stats[df_stats['strategy'] == strat]
        if len(sub) > 0:
            sub_wr = len(sub[sub['pnl_pct'] > 0]) / len(sub) * 100
            sub_stop = (sub['stopped']==True).sum()
            print(f"    {strat}: {len(sub)}笔, 胜率{sub_wr:.0f}%, 期望{sub['pnl_pct'].mean():.3f}%, 止损{sub_stop}笔")

print(f"\n  资金曲线(前5日):")
for _, row in df_curve.head(5).iterrows():
    print(f"    {str(row['date'])[:10]}  资金:{row['capital']:>14,.0f}  持仓:{row['positions']}只")

print(f"\n  资金曲线(后5日):")
for _, row in df_curve.tail(5).iterrows():
    print(f"    {str(row['date'])[:10]}  资金:{row['capital']:>14,.0f}  持仓:{row['positions']}只")

# ── 保存结果 ──
import os
OUT_DIR = 'data/param_results'
os.makedirs(OUT_DIR, exist_ok=True)
suffix = f"_{SUFFIX}" if SUFFIX else ""

# 交割单（含止损标记）
if 'security_id' in df_stats.columns and len(df_stats) > 0:
    conn3 = duckdb.connect(DB_PATH)
    conn3.execute("SET enable_progress_bar=false")
    try:
        names = conn3.execute("""
            SELECT security_id, symbol, name FROM security_master
            WHERE symbol LIKE 'SH60%%' OR symbol LIKE 'SZ00%%'
        """).df()
        conn3.close()
        df_stats = df_stats.merge(names, on='security_id', how='left')
        df_stats['code'] = df_stats['symbol'].fillna(df_stats['security_id'].astype(str))
        df_stats['name'] = df_stats['name'].fillna(df_stats['security_id'].astype(str))
    except:
        conn3.close()
        df_stats['code'] = df_stats['security_id'].astype(str)
        df_stats['name'] = df_stats['security_id'].astype(str)

trades_file = f"{OUT_DIR}/trades{suffix}.csv"
df_stats.to_csv(trades_file, index=False)
print(f"\n交割单 -> {trades_file}")

curve_file = f"{OUT_DIR}/capital_curve{suffix}.csv"
df_curve.to_csv(curve_file, index=False)

# 保存摘要参数
params = {
    'TEST_START': TEST_START, 'TEST_END': TEST_END,
    'MA_SLOW': MA_SLOW, 'HOLDING_DAYS': HOLDING_DAYS,
    'STOP_PCT': STOP_PCT, 'USE_TRAILING': USE_TRAILING,
    'INITIAL_CAPITAL': INITIAL_CAPITAL, 'FINAL_CAPITAL': capital,
    'TOTAL_RETURN': f"{(capital/INITIAL_CAPITAL-1)*100:.1f}%",
    'ANN_RETURN': f"{ann_return:.1f}%",
    'MAX_DRAWDOWN': f"{max_dd:.1f}%",
    'N_TRADES': n_trades,
    'WIN_RATE': f"{wr:.1f}%" if n_trades > 0 else 'N/A',
    'PL_RATIO': f"{pl:.2f}" if n_trades > 0 else 'N/A',
    'AVG_PCT': f"{avg_pct:.3f}%" if n_trades > 0 else 'N/A',
}
params_file = f"{OUT_DIR}/params{suffix}.txt"
with open(params_file, 'w') as f:
    for k, v in params.items():
        f.write(f"{k} = {v}\n")
print(f"参数摘要 -> {params_file}")

print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] 完成!")
