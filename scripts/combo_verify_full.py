"""
从2020年开始持有10万，用MA_CROSS+MACD+VOL_SURGE策略，每天最多2只持仓
计算到今天的最终资金
"""
import duckdb
import pandas as pd
import numpy as np

DB_PATH = 'data/quant.duckdb'
conn = duckdb.connect(DB_PATH)
conn.execute("SET enable_progress_bar=false")

INITIAL_CAPITAL = 100000.0
STOP_LOSS_PCT = -0.03
COST_PCT = 0.56
MAX_POSITIONS = 2
HOLDING_DAYS = 5

TEST_START = '2020-01-01'
TEST_END = '2026-06-01'

print(f"从 {TEST_START} 到 {TEST_END}，初始资金 {INITIAL_CAPITAL:.0f} 元")
print(f"策略: MA_CROSS + MACD + VOL_SURGE，每天最多 {MAX_POSITIONS} 只持仓")
print(f"{'='*60}\n")

# ── 拉取全量信号 ──
# MA_CROSS
q_ma = """
WITH base AS (
    SELECT security_id, trade_date, close_price, open_price, low_price, volume, amount,
        ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
    FROM daily_bar
    WHERE trade_date >= ? AND trade_date <= ?
),
price_ma AS (
    SELECT b.security_id, b.trade_date, b.day_idx, b.close_price, b.volume, b.amount,
        AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as ma5,
        AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) as ma60,
        AVG(b.volume) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as vol_ma5
    FROM base b WHERE b.day_idx > 60
),
signals AS (
    SELECT m.security_id, m.trade_date, m.day_idx,
        m.ma5 / m.ma60 - 1 as ma_gap
    FROM price_ma m
    WHERE m.ma5 > m.ma60
      AND (SELECT m2.ma5 FROM price_ma m2 WHERE m2.security_id = m.security_id AND m2.day_idx < m.day_idx ORDER BY m2.day_idx DESC LIMIT 1) <=
          (SELECT m2.ma60 FROM price_ma m2 WHERE m2.security_id = m.security_id AND m2.day_idx < m.day_idx ORDER BY m2.day_idx DESC LIMIT 1)
      AND m.volume > m.vol_ma5 * 1.5
      AND m.amount >= 100000000
),
all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, low_price FROM base)
SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
    e.trade_date as entry_date, e.open_price as entry_price,
    x.trade_date as exit_date, x.close_price as exit_price, x.low_price as exit_low,
    s.ma_gap as score
FROM signals s
LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + 6
WHERE e.trade_date IS NOT NULL AND e.open_price > 0 AND x.trade_date IS NOT NULL
"""
ma_raw = conn.execute(q_ma, [TEST_START, TEST_END]).fetchdf()
ma_raw['strategy'] = 'MA_CROSS'
print(f"MA_CROSS信号: {len(ma_raw)}笔")

# MACD
q_macd = """
WITH base AS (
    SELECT security_id, trade_date, close_price, open_price, low_price, volume, amount,
        ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
    FROM daily_bar
    WHERE trade_date >= ? AND trade_date <= ?
),
ema_base AS (
    SELECT b.security_id, b.trade_date, b.day_idx, b.close_price, b.volume, b.amount,
        AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 11 PRECEDING AND CURRENT ROW) as sma_fast,
        AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 25 PRECEDING AND CURRENT ROW) as sma_slow,
        AVG(b.volume) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as vol_ma5
    FROM base b WHERE b.day_idx > 40
),
macd_base AS (
    SELECT b.security_id, b.trade_date, b.day_idx, b.volume, b.amount, b.vol_ma5,
        b.sma_fast - b.sma_slow as macd
    FROM ema_base b
),
signal_line AS (
    SELECT b.security_id, b.trade_date, b.day_idx, b.volume, b.amount, b.vol_ma5, b.macd,
        AVG(b.macd) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 8 PRECEDING AND CURRENT ROW) as macd_signal
    FROM macd_base b
),
macline AS (
    SELECT s.security_id, s.trade_date, s.day_idx, s.volume, s.amount, s.vol_ma5,
        (SELECT m.macd - m.macd_signal FROM signal_line m
         WHERE m.security_id = s.security_id AND m.day_idx < s.day_idx
         ORDER BY m.day_idx DESC LIMIT 1) as macd_diff_prev
    FROM signal_line s
    WHERE s.macd > s.macd_signal
),
signals AS (
    SELECT m.security_id, m.trade_date, m.day_idx,
        ABS(m.macd_diff_prev) as score
    FROM macline m
    WHERE m.macd_diff_prev IS NOT NULL AND m.macd_diff_prev <= 0
      AND m.volume > m.vol_ma5 * 1.3 AND m.amount >= 200000000
),
all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, low_price FROM base)
SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
    e.trade_date as entry_date, e.open_price as entry_price,
    x.trade_date as exit_date, x.close_price as exit_price, x.low_price as exit_low,
    s.score as score
FROM signals s
LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + 6
WHERE e.trade_date IS NOT NULL AND e.open_price > 0 AND x.trade_date IS NOT NULL
"""
macd_raw = conn.execute(q_macd, [TEST_START, TEST_END]).fetchdf()
macd_raw['strategy'] = 'MACD'
print(f"MACD信号: {len(macd_raw)}笔")

# VOL_SURGE
q_vol = """
WITH base AS (
    SELECT security_id, trade_date, close_price, open_price, low_price, volume, amount,
        ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
    FROM daily_bar
    WHERE trade_date >= ? AND trade_date <= ?
),
price_vol AS (
    SELECT b.security_id, b.trade_date, b.day_idx, b.close_price, b.open_price, b.volume, b.amount,
        AVG(b.volume) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as vol_ma20,
        (b.close_price - b.open_price) / b.open_price * 100 as pct_change
    FROM base b WHERE b.day_idx > 20
),
signals AS (
    SELECT v.security_id, v.trade_date, v.day_idx,
        v.pct_change as score
    FROM price_vol v
    WHERE v.volume >= v.vol_ma20 * 1.5
      AND v.amount >= 100000000
      AND v.pct_change BETWEEN 3.0 AND 8.0
      AND (SELECT MIN(v2.close_price) / v.close_price - 1
           FROM price_vol v2
           WHERE v2.security_id = v.security_id AND v2.day_idx < v.day_idx AND v2.day_idx >= v.day_idx - 5) <= -0.04
),
all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, low_price FROM base)
SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
    e.trade_date as entry_date, e.open_price as entry_price,
    x.trade_date as exit_date, x.close_price as exit_price, x.low_price as exit_low,
    s.score as score
FROM signals s
LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + 6
WHERE e.trade_date IS NOT NULL AND e.open_price > 0 AND x.trade_date IS NOT NULL
"""
vol_raw = conn.execute(q_vol, [TEST_START, TEST_END]).fetchdf()
vol_raw['strategy'] = 'VOL_SURGE'
print(f"VOL_SURGE信号: {len(vol_raw)}笔")

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

# ── 模拟每日持仓+资金曲线 ──
capital = INITIAL_CAPITAL
active_positions = {}  # security_id -> {'exit_date', 'shares', 'entry_price', 'entry_date', 'strategy'}
position_history = []   # 记录每日资金和持仓状态

# 按entry_date逐日推进
unique_days = sorted(all_trades['entry_date'].unique())
capital_curve = []
stats = []

for day in unique_days:
    # 1. 当天到期的持仓，结算盈亏
    expired_secs = [s for s, info in active_positions.items() if info['exit_date'] <= day]
    for sec in expired_secs:
        info = active_positions[sec]
        # 找到这笔交易的exit_bar
        trade_row = all_trades[(all_trades['security_id'] == sec) &
                               (all_trades['entry_date'] == info['entry_date'])].iloc[0]
        entry = float(trade_row['entry_price'])
        exit_price = float(trade_row['exit_price'])
        low = float(trade_row.get('exit_low', 0))

        # 止损
        if low > 0 and low <= entry * (1 + STOP_LOSS_PCT):
            exit_price = entry * (1 + STOP_LOSS_PCT)

        pnl_pct = (exit_price - entry) / entry * 100 - COST_PCT
        pnl_money = info['shares'] * pnl_pct / 100 * entry
        capital += pnl_money

        stats.append({
            'security_id': sec,
            'entry_date': info['entry_date'],
            'exit_date': info['exit_date'],
            'strategy': info['strategy'],
            'pnl_pct': pnl_pct,
            'pnl_money': pnl_money,
            'capital_after': capital
        })

        del active_positions[sec]

    # 2. 当天新开仓（最多2只持仓总共）
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

            # 每只股票分配等量资金：总资金/2（最多2持仓）
            per_position_capital = capital / MAX_POSITIONS
            shares = int(per_position_capital / entry_price)  # 取整
            if shares <= 0:
                continue

            active_positions[sec] = {
                'exit_date': row['exit_date'],
                'shares': shares,
                'entry_price': entry_price,
                'entry_date': day,
                'strategy': row['strategy']
            }
            slots_used += 1

    # 3. 记录当日资金
    capital_curve.append({
        'date': day,
        'capital': capital,
        'positions': len(active_positions),
        'active': list(active_positions.keys())
    })

# 最后一个持仓日之后，剩余持仓按最后一天收盘结算（简化处理）
if active_positions:
    last_day = capital_curve[-1]['date'] if capital_curve else pd.Timestamp(TEST_END)
    for sec, info in list(active_positions.items()):
        trade_row = all_trades[(all_trades['security_id'] == sec) &
                               (all_trades['entry_date'] == info['entry_date'])].iloc[0]
        entry = float(trade_row['entry_price'])
        exit_price = float(trade_row['exit_price'])
        pnl_pct = (exit_price - entry) / entry * 100 - COST_PCT
        capital += info['shares'] * pnl_pct / 100 * entry
        stats.append({
            'security_id': sec,
            'entry_date': info['entry_date'],
            'exit_date': info['exit_date'],
            'strategy': info['strategy'],
            'pnl_pct': pnl_pct,
            'pnl_money': info['shares'] * pnl_pct / 100 * entry,
            'capital_after': capital
        })

# ── 结果统计 ──
df_stats = pd.DataFrame(stats)
df_curve = pd.DataFrame(capital_curve)

print(f"{'='*60}")
print(f"  最终资金: {capital:,.0f} 元")
print(f"  初始资金: {INITIAL_CAPITAL:,.0f} 元")
print(f"  总收益: {(capital/INITIAL_CAPITAL-1)*100:.1f}%")
print(f"{'='*60}")

# 年化
start_date = pd.Timestamp(TEST_START)
end_date = pd.Timestamp(TEST_END)
years = (end_date - start_date).days / 365.25
ann_return = ((capital / INITIAL_CAPITAL) ** (1 / years) - 1) * 100
print(f"  投资时长: {years:.1f} 年")
print(f"  年化复利: {ann_return:.1f}%")

# 最大回撤
df_curve['peak'] = df_curve['capital'].cummax()
df_curve['drawdown'] = (df_curve['capital'] / df_curve['peak'] - 1) * 100
max_dd = df_curve['drawdown'].min()
max_dd_date = df_curve.loc[df_curve['drawdown'].idxmin(), 'date']
print(f"  最大回撤: {max_dd:.1f}% (发生在 {str(max_dd_date)[:10]})")

# 交易统计
if len(df_stats) > 0:
    wins = df_stats[df_stats['pnl_pct'] > 0]
    wr = len(wins) / len(df_stats) * 100
    avg_win = wins['pnl_pct'].mean() if len(wins) > 0 else 0
    avg_loss = df_stats[df_stats['pnl_pct'] <= 0]['pnl_pct'].mean()
    pl = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    print(f"\n  总交易笔数: {len(df_stats)}")
    print(f"  胜率: {wr:.1f}%")
    print(f"  盈亏比: {pl:.2f}")
    print(f"  平均期望: {df_stats['pnl_pct'].mean():.3f}%/笔")
    print(f"\n  各策略贡献:")
    for strat in ['MA_CROSS', 'MACD', 'VOL_SURGE']:
        sub = df_stats[df_stats['strategy'] == strat]
        if len(sub) > 0:
            sub_wr = len(sub[sub['pnl_pct'] > 0]) / len(sub) * 100
            print(f"    {strat}: {len(sub)}笔, 胜率{sub_wr:.0f}%, 期望{sub['pnl_pct'].mean():.3f}%")

# 输出资金曲线前10和后10
print(f"\n  资金曲线(前10日):")
for _, row in df_curve.head(10).iterrows():
    print(f"    {str(row['date'])[:10]}  资金:{row['capital']:>12,.0f}  持仓:{row['positions']}只")

print(f"\n  资金曲线(后10日):")
for _, row in df_curve.tail(10).iterrows():
    print(f"    {str(row['date'])[:10]}  资金:{row['capital']:>12,.0f}  持仓:{row['positions']}只")
