// QuantWeb Pro - Modern Institutional Frontend Controller
(function () {
  "use strict";

  // Global State
  const STATE = {
    theme: localStorage.getItem("quant_theme") || "dark",
    charts: {},
    latestBacktestPayload: null,
    strategies: [],
    marketSentiment: null,
    tradesGrid: {
      allTrades: [],
      filteredTrades: [],
      currentPage: 1,
      pageSize: 25,
      filter: "all",
      searchQuery: "",
      sortField: "sell_date",
      sortOrder: "desc",
    },
    selectGrid: {
      candidates: [],
      searchQuery: "",
    },
  };

  // --- Theme Management ---
  function initTheme() {
    document.documentElement.setAttribute("data-theme", STATE.theme);
    const btn = document.getElementById("theme-toggle-btn");
    if (btn) {
      btn.textContent = STATE.theme === "dark" ? "☀️" : "🌙";
      btn.addEventListener("click", () => {
        STATE.theme = STATE.theme === "dark" ? "light" : "dark";
        localStorage.setItem("quant_theme", STATE.theme);
        document.documentElement.setAttribute("data-theme", STATE.theme);
        btn.textContent = STATE.theme === "dark" ? "☀️" : "🌙";
        resizeAndRefreshCharts();
      });
    }
  }

  function resizeAndRefreshCharts() {
    Object.values(STATE.charts).forEach((chart) => {
      if (chart && !chart.isDisposed()) {
        chart.resize();
      }
    });
    if (STATE.latestBacktestPayload) {
      renderBacktestCharts(STATE.latestBacktestPayload);
    }
  }

  window.addEventListener("resize", () => {
    Object.values(STATE.charts).forEach((chart) => {
      if (chart && !chart.isDisposed()) {
        chart.resize();
      }
    });
  });

  // --- Network Utilities ---
  async function fetchJson(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const errorJson = await response.json();
        detail = errorJson.detail || detail;
      } catch (_) {}
      throw new Error(detail);
    }
    return response.json();
  }

  function formatNum(val, decimals = 2) {
    if (val === null || val === undefined || isNaN(val)) return "--";
    return Number(val).toLocaleString("zh-CN", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
  }

  function formatPct(val, withSign = false) {
    if (val === null || val === undefined || isNaN(val)) return "--";
    const num = Number(val);
    const text = num.toFixed(2) + "%";
    if (withSign && num > 0) return "+" + text;
    return text;
  }

  function getSignClass(val) {
    const num = Number(val);
    if (isNaN(num) || num === 0) return "neutral";
    return num > 0 ? "pos" : "neg";
  }

  // --- Robust Job Streaming & Polling ---
  function waitForJob(onUpdate) {
    return new Promise((resolve, reject) => {
      let isDone = false;
      let source = null;
      let pollTimer = null;

      function finish(job) {
        if (isDone) return;
        isDone = true;
        if (pollTimer) clearInterval(pollTimer);
        if (source) {
          try {
            source.close();
          } catch (_) {}
        }
        resolve(job);
      }

      // 1. Try EventSource
      try {
        source = new EventSource("/api/jobs/stream");
        source.onmessage = (event) => {
          try {
            const job = JSON.parse(event.data);
            if (onUpdate) onUpdate(job);
            if (!job || job.status !== "running") {
              finish(job);
            }
          } catch (e) {
            console.error("Failed to parse job stream event:", e);
          }
        };
        source.onerror = () => {
          try {
            source.close();
          } catch (_) {}
          source = null;
        };
      } catch (_) {
        source = null;
      }

      // 2. Polling Fallback (dual guarantee)
      pollTimer = setInterval(async () => {
        if (isDone) return;
        try {
          const job = await fetchJson("/api/jobs/current");
          if (onUpdate) onUpdate(job);
          if (!job || job.status !== "running") {
            finish(job);
          }
        } catch (_) {}
      }, 350);
    });
  }

  // --- Candlestick & Trade Visualizer Modal ---
  function initKlineDrawer() {
    const backdrop = document.getElementById("kline-backdrop");
    const closeBtn = document.getElementById("kline-close-btn");
    if (!backdrop) return;

    function closeDrawer() {
      backdrop.classList.remove("open");
      if (STATE.charts.kline && !STATE.charts.kline.isDisposed()) {
        STATE.charts.kline.clear();
      }
    }

    if (closeBtn) closeBtn.addEventListener("click", closeDrawer);
    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) closeDrawer();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && backdrop.classList.contains("open")) {
        closeDrawer();
      }
    });
  }

  async function openKlineDrawer(symbol, centerDate, tradeContext = null) {
    const backdrop = document.getElementById("kline-backdrop");
    const titleEl = document.getElementById("kline-title");
    const metaEl = document.getElementById("kline-meta");
    const bannerEl = document.getElementById("kline-trade-banner");
    const chartEl = document.getElementById("kline-chart");
    if (!backdrop || !chartEl) return;

    backdrop.classList.add("open");
    titleEl.textContent = `加载中... ${symbol}`;
    metaEl.textContent = centerDate ? `中心基准日: ${centerDate}` : "";

    if (tradeContext && bannerEl) {
      bannerEl.style.display = "flex";
      const buyInfo = document.getElementById("kline-buy-info");
      const sellInfo = document.getElementById("kline-sell-info");
      const holdDays = document.getElementById("kline-hold-days");
      const pnlBadge = document.getElementById("kline-pnl-badge");

      if (buyInfo) buyInfo.textContent = `${tradeContext.buy_date} (¥${formatNum(tradeContext.buy_price)})`;
      if (sellInfo) sellInfo.textContent = `${tradeContext.sell_date} (¥${formatNum(tradeContext.sell_price)})`;
      if (holdDays) holdDays.textContent = tradeContext.holding_days ? `${tradeContext.holding_days} 天` : "-";
      if (pnlBadge) {
        const ret = Number(tradeContext.return_pct || 0);
        pnlBadge.textContent = `${formatPct(ret, true)} (${tradeContext.exit_reason || "正常平仓"})`;
        pnlBadge.className = `tag-badge ${ret >= 0 ? "bg-pos" : "bg-neg"}`;
      }
    } else if (bannerEl) {
      bannerEl.style.display = "none";
    }

    try {
      let url = `/api/stock-kline?symbol=${encodeURIComponent(symbol)}`;
      if (tradeContext && tradeContext.buy_date && tradeContext.sell_date) {
        url += `&start_date=${encodeURIComponent(tradeContext.buy_date)}&end_date=${encodeURIComponent(tradeContext.sell_date)}`;
      } else if (centerDate) {
        url += `&center_date=${encodeURIComponent(centerDate)}`;
      }
      const resp = await fetchJson(url);
      const rows = resp.data || [];
      titleEl.textContent = `${resp.name || symbol} (${resp.symbol})`;
      metaEl.textContent = `共 ${rows.length} 根日K线 · 交易区间打标复盘`;

      renderKlineChart(chartEl, rows, tradeContext);
    } catch (err) {
      titleEl.textContent = `加载失败: ${symbol}`;
      metaEl.textContent = err.message;
    }
  }

  function renderKlineChart(container, rows, tradeContext) {
    if (!STATE.charts.kline) {
      STATE.charts.kline = echarts.init(container);
    }
    const chart = STATE.charts.kline;
    const isDark = STATE.theme === "dark";

    const dates = rows.map((r) => r.trade_date);
    const ohlc = rows.map((r) => [r.open, r.close, r.low, r.high]);
    const ma5 = rows.map((r) => (r.ma5 ? Number(r.ma5) : null));
    const ma10 = rows.map((r) => (r.ma10 ? Number(r.ma10) : null));
    const ma20 = rows.map((r) => (r.ma20 ? Number(r.ma20) : null));
    const volumes = rows.map((r) => r.volume);

    // Calculate Volume Moving Averages (5-day & 10-day)
    function calcMa(data, window) {
      const result = [];
      for (let i = 0; i < data.length; i++) {
        if (i < window - 1) {
          result.push(null);
          continue;
        }
        let sum = 0;
        for (let j = 0; j < window; j++) {
          sum += data[i - j];
        }
        result.push(Math.round(sum / window));
      }
      return result;
    }
    const volMa5 = calcMa(volumes, 5);
    const volMa10 = calcMa(volumes, 10);

    // Build MarkPoints for Buy (B) and Sell (S) & MarkArea for holding span
    const markPoints = [];
    let markArea = undefined;
    if (tradeContext) {
      if (tradeContext.buy_date && dates.includes(tradeContext.buy_date)) {
        markPoints.push({
          name: "买入",
          coord: [tradeContext.buy_date, tradeContext.buy_price],
          value: "B",
          itemStyle: { color: "#f43f5e" },
          label: { color: "#fff", fontWeight: "bold", fontSize: 11 },
        });
      }
      if (tradeContext.sell_date && dates.includes(tradeContext.sell_date)) {
        markPoints.push({
          name: "卖出",
          coord: [tradeContext.sell_date, tradeContext.sell_price],
          value: "S",
          itemStyle: { color: "#10b981" },
          label: { color: "#fff", fontWeight: "bold", fontSize: 11 },
        });
      }
      if (tradeContext.buy_date && tradeContext.sell_date) {
        const isProfitable = Number(tradeContext.return_pct || 0) >= 0;
        markArea = {
          itemStyle: {
            color: isProfitable ? "rgba(244, 63, 94, 0.12)" : "rgba(16, 185, 129, 0.12)",
          },
          data: [[{ xAxis: tradeContext.buy_date }, { xAxis: tradeContext.sell_date }]],
        };
      }
    }

    const option = {
      backgroundColor: "transparent",
      animation: false,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        backgroundColor: isDark ? "rgba(17, 24, 39, 0.95)" : "rgba(255, 255, 255, 0.95)",
        borderColor: isDark ? "rgba(255, 255, 255, 0.15)" : "#cbd5e1",
        textStyle: { color: isDark ? "#f8fafc" : "#0f172a" },
        formatter: (params) => {
          const item = params[0];
          const r = rows[item.dataIndex];
          if (!r) return "";
          return `
            <div style="font-weight:700; margin-bottom:4px;">${r.trade_date}</div>
            <div>开盘: <strong>¥${r.open}</strong></div>
            <div>收盘: <strong>¥${r.close}</strong> (${r.change_pct !== undefined ? (r.change_pct >= 0 ? "+" : "") + r.change_pct + "%" : "-"})</div>
            <div>最高: <strong>¥${r.high}</strong></div>
            <div>最低: <strong>¥${r.low}</strong></div>
            <div>换手: <strong>${r.turnover_ratio !== undefined ? r.turnover_ratio + "%" : "-"}</strong></div>
            <div>成交量: <strong>${(r.volume / 10000).toFixed(1)} 万手</strong></div>
          `;
        },
      },
      legend: {
        data: ["日K", "MA5", "MA10", "MA20", "成交量", "VOL-MA5", "VOL-MA10"],
        textStyle: { color: isDark ? "#94a3b8" : "#475569" },
        top: 0,
      },
      grid: [
        { left: 50, right: 30, top: 40, height: "55%" },
        { left: 50, right: 30, top: "72%", height: "20%" },
      ],
      xAxis: [
        {
          type: "category",
          data: dates,
          axisLine: { lineStyle: { color: isDark ? "rgba(255,255,255,0.15)" : "#cbd5e1" } },
          axisLabel: { color: isDark ? "#94a3b8" : "#64748b", fontSize: 10 },
        },
        {
          type: "category",
          gridIndex: 1,
          data: dates,
          axisLine: { lineStyle: { color: isDark ? "rgba(255,255,255,0.15)" : "#cbd5e1" } },
          axisLabel: { show: false },
        },
      ],
      yAxis: [
        {
          type: "value",
          scale: true,
          splitLine: { lineStyle: { color: isDark ? "rgba(255,255,255,0.06)" : "#f1f5f9" } },
          axisLabel: { color: isDark ? "#94a3b8" : "#64748b", formatter: "¥{value}" },
        },
        {
          type: "value",
          gridIndex: 1,
          scale: true,
          splitLine: { show: false },
          axisLabel: {
            color: isDark ? "#94a3b8" : "#64748b",
            formatter: (v) => (v >= 10000 ? (v / 10000).toFixed(0) + "w" : v),
          },
        },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1] },
        { type: "slider", xAxisIndex: [0, 1], bottom: 2, height: 16 },
      ],
      series: [
        {
          name: "日K",
          type: "candlestick",
          data: ohlc,
          itemStyle: {
            color: "#f43f5e",
            color0: "#10b981",
            borderColor: "#f43f5e",
            borderColor0: "#10b981",
          },
          markPoint: {
            data: markPoints,
          },
          markArea: markArea,
        },
        {
          name: "MA5",
          type: "line",
          data: ma5,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1.5, color: "#f59e0b" },
        },
        {
          name: "MA10",
          type: "line",
          data: ma10,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1.5, color: "#a855f7" },
        },
        {
          name: "MA20",
          type: "line",
          data: ma20,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1.5, color: "#06b6d4" },
        },
        {
          name: "成交量",
          type: "bar",
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: volumes,
          itemStyle: {
            color: (params) => {
              const r = rows[params.dataIndex];
              return r && r.close >= r.open ? "rgba(244, 63, 94, 0.6)" : "rgba(16, 185, 129, 0.6)";
            },
          },
        },
        {
          name: "VOL-MA5",
          type: "line",
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: volMa5,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1.2, color: "#f59e0b" },
        },
        {
          name: "VOL-MA10",
          type: "line",
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: volMa10,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1.2, color: "#06b6d4" },
        },
      ],
    };

    chart.setOption(option, true);
  }

  // =========================================================
  // PAGE 1: DATA CENTER (/data)
  // =========================================================
  async function loadDataPage() {
    const ingestBtn = document.getElementById("ingest-btn");
    const calendarBtn = document.getElementById("calendar-btn");
    const ingestDays = document.getElementById("ingest-days");
    const jobLog = document.getElementById("job-log");
    const jobProgress = document.getElementById("job-progress");
    const consoleStatus = document.getElementById("console-job-status");
    const consoleDot = document.getElementById("console-status-pill");

    async function refreshStatus() {
      try {
        const st = await fetchJson("/api/data/status");
        const dailyDate = st.daily_bar_max_date || st.latest_daily_bar_date || "无记录";
        const stockCount = st.daily_bar_security_count ?? st.covered_stock_count ?? 0;
        const calDate = st.calendar_max_date || st.latest_calendar_date || "无记录";

        const dailyEl = document.getElementById("stat-daily-date");
        if (dailyEl) dailyEl.textContent = dailyDate;

        const coveredEl = document.getElementById("stat-covered-stocks");
        if (coveredEl) coveredEl.textContent = formatNum(stockCount, 0) + " 只";

        const calEl = document.getElementById("stat-calendar-date");
        if (calEl) calEl.textContent = calDate;

        const calWarning = document.getElementById("stat-calendar-warning");
        const calBadge = document.getElementById("stat-calendar-badge");
        if (calWarning && calBadge) {
          if (st.calendar_lagging) {
            calWarning.textContent = "⚠️ 日历落后于行情最新日，建议更新";
            calWarning.className = "pos";
            calBadge.textContent = "滞后待更新";
            calBadge.className = "hero-badge bg-amber";
          } else {
            calWarning.textContent = "日历与行情对齐同步";
            calWarning.className = "neg";
            calBadge.textContent = "日历对齐正常";
            calBadge.className = "hero-badge bg-neg";
          }
        }

        const tdxStatus = document.getElementById("stat-tdx-status");
        const tdxDetail = document.getElementById("stat-tdx-detail");
        const tdxBadge = document.getElementById("stat-tdx-badge");
        if (tdxStatus) {
          if (st.tdx_available) {
            tdxStatus.textContent = "在线畅通";
            tdxStatus.className = "hero-val neg";
            if (tdxDetail) tdxDetail.textContent = st.tdx_message || "高速通道可用";
            if (tdxBadge) {
              tdxBadge.textContent = "TDX ONLINE";
              tdxBadge.className = "hero-badge bg-neg";
            }
          } else {
            tdxStatus.textContent = "连接受限";
            tdxStatus.className = "hero-val dim";
            if (tdxDetail) tdxDetail.textContent = st.tdx_message || "通达信离线";
            if (tdxBadge) {
              tdxBadge.textContent = "OFFLINE";
              tdxBadge.className = "hero-badge bg-amber";
            }
          }
        }

        // Backward compatibility
        const legacyEl = document.getElementById("data-status-text");
        if (legacyEl) {
          legacyEl.textContent = `最新交易日: ${dailyDate} 标的: ${stockCount}`;
        }
      } catch (err) {
        console.error("Failed to refresh data status:", err);
      }
    }

    await refreshStatus();

    async function executeDataTask(endpoint, payload = null) {
      if (ingestBtn) ingestBtn.disabled = true;
      if (calendarBtn) calendarBtn.disabled = true;
      if (consoleStatus) consoleStatus.textContent = "任务已提交...";
      if (consoleDot) consoleDot.className = "status-dot warning";
      if (jobProgress) jobProgress.style.width = "5%";

      try {
        await fetchJson(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: payload ? JSON.stringify(payload) : undefined,
        });

        const job = await waitForJob((job) => {
          if (!job) return;
          if (job.progress) {
            const pct = job.progress.percentage || 0;
            if (jobProgress) jobProgress.style.width = `${pct}%`;
            if (consoleStatus) {
              const sym = job.progress.current_symbol ? ` (${job.progress.current_symbol})` : "";
              consoleStatus.textContent = `同步中: ${pct}%${sym}`;
            }
          }
          if (job.events && job.events.length) {
            if (jobLog) {
              jobLog.textContent = job.events
                .map((e) => `[${(e[0] || "").slice(11, 19)}] ${e[1] || ""}`)
                .join("\n");
              jobLog.scrollTop = jobLog.scrollHeight;
            }
          }
        });

        if (ingestBtn) ingestBtn.disabled = false;
        if (calendarBtn) calendarBtn.disabled = false;

        if (job && job.status === "succeeded") {
          if (jobProgress) jobProgress.style.width = "100%";
          if (consoleStatus) consoleStatus.textContent = "任务执行成功";
          if (consoleDot) consoleDot.className = "status-dot online";
          await refreshStatus();
        } else {
          if (consoleStatus) consoleStatus.textContent = `执行失败: ${(job && job.error) || "未知错误"}`;
          if (consoleDot) consoleDot.className = "status-dot offline";
        }
      } catch (err) {
        alert(`发起任务失败: ${err.message}`);
        if (ingestBtn) ingestBtn.disabled = false;
        if (calendarBtn) calendarBtn.disabled = false;
      }
    }

    if (ingestBtn) {
      ingestBtn.addEventListener("click", () => {
        const days = parseInt(ingestDays ? ingestDays.value : "5", 10) || 5;
        executeDataTask("/api/data/ingest", { days });
      });
    }

    if (calendarBtn) {
      calendarBtn.addEventListener("click", () => {
        executeDataTask("/api/data/calendar");
      });
    }
  }

  // =========================================================
  // PAGE 2: STRATEGY SELECTOR (/select)
  // =========================================================
  async function loadSelectPage() {
    const stratSelect = document.getElementById("select-strategy");
    const dateInput = document.getElementById("select-date");
    const runBtn = document.getElementById("run-select-btn") || document.getElementById("select-btn");
    const searchInput = document.getElementById("select-search-input");
    const exportBtn = document.getElementById("export-select-btn");
    const prevDateBtn = document.getElementById("btn-date-prev");
    const nextDateBtn = document.getElementById("btn-date-next");

    // Load registered strategies
    try {
      const strats = await fetchJson("/api/strategies");
      STATE.strategies = strats;
      if (stratSelect) {
        stratSelect.innerHTML = strats
          .map((s) => `<option value="${s.name}">${s.display_name || s.name}</option>`)
          .join("");

        if (strats.length > 0) {
          const badge = document.getElementById("select-current-strategy");
          if (badge) badge.textContent = strats[0].display_name || strats[0].name;
        }
      }
    } catch (_) {}

    // Load initial date from status
    try {
      const st = await fetchJson("/api/data/status");
      const initialDate = st.daily_bar_max_date || st.latest_daily_bar_date || "2026-09-17";
      if (dateInput) {
        dateInput.value = initialDate;
        const badge = document.getElementById("select-current-date-badge");
        if (badge) badge.textContent = initialDate;
        loadMarketSentiment(initialDate);
      }
    } catch (_) {
      if (dateInput) {
        dateInput.value = "2026-09-17";
        loadMarketSentiment(dateInput.value);
      }
    }

    if (stratSelect) {
      stratSelect.addEventListener("change", () => {
        const sel = stratSelect.options[stratSelect.selectedIndex];
        const badge = document.getElementById("select-current-strategy");
        if (badge) badge.textContent = sel ? sel.text : "";
      });
    }

    if (dateInput) {
      dateInput.addEventListener("change", () => {
        const badge = document.getElementById("select-current-date-badge");
        if (badge) badge.textContent = dateInput.value;
        loadMarketSentiment(dateInput.value);
      });
    }

    if (prevDateBtn && dateInput) {
      prevDateBtn.addEventListener("click", () => {
        const cur = new Date(dateInput.value);
        cur.setDate(cur.getDate() - 1);
        dateInput.value = cur.toISOString().split("T")[0];
        dateInput.dispatchEvent(new Event("change"));
      });
    }

    if (nextDateBtn && dateInput) {
      nextDateBtn.addEventListener("click", () => {
        const cur = new Date(dateInput.value);
        cur.setDate(cur.getDate() + 1);
        dateInput.value = cur.toISOString().split("T")[0];
        dateInput.dispatchEvent(new Event("change"));
      });
    }

    async function loadMarketSentiment(tradeDate) {
      if (!tradeDate) return;
      try {
        const data = await fetchJson(`/api/market/sentiment?trade_date=${encodeURIComponent(tradeDate)}`);
        STATE.marketSentiment = data;
        const textEl = document.getElementById("market-sentiment-text");
        if (!data.has_data) {
          if (textEl) textEl.textContent = "当日无全市场数据";
          return;
        }
        if (textEl) textEl.textContent = `${data.sentiment} (${data.up_ratio}%)`;
        const upEl = document.getElementById("market-up-count");
        if (upEl) upEl.textContent = `↑ ${data.up_count}`;
        const downEl = document.getElementById("market-down-count");
        if (downEl) downEl.textContent = `↓ ${data.down_count}`;
        const lUpEl = document.getElementById("market-limit-up");
        if (lUpEl) lUpEl.textContent = `${data.limit_up_count}`;
        const lDownEl = document.getElementById("market-limit-down");
        if (lDownEl) lDownEl.textContent = `${data.limit_down_count}`;
        const avgEl = document.getElementById("market-avg-change");
        if (avgEl) {
          avgEl.textContent = formatPct(data.avg_change_pct, true);
          avgEl.className = `regime-stat-value tnum ${getSignClass(data.avg_change_pct)}`;
        }
        const barFill = document.getElementById("market-breadth-fill");
        if (barFill) barFill.style.width = `${data.up_ratio}%`;
      } catch (_) {}
    }

    if (runBtn) {
      runBtn.addEventListener("click", async () => {
        const strategy_name = stratSelect ? stratSelect.value : "";
        const trade_date = dateInput ? dateInput.value : "";
        if (!strategy_name || !trade_date) {
          alert("请选择策略和交易日");
          return;
        }

        runBtn.disabled = true;
        const origHtml = runBtn.innerHTML;
        runBtn.innerHTML = "<span>⏳</span> 扫描计算中...";

        try {
          await fetchJson("/api/select", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ strategy_name, trade_date }),
          });

          const job = await waitForJob();

          runBtn.disabled = false;
          runBtn.innerHTML = origHtml;

          if (!job || job.status === "failed") {
            alert(`选股失败: ${(job && job.error) || "未知错误"}`);
            return;
          }

          const res = job.result || {};
          STATE.selectGrid.candidates = res.candidates || [];
          renderSelectTable();
        } catch (err) {
          alert(`发起选股失败: ${err.message}`);
          runBtn.disabled = false;
          runBtn.innerHTML = origHtml;
        }
      });
    }

    function renderSelectTable() {
      const q = STATE.selectGrid.searchQuery.trim().toLowerCase();
      const rows = STATE.selectGrid.candidates.filter((c) => {
        if (!q) return true;
        return (c.symbol && c.symbol.toLowerCase().includes(q)) || (c.name && c.name.toLowerCase().includes(q));
      });

      const badge = document.getElementById("select-count-badge");
      if (badge) badge.textContent = `${rows.length} 只标的`;

      const tbody = document.querySelector("#select-table tbody");
      if (!tbody) return;

      if (rows.length === 0) {
        tbody.innerHTML = `<tr><td colspan="11" style="text-align:center; color:var(--text-dim); padding:24px;">未找到符合条件的候选标的</td></tr>`;
        return;
      }

      tbody.innerHTML = rows
        .map((c, idx) => {
          const compScore = Number(c.composite_score || 0);
          const scoreBarPct = Math.min(100, Math.max(0, compScore));
          const isFalseBreak = c.false_break_occurred === true || c.false_break_occurred === "true";
          const centerDt = c.d0_date || (dateInput ? dateInput.value : "");

          return `
            <tr>
              <td class="tnum dim">${idx + 1}</td>
              <td class="tnum"><strong>${c.symbol}</strong></td>
              <td><span class="stock-clickable" data-symbol="${c.symbol}" data-center="${centerDt}">${c.name || "--"}</span></td>
              <td class="tnum dim">${c.d0_date || "--"}</td>
              <td>
                <div style="display:flex; align-items:center; gap:8px;">
                  <strong class="tnum ${compScore >= 60 ? "pos" : "neutral"}">${formatNum(compScore, 2)}</strong>
                  <div style="width:60px; height:5px; background:var(--bg-subtle); border-radius:3px; overflow:hidden;">
                    <div style="width:${scoreBarPct}%; height:100%; background:var(--accent-primary);"></div>
                  </div>
                </div>
              </td>
              <td class="tnum dim">${formatNum(c.pool_score_v3, 2)}</td>
              <td><span class="tag-badge bg-neutral">${c.branch_type || "首选"}</span></td>
              <td class="tnum"><span class="tag-badge ${c.board_count > 1 ? "bg-pos" : "bg-neutral"}">${c.board_count || 1} 连板</span></td>
              <td class="tnum dim" style="font-size:11px;">${c.first_limit_date || "-"} / ${c.last_limit_date || "-"}</td>
              <td>
                ${
                  isFalseBreak
                    ? `<span class="tag-badge bg-amber">⚠️ 假突破风险</span>`
                    : `<span class="tag-badge bg-neg">正常</span>`
                }
              </td>
              <td>
                <button type="button" class="btn btn-secondary btn-sm stock-kline-btn" data-symbol="${c.symbol}" data-center="${centerDt}">
                  📈 穿透
                </button>
              </td>
            </tr>
          `;
        })
        .join("");

      tbody.querySelectorAll(".stock-clickable, .stock-kline-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
          const sym = btn.getAttribute("data-symbol");
          const center = btn.getAttribute("data-center");
          openKlineDrawer(sym, center);
        });
      });
    }

    if (searchInput) {
      searchInput.addEventListener("input", (e) => {
        STATE.selectGrid.searchQuery = e.target.value;
        renderSelectTable();
      });
    }

    if (exportBtn) {
      exportBtn.addEventListener("click", () => {
        if (!STATE.selectGrid.candidates.length) {
          alert("无选股结果可导出");
          return;
        }
        exportToCsv("select_candidates.csv", STATE.selectGrid.candidates);
      });
    }
  }

  // =========================================================
  // PAGE 3: STRATEGY BACKTEST WORKBENCH (/backtests)
  // =========================================================
  async function loadBacktestPage() {
    const stratSelect = document.getElementById("backtest-strategy") || document.getElementById("bt-strategy");
    const runsSelect = document.getElementById("completed-runs") || document.getElementById("backtest-runs") || document.getElementById("bt-runs");
    const startInput = document.getElementById("backtest-start") || document.getElementById("bt-start");
    const endInput = document.getElementById("backtest-end") || document.getElementById("bt-end");
    const runBtn = document.getElementById("run-backtest-btn") || document.getElementById("backtest-run-btn") || document.getElementById("bt-run");
    const runBtnText = document.getElementById("run-btn-text") || document.getElementById("backtest-btn-text");
    const contIndicator = document.getElementById("continuation-indicator");

    function resizeAndRefreshCharts() {
      Object.values(STATE.charts).forEach((c) => {
        if (c && typeof c.resize === "function") {
          try {
            c.resize();
          } catch (_) {}
        }
      });
    }
    window.addEventListener("resize", resizeAndRefreshCharts);

    // Init tab navigation (support data-target and data-tab)
    document.querySelectorAll(".chart-tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".chart-tab-btn").forEach((b) => b.classList.remove("active"));
        document.querySelectorAll(".chart-pane, .chart-panel").forEach((p) => p.classList.remove("active"));
        btn.classList.add("active");
        const targetId = btn.getAttribute("data-target") || `tab-panel-${btn.getAttribute("data-tab")}`;
        const panel = document.getElementById(targetId);
        if (panel) panel.classList.add("active");
        setTimeout(resizeAndRefreshCharts, 50);
      });
    });

    // Quick range buttons
    document.querySelectorAll(".btn-range").forEach((btn) => {
      btn.addEventListener("click", () => {
        const range = btn.getAttribute("data-range");
        if (!endInput) return;
        const endDateStr = endInput.value || "2026-09-17";
        const endD = new Date(endDateStr);
        let startD = new Date(endD);
        if (range === "1m") {
          startD.setMonth(startD.getMonth() - 1);
        } else if (range === "3m") {
          startD.setMonth(startD.getMonth() - 3);
        } else if (range === "6m") {
          startD.setMonth(startD.getMonth() - 6);
        } else if (range === "1y") {
          startD.setFullYear(startD.getFullYear() - 1);
        } else if (range === "all") {
          startD = new Date("2020-01-01");
        }
        if (startInput) {
          startInput.value = startD.toISOString().slice(0, 10);
          checkContinuation();
        }
      });
    });

    // Load registered strategies
    try {
      const strats = await fetchJson("/api/strategies");
      STATE.strategies = strats;
      if (stratSelect) {
        stratSelect.innerHTML = strats
          .map((s) => `<option value="${s.name}">${s.display_name || s.name}</option>`)
          .join("");

        if (strats.length > 0) {
          const curBadge = document.getElementById("current-strategy-badge");
          if (curBadge) curBadge.textContent = strats[0].display_name || strats[0].name;
          await loadRunsForStrategy(strats[0].name);
        }
      }
    } catch (err) {
      console.error("Failed to load strategies:", err);
    }

    // Init default dates from status
    try {
      const st = await fetchJson("/api/data/status");
      const latestDate = st.daily_bar_max_date || st.latest_daily_bar_date;
      if (endInput) endInput.value = latestDate || "2026-09-17";
    } catch (_) {
      if (endInput) endInput.value = "2026-09-17";
    }
    if (startInput) startInput.value = "2025-12-31";

    // Multi-Run Comparison and Tweaker controls
    const compareSelect = document.getElementById("compare-runs");
    const compareCard = document.getElementById("run-compare-card");
    const closeCompareBtn = document.getElementById("close-compare-btn");
    const tweakerBtn = document.getElementById("toggle-tweaker-btn");
    const tweakerPanel = document.getElementById("parameter-tweaker-panel");
    const resetParamsBtn = document.getElementById("reset-params-btn");
    const runCustomBtn = document.getElementById("run-custom-btn");

    if (tweakerBtn && tweakerPanel) {
      tweakerBtn.addEventListener("click", () => {
        const isHidden = tweakerPanel.style.display === "none" || !tweakerPanel.style.display;
        tweakerPanel.style.display = isHidden ? "block" : "none";
      });
    }

    async function loadStrategyDefaultParams(stratName) {
      if (!stratName) return;
      try {
        const p = await fetchJson(`/api/strategy-params?strategy_name=${encodeURIComponent(stratName)}`);
        const sb = p.strategy_backtest || {};
        const setVal = (id, val) => {
          const el = document.getElementById(id);
          if (el && val !== undefined && val !== null) el.value = val;
        };
        setVal("param-holding-period", sb.holding_period ?? 1);
        setVal("param-take-profit", sb.take_profit_pct ?? 10);
        setVal("param-stop-loss", sb.stop_loss_pct ?? -5);
        setVal("param-min-score", sb.min_composite_score ?? 80);
        setVal("param-max-daily-buys", sb.max_daily_buys ?? 1);
        setVal("param-max-positions", sb.max_positions ?? 1);
        setVal("param-position-size", sb.position_size_pct ?? 50);
        setVal("param-market-filter", sb.market_filter_enabled ? "true" : "false");
      } catch (_) {}
    }

    if (resetParamsBtn && stratSelect) {
      resetParamsBtn.addEventListener("click", () => {
        loadStrategyDefaultParams(stratSelect.value);
      });
    }

    if (stratSelect) {
      stratSelect.addEventListener("change", async () => {
        const sel = stratSelect.options[stratSelect.selectedIndex];
        const curBadge = document.getElementById("current-strategy-badge");
        if (curBadge) curBadge.textContent = sel ? sel.text : "";
        await loadRunsForStrategy(stratSelect.value);
        await loadStrategyDefaultParams(stratSelect.value);
        await checkContinuation();
      });
    }

    if (startInput) startInput.addEventListener("change", checkContinuation);
    if (endInput) endInput.addEventListener("change", checkContinuation);

    async function loadRunsForStrategy(strategyName) {
      if (!runsSelect || !strategyName) return;
      try {
        const runs = await fetchJson(`/api/backtest-runs?strategy_name=${encodeURIComponent(strategyName)}`);
        runsSelect.innerHTML = '<option value="">选择历史试验记录...</option>';
        if (compareSelect) compareSelect.innerHTML = '<option value="">(无对比 - 单线模式)</option>';

        runs.forEach((r) => {
          const ret = r.total_return_pct !== null && r.total_return_pct !== undefined ? Number(r.total_return_pct) : null;
          const retText = ret !== null ? `收益: ${formatPct(ret, true)}` : "";
          const scoreText = r.score !== null && r.score !== undefined ? `分: ${formatNum(r.score, 2)}` : "";
          const metaText = [retText, scoreText].filter(Boolean).join(" | ");
          const optHtml = `<option value="${r.run_id}">Run #${r.run_id} [${r.start_date} ~ ${r.end_date}] ${metaText ? `(${metaText})` : ""}</option>`;
          runsSelect.innerHTML += optHtml;
          if (compareSelect) compareSelect.innerHTML += optHtml;
        });

        if (runs.length > 0) {
          runsSelect.value = runs[0].run_id;
          if (startInput && runs[0].start_date) startInput.value = runs[0].start_date;
          if (endInput && runs[0].end_date) endInput.value = runs[0].end_date;
          await loadBacktestRun(runs[0].run_id);
        } else {
          runsSelect.innerHTML = '<option value="">暂无历史试验记录</option>';
        }
      } catch (err) {
        console.error("Failed to load runs:", err);
      }
    }

    if (runsSelect) {
      runsSelect.addEventListener("change", () => {
        if (runsSelect.value) {
          loadBacktestRun(runsSelect.value);
          if (compareSelect && compareSelect.value) {
            compareSelect.dispatchEvent(new Event("change"));
          }
        }
      });
    }

    if (compareSelect) {
      compareSelect.addEventListener("change", async () => {
        const compId = compareSelect.value;
        if (!compId) {
          STATE.compareBacktestPayload = null;
          if (compareCard) compareCard.style.display = "none";
          if (STATE.latestBacktestPayload) renderBacktestCharts(STATE.latestBacktestPayload);
          return;
        }
        try {
          const compPayload = await fetchJson(`/api/backtests/${compId}`);
          STATE.compareBacktestPayload = compPayload;
          renderDiffCard(STATE.latestBacktestPayload, compPayload);
          if (STATE.latestBacktestPayload) renderBacktestCharts(STATE.latestBacktestPayload);
        } catch (err) {
          console.error("Failed to load compare run:", err);
        }
      });
    }

    if (closeCompareBtn) {
      closeCompareBtn.addEventListener("click", () => {
        if (compareSelect) compareSelect.value = "";
        STATE.compareBacktestPayload = null;
        if (compareCard) compareCard.style.display = "none";
        if (STATE.latestBacktestPayload) renderBacktestCharts(STATE.latestBacktestPayload);
      });
    }

    function renderDiffCard(base, comp) {
      const card = document.getElementById("run-compare-card");
      const grid = document.getElementById("diff-metrics-grid");
      const baseTag = document.getElementById("diff-base-tag");
      const compTag = document.getElementById("diff-comp-tag");
      if (!card || !grid || !base || !comp) return;

      if (baseTag) baseTag.textContent = `主实验 #${base.run_id}`;
      if (compTag) compTag.textContent = `对比实验 #${comp.run_id}`;

      const bm = base.metrics || {};
      const cm = comp.metrics || {};

      const retDiff = (Number(cm.total_return_pct || 0) - Number(bm.total_return_pct || 0)).toFixed(2);
      const annDiff = (Number(cm.annualized_return_pct || 0) - Number(bm.annualized_return_pct || 0)).toFixed(2);
      const mddDiff = (Math.abs(Number(bm.max_drawdown_pct || 0)) - Math.abs(Number(cm.max_drawdown_pct || 0))).toFixed(2);
      const sharpeDiff = (Number(cm.sharpe_ratio || 0) - Number(bm.sharpe_ratio || 0)).toFixed(2);
      const winDiff = (Number(cm.win_rate_pct || 0) - Number(bm.win_rate_pct || 0)).toFixed(1);

      const diffs = [
        {
          label: "累计收益率",
          baseVal: formatPct(bm.total_return_pct, true),
          compVal: formatPct(cm.total_return_pct, true),
          delta: (Number(retDiff) >= 0 ? "+" : "") + retDiff + "%",
          isPos: Number(retDiff) >= 0,
        },
        {
          label: "年化收益率",
          baseVal: formatPct(bm.annualized_return_pct, true),
          compVal: formatPct(cm.annualized_return_pct, true),
          delta: (Number(annDiff) >= 0 ? "+" : "") + annDiff + "%",
          isPos: Number(annDiff) >= 0,
        },
        {
          label: "最大回撤",
          baseVal: formatPct(bm.max_drawdown_pct ? -Math.abs(Number(bm.max_drawdown_pct)) : 0),
          compVal: formatPct(cm.max_drawdown_pct ? -Math.abs(Number(cm.max_drawdown_pct)) : 0),
          delta: (Number(mddDiff) >= 0 ? "+" : "") + mddDiff + "%",
          isPos: Number(mddDiff) >= 0,
        },
        {
          label: "夏普比率",
          baseVal: formatNum(bm.sharpe_ratio, 2),
          compVal: formatNum(cm.sharpe_ratio, 2),
          delta: (Number(sharpeDiff) >= 0 ? "+" : "") + sharpeDiff,
          isPos: Number(sharpeDiff) >= 0,
        },
        {
          label: "胜率",
          baseVal: formatPct(bm.win_rate_pct),
          compVal: formatPct(cm.win_rate_pct),
          delta: (Number(winDiff) >= 0 ? "+" : "") + winDiff + "%",
          isPos: Number(winDiff) >= 0,
        },
        {
          label: "交易笔数",
          baseVal: `${bm.closed_trades || 0} 笔`,
          compVal: `${cm.closed_trades || 0} 笔`,
          delta: `${(Number(cm.closed_trades || 0) - Number(bm.closed_trades || 0))} 笔`,
          isPos: true,
        },
      ];

      grid.innerHTML = diffs.map((d) => `
        <div class="diff-metric-box">
          <div class="diff-metric-label">${d.label}</div>
          <div class="diff-metric-val">
            <span style="font-size:12px; color:var(--text-muted);">${d.baseVal} ➔ ${d.compVal}</span>
            <span class="diff-delta ${d.isPos ? "pos" : "neg"}">${d.delta}</span>
          </div>
        </div>
      `).join("");

      card.style.display = "block";
    }

    async function checkContinuation() {
      if (!stratSelect || !startInput || !contIndicator) return;
      const strategyName = stratSelect.value;
      const startDate = startInput.value;
      if (!strategyName || !startDate) return;

      try {
        const res = await fetchJson(`/api/backtests/continuation?strategy_name=${encodeURIComponent(strategyName)}&start_date=${encodeURIComponent(startDate)}`);
        if (res.can_continue) {
          contIndicator.style.display = "inline-flex";
          contIndicator.textContent = `⚡ 可接续 Run #${res.run_id} (自 ${res.last_end_date})`;
          contIndicator.className = "tag-badge bg-amber";
          if (runBtnText) runBtnText.textContent = "增量追加回测";
        } else {
          contIndicator.style.display = "none";
          if (runBtnText) runBtnText.textContent = "执行回测";
        }
      } catch (_) {
        contIndicator.style.display = "none";
        if (runBtnText) runBtnText.textContent = "执行回测";
      }
    }

    const toggleBm = document.getElementById("toggle-benchmark-chk");
    if (toggleBm) {
      toggleBm.addEventListener("change", () => {
        if (STATE.latestBacktestPayload) {
          renderBacktestCharts(STATE.latestBacktestPayload);
        }
      });
    }

    async function executeBacktest(isCustom = false) {
      const strategy_name = stratSelect ? stratSelect.value : "";
      const start_date = startInput ? startInput.value : "";
      const end_date = endInput ? endInput.value : "";
      if (!strategy_name || !start_date || !end_date) {
        alert("请填写完整策略与回测起止日期");
        return;
      }

      let parameter_overrides = null;
      if (isCustom) {
        parameter_overrides = {
          "strategy_backtest.holding_period": parseInt(document.getElementById("param-holding-period")?.value || "1", 10),
          "strategy_backtest.take_profit_pct": parseFloat(document.getElementById("param-take-profit")?.value || "10"),
          "strategy_backtest.stop_loss_pct": parseFloat(document.getElementById("param-stop-loss")?.value || "-5"),
          "strategy_backtest.min_composite_score": parseFloat(document.getElementById("param-min-score")?.value || "80"),
          "strategy_backtest.max_daily_buys": parseInt(document.getElementById("param-max-daily-buys")?.value || "1", 10),
          "strategy_backtest.max_positions": parseInt(document.getElementById("param-max-positions")?.value || "1", 10),
          "strategy_backtest.position_size_pct": parseFloat(document.getElementById("param-position-size")?.value || "50"),
          "strategy_backtest.market_filter.enabled": document.getElementById("param-market-filter")?.value === "true",
        };
      }

      runBtn.disabled = true;
      if (runCustomBtn) runCustomBtn.disabled = true;
      if (runBtnText) runBtnText.textContent = "计算中...";
      const progressBanner = document.getElementById("backtest-progress-banner");
      if (progressBanner) progressBanner.style.display = "flex";

      try {
        await fetchJson("/api/backtests/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ strategy_name, start_date, end_date, parameter_overrides }),
        });

        const job = await waitForJob((job) => {
          if (!job) return;
          if (job.progress) {
            const pct = job.progress.percentage || 0;
            const pb = document.getElementById("stream-progress-bar");
            if (pb) pb.style.width = `${pct}%`;
            const curDate = job.progress.current_trade_day;
            const totalAsset = job.progress.current_total_assets;
            if (curDate) {
              const dEl = document.getElementById("stream-current-date");
              if (dEl) dEl.textContent = curDate;
            }
            if (totalAsset) {
              const aEl = document.getElementById("stream-current-asset");
              if (aEl) aEl.textContent = `¥${formatNum(totalAsset)}`;
            }
          }
        });

        runBtn.disabled = false;
        if (runCustomBtn) runCustomBtn.disabled = false;
        if (runBtnText) runBtnText.textContent = "执行回测";
        if (progressBanner) progressBanner.style.display = "none";

        if (job && job.status === "succeeded" && job.result && job.result.run_id) {
          await loadRunsForStrategy(stratSelect.value);
          await loadBacktestRun(job.result.run_id);
        } else {
          alert(`回测失败: ${(job && job.error) || "未知错误"}`);
        }
      } catch (err) {
        alert(`发起回测失败: ${err.message}`);
        runBtn.disabled = false;
        if (runCustomBtn) runCustomBtn.disabled = false;
        if (runBtnText) runBtnText.textContent = "执行回测";
        if (progressBanner) progressBanner.style.display = "none";
      }
    }

    if (runBtn) {
      runBtn.addEventListener("click", () => executeBacktest(false));
    }
    if (runCustomBtn) {
      runCustomBtn.addEventListener("click", () => executeBacktest(true));
    }

    if (stratSelect) {
      await loadRunsForStrategy(stratSelect.value);
      await loadStrategyDefaultParams(stratSelect.value);
    }
    await checkContinuation();
    initTradesGrid();
  }

  async function loadBacktestRun(runId) {
    try {
      const payload = await fetchJson(`/api/backtests/${runId}`);
      STATE.latestBacktestPayload = payload;

      const dateBadge = document.getElementById("current-date-range-badge");
      if (dateBadge) dateBadge.textContent = `${payload.start_date} ~ ${payload.end_date}`;

      renderHeroCards(payload);
      renderSecondaryFactsheet(payload);
      renderBacktestCharts(payload);
      renderMonthlyHeatmap(payload);
      renderYearlyMetricsTable(payload);
      renderParamsSnapshot(payload);
      renderOpenPositionsTable(payload);

      STATE.tradesGrid.allTrades = payload.trades || [];
      STATE.tradesGrid.filteredTrades = payload.trades || [];
      STATE.tradesGrid.currentPage = 1;
      applyTradesFilterAndRender();
    } catch (err) {
      console.error(`Failed to load run #${runId}:`, err);
    }
  }

  // --- Render 4 Hero KPI Cards ---
  function renderHeroCards(payload) {
    const m = payload.metrics || {};
    const totRet = m.total_return_pct;
    const annRet = m.annualized_return_pct;
    const sharpe = m.sharpe_ratio;
    const mdd = m.max_drawdown_pct;
    const sm = payload.summary || payload.parameter_snapshot?.summary || {};

    // Total Return
    const totEl = document.getElementById("kpi-total-return");
    if (totEl) {
      totEl.textContent = formatPct(totRet, true);
      totEl.className = `hero-val tnum ${getSignClass(totRet)}`;
    }
    const totBadge = document.getElementById("kpi-total-return-badge");
    if (totBadge) {
      totBadge.textContent = Number(totRet) >= 0 ? "盈利" : "亏损";
      totBadge.className = `hero-badge ${Number(totRet) >= 0 ? "bg-pos" : "bg-neg"} tnum`;
    }

    const curTotalAssets = sm.current_total_assets || (payload.equity?.length ? payload.equity[payload.equity.length - 1].total_assets : null);
    const initCapital = sm.initial_capital || payload.parameter_snapshot?.request?.initial_capital || payload.parameter_snapshot?.strategy_backtest?.initial_capital || 100000;
    const assetsEl = document.getElementById("kpi-total-assets");
    if (assetsEl) assetsEl.textContent = `¥${formatNum(curTotalAssets)} (本金 ¥${formatNum(initCapital, 0)})`;

    // Annualized Return
    const annEl = document.getElementById("kpi-annualized-return");
    if (annEl) {
      annEl.textContent = formatPct(annRet, true);
      annEl.className = `hero-val tnum ${getSignClass(annRet)}`;
    }
    const annBadge = document.getElementById("kpi-annual-badge");
    if (annBadge) {
      annBadge.className = `hero-badge ${Number(annRet) >= 0 ? "bg-pos" : "bg-neg"} tnum`;
    }

    let alphaText = "超额未计算";
    if (payload.benchmark && payload.benchmark.length > 0) {
      const bm = payload.benchmark;
      const bmRet = ((bm[bm.length - 1].close - bm[0].close) / bm[0].close) * 100;
      const alpha = Number(totRet) - bmRet;
      alphaText = `${formatPct(alpha, true)} (HS300: ${formatPct(bmRet, true)})`;
    }
    const alphaEl = document.getElementById("kpi-alpha-val");
    if (alphaEl) alphaEl.textContent = alphaText;

    // Sharpe
    const sharpeEl = document.getElementById("kpi-sharpe-ratio");
    if (sharpeEl) {
      sharpeEl.textContent = formatNum(sharpe, 2);
      sharpeEl.className = `hero-val tnum ${Number(sharpe) >= 1.5 ? "pos" : "neutral"}`;
    }
    const sortinoEl = document.getElementById("kpi-sortino-ratio");
    if (sortinoEl) sortinoEl.textContent = formatNum(m.sortino_ratio, 2);

    const sharpeRating = document.getElementById("kpi-sharpe-rating");
    if (sharpeRating) {
      if (Number(sharpe) >= 2.0) {
        sharpeRating.textContent = "超额卓越";
        sharpeRating.className = "hero-badge bg-pos tnum";
      } else if (Number(sharpe) >= 1.0) {
        sharpeRating.textContent = "收益稳健";
        sharpeRating.className = "hero-badge bg-neutral tnum";
      } else {
        sharpeRating.textContent = "待优化";
        sharpeRating.className = "hero-badge bg-amber tnum";
      }
    }

    // Max Drawdown & Calmar
    const mddEl = document.getElementById("kpi-max-drawdown");
    if (mddEl) mddEl.textContent = formatPct(mdd ? -Math.abs(Number(mdd)) : 0);
    const calmarEl = document.getElementById("kpi-calmar-ratio");
    if (calmarEl) calmarEl.textContent = formatNum(m.calmar_ratio, 2);
  }

  // --- Render Secondary Quant Factsheet ---
  function renderSecondaryFactsheet(payload) {
    const m = payload.metrics || {};
    const sm = payload.summary || payload.parameter_snapshot?.summary || {};
    const winCount = (payload.trades || []).filter((t) => Number(t.return_pct) > 0).length;
    const lossCount = (payload.trades || []).filter((t) => Number(t.return_pct) < 0).length;

    const setT = (id, txt) => {
      const el = document.getElementById(id);
      if (el) el.textContent = txt;
    };

    setT("fact-win-rate", formatPct(m.win_rate_pct));
    setT("fact-win-loss-count", `${winCount} 胜 / ${lossCount} 负`);
    setT("fact-profit-factor", formatNum(m.profit_factor, 2));
    setT("fact-volatility", formatPct(m.volatility_pct));

    const avgRetEl = document.getElementById("fact-avg-return");
    if (avgRetEl) {
      avgRetEl.textContent = formatPct(m.average_trade_return_pct, true);
      avgRetEl.className = `sec-val tnum ${getSignClass(m.average_trade_return_pct)}`;
    }

    setT("fact-median-return", formatPct(m.median_trade_return_pct, true));
    setT("fact-best-trade", formatPct(m.best_trade_return_pct, true));
    setT("fact-worst-trade", formatPct(m.worst_trade_return_pct, true));

    const realizedProfit = sm.realized_profit;
    const unrealizedProfit = sm.unrealized_profit;
    const costs = m.total_transaction_costs || sm.total_transaction_costs || 0;

    const relEl = document.getElementById("fact-realized-pnl");
    if (relEl) {
      relEl.textContent = `¥${formatNum(realizedProfit)}`;
      relEl.className = `sec-val tnum ${getSignClass(realizedProfit)}`;
    }
    setT("fact-unrealized-pnl", `¥${formatNum(unrealizedProfit)}`);
    setT("fact-costs", `¥${formatNum(costs)}`);
    setT("fact-closed-trades", `${m.closed_trades || (payload.trades || []).length} 笔`);
    setT("fact-open-count", `${(payload.open_positions || []).length} 只`);
  }

  // --- Render Backtest ECharts ---
  function renderBacktestCharts(payload) {
    const isDark = STATE.theme === "dark";
    const equityData = payload.equity || [];
    const drawdownData = payload.drawdown || [];
    const benchmarkData = payload.benchmark || [];
    const dates = equityData.map((d) => d.trade_date);
    const totalAssets = equityData.map((d) => Number(d.total_assets));
    const initCapital = payload.parameter_snapshot ? Number(payload.parameter_snapshot.initial_capital || 100000) : 100000;

    // 1. Equity vs Benchmark Chart
    const eqContainer = document.getElementById("equity-chart");
    if (eqContainer) {
      if (!STATE.charts.equity) STATE.charts.equity = echarts.init(eqContainer);
      const eqChart = STATE.charts.equity;

      const showBm = document.getElementById("toggle-benchmark-chk")?.checked && benchmarkData.length > 0;
      let scaledBenchmark = [];
      if (showBm) {
        const bmMap = new Map();
        benchmarkData.forEach((b) => bmMap.set(b.trade_date, b.close));
        const firstClose = benchmarkData[0].close;
        let lastVal = initCapital;
        scaledBenchmark = dates.map((d) => {
          if (bmMap.has(d)) {
            lastVal = (bmMap.get(d) / firstClose) * initCapital;
          }
          return Math.round(lastVal * 100) / 100;
        });
      }

      const eqSeries = [
        {
          name: "策略总资产",
          type: "line",
          data: totalAssets,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2.5, color: "#0284c7" },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: "rgba(2, 132, 199, 0.35)" },
              { offset: 1, color: "rgba(2, 132, 199, 0.01)" },
            ]),
          },
          markLine: {
            symbol: "none",
            lineStyle: { type: "dashed", color: isDark ? "rgba(255,255,255,0.3)" : "#94a3b8" },
            data: [{ yAxis: initCapital, name: "初始本金" }],
          },
        },
      ];

      if (showBm) {
        eqSeries.push({
          name: "基准 (沪深300归一)",
          type: "line",
          data: scaledBenchmark,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 1.5, type: "dashed", color: "#94a3b8" },
        });
      }

      if (STATE.compareBacktestPayload) {
        const compDaily = STATE.compareBacktestPayload.daily_metrics || STATE.compareBacktestPayload.curve || [];
        const compMap = new Map();
        compDaily.forEach((d) => compMap.set(d.trade_date, d.total_assets));
        const compSeriesData = dates.map((d) => compMap.get(d) ?? null);
        const compRunId = STATE.compareBacktestPayload.run_id;
        eqSeries.push({
          name: `对比实验 #${compRunId}`,
          type: "line",
          data: compSeriesData,
          smooth: true,
          showSymbol: false,
          lineStyle: { width: 2.2, type: "dashed", color: "#f59e0b" },
        });
      }

      eqChart.setOption(
        {
          backgroundColor: "transparent",
          tooltip: {
            trigger: "axis",
            backgroundColor: isDark ? "rgba(17, 24, 39, 0.95)" : "rgba(255, 255, 255, 0.95)",
            borderColor: isDark ? "rgba(255, 255, 255, 0.15)" : "#cbd5e1",
            textStyle: { color: isDark ? "#f8fafc" : "#0f172a", fontSize: 12 },
            formatter: (params) => {
              let text = `<div style="font-weight:700; margin-bottom:4px;">${params[0].axisValue}</div>`;
              params.forEach((p) => {
                if (p.value === null || p.value === undefined) return;
                const retPct = (((p.value - initCapital) / initCapital) * 100).toFixed(2);
                text += `<div>${p.marker} ${p.seriesName}: <strong>¥${Number(p.value).toLocaleString()}</strong> (${retPct >= 0 ? "+" : ""}${retPct}%)</div>`;
              });
              return text;
            },
          },
          legend: {
            data: eqSeries.map((s) => s.name),
            textStyle: { color: isDark ? "#94a3b8" : "#475569" },
            top: 6,
          },
          grid: { left: 60, right: 30, top: 46, bottom: 40 },
          xAxis: {
            type: "category",
            data: dates,
            axisLine: { lineStyle: { color: isDark ? "rgba(255,255,255,0.15)" : "#cbd5e1" } },
            axisLabel: { color: isDark ? "#94a3b8" : "#64748b", fontSize: 11 },
          },
          yAxis: {
            type: "value",
            scale: true,
            splitLine: { lineStyle: { color: isDark ? "rgba(255,255,255,0.06)" : "#f1f5f9" } },
            axisLabel: {
              color: isDark ? "#94a3b8" : "#64748b",
              formatter: (v) => "¥" + (v >= 10000 ? (v / 10000).toFixed(1) + "w" : v),
            },
          },
          dataZoom: [{ type: "inside" }, { type: "slider", bottom: 4, height: 16 }],
          series: eqSeries,
        },
        true
      );
    }

    // 2. Drawdown Chart
    const ddContainer = document.getElementById("drawdown-chart");
    if (ddContainer) {
      if (!STATE.charts.drawdown) STATE.charts.drawdown = echarts.init(ddContainer);
      const ddChart = STATE.charts.drawdown;
      const ddValues = drawdownData.map((d) => -Math.abs(Number(d.drawdown_pct || 0)));

      ddChart.setOption(
        {
          backgroundColor: "transparent",
          tooltip: {
            trigger: "axis",
            backgroundColor: isDark ? "rgba(17, 24, 39, 0.95)" : "rgba(255, 255, 255, 0.95)",
            borderColor: isDark ? "rgba(255, 255, 255, 0.15)" : "#cbd5e1",
            textStyle: { color: isDark ? "#f8fafc" : "#0f172a", fontSize: 12 },
            formatter: "{b}<br />历史回撤深度: <strong style='color:#10b981;'>{c}%</strong>",
          },
          grid: { left: 60, right: 30, top: 20, bottom: 40 },
          xAxis: {
            type: "category",
            data: drawdownData.map((d) => d.trade_date),
            axisLine: { lineStyle: { color: isDark ? "rgba(255,255,255,0.15)" : "#cbd5e1" } },
            axisLabel: { color: isDark ? "#94a3b8" : "#64748b", fontSize: 11 },
          },
          yAxis: {
            type: "value",
            max: 0,
            splitLine: { lineStyle: { color: isDark ? "rgba(255,255,255,0.06)" : "#f1f5f9" } },
            axisLabel: { color: isDark ? "#94a3b8" : "#64748b", formatter: "{value}%" },
          },
          dataZoom: [{ type: "inside" }, { type: "slider", bottom: 4, height: 16 }],
          series: [
            {
              name: "回撤深度",
              type: "line",
              data: ddValues,
              smooth: true,
              showSymbol: false,
              lineStyle: { width: 1.8, color: "#10b981" },
              areaStyle: {
                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                  { offset: 0, color: "rgba(16, 185, 129, 0.05)" },
                  { offset: 1, color: "rgba(16, 185, 129, 0.35)" },
                ]),
              },
            },
          ],
        },
        true
      );
    }

    // 3. Trade Returns Chart
    const trContainer = document.getElementById("trade-returns-chart");
    if (trContainer) {
      if (!STATE.charts.trades) STATE.charts.trades = echarts.init(trContainer);
      const trChart = STATE.charts.trades;
      const trades = (payload.trades || []).slice().reverse();
      const returns = trades.map((t) => Number(t.return_pct || 0));

      trChart.setOption(
        {
          backgroundColor: "transparent",
          tooltip: {
            trigger: "axis",
            backgroundColor: isDark ? "rgba(17, 24, 39, 0.95)" : "rgba(255, 255, 255, 0.95)",
            borderColor: isDark ? "rgba(255, 255, 255, 0.15)" : "#cbd5e1",
            textStyle: { color: isDark ? "#f8fafc" : "#0f172a", fontSize: 12 },
            formatter: (params) => {
              const idx = params[0].dataIndex;
              const t = trades[idx];
              return `<strong>#${idx + 1} ${t.name || t.symbol}</strong><br/>买入: ${t.buy_date} (¥${t.buy_price})<br/>卖出: ${t.sell_date} (¥${t.sell_price})<br/>收益率: <strong>${formatPct(t.return_pct, true)}</strong><br/>原因: ${t.exit_reason || "-"}`;
            },
          },
          grid: { left: 60, right: 30, top: 20, bottom: 40 },
          xAxis: {
            type: "category",
            data: trades.map((t, i) => `#${i + 1}`),
            axisLabel: { color: isDark ? "#94a3b8" : "#64748b", fontSize: 10 },
          },
          yAxis: {
            type: "value",
            splitLine: { lineStyle: { color: isDark ? "rgba(255,255,255,0.06)" : "#f1f5f9" } },
            axisLabel: { color: isDark ? "#94a3b8" : "#64748b", formatter: "{value}%" },
          },
          dataZoom: [{ type: "inside" }, { type: "slider", bottom: 4, height: 16 }],
          series: [
            {
              name: "逐笔收益率",
              type: "bar",
              data: returns,
              itemStyle: {
                color: (p) => (p.value >= 0 ? "#f43f5e" : "#10b981"),
                borderRadius: [2, 2, 0, 0],
              },
            },
          ],
        },
        true
      );
    }
  }

  // --- Render Monthly Heatmap & Bar Chart ---
  function renderMonthlyHeatmap(payload) {
    const wrap = document.getElementById("monthly-heatmap-wrap");
    const barContainer = document.getElementById("monthly-chart");
    if (!wrap) return;

    const equity = payload.equity || [];
    if (!equity.length) {
      wrap.innerHTML = `<div style="color:var(--text-dim); text-align:center; padding:24px;">无月度数据</div>`;
      return;
    }

    const monthBuckets = {};
    equity.forEach((row) => {
      const ym = row.trade_date.slice(0, 7);
      const val = Number(row.total_assets);
      if (!monthBuckets[ym]) {
        monthBuckets[ym] = { first: val, last: val };
      } else {
        monthBuckets[ym].last = val;
      }
    });

    const yearsSet = new Set();
    const monthlyMap = {};
    const barLabels = [];
    const barValues = [];

    Object.keys(monthBuckets)
      .sort()
      .forEach((ym) => {
        const [y, m] = ym.split("-");
        const monthNum = parseInt(m, 10);
        const { first, last } = monthBuckets[ym];
        const ret = ((last - first) / first) * 100;
        yearsSet.add(y);
        if (!monthlyMap[y]) monthlyMap[y] = {};
        monthlyMap[y][monthNum] = ret;
        barLabels.push(ym);
        barValues.push(Number(ret.toFixed(2)));
      });

    const years = Array.from(yearsSet).sort();

    let tableHtml = `
      <table class="heatmap-table tnum">
        <thead>
          <tr>
            <th>年份</th>
            <th>1月</th><th>2月</th><th>3月</th><th>4月</th><th>5月</th><th>6月</th>
            <th>7月</th><th>8月</th><th>9月</th><th>10月</th><th>11月</th><th>12月</th>
            <th>全年累计</th>
          </tr>
        </thead>
        <tbody>
    `;

    years.forEach((yr) => {
      tableHtml += `<tr><td style="font-weight:700; color:var(--text-main);">${yr}</td>`;
      let ytdMul = 1.0;
      for (let m = 1; m <= 12; m++) {
        if (monthlyMap[yr] && monthlyMap[yr][m] !== undefined) {
          const ret = monthlyMap[yr][m];
          ytdMul *= 1 + ret / 100;
          const bgStyle =
            ret > 0
              ? `background: rgba(244, 63, 94, ${Math.min(0.85, 0.15 + ret * 0.05)}); color: #fff;`
              : ret < 0
              ? `background: rgba(16, 185, 129, ${Math.min(0.85, 0.15 + Math.abs(ret) * 0.05)}); color: #fff;`
              : `background: var(--bg-subtle); color: var(--text-dim);`;
          tableHtml += `<td style="${bgStyle}">${formatPct(ret, true)}</td>`;
        } else {
          tableHtml += `<td class="heatmap-cell-empty">-</td>`;
        }
      }
      const ytdRet = (ytdMul - 1) * 100;
      const ytdColor = ytdRet >= 0 ? "pos" : "neg";
      tableHtml += `<td style="font-weight:800;" class="${ytdColor}">${formatPct(ytdRet, true)}</td></tr>`;
    });

    tableHtml += `</tbody></table>`;
    wrap.innerHTML = tableHtml;

    if (barContainer) {
      if (!STATE.charts.monthly) STATE.charts.monthly = echarts.init(barContainer);
      const mChart = STATE.charts.monthly;
      const isDark = STATE.theme === "dark";

      mChart.setOption(
        {
          backgroundColor: "transparent",
          title: {
            text: "月度收益率分布柱状图",
            textStyle: { fontSize: 12, color: isDark ? "#94a3b8" : "#475569" },
            left: 0,
          },
          tooltip: {
            trigger: "axis",
            backgroundColor: isDark ? "rgba(17, 24, 39, 0.95)" : "rgba(255, 255, 255, 0.95)",
            borderColor: isDark ? "rgba(255, 255, 255, 0.15)" : "#cbd5e1",
            textStyle: { color: isDark ? "#f8fafc" : "#0f172a" },
            formatter: "{b}: <strong>{c}%</strong>",
          },
          grid: { left: 50, right: 20, top: 40, bottom: 25 },
          xAxis: {
            type: "category",
            data: barLabels,
            axisLabel: { color: isDark ? "#94a3b8" : "#64748b", fontSize: 10 },
          },
          yAxis: {
            type: "value",
            splitLine: { lineStyle: { color: isDark ? "rgba(255,255,255,0.06)" : "#f1f5f9" } },
            axisLabel: { color: isDark ? "#94a3b8" : "#64748b", formatter: "{value}%" },
          },
          series: [
            {
              type: "bar",
              data: barValues,
              itemStyle: {
                color: (p) => (p.value >= 0 ? "#f43f5e" : "#10b981"),
                borderRadius: [2, 2, 0, 0],
              },
            },
          ],
        },
        true
      );
    }
  }

  // --- Render Yearly Metrics ---
  function renderYearlyMetricsTable(payload) {
    const tbody = document.getElementById("yearly-metrics-body");
    if (!tbody) return;
    const ym = payload.yearly_metrics || {};
    const years = Object.keys(ym).sort();

    if (!years.length) {
      tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color:var(--text-dim); padding:16px;">当前回测跨度无分年度记录</td></tr>`;
      return;
    }

    tbody.innerHTML = years
      .map((y) => {
        const item = ym[y] || {};
        const ret = Number(item.total_return_pct || item.annualized_return_pct || 0);
        return `
        <tr>
          <td class="tnum"><strong>${y}</strong></td>
          <td class="tnum ${getSignClass(ret)}">${formatPct(ret, true)}</td>
          <td class="tnum neg">${formatPct(item.max_drawdown_pct ? -Math.abs(Number(item.max_drawdown_pct)) : 0)}</td>
          <td class="tnum">${formatNum(item.sharpe_ratio, 2)}</td>
          <td class="tnum">${item.trade_count || item.total_trades || "--"}</td>
          <td class="tnum">${formatPct(item.win_rate_pct)}</td>
        </tr>
      `;
      })
      .join("");
  }

  // --- Render Parameters Snapshot ---
  function renderParamsSnapshot(payload) {
    const container = document.getElementById("backtest-params");
    if (!container) return;
    const params = payload.parameter_snapshot || {};
    const keys = Object.keys(params);

    if (!keys.length) {
      container.innerHTML = `<div style="color:var(--text-dim); font-size:12px;">无参数记录</div>`;
      return;
    }

    const labelMap = {
      holding_period: "标准持仓周期",
      max_positions: "最大同时持仓数",
      max_daily_buys: "单日最大买入数",
      initial_capital: "初始起始资金",
      position_size_pct: "单仓仓位上限",
      take_profit_pct: "硬性止盈阈值",
      stop_loss_pct: "硬性止损阈值",
      selection_sort_by: "候选排序字段",
      selection_top_n: "单日入选标的数",
      market_filter_enabled: "大盘环境择时过滤",
      intraday_stop_loss_enabled: "盘中止损保护",
      commission_rate: "佣金费率",
      stamp_tax_rate: "印花税率",
    };

    container.innerHTML = keys
      .map((k) => {
        const label = labelMap[k] || k;
        let v = params[k];
        if (typeof v === "boolean") v = v ? "启用" : "禁用";
        return `
        <div class="param-item">
          <span class="param-k">${label}</span>
          <span class="param-v tnum">${v}</span>
        </div>
      `;
      })
      .join("");
  }

  // --- Render Open Positions ---
  function renderOpenPositionsTable(payload) {
    const card = document.getElementById("open-positions-card");
    const tbody = document.getElementById("open-positions-body");
    const countBadge = document.getElementById("open-positions-count");
    if (!card || !tbody) return;

    const positions = payload.open_positions || [];
    if (!positions.length) {
      card.style.display = "none";
      return;
    }

    card.style.display = "block";
    if (countBadge) countBadge.textContent = `${positions.length} 只标的`;

    tbody.innerHTML = positions
      .map((p) => {
        const pnl = Number(p.unrealized_pnl || 0);
        const pnlPct = Number(p.unrealized_pnl_pct || 0);
        return `
        <tr>
          <td class="tnum"><strong>${p.symbol}</strong></td>
          <td><span class="stock-clickable" data-symbol="${p.symbol}" data-center="${p.buy_date}">${p.name || "--"}</span></td>
          <td class="tnum dim">${p.buy_date || "--"}</td>
          <td class="tnum">${p.holding_days || 1} 天</td>
          <td class="tnum">¥${formatNum(p.cost_price || p.buy_price)}</td>
          <td class="tnum">¥${formatNum(p.current_price || p.close_price)}</td>
          <td class="tnum">${p.shares || p.volume || "--"}</td>
          <td class="tnum">¥${formatNum(p.market_value)}</td>
          <td class="tnum ${getSignClass(pnl)}">${formatNum(pnl, 2)}</td>
          <td class="tnum ${getSignClass(pnlPct)}">${formatPct(pnlPct, true)}</td>
        </tr>
      `;
      })
      .join("");

    tbody.querySelectorAll(".stock-clickable").forEach((btn) => {
      btn.addEventListener("click", () => {
        openKlineDrawer(btn.getAttribute("data-symbol"), btn.getAttribute("data-center"));
      });
    });
  }

  // --- Trades DataGrid (Filter, Search, Sort, Paging, Export) ---
  function initTradesGrid() {
    const searchInput = document.getElementById("trades-search-input");
    const exportBtn = document.getElementById("export-trades-btn");

    document.querySelectorAll(".filter-pill-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".filter-pill-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        STATE.tradesGrid.filter = btn.getAttribute("data-filter");
        STATE.tradesGrid.currentPage = 1;
        applyTradesFilterAndRender();
      });
    });

    if (searchInput) {
      searchInput.addEventListener("input", (e) => {
        STATE.tradesGrid.searchQuery = e.target.value;
        STATE.tradesGrid.currentPage = 1;
        applyTradesFilterAndRender();
      });
    }

    if (exportBtn) {
      exportBtn.addEventListener("click", () => {
        if (!STATE.tradesGrid.allTrades.length) {
          alert("无成交明细可导出");
          return;
        }
        exportToCsv("trades_history.csv", STATE.tradesGrid.filteredTrades);
      });
    }

    document.querySelectorAll("#trades-table th.sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const field = th.getAttribute("data-sort");
        if (STATE.tradesGrid.sortField === field) {
          STATE.tradesGrid.sortOrder = STATE.tradesGrid.sortOrder === "asc" ? "desc" : "asc";
        } else {
          STATE.tradesGrid.sortField = field;
          STATE.tradesGrid.sortOrder = "desc";
        }
        applyTradesFilterAndRender();
      });
    });
  }

  function applyTradesFilterAndRender() {
    const all = STATE.tradesGrid.allTrades;
    const filter = STATE.tradesGrid.filter;
    const query = STATE.tradesGrid.searchQuery.trim().toLowerCase();
    const sortField = STATE.tradesGrid.sortField;
    const sortOrder = STATE.tradesGrid.sortOrder;

    let filtered = all.filter((t) => {
      const ret = Number(t.return_pct || 0);
      const reason = (t.exit_reason || "").toLowerCase();

      if (filter === "win" && ret <= 0) return false;
      if (filter === "loss" && ret >= 0) return false;
      if (filter === "tp" && !reason.includes("止盈") && !reason.includes("take_profit")) return false;
      if (filter === "sl" && !reason.includes("止损") && !reason.includes("stop_loss")) return false;
      if (filter === "expired" && !reason.includes("到期") && !reason.includes("expire")) return false;

      if (query) {
        const sym = (t.symbol || "").toLowerCase();
        const nm = (t.name || "").toLowerCase();
        if (!sym.includes(query) && !nm.includes(query)) return false;
      }
      return true;
    });

    filtered.sort((a, b) => {
      let vA = a[sortField];
      let vB = b[sortField];
      if (sortField === "return_pct" || sortField === "holding_days" || sortField === "buy_price" || sortField === "sell_price") {
        vA = Number(vA || 0);
        vB = Number(vB || 0);
      }
      if (vA < vB) return sortOrder === "asc" ? -1 : 1;
      if (vA > vB) return sortOrder === "asc" ? 1 : -1;
      return 0;
    });

    STATE.tradesGrid.filteredTrades = filtered;
    const badge = document.getElementById("trades-total-badge");
    if (badge) badge.textContent = `${filtered.length} 笔`;

    renderTradesTablePage();
  }

  function renderTradesTablePage() {
    const tbody = document.getElementById("trades-table-body");
    const pageInfo = document.getElementById("trades-page-info");
    const pagination = document.getElementById("trades-pagination");
    if (!tbody) return;

    const list = STATE.tradesGrid.filteredTrades;
    const total = list.length;
    const pageSize = STATE.tradesGrid.pageSize;
    const totalPages = Math.ceil(total / pageSize) || 1;
    let page = Math.max(1, Math.min(totalPages, STATE.tradesGrid.currentPage));
    STATE.tradesGrid.currentPage = page;

    if (total === 0) {
      tbody.innerHTML = `<tr><td colspan="12" style="text-align:center; color:var(--text-dim); padding:24px;">无匹配交易记录</td></tr>`;
      if (pageInfo) pageInfo.textContent = "显示 0 - 0 共 0 笔";
      if (pagination) pagination.innerHTML = "";
      return;
    }

    const startIdx = (page - 1) * pageSize;
    const endIdx = Math.min(total, startIdx + pageSize);
    const pageItems = list.slice(startIdx, endIdx);

    if (pageInfo) pageInfo.textContent = `显示 ${startIdx + 1} - ${endIdx} 共 ${total} 笔`;

    tbody.innerHTML = pageItems
      .map((t, i) => {
        const globalIdx = startIdx + i + 1;
        const ret = Number(t.return_pct || 0);
        const reason = t.exit_reason || "到期平仓";
        let reasonBadge = "bg-neutral";
        if (reason.includes("止盈")) reasonBadge = "bg-pos";
        else if (reason.includes("止损")) reasonBadge = "bg-neg";

        return `
        <tr>
          <td class="tnum dim">${globalIdx}</td>
          <td class="tnum"><strong>${t.symbol}</strong></td>
          <td><span class="stock-clickable" data-symbol="${t.symbol}" data-date="${t.buy_date}">${t.name || "--"}</span></td>
          <td class="tnum dim">${t.buy_date || "--"}</td>
          <td class="tnum dim">${t.sell_date || "--"}</td>
          <td class="tnum">${t.holding_days || 1} 天</td>
          <td class="tnum">¥${formatNum(t.buy_price)}</td>
          <td class="tnum">¥${formatNum(t.sell_price)}</td>
          <td class="tnum">¥${formatNum(t.buy_amount || t.amount)}</td>
          <td class="tnum ${getSignClass(ret)}"><strong>${formatPct(ret, true)}</strong></td>
          <td><span class="tag-badge ${reasonBadge}">${reason}</span></td>
          <td>
            <button type="button" class="btn btn-secondary btn-sm inspect-trade-btn" data-index="${startIdx + i}">
              📈 穿透
            </button>
          </td>
        </tr>
      `;
      })
      .join("");

    tbody.querySelectorAll(".inspect-trade-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const itemIdx = parseInt(btn.getAttribute("data-index"), 10);
        const trade = list[itemIdx];
        if (trade) {
          openKlineDrawer(trade.symbol, trade.buy_date, trade);
        }
      });
    });

    tbody.querySelectorAll(".stock-clickable").forEach((span) => {
      span.addEventListener("click", () => {
        const sym = span.getAttribute("data-symbol");
        const dt = span.getAttribute("data-date");
        openKlineDrawer(sym, dt);
      });
    });

    if (pagination) {
      let pagHtml = "";
      if (page > 1) {
        pagHtml += `<button type="button" class="page-btn" data-page="${page - 1}">‹</button>`;
      }
      const maxBtns = 5;
      let startPage = Math.max(1, page - Math.floor(maxBtns / 2));
      let endPage = Math.min(totalPages, startPage + maxBtns - 1);
      if (endPage - startPage + 1 < maxBtns) {
        startPage = Math.max(1, endPage - maxBtns + 1);
      }
      for (let p = startPage; p <= endPage; p++) {
        pagHtml += `<button type="button" class="page-btn ${p === page ? "active" : ""}" data-page="${p}">${p}</button>`;
      }
      if (page < totalPages) {
        pagHtml += `<button type="button" class="page-btn" data-page="${page + 1}">›</button>`;
      }
      pagination.innerHTML = pagHtml;

      pagination.querySelectorAll(".page-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
          STATE.tradesGrid.currentPage = parseInt(btn.getAttribute("data-page"), 10);
          renderTradesTablePage();
        });
      });
    }
  }

  // --- Export CSV Helper ---
  function exportToCsv(filename, rows) {
    if (!rows || !rows.length) return;
    const keys = Object.keys(rows[0]);
    let csvContent = "\uFEFF" + keys.join(",") + "\n";
    rows.forEach((row) => {
      const line = keys.map((k) => `"${(row[k] === undefined || row[k] === null ? "" : String(row[k])).replace(/"/g, '""')}"`).join(",");
      csvContent += line + "\n";
    });
    const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.setAttribute("download", filename);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }

  // =========================================================
  // PAGE 4: AUTOML PARAMETER OPTIMIZATION (/optimize)
  // =========================================================
  async function loadOptimizePage() {
    const stratSelect = document.getElementById("opt-strategy");
    const methodSelect = document.getElementById("opt-method");
    const trialsInput = document.getElementById("opt-max-trials");
    const seedInput = document.getElementById("opt-seed");
    const startInput = document.getElementById("opt-start-date");
    const endInput = document.getElementById("opt-end-date");
    const runBtn = document.getElementById("opt-run-btn");
    const runBtnText = document.getElementById("opt-run-text");
    const spaceTags = document.getElementById("opt-space-tags");
    const banner = document.getElementById("opt-progress-banner");
    const progressBar = document.getElementById("opt-progress-bar");
    const progressTitle = document.getElementById("opt-progress-title");
    const progressDetail = document.getElementById("opt-progress-detail");
    const resultsPane = document.getElementById("opt-results-pane");

    // Quick range buttons
    document.querySelectorAll(".opt-range").forEach((btn) => {
      btn.addEventListener("click", () => {
        const range = btn.getAttribute("data-range");
        if (!endInput) return;
        const endDateStr = endInput.value || "2026-09-17";
        const endD = new Date(endDateStr);
        let startD = new Date(endD);
        if (range === "6m") startD.setMonth(startD.getMonth() - 6);
        else if (range === "1y") startD.setFullYear(startD.getFullYear() - 1);
        else if (range === "2y") startD.setFullYear(startD.getFullYear() - 2);
        else if (range === "all") startD = new Date("2020-01-01");
        if (startInput) startInput.value = startD.toISOString().slice(0, 10);
      });
    });

    async function loadSearchSpace(stratName) {
      if (!stratName || !spaceTags) return;
      try {
        const data = await fetchJson(`/api/strategy-params?strategy_name=${encodeURIComponent(stratName)}`);
        const space = data.search_space || [];
        if (!space.length) {
          spaceTags.innerHTML = '<span class="tag-badge bg-neutral">默认连续搜索空间 (自动加载内置范围)</span>';
          return;
        }
        spaceTags.innerHTML = space
          .map((p) => {
            const rangeLabel = p.choices ? p.choices.join(",") : `${p.low} ~ ${p.high} (step ${p.step || 1})`;
            return `<span class="tag-badge bg-cyan" title="${p.name}"><strong>${p.name.split(".").pop()}</strong>: ${rangeLabel}</span>`;
          })
          .join("");
      } catch (_) {
        spaceTags.innerHTML = '<span class="tag-badge bg-neutral">默认连续搜索空间</span>';
      }
    }

    try {
      const strats = await fetchJson("/api/strategies");
      if (stratSelect) {
        stratSelect.innerHTML = strats.map((s) => `<option value="${s.name}">${s.display_name || s.name}</option>`).join("");
        if (strats.length > 0) {
          await loadSearchSpace(strats[0].name);
        }
      }
    } catch (_) {}

    try {
      const st = await fetchJson("/api/data/status");
      const latestDate = st.daily_bar_max_date || st.latest_daily_bar_date;
      if (endInput) endInput.value = latestDate || "2026-09-17";
    } catch (_) {
      if (endInput) endInput.value = "2026-09-17";
    }
    if (startInput) startInput.value = "2025-06-30";

    if (stratSelect) {
      stratSelect.addEventListener("change", () => {
        loadSearchSpace(stratSelect.value);
      });
    }

    if (runBtn) {
      runBtn.addEventListener("click", async () => {
        const strategy_name = stratSelect ? stratSelect.value : "";
        const method = methodSelect ? methodSelect.value : "random";
        const max_trials = parseInt(trialsInput ? trialsInput.value : "12", 10) || 12;
        const seed = seedInput && seedInput.value ? parseInt(seedInput.value, 10) : null;
        const start_date = startInput ? startInput.value : "";
        const end_date = endInput ? endInput.value : "";

        if (!strategy_name || !start_date || !end_date) {
          alert("请选择策略并填写起止日期");
          return;
        }

        runBtn.disabled = true;
        if (runBtnText) runBtnText.textContent = "寻优中...";
        if (banner) banner.style.display = "flex";
        if (resultsPane) resultsPane.style.display = "none";

        try {
          await fetchJson("/api/optimize/run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ strategy_name, method, max_trials, seed, start_date, end_date }),
          });

          const job = await waitForJob((job) => {
            if (!job) return;
            if (job.progress) {
              const pct = job.progress.percentage || 0;
              if (progressBar) progressBar.style.width = `${pct}%`;
              if (progressTitle) progressTitle.textContent = `参数自动寻优中... (${job.progress.completed_trials || 0}/${job.progress.total_trials || max_trials})`;
              if (progressDetail && job.progress.current_params) {
                progressDetail.textContent = `当前探索: ${JSON.stringify(job.progress.current_params).slice(0, 80)}...`;
              }
            }
          });

          runBtn.disabled = false;
          if (runBtnText) runBtnText.textContent = "启动自动寻优";
          if (banner) banner.style.display = "none";

          if (job && job.status === "succeeded" && job.result) {
            renderOptimizeResults(job.result);
          } else {
            alert(`寻优运行失败: ${(job && job.error) || "未知错误"}`);
          }
        } catch (err) {
          alert(`寻优请求异常: ${err.message}`);
          runBtn.disabled = false;
          if (runBtnText) runBtnText.textContent = "启动自动寻优";
          if (banner) banner.style.display = "none";
        }
      });
    }
  }

  function renderOptimizeResults(result) {
    const resultsPane = document.getElementById("opt-results-pane");
    if (resultsPane) resultsPane.style.display = "block";

    const bestTrial = result.best_trial || (result.trials && result.trials[0]) || {};
    const bestScore = bestTrial.score !== undefined ? bestTrial.score : result.best_score;
    const bestMetrics = bestTrial.metrics || {};

    const setT = (id, txt) => {
      const el = document.getElementById(id);
      if (el) el.textContent = txt;
    };

    setT("best-score", formatNum(bestScore, 2));
    setT("best-return", formatPct(bestMetrics.total_return_pct, true));
    setT("best-drawdown", formatPct(bestMetrics.max_drawdown_pct ? -Math.abs(Number(bestMetrics.max_drawdown_pct)) : 0));
    setT("best-sharpe", formatNum(bestMetrics.sharpe_ratio, 2));

    const totalEl = document.getElementById("opt-total-runs");
    if (totalEl) totalEl.textContent = `${result.trials_completed || (result.trials ? result.trials.length : 0)} 轮搜索`;

    // Render Pareto Bubble Chart
    renderParetoChart(result.trials || []);

    // Render Trials Leaderboard Table
    const tbody = document.getElementById("opt-trials-tbody");
    if (tbody && result.trials) {
      tbody.innerHTML = result.trials
        .map((t, idx) => {
          const m = t.metrics || {};
          const score = t.score !== undefined ? t.score : "-";
          const ret = m.total_return_pct !== undefined ? Number(m.total_return_pct) : null;
          const mdd = m.max_drawdown_pct !== undefined ? -Math.abs(Number(m.max_drawdown_pct)) : null;
          const overridesStr = t.overrides ? Object.entries(t.overrides).map(([k, v]) => `${k.split(".").pop()}=${v}`).join(", ") : "默认";

          return `
            <tr>
              <td class="tnum"><strong>#${idx + 1}</strong></td>
              <td class="tnum"><span class="tag-badge ${idx === 0 ? "bg-pos" : "bg-neutral"}">Run #${t.run_id || "-"}</span></td>
              <td class="tnum font-bold text-cyan">${formatNum(score, 2)}</td>
              <td class="tnum ${getSignClass(ret)}">${formatPct(ret, true)}</td>
              <td class="tnum ${getSignClass(m.annualized_return_pct)}">${formatPct(m.annualized_return_pct, true)}</td>
              <td class="tnum">${formatPct(mdd)}</td>
              <td class="tnum">${formatNum(m.sharpe_ratio, 2)}</td>
              <td class="tnum">${formatPct(m.win_rate_pct)}</td>
              <td style="max-width:280px; font-size:12px; font-family:var(--font-mono); color:var(--text-muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${overridesStr}">
                ${overridesStr}
              </td>
              <td>
                <a href="/backtests" class="btn btn-secondary btn-sm" style="text-decoration:none; padding:3px 8px; font-size:12px;">
                  📈 回测
                </a>
              </td>
            </tr>
          `;
        })
        .join("");
    }
  }

  function renderParetoChart(trials) {
    const chartContainer = document.getElementById("opt-bubble-chart");
    if (!chartContainer || !trials.length) return;

    if (!STATE.charts.pareto) STATE.charts.pareto = echarts.init(chartContainer);
    const chart = STATE.charts.pareto;
    const isDark = STATE.theme === "dark";

    const data = trials.map((t, idx) => {
      const m = t.metrics || {};
      const ret = m.total_return_pct !== undefined ? Number(m.total_return_pct) : 0;
      const mdd = m.max_drawdown_pct !== undefined ? Math.abs(Number(m.max_drawdown_pct)) : 0;
      const sharpe = m.sharpe_ratio !== undefined ? Math.max(0.1, Number(m.sharpe_ratio)) : 1;
      const score = t.score !== undefined ? Number(t.score) : 50;
      return [ret, mdd, sharpe, score, t.run_id || idx + 1, t.overrides || {}];
    });

    const option = {
      backgroundColor: "transparent",
      tooltip: {
        trigger: "item",
        backgroundColor: isDark ? "rgba(17, 24, 39, 0.95)" : "rgba(255, 255, 255, 0.95)",
        borderColor: isDark ? "rgba(255, 255, 255, 0.15)" : "#cbd5e1",
        textStyle: { color: isDark ? "#f8fafc" : "#0f172a", fontSize: 12 },
        formatter: (params) => {
          const p = params.value;
          const overridesText = Object.entries(p[5] || {})
            .map(([k, v]) => `${k.split(".").pop()}: <strong>${v}</strong>`)
            .join("<br>");
          return `
            <div style="font-weight:700; margin-bottom:4px;">Run #${p[4]} (综合得分: ${p[3].toFixed(1)})</div>
            <div>累计收益: <strong>${p[0] >= 0 ? "+" : ""}${p[0].toFixed(2)}%</strong></div>
            <div>最大回撤: <strong>-${p[1].toFixed(2)}%</strong></div>
            <div>夏普比率: <strong>${p[2].toFixed(2)}</strong></div>
            <hr style="border:none; border-top:1px solid rgba(255,255,255,0.1); margin:6px 0;">
            <div style="font-size:11px; color:var(--text-muted);">${overridesText || "默认参数"}</div>
          `;
        },
      },
      grid: { left: 60, right: 40, top: 30, bottom: 40 },
      xAxis: {
        name: "累计收益率 (%)",
        nameLocation: "middle",
        nameGap: 26,
        type: "value",
        axisLine: { lineStyle: { color: isDark ? "rgba(255,255,255,0.15)" : "#cbd5e1" } },
        axisLabel: { color: isDark ? "#94a3b8" : "#64748b", formatter: "{value}%" },
        splitLine: { lineStyle: { color: isDark ? "rgba(255,255,255,0.06)" : "#f1f5f9" } },
      },
      yAxis: {
        name: "最大回撤 (%) - 越低越好",
        nameLocation: "middle",
        nameGap: 36,
        type: "value",
        inverse: true,
        axisLine: { lineStyle: { color: isDark ? "rgba(255,255,255,0.15)" : "#cbd5e1" } },
        axisLabel: { color: isDark ? "#94a3b8" : "#64748b", formatter: "-{value}%" },
        splitLine: { lineStyle: { color: isDark ? "rgba(255,255,255,0.06)" : "#f1f5f9" } },
      },
      visualMap: {
        show: false,
        dimension: 3,
        min: 40,
        max: 95,
        inRange: {
          color: ["#94a3b8", "#38bdf8", "#06b6d4", "#10b981", "#f43f5e"],
        },
      },
      series: [
        {
          type: "scatter",
          data: data,
          symbolSize: (val) => Math.max(14, Math.min(36, val[2] * 12)),
          itemStyle: {
            shadowBlur: 8,
            shadowColor: "rgba(0, 0, 0, 0.3)",
          },
        },
      ],
    };

    chart.setOption(option, true);
  }

  // =========================================================
  // PAGE 5: STRATEGY VALIDATION & ROBUSTNESS (/validation)
  // =========================================================
  async function loadValidationPage() {
    const stratSelect = document.getElementById("val-strategy");
    const modeSelect = document.getElementById("val-mode");
    const splitInput = document.getElementById("val-split-ratios");
    const perturbInput = document.getElementById("val-perturbations");
    const startInput = document.getElementById("val-start-date");
    const endInput = document.getElementById("val-end-date");
    const runBtn = document.getElementById("val-run-btn");
    const runBtnText = document.getElementById("val-run-text");
    const banner = document.getElementById("val-progress-banner");
    const progressBar = document.getElementById("val-progress-bar");
    const resultsPane = document.getElementById("val-results-pane");

    // Quick range buttons
    document.querySelectorAll(".val-range").forEach((btn) => {
      btn.addEventListener("click", () => {
        const range = btn.getAttribute("data-range");
        if (!endInput) return;
        const endDateStr = endInput.value || "2026-09-17";
        const endD = new Date(endDateStr);
        let startD = new Date(endD);
        if (range === "2y") startD.setFullYear(startD.getFullYear() - 2);
        else if (range === "3y") startD.setFullYear(startD.getFullYear() - 3);
        else if (range === "5y") startD.setFullYear(startD.getFullYear() - 5);
        else if (range === "all") startD = new Date("2020-01-01");
        if (startInput) startInput.value = startD.toISOString().slice(0, 10);
      });
    });

    try {
      const strats = await fetchJson("/api/strategies");
      if (stratSelect) {
        stratSelect.innerHTML = strats.map((s) => `<option value="${s.name}">${s.display_name || s.name}</option>`).join("");
      }
    } catch (_) {}

    try {
      const st = await fetchJson("/api/data/status");
      const latestDate = st.daily_bar_max_date || st.latest_daily_bar_date;
      if (endInput) endInput.value = latestDate || "2026-09-17";
    } catch (_) {
      if (endInput) endInput.value = "2026-09-17";
    }
    if (startInput) startInput.value = "2024-01-01";

    if (runBtn) {
      runBtn.addEventListener("click", async () => {
        const strategy_name = stratSelect ? stratSelect.value : "";
        const mode = modeSelect ? modeSelect.value : "all";
        const split_ratios = splitInput ? splitInput.value : "6,2,2";
        const max_perturbations = parseInt(perturbInput ? perturbInput.value : "10", 10) || 10;
        const start_date = startInput ? startInput.value : "";
        const end_date = endInput ? endInput.value : "";

        if (!strategy_name || !start_date || !end_date) {
          alert("请选择策略并指定起止时间");
          return;
        }

        runBtn.disabled = true;
        if (runBtnText) runBtnText.textContent = "体检计算中...";
        if (banner) banner.style.display = "flex";
        if (resultsPane) resultsPane.style.display = "none";

        try {
          await fetchJson("/api/validation/run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ strategy_name, mode, split_ratios, max_perturbations, start_date, end_date }),
          });

          const job = await waitForJob((job) => {
            if (!job) return;
            if (job.progress) {
              const pct = job.progress.percentage || 0;
              if (progressBar) progressBar.style.width = `${pct}%`;
            }
          });

          runBtn.disabled = false;
          if (runBtnText) runBtnText.textContent = "启动稳健性体检";
          if (banner) banner.style.display = "none";

          if (job && job.status === "succeeded" && job.result) {
            renderValidationResults(job.result);
          } else {
            alert(`体检失败: ${(job && job.error) || "未知错误"}`);
          }
        } catch (err) {
          alert(`体检请求异常: ${err.message}`);
          runBtn.disabled = false;
          if (runBtnText) runBtnText.textContent = "启动稳健性体检";
          if (banner) banner.style.display = "none";
        }
      });
    }
  }

  function renderValidationResults(res) {
    const resultsPane = document.getElementById("val-results-pane");
    if (resultsPane) resultsPane.style.display = "block";

    const stability = Number(res.stability_score || 85);
    const decay = Number(res.decay_rate_pct || 0);

    const titleEl = document.getElementById("val-diagnosis-title");
    const tagEl = document.getElementById("val-diagnosis-tag");
    const descEl = document.getElementById("val-diagnosis-desc");
    const iconEl = document.getElementById("val-diagnosis-icon");
    const stabEl = document.getElementById("val-stability-score");

    if (stabEl) stabEl.textContent = `${stability.toFixed(1)} / 100`;

    if (stability >= 80) {
      if (titleEl) titleEl.textContent = "体检结论: 超额稳健型 (Robust Alpha)";
      if (tagEl) {
        tagEl.textContent = "建议实战关注";
        tagEl.className = "tag-badge bg-pos";
      }
      if (iconEl) iconEl.textContent = "🛡️";
      if (descEl) descEl.textContent = "样本外收益衰减处于温和良性区间，参数微扰未见断崖式下跌，具备穿透不同市场周期的内生驱动力。";
    } else if (stability >= 65) {
      if (titleEl) titleEl.textContent = "体检结论: 适度稳健 (Moderate)";
      if (tagEl) {
        tagEl.textContent = "需控制仓位";
        tagEl.className = "tag-badge bg-neutral";
      }
      if (iconEl) iconEl.textContent = "⚖️";
      if (descEl) descEl.textContent = "策略在验证集或微扰测试中表现平稳，但测试集表现存在适度衰退，建议以轻仓结合市场环境过滤运行。";
    } else {
      if (titleEl) titleEl.textContent = "体检结论: 潜在过拟合预警 (Overfit Risk)";
      if (tagEl) {
        tagEl.textContent = "严防拟合陷阱";
        tagEl.className = "tag-badge bg-neg";
      }
      if (iconEl) iconEl.textContent = "⚠️";
      if (descEl) descEl.textContent = "样本外盲测出现较大幅度回撤或收益腰斩，参数邻域敏感度过高，可能存在历史数据记忆效应。";
    }

    const setT = (id, txt) => {
      const el = document.getElementById(id);
      if (el) el.textContent = txt;
    };

    setT("val-train-score", formatNum(res.train_score, 2));
    setT("val-valid-score", formatNum(res.validation_score, 2));
    setT("val-test-score", formatNum(res.test_score, 2));
    setT("val-overfit-penalty", `${decay >= 0 ? "+" : ""}${decay.toFixed(1)}%`);

    // Render Split Segments Table
    const splitTbody = document.getElementById("val-split-tbody");
    if (splitTbody && res.split_results) {
      splitTbody.innerHTML = res.split_results
        .map((s) => {
          const m = s.metrics || {};
          const score = s.score !== undefined ? s.score : "-";
          const ret = m.total_return_pct !== undefined ? Number(m.total_return_pct) : null;
          const mdd = m.max_drawdown_pct !== undefined ? -Math.abs(Number(m.max_drawdown_pct)) : null;

          return `
          <tr>
            <td><span class="tag-badge ${s.segment === "test" ? "bg-cyan" : "bg-neutral"}"><strong>${s.segment.toUpperCase()}</strong></span></td>
            <td class="tnum dim">${s.start_date} ~ ${s.end_date}</td>
            <td class="tnum font-bold text-cyan">${formatNum(score, 2)}</td>
            <td class="tnum ${getSignClass(ret)}">${formatPct(ret, true)}</td>
            <td class="tnum ${getSignClass(m.annualized_return_pct)}">${formatPct(m.annualized_return_pct, true)}</td>
            <td class="tnum">${formatPct(mdd)}</td>
            <td class="tnum">${formatNum(m.sharpe_ratio, 2)}</td>
            <td class="tnum">${formatPct(m.win_rate_pct)}</td>
            <td class="tnum">${m.closed_trades || 0} 笔</td>
          </tr>
        `;
        })
        .join("");
    }

    // Render Perturbation Table
    const perturbTbody = document.getElementById("val-perturb-tbody");
    if (perturbTbody && res.perturbation_results) {
      perturbTbody.innerHTML = res.perturbation_results
        .map((p, idx) => {
          const m = p.metrics || {};
          const overridesStr = p.overrides ? Object.entries(p.overrides).map(([k, v]) => `${k.split(".").pop()}=${v}`).join(", ") : "标准参数";
          const ret = m.total_return_pct !== undefined ? Number(m.total_return_pct) : null;
          const mdd = m.max_drawdown_pct !== undefined ? -Math.abs(Number(m.max_drawdown_pct)) : null;

          return `
          <tr>
            <td class="tnum">#${idx + 1}</td>
            <td class="tnum font-bold text-cyan">${formatNum(p.score, 2)}</td>
            <td style="font-size:12px; font-family:var(--font-mono); color:var(--text-muted);">${overridesStr}</td>
            <td class="tnum ${getSignClass(ret)}">${formatPct(ret, true)}</td>
            <td class="tnum">${formatPct(mdd)}</td>
            <td class="tnum">${formatNum(m.sharpe_ratio, 2)}</td>
            <td class="tnum">${formatPct(m.win_rate_pct)}</td>
          </tr>
        `;
        })
        .join("");
    }
  }

  // =========================================================
  // PAGE 6: PAPER TRADING & DAILY SIGNAL BOARD (/paper)
  // =========================================================
  async function loadPaperPage() {
    const stratSelect = document.getElementById("paper-strategy");
    const dateInput = document.getElementById("paper-trade-date");
    const planBtn = document.getElementById("paper-plan-btn");
    const planTbody = document.getElementById("paper-plan-tbody");
    const pushBtn = document.getElementById("push-signal-btn");

    // Webhook Modal Elements
    const openModalBtn = document.getElementById("open-webhook-modal-btn");
    const modal = document.getElementById("webhook-modal");
    const closeModalBtn = document.getElementById("webhook-modal-close");
    const webhookInput = document.getElementById("webhook-url-input");
    const testSendBtn = document.getElementById("webhook-test-send-btn");
    const saveWebhookBtn = document.getElementById("webhook-save-btn");
    const statusMsg = document.getElementById("webhook-status-msg");

    // Load saved webhook URL
    if (webhookInput) {
      webhookInput.value = localStorage.getItem("quant_webhook_url") || "";
    }

    if (openModalBtn && modal) {
      openModalBtn.addEventListener("click", () => modal.classList.add("active"));
    }
    if (closeModalBtn && modal) {
      closeModalBtn.addEventListener("click", () => modal.classList.remove("active"));
    }
    if (saveWebhookBtn && webhookInput) {
      saveWebhookBtn.addEventListener("click", () => {
        localStorage.setItem("quant_webhook_url", webhookInput.value.trim());
        if (statusMsg) {
          statusMsg.style.display = "block";
          statusMsg.style.background = "rgba(16, 185, 129, 0.15)";
          statusMsg.style.color = "var(--accent-emerald)";
          statusMsg.textContent = "✓ Webhook 配置已成功保存在本地！";
        }
      });
    }

    if (testSendBtn && webhookInput) {
      testSendBtn.addEventListener("click", async () => {
        const url = webhookInput.value.trim();
        if (!url) {
          alert("请输入 Webhook 接口地址");
          return;
        }
        testSendBtn.disabled = true;
        try {
          const res = await fetchJson("/api/paper/webhook-test", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              webhook_url: url,
              title: "QuantWeb Pro 实战信号测试",
              text: "这是一条来自 QuantWeb Pro 的测试信号推送通知。",
            }),
          });
          if (statusMsg) {
            statusMsg.style.display = "block";
            statusMsg.style.background = res.ok ? "rgba(16, 185, 129, 0.15)" : "rgba(244, 63, 94, 0.15)";
            statusMsg.style.color = res.ok ? "var(--accent-emerald)" : "var(--accent-rose)";
            statusMsg.textContent = res.message || (res.ok ? "测试推送成功！" : "推送失败");
          }
        } catch (err) {
          if (statusMsg) {
            statusMsg.style.display = "block";
            statusMsg.style.background = "rgba(244, 63, 94, 0.15)";
            statusMsg.style.color = "var(--accent-rose)";
            statusMsg.textContent = `推送报错: ${err.message}`;
          }
        } finally {
          testSendBtn.disabled = false;
        }
      });
    }

    try {
      const strats = await fetchJson("/api/strategies");
      if (stratSelect) {
        stratSelect.innerHTML = strats.map((s) => `<option value="${s.name}">${s.display_name || s.name}</option>`).join("");
      }
    } catch (_) {}

    try {
      const st = await fetchJson("/api/data/status");
      const latestDate = st.daily_bar_max_date || st.latest_daily_bar_date;
      if (dateInput) dateInput.value = latestDate || "2026-09-17";
    } catch (_) {
      if (dateInput) dateInput.value = "2026-09-17";
    }

    async function fetchAndRenderPlan() {
      const strategy_name = stratSelect ? stratSelect.value : "";
      const trade_date = dateInput ? dateInput.value : "";
      if (!strategy_name || !trade_date) return;

      if (planBtn) planBtn.disabled = true;
      if (planTbody) {
        planTbody.innerHTML = `<tr><td colspan="10" style="text-align:center; padding:32px; color:var(--text-muted);">正在计算策略次日交易计划...</td></tr>`;
      }

      try {
        const plan = await fetchJson(`/api/paper/plan?strategy_name=${encodeURIComponent(strategy_name)}&trade_date=${encodeURIComponent(trade_date)}`);
        STATE.activePaperPlan = plan;

        const setT = (id, txt) => {
          const el = document.getElementById(id);
          if (el) el.textContent = txt;
        };

        const candidates = plan.candidates || [];
        setT("plan-candidate-count", `${candidates.length} 支`);
        setT("plan-strategy-name", plan.strategy_display_name || plan.strategy_name);
        setT("plan-position-size", `${plan.position_size_pct || 50}%`);
        setT("plan-take-profit", `+${plan.take_profit_pct || 10}%`);
        setT("plan-stop-loss", `${plan.stop_loss_pct || -5}%`);

        const badge = document.getElementById("plan-date-badge");
        if (badge) badge.textContent = `基准信号日: ${plan.trade_date} ➔ 次日执行`;

        if (!candidates.length) {
          if (planTbody) {
            planTbody.innerHTML = `<tr><td colspan="10" style="text-align:center; padding:32px; color:var(--text-muted);">该交易日该策略未产生买入信号 (空仓防守观望)</td></tr>`;
          }
          return;
        }

        if (planTbody) {
          planTbody.innerHTML = candidates
            .map((c) => {
              const maxBuyPrice = (Number(c.close) * 1.03).toFixed(2);
              const tpPrice = (Number(c.close) * (1 + (plan.take_profit_pct || 10) / 100)).toFixed(2);
              const slPrice = (Number(c.close) * (1 + (plan.stop_loss_pct || -5) / 100)).toFixed(2);

              return `
              <tr>
                <td class="tnum"><strong>${c.symbol}</strong></td>
                <td><span class="stock-clickable" data-symbol="${c.symbol}" data-date="${plan.trade_date}">${c.name || "--"}</span></td>
                <td class="tnum">¥${formatNum(c.close)}</td>
                <td class="tnum font-bold text-cyan">${formatNum(c.score, 2)}</td>
                <td class="tnum text-amber">≤ ¥${maxBuyPrice}</td>
                <td class="tnum text-pos">≥ ¥${tpPrice}</td>
                <td class="tnum text-neg">≤ ¥${slPrice}</td>
                <td class="tnum">${plan.position_size_pct || 50}%</td>
                <td style="font-size:12px; color:var(--text-muted);">开盘集合竞价或 9:30-9:35 分批建仓</td>
                <td style="text-align:right;">
                  <button type="button" class="btn btn-secondary btn-sm stock-kline-btn" data-symbol="${c.symbol}" data-center="${plan.trade_date}">
                    📈 K线
                  </button>
                </td>
              </tr>
            `;
            })
            .join("");

          planTbody.querySelectorAll(".stock-clickable, .stock-kline-btn").forEach((el) => {
            el.addEventListener("click", () => {
              const sym = el.getAttribute("data-symbol");
              const dt = el.getAttribute("data-date") || el.getAttribute("data-center");
              openKlineDrawer(sym, dt);
            });
          });
        }
      } catch (err) {
        if (planTbody) {
          planTbody.innerHTML = `<tr><td colspan="10" style="text-align:center; padding:32px; color:var(--accent-rose);">推演失败: ${err.message}</td></tr>`;
        }
      } finally {
        if (planBtn) planBtn.disabled = false;
      }
    }

    if (planBtn) {
      planBtn.addEventListener("click", fetchAndRenderPlan);
    }

    if (pushBtn) {
      pushBtn.addEventListener("click", async () => {
        const webhookUrl = localStorage.getItem("quant_webhook_url") || "";
        if (!webhookUrl) {
          alert("请先点击右上角“消息推送配置”保存您的 Webhook 地址！");
          return;
        }
        if (!STATE.activePaperPlan || !STATE.activePaperPlan.candidates || !STATE.activePaperPlan.candidates.length) {
          alert("当前暂无可推送的交易信号");
          return;
        }
        const plan = STATE.activePaperPlan;
        const symList = plan.candidates.map((c) => `${c.name}(${c.symbol}) 评分:${c.score}`).join("\n• ");
        const text = `【QuantWeb 次日买入计划】\n策略: ${plan.strategy_display_name}\n信号日: ${plan.trade_date}\n入选标的:\n• ${symList}\n止盈: +${plan.take_profit_pct}% | 止损: ${plan.stop_loss_pct}%\n推荐仓位: ${plan.position_size_pct}%`;

        try {
          const res = await fetchJson("/api/paper/webhook-test", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ webhook_url: webhookUrl, title: "🚀 次日开盘买入计划", text }),
          });
          alert(res.ok ? "信号已成功推送至移动端！" : `推送失败: ${res.message}`);
        } catch (e) {
          alert(`推送错误: ${e.message}`);
        }
      });
    }

    // Auto load plan on start
    await fetchAndRenderPlan();
  }

  // --- Safe Bootstrap Router ---
  function bootstrap() {
    initTheme();
    initKlineDrawer();

    const path = window.location.pathname;
    if (path.startsWith("/data")) {
      loadDataPage();
    } else if (path.startsWith("/select")) {
      loadSelectPage();
    } else if (path.startsWith("/backtests")) {
      loadBacktestPage();
    } else if (path.startsWith("/optimize")) {
      loadOptimizePage();
    } else if (path.startsWith("/validation")) {
      loadValidationPage();
    } else if (path.startsWith("/paper")) {
      loadPaperPage();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }
})();
