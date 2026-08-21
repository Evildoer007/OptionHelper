"""OptionReg产品到已验证Pricer内部结构的唯一模型路由。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from runtime.contracts.contract_api import load_registry


@dataclass(frozen=True)
class ProductCapability:
    family: str
    structure: str
    methods: tuple[str, ...]
    adapter: str


@dataclass(frozen=True)
class ModelRoute:
    capability: ProductCapability
    method: str


# 只有同时完成条款、单位、状态和产品级回归的映射可以进入此表。
PRODUCT_CAPABILITIES: dict[str, ProductCapability] = {
    "1.1": ProductCapability("VANILLA", "EUROPEAN_VANILLA", ("black_scholes", "monte_carlo"), "european_vanilla"),
    "1.2": ProductCapability("VANILLA", "EUROPEAN_VANILLA", ("black_scholes", "monte_carlo"), "european_vanilla"),
    **{
        product_id: ProductCapability("AIRBAG", "AIRBAG", ("black_scholes", "monte_carlo"), "european_portfolio")
        for product_id in ("2.1", "2.2", "2.3", "2.4", "3.1", "3.2", "3.3", "3.4")
    },
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


def resolve_route(product_id: str, optionreg_methods: Iterable[object], requested_method: str) -> ModelRoute | None:
    capability = capability_for(product_id)
    if capability is None:
        return None
    allowed = {str(method) for method in optionreg_methods}
    available = tuple(method for method in capability.methods if method in allowed)
    if not available:
        return None
    selected = "black_scholes" if requested_method == "auto" and "black_scholes" in available else (available[0] if requested_method == "auto" else requested_method)
    return ModelRoute(capability=capability, method=selected) if selected in available else None


__all__ = ("ModelRoute", "ProductCapability", "PRODUCT_CAPABILITIES", "capability_for", "resolve_route")
