"""Shared projections from frozen contract values into public result units.

Calculators and report readers use the same representation; these functions
neither price a contract nor decide which terms a module may solve.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

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

UNIT_PUBLIC_TRANSFORMS = {
    "normalized_point": "points_100_to_decimal_ratio",
    # OptionReg records 5% as 5.  The public fair-term protocol exposes the
    # economically meaningful decimal ratio 0.05 and never calls it points.
    "premium_percent_s0_100": "points_100_to_decimal_ratio",
    "rate": "identity_ratio",
    "volatility": "identity_ratio",
    "price": "relative_to_frozen_reference_price_basis",
}

def contains_private_money_compatibility_text(value: object) -> bool:
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
    if contains_private_money_compatibility_text(value):
        return "private_metadata_redacted"
    return value


def _require_finite_value(value: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("反解公开值必须为有限数")
    return number


def catalog_unit_transform(catalog_unit: str, *, internal_value_encoding: str | None = None) -> str:
    unit = str(catalog_unit).strip()
    if unit not in UNIT_PUBLIC_TRANSFORMS:
        raise ValueError(f"catalog unit {unit!r}没有连续百分比公开转换")
    if internal_value_encoding is not None:
        if internal_value_encoding != "percentage_points_internal" or unit != "volatility":
            raise ValueError("公平参数内部编码与目录单位不一致")
        return "points_100_to_decimal_ratio"
    return UNIT_PUBLIC_TRANSFORMS[unit]


def encode_public_value(
    value: float,
    *,
    catalog_unit: str,
    reference_price_basis: float | None = None,
    internal_value_encoding: str | None = None,
) -> float:
    """Encode a catalog value as the public decimal-ratio representation."""
    number = _require_finite_value(value)
    transform = catalog_unit_transform(catalog_unit, internal_value_encoding=internal_value_encoding)
    if transform == "points_100_to_decimal_ratio":
        return number / 100.0
    if transform == "identity_ratio":
        return number
    reference = _require_finite_value(reference_price_basis) if reference_price_basis is not None else None
    if reference is None or reference <= 0.0:
        raise ValueError("price公开转换必须绑定正的冻结reference_price_basis")
    return number / reference


def encode_contract_parameter(basis: Mapping[str, Any], value: float, contract: Mapping[str, Any]) -> float:
    """Use the target encoding and frozen price basis for a public parameter."""
    unit = basis["catalog_unit"]
    reference = None
    if unit == "price":
        identity = contract["identity"]
        convention = identity.get("price_convention")
        if convention == "normalized_100":
            terms = contract["terms"]
            reference = terms.get("S0")
            if reference is None and terms.get("S0Vec"):
                reference = terms["S0Vec"][0]
        elif convention == "absolute_market":
            assets = identity.get("underlyings", ())
            reference = (identity.get("reference_prices") or {}).get(assets[0]) if assets else None
        else:
            raise ValueError("ResolvedContract.price_convention不是Pricer正式报价口径")
    return encode_public_value(
        value, catalog_unit=unit, reference_price_basis=reference,
        internal_value_encoding=basis.get("value_encoding"),
    )


def decode_public_value(
    value: float,
    *,
    catalog_unit: str,
    reference_price_basis: float | None = None,
    internal_value_encoding: str | None = None,
) -> float:
    """Decode a public decimal-ratio value into the catalog's internal unit."""
    number = _require_finite_value(value)
    transform = catalog_unit_transform(catalog_unit, internal_value_encoding=internal_value_encoding)
    if transform == "points_100_to_decimal_ratio":
        return number * 100.0
    if transform == "identity_ratio":
        return number
    reference = _require_finite_value(reference_price_basis) if reference_price_basis is not None else None
    if reference is None or reference <= 0.0:
        raise ValueError("price公开转换必须绑定正的冻结reference_price_basis")
    return number * reference


