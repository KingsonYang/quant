"""
共享信号查询模块 - 回测和选股共用
==================================

这个模块是 combo_verify_optimized.py 和 daily_picker_simple.py 共用的 SQL 源。
任何 SQL 改动只需要改这里一次,两边自动一致。

【重要】所有 SQL 行为必须 100% 跟原 combo_verify_optimized.py 保持一致!
"""

import duckdb
import pandas as pd

# ══════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════

MA_SLOW = 60  # MA慢线窗口
HOLDING_DAYS_DEFAULT = 5  # 默认持仓天数
BULL_HOLDING_DAYS = 8  # 牛市持仓天数(A4 自适应)
BEAR_HOLDING_DAYS = 3  # 熊市持仓天数(A4 自适应)


# ══════════════════════════════════════════════════════════════
# MA_CROSS 信号查询
# ══════════════════════════════════════════════════════════════

def build_ma_cross_query(start, end, *, strict=False, holding_days=HOLDING_DAYS_DEFAULT, include_exit=True):
    """
    MA_CROSS信号查询(金叉+放量)
    
    参数:
        start/end: 区间起止
        strict: True=严格模式(vol_ratio=2.0, change_pct>0)
        holding_days: 持仓天数,影响exit_date
        include_exit: True=回测用(要entry+exit bar),False=选股用(只要entry,不要未来)
    
    返回:
        SQL 字符串
    """
    ma_rows = MA_SLOW - 1
    vol_ratio_ma = 2.0 if strict else 1.5
    
    if include_exit:
        exit_join = f"LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {holding_days + 1}"
        exit_filter = "AND x.trade_date IS NOT NULL"
        exit_select = "x.trade_date as exit_date, x.close_price as exit_price, x.low_price as exit_low,"
    else:
        exit_join = ""
        exit_filter = ""
        exit_select = "CAST(NULL AS DATE) as exit_date, CAST(NULL AS DOUBLE) as exit_price, CAST(NULL AS DOUBLE) as exit_low,"
    
    return f"""
WITH base AS (
    SELECT security_id, trade_date, close_price, open_price, low_price, volume, amount, change_pct,
        ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
    FROM daily_bar
    WHERE trade_date >= '{start}' AND trade_date <= '{end}'
),
price_ma AS (
    SELECT b.security_id, b.trade_date, b.day_idx, b.close_price, b.volume, b.amount, b.change_pct,
        AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as ma5,
        AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN {ma_rows} PRECEDING AND CURRENT ROW) as ma_slow,
        AVG(b.volume) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as vol_ma5
    FROM base b WHERE b.day_idx > {MA_SLOW + 1}
),
signals AS (
    SELECT m.security_id, m.trade_date, m.day_idx,
        m.ma5 / m.ma_slow - 1 as ma_gap
    FROM price_ma m
    WHERE m.ma5 > m.ma_slow
      AND (SELECT m2.ma5 FROM price_ma m2 WHERE m2.security_id = m.security_id AND m2.day_idx < m.day_idx ORDER BY m2.day_idx DESC LIMIT 1) <=
          (SELECT m2.ma_slow FROM price_ma m2 WHERE m2.security_id = m.security_id AND m2.day_idx < m.day_idx ORDER BY m2.day_idx DESC LIMIT 1)
      AND m.volume > m.vol_ma5 * {vol_ratio_ma}
      AND m.amount >= 100000000
      AND ABS(m.change_pct) < 9.8
      {'AND m.change_pct > 0' if strict else ''}
),
all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, low_price, change_pct FROM base)
SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
    e.trade_date as entry_date, e.open_price as entry_price,
    {exit_select}
    e.change_pct as entry_change,
    s.ma_gap as raw_score
FROM signals s
LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
{exit_join}
WHERE e.trade_date IS NOT NULL AND e.open_price > 0 {exit_filter}
"""


# ══════════════════════════════════════════════════════════════
# MACD 信号查询
# ══════════════════════════════════════════════════════════════

def build_macd_query(start, end, *, holding_days=HOLDING_DAYS_DEFAULT, include_exit=True):
    """
    MACD信号查询(MACD金叉)
    """
    if include_exit:
        exit_join = f"LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {holding_days + 1}"
        exit_filter = "AND x.trade_date IS NOT NULL"
        exit_select = "x.trade_date as exit_date, x.close_price as exit_price, x.low_price as exit_low,"
    else:
        exit_join = ""
        exit_filter = ""
        exit_select = "CAST(NULL AS DATE) as exit_date, CAST(NULL AS DOUBLE) as exit_price, CAST(NULL AS DOUBLE) as exit_low,"
    
    return f"""
WITH base AS (
    SELECT security_id, trade_date, close_price, open_price, low_price, volume, amount, change_pct,
        ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
    FROM daily_bar
    WHERE trade_date >= '{start}' AND trade_date <= '{end}'
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
        ABS(m.macd_diff_prev) as raw_score
    FROM macline m
    WHERE m.macd_diff_prev IS NOT NULL AND m.macd_diff_prev <= 0
      AND m.volume > m.vol_ma5 * 1.3 AND m.amount >= 200000000
      AND ABS(m.change_pct) < 9.8
),
all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, low_price, change_pct FROM base)
SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
    e.trade_date as entry_date, e.open_price as entry_price,
    {exit_select}
    e.change_pct as entry_change,
    s.raw_score as raw_score
FROM signals s
LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
{exit_join}
WHERE e.trade_date IS NOT NULL AND e.open_price > 0 {exit_filter}
"""


# ══════════════════════════════════════════════════════════════
# VOL_SURGE 信号查询
# ══════════════════════════════════════════════════════════════

def build_vol_surge_query(start, end, *, holding_days=HOLDING_DAYS_DEFAULT, include_exit=True):
    """
    VOL_SURGE信号查询(放量突破)
    """
    if include_exit:
        exit_join = f"LEFT JOIN all_bars x ON s.security_id = x.security_id AND x.day_idx = s.day_idx + {holding_days + 1}"
        exit_filter = "AND x.trade_date IS NOT NULL"
        exit_select = "x.trade_date as exit_date, x.close_price as exit_price, x.low_price as exit_low,"
    else:
        exit_join = ""
        exit_filter = ""
        exit_select = "CAST(NULL AS DATE) as exit_date, CAST(NULL AS DOUBLE) as exit_price, CAST(NULL AS DOUBLE) as exit_low,"
    
    return f"""
WITH base AS (
    SELECT security_id, trade_date, close_price, open_price, low_price, volume, amount, change_pct,
        ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
    FROM daily_bar
    WHERE trade_date >= '{start}' AND trade_date <= '{end}'
),
price_vol AS (
    SELECT b.security_id, b.trade_date, b.day_idx, b.close_price, b.open_price, b.volume, b.amount, b.change_pct,
        AVG(b.volume) OVER (PARTITION BY b.security_id ORDER BY b.trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as vol_ma20,
        (b.close_price - b.open_price) / b.open_price * 100 as pct_change
    FROM base b WHERE b.day_idx > 20
),
signals AS (
    SELECT v.security_id, v.trade_date, v.day_idx,
        v.pct_change as raw_score
    FROM price_vol v
    WHERE v.volume >= v.vol_ma20 * 1.5
      AND v.amount >= 100000000
      AND v.pct_change BETWEEN 3.0 AND 8.0
      AND (SELECT MIN(v2.close_price) / v.close_price - 1
           FROM price_vol v2
           WHERE v2.security_id = v.security_id AND v2.day_idx < v.day_idx AND v2.day_idx >= v.day_idx - 5) <= -0.04
      AND ABS(v.change_pct) < 9.8
),
all_bars AS (SELECT security_id, day_idx, trade_date, open_price, close_price, low_price, change_pct FROM base)
SELECT s.security_id, s.trade_date as signal_date, s.day_idx as sig_idx,
    e.trade_date as entry_date, e.open_price as entry_price,
    {exit_select}
    e.change_pct as entry_change,
    s.raw_score as raw_score
FROM signals s
LEFT JOIN all_bars e ON s.security_id = e.security_id AND e.day_idx = s.day_idx + 1
{exit_join}
WHERE e.trade_date IS NOT NULL AND e.open_price > 0 {exit_filter}
"""


# ══════════════════════════════════════════════════════════════
# 市场环境检测 (沪深300 MA20 方向)
# ══════════════════════════════════════════════════════════════

def get_market_regime(conn, start, end):
    """
    返回每天的市场方向: 'bull'/'bear',基于沪深300 MA20 方向
    
    规则: 当天 ma20 >= 昨天 ma20 → bull,否则 bear
    """
    q = """
    WITH idx AS (
        SELECT trade_date, close_price,
            AVG(close_price) OVER (ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as ma20
        FROM hs300_bar
        WHERE trade_date >= ? AND trade_date <= ?
    ),
    regimes AS (
        SELECT trade_date, close_price, ma20,
            LAG(ma20) OVER (ORDER BY trade_date) as prev_ma20
        FROM idx
    )
    SELECT trade_date,
        CASE WHEN ma20 >= prev_ma20 THEN 'bull' ELSE 'bear' END as regime
    FROM regimes
    WHERE prev_ma20 IS NOT NULL
    ORDER BY trade_date
    """
    df = conn.execute(q, [start, end]).fetchdf()
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    return dict(zip(df['trade_date'], df['regime']))


# ══════════════════════════════════════════════════════════════
# 信号百分位计算（统一接口 - 选股和回测必须用同一种算法）
# ══════════════════════════════════════════════════════════════

def compute_score_pct_rank(df, method='in_range'):
    """
    给 DataFrame 加 'score' 列（每策略内的百分位排名）
    
    参数:
        df: 必须包含 'strategy' 和 'raw_score' 列
        method: 
            - 'in_range' (默认): 在当前 df 各策略内算百分位（回测用）
            - 'cache': 用全量历史 cache 算百分位（已弃用,会导致与回测不一致）
    
    返回: 加了 'score' 列的 df
    """
    df = df.copy()
    
    if method == 'in_range':
        # 与回测完全一致: 在当前 df 各策略内 rank
        for strat in ['MA_CROSS', 'MACD', 'VOL_SURGE']:
            mask = df['strategy'] == strat
            scores = df.loc[mask, 'raw_score']
            if len(scores) > 0:
                df.loc[mask, 'score'] = scores.rank(pct=True, ascending=True)
    else:
        # 兼容旧 daily_picker 的 cache 方式（不推荐,与回测不一致）
        from pathlib import Path
        cache_path = Path("D:/Files/workspace-ai/quant/data/daily_picks/_all_signals_cache.csv")
        if cache_path.exists():
            hist = pd.read_csv(cache_path, encoding='utf-8-sig')
            for strat in ['MA_CROSS', 'MACD', 'VOL_SURGE']:
                hist_mask = hist['strategy'] == strat
                df_mask = df['strategy'] == strat
                if hist_mask.sum() > 0 and df_mask.sum() > 0:
                    hist_scores = hist.loc[hist_mask, 'raw_score'].values
                    df.loc[df_mask, 'score'] = df.loc[df_mask, 'raw_score'].apply(
                        lambda x: (hist_scores < x).sum() / len(hist_scores)
                    )
    return df


# ══════════════════════════════════════════════════════════════
# 一站式查询接口 (给 daily_picker 用)
# ══════════════════════════════════════════════════════════════

def query_signals_for_date(conn, db_path, signal_date, profile, holding_days=BULL_HOLDING_DAYS):
    """
    查询指定 signal_date 的所有信号(MA_CROSS / MACD / VOL_SURGE)
    
    参数:
        conn: duckdb 连接
        db_path: 数据库路径(暂时不用,保留兼容)
        signal_date: 信号日期,如 '2026-05-12'
        profile: dict 包含 ma_cross_strict 等参数
        holding_days: 持仓天数(影响回测 SQL 的 exit 计算,选股场景可忽略)
    
    返回:
        dict: {'MA_CROSS': df, 'MACD': df, 'VOL_SURGE': df}
    
    【重要】选股只关心\"哪天有信号\",不管 entry/exit 能不能找到 bar
             (那是回测的事,实盘不预测未来)
    """
    # 查询范围: 2019-01-01 ~ signal_date+1(给 entry bar 留空间)
    # 选股只关心\"哪天有信号 + 次日 entry bar 存在\",不查未来 5 天的 exit
    start = '2019-01-01'
    from datetime import datetime, timedelta
    end_dt = datetime.strptime(signal_date, '%Y-%m-%d') + timedelta(days=1)
    end = end_dt.strftime('%Y-%m-%d')
    
    results = {}
    
    # MA_CROSS
    try:
        q = build_ma_cross_query(start, end, 
                                  strict=profile.get('ma_cross_strict', False),
                                  holding_days=holding_days,
                                  include_exit=False)  # 选股不查未来
        df = conn.execute(q).df()
        # 筛选当天的信号
        df['signal_date'] = pd.to_datetime(df['signal_date'])
        df = df[df['signal_date'] == pd.Timestamp(signal_date)].copy()
        results['MA_CROSS'] = df
    except Exception as e:
        print(f"  [警告] MA_CROSS 查询失败: {e}")
        results['MA_CROSS'] = pd.DataFrame()
    
    # MACD
    try:
        q = build_macd_query(start, end, holding_days=holding_days, include_exit=False)
        df = conn.execute(q).df()
        df['signal_date'] = pd.to_datetime(df['signal_date'])
        df = df[df['signal_date'] == pd.Timestamp(signal_date)].copy()
        results['MACD'] = df
    except Exception as e:
        print(f"  [警告] MACD 查询失败: {e}")
        results['MACD'] = pd.DataFrame()
    
    # VOL_SURGE
    try:
        q = build_vol_surge_query(start, end, holding_days=holding_days, include_exit=False)
        df = conn.execute(q).df()
        df['signal_date'] = pd.to_datetime(df['signal_date'])
        df = df[df['signal_date'] == pd.Timestamp(signal_date)].copy()
        results['VOL_SURGE'] = df
    except Exception as e:
        print(f"  [警告] VOL_SURGE 查询失败: {e}")
        results['VOL_SURGE'] = pd.DataFrame()
    
    return results


# ══════════════════════════════════════════════════════════════
# 全量信号查询 (给回测用,一次查完整个区间)
# ══════════════════════════════════════════════════════════════

def query_signals_range(conn, start, end, profile, holding_days=HOLDING_DAYS_DEFAULT):
    """
    查询整个 [start, end] 区间的所有信号
    
    返回:
        pd.DataFrame 包含所有策略的信号
    """
    dfs = []
    
    # MA_CROSS
    q = build_ma_cross_query(start, end, 
                              strict=profile.get('ma_cross_strict', False),
                              holding_days=holding_days)
    df = conn.execute(q).df()
    df['strategy'] = 'MA_CROSS'
    dfs.append(df)
    
    # MACD
    q = build_macd_query(start, end, holding_days=holding_days)
    df = conn.execute(q).df()
    df['strategy'] = 'MACD'
    dfs.append(df)
    
    # VOL_SURGE
    q = build_vol_surge_query(start, end, holding_days=holding_days)
    df = conn.execute(q).df()
    df['strategy'] = 'VOL_SURGE'
    dfs.append(df)
    
    all_df = pd.concat(dfs, ignore_index=True)
    return all_df
