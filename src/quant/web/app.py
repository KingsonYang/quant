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
from quant.strategy_runtime import run_loaded_strategy_backtest, run_strategy_selection
from quant.web.jobs import JobBusyError, JobKind, JobSlot, JobSnapshot
from quant.web.queries import DataStatus, load_data_status


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


def _status_payload(status: DataStatus) -> dict[str, Any]:
    return {
        "daily_bar_max_date": None if status.daily_bar_max_date is None else status.daily_bar_max_date.isoformat(),
        "daily_bar_security_count": status.daily_bar_security_count,
        "calendar_max_date": None if status.calendar_max_date is None else status.calendar_max_date.isoformat(),
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
    }


def _date_text(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)[:10]


def _iso_or_none(value: date | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _default_ingest(db_path: Path, *, start_date: str, end_date: str) -> dict[str, Any]:
    summary = TdxQuantIngestionService(db_path=db_path).ingest(
        start_date=start_date,
        end_date=end_date,
        only_main_board=True,
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
    return {
        "run_id": detail.run_id,
        "strategy_name": detail.strategy_key,
        "start_date": _date_text(detail.start_date),
        "end_date": _date_text(detail.end_date),
        "metrics": detail.metrics,
        "equity": equity,
        "drawdown": _drawdown_series(equity),
        "trades": _read_csv_rows(artifact_dir / "trades.csv"),
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
        lambda **kwargs: _default_ingest(resolved_db, start_date=kwargs["start_date"], end_date=kwargs["end_date"])
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
            while True:
                snapshot = slot.snapshot()
                payload = _job_payload(snapshot)
                count = 0 if snapshot is None else len(snapshot.events)
                status = None if snapshot is None else snapshot.status
                if count != last_count or status != last_status:
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    last_count = count
                    last_status = status
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
            result = app.state.ingest_fn(
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                db_path=resolved_db,
                days=body.days,
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
        return [
            {
                "run_id": item.run_id,
                "start_date": _date_text(item.start_date),
                "end_date": _date_text(item.end_date),
                "score": item.score,
            }
            for item in items
            if item.run_status == "completed"
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

            emit(
                "开始追加回测" if should_append else "开始策略回测",
                {
                    "strategy_name": loaded.name,
                    "start_date": body.start_date.isoformat(),
                    "end_date": body.end_date.isoformat(),
                    "run_id": resume_run_id,
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
                mode = "full"
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

    return app
