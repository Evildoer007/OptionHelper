"""Run repeated OptionReg risk nodes through one prepared pricing-core session."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

from .engine.derivatives.basis import ResultBasis
from .engine.derivatives.enums import PricingMethod
from .engine.derivatives.instruments import OptionRegPathOption
from .engine.derivatives.models import MarketState, ValuationConfig, ValuationState
from .engine.derivatives.results import PricingResult
from .engine.derivatives.standard.optionreg_path import (
    _PathPricingSession,
    price_optionreg_path_monte_carlo,
)


class PreparedOptionRegPricer:
    """Trusted internal runner; the public Tool still enters through Pricer.price."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[Any, ...], _PathPricingSession] = {}

    def price(self, parameters: Mapping[str, Any]) -> PricingResult:
        contract_values = dict(parameters["contract"])
        market_values = dict(parameters["market"])
        config_values = dict(parameters.get("config", {}))
        state_values = dict(parameters.get("valuation_state", {}))
        basis = ResultBasis(**dict(contract_values["basis"]))
        instrument = OptionRegPathOption(
            basis=basis,
            resolved_contract=contract_values["resolved_contract"],
            asset_spots=tuple(contract_values.get("asset_spots", ())),
            asset_volatilities=tuple(contract_values.get("asset_volatilities", ())),
            asset_dividend_yields=tuple(contract_values.get("asset_dividend_yields", ())),
            correlation=(
                None
                if contract_values.get("correlation") is None
                else tuple(tuple(row) for row in contract_values["correlation"])
            ),
            trading_sessions=tuple(contract_values.get("trading_sessions", ())),
            calendar_id=str(contract_values.get("calendar_id", "")),
            calendar_revision=str(contract_values.get("calendar_revision", "")),
        )
        as_of = market_values["as_of"]
        if isinstance(as_of, str):
            as_of = date.fromisoformat(as_of)
        market = MarketState(
            as_of=as_of,
            spot=float(market_values["spot"]),
            volatility=float(market_values["volatility"]),
            risk_free_rate=float(market_values["risk_free_rate"]),
            dividend_yield=float(market_values.get("dividend_yield", 0.0)),
            carry=market_values.get("carry"),
            source=str(market_values.get("source", "pricing_config")),
        )
        greek_bumps = config_values.get("greek_bumps", {})
        diagnostics = config_values.get("diagnostics", {})
        config = ValuationConfig(
            method=PricingMethod.MONTE_CARLO_CPU,
            paths=int(config_values["paths"]),
            seed=int(config_values["seed"]),
            threads=int(config_values.get("threads", 1)),
            random_source=config_values.get("random_source"),
            greek_bumps=tuple(dict(greek_bumps).items()),
            diagnostics=tuple(dict(diagnostics).items()),
        )
        state = ValuationState(**state_values)
        return price_optionreg_path_monte_carlo(
            instrument,
            market,
            config,
            valuation_state=state,
            prepared_sessions=self._sessions,
        )


__all__ = ("PreparedOptionRegPricer",)
