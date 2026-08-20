"""
保存真实约束版完整回测结果 + 交割单 + 资金曲线
"""
import pandas as pd
import numpy as np
import duckdb

DB_PATH = 'data/quant.duckdb'
conn = duckdb.connect(DB_PATH)
conn.execute("SET enable_progress_bar=false")

INITIAL_CAPITAL = 100000.0
STOP_LOSS_PCT = -0.03
COST_PCT = 0.56
MAX_POSITIONS = 2
TEST_START = '2026-01-01'
TEST_END = '2026-06-03'

# ── MA_CROSS ──
q_ma = """
WITH base AS (
    SELECT security_id, trade_date, close_price, open_price, low_price, volume, amount, change_pct,
        ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
    FROM daily_bar
    WHERE trade_date >= ? AND trade_date <= ?
),
price_ma AS (
    SELECT b.security_id, b.trade_date, b.day_idx, b.close_price, b.volume, b.amount, b.change_pct,
        AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as ma5,
        AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) as ma60,
        AVG(b.volume) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as vol_ma5
    FROM base b WHERE b.day_idx > 60
),
signals AS (
    SELECT m.security_id, m.trade_date, m.day_idx, m.ma5 / m.ma60 - 1 as ma_gap
    FROM price_ma m
    WHERE m.ma5 > m.ma60
      AND (SELECT m2.ma5 FROM price_ma m2 WHERE m2.security_id = m.security_id AND m2.day_idx < m.day_idx ORDER BY m2.day_idx DESC LIMIT 1) <=
          (SELECT m2.ma60 FROM price_ma m2 WHERE m2.security_id = m.security_id AND m2.day_idx < m.day_idx ORDER BY m2.day_idx DESC LIMIT 1)
      AND m.volume > m.vol_ma5 * 1.5 AND m.amount >= 100000000
      AND ABS(m.change_pct) < 9.8
),
all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, low_price, change_pct FROM base)
SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
    e.trade_date as entry_date, e.open_price as entry_price,
    x.trade_date as exit_date, x.close_price as exit_price, x.low_price as exit_low,
    e.change_pct as entry_change, s.ma_gap as score
FROM signals s
LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + 6
WHERE e.trade_date IS NOT NULL AND e.open_price > 0 AND x.trade_date IS NOT NULL
"""
ma_raw = conn.execute(q_ma, [TEST_START, TEST_END]).fetchdf()
ma_raw['strategy'] = 'MA_CROSS'
print(f"MA_CROSS: {len(ma_raw)}笔")

# ── MACD ──
q_macd = """
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
        b.sma_fast - b.sma_slow as macd FROM ema_base b
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
    FROM signal_line s WHERE s.macd > s.macd_signal
),
signals AS (
    SELECT m.security_id, m.trade_date, m.day_idx, ABS(m.macd_diff_prev) as score
    FROM macline m
    WHERE m.macd_diff_prev IS NOT NULL AND m.macd_diff_prev <= 0
      AND m.volume > m.vol_ma5 * 1.3 AND m.amount >= 200000000
      AND ABS(m.change_pct) < 9.8
),
all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, low_price, change_pct FROM base)
SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
    e.trade_date as entry_date, e.open_price as entry_price,
    x.trade_date as exit_date, x.close_price as exit_price, x.low_price as exit_low,
    e.change_pct as entry_change, s.score as score
FROM signals s
LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + 6
WHERE e.trade_date IS NOT NULL AND e.open_price > 0 AND x.trade_date IS NOT NULL
"""
macd_raw = conn.execute(q_macd, [TEST_START, TEST_END]).fetchdf()
macd_raw['strategy'] = 'MACD'
print(f"MACD: {len(macd_raw)}笔")

# ── VOL_SURGE ──
q_vol = """
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
    SELECT v.security_id, v.trade_date, v.day_idx, v.pct_change as score
    FROM price_vol v
    WHERE v.volume >= v.vol_ma20 * 1.5 AND v.amount >= 100000000
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
    e.change_pct as entry_change, s.score as score
FROM signals s
LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + 6
WHERE e.trade_date IS NOT NULL AND e.open_price > 0 AND x.trade_date IS NOT NULL
"""
vol_raw = conn.execute(q_vol, [TEST_START, TEST_END]).fetchdf()
vol_raw['strategy'] = 'VOL_SURGE'
print(f"VOL_SURGE: {len(vol_raw)}笔")

# 查股票名称（在关闭连接前）
sec_map = conn.execute("SELECT security_id, symbol, name FROM security_master").fetchdf()
print(f"股票列表: {len(sec_map)}只")

conn.close()

# ── 合并+百分位化 ──
all_trades = pd.concat([ma_raw, macd_raw, vol_raw], ignore_index=True)
all_trades['signal_date'] = pd.to_datetime(all_trades['signal_date'])
all_trades['entry_date'] = pd.to_datetime(all_trades['entry_date'])
all_trades['exit_date'] = pd.to_datetime(all_trades['exit_date'])
all_trades['score'] = all_trades['score'].astype(float)

for strat in ['MA_CROSS', 'MACD', 'VOL_SURGE']:
    mask = all_trades['strategy'] == strat
    scores = all_trades.loc[mask, 'score']
    if len(scores) > 0:
        all_trades.loc[mask, 'score'] = scores.rank(pct=True, ascending=True)

all_trades = all_trades.sort_values('score', ascending=False)
all_trades = all_trades.drop_duplicates(subset=['signal_date', 'security_id'], keep='first')
all_trades = all_trades.sort_values(['entry_date', 'score'], ascending=[True, False]).reset_index(drop=True)
print(f"合并去重后: {len(all_trades)}笔\n")

# ── 模拟资金曲线+交割单 ──
capital = INITIAL_CAPITAL
active_positions = {}
capital_curve = []
trades_detail = []

unique_days = sorted(all_trades['entry_date'].unique())

for day in unique_days:
    expired_secs = [s for s, info in active_positions.items() if info['exit_date'] <= day]
    for sec in expired_secs:
        info = active_positions[sec]
        trade_row = all_trades[(all_trades['security_id'] == sec) &
                               (all_trades['entry_date'] == info['entry_date'])].iloc[0]
        entry = float(trade_row['entry_price'])
        exit_price = float(trade_row['exit_price'])
        low = float(trade_row.get('exit_low', 0))
        stopped = False
        if low > 0 and low <= entry * (1 + STOP_LOSS_PCT):
            exit_price = entry * (1 + STOP_LOSS_PCT)
            stopped = True
        pnl_pct = (exit_price - entry) / entry * 100 - COST_PCT
        pnl_money = info['shares'] * pnl_pct / 100 * entry
        capital += pnl_money

        trades_detail.append({
            'security_id': sec,
            'symbol': sec_map.loc[sec_map['security_id'] == sec, 'symbol'].values[0] if sec in sec_map['security_id'].values else str(sec),
            'name': sec_map.loc[sec_map['security_id'] == sec, 'name'].values[0] if sec in sec_map['security_id'].values else 'Unknown',
            'entry_date': info['entry_date'].strftime('%Y-%m-%d'),
            'entry_price': entry,
            'shares': info['shares'],
            'entry_amount': round(entry * info['shares'], 2),
            'exit_date': info['exit_date'].strftime('%Y-%m-%d'),
            'exit_price': exit_price,
            'exit_amount': round(exit_price * info['shares'], 2),
            'strategy': info['strategy'],
            'pnl_pct': round(pnl_pct, 4),
            'pnl_money': round(pnl_money, 2),
            'stopped_loss': stopped,
            'capital_after': round(capital, 2)
        })
        del active_positions[sec]

    current_count = len(active_positions)
    if current_count < MAX_POSITIONS:
        available_slots = MAX_POSITIONS - current_count
        day_signals = all_trades[all_trades['entry_date'] == day].copy()
        day_signals = day_signals[~day_signals['security_id'].isin(active_positions.keys())]
        slots_used = 0
        for _, row in day_signals.iterrows():
            if slots_used >= available_slots:
                break
            sec = row['security_id']
            entry_price = float(row['entry_price'])
            if entry_price <= 0:
                continue
            per_position_capital = capital / MAX_POSITIONS
            shares = int(per_position_capital / entry_price)
            if shares <= 0:
                continue
            active_positions[sec] = {
                'exit_date': row['exit_date'], 'shares': shares,
                'entry_price': entry_price, 'entry_date': day, 'strategy': row['strategy']
            }
            slots_used += 1

    capital_curve.append({
        'date': day.strftime('%Y-%m-%d'),
        'capital': round(capital, 2),
        'positions': len(active_positions)
    })

# 剩余持仓结算
if active_positions:
    for sec, info in list(active_positions.items()):
        trade_row = all_trades[(all_trades['security_id'] == sec) &
                               (all_trades['entry_date'] == info['entry_date'])].iloc[0]
        entry = float(trade_row['entry_price'])
        exit_price = float(trade_row['exit_price'])
        pnl_pct = (exit_price - entry) / entry * 100 - COST_PCT
        capital += info['shares'] * pnl_pct / 100 * entry
        trades_detail.append({
            'security_id': sec,
            'symbol': sec_map.loc[sec_map['security_id'] == sec, 'symbol'].values[0] if sec in sec_map['security_id'].values else str(sec),
            'name': sec_map.loc[sec_map['security_id'] == sec, 'name'].values[0] if sec in sec_map['security_id'].values else 'Unknown',
            'entry_date': info['entry_date'].strftime('%Y-%m-%d'),
            'entry_price': entry,
            'shares': info['shares'],
            'entry_amount': round(entry * info['shares'], 2),
            'exit_date': info['exit_date'].strftime('%Y-%m-%d'),
            'exit_price': exit_price,
            'exit_amount': round(exit_price * info['shares'], 2),
            'strategy': info['strategy'],
            'pnl_pct': round(pnl_pct, 4),
            'pnl_money': round(info['shares'] * pnl_pct / 100 * entry, 2),
            'stopped_loss': False,
            'capital_after': round(capital, 2)
        })

# ── 保存 ──
OUT = 'data/param_results'

df_trades = pd.DataFrame(trades_detail)
df_trades.to_csv(f'{OUT}/combo_realistic_trades.csv', index=False, encoding='utf-8-sig')
print(f'交割单: {len(df_trades)}笔 -> {OUT}/combo_realistic_trades.csv')

df_cap = pd.DataFrame(capital_curve)
df_cap.to_csv(f'{OUT}/combo_realistic_capital_curve.csv', index=False, encoding='utf-8-sig')
print(f'资金曲线: {len(df_cap)}日 -> {OUT}/combo_realistic_capital_curve.csv')

# 结果摘要
wins = df_trades[df_trades['pnl_pct'] > 0]
wr = len(wins) / len(df_trades) * 100
avg_win = wins['pnl_pct'].mean() if len(wins) > 0 else 0
avg_loss = df_trades[df_trades['pnl_pct'] <= 0]['pnl_pct'].mean()
pl = abs(avg_win / avg_loss) if avg_loss != 0 else 0
years = (pd.Timestamp(TEST_END) - pd.Timestamp(TEST_START)).days / 365.25
ann_return = ((capital / INITIAL_CAPITAL) ** (1 / years) - 1) * 100
df_cap['peak'] = df_cap['capital'].cummax()
df_cap['drawdown'] = (df_cap['capital'] / df_cap['peak'] - 1) * 100
max_dd = df_cap['drawdown'].min()

summary = f"""策略组合回测结果 (真实约束版)
=====================================
测试期: {TEST_START} ~ {TEST_END}
初始资金: {INITIAL_CAPITAL:,.0f} 元
最终资金: {capital:,.2f} 元
总收益: {(capital/INITIAL_CAPITAL-1)*100:.2f}%
年化复利: {ann_return:.2f}%
最大回撤: {max_dd:.2f}%

交易统计:
  总交易笔数: {len(df_trades)}
  胜率: {wr:.2f}%
  盈亏比: {pl:.2f}
  平均期望: {df_trades['pnl_pct'].mean():.4f}%/笔

各策略贡献:
"""
for strat in ['MA_CROSS', 'MACD', 'VOL_SURGE']:
    sub = df_trades[df_trades['strategy'] == strat]
    if len(sub) > 0:
        sub_wr = len(sub[sub['pnl_pct'] > 0]) / len(sub) * 100
        summary += f"  {strat}: {len(sub)}笔, 胜率{sub_wr:.1f}%, 期望{sub['pnl_pct'].mean():.4f}%\n"

with open(f'{OUT}/combo_realistic_results.txt', 'w', encoding='utf-8') as f:
    f.write(summary)
print(f'结果摘要 -> {OUT}/combo_realistic_results.txt')

print(f'\n最终资金: {capital:,.2f} 元 | 交割单 {len(df_trades)}笔')
