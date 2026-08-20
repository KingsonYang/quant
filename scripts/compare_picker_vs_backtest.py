"""
全面对比 daily_picker.py 的SQL 和 combo_verify_full_realistic.py 的SQL
找出两者对同一信号日、同一股票的判断差异
"""

import duckdb
import pandas as pd

conn = duckdb.connect('data/quant.duckdb')
conn.execute("SET enable_progress_bar=false")

# ================================================================
# MA_CROSS 对比
# ================================================================

# 回测SQL - MA_CROSS
q_ma_bt = """
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
    SELECT m.security_id, m.trade_date, m.day_idx,
        m.ma5 / m.ma60 - 1 as ma_gap
    FROM price_ma m
    WHERE m.ma5 > m.ma60
      AND (SELECT m2.ma5 FROM price_ma m2 WHERE m2.security_id = m.security_id AND m2.day_idx < m.day_idx ORDER BY m2.day_idx DESC LIMIT 1) <=
          (SELECT m2.ma60 FROM price_ma m2 WHERE m2.security_id = m.security_id AND m2.day_idx < m.day_idx ORDER BY m2.day_idx DESC LIMIT 1)
      AND m.volume > m.vol_ma5 * 1.5
      AND m.amount >= 100000000
      AND ABS(m.change_pct) < 9.8
)
SELECT s.security_id, s.trade_date, s.day_idx, s.ma_gap as score
FROM signals s
WHERE s.trade_date = ?
ORDER BY s.ma_gap DESC
LIMIT 50
"""

# daily_picker.py - MA_CROSS SQL (正确的，带 vol_ma)
q_ma_pk = """
WITH hist AS (
    SELECT security_id, trade_date, close_price, volume, amount, change_pct,
           AVG(close_price) OVER (PARTITION BY security_id
                                   ORDER BY trade_date
                                   ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS ma_fast,
           AVG(close_price) OVER (PARTITION BY security_id
                                   ORDER BY trade_date
                                   ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS ma_slow,
           AVG(volume) OVER (PARTITION BY security_id
                              ORDER BY trade_date
                              ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS vol_ma
    FROM daily_bar
    WHERE trade_date BETWEEN DATE '2020-01-02' - INTERVAL '80' DAY
                          AND DATE '2020-01-02'
      AND amount >= 100000000
),
today AS (
    SELECT security_id, trade_date, close_price, volume, change_pct,
           ma_fast, ma_slow, ma_fast / ma_slow - 1 AS ma_gap,
           LAG(ma_fast, 1) OVER (PARTITION BY security_id ORDER BY trade_date) AS prev_ma_fast,
           LAG(ma_slow, 1) OVER (PARTITION BY security_id ORDER BY trade_date) AS prev_ma_slow
    FROM hist
    WHERE trade_date = DATE '2020-01-02'
      AND ma_fast IS NOT NULL AND ma_slow IS NOT NULL
)
SELECT security_id, close_price, volume, change_pct,
       ma_fast, ma_slow, ma_fast / ma_slow - 1 AS ma_gap,
       CASE WHEN prev_ma_fast <= prev_ma_slow AND ma_fast > ma_slow
                 AND volume >= vol_ma * 1.5
            THEN 1 ELSE 0 END AS golden_cross
FROM today
WHERE golden_cross = 1
ORDER BY ma_gap DESC
"""

# ================================================================
# MACD 对比
# ================================================================

q_macd_bt = """
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
        ABS(m.macd_diff_prev) as score
    FROM macline m
    WHERE m.macd_diff_prev IS NOT NULL AND m.macd_diff_prev <= 0
      AND m.volume > m.vol_ma5 * 1.3 AND m.amount >= 200000000
      AND ABS(m.change_pct) < 9.8
)
SELECT s.security_id, s.trade_date, s.day_idx, s.score
FROM signals s
WHERE s.trade_date = ?
ORDER BY s.score DESC
LIMIT 50
"""

# daily_picker.py - MACD SQL
q_macd_pk = """
WITH price_ma AS (
    SELECT security_id, trade_date, close_price, volume, amount, change_pct,
           AVG(close_price) OVER (PARTITION BY security_id
                                   ORDER BY trade_date
                                   ROWS BETWEEN 11 PRECEDING AND CURRENT ROW) AS ema_fast,
           AVG(close_price) OVER (PARTITION BY security_id
                                   ORDER BY trade_date
                                   ROWS BETWEEN 25 PRECEDING AND CURRENT ROW) AS ema_slow,
           AVG(volume) OVER (PARTITION BY security_id
                              ORDER BY trade_date
                              ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS vol_ma5
    FROM daily_bar
    WHERE trade_date BETWEEN DATE '2020-01-02' - INTERVAL '80' DAY
                          AND DATE '2020-01-02'
      AND amount >= 200000000
),
macd_base AS (
    SELECT security_id, trade_date, close_price, volume, change_pct,
           ema_fast - ema_slow AS macd_value,
           vol_ma5
    FROM price_ma
    WHERE ema_fast IS NOT NULL AND ema_slow IS NOT NULL
),
macd_with_signal AS (
    SELECT security_id, trade_date, close_price, volume, change_pct,
           macd_value, vol_ma5,
           AVG(macd_value) OVER (PARTITION BY security_id
                                   ORDER BY trade_date
                                   ROWS BETWEEN 8 PRECEDING AND CURRENT ROW) AS signal_line
    FROM macd_base
),
macd_lag AS (
    SELECT security_id, trade_date, close_price, volume, change_pct,
           macd_value, signal_line, vol_ma5,
           LAG(macd_value, 1) OVER (PARTITION BY security_id ORDER BY trade_date) AS prev_macd,
           LAG(signal_line, 1) OVER (PARTITION BY security_id ORDER BY trade_date) AS prev_signal
    FROM macd_with_signal
)
SELECT security_id, close_price, volume, change_pct,
       macd_value, signal_line,
       macd_value - signal_line AS diff,
       CASE WHEN prev_macd IS NOT NULL AND prev_signal IS NOT NULL
                 AND (prev_macd - prev_signal) <= 0
                 AND (macd_value - signal_line) > 0
            THEN 1 ELSE 0 END AS golden_cross,
       CASE WHEN volume >= vol_ma5 * 1.3 THEN 1 ELSE 0 END AS vol_ok
FROM macd_lag
WHERE trade_date = DATE '2020-01-02'
  AND golden_cross = 1 AND vol_ok = 1
ORDER BY diff DESC
"""

# ================================================================
# VOL_SURGE 对比
# ================================================================

q_vol_bt = """
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
LIMIT 50
"""

# daily_picker.py - VOL_SURGE SQL
q_vol_pk = """
WITH today AS (
    SELECT security_id, trade_date, open_price, close_price,
           high_price, low_price, volume, amount, change_pct,
           (close_price - open_price) / open_price * 100 AS day_change_pct,
           volume * 1.0 / COALESCE(
               AVG(volume) OVER (PARTITION BY security_id
                                 ORDER BY trade_date
                                 ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING), 1
           ) AS vol_ratio
    FROM daily_bar
    WHERE trade_date = DATE '2020-01-02'
      AND amount >= 100000000
      AND volume > 0
),
prev_days AS (
    SELECT security_id, trade_date, close_price,
           MAX(close_price) OVER (PARTITION BY security_id
                                   ORDER BY trade_date
                                   ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS max_prev_close
    FROM daily_bar
    WHERE trade_date BETWEEN DATE '2020-01-02' - INTERVAL '10' DAY
                          AND DATE '2020-01-02' - INTERVAL '1' DAY
      AND amount >= 100000000
),
prev_max AS (
    SELECT security_id, MAX(max_prev_close) AS max_prev_close
    FROM prev_days
    GROUP BY security_id
)
SELECT t.security_id, t.close_price, t.volume, t.change_pct,
       t.day_change_pct, t.vol_ratio,
       CASE WHEN p.max_prev_close IS NOT NULL
                 AND (p.max_prev_close - t.close_price) / p.max_prev_close * 100 <= 4.0
            THEN 1 ELSE 0 END AS pullback_ok
FROM today t
LEFT JOIN prev_max p ON t.security_id = p.security_id
WHERE t.day_change_pct BETWEEN 3.0 AND 8.0
  AND t.vol_ratio >= 1.5
  AND t.day_change_pct IS NOT NULL
ORDER BY t.day_change_pct DESC
"""

# ================================================================
# 测试对比
# ================================================================
test_date = '2020-01-02'

for name, q_bt, q_pk, params_bt in [
    ('MA_CROSS', q_ma_bt, q_ma_pk, ['2020-01-01', '2026-06-01', test_date]),
    ('MACD', q_macd_bt, q_macd_pk, ['2020-01-01', '2026-06-01', test_date]),
    ('VOL_SURGE', q_vol_bt, q_vol_pk, ['2020-01-01', '2026-06-01', test_date]),
]:
    print(f"\n{'='*60}")
    print(f"  {name}  对比测试 (信号日: {test_date})")
    print(f"{'='*60}")

    df_bt = conn.execute(q_bt, params_bt).df()
    print(f"\n回测SQL: {len(df_bt)} 个信号")

    try:
        df_pk = conn.execute(q_pk).df()
        print(f"Picker SQL: {len(df_pk)} 个信号")

        if len(df_bt) > 0 and len(df_pk) > 0:
            bt_ids = set(df_bt['security_id'].tolist())
            pk_ids = set(df_pk['security_id'].tolist())

            common = bt_ids & pk_ids
            only_bt = bt_ids - pk_ids
            only_pk = pk_ids - bt_ids

            print(f"\n两者共有: {len(common)} 只")
            print(f"仅回测SQL有: {len(only_bt)} 只 → {sorted(only_bt)[:10]}")
            print(f"仅Picker有: {len(only_pk)} 只 → {sorted(only_pk)[:10]}")
        elif len(df_bt) == 0 and len(df_pk) == 0:
            print("\n两者都无信号")
        elif len(df_bt) == 0:
            print(f"\n回测无信号，Picker有 {len(df_pk)} 个")
        else:
            print(f"\n回测有 {len(df_bt)} 个，Picker无信号")
    except Exception as e:
        print(f"\nPicker SQL 错误: {e}")

conn.close()
