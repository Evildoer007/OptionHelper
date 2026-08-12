"""正式Pricer的市场快照与数值证据整理。"""

from __future__ import annotations

from typing import Any, Mapping


def vanilla_market_snapshot(*, asset: str, spot: float, normalized_spot: float, volatility: float, historical_volatility: float, volatility_source: str, risk_free_rate: float, dividend_yield: float, valuation_date: str, time_to_maturity: float, hv_window: int, source: str, random_source: Mapping[str, Any] | None = None) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "valuation_date": valuation_date,
        "spot": {asset: spot},
        "normalized_spot": {asset: normalized_spot},
        "volatility": {asset: volatility},
        "historical_volatility": {asset: historical_volatility},
        "volatility_source": volatility_source,
        "risk_free_rate": risk_free_rate,
        "dividend_yield": {asset: dividend_yield},
        "time_to_maturity": time_to_maturity,
        "hv_window": int(hv_window),
        "source": source,
    }
    if random_source is not None:
        snapshot["random_source"] = dict(random_source)
    return snapshot


def pricing_evidence(result: Any, *, numerical_core: str, config: Any, contract: Any) -> dict[str, Any]:
    output = {
        "contract": contract.to_protocol_dict(),
        "pricing_config": config.to_dict(),
        "numerical_core": numerical_core,
    }
    path_hash = result.diagnostics.get("path_pv_sha256_float64")
    if path_hash is not None:
        output["path_pv_sha256_float64"] = path_hash
    return output


__all__ = ("pricing_evidence", "vanilla_market_snapshot")
