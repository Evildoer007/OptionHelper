"""Single calendar gate shared by the App Host and the formal Pricer."""

from __future__ import annotations

from typing import Mapping

from .model_router import resolve_route


def requires_future_trading_calendar(
    product_id: object,
    terms: Mapping[str, object] | object,
    model_method: object,
) -> bool:
    """Return whether the selected formal route needs observed sessions.

    A calendar is required only when a Monte Carlo contract has a real
    observation schedule. Terminal-only contracts consume their valuation and
    contractual-maturity endpoints and must not manufacture intermediate
    sessions.
    """

    if not isinstance(terms, Mapping):
        return False
    allowed = terms.get("pricing_methods", ())
    if not isinstance(allowed, (list, tuple, set, frozenset)):
        return False
    route = resolve_route(str(product_id), allowed, str(model_method))
    if route is None:
        return False
    return route.method == "monte_carlo" and bool(terms.get("monitor"))


def requires_future_trading_calendar_for_protocol(
    resolved_contract: object,
    model_method: object,
) -> bool:
    """Read product identity from the frozen protocol, never from ref order."""

    if not isinstance(resolved_contract, Mapping):
        return False
    identity = resolved_contract.get("identity")
    terms = resolved_contract.get("terms")
    product_id = identity.get("product_id") if isinstance(identity, Mapping) else None
    return requires_future_trading_calendar(product_id, terms, model_method)


__all__ = (
    "requires_future_trading_calendar",
    "requires_future_trading_calendar_for_protocol",
)
