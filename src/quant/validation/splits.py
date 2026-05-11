from __future__ import annotations

from dataclasses import dataclass
from datetime import date


class SplitError(ValueError):
    """Raised when validation date windows cannot be built."""


@dataclass(frozen=True)
class DateSegment:
    name: str
    start_date: date
    end_date: date
    trade_days: int


@dataclass(frozen=True)
class WalkForwardWindow:
    fold: int
    train_start_date: date
    train_end_date: date
    test_start_date: date
    test_end_date: date
    train_trade_days: int
    test_trade_days: int


def build_split_segments(
    trade_dates: list[date],
    ratios: tuple[int, int, int],
) -> list[DateSegment]:
    if len(trade_dates) < 3:
        raise SplitError("At least 3 trading days are required for train/validation/test split.")
    if len(ratios) != 3 or any(value <= 0 for value in ratios):
        raise SplitError("split ratios must contain three positive integers.")
    total = sum(ratios)
    train_count = max(1, int(len(trade_dates) * ratios[0] / total))
    validation_count = max(1, int(len(trade_dates) * ratios[1] / total))
    while train_count + validation_count >= len(trade_dates):
        if train_count >= validation_count and train_count > 1:
            train_count -= 1
        elif validation_count > 1:
            validation_count -= 1
        else:
            break
    test_count = len(trade_dates) - train_count - validation_count
    if test_count < 1:
        raise SplitError("Unable to allocate at least 1 trading day to each split segment.")
    windows = [
        ("train", trade_dates[:train_count]),
        ("validation", trade_dates[train_count : train_count + validation_count]),
        ("test", trade_dates[train_count + validation_count :]),
    ]
    return [
        DateSegment(
            name=name,
            start_date=window[0],
            end_date=window[-1],
            trade_days=len(window),
        )
        for name, window in windows
    ]


def build_walk_forward_windows(
    trade_dates: list[date],
    folds: int,
) -> list[WalkForwardWindow]:
    if folds < 1:
        raise SplitError("walk-forward folds must be at least 1.")
    block_count = folds + 1
    if len(trade_dates) < block_count:
        raise SplitError(
            f"At least {block_count} trading days are required for {folds} walk-forward fold(s)."
        )
    blocks = _split_into_blocks(trade_dates, block_count)
    windows: list[WalkForwardWindow] = []
    for index in range(folds):
        train_dates = [day for block in blocks[: index + 1] for day in block]
        test_dates = blocks[index + 1]
        windows.append(
            WalkForwardWindow(
                fold=index + 1,
                train_start_date=train_dates[0],
                train_end_date=train_dates[-1],
                test_start_date=test_dates[0],
                test_end_date=test_dates[-1],
                train_trade_days=len(train_dates),
                test_trade_days=len(test_dates),
            )
        )
    return windows


def _split_into_blocks(trade_dates: list[date], block_count: int) -> list[list[date]]:
    base_size = len(trade_dates) // block_count
    remainder = len(trade_dates) % block_count
    blocks: list[list[date]] = []
    cursor = 0
    for index in range(block_count):
        size = base_size + (1 if index < remainder else 0)
        blocks.append(trade_dates[cursor : cursor + size])
        cursor += size
    return blocks
