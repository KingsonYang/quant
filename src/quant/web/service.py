from __future__ import annotations

import traceback
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from quant.backtest import (
    StockSelectionBacktestError,
    StockSelectionBacktestRequest,
    run_stock_selection_backtest,
)
from quant.db.init_db import DEFAULT_DB_PATH
from quant.selection import (
    StockSelectionError,
    load_selection_rules_file,
    parse_selection_rules,
)
from quant.strategy_backtest import (
    StrategyBacktestError,
    StrategyBacktestRequest,
    run_strategy_backtest,
)

DEFAULT_SELECTION_FORM = {
    "start_date": "",
    "end_date": "",
    "holding_period": "2",
    "rules_json": '[{"field":"close_price","operator":"gt","value":0}]',
    "logic": "AND",
    "selection_sort_by": "symbol",
    "selection_sort_order": "ASC",
    "selection_top_n": "",
    "db_path": str(DEFAULT_DB_PATH),
}

DEFAULT_STRATEGY_FORM = {
    "start_date": "",
    "end_date": "",
    "holding_period": "3",
    "rules_json": '[{"field":"close_price","operator":"gt","value":0}]',
    "logic": "AND",
    "selection_sort_by": "symbol",
    "selection_sort_order": "ASC",
    "selection_top_n": "",
    "db_path": str(DEFAULT_DB_PATH),
    "max_positions": "5",
    "max_daily_buys": "2",
    "initial_capital": "100000",
    "position_size_pct": "20",
    "take_profit_pct": "8",
    "stop_loss_pct": "-5",
}


@dataclass(frozen=True)
class ServiceResult:
    ok: bool
    form_values: dict[str, str]
    field_errors: dict[str, str]
    global_errors: list[str]
    payload: dict[str, Any] | None
    debug_details: dict[str, Any] | None = None


def _normalize_form_data(form_data: dict[str, Any], defaults: dict[str, str]) -> dict[str, str]:
    normalized = defaults.copy()
    for key, value in form_data.items():
        normalized[key] = str(value).strip()
    return normalized


def _serialize_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _serialize_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {key: _serialize_scalar(value) for key, value in data.items()}


def _failure_result(
    form_values: dict[str, str],
    *,
    field_errors: dict[str, str] | None = None,
    global_errors: list[str] | None = None,
    exc: BaseException | None = None,
) -> ServiceResult:
    debug_details: dict[str, Any] | None = None
    if exc is not None:
        debug_details = {
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "traceback": traceback.format_exc(),
        }
    return ServiceResult(
        ok=False,
        form_values=form_values,
        field_errors=field_errors or {},
        global_errors=global_errors or [],
        payload=None,
        debug_details=debug_details,
    )


def _int_field(form_values: dict[str, str], field_name: str, label: str) -> tuple[int | None, str | None]:
    raw_value = form_values[field_name]
    if raw_value == "":
        return None, f"{label}不能为空。"
    try:
        return int(raw_value), None
    except ValueError:
        return None, f"{label}必须是整数。"


def _optional_int_field(
    form_values: dict[str, str], field_name: str, label: str
) -> tuple[int | None, str | None]:
    raw_value = form_values[field_name]
    if raw_value == "":
        return None, None
    try:
        return int(raw_value), None
    except ValueError:
        return None, f"{label}必须是整数。"


def _decimal_field(
    form_values: dict[str, str], field_name: str, label: str
) -> tuple[Decimal | None, str | None]:
    raw_value = form_values[field_name]
    if raw_value == "":
        return None, f"{label}不能为空。"
    try:
        return Decimal(raw_value), None
    except InvalidOperation:
        return None, f"{label}必须是数字。"


def _date_field(form_values: dict[str, str], field_name: str, label: str) -> tuple[date | None, str | None]:
    raw_value = form_values[field_name]
    if raw_value == "":
        return None, f"{label}不能为空。"
    try:
        return date.fromisoformat(raw_value), None
    except ValueError:
        return None, f"{label}必须是 YYYY-MM-DD 格式。"


def _resolve_rules(form_values: dict[str, str]) -> tuple[list[Any] | None, str | None]:
    rules_json = form_values.get("rules_json", "")
    if rules_json:
        try:
            return parse_selection_rules(rules_json), None
        except StockSelectionError as exc:
            return None, str(exc)

    rules_file = form_values.get("rules_file", "")
    if rules_file:
        try:
            return load_selection_rules_file(Path(rules_file)), None
        except (OSError, StockSelectionError) as exc:
            return None, str(exc)

    return None, "请提供规则 JSON。"


def _selection_payload(report) -> dict[str, Any]:
    summary = {
        "start_date": report.summary.start_date,
        "end_date": report.summary.end_date,
        "holding_period": report.summary.holding_period,
        "trade_days": report.summary.trade_days,
        "rule_count": report.summary.rule_count,
        "selection_sort_by": report.summary.selection_sort_by,
        "selection_sort_order": report.summary.selection_sort_order,
        "selection_top_n": report.summary.selection_top_n,
        "hit_count": report.summary.hit_count,
        "completed_count": report.summary.completed_count,
        "incomplete_count": report.summary.incomplete_count,
        "positive_count": report.summary.positive_count,
        "negative_count": report.summary.negative_count,
        "flat_count": report.summary.flat_count,
        "win_rate_pct": report.summary.win_rate_pct,
        "average_return_pct": report.summary.average_return_pct,
    }
    return {
        "type": "selection",
        "empty_result": report.summary.hit_count == 0,
        "summary": _serialize_dict(summary),
        "return_distribution": [
            {"label": bucket.label, "count": bucket.count}
            for bucket in report.summary.return_distribution
        ],
        "details": [
            _serialize_dict(
                {
                    "symbol": detail.symbol,
                    "name": detail.name,
                    "selection_date": detail.selection_date,
                    "buy_date": detail.buy_date,
                    "sell_date": detail.sell_date,
                    "buy_price": detail.buy_price,
                    "sell_price": detail.sell_price,
                    "holding_period": detail.holding_period,
                    "return_pct": detail.return_pct,
                    "status": detail.status,
                    "incomplete_reason": detail.incomplete_reason,
                }
            )
            for detail in report.details
        ],
    }


def _strategy_payload(report) -> dict[str, Any]:
    summary = {
        "start_date": report.summary.start_date,
        "end_date": report.summary.end_date,
        "holding_period": report.summary.holding_period,
        "trade_days": report.summary.trade_days,
        "rule_count": report.summary.rule_count,
        "selection_sort_by": report.summary.selection_sort_by,
        "selection_sort_order": report.summary.selection_sort_order,
        "selection_top_n": report.summary.selection_top_n,
        "max_positions": report.summary.max_positions,
        "max_daily_buys": report.summary.max_daily_buys,
        "initial_capital": report.summary.initial_capital,
        "position_size_pct": report.summary.position_size_pct,
        "take_profit_pct": report.summary.take_profit_pct,
        "stop_loss_pct": report.summary.stop_loss_pct,
        "closed_trade_count": report.summary.closed_trade_count,
        "open_position_count": report.summary.open_position_count,
        "total_trade_count": report.summary.total_trade_count,
        "current_cash_balance": report.summary.current_cash_balance,
        "current_market_value": report.summary.current_market_value,
        "current_total_assets": report.summary.current_total_assets,
        "realized_profit": report.summary.realized_profit,
        "unrealized_profit": report.summary.unrealized_profit,
        "total_return_pct": report.summary.total_return_pct,
        "annualized_return_pct": report.summary.annualized_return_pct,
        "transaction_costs_included": report.summary.transaction_costs_included,
    }
    trades = [
        _serialize_dict(
            {
                "symbol": trade.symbol,
                "name": trade.name,
                "selection_date": trade.selection_date,
                "buy_date": trade.buy_date,
                "sell_date": trade.sell_date,
                "buy_price": trade.buy_price,
                "sell_price": trade.sell_price,
                "shares": trade.shares,
                "lots": trade.lots,
                "buy_amount": trade.buy_amount,
                "sell_amount": trade.sell_amount,
                "holding_days": trade.holding_days,
                "return_pct": trade.return_pct,
                "status": trade.status,
                "exit_reason": trade.exit_reason,
            }
        )
        for trade in report.trades
    ]
    return {
        "type": "strategy",
        "empty_result": len(trades) == 0,
        "summary": _serialize_dict(summary),
        "closed_trades": [trade for trade in trades if trade["status"] == "closed"],
        "open_positions": [trade for trade in trades if trade["status"] == "open"],
        "trades": trades,
    }


def run_selection_service(
    form_data: dict[str, Any], default_db_path: Path = DEFAULT_DB_PATH
) -> ServiceResult:
    form_values = _normalize_form_data(form_data, DEFAULT_SELECTION_FORM)
    form_values.setdefault("db_path", str(default_db_path))
    field_errors: dict[str, str] = {}

    start_date, error = _date_field(form_values, "start_date", "开始日期")
    if error:
        field_errors["start_date"] = error
    end_date, error = _date_field(form_values, "end_date", "结束日期")
    if error:
        field_errors["end_date"] = error
    holding_period, error = _int_field(form_values, "holding_period", "持有周期")
    if error:
        field_errors["holding_period"] = error
    selection_top_n, error = _optional_int_field(form_values, "selection_top_n", "Top N")
    if error:
        field_errors["selection_top_n"] = error
    rules, rules_error = _resolve_rules(form_values)
    if rules_error:
        field_errors["rules_json"] = rules_error

    if field_errors:
        return _failure_result(form_values, field_errors=field_errors)

    try:
        request = StockSelectionBacktestRequest(
            start_date=start_date,
            end_date=end_date,
            holding_period=holding_period,
            rules=rules,
            logic=form_values["logic"] or "AND",
            selection_sort_by=form_values["selection_sort_by"] or "symbol",
            selection_sort_order=form_values["selection_sort_order"] or "ASC",
            selection_top_n=selection_top_n,
        )
        report = run_stock_selection_backtest(
            db_path=Path(form_values["db_path"] or default_db_path),
            request=request,
        )
    except (ValueError, StockSelectionBacktestError, StockSelectionError, OSError) as exc:
        return _failure_result(form_values, global_errors=[str(exc)], exc=exc)
    except Exception as exc:  # pragma: no cover
        return _failure_result(form_values, global_errors=[f"普通回测执行失败：{exc}"], exc=exc)

    return ServiceResult(True, form_values, {}, [], _selection_payload(report))


def run_strategy_service(
    form_data: dict[str, Any], default_db_path: Path = DEFAULT_DB_PATH
) -> ServiceResult:
    form_values = _normalize_form_data(form_data, DEFAULT_STRATEGY_FORM)
    form_values.setdefault("db_path", str(default_db_path))
    field_errors: dict[str, str] = {}

    start_date, error = _date_field(form_values, "start_date", "开始日期")
    if error:
        field_errors["start_date"] = error
    end_date, error = _date_field(form_values, "end_date", "结束日期")
    if error:
        field_errors["end_date"] = error
    holding_period, error = _int_field(form_values, "holding_period", "持有周期")
    if error:
        field_errors["holding_period"] = error
    max_positions, error = _int_field(form_values, "max_positions", "最大持仓数")
    if error:
        field_errors["max_positions"] = error
    max_daily_buys, error = _int_field(form_values, "max_daily_buys", "单日最大买入数")
    if error:
        field_errors["max_daily_buys"] = error
    selection_top_n, error = _optional_int_field(form_values, "selection_top_n", "Top N")
    if error:
        field_errors["selection_top_n"] = error
    initial_capital, error = _decimal_field(form_values, "initial_capital", "初始资金")
    if error:
        field_errors["initial_capital"] = error
    position_size_pct, error = _decimal_field(form_values, "position_size_pct", "单笔仓位比例")
    if error:
        field_errors["position_size_pct"] = error
    take_profit_pct, error = _decimal_field(form_values, "take_profit_pct", "止盈阈值")
    if error:
        field_errors["take_profit_pct"] = error
    stop_loss_pct, error = _decimal_field(form_values, "stop_loss_pct", "止损阈值")
    if error:
        field_errors["stop_loss_pct"] = error
    rules, rules_error = _resolve_rules(form_values)
    if rules_error:
        field_errors["rules_json"] = rules_error

    if field_errors:
        return _failure_result(form_values, field_errors=field_errors)

    try:
        request = StrategyBacktestRequest(
            start_date=start_date,
            end_date=end_date,
            holding_period=holding_period,
            rules=rules,
            max_positions=max_positions,
            max_daily_buys=max_daily_buys,
            initial_capital=initial_capital,
            position_size_pct=position_size_pct,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            logic=form_values["logic"] or "AND",
            selection_sort_by=form_values["selection_sort_by"] or "symbol",
            selection_sort_order=form_values["selection_sort_order"] or "ASC",
            selection_top_n=selection_top_n,
        )
        report = run_strategy_backtest(
            db_path=Path(form_values["db_path"] or default_db_path),
            request=request,
        )
    except (ValueError, StrategyBacktestError, StockSelectionError, OSError) as exc:
        return _failure_result(form_values, global_errors=[str(exc)], exc=exc)
    except Exception as exc:  # pragma: no cover
        return _failure_result(form_values, global_errors=[f"策略回测执行失败：{exc}"], exc=exc)

    return ServiceResult(True, form_values, {}, [], _strategy_payload(report))


def service_result_to_dict(result: ServiceResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "form_values": result.form_values,
        "field_errors": result.field_errors,
        "global_errors": result.global_errors,
        "payload": result.payload,
    }
