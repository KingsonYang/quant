"""导入沪深300历史K线到DuckDB"""
import sys
sys.path.insert(0, 'D:/SoftWare/tdx/PYPlugins')
sys.path.insert(0, 'D:/SoftWare/tdx/PYPlugins/user')
from tqcenter import tq
import duckdb
import pandas as pd

tq.initialize('D:/SoftWare/tdx/TdxW.exe')
print('TdxQuant initialized')

# fill_data=False: dict of DataFrames keyed by stock code
bars = tq.get_market_data(
    field_list=[],
    stock_list=['000300.SH'],
    period='1d',
    start_time='20190101',
    end_time='20260603',
    dividend_type='none',
    fill_data=False
)

code = '000300.SH'
close = bars['Close'][code]
open_ = bars['Open'][code]
high = bars['High'][code]
low = bars['Low'][code]
volume = bars['Volume'][code]
amount = bars['Amount'][code]

print(f'获取到 {len(close)} 条K线')
print(f'日期范围: {close.index[0]} ~ {close.index[-1]}')

# 构建DataFrame
dates = pd.to_datetime(close.index).date
df = pd.DataFrame({
    'trade_date': dates,
    'open_price': open_.values,
    'high_price': high.values,
    'low_price': low.values,
    'close_price': close.values,
    'volume': volume.values,
    'amount': amount.values,
})
print(df.head(3))

# 写入DuckDB
conn = duckdb.connect('D:/Files/workspace-ai/quant/data/quant.duckdb')

conn.execute('''
CREATE SEQUENCE IF NOT EXISTS hs300_id;
CREATE TABLE IF NOT EXISTS hs300_bar (
    id BIGINT DEFAULT NEXTVAL('hs300_id'),
    trade_date DATE,
    open_price DECIMAL(18,4),
    high_price DECIMAL(18,4),
    low_price DECIMAL(18,4),
    close_price DECIMAL(18,4),
    volume BIGINT,
    amount DECIMAL(20,2)
)
''')

conn.execute("DELETE FROM hs300_bar")
conn.execute("INSERT INTO hs300_bar (trade_date, open_price, high_price, low_price, close_price, volume, amount) "
             "SELECT trade_date, open_price, high_price, low_price, close_price, volume, amount FROM df")

cnt = conn.execute("SELECT COUNT(*) FROM hs300_bar").fetchone()[0]
print(f'\n写入 {cnt} 条')

sample = conn.execute("SELECT * FROM hs300_bar ORDER BY trade_date DESC LIMIT 5").fetchdf()
print('最新5条:')
print(sample.to_string())

conn.close()
print('\n完成!')
