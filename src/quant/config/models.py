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
    platform_min_low_ratio: Decimal = Decimal("1.0")
    low_position_window: int = 60
    max_d0_position_pct: Decimal | None = None
    enable_v2_filter: bool = False  # V2 过滤开关 (3 条规律)


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


class LimitUpFirstPullbackMa5ReclaimParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lookback_calendar_days: int = 120
    min_board_count: int = 1
    max_board_count: int = 3

    limit_up_min_pct: Decimal = Decimal("9.5")
    limit_up_close_high_min_ratio: Decimal = Decimal("0.995")
    limit_up_volume_multiple: Decimal = Decimal("1.0")

    pullback_min_trade_days: int = 3
    pullback_max_trade_days: int = 15

    enable_direct_pullback: bool = True
    enable_high_platform: bool = True
    platform_high_limit_pct: Decimal = Decimal("10.0")
    pullback_confirm_mode: Literal["below_last_limit_close", "ma5_touch", "either"] = "either"

    support_anchor: Literal["first_limit_open"] = "first_limit_open"
    standard_support_mode: Literal["close"] = "close"

    enable_false_break: bool = True
    false_break_tolerance_pct: Decimal = Decimal("5.0")
    false_break_reclaim_days: int = 2

    signal_open_below_ma5: bool = True
    signal_close_above_ma5: bool = True
    signal_require_bullish_candle: bool = True
    signal_min_change_pct: Decimal = Decimal("0.0")
    signal_max_above_last_limit_close_pct: Decimal = Decimal("0.0")

    enabled_branches: list[str] = Field(
        default_factory=lambda: [
            "single_direct_pullback_reclaim",
            "single_platform_then_pullback_reclaim",
            "false_break_first_open_reclaim",
            "multi_limit_pullback_reclaim",
        ]
    )

    @field_validator("enabled_branches")
    @classmethod
    def _validate_enabled_branches(cls, value: list[str]) -> list[str]:
        supported = {
            "single_direct_pullback_reclaim",
            "single_platform_then_pullback_reclaim",
            "false_break_first_open_reclaim",
            "multi_limit_pullback_reclaim",
        }
        if not value:
            raise ValueError("enabled_branches cannot be empty.")
        unknown = sorted(set(value) - supported)
        if unknown:
            raise ValueError(f"Unsupported enabled_branches: {', '.join(unknown)}.")
        return value

    @model_validator(mode="after")
    def _validate_board_count_range(self) -> "LimitUpFirstPullbackMa5ReclaimParams":
        if self.min_board_count < 1:
            raise ValueError("min_board_count must be >= 1.")
        if self.max_board_count > 3:
            raise ValueError("max_board_count must be <= 3.")
        if self.min_board_count > self.max_board_count:
            raise ValueError("min_board_count cannot be greater than max_board_count.")
        return self


class FirstLimitUpTrendContinuationParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit_up_min_pct: Decimal = Decimal("9.5")
    limit_up_close_high_min_ratio: Decimal = Decimal("0.995")
    volume_ratio_prev5_min: Decimal = Decimal("0.2")
    volume_ratio_prev5_max: Decimal = Decimal("4.0")
    turnover_min: Decimal = Decimal("0.5")
    turnover_max: Decimal = Decimal("60.0")
    amount_min_yi: Decimal = Decimal("0.05")
    require_first_board: bool = True
    require_break_prev20_high: bool = False
    require_ma_bull_stack: bool = False
    prev10_change_sum_min: Decimal = Decimal("-20.0")
    prev10_change_sum_max: Decimal = Decimal("80.0")


class LowPositionVolumeBreakoutParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    low_position_window: int = 60
    platform_window: int = 30
    breakout_window: int = 20
    volume_ratio_min: Decimal = Decimal("1.8")
    amount_min_yi: Decimal = Decimal("0.05")
    turnover_min: Decimal = Decimal("0.5")
    turnover_max: Decimal = Decimal("20.0")
    recent_10d_change_max: Decimal = Decimal("25.0")
    max_close_position_pct: Decimal = Decimal("65.0")
    platform_amplitude_max_pct: Decimal = Decimal("35.0")
    min_breakout_pct: Decimal = Decimal("0.0")
    max_breakout_pct: Decimal = Decimal("12.0")

    @model_validator(mode="after")
    def _validate_windows(self) -> "LowPositionVolumeBreakoutParams":
        if self.low_position_window < 20:
            raise ValueError("low_position_window must be at least 20.")
        if self.platform_window < 10:
            raise ValueError("platform_window must be at least 10.")
        if self.breakout_window < 5:
            raise ValueError("breakout_window must be at least 5.")
        if self.breakout_window > self.low_position_window:
            raise ValueError("breakout_window cannot exceed low_position_window.")
        if self.turnover_min < 0 or self.turnover_max <= self.turnover_min:
            raise ValueError("turnover_max must be greater than turnover_min.")
        return self


class TrendMainWaveRelayParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trend_slope_lookback: int = 10
    pullback_window: int = 8
    ma10_touch_tolerance_pct: Decimal = Decimal("3.0")
    ma20_support_tolerance_pct: Decimal = Decimal("3.0")
    signal_min_change_pct: Decimal = Decimal("1.0")
    signal_max_change_pct: Decimal = Decimal("8.0")
    signal_volume_ratio_min: Decimal = Decimal("0.8")
    signal_volume_ratio_max: Decimal = Decimal("2.5")
    amount_min_yi: Decimal = Decimal("0.05")
    turnover_min: Decimal = Decimal("0.8")
    turnover_max: Decimal = Decimal("25.0")
    recent_20d_change_min: Decimal = Decimal("8.0")
    recent_20d_change_max: Decimal = Decimal("70.0")
    position_from_60d_low_min_pct: Decimal = Decimal("12.0")
    position_from_60d_low_max_pct: Decimal = Decimal("120.0")
    max_distance_from_ma10_pct: Decimal = Decimal("12.0")
    max_upper_shadow_pct: Decimal = Decimal("6.0")
    ma20_slope_min_pct: Decimal = Decimal("-1.0")

    @model_validator(mode="after")
    def _validate_ranges(self) -> "TrendMainWaveRelayParams":
        if self.trend_slope_lookback < 3:
            raise ValueError("trend_slope_lookback must be at least 3.")
        if self.pullback_window < 3:
            raise ValueError("pullback_window must be at least 3.")
        if self.signal_max_change_pct <= self.signal_min_change_pct:
            raise ValueError("signal_max_change_pct must be greater than signal_min_change_pct.")
        if self.signal_volume_ratio_max <= self.signal_volume_ratio_min:
            raise ValueError("signal_volume_ratio_max must be greater than signal_volume_ratio_min.")
        if self.turnover_max <= self.turnover_min:
            raise ValueError("turnover_max must be greater than turnover_min.")
        if self.recent_20d_change_max <= self.recent_20d_change_min:
            raise ValueError("recent_20d_change_max must be greater than recent_20d_change_min.")
        if self.position_from_60d_low_max_pct <= self.position_from_60d_low_min_pct:
            raise ValueError(
                "position_from_60d_low_max_pct must be greater than "
                "position_from_60d_low_min_pct."
            )
        return self


class MainForceAccumulationParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    low_position_window: int = 60
    consolidation_window: int = 20
    max_close_position_pct: Decimal = Decimal("40.0")
    volume_shrink_ratio_max: Decimal = Decimal("0.85")
    signal_volume_ratio_min: Decimal = Decimal("1.5")
    signal_volume_ratio_max: Decimal = Decimal("3.0")
    amount_min_yi: Decimal = Decimal("5.0")
    min_change_pct: Decimal = Decimal("2.0")
    max_change_pct: Decimal = Decimal("9.5")
    require_platform_breakout: bool = True

    @model_validator(mode="after")
    def _validate(self) -> "MainForceAccumulationParams":
        if self.consolidation_window >= self.low_position_window:
            raise ValueError("consolidation_window must be less than low_position_window.")
        if self.signal_volume_ratio_max <= self.signal_volume_ratio_min:
            raise ValueError("signal_volume_ratio_max must be greater than signal_volume_ratio_min.")
        if self.max_change_pct <= self.min_change_pct:
            raise ValueError("max_change_pct must be greater than min_change_pct.")
        return self


class PatternSelectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["pattern_sql"]
    pattern_id: Literal[
        "second_volume_launch_v1",
        "limit_up_pullback_ma5_reclaim_v1",
        "limit_up_first_pullback_ma5_reclaim_v1",
        "first_limit_up_trend_continuation_v1",
        "low_position_volume_breakout_v1",
        "trend_main_wave_relay_v1",
        "main_force_accumulation_v1",
    ]
    params: (
        SecondVolumeLaunchParams
        | LimitUpPullbackMa5ReclaimParams
        | LimitUpFirstPullbackMa5ReclaimParams
        | FirstLimitUpTrendContinuationParams
        | LowPositionVolumeBreakoutParams
        | TrendMainWaveRelayParams
        | MainForceAccumulationParams
    )
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
        if self.pattern_id == "limit_up_first_pullback_ma5_reclaim_v1" and not isinstance(
            self.params, LimitUpFirstPullbackMa5ReclaimParams
        ):
            raise ValueError(
                "limit_up_first_pullback_ma5_reclaim_v1 requires "
                "LimitUpFirstPullbackMa5ReclaimParams."
            )
        if self.pattern_id == "first_limit_up_trend_continuation_v1" and not isinstance(
            self.params, FirstLimitUpTrendContinuationParams
        ):
            raise ValueError(
                "first_limit_up_trend_continuation_v1 requires "
                "FirstLimitUpTrendContinuationParams."
            )
        if self.pattern_id == "low_position_volume_breakout_v1" and not isinstance(
            self.params, LowPositionVolumeBreakoutParams
        ):
            raise ValueError(
                "low_position_volume_breakout_v1 requires "
                "LowPositionVolumeBreakoutParams."
            )
        if self.pattern_id == "trend_main_wave_relay_v1" and not isinstance(
            self.params, TrendMainWaveRelayParams
        ):
            raise ValueError(
                "trend_main_wave_relay_v1 requires "
                "TrendMainWaveRelayParams."
            )
        if self.pattern_id == "main_force_accumulation_v1" and not isinstance(
            self.params, MainForceAccumulationParams
        ):
            raise ValueError(
                "main_force_accumulation_v1 requires "
                "MainForceAccumulationParams."
            )
        return self


SelectionConfig = Annotated[
    RulesSelectionConfig | PatternSelectionConfig,
    Field(discriminator="type"),
]


class BacktestSelectionDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    holding_period: int
    selection_top_n: int | None = None


class MarketRegimeFilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    lookback_days: int = 20
    min_first_limit_count_avg: Decimal | None = None
    min_up_ratio: Decimal | None = None
    min_above_ma5_ratio: Decimal | None = None
    max_down_gt5_count: int | None = None
    max_limit_down_count: int | None = None
    recent_signal_window: int = 20
    recent_signal_min_count: int = 0
    min_recent_signal_win_rate: Decimal | None = None
    min_recent_signal_avg_return: Decimal | None = None
    limit_up_min_pct: Decimal = Decimal("9.5")
    limit_up_close_high_min_ratio: Decimal = Decimal("0.995")

    @model_validator(mode="after")
    def _validate_thresholds(self) -> "MarketRegimeFilterConfig":
        if self.lookback_days < 1:
            raise ValueError("market_filter.lookback_days must be at least 1.")
        if self.recent_signal_window < 1:
            raise ValueError("market_filter.recent_signal_window must be at least 1.")
        if self.recent_signal_min_count < 0:
            raise ValueError("market_filter.recent_signal_min_count must be non-negative.")
        for field_name in ("min_up_ratio", "min_above_ma5_ratio"):
            value = getattr(self, field_name)
            if value is not None and (value < 0 or value > 100):
                raise ValueError(f"market_filter.{field_name} must be between 0 and 100.")
        if self.min_recent_signal_win_rate is not None:
            if self.min_recent_signal_win_rate < 0 or self.min_recent_signal_win_rate > 100:
                raise ValueError("market_filter.min_recent_signal_win_rate must be between 0 and 100.")
        for field_name in ("max_down_gt5_count", "max_limit_down_count"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"market_filter.{field_name} must be non-negative.")
        return self


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
    min_composite_score: Decimal | None = None
    min_score_gap: Decimal | None = None
    market_filter: MarketRegimeFilterConfig | None = None
    intraday_stop_loss_enabled: bool = False
    commission_rate: Decimal = Decimal("0")
    stamp_tax_rate: Decimal = Decimal("0")
    transfer_fee_rate: Decimal = Decimal("0")
    buy_slippage_rate: Decimal = Decimal("0")
    sell_slippage_rate: Decimal = Decimal("0")


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
