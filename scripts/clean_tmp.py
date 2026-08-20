"""清理临时duckdb文件"""
import os
files = [
    "D:/Files/workspace-ai/quant/data/_tmp_quant.duckdb",
    "D:/Files/workspace-ai/quant/data/_tmp_v4.duckdb",
]
for f in files:
    if os.path.exists(f):
        size = os.path.getsize(f) / 1e9
        print(f"删除 {f} ({size:.2f} GB)")
        os.remove(f)
        print(f"✅ 已删除")
print("\n当前文件：")
for f in os.listdir("D:/Files/workspace-ai/quant/data"):
    if f.endswith(".duckdb"):
        path = f"D:/Files/workspace-ai/quant/data/{f}"
        print(f"  {f}: {os.path.getsize(path)/1e9:.2f} GB")
