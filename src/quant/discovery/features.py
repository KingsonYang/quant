from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant.backtest_common import (
    connect,
    load_all_trade_dates,
    load_price_cache,
    load_trade_dates_in_range,
    quantize_pct,
    validate_trade_date,
)
from quant.config.loader import LoadedStrategyConfig
from quant.config.models import PatternSelectionConfig, RulesSelectionConfig
from quant.pattern_selection import select_pattern_candidates_in_range_with_connection
from quant.selection import (
    SelectionResultRow,
    _select_stocks_in_range_with_connection,
    load_selection_rows_for_symbol_dates_with_connection,
)


DISCOVERY_FEATURE_FIELDS = [
    "close_price",
    "change_pct",
    "turnover_ratio",
    "amount",
    "volume",
    "amount_ratio_prev_5d_avg",
    "volume_ratio_prev_5d_avg",
    "last_5d_avg_volume",
    "prev_5d_avg_amount",
    "amount_ratio_last_5d_avg",
    "volume_ratio_last_5d_avg",
    "last_5d_avg_amount",
]


class DiscoveryFeatureError(ValueError):
    """Raised when candidate feature samples cannot be built."""


@dataclass(frozen=True)
class CandidateFeatureSample:
    symbol: str
    name: str
    selection_date: date
    d0_date: date | None
    buy_date: date | None
    sell_date: date | None
    return_pct: float | None
    is_positive: bool | None
    status: str
    incomplete_reason: str | None
    features: dict[str, float | None]


@dataclass(frozen=True)
class _CandidateFeatureRow:
    row: SelectionResultRow
    d0_date: date | None = None


def build_candidate_feature_samples(
    *,
    db_path: Path,
    loaded: LoadedStrategyConfig,
    start_date: date,
    end_date: date,
) -> list[CandidateFeatureSample]:
    selection = loaded.config.selection
    if not isinstance(selection, (RulesSelectionConfig, PatternSelectionConfig)):
        raise DiscoveryFeatureError("discover filters supports rules and pattern_sql selection strategies only.")
    if loaded.config.backtest_selection is None:
        raise DiscoveryFeatureError(
            f"Strategy '{loaded.name}' does not define backtest_selection defaults."
        )

    holding_period = loaded.config.backtest_selection.holding_period
    selection_top_n = loaded.config.backtest_selection.selection_top_n
    if holding_period < 1:
        raise DiscoveryFeatureError("backtest_selection.holding_period must be at least 1.")
    if selection_top_n is not None and selection_top_n < 1:
        raise DiscoveryFeatureError("backtest_selection.selection_top_n must be at least 1 when provided.")
    if start_date > end_date:
        raise DiscoveryFeatureError("start_date must be on or before end_date.")

    with connect(db_path) as connection:
        validate_trade_date(connection, start_date, DiscoveryFeatureError)
        validate_trade_date(connection, end_date, DiscoveryFeatureError)
        trade_dates = load_trade_dates_in_range(connection, start_date, end_date)
        if not trade_dates:
            raise DiscoveryFeatureError("No trading dates found in the requested discovery range.")
        all_trade_dates = load_all_trade_dates(connection)
        trade_date_index = {trade_day: index for index, trade_day in enumerate(all_trade_dates)}
        selection_batches = _build_selection_batches(
            connection=connection,
            selection=selection,
            trade_dates=trade_dates,
            start_date=start_date,
            end_date=end_date,
            selection_top_n=selection_top_n,
        )

        relevant_symbols = {
            item.row.symbol
            for _, rows in selection_batches
            for item in rows
        }
        final_sell_index = max(
            (
                min(trade_date_index[selection_date] + 1 + holding_period, len(all_trade_dates) - 1)
                for selection_date, rows in selection_batches
                if rows
            ),
            default=0,
        )
        price_cache = load_price_cache(
            connection=connection,
            symbols=relevant_symbols,
            start_trade_date=trade_dates[0],
            end_trade_date=all_trade_dates[final_sell_index],
        )

    samples: list[CandidateFeatureSample] = []
    for selection_date, rows in selection_batches:
        selection_index = trade_date_index[selection_date]
        buy_index = selection_index + 1
        sell_index = buy_index + holding_period
        buy_date = all_trade_dates[buy_index] if buy_index < len(all_trade_dates) else None
        sell_date = all_trade_dates[sell_index] if sell_index < len(all_trade_dates) else None
        for item in rows:
            row = item.row
            features = _features_from_selection_row(row)
            if buy_date is None:
                samples.append(
                    _build_incomplete_sample(
                        item,
                        buy_date=None,
                        sell_date=None,
                        reason="missing_next_trade_date",
                        features=features,
                    )
                )
                continue
            buy_price, _ = price_cache.get((row.symbol, buy_date), (None, None))
            if buy_price is None:
                samples.append(
                    _build_incomplete_sample(
                        item,
                        buy_date=buy_date,
                        sell_date=None,
                        reason="missing_buy_bar",
                        features=features,
                    )
                )
                continue
            if sell_date is None:
                samples.append(
                    _build_incomplete_sample(
                        item,
                        buy_date=buy_date,
                        sell_date=None,
                        reason="insufficient_holding_window",
                        features=features,
                    )
                )
                continue
            _, sell_price = price_cache.get((row.symbol, sell_date), (None, None))
            if sell_price is None:
                samples.append(
                    _build_incomplete_sample(
                        item,
                        buy_date=buy_date,
                        sell_date=sell_date,
                        reason="missing_sell_bar",
                        features=features,
                    )
                )
                continue
            return_pct = quantize_pct((sell_price - buy_price) * Decimal("100") / buy_price)
            return_float = float(return_pct)
            samples.append(
                CandidateFeatureSample(
                    symbol=row.symbol,
                    name=row.name,
                    selection_date=selection_date,
                    d0_date=item.d0_date,
                    buy_date=buy_date,
                    sell_date=sell_date,
                    return_pct=return_float,
                    is_positive=return_float > 0,
                    status="completed",
                    incomplete_reason=None,
                    features=features,
                )
            )
    return samples


def completed_samples(samples: list[CandidateFeatureSample]) -> list[CandidateFeatureSample]:
    return [sample for sample in samples if sample.status == "completed" and sample.return_pct is not None]


def _build_selection_batches(
    *,
    connection,
    selection: RulesSelectionConfig | PatternSelectionConfig,
    trade_dates: list[date],
    start_date: date,
    end_date: date,
    selection_top_n: int | None,
) -> list[tuple[date, list[_CandidateFeatureRow]]]:
    if isinstance(selection, RulesSelectionConfig):
        grouped = _select_stocks_in_range_with_connection(
            connection=connection,
            start_date=start_date,
            end_date=end_date,
            rules=selection.to_selection_rules(),
            logic=selection.logic,
            sort_by=selection.sort_by,
            sort_order=selection.sort_order,
        )
        batches: list[tuple[date, list[_CandidateFeatureRow]]] = []
        for selection_date in trade_dates:
            rows = grouped.get(selection_date, [])
            if selection_top_n is not None:
                rows = rows[:selection_top_n]
            batches.append((selection_date, [_CandidateFeatureRow(row=row) for row in rows]))
        return batches

    pattern_grouped = select_pattern_candidates_in_range_with_connection(
        connection,
        start_date,
        end_date,
        selection,
    )
    symbol_dates = {
        (candidate.symbol, candidate.selection_date)
        for rows in pattern_grouped.values()
        for candidate in rows
    }
    feature_rows = load_selection_rows_for_symbol_dates_with_connection(
        connection,
        symbol_dates,
        end_date=end_date,
    )
    batches = []
    for selection_date in trade_dates:
        candidates = pattern_grouped.get(selection_date, [])
        if selection_top_n is not None:
            candidates = candidates[:selection_top_n]
        rows = []
        for candidate in candidates:
            feature_row = feature_rows.get((candidate.symbol, candidate.selection_date))
            if feature_row is not None:
                rows.append(_CandidateFeatureRow(row=feature_row, d0_date=candidate.d0_date))
        batches.append((selection_date, rows))
    return batches


def _build_incomplete_sample(
    item: _CandidateFeatureRow,
    *,
    buy_date: date | None,
    sell_date: date | None,
    reason: str,
    features: dict[str, float | None],
) -> CandidateFeatureSample:
    row = item.row
    return CandidateFeatureSample(
        symbol=row.symbol,
        name=row.name,
        selection_date=row.trade_date,
        d0_date=item.d0_date,
        buy_date=buy_date,
        sell_date=sell_date,
        return_pct=None,
        is_positive=None,
        status="incomplete",
        incomplete_reason=reason,
        features=features,
    )


def _features_from_selection_row(row: SelectionResultRow) -> dict[str, float | None]:
    return {
        "close_price": _to_float(row.close_price),
        "change_pct": _to_float(row.change_pct),
        "turnover_ratio": _to_float(row.turnover_ratio),
        "amount": _to_float(row.amount),
        "volume": _to_float(row.volume),
        "amount_ratio_prev_5d_avg": _safe_ratio(row.amount, row.prev_5d_avg_amount),
        "volume_ratio_prev_5d_avg": _safe_ratio(row.volume, row.prev_5d_avg_volume),
        "last_5d_avg_volume": _to_float(row.last_5d_avg_volume),
        "prev_5d_avg_amount": _to_float(row.prev_5d_avg_amount),
        "amount_ratio_last_5d_avg": _safe_ratio(row.amount, row.last_5d_avg_amount),
        "volume_ratio_last_5d_avg": _safe_ratio(row.volume, row.last_5d_avg_volume),
        "last_5d_avg_amount": _to_float(row.last_5d_avg_amount),
    }


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _safe_ratio(numerator: Any, denominator: Any) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    denominator_float = float(denominator)
    if denominator_float == 0:
        return None
    return float(numerator) / denominator_float
