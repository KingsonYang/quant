async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail;
    const message = Array.isArray(detail)
      ? (detail[0] && (detail[0].msg || JSON.stringify(detail[0]))) || response.statusText
      : (detail || response.statusText);
    throw new Error(message);
  }
  return payload;
}

async function waitForJob(onUpdate) {
  while (true) {
    const job = await fetchJson("/api/jobs/current");
    if (onUpdate) {
      onUpdate(job);
    }
    if (!job || job.status !== "running") {
      return job;
    }
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
}

function subscribeJob(onEvent) {
  return new Promise((resolve, reject) => {
    const source = new EventSource("/api/jobs/stream");
    source.onmessage = (event) => {
      const job = JSON.parse(event.data);
      if (onEvent) {
        onEvent(job);
      }
      if (!job || job.status !== "running") {
        source.close();
        resolve(job);
      }
    };
    source.onerror = () => {
      source.close();
      reject(new Error("任务流中断"));
    };
  });
}

function fillStrategySelect(selectEl, strategies, onlyBacktest) {
  const items = onlyBacktest
    ? strategies.filter((item) => item.has_strategy_backtest)
    : strategies;
  selectEl.replaceChildren();
  items.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.name;
    option.textContent = item.display_name || item.name;
    selectEl.appendChild(option);
  });
}

function esc(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function num(value) {
  if (value === undefined || value === null || value === "" || value === "None" || value === "null") {
    return null;
  }
  const parsed = Number(value);
  return Number.isNaN(parsed) ? null : parsed;
}

function fmtMoney(value) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "—";
  return "¥" + Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function fmtSignedMoney(value) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "—";
  const amount = Number(value);
  const sign = amount > 0 ? "+" : amount < 0 ? "-" : "";
  return sign + "¥" + Math.abs(amount).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function fmtSignedPct(value) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "—";
  const amount = Number(value);
  const sign = amount > 0 ? "+" : "";
  return sign + amount.toFixed(2) + "%";
}

function signCls(value) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "neutral";
  if (value > 0) return "pos";
  if (value < 0) return "neg";
  return "neutral";
}

function renderTable(table, rows, columns) {
  table.replaceChildren();
  if (!rows.length) {
    return;
  }
  const visible = columns.filter((column, index) => (
    index < 2 || rows.some((row) => row[column.key] != null && row[column.key] !== "")
  ));
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  visible.forEach((column) => {
    const th = document.createElement("th");
    th.textContent = column.label;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    visible.forEach((column) => {
      const td = document.createElement("td");
      const value = row[column.key];
      td.textContent = value == null ? "" : String(value);
      if (column.key === "name" && row.symbol) { td.dataset.klineSymbol = row.symbol; td.dataset.klineDate = row.selection_date || row.selectionDate || row.buy_date || row.d0_date || ""; td.style.cursor = "pointer"; }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
}

const klineCache = new Map();

function klineOption(bars) {
  const dates = bars.map((item) => item.trade_date);
  return {
    tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
    grid: [
      { left: 60, right: 16, top: 24, bottom: "42%" },
      { left: 60, right: 16, top: "68%", bottom: 32 },
    ],
    xAxis: [
      { type: "category", data: dates, gridIndex: 0, axisLine: { onZero: false }, splitLine: { show: false } },
      { type: "category", data: dates, gridIndex: 1, axisLine: { onZero: false }, axisTick: { show: false }, axisLabel: { show: false }, splitLine: { show: false } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0 },
      { type: "value", scale: true, gridIndex: 1, splitNumber: 2 },
    ],
    series: [
      { type: "candlestick", data: bars.map((item) => [item.open, item.close, item.low, item.high]) },
      { type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: bars.map((item) => item.volume) },
    ],
  };
}

async function showKline(symbol, centerDate) {
  const drawer = document.getElementById("kline-drawer");
  if (!drawer) return;
  drawer.hidden = false;
  document.getElementById("kline-title").textContent = symbol + " K线";
  const key = symbol + ":" + centerDate;
  let data = klineCache.get(key);
  try {
    document.getElementById("kline-status").textContent = "加载中…";
    data = data || await fetchJson(`/api/stock-kline?symbol=${encodeURIComponent(symbol)}&center_date=${centerDate}`);
    klineCache.set(key, data);
    document.getElementById("kline-status").textContent = `${data.requested_start} 至 ${data.requested_end}`;
    setChartOption("kline-chart", "kline", klineOption(data.data));
    setTimeout(() => {
      if (charts.kline) {
        charts.kline.resize();
      }
    }, 50);
  } catch (e) {
    document.getElementById("kline-status").textContent = e.message;
  }
}
document.addEventListener("click", e => { const el=e.target.closest("[data-kline-symbol]"); if(el) showKline(el.dataset.klineSymbol,el.dataset.klineDate); if(e.target.id === "kline-close") document.getElementById("kline-drawer").hidden=true; });
let klineHoverTimer;
document.addEventListener("mouseover", e => { const el=e.target.closest("[data-kline-symbol]"); if (!el || window.matchMedia("(max-width: 768px)").matches) return; clearTimeout(klineHoverTimer); klineHoverTimer=setTimeout(()=>showKline(el.dataset.klineSymbol,el.dataset.klineDate),500); });
document.addEventListener("mouseout", e => { if(e.target.closest("[data-kline-symbol]")) clearTimeout(klineHoverTimer); });

function formatJobLog(job) {
  if (!job) {
    return "尚无任务";
  }
  const lines = (job.events || []).map((event) => event.message);
  if (job.status === "failed" && job.error) {
    lines.push(job.error);
  }
  if (job.result && typeof job.result === "object") {
    const result = job.result;
    if (result.inserted != null) {
      lines.push(`写入 ${result.inserted} 行，跳过 ${result.skipped_existing ?? 0} 行，错误 ${result.errors ?? 0}`);
    } else if (result.calendar_rows_written != null) {
      lines.push(`日历写入 ${result.calendar_rows_written}，更新 ${result.calendar_rows_updated ?? 0}`);
    }
  }
  return lines.join("\n") || "尚无任务";
}

function formatIngestProgress(progress) {
  if (!progress) {
    return "";
  }
  const total = Number(progress.total) || 0;
  const current = Number(progress.current) || 0;
  const pct = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0;
  const parts = [`${pct}%`, `${current}/${total}`];
  if (progress.inserted != null) {
    parts.push(`写入 ${progress.inserted}`);
  }
  if (progress.errors != null) {
    parts.push(`错误 ${progress.errors}`);
  }
  if (progress.symbol) {
    parts.push(progress.symbol);
  }
  return parts.join("  ");
}

const colorPos = "#e74c3c";
const colorNeg = "#27ae60";
const colorBlue = "#0f3460";
const exitReasonMap = {
  holding_period: { label: "持有到期", cls: "tag-hold" },
  take_profit: { label: "止盈", cls: "tag-tp" },
  stop_loss: { label: "止损", cls: "tag-sl" },
  open_at_end: { label: "持仓中", cls: "tag-open" },
};
const PARAM_LABELS = {
  holding_period: "持有周期(天)",
  selection_sort_by: "选股排序字段",
  selection_sort_order: "选股排序方向",
  selection_top_n: "选股数量",
  min_composite_score: "最低综合分",
  min_score_gap: "最小分差",
  market_filter_enabled: "大盘过滤",
  intraday_stop_loss_enabled: "盘中止损",
  max_positions: "最大持仓数",
  max_daily_buys: "每日最大买入",
  initial_capital: "初始资金",
  position_size_pct: "仓位比例(%)",
  take_profit_pct: "止盈(%)",
  stop_loss_pct: "止损(%)",
  commission_rate: "佣金(%)",
  stamp_tax_rate: "印花税(%)",
  transfer_fee_rate: "过户费(%)",
  buy_slippage_rate: "买入滑点(%)",
  sell_slippage_rate: "卖出滑点(%)",
};

let charts = {};

function waitForEcharts() {
  return new Promise((resolve) => {
    const start = Date.now();
    function tick() {
      if (typeof echarts !== "undefined") {
        resolve(true);
        return;
      }
      if (Date.now() - start > 8000) {
        resolve(false);
        return;
      }
      setTimeout(tick, 200);
    }
    tick();
  });
}

function disposeCharts() {
  Object.keys(charts).forEach((key) => {
    try {
      charts[key].dispose();
    } catch (error) {
      /* ignore */
    }
  });
  charts = {};
}

function setChartOption(id, name, option) {
  const el = document.getElementById(id);
  if (!el || typeof echarts === "undefined") {
    return;
  }
  if (!charts[name]) {
    charts[name] = echarts.init(el);
  }
  charts[name].setOption(option, true);
}

function showChartFallback(ids) {
  ids.forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#999;font-size:14px;">ECharts 加载失败, 请检查网络后刷新页面</div>';
    }
  });
}

function normalizeTrade(row) {
  const mapped = {
    code: row.symbol || row.code,
    name: row.name,
    selectionDate: row.selection_date || row.selectionDate,
    buyDate: row.buy_date || row.buyDate,
    sellDate: row.sell_date || row.sellDate,
    buyPrice: num(row.buy_price ?? row.buyPrice),
    sellPrice: num(row.sell_price ?? row.sellPrice),
    shares: num(row.shares),
    lots: num(row.lots),
    buyAmount: num(row.buy_amount ?? row.buyAmount),
    sellAmount: num(row.sell_amount ?? row.sellAmount),
    holdingDays: num(row.holding_days ?? row.holdingDays),
    returnPct: num(row.return_pct ?? row.returnPct),
    status: row.status,
    exitReason: row.exit_reason || row.exitReason,
  };
  ["selectionDate", "buyDate", "sellDate"].forEach((key) => {
    if (mapped[key] === "None" || mapped[key] === "null" || mapped[key] === "") {
      mapped[key] = null;
    }
  });
  return mapped;
}

function extractSummary(report) {
  const snapshot = report.parameter_snapshot || {};
  const summary = snapshot.summary || {};
  const request = snapshot.request || {};
  const metrics = report.metrics || {};
  return {
    run_id: report.run_id,
    score: num(metrics.score),
    initial_capital: num(summary.initial_capital) ?? num(request.initial_capital) ?? 100000,
    total_assets: num(summary.current_total_assets),
    total_return_pct: num(metrics.total_return_pct ?? summary.total_return_pct),
    annualized_return_pct: num(metrics.annualized_return_pct ?? summary.annualized_return_pct),
    realized_profit: num(summary.realized_profit),
    unrealized_profit: num(summary.unrealized_profit),
    trade_days: num(summary.trade_days),
    closed_trades: num(metrics.closed_trades ?? summary.closed_trade_count),
    open_positions: num(metrics.open_positions ?? summary.open_position_count),
    max_drawdown_pct: num(metrics.max_drawdown_pct),
    cash: num(summary.current_cash_balance),
    market_value: num(summary.current_market_value),
    total_transaction_costs: num(summary.total_transaction_costs ?? metrics.total_transaction_costs),
    costs_included: summary.costs_included ?? Boolean(metrics.costs_included),
    holding_period: summary.holding_period ?? request.holding_period,
    request,
    snapshot,
  };
}

function paramEntries(summary) {
  const request = summary.request || {};
  const items = [];
  Object.entries(PARAM_LABELS).forEach(([key, label]) => {
    const value = request[key];
    if (value === undefined || value === null || value === "") {
      return;
    }
    items.push([label, String(value)]);
  });
  if (summary.cash != null) items.push(["当前现金", fmtMoney(summary.cash)]);
  if (summary.market_value != null) items.push(["当前市值", fmtMoney(summary.market_value)]);
  if (summary.snapshot.run_name) items.push(["实验名称", String(summary.snapshot.run_name)]);
  if (summary.snapshot.score_version) items.push(["评分版本", String(summary.snapshot.score_version)]);
  return items;
}

function equitySeries(report, initialCapital) {
  const points = report.equity || [];
  if (!points.length) {
    return { dates: [], values: [initialCapital], drawdown: [0] };
  }
  const dates = points.map((point) => point.trade_date);
  const values = points.map((point) => Number(point.total_assets));
  const drawdown = [];
  let peak = values[0];
  values.forEach((value) => {
    if (value > peak) peak = value;
    drawdown.push(peak > 0 ? Number((((value - peak) / peak) * 100).toFixed(2)) : 0);
  });
  return { dates, values, drawdown };
}

function renderMetricCards(summary) {
  const cardDefs = [
    { label: "初始资金", value: fmtMoney(summary.initial_capital), cls: "neutral" },
    {
      label: "当前总资产",
      value: fmtMoney(summary.total_assets),
      sub: fmtSignedMoney(summary.total_assets - summary.initial_capital),
      cls: signCls(summary.total_assets - summary.initial_capital),
    },
    { label: "总收益率", value: fmtSignedPct(summary.total_return_pct), cls: signCls(summary.total_return_pct) },
    { label: "年化收益率", value: fmtSignedPct(summary.annualized_return_pct), cls: signCls(summary.annualized_return_pct) },
    { label: "已实现收益", value: fmtSignedMoney(summary.realized_profit), cls: signCls(summary.realized_profit) },
    { label: "未实现收益", value: fmtSignedMoney(summary.unrealized_profit), cls: signCls(summary.unrealized_profit) },
    { label: "交易成本", value: fmtMoney(summary.total_transaction_costs), cls: "neutral", sub: summary.costs_included ? "已计入收益" : "未计入" },
    { label: "最大回撤", value: fmtSignedPct(summary.max_drawdown_pct == null ? null : -Math.abs(summary.max_drawdown_pct)), cls: "neg" },
    {
      label: "已平仓交易",
      value: (summary.closed_trades ?? "—") + " 笔",
      sub: summary.open_positions ? "+ " + summary.open_positions + " 笔持仓中" : (summary.trade_days != null ? summary.trade_days + " 个交易日" : ""),
      cls: "neutral",
    },
  ];
  document.getElementById("summaryGrid").innerHTML = cardDefs.map((card) =>
    `<div class="metric-card"><div class="label">${esc(card.label)}</div><div class="value ${card.cls}">${card.value}</div>${card.sub ? `<div class="sub ${card.cls}">${card.sub}</div>` : ""}</div>`
  ).join("");
}

function renderWinLoss(trades) {
  const closedTrades = trades.filter((trade) => trade.status === "closed" && trade.returnPct != null);
  const wins = closedTrades.filter((trade) => trade.returnPct > 0);
  const losses = closedTrades.filter((trade) => trade.returnPct < 0);
  const winCount = wins.length;
  const lossCount = losses.length;
  const winRate = closedTrades.length ? (winCount / closedTrades.length * 100).toFixed(1) : "0";
  const avgWin = wins.length ? (wins.reduce((sum, trade) => sum + trade.returnPct, 0) / wins.length).toFixed(2) : "0";
  const avgLoss = losses.length ? (losses.reduce((sum, trade) => sum + trade.returnPct, 0) / losses.length).toFixed(2) : "0";
  const totalWinProfit = wins.reduce((sum, trade) => sum + trade.returnPct, 0);
  const totalLossProfit = Math.abs(losses.reduce((sum, trade) => sum + trade.returnPct, 0));
  const profitFactor = totalLossProfit > 0 ? (totalWinProfit / totalLossProfit).toFixed(2) : "∞";
  const maxWin = closedTrades.length ? Math.max(...closedTrades.map((trade) => trade.returnPct)) : 0;
  const maxLoss = closedTrades.length ? Math.min(...closedTrades.map((trade) => trade.returnPct)) : 0;
  const statDefs = [
    { num: winCount, cls: "pos", desc: "盈利交易" },
    { num: lossCount, cls: "neg", desc: "亏损交易" },
    { num: winRate + "%", cls: "neutral", desc: "胜率" },
    { num: "+" + avgWin + "%", cls: "pos", desc: "平均盈利" },
    { num: avgLoss + "%", cls: "neg", desc: "平均亏损" },
    { num: profitFactor, cls: "pos", desc: "盈亏比" },
    { num: "+" + Number(maxWin).toFixed(2) + "%", cls: "pos", desc: "最大单笔盈利" },
    { num: Number(maxLoss).toFixed(2) + "%", cls: "neg", desc: "最大单笔亏损" },
  ];
  document.getElementById("statsRow").innerHTML = statDefs.map((stat) =>
    `<div class="stat-block"><div class="num ${stat.cls}">${stat.num}</div><div class="desc">${stat.desc}</div></div>`
  ).join("");
}

function renderParams(entries, targetId) {
  document.getElementById(targetId).innerHTML = entries.map(([key, value]) =>
    `<div class="param-item"><span class="k">${esc(key)}</span><span class="v">${esc(value)}</span></div>`
  ).join("");
}

function renderTradeTable(trades) {
  const tbody = document.getElementById("tradeBody");
  tbody.replaceChildren();
  trades.forEach((trade, index) => {
    const hasRet = trade.returnPct != null;
    const retClass = hasRet ? (trade.returnPct >= 0 ? "pos" : "neg") : "neutral";
    const retSign = hasRet && trade.returnPct >= 0 ? "+" : "";
    const exitInfo = exitReasonMap[trade.exitReason] || { label: trade.exitReason || "—", cls: "tag-hold" };
    const statusInfo = trade.status === "open" ? { label: "持仓中", cls: "tag-open" } : { label: "已平仓", cls: "tag-hold" };
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${index + 1}</td>
      <td style="font-weight:600;">${esc(trade.code)}</td>
      <td data-kline-symbol="${esc(trade.code)}" data-kline-date="${trade.selectionDate || trade.buyDate || ""}" style="cursor:pointer">${esc(trade.name)}</td>
      <td>${trade.buyDate || "—"}</td>
      <td>${trade.sellDate || "—"}</td>
      <td>${trade.buyPrice != null ? Number(trade.buyPrice).toFixed(2) : "—"}</td>
      <td>${trade.sellPrice != null ? Number(trade.sellPrice).toFixed(2) : "—"}</td>
      <td>${trade.shares != null ? Number(trade.shares).toLocaleString() : "—"}</td>
      <td>${trade.buyAmount != null ? "¥" + Number(trade.buyAmount).toLocaleString() : "—"}</td>
      <td>${trade.sellAmount != null ? "¥" + Number(trade.sellAmount).toLocaleString() : "—"}</td>
      <td>${trade.holdingDays != null ? trade.holdingDays : "—"}</td>
      <td class="${retClass}" style="font-weight:700;">${hasRet ? retSign + Number(trade.returnPct).toFixed(2) + "%" : "—"}</td>
      <td><span class="tag ${exitInfo.cls}">${esc(exitInfo.label)}</span></td>
      <td><span class="tag ${statusInfo.cls}">${esc(statusInfo.label)}</span></td>
    `;
    tbody.appendChild(row);
  });
}

function renderPositionTable(positions) {
  const table = document.getElementById("positionTable");
  const section = document.getElementById("positions-section");
  const rows = (positions || []).map((item) => ({
    symbol: item.symbol,
    name: item.name,
    selection_date: item.selection_date,
    buy_date: item.buy_date,
    buy_price: item.buy_price,
    shares: item.shares,
    buy_amount: item.buy_amount,
  }));
  if (!rows.length) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  renderTable(table, rows, [
    { key: "symbol", label: "代码" },
    { key: "name", label: "名称" },
    { key: "selection_date", label: "选股日" },
    { key: "buy_date", label: "买入日" },
    { key: "buy_price", label: "买入价" },
    { key: "shares", label: "数量" },
    { key: "buy_amount", label: "买入金额" },
  ]);
}

function renderYearly(yearly) {
  const section = document.getElementById("yearly-section");
  const entries = Object.entries(yearly || {}).map(([year, metrics]) => {
    const ret = metrics.total_return_pct;
    const dd = metrics.max_drawdown_pct;
    const text = [
      ret != null ? `收益 ${Number(ret).toFixed(2)}%` : null,
      dd != null ? `回撤 ${Number(dd).toFixed(2)}%` : null,
      metrics.closed_trades != null ? `成交 ${metrics.closed_trades}` : null,
    ].filter(Boolean).join(" · ");
    return [year, text || "—"];
  });
  if (!entries.length) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  renderParams(entries, "yearlyGrid");
}

function equityOption(dates, values, initialCapital) {
  return {
    tooltip: {
      trigger: "axis",
      formatter(params) {
        const point = params[0];
        return `${point.axisValue}<br/>资产: <b>¥${Number(point.value).toLocaleString()}</b>`;
      },
    },
    grid: { left: 70, right: 40, top: 30, bottom: 50 },
    xAxis: { type: "category", data: dates, axisLabel: { rotate: 35, fontSize: 11 }, axisLine: { lineStyle: { color: "#ccc" } } },
    yAxis: {
      type: "value",
      name: "资产 (¥)",
      axisLabel: { formatter(val) { return "¥" + (val / 10000).toFixed(1) + "万"; } },
      splitLine: { lineStyle: { color: "#f0f0f0" } },
    },
    series: [{
      name: "总资产",
      type: "line",
      data: values,
      smooth: true,
      symbol: values.length > 80 ? "none" : "circle",
      symbolSize: 5,
      lineStyle: { color: colorBlue, width: 2.5 },
      itemStyle: { color: colorBlue },
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: "rgba(15,52,96,0.25)" },
          { offset: 1, color: "rgba(15,52,96,0.02)" },
        ]),
      },
      markLine: initialCapital == null ? undefined : {
        data: [{ yAxis: initialCapital, name: "初始资金" }],
        lineStyle: { color: "#999", type: "dashed" },
        label: { formatter: "初始资金 ¥" + Number(initialCapital).toLocaleString(), position: "insideEndTop", fontSize: 11 },
      },
    }],
  };
}

function tradeReturnOption(returnData) {
  return {
    tooltip: {
      trigger: "axis",
      formatter(params) {
        const point = params[0];
        const item = returnData[point.dataIndex];
        const sign = item.value >= 0 ? "+" : "";
        return `<b>${esc(item.name)} (${esc(item.code)})</b><br/>买入: ${item.date}<br/>收益: <b style="color:${item.value >= 0 ? colorPos : colorNeg}">${sign}${item.value}%</b><br/>退出: ${esc((exitReasonMap[item.exitReason] || { label: item.exitReason || "—" }).label)}`;
      },
    },
    grid: { left: 50, right: 30, top: 30, bottom: 60 },
    xAxis: { type: "category", data: returnData.map((_, index) => `#${index + 1}`), axisLabel: { fontSize: 11 }, axisLine: { lineStyle: { color: "#ccc" } } },
    yAxis: { type: "value", name: "收益率 (%)", axisLabel: { formatter: "{value}%" }, splitLine: { lineStyle: { color: "#f0f0f0" } } },
    series: [{
      name: "收益率",
      type: "bar",
      data: returnData.map((item) => ({ value: item.value, itemStyle: { color: item.value >= 0 ? colorPos : colorNeg } })),
      barWidth: "60%",
      markLine: { data: [{ yAxis: 0 }], lineStyle: { color: "#999", width: 1 }, label: { show: false } },
    }],
  };
}

function drawdownOption(dates, drawdown) {
  return {
    tooltip: { trigger: "axis", formatter(params) { const point = params[0]; return `${point.axisValue}<br/>回撤: <b style="color:${colorNeg}">${point.value}%</b>`; } },
    grid: { left: 60, right: 40, top: 30, bottom: 50 },
    xAxis: { type: "category", data: dates, axisLabel: { rotate: 35, fontSize: 11 }, axisLine: { lineStyle: { color: "#ccc" } } },
    yAxis: { type: "value", name: "回撤 (%)", axisLabel: { formatter: "{value}%" }, splitLine: { lineStyle: { color: "#f0f0f0" } } },
    series: [{
      name: "回撤",
      type: "line",
      data: drawdown,
      smooth: true,
      symbol: "none",
      lineStyle: { color: colorNeg, width: 2 },
      areaStyle: { color: "rgba(39,174,96,0.15)" },
    }],
  };
}

function monthlyOption(monthLabels, monthlyProfits, monthlyMap) {
  return {
    tooltip: {
      trigger: "axis",
      formatter(params) {
        const point = params[0];
        const month = monthlyMap[monthLabels[point.dataIndex]];
        return `<b>${point.axisValue}</b><br/>月度收益: <b style="color:${point.value >= 0 ? colorPos : colorNeg}">${point.value >= 0 ? "+" : ""}${point.value}%</b><br/>交易笔数: ${month.trades}`;
      },
    },
    grid: { left: 60, right: 30, top: 30, bottom: 30 },
    xAxis: { type: "category", data: monthLabels, axisLine: { lineStyle: { color: "#ccc" } } },
    yAxis: { type: "value", name: "收益率 (%)", axisLabel: { formatter: "{value}%" }, splitLine: { lineStyle: { color: "#f0f0f0" } } },
    series: [{
      name: "月度收益",
      type: "bar",
      data: monthlyProfits.map((value) => ({ value, itemStyle: { color: value >= 0 ? colorPos : colorNeg } })),
      barWidth: "50%",
      label: { show: true, position: "top", formatter(point) { return (point.value >= 0 ? "+" : "") + point.value + "%"; }, fontSize: 11 },
    }],
  };
}

async function renderBacktestCharts(report, summary, trades) {
  const ready = await waitForEcharts();
  if (!ready) {
    showChartFallback(["equityChart", "tradeChart", "drawdownChart", "monthlyChart"]);
    return;
  }
  const series = equitySeries(report, summary.initial_capital);
  const returnData = trades
    .filter((trade) => trade.returnPct != null)
    .map((trade) => ({
      value: Number(trade.returnPct.toFixed(2)),
      name: trade.name,
      code: trade.code,
      date: trade.buyDate,
      exitReason: trade.exitReason,
    }));
  const monthlyMap = {};
  trades.forEach((trade) => {
    const day = trade.sellDate || trade.buyDate;
    if (!day) return;
    const month = String(day).substring(0, 7);
    if (!monthlyMap[month]) monthlyMap[month] = { profit: 0, trades: 0 };
    monthlyMap[month].profit += trade.returnPct || 0;
    monthlyMap[month].trades += 1;
  });
  const monthLabels = Object.keys(monthlyMap).sort();
  const monthlyProfits = monthLabels.map((month) => Number(monthlyMap[month].profit.toFixed(2)));
  disposeCharts();
  setChartOption("equityChart", "equity", equityOption(series.dates, series.values, summary.initial_capital));
  setChartOption("tradeChart", "trade", tradeReturnOption(returnData));
  setChartOption("drawdownChart", "drawdown", drawdownOption(series.dates, series.drawdown));
  setChartOption("monthlyChart", "monthly", monthlyOption(monthLabels, monthlyProfits, monthlyMap));
}

async function loadDataPage() {
  const page = document.getElementById("data-page");
  if (!page) {
    return;
  }
  const cardsEl = document.getElementById("data-cards");
  const tdxEl = document.getElementById("tdx-message");
  const ingestBtn = document.getElementById("ingest-btn");
  const calendarBtn = document.getElementById("calendar-btn");
  const logEl = document.getElementById("job-log");
  const daysEl = document.getElementById("ingest-days");
  const statusEl = document.getElementById("data-status");
  const progressWrap = document.getElementById("job-progress");
  const progressFill = document.getElementById("job-progress-fill");
  const progressMeta = document.getElementById("job-progress-meta");
  let tdxAvailable = false;
  let jobBusy = false;

  function setBusy(busy) {
    jobBusy = busy;
    ingestBtn.disabled = busy || !tdxAvailable;
    calendarBtn.disabled = busy;
  }

  function renderProgress(job) {
    const progress = job && job.kind === "ingest" ? job.progress : null;
    if (!progressWrap || !progressFill || !progressMeta) {
      return;
    }
    if (!progress || progress.total == null) {
      progressWrap.hidden = true;
      progressFill.style.width = "0%";
      progressMeta.textContent = "";
      return;
    }
    const total = Number(progress.total) || 0;
    const current = Number(progress.current) || 0;
    const pct = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0;
    progressWrap.hidden = false;
    progressFill.style.width = `${pct}%`;
    progressMeta.textContent = formatIngestProgress(progress);
  }

  function renderJob(job) {
    logEl.textContent = formatJobLog(job);
    renderProgress(job);
  }

  async function followJob() {
    setBusy(true);
    try {
      const job = await waitForJob(renderJob);
      renderJob(job);
      await refresh();
    } catch (error) {
      logEl.textContent = error.message;
    } finally {
      setBusy(false);
      ingestBtn.disabled = !tdxAvailable;
    }
  }

  async function refresh() {
    const status = await fetchJson("/api/data/status");
    tdxAvailable = Boolean(status.tdx_available);
    statusEl.textContent =
      `行情最新日 ${status.daily_bar_max_date || "-"}，覆盖 ${status.daily_bar_security_count} 只；` +
      `日历最新日 ${status.calendar_max_date || "-"}` +
      (status.calendar_lagging ? "。请同步日历。" : "。");
    const cards = [
      { label: "行情最新日", value: status.daily_bar_max_date || "—", cls: "neutral" },
      { label: "覆盖股票", value: String(status.daily_bar_security_count ?? 0) + " 只", cls: "neutral" },
      { label: "日历最新日", value: status.calendar_max_date || "—", sub: status.calendar_lagging ? "请同步日历" : "已对齐", cls: status.calendar_lagging ? "pos" : "neutral" },
      { label: "通达信", value: status.tdx_available ? "可用" : "不可用", cls: status.tdx_available ? "neutral" : "pos" },
    ];
    cardsEl.innerHTML = cards.map((card) =>
      `<div class="metric-card"><div class="label">${esc(card.label)}</div><div class="value ${card.cls}">${esc(card.value)}</div>${card.sub ? `<div class="sub ${card.cls}">${esc(card.sub)}</div>` : ""}</div>`
    ).join("");
    tdxEl.textContent = status.tdx_message || "";
    ingestBtn.disabled = jobBusy || !tdxAvailable;
    calendarBtn.disabled = jobBusy;
  }

  ingestBtn.addEventListener("click", async () => {
    logEl.textContent = "正在增量更新…";
    renderProgress(null);
    setBusy(true);
    try {
      await fetchJson("/api/data/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ days: Number(daysEl.value || 5) }),
      });
      await followJob();
    } catch (error) {
      logEl.textContent = error.message;
      setBusy(false);
      ingestBtn.disabled = !tdxAvailable;
    }
  });

  calendarBtn.addEventListener("click", async () => {
    logEl.textContent = "正在同步日历…";
    renderProgress(null);
    setBusy(true);
    try {
      await fetchJson("/api/data/calendar", { method: "POST" });
      await followJob();
    } catch (error) {
      logEl.textContent = error.message;
      setBusy(false);
      ingestBtn.disabled = !tdxAvailable;
    }
  });

  await refresh();
  const existing = await fetchJson("/api/jobs/current");
  if (existing && existing.status === "running") {
    renderJob(existing);
    await followJob();
  } else if (existing) {
    renderJob(existing);
  }
}

async function loadSelectPage() {
  const strategyEl = document.getElementById("select-strategy");
  if (!strategyEl) {
    return;
  }
  const dateEl = document.getElementById("select-date");
  const emptyEl = document.getElementById("select-empty");
  const table = document.getElementById("select-table");
  const errorEl = document.getElementById("select-error");
  const metaEl = document.getElementById("select-meta");
  const strategies = await fetchJson("/api/strategies");
  fillStrategySelect(strategyEl, strategies, false);
  const status = await fetchJson("/api/data/status");
  if (status.calendar_max_date) {
    dateEl.value = status.calendar_max_date;
  }

  document.getElementById("select-btn").addEventListener("click", async () => {
    emptyEl.textContent = "";
    errorEl.textContent = "";
    table.replaceChildren();
    try {
      await fetchJson("/api/select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          strategy_name: strategyEl.value,
          trade_date: dateEl.value,
        }),
      });
      const job = await waitForJob();
      if (!job || job.status === "failed") {
        errorEl.textContent = (job && job.error) || "选股失败";
        return;
      }
      const result = job.result || {};
      metaEl.textContent = `${result.display_name || result.strategy_name || ""} · ${result.trade_date || ""} · ${result.selection_type || ""}`;
      if (!result.candidate_count) {
        emptyEl.textContent = "当日 0 只";
        return;
      }
      emptyEl.textContent = `命中 ${result.candidate_count} 只`;
      const candidates = (result.candidates || []).map((candidate) => ({ ...candidate, selection_date: result.trade_date }));
      renderTable(table, candidates, [
        { key: "symbol", label: "代码" },
        { key: "name", label: "名称" },
        { key: "d0_date", label: "d0_date" },
        { key: "composite_score", label: "综合分" },
        { key: "pool_score_v3", label: "pool_score_v3" },
        { key: "branch_type", label: "分支" },
        { key: "board_count", label: "连板" },
        { key: "first_limit_date", label: "首板日" },
        { key: "last_limit_date", label: "末板日" },
        { key: "false_break_occurred", label: "假突破" },
      ]);
    } catch (error) {
      errorEl.textContent = error.message;
    }
  });
}

async function loadBacktestPage() {
  const strategyEl = document.getElementById("bt-strategy");
  if (!strategyEl) {
    return;
  }
  const startEl = document.getElementById("bt-start");
  const endEl = document.getElementById("bt-end");
  const runBtn = document.getElementById("bt-run");
  const runsEl = document.getElementById("bt-runs");
  const metaEl = document.getElementById("bt-meta");
  const resultsEl = document.getElementById("bt-results");
  const titleEl = document.getElementById("reportTitle");
  const subtitleEl = document.getElementById("reportSubtitle");
  const runInfoEl = document.getElementById("runInfo");
  let continuation = null;

  const strategies = await fetchJson("/api/strategies");
  fillStrategySelect(strategyEl, strategies, true);
  const status = await fetchJson("/api/data/status");
  if (status.calendar_max_date) {
    endEl.value = status.calendar_max_date;
  }

  window.addEventListener("resize", () => {
    Object.keys(charts).forEach((key) => charts[key].resize());
  });

  function updateActionState() {
    if (!continuation) {
      runBtn.textContent = "运行";
      runBtn.disabled = false;
      return;
    }
    runBtn.textContent = continuation.can_continue ? "追加" : "运行";
    const endTooEarly = Boolean(
      continuation.can_continue &&
      continuation.last_end_date &&
      endEl.value &&
      endEl.value <= continuation.last_end_date,
    );
    runBtn.disabled = endTooEarly;
    const parts = [];
    if (continuation.run_id) parts.push(`run_id ${continuation.run_id}`);
    if (continuation.last_end_date) parts.push(`已算到 ${continuation.last_end_date}`);
    if (endTooEarly) parts.push("结束日需要更晚才能追加，下面是已有结果");
    if (continuation.reason) parts.push(continuation.reason);
    metaEl.textContent = parts.join(" · ");
  }

  function uniqueRuns(runs) {
    const seen = new Set();
    const out = [];
    runs.forEach((run) => {
      const key = `${run.start_date}|${run.end_date}`;
      if (seen.has(key)) {
        return;
      }
      seen.add(key);
      out.push(run);
    });
    return out;
  }

  function fillRunShortcuts(runs, selectedRunId) {
    const items = uniqueRuns(runs);
    runsEl.replaceChildren();
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = items.length ? "选择已跑区间" : "暂无已跑区间";
    runsEl.appendChild(blank);
    items.forEach((run) => {
      const option = document.createElement("option");
      option.value = String(run.run_id);
      option.dataset.start = run.start_date;
      option.dataset.end = run.end_date;
      option.textContent = `${run.start_date} → ${run.end_date}`;
      if (selectedRunId != null && String(run.run_id) === String(selectedRunId)) {
        option.selected = true;
      }
      runsEl.appendChild(option);
    });
  }

  async function refreshRunShortcuts(autoApplyLatest, preferRunId) {
    if (!strategyEl.value) {
      fillRunShortcuts([]);
      return [];
    }
    let runs = [];
    try {
      runs = await fetchJson(
        `/api/backtest-runs?strategy_name=${encodeURIComponent(strategyEl.value)}`,
      );
    } catch (error) {
      metaEl.textContent = `已跑区间加载失败：${error.message}`;
      runs = [];
    }
    const selected = preferRunId
      || (autoApplyLatest && !startEl.value && runs.length ? runs[0].run_id : runsEl.value);
    fillRunShortcuts(runs, selected);
    return runs;
  }

  async function applySelectedRun() {
    const option = runsEl.selectedOptions[0];
    if (!option || !option.value) {
      return;
    }
    startEl.value = option.dataset.start || "";
    if (option.dataset.end) {
      endEl.value = option.dataset.end;
    }
    await refreshContinuation();
  }

  async function refreshContinuation() {
    if (!strategyEl.value || !startEl.value) {
      continuation = null;
      resultsEl.hidden = true;
      updateActionState();
      metaEl.textContent = "";
      return;
    }
    continuation = await fetchJson(
      `/api/backtests/continuation?strategy_name=${encodeURIComponent(strategyEl.value)}&start_date=${startEl.value}`,
    );
    if (continuation.run_id) {
      await renderReport(continuation.run_id);
      if (![...runsEl.options].some((option) => option.value === String(continuation.run_id))) {
        await refreshRunShortcuts(false, continuation.run_id);
        if (![...runsEl.options].some((option) => option.value === String(continuation.run_id))) {
          fillRunShortcuts(
            [{
              run_id: continuation.run_id,
              start_date: startEl.value,
              end_date: continuation.last_end_date,
            }],
            continuation.run_id,
          );
        }
      } else {
        runsEl.value = String(continuation.run_id);
      }
    } else {
      resultsEl.hidden = true;
    }
    updateActionState();
  }

  async function renderReport(runId) {
    const report = await fetchJson(`/api/backtests/${runId}`);
    const summary = extractSummary(report);
    const trades = (report.trades || []).map(normalizeTrade);
    const selected = strategyEl.options[strategyEl.selectedIndex];
    resultsEl.hidden = false;
    titleEl.textContent = (selected && selected.textContent) || report.strategy_name || "策略回测";
    subtitleEl.textContent = `回测区间: ${report.start_date} -> ${report.end_date}` +
      (summary.holding_period != null ? ` | 持有周期: ${summary.holding_period} 天` : "");
    const runParts = [`Experiment Run ID: ${report.run_id}`];
    if (summary.score != null) runParts.push("Score: " + summary.score);
    runInfoEl.textContent = runParts.join(" | ");
    renderMetricCards(summary);
    renderWinLoss(trades);
    renderParams(paramEntries(summary), "paramsGrid");
    renderYearly(report.yearly_metrics);
    renderPositionTable(report.open_positions);
    renderTradeTable(trades);
    await renderBacktestCharts(report, summary, trades);
  }

  async function renderLiveEquity(job) {
    const ready = await waitForEcharts();
    if (!ready) return;
    const events = (job && job.events) || [];
    const points = events.filter((event) => event.payload && event.payload.equity != null);
    if (!points.length) return;
    resultsEl.hidden = false;
    const dates = points.map((event) => event.payload.trade_date);
    const values = points.map((event) => Number(event.payload.equity));
    setChartOption("equityChart", "equity", equityOption(dates, values, null));
  }

  strategyEl.addEventListener("change", async () => {
    await refreshRunShortcuts(true);
    await applySelectedRun();
    if (!runsEl.value) {
      await refreshContinuation();
    }
  });
  startEl.addEventListener("change", refreshContinuation);
  endEl.addEventListener("change", updateActionState);
  runsEl.addEventListener("change", applySelectedRun);

  runBtn.addEventListener("click", async () => {
    try {
      await fetchJson("/api/backtests/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          strategy_name: strategyEl.value,
          start_date: startEl.value,
          end_date: endEl.value,
        }),
      });
      resultsEl.hidden = false;
      const job = await subscribeJob((current) => {
        if (!current) return;
        renderLiveEquity(current);
        const last = current.events && current.events[current.events.length - 1];
        if (last && last.payload && last.payload.current_trade_day) {
          metaEl.textContent =
            `进度 ${last.payload.current_trade_day}/${last.payload.total_trade_days} · ${last.payload.trade_date}`;
          runInfoEl.textContent = metaEl.textContent;
        }
      });
      if (!job || job.status === "failed") {
        metaEl.textContent = (job && job.error) || "回测失败";
        return;
      }
      await renderReport(job.result.run_id);
      await refreshRunShortcuts(false, job.result.run_id);
      await refreshContinuation();
    } catch (error) {
      updateActionState();
      if (continuation && continuation.run_id) {
        metaEl.textContent = [metaEl.textContent, error.message].filter(Boolean).join(" · ");
      } else {
        metaEl.textContent = error.message;
      }
    }
  });

  await refreshRunShortcuts(true);
  if (runsEl.value) {
    await applySelectedRun();
  } else {
    await refreshContinuation();
  }
}

document.addEventListener("DOMContentLoaded", () => {
  loadDataPage();
  loadSelectPage();
  loadBacktestPage();
});
