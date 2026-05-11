from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quant.selection import SelectionRule


class StrategyRuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    operator: str
    value: str | int | float

    def to_selection_rule(self) -> SelectionRule:
        return SelectionRule(
            field=self.field,
            operator=self.operator,
            value=self.value,
        )


class StrategyMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = None
    description: str | None = None


class RulesSelectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["rules"] = "rules"
    rules: list[StrategyRuleConfig]
    logic: str = "AND"
    sort_by: str = "symbol"
    sort_order: str = "ASC"

    def to_selection_rules(self) -> list[SelectionRule]:
        return [rule.to_selection_rule() for rule in self.rules]


class SecondVolumeLaunchParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lookback_calendar_days: int = 120
    platform_max_trade_days: int = 30
    k0_volume_multiple: Decimal = Decimal("2.0")
    k1_volume_multiple: Decimal = Decimal("2.0")
    d0_change_min_pct: Decimal = Decimal("5.0")
    d1_change_min_pct: Decimal = Decimal("5.0")
    d1_prev2_max_change_pct: Decimal = Decimal("10.0")


class LimitUpPullbackMa5ReclaimParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lookback_calendar_days: int = 120
    limit_up_min_pct: Decimal = Decimal("9.5")
    limit_up_close_high_min_ratio: Decimal = Decimal("0.995")
    limit_up_volume_multiple: Decimal = Decimal("1.5")
    pullback_min_trade_days: int = 3
    pullback_max_trade_days: int = 15
    min_pullback_weak_days: int = 2
    weak_day_max_change_pct: Decimal = Decimal("1.5")
    support_low_tolerance_pct: Decimal = Decimal("6.0")
    close_support_tolerance_pct: Decimal = Decimal("2.5")
    must_touch_low_zone_pct: Decimal = Decimal("8.0")
    pullback_avg_volume_max_ratio: Decimal = Decimal("0.80")
    pullback_max_daily_change_pct: Decimal = Decimal("4.0")
    pullback_min_daily_change_pct: Decimal = Decimal("-6.0")
    recent_ma5_touch_tolerance_pct: Decimal = Decimal("1.0")
    signal_min_change_pct: Decimal = Decimal("0.5")
    signal_reclaim_low_tolerance_pct: Decimal = Decimal("2.0")
    signal_max_distance_from_t0_low_pct: Decimal = Decimal("12.0")
    signal_volume_ratio_min: Decimal = Decimal("0.5")
    signal_volume_ratio_max: Decimal = Decimal("2.5")
    deep_pullback_enabled: bool = False
    deep_limit_up_volume_multiple: Decimal = Decimal("0.6")
    deep_support_low_tolerance_pct: Decimal = Decimal("18.0")
    deep_close_support_tolerance_pct: Decimal = Decimal("15.0")
    deep_pullback_avg_volume_max_ratio: Decimal = Decimal("2.0")
    deep_pullback_max_daily_change_pct: Decimal = Decimal("12.0")
    deep_pullback_min_daily_change_pct: Decimal = Decimal("-10.0")
    deep_signal_reclaim_low_tolerance_pct: Decimal = Decimal("5.0")
    # composite score weights (sum should equal 1.0)
    score_weight_pullback_depth: Decimal = Decimal("0.25")
    score_weight_reclaim_strength: Decimal = Decimal("0.30")
    score_weight_volume_shrink: Decimal = Decimal("0.20")
    score_weight_weak_day_ratio: Decimal = Decimal("0.15")
    score_weight_turnover: Decimal = Decimal("0.10")


class PatternSelectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["pattern_sql"]
    pattern_id: Literal["second_volume_launch_v1", "limit_up_pullback_ma5_reclaim_v1"]
    params: SecondVolumeLaunchParams | LimitUpPullbackMa5ReclaimParams
    sort_by: str = "symbol"
    sort_order: str = "ASC"
    filter_rules: list[StrategyRuleConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_pattern_params(self) -> "PatternSelectionConfig":
        if self.pattern_id == "second_volume_launch_v1" and not isinstance(
            self.params, SecondVolumeLaunchParams
        ):
            raise ValueError("second_volume_launch_v1 requires SecondVolumeLaunchParams.")
        if self.pattern_id == "limit_up_pullback_ma5_reclaim_v1" and not isinstance(
            self.params, LimitUpPullbackMa5ReclaimParams
        ):
            raise ValueError("limit_up_pullback_ma5_reclaim_v1 requires LimitUpPullbackMa5ReclaimParams.")
        return self


SelectionConfig = Annotated[
    RulesSelectionConfig | PatternSelectionConfig,
    Field(discriminator="type"),
]


class BacktestSelectionDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    holding_period: int
    selection_top_n: int | None = None


class StrategyBacktestDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    holding_period: int
    max_positions: int
    max_daily_buys: int
    initial_capital: Decimal
    position_size_pct: Decimal
    take_profit_pct: Decimal
    stop_loss_pct: Decimal
    selection_top_n: int | None = None


OptimizationValue = str | int | float | Decimal


class OptimizationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    search_space: dict[str, list[OptimizationValue]]

    @field_validator("search_space")
    @classmethod
    def _validate_search_space(cls, value: dict[str, list[OptimizationValue]]):
        if not value:
            raise ValueError("optimization.search_space cannot be empty.")
        for path, candidates in value.items():
            if not path.strip():
                raise ValueError("optimization.search_space contains an empty parameter path.")
            if not candidates:
                raise ValueError(
                    f"optimization.search_space['{path}'] must contain at least one candidate value."
                )
        return value


class StrategyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    strategy: StrategyMetadata
    selection: SelectionConfig
    backtest_selection: BacktestSelectionDefaults | None = None
    strategy_backtest: StrategyBacktestDefaults | None = None
    optimization: OptimizationConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def _default_selection_type(cls, data):
        if not isinstance(data, dict):
            return data
        selection = data.get("selection")
        if isinstance(selection, dict) and "type" not in selection:
            patched_selection = dict(selection)
            patched_selection["type"] = "rules"
            patched_data = dict(data)
            patched_data["selection"] = patched_selection
            return patched_data
        return data
