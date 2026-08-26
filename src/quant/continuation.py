from __future__ import annotations

import hashlib
import json

from quant.config import LoadedStrategyConfig


def continuation_parameter_hash(loaded: LoadedStrategyConfig) -> str:
    payload = {
        "selection": loaded.config.selection.model_dump(mode="json"),
        "strategy_backtest": (
            None
            if loaded.config.strategy_backtest is None
            else loaded.config.strategy_backtest.model_dump(mode="json")
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
