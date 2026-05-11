from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Callable

from quant.strategy_backtest import (
    StrategyBacktestProgress,
    StrategyBacktestRequest,
    StrategyBacktestReport,
    run_strategy_backtest,
)


@dataclass(frozen=True)
class StrategyHoldingPeriodSummary:
    strategy_name: str
    holding_period: int
    start_date: date
    end_date: date
    closed_trade_count: int
    open_position_count: int
    total_trade_count: int
    realized_profit: Decimal
    unrealized_profit: Decimal
    current_total_assets: Decimal
    total_return_pct: Decimal
    annualized_return_pct: Decimal | None
    win_rate_pct: Decimal | None


def _win_rate_pct(report: StrategyBacktestReport) -> Decimal | None:
    closed_trades = [trade for trade in report.trades if trade.status == "closed"]
    if not closed_trades:
        return None
    positive_trades = sum(
        1
        for trade in closed_trades
        if trade.return_pct is not None and trade.return_pct > 0
    )
    return (
        Decimal(positive_trades) * Decimal("100") / Decimal(len(closed_trades))
    ).quantize(Decimal("0.0001"))


def _summary_from_report(
    strategy_name: str,
    report: StrategyBacktestReport,
) -> StrategyHoldingPeriodSummary:
    return StrategyHoldingPeriodSummary(
        strategy_name=strategy_name,
        holding_period=report.request.holding_period,
        start_date=report.summary.start_date,
        end_date=report.summary.end_date,
        closed_trade_count=report.summary.closed_trade_count,
        open_position_count=report.summary.open_position_count,
        total_trade_count=report.summary.total_trade_count,
        realized_profit=report.summary.realized_profit,
        unrealized_profit=report.summary.unrealized_profit,
        current_total_assets=report.summary.current_total_assets,
        total_return_pct=report.summary.total_return_pct,
        annualized_return_pct=report.summary.annualized_return_pct,
        win_rate_pct=_win_rate_pct(report),
    )


def _serialize_scalar(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _serialize_summary(summary: StrategyHoldingPeriodSummary) -> dict[str, str | int | None]:
    return {
        key: _serialize_scalar(value)
        for key, value in asdict(summary).items()
    }


def run_strategy_holding_period_matrix(
    *,
    strategy_name: str,
    db_path: Path,
    base_request: StrategyBacktestRequest,
    holding_periods: list[int],
    results_dir: Path,
    progress_callback: Callable[[StrategyBacktestProgress], None] | None = None,
) -> list[StrategyHoldingPeriodSummary]:
    results_dir.mkdir(parents=True, exist_ok=True)
    summaries: list[StrategyHoldingPeriodSummary] = []

    for holding_period in holding_periods:
        report = run_strategy_backtest(
            db_path=db_path,
            request=replace(base_request, holding_period=holding_period),
            progress_callback=progress_callback,
        )
        summary = _summary_from_report(strategy_name, report)
        summary_path = results_dir / f"holding_period_{holding_period}.json"
        payload = {
            "summary": _serialize_summary(summary),
            "request": {
                "strategy_name": strategy_name,
                "holding_period": holding_period,
            },
        }
        summary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        summaries.append(summary)

    return summaries


def load_strategy_holding_period_summaries(
    results_dir: Path,
) -> list[StrategyHoldingPeriodSummary]:
    summaries: list[StrategyHoldingPeriodSummary] = []
    for path in sorted(results_dir.glob("holding_period_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        summary_payload = payload.get("summary")
        if not isinstance(summary_payload, dict):
            raise ValueError(f"Missing summary payload in {path}")
        summaries.append(
            StrategyHoldingPeriodSummary(
                strategy_name=str(summary_payload["strategy_name"]),
                holding_period=int(summary_payload["holding_period"]),
                start_date=date.fromisoformat(str(summary_payload["start_date"])),
                end_date=date.fromisoformat(str(summary_payload["end_date"])),
                closed_trade_count=int(summary_payload["closed_trade_count"]),
                open_position_count=int(summary_payload["open_position_count"]),
                total_trade_count=int(summary_payload["total_trade_count"]),
                realized_profit=Decimal(str(summary_payload["realized_profit"])),
                unrealized_profit=Decimal(str(summary_payload["unrealized_profit"])),
                current_total_assets=Decimal(str(summary_payload["current_total_assets"])),
                total_return_pct=Decimal(str(summary_payload["total_return_pct"])),
                annualized_return_pct=(
                    None
                    if summary_payload["annualized_return_pct"] is None
                    else Decimal(str(summary_payload["annualized_return_pct"]))
                ),
                win_rate_pct=(
                    None
                    if summary_payload["win_rate_pct"] is None
                    else Decimal(str(summary_payload["win_rate_pct"]))
                ),
            )
        )
    if not summaries:
        raise ValueError(f"No holding period summaries found in {results_dir}")
    return summaries


def write_strategy_holding_period_summary_csv(
    summaries: list[StrategyHoldingPeriodSummary],
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "strategy_name",
                "holding_period",
                "start_date",
                "end_date",
                "closed_trade_count",
                "open_position_count",
                "total_trade_count",
                "realized_profit",
                "unrealized_profit",
                "current_total_assets",
                "total_return_pct",
                "annualized_return_pct",
                "win_rate_pct",
            ],
        )
        writer.writeheader()
        for summary in summaries:
            writer.writerow(_serialize_summary(summary))


def format_strategy_holding_period_table(
    summaries: list[StrategyHoldingPeriodSummary],
) -> str:
    headers = [
        "HP",
        "Closed",
        "Open",
        "Trades",
        "Realized",
        "Assets",
        "Ret%",
        "Ann.Ret%",
        "WinRate%",
    ]
    rows = [
        [
            str(summary.holding_period),
            str(summary.closed_trade_count),
            str(summary.open_position_count),
            str(summary.total_trade_count),
            format(summary.realized_profit, "f"),
            format(summary.current_total_assets, "f"),
            format(summary.total_return_pct, "f"),
            "" if summary.annualized_return_pct is None else format(summary.annualized_return_pct, "f"),
            "" if summary.win_rate_pct is None else format(summary.win_rate_pct, "f"),
        ]
        for summary in summaries
    ]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(cells))

    parts = [render(headers), render(["-" * width for width in widths])]
    parts.extend(render(row) for row in rows)
    return "\n".join(parts)
