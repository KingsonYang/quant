#!/usr/bin/env python3
"""
Legacy wrapper for the second-volume-launch pattern backtest.

Recommended replacement:
  quant run matrix strategy-holding-periods shandian_1hao_erci_baoliang_qidong ...
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from quant.backtest_common import (
    connect,
    load_all_trade_dates,
    load_price_cache,
    load_trade_dates_in_range,
    quantize_pct,
    validate_trade_date,
)
from quant.config import load_strategy_config
from quant.config.models import SecondVolumeLaunchParams
from quant.pattern_selection import (
    PatternSelectionCandidate,
    select_pattern_candidates_in_range_with_connection,
    select_pattern_candidates_with_connection,
)


STRATEGY_NAME = "shandian_1hao_erci_baoliang_qidong"
STRATEGIES_DIR = Path(__file__).resolve().parents[2] / "strategies"


@dataclass(frozen=True)
class PatternConfig:
    lookback_calendar_days: int = 120
    platform_max_trade_days: int = 30
    k0_volume_multiple: Decimal = Decimal("2.0")
    k1_volume_multiple: Decimal = Decimal("2.0")
    d0_change_min_pct: Decimal = Decimal("5.0")
    d1_change_min_pct: Decimal = Decimal("5.0")
    d1_prev2_max_change_pct: Decimal = Decimal("10.0")

    def to_params(self) -> SecondVolumeLaunchParams:
        return SecondVolumeLaunchParams(
            lookback_calendar_days=self.lookback_calendar_days,
            platform_max_trade_days=self.platform_max_trade_days,
            k0_volume_multiple=self.k0_volume_multiple,
            k1_volume_multiple=self.k1_volume_multiple,
            d0_change_min_pct=self.d0_change_min_pct,
            d1_change_min_pct=self.d1_change_min_pct,
            d1_prev2_max_change_pct=self.d1_prev2_max_change_pct,
        )


def select_symbols_for_date(
    connection,
    selection_date: date,
    config: PatternConfig,
) -> list[tuple[str, str, date]]:
    loaded = load_strategy_config(STRATEGY_NAME, STRATEGIES_DIR)
    selection = loaded.config.selection
    selection.params = config.to_params()
    candidates = select_pattern_candidates_with_connection(connection, selection_date, selection)
    return [(candidate.symbol, candidate.name, candidate.d0_date) for candidate in candidates]


def _pct(buy: Decimal, sell: Decimal) -> Decimal:
    return quantize_pct((sell - buy) * Decimal("100") / buy)


# 买入时可观测特征 (selection_date 当天算出, 无未来函数), 供选股池排序研究
FEATURE_COLUMNS = [
    "f_signal_change",
    "f_signal_turnover",
    "f_signal_vol_ratio_prev5",
    "f_signal_vol_vs_prev1",
    "f_d0_vol_ratio_prev5",
    "f_signal_vs_d0_close_pct",
    "f_platform_min_low_vs_d0_low_pct",
    "f_platform_avg_vol_vs_d0",
    "f_platform_weak_day_ratio",
    "f_between_days",
    "composite_score",
]


def _feat(candidate: PatternSelectionCandidate) -> list[str]:
    def s(v: object) -> str:
        return "" if v is None else str(v)

    return [
        s(candidate.f_signal_change),
        s(candidate.f_signal_turnover),
        s(candidate.f_signal_vol_ratio_prev5),
        s(candidate.f_signal_vol_vs_prev1),
        s(candidate.f_d0_vol_ratio_prev5),
        s(candidate.f_signal_vs_d0_close_pct),
        s(candidate.f_platform_min_low_vs_d0_low_pct),
        s(candidate.f_platform_avg_vol_vs_d0),
        s(candidate.f_platform_weak_day_ratio),
        s(candidate.f_between_days),
        s(candidate.composite_score),
    ]


def _strategy_candidates_in_range(
    connection,
    start_date: date,
    end_date: date,
    config: PatternConfig,
) -> dict[date, list[PatternSelectionCandidate]]:
    loaded = load_strategy_config(STRATEGY_NAME, STRATEGIES_DIR)
    selection = loaded.config.selection
    selection.params = config.to_params()
    return select_pattern_candidates_in_range_with_connection(
        connection,
        start_date,
        end_date,
        selection,
    )


def run_backtest(
    db_path: Path,
    start_date: date,
    end_date: date,
    holding_periods: list[int],
    config: PatternConfig,
    top_n: int | None = None,
) -> None:
    results_dir = Path("results")
    results_dir.mkdir(exist_ok=True)

    with connect(db_path) as connection:
        validate_trade_date(connection, start_date, ValueError)
        validate_trade_date(connection, end_date, ValueError)

        trade_dates = load_trade_dates_in_range(connection, start_date, end_date)
        all_trade_dates = load_all_trade_dates(connection)
        trade_date_index = {d: i for i, d in enumerate(all_trade_dates)}
        selection_map = _strategy_candidates_in_range(connection, start_date, end_date, config)

        for hp in holding_periods:
            relevant_symbols: set[str] = {
                candidate.symbol
                for rows in selection_map.values()
                for candidate in (rows[:top_n] if top_n is not None else rows)
            }
            if not relevant_symbols:
                print(f"[hp={hp}] No selections in range {start_date} -> {end_date}.")
                continue

            max_sell_index = 0
            for sel_date, rows in selection_map.items():
                selected_rows = rows[:top_n] if top_n is not None else rows
                if not selected_rows:
                    continue
                sel_index = trade_date_index.get(sel_date)
                if sel_index is None:
                    continue
                sell_index = sel_index + 1 + hp
                if sell_index > max_sell_index:
                    max_sell_index = sell_index
            max_sell_index = min(max_sell_index, len(all_trade_dates) - 1)

            price_cache = load_price_cache(
                connection=connection,
                symbols=relevant_symbols,
                start_trade_date=trade_dates[0],
                end_trade_date=all_trade_dates[max_sell_index],
            )

            out_csv = results_dir / f"pattern_selection_hp_{hp}.csv"
            completed_returns: list[Decimal] = []
            completed_count = 0
            incomplete_count = 0

            with out_csv.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    [
                        "symbol",
                        "name",
                        "selection_date",
                        "d0_date",
                        "buy_date",
                        "sell_date",
                        "buy_open",
                        "sell_close",
                        "return_pct",
                        "status",
                        "incomplete_reason",
                    ]
                    + FEATURE_COLUMNS
                )

                for sel_date, rows in selection_map.items():
                    selected_rows = rows[:top_n] if top_n is not None else rows
                    sel_index = trade_date_index[sel_date]
                    buy_index = sel_index + 1
                    sell_index = buy_index + hp
                    buy_date = all_trade_dates[buy_index] if buy_index < len(all_trade_dates) else None
                    sell_date = all_trade_dates[sell_index] if sell_index < len(all_trade_dates) else None

                    for candidate in selected_rows:
                        if buy_date is None:
                            incomplete_count += 1
                            writer.writerow(
                                [
                                    candidate.symbol,
                                    candidate.name,
                                    sel_date.isoformat(),
                                    candidate.d0_date.isoformat(),
                                    "",
                                    "",
                                    "",
                                    "",
                                    "",
                                    "incomplete",
                                    "missing_next_trade_date",
                                ]
                                + _feat(candidate)
                            )
                            continue

                        buy_open, _ = price_cache.get((candidate.symbol, buy_date), (None, None))
                        if buy_open is None:
                            incomplete_count += 1
                            writer.writerow(
                                [
                                    candidate.symbol,
                                    candidate.name,
                                    sel_date.isoformat(),
                                    candidate.d0_date.isoformat(),
                                    buy_date.isoformat(),
                                    "",
                                    "",
                                    "",
                                    "",
                                    "incomplete",
                                    "missing_buy_bar",
                                ]
                                + _feat(candidate)
                            )
                            continue

                        if sell_date is None:
                            incomplete_count += 1
                            writer.writerow(
                                [
                                    candidate.symbol,
                                    candidate.name,
                                    sel_date.isoformat(),
                                    candidate.d0_date.isoformat(),
                                    buy_date.isoformat(),
                                    "",
                                    str(buy_open),
                                    "",
                                    "",
                                    "incomplete",
                                    "insufficient_holding_window",
                                ]
                                + _feat(candidate)
                            )
                            continue

                        _, sell_close = price_cache.get((candidate.symbol, sell_date), (None, None))
                        if sell_close is None:
                            incomplete_count += 1
                            writer.writerow(
                                [
                                    candidate.symbol,
                                    candidate.name,
                                    sel_date.isoformat(),
                                    candidate.d0_date.isoformat(),
                                    buy_date.isoformat(),
                                    sell_date.isoformat(),
                                    str(buy_open),
                                    "",
                                    "",
                                    "incomplete",
                                    "missing_sell_bar",
                                ]
                                + _feat(candidate)
                            )
                            continue

                        ret = _pct(buy_open, sell_close)
                        completed_returns.append(ret)
                        completed_count += 1
                        writer.writerow(
                            [
                                candidate.symbol,
                                candidate.name,
                                sel_date.isoformat(),
                                candidate.d0_date.isoformat(),
                                buy_date.isoformat(),
                                sell_date.isoformat(),
                                str(buy_open),
                                str(sell_close),
                                str(ret),
                                "completed",
                                "",
                            ]
                            + _feat(candidate)
                        )

            avg_ret = None
            win_rate = None
            if completed_returns:
                avg_ret = quantize_pct(
                    sum(completed_returns, start=Decimal("0")) / Decimal(len(completed_returns))
                )
                win_rate = quantize_pct(
                    Decimal(sum(1 for r in completed_returns if r > 0)) * Decimal("100")
                    / Decimal(len(completed_returns))
                )

            print(
                f"[hp={hp}] range={start_date}->{end_date} "
                f"completed={completed_count} incomplete={incomplete_count} "
                f"avg_return_pct={avg_ret} win_rate_pct={win_rate} "
                f"csv={out_csv}"
            )


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(
            "Usage: python scripts/research/backtest_pattern_selection_hps.py <start_date> <end_date> <db_path> "
            "[--top-n N] [--k0 X] [--k1 X] [--hps 5,10,20,30] [--platform-max 30] [--lookback 120] "
            "[--d1-prev2-max-change 10]"
        )
        return 2

    print(
        "Legacy wrapper: prefer "
        "quant run matrix strategy-holding-periods "
        "shandian_1hao_erci_baoliang_qidong ..."
    )

    start = date.fromisoformat(argv[1])
    end = date.fromisoformat(argv[2])
    db_path = Path(argv[3])

    top_n = None
    holding_periods = [5, 10, 20, 30]
    cfg = PatternConfig()

    i = 4
    while i < len(argv):
        arg = argv[i]
        if arg == "--top-n":
            top_n = int(argv[i + 1])
            i += 2
            continue
        if arg == "--k0":
            cfg = PatternConfig(**{**cfg.__dict__, "k0_volume_multiple": Decimal(argv[i + 1])})
            i += 2
            continue
        if arg == "--k1":
            cfg = PatternConfig(**{**cfg.__dict__, "k1_volume_multiple": Decimal(argv[i + 1])})
            i += 2
            continue
        if arg == "--hps":
            holding_periods = [int(x.strip()) for x in argv[i + 1].split(",") if x.strip()]
            i += 2
            continue
        if arg == "--platform-max":
            cfg = PatternConfig(**{**cfg.__dict__, "platform_max_trade_days": int(argv[i + 1])})
            i += 2
            continue
        if arg == "--lookback":
            cfg = PatternConfig(**{**cfg.__dict__, "lookback_calendar_days": int(argv[i + 1])})
            i += 2
            continue
        if arg == "--d0-min-change":
            cfg = PatternConfig(**{**cfg.__dict__, "d0_change_min_pct": Decimal(argv[i + 1])})
            i += 2
            continue
        if arg == "--d1-min-change":
            cfg = PatternConfig(**{**cfg.__dict__, "d1_change_min_pct": Decimal(argv[i + 1])})
            i += 2
            continue
        if arg == "--d1-prev2-max-change":
            cfg = PatternConfig(**{**cfg.__dict__, "d1_prev2_max_change_pct": Decimal(argv[i + 1])})
            i += 2
            continue
        raise SystemExit(f"Unknown arg: {arg}")

    run_backtest(
        db_path=db_path,
        start_date=start,
        end_date=end,
        holding_periods=holding_periods,
        config=cfg,
        top_n=top_n,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
