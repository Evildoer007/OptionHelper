"""Independent STANDARD engine for explicit Airbag option-leg portfolios."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any

from ..basis import convert_points_100
from ..enums import PricingMethod
from ..instruments import (
    BarrierOption,
    BinaryOption,
    CompositeOption,
    EuropeanVanillaOption,
)
from ..models import MarketState, ValuationConfig, ValuationState
from ..results import GreekValue, PricingResult
from .risk import make_risk_value


_VERSION = "standard-airbag-2"
_SUPPORTED_LEG_TYPES = (EuropeanVanillaOption, BinaryOption, BarrierOption)


def price_airbag_standard(
    instrument: CompositeOption,
    market: MarketState,
    config: ValuationConfig,
    valuation_state: ValuationState | None = None,
) -> PricingResult:
    """Price an Airbag as an explicit weighted portfolio of STANDARD legs."""
    if config.method is not PricingMethod.STATIC_REPLICATION:
        raise ValueError("Airbag STANDARD仅支持STATIC_REPLICATION")
    if not instrument.legs:
        raise ValueError("CompositeOption至少需要一条腿")

    # Runtime import avoids an import cycle while retaining the unified registry
    # as the sole router for each explicitly selected leg method.
    from ..api import price

    total_points = 0.0
    warnings: list[str] = []
    leg_diagnostics: list[dict[str, Any]] = []
    leg_greeks: list[tuple[str, float, dict[str, GreekValue]]] = []
    leg_extended_greeks: list[tuple[str, float, dict[str, GreekValue]]] = []

    for leg in instrument.legs:
        if not isinstance(leg.instrument, _SUPPORTED_LEG_TYPES):
            raise ValueError(
                "Airbag STANDARD腿只支持EuropeanVanillaOption、BinaryOption或BarrierOption"
            )
        leg_config = replace(config, method=leg.method)
        leg_result = price(
            leg.instrument,
            market,
            leg_config,
            valuation_state=valuation_state,
        )
        if leg_result.pv_points_100 is None:
            raise ValueError(f"组合腿{leg.label}没有可聚合的pv_points_100")

        total_points += leg.weight * leg_result.pv_points_100
        warnings.extend(leg_result.warnings)
        leg_diagnostics.append(
            {
                "label": leg.label,
                "weight": leg.weight,
                "instrument": type(leg.instrument).__name__,
                "method": leg.method.name,
                "engine": leg_result.diagnostics.get("engine"),
                "version": leg_result.version,
                "pv_points_100": leg_result.pv_points_100,
                "engine_raw": deepcopy(leg_result.engine_raw),
            }
        )
        leg_greeks.append((leg.label, leg.weight, leg_result.greeks))
        leg_extended_greeks.append(
            (leg.label, leg.weight, leg_result.extended_greeks)
        )

    raw = total_points
    converted = convert_points_100(raw, instrument.basis)
    warnings.extend(converted.warnings)

    def aggregate(
        values_by_leg: list[tuple[str, float, dict[str, GreekValue]]],
    ) -> dict[str, GreekValue]:
        aggregated: dict[str, GreekValue] = {}
        greek_names = set().union(*(set(values) for _, _, values in values_by_leg))
        for name in sorted(greek_names):
            present = [
                (label, weight, values.get(name))
                for label, weight, values in values_by_leg
            ]
            metadata = {
                (greek.unit, greek.time_basis)
                for _, _, greek in present
                if greek is not None
            }
            if any(greek is None for _, _, greek in present) or len(metadata) != 1:
                detail = ", ".join(
                    f"{label}={None if greek is None else (greek.unit, greek.time_basis)}"
                    for label, _, greek in present
                )
                warnings.append(
                    f"组合Greek {name}的unit或time_basis口径不兼容，已省略：{detail}"
                )
                continue
            template = present[0][2]
            assert template is not None
            value = sum(
                weight * greek.value
                for _, weight, greek in present
                if greek is not None
            )
            aggregated[name] = make_risk_value(
                value,
                unit=template.unit,
                bump=None,
                difference="composite",
                basis=instrument.basis,
                time_basis=template.time_basis,
                bump_details={
                    "aggregation": "weighted_sum",
                    "components": {
                        label: deepcopy(greek.bump_details)
                        for label, _, greek in present
                        if greek is not None
                    },
                },
            )
        return aggregated

    greeks = aggregate(leg_greeks)
    extended_greeks = aggregate(leg_extended_greeks)

    diagnostics = {
        "engine": "standard.airbag",
        "model": "explicit option-leg static replication",
        "runtime": "STANDARD_ONLY",
        "legs": leg_diagnostics,
    }
    return PricingResult(
        pv_amount=converted.pv_amount,
        pv_percent=converted.pv_percent,
        pv_points_100=converted.pv_points_100,
        currency=converted.currency,
        greeks=greeks,
        method=config.method,
        version=_VERSION,
        extended_greeks=extended_greeks,
        warnings=tuple(dict.fromkeys(warnings)),
        diagnostics=diagnostics,
        engine_raw={
            "price": raw,
            "unit": "POINTS_100",
            "legs": deepcopy(leg_diagnostics),
            "note": "组合腿结果按每100点口径加权",
        },
    )
