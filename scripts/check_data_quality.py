"""确认回测所需数据质量"""
import sys
sys.path.insert(0, 'D:/SoftWare/tdx/PYPlugins/user')
import os
os.environ['TDXPATH'] = 'D:/SoftWare/tdx/TdxW.exe'
import duckdb

conn = duckdb.connect('D:/Files/workspace-ai/quant/data/quant.duckdb')

# 1. ths_daily_snapshot 里有多少种 limit_type
print("=" * 60)
print("ths_daily_snapshot 的 limit_type 分布")
print("=" * 60)
lt = conn.execute("""
    SELECT limit_type, COUNT(*) as cnt
    FROM ths_daily_snapshot
    GROUP BY limit_type
    ORDER BY cnt DESC
""").fetchall()
for r in lt:
    print(f"  {r[0]}: {r[1]}条")

# 2. daily_bar 中 2024年至今有多少交易日
print("\n" + "=" * 60)
print("daily_bar 交易日分布")
print("=" * 60)
cal = conn.execute("""
    SELECT
        COUNT(DISTINCT trade_date) as total_days,
        SUM(CASE WHEN trade_date >= '2024-01-01' THEN 1 ELSE 0 END) as days_2024_plus,
        SUM(CASE WHEN trade_date >= '2026-01-01' THEN 1 ELSE 0 END) as days_2026
    FROM daily_bar
""").fetchone()
print(f"  总交易日: {cal[0]}")
print(f"  2024年以来: {cal[1]}")
print(f"  2026年以来: {cal[2]}")

# 3. 确认 amount 单位（看一条大票的数据）
print("\n" + "=" * 60)
print("验证 amount 单位（用平安银行 000001.SZ）")
print("=" * 60)
sample = conn.execute("""
    SELECT trade_date, close_price, volume, amount, turnover_ratio
    FROM daily_bar
    WHERE security_id = 1
    ORDER BY trade_date DESC
    LIMIT 3
""").fetchall()
for r in sample:
    close, vol, amt, tr = float(r[1]), r[2], float(r[3]), float(r[4])
    # amount(元) / close / volume = ?
    if close > 0 and vol > 0:
        price_per_share = amt / vol
        shares_outstanding = vol / (tr / 100) if tr > 0 else 0
        mktcap = amt / 1e8 if amt > 0 else 0
        print(f"  {r[0]}: close={close}, amount={amt:,.0f}元, vol={vol:,}")
        print(f"    算出来每股价格: {price_per_share:.2f}元, 市值估算: {mktcap:.2f}亿元")

# 4. 看2024年涨停股数量（用change_pct>9.9%）
print("\n" + "=" * 60)
print("2024年以来每日涨停股数量统计")
print("=" * 60)
zt = conn.execute("""
    SELECT
        COUNT(DISTINCT security_id) as zt_count_per_day,
        COUNT(*) as total_records
    FROM daily_bar
    WHERE trade_date >= '2024-01-01'
      AND change_pct > 9.9
""").fetchone()
print(f"  涨停记录总数: {zt[1]}")
avg_zhu = conn.execute("""
    SELECT AVG(cnt) as avg_daily_zt
    FROM (
        SELECT trade_date, COUNT(*) as cnt
        FROM daily_bar
        WHERE trade_date >= '2024-01-01'
          AND change_pct > 9.9
        GROUP BY trade_date
    )
""").fetchone()
print(f"  日均涨停股数: {avg_zhu[0]:.1f}")

# 5. 看 backtest_run 里的完整策略定义
print("\n" + "=" * 60)
print("已有策略定义")
print("=" * 60)
strats = conn.execute("""
    SELECT strategy_id, strategy_name, strategy_key, strategy_type, description
    FROM strategy_definition
""").fetchall()
for r in strats:
    print(f"  [{r[0]}] {r[1]} ({r[2]}) - {r[3]}")
    print(f"    描述: {r[4]}")

conn.close()
