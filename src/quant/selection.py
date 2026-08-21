from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb


SUPPORTED_LOGIC = {"AND"}
SUPPORTED_OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte"}
SUPPORTED_SORT_ORDERS = {"ASC", "DESC"}

FIELD_DEFINITIONS: dict[str, dict[str, Any]] = {
    "exchange": {"expr": "s.exchange", "type": "string"},
    "board": {"expr": "s.board", "type": "string"},
    "list_status": {"expr": "s.list_status", "type": "string"},
    "listed_days": {"expr": "date_diff('day', s.list_date, ?::DATE)", "type": "number"},
    "close_price": {"expr": "COALESCE(b.close_qfq, b.close_price)", "type": "number"},
    "change_pct": {"expr": "b.change_pct", "type": "number"},
    "turnover_ratio": {"expr": "b.turnover_ratio", "type": "number"},
    "amount": {"expr": "b.amount", "type": "number"},
    "volume": {"expr": "b.volume", "type": "number"},
    "ma5_price": {"expr": "b.ma5_price", "type": "number"},
    "ma10_price": {"expr": "b.ma10_price", "type": "number"},
    "ma20_price": {"expr": "b.ma20_price", "type": "number"},
    "ma5_volume": {"expr": "b.ma5_volume", "type": "number"},
    "ma10_volume": {"expr": "b.ma10_volume", "type": "number"},
    "ma20_volume": {"expr": "b.ma20_volume", "type": "number"},
    "prev_5d_avg_volume": {"expr": "b.prev_5d_avg_volume", "type": "number"},
    "prev_5d_sum_volume": {"expr": "b.prev_5d_sum_volume", "type": "number"},
    "last_5d_avg_volume": {"expr": "b.last_5d_avg_volume", "type": "number"},
    "last_5d_sum_volume": {"expr": "b.last_5d_sum_volume", "type": "number"},
    "volume_ratio_prev_5d_avg": {
        "expr": "CASE WHEN b.prev_5d_avg_volume IS NULL OR b.prev_5d_avg_volume = 0 THEN NULL ELSE b.volume / b.prev_5d_avg_volume END",
        "type": "number",
    },
    "volume_ratio_last_5d_avg": {
        "expr": "CASE WHEN b.last_5d_avg_volume IS NULL OR b.last_5d_avg_volume = 0 THEN NULL ELSE b.volume / b.last_5d_avg_volume END",
        "type": "number",
    },
    "volume_plus_prev_5d_sum": {
        "expr": "CASE WHEN b.prev_5d_sum_volume IS NULL THEN NULL ELSE b.volume + b.prev_5d_sum_volume END",
        "type": "number",
    },
    "volume_plus_last_5d_sum": {
        "expr": "CASE WHEN b.last_5d_sum_volume IS NULL THEN NULL ELSE b.volume + b.last_5d_sum_volume END",
        "type": "number",
    },
    "prev_5d_avg_amount": {"expr": "b.prev_5d_avg_amount", "type": "number"},
    "prev_5d_sum_amount": {"expr": "b.prev_5d_sum_amount", "type": "number"},
    "last_5d_avg_amount": {"expr": "b.last_5d_avg_amount", "type": "number"},
    "last_5d_sum_amount": {"expr": "b.last_5d_sum_amount", "type": "number"},
    "amount_ratio_prev_5d_avg": {
        "expr": "CASE WHEN b.prev_5d_avg_amount IS NULL OR b.prev_5d_avg_amount = 0 THEN NULL ELSE b.amount / b.prev_5d_avg_amount END",
        "type": "number",
    },
    "amount_ratio_last_5d_avg": {
        "expr": "CASE WHEN b.last_5d_avg_amount IS NULL OR b.last_5d_avg_amount = 0 THEN NULL ELSE b.amount / b.last_5d_avg_amount END",
        "type": "number",
    },
    "amount_plus_prev_5d_sum": {
        "expr": "CASE WHEN b.prev_5d_sum_amount IS NULL THEN NULL ELSE b.amount + b.prev_5d_sum_amount END",
        "type": "number",
    },
    "amount_plus_last_5d_sum": {
        "expr": "CASE WHEN b.last_5d_sum_amount IS NULL THEN NULL ELSE b.amount + b.last_5d_sum_amount END",
        "type": "number",
    },
    "financial_report_days_since": {
        "expr": "date_diff('day', b.latest_financial_report_date, ?::DATE)",
        "type": "number",
    },
}

SORT_DEFINITIONS: dict[str, str] = {
    "symbol": "s.symbol",
    "name": "s.name",
    "close_price": "COALESCE(b.close_qfq, b.close_price)",
    "change_pct": "b.change_pct",
    "turnover_ratio": "b.turnover_ratio",
    "volume": "b.volume",
    "prev_5d_avg_volume": "b.prev_5d_avg_volume",
    "prev_5d_sum_volume": "b.prev_5d_sum_volume",
    "last_5d_avg_volume": "b.last_5d_avg_volume",
    "last_5d_sum_volume": "b.last_5d_sum_volume",
    "volume_plus_prev_5d_sum": "CASE WHEN b.prev_5d_sum_volume IS NULL THEN NULL ELSE b.volume + b.prev_5d_sum_volume END",
    "volume_plus_last_5d_sum": "CASE WHEN b.last_5d_sum_volume IS NULL THEN NULL ELSE b.volume + b.last_5d_sum_volume END",
    "amount": "b.amount",
    "prev_5d_avg_amount": "b.prev_5d_avg_amount",
    "prev_5d_sum_amount": "b.prev_5d_sum_amount",
    "last_5d_avg_amount": "b.last_5d_avg_amount",
    "last_5d_sum_amount": "b.last_5d_sum_amount",
    "amount_plus_prev_5d_sum": "CASE WHEN b.prev_5d_sum_amount IS NULL THEN NULL ELSE b.amount + b.prev_5d_sum_amount END",
    "amount_plus_last_5d_sum": "CASE WHEN b.last_5d_sum_amount IS NULL THEN NULL ELSE b.amount + b.last_5d_sum_amount END",
}

SQL_OPERATORS = {
    "eq": "=",
    "ne": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}


class StockSelectionError(ValueError):
    """Raised when stock selection input is invalid."""


@dataclass(frozen=True)
class SelectionRule:
    field: str
    operator: str
    value: str | int | float


@dataclass(frozen=True)
class SelectionRequest:
    trade_date: date
    rules: list[SelectionRule]
    logic: str = "AND"


@dataclass(frozen=True)
class SelectionResultRow:
    symbol: str
    name: str
    trade_date: date
    close_price: Decimal
    volume: int | None
    amount: Decimal | None
    change_pct: Decimal | None
    turnover_ratio: Decimal | None
    prev_5d_avg_volume: float | None
    prev_5d_sum_volume: int | None
    last_5d_avg_volume: float | None
    last_5d_sum_volume: int | None
    volume_plus_prev_5d_sum: int | None
    volume_plus_last_5d_sum: int | None
    prev_5d_avg_amount: float | None
    prev_5d_sum_amount: Decimal | None
    last_5d_avg_amount: float | None
    last_5d_sum_amount: Decimal | None
    amount_plus_prev_5d_sum: Decimal | None
    amount_plus_last_5d_sum: Decimal | None


ROW_FILTER_NUMERIC_FIELDS = {
    "close_price",
    "change_pct",
    "turnover_ratio",
    "amount",
    "volume",
    "prev_5d_avg_volume",
    "prev_5d_sum_volume",
    "last_5d_avg_volume",
    "last_5d_sum_volume",
    "volume_ratio_prev_5d_avg",
    "volume_ratio_last_5d_avg",
    "volume_plus_prev_5d_sum",
    "volume_plus_last_5d_sum",
    "prev_5d_avg_amount",
    "prev_5d_sum_amount",
    "last_5d_avg_amount",
    "last_5d_sum_amount",
    "amount_ratio_prev_5d_avg",
    "amount_ratio_last_5d_avg",
    "amount_plus_prev_5d_sum",
    "amount_plus_last_5d_sum",
}


@dataclass(frozen=True)
class SelectionSummary:
    trade_date: date
    logic: str
    rule_count: int
    result_count: int
    sort_by: str
    sort_order: str
    results: list[SelectionResultRow]


def parse_selection_rules(rules_json: str) -> list[SelectionRule]:
    try:
        payload = json.loads(rules_json)
    except json.JSONDecodeError as exc:
        raise StockSelectionError("Invalid rules JSON.") from exc

    if not isinstance(payload, list) or not payload:
        raise StockSelectionError("Rules JSON must be a non-empty array.")

    rules: list[SelectionRule] = []
    for item in payload:
        if not isinstance(item, dict):
            raise StockSelectionError("Each rule must be a JSON object.")
        field = item.get("field")
        operator = item.get("operator")
        if "value" not in item:
            raise StockSelectionError("Each rule must contain value.")
        value = item["value"]
        if not isinstance(field, str) or not field:
            raise StockSelectionError("Rule field must be a non-empty string.")
        if not isinstance(operator, str) or not operator:
            raise StockSelectionError("Rule operator must be a non-empty string.")
        rules.append(SelectionRule(field=field, operator=operator, value=value))
    return rules


def load_selection_rules_file(rules_file: Path) -> list[SelectionRule]:
    if not rules_file.exists():
        raise StockSelectionError(f"Rules file not found: {rules_file}")
    return parse_selection_rules(rules_file.read_text(encoding="utf-8"))


def _validate_logic(logic: str) -> str:
    normalized = logic.upper()
    if normalized not in SUPPORTED_LOGIC:
        raise StockSelectionError(f"Unsupported logic: {logic}. Supported logic: AND.")
    return normalized


def _validate_sort(sort_by: str, sort_order: str) -> tuple[str, str]:
    if sort_by not in SORT_DEFINITIONS:
        supported_fields = ", ".join(sorted(SORT_DEFINITIONS))
        raise StockSelectionError(
            f"Unsupported sort field: {sort_by}. Supported sort fields: {supported_fields}."
        )
    normalized_order = sort_order.upper()
    if normalized_order not in SUPPORTED_SORT_ORDERS:
        raise StockSelectionError(
            f"Unsupported sort order: {sort_order}. Supported sort orders: ASC, DESC."
        )
    return sort_by, normalized_order


def _validate_rule(rule: SelectionRule) -> None:
    if rule.field not in FIELD_DEFINITIONS:
        supported_fields = ", ".join(sorted(FIELD_DEFINITIONS))
        raise StockSelectionError(
            f"Unsupported field: {rule.field}. Supported fields: {supported_fields}."
        )
    if rule.operator not in SUPPORTED_OPERATORS:
        supported_ops = ", ".join(sorted(SUPPORTED_OPERATORS))
        raise StockSelectionError(
            f"Unsupported operator: {rule.operator}. Supported operators: {supported_ops}."
        )

    field_type = FIELD_DEFINITIONS[rule.field]["type"]
    if field_type == "number" and not isinstance(rule.value, (int, float)):
        raise StockSelectionError(f"Field {rule.field} requires a numeric value.")
    if field_type == "string" and not isinstance(rule.value, str):
        raise StockSelectionError(f"Field {rule.field} requires a string value.")


def _validate_row_filter_rule(rule: SelectionRule) -> None:
    if rule.field not in ROW_FILTER_NUMERIC_FIELDS:
        supported_fields = ", ".join(sorted(ROW_FILTER_NUMERIC_FIELDS))
        raise StockSelectionError(
            f"Unsupported row filter field: {rule.field}. Supported fields: {supported_fields}."
        )
    if rule.operator not in SUPPORTED_OPERATORS:
        supported_ops = ", ".join(sorted(SUPPORTED_OPERATORS))
        raise StockSelectionError(
            f"Unsupported row filter operator: {rule.operator}. Supported operators: {supported_ops}."
        )
    if not isinstance(rule.value, (int, float)):
        raise StockSelectionError(f"Row filter field {rule.field} requires a numeric value.")


def selection_row_value(row: SelectionResultRow, field: str) -> float | None:
    if field == "volume_ratio_prev_5d_avg":
        return _safe_ratio(row.volume, row.prev_5d_avg_volume)
    if field == "volume_ratio_last_5d_avg":
        return _safe_ratio(row.volume, row.last_5d_avg_volume)
    if field == "amount_ratio_prev_5d_avg":
        return _safe_ratio(row.amount, row.prev_5d_avg_amount)
    if field == "amount_ratio_last_5d_avg":
        return _safe_ratio(row.amount, row.last_5d_avg_amount)
    value = getattr(row, field, None)
    if value is None:
        return None
    return float(value)


def filter_selection_rows_by_rules(
    rows: list[SelectionResultRow],
    rules: list[SelectionRule],
) -> list[SelectionResultRow]:
    if not rules:
        return rows
    for rule in rules:
        _validate_row_filter_rule(rule)
    return [
        row
        for row in rows
        if all(_row_matches_filter_rule(row, rule) for rule in rules)
    ]


def _row_matches_filter_rule(row: SelectionResultRow, rule: SelectionRule) -> bool:
    value = selection_row_value(row, rule.field)
    if value is None:
        return False
    target = float(rule.value)
    if rule.operator == "eq":
        return value == target
    if rule.operator == "ne":
        return value != target
    if rule.operator == "gt":
        return value > target
    if rule.operator == "gte":
        return value >= target
    if rule.operator == "lt":
        return value < target
    if rule.operator == "lte":
        return value <= target
    raise StockSelectionError(f"Unsupported row filter operator: {rule.operator}.")


def _safe_ratio(numerator: object, denominator: object) -> float | None:
    if numerator is None or denominator is None:
        return None
    denominator_float = float(denominator)
    if denominator_float == 0:
        return None
    return float(numerator) / denominator_float


def _validate_trade_date(
    connection: duckdb.DuckDBPyConnection,
    trade_date: date,
) -> None:
    exists = connection.execute(
        """
        SELECT 1
        FROM trading_calendar
        WHERE market = 'ab' AND trade_date = ? AND is_trading_day = TRUE
        """,
        [trade_date],
    ).fetchone()
    if exists is None:
        raise StockSelectionError(
            f"Trade date {trade_date.isoformat()} is not available in trading_calendar."
        )


def _build_rule_clause(rule: SelectionRule, trade_date: date) -> tuple[str, list[Any]]:
    field_def = FIELD_DEFINITIONS[rule.field]
    expr = field_def["expr"]
    sql_operator = SQL_OPERATORS[rule.operator]
    params: list[Any] = []

    if "?::DATE" in expr:
        params.append(trade_date)
    params.append(rule.value)
    return f"{expr} {sql_operator} ?", params


def _build_batch_rule_clause(rule: SelectionRule) -> tuple[str, list[Any]]:
    field_def = FIELD_DEFINITIONS[rule.field]
    expr = field_def["expr"].replace("?::DATE", "b.trade_date")
    sql_operator = SQL_OPERATORS[rule.operator]
    return f"{expr} {sql_operator} ?", [rule.value]


def _select_stocks_with_connection(
    connection: duckdb.DuckDBPyConnection,
    trade_date: date,
    rules: list[SelectionRule],
    logic: str = "AND",
    sort_by: str = "symbol",
    sort_order: str = "ASC",
) -> SelectionSummary:
    normalized_logic = _validate_logic(logic)
    validated_sort_by, validated_sort_order = _validate_sort(sort_by, sort_order)
    if not rules:
        raise StockSelectionError("At least one selection rule is required.")
    for rule in rules:
        _validate_rule(rule)

    _validate_trade_date(connection, trade_date)

    clauses = ["b.trade_date = ?"]
    params: list[Any] = [trade_date]

    for rule in rules:
        clause, clause_params = _build_rule_clause(rule, trade_date)
        clauses.append(clause)
        params.extend(clause_params)

    where_clause = f" {normalized_logic} ".join(clauses)
    order_expr = SORT_DEFINITIONS[validated_sort_by]
    rows = connection.execute(
        f"""
        WITH latest_events AS (
            SELECT
                security_id,
                MAX(event_date) AS latest_financial_report_date
            FROM event_record
            WHERE event_type = 'financial_report' AND event_date <= ?
            GROUP BY security_id
        ),
        selection_base AS (
            SELECT
                d.*,
                e.latest_financial_report_date,
                AVG(d.volume) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
                ) AS prev_5d_avg_volume,
                SUM(d.volume) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
                ) AS prev_5d_sum_volume
                ,
                AVG(d.volume) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
                ) AS last_5d_avg_volume,
                SUM(d.volume) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
                ) AS last_5d_sum_volume,
                AVG(d.amount) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
                ) AS prev_5d_avg_amount,
                SUM(d.amount) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
                ) AS prev_5d_sum_amount
                ,
                AVG(d.amount) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
                ) AS last_5d_avg_amount,
                SUM(d.amount) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
                ) AS last_5d_sum_amount
            FROM daily_bar AS d
            LEFT JOIN latest_events AS e
                ON d.security_id = e.security_id
        )
        SELECT
            s.symbol,
            s.name,
            b.trade_date,
            COALESCE(b.close_qfq, b.close_price) AS close_price,
            b.volume,
            b.amount,
            b.change_pct,
            b.turnover_ratio,
            b.prev_5d_avg_volume,
            b.prev_5d_sum_volume,
            b.last_5d_avg_volume,
            b.last_5d_sum_volume,
            CASE
                WHEN b.prev_5d_avg_volume IS NULL OR b.prev_5d_avg_volume = 0 THEN NULL
                ELSE b.volume / b.prev_5d_avg_volume
            END AS volume_ratio_prev_5d_avg,
            CASE
                WHEN b.last_5d_avg_volume IS NULL OR b.last_5d_avg_volume = 0 THEN NULL
                ELSE b.volume / b.last_5d_avg_volume
            END AS volume_ratio_last_5d_avg,
            CASE
                WHEN b.prev_5d_sum_volume IS NULL THEN NULL
                ELSE b.volume + b.prev_5d_sum_volume
            END AS volume_plus_prev_5d_sum,
            CASE
                WHEN b.last_5d_sum_volume IS NULL THEN NULL
                ELSE b.volume + b.last_5d_sum_volume
            END AS volume_plus_last_5d_sum,
            b.prev_5d_avg_amount,
            b.prev_5d_sum_amount,
            b.last_5d_avg_amount,
            b.last_5d_sum_amount,
            CASE
                WHEN b.prev_5d_avg_amount IS NULL OR b.prev_5d_avg_amount = 0 THEN NULL
                ELSE b.amount / b.prev_5d_avg_amount
            END AS amount_ratio_prev_5d_avg,
            CASE
                WHEN b.last_5d_avg_amount IS NULL OR b.last_5d_avg_amount = 0 THEN NULL
                ELSE b.amount / b.last_5d_avg_amount
            END AS amount_ratio_last_5d_avg,
            CASE
                WHEN b.prev_5d_sum_amount IS NULL THEN NULL
                ELSE b.amount + b.prev_5d_sum_amount
            END AS amount_plus_prev_5d_sum,
            CASE
                WHEN b.last_5d_sum_amount IS NULL THEN NULL
                ELSE b.amount + b.last_5d_sum_amount
            END AS amount_plus_last_5d_sum
        FROM selection_base AS b
        JOIN security_master AS s
            ON b.security_id = s.security_id
        WHERE {where_clause}
        ORDER BY {order_expr} {validated_sort_order}, s.symbol ASC
        """,
        [trade_date, *params],
    ).fetchall()

    results = [_selection_result_row_from_sql(row) for row in rows]
    return SelectionSummary(
        trade_date=trade_date,
        logic=normalized_logic,
        rule_count=len(rules),
        result_count=len(results),
        sort_by=validated_sort_by,
        sort_order=validated_sort_order,
        results=results,
    )


def _select_stocks_in_range_with_connection(
    connection: duckdb.DuckDBPyConnection,
    start_date: date,
    end_date: date,
    rules: list[SelectionRule],
    logic: str = "AND",
    sort_by: str = "symbol",
    sort_order: str = "ASC",
) -> dict[date, list[SelectionResultRow]]:
    normalized_logic = _validate_logic(logic)
    validated_sort_by, validated_sort_order = _validate_sort(sort_by, sort_order)
    if not rules:
        raise StockSelectionError("At least one selection rule is required.")
    for rule in rules:
        _validate_rule(rule)

    _validate_trade_date(connection, start_date)
    _validate_trade_date(connection, end_date)

    clauses = ["b.trade_date BETWEEN ? AND ?"]
    params: list[Any] = [start_date, end_date]

    for rule in rules:
        clause, clause_params = _build_batch_rule_clause(rule)
        clauses.append(clause)
        params.extend(clause_params)

    where_clause = f" {normalized_logic} ".join(clauses)
    order_expr = SORT_DEFINITIONS[validated_sort_by]
    rows = connection.execute(
        f"""
        WITH selection_base AS (
            SELECT
                d.*,
                (
                    SELECT MAX(e.event_date)
                    FROM event_record AS e
                    WHERE e.security_id = d.security_id
                      AND e.event_type = 'financial_report'
                      AND e.event_date <= d.trade_date
                ) AS latest_financial_report_date,
                AVG(d.volume) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
                ) AS prev_5d_avg_volume,
                SUM(d.volume) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
                ) AS prev_5d_sum_volume,
                AVG(d.volume) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
                ) AS last_5d_avg_volume,
                SUM(d.volume) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
                ) AS last_5d_sum_volume,
                AVG(d.amount) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
                ) AS prev_5d_avg_amount,
                SUM(d.amount) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
                ) AS prev_5d_sum_amount,
                AVG(d.amount) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
                ) AS last_5d_avg_amount,
                SUM(d.amount) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
                ) AS last_5d_sum_amount
            FROM daily_bar AS d
            WHERE d.trade_date <= ?
        )
        SELECT
            s.symbol,
            s.name,
            b.trade_date,
            COALESCE(b.close_qfq, b.close_price) AS close_price,
            b.volume,
            b.amount,
            b.change_pct,
            b.turnover_ratio,
            b.prev_5d_avg_volume,
            b.prev_5d_sum_volume,
            b.last_5d_avg_volume,
            b.last_5d_sum_volume,
            CASE
                WHEN b.prev_5d_avg_volume IS NULL OR b.prev_5d_avg_volume = 0 THEN NULL
                ELSE b.volume / b.prev_5d_avg_volume
            END AS volume_ratio_prev_5d_avg,
            CASE
                WHEN b.last_5d_avg_volume IS NULL OR b.last_5d_avg_volume = 0 THEN NULL
                ELSE b.volume / b.last_5d_avg_volume
            END AS volume_ratio_last_5d_avg,
            CASE
                WHEN b.prev_5d_sum_volume IS NULL THEN NULL
                ELSE b.volume + b.prev_5d_sum_volume
            END AS volume_plus_prev_5d_sum,
            CASE
                WHEN b.last_5d_sum_volume IS NULL THEN NULL
                ELSE b.volume + b.last_5d_sum_volume
            END AS volume_plus_last_5d_sum,
            b.prev_5d_avg_amount,
            b.prev_5d_sum_amount,
            b.last_5d_avg_amount,
            b.last_5d_sum_amount,
            CASE
                WHEN b.prev_5d_avg_amount IS NULL OR b.prev_5d_avg_amount = 0 THEN NULL
                ELSE b.amount / b.prev_5d_avg_amount
            END AS amount_ratio_prev_5d_avg,
            CASE
                WHEN b.last_5d_avg_amount IS NULL OR b.last_5d_avg_amount = 0 THEN NULL
                ELSE b.amount / b.last_5d_avg_amount
            END AS amount_ratio_last_5d_avg,
            CASE
                WHEN b.prev_5d_sum_amount IS NULL THEN NULL
                ELSE b.amount + b.prev_5d_sum_amount
            END AS amount_plus_prev_5d_sum,
            CASE
                WHEN b.last_5d_sum_amount IS NULL THEN NULL
                ELSE b.amount + b.last_5d_sum_amount
            END AS amount_plus_last_5d_sum
        FROM selection_base AS b
        JOIN security_master AS s
            ON b.security_id = s.security_id
        WHERE {where_clause}
        ORDER BY b.trade_date ASC, {order_expr} {validated_sort_order}, s.symbol ASC
        """,
        [end_date, *params],
    ).fetchall()

    grouped_results: dict[date, list[SelectionResultRow]] = {}
    for row in rows:
        result = _selection_result_row_from_sql(row)
        grouped_results.setdefault(result.trade_date, []).append(result)

    return grouped_results


def load_selection_rows_for_symbol_dates_with_connection(
    connection: duckdb.DuckDBPyConnection,
    symbol_dates: set[tuple[str, date]],
    *,
    end_date: date,
) -> dict[tuple[str, date], SelectionResultRow]:
    if not symbol_dates:
        return {}

    ordered_pairs = sorted(symbol_dates, key=lambda item: (item[1], item[0]))
    values_clause = ", ".join("(?, CAST(? AS DATE))" for _ in ordered_pairs)
    params: list[Any] = []
    for symbol, trade_date in ordered_pairs:
        params.extend([symbol, trade_date])
    rows = connection.execute(
        f"""
        WITH requested(symbol, trade_date) AS (
            VALUES {values_clause}
        ),
        requested_securities AS (
            SELECT
                s.security_id,
                r.symbol,
                r.trade_date
            FROM requested AS r
            JOIN security_master AS s
                ON s.symbol = r.symbol
        ),
        selection_base AS (
            SELECT
                d.*,
                (
                    SELECT MAX(e.event_date)
                    FROM event_record AS e
                    WHERE e.security_id = d.security_id
                      AND e.event_type = 'financial_report'
                      AND e.event_date <= d.trade_date
                ) AS latest_financial_report_date,
                AVG(d.volume) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
                ) AS prev_5d_avg_volume,
                SUM(d.volume) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
                ) AS prev_5d_sum_volume,
                AVG(d.volume) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
                ) AS last_5d_avg_volume,
                SUM(d.volume) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
                ) AS last_5d_sum_volume,
                AVG(d.amount) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
                ) AS prev_5d_avg_amount,
                SUM(d.amount) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
                ) AS prev_5d_sum_amount,
                AVG(d.amount) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
                ) AS last_5d_avg_amount,
                SUM(d.amount) OVER (
                    PARTITION BY d.security_id
                    ORDER BY d.trade_date
                    ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
                ) AS last_5d_sum_amount
            FROM daily_bar AS d
            JOIN (
                SELECT DISTINCT security_id
                FROM requested_securities
            ) AS requested_ids
                ON requested_ids.security_id = d.security_id
            WHERE d.trade_date <= ?
        )
        SELECT
            s.symbol,
            s.name,
            b.trade_date,
            COALESCE(b.close_qfq, b.close_price) AS close_price,
            b.volume,
            b.amount,
            b.change_pct,
            b.turnover_ratio,
            b.prev_5d_avg_volume,
            b.prev_5d_sum_volume,
            b.last_5d_avg_volume,
            b.last_5d_sum_volume,
            CASE
                WHEN b.prev_5d_avg_volume IS NULL OR b.prev_5d_avg_volume = 0 THEN NULL
                ELSE b.volume / b.prev_5d_avg_volume
            END AS volume_ratio_prev_5d_avg,
            CASE
                WHEN b.last_5d_avg_volume IS NULL OR b.last_5d_avg_volume = 0 THEN NULL
                ELSE b.volume / b.last_5d_avg_volume
            END AS volume_ratio_last_5d_avg,
            CASE
                WHEN b.prev_5d_sum_volume IS NULL THEN NULL
                ELSE b.volume + b.prev_5d_sum_volume
            END AS volume_plus_prev_5d_sum,
            CASE
                WHEN b.last_5d_sum_volume IS NULL THEN NULL
                ELSE b.volume + b.last_5d_sum_volume
            END AS volume_plus_last_5d_sum,
            b.prev_5d_avg_amount,
            b.prev_5d_sum_amount,
            b.last_5d_avg_amount,
            b.last_5d_sum_amount,
            CASE
                WHEN b.prev_5d_avg_amount IS NULL OR b.prev_5d_avg_amount = 0 THEN NULL
                ELSE b.amount / b.prev_5d_avg_amount
            END AS amount_ratio_prev_5d_avg,
            CASE
                WHEN b.last_5d_avg_amount IS NULL OR b.last_5d_avg_amount = 0 THEN NULL
                ELSE b.amount / b.last_5d_avg_amount
            END AS amount_ratio_last_5d_avg,
            CASE
                WHEN b.prev_5d_sum_amount IS NULL THEN NULL
                ELSE b.amount + b.prev_5d_sum_amount
            END AS amount_plus_prev_5d_sum,
            CASE
                WHEN b.last_5d_sum_amount IS NULL THEN NULL
                ELSE b.amount + b.last_5d_sum_amount
            END AS amount_plus_last_5d_sum
        FROM selection_base AS b
        JOIN security_master AS s
            ON b.security_id = s.security_id
        JOIN requested_securities AS r
            ON r.security_id = b.security_id
           AND r.trade_date = b.trade_date
        ORDER BY b.trade_date ASC, s.symbol ASC
        """,
        [*params, end_date],
    ).fetchall()
    return {
        (row.symbol, row.trade_date): row
        for row in (_selection_result_row_from_sql(sql_row) for sql_row in rows)
    }


def _selection_result_row_from_sql(row) -> SelectionResultRow:
    return SelectionResultRow(
        symbol=row[0],
        name=row[1],
        trade_date=row[2],
        close_price=row[3],
        volume=row[4],
        amount=row[5],
        change_pct=row[6],
        turnover_ratio=row[7],
        prev_5d_avg_volume=row[8],
        prev_5d_sum_volume=row[9],
        last_5d_avg_volume=row[10],
        last_5d_sum_volume=row[11],
        volume_plus_prev_5d_sum=row[14],
        volume_plus_last_5d_sum=row[15],
        prev_5d_avg_amount=row[16],
        prev_5d_sum_amount=row[17],
        last_5d_avg_amount=row[18],
        last_5d_sum_amount=row[19],
        amount_plus_prev_5d_sum=row[22],
        amount_plus_last_5d_sum=row[23],
    )


def select_stocks(
    db_path: Path,
    trade_date: date,
    rules: list[SelectionRule],
    logic: str = "AND",
    sort_by: str = "symbol",
    sort_order: str = "ASC",
) -> SelectionSummary:
    with duckdb.connect(str(db_path)) as connection:
        return _select_stocks_with_connection(
            connection=connection,
            trade_date=trade_date,
            rules=rules,
            logic=logic,
            sort_by=sort_by,
            sort_order=sort_order,
        )
