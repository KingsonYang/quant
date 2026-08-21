"""
Front-adjustment (QFQ) price computation module.

Implements the linear-transform front-adjustment algorithm verified against
TDXQuant's dividend_type='front' API output:

    QFQ_price = A * raw_price + B

A and B are computed by reverse-iterating dividend events (newest ex-date
to oldest), starting from A=1, B=0.  For each event:

    m = (10 + ShareBonus + Allotment) / 10   -- stock-change ratio
    c = (Bonus - Allotment * AllotPrice) / 10  -- cash offset per share
    A = A / m           # update A first
    B = B - A * c        # use updated A

Pure cash dividends (ShareBonus=0, Allotment=0) leave A=1 and only
decrease B by the per-share dividend amount.  Stock splits change A.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence

import pandas as pd


@dataclass(frozen=True)
class DividendEvent:
    """A single ex-dividend/ex-rights event.

    Fields mirror TDXQuant get_divid_factors() columns, normalised to
    per-share values where convenient.
    """
    ex_date: date
    bonus: float
    share_bonus: float
    allotment: float
    allot_price: float


def parse_divid_factors(df: pd.DataFrame) -> list[DividendEvent]:
    """Convert a get_divid_factors() DataFrame into a sorted list of events."""
    if df is None or df.empty:
        return []
    events: list[DividendEvent] = []
    for idx, row in df.iterrows():
        ex_date = pd.Timestamp(idx).date()
        events.append(
            DividendEvent(
                ex_date=ex_date,
                bonus=float(row.get("Bonus", 0) or 0),
                share_bonus=float(row.get("ShareBonus", 0) or 0),
                allotment=float(row.get("Allotment", 0) or 0),
                allot_price=float(row.get("AllotPrice", 0) or 0),
            )
        )
    events.sort(key=lambda e: e.ex_date)
    return events


def compute_qfq_params(
    events: Sequence[DividendEvent],
    target_date: date,
) -> tuple[float, float]:
    """Return (A, B) such that QFQ = A * raw_price + B for target_date.

    Only events with ex_date > target_date affect the result (future
    dividends relative to the target date are subtracted to bring the
    historical price to the front-adjusted baseline).
    """
    a, b = 1.0, 0.0
    future_events = [
        e for e in sorted(events, key=lambda e: e.ex_date, reverse=True)
        if e.ex_date > target_date
    ]
    for event in future_events:
        m = (10.0 + event.share_bonus + event.allotment) / 10.0
        c = (event.bonus - event.allotment * event.allot_price) / 10.0
        a = a / m
        b = b - a * c
    return a, b


def compute_qfq_params_for_all_dates(
    events: Sequence[DividendEvent],
    trade_dates: Sequence[date],
) -> dict[date, tuple[float, float]]:
    """Compute (A, B) for every date in trade_dates efficiently.

    Walks trade_dates newest-to-oldest, un-applying events as we cross
    their ex_date.  O(n_dates + n_events).
    """
    sorted_events = sorted(events, key=lambda e: e.ex_date)
    sorted_dates = sorted(trade_dates)
    if not sorted_dates:
        return {}

    result: dict[date, tuple[float, float]] = {}

    # Start at the latest trade date with all future events applied.
    a, b = 1.0, 0.0
    for event in reversed(sorted_events):
        if event.ex_date <= sorted_dates[-1]:
            break
        m = (10.0 + event.share_bonus + event.allotment) / 10.0
        c = (event.bonus - event.allotment * event.allot_price) / 10.0
        a = a / m
        b = b - a * c

    # Position to the last unapplied event (ex_date <= latest trade date).
    event_idx = len(sorted_events) - 1
    while event_idx >= 0 and sorted_events[event_idx].ex_date > sorted_dates[-1]:
        event_idx -= 1

    # Walk backwards, applying events as we cross their ex_date.
    for d in reversed(sorted_dates):
        while event_idx >= 0 and sorted_events[event_idx].ex_date > d:
            ev = sorted_events[event_idx]
            m = (10.0 + ev.share_bonus + ev.allotment) / 10.0
            c = (ev.bonus - ev.allotment * ev.allot_price) / 10.0
            # Apply forward transform (same direction as initial loop)
            a = a / m
            b = b - a * c
            event_idx -= 1
        result[d] = (a, b)

    return result


def apply_qfq(
    ohlc: pd.DataFrame,
    events: Sequence[DividendEvent],
    date_col: str = "trade_date",
    price_cols: tuple[str, ...] = ("open_price", "high_price", "low_price", "close_price"),
) -> pd.DataFrame:
    """Add QFQ columns to ohlc DataFrame and return it.

    Adds open_qfq, high_qfq, low_qfq, close_qfq columns computed via
    the linear transform QFQ = A * price + B.
    """
    qfq_cols = [c.replace("_price", "_qfq") for c in price_cols]
    if ohlc.empty:
        for qc in qfq_cols:
            ohlc[qc] = None
        return ohlc

    trade_dates = [pd.Timestamp(d).date() for d in ohlc[date_col].values]
    params_map = compute_qfq_params_for_all_dates(events, trade_dates)

    a_values: list[float] = []
    b_values: list[float] = []
    for d in trade_dates:
        a, b = params_map.get(d, (1.0, 0.0))
        a_values.append(a)
        b_values.append(b)

    a_series = pd.Series(a_values, index=ohlc.index, dtype=float)
    b_series = pd.Series(b_values, index=ohlc.index, dtype=float)

    for col, qfq_col in zip(price_cols, qfq_cols):
        ohlc[qfq_col] = (ohlc[col].astype(float) * a_series + b_series).round(4)

    return ohlc


# Backwards-compatible alias.
compute_qfq_ohlc = apply_qfq