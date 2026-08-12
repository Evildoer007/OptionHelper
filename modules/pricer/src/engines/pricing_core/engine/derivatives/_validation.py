"""Small validation helpers shared by immutable public domain objects."""

from __future__ import annotations

import math
from numbers import Real
from typing import Any


def require_plain_int(
    label: str,
    value: Any,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> None:
    if type(value) is not int:
        raise ValueError(f"{label}必须为整数")
    if positive and value <= 0:
        raise ValueError(f"{label}必须为正整数")
    if nonnegative and value < 0:
        raise ValueError(f"{label}必须为非负整数")


def require_finite_real(label: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label}必须为有限实数")
    if not math.isfinite(float(value)):
        raise ValueError(f"{label}必须为有限实数")


def require_positive_real(label: str, value: Any) -> None:
    require_finite_real(label, value)
    if value <= 0:
        raise ValueError(f"{label}必须为正有限数")


def require_nonnegative_real(label: str, value: Any) -> None:
    require_finite_real(label, value)
    if value < 0:
        raise ValueError(f"{label}必须为非负有限数")
