"""检查 security_master 字段以确定股票池过滤规则"""
import sys
sys.path.insert(0, 'D:/SoftWare/tdx/PYPlugins/user')
import os
os.environ['TDXPATH'] = 'D:/SoftWare/tdx/TdxW.exe'
import duckdb

conn = duckdb.connect('D:/Files/workspace-ai/quant/data/quant.duckdb')

# 1. security_master 全部字段
print("=" * 70)
print("security_master 全部字段")
print("=" * 70)
cols = conn.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'security_master'").fetchall()
for c in cols:
    print(f"  {c[0]}: {c[1]}")

# 2. 样本数据
print("\n样本（10条）")
print("=" * 70)
rows = conn.execute("""
    SELECT security_id, symbol, exchange, code, name, market, board, list_status
    FROM security_master
    LIMIT 10
""").fetchall()
cols = ['security_id', 'symbol', 'exchange', 'code', 'name', 'market', 'board', 'list_status']
for r in rows:
    print({c: v for c, v in zip(cols, r)})

# 3. board 的所有值
print("\nboard 分布")
print("=" * 70)
br = conn.execute("SELECT board, COUNT(*) as cnt FROM security_master GROUP BY board ORDER BY cnt DESC").fetchall()
for r in br:
    print(f"  {r[0]}: {r[1]}条")

# 4. exchange 分布
print("\nexchange 分布")
print("=" * 70)
ex = conn.execute("SELECT exchange, COUNT(*) as cnt FROM security_master GROUP BY exchange ORDER BY cnt DESC").fetchall()
for r in ex:
    print(f"  {r[0]}: {r[1]}条")

# 5. market 分布
print("\nmarket 分布")
print("=" * 70)
mk = conn.execute("SELECT market, COUNT(*) as cnt FROM security_master GROUP BY market ORDER BY cnt DESC").fetchall()
for r in mk:
    print(f"  {r[0]}: {r[1]}条")

# 6. symbol 特征（用前缀判断创业板/科创板/ST）
print("\n按symbol前缀分组")
print("=" * 70)
sym = conn.execute("""
    SELECT
        SUBSTR(symbol, 1, 3) as prefix,
        COUNT(*) as cnt
    FROM security_master
    GROUP BY prefix
    ORDER BY prefix
""").fetchall()
for r in sym:
    print(f"  {r[0]}: {r[1]}")

# 7. 找 ST 股票
print("\n按名称识别ST股票（样本）")
print("=" * 70)
st = conn.execute("""
    SELECT security_id, symbol, name
    FROM security_master
    WHERE name LIKE '%ST%' OR name LIKE '%*%'
    LIMIT 10
""").fetchall()
for r in st:
    print(f"  {r[0]} {r[1]} {r[2]}")

st_cnt = conn.execute("""
    SELECT COUNT(*) FROM security_master
    WHERE name LIKE '%ST%' OR name LIKE '%*%'
""").fetchone()
print(f"  ST股票总数: {st_cnt[0]}")

conn.close()
