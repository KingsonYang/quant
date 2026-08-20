"""
信号综合评分系统 v1
对每个信号计算多维度评分，返回 0-100 的综合评分
评分维度：期望收益 + 趋势强度 + 量能强度 + 风险分散
"""
import sys
sys.path.insert(0, 'D:/SoftWare/tdx/PYPlugins/user')
import os
os.environ['TDXPATH'] = 'D:/SoftWare/tdx/TdxW.exe'

import duckdb
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

DB_PATH = "D:/Files/workspace-ai/quant/data/quant.duckdb"

# 权重配置
WEIGHTS = {
    'expected': 0.30,   # 历史期望收益
    'trend': 0.25,     # 趋势强度
    'volume': 0.20,     # 量能强度
    'relative': 0.15,   # 相对强度（相对沪深300）
    'uniqueness': 0.10, # 当日唯一性
}


class SignalScorer:
    def __init__(self, conn):
        self.conn = conn

    def score(self, signals_df, trade_date_col='entry_date', stock_col='security_id'):
        """
        对信号集计算综合评分
        signals_df 必须包含: security_id, entry_date, entry_price, strategy_name
        返回带 score 列的 DataFrame
        """
        if signals_df.empty:
            return signals_df

        df = signals_df.copy()

        # 1. 期望收益评分 (0-100)
        df['score_expected'] = self._score_expected(df)

        # 2. 趋势强度评分 (0-100)
        df = self._add_price_data(df, trade_date_col)
        df['score_trend'] = self._score_trend(df)

        # 3. 量能强度评分 (0-100)
        df['score_volume'] = self._score_volume(df)

        # 4. 相对强度评分 (0-100)
        df['score_relative'] = self._score_relative(df, trade_date_col)

        # 5. 唯一性评分 (0-100)
        df['score_uniqueness'] = self._score_uniqueness(df, trade_date_col, stock_col)

        # 综合评分
        df['score'] = (
            df['score_expected'] * WEIGHTS['expected'] +
            df['score_trend'] * WEIGHTS['trend'] +
            df['score_volume'] * WEIGHTS['volume'] +
            df['score_relative'] * WEIGHTS['relative'] +
            df['score_uniqueness'] * WEIGHTS['uniqueness']
        )

        return df

    def _score_expected(self, df):
        """基于策略历史期望值评分"""
        # 策略期望值基准表
        strategy_expected = {
            'old_duck': 0.37,   # 老鸭头 训练集期望
            '520': 0.57,         # 520战法 训练集期望
            'vol_surge': 0.16,   # 倍量回调 训练集期望
        }
        # 归一化到 0-100，以 1% 期望为满分
        scores = df['strategy_name'].map(strategy_expected).fillna(0) / 1.0 * 100
        return scores.clip(0, 100)

    def _add_price_data(self, df, trade_date_col):
        """补充K线数据"""
        if df.empty:
            return df

        # 获取需要的股票和日期范围
        stocks = df['security_id'].unique()
        dates = pd.to_datetime(df[trade_date_col]).unique()

        if len(stocks) == 0 or len(dates) == 0:
            return df

        min_date = pd.Timestamp(dates.min()) - pd.Timedelta(days=30)
        max_date = pd.Timestamp(dates.max()) + pd.Timedelta(days=5)

        query = f"""
        WITH base AS (
            SELECT security_id, trade_date,
                close_price, open_price, high_price, low_price, volume, amount,
                AVG(close_price) OVER w5 as ma5,
                AVG(close_price) OVER w10 as ma10,
                AVG(close_price) OVER w20 as ma20,
                AVG(close_price) OVER w60 as ma60,
                AVG(volume) OVER w5 as vol_ma5
            FROM daily_bar
            WHERE trade_date >= '{min_date.date()}'
              AND trade_date <= '{max_date.date()}'
              AND security_id IN ({','.join(str(int(s)) for s in stocks)})
            WINDOW
                w5 AS (PARTITION BY security_id ORDER BY trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
                w10 AS (PARTITION BY security_id ORDER BY trade_date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW),
                w20 AS (PARTITION BY security_id ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
                w60 AS (PARTITION BY security_id ORDER BY trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW)
        )
        SELECT security_id, trade_date, close_price, open_price, high_price, low_price,
               volume, amount, ma5, ma10, ma20, ma60, vol_ma5
        FROM base
        """
        price_data = self.conn.execute(query).fetchdf()

        if price_data.empty:
            return df

        # 合并到信号
        df['_trade_date'] = pd.to_datetime(df[trade_date_col])
        price_data = price_data.rename(columns={'trade_date': '_trade_date'})
        df = df.merge(
            price_data[['security_id', '_trade_date', 'close_price', 'ma5', 'ma10', 'ma20', 'vol_ma5', 'volume']],
            left_on=['security_id', '_trade_date'],
            right_on=['security_id', '_trade_date'],
            how='left',
            suffixes=('', '_price')
        )
        return df

    def _score_trend(self, df):
        """趋势强度：MA多头排列程度"""
        if 'ma5' not in df.columns or 'ma10' not in df.columns or 'ma20' not in df.columns:
            return 50.0  # 默认中性

        ma5_ok = (df['ma5'] > df['ma10']).astype(float).fillna(0)
        ma10_ok = (df['ma10'] > df['ma20']).astype(float).fillna(0)
        price_above_ma20 = (df['close_price'] > df['ma20']).astype(float).fillna(0)

        # 三者都为真 = 完美多头排列 = 100分
        score = (ma5_ok * 0.4 + ma10_ok * 0.3 + price_above_ma20 * 0.3) * 100 / 0.7
        return score.clip(0, 100)

    def _score_volume(self, df):
        """量能强度：当日量/前5日均量"""
        if 'volume' not in df.columns or 'vol_ma5' not in df.columns:
            return 50.0

        ratio = df['volume'] / df['vol_ma5'].replace(0, np.nan)
        # 1.5倍 = 60分，3倍 = 100分
        score = ((ratio - 1.0) / 2.0 * 40 + 50).clip(0, 100)
        return score.fillna(50)

    def _score_relative(self, df, trade_date_col):
        """相对强度：相对于沪深300的超额收益"""
        # 这里简化：用当日涨幅替代（实际应该用沪深300对比）
        if 'entry_change_pct' not in df.columns:
            return 50.0
        # 涨幅2%-5%最优：100分，<0或>9%：0分
        pct = df['entry_change_pct'].fillna(0)
        score = ((pct - 0) / 5.0 * 60 + 20).clip(0, 100)
        return score

    def _score_uniqueness(self, df, trade_date_col, stock_col):
        """
        唯一性评分：当日同策略信号越少越好（减少日内冲突）
        100只股票同日信号 vs 5只 → 5只的得100分
        """
        if df.empty:
            return pd.Series(dtype=float)

        date_col = trade_date_col
        counts = df.groupby([date_col, 'strategy_name'])[stock_col].transform('count')
        # 信号数越少分数越高，10只以内满分
        score = (10 / counts.clip(lower=1)) * 100
        return score.clip(0, 100)


def get_strategy_params():
    """返回各策略的最优参数"""
    return {
        'old_duck': {
            'holding_days': 3,
            'ma_strict': 0.005,
            'volume_mult': 1.2,
            'had_below_window': 10,
            'amount_filter_yi': 1.0,
        },
        '520': {
            'holding_days': 5,
            'ma_strict': 0.005,
            'volume_mult': 1.2,
            'amount_filter_yi': 1.0,
            'need_trend': False,
        },
        'vol_surge': {
            'holding_days': 5,
            'vol_mult': 1.5,
            'pullback1': 0.6,
            'pullback2': 0.6,
            'change_min': 3.0,
            'change_max': 8.0,
            'amount_filter_yi': 1.0,
        },
    }
