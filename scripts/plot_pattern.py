#!/usr/bin/env python3
import duckdb
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

# Use Chinese font
chinese_font = fm.FontProperties(fname='/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc')
plt.rcParams['font.family'] = ['WenQuanYi Zen Hei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle, FancyArrowPatch
import numpy as np
from datetime import datetime, timedelta

conn = duckdb.connect('/mnt/d/Files/workspace-ai/quant/data/quant.duckdb', read_only=True)

# Top 3 candidates from 2026-05-08
candidates = [
    {'symbol': 'SH603220', 'name': '中贝通信', 'security_id': None, 'd0': '2026-04-23', 'signal': '2026-05-08'},
    {'symbol': 'SZ002361', 'name': '神剑股份', 'security_id': None, 'd0': '2026-04-21', 'signal': '2026-05-08'},
    {'symbol': 'SZ002429', 'name': '兆驰股份', 'security_id': None, 'd0': '2026-04-23', 'signal': '2026-05-08'},
]

# Get security_ids and bars
for c in candidates:
    row = conn.execute(f"SELECT security_id FROM security_master WHERE symbol = '{c['symbol']}'").fetchone()
    c['security_id'] = row[0]
    
    # Get bars from 20 days before d0 to 5 days after signal
    rows = conn.execute(f"""
    SELECT trade_date, open_price, high_price, low_price, close_price, 
           volume, change_pct, ma5_price, turnover_ratio
    FROM daily_bar
    WHERE security_id = {c['security_id']}
      AND trade_date BETWEEN DATE '{c['d0']}' - INTERVAL 20 days AND DATE '{c['signal']}' + INTERVAL 5 days
    ORDER BY trade_date
    """).fetchall()
    
    c['bars'] = []
    for r in rows:
        td = r[0] if hasattr(r[0], 'strftime') else datetime.strptime(str(r[0]), '%Y-%m-%d').date()
        c['bars'].append({
            'date': td,
            'open': float(r[1]),
            'high': float(r[2]),
            'low': float(r[3]),
            'close': float(r[4]),
            'volume': int(r[5]),
            'change_pct': float(r[6]),
            'ma5': float(r[7]) if r[7] is not None else None,
            'turnover': float(r[8]) if r[8] is not None else None,
        })
    
    # Also get d0 details
    d0_row = conn.execute(f"""
    SELECT trade_date, open_price, high_price, low_price, close_price, volume, change_pct, ma5_price
    FROM daily_bar WHERE security_id = {c['security_id']} AND trade_date = '{c['d0']}'
    """).fetchone()
    c['d0_data'] = {
        'date': d0_row[0], 'open': float(d0_row[1]), 'high': float(d0_row[2]),
        'low': float(d0_row[3]), 'close': float(d0_row[4]), 'vol': int(d0_row[5]),
        'change': float(d0_row[6]), 'ma5': float(d0_row[7]) if d0_row[7] else None
    }
    
    sig_row = conn.execute(f"""
    SELECT trade_date, open_price, high_price, low_price, close_price, volume, change_pct, ma5_price
    FROM daily_bar WHERE security_id = {c['security_id']} AND trade_date = '{c['signal']}'
    """).fetchone()
    c['sig_data'] = {
        'date': sig_row[0], 'open': float(sig_row[1]), 'high': float(sig_row[2]),
        'low': float(sig_row[3]), 'close': float(sig_row[4]), 'vol': int(sig_row[5]),
        'change': float(sig_row[6]), 'ma5': float(sig_row[7]) if sig_row[7] else None
    }

conn.close()

# Create figure with 3 subplots
fig, axes = plt.subplots(3, 1, figsize=(16, 18))
fig.suptitle('涨停回踩MA5收复策略 (limit_up_pullback_ma5_reclaim_v1) 典型走势图', fontsize=16, fontweight='bold')

for idx, c in enumerate(candidates):
    ax = axes[idx]
    bars = c['bars']
    d0_data = c['d0_data']
    sig_data = c['sig_data']
    
    dates = [b['date'] for b in bars]
    opens = [b['open'] for b in bars]
    highs = [b['high'] for b in bars]
    lows = [b['low'] for b in bars]
    closes = [b['close'] for b in bars]
    volumes = [b['volume'] for b in bars]
    ma5s = [b['ma5'] for b in bars]
    
    # Normalize volume for display
    max_vol = max(volumes)
    vol_scale = max(highs) / max_vol * max(volumes) * 0.4
    
    # Determine d0 and signal bar indices
    d0_idx = None
    sig_idx = None
    for i, b in enumerate(bars):
        bdate = b['date']
        d0date = d0_data['date']
        sigdate = sig_data['date']
        if hasattr(d0date, 'strftime'):
            d0date = datetime.strptime(str(d0date), '%Y-%m-%d').date()
            sigdate = datetime.strptime(str(sigdate), '%Y-%m-%d').date()
        if bdate == d0date:
            d0_idx = i
        if bdate == sigdate:
            sig_idx = i
    
    # Draw candlesticks manually
    for i, (d, o, h, l, c_close, v) in enumerate(zip(dates, opens, highs, lows, closes, volumes)):
        color = '#DC143C' if c_close >= o else '#00CED1'
        # Wick
        ax.plot([d, d], [l, h], color=color, linewidth=0.8)
        # Body
        bottom = min(o, c_close)
        height = abs(c_close - o)
        rect = Rectangle((mdates.date2num(d) - 0.25, bottom), 0.5, max(height, 0.01) if height < 0.01 else height,
                        facecolor=color if c_close >= o else 'none', 
                        edgecolor=color, linewidth=0.8)
        ax.add_patch(rect)
    
    # Draw MA5
    ma5_dates = [b['date'] for b in bars if b['ma5'] is not None]
    ma5_vals = [b['ma5'] for b in bars if b['ma5'] is not None]
    ax.plot(ma5_dates, ma5_vals, 'b-', linewidth=1.2, label='MA5', alpha=0.8)
    
    # Volume bars at bottom
    ax2 = ax.twinx()
    vol_colors = ['#DC143C' if bars[i]['close'] >= bars[i]['open'] else '#00CED1' for i in range(len(bars))]
    ax2.bar(dates, [v/max_vol for v in volumes], alpha=0.15, color='gray', width=0.8)
    ax2.set_ylabel('Volume (scaled)', fontsize=9, color='gray')
    ax2.set_ylim(0, 1)
    ax2.tick_params(axis='y', labelcolor='gray', labelsize=8)
    
    # Mark T0 day
    if d0_idx is not None:
        d0_bar = bars[d0_idx]
        d0_x = d0_bar['date']
        # Vertical shade for T0
        ax.axvspan(mdates.date2num(d0_x) - 0.5, mdates.date2num(d0_x) + 0.5,
                   alpha=0.15, color='orange', label=f"T0={c['d0']}")
        ax.annotate(f"T0\n{d0_bar['close']:.2f}\n({d0_bar['change_pct']:.1f}%)",
                   xy=(mdates.date2num(d0_x), d0_bar['low']),
                   xytext=(mdates.date2num(d0_x), d0_bar['low'] * 0.97),
                   fontsize=8, ha='center', color='darkorange',
                   arrowprops=dict(arrowstyle='->', color='darkorange', lw=1.5))
        
        # Draw T0 low line
        ax.axhline(y=d0_data['low'], color='darkorange', linestyle='--', linewidth=1, alpha=0.7)
        ax.text(dates[0], d0_data['low'] * 1.01, f" T0_low={d0_data['low']:.2f}", 
                fontsize=8, color='darkorange', va='bottom')
    
    # Mark Signal day
    if sig_idx is not None:
        sig_bar = bars[sig_idx]
        sig_x = sig_bar['date']
        ax.axvspan(mdates.date2num(sig_x) - 0.5, mdates.date2num(sig_x) + 0.5,
                   alpha=0.2, color='green', label=f"Signal={c['signal']}")
        ax.annotate(f"Signal\n{sig_bar['close']:.2f}\n({sig_bar['change_pct']:.1f}%)\nMA5={sig_bar['ma5']:.2f}",
                   xy=(mdates.date2num(sig_x), sig_bar['close']),
                   xytext=(mdates.date2num(sig_x) + 1.5, sig_bar['close']),
                   fontsize=8, ha='left', color='green',
                   arrowprops=dict(arrowstyle='->', color='green', lw=1.5))
        
        # MA5 reclaim line
        ax.axhline(y=sig_data['ma5'], color='green', linestyle=':', linewidth=1, alpha=0.7)
    
    # Shade pullback zone
    if d0_idx is not None and sig_idx is not None:
        ax.axvspan(mdates.date2num(dates[d0_idx + 1]), mdates.date2num(dates[sig_idx - 1]),
                   alpha=0.07, color='blue', label='Pullback zone')
    
    ax.set_title(f"{c['symbol']} {c['name']} | T0={c['d0']} Signal={c['signal']} | "
                 f"T0: close={d0_data['close']:.2f} low={d0_data['low']:.2f} vol={d0_data['vol']/1e6:.1f}M | "
                 f"Signal: close={sig_data['close']:.2f} MA5={sig_data['ma5']:.2f}",
                 fontsize=10, fontweight='bold')
    ax.set_ylabel('Price', fontsize=9)
    ax.tick_params(axis='y', labelsize=8)
    ax.tick_params(axis='x', labelsize=8, rotation=30)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
    ax.grid(True, alpha=0.2)
    ax.legend(loc='upper left', fontsize=8)
    
    # Set y margin
    price_min = min(lows)
    price_max = max(highs)
    ax.set_ylim(price_min * 0.93, price_max * 1.07)

plt.tight_layout()
output_path = '/mnt/d/Files/workspace-ai/quant/docs/pattern_limit_up_pullback_ma5_reclaim_examples.png'
plt.savefig(output_path, dpi=120, bbox_inches='tight', facecolor='white')
print(f"Saved to {output_path}")
plt.close()
