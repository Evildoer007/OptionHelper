"""Greeks单位换算与公开披露。数值本身只由Pricer内部数值核计算。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any


def scale_result(result: Any, quantity: float) -> Any:
    """按OptionReg显式数量缩放基座输出，不重新计算PV或Greek。"""
    def scaled(value: Any) -> Any:
        return replace(
            value,
            value=None if value.value is None else value.value * quantity,
            pv_amount_value=None if value.pv_amount_value is None else value.pv_amount_value * quantity,
            pv_percent_value=None if value.pv_percent_value is None else value.pv_percent_value * quantity,
            pv_points_100_value=None if value.pv_points_100_value is None else value.pv_points_100_value * quantity,
        )
    return replace(
        result,
        pv_amount=None if result.pv_amount is None else result.pv_amount * quantity,
        pv_percent=None if result.pv_percent is None else result.pv_percent * quantity,
        pv_points_100=None if result.pv_points_100 is None else result.pv_points_100 * quantity,
        standard_error=None if result.standard_error is None else result.standard_error * quantity,
        standard_error_points_100=(
            None if result.standard_error_points_100 is None
            else result.standard_error_points_100 * quantity
        ),
        standard_error_percent=(
            None if result.standard_error_percent is None
            else result.standard_error_percent * quantity
        ),
        greeks={key: scaled(value) for key, value in result.greeks.items()},
        extended_greeks={key: scaled(value) for key, value in result.extended_greeks.items()},
    )


def real_spot_greeks(result: Any, reference_price: float) -> Any:
    """把100归一化标的坐标的Delta、Gamma还原至真实标的价格坐标。"""
    conversion = 100.0 / reference_price

    def converted(name: str, value: Any) -> Any:
        multiplier = conversion if name == "delta" else conversion * conversion
        return replace(
            value,
            value=value.value * multiplier,
            pv_amount_value=None if value.pv_amount_value is None else value.pv_amount_value * multiplier,
            pv_percent_value=None if value.pv_percent_value is None else value.pv_percent_value * multiplier,
            pv_points_100_value=None if value.pv_points_100_value is None else value.pv_points_100_value * multiplier,
        )

    greeks = dict(result.greeks)
    for name in ("delta", "gamma"):
        greeks[name] = converted(name, greeks[name])
    extended = dict(result.extended_greeks)
    if "vanna" in extended:
        value = extended["vanna"]
        extended["vanna"] = replace(
            value,
            value=None if value.value is None else value.value * conversion,
            pv_amount_value=None if value.pv_amount_value is None else value.pv_amount_value * conversion,
            pv_percent_value=None if value.pv_percent_value is None else value.pv_percent_value * conversion,
            pv_points_100_value=None if value.pv_points_100_value is None else value.pv_points_100_value * conversion,
        )
    return replace(result, greeks=greeks, extended_greeks=extended)


def add_time_zero_cashflow(result: Any, cashflow: float, cashflow_scale: float | None) -> Any:
    """Add an already-fixed contract cashflow without changing sensitivities.

    OptionReg paths include ``cash(0, ...)`` in holder PnL.  Closed-form
    structures therefore add the same contractual premium here, keeping the
    public PV on a single net-cashflow convention while Delta through Rho
    remain model sensitivities of the future contingent leg.
    """
    if cashflow == 0.0:
        return result
    points = cashflow if cashflow_scale is None else cashflow / cashflow_scale * 100.0
    return replace(
        result,
        # OptionReg cash(0, amount) is already denominated in contract
        # currency. Only percentage and points views require cashflow-scale
        # conversion; applying it again to pv_amount changes the cash unit.
        pv_amount=None if result.pv_amount is None else result.pv_amount + cashflow,
        pv_percent=None if result.pv_percent is None else result.pv_percent + points / 100.0,
        pv_points_100=None if result.pv_points_100 is None else result.pv_points_100 + points,
    )


__all__ = ("add_time_zero_cashflow", "real_spot_greeks", "scale_result")
