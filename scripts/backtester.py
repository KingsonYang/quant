"""
A股策略回测引擎
- 基于 DuckDB daily_bar 历史数据
- 实现7个核心策略的回测函数
- 统计各策略在 不同市场环境 × 不同持仓时间 下的表现
- 输出：胜率、均盈、均亏、盈亏比、期望值、最大回撤
"""

import sys
sys.path.insert(0, 'D:/SoftWare/tdx/PYPlugins/user')
import os
os.environ['TDXPATH'] = 'D:/SoftWare/tdx/TdxW.exe'

import duckdb
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, date
from typing import Optional
import json
import random

DB_PATH = "D:/Files/workspace-ai/quant/data/quant.duckdb"
OUTPUT_DIR = "D:/Files/workspace-ai/quant/data/backtest_results"

# ─── 数据库连接 ───────────────────────────────────────────────

def get_conn():
    return duckdb.connect(DB_PATH)


def get_market_regime_dict(conn, trade_date) -> dict:
    """获取指定日期的市场环境打分"""
    row = conn.execute("""
        SELECT total_score, regime_type, suggested_position
        FROM market_regime
        WHERE date <= ?
        ORDER BY date DESC
        LIMIT 1
    """, [trade_date]).fetchone()
    if row:
        return {"score": row[0], "type": row[1], "position": row[2]}
    # 默认中性
    return {"score": 0, "type": "unknown", "position": "20%"}


# ─── 获取历史K线数据 ─────────────────────────────────────────

def load_klines(conn, security_id: int, start_date: str, end_date: str) -> pd.DataFrame:
    """加载指定股票的历史K线"""
    df = conn.execute("""
        SELECT trade_date, open_price, high_price, low_price, close_price,
               volume, amount, change_pct, turnover_ratio,
               ma5_price, ma10_price, ma20_price,
               ma5_volume, ma10_volume, ma20_volume
        FROM daily_bar
        WHERE security_id = ?
          AND trade_date >= ?
          AND trade_date <= ?
        ORDER BY trade_date
    """, [security_id, start_date, end_date]).fetchdf()

    df.columns = [c.lower() for c in df.columns]
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    for col in ['open_price', 'high_price', 'low_price', 'close_price', 'amount',
                'ma5_price', 'ma10_price', 'ma20_price', 'change_pct', 'turnover_ratio']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    # 计算辅助指标（优先用预计算的，没有时本地计算）
    df['vol_ma5'] = df['volume'].rolling(5).mean()
    df['vol_ma10'] = df['volume'].rolling(10).mean()
    df['vol_ma5_shift1'] = df['vol_ma5'].shift(1)

    # 本地计算 MA（如果没有预计算值）
    if df['ma5_price'].isna().any():
        df['ma5_price_calc'] = df['close_price'].rolling(5).mean()
    else:
        df['ma5_price_calc'] = df['ma5_price']

    if df['ma10_price'].isna().any():
        df['ma10_price_calc'] = df['close_price'].rolling(10).mean()
    else:
        df['ma10_price_calc'] = df['ma10_price']

    if df['ma20_price'].isna().any():
        df['ma20_price_calc'] = df['close_price'].rolling(20).mean()
    else:
        df['ma20_price_calc'] = df['ma20_price']

    df['vol_ratio_prev5'] = df['volume'] / df['vol_ma5_shift1']  # 当日量/前5日均量

    # 统一使用_calc列（兼容有/无预计算的情况）
    df['ma5_price'] = df['ma5_price_calc']
    df['ma10_price'] = df['ma10_price_calc']
    df['ma20_price'] = df['ma20_price_calc']
    df['ma5_volume'] = df['vol_ma5']
    df['ma10_volume'] = df['vol_ma10']

    return df


def get_trading_dates(conn, start_date: str, end_date: str) -> list:
    """获取交易日列表"""
    rows = conn.execute("""
        SELECT trade_date
        FROM trading_calendar
        WHERE is_trading_day = 1
          AND trade_date >= ?
          AND trade_date <= ?
        ORDER BY trade_date
    """, [start_date, end_date]).fetchall()
    return [r[0] for r in rows]


# ─── 策略一：首板策略 ─────────────────────────────────────────

def find_first_limit_up(df: pd.DataFrame) -> list:
    """
    找出首板日（今日涨幅>9.9%，昨日涨幅<9.9%）
    返回：[(date, row_dict), ...]
    """
    signals = []
    for i in range(1, len(df)):
        today = df.iloc[i]
        yesterday = df.iloc[i - 1]
        if (today['change_pct'] > 9.9 and
                yesterday['change_pct'] <= 9.9 and
                pd.notna(today['close_price']) and
                pd.notna(yesterday['close_price'])):
            signals.append({
                'date': today['trade_date'],
                'open': float(today['open_price']),
                'close': float(today['close_price']),
                'high': float(today['high_price']),
                'low': float(today['low_price']),
                'volume': int(today['volume']),
                'amount': float(today['amount']),
                'turnover_ratio': float(today['turnover_ratio']) if pd.notna(today['turnover_ratio']) else 0,
                'change_pct': float(today['change_pct']),
                'vol_ratio': float(today['vol_ratio_prev5']) if pd.notna(today['vol_ratio_prev5']) else 0,
                'ma5': float(today['ma5_price']) if pd.notna(today['ma5_price']) else None,
                'ma10': float(today['ma10_price']) if pd.notna(today['ma10_price']) else None,
                'ma20': float(today['ma20_price']) if pd.notna(today['ma20_price']) else None,
            })
    return signals


def backtest_first_board(conn, security_id: int, start_date: str, end_date: str,
                          holding_days: int = 3) -> list:
    """
    首板策略回测
    买入：首板次日开盘买入
    卖出：持有N个交易日后卖出（用收盘价）
    过滤：成交额>1亿，换手率5-30%
    """
    df = load_klines(conn, security_id, start_date, end_date)
    if len(df) < 20:
        return []

    signals = find_first_limit_up(df)
    results = []

    for sig in signals:
        sig_date = sig['date']
        # 找次日开盘价（买入价）
        sig_idx = df[df['trade_date'] == sig_date].index[0]
        if sig_idx + 1 >= len(df):
            continue

        buy_row = df.iloc[sig_idx + 1]  # T+1开盘
        if pd.isna(buy_row['open_price']):
            continue

        entry_price = float(buy_row['open_price'])

        # 过滤条件
        amount_yi = sig['amount'] / 1e8  # 成交额亿元
        if amount_yi < 1.0:  # 成交额<1亿跳过
            continue
        if not (5.0 <= sig['turnover_ratio'] <= 30.0):  # 换手率过滤
            continue

        # 找卖出日（持有holding_days天后卖）
        sell_idx = sig_idx + holding_days
        if sell_idx >= len(df):
            continue

        sell_row = df.iloc[sell_idx]
        exit_price = float(sell_row['close_price'])

        pnl_pct = (exit_price - entry_price) / entry_price * 100
        results.append({
            'security_id': security_id,
            'signal_date': sig_date.date() if hasattr(sig_date, 'date') else sig_date,
            'entry_date': buy_row['trade_date'].date() if hasattr(buy_row['trade_date'], 'date') else buy_row['trade_date'],
            'exit_date': sell_row['trade_date'].date() if hasattr(sell_row['trade_date'], 'date') else sell_row['trade_date'],
            'entry_price': entry_price,
            'exit_price': exit_price,
            'pnl_pct': round(pnl_pct, 2),
            'holding_days': holding_days,
            'amount_yi': round(amount_yi, 2),
            'turnover_ratio': sig['turnover_ratio'],
            'vol_ratio': round(sig['vol_ratio'], 2),
            'change_pct_signal': sig['change_pct'],
        })

    return results


# ─── 策略二：N字反转 ─────────────────────────────────────────

def backtest_n_pattern(conn, security_id: int, start_date: str, end_date: str,
                        holding_days: int = 5) -> list:
    """
    N字反转策略
    条件：
      1. 近20日内有明显的"上涨→回调→再突破"结构
      2. 回调幅度 > 8%（深回调）
      3. 突破日涨幅 > 3%
      4. 突破时成交量 > 前5日均量1.5倍
    买入：突破日次日开盘
    卖出：持有holding_days天后收盘卖出
    """
    df = load_klines(conn, security_id, start_date, end_date)
    if len(df) < 25:
        return []

    results = []
    for i in range(20, len(df) - holding_days - 1):
        today = df.iloc[i]
        if pd.isna(today['ma10_price']) or pd.isna(today['ma20_price']):
            continue

        # 突破条件：今日涨幅>3%，且收盘价创20日新高
        close_today = float(today['close_price'])
        close_prev20_max = df.iloc[i-20:i]['close_price'].max()
        vol_ratio = float(today['vol_ratio_prev5']) if pd.notna(today['vol_ratio_prev5']) else 0
        change_pct = float(today['change_pct']) if pd.notna(today['change_pct']) else 0

        if not (change_pct > 3.0 and close_today > close_prev20_max and vol_ratio > 1.5):
            continue

        # 检查前面是否有深回调（15-20日前有>8%回调）
        max_decline = 0.0
        for j in range(i - 15, i):
            if j < 0:
                continue
            cp = float(df.iloc[j]['change_pct']) if pd.notna(df.iloc[j]['change_pct']) else 0
            if cp < max_decline:
                max_decline = cp
        if max_decline < -8.0:
            continue

        # 买入：明日开盘
        buy_row = df.iloc[i + 1]
        if pd.isna(buy_row['open_price']):
            continue
        entry_price = float(buy_row['open_price'])

        # 卖出
        sell_row = df.iloc[i + holding_days]
        if pd.isna(sell_row['close_price']):
            continue
        exit_price = float(sell_row['close_price'])

        pnl_pct = (exit_price - entry_price) / entry_price * 100
        amount_yi = float(today['amount']) / 1e8 if pd.notna(today['amount']) else 0

        results.append({
            'security_id': security_id,
            'signal_date': today['trade_date'].date(),
            'entry_date': buy_row['trade_date'].date(),
            'exit_date': sell_row['trade_date'].date(),
            'entry_price': entry_price,
            'exit_price': exit_price,
            'pnl_pct': round(pnl_pct, 2),
            'holding_days': holding_days,
            'amount_yi': round(amount_yi, 2),
            'vol_ratio': round(vol_ratio, 2),
            'change_pct': round(change_pct, 2),
            'max_decline': round(max_decline, 2),
        })

    return results


# ─── 策略三：520战法 ─────────────────────────────────────────

def backtest_520(conn, security_id: int, start_date: str, end_date: str,
                   holding_days: int = 5) -> list:
    """
    520战法
    条件：
      1. MA5 > MA10 > MA20（多头排列）
      2. MA5今日向上拐头（上穿昨日MA5）
      3. 成交量 > MA5量
    买入：当日收盘（或次日开盘）
    卖出：持有holding_days天
    """
    df = load_klines(conn, security_id, start_date, end_date)
    if len(df) < 25:
        return []

    results = []
    for i in range(20, len(df) - holding_days - 1):
        today = df.iloc[i]
        yesterday = df.iloc[i - 1]

        ma5_today = float(today['ma5_price']) if pd.notna(today['ma5_price']) else None
        ma5_yesterday = float(yesterday['ma5_price']) if pd.notna(yesterday['ma5_price']) else None
        ma10_today = float(today['ma10_price']) if pd.notna(today['ma10_price']) else None
        ma20_today = float(today['ma20_price']) if pd.notna(today['ma20_price']) else None
        ma5_vol = float(today['ma5_volume']) if pd.notna(today['ma5_volume']) else None
        vol = int(today['volume']) if pd.notna(today['volume']) else 0

        if not all([ma5_today, ma10_today, ma20_today, ma5_vol]):
            continue

        # 多头排列 + MA5向上拐头 + 放量
        cond1 = ma5_today > ma10_today > ma20_today
        cond2 = ma5_today > ma5_yesterday * 1.001
        cond3 = vol > ma5_vol
        if not (cond1 and cond2 and cond3):
            continue

        # 买入：次日开盘
        buy_row = df.iloc[i + 1]
        if pd.isna(buy_row['open_price']):
            continue
        entry_price = float(buy_row['open_price'])

        # 卖出
        sell_row = df.iloc[i + holding_days]
        if pd.isna(sell_row['close_price']):
            continue
        exit_price = float(sell_row['close_price'])

        pnl_pct = (exit_price - entry_price) / entry_price * 100
        amount_yi = float(today['amount']) / 1e8 if pd.notna(today['amount']) else 0
        change_pct = float(today['change_pct']) if pd.notna(today['change_pct']) else 0

        results.append({
            'security_id': security_id,
            'signal_date': today['trade_date'].date(),
            'entry_date': buy_row['trade_date'].date(),
            'exit_date': sell_row['trade_date'].date(),
            'entry_price': entry_price,
            'exit_price': exit_price,
            'pnl_pct': round(pnl_pct, 2),
            'holding_days': holding_days,
            'amount_yi': round(amount_yi, 2),
            'vol_ratio': round(vol / ma5_vol, 2) if ma5_vol > 0 else 0,
            'change_pct': round(change_pct, 2),
        })

    return results


# ─── 策略四：倍量回调确认 ─────────────────────────────────────

def backtest_volume_surge_pullback(conn, security_id: int, start_date: str, end_date: str,
                                     holding_days: int = 5) -> list:
    """
    倍量回调确认策略
    条件：
      1. D0成交量 > 前5日均量 × 2（倍量）
      2. D1-D2成交量萎缩（缩量回调）
      3. D3价格企稳（收盘站稳5日线）
    买入：D3收盘前买入
    卖出：持有holding_days天
    """
    df = load_klines(conn, security_id, start_date, end_date)
    if len(df) < 10:
        return []

    results = []
    for i in range(5, len(df) - holding_days - 1):
        today = df.iloc[i]
        prev1 = df.iloc[i - 1]
        prev2 = df.iloc[i - 2]
        prev5_vol_mean = float(df.iloc[i - 5:i]['volume'].mean())

        vol_today = int(today['volume']) if pd.notna(today['volume']) else 0
        vol_prev1 = int(prev1['volume']) if pd.notna(prev1['volume']) else 0
        vol_prev2 = int(prev2['volume']) if pd.notna(prev2['volume']) else 0
        close_today = float(today['close_price']) if pd.notna(today['close_price']) else None
        ma5_today = float(today['ma5_price']) if pd.notna(today['ma5_price']) else None

        if not all([close_today, ma5_today, prev5_vol_mean]):
            continue

        # D0倍量
        if vol_today < prev5_vol_mean * 2.0:
            continue
        # D1/D2缩量
        if not (vol_prev1 < vol_today * 0.8 and vol_prev2 < vol_today * 0.6):
            continue
        # D0涨幅适中（3-10%，不能太大也不能太小）
        change_today = float(today['change_pct']) if pd.notna(today['change_pct']) else 0
        if not (3.0 <= change_today <= 10.0):
            continue

        # D0站稳均线
        if close_today < ma5_today:
            continue

        # 买入：次日开盘
        buy_row = df.iloc[i + 1]
        if pd.isna(buy_row['open_price']):
            continue
        entry_price = float(buy_row['open_price'])

        # 卖出
        sell_row = df.iloc[i + holding_days]
        if pd.isna(sell_row['close_price']):
            continue
        exit_price = float(sell_row['close_price'])

        pnl_pct = (exit_price - entry_price) / entry_price * 100
        amount_yi = float(today['amount']) / 1e8 if pd.notna(today['amount']) else 0

        results.append({
            'security_id': security_id,
            'signal_date': today['trade_date'].date(),
            'entry_date': buy_row['trade_date'].date(),
            'exit_date': sell_row['trade_date'].date(),
            'entry_price': entry_price,
            'exit_price': exit_price,
            'pnl_pct': round(pnl_pct, 2),
            'holding_days': holding_days,
            'amount_yi': round(amount_yi, 2),
            'vol_ratio': round(vol_today / prev5_vol_mean, 2),
            'change_pct': round(change_today, 2),
        })

    return results


# ─── 策略五：老鸭头 ─────────────────────────────────────────

def backtest_old_duck_head(conn, security_id: int, start_date: str, end_date: str,
                             holding_days: int = 5) -> list:
    """
    老鸭头策略
    条件：
      1. MA5下穿MA10（鸭脖子死叉）
      2. 回调后，MA5再次上穿MA10（金叉）
      3. 金叉当日成交量放大（>MA5量）
      4. 价格在20日均线上方
    买入：金叉后次日开盘
    卖出：持有holding_days天
    """
    df = load_klines(conn, security_id, start_date, end_date)
    if len(df) < 30:
        return []

    results = []
    for i in range(10, len(df) - holding_days - 1):
        today = df.iloc[i]
        yesterday = df.iloc[i - 1]
        prev5 = df.iloc[i - 5:i]

        ma5_today = float(today['ma5_price']) if pd.notna(today['ma5_price']) else None
        ma5_yesterday = float(yesterday['ma5_price']) if pd.notna(yesterday['ma5_price']) else None
        ma10_today = float(today['ma10_price']) if pd.notna(today['ma10_price']) else None
        ma10_yesterday = float(yesterday['ma10_price']) if pd.notna(yesterday['ma10_price']) else None
        ma20_today = float(today['ma20_price']) if pd.notna(today['ma20_price']) else None
        ma5_vol = float(today['ma5_volume']) if pd.notna(today['ma5_volume']) else None
        vol_today = int(today['volume']) if pd.notna(today['volume']) else 0
        close_today = float(today['close_price']) if pd.notna(today['close_price']) else None

        if not all([ma5_today, ma5_yesterday, ma10_today, ma10_yesterday, ma20_today, ma5_vol, close_today]):
            continue

        # 检查前面是否有MA5<MA10的阶段（鸭脖子）
        prev_ma5_below = False
        for j in range(i - 10, i - 1):
            ma5_j = float(df.iloc[j]['ma5_price']) if pd.notna(df.iloc[j]['ma5_price']) else None
            ma10_j = float(df.iloc[j]['ma10_price']) if pd.notna(df.iloc[j]['ma10_price']) else None
            if ma5_j is not None and ma10_j is not None and ma5_j < ma10_j:
                prev_ma5_below = True
                break

        if not prev_ma5_below:
            continue

        # MA5上穿MA10（金叉）
        if not (ma5_today > ma10_today and ma5_yesterday <= ma10_yesterday):
            continue
        # 价格在20日线上方
        if close_today < ma20_today:
            continue
        # 成交量放大
        if vol_today <= ma5_vol:
            continue

        # 买入：次日开盘
        buy_row = df.iloc[i + 1]
        if pd.isna(buy_row['open_price']):
            continue
        entry_price = float(buy_row['open_price'])

        # 卖出
        sell_row = df.iloc[i + holding_days]
        if pd.isna(sell_row['close_price']):
            continue
        exit_price = float(sell_row['close_price'])

        pnl_pct = (exit_price - entry_price) / entry_price * 100
        amount_yi = float(today['amount']) / 1e8 if pd.notna(today['amount']) else 0
        change_today = float(today['change_pct']) if pd.notna(today['change_pct']) else 0

        results.append({
            'security_id': security_id,
            'signal_date': today['trade_date'].date(),
            'entry_date': buy_row['trade_date'].date(),
            'exit_date': sell_row['trade_date'].date(),
            'entry_price': entry_price,
            'exit_price': exit_price,
            'pnl_pct': round(pnl_pct, 2),
            'holding_days': holding_days,
            'amount_yi': round(amount_yi, 2),
            'vol_ratio': round(vol_today / ma5_vol, 2),
            'change_pct': round(change_today, 2),
        })

    return results


# ─── 策略六：RSI超买超卖 ─────────────────────────────────────

def calc_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """计算RSI"""
    deltas = closes.diff()
    gains = deltas.clip(lower=0).rolling(period).mean()
    losses = (-deltas.clip(upper=0)).rolling(period).mean()
    rs = gains / losses.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def backtest_rsi(conn, security_id: int, start_date: str, end_date: str,
                  holding_days: int = 3) -> list:
    """
    RSI超买超卖策略
    条件：
      1. RSI(14) < 30（超卖区域）
      2. 今日收盘价站稳5日线
    买入：RSI<30次日开盘买入
    卖出：持有holding_days天（或RSI>70时提前卖）
    """
    df = load_klines(conn, security_id, start_date, end_date)
    if len(df) < 20:
        return []

    closes = df['close_price'].copy()
    df['rsi'] = calc_rsi(closes, 14)

    results = []
    for i in range(20, len(df) - holding_days - 1):
        today = df.iloc[i]
        rsi_today = float(today['rsi']) if pd.notna(today['rsi']) else None
        close_today = float(today['close_price']) if pd.notna(today['close_price']) else None
        ma5_today = float(today['ma5_price']) if pd.notna(today['ma5_price']) else None

        if rsi_today is None or close_today is None or ma5_today is None:
            continue

        # RSI<30 且站稳MA5
        if not (rsi_today < 30 and close_today > ma5_today):
            continue

        # 买入
        buy_row = df.iloc[i + 1]
        if pd.isna(buy_row['open_price']):
            continue
        entry_price = float(buy_row['open_price'])

        # 卖出（持有N天）
        sell_row = df.iloc[i + holding_days]
        if pd.isna(sell_row['close_price']):
            continue
        exit_price = float(sell_row['close_price'])

        pnl_pct = (exit_price - entry_price) / entry_price * 100
        amount_yi = float(today['amount']) / 1e8 if pd.notna(today['amount']) else 0

        results.append({
            'security_id': security_id,
            'signal_date': today['trade_date'].date(),
            'entry_date': buy_row['trade_date'].date(),
            'exit_date': sell_row['trade_date'].date(),
            'entry_price': entry_price,
            'exit_price': exit_price,
            'pnl_pct': round(pnl_pct, 2),
            'holding_days': holding_days,
            'amount_yi': round(amount_yi, 2),
            'rsi': round(rsi_today, 1),
            'change_pct': round(float(today['change_pct']), 2) if pd.notna(today['change_pct']) else 0,
        })

    return results


# ─── 策略七：布林带极限 ─────────────────────────────────────

def backtest_bollinger(conn, security_id: int, start_date: str, end_date: str,
                         holding_days: int = 3) -> list:
    """
    布林带极限策略
    条件：
      1. 价格触及布林带下轨（MA20 - 2*STD）
      2. 布林带宽度处于历史低位（波动率收缩）
      3. 成交量萎缩（地量）
    买入：触及下轨后次日开盘
    卖出：持有holding_days天（或触及上轨提前卖）
    """
    df = load_klines(conn, security_id, start_date, end_date)
    if len(df) < 25:
        return []

    # 计算布林带
    df['bb_mid'] = df['close_price'].rolling(20).mean()
    df['bb_std'] = df['close_price'].rolling(20).std()
    df['bb_lower'] = df['bb_mid'] - 2 * df['bb_std']
    df['bb_upper'] = df['bb_mid'] + 2 * df['bb_std']
    df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['bb_mid']

    # 布林带宽度历史分位
    df['bb_width_pct'] = df['bb_width'].rolling(60).apply(
        lambda x: pd.Series(x).rank(pct=True).iloc[-1] if len(x) >= 20 else np.nan
    , raw=False)

    results = []
    for i in range(25, len(df) - holding_days - 1):
        today = df.iloc[i]
        close_today = float(today['close_price']) if pd.notna(today['close_price']) else None
        bb_lower = float(today['bb_lower']) if pd.notna(today['bb_lower']) else None
        bb_upper = float(today['bb_upper']) if pd.notna(today['bb_upper']) else None
        bb_width_pct = float(today['bb_width_pct']) if pd.notna(today['bb_width_pct']) else None
        vol_today = int(today['volume']) if pd.notna(today['volume']) else 0
        vol_ma5 = float(today['ma5_volume']) if pd.notna(today['ma5_volume']) else None

        if not all([close_today, bb_lower, bb_upper, vol_ma5]):
            continue

        # 价格触及下轨
        if close_today > bb_lower * 1.02:
            continue

        # 布林带宽度处于历史30%分位以下（波动率收缩）
        if bb_width_pct is not None and bb_width_pct > 0.3:
            continue

        # 成交量萎缩（<MA5量）
        if vol_today >= vol_ma5:
            continue

        # 买入
        buy_row = df.iloc[i + 1]
        if pd.isna(buy_row['open_price']):
            continue
        entry_price = float(buy_row['open_price'])

        # 卖出
        sell_row = df.iloc[i + holding_days]
        if pd.isna(sell_row['close_price']):
            continue
        exit_price = float(sell_row['close_price'])

        pnl_pct = (exit_price - entry_price) / entry_price * 100
        amount_yi = float(today['amount']) / 1e8 if pd.notna(today['amount']) else 0

        results.append({
            'security_id': security_id,
            'signal_date': today['trade_date'].date(),
            'entry_date': buy_row['trade_date'].date(),
            'exit_date': sell_row['trade_date'].date(),
            'entry_price': entry_price,
            'exit_price': exit_price,
            'pnl_pct': round(pnl_pct, 2),
            'holding_days': holding_days,
            'amount_yi': round(amount_yi, 2),
            'bb_width_pct': round(bb_width_pct, 2) if bb_width_pct else None,
            'vol_ratio': round(vol_today / vol_ma5, 2) if vol_ma5 > 0 else 0,
        })

    return results


# ─── 统计计算 ───────────────────────────────────────────────

def calc_stats(results: list) -> dict:
    """计算策略统计数据"""
    if not results:
        return {
            'count': 0, 'win_rate': None, 'avg_win': None,
            'avg_loss': None, 'profit_loss_ratio': None,
            'expected_value': None, 'total_pnl': None,
            'max_win': None, 'max_loss': None, 'avg_holding': None,
        }

    pnls = [r['pnl_pct'] for r in results]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    count = len(results)
    win_rate = len(wins) / count * 100 if count > 0 else 0
    avg_win = np.mean(wins) if wins else 0
    avg_loss = np.mean(losses) if losses else 0
    pl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    expected = (win_rate / 100 * avg_win) - ((1 - win_rate / 100) * abs(avg_loss))
    total_pnl = sum(pnls)
    max_win = max(wins) if wins else 0
    max_loss = min(losses) if losses else 0

    return {
        'count': count,
        'win_rate': round(win_rate, 1),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'profit_loss_ratio': round(pl_ratio, 2),
        'expected_value': round(expected, 2),
        'total_pnl': round(total_pnl, 2),
        'max_win': round(max_win, 2),
        'max_loss': round(max_loss, 2),
    }


# ─── 全量回测主函数 ─────────────────────────────────────────

def run_full_backtest(
    strategy_fn,
    start_date: str = "2024-01-01",
    end_date: str = "2026-06-01",
    holding_days_list: list = [1, 3, 5],
    max_stocks: int = 200,  # 先用200只股票测试
) -> dict:
    """
    对所有策略跑全量回测
    """
    conn = get_conn()

    # 获取股票列表（取有K线数据的活跃股）
    stocks = conn.execute("""
        SELECT s.security_id, s.symbol, s.name
        FROM security_master s
        INNER JOIN (
            SELECT security_id, COUNT(*) as bar_count
            FROM daily_bar
            WHERE trade_date >= '2024-01-01'
            GROUP BY security_id
        ) b ON s.security_id = b.security_id
        WHERE s.list_status = 'listed'
          AND b.bar_count > 200
        LIMIT ?
    """, [max_stocks]).fetchall()

    all_results = {}
    for holding_days in holding_days_list:
        print(f"\n{'='*60}")
        print(f"📊 回测中：持仓 {holding_days} 天 | 股票 {max_stocks} 只")
        print(f"{'='*60}")

        strategy_results = []
        processed = 0

        for (sid, symbol, name) in stocks:
            try:
                res = strategy_fn(conn, sid, start_date, end_date, holding_days)
                strategy_results.extend(res)
                processed += 1
                if processed % 50 == 0:
                    print(f"  已处理 {processed}/{max_stocks} 只股票，信号数: {len(strategy_results)}")
            except Exception as e:
                continue

        print(f"\n  ✅ 完成：共 {len(strategy_results)} 个交易信号")

        stats = calc_stats(strategy_results)
        stats['signals'] = strategy_results  # 保留原始信号数据
        all_results[f'hold_{holding_days}d'] = stats

        # 打印统计摘要
        print(f"\n  📈 统计结果（持有{holding_days}天）：")
        print(f"     信号数: {stats['count']}")
        print(f"     胜率: {stats['win_rate']}%")
        print(f"     均盈: +{stats['avg_win']}%")
        print(f"     均亏: {stats['avg_loss']}%")
        print(f"     盈亏比: {stats['profit_loss_ratio']}")
        print(f"     期望值: {stats['expected_value']}%")
        print(f"     最大单笔盈利: +{stats['max_win']}%")
        print(f"     最大单笔亏损: {stats['max_loss']}%")

    conn.close()
    return all_results


# ─── 主程序 ─────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    strategies = {
        "01_first_board": ("首板策略", backtest_first_board),
        "02_n_pattern": ("N字反转", backtest_n_pattern),
        "03_520": ("520战法", backtest_520),
        "04_vol_surge": ("倍量回调", backtest_volume_surge_pullback),
        "05_old_duck": ("老鸭头", backtest_old_duck_head),
        "06_rsi": ("RSI超卖", backtest_rsi),
        "07_bollinger": ("布林带", backtest_bollinger),
    }

    holding_days_list = [1, 3, 5]  # T+1、3天、5天三种持仓

    all_strategy_results = {}

    for key, (name, fn) in strategies.items():
        print(f"\n\n{'#'*60}")
        print(f"# 策略：{name}")
        print(f"{'#'*60}")
        results = run_full_backtest(fn, holding_days_list=holding_days_list, max_stocks=300)
        all_strategy_results[key] = {"name": name, "results": results}

    # ─── 汇总对比表 ─────────────────────────────────────────
    print("\n\n" + "=" * 100)
    print("📊 所有策略汇总对比（按期望值排序）")
    print("=" * 100)

    summary_rows = []
    for key, strat in all_strategy_results.items():
        name = strat['name']
        for hold_key, stats in strat['results'].items():
            if stats['count'] > 0:
                summary_rows.append({
                    'strategy': name,
                    'holding': hold_key,
                    'signals': stats['count'],
                    'win_rate': stats['win_rate'],
                    'avg_win': stats['avg_win'],
                    'avg_loss': stats['avg_loss'],
                    'pl_ratio': stats['profit_loss_ratio'],
                    'expected': stats['expected_value'],
                })

    summary_df = pd.DataFrame(summary_rows)
    summary_df = summary_df.sort_values('expected', ascending=False)

    header = f"{'策略':<18} {'持仓':<8} {'信号数':>6} {'胜率':>6} {'均盈':>8} {'均亏':>8} {'盈亏比':>7} {'期望值':>8}"
    print(header)
    print("-" * 80)
    for _, row in summary_df.iterrows():
        print(f"{row['strategy']:<18} {row['holding']:<8} {row['signals']:>6} "
              f"{row['win_rate']:>5.1f}% {row['avg_win']:>+7.2f}% {row['avg_loss']:>+7.2f}% "
              f"{row['pl_ratio']:>6.2f}  {row['expected']:>+7.2f}%")

    # 保存结果
    summary_path = f"{OUTPUT_DIR}/strategy_summary.csv"
    summary_df.to_csv(summary_path, index=False, encoding='utf-8-sig')
    print(f"\n✅ 汇总已保存: {summary_path}")

    # 保存详细信号
    for key, strat in all_strategy_results.items():
        for hold_key, stats in strat['results'].items():
            if stats['count'] > 0:
                df = pd.DataFrame(stats['signals'])
                fn = f"{OUTPUT_DIR}/{key}_{hold_key}_signals.csv"
                df.to_csv(fn, index=False, encoding='utf-8-sig')

    print(f"✅ 详细信号已保存: {OUTPUT_DIR}/")
