"""
批量运行参数矩阵测试
测试组合：
  - 5个时间区间
  - 5个参数组合（基准 + 4个优化方向）

用法:
  python scripts/param_matrix_runner.py
"""
import subprocess
import time
import datetime
import json

# ── 测试矩阵 ──
INTERVALS = [
    ('2020-01-01', '2026-06-03', '全区间(2020~2026)'),
    ('2020-01-01', '2022-01-01', '疫情牛市(2020~2022)'),
    ('2022-01-01', '2024-01-01', '熊市(2022~2024)'),
    ('2024-01-01', '2026-01-01', '牛市(2024~2026)'),
    ('2025-01-01', '2026-06-03', '近期(2025~2026)'),
]

PARAM_COMBOS = [
    # 基准组：MA60 + 持仓5天 + 固定止损-3%
    dict(ma_slow=60, holding_days=5,  stop_pct=0.97, use_trailing=False, suffix='基准MA60_5天_固定止损'),
    # 测试组A：MA20 + 持仓5天 + 固定止损-3%
    dict(ma_slow=20, holding_days=5,  stop_pct=0.97, use_trailing=False, suffix='MA20_5天_固定止损'),
    # 测试组B：MA60 + 持仓8天 + 固定止损-3%
    dict(ma_slow=60, holding_days=8,  stop_pct=0.97, use_trailing=False, suffix='MA60_8天_固定止损'),
    # 测试组C：MA60 + 持仓5天 + 移动止损-2%
    dict(ma_slow=60, holding_days=5,  stop_pct=0.98, use_trailing=True,  suffix='MA60_5天_移动止损2%'),
    # 测试组D：MA20 + 持仓8天 + 移动止损-2%
    dict(ma_slow=20, holding_days=8,  stop_pct=0.98, use_trailing=True,  suffix='MA20_8天_移动止损2%'),
]

SCRIPT = 'scripts/combo_verify_full_realistic.py'
RESULTS_FILE = 'data/param_results/matrix_results.json'

def run_one(start, end, label, params):
    suffix = params['suffix']
    trailing_flag = '--use-trailing-stop' if params['use_trailing'] else ''
    cmd = [
        'python', SCRIPT,
        '--start', start, '--end', end,
        '--ma-slow', str(params['ma_slow']),
        '--holding-days', str(params['holding_days']),
        '--stop-pct', str(params['stop_pct']),
        '--output-suffix', suffix,
    ] + ([trailing_flag] if trailing_flag else [])
    cmd = [c for c in cmd if c]  # 过滤空字符串

    print(f"\n{'='*60}")
    print(f"  [{label}] {suffix}")
    print(f"  {' '.join(cmd)}")

    start_time = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, cwd='D:/Files/workspace-ai/quant')
    elapsed = time.time() - start_time

    if result.returncode != 0:
        print(f"  ❌ 失败 ({elapsed:.0f}s): {result.stderr[:200]}")
        return None

    # 解析输出
    lines = result.stdout.split('\n')
    summary = {'suffix': suffix, 'label': label, 'interval': f'{start}~{end}', 'elapsed_s': round(elapsed)}
    for line in lines:
        line = line.strip()
        if '最终资金:' in line:
            try:
                summary['final_capital'] = float(line.split('最终资金:')[1].split('元')[0].replace(',',''))
            except:
                pass
        elif '总收益:' in line:
            try:
                summary['total_return'] = line.split('总收益:')[1].strip()
            except:
                pass
        elif '年化复利:' in line:
            try:
                summary['ann_return'] = line.split('年化复利:')[1].split('%')[0].strip()
            except:
                pass
        elif '最大回撤:' in line:
            try:
                summary['max_drawdown'] = line.split('最大回撤:')[1].split('%')[0].strip()
            except:
                pass
        elif '胜率:' in line and '各策略' not in line and '止损率' not in line:
            try:
                summary['win_rate'] = line.split('胜率:')[1].split('%')[0].strip()
            except:
                pass
        elif '盈亏比:' in line:
            try:
                summary['pl_ratio'] = line.split('盈亏比:')[1].strip()
            except:
                pass
        elif '平均期望:' in line:
            try:
                summary['avg_pct'] = line.split('平均期望:')[1].split('%')[0].strip()
            except:
                pass
        elif '总交易笔数:' in line:
            try:
                summary['n_trades'] = int(line.split('总交易笔数:')[1].split('笔')[0].strip())
            except:
                pass
        elif '止损率:' in line:
            try:
                summary['stop_rate'] = line.split('止损率:')[1].split('%')[0].strip()
            except:
                pass

    print(f"  ✅ 完成 ({elapsed:.0f}s) | 资金:{summary.get('final_capital','?')} | 年化:{summary.get('ann_return','?')}% | 胜率:{summary.get('win_rate','?')}%")
    return summary

def main():
    results = []

    total = len(INTERVALS) * len(PARAM_COMBOS)
    print(f"开始参数矩阵测试: {len(INTERVALS)}个区间 × {len(PARAM_COMBOS)}个参数 = {total}次回测")
    print(f"开始时间: {datetime.datetime.now().strftime('%H:%M:%S')}")

    for i, (start, end, ilabel) in enumerate(INTERVALS):
        for j, params in enumerate(PARAM_COMBOS):
            idx = i * len(PARAM_COMBOS) + j + 1
            print(f"\n[{idx}/{total}] ", end='')
            r = run_one(start, end, ilabel, params)
            if r:
                results.append(r)
            # 每次跑完休息2秒
            time.sleep(2)

    # 保存结果
    with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # 打印汇总表
    print(f"\n\n{'='*80}")
    print(f"  参数矩阵测试汇总  ({datetime.datetime.now().strftime('%H:%M:%S')})")
    print(f"{'='*80}")
    print(f"{'区间':<22} {'参数组合':<24} {'年化%':>6} {'最大回撤':>8} {'胜率%':>6} {'盈亏比':>6} {'笔数':>5} {'止损率':>6}")
    print('-'*80)
    for r in results:
        print(f"{r.get('label',''):<22} {r.get('suffix',''):<24} "
              f"{r.get('ann_return','?'):>6} {r.get('max_drawdown','?'):>8} "
              f"{r.get('win_rate','?'):>6} {r.get('pl_ratio','?'):>6} "
              f"{r.get('n_trades','?'):>5} {r.get('stop_rate','?'):>6}")
    print(f"\n结果已保存: {RESULTS_FILE}")

if __name__ == '__main__':
    main()
