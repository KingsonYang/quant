from __future__ import annotations

from dataclasses import dataclass


SCORE_VERSION = "v1"


@dataclass(frozen=True)
class ScoreResult:
    score: float
    score_version: str = SCORE_VERSION


def _normalize(value: float | int | None, low: float, high: float, *, inverse: bool = False) -> float:
    if value is None:
        return 0.0
    numeric = float(value)
    if high <= low:
        normalized = 0.0
    elif numeric <= low:
        normalized = 0.0
    elif numeric >= high:
        normalized = 1.0
    else:
        normalized = (numeric - low) / (high - low)
    return 1.0 - normalized if inverse else normalized


def score_selection_backtest(metrics: dict[str, float | int | None]) -> ScoreResult:
    score = (
        _normalize(metrics.get("average_return_pct"), -10.0, 15.0) * 0.35
        + _normalize(metrics.get("win_rate_pct"), 30.0, 80.0) * 0.20
        + _normalize(metrics.get("median_return_pct"), -10.0, 15.0) * 0.15
        + _normalize(metrics.get("sample_count"), 5.0, 100.0) * 0.15
        + _normalize(metrics.get("return_std_pct"), 0.0, 20.0, inverse=True) * 0.15
    )
    return ScoreResult(score=round(score * 100, 4))


def score_strategy_backtest(metrics: dict[str, float | int | None]) -> ScoreResult:
    score = (
        _normalize(metrics.get("annualized_return_pct"), -50.0, 150.0) * 0.25
        + _normalize(metrics.get("max_drawdown_pct"), 0.0, 50.0, inverse=True) * 0.20
        + _normalize(metrics.get("sharpe_ratio"), -1.0, 3.0) * 0.20
        + _normalize(metrics.get("sortino_ratio"), -1.0, 4.0) * 0.10
        + _normalize(metrics.get("calmar_ratio"), -1.0, 3.0) * 0.10
        + _normalize(metrics.get("profit_factor"), 0.5, 3.0) * 0.10
        + _normalize(metrics.get("closed_trades"), 5.0, 100.0) * 0.05
    )
    return ScoreResult(score=round(score * 100, 4))
