# -*- coding: utf-8 -*-
"""
趋势雷达（选股）—— 干净重写版 v1

设计哲学：选股是减法，不是加法。用四层漏斗把 5000 只里绝大多数不合格的排除掉，
剩下的几十只做成"观察池"，每只带上诊断数据和止损位，方便人工肉眼复盘。

四层漏斗：
  ① 基础排除：只留沪深主板、非 ST、非退市、流动性达标
  ② 趋势排除：只留 close > MA20 > MA60 且 MA60 走平/向上（多头结构）
  ③ 强度排选：近 20 日内有过放量突破平台，量价健康
  ④ 分类打标：启动 / 回踩 / 加速 / 风险，各给评分与止损位

用法：
  # 跑最近交易日（或指定历史日做验证）
  python scripts/radar/trend_radar.py --date 20260630
  python scripts/radar/trend_radar.py              # 默认今天

输出：
  data/radar/YYYYMMDD_pool.csv   观察池（按 bucket + score 排序）
"""

import sys
import argparse
import datetime
from pathlib import Path

import pandas as pd

# ── 通达信接口路径（与 daily_picker.py 一致）──
sys.path.insert(0, 'D:/SoftWare/tdx/PYPlugins')
sys.path.insert(0, 'D:/SoftWare/tdx/PYPlugins/user')
from tqcenter import tq

OUT_DIR = Path('D:/Files/workspace-ai/quant/data/radar')

# ── 漏斗参数（集中在此，方便调）──
P = {
    'min_amount_yi': 1.0,      # ①层：当日成交额下限（亿），流动性门槛
    'ma60_lookback': 5,        # ②层：MA60 与几日前比较判断走平/向上
    'breakout_window': 20,     # ③层：在近 N 日内寻找放量突破
    'breakout_vol_mult': 1.5,  # ③层：突破日量能 >= 1.5 倍 5 日均量
    'kline_count': 90,         # 取多少根日K
    # ④层分类阈值
    'accel_chg20_min': 30.0,   # 加速：20日涨幅下限%
    'risk_dist_ma20': 20.0,    # 风险：距 MA20 超过此值%（严重远离）
    'risk_chg20': 50.0,        # 风险：20日涨幅过热%
    'risk_upper_shadow': 5.0,  # 风险：长上影阈值%（(high-close)/close）
    'pullback_dist_max': 12.0, # 回踩：距 MA20 不超过此值%（否则不算舒服买点）
}


# ══════════════════════════════════════════════════════════
# 第①层：基础股票池
# ══════════════════════════════════════════════════════════
def get_base_universe():
    """沪深主板、非 ST、非退市。返回 [{'code','name'}]"""
    raw = []
    for mkt in ['7', '8']:  # 7=上证主板 8=深证主板
        try:
            lst = tq.get_stock_list(mkt, list_type=1)
            if lst:
                raw.extend(lst)
        except Exception as e:
            print(f"  get_stock_list({mkt}) 失败: {e}")

    universe = []
    for item in raw:
        code = item.get('Code') or item.get('code', '')
        name = item.get('Name') or item.get('name', '')
        if not code:
            continue
        # 排除 ST / *ST / 退市
        if 'ST' in name.upper() or '退' in name:
            continue
        # 主板前缀白名单（get_stock_list 已按主板过滤，这里双保险）
        pure = code.split('.')[0]
        if not (pure.startswith(('600', '601', '603', '605',
                                 '000', '001', '002', '003'))):
            continue
        universe.append({'code': code, 'name': name})
    return universe


# ══════════════════════════════════════════════════════════
# 取 K 线（沿用 daily_picker 已验证的调用方式）
# ══════════════════════════════════════════════════════════
def fetch_klines(codes, date_str, count):
    """批量取日K，返回 {code: DataFrame(open/high/low/close/volume/amount)}"""
    results = {}
    batch_size = 50
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        try:
            bars = tq.get_market_data(
                field_list=["Open", "High", "Low", "Close", "Volume", "Amount"],
                stock_list=batch,
                period='1d',
                end_time=date_str,
                count=count,
                dividend_type='front',   # 前复权，均线才连续
                fill_data=True,
            )
        except Exception as e:
            print(f"  批量 {i//batch_size+1} 取K失败: {e}")
            continue
        if not bars:
            continue
        close_df = bars.get('Close')
        if close_df is None:
            continue
        for code in batch:
            if code not in close_df.columns:
                continue
            try:
                df = pd.DataFrame({
                    'open': bars['Open'][code],
                    'high': bars['High'][code],
                    'low': bars['Low'][code],
                    'close': bars['Close'][code],
                    'volume': bars['Volume'][code],
                    'amount': bars['Amount'][code],
                }).dropna()
                if len(df) >= 65:
                    results[code] = df.reset_index(drop=True)
            except Exception:
                continue
    return results


# ══════════════════════════════════════════════════════════
# 指标计算
# ══════════════════════════════════════════════════════════
def compute_features(df):
    """算出分类需要的所有特征。df 最后一行是信号日。"""
    close, high, vol = df['close'], df['high'], df['volume']
    ma = lambda s, n: s.rolling(n).mean().iloc[-1]

    f = {}
    f['close'] = float(close.iloc[-1])
    f['ma5'] = ma(close, 5)
    f['ma10'] = ma(close, 10)
    f['ma20'] = ma(close, 20)
    f['ma60'] = ma(close, 60)
    f['ma60_prev'] = close.rolling(60).mean().iloc[-1 - P['ma60_lookback']]
    f['vol_ma5'] = ma(vol, 5)
    f['vol_ma20'] = ma(vol, 20)
    f['amount_yi'] = float(df['amount'].iloc[-1]) / 1e4   # amount 单位为万元

    # 涨幅
    f['chg5'] = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) > 6 else 0
    f['chg20'] = (close.iloc[-1] / close.iloc[-21] - 1) * 100 if len(close) > 21 else 0
    # 距 MA20
    f['dist_ma20'] = (f['close'] / f['ma20'] - 1) * 100 if f['ma20'] else 0
    # 当日上影线
    f['upper_shadow'] = (float(high.iloc[-1]) - f['close']) / f['close'] * 100 if f['close'] else 0
    # 当日量比（相对 5 日均量）
    f['vol_ratio'] = float(vol.iloc[-1]) / f['vol_ma5'] if f['vol_ma5'] else 0

    # 近 breakout_window 日内是否有过放量突破平台
    w = P['breakout_window']
    breakout_ago = None
    for k in range(1, min(w, len(df) - 60) + 1):
        idx = len(df) - k
        c_k = close.iloc[idx]
        v_k = vol.iloc[idx]
        vma_k = vol.iloc[max(0, idx - 5):idx].mean()
        # 突破：当日收盘是近 20 日最高 且 放量
        prior_high = close.iloc[max(0, idx - w):idx].max()
        if c_k >= prior_high and vma_k > 0 and v_k >= vma_k * P['breakout_vol_mult']:
            breakout_ago = k  # 距今 k 个交易日前有过突破
            break
    f['breakout_ago'] = breakout_ago
    return f


# ══════════════════════════════════════════════════════════
# 第②③④层：过滤 + 分类
# ══════════════════════════════════════════════════════════
def classify(f):
    """返回 (bucket, score, stop_loss, note) 或 None（被漏斗排除）"""
    # ── 第②层 趋势排除：必须多头结构 + MA60 不向下 ──
    if not (f['close'] > f['ma20'] > f['ma60']):
        return None
    if f['ma60'] < f['ma60_prev']:   # MA60 向下，趋势没转，排除
        return None

    duo = f['ma5'] > f['ma10'] > f['ma20'] > f['ma60']   # 完全多头排列

    # ── 风险信号收集 ──
    risk_flags = []
    if f['dist_ma20'] > P['risk_dist_ma20']:
        risk_flags.append('严重远离MA20')
    if f['chg20'] > P['risk_chg20']:
        risk_flags.append('20日涨幅过热')
    if f['upper_shadow'] > P['risk_upper_shadow'] and f['vol_ratio'] > 1.5:
        risk_flags.append('高位放量长上影')
    if f['close'] < f['ma5']:
        risk_flags.append('跌破MA5')

    # ── 第④层 分类（优先级：风险 > 回踩 > 启动 > 加速）──
    # 风险：有 2 个及以上风险信号，或严重远离+过热同时出现
    if len(risk_flags) >= 2:
        score = f['chg20'] - f['dist_ma20']   # 越危险其实越强，仅供排序
        return ('QSFX风险', round(score, 1), round(f['ma5'], 2), '; '.join(risk_flags))

    # 回踩：近期有过突破，现在缩量回踩不破 MA10，距 MA20 不极端
    if (f['breakout_ago'] is not None and f['breakout_ago'] >= 2
            and f['close'] > f['ma10']
            and f['vol_ratio'] < 1.0                        # 缩量
            and f['dist_ma20'] < P['pullback_dist_max']):
        score = 100 - f['dist_ma20'] + (5 if f['close'] > f['ma5'] else 0)
        note = f"突破后{f['breakout_ago']}日缩量回踩不破MA10"
        return ('QSHC回踩', round(score, 1), round(f['ma10'] * 0.98, 2), note)

    # 启动：刚放量突破平台（近 3 日内），站上 MA20/MA60，不过热
    if (f['breakout_ago'] is not None and f['breakout_ago'] <= 3
            and f['chg20'] < P['risk_chg20']
            and f['dist_ma20'] < P['risk_dist_ma20']
            and f['upper_shadow'] < P['risk_upper_shadow']):
        score = 80 + f['vol_ratio'] * 5 - f['dist_ma20']
        note = f"{f['breakout_ago']}日前放量突破平台"
        return ('QSQD启动', round(score, 1), round(f['ma20'], 2), note)

    # 加速：完全多头排列 + 强涨幅，沿 MA5/MA10 上行（偏高但必须纳入观察）
    if duo and f['chg20'] >= P['accel_chg20_min'] and f['close'] > f['ma10']:
        score = 60 + f['chg20'] * 0.2
        note = f"多头排列强趋势 20日涨{f['chg20']:.0f}%（观察非买点）"
        return ('QSJS加速', round(score, 1), round(f['ma10'], 2), note)

    return None


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════
def run(date_str):
    print(f"\n[趋势雷达] 信号日 {date_str}")
    print("─" * 56)

    # 第①层
    universe = get_base_universe()
    print(f"① 基础池（沪深主板非ST）: {len(universe)} 只")
    if not universe:
        print("  基础池为空，检查接口是否初始化 / 是否为交易日")
        return

    # 取K线
    codes = [u['code'] for u in universe]
    name_map = {u['code']: u['name'] for u in universe}
    print(f"  取K线中（{P['kline_count']}根/只）...")
    klines = fetch_klines(codes, date_str, P['kline_count'])
    print(f"  有效K线: {len(klines)} 只")

    # ②③④层
    rows = []
    for code, df in klines.items():
        # 跳过停牌 / 涨跌停信号日（追不进也没意义）
        if df['volume'].iloc[-1] == 0:
            continue
        if df['amount'].iloc[-1] / 1e4 < P['min_amount_yi']:   # ①层流动性（万元→亿）
            continue
        try:
            f = compute_features(df)
        except Exception:
            continue
        res = classify(f)
        if res is None:
            continue
        bucket, score, stop, note = res
        rows.append({
            'bucket': bucket,
            'code': code,
            'name': name_map.get(code, code),
            'score': score,
            'close': round(f['close'], 2),
            'stop_loss': stop,
            'chg5%': round(f['chg5'], 1),
            'chg20%': round(f['chg20'], 1),
            'dist_ma20%': round(f['dist_ma20'], 1),
            'vol_ratio': round(f['vol_ratio'], 2),
            'upper_shadow%': round(f['upper_shadow'], 1),
            'note': note,
        })

    if not rows:
        print("\n没有股票通过漏斗（可能市场极弱，或该日非交易日）")
        return

    pool = pd.DataFrame(rows)
    bucket_order = {'QSHC回踩': 0, 'QSQD启动': 1, 'QSJS加速': 2, 'QSFX风险': 3}
    pool['_o'] = pool['bucket'].map(bucket_order)
    pool = pool.sort_values(['_o', 'score'], ascending=[True, False]).drop(columns='_o')

    # 汇总
    print("\n② 趋势多头 → ③ 放量突破 → ④ 分类结果:")
    for b in ['QSHC回踩', 'QSQD启动', 'QSJS加速', 'QSFX风险']:
        sub = pool[pool['bucket'] == b]
        print(f"  {b}: {len(sub)} 只")

    # 打印回踩+启动（最有买点价值的两档）
    print("\n★ 最有买点价值（回踩 + 启动）TOP:")
    top = pool[pool['bucket'].isin(['QSHC回踩', 'QSQD启动'])].head(15)
    for _, r in top.iterrows():
        print(f"  [{r['bucket']}] {r['code']} {r['name']:<6s} "
              f"收{r['close']:<7} 止损{r['stop_loss']:<7} "
              f"20日{r['chg20%']:+.0f}% 距MA20{r['dist_ma20%']:+.0f}% "
              f"量比{r['vol_ratio']}  {r['note']}")

    # 存盘
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUT_DIR / f"{date_str}_pool.csv"
    pool.to_csv(out_file, index=False, encoding='utf-8-sig')
    print(f"\n观察池已存: {out_file}  （共 {len(pool)} 只）")


def get_trade_date():
    d = datetime.date.today()
    if d.weekday() == 5:
        d -= datetime.timedelta(days=1)
    elif d.weekday() == 6:
        d -= datetime.timedelta(days=2)
    return d


def main():
    ap = argparse.ArgumentParser(description='趋势雷达选股')
    ap.add_argument('--date', '-d', default='', help='信号日 YYYYMMDD（默认最近交易日）')
    args = ap.parse_args()

    tq.initialize('D:/SoftWare/tdx/TdxW.exe')
    date_str = args.date or get_trade_date().strftime('%Y%m%d')
    run(date_str)


if __name__ == '__main__':
    main()
