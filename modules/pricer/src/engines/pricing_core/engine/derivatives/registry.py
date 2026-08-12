"""Exact instrument-type and method registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .enums import PricingMethod
from .instruments import OptionInstrument


PricingHandler = Callable[..., Any]
SolveHandler = Callable[..., Any]


@dataclass
class EngineRegistry:
    _price_handlers: dict[
        tuple[type[OptionInstrument], PricingMethod], PricingHandler
    ] = field(default_factory=dict)
    _solve_handlers: dict[
        tuple[type[OptionInstrument], PricingMethod], SolveHandler
    ] = field(default_factory=dict)

    def register_price(
        self,
        instrument_type: type[OptionInstrument],
        method: PricingMethod,
        handler: PricingHandler,
    ) -> None:
        self._price_handlers[(instrument_type, method)] = handler

    def register_solve(
        self,
        instrument_type: type[OptionInstrument],
        method: PricingMethod,
        handler: SolveHandler,
    ) -> None:
        self._solve_handlers[(instrument_type, method)] = handler

    def price_handler(
        self,
        instrument: OptionInstrument,
        method: PricingMethod,
    ) -> PricingHandler:
        key = (type(instrument), method)
        try:
            return self._price_handlers[key]
        except KeyError as exc:
            raise ValueError(
                f"不支持{type(instrument).__name__}与{method.name}的组合"
            ) from exc

    def solve_handler(
        self,
        instrument: OptionInstrument,
        method: PricingMethod,
    ) -> SolveHandler:
        key = (type(instrument), method)
        try:
            return self._solve_handlers[key]
        except KeyError as exc:
            raise ValueError(
                f"不支持{type(instrument).__name__}与{method.name}的反解组合"
            ) from exc
