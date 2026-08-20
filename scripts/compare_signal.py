import duckdb
import pandas as pd

conn = duckdb.connect('data/quant.duckdb')
conn.execute("SET enable_progress_bar=false")

# 回测的SQL逻辑 - VOL_SURGE
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
    SELECT v.security_id, v.trade_date, v.day_idx,
        v.pct_change as score
    FROM price_vol v
    WHERE v.volume >= v.vol_ma20 * 1.5
      AND v.amount >= 100000000
      AND v.pct_change BETWEEN 3.0 AND 8.0
      AND (SELECT MIN(v2.close_price) / v.close_price - 1
           FROM price_vol v2
           WHERE v2.security_id = v.security_id AND v2.day_idx < v.day_idx AND v2.day_idx >= v.day_idx - 5) <= -0.04
      AND ABS(v.change_pct) < 9.8
)
SELECT s.security_id, s.trade_date, s.day_idx, s.score
FROM signals s
WHERE s.trade_date = ?
ORDER BY s.score DESC
"""

# 读取交割单
trades = pd.read_csv('D:/Files/workspace-ai/quant/data/param_results/combo_realistic_trades.csv')
trades['entry_date'] = pd.to_datetime(trades['entry_date'])

# 看几个有代表性的交易日
test_dates = ['2020-02-10', '2020-02-17', '2020-03-01']

for date_str in test_dates:
    entry_date = pd.to_datetime(date_str) + pd.Timedelta(days=1)
    entry_date_str = entry_date.strftime('%Y-%m-%d')

    # 回测SQL查当天信号
    df_signal = conn.execute(q_vol, ['2020-01-01', '2026-06-01', date_str]).df()

    # 交割单里这天的买入
    day_trades = trades[trades['entry_date'] == entry_date]

    print(f"\n{'='*60}")
    print(f"信号日: {date_str}  →  买入日(回测): {entry_date_str}")
    print(f"{'='*60}")

    print(f"\n回测SQL选出的VOL_SURGE信号（前10）:")
    if len(df_signal) > 0:
        print(df_signal.head(10).to_string(index=False))
    else:
        print("  (无信号)")

    print(f"\n交割单里当天买入的股票:")
    if len(day_trades) > 0:
        for _, t in day_trades.iterrows():
            print(f"  {t['symbol']} {t['name']} [{t['strategy']}] entry_price={t['entry_price']} pnl={t['pnl_pct']:.2f}%")
    else:
        print("  (无买入)")

    # 检查交割单里的股票是否在信号里
    if len(day_trades) > 0 and len(df_signal) > 0:
        bought_ids = set(day_trades['security_id'].values)
        signal_ids = set(df_signal['security_id'].values)
        in_signal = bought_ids & signal_ids
        not_in = bought_ids - signal_ids
        print(f"\n买入的股票在信号里: {in_signal if in_signal else '(无)'}")
        print(f"买入的股票不在信号里: {not_in if not_in else '(无)'}")

conn.close()
