"""Execute the deterministic 65-product Pricer acceptance matrix."""

from __future__ import annotations

import json
from dataclasses import replace
from math import isfinite
from typing import Any, Iterable
from unittest.mock import patch

from runtime.contracts.contract_api import load_registry, resolve_contract

from modules.pricer import HistoricalData, PricingInput, price
from modules.pricer.calendar_policy import requires_future_trading_calendar
from modules.pricer.model_router import resolve_route

from .pricer_test_fixtures import calendar_asset, demo_config, market_asset


REQUIRED_CONFIG_FIELDS = (
    "valuation_date", "spot", "historical_volatility", "risk_free_rate",
    "dividend_yield", "model_method", "path_count", "demo_mode", "random_seed",
)


def build_acceptance_rows(product_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Price every executable product and return only auditable output facts."""
    registry = load_registry()
    selected = None if product_ids is None else {str(product_id) for product_id in product_ids}
    rows: list[dict[str, Any]] = []
    for product_id, product in registry["products"].items():
        if product["identity"]["entry_status"] is not True:
            continue
        if selected is not None and str(product_id) not in selected:
            continue
        # The matrix is an MC10 logic probe for every MC-capable route.  The
        # closed-form tests stay elsewhere; they must not downgrade this
        # acceptance evidence into a Black-Scholes-only route check.
        method = "monte_carlo"
        terms = product["terms"]
        assets = ("A", "B") if "S0Vec" in terms else ("A",)
        contract, config, pricing_input = _input_for(str(product_id), terms, assets, method)
        route = resolve_route(str(product_id), terms["pricing_methods"], method)
        if route is None:
            raise AssertionError(f"{product_id}没有{method}路由")
        requires_calendar = requires_future_trading_calendar(product_id, terms, method)
        missing_calendar = _missing_calendar_category(
            contract, config, pricing_input, requires_calendar,
        )
        # Keep the full risk report on the ordinary result.  The lagged
        # probe exercises the same public pricing entry, market resolver,
        # trading-calendar validation, and MC10 base valuation; it suppresses
        # only its duplicate display-risk grid.  The grid is unrelated to the
        # requested/effective/as-of date assertion and otherwise makes a
        # single row exceed the bounded test runner deadline.
        lagged_input = _lagged_market_input(contract, config, pricing_input, requires_calendar)
        result = price(pricing_input)
        lagged = _market_date_probe(lagged_input)
        _validate_priced(result, product_id)
        _validate_lagged(lagged, contract.contract_fingerprint, product_id)
        rows.append({
            "product_id": str(product_id),
            "canonical_name": product["identity"]["name_zh"],
            "route_adapter": route.capability.adapter,
            "method": method,
            "probe_scope": "demo/logic-only",
            "calendar_requirement": "trading-calendar" if requires_calendar else "not_required",
            "resolved_contract": {
                "bound": True,
                "product_id_matches": result.input_snapshot["contract"]["identity"]["product_id"] == str(product_id),
                "underlyings": list(contract.underlyings),
                "contract_fingerprint": contract.contract_fingerprint,
            },
            "input_fields_summary": {
                "identity_fields": ["underlyings", "reference_prices"],
                "pricing_config_fields": list(REQUIRED_CONFIG_FIELDS),
                "market_data_ref": {
                    "schema_id": pricing_input.market_data_refs[0].schema_id,
                    "normalized_fields": list(pricing_input.market_data_refs[0].normalized_fields),
                },
                "trading_calendar_ref_bound": pricing_input.trading_calendar_ref is not None,
                "path_count": config.path_count if method == "monte_carlo" else None,
                "demo_mode": config.demo_mode,
                "random_seed": config.random_seed,
            },
            "valuation_dates": _dates(result),
            "market_date_fallback": {
                **_dates(lagged),
                "status": lagged.status,
                "method": method,
                "path_count": lagged.path_count,
                "precision_status": lagged.precision_status,
                "quote_eligible": lagged.quote_eligible,
            },
            "fallback_probe_scope": "date-routing-only",
            "status": result.status,
            "pv_amount": float(result.pv_amount),
            "greeks_shape": {
                "names": sorted(result.greeks),
                "count": len(result.greeks),
                "statuses": {name: value.status for name, value in sorted(result.greeks.items())},
                "finite_values": all(
                    value.value is not None and isfinite(float(value.value))
                    for value in result.greeks.values()
                ),
            },
            "risk_output_shape": {
                "curve_count": len(result.risk_curves),
                "curve_point_counts": [len(curve["points"]) for curve in result.risk_curves],
                "surface_count": len(result.risk_surfaces),
                "scenario_count": len(result.scenario_pv),
            },
            "limitations_or_error": {
                "limitations": list(result.limitations),
                "messages": list(result.messages),
                "missing_calendar": missing_calendar,
            },
            "quote_eligible": result.quote_eligible,
            "precision_status": result.precision_status,
            "path_count": result.path_count,
        })
    if selected is None and len(rows) != 65:
        raise AssertionError(f"entry_status=True产品数不是65，而是{len(rows)}")
    return rows


def csv_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Flatten the full audited row set without losing nested output shapes."""
    flattened: list[dict[str, str]] = []
    for row in rows:
        flattened.append({
            "product_id": str(row["product_id"]),
            "canonical_name": str(row["canonical_name"]),
            "route_adapter": str(row["route_adapter"]),
            "method": str(row["method"]),
            "probe_scope": str(row["probe_scope"]),
            "calendar_requirement": str(row["calendar_requirement"]),
            "resolved_contract_bound": str(row["resolved_contract"]["bound"]).lower(),
            "contract_fingerprint": str(row["resolved_contract"]["contract_fingerprint"]),
            "input_fields_summary": _compact(row["input_fields_summary"]),
            "requested_valuation_date": str(row["valuation_dates"]["requested_valuation_date"]),
            "effective_valuation_session": str(row["valuation_dates"]["effective_valuation_session"]),
            "market_as_of_date": str(row["valuation_dates"]["market_as_of_date"]),
            "fallback_dates": _compact(row["market_date_fallback"]),
            "fallback_probe_scope": str(row["fallback_probe_scope"]),
            "status": str(row["status"]),
            "pv_amount": repr(row["pv_amount"]),
            "greeks_shape": _compact(row["greeks_shape"]),
            "risk_output_shape": _compact(row["risk_output_shape"]),
            "limitations_or_error": _compact(row["limitations_or_error"]),
            "quote_eligible": str(row["quote_eligible"]).lower(),
            "precision_status": str(row["precision_status"]),
            "path_count": "" if row["path_count"] is None else str(row["path_count"]),
        })
    return flattened


def csv_fieldnames() -> tuple[str, ...]:
    return (
        "product_id", "canonical_name", "route_adapter", "method", "probe_scope", "calendar_requirement",
        "resolved_contract_bound", "contract_fingerprint", "input_fields_summary",
        "requested_valuation_date", "effective_valuation_session", "market_as_of_date",
        "fallback_dates", "fallback_probe_scope", "status", "pv_amount", "greeks_shape", "risk_output_shape",
        "limitations_or_error", "quote_eligible", "precision_status", "path_count",
    )


def _input_for(product_id: str, terms: dict[str, Any], assets: tuple[str, ...], method: str):
    contract = resolve_contract(
        product_id,
        identity={"underlyings": list(assets), "reference_prices": {asset: 100.0 for asset in assets}},
    )
    config = replace(demo_config(assets), model_method=method, demo_mode=method == "monte_carlo")
    history, history_ref = market_asset(assets)
    required = requires_future_trading_calendar(product_id, terms, method)
    if required:
        calendar, calendar_ref = calendar_asset(assets)
        return contract, config, PricingInput(contract, config, history, (history_ref,), calendar, calendar_ref)
    return contract, config, PricingInput(contract, config, history, (history_ref,))


def _lagged_market_input(contract, config, pricing_input, requires_calendar: bool) -> PricingInput:
    cutoff = "2023-07-27"
    rows = tuple(item for item in pricing_input.historical_data.rows if str(item["date"]) <= cutoff)
    assets = tuple(contract.underlyings)
    sessions = tuple(sorted({str(item["date"]) for item in rows}))
    coverage = {
        "start_date": sessions[0], "end_date": sessions[-1], "sessions": sessions,
        "calendar_id": "CN-SSE", "calendar_version": "lagged-market-fixture",
        "by_asset": {asset: {"start": sessions[0], "end": sessions[-1]} for asset in assets},
    }
    history = HistoricalData("memory://pricer-test-market-lagged", rows, asset_ids=assets, coverage=coverage)
    history_ref = replace(
        pricing_input.market_data_refs[0], storage_ref=history.source_ref,
        coverage=coverage, row_count=len(rows), content_hash=history.content_hash,
    )
    if requires_calendar:
        calendar, calendar_ref = calendar_asset(assets, start_date=cutoff)
        return PricingInput(contract, config, history, (history_ref,), calendar, calendar_ref)
    return PricingInput(contract, config, history, (history_ref,))


def _missing_calendar_category(contract, config, pricing_input, required: bool) -> dict[str, str]:
    if not required:
        return {"category": "not_required", "exception": "", "message": ""}
    try:
        price(PricingInput(contract, config, pricing_input.historical_data, pricing_input.market_data_refs))
    except ValueError as error:
        if "trading-calendar" not in str(error):
            raise AssertionError(f"缺日历错误分类漂移：{error}") from error
        return {"category": "missing_trading_calendar", "exception": type(error).__name__, "message": str(error)}
    raise AssertionError("真实monitor结构缺少trading-calendar不应定价")


def _dates(result) -> dict[str, str]:
    snapshot = result.market_snapshot
    return {
        "requested_valuation_date": str(snapshot["requested_valuation_date"]),
        "effective_valuation_session": str(snapshot["effective_valuation_session"]),
        "market_as_of_date": str(snapshot["market_as_of_date"]),
    }


def _validate_priced(result, product_id: object) -> None:
    if result.status != "priced" or result.product_id != str(product_id) or result.pv_amount is None:
        raise AssertionError(f"{product_id}未得到priced有限PV")
    if not isfinite(float(result.pv_amount)):
        raise AssertionError(f"{product_id}PV非有限")
    if set(result.greeks) != {"delta", "gamma", "vega", "theta", "rho"}:
        raise AssertionError(f"{product_id}Greek形状不完整")
    if len(result.risk_curves) != 4 or any(not curve["points"] for curve in result.risk_curves):
        raise AssertionError(f"{product_id}风险输出形状不完整")


def _validate_lagged(result, fingerprint: str, product_id: object) -> None:
    if result.status != "priced" or result.product_id != str(product_id) or result.pv_amount is None:
        raise AssertionError(f"{product_id}行情滞后MC基准估值未得到priced有限PV")
    if not isfinite(float(result.pv_amount)):
        raise AssertionError(f"{product_id}行情滞后MC基准PV非有限")
    dates = _dates(result)
    expected = {
        "requested_valuation_date": "2023-07-28",
        "effective_valuation_session": "2023-07-27",
        "market_as_of_date": "2023-07-27",
    }
    if dates != expected or result.contract_fingerprint != fingerprint:
        raise AssertionError(f"{product_id}行情滞后回退或合同身份漂移：{dates}")


def _market_date_probe(pricing_input: PricingInput):
    """Run the official market/MC entry while omitting only duplicate risk charts."""
    # The matrix stores the complete risk report from the ordinary MC10 run.
    # A second copy is not evidence about market-date fallback and has no
    # effect on its three dates, identity, PV, Greeks, or quote gate.
    with patch(
        "modules.pricer.engines.pricing_core.optionhelper_core.vanilla_risk_outputs",
        return_value=([], [], []),
    ):
        return price(pricing_input)


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ("REQUIRED_CONFIG_FIELDS", "build_acceptance_rows", "csv_fieldnames", "csv_rows")
