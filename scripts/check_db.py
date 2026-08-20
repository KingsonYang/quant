"""检查 DuckDB 数据库现有表结构"""
import sys
sys.path.insert(0, 'D:/SoftWare/tdx/PYPlugins/user')
import os
os.environ['TDXPATH'] = 'D:/SoftWare/tdx/TdxW.exe'
import duckdb

conn = duckdb.connect('D:/Files/workspace-ai/quant/data/quant.duckdb')
tables = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()
print('现有表：', [t[0] for t in tables])
for t in tables:
    name = t[0]
    cols = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = ?", [name]).fetchall()
    count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    print(f'\n{name}: {count}行')
    print(f'  字段: {[c[0] for c in cols]}')

conn.close()
