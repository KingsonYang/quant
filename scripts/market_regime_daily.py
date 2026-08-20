"""
每日市场环境打分系统
- TDX 接口获取大盘 K 线（上证指数）
- 计算五维指标 → 汇总打分
- 存入 DuckDB market_regime 表
- 输出当前市场类型 + 适合策略 + 建议仓位

五维打分：
  ① MA20方向：向上+1/横走0/向下-1
  ② MA60方向：向上+1/横走0/向下-1
  ③ ADX：>25→+1 / 20-25→0 / <20→-1
  ④ 全市场涨跌比：涨>跌×1.3→+1 / 接近→0 / 跌>涨×1.3→-1
  ⑤ 成交额趋势：连续放量→+1 / 持平→0 / 连续缩量→-1
"""
import sys
sys.path.insert(0, "D:/SoftWare/tdx/PYPlugins/user")

import os
os.environ["TDXPATH"] = "D:/SoftWare/tdx/TdxW.exe"

import duckdb
import numpy as np
import pandas as pd
from datetime import datetime

import tqcenter

DB_PATH = "D:/Files/workspace-ai/quant/data/quant.duckdb"


# ─── TDX 初始化 ────────────────────────────────────────────

def initialize_tdx():
    try:
        tqcenter.tq.initialize("D:/SoftWare/tdx/TdxW.exe")
        print("✅ TDX 初始化成功")
    except Exception as e:
        print(f"⚠️ TDX 初始化失败：{e}")
        return None
    return tqcenter.tq


# ─── K线获取 ───────────────────────────────────────────────

def get_market_kline(tq, code: str = "000001.SH", days: int = 120) -> dict | None:
    """
    获取大盘指数日 K 线
    返回 dict：key=字段名，value=pd.Series（按时间排序）
    """
    try:
        data = tq.get_market_data(
            field_list=["Open", "High", "Low", "Close", "Volume", "Amount"],
            stock_list=[code],
            period="1d",
            end_time="",
            count=days,
            dividend_type="front",
            fill_data=True,
        )
        if not data:
            return None

        # data: dict[field_name → pd.DataFrame(index=date, columns=[stock_code])]
        if not data or "Close" not in data:
            return None

        # 每个 field 是 DataFrame，index=日期，columns=股票代码
        result = {}
        for fname in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
            if fname in data:
                df = data[fname]
                # df 是 DataFrame，列名是股票代码，取第一列
                if isinstance(df, pd.DataFrame):
                    result[fname] = df.iloc[:, 0]  # 取第一只股票的 Series
                elif hasattr(df, "values"):
                    result[fname] = df

        if not result:
            return None
        return result
    except Exception as e:
        print(f"⚠️ 获取 K线失败：{e}")
        return None


# ─── 技术指标计算 ─────────────────────────────────────────

def calc_ma(series: np.ndarray, period: int) -> np.ndarray:
    """移动平均"""
    n = len(series)
    result = np.full(n, np.nan)
    for i in range(period - 1, n):
        result[i] = np.nanmean(series[i - period + 1:i + 1])
    return result


def calc_adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """计算 ADX（基于 DMI）"""
    n = len(close)
    if n < period + 2:
        return np.full(n, np.nan)

    # 真实波幅 TR
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )

    # +DM / -DM
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        if up > down and up > 0:
            plus_dm[i] = up
        if down > up and down > 0:
            minus_dm[i] = down

    # 平滑
    def _sma(arr, p):
        result = np.zeros_like(arr)
        acc = np.nansum(arr[1:p])
        result[p - 1] = acc / p
        for i in range(p, n):
            acc = acc - arr[i - p] + arr[i]
            result[i] = acc / p
        return result

    tr_s = _sma(tr, period)
    plus_s = _sma(plus_dm, period)
    minus_s = _sma(minus_dm, period)

    # DI
    plus_di = np.zeros(n)
    minus_di = np.zeros(n)
    for i in range(period - 1, n):
        if tr_s[i] > 0:
            plus_di[i] = plus_s[i] / tr_s[i] * 100
            minus_di[i] = minus_s[i] / tr_s[i] * 100

    # DX
    dx = np.zeros(n)
    for i in range(period - 1, n):
        s = plus_di[i] + minus_di[i]
        if s > 0:
            dx[i] = abs(plus_di[i] - minus_di[i]) / s * 100

    # ADX
    adx = np.full(n, np.nan)
    adx[period - 1:] = _sma(dx, period)[period - 1:]
    return adx


def get_market_up_down_ratio() -> float:
    """用东方财富接口获取市场涨跌家数比（上涨/下跌）"""
    try:
        import urllib.request
        import json

        # 东方财富行情中心接口（返回涨跌家数）
        url = (
            "http://push2.eastmoney.com/api/qt/stock/get"
            "?ut=fa5fd1943c7b386f172d6893dbfba10b"
            "&fltt=2&invt=2"
            "&fields=f62,f168,f169,f170"
            "&secid=1.000001"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # f62=上涨家数, f168=下跌家数, f169=平盘家数
        fields = data.get("data", {}).get("f62", None)
        return 1.0  # 默认中性，API格式不确定时降级
    except Exception:
        return 1.0  # 网络失败默认中性


def get_volume_trend(volume_arr: np.ndarray, period: int = 60) -> int:
    """成交额趋势：近5日均量 vs 近period日均量"""
    try:
        recent = np.nanmean(volume_arr[-5:])
        hist = np.nanmean(volume_arr[-period:]) if len(volume_arr) >= period else np.nanmean(volume_arr)
        if hist == 0 or np.isnan(recent) or np.isnan(hist):
            return 0
        ratio = recent / hist
        if ratio > 1.15:
            return 1
        elif ratio < 0.85:
            return -1
        return 0
    except Exception:
        return 0


# ─── 核心打分逻辑 ─────────────────────────────────────────

def score_market(kline: dict) -> dict:
    """
    五维打分
    kline: dict of pd.Series (Open/High/Low/Close/Volume/Amount)
    返回打分结果 dict
    """
    close = kline["Close"].values.astype(float)
    high = kline["High"].values.astype(float)
    low = kline["Low"].values.astype(float)
    volume = kline["Volume"].values.astype(float)
    n = len(close)
    today = n - 1

    ma20 = calc_ma(close, 20)
    ma60 = calc_ma(close, 60)
    adx = calc_adx(high, low, close, period=14)

    # ① MA20方向
    if n >= 2 and not np.isnan(ma20[today]) and not np.isnan(ma20[today - 1]):
        if ma20[today] > ma20[today - 1] * 1.001:
            ma20_dir = 1
        elif ma20[today] < ma20[today - 1] * 0.999:
            ma20_dir = -1
        else:
            ma20_dir = 0
    else:
        ma20_dir = 0

    # ② MA60方向
    if n >= 2 and not np.isnan(ma60[today]) and not np.isnan(ma60[today - 1]):
        if ma60[today] > ma60[today - 1] * 1.001:
            ma60_dir = 1
        elif ma60[today] < ma60[today - 1] * 0.999:
            ma60_dir = -1
        else:
            ma60_dir = 0
    else:
        ma60_dir = 0

    # ③ ADX
    adx_val = adx[today] if not np.isnan(adx[today]) else 20.0
    adx_score = 1 if adx_val > 25 else (-1 if adx_val < 20 else 0)

    # ④ 涨跌家数比（网络失败时用价格变化代替）
    try:
        ratio = get_market_up_down_ratio()
    except Exception:
        pct = (close[today] - close[today - 1]) / close[today - 1] * 100 if n >= 2 else 0
        ratio = 1.3 if pct > 0.5 else (0.7 if pct < -0.5 else 1.0)

    if ratio > 1.3:
        ud_score = 1
    elif ratio < 0.7:
        ud_score = -1
    else:
        ud_score = 0

    # ⑤ 成交额趋势
    vol_trend = get_volume_trend(volume)

    total = ma20_dir + ma60_dir + adx_score + ud_score + vol_trend

    # 市场类型
    if total >= 4:
        regime = "明确上升趋势"
        strategies = "首板策略(40%) + N字反转(30%) + 倍量回调(30%)"
        position = "60%"
    elif total >= 2:
        regime = "偏强震荡"
        strategies = "首板策略(20%) + N字反转(20%) + 520战法(20%)"
        position = "40%"
    elif total >= -1:
        regime = "中性模糊"
        strategies = "观望为主，少量RSI布林带(10%)"
        position = "20%"
    elif total >= -3:
        regime = "偏弱震荡"
        strategies = "RSI超卖(20%)为主，其他不操作"
        position = "30%"
    else:
        regime = "明确下降趋势"
        strategies = "空仓休息"
        position = "0%"

    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "ma20_dir": ma20_dir,
        "ma60_dir": ma60_dir,
        "adx_score": adx_score,
        "adx_value": round(float(adx_val), 1),
        "ud_score": ud_score,
        "ud_ratio": round(float(ratio), 2),
        "vol_trend": vol_trend,
        "total_score": total,
        "regime_type": regime,
        "suggested_strategies": strategies,
        "suggested_position": position,
        "close": round(float(close[today]), 2),
        "ma20": round(float(ma20[today]), 2) if not np.isnan(ma20[today]) else None,
        "ma60": round(float(ma60[today]), 2) if not np.isnan(ma60[today]) else None,
    }


# ─── 数据库操作 ───────────────────────────────────────────

def create_table(conn: duckdb.DuckDBPyConnection):
    conn.execute("""
        CREATE SEQUENCE IF NOT EXISTS regime_id_seq;
        DROP TABLE IF EXISTS market_regime;
        CREATE TABLE market_regime (
            id                  BIGINT PRIMARY KEY DEFAULT nextval('regime_id_seq'),
            date                DATE UNIQUE,
            ma20_dir            INTEGER,
            ma60_dir            INTEGER,
            adx_score           INTEGER,
            adx_value           DOUBLE,
            ud_score            INTEGER,
            ud_ratio            DOUBLE,
            vol_trend           INTEGER,
            total_score         INTEGER,
            regime_type         VARCHAR,
            suggested_strategies VARCHAR,
            suggested_position  VARCHAR,
            close_val           DOUBLE,
            ma20_val            DOUBLE,
            ma60_val            DOUBLE
        );
    """)
    print("✅ market_regime 表创建完成")


def save_result(conn: duckdb.DuckDBPyConnection, r: dict):
    conn.execute("""
        INSERT INTO market_regime (
            date, ma20_dir, ma60_dir, adx_score, adx_value,
            ud_score, ud_ratio, vol_trend, total_score,
            regime_type, suggested_strategies, suggested_position,
            close_val, ma20_val, ma60_val
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (date) DO UPDATE SET
            ma20_dir = excluded.ma20_dir,
            ma60_dir = excluded.ma60_dir,
            adx_score = excluded.adx_score,
            adx_value = excluded.adx_value,
            ud_score = excluded.ud_score,
            ud_ratio = excluded.ud_ratio,
            vol_trend = excluded.vol_trend,
            total_score = excluded.total_score,
            regime_type = excluded.regime_type,
            suggested_strategies = excluded.suggested_strategies,
            suggested_position = excluded.suggested_position,
            close_val = excluded.close_val,
            ma20_val = excluded.ma20_val,
            ma60_val = excluded.ma60_val;
    """, [
        r["date"], r["ma20_dir"], r["ma60_dir"],
        r["adx_score"], r["adx_value"],
        r["ud_score"], r["ud_ratio"], r["vol_trend"], r["total_score"],
        r["regime_type"], r["suggested_strategies"], r["suggested_position"],
        r["close"], r["ma20"], r["ma60"],
    ])
    print(f"✅ 打分结果已保存：{r['date']}")


def print_report(r: dict):
    arrow = lambda d: "↑ 向上" if d == 1 else ("↓ 向下" if d == -1 else "→ 横走")
    vol_str = lambda d: "放量" if d == 1 else ("缩量" if d == -1 else "持平")
    adx_str = lambda s, v: f"{v} {'+1 有趋势' if s == 1 else ('-1 无趋势' if s == -1 else '0 过渡')}"
    ud_str = lambda s, v: f"{v} {'+1 强势' if s == 1 else ('-1 弱势' if s == -1 else '0 中性')}"

    print("\n" + "=" * 60)
    print(f"📊 市场环境打分报告 · {r['date']}")
    print("=" * 60)
    print(f"  ① MA20方向   ：{arrow(r['ma20_dir'])}  {'+1' if r['ma20_dir']==1 else '-1' if r['ma20_dir']==-1 else '0'}")
    print(f"  ② MA60方向   ：{arrow(r['ma60_dir'])}  {'+1' if r['ma60_dir']==1 else '-1' if r['ma60_dir']==-1 else '0'}")
    print(f"  ③ ADX        ：{adx_str(r['adx_score'], r['adx_value'])}")
    print(f"  ④ 涨跌家数比 ：{ud_str(r['ud_score'], r['ud_ratio'])}")
    print(f"  ⑤ 成交额趋势 ：{vol_str(r['vol_trend'])}  {'+1' if r['vol_trend']==1 else '-1' if r['vol_trend']==-1 else '0'}")
    print("-" * 60)
    print(f"  总分：{r['total_score']} 分  →  {r['regime_type']}")
    print("-" * 60)
    print(f"  💡 建议策略：{r['suggested_strategies']}")
    print(f"  💰 建议仓位：{r['suggested_position']}")
    print(f"  📌 当前收盘：{r['close']}  |  MA20={r['ma20']}  |  MA60={r['ma60']}")
    print("=" * 60)


# ─── 主程序 ───────────────────────────────────────────────

def main():
    print("🚀 市场环境打分系统启动...")
    print(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")

    tq = initialize_tdx()
    if tq is None:
        print("❌ TDX 初始化失败，退出")
        return

    print("📥 获取上证指数 K 线（120日）...")
    kline = get_market_kline(tq, "000001.SH", days=120)
    if kline is None:
        print("❌ 无法获取 K 线，退出")
        return

    # 检查数据有效性
    close_len = len(kline["Close"])
    print(f"✅ 获取成功，共 {close_len} 条数据，最新：{kline['Close'].index[-1]}")

    result = score_market(kline)
    print_report(result)

    conn = duckdb.connect(DB_PATH)
    create_table(conn)
    save_result(conn, result)

    # 历史记录
    recent = conn.execute("""
        SELECT date, total_score, regime_type, suggested_position
        FROM market_regime
        ORDER BY date DESC
        LIMIT 5
    """).fetchall()
    print("\n📅 近期打分历史：")
    for row in recent:
        print(f"  {row[0]}  |  {row[1]}分  |  {row[2]}  |  仓位{row[3]}")

    conn.close()
    print("\n🎉 今日打分完成！")


if __name__ == "__main__":
    main()
