"""
组合回测验证：MA_CROSS + MACD + VOL_SURGE
每天最多2只持仓，各策略评分取TOP信号
"""
import duckdb
import pandas as pd
import numpy as np

DB_PATH = 'data/quant.duckdb'
TEST_START = '2026-01-01'
TEST_END = '2026-06-01'
STOP_LOSS_PCT = -0.03
COST_PCT = 0.56
MAX_POSITIONS = 2          # 每天最多2只持仓
HOLDING_DAYS = 5           # 持仓天数

conn = duckdb.connect(DB_PATH)
conn.execute("SET enable_progress_bar=false")

# ── 1. MA_CROSS: fast_ma=5, slow_ma=60, vol_mult=1.5, amount=1亿 ──
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
print(f"MA_CROSS: {len(ma_raw)}笔")

# ── 2. MACD: fast=12, slow=26, signal=9, vol_mult=1.3, amount=2亿, hold=5 ──
q_macd = """
WITH base AS (
    SELECT security_id, trade_date, close_price, open_price, low_price, volume, amount,
        ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
    FROM daily_bar
    WHERE trade_date >= ? AND trade_date <= ?
),
ema_base AS (
    SELECT b.security_id, b.trade_date, b.day_idx, b.close_price, b.volume, b.amount, b.open_price,
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
      AND m.volume > m.vol_ma5 * 1.3
      AND m.amount >= 200000000
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
print(f"MACD: {len(macd_raw)}笔")

# ── 3. VOL_SURGE: vol_mult=1.5, pullback<=4%, change=3-8%, amount=1亿, hold=5 ──
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
print(f"VOL_SURGE: {len(vol_raw)}笔")

# 合并+去重
ma_raw['strategy'] = 'MA_CROSS'
macd_raw['strategy'] = 'MACD'
vol_raw['strategy'] = 'VOL_SURGE'
all_trades = pd.concat([ma_raw, macd_raw, vol_raw], ignore_index=True)

# 日期强转
all_trades['signal_date'] = pd.to_datetime(all_trades['signal_date'])
all_trades['entry_date'] = pd.to_datetime(all_trades['entry_date'])
all_trades['exit_date'] = pd.to_datetime(all_trades['exit_date'])
all_trades['score'] = all_trades['score'].astype(float)

# ── 评分可比性修复：各策略内百分位化 ──
# 每个策略的原始score量纲不同，转为0-1百分位排名后再比较
for strat in ['MA_CROSS', 'MACD', 'VOL_SURGE']:
    mask = all_trades['strategy'] == strat
    scores = all_trades.loc[mask, 'score']
    if len(scores) > 0:
        # 百分位排名：值越大排名越高（0-1之间）
        pct_rank = scores.rank(pct=True, ascending=True)
        all_trades.loc[mask, 'score'] = pct_rank

# 去重：同股票同一天只留评分最高的信号
all_trades = all_trades.sort_values('score', ascending=False)
all_trades = all_trades.drop_duplicates(subset=['signal_date', 'security_id'], keep='first')
all_trades = all_trades.sort_values(['entry_date', 'score'], ascending=[True, False]).reset_index(drop=True)
print(f"评分百分位化后，合并去重: {len(all_trades)}笔")

# ── 每日TOP2仓位过滤 ──
# 模拟真实操作：每天按评分选最多2只持仓
portfolio = []  # (exit_date, security_id) 已持仓到期日
active_positions = {}  # security_id -> exit_date
daily_selected = {}    # entry_date -> [security_ids]

for _, row in all_trades.iterrows():
    entry = row['entry_date']
    exit_d = row['exit_date']
    sec = row['security_id']

    # 清理已到期持仓
    expired = [s for s, ed in active_positions.items() if ed <= entry]
    for s in expired:
        del active_positions[s]

    # 已持仓的不重复开
    if sec in active_positions:
        continue

    # 当天已满2只，跳过
    today_count = sum(1 for ed in active_positions.values() if ed > entry)
    if today_count >= MAX_POSITIONS:
        continue

    # 记录开仓
    active_positions[sec] = exit_d
    daily_selected.setdefault(entry, []).append(sec)
    portfolio.append(row)

selected_df = pd.DataFrame(portfolio)
if len(selected_df) == 0:
    print("没有满足条件的信号！")
    conn.close()
    exit()

# 计算止损盈亏
def calc_pnl(row):
    entry = float(row['entry_price'])
    exit_price = float(row['exit_price'])
    low = float(row.get('exit_low', 0))
    if entry <= 0:
        return np.nan
    if low > 0 and low <= entry * (1 + STOP_LOSS_PCT):
        exit_price = entry * (1 + STOP_LOSS_PCT)
    pnl = (exit_price - entry) / entry * 100 - COST_PCT
    return pnl

selected_df['pnl_pct'] = selected_df.apply(calc_pnl, axis=1)
selected_df = selected_df.dropna(subset=['pnl_pct'])
print(f"每日TOP2有效交易: {len(selected_df)}笔")

df = selected_df

# 统计
wins = df[df['pnl_pct'] > 0]
losses = df[df['pnl_pct'] <= 0]
wr = len(wins) / len(df) * 100
avg_win = wins['pnl_pct'].mean() if len(wins) > 0 else 0
avg_loss = losses['pnl_pct'].mean() if len(losses) > 0 else 0
pl = abs(avg_win / avg_loss) if avg_loss != 0 else 0
expected = (wr/100 * avg_win) - ((1-wr/100) * abs(avg_loss))
total_pnl = df['pnl_pct'].sum()
n_days = (df['exit_date'].max() - df['entry_date'].min()).days
n_trades_year = 252 / HOLDING_DAYS
annual_simple = df['pnl_pct'].mean() * n_trades_year

# 最大回撤
df_sorted = df.sort_values('exit_date').reset_index(drop=True)
df_sorted['nav'] = (1 + df_sorted['pnl_pct']/100).cumprod()
df_sorted['peak'] = df_sorted['nav'].cummax()
df_sorted['drawdown'] = (df_sorted['nav'] / df_sorted['peak'] - 1) * 100
max_dd = df_sorted['drawdown'].min()

print(f"\n{'='*55}")
print(f"  MA_CROSS + MACD + VOL_SURGE 组合回测")
print(f"  测试期: {TEST_START} ~ {TEST_END}")
print(f"  持仓约束: 每天最多 {MAX_POSITIONS} 只")
print(f"{'='*55}")
print(f"  总交易次数: {len(df)}")
print(f"  胜率: {wr:.1f}%")
print(f"  盈亏比: {pl:.2f}")
print(f"  平均期望: {expected:.3f}%/笔")
print(f"  总收益: {total_pnl:.1f}%")
print(f"  年化(单利): {annual_simple:.1f}%")
print(f"  最大回撤: {max_dd:.1f}%")
print(f"\n--- 各策略贡献 ---")
for strat in ['MA_CROSS', 'MACD', 'VOL_SURGE']:
    sub = df[df['strategy'] == strat]
    if len(sub) == 0:
        print(f"  {strat}: 0笔")
        continue
    sub_wr = len(sub[sub['pnl_pct'] > 0]) / len(sub) * 100
    sub_exp = sub['pnl_pct'].mean()
    print(f"  {strat}: {len(sub)}笔  期望{sub_exp:.3f}%  胜率{sub_wr:.0f}%")

conn.close()
