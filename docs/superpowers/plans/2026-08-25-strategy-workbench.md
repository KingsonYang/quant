# Strategy Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 做一个本地 Web 工作台：TDXQuant 增量入库、同步交易日历、展示 `quant run select` 结果，以及可续跑的策略回测（净值/回撤/交割，结束日往后追加）。

**Architecture:** 回测续跑进 `strategy_runtime` + `ExperimentStore`，Web 只调现有函数。FastAPI 提供三个页面；进程内单槽 `JobSlot` 跑入库/日历/选股/回测，进度用 SSE 推给页面。

**Tech Stack:** Python 3.12, DuckDB, Typer, FastAPI, Jinja2, uvicorn, pytest。前端只有 HTML 模板 + 少量 JS，不引入 React/Vite。

## Global Constraints

- 界面文案用简体中文。
- 不实现：普通回测页、优化/验证/进化、YAML 编辑、实盘下单、百度/同花顺入库、`backfill-prices`、`compact-db`、多 run 对比。
- TDXQuant 仍只补充 baidu/ths 未覆盖的日期，页面不得改这个语义。
- `quant run backtest-strategy` 默认仍整段重跑；续跑只给工作台（及专门的 store/runtime API）用。
- 全局同一时刻只允许一个后台任务。
- 追加失败不得把已有 `end_date` 和旧快照改坏。
- 未经用户明确要求不要 `git commit`；遇到本计划里的 Commit 步骤直接跳过。
- 每个改动只服务本任务，不顺手重构无关代码。

## File Map

- Create: `tests/conftest.py` — 临时 DuckDB + 迷你行情/日历/策略夹具
- Create: `tests/fixtures/strategies/tiny_hold.yml` — 规则型测试策略
- Create: `tests/test_continuation_hash.py`
- Create: `tests/test_backtest_resume.py`
- Create: `tests/test_experiment_append.py`
- Create: `tests/test_web_jobs.py`
- Create: `tests/test_web_data.py`
- Create: `tests/test_web_select.py`
- Create: `tests/test_web_backtest_api.py`
- Create: `src/quant/continuation.py` — 续跑参数哈希（runtime 与 store 共用）
- Create: `src/quant/web/__init__.py`
- Create: `src/quant/web/jobs.py` — 单槽任务
- Create: `src/quant/web/queries.py` — 数据状态、TDX 探测
- Create: `src/quant/web/app.py` — FastAPI 工厂与页面路由
- Create: `src/quant/web/api.py` — JSON/SSE API
- Create: `src/quant/web/templates/base.html`
- Create: `src/quant/web/templates/data.html`
- Create: `src/quant/web/templates/select.html`
- Create: `src/quant/web/templates/backtest.html`
- Create: `src/quant/web/static/style.css`
- Create: `src/quant/web/static/app.js`
- Create: `src/quant/commands/web.py`
- Modify: `src/quant/strategy_runtime.py` — `StrategyBacktestResumeState` + `resume_state` 参数
- Modify: `src/quant/experiments/store.py` — 查找可续跑 run、写 `resume_state.json`、追加保存
- Modify: `src/quant/experiments/models.py` — 如需补充 DTO（优先把 DTO 放 runtime/continuation，避免膨胀）
- Modify: `src/quant/cli.py` — 注册 `quant web`
- Modify: `pyproject.toml` — fastapi、uvicorn、jinja2；web 静态资源
- Modify: `environment.yml` — 同上 pip 依赖
- Modify: `README.md` — 工作台启动方式

---

### Task 1: 测试夹具与续跑参数哈希

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/fixtures/strategies/tiny_hold.yml`
- Create: `src/quant/continuation.py`
- Create: `tests/test_continuation_hash.py`
- Modify: `pyproject.toml`（仅在缺少 pytest 配置时加 `[tool.pytest.ini_options]`，`testpaths = ["tests"]`）

**Interfaces:**
- Consumes: `quant.config.load_strategy_config`, `quant.db.init_db.init_duckdb`
- Produces: `continuation_parameter_hash(loaded: LoadedStrategyConfig) -> str`；夹具 `make_mini_db(tmp_path) -> Path`

- [ ] **Step 1: 写测试策略 YAML**

```yaml
version: 1
strategy:
  name: tiny_hold
  display_name: 测试持有
selection:
  type: rules
  rules:
    - field: close_price
      operator: gt
      value: 0
  logic: AND
  sort_by: symbol
  sort_order: ASC
backtest_selection:
  holding_period: 1
  selection_top_n: 1
strategy_backtest:
  holding_period: 5
  max_positions: 1
  max_daily_buys: 1
  initial_capital: 100000
  position_size_pct: 100
  take_profit_pct: 80
  stop_loss_pct: -80
  selection_top_n: 1
```

- [ ] **Step 2: 写失败测试 `tests/test_continuation_hash.py`**

```python
from pathlib import Path
from quant.config import load_strategy_config
from quant.continuation import continuation_parameter_hash

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "strategies"

def test_hash_stable_for_same_yaml():
    loaded = load_strategy_config("tiny_hold", FIXTURE_DIR)
    assert continuation_parameter_hash(loaded) == continuation_parameter_hash(loaded)

def test_hash_changes_when_holding_period_changes(tmp_path: Path):
    src = (FIXTURE_DIR / "tiny_hold.yml").read_text(encoding="utf-8")
    dest = tmp_path / "tiny_hold.yml"
    dest.write_text(src.replace("holding_period: 5", "holding_period: 6"), encoding="utf-8")
    original = load_strategy_config("tiny_hold", FIXTURE_DIR)
    changed = load_strategy_config("tiny_hold", tmp_path)
    assert continuation_parameter_hash(original) != continuation_parameter_hash(changed)
```

- [ ] **Step 3: 跑测试确认失败**

Run: `pytest tests/test_continuation_hash.py -v`  
Expected: FAIL，`continuation_parameter_hash` 未定义

- [ ] **Step 4: 实现哈希与夹具**

`src/quant/continuation.py`:

```python
from __future__ import annotations

import hashlib
import json

from quant.config import LoadedStrategyConfig


def continuation_parameter_hash(loaded: LoadedStrategyConfig) -> str:
    payload = {
        "selection": loaded.config.selection.model_dump(mode="json"),
        "strategy_backtest": (
            None
            if loaded.config.strategy_backtest is None
            else loaded.config.strategy_backtest.model_dump(mode="json")
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

`tests/conftest.py` 提供 `make_mini_db(tmp_path: Path) -> Path`：调用 `init_duckdb`，插入一只股票 `SZ000001` / `平安测试`，交易日 `2024-01-02` 到 `2024-01-10`（跳过周末，至少 6 个交易日），每日 `open=close=10+i*0.1`，volume/amount 填正数。再调用 `build_trading_calendar`。

- [ ] **Step 5: 再跑哈希测试**

Run: `pytest tests/test_continuation_hash.py -v`  
Expected: PASS

- [ ] **Step 6: Commit**（用户未要求则跳过）

---

### Task 2: 策略回测引擎支持 resume_state

**Files:**
- Modify: `src/quant/strategy_runtime.py`
- Create: `tests/test_backtest_resume.py`

**Interfaces:**
- Consumes: `continuation_parameter_hash`（本任务不调用，给 Task 3 用）；现有 `run_loaded_strategy_backtest`
- Produces:

```python
@dataclass(frozen=True)
class StrategyBacktestResumePosition:
    symbol: str
    name: str
    selection_date: date
    buy_date: date
    buy_price: Decimal
    shares: int
    buy_amount: Decimal
    buy_time: datetime | None = None

@dataclass(frozen=True)
class StrategyBacktestResumeState:
    original_start_date: date
    last_end_date: date
    settled_cash: Decimal
    unsettled_cash: Decimal
    open_positions: tuple[StrategyBacktestResumePosition, ...]
    closed_trades: tuple[StrategyBacktestTrade, ...]
    equity_curve: tuple[StrategyBacktestEquityPoint, ...]

def run_loaded_strategy_backtest(
    *,
    db_path: Path,
    loaded: LoadedStrategyConfig,
    start_date: date,
    end_date: date,
    holding_period: int | None = None,
    progress_callback: Callable[[StrategyBacktestProgress], None] | None = None,
    resume_state: StrategyBacktestResumeState | None = None,
) -> StrategyBacktestReport: ...
```

续跑语义（必须写进实现，不要另起一套成交逻辑）：

- `resume_state is None`：行为与现在完全一致（第一天不买入）。
- 有 `resume_state` 时：`start_date` 必须等于 `original_start_date`；模拟区间是交易日历上 `(last_end_date, end_date]`。
- 用 `resume_state` 的现金/持仓/已有 closed trades/已有 equity 作为初值。
- 续跑第一天**要**处理买入（因为原回测的「第一天 skip buy」只针对整段第一天）。买入用的 `selection_date` 仍是「当前交易日的前一个交易日」。
- `buy_trade_index` 按 `load_all_trade_dates` 里 `buy_date` 的下标重算。
- `request.start_date` / `summary.start_date` 保持 `original_start_date`；`end_date` 为新结束日。
- 价格缓存的 symbol 集合 = 新选股候选 ∪ 已有持仓 symbol。
- `progress_callback` 的 `total_trade_days` 为本次新模拟的天数，不是整段。

- [ ] **Step 1: 写失败测试**

`tests/test_backtest_resume.py` 核心断言：

1. 无 resume：`2024-01-02`→`2024-01-05` 与 `2024-01-02`→`2024-01-08` 整段跑，记下短报告。
2. 把短报告编成 `StrategyBacktestResumeState`（只要 `status=="closed"` 的成交；open 仓放进 `open_positions`；`settled_cash`/`unsettled_cash` 若引擎没分开暴露，则从短报告结束后用 `current_cash_balance` 作为 `settled_cash`、`unsettled_cash=0`——**若测试发现 T+1 导致对不齐，就把 runtime 在 report.summary 之外返回 resume 所需现金拆分，见 Step 3**）。
3. `run_loaded_strategy_backtest(..., start_date=2024-01-02, end_date=2024-01-08, resume_state=state)`。
4. Assert：续跑后 `len(equity_curve)` 等于整段长跑；`total_return_pct` 与整段长跑一致；`closed_trade_count` 只增不减；新成交的 `buy_date` 均 `> 2024-01-05`。
5. Assert：`resume_state.last_end_date >= end_date` 时 raise `StrategyRuntimeError`。
6. Assert：不传 resume 时结果与改前一致（第一天无买入）。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_backtest_resume.py -v`  
Expected: FAIL（`resume_state` 参数不存在或续跑结果对不齐）

- [ ] **Step 3: 最小实现**

在 `run_loaded_strategy_backtest` 里：

1. 在循环外根据 `resume_state` 初始化 `settled_cash` / `unsettled_cash` / `open_positions` / `trades` / `equity_curve`。
2. `trade_dates`：无 resume 用 `[start, end]`；有 resume 用 `d for d in all_in_range if d > last_end_date`（`all_in_range` 仍要包含 original start 到 new end 以便校验）。若新区间为空则 raise。
3. 把 `if current_trade_day == 1: continue` 改成 `if resume_state is None and current_trade_day == 1: continue`。
4. 为了对齐 T+1，增加 `build_resume_state_from_runtime(...)` 或让短跑结束时能读到 `settled_cash`/`unsettled_cash`。推荐：循环结束后、把 open 仓写成 `open_at_end` 成交**之前**，构造 `StrategyBacktestResumeState` 所需字段；测试用第一次短跑的内部状态。对外可在 `StrategyBacktestReport` 不加字段，而提供：

```python
def resume_state_from_report(
    report: StrategyBacktestReport,
    *,
    settled_cash: Decimal,
    unsettled_cash: Decimal,
    open_positions: list[_OpenPosition],
) -> StrategyBacktestResumeState: ...
```

更干净的做法：在 `run_loaded_strategy_backtest` **return 之前**把「循环刚结束时的 settled/unsettled/open_positions」写进模块级辅助函数 `extract_resume_state(report, settled_cash, unsettled_cash, open_positions) -> StrategyBacktestResumeState`，测试通过两次调用对齐；Web/Store 用 Store 里持久化的 resume JSON，不依赖这个内部函数。

**必须**让函数在 return 前能得到准确的 `settled_cash`/`unsettled_cash`（循环结束后、把 open 仓追加进 trades 之前的值）。把这两个数和 open positions 序列化进 resume state，供 Task 3 写入 `resume_state.json`。可以在 `StrategyBacktestReport` 增加可选字段 `resume_state: StrategyBacktestResumeState | None = None`，无 resume 的整段跑也填上（方便第一次保存）。这是本任务允许的最小报告扩展。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_backtest_resume.py tests/test_continuation_hash.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**（用户未要求则跳过）

---

### Task 3: ExperimentStore 查找可续跑 run 并追加保存

**Files:**
- Modify: `src/quant/experiments/store.py`
- Create: `tests/test_experiment_append.py`

**Interfaces:**
- Consumes: `continuation_parameter_hash`, `StrategyBacktestResumeState`, `run_loaded_strategy_backtest`
- Produces:

```python
class ExperimentStore:
    def find_continuable_strategy_run(
        self,
        *,
        strategy_key: str,
        start_date: date,
        parameter_hash: str,
    ) -> int | None: ...

    def load_resume_state(self, run_id: int) -> StrategyBacktestResumeState: ...

    def save_strategy_backtest(...) -> SavedExperiment:  # 第一次：写 resume_state.json，universe_definition 含 continuation_hash

    def append_strategy_backtest(
        self,
        run_id: int,
        loaded: LoadedStrategyConfig,
        new_segment: StrategyBacktestReport,
    ) -> SavedExperiment: ...
```

保存规则：

- 第一次 `save_strategy_backtest`：现有逻辑不变，另外写入 artifact `resume_state.json`（`_serialize_for_json(report.resume_state)`），`universe_definition` 增加 `"continuation_hash": continuation_parameter_hash(loaded)`。
- `find_continuable_strategy_run`：`run_kind=strategy_backtest` 且 `run_status` 为成功完成（与现网写入值一致，先读现有 `save_strategy_backtest` 用的 status 字符串，不要臆造）、`start_date` 相等、`continuation_hash` 相等；若多条取 `run_id` 最大的一条。
- `append_strategy_backtest`：
  1. 校验 `new_segment.summary.start_date` 等于原 run start；`new_segment` 的 equity/trades 已是**合并后的全量**（runtime 续跑返回全量曲线和全量成交，append 只插入「日期 > 旧 end_date」的 snapshot）。
  2. 先插入新区间 `equity_snapshot` / `position_snapshot` / 新成交对应的 orders/fills。
  3. 成功后再 `UPDATE backtest_run SET end_date=?`，重写 artifacts（`trades.csv`、`equity_curve.csv`、`yearly_metrics.json`、`summary.json`、`resume_state.json`），重写 `run_metric`。
  4. 任一步失败则不要更新 `end_date`。用同一 connection 事务：DuckDB 上包在一个 `BEGIN`/`COMMIT` 里；失败 `ROLLBACK`。
- 参数哈希不一致或 `new_end <= old_end`：raise `ValueError`，中文或英文均可，但 Web 层会翻译。

- [ ] **Step 1: 写失败测试**

用 `make_mini_db` + `tiny_hold`：

1. 短跑并 `save_strategy_backtest`。
2. `find_continuable_strategy_run` 返回该 `run_id`。
3. 改 YAML holding_period 后哈希不同，find 返回 `None`。
4. 用 `load_resume_state` + runtime 续跑到更晚结束日，再 `append_strategy_backtest`。
5. Assert：`end_date` 更新；`equity_snapshot` 行数增加；旧日期净值不变；`trades.csv` 行数 ≥ 原来。
6. 用 mock/制造 append 中途异常（可对 `_write_equity_snapshots` 临时包一层，或插入重复 PK）确认 `end_date` 仍是旧值。若不好模拟异常，至少测「new_end <= old_end → 不改 end_date」。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_experiment_append.py -v`  
Expected: FAIL

- [ ] **Step 3: 实现 store 方法**

读现有 `_insert_backtest_run` / `_write_metrics` / `_write_artifacts` 并扩展，不要复制成交引擎。`load_resume_state` 只读 `resume_state.json`；文件缺失则 raise，提示该 run 是工作台之前保存的、不能追加。

- [ ] **Step 4: 跑测试**

Run: `pytest tests/test_experiment_append.py tests/test_backtest_resume.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**（用户未要求则跳过）

---

### Task 4: 进程内单槽 JobSlot

**Files:**
- Create: `src/quant/web/jobs.py`
- Create: `src/quant/web/__init__.py`
- Create: `tests/test_web_jobs.py`

**Interfaces:**
- Consumes: 无业务依赖
- Produces:

```python
from enum import Enum
from typing import Any, Callable, Literal

class JobKind(str, Enum):
    INGEST = "ingest"
    CALENDAR = "calendar"
    SELECT = "select"
    BACKTEST = "backtest"

class JobBusyError(RuntimeError):
    pass

@dataclass
class JobSnapshot:
    job_id: str
    kind: JobKind
    status: Literal["running", "succeeded", "failed"]
    events: list[dict[str, Any]]
    result: dict[str, Any] | None
    error: str | None

class JobSlot:
    def try_start(self, kind: JobKind, worker: Callable[[Callable[[str, dict | None], None]], dict[str, Any]]) -> str: ...
    def snapshot(self) -> JobSnapshot | None: ...
```

`try_start`：若 `status=="running"` raise `JobBusyError("已有任务在跑")`。`worker` 收到 `emit(message, payload=None)`。worker 返回的 dict 作为 `result`。异常则 `status=failed`，`error=str(exc)`。用 `threading.Thread` 跑 worker，不要 asyncio 任务里直接跑回测。

- [ ] **Step 1: 写失败测试**

```python
import time
from quant.web.jobs import JobBusyError, JobKind, JobSlot

def test_second_start_rejected_while_running():
    slot = JobSlot()
    def worker(emit):
        time.sleep(0.2)
        emit("工作中", {"n": 1})
        return {"ok": True}
    slot.try_start(JobKind.SELECT, worker)
    try:
        slot.try_start(JobKind.INGEST, worker)
        raise AssertionError("should busy")
    except JobBusyError:
        pass
    deadline = time.time() + 2
    while slot.snapshot().status == "running" and time.time() < deadline:
        time.sleep(0.05)
    assert slot.snapshot().status == "succeeded"
    assert slot.snapshot().result == {"ok": True}

def test_failed_job_records_error():
    slot = JobSlot()
    def worker(emit):
        raise ValueError("boom")
    slot.try_start(JobKind.CALENDAR, worker)
    deadline = time.time() + 2
    while slot.snapshot().status == "running" and time.time() < deadline:
        time.sleep(0.05)
    assert slot.snapshot().status == "failed"
    assert "boom" in (slot.snapshot().error or "")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_web_jobs.py -v`  
Expected: FAIL

- [ ] **Step 3: 实现 `JobSlot`**

`__init__.py` 只导出 `JobSlot`, `JobKind`, `JobBusyError`, `JobSnapshot`。

- [ ] **Step 4: 跑测试**

Run: `pytest tests/test_web_jobs.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**（用户未要求则跳过）

---

### Task 5: 数据状态查询与日历同步（可单测）；TDX 探测可 mock

**Files:**
- Create: `src/quant/web/queries.py`
- Create: `tests/test_web_data.py`

**Interfaces:**
- Consumes: `build_trading_calendar_incremental`, DuckDB `daily_bar` / `trading_calendar`
- Produces:

```python
@dataclass(frozen=True)
class DataStatus:
    daily_bar_max_date: date | None
    daily_bar_security_count: int
    calendar_max_date: date | None
    calendar_lagging: bool
    tdx_available: bool
    tdx_message: str

def load_data_status(db_path: Path, *, tdx_probe: Callable[[], tuple[bool, str]] | None = None) -> DataStatus: ...
def probe_tdxquant(tqcenter_path: Path | None = None) -> tuple[bool, str]: ...
```

`calendar_lagging`：两边日期都有且 `calendar_max_date < daily_bar_max_date`。  
`probe_tdxquant`：尝试按现有 `TdxQuantIngestionService` 的路径导入/初始化；失败返回 `(False, 原因中文)`。测试里不要真连通达信，给 `load_data_status(..., tdx_probe=lambda: (True, "ok"))`。

日历同步测试：夹具里先 `build_trading_calendar`，再插入一条更晚的 `daily_bar`，调用 `build_trading_calendar_incremental`，断言日历最大日追上。

- [ ] **Step 1: 写失败测试**（状态 + 日历增量；不测真 TDX 入库）
- [ ] **Step 2: `pytest tests/test_web_data.py -v` 确认失败**
- [ ] **Step 3: 实现 `queries.py`**
- [ ] **Step 4: 测试通过**
- [ ] **Step 5: Commit**（用户未要求则跳过）

---

### Task 6: FastAPI 应用、顶栏三页、数据页 API

**Files:**
- Create: `src/quant/web/app.py`
- Create: `src/quant/web/api.py`
- Create: `src/quant/web/templates/base.html`
- Create: `src/quant/web/templates/data.html`
- Create: `src/quant/web/templates/select.html`（先空壳，Task 7 填）
- Create: `src/quant/web/templates/backtest.html`（先空壳，Task 8 填）
- Create: `src/quant/web/static/style.css`
- Create: `src/quant/web/static/app.js`
- Modify: `pyproject.toml` — dependencies 增加 `fastapi`, `uvicorn`, `jinja2`；`[tool.setuptools.package-data] "quant.web" = ["templates/*.html", "static/*.css", "static/*.js"]`
- Modify: `environment.yml` — pip 列表同样加上这三项
- Create: `tests/test_web_app_data.py`（或并入 `test_web_data.py`）

**Interfaces:**
- Consumes: `JobSlot`, `load_data_status`, `build_trading_calendar_incremental`, `ingest_tdxquant` / `TdxQuantIngestionService`
- Produces: `create_app(db_path: Path, *, job_slot: JobSlot | None = None, tdx_probe=None) -> FastAPI`

页面：

- `GET /` → 302 `/data`
- `GET /data` `/select` `/backtests`
- `GET /api/data/status` → DataStatus JSON（date 用 isoformat）
- `POST /api/data/ingest` body `{"days": 5}` → 启动 JobKind.INGEST；busy 时 409 `{"detail":"已有任务在跑"}`
- `POST /api/data/calendar` → JobKind.CALENDAR
- `GET /api/jobs/current` → snapshot 或 `null`
- `GET /api/jobs/stream` → SSE，有任务时推 events；无任务也保持连接直到当前 job 结束

数据页按钮：「增量更新行情」（天数输入，默认 5）、「同步交易日历」。`tdx_available is False` 时禁用增量按钮，展示 `tdx_message`。`calendar_lagging` 时显示「请同步日历」。

ingest worker：用与 CLI 相同的日期算法（`end=today`, `start=today-days`），`only_main_board=True`，调用现有 service。测试里把 ingest 换成依赖注入：

```python
def create_app(..., ingest_fn=None, calendar_fn=None)
```

测试用假 `ingest_fn` 返回 `{"inserted": 1, "skipped_existing": 2, "errors": 0}`，不要打真 TDX。

- [ ] **Step 1: 用 FastAPI TestClient 写失败测试**（status、calendar 真库、ingest mock、409 busy）
- [ ] **Step 2: 跑测失败**
- [ ] **Step 3: 实现 app/api/templates**。`base.html` 顶栏三个链接：数据、选股、策略回测。样式保持克制：无渐变、无阴影。
- [ ] **Step 4: 测试通过**
- [ ] **Step 5: Commit**（用户未要求则跳过）

---

### Task 7: 选股页

**Files:**
- Modify: `src/quant/web/api.py`
- Modify: `src/quant/web/templates/select.html`
- Modify: `src/quant/web/static/app.js`
- Create: `tests/test_web_select.py`

**Interfaces:**
- Consumes: `discover_strategy_configs`, `load_strategy_config`, `run_strategy_selection`, `JobSlot`
- Produces:

```http
GET /api/strategies
POST /api/select  {"strategy_name": "tiny_hold", "trade_date": "2024-01-08"}
```

`GET /api/strategies` → `[{"name","display_name","has_strategy_backtest"}]`。  
选股走 JobSlot（`JobKind.SELECT`），因为有的 pattern SQL 可能较慢。完成后 `result` 为：

```python
{
  "strategy_name": "...",
  "display_name": "...",
  "selection_type": "...",
  "trade_date": "YYYY-MM-DD",
  "candidate_count": 0,
  "candidates": [
    {
      "symbol", "name", "d0_date", "composite_score", "pool_score_v3",
      "branch_type", "board_count", "first_limit_date", "last_limit_date",
      "false_break_occurred"
    }
  ]
}
```

空结果：`candidate_count=0`，页面文案「当日 0 只」，不要空白。  
非交易日 / 配置错误：job failed，页面显示 error。不写 ExperimentStore。

- [ ] **Step 1: TestClient 测试 tiny_hold 在夹具交易日能选出 SZ000001；非法日期 failed**
- [ ] **Step 2: 跑测失败**
- [ ] **Step 3: 实现 API + 表格**（缺省列有值才渲染列头）
- [ ] **Step 4: 测试通过**
- [ ] **Step 5: Commit**（用户未要求则跳过）

---

### Task 8: 策略回测页（运行/追加 + 曲线 + 明细）

**Files:**
- Modify: `src/quant/web/api.py`
- Modify: `src/quant/web/templates/backtest.html`
- Modify: `src/quant/web/static/app.js`
- Create: `tests/test_web_backtest_api.py`

**Interfaces:**
- Consumes: Task 2/3 API, `JobSlot`, `load_strategy_config`
- Produces:

```http
GET  /api/backtests/continuation?strategy_name=&start_date=
POST /api/backtests/run  {"strategy_name","start_date","end_date"}
GET  /api/backtests/{run_id}
```

`GET continuation`：用当前 YAML 哈希 + start_date 找 run。  
返回：

```python
{
  "can_continue": bool,
  "run_id": int | None,
  "last_end_date": str | None,
  "reason": str | None  # 例如「参数已变化，不能追加」
}
```

`POST run`：

- 若 `can_continue` 且 `end_date > last_end_date`：`load_resume_state` → runtime 续跑 → `append_strategy_backtest`
- 否则：整段 `run_loaded_strategy_backtest` → `save_strategy_backtest`
- `end_date <= last_end_date` 且可续跑：400，提示结束日需要更晚
- 策略没有 `strategy_backtest`：400
- 已有任务：409

进度：runtime 的 `progress_callback` 里 `emit(f"{date}", {"trade_date","current_trade_day","total_trade_days","equity": last_point.total_assets if 已有})`。equity 点在每个交易日循环结束 append 后才能有；可在循环内现有 progress 之后不推净值，改为循环里 equity append 后加一句 emit——若要改 runtime，只允许给 callback 增加可选 equity，**不要**让 web 复制成交循环。更简单：progress 仍按日推送日期；曲线在 job 结束后用 `GET /api/backtests/{run_id}` 拉全量。第一版允许「跑完再画完整曲线」，但 job events 必须按日更新进度百分比。若实现成本低，可在 `StrategyBacktestProgress` 增加可选 `total_assets: Decimal | None = None`，引擎在 equity append 后填上；Web 实时画点。优先做带 `total_assets` 的进度（满足规格「跑的时候往右长」）。

`GET /api/backtests/{run_id}` 返回指标、equity 序列、回撤序列、trades、open positions、parameter_snapshot、yearly_metrics。

页面：策略下拉、开始/结束日、主按钮文案「运行」或「追加」、run_id/已算到/状态、净值+回撤 canvas 或 SVG 折线、指标条、四个页签。

- [ ] **Step 1: API 测试**（夹具库短跑、再追加、改 YAML 后 can_continue=false、busy 409）
- [ ] **Step 2: 跑测失败**
- [ ] **Step 3: 实现 API + 页面 JS**（`app.js` 里 `POST run` 后订阅 `/api/jobs/stream`）
- [ ] **Step 4: 测试通过；并回归 `pytest tests/ -v`**
- [ ] **Step 5: Commit**（用户未要求则跳过）

---

### Task 9: `quant web` CLI 与 README

**Files:**
- Create: `src/quant/commands/web.py`
- Modify: `src/quant/cli.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `create_app`
- Produces: `quant web --host 127.0.0.1 --port 8000 --db-path ...`

```python
def register(app: typer.Typer) -> None:
    @app.command("web")
    def web_command(host: str = "127.0.0.1", port: int = 8000, db_path: Path = DEFAULT_DB_PATH) -> None:
        import uvicorn
        from quant.web.app import create_app
        uvicorn.run(create_app(db_path), host=host, port=port)
```

在 `cli.py` 里 `from quant.commands import web` 并 `web.register(app)`。不要放进 `legacy.py`。

README「基础工作流」附近加：

```powershell
quant web
```

访问 `http://127.0.0.1:8000`，三页用途各一句。不要写已删除的旧回测表单。

- [ ] **Step 1: `python -c "from quant.cli import app; assert any(c.name=='web' for c in app.registered_commands)"`**
- [ ] **Step 2: 实现 register + README**
- [ ] **Step 3: 全量 `pytest tests/ -q` 通过**
- [ ] **Step 4: Commit**（用户未要求则跳过）

---

## Spec Coverage

| 规格 | 任务 |
|------|------|
| 数据状态、TDX 禁用增量、日历落后提示 | Task 5–6 |
| TDX 增量默认 5 天主板、不改入库语义 | Task 6 |
| 同步 `build_trading_calendar_incremental` | Task 5–6 |
| 选股页字段、0 只、不写实验 | Task 7 |
| 回测曲线/回撤/交割/持仓/参数/分年 | Task 8 |
| 续跑键、追加、失败不改旧 end_date | Task 2–3、8 |
| 单槽任务、409 | Task 4、6、8 |
| FastAPI+模板+JS、`quant web` | Task 6、9 |
| CLI 默认整段回测不变 | Task 2（resume 默认 None） |
| 非目标列表 | 全局约束，无对应实现任务 |

## Self-Review

- 无 TBD/TODO 占位。
- `continuation_parameter_hash`、`StrategyBacktestResumeState`、`JobSlot.try_start`、`append_strategy_backtest` 名称前后任务一致。
- 追加用 runtime 返回的全量 report + store 只插新日期快照，避免 Web 自己算成交。
