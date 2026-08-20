"""
一键回测（自验证版）
====================
用法（记住这5个命令就够了）：

  cd D:/Files/workspace-ai/quant

  # 1. 跑完整 48 组（6时间段 × 8策略），约 30~50 分钟
  python scripts/run_backtest.py

  # 2. 快速验证：只跑 A4 + 近1年（看现在最优参数）
  python scripts/run_backtest.py --single A4 --period 近1年

  # 3. 跑完整 48 组并导出 CSV
  python scripts/run_backtest.py --export

  # 4. 自定义日期范围（任意组合）
  python scripts/run_backtest.py --single A4 --start 2024-01-01 --end 2025-12-31

  # 5. 自定义范围 + 全部策略 + 导出
  python scripts/run_backtest.py --start 2024-06-01 --end 2025-06-01 --export

参数说明:
  --single    只跑某个策略（B, A1, A2, A3, A4, A5, A1+2, A1+2+3）
  --period    只跑某个时间段（近1年, 近2年, 熊市, 牛市, 熊市反弹, 疫情爆发）
  --start     自定义开始日期（YYYY-MM-DD）
  --end       自定义结束日期（YYYY-MM-DD）
  --export    导出汇总CSV到 data/param_results/
  --timeout   单组超时秒数（默认900）

输出文件:
  data/param_results/trades_<测试>_<时间段>.csv  ← 交割单（每笔交易详情）
  data/param_results/capital_curve_<测试>_<时间段>.csv  ← 每日权益曲线
  data/param_results/backtest_<时间戳>.json  ← 汇总指标（始终生成）
  data/param_results/backtest_<时间戳>.csv  ← 汇总指标（仅 --export 时生成）
"""

import sys
import os
import argparse
import subprocess
import json
import datetime
from pathlib import Path

SCRIPT_DIR    = Path(__file__).parent
OUT_DIR       = SCRIPT_DIR.parent / "data" / "param_results"
OUT_DIR.mkdir(parents=True, exist_ok=True)
COMBO_SCRIPT  = SCRIPT_DIR / "combo_verify_optimized.py"

# 预设时间段
PERIODS = [
    ('疫情爆发',  '2020-01-01', '2026-06-03'),
    ('熊市',      '2021-01-01', '2026-06-03'),
    ('牛市',      '2022-10-31', '2026-06-03'),
    ('熊市反弹',  '2023-01-01', '2026-06-03'),
    ('近2年',     '2024-01-01', '2026-06-03'),
    ('近1年',     '2025-01-01', '2026-06-03'),
]

# 8个测试配置
TESTS = [
    ('B',         '',                                                                  '基准'),
    ('A1',        '--market-filter',                                                   '市场过滤'),
    ('A2',        '--ma-cross-strict',                                                 'MA加固'),
    ('A3',        '--profit-target 6.0',                                               '动态止盈'),
    ('A4',        '--adaptive-holding',                                                '持仓自适应'),
    ('A5',        '--confidence-threshold 0.85',                                       '置信度>0.85'),
    ('A1+2',      '--market-filter --ma-cross-strict',                                 '市场+MA'),
    ('A1+2+3',    '--market-filter --ma-cross-strict --profit-target 6.0',             '全组合'),
]

FIXED_ARGS = '--ma-slow 60 --holding-days 5 --stop-pct 0.97'


def run_one(test_id, test_name, period_name, start, end, extra_args, timeout=900):
    """调 combo_verify_optimized.py 跑一组回测"""
    tag = f"[{test_id}][{period_name}]"
    print(f"{tag} {start} ~ {end} 开始…", flush=True)

    # 用稳定的 suffix（不包含 timestamp，方便对比多次跑的结果）
    suffix = f"{test_id}_{period_name}"

    cmd = [
        sys.executable, '-u', str(COMBO_SCRIPT),
        '--start', start, '--end', end,
        '--output-suffix', suffix,
    ] + FIXED_ARGS.split() + (extra_args.split() if extra_args else [])

    t0 = datetime.datetime.now()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        elapsed = (datetime.datetime.now() - t0).total_seconds()
        output = result.stdout + result.stderr
        success = result.returncode == 0

        def extract(key):
            for line in output.split('\n'):
                if key in line:
                    for sep in ['：', ':']:
                        parts = line.split(sep)
                        if len(parts) >= 2:
                            val = parts[-1].strip().replace('%', '').replace(',', '').replace('元', '').strip()
                            try:
                                return float(val.split()[0])
                            except:
                                pass
            return None

        return {
            'test_id':     test_id,
            'test_name':   test_name,
            'period':      period_name,
            'start':       start,
            'end':         end,
            'ann_return':  extract('年化复利'),
            'max_drawdown': extract('最大回撤'),
            'n_trades':    int(extract('总交易笔数') or 0),
            'win_rate':    extract('胜率'),
            'pl_ratio':    extract('盈亏比'),
            'avg_pct':     extract('平均期望'),
            'elapsed_s':   round(elapsed, 1),
            'success':     success,
        }
    except subprocess.TimeoutExpired:
        return {
            'test_id': test_id, 'test_name': test_name, 'period': period_name,
            'start': start, 'end': end,
            'ann_return': None, 'max_drawdown': None, 'n_trades': 0,
            'win_rate': None, 'pl_ratio': None, 'avg_pct': None,
            'elapsed_s': timeout, 'success': False, 'error': 'TIMEOUT',
        }


def fmt(v, suffix='%'):
    if v is None: return '  - '
    return f"{v:.1f}{suffix}" if isinstance(v, (int, float)) else '  - '


def print_table(rows, period_names):
    """打印横向汇总表"""
    if not rows:
        return
    col_w = 15

    print()
    print("=" * 80)
    print("  完整回测汇总表（年化% / 最大回撤% / 胜率%）")
    print("=" * 80)

    header = "  " + "".join(f"{p[:12]:^{col_w}}" for p in period_names)
    print(header)
    subheader = "  " + "".join(f"{'年化/回撤/胜':^{col_w}}" for _ in period_names)
    print(subheader)
    print("  " + "─" * (col_w * len(period_names)))

    for tid, _, _ in TESTS:
        row_str = f"  {tid:<{col_w-2}}"
        for pname in period_names:
            matched = [r for r in rows if r['test_id'] == tid and r['period'] == pname]
            if matched:
                r = matched[0]
                cell = f"{fmt(r['ann_return'])}/{fmt(r['max_drawdown'])}/{fmt(r['win_rate'])}"
            else:
                cell = "N/A"
            row_str += f"{cell:^{col_w}}"
        print(row_str)
    print()
    print("  说明：每个单元格格式为「年化%/回撤%/胜率%」")
    print("  ★ 当前推荐：A4（持仓自适应）→ 编辑 daily_picker_simple.py 顶部的 CONFIG = 'A4'")
    print()
    print(f"  交割单: data/param_results/trades_<测试>_<段>.csv")
    print(f"  权益曲线: data/param_results/capital_curve_<测试>_<段>.csv")


def parse_period_arg(s):
    """支持 友好名称（近1年/熊市）和模糊匹配"""
    kw_map = {
        '近1年': '近1年', '近2年': '近2年',
        '疫情': '疫情爆发', '熊市': '熊市',
        '牛市': '牛市', '反弹': '熊市反弹',
        '全部': None,
    }
    if s in kw_map:
        return kw_map[s]
    return s  # 模糊匹配


def main():
    parser = argparse.ArgumentParser(
        description='一键回测（基于已验证的 combo_verify_optimized.py）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--single', type=str, nargs='+', default=[],
                        help='只跑指定策略（可多个），如 --single A4 A3 B')
    parser.add_argument('--period', type=str, default='',
                        help='只跑某个时间段（近1年, 近2年, 熊市, 牛市, 熊市反弹, 疫情爆发）')
    parser.add_argument('--start', type=str, default='',
                        help='自定义开始日期（YYYY-MM-DD），与 --period 互斥，优先级更高')
    parser.add_argument('--end', type=str, default='',
                        help='自定义结束日期（YYYY-MM-DD）')
    parser.add_argument('--export', action='store_true',
                        help='保存汇总CSV到 data/param_results/')
    parser.add_argument('--timeout', type=int, default=900,
                        help='单组超时秒数（默认900=15分钟）')
    args = parser.parse_args()

    # ── 1. 解析时间范围 ──
    if args.start or args.end:
        # 自定义日期模式
        if not args.start or not args.end:
            print("❌ 自定义日期必须同时指定 --start 和 --end")
            return
        # 验证日期格式
        try:
            datetime.datetime.strptime(args.start, '%Y-%m-%d')
            datetime.datetime.strptime(args.end,   '%Y-%m-%d')
        except ValueError:
            print("❌ 日期格式错误，应为 YYYY-MM-DD")
            return
        if args.start >= args.end:
            print("❌ 开始日期必须早于结束日期")
            return

        # 自定义日期下用一段，名字叫"自定义"
        period_name = '自定义'
        selected_periods = [(period_name, args.start, args.end)]
        if args.period:
            print(f"⚠️  --period ({args.period}) 被忽略，因为已指定 --start/--end")
    elif args.period:
        kw = parse_period_arg(args.period)
        if kw is None:
            selected_periods = PERIODS
        else:
            matched = [p for p in PERIODS if p[0] == kw]
            if not matched:
                # 模糊匹配
                matched = [p for p in PERIODS if kw in p[0]]
            if not matched:
                print(f"❌ 未匹配到时间段: {args.period}")
                print(f"   可选: {[p[0] for p in PERIODS]}")
                return
            selected_periods = matched
    else:
        selected_periods = PERIODS

    # ── 2. 解析要跑的策略 ──
    if args.single:
        # 支持多个策略：--single A4 A3 B
        tids = [s.upper() for s in args.single]
        selected_tests = [t for t in TESTS if t[0] in tids]
        if not selected_tests:
            print(f"❌ 未知策略: {tids}")
            print(f"   可选: {[t[0] for t in TESTS]}")
            return
    else:
        selected_tests = TESTS

    # ── 3. 打印头部信息 ──
    total = len(selected_tests) * len(selected_periods)
    print()
    print("=" * 80)
    print(f"  一键回测  共 {total} 组（{len(selected_tests)}策略 × {len(selected_periods)}段）")
    print("=" * 80)
    print(f"  策略: {[t[0] for t in selected_tests]}")
    print(f"  段:   {[(p[0], p[1][:7], p[2][:7]) for p in selected_periods]}")
    print(f"  结果: data/param_results/")
    print()

    all_rows = []
    count = 0
    for period_name, start, end in selected_periods:
        print(f"\n━━━ 【{period_name}】{start} ~ {end} ━━━")
        for test_id, extra_args, test_name in selected_tests:
            count += 1
            row = run_one(test_id, test_name, period_name, start, end,
                         extra_args, timeout=args.timeout)
            row['run_idx'] = count
            all_rows.append(row)

            ann = fmt(row['ann_return'])
            dd  = fmt(row['max_drawdown'])
            wr  = fmt(row['win_rate'])
            pl  = f"{row['pl_ratio']:.2f}" if row['pl_ratio'] else '  -  '
            n   = row['n_trades']
            status = '✓' if row['success'] else '✗'
            print(f"  {test_id:<8} {status} 年化{ann:>8}  回撤{dd:>8}  胜率{wr:>7}  "
                  f"盈亏比{pl:>6}  {n:>3}笔  {row['elapsed_s']:.0f}s",
                  flush=True)

    # ── 4. 横向汇总表 ──
    period_names = [p[0] for p in selected_periods]
    print_table(all_rows, period_names)

    # ── 5. 保存 JSON ──
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    json_path = OUT_DIR / f"backtest_{ts}.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(all_rows, f, ensure_ascii=False, indent=2)
    print(f"\n💾 JSON: {json_path}")

    # ── 6. 保存 CSV（仅 --export）──
    if args.export:
        csv_path = OUT_DIR / f"backtest_{ts}.csv"
        import csv
        with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=[
                'test_id', 'test_name', 'period', 'start', 'end',
                'run_idx', 'ann_return', 'max_drawdown', 'n_trades',
                'win_rate', 'pl_ratio', 'avg_pct', 'elapsed_s', 'success',
            ])
            w.writeheader()
            for r in all_rows:
                w.writerow(r)
        print(f"💾 CSV:  {csv_path}")

    # ── 7. 列出交割单文件 ──
    print(f"\n📋 交割单（每组自动生成，可打开查看每笔交易）:")
    for period_name, start, end in selected_periods:
        for test_id, _, _ in selected_tests:
            trade_file = OUT_DIR / f"trades_{test_id}_{period_name}.csv"
            curve_file = OUT_DIR / f"capital_curve_{test_id}_{period_name}.csv"
            if trade_file.exists():
                # 文件行数
                with open(trade_file, 'r', encoding='utf-8-sig') as f:
                    n_lines = sum(1 for _ in f) - 1
                print(f"   • {trade_file.name}  ({n_lines}笔)  +  {curve_file.name}")
            else:
                print(f"   ✗ {trade_file.name}  未生成（可能该组没交易）")

    print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] 全部完成！共 {count} 组")


if __name__ == '__main__':
    main()
