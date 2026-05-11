from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation

from quant.ingest.baidu import BAIDU_SOURCE, PARSER_VERSION
from quant.ingest.models import DailyBarRecord, EventRecord


class JsonpParseError(ValueError):
    """Raised when JSONP content cannot be unpacked or parsed."""


def unwrap_jsonp(payload: str) -> str:
    text = payload.strip()
    start = text.find("(")
    end = text.rfind(")")
    if start <= 0 or end <= start:
        raise JsonpParseError("Invalid JSONP payload: missing callback wrapper")
    return text[start + 1 : end].strip()


def parse_baidu_payload(payload: str) -> dict:
    raw_json = unwrap_jsonp(payload)
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise JsonpParseError("Invalid JSON payload inside JSONP wrapper") from exc
    if "Result" not in parsed:
        raise JsonpParseError("Unexpected payload: missing Result field")
    return parsed


def _parse_decimal(value: str) -> Decimal | None:
    if value in {"", "--", "null", "None"}:
        return None
    try:
        return Decimal(value.replace("+", ""))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid decimal value: {value}") from exc


def _parse_int(value: str) -> int | None:
    decimal_value = _parse_decimal(value)
    if decimal_value is None:
        return None
    return int(decimal_value)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def parse_market_data_rows(payload: dict, security_id: int) -> list[DailyBarRecord]:
    result = payload["Result"]["newMarketData"]
    keys = result["keys"]
    market_data = result["marketData"]
    rows: list[DailyBarRecord] = []

    for raw_row in market_data.split(";"):
        if not raw_row.strip():
            continue
        values = raw_row.split(",")
        if len(values) != len(keys):
            raise ValueError(
                f"Market data row field count mismatch: expected {len(keys)}, got {len(values)}"
            )
        row = dict(zip(keys, values, strict=True))
        rows.append(
            DailyBarRecord(
                security_id=security_id,
                trade_date=_parse_date(row["time"]),
                ts_epoch=int(row["timestamp"]),
                open_price=_parse_decimal(row["open"]) or Decimal("0"),
                high_price=_parse_decimal(row["high"]) or Decimal("0"),
                low_price=_parse_decimal(row["low"]) or Decimal("0"),
                close_price=_parse_decimal(row["close"]) or Decimal("0"),
                pre_close_price=_parse_decimal(row["preClose"]),
                volume=_parse_int(row["volume"]),
                amount=_parse_decimal(row["amount"]),
                change_amount=_parse_decimal(row["range"]),
                change_pct=_parse_decimal(row["ratio"]),
                turnover_ratio=_parse_decimal(row["turnoverratio"]),
                ma5_price=_parse_decimal(row["ma5avgprice"]),
                ma10_price=_parse_decimal(row["ma10avgprice"]),
                ma20_price=_parse_decimal(row["ma20avgprice"]),
                ma5_volume=_parse_int(row["ma5volume"]),
                ma10_volume=_parse_int(row["ma10volume"]),
                ma20_volume=_parse_int(row["ma20volume"]),
                source_name=BAIDU_SOURCE,
                parser_version=PARSER_VERSION,
            )
        )
    return rows


def parse_report_events(payload: dict, security_id: int) -> list[EventRecord]:
    report_data = payload["Result"].get("reportData", {})
    records: list[EventRecord] = []

    for event_date, items in report_data.items():
        parsed_date = _parse_date(event_date)
        for item in items:
            content = item.get("data", "")
            payload_text = json.dumps(item, ensure_ascii=False, sort_keys=True)
            title = content.split(":", 1)[0] if ":" in content else None
            records.append(
                EventRecord(
                    security_id=security_id,
                    event_date=parsed_date,
                    event_type="financial_report",
                    event_title=title,
                    event_content=content,
                    event_payload=payload_text,
                    source_name=BAIDU_SOURCE,
                )
            )
    return records
