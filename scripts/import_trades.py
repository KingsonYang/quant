"""
历史交割单导入 DuckDB
- 读取 2026-trades-raw.txt
- 解析字段，清理脏数据
- 存入 DuckDB trades 表
- 支持买卖配对计算盈亏
"""
import re
from datetime import datetime
import duckdb

DB_PATH = "D:/Files/workspace-ai/quant/data/quant.duckdb"
TRADES_FILE = "D:/Files/hermes/wiki/projects/a-stock-trade-log/data/2026-trades-raw.txt"


def parse_market_name(raw: str) -> str:
    """解析市场名称：深圳Ａ股 → SZ / 上海Ａ股 → SH"""
    if "深圳" in raw:
        return "SZ"
    elif "上海" in raw:
        return "SH"
    return raw.strip()


def normalize_code(code: str) -> str:
    """把纯数字代码标准化为字符串，去前导零"""
    code = code.strip()
    # 6位股票代码：直接返回（A股）
    if len(code) == 6:
        return code
    # 不足6位说明有前导零，去掉后补齐
    return code.lstrip("0").zfill(6)


def build_full_code(code: str, market: str) -> str:
    """组合完整代码：000001 + SZ → 000001.SZ"""
    c = normalize_code(code)
    suffix = ".SZ" if market == "SZ" else ".SH"
    return c + suffix


def load_trades_file(path: str) -> list[dict]:
    """读取并解析交割单txt"""
    records = []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # 跳过前4行（标题/说明），第5行是表头
    header_line = None
    data_lines = []
    for i, line in enumerate(lines):
        if i < 4:
            continue
        line = line.strip()
        if not line:
            continue
        if header_line is None:
            header_line = line
            continue
        data_lines.append(line)

    # 解析表头
    headers = header_line.split("\t")
    print(f"表头字段: {headers}")
    print(f"数据行数: {len(data_lines)}")

    for line in data_lines:
        cols = line.split("\t")
        if len(cols) < 17:
            print(f"⚠️ 跳过异常行（字段数={len(cols)}）: {line[:60]}")
            continue

        raw_code = cols[2].strip()
        market_raw = cols[16].strip()
        action = cols[4].strip()
        market = parse_market_name(market_raw)

        # 跳过债券回购（131810 R-001 等）
        if raw_code.startswith("131") or "融券" in action or "购回" in action:
            continue

        full_code = build_full_code(raw_code, market)

        # 成交日期转 YYYY-MM-DD
        date_raw = cols[0].strip()  # 20260519
        date_fmt = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"

        # 时间
        time_str = cols[1].strip()

        record = {
            "trade_date": date_fmt,
            "trade_time": time_str,
            "code": full_code,
            "code_raw": raw_code,
            "name": cols[3].strip(),
            "action": action,
            "quantity": int(cols[5]),
            "price": float(cols[7]),
            "amount": float(cols[8]),
            "net_amount": float(cols[11]),
            "commission": float(cols[12]),
            "stamp_duty": float(cols[13]),
            "other_fee": float(cols[14]),
            "market": market,
        }
        records.append(record)

    print(f"✅ 解析完成，有效交易记录: {len(records)} 条")
    return records


def create_table(conn: duckdb.DuckDBPyConnection):
    """创建 trades 表"""
    conn.execute("""
        CREATE SEQUENCE IF NOT EXISTS trades_id_seq;
        DROP TABLE IF EXISTS trades;
        CREATE TABLE trades (
            id          BIGINT PRIMARY KEY DEFAULT nextval('trades_id_seq'),
            trade_date  DATE,
            trade_time  VARCHAR,
            code        VARCHAR,
            code_raw    VARCHAR,
            name        VARCHAR,
            action      VARCHAR,
            quantity    INTEGER,
            price       DOUBLE,
            amount      DOUBLE,
            net_amount  DOUBLE,
            commission  DOUBLE,
            stamp_duty DOUBLE,
            other_fee   DOUBLE,
            market      VARCHAR,
            -- 买卖配对后填入
            paired_id   BIGINT,      -- 配对的那笔交易ID（买入配卖出，或卖出配买入）
            pnl         DOUBLE,      -- 本笔盈亏（仅配对卖出时有值）
            pnl_pct     DOUBLE,      -- 盈亏比例（仅配对卖出时有值）
            matched     BOOLEAN DEFAULT FALSE  -- 是否已完成配对
        );
    """)
    print("✅ trades 表创建完成")


def insert_records(conn: duckdb.DuckDBPyConnection, records: list[dict]):
    """批量插入记录"""
    for r in records:
        conn.execute("""
            INSERT INTO trades (
                trade_date, trade_time, code, code_raw, name, action,
                quantity, price, amount, net_amount,
                commission, stamp_duty, other_fee, market
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            r["trade_date"], r["trade_time"], r["code"], r["code_raw"],
            r["name"], r["action"], r["quantity"], r["price"], r["amount"],
            r["net_amount"], r["commission"], r["stamp_duty"], r["other_fee"],
            r["market"],
        ])

    total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    print(f"✅ 插入完成，当前共 {total} 条记录")


def pair_trades(conn: duckdb.DuckDBPyConnection):
    """
    买卖配对：FIFO 方式
    配对逻辑：
    - 遍历所有"卖出"记录
    - 在同一只股票中找最接近的"未配对买入"
    - 计算 pnl = 卖出净额 - 买入净额
    - 配对卖出记录
    """
    sells = conn.execute("""
        SELECT id, code, name, trade_date, quantity, price,
               net_amount, commission, stamp_duty
        FROM trades
        WHERE action = '卖出' AND matched = FALSE
        ORDER BY code, trade_date, trade_time
    """).fetchall()

    paired_count = 0
    for sell in sells:
        sid, code, name, trade_date, qty, price = sell[0], sell[1], sell[2], sell[3], sell[4], sell[5]
        net_amount = sell[6]

        # 找对应股票的未配对买入（同一只股票，按时间顺序）
        buys = conn.execute("""
            SELECT id, quantity, net_amount, commission
            FROM trades
            WHERE action = '买入' AND code = ? AND matched = FALSE
            ORDER BY trade_date, trade_time
        """, [code]).fetchall()

        if not buys:
            print(f"⚠️ 卖出 {code} {trade_date} 无法配对买入")
            continue

        # FIFO：找第一笔买入
        buy = buys[0]
        buy_id, buy_qty, buy_net, buy_comm = buy

        # 按数量配对
        if buy_qty >= qty:
            # 买入数量足够，扣减买入数量（可能部分配对）
            left_qty = buy_qty - qty
            conn.execute("UPDATE trades SET quantity = ? WHERE id = ?", [left_qty, buy_id])
            this_qty = qty
        else:
            # 买入数量不够，全部配对，标记买入已配对
            conn.execute("UPDATE trades SET matched = TRUE WHERE id = ?", [buy_id])
            this_qty = buy_qty

        # 计算盈亏
        total_cost = abs(buy_net) + buy_comm  # 买入总成本（含手续费）
        total_proceeds = abs(net_amount) - conn.execute(
            "SELECT stamp_duty FROM trades WHERE id = ?", [sid]
        ).fetchone()[0]  # 卖出净得（扣印花税前）
        pnl = total_proceeds - total_cost
        pnl_pct = pnl / total_cost * 100 if total_cost > 0 else 0

        # 更新卖出记录
        conn.execute("""
            UPDATE trades
            SET paired_id = ?, pnl = ?, pnl_pct = ?, matched = TRUE
            WHERE id = ?
        """, [buy_id, pnl, pnl_pct, sid])

        paired_count += 1

    print(f"✅ 配对完成，共配对 {paired_count} 笔卖出")


def summary(conn: duckdb.DuckDBPyConnection):
    """打印汇总统计"""
    print("\n" + "=" * 50)
    print("📊 交割单统计")
    print("=" * 50)

    total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    print(f"总记录数：{total}")

    by_action = conn.execute("""
        SELECT action, COUNT(*), SUM(quantity)
        FROM trades GROUP BY action
    """).fetchall()
    for row in by_action:
        print(f"  {row[0]}：{row[1]} 笔，{row[2]} 股")

    # 盈亏统计（仅配对卖出）
    pnl_stats = conn.execute("""
        SELECT
            COUNT(*) as trades,
            ROUND(AVG(pnl), 2) as avg_pnl,
            ROUND(SUM(pnl), 2) as total_pnl,
            ROUND(AVG(pnl_pct), 2) as avg_pnl_pct,
            ROUND(MAX(pnl), 2) as max_pnl,
            ROUND(MIN(pnl), 2) as min_pnl
        FROM trades
        WHERE action = '卖出' AND pnl IS NOT NULL
    """).fetchone()
    if pnl_stats:
        print(f"\n卖出盈亏统计（{pnl_stats[0]} 笔配对卖出）：")
        print(f"  均盈亏：{pnl_stats[1]} 元")
        print(f"  总盈亏：{pnl_stats[2]} 元")
        print(f"  均盈亏%：{pnl_stats[3]}%")
        print(f"  最大盈利：{pnl_stats[4]} 元")
        print(f"  最大亏损：{pnl_stats[5]} 元")

    # 胜率
    wins = conn.execute("SELECT COUNT(*) FROM trades WHERE action='卖出' AND pnl > 0").fetchone()[0]
    losses = conn.execute("SELECT COUNT(*) FROM trades WHERE action='卖出' AND pnl <= 0").fetchone()[0]
    total_sells = wins + losses
    if total_sells > 0:
        print(f"\n胜率：{wins}/{total_sells} = {wins/total_sells*100:.1f}%")

    # 按股票统计
    print("\n按股票盈亏排行（Top10）：")
    by_stock = conn.execute("""
        SELECT code, name,
               COUNT(*) as trades,
               ROUND(SUM(pnl), 2) as total_pnl,
               ROUND(AVG(pnl), 2) as avg_pnl
        FROM trades
        WHERE action = '卖出' AND pnl IS NOT NULL
        GROUP BY code, name
        ORDER BY total_pnl DESC
        LIMIT 10
    """).fetchall()
    for i, row in enumerate(by_stock, 1):
        print(f"  {i}. {row[0]} {row[1]} | {row[2]}笔 | 总盈亏 {row[3]}元 | 均盈亏 {row[4]}元")

    # 按月统计
    print("\n按月盈亏：")
    by_month = conn.execute("""
        SELECT
            strftime(trade_date, '%Y-%m') as month,
            COUNT(*) as trades,
            ROUND(SUM(pnl), 2) as total_pnl
        FROM trades
        WHERE action = '卖出' AND pnl IS NOT NULL
        GROUP BY month
        ORDER BY month
    """).fetchall()
    for row in by_month:
        print(f"  {row[0]} | {row[1]}笔 | 盈亏 {row[2]}元")


def write_sql_file():
    """写常用 SQL 查询到文件"""
    sql_content = """-- A股交割单常用查询
-- 文件：D:/Files/workspace-ai/quant/data/trades_queries.sql

-- 1. 按月统计胜率/均赚/均亏
SELECT
    strftime(trade_date, '%Y-%m') as month,
    COUNT(*) as trades,
    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
    ROUND(AVG(pnl), 2) as avg_pnl,
    ROUND(SUM(pnl), 2) as total_pnl,
    ROUND(MAX(pnl), 2) as max_win,
    ROUND(MIN(pnl), 2) as max_loss
FROM trades
WHERE action = '卖出' AND pnl IS NOT NULL
GROUP BY month
ORDER BY month;

-- 2. 按股票统计胜率
SELECT
    code, name,
    COUNT(*) as trades,
    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
    ROUND(SUM(CASE WHEN pnl > 0 THEN 1.0 ELSE 0 END) / COUNT(*) * 100, 1) as win_rate,
    ROUND(SUM(pnl), 2) as total_pnl,
    ROUND(AVG(pnl), 2) as avg_pnl
FROM trades
WHERE action = '卖出' AND pnl IS NOT NULL
GROUP BY code, name
ORDER BY total_pnl DESC
LIMIT 20;

-- 3. 亏损最大的10笔交易
SELECT
    id, trade_date, code, name, quantity, price,
    pnl, pnl_pct
FROM trades
WHERE action = '卖出' AND pnl IS NOT NULL
ORDER BY pnl ASC
LIMIT 10;

-- 4. 盈利最大的10笔交易
SELECT
    id, trade_date, code, name, quantity, price,
    pnl, pnl_pct
FROM trades
WHERE action = '卖出' AND pnl IS NOT NULL
ORDER BY pnl DESC
LIMIT 10;

-- 5. 某只股票的全部交易明细
SELECT * FROM trades
WHERE code LIKE '%000001%'
ORDER BY trade_date, trade_time;
"""
    with open("D:/Files/workspace-ai/quant/data/trades_queries.sql", "w", encoding="utf-8") as f:
        f.write(sql_content)
    print("✅ 常用SQL查询文件已写入：D:/Files/workspace-ai/quant/data/trades_queries.sql")


def main():
    print("🚀 开始导入交割单到 DuckDB...")
    print(f"源文件：{TRADES_FILE}")
    print(f"目标库：{DB_PATH}")

    # 连接 DuckDB
    conn = duckdb.connect(DB_PATH)

    # 创建表
    create_table(conn)

    # 解析文件
    records = load_trades_file(TRADES_FILE)

    # 插入
    insert_records(conn, records)

    # 配对
    pair_trades(conn)

    # 统计
    summary(conn)

    # 写 SQL 文件
    write_sql_file()

    conn.close()
    print("\n🎉 导入完成！")


if __name__ == "__main__":
    main()
