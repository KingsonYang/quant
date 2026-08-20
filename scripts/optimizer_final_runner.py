"""
48组回测批量Runner：8个测试 × 6个时间段
用法: python -u scripts/optimizer_final_runner.py 2>&1
"""
import subprocess
import json
import datetime
import time
import os
import sys

OUT_DIR = 'data/param_results'
os.makedirs(OUT_DIR, exist_ok=True)
TIMESTAMP = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
RESULTS_FILE = f'{OUT_DIR}/final_matrix_{TIMESTAMP}.json'
LOG_FILE = f'{OUT_DIR}/final_runner_{TIMESTAMP}.log'

# 6个时间段
PERIODS = [
    ('疫情爆发',       '2020-01-01', '2026-06-03'),
    ('熊市',           '2021-01-01', '2026-06-03'),
    ('牛市',           '2022-10-31', '2026-06-03'),
    ('熊市反弹',       '2023-01-01', '2026-06-03'),
    ('近2年',          '2024-01-01', '2026-06-03'),
    ('近1年',          '2025-01-01', '2026-06-03'),
]

# 8个测试配置
TESTS = [
    ('B',     '',                                         '基准'),
    ('A1',    '--market-filter',                          '市场过滤'),
    ('A2',    '--ma-cross-strict',                       'MA_CROSS加固'),
    ('A3',    '--profit-target 6.0',                     '动态止盈6%'),
    ('A4',    '--adaptive-holding',                       '持仓自适应'),
    ('A5',    '--confidence-threshold 0.85',             '置信度>0.85'),
    ('A1+2',  '--market-filter --ma-cross-strict',       '市场+MA加固'),
    ('A1+2+3','--market-filter --ma-cross-strict --profit-target 6.0', '全组合'),
]

# 固定参数
FIXED_ARGS = '--start {start} --end {end} --ma-slow 60 --holding-days 5 --stop-pct 0.97 --output-suffix {suffix}'

total = len(TESTS) * len(PERIODS)
print(f"总计 {total} 组回测 ({len(TESTS)} 测试 × {len(PERIODS)} 段)")
print(f"结果 -> {RESULTS_FILE}")
print(f"日志 -> {LOG_FILE}")
print('='*60)

def run_test(test_id, test_name, period_name, start, end, extra_args, suffix):
    tag = f"[{test_id}][{period_name}]"
    print(f"{tag} 开始…", flush=True)
    
    cmd = (f'python -u scripts/combo_verify_optimized.py '
           f'--start {start} --end {end} --ma-slow 60 '
           f'--holding-days 5 --stop-pct 0.97 '
           f'--output-suffix {suffix} {extra_args}')
    
    t0 = time.time()
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=600)
        elapsed = time.time() - t0
        
        # 从stdout提取关键指标
        output = result.stdout + result.stderr
        
        def extract(key):
            lines = output.split('\n')
            for line in lines:
                if key in line:
                    # 支持中文冒号：和英文冒号:
                    for sep in ['：', ':']:
                        parts = line.split(sep)
                        if len(parts) >= 2:
                            val = parts[-1].strip().replace('%','').replace(',','').replace('元','').strip()
                            # 取第一个能转成浮点数的部分
                            try:
                                return float(val.split()[0])
                            except:
                                pass
            return None
        
        ann_ret = extract('年化复利')
        max_dd  = extract('最大回撤')
        n_trade = extract('总交易笔数')
        win_rate = extract('胜率')
        pl_ratio = extract('盈亏比')
        avg_pct = extract('平均期望')
        
        success = result.returncode == 0
        err = result.stderr[:200] if result.returncode != 0 else ''
        
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        ann_ret = max_dd = n_trade = win_rate = pl_ratio = avg_pct = None
        success = False
        err = 'TIMEOUT(600s)'
    
    row = {
        'test_id': test_id,
        'test_name': test_name,
        'period': period_name,
        'start': start,
        'end': end,
        'ann_return': ann_ret,
        'max_drawdown': max_dd,
        'n_trades': int(n_trade) if n_trade is not None else 0,
        'win_rate': win_rate,
        'pl_ratio': pl_ratio,
        'avg_pct': avg_pct,
        'elapsed_s': round(elapsed, 1),
        'success': success,
        'error': err if not success else '',
    }
    
    # 打印结果行
    ret_str = f"{ann_ret:.1f}%" if ann_ret is not None else "FAIL"
    dd_str  = f"{max_dd:.1f}%" if max_dd is not None else "-"
    n_str   = f"{int(n_trade):>4}笔" if n_trade is not None else "  FAIL"
    wr_str  = f"{win_rate:.1f}%" if win_rate is not None else "-"
    pl_str  = f"{pl_ratio:.2f}" if pl_ratio is not None else "-"
    status  = "✓" if success else "✗"
    
    print(f"{tag} {status} {ret_str:>8} | 回撤{dd_str:>7} | {n_str} | 胜率{wr_str:>6} | 盈亏{pl_str} | {elapsed:.0f}s", flush=True)
    
    return row

# 顺序执行
results = []
count = 0
for period_name, start, end in PERIODS:
    for test_id, extra_args, test_name in TESTS:
        count += 1
        suffix = f'{test_id}_{period_name}'
        row = run_test(test_id, test_name, period_name, start, end, extra_args, suffix)
        row['run_idx'] = count
        results.append(row)
        
        # 每完成一组就保存
        with open(RESULTS_FILE, 'w') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

print()
print('='*60)
print(f"全部完成！{count}组")
print(f"结果 -> {RESULTS_FILE}")

# ── 汇总分析 ──
print()
print("【汇总表】")
print(f"{'测试':<12} {'区间':<8} {'年化':>8} {'回撤':>7} {'笔数':>6} {'胜率':>6} {'盈亏比':>6}")
print('-'*60)

for row in results:
    ann = f"{row['ann_return']:.1f}%" if row['ann_return'] is not None else "FAIL"
    dd  = f"{row['max_drawdown']:.1f}%" if row['max_drawdown'] is not None else "-"
    n   = f"{row['n_trades']}" if row['n_trades'] else "-"
    wr  = f"{row['win_rate']:.1f}%" if row['win_rate'] is not None else "-"
    pl  = f"{row['pl_ratio']:.2f}" if row['pl_ratio'] is not None else "-"
    print(f"{row['test_id']:<12} {row['period']:<8} {ann:>8} {dd:>7} {n:>6} {wr:>6} {pl:>6}")

print()
print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 全部完成!")
