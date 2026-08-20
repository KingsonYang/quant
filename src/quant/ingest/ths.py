from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import duckdb

from quant.db.trading_calendar import TradingCalendarBuildSummary, build_trading_calendar_incremental
from quant.ingest.baidu import normalize_symbol
from quant.ingest.models import DailyBarRecord, SecuritySeed
from quant.ingest.storage import DuckDBIngestionStore


THS_SOURCE = "ths"
THS_PARSER_VERSION = "ths-xls-v1"


class ThsIngestionError(ValueError):
    """Raised when a THS xls/TSV file cannot be imported."""


class ThsParseError(ThsIngestionError):
    """Raised when a THS xls/TSV file cannot be parsed."""


@dataclass(frozen=True)
class ThsSnapshotRecord:
    symbol: str
    name: str
    trade_date: date
    source_file: str
    row_number: int
    raw_payload: dict[str, str]
    current_price: Decimal | None
    open_price: Decimal | None
    high_price: Decimal | None
    low_price: Decimal | None
    pre_close_price: Decimal | None
    change_amount: Decimal | None
    change_pct: Decimal | None
    volume: int | None
    amount: Decimal | None
    turnover_ratio: Decimal | None
    volume_ratio: Decimal | None
    total_market_cap: Decimal | None
    float_market_cap: Decimal | None
    float_market_cap_tagged: Decimal | None
    float_ratio: Decimal | None
    industry: str | None
    sub_industry: str | None
    concepts: str | None
    limit_type: str | None
    limit_reason: str | None
    first_limit_time: str | None
    last_limit_time: str | None
    limit_open_count: int | None
    limit_seal_ratio: Decimal | None
    limit_seal_amount: Decimal | None
    limit_seal_amount_new: Decimal | None
    limit_seal_volume_new: int | None
    consecutive_limit_days: int | None
    open_change_pct: Decimal | None
    auction_turnover_ratio: Decimal | None
    five_day_change_pct: Decimal | None
    ten_day_change_pct: Decimal | None
    twenty_day_change_pct: Decimal | None
    amplitude_pct: Decimal | None
    inner_volume: int | None
    outer_volume: int | None
    inner_outer_ratio: Decimal | None
    main_net_volume: Decimal | None
    buy_volume: int | None
    sell_volume: int | None
    transaction_count: int | None
    institutional_trend: Decimal | None
    total_shares: int | None
    float_shares: int | None
    net_profit_growth_pct: Decimal | None
    net_asset_per_share: Decimal | None
    golden_cross_count: int | None
    retail_holder_count: Decimal | None
    list_date: date | None


@dataclass(frozen=True)
class ThsImportSummary:
    source_file: Path
    trade_date: date
    parsed_rows: int
    snapshot_rows_written: int
    daily_bars_written: int
    securities_created: int
    invalid_rows: int
    skipped_daily_bar_rows: int
    derived_start_date: date
    derived_end_date: date
    derived_rows_refreshed: int
    calendar_rows_written: int
    calendar_rows_updated: int


@dataclass(frozen=True)
class DailyDerivedRefreshSummary:
    start_date: date
    end_date: date
    rows_refreshed: int


THS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ths_daily_snapshot (
    snapshot_id BIGINT PRIMARY KEY,
    security_id BIGINT NOT NULL,
    symbol VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    trade_date DATE NOT NULL,
    source_file TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    raw_payload_json TEXT NOT NULL,
    current_price DECIMAL(18,4),
    open_price DECIMAL(18,4),
    high_price DECIMAL(18,4),
    low_price DECIMAL(18,4),
    pre_close_price DECIMAL(18,4),
    change_amount DECIMAL(18,4),
    change_pct DECIMAL(10,4),
    volume BIGINT,
    amount DECIMAL(20,2),
    turnover_ratio DECIMAL(10,4),
    volume_ratio DECIMAL(18,4),
    total_market_cap DECIMAL(24,2),
    float_market_cap DECIMAL(24,2),
    float_market_cap_tagged DECIMAL(24,2),
    float_ratio DECIMAL(10,4),
    industry VARCHAR,
    sub_industry VARCHAR,
    concepts TEXT,
    limit_type VARCHAR,
    limit_reason TEXT,
    first_limit_time VARCHAR,
    last_limit_time VARCHAR,
    limit_open_count INTEGER,
    limit_seal_ratio DECIMAL(18,4),
    limit_seal_amount DECIMAL(24,2),
    limit_seal_amount_new DECIMAL(24,2),
    limit_seal_volume_new BIGINT,
    consecutive_limit_days INTEGER,
    open_change_pct DECIMAL(10,4),
    auction_turnover_ratio DECIMAL(10,4),
    five_day_change_pct DECIMAL(10,4),
    ten_day_change_pct DECIMAL(10,4),
    twenty_day_change_pct DECIMAL(10,4),
    amplitude_pct DECIMAL(10,4),
    inner_volume BIGINT,
    outer_volume BIGINT,
    inner_outer_ratio DECIMAL(18,4),
    main_net_volume DECIMAL(18,4),
    buy_volume BIGINT,
    sell_volume BIGINT,
    transaction_count BIGINT,
    institutional_trend DECIMAL(18,4),
    total_shares BIGINT,
    float_shares BIGINT,
    net_profit_growth_pct DECIMAL(18,4),
    net_asset_per_share DECIMAL(18,4),
    golden_cross_count INTEGER,
    retail_holder_count DECIMAL(18,4),
    list_date DATE,
    ingested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(symbol, trade_date),
    FOREIGN KEY (security_id) REFERENCES security_master(security_id)
);
"""


REQUIRED_COLUMNS = {
    "代码",
    "名称",
    "涨幅",
    "现价",
    "总手",
    "总金额",
    "换手",
    "昨收",
    "开盘",
    "最高",
    "最低",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ths_trade_date_from_path(path: Path) -> date:
    if path.suffix.lower() != ".xls":
        raise ThsParseError(f"THS file must use .xls suffix: {path}")
    stem = path.stem
    if not re.fullmatch(r"\d{8}", stem):
        raise ThsParseError(f"THS file name must be YYYYMMDD.xls: {path.name}")
    return date(int(stem[:4]), int(stem[4:6]), int(stem[6:8]))


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text or text == "--":
        return None
    return text


def _parse_decimal(value: str | None) -> Decimal | None:
    text = _clean_text(value)
    if text is None:
        return None
    text = (
        text.replace(",", "")
        .replace("↑", "")
        .replace("↓", "")
        .replace("+", "")
        .replace("%", "")
        .strip()
    )
    multiplier = Decimal("1")
    if text.endswith("亿"):
        multiplier = Decimal("100000000")
        text = text[:-1]
    elif text.endswith("万"):
        multiplier = Decimal("10000")
        text = text[:-1]
    try:
        return Decimal(text) * multiplier
    except InvalidOperation:
        return None


def _parse_int(value: str | None) -> int | None:
    decimal_value = _parse_decimal(value)
    if decimal_value is None:
        return None
    return int(decimal_value)


def _parse_date_yyyymmdd(value: str | None) -> date | None:
    text = _clean_text(value)
    if text is None or not re.fullmatch(r"\d{8}", text):
        return None
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def _get_by_prefix(payload: dict[str, str], prefix: str) -> str | None:
    for key, value in payload.items():
        if key.startswith(prefix):
            return value
    return None


def _normalize_header_and_values(
    header: list[str],
    values: list[str],
    *,
    row_number: int,
) -> tuple[list[str], list[str]]:
    normalized_header = [item.strip() for item in header]
    normalized_values = [item.strip() for item in values]
    if normalized_header and normalized_header[-1] == "":
        normalized_header = normalized_header[:-1]
        if len(normalized_values) == len(header) and normalized_values[-1] == "":
            normalized_values = normalized_values[:-1]
    if any(item == "" for item in normalized_header):
        raise ThsParseError("THS header contains empty columns outside the trailing blank column.")
    if len(normalized_values) != len(normalized_header):
        raise ThsParseError(
            f"THS row field count mismatch at row {row_number}: "
            f"expected {len(normalized_header)}, got {len(normalized_values)}"
        )
    return normalized_header, normalized_values


def _record_from_payload(
    payload: dict[str, str],
    *,
    trade_date: date,
    source_file: str,
    row_number: int,
) -> ThsSnapshotRecord:
    symbol = _clean_text(payload.get("代码"))
    name = _clean_text(payload.get("名称"))
    if symbol is None or name is None:
        raise ThsParseError(f"THS row {row_number} is missing symbol or name.")
    normalized_symbol, _, _ = normalize_symbol(symbol)
    return ThsSnapshotRecord(
        symbol=normalized_symbol,
        name=name,
        trade_date=trade_date,
        source_file=source_file,
        row_number=row_number,
        raw_payload=payload,
        current_price=_parse_decimal(payload.get("现价")),
        open_price=_parse_decimal(payload.get("开盘")),
        high_price=_parse_decimal(payload.get("最高")),
        low_price=_parse_decimal(payload.get("最低")),
        pre_close_price=_parse_decimal(payload.get("昨收")),
        change_amount=_parse_decimal(payload.get("涨跌")),
        change_pct=_parse_decimal(payload.get("涨幅")),
        volume=_parse_int(payload.get("总手")),
        amount=_parse_decimal(payload.get("总金额")),
        turnover_ratio=_parse_decimal(payload.get("换手")),
        volume_ratio=_parse_decimal(payload.get("量比")),
        total_market_cap=_parse_decimal(payload.get("总市值")),
        float_market_cap=_parse_decimal(payload.get("流通市值")),
        float_market_cap_tagged=_parse_decimal(payload.get(f"流通市值[{trade_date:%Y%m%d}]")),
        float_ratio=_parse_decimal(payload.get("流通比例")),
        industry=_clean_text(payload.get("所属行业")),
        sub_industry=_clean_text(payload.get("细分行业")),
        concepts=_clean_text(payload.get("所属概念")),
        limit_type=_clean_text(payload.get("涨停类型")),
        limit_reason=_clean_text(payload.get(f"涨停原因类别[{trade_date:%Y%m%d}]")),
        first_limit_time=_clean_text(payload.get(f"首次涨停时间[{trade_date:%Y%m%d}]")),
        last_limit_time=_clean_text(payload.get(f"最终涨停时间[{trade_date:%Y%m%d}]")),
        limit_open_count=_parse_int(payload.get(f"涨停开板次数[{trade_date:%Y%m%d}]")),
        limit_seal_ratio=_parse_decimal(_get_by_prefix(payload, "涨停封成比")),
        limit_seal_amount=_parse_decimal(payload.get(f"涨停封单额[{trade_date:%Y%m%d}]")),
        limit_seal_amount_new=_parse_decimal(payload.get("封单额_新")),
        limit_seal_volume_new=_parse_int(payload.get("封单量_新")),
        consecutive_limit_days=_parse_int(payload.get(f"连续涨停天数[{trade_date:%Y%m%d}]")),
        open_change_pct=_parse_decimal(payload.get("开盘涨幅")),
        auction_turnover_ratio=_parse_decimal(payload.get("竞价换手")),
        five_day_change_pct=_parse_decimal(payload.get("5日涨幅")),
        ten_day_change_pct=_parse_decimal(payload.get("10日涨幅")),
        twenty_day_change_pct=_parse_decimal(payload.get("20日涨幅")),
        amplitude_pct=_parse_decimal(payload.get("振幅")),
        inner_volume=_parse_int(payload.get("内盘")),
        outer_volume=_parse_int(payload.get("外盘")),
        inner_outer_ratio=_parse_decimal(payload.get("内外比")),
        main_net_volume=_parse_decimal(payload.get("主力净量")),
        buy_volume=_parse_int(payload.get("买量")),
        sell_volume=_parse_int(payload.get("卖量")),
        transaction_count=_parse_int(payload.get("笔数")),
        institutional_trend=_parse_decimal(payload.get("机构动向")),
        total_shares=_parse_int(payload.get("总股本")),
        float_shares=_parse_int(payload.get("流通股本")),
        net_profit_growth_pct=_parse_decimal(payload.get("净利润增长率")),
        net_asset_per_share=_parse_decimal(payload.get("每股净资产")),
        golden_cross_count=_parse_int(payload.get("金叉个数")),
        retail_holder_count=_parse_decimal(payload.get("散户数量")),
        list_date=_parse_date_yyyymmdd(payload.get("上市日期")),
    )


def parse_ths_xls(path: Path) -> tuple[date, list[ThsSnapshotRecord]]:
    trade_date = parse_ths_trade_date_from_path(path)
    if not path.exists():
        raise ThsParseError(f"THS file not found: {path}")
    lines = [
        line.rstrip("\r\n")
        for line in path.read_text(encoding="gb18030").splitlines()
        if line.strip()
    ]
    if not lines:
        raise ThsParseError(f"THS file is empty: {path}")

    raw_header = lines[0].split("\t")
    records: list[ThsSnapshotRecord] = []
    header: list[str] | None = None
    for row_number, line in enumerate(lines[1:], start=2):
        raw_values = line.split("\t")
        normalized_header, values = _normalize_header_and_values(
            raw_header,
            raw_values,
            row_number=row_number,
        )
        if header is None:
            header = normalized_header
            missing = sorted(REQUIRED_COLUMNS - set(header))
            if missing:
                raise ThsParseError(f"THS file missing required columns: {', '.join(missing)}")
        payload = dict(zip(header, values, strict=True))
        records.append(
            _record_from_payload(
                payload,
                trade_date=trade_date,
                source_file=str(path),
                row_number=row_number,
            )
        )
    return trade_date, records


def ensure_ths_schema(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(THS_SCHEMA_SQL)


def _next_snapshot_id(connection: duckdb.DuckDBPyConnection) -> int:
    return int(
        connection.execute(
            "SELECT COALESCE(MAX(snapshot_id), 0) + 1 FROM ths_daily_snapshot"
        ).fetchone()[0]
    )


def _insert_snapshot_rows(
    connection: duckdb.DuckDBPyConnection,
    security_ids: dict[str, int],
    records: list[ThsSnapshotRecord],
) -> int:
    if not records:
        return 0
    connection.execute("DELETE FROM ths_daily_snapshot WHERE trade_date = ?", [records[0].trade_date])
    now = _utcnow()
    next_id = _next_snapshot_id(connection)
    params = []
    for offset, record in enumerate(records):
        params.append(
            (
                next_id + offset,
                security_ids[record.symbol],
                record.symbol,
                record.name,
                record.trade_date,
                record.source_file,
                record.row_number,
                json.dumps(record.raw_payload, ensure_ascii=False, sort_keys=True),
                record.current_price,
                record.open_price,
                record.high_price,
                record.low_price,
                record.pre_close_price,
                record.change_amount,
                record.change_pct,
                record.volume,
                record.amount,
                record.turnover_ratio,
                record.volume_ratio,
                record.total_market_cap,
                record.float_market_cap,
                record.float_market_cap_tagged,
                record.float_ratio,
                record.industry,
                record.sub_industry,
                record.concepts,
                record.limit_type,
                record.limit_reason,
                record.first_limit_time,
                record.last_limit_time,
                record.limit_open_count,
                record.limit_seal_ratio,
                record.limit_seal_amount,
                record.limit_seal_amount_new,
                record.limit_seal_volume_new,
                record.consecutive_limit_days,
                record.open_change_pct,
                record.auction_turnover_ratio,
                record.five_day_change_pct,
                record.ten_day_change_pct,
                record.twenty_day_change_pct,
                record.amplitude_pct,
                record.inner_volume,
                record.outer_volume,
                record.inner_outer_ratio,
                record.main_net_volume,
                record.buy_volume,
                record.sell_volume,
                record.transaction_count,
                record.institutional_trend,
                record.total_shares,
                record.float_shares,
                record.net_profit_growth_pct,
                record.net_asset_per_share,
                record.golden_cross_count,
                record.retail_holder_count,
                record.list_date,
                now,
            )
        )

    connection.executemany(
        """
        INSERT INTO ths_daily_snapshot (
            snapshot_id, security_id, symbol, name, trade_date, source_file,
            row_number, raw_payload_json, current_price, open_price, high_price,
            low_price, pre_close_price, change_amount, change_pct, volume, amount,
            turnover_ratio, volume_ratio, total_market_cap, float_market_cap,
            float_market_cap_tagged, float_ratio, industry, sub_industry, concepts,
            limit_type, limit_reason, first_limit_time, last_limit_time,
            limit_open_count, limit_seal_ratio, limit_seal_amount,
            limit_seal_amount_new, limit_seal_volume_new, consecutive_limit_days,
            open_change_pct, auction_turnover_ratio, five_day_change_pct,
            ten_day_change_pct, twenty_day_change_pct, amplitude_pct, inner_volume,
            outer_volume, inner_outer_ratio, main_net_volume, buy_volume,
            sell_volume, transaction_count, institutional_trend, total_shares,
            float_shares, net_profit_growth_pct, net_asset_per_share,
            golden_cross_count, retail_holder_count, list_date, ingested_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        params,
    )
    return len(params)


def _existing_security_id(connection: duckdb.DuckDBPyConnection, symbol: str) -> int | None:
    row = connection.execute(
        "SELECT security_id FROM security_master WHERE symbol = ?",
        [symbol],
    ).fetchone()
    if row is None:
        return None
    return int(row[0])


def _ensure_security(
    store: DuckDBIngestionStore,
    connection: duckdb.DuckDBPyConnection,
    record: ThsSnapshotRecord,
) -> tuple[int, bool]:
    existing_id = _existing_security_id(connection, record.symbol)
    _, exchange, code = normalize_symbol(record.symbol)
    security_id = store.ensure_security(
        connection,
        SecuritySeed(
            symbol=record.symbol,
            exchange=exchange,
            code=code,
            name=record.name,
            data_source=THS_SOURCE if existing_id is None else "baidu",
        ),
    )
    if record.list_date is not None:
        connection.execute(
            """
            UPDATE security_master
            SET list_date = COALESCE(list_date, ?), updated_at = ?
            WHERE security_id = ?
            """,
            [record.list_date, _utcnow(), security_id],
        )
    return security_id, existing_id is None


def _daily_bar_from_record(security_id: int, record: ThsSnapshotRecord) -> DailyBarRecord | None:
    if (
        record.open_price is None
        or record.high_price is None
        or record.low_price is None
        or record.current_price is None
    ):
        return None
    return DailyBarRecord(
        security_id=security_id,
        trade_date=record.trade_date,
        ts_epoch=0,
        open_price=record.open_price,
        high_price=record.high_price,
        low_price=record.low_price,
        close_price=record.current_price,
        pre_close_price=record.pre_close_price,
        volume=record.volume,
        amount=record.amount,
        change_amount=record.change_amount,
        change_pct=record.change_pct,
        turnover_ratio=record.turnover_ratio,
        ma5_price=None,
        ma10_price=None,
        ma20_price=None,
        ma5_volume=None,
        ma10_volume=None,
        ma20_volume=None,
        source_name=THS_SOURCE,
        parser_version=THS_PARSER_VERSION,
    )


def _insert_missing_daily_bars(
    connection: duckdb.DuckDBPyConnection,
    bars: list[DailyBarRecord],
) -> int:
    if not bars:
        return 0
    now = _utcnow()
    params = []
    for bar in bars:
        exists = connection.execute(
            """
            SELECT 1
            FROM daily_bar
            WHERE security_id = ? AND trade_date = ?
            """,
            [bar.security_id, bar.trade_date],
        ).fetchone()
        if exists is not None:
            continue
        params.append(
            (
                bar.security_id,
                bar.trade_date,
                bar.ts_epoch,
                bar.open_price,
                bar.high_price,
                bar.low_price,
                bar.close_price,
                bar.pre_close_price,
                bar.volume,
                bar.amount,
                bar.change_amount,
                bar.change_pct,
                bar.turnover_ratio,
                bar.ma5_price,
                bar.ma10_price,
                bar.ma20_price,
                bar.ma5_volume,
                bar.ma10_volume,
                bar.ma20_volume,
                bar.source_name,
                bar.parser_version,
                now,
            )
        )
    if not params:
        return 0
    connection.executemany(
        """
        INSERT INTO daily_bar (
            security_id, trade_date, ts_epoch, open_price, high_price, low_price,
            close_price, pre_close_price, volume, amount, change_amount,
            change_pct, turnover_ratio, ma5_price, ma10_price, ma20_price,
            ma5_volume, ma10_volume, ma20_volume, source_name, parser_version,
            ingested_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        params,
    )
    return len(params)


def _normalize_derived_date_range(
    start_date: date | None,
    end_date: date | None,
    *,
    default_date: date,
) -> tuple[date, date]:
    normalized_start = start_date or end_date or default_date
    normalized_end = end_date or start_date or default_date
    if normalized_end < normalized_start:
        raise ThsIngestionError(
            f"Derived refresh end date {normalized_end} is before start date {normalized_start}."
        )
    return normalized_start, normalized_end


def refresh_daily_bar_derived_fields(
    db_path: Path,
    *,
    start_date: date,
    end_date: date | None = None,
) -> DailyDerivedRefreshSummary:
    """Refresh daily_bar moving-average fields for THS or incomplete rows."""
    normalized_end = end_date or start_date
    if normalized_end < start_date:
        raise ThsIngestionError(
            f"Derived refresh end date {normalized_end} is before start date {start_date}."
        )

    store = DuckDBIngestionStore(db_path)
    with store.connect() as connection:
        return _refresh_daily_bar_derived_fields_for_connection(
            connection,
            start_date=start_date,
            end_date=normalized_end,
        )


def _refresh_daily_bar_derived_fields_for_connection(
    connection: duckdb.DuckDBPyConnection,
    *,
    start_date: date,
    end_date: date,
) -> DailyDerivedRefreshSummary:
    target_rows = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM daily_bar
            WHERE trade_date BETWEEN ? AND ?
              AND (
                  source_name = ?
                  OR ma5_price IS NULL
                  OR ma10_price IS NULL
                  OR ma20_price IS NULL
                  OR ma5_volume IS NULL
                  OR ma10_volume IS NULL
                  OR ma20_volume IS NULL
              )
            """,
            [start_date, end_date, THS_SOURCE],
        ).fetchone()[0]
    )
    if target_rows == 0:
        return DailyDerivedRefreshSummary(
            start_date=start_date,
            end_date=end_date,
            rows_refreshed=0,
        )

    connection.execute(
        """
        CREATE OR REPLACE TEMP TABLE daily_bar_derived_refresh AS
        WITH calculated AS (
            SELECT
                security_id,
                trade_date,
                CAST(ROUND(AVG(close_price) OVER (
                    PARTITION BY security_id
                    ORDER BY trade_date
                    ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
                ), 4) AS DECIMAL(18,4)) AS ma5_price_calc,
                CAST(ROUND(AVG(close_price) OVER (
                    PARTITION BY security_id
                    ORDER BY trade_date
                    ROWS BETWEEN 9 PRECEDING AND CURRENT ROW
                ), 4) AS DECIMAL(18,4)) AS ma10_price_calc,
                CAST(ROUND(AVG(close_price) OVER (
                    PARTITION BY security_id
                    ORDER BY trade_date
                    ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                ), 4) AS DECIMAL(18,4)) AS ma20_price_calc,
                CAST(ROUND(AVG(volume) OVER (
                    PARTITION BY security_id
                    ORDER BY trade_date
                    ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
                ), 0) AS BIGINT) AS ma5_volume_calc,
                CAST(ROUND(AVG(volume) OVER (
                    PARTITION BY security_id
                    ORDER BY trade_date
                    ROWS BETWEEN 9 PRECEDING AND CURRENT ROW
                ), 0) AS BIGINT) AS ma10_volume_calc,
                CAST(ROUND(AVG(volume) OVER (
                    PARTITION BY security_id
                    ORDER BY trade_date
                    ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                ), 0) AS BIGINT) AS ma20_volume_calc
            FROM daily_bar
            WHERE trade_date <= ?
        )
        SELECT *
        FROM calculated
        WHERE trade_date BETWEEN ? AND ?
        """,
        [end_date, start_date, end_date],
    )
    connection.execute(
        """
        UPDATE daily_bar AS target
        SET
            ma5_price = derived.ma5_price_calc,
            ma10_price = derived.ma10_price_calc,
            ma20_price = derived.ma20_price_calc,
            ma5_volume = derived.ma5_volume_calc,
            ma10_volume = derived.ma10_volume_calc,
            ma20_volume = derived.ma20_volume_calc
        FROM daily_bar_derived_refresh AS derived
        WHERE target.security_id = derived.security_id
          AND target.trade_date = derived.trade_date
          AND (
              target.source_name = ?
              OR target.ma5_price IS NULL
              OR target.ma10_price IS NULL
              OR target.ma20_price IS NULL
              OR target.ma5_volume IS NULL
              OR target.ma10_volume IS NULL
              OR target.ma20_volume IS NULL
          )
        """,
        [THS_SOURCE],
    )
    connection.execute("DROP TABLE daily_bar_derived_refresh")
    return DailyDerivedRefreshSummary(
        start_date=start_date,
        end_date=end_date,
        rows_refreshed=target_rows,
    )


def ingest_ths_xls(
    xls_file: Path,
    db_path: Path,
    *,
    derived_start_date: date | None = None,
    derived_end_date: date | None = None,
) -> ThsImportSummary:
    trade_date, records = parse_ths_xls(xls_file)
    normalized_derived_start_date, normalized_derived_end_date = _normalize_derived_date_range(
        derived_start_date,
        derived_end_date,
        default_date=trade_date,
    )
    store = DuckDBIngestionStore(db_path)
    securities_created = 0
    skipped_daily_bar_rows = 0

    with store.connect() as connection:
        ensure_ths_schema(connection)
        security_ids: dict[str, int] = {}
        daily_bars: list[DailyBarRecord] = []
        for record in records:
            security_id, created = _ensure_security(store, connection, record)
            security_ids[record.symbol] = security_id
            if created:
                securities_created += 1
            daily_bar = _daily_bar_from_record(security_id, record)
            if daily_bar is None:
                skipped_daily_bar_rows += 1
                continue
            daily_bars.append(daily_bar)
        snapshot_rows_written = _insert_snapshot_rows(connection, security_ids, records)
        daily_bars_written = _insert_missing_daily_bars(connection, daily_bars)
        derived_summary = _refresh_daily_bar_derived_fields_for_connection(
            connection,
            start_date=normalized_derived_start_date,
            end_date=normalized_derived_end_date,
        )

    calendar_summary: TradingCalendarBuildSummary = build_trading_calendar_incremental(db_path)
    return ThsImportSummary(
        source_file=xls_file,
        trade_date=trade_date,
        parsed_rows=len(records),
        snapshot_rows_written=snapshot_rows_written,
        daily_bars_written=daily_bars_written,
        securities_created=securities_created,
        invalid_rows=0,
        skipped_daily_bar_rows=skipped_daily_bar_rows,
        derived_start_date=derived_summary.start_date,
        derived_end_date=derived_summary.end_date,
        derived_rows_refreshed=derived_summary.rows_refreshed,
        calendar_rows_written=calendar_summary.calendar_rows_written,
        calendar_rows_updated=calendar_summary.calendar_rows_updated,
    )
