"""
多策略组合回测 - 优化版（支持5个优化方向）
用法:
  python scripts/combo_verify_optimized.py --start 2020-01-01 --end 2026-06-03
  python scripts/combo_verify_optimized.py --market-filter --ma-cross-strict
  python scripts/combo_verify_optimized.py --profit-target 6.0
  python scripts/combo_verify_optimized.py --confidence-threshold 0.85
"""
import duckdb
import pandas as pd
import numpy as np
import argparse
import datetime
import os
import sys

# 引入共享信号查询模块（与 daily_picker_simple.py 共用同一份 SQL）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signal_queries import (
    build_ma_cross_query, build_macd_query, build_vol_surge_query,
    get_market_regime as _get_market_regime_shared
)

DB_PATH = 'data/quant.duckdb'

# ── 参数解析 ──
parser = argparse.ArgumentParser()
parser.add_argument('--start',   default='2020-01-01', help='回测开始日期')
parser.add_argument('--end',     default='2026-06-03', help='回测结束日期')
parser.add_argument('--holding-days', type=int, default=5,  help='持仓天数（牛市默认）')
parser.add_argument('--ma-slow', type=int, default=60,     help='MA慢线周期')
parser.add_argument('--stop-pct', type=float, default=0.97,  help='止损线（默认0.97=-3%%）')
parser.add_argument('--output-suffix', default='',          help='输出文件后缀')

# ── 优化开关 ──
parser.add_argument('--market-filter',    action='store_true', help='优化①：市场环境过滤（指数MA20方向）')
parser.add_argument('--index-code',       default='SH000300',  help='过滤用指数代码（默认沪深300）')
parser.add_argument('--ma-cross-strict',  action='store_true', help='优化②：MA_CROSS信号加固（涨幅>0 + 量比>2x）')
parser.add_argument('--profit-target',    type=float, default=0, help='优化③：动态止盈阈值（%%），0=关闭')
parser.add_argument('--adaptive-holding', action='store_true', help='优化④：持仓周期自适应（牛8天/熊3天）')
parser.add_argument('--confidence-threshold', type=float, default=0, help='优化⑤：置信度过滤（pct_rank阈值），0=关闭')
parser.add_argument('--market-hs300',     action='store_true', help='用沪深300判断市场（默认创业板）')

args = parser.parse_args()

INITIAL_CAPITAL = 100000.0
TEST_START      = args.start
TEST_END        = args.end
HOLDING_DAYS    = args.holding_days
MA_SLOW         = args.ma_slow
STOP_PCT        = args.stop_pct
SUFFIX          = args.output_suffix

COST_PCT   = 0.56
MAX_POSITIONS = 2
BULL_HOLDING  = 8   # 优化④：牛市持仓
BEAR_HOLDING  = 3   # 优化④：熊市持仓

conn = duckdb.connect(DB_PATH)
conn.execute("SET enable_progress_bar=false")

# 加载股票代码/名称映射（用于交割单）
print("加载股票代码/名称映射…", flush=True)
sec_info = conn.execute(
    "SELECT security_id, code, name FROM security_master"
).fetchall()
SEC_MAP = {sid: (code, name) for sid, code, name in sec_info}
print(f"  {len(SEC_MAP):,} 只股票\n")

print(f"参数: MA_SLOW={MA_SLOW} HOLDING={HOLDING_DAYS} STOP={STOP_PCT}")
print(f"区间: {TEST_START} ~ {TEST_END}")
opt_list = []
if args.market_filter:    opt_list.append('市场过滤')
if args.ma_cross_strict:  opt_list.append('MA_CROSS加固')
if args.profit_target > 0: opt_list.append(f'动态止盈>{args.profit_target}%')
if args.adaptive_holding: opt_list.append('持仓自适应')
if args.confidence_threshold > 0: opt_list.append(f'置信度>{args.confidence_threshold}')
print(f"优化: {', '.join(opt_list) if opt_list else '无'}")
print(f"{'='*60}\n")

# ── 市场环境检测（优化①）── 用hs300_bar表（共享 SQL）──
def get_market_regime(conn, start, end):
    """返回每天的市场方向：'bull'/'bear'，基于沪深300 MA20方向（共享 signal_queries）"""
    return _get_market_regime_shared(conn, start, end)

market_regime = None
if args.market_filter or args.adaptive_holding:
    print("加载市场环境（沪深300 MA20）…")
    market_regime = get_market_regime(conn, TEST_START, TEST_END)
    regimes = set(market_regime.values())
    bull_days = sum(1 for v in market_regime.values() if v=='bull')
    bear_days = sum(1 for v in market_regime.values() if v=='bear')
    print(f"  沪深300: bull={bull_days}天, bear={bear_days}天")

# ── 动态SQL：MA慢线窗口 ──
ma_rows = MA_SLOW - 1

# ── MA_CROSS（支持严格模式） ──
# 使用共享 SQL（来自 signal_queries.py）
q_ma = build_ma_cross_query(TEST_START, TEST_END,
                             strict=args.ma_cross_strict,
                             holding_days=HOLDING_DAYS)
ma_raw = conn.execute(q_ma).fetchdf()
ma_raw['strategy'] = 'MA_CROSS'
print(f"MA_CROSS: {len(ma_raw)}笔")

# ── MACD ──
# 使用共享 SQL（来自 signal_queries.py）
q_macd = build_macd_query(TEST_START, TEST_END, holding_days=HOLDING_DAYS)
macd_raw = conn.execute(q_macd).fetchdf()
macd_raw['strategy'] = 'MACD'
print(f"MACD: {len(macd_raw)}笔")

# ── VOL_SURGE ──
# 使用共享 SQL（来自 signal_queries.py）
q_vol = build_vol_surge_query(TEST_START, TEST_END, holding_days=HOLDING_DAYS)
vol_raw = conn.execute(q_vol).fetchdf()
vol_raw['strategy'] = 'VOL_SURGE'
print(f"VOL_SURGE: {len(vol_raw)}笔")

conn.close()

# ── 合并+百分位化 ──
all_trades = pd.concat([ma_raw, macd_raw, vol_raw], ignore_index=True)
all_trades['signal_date'] = pd.to_datetime(all_trades['signal_date'])
all_trades['entry_date']  = pd.to_datetime(all_trades['entry_date'])
all_trades['exit_date']   = pd.to_datetime(all_trades['exit_date'])
all_trades['raw_score']   = all_trades['raw_score'].astype(float)

# 百分位化（各策略内排序）
for strat in ['MA_CROSS', 'MACD', 'VOL_SURGE']:
    mask = all_trades['strategy'] == strat
    scores = all_trades.loc[mask, 'raw_score']
    if len(scores) > 0:
        all_trades.loc[mask, 'score'] = scores.rank(pct=True, ascending=True)

# ── 优化⑤：置信度过滤 ──
if args.confidence_threshold > 0:
    before = len(all_trades)
    all_trades = all_trades[all_trades['score'] >= args.confidence_threshold]
    print(f"置信度过滤: {before} -> {len(all_trades)}笔 (threshold={args.confidence_threshold})")

# 去重（同一信号日同一股票只留最高分）
all_trades = all_trades.sort_values('score', ascending=False)
all_trades = all_trades.drop_duplicates(subset=['signal_date', 'security_id'], keep='first')
all_trades = all_trades.sort_values(['entry_date', 'score'], ascending=[True, False]).reset_index(drop=True)
print(f"合并去重后: {len(all_trades)}笔\n")

# ── 优化①：市场环境过滤（信号日当天指数MA20向下→跳过） ──
if args.market_filter and market_regime:
    before = len(all_trades)
    all_trades = all_trades[all_trades['signal_date'].map(lambda d: market_regime.get(d, 'bull') == 'bull')]
    print(f"市场过滤: {before} -> {len(all_trades)}笔 (剔除熊市信号)")

# ── 加载日K（用于动态止盈和持仓自适应） ──
conn2 = duckdb.connect(DB_PATH)
conn2.execute("SET enable_progress_bar=false")
all_bars_df = conn2.execute(f"""
    SELECT security_id, trade_date,
        ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY trade_date) as day_idx,
        open_price, close_price, low_price, change_pct
    FROM daily_bar
    WHERE trade_date >= ? AND trade_date <= ?
""", [TEST_START, TEST_END]).fetchdf()
conn2.close()
all_bars_df['trade_date'] = pd.to_datetime(all_bars_df['trade_date'])

# ── 模拟资金曲线 ──
capital = INITIAL_CAPITAL
active_positions = {}
capital_curve = []
stats = []

# 信号→exit_idx映射（基础值，实际持仓天数会根据market regime调整）
all_trades['base_exit_idx'] = all_trades['sig_idx'] + HOLDING_DAYS + 1

unique_days = sorted(all_trades['entry_date'].unique())

for day in unique_days:
    # 优化④：计算持仓天数（仅在adaptive_holding开启时生效）
    current_regime = market_regime.get(day, 'bull') if market_regime else 'bull'
    today_holding = HOLDING_DAYS
    if args.adaptive_holding and market_regime is not None:
        today_holding = BULL_HOLDING if current_regime == 'bull' else BEAR_HOLDING

    # ── 持仓到期/止损结算 ──
    expired_secs = [s for s, info in active_positions.items()
                    if info['exit_date'] <= day]
    for sec in expired_secs:
        info = active_positions[sec]
        entry      = info['entry_price']
        exit_date  = info['exit_date']   # 存的就是自适应后的真实exit_date

        # 用 trade_date 直接查，不用 day_idx（A4关键修复）
        exit_bar = all_bars_df[(all_bars_df['security_id'] == sec) &
                               (all_bars_df['trade_date'] == exit_date)]
        if len(exit_bar) == 0:
            # fallback：沿用 all_trades 里的固定5天价格
            trade_row = all_trades[(all_trades['security_id'] == sec) &
                                   (all_trades['entry_date'] == info['entry_date'])].iloc[0]
            exit_price = float(trade_row['exit_price'])
            low        = float(trade_row.get('exit_low', 0))
        else:
            exit_price = float(exit_bar.iloc[0]['close_price'])
            low        = float(exit_bar.iloc[0]['low_price'])

        # 固定止损检测
        if low > 0 and low <= entry * STOP_PCT:
            exit_price = entry * STOP_PCT

        pnl_pct   = (exit_price - entry) / entry * 100 - COST_PCT
        pnl_money = info['shares'] * pnl_pct / 100 * entry
        capital  += pnl_money
        code, name = SEC_MAP.get(sec, (str(sec), str(sec)))
        stats.append({
            'security_id': sec, 'code': code, 'name': name,
            'entry_date': info['entry_date'], 'exit_date': info['exit_date'],
            'strategy': info['strategy'],
            'pnl_pct': pnl_pct, 'capital_after': capital,
            'stopped': info.get('stopped', False),
            'exit_reason': info.get('exit_reason', 'hold')
        })
        del active_positions[sec]

    # ── 动态止盈检查（优化③） ──
    if args.profit_target > 0 and active_positions:
        for sec, info in list(active_positions.items()):
            entry_date = info['entry_date']
            sig_idx    = info['sig_idx']
            entry      = info['entry_price']

            # 找持仓期间的K线
            bars = all_bars_df[(all_bars_df['security_id'] == sec) &
                               (all_bars_df['day_idx'] > sig_idx) &
                               (all_bars_df['trade_date'] <= day)]

            for _, bar in bars.iterrows():
                bar_close = float(bar['close_price'])
                bar_date   = bar['trade_date']
                gain_pct   = (bar_close - entry) / entry * 100

                if gain_pct >= args.profit_target:
                    # 次日开盘价止盈
                    next_bar = all_bars_df[(all_bars_df['security_id'] == sec) &
                                          (all_bars_df['trade_date'] > bar_date)]
                    if len(next_bar) > 0:
                        exit_px = float(next_bar.iloc[0]['open_price'])
                        pnl_pct = (exit_px - entry) / entry * 100 - COST_PCT
                        capital += info['shares'] * pnl_pct / 100 * entry
                        code, name = SEC_MAP.get(sec, (str(sec), str(sec)))
                        stats.append({
                            'security_id': sec, 'code': code, 'name': name,
                            'entry_date': entry_date, 'exit_date': bar_date,
                            'strategy': info['strategy'],
                            'pnl_pct': pnl_pct, 'capital_after': capital,
                            'stopped': False, 'exit_reason': f'profit_target_{args.profit_target}'
                        })
                        del active_positions[sec]
                        break

    # ── 新开仓 ──
    current_count   = len(active_positions)
    if current_count < MAX_POSITIONS:
        available_slots = MAX_POSITIONS - current_count
        day_signals     = all_trades[all_trades['entry_date'] == day].copy()
        day_signals     = day_signals[~day_signals['security_id'].isin(active_positions.keys())]

        slots_used = 0
        for _, row in day_signals.iterrows():
            if slots_used >= available_slots:
                break
            sec        = row['security_id']
            entry_price = float(row['entry_price'])
            if entry_price <= 0:
                continue

            # 优化④：自适应持仓天数
            pos_holding = BULL_HOLDING if current_regime == 'bull' else BEAR_HOLDING if args.adaptive_holding else HOLDING_DAYS
            exit_idx    = int(row['sig_idx']) + pos_holding + 1
            exit_row    = all_bars_df[(all_bars_df['security_id'] == sec) &
                                      (all_bars_df['day_idx'] == exit_idx)]
            if len(exit_row) == 0:
                continue
            exit_date   = exit_row.iloc[0]['trade_date']

            per_pos_cap = capital / MAX_POSITIONS
            shares      = int(per_pos_cap / entry_price)
            if shares <= 0:
                continue

            active_positions[sec] = {
                'exit_date':   exit_date,
                'exit_idx':    exit_idx,
                'sig_idx':     int(row['sig_idx']),
                'shares':      shares,
                'entry_price': entry_price,
                'entry_date':  day,
                'strategy':    row['strategy'],
                'stopped':     False,
                'exit_reason': 'hold'
            }
            slots_used += 1

    capital_curve.append({'date': day, 'capital': capital, 'positions': len(active_positions)})

# ── 剩余持仓结算 ──
if active_positions:
    for sec, info in list(active_positions.items()):
        entry = info['entry_price']
        exit_date = info['exit_date']

        # 用 trade_date 直接查
        exit_bar = all_bars_df[(all_bars_df['security_id'] == sec) &
                               (all_bars_df['trade_date'] == exit_date)]
        if len(exit_bar) == 0:
            trade_row = all_trades[(all_trades['security_id'] == sec) &
                                   (all_trades['entry_date'] == info['entry_date'])].iloc[0]
            exit_price = float(trade_row['exit_price'])
            low        = float(trade_row.get('exit_low', 0))
        else:
            exit_price = float(exit_bar.iloc[0]['close_price'])
            low        = float(exit_bar.iloc[0]['low_price'])

        if low > 0 and low <= entry * STOP_PCT:
            exit_price = entry * STOP_PCT

        pnl_pct   = (exit_price - entry) / entry * 100 - COST_PCT
        capital  += info['shares'] * pnl_pct / 100 * entry
        code, name = SEC_MAP.get(sec, (str(sec), str(sec)))
        stats.append({
            'security_id': sec, 'code': code, 'name': name,
            'entry_date': info['entry_date'], 'exit_date': info['exit_date'],
            'strategy': info['strategy'],
            'pnl_pct': pnl_pct, 'capital_after': capital,
            'stopped': info.get('stopped', False),
            'exit_reason': 'final'
        })

# ── 结果输出 ──
df_stats  = pd.DataFrame(stats)
df_curve  = pd.DataFrame(capital_curve)

print(f"\n{'='*60}")
print(f"  最终资金: {capital:,.0f} 元")
print(f"  初始资金: {INITIAL_CAPITAL:,.0f} 元")
print(f"  总收益: {(capital/INITIAL_CAPITAL-1)*100:.1f}%")

years = (pd.Timestamp(TEST_END) - pd.Timestamp(TEST_START)).days / 365.25
ann_return = ((capital / INITIAL_CAPITAL) ** (1 / years) - 1) * 100 if years > 0 else 0
print(f"  投资时长: {years:.1f} 年")
print(f"  年化复利: {ann_return:.1f}%")

df_curve['peak'] = df_curve['capital'].cummax()
df_curve['drawdown'] = (df_curve['capital'] / df_curve['peak'] - 1) * 100
max_dd = df_curve['drawdown'].min()
max_dd_date = df_curve.loc[df_curve['drawdown'].idxmin(), 'date']
print(f"  最大回撤: {max_dd:.1f}% (发生在 {str(max_dd_date)[:10]})")

n_trades = len(df_stats)
wr = pl = avg_pct = 0.0
if n_trades > 0:
    wins     = df_stats[df_stats['pnl_pct'] > 0]
    wr       = len(wins) / n_trades * 100
    avg_win  = wins['pnl_pct'].mean() if len(wins) > 0 else 0
    avg_loss = df_stats[df_stats['pnl_pct'] <= 0]['pnl_pct'].mean()
    pl       = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    avg_pct  = df_stats['pnl_pct'].mean()
    print(f"\n  总交易笔数: {n_trades}")
    print(f"  胜率: {wr:.1f}%")
    print(f"  盈亏比: {pl:.2f}")
    print(f"  平均期望: {avg_pct:.3f}%/笔")
    stopped_count = (df_stats['stopped'] == True).sum() if 'stopped' in df_stats.columns else 0
    print(f"  止损率: {stopped_count/n_trades*100:.1f}%")
    print(f"\n  各策略贡献:")
    for strat in ['MA_CROSS', 'MACD', 'VOL_SURGE']:
        sub = df_stats[df_stats['strategy'] == strat]
        if len(sub) > 0:
            sub_wr = len(sub[sub['pnl_pct'] > 0]) / len(sub) * 100
            print(f"    {strat}: {len(sub)}笔, 胜率{sub_wr:.0f}%, 期望{sub['pnl_pct'].mean():.3f}%")

# ── 保存结果 ──
OUT_DIR = 'data/param_results'
os.makedirs(OUT_DIR, exist_ok=True)
suffix = f"_{SUFFIX}" if SUFFIX else ""

trades_file = f"{OUT_DIR}/trades{suffix}.csv"
df_stats.to_csv(trades_file, index=False)

curve_file = f"{OUT_DIR}/capital_curve{suffix}.csv"
df_curve.to_csv(curve_file, index=False)

params = {
    'TEST_START': TEST_START, 'TEST_END': TEST_END,
    'MA_SLOW': MA_SLOW, 'HOLDING_DAYS': HOLDING_DAYS,
    'STOP_PCT': STOP_PCT,
    'market_filter': args.market_filter,
    'ma_cross_strict': args.ma_cross_strict,
    'profit_target': args.profit_target,
    'adaptive_holding': args.adaptive_holding,
    'confidence_threshold': args.confidence_threshold,
    'INITIAL_CAPITAL': INITIAL_CAPITAL, 'FINAL_CAPITAL': capital,
    'TOTAL_RETURN': f"{(capital/INITIAL_CAPITAL-1)*100:.1f}%",
    'ANN_RETURN': f"{ann_return:.1f}%",
    'MAX_DRAWDOWN': f"{max_dd:.1f}%",
    'N_TRADES': n_trades,
    'WIN_RATE': f"{wr:.1f}%",
    'PL_RATIO': f"{pl:.2f}",
    'AVG_PCT': f"{avg_pct:.3f}%",
}
params_file = f"{OUT_DIR}/params{suffix}.txt"
with open(params_file, 'w') as f:
    for k, v in params.items():
        f.write(f"{k} = {v}\n")

print(f"\n交割单 -> {trades_file}")
print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 完成!")
