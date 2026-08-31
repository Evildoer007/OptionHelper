"""Pricer运行配置。条款仍只来自ResolvedContract。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import isfinite
from typing import Any, Mapping


class PricingConfigError(ValueError):
    """本次模型与数值配置不满足正式Pricer约束。"""


@dataclass(frozen=True)
class RiskGridConfig:
    """Greeks曲线与曲面的真实重估范围。"""

    mode: str = "contract_aware"
    spot_min_normalized: float | None = None
    spot_max_normalized: float | None = None
    spot_curve_points: int = 61
    spot_surface_points: int = 31
    time_points: int = 11
    volatility_min: float | None = None
    volatility_max: float | None = None
    volatility_points: int = 31
    risk_free_rate_min: float | None = None
    risk_free_rate_max: float | None = None
    risk_free_rate_points: int = 31

    def __post_init__(self) -> None:
        if self.mode not in {"contract_aware", "custom"}:
            raise PricingConfigError("risk_grid.mode只能为contract_aware或custom")
        if self.mode == "custom" and (
            self.spot_min_normalized is None or self.spot_max_normalized is None
        ):
            raise PricingConfigError("custom风险网格必须同时提供spot_min_normalized和spot_max_normalized")
        _optional_positive("risk_grid.spot_min_normalized", self.spot_min_normalized)
        _optional_positive("risk_grid.spot_max_normalized", self.spot_max_normalized)
        _optional_positive("risk_grid.volatility_min", self.volatility_min)
        _optional_positive("risk_grid.volatility_max", self.volatility_max)
        _optional_finite("risk_grid.risk_free_rate_min", self.risk_free_rate_min)
        _optional_finite("risk_grid.risk_free_rate_max", self.risk_free_rate_max)
        if (
            self.spot_min_normalized is not None
            and self.spot_max_normalized is not None
            and self.spot_min_normalized >= self.spot_max_normalized
        ):
            raise PricingConfigError("risk_grid的Spot下限必须小于上限")
        if (
            self.volatility_min is not None
            and self.volatility_max is not None
            and self.volatility_min >= self.volatility_max
        ):
            raise PricingConfigError("risk_grid的波动率下限必须小于上限")
        if (
            self.risk_free_rate_min is not None
            and self.risk_free_rate_max is not None
            and self.risk_free_rate_min >= self.risk_free_rate_max
        ):
            raise PricingConfigError("risk_grid的无风险利率下限必须小于上限")
        _bounded_count("risk_grid.spot_curve_points", self.spot_curve_points, 9, 81)
        _bounded_count("risk_grid.spot_surface_points", self.spot_surface_points, 7, 41)
        _bounded_count("risk_grid.time_points", self.time_points, 3, 17)
        _bounded_count("risk_grid.volatility_points", self.volatility_points, 7, 41)
        _bounded_count("risk_grid.risk_free_rate_points", self.risk_free_rate_points, 7, 61)


@dataclass(frozen=True)
class PricingConfig:
    valuation_date: str | None = None
    spot: float | dict[str, float] | None = None
    historical_volatility: float | dict[str, float] | None = None
    volatility_override: float | dict[str, float] | None = None
    hv_window: int = 20
    risk_free_rate: float = 0.0
    dividend_yield: float | dict[str, float] = 0.0
    time_to_maturity: float | None = None
    model_method: str | None = None
    # Monte Carlo路径数必须由调用方显式指定；Pricer不替用户选择精度。
    path_count: int | None = None
    # 测试夹具专用，不属于页面或正式Tool公开输入。
    demo_mode: bool = False
    demo_calendar: dict[str, Any] | None = None
    random_seed: int = 20240101
    correlation: list[list[float]] | None = None
    greek_bumps: dict[str, float] = field(default_factory=lambda: {"spot": 0.01, "volatility": 0.01, "time": 1 / 365, "rate": 0.0001})
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    risk_grid: RiskGridConfig = field(default_factory=RiskGridConfig)

    def __post_init__(self) -> None:
        if isinstance(self.risk_grid, Mapping):
            object.__setattr__(self, "risk_grid", RiskGridConfig(**dict(self.risk_grid)))
        elif not isinstance(self.risk_grid, RiskGridConfig):
            raise PricingConfigError("risk_grid必须为RiskGridConfig或对象")
        if self.hv_window not in {5, 10, 20, 60, 122, 244}:
            raise PricingConfigError("hv_window只能为5、10、20、60、122或244")
        if self.model_method not in {None, "analytical", "monte_carlo"}:
            raise PricingConfigError("model_method只能为analytical、monte_carlo或省略")
        if self.path_count is not None and (
            not isinstance(self.path_count, int) or isinstance(self.path_count, bool) or self.path_count <= 0
        ):
            raise PricingConfigError("path_count必须为正整数")
        if not isinstance(self.demo_mode, bool):
            raise PricingConfigError("demo_mode必须为布尔值")
        if self.demo_calendar is not None and not isinstance(self.demo_calendar, Mapping):
            raise PricingConfigError("demo_calendar必须为对象或None")
        if (
            not isinstance(self.random_seed, int)
            or isinstance(self.random_seed, bool)
            or not 0 <= self.random_seed <= 0xFFFFFFFF
        ):
            raise PricingConfigError("random_seed必须为0至4294967295之间的整数")
        if self.correlation is not None:
            if not isinstance(self.correlation, list) or not self.correlation or any(
                not isinstance(row, list) for row in self.correlation
            ):
                raise PricingConfigError("correlation必须为非空数值方阵或None")
            size = len(self.correlation)
            if any(len(row) != size for row in self.correlation):
                raise PricingConfigError("correlation必须为方阵")
            for row in self.correlation:
                for value in row:
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
                        raise PricingConfigError("correlation必须仅含有限数值")
        if self.time_to_maturity is not None and (
            isinstance(self.time_to_maturity, bool)
            or not isinstance(self.time_to_maturity, (int, float))
            or not isfinite(float(self.time_to_maturity))
            or self.time_to_maturity <= 0
        ):
            raise PricingConfigError("time_to_maturity为空或正有限数")
        if not isinstance(self.greek_bumps, Mapping) or set(self.greek_bumps) != {"spot", "volatility", "time", "rate"}:
            raise PricingConfigError("greek_bumps必须含spot、volatility、time、rate")
        for key, value in self.greek_bumps.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)) or value <= 0:
                raise PricingConfigError(f"greek_bumps.{key}必须为正有限数")
        if self.greek_bumps["spot"] >= 1:
            raise PricingConfigError("greek_bumps.spot必须小于1")
        if not isinstance(self.scenarios, list):
            raise PricingConfigError("scenarios必须为列表")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None = None) -> "PricingConfig":
        supplied = dict(value or {})
        unknown = set(supplied) - set(cls.__dataclass_fields__)
        if unknown:
            raise PricingConfigError(f"PricingConfig含未知字段：{','.join(sorted(unknown))}")
        return cls(**supplied)


def _optional_positive(name: str, value: float | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)) or value <= 0:
        raise PricingConfigError(f"{name}必须为正有限数或None")


def _optional_finite(name: str, value: float | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise PricingConfigError(f"{name}必须为有限数或None")


def _bounded_count(name: str, value: int, minimum: int, maximum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise PricingConfigError(f"{name}必须为{minimum}至{maximum}之间的整数")


__all__ = ("PricingConfig", "PricingConfigError", "RiskGridConfig")
