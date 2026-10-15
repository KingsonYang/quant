from __future__ import annotations

import csv
import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import httpx

from quant.config import (
    DEFAULT_STRATEGIES_DIR,
    StrategyConfigError,
    discover_strategy_configs,
    load_strategy_config,
)
from quant.continuation import continuation_parameter_hash
from quant.db.trading_calendar import build_trading_calendar_incremental
from quant.experiments.models import RUN_KIND_STRATEGY_BACKTEST
from quant.experiments.store import DEFAULT_EXPERIMENTS_DIR, ExperimentStore
from quant.ingest.tdxquant import TdxQuantIngestionService
from quant.optimization import optimize_loaded_strategy
from quant.optimization.runner import default_optimization_results_dir
from quant.optimization.search_space import ParameterOverride, apply_parameter_overrides
from quant.strategy_runtime import run_loaded_strategy_backtest, run_strategy_selection
from quant.validation import validate_loaded_strategy
from quant.validation.runner import default_validation_results_dir
from quant.web.jobs import JobBusyError, JobKind, JobSlot, JobSnapshot
from quant.web.queries import (
    DataStatus,
    load_benchmark_series,
    load_data_status,
    load_market_sentiment,
    load_stock_kline,
)


WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


class IngestRequest(BaseModel):
    days: int = Field(default=5, ge=1, le=30)


class SelectRequest(BaseModel):
    strategy_name: str
    trade_date: date


class BacktestRunRequest(BaseModel):
    strategy_name: str
    start_date: date
    end_date: date
    parameter_overrides: dict[str, Any] | None = None


class OptimizeRunRequest(BaseModel):
    strategy_name: str
    start_date: date
    end_date: date
    method: str = "random"
    max_trials: int = 30
    seed: int | None = 42
    top_n: int = 10


class ValidationRunRequest(BaseModel):
    strategy_name: str
    start_date: date
    end_date: date
    mode: str = "split"
    split_ratios: str = "6,2,2"
    walk_folds: int = 3
    max_perturbations: int = 10
    seed: int | None = 42


class WebhookTestRequest(BaseModel):
    webhook_url: str
    title: str = "QuantWeb 策略选股信号"
    content: str = "【QuantWeb】测试推送：明日选股信号就绪"


def _status_payload(status: DataStatus) -> dict[str, Any]:
    daily_date = None if status.daily_bar_max_date is None else status.daily_bar_max_date.isoformat()
    cal_date = None if status.calendar_max_date is None else status.calendar_max_date.isoformat()
    return {
        "daily_bar_max_date": daily_date,
        "latest_daily_bar_date": daily_date,
        "daily_bar_security_count": status.daily_bar_security_count,
        "covered_stock_count": status.daily_bar_security_count,
        "calendar_max_date": cal_date,
        "latest_calendar_date": cal_date,
        "calendar_lagging": status.calendar_lagging,
        "tdx_available": status.tdx_available,
        "tdx_message": status.tdx_message,
    }



def _job_payload(snapshot: JobSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "job_id": snapshot.job_id,
        "kind": snapshot.kind.value,
        "status": snapshot.status,
        "events": snapshot.events,
        "result": snapshot.result,
        "error": snapshot.error,
        "progress": snapshot.progress,
    }


def _date_text(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)[:10]


def _iso_or_none(value: date | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _default_ingest(
    db_path: Path,
    *,
    start_date: str,
    end_date: str,
    progress_callback: Callable[..., None] | None = None,
) -> dict[str, Any]:
    summary = TdxQuantIngestionService(db_path=db_path).ingest(
        start_date=start_date,
        end_date=end_date,
        only_main_board=True,
        progress_callback=progress_callback,
    )
    return {
        "inserted": summary.inserted,
        "skipped_existing": summary.skipped_existing,
        "errors": summary.errors,
        "fetched": summary.fetched,
        "stock_count": summary.stock_count,
    }


def _default_calendar(db_path: Path) -> dict[str, Any]:
    summary = build_trading_calendar_incremental(db_path=db_path)
    return {
        "mode": summary.mode,
        "scanned_trade_dates": summary.scanned_trade_dates,
        "calendar_rows_written": summary.calendar_rows_written,
        "calendar_rows_updated": summary.calendar_rows_updated,
    }


def _load_strategy(strategy_name: str, strategies_dir: Path):
    try:
        return load_strategy_config(strategy_name, strategies_dir)
    except StrategyConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _candidate_payload(candidate) -> dict[str, Any]:
    return {
        "symbol": candidate.symbol,
        "name": candidate.name,
        "d0_date": _iso_or_none(candidate.d0_date),
        "composite_score": candidate.composite_score,
        "pool_score_v3": candidate.pool_score_v3,
        "branch_type": candidate.branch_type,
        "board_count": candidate.board_count,
        "first_limit_date": _iso_or_none(candidate.first_limit_date),
        "last_limit_date": _iso_or_none(candidate.last_limit_date),
        "false_break_occurred": candidate.false_break_occurred,
    }


def _continuation_payload(store: ExperimentStore, loaded, start_date: date) -> dict[str, Any]:
    if loaded.config.strategy_backtest is None:
        return {
            "can_continue": False,
            "run_id": None,
            "last_end_date": None,
            "reason": "策略没有 strategy_backtest",
        }
    parameter_hash = continuation_parameter_hash(loaded)
    run_id = store.find_continuable_strategy_run(
        strategy_key=loaded.name,
        start_date=start_date,
        parameter_hash=parameter_hash,
    )
    if run_id is not None:
        detail = store.load_run_detail(run_id)
        return {
            "can_continue": True,
            "run_id": run_id,
            "last_end_date": _date_text(detail.end_date),
            "reason": None,
        }
    for item in store.list_runs(
        strategy_name=loaded.name,
        run_kind=RUN_KIND_STRATEGY_BACKTEST,
        limit=50,
    ):
        if item.run_status != "completed":
            continue
        if _date_text(item.start_date) != start_date.isoformat():
            continue
        return {
            "can_continue": False,
            "run_id": item.run_id,
            "last_end_date": _date_text(item.end_date),
            "reason": "参数已变化，不能追加",
        }
    return {
        "can_continue": False,
        "run_id": None,
        "last_end_date": None,
        "reason": None,
    }


def _drawdown_series(equity: list[dict[str, Any]]) -> list[dict[str, Any]]:
    peak = None
    series = []
    for point in equity:
        assets = float(point["total_assets"])
        peak = assets if peak is None else max(peak, assets)
        drawdown_pct = 0.0 if peak <= 0 else (peak - assets) * 100 / peak
        series.append({"trade_date": point["trade_date"], "drawdown_pct": drawdown_pct})
    return series


def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _load_backtest_payload(store: ExperimentStore, run_id: int) -> dict[str, Any]:
    try:
        detail = store.load_run_detail(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    artifact_dir = store.artifacts_root / f"run_{run_id}"
    equity_rows = _read_csv_rows(artifact_dir / "equity_curve.csv")
    equity = [
        {
            "trade_date": row["trade_date"],
            "cash_balance": row.get("cash_balance"),
            "market_value": row.get("market_value"),
            "total_assets": row.get("total_assets"),
        }
        for row in equity_rows
        if row.get("trade_date")
    ]
    yearly_path = artifact_dir / "yearly_metrics.json"
    yearly_metrics = {}
    if yearly_path.exists():
        yearly_metrics = json.loads(yearly_path.read_text(encoding="utf-8"))
    resume_path = artifact_dir / "resume_state.json"
    open_positions: list[dict[str, Any]] = []
    if resume_path.exists():
        resume_payload = json.loads(resume_path.read_text(encoding="utf-8")) or {}
        open_positions = list(resume_payload.get("open_positions") or [])
    benchmark = load_benchmark_series(store.db_path, detail.start_date, detail.end_date)
    return {
        "run_id": detail.run_id,
        "strategy_name": detail.strategy_key,
        "start_date": _date_text(detail.start_date),
        "end_date": _date_text(detail.end_date),
        "metrics": detail.metrics,
        "equity": equity,
        "benchmark": benchmark,
        "drawdown": _drawdown_series(equity),
        "trades": sorted(_read_csv_rows(artifact_dir / "trades.csv"), key=lambda row: (row.get("selection_date") or row.get("buy_date") or "", row.get("sell_date") or "", row.get("symbol") or ""), reverse=True),
        "open_positions": open_positions,
        "parameter_snapshot": detail.parameter_snapshot,
        "yearly_metrics": yearly_metrics,
    }


def _start_job(slot: JobSlot, kind: JobKind, worker) -> dict[str, str]:
    try:
        job_id = slot.try_start(kind, worker)
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_id": job_id}


def create_app(
    db_path: Path,
    *,
    job_slot: JobSlot | None = None,
    tdx_probe: Callable[[], tuple[bool, str]] | None = None,
    ingest_fn: Callable[..., dict[str, Any]] | None = None,
    calendar_fn: Callable[..., dict[str, Any]] | None = None,
    strategies_dir: Path | None = None,
    artifacts_root: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="quant 工作台")
    slot = job_slot or JobSlot()
    resolved_db = Path(db_path)
    resolved_strategies = Path(strategies_dir) if strategies_dir else DEFAULT_STRATEGIES_DIR
    resolved_artifacts = Path(artifacts_root) if artifacts_root else DEFAULT_EXPERIMENTS_DIR
    store = ExperimentStore(resolved_db, artifacts_root=resolved_artifacts)
    app.state.db_path = resolved_db
    app.state.job_slot = slot
    app.state.tdx_probe = tdx_probe
    app.state.ingest_fn = ingest_fn or (
        lambda **kwargs: _default_ingest(
            resolved_db,
            start_date=kwargs["start_date"],
            end_date=kwargs["end_date"],
            progress_callback=kwargs.get("progress_callback"),
        )
    )
    app.state.calendar_fn = calendar_fn or (lambda **kwargs: _default_calendar(resolved_db))
    app.state.strategies_dir = resolved_strategies
    app.state.artifacts_root = resolved_artifacts
    app.state.experiment_store = store
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.get("/")
    def home():
        return RedirectResponse("/data", status_code=302)

    @app.get("/data")
    def data_page(request: Request):
        return templates.TemplateResponse(request, "data.html", {"active": "data"})

    @app.get("/select")
    def select_page(request: Request):
        return templates.TemplateResponse(request, "select.html", {"active": "select"})

    @app.get("/backtests")
    def backtest_page(request: Request):
        return templates.TemplateResponse(request, "backtest.html", {"active": "backtest"})

    @app.get("/optimize")
    def optimize_page(request: Request):
        return templates.TemplateResponse(request, "optimize.html", {"active": "optimize"})

    @app.get("/validation")
    def validation_page(request: Request):
        return templates.TemplateResponse(request, "validation.html", {"active": "validation"})

    @app.get("/paper")
    def paper_page(request: Request):
        return templates.TemplateResponse(request, "paper.html", {"active": "paper"})

    @app.get("/api/data/status")
    def data_status():
        status = load_data_status(resolved_db, tdx_probe=tdx_probe)
        return _status_payload(status)

    @app.get("/api/jobs/current")
    def current_job():
        return _job_payload(slot.snapshot())

    @app.get("/api/jobs/stream")
    def stream_jobs():
        def generate():
            deadline = time.time() + 2
            snapshot = slot.snapshot()
            while snapshot is None and time.time() < deadline:
                time.sleep(0.05)
                snapshot = slot.snapshot()
            last_count = -1
            last_status = None
            last_progress = None
            while True:
                snapshot = slot.snapshot()
                payload = _job_payload(snapshot)
                count = 0 if snapshot is None else len(snapshot.events)
                status = None if snapshot is None else snapshot.status
                progress_key = None if snapshot is None or snapshot.progress is None else snapshot.progress.get("current")
                if count != last_count or status != last_status or progress_key != last_progress:
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    last_count = count
                    last_status = status
                    last_progress = progress_key
                if snapshot is None or snapshot.status != "running":
                    break
                time.sleep(0.15)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.post("/api/data/ingest")
    def start_ingest(body: IngestRequest):
        end_date = date.today()
        start_date = end_date - timedelta(days=body.days)

        def worker(emit):
            emit("开始增量更新行情", {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()})

            def on_progress(payload: dict[str, Any]) -> None:
                emit("ingest_progress", {"kind": "progress", **payload})

            result = app.state.ingest_fn(
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                db_path=resolved_db,
                days=body.days,
                progress_callback=on_progress,
            )
            emit("增量更新完成", result)
            return result

        return _start_job(slot, JobKind.INGEST, worker)

    @app.post("/api/data/calendar")
    def start_calendar():
        def worker(emit):
            emit("开始同步交易日历", None)
            result = app.state.calendar_fn(db_path=resolved_db)
            emit("日历同步完成", result)
            return result

        return _start_job(slot, JobKind.CALENDAR, worker)

    @app.get("/api/strategies")
    def list_strategies():
        try:
            loaded_list = discover_strategy_configs(resolved_strategies)
        except StrategyConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return [
            {
                "name": item.name,
                "display_name": item.config.strategy.display_name or item.name,
                "has_strategy_backtest": item.config.strategy_backtest is not None,
            }
            for item in loaded_list
        ]

    @app.get("/api/stock-kline")
    def stock_kline(symbol: str, center_date: date, start_date: date | None = None, end_date: date | None = None):
        if not symbol or len(symbol) > 16:
            raise HTTPException(status_code=400, detail="股票代码无效")
        result = load_stock_kline(resolved_db, symbol, center_date, start_date=start_date, end_date=end_date)
        if not result["data"]:
            raise HTTPException(status_code=404, detail="指定窗口没有日线数据")
        return result

    @app.get("/api/market/sentiment")
    def market_sentiment(trade_date: date):
        return load_market_sentiment(resolved_db, trade_date)

    @app.post("/api/select")
    def start_select(body: SelectRequest):
        loaded = _load_strategy(body.strategy_name, resolved_strategies)

        def worker(emit):
            emit("开始选股", {"strategy_name": loaded.name, "trade_date": body.trade_date.isoformat()})
            summary = run_strategy_selection(
                db_path=resolved_db,
                trade_date=body.trade_date,
                loaded=loaded,
            )
            result = {
                "strategy_name": summary.strategy_name,
                "display_name": summary.display_name,
                "selection_type": summary.selection_type,
                "trade_date": summary.trade_date.isoformat(),
                "candidate_count": summary.candidate_count,
                "candidates": [_candidate_payload(item) for item in summary.candidates],
            }
            emit("选股完成", {"candidate_count": summary.candidate_count})
            return result

        return _start_job(slot, JobKind.SELECT, worker)

    @app.get("/api/backtest-runs")
    def list_backtest_runs(strategy_name: str):
        items = store.list_runs(
            strategy_name=strategy_name,
            run_kind=RUN_KIND_STRATEGY_BACKTEST,
            limit=100,
        )
        completed = [item for item in items if item.run_status == "completed"]
        run_ids = [item.run_id for item in completed]
        metrics_map: dict[int, dict[str, float]] = {}
        if run_ids:
            with store.connect() as connection:
                placeholders = ",".join(["?"] * len(run_ids))
                rows = connection.execute(
                    f"""
                    SELECT run_id, metric_name, metric_value
                    FROM run_metric
                    WHERE run_id IN ({placeholders})
                      AND metric_name IN ('total_return_pct', 'annualized_return_pct')
                    """,
                    run_ids,
                ).fetchall()
                for rid, mname, mval in rows:
                    if rid not in metrics_map:
                        metrics_map[rid] = {}
                    metrics_map[rid][mname] = float(mval) if mval is not None else None

        return [
            {
                "run_id": item.run_id,
                "start_date": _date_text(item.start_date),
                "end_date": _date_text(item.end_date),
                "score": item.score,
                "total_return_pct": metrics_map.get(item.run_id, {}).get("total_return_pct"),
                "annualized_return_pct": metrics_map.get(item.run_id, {}).get("annualized_return_pct"),
            }
            for item in completed
        ]

    @app.get("/api/backtests/continuation")
    def continuation_status(strategy_name: str, start_date: date):
        loaded = _load_strategy(strategy_name, resolved_strategies)
        return _continuation_payload(store, loaded, start_date)

    @app.post("/api/backtests/run")
    def start_backtest(body: BacktestRunRequest):
        loaded = _load_strategy(body.strategy_name, resolved_strategies)
        if loaded.config.strategy_backtest is None:
            raise HTTPException(status_code=400, detail="策略没有 strategy_backtest")
        
        has_overrides = bool(body.parameter_overrides)
        if has_overrides:
            override_objs = [
                ParameterOverride(path=k, value=v)
                for k, v in body.parameter_overrides.items()
            ]
            loaded = apply_parameter_overrides(loaded, override_objs)
            should_append = False
            resume_run_id = None
        else:
            info = _continuation_payload(store, loaded, body.start_date)
            if info["can_continue"] and body.end_date <= date.fromisoformat(info["last_end_date"]):
                raise HTTPException(status_code=400, detail="结束日需要更晚")
            should_append = bool(info["can_continue"] and body.end_date > date.fromisoformat(info["last_end_date"]))
            resume_run_id = info["run_id"] if should_append else None

        def worker(emit):
            def on_progress(progress) -> None:
                payload = {
                    "trade_date": progress.trade_date.isoformat(),
                    "current_trade_day": progress.current_trade_day,
                    "total_trade_days": progress.total_trade_days,
                }
                if progress.total_assets is not None:
                    payload["equity"] = float(progress.total_assets)
                emit(progress.trade_date.isoformat(), payload)

            action_desc = "开始自定义参数回测" if has_overrides else ("开始追加回测" if should_append else "开始策略回测")
            emit(
                action_desc,
                {
                    "strategy_name": loaded.name,
                    "start_date": body.start_date.isoformat(),
                    "end_date": body.end_date.isoformat(),
                    "run_id": resume_run_id,
                    "is_custom": has_overrides,
                },
            )
            resume_state = None
            if should_append and resume_run_id is not None:
                resume_state = store.load_resume_state(resume_run_id)
            report = run_loaded_strategy_backtest(
                db_path=resolved_db,
                loaded=loaded,
                start_date=body.start_date,
                end_date=body.end_date,
                progress_callback=on_progress,
                resume_state=resume_state,
            )
            if should_append and resume_run_id is not None:
                saved = store.append_strategy_backtest(resume_run_id, loaded, report)
                mode = "append"
            else:
                saved = store.save_strategy_backtest(loaded, report)
                mode = "custom" if has_overrides else "full"
            result = {
                "run_id": saved.run_id,
                "mode": mode,
                "start_date": report.summary.start_date.isoformat(),
                "end_date": report.summary.end_date.isoformat(),
            }
            emit("回测完成", result)
            return result

        return _start_job(slot, JobKind.BACKTEST, worker)

    @app.get("/api/backtests/{run_id}")
    def backtest_detail(run_id: int):
        return _load_backtest_payload(store, run_id)

    @app.get("/api/strategy-params")
    def get_strategy_params(strategy_name: str):
        loaded = _load_strategy(strategy_name, resolved_strategies)
        sb = loaded.config.strategy_backtest
        opt = loaded.config.optimization
        return {
            "strategy_name": loaded.name,
            "display_name": loaded.config.strategy.display_name or loaded.name,
            "strategy_backtest": {
                "holding_period": sb.holding_period if sb else 1,
                "max_positions": sb.max_positions if sb else 1,
                "max_daily_buys": sb.max_daily_buys if sb else 1,
                "position_size_pct": float(sb.position_size_pct) if sb else 50.0,
                "take_profit_pct": float(sb.take_profit_pct) if sb else 10.0,
                "stop_loss_pct": float(sb.stop_loss_pct) if sb else -5.0,
                "min_composite_score": float(sb.min_composite_score) if sb and sb.min_composite_score else 80.0,
                "market_filter_enabled": bool(sb.market_filter and sb.market_filter.enabled) if sb else False,
            },
            "search_space": (opt.search_space if opt and opt.enabled and opt.search_space else []),
        }

    @app.post("/api/optimize/run")
    def start_optimize(body: OptimizeRunRequest):
        loaded = _load_strategy(body.strategy_name, resolved_strategies)
        if loaded.config.strategy_backtest is None:
            raise HTTPException(status_code=400, detail="策略没有 strategy_backtest")
        method = body.method.strip().lower()
        if method not in {"grid", "random"}:
            raise HTTPException(status_code=400, detail="寻优方式必须为 grid 或 random")

        if loaded.config.optimization is None or not loaded.config.optimization.enabled:
            from quant.config.models import OptimizationConfig
            loaded = LoadedStrategyConfig(
                name=loaded.name,
                path=loaded.path,
                config=loaded.config.model_copy(
                    update={
                        "optimization": OptimizationConfig(
                            enabled=True,
                            search_space={
                                "strategy_backtest.holding_period": [1, 2, 3, 5],
                                "strategy_backtest.take_profit_pct": [6.0, 8.0, 10.0, 15.0, 20.0],
                                "strategy_backtest.stop_loss_pct": [-10.0, -8.0, -5.0, -3.0],
                                "strategy_backtest.selection_top_n": [1, 2, 3],
                                "strategy_backtest.min_composite_score": [60.0, 70.0, 80.0, 85.0],
                            },
                        )
                    }
                ),
            )

        results_dir = default_optimization_results_dir(app.state.artifacts_root, loaded.name)

        def worker(emit):
            emit("开始参数寻优", {"strategy_name": loaded.name, "method": method, "max_trials": body.max_trials})

            def on_progress(msg: str):
                emit("optimize_progress", {"kind": "progress", "message": msg})

            res = optimize_loaded_strategy(
                db_path=resolved_db,
                loaded=loaded,
                method=method,
                target="strategy_backtest",
                start_date=body.start_date,
                end_date=body.end_date,
                top_n=body.top_n,
                results_dir=results_dir,
                max_trials=body.max_trials,
                seed=body.seed,
                progress_callback=on_progress,
            )

            trials_data = []
            with store.connect() as con:
                for t in res.top_trials:
                    m_rows = con.execute(
                        "SELECT metric_name, metric_value FROM run_metric WHERE run_id = ?",
                        [t.run_id],
                    ).fetchall()
                    m_dict = {r[0]: float(r[1]) if r[1] is not None else None for r in m_rows}
                    trials_data.append({
                        "trial_index": t.trial_index,
                        "run_id": t.run_id,
                        "score": round(t.score, 3),
                        "overrides": t.parameter_overrides,
                        "total_return_pct": m_dict.get("total_return_pct"),
                        "annualized_return_pct": m_dict.get("annualized_return_pct"),
                        "max_drawdown_pct": m_dict.get("max_drawdown_pct"),
                        "sharpe_ratio": m_dict.get("sharpe_ratio"),
                        "win_rate_pct": m_dict.get("win_rate_pct"),
                        "profit_factor": m_dict.get("profit_factor"),
                        "closed_trades": m_dict.get("closed_trades"),
                    })

            result_payload = {
                "strategy_name": res.strategy_name,
                "method": res.method,
                "trial_count": res.trial_count,
                "best_run_id": res.best_run_id,
                "best_score": round(res.best_score, 3),
                "top_trials": trials_data,
            }
            emit("寻优完成", result_payload)
            return result_payload

        return _start_job(slot, JobKind.OPTIMIZE, worker)

    @app.post("/api/validation/run")
    def start_validation(body: ValidationRunRequest):
        loaded = _load_strategy(body.strategy_name, resolved_strategies)
        if loaded.config.strategy_backtest is None:
            raise HTTPException(status_code=400, detail="策略没有 strategy_backtest")
        results_dir = default_validation_results_dir(app.state.artifacts_root, loaded.name)
        parts = [int(p.strip()) for p in body.split_ratios.split(",") if p.strip()]
        split_ratios = (parts[0], parts[1], parts[2]) if len(parts) == 3 else (6, 2, 2)

        def worker(emit):
            emit("开始稳健性体检", {"strategy_name": loaded.name, "mode": body.mode})

            def on_progress(msg: str):
                emit("validation_progress", {"kind": "progress", "message": msg})

            res = validate_loaded_strategy(
                db_path=resolved_db,
                loaded=loaded,
                target="strategy_backtest",
                mode=body.mode,
                start_date=body.start_date,
                end_date=body.end_date,
                split_ratios=split_ratios,
                walk_folds=body.walk_folds,
                max_perturbations=body.max_perturbations,
                seed=body.seed,
                save_experiments=True,
                results_dir=results_dir,
                progress_callback=on_progress,
            )

            is_stable = (res.overfit_penalty or 0) <= 15.0 and (res.stability_score or 0) >= 60.0
            diagnosis = "超额稳健型" if is_stable else ("潜在过拟合" if (res.overfit_penalty or 0) > 25.0 else "中度稳健")

            result_payload = {
                "strategy_name": res.strategy_name,
                "mode": res.mode,
                "train_score": round(res.train_score, 2) if res.train_score is not None else None,
                "validation_score": round(res.validation_score, 2) if res.validation_score is not None else None,
                "test_score": round(res.test_score, 2) if res.test_score is not None else None,
                "stability_score": round(res.stability_score, 2) if res.stability_score is not None else None,
                "overfit_penalty": round(res.overfit_penalty, 2) if res.overfit_penalty is not None else None,
                "diagnosis": diagnosis,
                "split_rows": res.split_rows,
                "walk_forward_rows": res.walk_forward_rows,
                "perturbation_rows": res.perturbation_rows,
            }
            emit("体检完成", result_payload)
            return result_payload

        return _start_job(slot, JobKind.VALIDATE, worker)

    @app.get("/api/paper/plan")
    def paper_plan(strategy_name: str, trade_date: date):
        loaded = _load_strategy(strategy_name, resolved_strategies)
        summary = run_strategy_selection(
            db_path=resolved_db,
            trade_date=trade_date,
            loaded=loaded,
        )
        sb = loaded.config.strategy_backtest
        tp_pct = float(sb.take_profit_pct) if sb else 10.0
        sl_pct = float(sb.stop_loss_pct) if sb else -5.0
        pos_size = float(sb.position_size_pct) if sb else 50.0

        candidates = []
        for c in summary.candidates:
            name = getattr(c, "name", None) or getattr(c, "company_name", None) or c.symbol
            close_px = float(getattr(c, "signal_close", None) or getattr(c, "close_price", 0.0) or 0.0)
            score_val = getattr(c, "composite_score", None) if getattr(c, "composite_score", None) is not None else getattr(c, "score", None)
            score = round(float(score_val), 2) if score_val is not None else None
            tp_px = round(close_px * (1 + tp_pct / 100), 2) if close_px else None
            sl_px = round(close_px * (1 + sl_pct / 100), 2) if close_px else None
            candidates.append({
                "symbol": c.symbol,
                "name": name,
                "company_name": name,
                "close": close_px,
                "close_price": close_px,
                "score": score,
                "take_profit_price": tp_px,
                "stop_loss_price": sl_px,
                "recommended_position_pct": pos_size,
                "target_take_profit_pct": tp_pct,
                "target_stop_loss_pct": sl_pct,
                "action_suggestion": "次日早盘 09:25-09:30 集合竞价若涨幅低于 +3% 择机买入",
            })

        return {
            "strategy_name": summary.strategy_name,
            "display_name": summary.display_name,
            "strategy_display_name": summary.display_name,
            "trade_date": summary.trade_date.isoformat(),
            "candidate_count": summary.candidate_count,
            "position_size_pct": pos_size,
            "take_profit_pct": tp_pct,
            "stop_loss_pct": sl_pct,
            "candidates": candidates,
        }

    @app.post("/api/paper/webhook-test")
    def webhook_test(body: WebhookTestRequest):
        url = body.webhook_url.strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            raise HTTPException(status_code=400, detail="Webhook URL 必须以 http:// 或 https:// 开头")

        payload = {
            "msgtype": "text",
            "text": {"content": f"{body.title}\n{body.content}"},
            "msg_type": "text",
            "content": {"text": f"{body.title}\n{body.content}"},
            "title": body.title,
            "desp": body.content,
        }
        try:
            with httpx.Client(timeout=6.0) as client:
                resp = client.post(url, json=payload)
                return {
                    "success": resp.is_success,
                    "status_code": resp.status_code,
                    "message": f"响应状态码: {resp.status_code}",
                }
        except Exception as exc:
            return {
                "success": False,
                "status_code": 500,
                "message": f"推送异常: {str(exc)}",
            }

    return app
