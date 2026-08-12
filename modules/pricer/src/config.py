"""Pricer运行配置。条款仍只来自ResolvedContract。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import isfinite
from typing import Any, Mapping


class PricingConfigError(ValueError):
    """本次模型与数值配置不满足正式Pricer约束。"""


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
    model_method: str = "auto"
    path_count: int = 2_000
    demo_mode: bool = False
    demo_calendar: dict[str, Any] | None = None
    random_seed: int = 20240101
    correlation: list[list[float]] | None = None
    greek_bumps: dict[str, float] = field(default_factory=lambda: {"spot": 0.01, "volatility": 0.01, "time": 1 / 365, "rate": 0.0001})
    scenarios: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.hv_window not in {5, 10, 20, 60, 122, 244}:
            raise PricingConfigError("hv_window只能为5、10、20、60、122或244")
        if self.model_method not in {"auto", "black_scholes", "monte_carlo"}:
            raise PricingConfigError("model_method只能为auto、black_scholes或monte_carlo")
        if not isinstance(self.path_count, int) or isinstance(self.path_count, bool) or not 10 <= self.path_count <= 2_000:
            raise PricingConfigError("path_count必须为10至2000的整数，以匹配已冻结随机矩阵")
        if not isinstance(self.demo_mode, bool):
            raise PricingConfigError("demo_mode必须为布尔值")
        if self.demo_calendar is not None and not isinstance(self.demo_calendar, Mapping):
            raise PricingConfigError("demo_calendar必须为对象或None")
        if self.random_seed != 20240101:
            raise PricingConfigError("random_seed固定为20240101，以保持正式MC可复现")
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


__all__ = ("PricingConfig", "PricingConfigError")
