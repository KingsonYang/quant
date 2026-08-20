"""探索 DuckDB 数据结构"""
import sys
sys.path.insert(0, 'D:/SoftWare/tdx/PYPlugins/user')
import os
os.environ['TDXPATH'] = 'D:/SoftWare/tdx/TdxW.exe'
import duckdb

conn = duckdb.connect('D:/Files/workspace-ai/quant/data/quant.duckdb')

# 1. daily_bar 样本
print("=" * 60)
print("daily_bar 样本（5条）")
print("=" * 60)
rows = conn.execute("SELECT * FROM daily_bar LIMIT 5").fetchall()
cols = [d[0] for d in conn.description]
for r in rows:
    print({c: v for c, v in zip(cols, r)})

# 2. daily_bar 数据范围
print("\n" + "=" * 60)
print("daily_bar 数据范围")
print("=" * 60)
range_q = conn.execute("""
    SELECT 
        MIN(trade_date) as start_date,
        MAX(trade_date) as end_date,
        COUNT(DISTINCT security_id) as stock_count,
        COUNT(*) as total_rows
    FROM daily_bar
""").fetchone()
print(f"  起始日期: {range_q[0]}")
print(f"  结束日期: {range_q[1]}")
print(f"  股票数量: {range_q[2]}")
print(f"  总行数: {range_q[3]}")

# 3. ths_daily_snapshot 样本（涨停相关）
print("\n" + "=" * 60)
print("ths_daily_snapshot 样本（同花顺，涨停数据）")
print("=" * 60)
ths_rows = conn.execute("""
    SELECT trade_date, security_id, symbol, name, 
           current_price, change_pct, turnover_ratio, 
           limit_type, consecutive_limit_days, first_limit_time
    FROM ths_daily_snapshot 
    WHERE limit_type IS NOT NULL
    LIMIT 5
""").fetchall()
ths_cols = [d[0] for d in conn.description]
for r in ths_rows:
    print({c: v for c, v in zip(ths_cols, r)})

# 4. ths_daily_snapshot 数据范围
print("\n" + "=" * 60)
print("ths_daily_snapshot 数据范围")
print("=" * 60)
ths_range = conn.execute("""
    SELECT 
        MIN(trade_date) as start_date,
        MAX(trade_date) as end_date,
        COUNT(DISTINCT security_id) as stock_count,
        COUNT(*) as total_rows,
        SUM(CASE WHEN limit_type = '涨停' THEN 1 ELSE 0 END) as limit_up_count
    FROM ths_daily_snapshot
""").fetchone()
print(f"  起始日期: {ths_range[0]}")
print(f"  结束日期: {ths_range[1]}")
print(f"  股票数量: {ths_range[2]}")
print(f"  总行数: {ths_range[3]}")
print(f"  涨停记录数: {ths_range[4]}")

# 5. security_master 样本
print("\n" + "=" * 60)
print("security_master 样本（5条）")
print("=" * 60)
sec_rows = conn.execute("""
    SELECT security_id, symbol, code, name, exchange, market, board, list_status
    FROM security_master 
    WHERE list_status = '上市'
    LIMIT 5
""").fetchall()
sec_cols = [d[0] for d in conn.description]
for r in sec_rows:
    print({c: v for c, v in zip(sec_cols, r)})

# 6. 查看现有回测结果
print("\n" + "=" * 60)
print("已有回测结果（最新5条）")
print("=" * 60)
br_rows = conn.execute("""
    SELECT r.run_id, r.strategy_id, r.run_name, r.start_date, r.end_date,
           r.initial_cash, r.run_status,
           m.metric_name, m.metric_value
    FROM backtest_run r
    LEFT JOIN run_metric m ON r.run_id = m.run_id AND m.metric_name IN ('total_return','annual_return','max_drawdown','sharpe_ratio')
    ORDER BY r.created_at DESC
    LIMIT 10
""").fetchall()
br_cols = [d[0] for d in conn.description]
for r in br_rows:
    print({c: v for c, v in zip(br_cols, r)})

conn.close()
