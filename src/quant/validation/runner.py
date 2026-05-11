from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import pstdev
from typing import Any, Callable

from quant.backtest_common import connect, load_trade_dates_in_range, validate_trade_date
from quant.config.loader import LoadedStrategyConfig
from quant.evaluation.metrics import build_selection_backtest_metrics, build_strategy_backtest_metrics
from quant.evaluation.scoring import score_selection_backtest, score_strategy_backtest
from quant.experiments import ExperimentStore
from quant.experiments.models import SavedExperiment
from quant.experiments.store import _serialize_for_json
from quant.optimization.search_space import apply_parameter_overrides
from quant.pattern_selection import PatternSelectionError
from quant.strategy_runtime import (
    StrategyRuntimeError,
    run_loaded_strategy_backtest,
    run_loaded_strategy_selection_backtest,
)
from quant.validation.robustness import PerturbationTrial, build_perturbation_trials
from quant.validation.splits import (
    DateSegment,
    WalkForwardWindow,
    build_split_segments,
    build_walk_forward_windows,
)


VALIDATION_TARGET_STRATEGY = "strategy_backtest"
VALIDATION_TARGET_SELECTION = "selection_backtest"
VALIDATION_MODE_ALL = "all"
VALIDATION_MODE_SPLIT = "split"
VALIDATION_MODE_WALK_FORWARD = "walk-forward"
VALIDATION_MODE_PERTURBATION = "perturbation"


class ValidationError(ValueError):
    """Raised when validation cannot be executed."""


@dataclass(frozen=True)
class ValidationRunResult:
    label: str
    start_date: date
    end_date: date
    score: float
    score_version: str
    run_id: int | None


@dataclass(frozen=True)
class ValidationResult:
    strategy_name: str
    target: str
    mode: str
    start_date: date
    end_date: date
    results_dir: Path
    train_score: float | None
    validation_score: float | None
    test_score: float | None
    stability_score: float | None
    overfit_penalty: float | None
    split_rows: list[dict[str, Any]]
    walk_forward_rows: list[dict[str, Any]]
    perturbation_rows: list[dict[str, Any]]


def validate_loaded_strategy(
    *,
    db_path: Path,
    loaded: LoadedStrategyConfig,
    target: str,
    mode: str,
    start_date: date,
    end_date: date,
    split_ratios: tuple[int, int, int],
    walk_folds: int,
    max_perturbations: int,
    seed: int | None,
    save_experiments: bool,
    results_dir: Path,
    progress_callback: Callable[[str], None] | None = None,
) -> ValidationResult:
    if target not in {VALIDATION_TARGET_STRATEGY, VALIDATION_TARGET_SELECTION}:
        raise ValidationError("target must be one of: strategy_backtest, selection_backtest.")
    if mode not in {
        VALIDATION_MODE_ALL,
        VALIDATION_MODE_SPLIT,
        VALIDATION_MODE_WALK_FORWARD,
        VALIDATION_MODE_PERTURBATION,
    }:
        raise ValidationError("mode must be one of: all, split, walk-forward, perturbation.")
    if start_date > end_date:
        raise ValidationError("start_date must be on or before end_date.")
    _assert_target_defaults(loaded, target)
    trade_dates = _load_trade_dates(db_path, start_date, end_date)
    results_dir.mkdir(parents=True, exist_ok=True)
    experiment_store = ExperimentStore(db_path) if save_experiments else None

    split_rows: list[dict[str, Any]] = []
    walk_forward_rows: list[dict[str, Any]] = []
    perturbation_rows: list[dict[str, Any]] = []
    split_scores: dict[str, float] = {}

    if mode in {VALIDATION_MODE_ALL, VALIDATION_MODE_SPLIT}:
        segments = build_split_segments(trade_dates, split_ratios)
        split_rows, split_scores = _run_split_validation(
            db_path=db_path,
            loaded=loaded,
            target=target,
            segments=segments,
            experiment_store=experiment_store,
            progress_callback=progress_callback,
        )
        _write_csv(results_dir / "split_segments.csv", split_rows)

    if mode in {VALIDATION_MODE_ALL, VALIDATION_MODE_WALK_FORWARD}:
        windows = build_walk_forward_windows(trade_dates, walk_folds)
        walk_forward_rows = _run_walk_forward_validation(
            db_path=db_path,
            loaded=loaded,
            target=target,
            windows=windows,
            experiment_store=experiment_store,
            progress_callback=progress_callback,
        )
        _write_csv(results_dir / "walk_forward.csv", walk_forward_rows)

    if mode in {VALIDATION_MODE_ALL, VALIDATION_MODE_PERTURBATION}:
        trials = build_perturbation_trials(
            loaded,
            max_perturbations=max_perturbations,
            seed=seed,
        )
        if trials:
            perturbation_rows = _run_perturbation_validation(
                db_path=db_path,
                loaded=loaded,
                target=target,
                start_date=start_date,
                end_date=end_date,
                trials=trials,
                experiment_store=experiment_store,
                progress_callback=progress_callback,
            )
            _write_csv(results_dir / "perturbations.csv", perturbation_rows)

    train_score = split_scores.get("train")
    validation_score = split_scores.get("validation")
    test_score = split_scores.get("test")
    stability_score = _build_stability_score(perturbation_rows)
    overfit_penalty = _build_overfit_penalty(train_score, validation_score, test_score)
    result = ValidationResult(
        strategy_name=loaded.name,
        target=target,
        mode=mode,
        start_date=start_date,
        end_date=end_date,
        results_dir=results_dir,
        train_score=train_score,
        validation_score=validation_score,
        test_score=test_score,
        stability_score=stability_score,
        overfit_penalty=overfit_penalty,
        split_rows=split_rows,
        walk_forward_rows=walk_forward_rows,
        perturbation_rows=perturbation_rows,
    )
    _write_summary(results_dir / "summary.json", result)
    return result


def default_validation_results_dir(
    strategy_name: str,
    target: str,
    start_date: date,
    end_date: date,
) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return (
        Path("results")
        / "validation"
        / f"{strategy_name}_{target}_{start_date.isoformat()}_{end_date.isoformat()}_{timestamp}"
    )


def _assert_target_defaults(loaded: LoadedStrategyConfig, target: str) -> None:
    if target == VALIDATION_TARGET_STRATEGY and loaded.config.strategy_backtest is None:
        raise ValidationError(f"Strategy '{loaded.name}' does not define strategy_backtest defaults.")
    if target == VALIDATION_TARGET_SELECTION and loaded.config.backtest_selection is None:
        raise ValidationError(f"Strategy '{loaded.name}' does not define backtest_selection defaults.")


def _load_trade_dates(db_path: Path, start_date: date, end_date: date) -> list[date]:
    with connect(db_path) as connection:
        validate_trade_date(connection, start_date, ValidationError)
        validate_trade_date(connection, end_date, ValidationError)
        trade_dates = load_trade_dates_in_range(connection, start_date, end_date)
    if not trade_dates:
        raise ValidationError("No trading dates found in validation range.")
    return trade_dates


def _run_split_validation(
    *,
    db_path: Path,
    loaded: LoadedStrategyConfig,
    target: str,
    segments: list[DateSegment],
    experiment_store: ExperimentStore | None,
    progress_callback: Callable[[str], None] | None,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    rows: list[dict[str, Any]] = []
    scores: dict[str, float] = {}
    for segment in segments:
        if progress_callback is not None:
            progress_callback(f"[split] running {segment.name}")
        run = _run_validation_backtest(
            db_path=db_path,
            loaded=loaded,
            target=target,
            start_date=segment.start_date,
            end_date=segment.end_date,
            experiment_store=experiment_store,
            run_name=f"{loaded.name}__validate__{target}__split__{segment.name}",
            metadata={
                "entrypoint": "quant validate",
                "validation_mode": "split",
                "validation_segment": segment.name,
                "validation_base_strategy": loaded.name,
                "validation_target": target,
            },
        )
        scores[segment.name] = run.score
        rows.append(
            {
                "segment": segment.name,
                "start_date": segment.start_date,
                "end_date": segment.end_date,
                "trade_days": segment.trade_days,
                "score": run.score,
                "score_version": run.score_version,
                "run_id": run.run_id,
            }
        )
    return rows, scores


def _run_walk_forward_validation(
    *,
    db_path: Path,
    loaded: LoadedStrategyConfig,
    target: str,
    windows: list[WalkForwardWindow],
    experiment_store: ExperimentStore | None,
    progress_callback: Callable[[str], None] | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for window in windows:
        if progress_callback is not None:
            progress_callback(f"[walk-forward] running fold {window.fold}")
        train_run = _run_validation_backtest(
            db_path=db_path,
            loaded=loaded,
            target=target,
            start_date=window.train_start_date,
            end_date=window.train_end_date,
            experiment_store=experiment_store,
            run_name=f"{loaded.name}__validate__{target}__walk_forward__fold_{window.fold}__train",
            metadata={
                "entrypoint": "quant validate",
                "validation_mode": "walk-forward",
                "validation_segment": "train",
                "validation_base_strategy": loaded.name,
                "validation_target": target,
                "validation_fold": window.fold,
            },
        )
        test_run = _run_validation_backtest(
            db_path=db_path,
            loaded=loaded,
            target=target,
            start_date=window.test_start_date,
            end_date=window.test_end_date,
            experiment_store=experiment_store,
            run_name=f"{loaded.name}__validate__{target}__walk_forward__fold_{window.fold}__test",
            metadata={
                "entrypoint": "quant validate",
                "validation_mode": "walk-forward",
                "validation_segment": "test",
                "validation_base_strategy": loaded.name,
                "validation_target": target,
                "validation_fold": window.fold,
            },
        )
        rows.append(
            {
                "fold": window.fold,
                "train_start_date": window.train_start_date,
                "train_end_date": window.train_end_date,
                "test_start_date": window.test_start_date,
                "test_end_date": window.test_end_date,
                "train_trade_days": window.train_trade_days,
                "test_trade_days": window.test_trade_days,
                "train_score": train_run.score,
                "test_score": test_run.score,
                "train_run_id": train_run.run_id,
                "test_run_id": test_run.run_id,
                "score_version": test_run.score_version,
            }
        )
    return rows


def _run_perturbation_validation(
    *,
    db_path: Path,
    loaded: LoadedStrategyConfig,
    target: str,
    start_date: date,
    end_date: date,
    trials: list[PerturbationTrial],
    experiment_store: ExperimentStore | None,
    progress_callback: Callable[[str], None] | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trial in trials:
        if progress_callback is not None:
            progress_callback(f"[perturbation] running trial {trial.trial_index}")
        perturbed = apply_parameter_overrides(loaded, trial.overrides)
        run = _run_validation_backtest(
            db_path=db_path,
            loaded=perturbed,
            target=target,
            start_date=start_date,
            end_date=end_date,
            experiment_store=experiment_store,
            run_name=f"{loaded.name}__validate__{target}__perturbation__trial_{trial.trial_index}",
            metadata={
                "entrypoint": "quant validate",
                "validation_mode": "perturbation",
                "validation_segment": f"trial_{trial.trial_index}",
                "validation_base_strategy": loaded.name,
                "validation_target": target,
                "validation_perturbation_path": trial.parameter_path,
                "validation_base_value": _serialize_for_json(trial.base_value),
                "validation_perturbation_value": _serialize_for_json(trial.perturbation_value),
            },
        )
        rows.append(
            {
                "trial_index": trial.trial_index,
                "parameter_path": trial.parameter_path,
                "base_value": trial.base_value,
                "perturbation_value": trial.perturbation_value,
                "score": run.score,
                "score_version": run.score_version,
                "run_id": run.run_id,
            }
        )
    return rows


def _run_validation_backtest(
    *,
    db_path: Path,
    loaded: LoadedStrategyConfig,
    target: str,
    start_date: date,
    end_date: date,
    experiment_store: ExperimentStore | None,
    run_name: str,
    metadata: dict[str, Any],
) -> ValidationRunResult:
    try:
        if target == VALIDATION_TARGET_SELECTION:
            report = run_loaded_strategy_selection_backtest(
                db_path=db_path,
                loaded=loaded,
                start_date=start_date,
                end_date=end_date,
            )
            metrics = build_selection_backtest_metrics(report)
            score = score_selection_backtest(metrics)
            saved = (
                experiment_store.save_selection_backtest(
                    loaded,
                    report,
                    run_name=run_name,
                    metadata=metadata,
                )
                if experiment_store is not None
                else None
            )
        else:
            report = run_loaded_strategy_backtest(
                db_path=db_path,
                loaded=loaded,
                start_date=start_date,
                end_date=end_date,
            )
            metrics = build_strategy_backtest_metrics(report)
            score = score_strategy_backtest(metrics)
            saved = (
                experiment_store.save_strategy_backtest(
                    loaded,
                    report,
                    run_name=run_name,
                    metadata=metadata,
                )
                if experiment_store is not None
                else None
            )
    except (PatternSelectionError, StrategyRuntimeError, ValueError) as exc:
        raise ValidationError(str(exc)) from exc
    return ValidationRunResult(
        label=run_name,
        start_date=start_date,
        end_date=end_date,
        score=score.score,
        score_version=score.score_version,
        run_id=_saved_run_id(saved),
    )


def _saved_run_id(saved: SavedExperiment | None) -> int | None:
    return saved.run_id if saved is not None else None


def _build_overfit_penalty(
    train_score: float | None,
    validation_score: float | None,
    test_score: float | None,
) -> float | None:
    if train_score is None or validation_score is None or test_score is None:
        return None
    return round(max(0.0, train_score - min(validation_score, test_score)), 4)


def _build_stability_score(rows: list[dict[str, Any]]) -> float | None:
    scores = [float(row["score"]) for row in rows if row.get("score") is not None]
    if not scores:
        return None
    if len(scores) == 1:
        return 100.0
    return round(max(0.0, 100.0 - pstdev(scores)), 4)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _serialize_for_json(value) for key, value in row.items()})


def _write_summary(path: Path, result: ValidationResult) -> None:
    payload = {
        "strategy_name": result.strategy_name,
        "target": result.target,
        "mode": result.mode,
        "start_date": result.start_date,
        "end_date": result.end_date,
        "train_score": result.train_score,
        "validation_score": result.validation_score,
        "test_score": result.test_score,
        "stability_score": result.stability_score,
        "overfit_penalty": result.overfit_penalty,
        "split_segments": result.split_rows,
        "walk_forward": result.walk_forward_rows,
        "perturbations": result.perturbation_rows,
    }
    path.write_text(
        json.dumps(_serialize_for_json(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
