from __future__ import annotations

import csv
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from quant.backtest import StockSelectionBacktestReport
from quant.config.loader import LoadedStrategyConfig
from quant.evaluation.metrics import (
    build_selection_backtest_metrics,
    build_selection_yearly_metrics,
    build_strategy_backtest_metrics,
    build_strategy_yearly_metrics,
)
from quant.evaluation.scoring import SCORE_VERSION, score_selection_backtest, score_strategy_backtest
from quant.experiments.models import (
    RUN_KIND_SELECTION_BACKTEST,
    RUN_KIND_STRATEGY_BACKTEST,
    ExperimentArtifactRef,
    ExperimentDetail,
    ExperimentListItem,
    SavedExperiment,
)
from quant.continuation import continuation_parameter_hash
from quant.strategy_backtest import (
    StrategyBacktestEquityPoint,
    StrategyBacktestReport,
    StrategyBacktestResumePosition,
    StrategyBacktestResumeState,
    StrategyBacktestTrade,
)


DEFAULT_EXPERIMENTS_DIR = Path("results/experiments")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_for_json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: _serialize_for_json(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _serialize_for_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_for_json(item) for item in value]
    return value


def _parse_iso_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value)[:10])


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _parse_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _deserialize_resume_state(payload: dict[str, Any]) -> StrategyBacktestResumeState:
    positions = tuple(
        StrategyBacktestResumePosition(
            symbol=item["symbol"],
            name=item["name"],
            selection_date=_parse_iso_date(item["selection_date"]),
            buy_date=_parse_iso_date(item["buy_date"]),
            buy_price=_parse_decimal(item["buy_price"]),
            shares=int(item["shares"]),
            buy_amount=_parse_decimal(item["buy_amount"]),
            buy_time=_parse_optional_datetime(item.get("buy_time")),
        )
        for item in payload.get("open_positions", [])
    )
    closed_trades = tuple(
        StrategyBacktestTrade(
            symbol=item["symbol"],
            name=item["name"],
            selection_date=_parse_iso_date(item["selection_date"]),
            buy_date=_parse_iso_date(item["buy_date"]),
            sell_date=None if item.get("sell_date") in (None, "") else _parse_iso_date(item["sell_date"]),
            buy_price=_parse_decimal(item["buy_price"]),
            sell_price=None if item.get("sell_price") in (None, "") else _parse_decimal(item["sell_price"]),
            shares=int(item["shares"]),
            lots=int(item["lots"]),
            buy_amount=_parse_decimal(item["buy_amount"]),
            sell_amount=None if item.get("sell_amount") in (None, "") else _parse_decimal(item["sell_amount"]),
            holding_days=int(item["holding_days"]),
            return_pct=None if item.get("return_pct") in (None, "") else _parse_decimal(item["return_pct"]),
            status=item["status"],
            exit_reason=item.get("exit_reason"),
            buy_time=_parse_optional_datetime(item.get("buy_time")),
            sell_time=_parse_optional_datetime(item.get("sell_time")),
        )
        for item in payload.get("closed_trades", [])
    )
    equity_curve = tuple(
        StrategyBacktestEquityPoint(
            trade_date=_parse_iso_date(item["trade_date"]),
            cash_balance=_parse_decimal(item["cash_balance"]),
            market_value=_parse_decimal(item["market_value"]),
            total_assets=_parse_decimal(item["total_assets"]),
        )
        for item in payload.get("equity_curve", [])
    )
    return StrategyBacktestResumeState(
        original_start_date=_parse_iso_date(payload["original_start_date"]),
        last_end_date=_parse_iso_date(payload["last_end_date"]),
        settled_cash=_parse_decimal(payload["settled_cash"]),
        unsettled_cash=_parse_decimal(payload["unsettled_cash"]),
        open_positions=positions,
        closed_trades=closed_trades,
        equity_curve=equity_curve,
    )


def ensure_experiment_schema(connection: duckdb.DuckDBPyConnection) -> None:
    backtest_run_exists = connection.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_name = 'backtest_run'
        """
    ).fetchone()
    if backtest_run_exists is None:
        return
    connection.execute("ALTER TABLE backtest_run ADD COLUMN IF NOT EXISTS run_kind VARCHAR")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS run_metric (
            run_metric_id BIGINT PRIMARY KEY,
            run_id BIGINT NOT NULL,
            metric_name VARCHAR NOT NULL,
            metric_value DOUBLE,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, metric_name),
            FOREIGN KEY (run_id) REFERENCES backtest_run(run_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS run_artifact (
            artifact_id BIGINT PRIMARY KEY,
            run_id BIGINT NOT NULL,
            artifact_name VARCHAR NOT NULL,
            artifact_type VARCHAR NOT NULL,
            relative_path TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, artifact_name),
            FOREIGN KEY (run_id) REFERENCES backtest_run(run_id)
        )
        """
    )


class ExperimentStore:
    def __init__(
        self,
        db_path: Path,
        *,
        artifacts_root: Path = DEFAULT_EXPERIMENTS_DIR,
    ) -> None:
        self.db_path = db_path
        self.artifacts_root = artifacts_root

    def connect(self) -> duckdb.DuckDBPyConnection:
        connection = duckdb.connect(str(self.db_path))
        ensure_experiment_schema(connection)
        return connection

    def _next_id(self, connection: duckdb.DuckDBPyConnection, table: str, column: str) -> int:
        return int(
            connection.execute(
                f"SELECT COALESCE(MAX({column}), 0) + 1 FROM {table}"
            ).fetchone()[0]
        )

    def _ensure_strategy_definition(
        self,
        connection: duckdb.DuckDBPyConnection,
        loaded: LoadedStrategyConfig,
    ) -> int:
        version = f"v{loaded.config.version}"
        existing = connection.execute(
            """
            SELECT strategy_id
            FROM strategy_definition
            WHERE strategy_key = ? AND version = ?
            """,
            [loaded.name, version],
        ).fetchone()
        strategy_type = loaded.config.selection.type
        if loaded.config.selection.type == "pattern_sql":
            strategy_type = loaded.config.selection.pattern_id
        strategy_name = loaded.config.strategy.display_name or loaded.name
        description = loaded.config.strategy.description
        parameter_schema = json.dumps(
            loaded.config.model_json_schema(),
            ensure_ascii=False,
            sort_keys=True,
        )
        if existing is not None:
            strategy_id = int(existing[0])
            connection.execute(
                """
                UPDATE strategy_definition
                SET strategy_name = ?,
                    strategy_type = ?,
                    description = ?,
                    parameter_schema = ?,
                    updated_at = ?
                WHERE strategy_id = ?
                """,
                [strategy_name, strategy_type, description, parameter_schema, _utcnow(), strategy_id],
            )
            return strategy_id

        strategy_id = self._next_id(connection, "strategy_definition", "strategy_id")
        now = _utcnow()
        connection.execute(
            """
            INSERT INTO strategy_definition (
                strategy_id,
                strategy_name,
                strategy_key,
                strategy_type,
                description,
                parameter_schema,
                version,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                strategy_id,
                strategy_name,
                loaded.name,
                strategy_type,
                description,
                parameter_schema,
                version,
                now,
                now,
            ],
        )
        return strategy_id

    def _default_run_name(
        self,
        loaded: LoadedStrategyConfig,
        run_kind: str,
        start_date: date,
        end_date: date,
    ) -> str:
        return f"{loaded.name}_{run_kind}_{start_date.isoformat()}_{end_date.isoformat()}"

    def _insert_backtest_run(
        self,
        connection: duckdb.DuckDBPyConnection,
        *,
        strategy_id: int,
        run_kind: str,
        run_name: str,
        start_date: date,
        end_date: date,
        universe_definition: dict[str, Any],
        parameter_snapshot: dict[str, Any],
        initial_cash: Decimal,
    ) -> int:
        run_id = self._next_id(connection, "backtest_run", "run_id")
        now = _utcnow()
        connection.execute(
            """
            INSERT INTO backtest_run (
                run_id,
                strategy_id,
                run_kind,
                run_name,
                start_date,
                end_date,
                benchmark_symbol,
                universe_definition,
                parameter_snapshot,
                initial_cash,
                commission_rate,
                tax_rate,
                slippage_rate,
                run_status,
                started_at,
                finished_at,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                strategy_id,
                run_kind,
                run_name,
                start_date,
                end_date,
                None,
                json.dumps(universe_definition, ensure_ascii=False, sort_keys=True),
                json.dumps(
                    _serialize_for_json(parameter_snapshot),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                initial_cash,
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                "completed",
                now,
                now,
                now,
            ],
        )
        return run_id

    def _write_metrics(
        self,
        connection: duckdb.DuckDBPyConnection,
        run_id: int,
        metrics: dict[str, float | int | None],
    ) -> None:
        now = _utcnow()
        next_id = self._next_id(connection, "run_metric", "run_metric_id")
        rows: list[tuple[int, int, str, float | None, datetime]] = []
        for metric_name, metric_value in metrics.items():
            rows.append(
                (
                    next_id,
                    run_id,
                    metric_name,
                    None if metric_value is None else float(metric_value),
                    now,
                )
            )
            next_id += 1
        connection.executemany(
            """
            INSERT INTO run_metric (
                run_metric_id,
                run_id,
                metric_name,
                metric_value,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )

    def _write_artifacts(
        self,
        connection: duckdb.DuckDBPyConnection,
        run_id: int,
        artifact_dir: Path,
        artifact_refs: list[ExperimentArtifactRef],
    ) -> None:
        now = _utcnow()
        next_id = self._next_id(connection, "run_artifact", "artifact_id")
        rows = [
            (
                next_id + index,
                run_id,
                artifact.artifact_name,
                artifact.artifact_type,
                artifact.relative_path,
                now,
            )
            for index, artifact in enumerate(artifact_refs)
        ]
        if rows:
            connection.executemany(
                """
                INSERT INTO run_artifact (
                    artifact_id,
                    run_id,
                    artifact_name,
                    artifact_type,
                    relative_path,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def _prepare_artifact_dir(self, run_id: int) -> Path:
        artifact_dir = self.artifacts_root / f"run_{run_id}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        return artifact_dir

    def _artifact_ref(self, artifact_path: Path, *, artifact_name: str, artifact_type: str) -> ExperimentArtifactRef:
        try:
            relative_path = artifact_path.relative_to(Path.cwd()).as_posix()
        except ValueError:
            relative_path = artifact_path.as_posix()
        return ExperimentArtifactRef(
            artifact_name=artifact_name,
            artifact_type=artifact_type,
            relative_path=relative_path,
        )

    def _write_json_artifact(self, artifact_dir: Path, filename: str, payload: Any) -> Path:
        path = artifact_dir / filename
        path.write_text(
            json.dumps(_serialize_for_json(payload), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def _write_csv_artifact(self, artifact_dir: Path, filename: str, rows: list[dict[str, Any]]) -> Path:
        path = artifact_dir / filename
        fieldnames = sorted({key for row in rows for key in row.keys()})
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _serialize_for_json(value) for key, value in row.items()})
        return path

    def _selection_parameter_snapshot(
        self,
        loaded: LoadedStrategyConfig,
        report: StockSelectionBacktestReport,
        run_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = {
            "strategy": loaded.config.model_dump(mode="python"),
            "strategy_file": str(loaded.path.resolve()),
            "request": _serialize_for_json(report.request),
            "summary": _serialize_for_json(report.summary),
            "run_name": run_name,
            "run_kind": RUN_KIND_SELECTION_BACKTEST,
            "score_version": SCORE_VERSION,
            "entrypoint": "quant run backtest-selection",
        }
        if metadata:
            snapshot.update(metadata)
        return snapshot

    def _strategy_parameter_snapshot(
        self,
        loaded: LoadedStrategyConfig,
        report: StrategyBacktestReport,
        run_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = {
            "strategy": loaded.config.model_dump(mode="python"),
            "strategy_file": str(loaded.path.resolve()),
            "request": _serialize_for_json(report.request),
            "summary": _serialize_for_json(report.summary),
            "run_name": run_name,
            "run_kind": RUN_KIND_STRATEGY_BACKTEST,
            "score_version": SCORE_VERSION,
            "entrypoint": "quant run backtest-strategy",
        }
        if metadata:
            snapshot.update(metadata)
        return snapshot

    def save_selection_backtest(
        self,
        loaded: LoadedStrategyConfig,
        report: StockSelectionBacktestReport,
        *,
        run_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SavedExperiment:
        with self.connect() as connection:
            strategy_id = self._ensure_strategy_definition(connection, loaded)
            effective_run_name = run_name or self._default_run_name(
                loaded,
                RUN_KIND_SELECTION_BACKTEST,
                report.summary.start_date,
                report.summary.end_date,
            )
            parameter_snapshot = self._selection_parameter_snapshot(
                loaded,
                report,
                effective_run_name,
                metadata=metadata,
            )
            run_id = self._insert_backtest_run(
                connection,
                strategy_id=strategy_id,
                run_kind=RUN_KIND_SELECTION_BACKTEST,
                run_name=effective_run_name,
                start_date=report.summary.start_date,
                end_date=report.summary.end_date,
                universe_definition={
                    "selection_type": loaded.config.selection.type,
                    "sort_by": loaded.config.selection.sort_by,
                    "sort_order": loaded.config.selection.sort_order,
                },
                parameter_snapshot=parameter_snapshot,
                initial_cash=Decimal("0"),
            )
            metrics = build_selection_backtest_metrics(report)
            score = score_selection_backtest(metrics)
            all_metrics = dict(metrics)
            all_metrics["score"] = score.score
            artifact_dir = self._prepare_artifact_dir(run_id)
            details_path = self._write_csv_artifact(
                artifact_dir,
                "details.csv",
                [_serialize_for_json(detail) for detail in report.details],
            )
            yearly_path = self._write_json_artifact(
                artifact_dir,
                "yearly_metrics.json",
                build_selection_yearly_metrics(report),
            )
            summary_path = self._write_json_artifact(
                artifact_dir,
                "summary.json",
                {
                    "request": report.request,
                    "summary": report.summary,
                    "metrics": all_metrics,
                    "score_version": score.score_version,
                },
            )
            self._write_metrics(connection, run_id, all_metrics)
            self._write_artifacts(
                connection,
                run_id,
                artifact_dir,
                [
                    self._artifact_ref(details_path, artifact_name="details", artifact_type="csv"),
                    self._artifact_ref(yearly_path, artifact_name="yearly_metrics", artifact_type="json"),
                    self._artifact_ref(summary_path, artifact_name="summary", artifact_type="json"),
                ],
            )
            return SavedExperiment(
                run_id=run_id,
                run_kind=RUN_KIND_SELECTION_BACKTEST,
                score=score.score,
                score_version=score.score_version,
                artifact_dir=artifact_dir,
            )

    def _load_security_ids(
        self,
        connection: duckdb.DuckDBPyConnection,
        symbols: set[str],
    ) -> dict[str, int]:
        if not symbols:
            return {}
        placeholders = ", ".join("?" for _ in symbols)
        rows = connection.execute(
            f"""
            SELECT symbol, security_id
            FROM security_master
            WHERE symbol IN ({placeholders})
            """,
            list(sorted(symbols)),
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def _close_prices_by_symbol_date(
        self,
        connection: duckdb.DuckDBPyConnection,
        symbols: set[str],
        start_date: date,
        end_date: date,
    ) -> dict[tuple[str, date], Decimal]:
        if not symbols:
            return {}
        placeholders = ", ".join("?" for _ in symbols)
        rows = connection.execute(
            f"""
            SELECT s.symbol, d.trade_date, d.close_price
            FROM daily_bar AS d
            JOIN security_master AS s
              ON s.security_id = d.security_id
            WHERE s.symbol IN ({placeholders})
              AND d.trade_date BETWEEN ? AND ?
            """,
            [*sorted(symbols), start_date, end_date],
        ).fetchall()
        return {(row[0], row[1]): row[2] for row in rows}

    def _write_strategy_orders_and_fills(
        self,
        connection: duckdb.DuckDBPyConnection,
        run_id: int,
        report: StrategyBacktestReport,
        security_ids: dict[str, int],
        after_date: date | None = None,
    ) -> dict[date, Decimal]:
        turnover_by_date: dict[date, Decimal] = {}
        for trade in report.trades:
            security_id = security_ids[trade.symbol]
            write_buy = after_date is None or trade.buy_date > after_date
            if write_buy:
                order_id = self._next_id(connection, "backtest_order", "order_id")
                connection.execute(
                    """
                    INSERT INTO backtest_order (
                        order_id, run_id, security_id, submitted_date, intended_exec_date,
                        side, order_type, quantity, target_weight, status, reject_reason, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        order_id,
                        run_id,
                        security_id,
                        trade.selection_date,
                        trade.buy_date,
                        "buy",
                        "market",
                        trade.shares,
                        None,
                        "filled",
                        None,
                        _utcnow(),
                    ],
                )
                fill_id = self._next_id(connection, "backtest_fill", "fill_id")
                connection.execute(
                    """
                    INSERT INTO backtest_fill (
                        fill_id, order_id, run_id, security_id, fill_date, fill_price,
                        fill_quantity, gross_amount, commission, tax, slippage_cost, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        fill_id,
                        order_id,
                        run_id,
                        security_id,
                        trade.buy_date,
                        trade.buy_price,
                        trade.shares,
                        trade.buy_amount,
                        Decimal("0"),
                        Decimal("0"),
                        Decimal("0"),
                        _utcnow(),
                    ],
                )
                turnover_by_date[trade.buy_date] = (
                    turnover_by_date.get(trade.buy_date, Decimal("0")) + trade.buy_amount
                )

            write_sell = (
                trade.status == "closed"
                and trade.sell_date is not None
                and trade.sell_price is not None
                and trade.sell_amount is not None
                and (after_date is None or trade.sell_date > after_date)
            )
            if not write_sell:
                continue

            sell_order_id = self._next_id(connection, "backtest_order", "order_id")
            connection.execute(
                """
                INSERT INTO backtest_order (
                    order_id, run_id, security_id, submitted_date, intended_exec_date,
                    side, order_type, quantity, target_weight, status, reject_reason, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    sell_order_id,
                    run_id,
                    security_id,
                    trade.sell_date,
                    trade.sell_date,
                    "sell",
                    "market",
                    trade.shares,
                    None,
                    "filled",
                    None,
                    _utcnow(),
                ],
            )
            sell_fill_id = self._next_id(connection, "backtest_fill", "fill_id")
            connection.execute(
                """
                INSERT INTO backtest_fill (
                    fill_id, order_id, run_id, security_id, fill_date, fill_price,
                    fill_quantity, gross_amount, commission, tax, slippage_cost, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    sell_fill_id,
                    sell_order_id,
                    run_id,
                    security_id,
                    trade.sell_date,
                    trade.sell_price,
                    trade.shares,
                    trade.sell_amount,
                    Decimal("0"),
                    Decimal("0"),
                    Decimal("0"),
                    _utcnow(),
                ],
            )
            turnover_by_date[trade.sell_date] = (
                turnover_by_date.get(trade.sell_date, Decimal("0")) + trade.sell_amount
            )
        return turnover_by_date

    def _write_position_snapshots(
        self,
        connection: duckdb.DuckDBPyConnection,
        run_id: int,
        report: StrategyBacktestReport,
        security_ids: dict[str, int],
        close_prices: dict[tuple[str, date], Decimal],
        after_date: date | None = None,
    ) -> None:
        for point in report.equity_curve:
            if after_date is not None and point.trade_date <= after_date:
                continue
            for trade in report.trades:
                if trade.buy_date > point.trade_date:
                    continue
                if trade.sell_date is not None and trade.sell_date <= point.trade_date:
                    continue
                security_id = security_ids[trade.symbol]
                close_price = close_prices.get((trade.symbol, point.trade_date), trade.buy_price)
                market_value = (close_price * Decimal(trade.shares)).quantize(Decimal("0.01"))
                unrealized_pnl = (market_value - trade.buy_amount).quantize(Decimal("0.01"))
                connection.execute(
                    """
                    INSERT INTO position_snapshot (
                        position_snapshot_id, run_id, trade_date, security_id, quantity,
                        sellable_quantity, avg_cost, close_price, market_value, unrealized_pnl, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        self._next_id(connection, "position_snapshot", "position_snapshot_id"),
                        run_id,
                        point.trade_date,
                        security_id,
                        trade.shares,
                        trade.shares,
                        trade.buy_price,
                        close_price,
                        market_value,
                        unrealized_pnl,
                        _utcnow(),
                    ],
                )

    def _write_equity_snapshots(
        self,
        connection: duckdb.DuckDBPyConnection,
        run_id: int,
        report: StrategyBacktestReport,
        turnover_by_date: dict[date, Decimal],
        after_date: date | None = None,
    ) -> None:
        max_total_assets = None
        previous_total_assets = None
        initial_capital = report.request.initial_capital
        for point in report.equity_curve:
            total_assets = point.total_assets
            max_total_assets = total_assets if max_total_assets is None else max(max_total_assets, total_assets)
            daily_pnl = Decimal("0")
            if previous_total_assets is not None:
                daily_pnl = total_assets - previous_total_assets
            cumulative_return = Decimal("0")
            if initial_capital > 0:
                cumulative_return = (total_assets - initial_capital) / initial_capital
            max_drawdown = None
            if max_total_assets and max_total_assets > 0:
                max_drawdown = (max_total_assets - total_assets) / max_total_assets
            turnover = None
            traded_amount = turnover_by_date.get(point.trade_date)
            if traded_amount is not None and previous_total_assets not in (None, Decimal("0")):
                turnover = traded_amount / previous_total_assets
            skip_insert = after_date is not None and point.trade_date <= after_date
            previous_total_assets = total_assets
            if skip_insert:
                continue
            connection.execute(
                """
                INSERT INTO equity_snapshot (
                    equity_snapshot_id, run_id, trade_date, cash, market_value, total_equity,
                    daily_pnl, cumulative_return, turnover, max_drawdown, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    self._next_id(connection, "equity_snapshot", "equity_snapshot_id"),
                    run_id,
                    point.trade_date,
                    point.cash_balance,
                    point.market_value,
                    total_assets,
                    daily_pnl.quantize(Decimal("0.01")),
                    cumulative_return.quantize(Decimal("0.000001")),
                    None if turnover is None else turnover.quantize(Decimal("0.000001")),
                    None if max_drawdown is None else max_drawdown.quantize(Decimal("0.000001")),
                    _utcnow(),
                ],
            )
            previous_total_assets = total_assets

    def save_strategy_backtest(
        self,
        loaded: LoadedStrategyConfig,
        report: StrategyBacktestReport,
        *,
        run_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SavedExperiment:
        with self.connect() as connection:
            strategy_id = self._ensure_strategy_definition(connection, loaded)
            effective_run_name = run_name or self._default_run_name(
                loaded,
                RUN_KIND_STRATEGY_BACKTEST,
                report.summary.start_date,
                report.summary.end_date,
            )
            parameter_snapshot = self._strategy_parameter_snapshot(
                loaded,
                report,
                effective_run_name,
                metadata=metadata,
            )
            run_id = self._insert_backtest_run(
                connection,
                strategy_id=strategy_id,
                run_kind=RUN_KIND_STRATEGY_BACKTEST,
                run_name=effective_run_name,
                start_date=report.summary.start_date,
                end_date=report.summary.end_date,
                universe_definition={
                    "selection_type": loaded.config.selection.type,
                    "sort_by": loaded.config.selection.sort_by,
                    "sort_order": loaded.config.selection.sort_order,
                    "continuation_hash": continuation_parameter_hash(loaded),
                },
                parameter_snapshot=parameter_snapshot,
                initial_cash=report.request.initial_capital,
            )
            metrics = build_strategy_backtest_metrics(report)
            score = score_strategy_backtest(metrics)
            all_metrics = dict(metrics)
            all_metrics["score"] = score.score
            artifact_dir = self._prepare_artifact_dir(run_id)
            trades_path = self._write_csv_artifact(
                artifact_dir,
                "trades.csv",
                [_serialize_for_json(trade) for trade in report.trades],
            )
            equity_path = self._write_csv_artifact(
                artifact_dir,
                "equity_curve.csv",
                [_serialize_for_json(point) for point in report.equity_curve],
            )
            yearly_path = self._write_json_artifact(
                artifact_dir,
                "yearly_metrics.json",
                build_strategy_yearly_metrics(report),
            )
            resume_path = self._write_json_artifact(
                artifact_dir,
                "resume_state.json",
                report.resume_state,
            )
            summary_path = self._write_json_artifact(
                artifact_dir,
                "summary.json",
                {
                    "request": report.request,
                    "summary": report.summary,
                    "metrics": all_metrics,
                    "score_version": score.score_version,
                },
            )
            symbols = {trade.symbol for trade in report.trades}
            security_ids = self._load_security_ids(connection, symbols)
            close_prices = self._close_prices_by_symbol_date(
                connection,
                symbols,
                report.summary.start_date,
                report.summary.end_date,
            )
            turnover_by_date = self._write_strategy_orders_and_fills(connection, run_id, report, security_ids)
            self._write_position_snapshots(connection, run_id, report, security_ids, close_prices)
            self._write_equity_snapshots(connection, run_id, report, turnover_by_date)
            self._write_metrics(connection, run_id, all_metrics)
            self._write_artifacts(
                connection,
                run_id,
                artifact_dir,
                [
                    self._artifact_ref(trades_path, artifact_name="trades", artifact_type="csv"),
                    self._artifact_ref(equity_path, artifact_name="equity_curve", artifact_type="csv"),
                    self._artifact_ref(yearly_path, artifact_name="yearly_metrics", artifact_type="json"),
                    self._artifact_ref(resume_path, artifact_name="resume_state", artifact_type="json"),
                    self._artifact_ref(summary_path, artifact_name="summary", artifact_type="json"),
                ],
            )
            return SavedExperiment(
                run_id=run_id,
                run_kind=RUN_KIND_STRATEGY_BACKTEST,
                score=score.score,
                score_version=score.score_version,
                artifact_dir=artifact_dir,
            )

    def find_continuable_strategy_run(
        self,
        *,
        strategy_key: str,
        start_date: date,
        parameter_hash: str,
    ) -> int | None:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT br.run_id, br.universe_definition
                FROM backtest_run AS br
                JOIN strategy_definition AS sd
                  ON sd.strategy_id = br.strategy_id
                WHERE sd.strategy_key = ?
                  AND br.start_date = ?
                  AND br.run_kind = ?
                  AND br.run_status = 'completed'
                ORDER BY br.run_id DESC
                """,
                [strategy_key, start_date, RUN_KIND_STRATEGY_BACKTEST],
            ).fetchall()
        for run_id, universe_raw in rows:
            universe = json.loads(universe_raw) if universe_raw else {}
            if universe.get("continuation_hash") == parameter_hash:
                return int(run_id)
        return None

    def load_resume_state(self, run_id: int) -> StrategyBacktestResumeState:
        artifact_path = self.artifacts_root / f"run_{run_id}" / "resume_state.json"
        if not artifact_path.exists():
            raise ValueError(f"Run {run_id} has no resume_state.json and cannot be continued.")
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        if payload is None:
            raise ValueError(f"Run {run_id} resume_state.json is empty.")
        return _deserialize_resume_state(payload)

    def append_strategy_backtest(
        self,
        run_id: int,
        loaded: LoadedStrategyConfig,
        new_segment: StrategyBacktestReport,
    ) -> SavedExperiment:
        expected_hash = continuation_parameter_hash(loaded)
        with self.connect() as connection:
            header = connection.execute(
                """
                SELECT br.start_date, br.end_date, br.universe_definition, br.run_status
                FROM backtest_run AS br
                WHERE br.run_id = ?
                """,
                [run_id],
            ).fetchone()
            if header is None:
                raise ValueError(f"Unknown run_id: {run_id}")
            old_start, old_end, universe_raw, run_status = header
            if run_status != "completed":
                raise ValueError(f"Run {run_id} is not completed.")
            universe = json.loads(universe_raw) if universe_raw else {}
            if universe.get("continuation_hash") != expected_hash:
                raise ValueError("Strategy parameters changed; cannot append this run.")
            if new_segment.summary.start_date != old_start:
                raise ValueError("Append start_date must match the original run start_date.")
            if new_segment.summary.end_date <= old_end:
                raise ValueError("Append end_date must be after the saved run end_date.")

            connection.execute("BEGIN TRANSACTION")
            try:
                metrics = build_strategy_backtest_metrics(new_segment)
                score = score_strategy_backtest(metrics)
                all_metrics = dict(metrics)
                all_metrics["score"] = score.score
                symbols = {trade.symbol for trade in new_segment.trades}
                security_ids = self._load_security_ids(connection, symbols)
                close_prices = self._close_prices_by_symbol_date(
                    connection,
                    symbols,
                    new_segment.summary.start_date,
                    new_segment.summary.end_date,
                )
                turnover_by_date = self._write_strategy_orders_and_fills(
                    connection,
                    run_id,
                    new_segment,
                    security_ids,
                    after_date=old_end,
                )
                self._write_position_snapshots(
                    connection,
                    run_id,
                    new_segment,
                    security_ids,
                    close_prices,
                    after_date=old_end,
                )
                self._write_equity_snapshots(
                    connection,
                    run_id,
                    new_segment,
                    turnover_by_date,
                    after_date=old_end,
                )
                connection.execute("DELETE FROM run_metric WHERE run_id = ?", [run_id])
                self._write_metrics(connection, run_id, all_metrics)
                artifact_dir = self._prepare_artifact_dir(run_id)
                trades_path = self._write_csv_artifact(
                    artifact_dir,
                    "trades.csv",
                    [_serialize_for_json(trade) for trade in new_segment.trades],
                )
                equity_path = self._write_csv_artifact(
                    artifact_dir,
                    "equity_curve.csv",
                    [_serialize_for_json(point) for point in new_segment.equity_curve],
                )
                yearly_path = self._write_json_artifact(
                    artifact_dir,
                    "yearly_metrics.json",
                    build_strategy_yearly_metrics(new_segment),
                )
                resume_path = self._write_json_artifact(
                    artifact_dir,
                    "resume_state.json",
                    new_segment.resume_state,
                )
                summary_path = self._write_json_artifact(
                    artifact_dir,
                    "summary.json",
                    {
                        "request": new_segment.request,
                        "summary": new_segment.summary,
                        "metrics": all_metrics,
                        "score_version": score.score_version,
                    },
                )
                connection.execute("DELETE FROM run_artifact WHERE run_id = ?", [run_id])
                self._write_artifacts(
                    connection,
                    run_id,
                    artifact_dir,
                    [
                        self._artifact_ref(trades_path, artifact_name="trades", artifact_type="csv"),
                        self._artifact_ref(equity_path, artifact_name="equity_curve", artifact_type="csv"),
                        self._artifact_ref(yearly_path, artifact_name="yearly_metrics", artifact_type="json"),
                        self._artifact_ref(resume_path, artifact_name="resume_state", artifact_type="json"),
                        self._artifact_ref(summary_path, artifact_name="summary", artifact_type="json"),
                    ],
                )
                connection.execute(
                    "UPDATE backtest_run SET end_date = ?, finished_at = ? WHERE run_id = ?",
                    [new_segment.summary.end_date, _utcnow(), run_id],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            return SavedExperiment(
                run_id=run_id,
                run_kind=RUN_KIND_STRATEGY_BACKTEST,
                score=score.score,
                score_version=score.score_version,
                artifact_dir=self._prepare_artifact_dir(run_id),
            )

    def list_runs(
        self,
        *,
        strategy_name: str | None = None,
        run_kind: str | None = None,
        limit: int = 20,
    ) -> list[ExperimentListItem]:
        with self.connect() as connection:
            clauses = ["1 = 1"]
            params: list[Any] = []
            if strategy_name is not None:
                clauses.append("sd.strategy_key = ?")
                params.append(strategy_name)
            if run_kind is not None:
                clauses.append("br.run_kind = ?")
                params.append(run_kind)
            params.append(limit)
            rows = connection.execute(
                f"""
                SELECT
                    br.run_id,
                    sd.strategy_key,
                    sd.strategy_name,
                    br.run_kind,
                    br.start_date,
                    br.end_date,
                    br.run_status,
                    rm.metric_value AS score,
                    br.created_at
                FROM backtest_run AS br
                JOIN strategy_definition AS sd
                  ON sd.strategy_id = br.strategy_id
                LEFT JOIN run_metric AS rm
                  ON rm.run_id = br.run_id AND rm.metric_name = 'score'
                WHERE {' AND '.join(clauses)}
                ORDER BY br.run_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            ExperimentListItem(
                run_id=int(row[0]),
                strategy_key=row[1],
                strategy_name=row[2],
                run_kind=row[3],
                start_date=row[4],
                end_date=row[5],
                run_status=row[6],
                score=None if row[7] is None else float(row[7]),
                created_at=row[8],
            )
            for row in rows
        ]

    def load_run_detail(self, run_id: int) -> ExperimentDetail:
        with self.connect() as connection:
            header = connection.execute(
                """
                SELECT
                    br.run_id,
                    sd.strategy_key,
                    sd.strategy_name,
                    br.run_kind,
                    br.run_name,
                    br.start_date,
                    br.end_date,
                    br.run_status,
                    br.created_at,
                    br.parameter_snapshot
                FROM backtest_run AS br
                JOIN strategy_definition AS sd
                  ON sd.strategy_id = br.strategy_id
                WHERE br.run_id = ?
                """,
                [run_id],
            ).fetchone()
            if header is None:
                raise ValueError(f"Experiment run_id {run_id} not found.")
            metric_rows = connection.execute(
                """
                SELECT metric_name, metric_value
                FROM run_metric
                WHERE run_id = ?
                ORDER BY metric_name
                """,
                [run_id],
            ).fetchall()
            artifact_rows = connection.execute(
                """
                SELECT artifact_name, artifact_type, relative_path
                FROM run_artifact
                WHERE run_id = ?
                ORDER BY artifact_name
                """,
                [run_id],
            ).fetchall()
            latest_equity_row = connection.execute(
                """
                SELECT trade_date, cash, market_value, total_equity, cumulative_return, max_drawdown
                FROM equity_snapshot
                WHERE run_id = ?
                ORDER BY trade_date DESC
                LIMIT 1
                """,
                [run_id],
            ).fetchone()
        parameter_snapshot = json.loads(header[9])
        metrics = {row[0]: None if row[1] is None else float(row[1]) for row in metric_rows}
        latest_equity = None
        if latest_equity_row is not None:
            latest_equity = {
                "trade_date": latest_equity_row[0],
                "cash": latest_equity_row[1],
                "market_value": latest_equity_row[2],
                "total_equity": latest_equity_row[3],
                "cumulative_return": latest_equity_row[4],
                "max_drawdown": latest_equity_row[5],
            }
        trade_overview = None
        if header[3] == RUN_KIND_STRATEGY_BACKTEST:
            trade_overview = {
                "closed_trades": metrics.get("closed_trades"),
                "total_trades": metrics.get("total_trades"),
                "open_positions": metrics.get("open_positions"),
                "win_rate_pct": metrics.get("win_rate_pct"),
            }
        return ExperimentDetail(
            run_id=int(header[0]),
            strategy_key=header[1],
            strategy_name=header[2],
            run_kind=header[3],
            run_name=header[4],
            start_date=header[5],
            end_date=header[6],
            run_status=header[7],
            score=metrics.get("score"),
            score_version=parameter_snapshot.get("score_version"),
            created_at=header[8],
            parameter_snapshot=parameter_snapshot,
            metrics=metrics,
            artifacts=[
                ExperimentArtifactRef(
                    artifact_name=row[0],
                    artifact_type=row[1],
                    relative_path=row[2],
                )
                for row in artifact_rows
            ],
            latest_equity=latest_equity,
            trade_overview=trade_overview,
        )

    def load_metric_map(self, run_id: int) -> dict[str, float | None]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT metric_name, metric_value
                FROM run_metric
                WHERE run_id = ?
                ORDER BY metric_name
                """,
                [run_id],
            ).fetchall()
        return {row[0]: None if row[1] is None else float(row[1]) for row in rows}
