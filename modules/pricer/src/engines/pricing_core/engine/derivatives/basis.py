"""Canonical conversion from a price per 100 notional to all public PV bases."""

from __future__ import annotations

from dataclasses import dataclass

from ._validation import require_positive_real
@dataclass(frozen=True)
class ResultBasis:
    notional: float | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        if self.notional is not None:
            require_positive_real("notional", self.notional)
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

    valid_notional = basis.notional is not None and basis.notional > 0
    valid_currency = isinstance(basis.currency, str) and bool(basis.currency.strip())
    if valid_notional and valid_currency:
        amount = percent * float(basis.notional)
    else:
        warnings.append("pv_amount要求有效的notional和currency同时存在")
    return ConvertedPV(points, percent, amount, basis.currency if valid_currency else None, tuple(warnings))
