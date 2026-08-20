"""
MA_CROSS + MACD + VOL_SURGE 组合回测
最优参数组合，测试集2026-01-01~2026-06-01
"""
import duckdb
import pandas as pd
import numpy as np

DB_PATH = 'data/quant.duckdb'
TEST_START = '2026-01-01'
TEST_END = '2026-06-01'
HOLD_DAYS = 5  # MACD和VOL_SURGE最优都是5，MA_CROSS是3，我们用5统一
STOP_LOSS_PCT = -0.03
TOTAL_COST_PCT = 0.56  # 0.56%

conn = duckdb.connect(DB_PATH)
conn.execute('SET enable_progress_bar=false')

# 读取每日bar建索引
bars = conn.execute("""
    SELECT security_id, trade_date, day_idx, open_price, close_price, low_price, volume, amount
    FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
        FROM daily_bar WHERE trade_date >= '2026-01-01' AND trade_date <= '2026-06-01'
    ) t
    ORDER BY security_id, day_idx
""").fetchdf()
print(f"日线记录: {len(bars)}")

# 建索引
bars_dict = {}
for _, row in bars.iterrows():
    bars_dict[(row['security_id'], int(row['day_idx']))] = row

def get_bar(sec, idx):
    return bars_dict.get((sec, idx))

# ── MA_CROSS ──────────────────────────────────────────────
# holding_days=3, fast_ma=5, slow_ma=60, volume_mult=1.5
ma_bars = bars[bars['day_idx'] > 60].copy()
ma_bars['ma5'] = ma_bars.groupby('security_id')['close_price'].transform(
    lambda x: x.rolling(5, min_periods=5).mean())
ma_bars['ma60'] = ma_bars.groupby('security_id')['close_price'].transform(
    lambda x: x.rolling(60, min_periods=60).mean())
ma_bars['vol_ma5'] = ma_bars.groupby('security_id')['volume'].transform(
    lambda x: x.rolling(5, min_periods=5).mean())

# 金叉：ma5上穿ma60
ma_bars['ma5_prev'] = ma_bars.groupby('security_id')['ma5'].shift(1)
ma_bars['ma60_prev'] = ma_bars.groupby('security_id')['ma60'].shift(1)
ma_signals = ma_bars[
    (ma_bars['ma5'] > ma_bars['ma60']) &
    (ma_bars['ma5_prev'] <= ma_bars['ma60_prev']) &
    (ma_bars['volume'] > ma_bars['vol_ma5'] * 1.5) &
    (ma_bars['amount'] >= 1e8)
][['security_id', 'trade_date', 'day_idx']].copy()
ma_signals['strategy'] = 'MA_CROSS'
ma_signals['holding_days'] = 3
print(f"MA_CROSS信号: {len(ma_signals)}")

# ── MACD ──────────────────────────────────────────────────
# fast=12, slow=26, signal=9, volume_mult=1.3, amount_filter_yi=2.0
macd_bars = bars[bars['day_idx'] > 45].copy()
macd_bars['sma_fast'] = macd_bars.groupby('security_id')['close_price'].transform(
    lambda x: x.rolling(12, min_periods=12).mean())
macd_bars['sma_slow'] = macd_bars.groupby('security_id')['close_price'].transform(
    lambda x: x.rolling(26, min_periods=26).mean())
macd_bars['macd'] = macd_bars['sma_fast'] - macd_bars['sma_slow']
macd_bars['macd_signal'] = macd_bars.groupby('security_id')['macd'].transform(
    lambda x: x.rolling(9, min_periods=9).mean())
macd_bars['vol_ma5'] = macd_bars.groupby('security_id')['volume'].transform(
    lambda x: x.rolling(5, min_periods=5).mean())
macd_bars['macd_diff'] = macd_bars['macd'] - macd_bars['macd_signal']
macd_bars['macd_diff_prev'] = macd_bars.groupby('security_id')['macd_diff'].shift(1)

macd_signals = macd_bars[
    (macd_bars['macd'] > macd_bars['macd_signal']) &
    (macd_bars['macd_diff_prev'].notna()) &
    (macd_bars['macd_diff_prev'] <= 0) &
    (macd_bars['volume'] > macd_bars['vol_ma5'] * 1.3) &
    (macd_bars['amount'] >= 2e8)
][['security_id', 'trade_date', 'day_idx']].copy()
macd_signals['strategy'] = 'MACD'
macd_signals['holding_days'] = 5
print(f"MACD信号: {len(macd_signals)}")

# ── VOL_SURGE ─────────────────────────────────────────────
# vol_mult=1.5, pullback1=0.8, pullback2=0.6, change_min=5.0, change_max=8.0
vol_bars = bars[bars['day_idx'] > 20].copy()
vol_bars['vol_ma20'] = vol_bars.groupby('security_id')['volume'].transform(
    lambda x: x.rolling(20, min_periods=20).mean())
vol_bars['pct_change'] = (vol_bars['close_price'] - vol_bars['open_price']) / vol_bars['open_price'] * 100

# 最近5日最低收盘价（取当天之前）
vol_bars['low_5d_min'] = vol_bars.groupby('security_id')['close_price'].transform(
    lambda x: x.rolling(5, min_periods=1).min().shift(1))

vol_signals = vol_bars[
    (vol_bars['volume'] >= vol_bars['vol_ma20'] * 1.5) &
    (vol_bars['amount'] >= 1e8) &
    (vol_bars['pct_change'] >= 5.0) &
    (vol_bars['pct_change'] <= 8.0) &
    ((vol_bars['low_5d_min'] / vol_bars['close_price'] - 1) <= -0.04)
][['security_id', 'trade_date', 'day_idx']].copy()
vol_signals['strategy'] = 'VOL_SURGE'
vol_signals['holding_days'] = 5
print(f"VOL_SURGE信号: {len(vol_signals)}")

# ── 合并：每日各策略信号，评分取TOP1 ──────────────────────
all_signals = pd.concat([ma_signals, macd_signals, vol_signals], ignore_index=True)
print(f"\n合并后总信号: {len(all_signals)}")

# 同一股票同一天多个信号：保留一个（按MA_CROSS > MACD > VOL_SURGE优先）
priority = {'MA_CROSS': 0, 'MACD': 1, 'VOL_SURGE': 2}
all_signals['priority'] = all_signals['strategy'].map(priority)
all_signals = all_signals.sort_values('priority').drop_duplicates(subset=['trade_date', 'security_id'], keep='first')
print(f"去重后信号: {len(all_signals)}")

# ── 模拟交易 ───────────────────────────────────────────────
trades = []
for _, sig in all_signals.iterrows():
    sec = sig['security_id']
    sig_idx = int(sig['day_idx'])
    hd = int(sig['holding_days'])  # 各策略自己的持仓天数
    entry_idx = sig_idx + 1
    exit_idx = sig_idx + hd + 1

    entry_bar = get_bar(sec, entry_idx)
    exit_bar = get_bar(sec, exit_idx)
    if entry_bar is None or exit_bar is None:
        continue

    entry_price = float(entry_bar['open_price'])
    if entry_price <= 0:
        continue

    # 持仓期间最低价（算止损）
    holding_low = entry_price
    for di in range(entry_idx + 1, exit_idx + 1):
        b = get_bar(sec, di)
        if b is not None:
            holding_low = min(holding_low, float(b['low_price']))

    # 止损
    if holding_low <= entry_price * (1 + STOP_LOSS_PCT):
        exit_price = entry_price * (1 + STOP_LOSS_PCT)
    else:
        exit_price = float(exit_bar['close_price'])

    pnl_pct = (exit_price - entry_price) / entry_price * 100 - TOTAL_COST_PCT
    trades.append({
        'signal_date': sig['trade_date'],
        'security_id': sec,
        'strategy': sig['strategy'],
        'entry_date': entry_bar['trade_date'],
        'entry_price': entry_price,
        'exit_date': exit_bar['trade_date'],
        'exit_price': exit_price,
        'pnl_pct': pnl_pct
    })

df = pd.DataFrame(trades)
print(f"\n有效交易数: {len(df)}")
df.to_csv('data/param_results/combo_top3_trades.csv', index=False, encoding='utf-8-sig')

# ── 统计 ──────────────────────────────────────────────────
wins = df[df['pnl_pct'] > 0]
losses = df[df['pnl_pct'] <= 0]
win_rate = len(wins) / len(df) * 100
avg_win_pct = wins['pnl_pct'].mean() if len(wins) > 0 else 0
avg_loss_pct = losses['pnl_pct'].mean() if len(losses) > 0 else 0
pl_ratio = abs(avg_win_pct / avg_loss_pct) if avg_loss_pct != 0 else 0
expected = (win_rate / 100 * avg_win_pct) - ((1 - win_rate / 100) * abs(avg_loss_pct))
total_pnl = df['pnl_pct'].sum()

n_days = (pd.Timestamp(TEST_END) - pd.Timestamp(TEST_START)).days
n_trades_year = 252 / HOLD_DAYS  # 每年交易次数
annual_return_simple = df['pnl_pct'].mean() * n_trades_year  # 单利
annual_return_compound = ((1 + df['pnl_pct'] / 100).prod() ** (252 / HOLD_DAYS / len(df)) - 1) * 100 if len(df) > 0 else 0

print(f"\n{'='*50}")
print(f"  MA_CROSS + MACD + VOL_SURGE 组合回测")
print(f"{'='*50}")
print(f"  测试期: {TEST_START} ~ {TEST_END} ({n_days}天)")
print(f"  总交易次数: {len(df)}")
print(f"  胜率: {win_rate:.1f}%")
print(f"  盈亏比: {pl_ratio:.2f}")
print(f"  平均期望: {expected:.3f}%/笔")
print(f"  总收益: {total_pnl:.2f}%")
print(f"  年化收益(单利): {annual_return_simple:.1f}%")
print(f"  年化收益(复利): {annual_return_compound:.1f}%")

print(f"\n--- 各策略贡献 ---")
for strat in ['MA_CROSS', 'MACD', 'VOL_SURGE']:
    sub = df[df['strategy'] == strat]
    if len(sub) == 0:
        print(f"  {strat}: 0笔")
        continue
    wr = len(sub[sub['pnl_pct'] > 0]) / len(sub) * 100
    exp = sub['pnl_pct'].mean()
    print(f"  {strat}: {len(sub)}笔  期望{exp:.3f}%  胜率{wr:.0f}%")

conn.close()
