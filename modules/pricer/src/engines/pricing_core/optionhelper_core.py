"""OptionHelper合同到Pricer内部数值核的唯一数值适配层。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any, Mapping

from runtime.contracts.contract_api import ResolvedContract

from ...diagnostics import pricing_evidence, vanilla_market_snapshot
from ...model_router import PRODUCT_CAPABILITIES, ProductCapability, capability_for
from ...observed_state import ObservedContractState, ObservedStateError
from ...mc_run_assessment import assess_monte_carlo_run
from ...product_pricing_adapter import ProductNotAvailable, ProductPricingAdapter
from ...risk_engine import scenario_values, vanilla_risk_outputs
from .engine.derivatives.models import MarketState
from .engine.derivatives.results import GreekValue, PricingResult


class PricingInputError(ValueError):
    """正式Pricer输入无法形成可验证估值上下文。"""


def price(contract: ResolvedContract, pricing_config: Any, *, market_snapshot: Mapping[str, Any] | None = None, observed_contract_state: ObservedContractState | Mapping[str, Any] | None = None) -> PricingResult:
    """唯一合同适配入口。数值PV和Greek只来自standard.vanilla。"""
    if not isinstance(contract, ResolvedContract):
        raise PricingInputError("Pricer只接受ResolvedContract")
    config = _validated_config(pricing_config)
    try:
        state = ObservedContractState.from_value(observed_contract_state, valuation_date=config.valuation_date)
    except ObservedStateError as error:
        raise PricingInputError(str(error)) from error
    if contract.product_id in {"2.1", "2.2"}:
        try:
            state.validate_european_vanilla()
        except ObservedStateError as error:
            raise PricingInputError(str(error)) from error
    try:
        _require_midlife_path_state(contract, config, observed_contract_state)
        adapter = ProductPricingAdapter(contract, config, market_snapshot, state)
    except (ObservedStateError, ProductNotAvailable, ValueError) as error:
        allowed = tuple(str(value) for value in contract.terms.get("pricing_methods", ()))
        return _unsupported(contract, config, state, allowed, str(error))
    return _price_with_risk(adapter, contract, config, _market_state(contract, config, market_snapshot), state, market_snapshot or {})


def _validated_config(value: Any) -> Any:
    required = ("model_method", "valuation_date", "spot", "historical_volatility", "volatility_override", "risk_free_rate", "dividend_yield", "time_to_maturity", "path_count", "demo_mode", "random_seed", "greek_bumps", "scenarios", "hv_window")
    missing = [name for name in required if not hasattr(value, name)]
    if missing:
        raise PricingInputError("PricingConfig缺少字段：" + ",".join(missing))
    return value


def _unsupported(contract: ResolvedContract, config: Any, state: ObservedContractState, allowed: tuple[str, ...], reason: str) -> PricingResult:
    return PricingResult(
        pv_amount=None, pv_percent=None, pv_points_100=None, currency=contract.currency,
        method="unsupported", implementation_id="optionhelper.unsupported", status="unsupported", product_id=contract.product_id,
        contract_fingerprint=contract.contract_fingerprint,
        greeks={name: GreekValue(value=None, unit=None, bump=None, difference="not_applicable", status="not_applicable", reason=reason) for name in ("delta", "gamma", "vega", "theta", "rho")},
        input_snapshot={"contract": contract.to_protocol_dict(), "pricing_config": _config_dict(config), "optionreg_allowed_methods": list(allowed)},
        resolved_pricing_config=_config_dict(config), observed_contract_state=state.to_dict(), limitations=(reason,), messages=(reason,),
        precision_status="not_priced", quote_eligible=False,
        path_count=int(config.path_count) if getattr(config, "model_method", "") == "monte_carlo" and config.path_count is not None else None,
    )


def _market_state(contract: ResolvedContract, config: Any, snapshot: Mapping[str, Any] | None) -> MarketState:
    asset = contract.underlyings[0]
    spot = _asset_value(config.spot, asset, "spot")
    references = contract.identity.get("reference_prices")
    if not references or asset not in references:
        raise PricingInputError("定价必须提供合同起始参考价reference_prices")
    volatility = _asset_value(config.volatility_override if config.volatility_override is not None else config.historical_volatility, asset, "volatility")
    tau = float(config.time_to_maturity if config.time_to_maturity is not None else contract.terms["T"])
    if not 0.0 < tau <= float(contract.terms["T"]) + 1e-12:
        raise PricingInputError("time_to_maturity必须在(0,T]内")
    try:
        as_of = date.fromisoformat(str(config.valuation_date)) if config.valuation_date else date.today()
    except ValueError as error:
        raise PricingInputError("valuation_date必须为YYYY-MM-DD") from error
    return MarketState(as_of=as_of, spot=spot, volatility=volatility, risk_free_rate=float(config.risk_free_rate), dividend_yield=_asset_value(config.dividend_yield, asset, "dividend_yield"), source=str((snapshot or {}).get("source_ref") or (snapshot or {}).get("source") or "pricing_config"))


def _asset_value(value: Any, asset: str, label: str) -> float:
    if isinstance(value, Mapping):
        if asset not in value:
            raise PricingInputError(f"{label}必须逐一覆盖合同标的")
        value = value[asset]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PricingInputError(f"PricingConfig缺少{label}")
    number = float(value)
    if label in {"spot", "volatility"} and number <= 0.0:
        raise PricingInputError(f"{label}必须为正数")
    return number


def _price_with_risk(adapter: ProductPricingAdapter, contract: ResolvedContract, config: Any, market: MarketState, state: ObservedContractState, supplied_snapshot: Mapping[str, Any]) -> PricingResult:
    asset = contract.underlyings[0]
    references = contract.identity.get("reference_prices")
    reference_price = float(references[asset])
    def price_one(local_market: MarketState, maturity: float) -> PricingResult:
        return adapter.reprice(
            spot=local_market.spot,
            volatility=local_market.volatility,
            risk_free_rate=local_market.risk_free_rate,
            maturity_years=maturity,
        )

    maturity_years = float(config.time_to_maturity or contract.terms["T"])
    result = price_one(market, maturity_years)
    spot_factor = "单标的现价" if len(contract.underlyings) == 1 else "多标的平行比例变动（首标的现价）"
    # ``MarketState.spot`` is always the caller's raw market price.  Vanilla
    # adapters normalize it only inside ``price_one``; converting the risk
    # panel coordinate a second time by ``reference_price / 100`` would turn
    # a 7,443 index level into an impossible 554,047 display value.
    curves, surfaces, risk_scenarios = vanilla_risk_outputs(
        price_one=price_one,
        market=market,
        maturity_years=maturity_years,
        reference_price=reference_price,
        method=adapter.method,
        spot_factor=spot_factor,
        actual_spot_coordinates=True,
    )
    try:
        submitted_scenarios = scenario_values(
            price_one=price_one,
            market=market,
            maturity_years=maturity_years,
            scenarios=config.scenarios,
            method=adapter.method,
            underlyings=contract.underlyings,
        )
    except ValueError as error:
        raise PricingInputError(str(error)) from error
    historical_volatility = _snapshot_asset_value(supplied_snapshot.get("historical_volatility"), asset, market.volatility)
    volatility_source = "volatility_override" if config.volatility_override is not None else "historical_volatility"
    snapshot = vanilla_market_snapshot(asset=asset, spot=_asset_value(config.spot, asset, "spot"), normalized_spot=market.spot / reference_price * float(contract.terms.get("S0", 100.0)), volatility=market.volatility, historical_volatility=historical_volatility, volatility_source=volatility_source, risk_free_rate=market.risk_free_rate, dividend_yield=market.dividend_yield, valuation_date=market.as_of.isoformat(), time_to_maturity=maturity_years, hv_window=int(config.hv_window), source=market.source, random_source=result.diagnostics.get("random_source"))
    if len(contract.underlyings) > 1:
        snapshot["asset_spots"] = {asset_id: _asset_value(config.spot, asset_id, "spot") for asset_id in contract.underlyings}
        snapshot["asset_volatilities"] = {asset_id: adapter._asset_volatility(asset_id) for asset_id in contract.underlyings}
        snapshot["asset_dividend_yields"] = {asset_id: _asset_value(config.dividend_yield, asset_id, "dividend_yield") for asset_id in contract.underlyings}
        snapshot["correlation"] = config.correlation
    probabilities = {"in_the_money": result.diagnostics.get("black_scholes_analytics", {}).get("model_in_the_money_probability", result.diagnostics.get("model_in_the_money_probability")), "knock_in": {"status": "not_applicable", "reason": "未由通用路径概率解释器单列"}, "knock_out": {"status": "not_applicable", "reason": "未由通用路径概率解释器单列"}}
    precision = assess_monte_carlo_run(
        result,
        method=adapter.method,
        path_count=int(config.path_count) if adapter.method == "monte_carlo" else 0,
        demo_mode=bool(config.demo_mode),
    )
    messages = tuple(result.warnings)
    if precision.message:
        messages += (precision.message,)
    return replace(
        result,
        status="priced",
        product_id=contract.product_id,
        contract_fingerprint=contract.contract_fingerprint,
        method=adapter.method,
        probabilities=probabilities,
        scenario_pv=submitted_scenarios,
        risk_curves=curves,
        risk_surfaces=surfaces,
        risk_scenarios=risk_scenarios,
        market_snapshot=snapshot,
        input_snapshot=pricing_evidence(result, numerical_core="Pricer.unified.price", config=config, contract=contract),
        resolved_pricing_config=_config_dict(config),
        observed_contract_state=state.to_dict(),
        diagnostics={**result.diagnostics, "monte_carlo_statistics": precision.diagnostics},
        messages=messages,
        precision_status=precision.status,
        quote_eligible=precision.eligible,
        path_count=int(config.path_count) if adapter.method == "monte_carlo" else None,
    )


def _require_midlife_path_state(
    contract: ResolvedContract,
    config: Any,
    supplied_state: ObservedContractState | Mapping[str, Any] | None,
) -> None:
    """存续路径合约不能把缺失历史状态默认为初始状态。"""
    monitor = contract.terms.get("monitor")
    if not isinstance(monitor, Mapping) or not monitor or supplied_state is not None:
        return
    start_value = contract.identity.get("contract_start_date")
    valuation_value = config.valuation_date
    if start_value is None or valuation_value is None:
        return
    try:
        start = date.fromisoformat(str(start_value))
        valuation = date.fromisoformat(str(valuation_value))
    except ValueError as error:
        raise ProductNotAvailable("contract_start_date和valuation_date必须为YYYY-MM-DD") from error
    if valuation > start:
        raise ProductNotAvailable(
            "存续期路径结构必须提供observed_contract_state，不得把缺失的已发生状态默认为initial"
        )


def _snapshot_asset_value(value: Any, asset: str, fallback: float) -> float:
    if isinstance(value, Mapping) and asset in value:
        return float(value[asset])
    return float(value) if isinstance(value, (int, float)) else fallback


def _config_dict(config: Any) -> dict[str, Any]:
    return config.to_dict() if hasattr(config, "to_dict") else dict(config)


__all__ = ("PricingInputError", "PricingResult", "ProductCapability", "PRODUCT_CAPABILITIES", "capability_for", "price")
