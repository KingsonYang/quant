from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from quant.config.models import StrategyConfig


DEFAULT_STRATEGIES_DIR = Path("strategies")


class StrategyConfigError(ValueError):
    """Raised when a strategy YAML file is invalid."""


@dataclass(frozen=True)
class LoadedStrategyConfig:
    name: str
    path: Path
    config: StrategyConfig


def _strategy_files(strategies_dir: Path) -> list[Path]:
    if not strategies_dir.exists():
        raise StrategyConfigError(f"Strategies directory not found: {strategies_dir}")
    return sorted(
        [
            path
            for path in strategies_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {".yml", ".yaml"}
        ]
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StrategyConfigError(f"Failed to read strategy file: {path}") from exc
    except yaml.YAMLError as exc:
        raise StrategyConfigError(f"Invalid YAML in strategy file: {path}") from exc
    if not isinstance(payload, dict):
        raise StrategyConfigError(f"Strategy file must contain a YAML mapping: {path}")
    return payload


def _load_strategy_file(path: Path) -> LoadedStrategyConfig:
    payload = _load_yaml(path)
    try:
        config = StrategyConfig.model_validate(payload)
    except ValidationError as exc:
        raise StrategyConfigError(f"Invalid strategy config {path}: {exc}") from exc

    name = path.stem
    if config.strategy.name != name:
        raise StrategyConfigError(
            f"Strategy name mismatch for {path}: expected '{name}', got '{config.strategy.name}'."
        )

    return LoadedStrategyConfig(name=name, path=path, config=config)


def discover_strategy_configs(
    strategies_dir: Path = DEFAULT_STRATEGIES_DIR,
) -> list[LoadedStrategyConfig]:
    return [_load_strategy_file(path) for path in _strategy_files(strategies_dir)]


def load_strategy_config(
    strategy_name: str,
    strategies_dir: Path = DEFAULT_STRATEGIES_DIR,
) -> LoadedStrategyConfig:
    normalized_name = strategy_name.strip()
    if not normalized_name:
        raise StrategyConfigError("Strategy name cannot be empty.")

    for suffix in (".yml", ".yaml"):
        path = strategies_dir / f"{normalized_name}{suffix}"
        if path.exists():
            return _load_strategy_file(path)
    raise StrategyConfigError(
        f"Strategy '{normalized_name}' not found in {strategies_dir}."
    )
