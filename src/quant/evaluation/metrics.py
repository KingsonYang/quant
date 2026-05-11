from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from math import sqrt
from statistics import median, pstdev

from quant.backtest import StockSelectionBacktestDetail, StockSelectionBacktestReport
from quant.strategy_backtest import StrategyBacktestEquityPoint, StrategyBacktestReport, StrategyBacktestTrade


def _decimal_to_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(median(values))


def _std(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return 0.0
    return float(pstdev(values))


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _max_drawdown_pct(points: list[StrategyBacktestEquityPoint]) -> float | None:
    if not points:
        return None
    peak = float(points[0].total_assets)
    max_drawdown = 0.0
    for point in points:
        total_assets = float(point.total_assets)
        peak = max(peak, total_assets)
        if peak <= 0:
            continue
        drawdown = (peak - total_assets) * 100 / peak
        max_drawdown = max(max_drawdown, drawdown)
    return max_drawdown


def _daily_returns(points: list[StrategyBacktestEquityPoint]) -> list[float]:
    returns: list[float] = []
    for previous, current in zip(points, points[1:]):
        previous_assets = float(previous.total_assets)
        if previous_assets <= 0:
            continue
        current_assets = float(current.total_assets)
        returns.append((current_assets - previous_assets) / previous_assets)
    return returns


def _annualized_volatility_pct(returns: list[float]) -> float | None:
    if not returns:
        return None
    std = _std(returns)
    if std is None:
        return None
    return std * sqrt(252) * 100


def _sharpe_ratio(returns: list[float]) -> float | None:
    if not returns:
        return None
    std = _std(returns)
    avg = _mean(returns)
    if std in (None, 0.0) or avg is None:
        return None
    return avg / std * sqrt(252)


def _sortino_ratio(returns: list[float]) -> float | None:
    if not returns:
        return None
    avg = _mean(returns)
    downside = [value for value in returns if value < 0]
    if avg is None or not downside:
        return None
    downside_std = _std(downside)
    if downside_std in (None, 0.0):
        return None
    return avg / downside_std * sqrt(252)


def _calmar_ratio(annualized_return_pct: Decimal | None, max_drawdown_pct: float | None) -> float | None:
    if annualized_return_pct is None or max_drawdown_pct in (None, 0.0):
        return None
    return float(annualized_return_pct) / max_drawdown_pct


def _profit_factor(closed_trades: list[StrategyBacktestTrade]) -> float | None:
    profits: list[float] = []
    losses: list[float] = []
    for trade in closed_trades:
        if trade.sell_amount is None:
            continue
        pnl = float(trade.sell_amount - trade.buy_amount)
        if pnl > 0:
            profits.append(pnl)
        elif pnl < 0:
            losses.append(abs(pnl))
    gross_profit = sum(profits)
    gross_loss = sum(losses)
    if gross_loss == 0:
        if gross_profit > 0:
            return 10.0
        return None
    return gross_profit / gross_loss


def build_selection_backtest_metrics(
    report: StockSelectionBacktestReport,
) -> dict[str, float | int | None]:
    completed_returns = [
        float(detail.return_pct)
        for detail in report.details
        if detail.status == "completed" and detail.return_pct is not None
    ]
    return {
        "sample_count": report.summary.hit_count,
        "completed_count": report.summary.completed_count,
        "incomplete_count": report.summary.incomplete_count,
        "win_rate_pct": _decimal_to_float(report.summary.win_rate_pct),
        "average_return_pct": _decimal_to_float(report.summary.average_return_pct),
        "median_return_pct": _median(completed_returns),
        "return_std_pct": _std(completed_returns),
        "best_return_pct": max(completed_returns) if completed_returns else None,
        "worst_return_pct": min(completed_returns) if completed_returns else None,
        "positive_count": report.summary.positive_count,
        "negative_count": report.summary.negative_count,
        "flat_count": report.summary.flat_count,
    }


def build_selection_yearly_metrics(
    report: StockSelectionBacktestReport,
) -> dict[str, dict[str, float | int | None]]:
    grouped: dict[int, list[StockSelectionBacktestDetail]] = defaultdict(list)
    for detail in report.details:
        if detail.status != "completed" or detail.return_pct is None or detail.sell_date is None:
            continue
        grouped[detail.sell_date.year].append(detail)

    yearly: dict[str, dict[str, float | int | None]] = {}
    for year, details in sorted(grouped.items()):
        returns = [float(detail.return_pct) for detail in details if detail.return_pct is not None]
        positive_count = sum(1 for value in returns if value > 0)
        yearly[str(year)] = {
            "sample_count": len(details),
            "win_rate_pct": _safe_ratio(positive_count * 100.0, len(returns)) if returns else None,
            "average_return_pct": _mean(returns),
            "median_return_pct": _median(returns),
            "best_return_pct": max(returns) if returns else None,
            "worst_return_pct": min(returns) if returns else None,
        }
    return yearly


def build_strategy_backtest_metrics(
    report: StrategyBacktestReport,
) -> dict[str, float | int | None]:
    closed_trades = [
        trade
        for trade in report.trades
        if trade.status == "closed" and trade.return_pct is not None
    ]
    closed_returns = [float(trade.return_pct) for trade in closed_trades if trade.return_pct is not None]
    positive_count = sum(1 for value in closed_returns if value > 0)
    daily_returns = _daily_returns(report.equity_curve)
    max_drawdown_pct = _max_drawdown_pct(report.equity_curve)
    annualized_return_pct = _decimal_to_float(report.summary.annualized_return_pct)
    return {
        "total_return_pct": _decimal_to_float(report.summary.total_return_pct),
        "annualized_return_pct": annualized_return_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "volatility_pct": _annualized_volatility_pct(daily_returns),
        "sharpe_ratio": _sharpe_ratio(daily_returns),
        "sortino_ratio": _sortino_ratio(daily_returns),
        "calmar_ratio": _calmar_ratio(report.summary.annualized_return_pct, max_drawdown_pct),
        "win_rate_pct": _safe_ratio(positive_count * 100.0, len(closed_returns)) if closed_returns else None,
        "profit_factor": _profit_factor(closed_trades),
        "average_trade_return_pct": _mean(closed_returns),
        "median_trade_return_pct": _median(closed_returns),
        "best_trade_return_pct": max(closed_returns) if closed_returns else None,
        "worst_trade_return_pct": min(closed_returns) if closed_returns else None,
        "total_trades": report.summary.total_trade_count,
        "closed_trades": report.summary.closed_trade_count,
        "open_positions": report.summary.open_position_count,
    }


def build_strategy_yearly_metrics(
    report: StrategyBacktestReport,
) -> dict[str, dict[str, float | int | None]]:
    points_by_year: dict[int, list[StrategyBacktestEquityPoint]] = defaultdict(list)
    for point in report.equity_curve:
        points_by_year[point.trade_date.year].append(point)

    closed_by_year: dict[int, list[StrategyBacktestTrade]] = defaultdict(list)
    for trade in report.trades:
        if trade.status != "closed" or trade.sell_date is None:
            continue
        closed_by_year[trade.sell_date.year].append(trade)

    yearly: dict[str, dict[str, float | int | None]] = {}
    for year, points in sorted(points_by_year.items()):
        first_assets = float(points[0].total_assets)
        last_assets = float(points[-1].total_assets)
        yearly_return_pct = None
        if first_assets > 0:
            yearly_return_pct = (last_assets - first_assets) * 100 / first_assets
        trades = closed_by_year.get(year, [])
        trade_returns = [float(trade.return_pct) for trade in trades if trade.return_pct is not None]
        positive_count = sum(1 for value in trade_returns if value > 0)
        yearly[str(year)] = {
            "total_return_pct": yearly_return_pct,
            "max_drawdown_pct": _max_drawdown_pct(points),
            "closed_trades": len(trades),
            "win_rate_pct": _safe_ratio(positive_count * 100.0, len(trade_returns)) if trade_returns else None,
            "average_trade_return_pct": _mean(trade_returns),
        }
    return yearly

