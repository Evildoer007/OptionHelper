"""Stable result envelopes returned by the public API."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from .enums import PricingMethod


@dataclass(frozen=True)
class GreekValue:
    """One risk sensitivity reported on the unified PV bases.

    ``value`` is retained as the canonical ``pv_points_100_value`` for the
    public API.  The explicitly named fields prevent callers from guessing
    whether a Greek is expressed in points, percent-of-notional, or money.
    """

    value: float | None
    unit: str | None
    bump: float | None
    difference: str
    time_basis: str | None = None
    bump_details: dict[str, Any] = field(default_factory=dict)
    pv_amount_value: float | None = None
    pv_amount_unit: str | None = None
    pv_percent_value: float | None = None
    pv_percent_unit: str | None = None
    pv_points_100_value: float | None = None
    pv_points_100_unit: str | None = None
    status: str = "available"
    reason: str | None = None


@dataclass(frozen=True)
class PricingResult:
    pv_amount: float | None
    pv_percent: float | None
    pv_points_100: float | None
    currency: str | None
    greeks: dict[str, GreekValue]
    method: PricingMethod
    version: str
    extended_greeks: dict[str, GreekValue] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)
    engine_raw: dict[str, Any] = field(default_factory=dict)
    # OptionHelper模块协议字段。STANDARD引擎不必了解OptionReg，但可在适配层以
    # dataclasses.replace补齐，避免形成第二个同名结果模型。
    status: str = "priced"
    product_id: str | None = None
    contract_fingerprint: str | None = None
    standard_error: float | None = None
    probabilities: dict[str, Any] = field(default_factory=dict)
    scenario_pv: list[dict[str, Any]] = field(default_factory=list)
    market_snapshot: dict[str, Any] = field(default_factory=dict)
    input_snapshot: dict[str, Any] = field(default_factory=dict)
    resolved_pricing_config: dict[str, Any] = field(default_factory=dict)
    observed_contract_state: dict[str, Any] = field(default_factory=dict)
    risk_curves: list[dict[str, Any]] = field(default_factory=list)
    risk_surfaces: list[dict[str, Any]] = field(default_factory=list)
    risk_scenarios: list[dict[str, Any]] = field(default_factory=list)
    limitations: tuple[str, ...] = ()
    messages: tuple[str, ...] = ()
    # Machine-readable quotation gate.  A demo result is numerically real but
    # cannot be confused with a quote-eligible valuation downstream.
    precision_status: str = "quote_eligible"
    quote_eligible: bool = True
    path_count: int | None = None

    @property
    def model_version(self) -> str:
        """OptionHelper协议的名称，STANDARD仍以version保存实体版本。"""
        return self.version

    @property
    def pv(self) -> float | None:
        """OptionHelper持有方金额口径的兼容读取名。"""
        return self.pv_amount

    def to_dict(self) -> dict[str, Any]:
        def normalize(value: Any) -> Any:
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, dict):
                return {str(key): normalize(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [normalize(item) for item in value]
            return value
        return normalize(asdict(self))


@dataclass(frozen=True)
class SolveResult:
    value: float
    variable: str
    target_pv: float
    converged: bool
    method: PricingMethod
    version: str
    warnings: tuple[str, ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)
    engine_raw: dict[str, Any] = field(default_factory=dict)
