from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


RUN_KIND_SELECTION_BACKTEST = "selection_backtest"
RUN_KIND_STRATEGY_BACKTEST = "strategy_backtest"


@dataclass(frozen=True)
class SavedExperiment:
    run_id: int
    run_kind: str
    score: float
    score_version: str
    artifact_dir: Path


@dataclass(frozen=True)
class ExperimentArtifactRef:
    artifact_name: str
    artifact_type: str
    relative_path: str


@dataclass(frozen=True)
class ExperimentListItem:
    run_id: int
    strategy_key: str
    strategy_name: str
    run_kind: str
    start_date: date
    end_date: date
    run_status: str
    score: float | None
    created_at: datetime


@dataclass(frozen=True)
class ExperimentDetail:
    run_id: int
    strategy_key: str
    strategy_name: str
    run_kind: str
    run_name: str
    start_date: date
    end_date: date
    run_status: str
    score: float | None
    score_version: str | None
    created_at: datetime
    parameter_snapshot: dict[str, Any]
    metrics: dict[str, float | None]
    artifacts: list[ExperimentArtifactRef]
    latest_equity: dict[str, Any] | None
    trade_overview: dict[str, Any] | None


@dataclass(frozen=True)
class ExperimentMetricComparisonRow:
    metric_name: str
    left_value: float | None
    right_value: float | None
