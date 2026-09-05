"""Stable result envelopes returned by the public API."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import re
from collections.abc import Mapping
from typing import Any

from .enums import PricingMethod


_PRIVATE_MONEY_COMPATIBILITY_FIELDS = frozenset({
    "notional", "cashflow_scale", "cashflow_scale_kind", "pv_amount", "currency", "engine_raw",
    "pv_amount_value", "pv_amount_unit",
})

_PRIVATE_CONTRACT_SCALE_FIELDS = frozenset({"n", "nvar", "nvega"})

_PUBLIC_CONTRACT_IDENTITY_FIELDS = frozenset({
    "product_id", "name_zh", "entry_status", "contract_id", "underlyings",
    "contract_start_date", "contract_end_date", "reference_prices",
    "price_convention", "calendar_id", "calendar_revision", "rule_revision",
})

# ``pv_points_100`` remains the numerical kernel's reconciliation basis.  It
# is not a public presentation unit: public Pricer envelopes must carry the
# already-computed percent projection instead.  Keep this list narrow so the
# ordinary ``points`` arrays used by risk charts remain intact.
_MACHINE_POINT_FIELDS = frozenset({
    "pv_points_100",
    "standard_error_points_100",
    "pv_points_100_value",
    "pv_points_100_unit",
    "canonical_value_basis",
})

_PUBLIC_PERCENT_TEXT_REPLACEMENTS = (
    ("standard_error_points_100", "standard_error_percent"),
    ("variance_points_100", "variance_percent"),
    ("pv_points_100", "pv_percent"),
)

_PUBLIC_METHOD_LABELS = {
    "analytical": "Analytical",
    "monte_carlo": "Monte Carlo",
}


def _percent_unit(unit: object) -> object:
    if not isinstance(unit, str):
        return unit
    projected = unit
    for machine, public in _PUBLIC_PERCENT_TEXT_REPLACEMENTS:
        projected = re.sub(re.escape(machine), public, projected, flags=re.IGNORECASE)
    return projected


def _percent_value_basis(value: object) -> object:
    return _percent_unit(value)


def _public_percent_greek(value: dict[str, Any]) -> dict[str, Any]:
    """Project one structured Greek from machine points into percent units."""
    result = {
        key: project_public_percent(item)
        for key, item in value.items()
        if str(key).casefold() not in _MACHINE_POINT_FIELDS
    }
    result["value"] = value.get("pv_percent_value")
    result["unit"] = _percent_unit(value.get("pv_percent_unit") or value.get("unit"))
    if "pv_percent_value" in value:
        result["pv_percent_value"] = value.get("pv_percent_value")
    if "pv_percent_unit" in value:
        result["pv_percent_unit"] = _percent_unit(value.get("pv_percent_unit"))
    return result


def _public_percent_scalar_greeks(value: dict[str, Any]) -> dict[str, Any]:
    """Give scalar risk-grid Greeks an explicit percent sensitivity unit."""
    units = {
        "delta": "pv_percent_per_spot",
        "gamma": "pv_percent_per_spot_squared",
        "theta": "pv_percent_per_calendar_day",
        "vega": "pv_percent_per_1pct_volatility",
        "rho": "pv_percent_per_1pct_rate",
    }
    projected: dict[str, Any] = {}
    for name, raw_value in value.items():
        key = str(name).casefold()
        if key in units and (raw_value is None or isinstance(raw_value, (int, float))):
            projected[str(name)] = {
                "value": None if raw_value is None else float(raw_value) / 100.0,
                "unit": units[key],
            }
        else:
            projected[str(name)] = project_public_percent(raw_value)
    return projected


def _public_percent_curve(value: dict[str, Any]) -> dict[str, Any]:
    result = project_public_percent(value)
    y_axis = result.get("y_axis")
    if isinstance(y_axis, dict):
        y_axis["unit"] = _percent_unit(y_axis.get("unit"))
    points = result.get("points")
    if isinstance(points, list):
        for point in points:
            if isinstance(point, dict) and isinstance(point.get("y"), (int, float)):
                point["y"] = float(point["y"]) / 100.0
    return result


def _public_percent_surface(value: dict[str, Any]) -> dict[str, Any]:
    result = project_public_percent(value)
    z_axis = result.get("z_axis")
    if isinstance(z_axis, dict):
        z_axis["unit"] = _percent_unit(z_axis.get("unit"))
    data = result.get("data")
    if isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                continue
            raw_value = row.get("value")
            if isinstance(raw_value, list) and len(raw_value) >= 3 and isinstance(raw_value[2], (int, float)):
                raw_value[2] = float(raw_value[2]) / 100.0
    return result


def _public_percent_scenario(value: dict[str, Any]) -> dict[str, Any]:
    result = project_public_percent(value)
    greeks = value.get("greeks")
    if isinstance(greeks, dict):
        scalar = _public_percent_scalar_greeks(greeks)
        if str(value.get("value_basis")) == "variance_points_100":
            for greek in scalar.values():
                if isinstance(greek, dict):
                    greek["unit"] = _percent_unit(
                        str(greek.get("unit", "")).replace("pv_percent", "variance_percent")
                    )
        result["greeks"] = scalar
    return result


def project_public_percent(value: Any) -> Any:
    """Remove machine-point fields and retain only public percent economics.

    This is a presentation projection, never a numerical transformation of the
    kernel result.  Internal ``PricingResult`` instances and acceptance matrix
    evidence keep their 100-point values for reproducible comparison.
    """
    if isinstance(value, Mapping):
        if "pv_percent_value" in value and "pv_points_100_value" in value:
            return _public_percent_greek(value)
        result: dict[str, Any] = {}
        point_pv = value.get("pv_points_100")
        point_standard_error = value.get("standard_error_points_100")
        for key, item in value.items():
            field = str(key).casefold()
            if field in _MACHINE_POINT_FIELDS:
                continue
            if field == "method" and item in _PUBLIC_METHOD_LABELS:
                result[str(key)] = _PUBLIC_METHOD_LABELS[str(item)]
                continue
            if field == "value_basis":
                result[str(key)] = _percent_value_basis(item)
            elif field in {"unit", "pv_percent_unit"}:
                result[str(key)] = _percent_unit(item)
            else:
                result[str(key)] = project_public_percent(item)
        diagnostics = result.get("diagnostics")
        if isinstance(diagnostics, dict):
            # The public diagnostics may retain model provenance but not a
            # duplicate machine-unit label from the numerical engine.
            diagnostics.pop("canonical_value_basis", None)
        if "pv_percent" not in result and isinstance(point_pv, (int, float)):
            result["pv_percent"] = float(point_pv) / 100.0
        if "standard_error_percent" not in result and "standard_error_points_100" in value:
            result["standard_error_percent"] = (
                None
                if point_standard_error is None
                else float(point_standard_error) / 100.0
            )
        return result
    if isinstance(value, list):
        return [project_public_percent(item) for item in value]
    if isinstance(value, tuple):
        return [project_public_percent(item) for item in value]
    if isinstance(value, str):
        return _percent_unit(value)
    return value


def _contains_private_money_compatibility_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value.casefold()
    return any(field in text for field in _PRIVATE_MONEY_COMPATIBILITY_FIELDS) or "cny" in text


def redact_public_money_compatibility(value: Any) -> Any:
    """Remove private money-projection fields from a public Pricer envelope.

    The calculation kernel may retain amount/currency fields for internal
    reconciliation.  They must not leak back through nested diagnostics or
    snapshots after the public result has adopted the 100-point contract
    basis.
    """
    if isinstance(value, Mapping):
        is_contract_identity = {
            "product_id", "underlyings",
        }.issubset({str(key) for key in value})
        return {
            str(key): redact_public_money_compatibility(item)
            for key, item in value.items()
            if str(key).casefold() not in (
                _PRIVATE_MONEY_COMPATIBILITY_FIELDS | _PRIVATE_CONTRACT_SCALE_FIELDS
            )
            and (
                not is_contract_identity
                or str(key) in _PUBLIC_CONTRACT_IDENTITY_FIELDS
            )
        }
    if isinstance(value, list):
        return [redact_public_money_compatibility(item) for item in value]
    if isinstance(value, tuple):
        return [redact_public_money_compatibility(item) for item in value]
    if _contains_private_money_compatibility_text(value):
        return "private_metadata_redacted"
    return value


@dataclass(frozen=True)
class GreekValue:
    """One risk sensitivity reported on the unified PV bases.

    ``value`` is retained as the canonical ``pv_points_100_value`` for the
    public API.  The explicitly named fields prevent callers from guessing
    whether a Greek is expressed in points, percent-of-notional, or money.
    """

    value: float | None
    unit: str | None
    bump: float | None
    difference: str
    time_basis: str | None = None
    bump_details: dict[str, Any] = field(default_factory=dict)
    pv_amount_value: float | None = None
    pv_amount_unit: str | None = None
    pv_percent_value: float | None = None
    pv_percent_unit: str | None = None
    pv_points_100_value: float | None = None
    pv_points_100_unit: str | None = None
    status: str = "available"
    reason: str | None = None


@dataclass(frozen=True)
class PricingResult:
    pv_amount: float | None
    pv_percent: float | None
    pv_points_100: float | None
    currency: str | None
    greeks: dict[str, GreekValue]
    method: PricingMethod
    implementation_id: str
    extended_greeks: dict[str, GreekValue] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)
    engine_raw: dict[str, Any] = field(default_factory=dict)
    # OptionHelper模块协议字段。STANDARD引擎不必了解OptionReg，但可在适配层以
    # dataclasses.replace补齐，避免形成第二个同名结果模型。
    status: str = "priced"
    product_id: str | None = None
    # The public valuation is always a dimensionless price per contractual 100
    # base.  Variance swaps use the same 100 scale but label its squared-
    # volatility-point economics explicitly.
    value_basis: str = "pv_points_100"
    standard_error_points_100: float | None = None
    standard_error_percent: float | None = None
    standard_error: float | None = None
    probabilities: dict[str, Any] = field(default_factory=dict)
    scenario_pv: list[dict[str, Any]] = field(default_factory=list)
    market_snapshot: dict[str, Any] = field(default_factory=dict)
    input_snapshot: dict[str, Any] = field(default_factory=dict)
    resolved_pricing_config: dict[str, Any] = field(default_factory=dict)
    observed_contract_state: dict[str, Any] = field(default_factory=dict)
    risk_curves: list[dict[str, Any]] = field(default_factory=list)
    risk_surfaces: list[dict[str, Any]] = field(default_factory=list)
    risk_scenarios: list[dict[str, Any]] = field(default_factory=list)
    limitations: tuple[str, ...] = ()
    messages: tuple[str, ...] = ()
    # Machine-readable quotation gate.  A demo result is numerically real but
    # cannot be confused with a quote-eligible valuation downstream.
    precision_status: str = "quote_eligible"
    quote_eligible: bool = True
    path_count: int | None = None
    cashflow_lifecycle: dict[str, Any] = field(default_factory=dict)

    @property
    def pv(self) -> float | None:
        """Public shorthand for the contractual 100-point PV."""
        return self.pv_points_100

    def to_dict(self) -> dict[str, Any]:
        """Return the internal machine-result serialization for reconciliation."""
        def normalize(value: Any) -> Any:
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, Mapping):
                return {str(key): normalize(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [normalize(item) for item in value]
            return value
        payload = redact_public_money_compatibility(normalize(asdict(self)))
        payload.pop("standard_error", None)
        for name in ("warnings", "messages", "limitations"):
            values = payload.get(name)
            if isinstance(values, list):
                payload[name] = [
                    value for value in values
                    if not _contains_private_money_compatibility_text(value)
                ]
        return payload

    def to_public_percent_dict(self) -> dict[str, Any]:
        """Return the user-facing valuation, risk and precision projection.

        ``to_dict`` deliberately remains the internal machine serialization so
        Golden and 65-product evidence can compare the frozen 100-point
        numerical basis.  Service, page, CSV and CLI routes must use this
        explicit public projection instead.
        """
        payload = project_public_percent(self.to_dict())
        # Public callers select one of two stable method categories. Engine
        # implementation identifiers and numerical diagnostics remain in the
        # Store-only audit projection assembled by the Pricer service.
        payload.pop("implementation_id", None)
        payload.pop("diagnostics", None)
        payload["risk_curves"] = [
            _public_percent_curve(value)
            for value in self.to_dict().get("risk_curves", [])
            if isinstance(value, dict)
        ]
        payload["risk_surfaces"] = [
            _public_percent_surface(value)
            for value in self.to_dict().get("risk_surfaces", [])
            if isinstance(value, dict)
        ]
        payload["risk_scenarios"] = [
            _public_percent_scenario(value)
            for value in self.to_dict().get("risk_scenarios", [])
            if isinstance(value, dict)
        ]
        payload["scenario_pv"] = [
            _public_percent_scenario(value)
            for value in self.to_dict().get("scenario_pv", [])
            if isinstance(value, dict)
        ]
        return payload


@dataclass(frozen=True)
class SolveResult:
    value: float
    variable: str
    target_pv: float
    converged: bool
    method: PricingMethod
    implementation_id: str
    warnings: tuple[str, ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)
    engine_raw: dict[str, Any] = field(default_factory=dict)
