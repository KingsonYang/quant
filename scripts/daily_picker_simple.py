# -*- coding: utf-8 -*-
"""
每日选股脚本（傻瓜版）
=====================
用法极其简单，修改顶部 CONFIG = 'A4' 即可切换策略参数。

参数说明：
  CONFIG = 'B'     → 基准（固定5天持仓，最稳定）
  CONFIG = 'A1'    → 市场过滤（沪深300熊市不开仓，保守）
  CONFIG = 'A2'    → MA_CROSS加固（牛市效果更好）
  CONFIG = 'A3'    → 动态止盈6%（近2年最强，283%年化）
  CONFIG = 'A4'    → 持仓自适应（牛市8天/熊市3天，近1年最强）
  CONFIG = 'A5'    → 置信度过滤（只做最强信号）
  CONFIG = 'A1+2'  → 市场过滤 + MA_CROSS加固（回撤小）
  CONFIG = 'A1+2+3'→ 全组合（最激进，近2年年化205%）

切换方法：只改下面这一行 CONFIG = 'xxx'

【架构】本脚本使用 signal_queries.py 的共享 SQL，与回测 combo_verify_optimized.py
        100% 同源。选股和回测永远不会出现不一致的情况。
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 共享信号查询模块（与回测同源）
from signal_queries import (
    build_ma_cross_query, build_macd_query, build_vol_surge_query,
    get_market_regime as _get_market_regime_shared,
    query_signals_for_date, compute_score_pct_rank
)

# ══════════════════════════════════════════════════════════════
# ★ 策略参数配置（只需要改这一行！）
# ══════════════════════════════════════════════════════════════
CONFIG = 'A4'   # 可选: B, A1, A2, A3, A4, A5, A1+2, A1+2+3

# ══════════════════════════════════════════════════════════════
# 参数说明（无需修改）
# ══════════════════════════════════════════════════════════════
PARAM_PROFILES = {
    'B': {
        'label': '基准（固定5天）',
        'market_filter':    False,
        'ma_cross_strict':  False,
        'profit_target':    0,       # 不开启动态止盈
        'adaptive_holding': False,
        'confidence_threshold': 0,
        'holding_days': 5,
        'note': '最稳定，适合新手；不判断市场，始终全仓轮动'
    },
    'A1': {
        'label': '市场过滤（沪深300熊市不开仓）',
        'market_filter':    True,
        'ma_cross_strict':  False,
        'profit_target':    0,
        'adaptive_holding': False,
        'confidence_threshold': 0,
        'holding_days': 5,
        'note': '沪深300 MA20向下时停止开仓，熊市保护资金'
    },
    'A2': {
        'label': 'MA_CROSS加固（牛市效果更好）',
        'market_filter':    False,
        'ma_cross_strict':  True,   # 涨幅>0 + 量比>2x
        'profit_target':    0,
        'adaptive_holding': False,
        'confidence_threshold': 0,
        'holding_days': 5,
        'note': '要求MA_CROSS信号当日涨幅为正且量比>2x，减少假信号'
    },
    'A3': {
        'label': '动态止盈6%（近2年最强）',
        'market_filter':    False,
        'ma_cross_strict':  False,
        'profit_target':    6.0,     # 涨超6%自动止盈
        'adaptive_holding': False,
        'confidence_threshold': 0,
        'holding_days': 5,
        'note': '持仓期间涨超6%自动止盈，近2年验证283%年化'
    },
    'A4': {
        'label': '持仓自适应（近1年最强）',
        'market_filter':    False,
        'ma_cross_strict':  False,
        'profit_target':    0,
        'adaptive_holding': True,   # 牛市8天/熊市3天
        'confidence_threshold': 0,
        'holding_days': 5,
        'note': '牛市持仓8天吃完整行情，熊市持仓3天快进快出'
    },
    'A5': {
        'label': '置信度过滤（只做最强信号）',
        'market_filter':    False,
        'ma_cross_strict':  False,
        'profit_target':    0,
        'adaptive_holding': False,
        'confidence_threshold': 0.85,  # 只做前15%最强信号
        'holding_days': 5,
        'note': '只选pct_rank>0.85的历史最强信号，信号少但质量高'
    },
    'A1+2': {
        'label': '市场过滤 + MA_CROSS加固',
        'market_filter':    True,
        'ma_cross_strict':  True,
        'profit_target':    0,
        'adaptive_holding': False,
        'confidence_threshold': 0,
        'holding_days': 5,
        'note': '熊市不开仓 + 信号质量要求高，回撤最小'
    },
    'A1+2+3': {
        'label': '全组合（最激进）',
        'market_filter':    True,
        'ma_cross_strict':  True,
        'profit_target':    6.0,
        'adaptive_holding': True,
        'confidence_threshold': 0,
        'holding_days': 5,
        'note': '同时开启所有优化，近2年205%年化，波动也最大'
    },
}

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import datetime
import pandas as pd
from pathlib import Path

DB_PATH       = "D:/Files/workspace-ai/quant/data/quant.duckdb"
PICKS_DIR     = Path("D:/Files/workspace-ai/quant/data/daily_picks")
TRADES_PATH   = "D:/Files/workspace-ai/quant/data/param_results/combo_realistic_trades.csv"
MAX_POSITIONS = 2

# 基础参数（所有策略通用）
MA_SLOW   = 60
STOP_PCT  = 0.97
INITIAL_CAPITAL = 100000.0

# ──────────────────────────────────────────────
# 子策略参数（和回测完全一致）
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

BACKTEST_START = datetime.date(2020, 1, 1)
BACKTEST_END   = datetime.date(2026, 6, 1)

# ══════════════════════════════════════════════════════════════
# 【架构说明】SQL 查询已经从 combo_verify_optimized.py 抽离到 signal_queries.py
# 选股和回测共享同一份 SQL,不可能再出现不一致
# ══════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════
# 市场环境检测
# ══════════════════════════════════════════════════════════════

def get_market_regime(conn, start, end):
    """返回每天的市场方向：'bull'/'bear'，基于沪深300 MA20方向（共享 signal_queries）"""
    return _get_market_regime_shared(conn, start, end)


def get_today_market_status(conn, date_str):
    """返回今天的市场状态（bull/bear）"""
    import datetime
    date_obj = datetime.date(int(date_str[:4]), int(date_str[4:6]), int(date_str[7:8]))
    regimes = get_market_regime(conn, '2020-01-01', date_str)
    return regimes.get(date_obj, 'unknown')


# ══════════════════════════════════════════════════════════════
# 候选股筛选（核心逻辑 - 使用共享 signal_queries）
# ══════════════════════════════════════════════════════════════

def screen_candidates(date_str, profile, pct_range=None):
    """
    对指定日期运行选股，返回候选股票列表。
    使用 signal_queries.query_signals_for_date（与回测同源 SQL）
    
    参数:
        date_str: 信号日期
        profile: 策略 profile
        pct_range: (start, end) 百分位排名范围,默认 None 表示 in_range (用当天)
    
    返回: DataFrame [security_id, strategy, raw_score, change_pct, reason]
    """
    import duckdb

    conn = duckdb.connect(DB_PATH)
    conn.execute("SET enable_progress_bar=false")

    holding_days = profile.get('holding_days', 5)
    
    # 调用共享查询接口（自动处理 end 范围 + holding_days buffer）
    results = query_signals_for_date(
        conn, DB_PATH, date_str, profile, holding_days=holding_days
    )

    conn.close()

    # 合并 3 个策略的信号
    all_signals = []
    for strat, df in results.items():
        if len(df) > 0:
            df = df.copy()
            df['strategy'] = strat
            all_signals.append(df)
    
    if not all_signals:
        return None

    df_all = pd.concat(all_signals, ignore_index=True)
    
    # 如果指定了 pct_range,用整段范围的 signals 算 pct_rank
    if pct_range is not None:
        pct_start, pct_end = pct_range
        from signal_queries import build_ma_cross_query, build_macd_query, build_vol_surge_query
        conn2 = duckdb.connect(DB_PATH)
        conn2.execute("SET enable_progress_bar=false")
        # 查整段范围的 signals
        all_dfs = []
        for strat, builder in [('MA_CROSS', build_ma_cross_query), 
                                ('MACD', build_macd_query), 
                                ('VOL_SURGE', build_vol_surge_query)]:
            if strat == 'MA_CROSS':
                q = builder(pct_start, pct_end, strict=profile.get('ma_cross_strict', False), holding_days=holding_days)
            else:
                q = builder(pct_start, pct_end, holding_days=holding_days)
            hist_df = conn2.execute(q).df()
            hist_df['strategy'] = strat
            all_dfs.append(hist_df)
        conn2.close()
        hist_all = pd.concat(all_dfs, ignore_index=True)
        
        # 用 hist 算 pct_rank
        df_all['_pct_rank'] = 0.0
        for strat in ['MA_CROSS', 'MACD', 'VOL_SURGE']:
            df_mask = df_all['strategy'] == strat
            hist_mask = hist_all['strategy'] == strat
            if df_mask.sum() > 0 and hist_mask.sum() > 0:
                hist_scores = hist_all.loc[hist_mask, 'raw_score'].values
                df_all.loc[df_mask, '_pct_rank'] = df_all.loc[df_mask, 'raw_score'].apply(
                    lambda x: (hist_scores < x).sum() / len(hist_scores)
                )
        df_all['score'] = df_all['_pct_rank']
        print(f"  [百分位] pct_range={pct_range}（与回测 {pct_start}~{pct_end} 范围一致）")
    else:
        # 默认 in_range（用当天样本,跟回测有 ~1/2 选股差异）
        df_all = compute_score_pct_rank(df_all, method='in_range')
        print(f"  [百分位] in_range 方法（与回测有 ~50% 重合,推荐用 --pct-range 与回测一致）")
    
    # 添加 change_pct 字段（从 daily_bar 查，因为共享 SQL 没返回这个字段）
    # 用 1 次查询补全所有股票的当日 change_pct
    if 'change_pct' not in df_all.columns:
        import duckdb
        conn2 = duckdb.connect(DB_PATH)
        conn2.execute("SET enable_progress_bar=false")
        sec_ids = df_all['security_id'].unique().tolist()
        if sec_ids:
            sec_ids_str = ','.join(str(s) for s in sec_ids)
            cp_df = conn2.execute(f"""
                SELECT security_id, change_pct
                FROM daily_bar
                WHERE trade_date = '{date_str}' AND security_id IN ({sec_ids_str})
            """).df()
            df_all = df_all.merge(cp_df, on='security_id', how='left')
        conn2.close()

    # ── 百分位化（已在上方 in_range / pct_range 完成）──
    df_all['_global_pct'] = df_all['score'].fillna(0)
    df_all = df_all.sort_values('_global_pct', ascending=False).reset_index(drop=True)

    # ── A5 置信度过滤 ──
    if profile['confidence_threshold'] > 0:
        threshold = profile['confidence_threshold']
        before = len(df_all)
        df_all = df_all[df_all['_global_pct'] >= threshold].copy()
        print(f"  [置信度过滤] {before} → {len(df_all)} 只（保留pct_rank>={threshold}）")

    # ── 生成 reason ──
    def make_reason(row):
        if row['strategy'] == 'MA_CROSS':
            return f"MA5/60金叉 gap={row['raw_score']*100:.2f}%"
        elif row['strategy'] == 'MACD':
            return f"MACD金叉 diff={row['raw_score']:.4f}"
        else:
            return f"放量涨幅{row['raw_score']:.1f}%"

    df_all['reason'] = df_all.apply(make_reason, axis=1)

    # ── 关联股票名称 ──
    df_all = enrich_names(df_all)

    return df_all


def enrich_names(df):
    """关联股票名称"""
    if df is None or df.empty:
        return df
    import duckdb
    conn = duckdb.connect(DB_PATH)
    conn.execute("SET enable_progress_bar=false")
    try:
        names = conn.execute("""
            SELECT security_id, code, name
            FROM security_master
            WHERE exchange IN ('SH', 'SZ', 'BJ')
        """).df()
        conn.close()
        df = df.merge(names, on='security_id', how='left')
        # code: 优先用 security_master 的 6 位 code
        df['code'] = df['code'].fillna(df['security_id'].astype(str))
        # name: 优先用名字
        df['name'] = df['name'].fillna(df['code'])
        # 市场前缀：根据 code 前缀判断 SH/SZ
        def market_prefix(c):
            s = str(c)
            if s.startswith(('6', '9')): return 'SH'
            if s.startswith(('0', '2', '3')): return 'SZ'
            if s.startswith(('4', '8')): return 'BJ'
            return ''
        df['code'] = df['code'].apply(lambda c: f"{market_prefix(c)}{c}")
    except Exception as e:
        print(f"  [警告] enrich_names 失败: {e}")
        conn.close()
        df['code'] = df['security_id'].astype(str)
        df['name'] = df['security_id'].astype(str)
    return df


# ══════════════════════════════════════════════════════════════
# 持仓自适应：根据市场状态决定持仓天数
# ══════════════════════════════════════════════════════════════

BULL_HOLDING = 8
BEAR_HOLDING = 3


def get_adaptive_holding_days(conn, date_str):
    """返回指定日期应该持仓几天（牛市8天/熊市3天）"""
    date_obj = datetime.date(int(date_str[:4]), int(date_str[4:6]), int(date_str[7:8]))
    regimes = get_market_regime(conn, '2020-01-01', date_str)
    regime = regimes.get(date_obj, 'bull')
    return BULL_HOLDING if regime == 'bull' else BEAR_HOLDING, regime


# ══════════════════════════════════════════════════════════════
# 动态止盈检测
# ══════════════════════════════════════════════════════════════

def check_profit_target(row, conn):
    """检测持仓股票是否触发动态止盈（持仓期间涨幅>6%）"""
    if row.get('profit_pct', 0) >= 6.0:
        return True
    return False


# ══════════════════════════════════════════════════════════════
# 打印最终推荐（傻瓜友好输出）
# ══════════════════════════════════════════════════════════════

def print_picks(df, date_str, profile, market_status, holding_days):
    """打印最终的傻瓜友好选股结果"""
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print(f"║         每日选股结果  [{date_str}]                          ║")
    print("╠══════════════════════════════════════════════════════════════╣")

    # 市场状态
    emoji = "🐂 牛市" if market_status == 'bull' else "🐻 熊市"
    print(f"║  沪深300状态：{emoji:<42}║")
    print(f"║  当前策略：{profile['label']:<44}║")
    print(f"║  持仓天数：{'自适应（牛市8天/熊市3天）' if profile['adaptive_holding'] else f'{holding_days}天（固定）':<36}║")
    if profile['profit_target'] > 0:
        print(f"║  动态止盈：涨超{profile['profit_target']}%自动止盈{' '*39}║")
    print("╠══════════════════════════════════════════════════════════════╣")

    if df is None or df.empty:
        print("║                                                              ║")
        print("║       🎯 今天没有符合条件的股票！                          ║")
        print("║       原因：可能是熊市市场过滤、信号稀少或条件过严           ║")
        print("║       建议：等待下一个交易日，或尝试切到更宽松的参数         ║")
        print("║                                                              ║")
        print("╚══════════════════════════════════════════════════════════════╝")
        return

    # 【与回测 100% 一致】按 entry_date 分组,每天取 TOP MAX_POSITIONS
    # 模拟回测 line 144-146 排序 + line 269 iterrows 取前 MAX_POSITIONS
    df_sorted = df.sort_values(['entry_date', 'score'], ascending=[True, False]).reset_index(drop=True)
    
    # 收集所有 entry_date 的 TOP MAX_POSITIONS
    picks = []
    for ed in df_sorted['entry_date'].unique():
        day_signals = df_sorted[df_sorted['entry_date'] == ed]
        for _, r in day_signals.head(MAX_POSITIONS).iterrows():
            picks.append(r)
    
    if not picks:
        print("║                                                              ║")
        print("║       🎯 今天没有符合条件的股票！                          ║")
        print("╚══════════════════════════════════════════════════════════════╝")
        return
    
    top = pd.DataFrame(picks)
    print(f"║  📋 建议持仓（按 entry_date 分组取 TOP{MAX_POSITIONS}，与回测一致）：{' '*15}║")
    print("║                                                              ║")
    for i, (_, r) in enumerate(top.iterrows(), 1):
        code = r['code']
        name = str(r['name'])[:6]
        strat = r['strategy']
        reason = r.get('reason', '')[:25]
        entry_date_str = r['entry_date'].strftime('%m-%d')

        # 格式化原因，截断
        print(f"║    {i}. {code} {name:<6} [{strat:<10}]  入场:{entry_date_str}  {' '*10}║")
        print(f"║       信号：{reason:<47}║")

        # 告知明日操作
        print(f"║       📌 明日操作：以信号日收盘价附近价格买入，持有{holding_days}天     ║")

    print("║                                                              ║")
    print("╠══════════════════════════════════════════════════════════════╣")
    print("║  ⚠️  操作提醒：                                              ║")
    print("║     1. 确认明日开盘前账户有足够资金                            ║")
    print("║     2. 候选股中若有开盘涨停/跌停 → 跳过，顺位选下一只           ║")
    print("║     3. 持仓期间跌幅≥3% → 无条件止损                            ║")
    print("║     4. 持仓期间涨超6%（若开启止盈）→ 次日开盘卖出              ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    # 如果有更多候选，打印出来备用
    if len(df) > MAX_POSITIONS:
        print(f"\n📌 备选候选（共 {len(df)} 只，TOP10）：")
        print(f"  {'#':>3}  {'代码':<12} {'名称':<8} {'策略':<10} {'信号强度'}")
        print(f"  {'-'*50}")
        for i, (_, r) in enumerate(df.head(10).iterrows(), 1):
            score = r['raw_score']
            print(f"  {i:3d}. {r['code']:<12} {str(r['name'])[:8]:<8} "
                  f"{r['strategy']:<10} {r.get('reason', '')}")


# ══════════════════════════════════════════════════════════════
# 手动模式：查看历史候选
# ══════════════════════════════════════════════════════════════

def show_history(date_obj):
    """查看历史某日的选股记录"""
    import pandas as pd
    f = PICKS_DIR / f"{date_obj.strftime('%Y%m%d')}.csv"
    if f.exists():
        df = pd.read_csv(f, encoding='utf-8-sig')
        print(f"\n📋 历史选股记录：{date_obj}（共 {len(df)} 只）")
        print(f"  {'#':>3}  {'代码':<12} {'名称':<8} {'策略':<10} {'当时说明'}")
        print(f"  {'-'*60}")
        for i, (_, r) in enumerate(df.iterrows(), 1):
            print(f"  {i:3d}. {r['code']:<12} {str(r.get('name',''))[:8]:<8} "
                  f"{r['strategy']:<10} {r.get('reason', '')}")
    else:
        print(f"\n  未找到 {date_obj} 的选股记录")


# ══════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════

def main():
    import duckdb
    import argparse

    # ── 解析参数 ──
    parser = argparse.ArgumentParser(description='每日选股（傻瓜版）')
    parser.add_argument('--date', '-d', type=str, default='',
                        help='指定日期，如 2026-06-04（默认今天）')
    parser.add_argument('--list', '-l', action='store_true',
                        help='查看最近选股记录')
    parser.add_argument('--load', type=str, default='',
                        help='查看指定日期历史结果，如 --load 2026-06-03')
    parser.add_argument('--all', action='store_true',
                        help='显示所有候选（TOP10），默认只显示持仓建议TOP2')
    parser.add_argument('--config', type=str, default='',
                        help=f'切换策略参数覆盖CONFIG设置，可选: {list(PARAM_PROFILES.keys())}')
    parser.add_argument('--pct-range', type=str, default='',
                        help='百分位排名范围，格式 start:end，如 2025-06-04:2026-06-03（与回测一致用）')
    parser.add_argument('--pct-auto', action='store_true',
                        help='自动用近 1 年滑动窗口（end=今天, start=end-365天），等价于 --pct-range <today-365>:<today>')
    args = parser.parse_args()

    # ── 策略配置 ──
    config = args.config.upper() if args.config else CONFIG.upper()
    if config not in PARAM_PROFILES:
        print(f"❌ 未知配置：{config}，可选：{list(PARAM_PROFILES.keys())}")
        return
    profile = PARAM_PROFILES[config]

    # ── 打印头部信息 ──
    print()
    print("=" * 60)
    print(f"  🎯 每日选股（傻瓜版）  参数配置：{config} - {profile['label']}")
    print("=" * 60)
    print(f"  📌 {profile['note']}")
    print()

    # ── 历史查询模式 ──
    if args.list:
        files = sorted(f for f in PICKS_DIR.glob('*.csv')
                      if f.name != '_all_signals_cache.csv')
        if not files:
            print("还没有历史选股记录")
            return
        print(f"最近 {min(10, len(files))} 天选股摘要：")
        for f in sorted(files)[-10:]:
            try:
                df = pd.read_csv(f, encoding='utf-8-sig')
                top3 = ', '.join([f"{r['code']}({str(r.get('name',''))[:4]})"
                                  for _, r in df.head(3).iterrows()])
                print(f"  {f.stem}  {len(df):4d}只  {top3}")
            except Exception:
                print(f"  {f.stem}  (读取失败)")
        return

    if args.load:
        date_str = args.load.replace('-', '')
        date_obj = datetime.date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
        show_history(date_obj)
        return

    # ── 日期处理 ──
    if args.date:
        date_str = args.date.replace('-', '').replace('/', '')
    else:
        trade_date = datetime.date.today()
        if trade_date.weekday() == 5:
            trade_date -= datetime.timedelta(days=1)
        elif trade_date.weekday() == 6:
            trade_date -= datetime.timedelta(days=2)
        date_str = trade_date.strftime('%Y%m%d')

    date_formatted = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    # ── 获取市场状态 ──
    print(f"📊 正在查询 {date_formatted} 的市场状态和选股信号…")
    conn = duckdb.connect(DB_PATH)
    conn.execute("SET enable_progress_bar=false")

    market_status = 'unknown'
    holding_days = profile['holding_days']

    if profile['market_filter'] or profile['adaptive_holding']:
        regimes = get_market_regime(conn, '2020-01-01', date_formatted)
        market_status = regimes.get(datetime.date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8])), 'unknown')
        if profile['adaptive_holding']:
            holding_days = BULL_HOLDING if market_status == 'bull' else BEAR_HOLDING
        bull_days = sum(1 for v in regimes.values() if v == 'bull')
        bear_days = sum(1 for v in regimes.values() if v == 'bear')
        emoji = "🐂" if market_status == 'bull' else "🐻"
        print(f"  沪深300状态：{emoji} {market_status}（历史: 牛市{bull_days}天 / 熊市{bear_days}天）")
        if market_status == 'bear' and profile['market_filter']:
            print(f"  ⚠️  当前熊市，市场过滤开启，不会开新仓")
            print("  💡 提示：将 CONFIG 切换为 'B' 或 'A4' 可绕过市场过滤")
            print("  📋 以下是候选股参考（熊市期间不会自动建仓）：")

    conn.close()

    # 解析 pct_range（可选,默认 None 用当天样本）
    pct_range = None
    if args.pct_auto:
        # 自动算近 1 年滑动窗口:end=今天(或 date_formatted), start=end-365天
        end_dt = datetime.datetime.strptime(date_formatted, '%Y-%m-%d')
        start_dt = end_dt - datetime.timedelta(days=365)
        pct_range = (start_dt.strftime('%Y-%m-%d'), end_dt.strftime('%Y-%m-%d'))
        print(f"  📌 --pct-auto: 自动近 1 年窗口 = {pct_range}")
    elif args.pct_range:
        try:
            pct_start, pct_end = args.pct_range.split(':')
            pct_range = (pct_start.strip(), pct_end.strip())
            print(f"  📌 --pct-range: {pct_range}")
        except Exception as e:
            print(f"  [警告] --pct-range 格式错误: {args.pct_range}，应如 2025-06-04:2026-06-03")
            return

    # ── 运行选股 ──
    df = screen_candidates(date_formatted, profile, pct_range=pct_range)

    # ── 打印结果 ──
    print_picks(df, date_formatted, profile, market_status, holding_days)

    # ── 保存 ──
    if df is not None and not df.empty:
        out_file = PICKS_DIR / f"{date_str}.csv"
        PICKS_DIR.mkdir(parents=True, exist_ok=True)
        cols = ['strategy', 'code', 'name', 'raw_score', 'reason', 'day_idx']
        present = [c for c in cols if c in df.columns]
        df[present].to_csv(out_file, index=False, encoding='utf-8-sig')
        print(f"\n💾 已保存: {out_file}")

    print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] 完成！")


if __name__ == '__main__':
    main()
