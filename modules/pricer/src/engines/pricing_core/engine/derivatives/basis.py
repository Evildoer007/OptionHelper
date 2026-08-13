"""Canonical conversion from a contractual 100-point value to PV bases.

``reference_price_basis`` is a market-coordinate conversion only.  It is
deliberately separate from ``cashflow_scale``: the former maps a raw market
spot to the contract's dimensionless ``S=100`` basis, while the latter is the
only quantity allowed to create a private currency compatibility projection.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._validation import require_positive_real
@dataclass(frozen=True)
class ResultBasis:
    reference_price_basis: float | None = None
    cashflow_scale: float | None = None
    cashflow_scale_kind: str | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        if self.reference_price_basis is not None:
            require_positive_real("reference_price_basis", self.reference_price_basis)
        if self.cashflow_scale is not None:
            require_positive_real("cashflow_scale", self.cashflow_scale)
        if self.cashflow_scale_kind is not None and not str(self.cashflow_scale_kind).strip():
            raise ValueError("cashflow_scale_kind必须为非空字符串")
        if self.currency is not None:
            if not isinstance(self.currency, str) or not self.currency.strip():
                raise ValueError("currency必须为非空字符串")


@dataclass(frozen=True)
class ConvertedPV:
    pv_points_100: float | None
    pv_percent: float | None
    pv_amount: float | None
    currency: str | None
    warnings: tuple[str, ...]


def convert_points_100(points_value: float, basis: ResultBasis) -> ConvertedPV:
    """Convert one explicitly named price-per-100 value without unit inference."""
    warnings: list[str] = []
    points = float(points_value)
    percent = points / 100.0
    amount: float | None = None

    valid_cashflow_scale = basis.cashflow_scale is not None and basis.cashflow_scale > 0
    valid_currency = isinstance(basis.currency, str) and bool(basis.currency.strip())
    if valid_cashflow_scale and valid_currency:
        # Variance points are raw payoff divided by Nvar, not a conventional
        # monetary price per contractual 100 base. Its private cash reconciliation is
        # therefore points × Nvar; normal contractual price points retain the
        # usual points / 100 × scale conversion.
        amount = (
            points * float(basis.cashflow_scale)
            if basis.cashflow_scale_kind == "variance_notional"
            else percent * float(basis.cashflow_scale)
        )
    elif basis.cashflow_scale is not None:
        # A missing scale is an intentional and valid outcome for contracts
        # whose cashflows are already expressed in the contractual 100-point
        # basis.  Only a partially declared private compatibility projection
        # needs a diagnostic.
        warnings.append("现金流兼容投影缺少有效币种")
    return ConvertedPV(points, percent, amount, basis.currency if valid_currency else None, tuple(warnings))
