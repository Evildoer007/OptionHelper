"""OptionReg产品到已验证Pricer内部结构的唯一模型路由。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from runtime.contracts.contract_api import load_registry


@dataclass(frozen=True)
class ProductCapability:
    family: str
    structure: str
    methods: tuple[str, ...]
    adapter: str
    analytical_engine_method: str | None = None


@dataclass(frozen=True)
class ModelRoute:
    capability: ProductCapability
    method: str


# 只有同时完成条款、单位、状态和产品级回归的映射可以进入此表。
PRODUCT_CAPABILITIES: dict[str, ProductCapability] = {
    "1.1": ProductCapability("VANILLA", "EUROPEAN_VANILLA", ("analytical", "monte_carlo"), "european_vanilla", "BLACK_SCHOLES"),
    "1.2": ProductCapability("VANILLA", "EUROPEAN_VANILLA", ("analytical", "monte_carlo"), "european_vanilla", "BLACK_SCHOLES"),
    **{
        product_id: ProductCapability("AIRBAG", "AIRBAG", ("analytical", "monte_carlo"), "european_portfolio", "STATIC_REPLICATION")
        for product_id in ("2.1", "2.2", "2.3", "2.4", "3.1", "3.2", "3.3", "3.4")
    },
    **{
        product_id: ProductCapability("BARRIER", "BARRIER", ("analytical", "monte_carlo"), "barrier", "REINER_RUBINSTEIN")
        for product_id in ("4.1", "4.2", "4.3", "4.4", "4.5", "4.6", "4.7", "4.8")
    },
    **{
        product_id: ProductCapability("DIGITAL", "BINARY", ("analytical", "monte_carlo"), "binary", "BINARY_ANALYTIC")
        for product_id in ("5.1", "5.2", "5.3", "5.4")
    },
    **{
        product_id: ProductCapability("AIRBAG", "AIRBAG", ("analytical", "monte_carlo"), "touch_portfolio", "STATIC_REPLICATION")
        for product_id in ("5.5", "5.6")
    },
    **{
        product_id: ProductCapability("AIRBAG", "AIRBAG", ("analytical", "monte_carlo"), "airbag_portfolio", "STATIC_REPLICATION")
        for product_id in ("6.1", "6.2", "6.3")
    },
    **{
        product_id: ProductCapability("AIRBAG", "AIRBAG", ("analytical", "monte_carlo"), "terminal_portfolio", "STATIC_REPLICATION")
        for product_id in ("9.2", "9.3")
    },
    **{
        product_id: ProductCapability("BARRIER", "BARRIER", ("analytical", "monte_carlo"), "sharkfin", "REINER_RUBINSTEIN")
        for product_id in ("9.5", "9.6")
    },
    "9.1": ProductCapability("MULTI_ASSET", "WORST_OF_CALL", ("analytical", "monte_carlo"), "worst_of", "WORST_OF_ANALYTIC"),
    "9.4": ProductCapability("VOLATILITY", "VARIANCE_SWAP", ("analytical", "monte_carlo"), "variance_swap", "VARIANCE_EXPECTATION"),
    "9.8": ProductCapability("ACCRUAL", "RANGE_ACCRUAL", ("analytical", "monte_carlo"), "range_accrual", "RANGE_ACCRUAL_ANALYTIC"),
}

# Every executable OptionReg entry has the same formal discrete-path fallback.
# Read the registry once instead of manufacturing numeric identifiers: unknown
# products and entry_status=False products remain rejected.
for _product_id, _product in load_registry()["products"].items():
    _identity = _product.get("identity", {})
    _terms = _product.get("terms", {})
    if _identity.get("entry_status") is True and "monte_carlo" in _terms.get("pricing_methods", ()):
        PRODUCT_CAPABILITIES.setdefault(
            str(_product_id),
            ProductCapability("OPTIONREG", "OPTIONREG_PATH", ("monte_carlo",), "optionreg_path"),
        )


def capability_for(product_id: str) -> ProductCapability | None:
    return PRODUCT_CAPABILITIES.get(str(product_id))


def resolve_route(product_id: str, optionreg_methods: Iterable[object], requested_method: object) -> ModelRoute | None:
    capability = capability_for(product_id)
    if capability is None:
        return None
    allowed = {str(method) for method in optionreg_methods}
    available = tuple(method for method in capability.methods if method in allowed)
    if not available:
        return None
    requested = None if requested_method is None else str(requested_method).strip()
    selected = available[0] if not requested else requested
    return ModelRoute(capability=capability, method=selected) if selected in available else None


__all__ = ("PRODUCT_CAPABILITIES", "ModelRoute", "ProductCapability", "capability_for", "resolve_route")
