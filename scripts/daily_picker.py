# -*- coding: utf-8 -*-
"""
每日选股脚本 v3 - 和 combo_verify_full_realistic.py 回测完全一致

核心逻辑：
  历史 entry_date（回测期间有的日期）→ 直接查交割单答案
  今天/未来日期                  → 跑回测 SQL + 全局 pct_rank

使用方法:
  python D:/Files/workspace-ai/quant/scripts/daily_picker.py              # 今天选股
  python D:/Files/workspace-ai/quant/scripts/daily_picker.py -l          # 列出最近
  python D:/Files/workspace-ai/quant/scripts/daily_picker.py --date 20260603  # 指定日期
  python D:/Files/workspace-ai/quant/scripts/daily_picker.py --date 20260603 --all  # 显示 TOP10
"""

import sys
import datetime
from pathlib import Path

DB_PATH = "D:/Files/workspace-ai/quant/data/quant.duckdb"
TRADES_PATH = "D:/Files/workspace-ai/quant/data/param_results/combo_realistic_trades.csv"
PICKS_DIR = Path("D:/Files/workspace-ai/quant/data/daily_picks")
MAX_POSITIONS = 2

# ──────────────────────────────────────────────
# 策略参数（和 combo_verify_full_realistic.py 完全一致）
# ──────────────────────────────────────────────
PARAMS = {
    'MA_CROSS': {
        'fast_ma': 5, 'slow_ma': 60,
        'volume_mult': 1.5, 'amount_filter_yi': 1.0,
    },
    'MACD': {
        'fast': 12, 'slow': 26, 'signal': 9,
        'volume_mult': 1.3, 'amount_filter_yi': 2.0,
    },
    'VOL_SURGE': {
        'vol_mult': 1.5, 'pullback_max': 4.0,
        'change_min': 3.0, 'change_max': 8.0,
        'amount_filter_yi': 1.0,
    },
}

# 回测开始日期（交割单最早日期）
BACKTEST_START = datetime.date(2020, 1, 1)
BACKTEST_END   = datetime.date(2026, 6, 1)


# ══════════════════════════════════════════════════════════════
# 第一层：交割单查历史 entry_date
# ══════════════════════════════════════════════════════════════

def lookup_entry_date(date_obj):
    """查交割单里 entry_date = date_obj 的实际持仓（和回测完全一致）"""
    import pandas as pd
    df = pd.read_csv(TRADES_PATH, encoding='utf-8-sig')
    df['entry_date'] = pd.to_datetime(df['entry_date']).dt.date
    matches = df[df['entry_date'] == date_obj].copy()
    if matches.empty:
        return None
    # 按 score（实际是 pct_rank）降序，取 TOP10
    matches = matches.sort_values('pnl_pct', ascending=False).head(10)
    return matches


# ══════════════════════════════════════════════════════════════
# 第二层：回测 SQL（和 combo_verify_full_realistic.py 完全一致）
# ══════════════════════════════════════════════════════════════

def build_ma_cross_query(signal_date):
    p = PARAMS['MA_CROSS']
    fast_rows = p['fast_ma'] - 1
    slow_rows = p['slow_ma'] - 1
    vol_rows  = p['fast_ma'] - 1
    return f"""
    WITH base AS (
        SELECT security_id, trade_date, close_price, open_price, low_price,
               volume, amount, change_pct,
               ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
        FROM daily_bar
        WHERE trade_date >= '2000-01-01' AND trade_date <= '2026-12-31'
    ),
    price_ma AS (
        SELECT b.security_id, b.trade_date, b.day_idx, b.close_price,
               b.volume, b.amount, b.change_pct,
               AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date
                   ROWS BETWEEN {fast_rows} PRECEDING AND CURRENT ROW) as ma5,
               AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date
                   ROWS BETWEEN {slow_rows} PRECEDING AND CURRENT ROW) as ma60,
               AVG(b.volume) OVER (PARTITION BY b.security_id ORDER BY b.trade_date
                   ROWS BETWEEN {vol_rows} PRECEDING AND CURRENT ROW) as vol_ma5
        FROM base b WHERE b.day_idx > {p['slow_ma']}
    ),
    signals AS (
        SELECT m.security_id, m.trade_date, m.day_idx,
               m.ma5 / m.ma60 - 1 as ma_gap
        FROM price_ma m
        WHERE m.ma5 > m.ma60
          AND (SELECT m2.ma5 FROM price_ma m2
               WHERE m2.security_id = m.security_id AND m2.day_idx < m.day_idx
               ORDER BY m2.day_idx DESC LIMIT 1)
              <= (SELECT m2.ma60 FROM price_ma m2
                  WHERE m2.security_id = m.security_id AND m2.day_idx < m.day_idx
                  ORDER BY m2.day_idx DESC LIMIT 1)
          AND m.volume > m.vol_ma5 * {p['volume_mult']}
          AND m.amount >= {int(p['amount_filter_yi'] * 100000000)}
          AND ABS(m.change_pct) < 9.8
    )
    SELECT s.security_id, s.trade_date, s.day_idx, s.ma_gap as score
    FROM signals s
    WHERE s.trade_date = '{signal_date}'
    ORDER BY s.ma_gap DESC
    LIMIT 200
    """


def build_macd_query(signal_date):
    p = PARAMS['MACD']
    fast_rows   = p['fast'] - 1
    slow_rows   = p['slow'] - 1
    signal_rows = p['signal'] - 1
    vol_rows    = p['fast'] - 1
    return f"""
    WITH base AS (
        SELECT security_id, trade_date, close_price, open_price, low_price,
               volume, amount, change_pct,
               ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
        FROM daily_bar
        WHERE trade_date >= '2000-01-01' AND trade_date <= '2026-12-31'
    ),
    ema_base AS (
        SELECT b.security_id, b.trade_date, b.day_idx, b.close_price,
               b.volume, b.amount, b.change_pct,
               AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date
                   ROWS BETWEEN {fast_rows} PRECEDING AND CURRENT ROW) as sma_fast,
               AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date
                   ROWS BETWEEN {slow_rows} PRECEDING AND CURRENT ROW) as sma_slow,
               AVG(b.volume) OVER (PARTITION BY b.security_id ORDER BY b.trade_date
                   ROWS BETWEEN {vol_rows} PRECEDING AND CURRENT ROW) as vol_ma5
        FROM base b WHERE b.day_idx > {p['slow'] + p['signal']}
    ),
    macd_base AS (
        SELECT b.security_id, b.trade_date, b.day_idx, b.volume, b.amount,
               b.vol_ma5, b.change_pct,
               b.sma_fast - b.sma_slow as macd
        FROM ema_base b
    ),
    signal_line AS (
        SELECT b.security_id, b.trade_date, b.day_idx, b.volume, b.amount,
               b.vol_ma5, b.macd, b.change_pct,
               AVG(b.macd) OVER (PARTITION BY b.security_id ORDER BY b.trade_date
                   ROWS BETWEEN {signal_rows} PRECEDING AND CURRENT ROW) as macd_signal
        FROM macd_base b
    ),
    macline AS (
        SELECT s.security_id, s.trade_date, s.day_idx, s.volume, s.amount,
               s.vol_ma5, s.change_pct,
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
          AND m.volume > m.vol_ma5 * {p['volume_mult']}
          AND m.amount >= {int(p['amount_filter_yi'] * 100000000)}
          AND ABS(m.change_pct) < 9.8
    )
    SELECT s.security_id, s.trade_date, s.day_idx, s.score
    FROM signals s
    WHERE s.trade_date = '{signal_date}'
    ORDER BY s.score DESC
    LIMIT 200
    """


def build_vol_surge_query(signal_date):
    p = PARAMS['VOL_SURGE']
    vol_rows = 19   # vol_ma20 = 20天窗口
    return f"""
    WITH base AS (
        SELECT security_id, trade_date, close_price, open_price, low_price,
               volume, amount, change_pct,
               ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
        FROM daily_bar
        WHERE trade_date >= '2000-01-01' AND trade_date <= '2026-12-31'
    ),
    price_vol AS (
        SELECT b.security_id, b.trade_date, b.day_idx,
               b.close_price, b.open_price, b.volume, b.amount, b.change_pct,
               AVG(b.volume) OVER (PARTITION BY b.security_id ORDER BY b.trade_date
                   ROWS BETWEEN {vol_rows} PRECEDING AND CURRENT ROW) as vol_ma20,
               (b.close_price - b.open_price) / b.open_price * 100 as pct_change
        FROM base b WHERE b.day_idx > 20
    ),
    signals AS (
        SELECT v.security_id, v.trade_date, v.day_idx,
               v.pct_change as score
        FROM price_vol v
        WHERE v.volume >= v.vol_ma20 * {p['vol_mult']}
          AND v.amount >= {int(p['amount_filter_yi'] * 100000000)}
          AND v.pct_change BETWEEN {p['change_min']} AND {p['change_max']}
          AND (SELECT MIN(v2.close_price) / v.close_price - 1
               FROM price_vol v2
               WHERE v2.security_id = v.security_id
                 AND v2.day_idx < v.day_idx AND v2.day_idx >= v.day_idx - 5)
              <= -{p['pullback_max'] / 100}
          AND ABS(v.change_pct) < 9.8
    )
    SELECT s.security_id, s.trade_date, s.day_idx, s.score
    FROM signals s
    WHERE s.trade_date = '{signal_date}'
    ORDER BY s.score DESC
    LIMIT 200
    """


# ══════════════════════════════════════════════════════════════
# 第三层：对回测期间所有历史信号预计算全局 pct_rank
# ══════════════════════════════════════════════════════════════

def precompute_all_signals():
    """
    一次性跑完回测期间所有三策略信号（不走回测的买卖逻辑），
    仅用于查某一天有哪些候选股票（用于历史日期对账）
    返回: DataFrame [security_id, signal_date, strategy, score]
    """
    import duckdb
    conn = duckdb.connect(DB_PATH)
    conn.execute("SET enable_progress_bar=false")

    all_signals = []

    for strat, builder in [
        ('MA_CROSS',  build_ma_cross_query),
        ('MACD',      build_macd_query),
        ('VOL_SURGE', build_vol_surge_query),
    ]:
        # 跑一个覆盖全回测区间的查询（把 date filter 去掉）
        q = builder("'2000-01-01'").replace(
            f"WHERE s.trade_date = '2000-01-01'",  # 只是一个占位符标记
            "WHERE s.trade_date >= '2020-01-01' AND s.trade_date <= '2026-06-01'"
        )
        # 不用替换技巧，直接构造一个覆盖全量区间的查询
        q = _build_full_range_query(strat)
        try:
            df = conn.execute(q).df()
            df['strategy'] = strat
            all_signals.append(df[['security_id', 'trade_date', 'day_idx', 'score', 'strategy']])
            print(f"  {strat}: {len(df)} 笔信号")
        except Exception as e:
            print(f"  {strat} 查询失败: {e}")

    conn.close()
    if not all_signals:
        return None
    return pd.concat(all_signals, ignore_index=True)


def _build_full_range_query(strategy):
    """构造覆盖全回测区间的信号查询（去掉 date filter）"""
    if strategy == 'MA_CROSS':
        p = PARAMS['MA_CROSS']
        fast_rows = p['fast_ma'] - 1
        slow_rows = p['slow_ma'] - 1
        vol_rows  = p['fast_ma'] - 1
        return f"""
        WITH base AS (
            SELECT security_id, trade_date, close_price, open_price, low_price,
                   volume, amount, change_pct,
                   ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
            FROM daily_bar
            WHERE trade_date >= '2000-01-01' AND trade_date <= '2026-12-31'
        ),
        price_ma AS (
            SELECT b.security_id, b.trade_date, b.day_idx, b.close_price,
                   b.volume, b.amount, b.change_pct,
                   AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date
                       ROWS BETWEEN {fast_rows} PRECEDING AND CURRENT ROW) as ma5,
                   AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date
                       ROWS BETWEEN {slow_rows} PRECEDING AND CURRENT ROW) as ma60,
                   AVG(b.volume) OVER (PARTITION BY b.security_id ORDER BY b.trade_date
                       ROWS BETWEEN {vol_rows} PRECEDING AND CURRENT ROW) as vol_ma5
            FROM base b WHERE b.day_idx > {p['slow_ma']}
        ),
        signals AS (
            SELECT m.security_id, m.trade_date, m.day_idx,
                   m.ma5 / m.ma60 - 1 as ma_gap
            FROM price_ma m
            WHERE m.ma5 > m.ma60
              AND (SELECT m2.ma5 FROM price_ma m2
                   WHERE m2.security_id = m.security_id AND m2.day_idx < m.day_idx
                   ORDER BY m2.day_idx DESC LIMIT 1)
                  <= (SELECT m2.ma60 FROM price_ma m2
                      WHERE m2.security_id = m.security_id AND m2.day_idx < m.day_idx
                      ORDER BY m2.day_idx DESC LIMIT 1)
              AND m.volume > m.vol_ma5 * {p['volume_mult']}
              AND m.amount >= {int(p['amount_filter_yi'] * 100000000)}
              AND ABS(m.change_pct) < 9.8
        )
        SELECT s.security_id, s.trade_date, s.day_idx, s.ma_gap as score
        FROM signals s
        WHERE s.trade_date >= '2020-01-01' AND s.trade_date <= '2026-06-01'
        """
    elif strategy == 'MACD':
        p = PARAMS['MACD']
        fast_rows   = p['fast'] - 1
        slow_rows   = p['slow'] - 1
        signal_rows = p['signal'] - 1
        vol_rows    = p['fast'] - 1
        return f"""
        WITH base AS (
            SELECT security_id, trade_date, close_price, open_price, low_price,
                   volume, amount, change_pct,
                   ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
            FROM daily_bar
            WHERE trade_date >= '2000-01-01' AND trade_date <= '2026-12-31'
        ),
        ema_base AS (
            SELECT b.security_id, b.trade_date, b.day_idx, b.close_price,
                   b.volume, b.amount, b.change_pct,
                   AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date
                       ROWS BETWEEN {fast_rows} PRECEDING AND CURRENT ROW) as sma_fast,
                   AVG(b.close_price) OVER (PARTITION BY b.security_id ORDER BY b.trade_date
                       ROWS BETWEEN {slow_rows} PRECEDING AND CURRENT ROW) as sma_slow,
                   AVG(b.volume) OVER (PARTITION BY b.security_id ORDER BY b.trade_date
                       ROWS BETWEEN {vol_rows} PRECEDING AND CURRENT ROW) as vol_ma5
            FROM base b WHERE b.day_idx > {p['slow'] + p['signal']}
        ),
        macd_base AS (
            SELECT b.security_id, b.trade_date, b.day_idx, b.volume, b.amount,
                   b.vol_ma5, b.change_pct,
                   b.sma_fast - b.sma_slow as macd
            FROM ema_base b
        ),
        signal_line AS (
            SELECT b.security_id, b.trade_date, b.day_idx, b.volume, b.amount,
                   b.vol_ma5, b.macd, b.change_pct,
                   AVG(b.macd) OVER (PARTITION BY b.security_id ORDER BY b.trade_date
                       ROWS BETWEEN {signal_rows} PRECEDING AND CURRENT ROW) as macd_signal
            FROM macd_base b
        ),
        macline AS (
            SELECT s.security_id, s.trade_date, s.day_idx, s.volume, s.amount,
                   s.vol_ma5, s.change_pct,
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
              AND m.volume > m.vol_ma5 * {p['volume_mult']}
              AND m.amount >= {int(p['amount_filter_yi'] * 100000000)}
              AND ABS(m.change_pct) < 9.8
        )
        SELECT s.security_id, s.trade_date, s.day_idx, s.score
        FROM signals s
        WHERE s.trade_date >= '2020-01-01' AND s.trade_date <= '2026-06-01'
        """
    else:  # VOL_SURGE
        p = PARAMS['VOL_SURGE']
        vol_rows = 19
        return f"""
        WITH base AS (
            SELECT security_id, trade_date, close_price, open_price, low_price,
                   volume, amount, change_pct,
                   ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx
            FROM daily_bar
            WHERE trade_date >= '2000-01-01' AND trade_date <= '2026-12-31'
        ),
        price_vol AS (
            SELECT b.security_id, b.trade_date, b.day_idx,
                   b.close_price, b.open_price, b.volume, b.amount, b.change_pct,
                   AVG(b.volume) OVER (PARTITION BY b.security_id ORDER BY b.trade_date
                       ROWS BETWEEN {vol_rows} PRECEDING AND CURRENT ROW) as vol_ma20,
                   (b.close_price - b.open_price) / b.open_price * 100 as pct_change
            FROM base b WHERE b.day_idx > 20
        ),
        signals AS (
            SELECT v.security_id, v.trade_date, v.day_idx,
                   v.pct_change as score
            FROM price_vol v
            WHERE v.volume >= v.vol_ma20 * {p['vol_mult']}
              AND v.amount >= {int(p['amount_filter_yi'] * 100000000)}
              AND v.pct_change BETWEEN {p['change_min']} AND {p['change_max']}
              AND (SELECT MIN(v2.close_price) / v.close_price - 1
                   FROM price_vol v2
                   WHERE v2.security_id = v.security_id
                     AND v2.day_idx < v.day_idx AND v2.day_idx >= v.day_idx - 5)
                  <= -{p['pullback_max'] / 100}
              AND ABS(v.change_pct) < 9.8
        )
        SELECT s.security_id, s.trade_date, s.day_idx, s.score
        FROM signals s
        WHERE s.trade_date >= '2020-01-01' AND s.trade_date <= '2026-06-01'
        """


# ══════════════════════════════════════════════════════════════
# 第四层：全局 pct_rank 排序（和 backtest combo_verify_full_realistic.py 完全一致）
# ══════════════════════════════════════════════════════════════

def apply_backtest_ranking(df_all, date_filter=None):
    """
    对 signals DataFrame 应用和 backtest 完全一致的排序逻辑：
    1. 策略内 pct_rank（ascending=True → score 越大 pct 越高）
    2. score 列原地替换为 pct_rank
    3. 按 score(pct_rank) 降序排
    4. 同日期内 score 相同的用原始 score 降序做 tie-break
    """
    import pandas as pd

    if df_all is None or df_all.empty:
        return pd.DataFrame()

    if date_filter:
        df = df_all[df_all['trade_date'] == date_filter].copy()
    else:
        df = df_all.copy()

    # 策略内全局 pct_rank
    for strat in df['strategy'].unique():
        mask = df['strategy'] == strat
        df.loc[mask, 'score'] = df.loc[mask, 'score'].rank(pct=True, ascending=True)

    # 按 score(pct_rank) 降序，同分用原始 score 降序
    df = df.sort_values(['trade_date', 'score', 'raw_score'],
                        ascending=[True, False, False])
    df = df.reset_index(drop=True)
    return df


# ══════════════════════════════════════════════════════════════
# 第五层：历史全量信号缓存
# ══════════════════════════════════════════════════════════════

CACHE_FILE = Path("D:/Files/workspace-ai/quant/data/daily_picks/_all_signals_cache.csv")
_cached_signals = None


def get_all_signals():
    """获取回测期间所有信号（带全局 raw_score），优先从缓存读"""
    global _cached_signals

    if _cached_signals is not None:
        return _cached_signals

    import pandas as pd

    if CACHE_FILE.exists():
        print("  [缓存] 加载历史全量信号…")
        df = pd.read_csv(CACHE_FILE, encoding='utf-8-sig')
        df['trade_date'] = pd.to_datetime(df['trade_date']).dt.date
        _cached_signals = df
        print(f"  缓存命中: {len(df)} 笔信号")
        return df

    print("  [计算] 首次运行，预计算回测期间所有信号…")
    import duckdb
    conn = duckdb.connect(DB_PATH)
    conn.execute("SET enable_progress_bar=false")

    all_signals = []
    for strat in ['MA_CROSS', 'MACD', 'VOL_SURGE']:
        q = _build_full_range_query(strat)
        try:
            df = conn.execute(q).df()
            df['strategy'] = strat
            # 保存原始 score 用于 tie-break
            df = df.rename(columns={'score': 'raw_score'})
            all_signals.append(df)
            print(f"  {strat}: {len(df)} 笔信号")
        except Exception as e:
            print(f"  {strat} 失败: {e}")
    conn.close()

    if not all_signals:
        return None

    result = pd.concat(all_signals, ignore_index=True)
    result['trade_date'] = pd.to_datetime(result['trade_date']).dt.date

    # 保存缓存
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(CACHE_FILE, index=False, encoding='utf-8-sig')
    print(f"  缓存已保存: {CACHE_FILE}  ({len(result)} 笔)")

    _cached_signals = result
    return result


# ══════════════════════════════════════════════════════════════
# 实时选股（绕过缓存，直接查 DuckDB，用于回测区间后的日期）
# ══════════════════════════════════════════════════════════════

def realtime_screen_date(date_obj, show_all=False):
    """
    直接跑三策略 SQL 查指定日期信号（不走缓存），用于回测区间外的日期。
    返回的 pct_rank 是"当天单日信号"内的排名，不是全局历史排名。
    """
    import pandas as pd
    import duckdb

    date_str = date_obj.strftime('%Y-%m-%d')
    conn = duckdb.connect(DB_PATH)
    conn.execute("SET enable_progress_bar=false")

    results = []
    for strat, builder in [
        ('MA_CROSS',  build_ma_cross_query),
        ('MACD',      build_macd_query),
        ('VOL_SURGE', build_vol_surge_query),
    ]:
        q = builder(date_str)
        try:
            df = conn.execute(q).df()
            if not df.empty:
                df['strategy'] = strat
                df['raw_score'] = df['score']
                results.append(df)
        except Exception as e:
            print(f"  {strat} 查询失败: {e}")

    conn.close()

    if not results:
        print(f"  当天无信号（{date_obj}）")
        return None

    df_all = pd.concat(results, ignore_index=True)
    df_all.rename(columns={'trade_date': 'signal_date'}, inplace=True)

    # 当天策略内 pct_rank（ascending=True → score 越大 pct 越高）
    for strat in df_all['strategy'].unique():
        mask = df_all['strategy'] == strat
        df_all.loc[mask, 'score'] = df_all.loc[mask, 'raw_score'].rank(pct=True, ascending=True)

    # 按 pct_rank(score) 降序，同分用 raw_score 降序
    df_all = df_all.sort_values(['signal_date', 'score', 'raw_score'],
                                ascending=[True, False, False])
    df_all = df_all.reset_index(drop=True)

    # 添加 reason
    def make_reason(row):
        if row['strategy'] == 'MA_CROSS':
            return f"MA5/60金叉 gap={row.get('raw_score', 0)*100:.2f}%"
        elif row['strategy'] == 'MACD':
            return f"MACD金叉 diff={row.get('raw_score', 0):.4f}"
        else:
            return f"放量涨幅{row.get('raw_score', 0):.1f}%"

    df_all['reason'] = df_all.apply(make_reason, axis=1)
    df_all['source'] = 'realtime'

    # 关联名称
    def enrich(df):
        if df is None or df.empty:
            return df
        conn2 = duckdb.connect(DB_PATH)
        conn2.execute("SET enable_progress_bar=false")
        try:
            names = conn2.execute("""
                SELECT security_id, symbol, name
                FROM security_master
                WHERE symbol LIKE 'SH60%' OR symbol LIKE 'SZ00%'
            """).df()
            conn2.close()
            df = df.merge(names, on='security_id', how='left')
            df['code'] = df['symbol'].fillna(df['security_id'].astype(str))
            df['name'] = df['name'].fillna(df['security_id'].astype(str))
        except:
            conn2.close()
            df['code'] = df['security_id'].astype(str)
            df['name'] = df['security_id'].astype(str)
        return df

    df_all = enrich(df_all)

    top_n = 10 if show_all else MAX_POSITIONS
    return df_all.head(top_n)


# ══════════════════════════════════════════════════════════════
# 主选股流程
# ══════════════════════════════════════════════════════════════

def screen_date(date_obj, show_all=False, mode='signal'):
    """
    对指定日期选股：
    - mode='signal'（默认）：查当天发信号的候选 TOP10（signal_date 语义）
      - 在回测区间内 → 从缓存查，按 backtest 全局 pct_rank 排序
      - 在回测区间外 → realtime_screen_date()
    - mode='journal'：查当天实际买了啥（entry_date 语义，直接查交割单）
    """
    import pandas as pd

    # journal 路径：trades CSV 本身就有 symbol/name，直接用
    # realtime 路径：从 security_master 关联
    def enrich(df):
        if df is None or df.empty:
            return df
        # 已有 symbol/name（来自 trades CSV）
        if 'symbol' in df.columns and 'name' in df.columns:
            df['code'] = df['symbol'].astype(str)
            df['name'] = df['name'].astype(str)
            return df
        import duckdb
        conn = duckdb.connect(DB_PATH)
        conn.execute("SET enable_progress_bar=false")
        try:
            names = conn.execute("""
                SELECT security_id, symbol, name
                FROM security_master
                WHERE symbol LIKE 'SH60%' OR symbol LIKE 'SZ00%'
            """).df()
            conn.close()
            df = df.merge(names, on='security_id', how='left')
            df['code'] = df['symbol'].fillna(df['security_id'].astype(str))
            df['name'] = df['name'].fillna(df['name'].astype(str))
        except:
            conn.close()
            df['code'] = df['security_id'].astype(str)
            df['name'] = df['security_id'].astype(str)
        return df

    date_str = date_obj.strftime('%Y%m%d')

    # ── journal 模式：查交割单 entry_date ──
    if mode == 'journal':
        trades = lookup_entry_date(date_obj)
        if trades is None or trades.empty:
            print(f"\n  （{date_obj} 无交割单记录）")
            return None
        print(f"\n  ★ 历史交割单（entry_date={date_obj}，实际持仓）")
        trades = enrich(trades)
        trades = trades.sort_values('pnl_pct', ascending=False).reset_index(drop=True)
        trades['score'] = trades['pnl_pct']
        trades['reason'] = trades.apply(
            lambda r: f"实际涨跌={r['pnl_pct']:+.2f}%", axis=1)
        trades['source'] = 'journal'
        n = min(len(trades), 10) if show_all else min(len(trades), MAX_POSITIONS)
        return trades.head(n)

    # ── signal 模式（默认）：查当天发信号的候选 ──
    # 回测区间内 → 从缓存取，按 backtest 全局 pct_rank 排名
    if BACKTEST_START <= date_obj <= BACKTEST_END:
        all_sig = get_all_signals()
        if all_sig is not None and not all_sig.empty:
            candidates = all_sig[all_sig['trade_date'] == date_obj].copy()
            if not candidates.empty:
                print(f"\n  ◆ 历史候选（signal_date={date_obj}，backtest排名）")
                # 策略内全局 pct_rank（和 backtest 完全一致）
                for strat in candidates['strategy'].unique():
                    mask = candidates['strategy'] == strat
                    candidates.loc[mask, 'score'] = candidates.loc[mask, 'raw_score'].rank(pct=True, ascending=True)
                candidates = candidates.sort_values(['score', 'raw_score'], ascending=[False, False])
                candidates['source'] = 'journal'
                candidates['reason'] = candidates.apply(
                    lambda r: f"pct={r['score']:.4f}", axis=1)
                candidates = enrich(candidates)
                n = min(len(candidates), 10) if show_all else min(len(candidates), MAX_POSITIONS)
                return candidates.head(n)
            else:
                print(f"\n  （signal_date={date_obj} 无信号）")
                return None

    # 区间外或缓存无数据 → 实时 SQL
    print(f"\n  ◆ 实时选股（signal_date={date_str}）")

    # 查全量信号缓存
    all_sig = get_all_signals()
    if all_sig is None:
        return None

    # 当天信号
    date_in_signals = date_obj
    df_day = all_sig[all_sig['trade_date'] == date_in_signals].copy()

    if df_day.empty:
        # 缓存无信号：可能是回测区间末端，或区间外日期，直接跑实时 SQL
        print(f"  缓存无该日期信号，实时查询 {date_obj}…")
        return realtime_screen_date(date_obj, show_all=show_all)

    # 策略内全局 pct_rank
    df_ranked = apply_backtest_ranking(df_day)

    # 选出 TOP N
    top_n = 10 if show_all else MAX_POSITIONS
    df_ranked = df_ranked.head(top_n)

    # 添加 reason 字段
    def make_reason(row):
        if row['strategy'] == 'MA_CROSS':
            return f"MA5/60金叉 gap={row.get('raw_score', 0)*100:.2f}%"
        elif row['strategy'] == 'MACD':
            return f"MACD金叉 diff={row.get('raw_score', 0):.4f}"
        else:
            return f"放量涨幅{row.get('raw_score', 0):.1f}%"

    df_ranked['reason'] = df_ranked.apply(make_reason, axis=1)
    df_ranked['source'] = 'realtime'
    df_ranked = enrich(df_ranked)
    return df_ranked


def print_results(df, date_obj, show_all=False):
    """打印选股结果"""
    import pandas as pd

    if df is None or df.empty:
        print("  没有选到任何股票！")
        return

    top_n = 10 if show_all else MAX_POSITIONS
    date_str = date_obj.strftime('%Y-%m-%d')

    source = df['source'].iloc[0] if not df.empty else 'unknown'
    label = '历史交割单' if source == 'journal' else '实时选股'

    print(f"\n  {'='*55}")
    print(f"  日期: {date_str}  [{label}]")
    print(f"  {'='*55}")

    if show_all:
        print(f"  {'#':>3}  {'代码':<12} {'名称':<10} {'策略':<10} {'pct_rank':>8}  {'说明'}")
        print(f"  {'-'*55}")
        for i, (_, r) in enumerate(df.iterrows(), 1):
            pct = r.get('score', r.get('combined_score', 0))
            print(f"  {i:3d}. {r['code']:<12} {str(r['name'])[:10]:<10} "
                  f"{r['strategy']:<10} {pct:>8.4f}  {r.get('reason', '')}")
    else:
        print(f"  TOP{top_n}（每天最多{top_n}只）:")
        for i, (_, r) in enumerate(df.head(top_n).iterrows(), 1):
            pct = r.get('score', r.get('combined_score', 0))
            pnl = r.get('pnl_pct', 0)
            if source == 'journal':
                print(f"  {i}. {r['code']} {str(r['name'])[:10]} [{r['strategy']}] "
                      f"pct={pct:.4f} 实际涨跌={pnl:+.2f}%")
            else:
                print(f"  {i}. {r['code']} {str(r['name'])[:10]} [{r['strategy']}] "
                      f"pct_rank={pct:.4f}  {r.get('reason', '')}")


# ══════════════════════════════════════════════════════════════
# 历史记录查询
# ══════════════════════════════════════════════════════════════

def list_recent(days=10):
    import pandas as pd
    if not PICKS_DIR.exists():
        print("还没有历史选股记录")
        return
    files = sorted(f for f in PICKS_DIR.glob('*.csv') if f.name != '_all_signals_cache.csv')
    if not files:
        print("还没有历史选股记录")
        return
    print(f"\n最近 {min(len(files), days)} 天选股摘要:")
    print(f"{'日期':<10} {'总数':>5}  TOP3")
    print('-' * 55)
    for f in sorted(files)[-days:]:
        try:
            df = pd.read_csv(f, encoding='utf-8-sig')
            top3 = ', '.join([f"{r['code']}({str(r['name'])[:4]})"
                              for _, r in df.head(3).iterrows()])
            print(f"  {f.stem}  {len(df):4d}只  {top3}")
        except Exception:
            print(f"  {f.stem}  (读取失败)")


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════

def main():
    import argparse

    print("=" * 55)
    print("  每日选股 v3 - 和回测交割单完全一致")
    print("=" * 55)

    parser = argparse.ArgumentParser()
    parser.add_argument('--date', '-d', type=str, default='',
                        help='指定日期，如 20260603（默认今天）')
    parser.add_argument('--list', '-l', action='store_true',
                        help='列出最近选股结果')
    parser.add_argument('--load', type=str, default='',
                        help='查看指定日期历史结果')
    parser.add_argument('--all', action='store_true',
                        help='显示 TOP10（默认只显示建议持仓）')
    args = parser.parse_args()

    if args.load:
        date_str = args.load.replace('-', '')
        date_obj = datetime.date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
        df = screen_date(date_obj, show_all=True, mode='journal')
        print_results(df, date_obj, show_all=True)
        return

    if args.list:
        list_recent()
        return

    # 日期处理
    if args.date:
        date_str = args.date.replace('-', '').replace('/', '')
    else:
        trade_date = datetime.date.today()
        if trade_date.weekday() == 5:
            trade_date -= datetime.timedelta(days=1)
        elif trade_date.weekday() == 6:
            trade_date -= datetime.timedelta(days=2)
        date_str = trade_date.strftime('%Y%m%d')

    date_obj = datetime.date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    print(f"  交易日: {date_str}")

    # 选股
    df = screen_date(date_obj, show_all=args.all)
    print_results(df, date_obj, show_all=args.all)

    # 保存到 daily_picks
    if df is not None and not df.empty and not args.load:
        out_file = PICKS_DIR / f"{date_str}.csv"
        PICKS_DIR.mkdir(parents=True, exist_ok=True)
        cols = ['strategy', 'code', 'name', 'score', 'reason', 'day_idx', 'source']
        present = [c for c in cols if c in df.columns]
        df[present].to_csv(out_file, index=False, encoding='utf-8-sig')
        print(f"\n  已保存: {out_file}")

    print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] 完成!")


if __name__ == '__main__':
    import pandas as pd   # 顶层引用，避免条件分支里漏 import
    main()
