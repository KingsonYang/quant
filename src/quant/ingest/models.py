from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class RawRequestRecord:
    source_name: str
    endpoint: str
    http_method: str
    query_params: str
    requested_at: datetime
    request_status: str
    error_message: str | None = None
    security_id: int | None = None


@dataclass(frozen=True)
class RawResponseRecord:
    source_name: str
    response_format: str
    response_body: str
    fetched_at: datetime
    parse_status: str
    parser_version: str
    checksum: str
    security_id: int | None = None


@dataclass(frozen=True)
class SecuritySeed:
    symbol: str
    exchange: str
    code: str
    name: str
    market: str = "ab"
    board: str | None = None
    list_status: str = "listed"
    data_source: str = "baidu"


@dataclass(frozen=True)
class DailyBarRecord:
    security_id: int
    trade_date: date
    ts_epoch: int
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    pre_close_price: Decimal | None
    volume: int | None
    amount: Decimal | None
    change_amount: Decimal | None
    change_pct: Decimal | None
    turnover_ratio: Decimal | None
    ma5_price: Decimal | None
    ma10_price: Decimal | None
    ma20_price: Decimal | None
    ma5_volume: int | None
    ma10_volume: int | None
    ma20_volume: int | None
    source_name: str
    parser_version: str


@dataclass(frozen=True)
class EventRecord:
    security_id: int
    event_date: date
    event_type: str
    event_title: str | None
    event_content: str
    event_payload: str
    source_name: str


@dataclass(frozen=True)
class IngestionSummary:
    symbol: str
    security_id: int
    raw_request_written: int
    raw_response_written: int
    daily_bars_written: int
    events_written: int


@dataclass(frozen=True)
class BatchIngestionSummary:
    total_symbols: int
    succeeded_symbols: int
    failed_symbols: int
    raw_requests_written: int
    raw_responses_written: int
    daily_bars_written: int
    events_written: int
