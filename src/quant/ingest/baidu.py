from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from quant.ingest.models import RawRequestRecord, RawResponseRecord


BAIDU_SOURCE = "baidu"
BAIDU_ENDPOINT = "https://finance.pae.baidu.com/vapi/v1/getquotation"
PARSER_VERSION = "baidu-jsonp-v1"


@dataclass(frozen=True)
class FetchResult:
    request: RawRequestRecord
    response: RawResponseRecord


def normalize_symbol(symbol: str) -> tuple[str, str, str]:
    normalized = symbol.strip().upper()
    if len(normalized) != 8:
        raise ValueError(f"Unsupported symbol format: {symbol}")
    exchange = normalized[:2]
    code = normalized[2:]
    if exchange not in {"SZ", "SH", "BJ"} or not code.isdigit():
        raise ValueError(f"Unsupported symbol format: {symbol}")
    return normalized, exchange, code


def build_baidu_kline_params(symbol: str) -> dict[str, str | int]:
    _, _, code = normalize_symbol(symbol)
    callback = f"jsonp_{int(time.time() * 1000)}_{random.randint(10000, 99999)}"
    return {
        "srcid": 5353,
        "pointType": "string",
        "group": "quotation_kline_ab",
        "query": code,
        "code": code,
        "market_type": "ab",
        "newFormat": 1,
        "is_kc": 0,
        "ktype": "day",
        "cb": callback,
    }


def build_baidu_kline_url(symbol: str) -> str:
    return f"{BAIDU_ENDPOINT}?{urlencode(build_baidu_kline_params(symbol))}"


def fetch_baidu_kline(
    symbol: str,
    timeout: float = 15.0,
    client: httpx.Client | None = None,
) -> FetchResult:
    params = build_baidu_kline_params(symbol)
    requested_at = datetime.now(timezone.utc)

    if client is None:
        with httpx.Client(timeout=timeout) as managed_client:
            response = managed_client.get(BAIDU_ENDPOINT, params=params)
            response.raise_for_status()
    else:
        response = client.get(BAIDU_ENDPOINT, params=params)
        response.raise_for_status()

    fetched_at = datetime.now(timezone.utc)
    query_params = json.dumps(params, ensure_ascii=False, sort_keys=True)
    request_record = RawRequestRecord(
        source_name=BAIDU_SOURCE,
        endpoint=BAIDU_ENDPOINT,
        http_method="GET",
        query_params=query_params,
        requested_at=requested_at,
        request_status="success",
    )
    response_record = RawResponseRecord(
        source_name=BAIDU_SOURCE,
        response_format="jsonp",
        response_body=response.text,
        fetched_at=fetched_at,
        parse_status="pending",
        parser_version=PARSER_VERSION,
        checksum=str(hash(response.text)),
    )
    return FetchResult(request=request_record, response=response_record)
