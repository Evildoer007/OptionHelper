"""STANDARD-only pricing and solve entry points with explicit method routing."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from .enums import PricingMethod
from .instruments import (
    BarrierOption,
    BinaryOption,
    CompositeOption,
    EuropeanVanillaOption,
    FixedCashflowOption,
    OptionInstrument,
    OptionRegPathOption,
    StaticAccumulatorOption,
)
from .models import MarketState, SolveTarget, ValuationConfig, ValuationState
from .registry import EngineRegistry
from .results import PricingResult, SolveResult
from .standard.airbag import price_airbag_standard
from .standard.barrier import price_barrier_standard
from .standard.digital import price_binary_standard
from .standard.fixed_cashflow import price_fixed_cashflow_standard
from .standard.static_accumulator import price_static_accumulator_standard
from .standard.vanilla import price_vanilla_standard
from .standard.optionreg_path import price_optionreg_path_monte_carlo


def _load_product_catalog():
    alias = "_pricer_engine_catalog"
    current = sys.modules.get(alias)
    if current is not None:
        return current
    path = Path(__file__).resolve().parents[1] / "catalog.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载产品目录：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if sys.modules.get(alias) is module:
            sys.modules.pop(alias, None)
        raise
    return module


_CATALOG = _load_product_catalog()
REGISTRY = EngineRegistry()
_INSTRUMENT_TYPES = {
    "EuropeanVanillaOption": EuropeanVanillaOption,
    "FixedCashflowOption": FixedCashflowOption,
    "BinaryOption": BinaryOption,
    "BarrierOption": BarrierOption,
    "CompositeOption": CompositeOption,
    "StaticAccumulatorOption": StaticAccumulatorOption,
    "OptionRegPathOption": OptionRegPathOption,
}
_PRICE_HANDLERS = {
    "price_vanilla_standard": price_vanilla_standard,
    "price_binary_standard": price_binary_standard,
    "price_fixed_cashflow_standard": price_fixed_cashflow_standard,
    "price_barrier_standard": price_barrier_standard,
    "price_airbag_standard": price_airbag_standard,
    "price_static_accumulator_standard": price_static_accumulator_standard,
    "price_optionreg_path_monte_carlo": price_optionreg_path_monte_carlo,
}

for _route in _CATALOG.ENGINE_ROUTES.values():
    _instrument_type = _INSTRUMENT_TYPES[_route["instrument_type"]]
    _method = PricingMethod[_route["method"]]
    REGISTRY.register_price(
        _instrument_type,
        _method,
        _PRICE_HANDLERS[_route["price_handler"]],
    )

def price(
    instrument: OptionInstrument,
    market: MarketState,
    config: ValuationConfig,
    valuation_state: ValuationState | None = None,
) -> PricingResult:
    handler = REGISTRY.price_handler(instrument, config.method)
    return handler(instrument, market, config, valuation_state)


def solve(
    instrument: OptionInstrument,
    market: MarketState,
    config: ValuationConfig,
    target: SolveTarget,
    valuation_state: ValuationState | None = None,
) -> SolveResult:
    handler = REGISTRY.solve_handler(instrument, config.method)
    return handler(instrument, market, config, target, valuation_state)
