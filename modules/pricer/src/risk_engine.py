"""以同一Pricer估值核生成风险曲线、曲面和Spot/Time情景。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Mapping


PriceOne = Callable[[Any, float], Any]


def vanilla_risk_outputs(*, price_one: PriceOne, market: Any, maturity_years: float, reference_price: float, method: str, spot_factor: str = "单标的现价", actual_spot_coordinates: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """受控小网格的重新估值结果；每个点均调用同一正式模型和固定MC随机源。"""
    spot_shifts = (-0.10, -0.05, 0.0, 0.05, 0.10)
    spot_markets = [replace(market, spot=market.spot * (1.0 + shift)) for shift in spot_shifts]
    time_values = _time_grid(maturity_years)
    # A short contractual tenor can place an intermediate calendar-day risk
    # point between two real exchange sessions.  Keep the base PV intact and
    # expose that point as unavailable instead of asking the path engine to
    # manufacture a weekday observation.
    grid = [[_risk_price(price_one, local_market, time_value) for local_market in spot_markets] for time_value in time_values]
    spot_values = [local_market.spot if actual_spot_coordinates else _raw_spot(local_market.spot, reference_price) for local_market in spot_markets]
    time_days = [round(value * 365.0, 6) for value in time_values]

    curves = [
        _curve("delta_spot", "Delta-Spot", spot_factor, "raw_spot", "Delta", "pv_points_100_per_spot", spot_values, grid[-1], "delta", method),
        _curve("gamma_spot", "Gamma-Spot", spot_factor, "raw_spot", "Gamma", "pv_points_100_per_spot_squared", spot_values, grid[-1], "gamma", method),
        _curve("theta_time", "Theta-Time", "剩余期限", "calendar_days", "Theta", "pv_points_100_per_calendar_day", time_days, [grid[index][2] for index in range(len(time_values))], "theta", method),
    ]
    vol_shift = (-0.10, -0.05, 0.0, 0.05, 0.10)
    vol_markets = [replace(market, volatility=max(1e-8, market.volatility + shift)) for shift in vol_shift]
    vol_results = [_risk_price(price_one, local_market, maturity_years) for local_market in vol_markets]
    curves.append(_curve("vega_volatility", "Vega-Volatility", "波动率", "decimal", "Vega", "pv_points_100_per_1pct_volatility", [item.volatility for item in vol_markets], vol_results, "vega", method))

    surfaces = [_surface(name, label, unit, spot_values, time_days, grid, greek, method, spot_factor) for name, label, unit, greek in (
        ("delta_surface", "Delta曲面", "pv_points_100_per_spot", "delta"),
        ("gamma_surface", "Gamma曲面", "pv_points_100_per_spot_squared", "gamma"),
        ("theta_surface", "Theta曲面", "pv_points_100_per_calendar_day", "theta"),
        ("vega_surface", "Vega曲面", "pv_points_100_per_1pct_volatility", "vega"),
    )]
    scenarios = _spot_time_scenarios(grid, spot_shifts, time_days, spot_values, method, spot_factor)
    return curves, surfaces, scenarios


def scenario_values(*, price_one: PriceOne, market: Any, maturity_years: float, scenarios: list[Mapping[str, Any]], method: str, underlyings: tuple[str, ...]) -> list[dict[str, Any]]:
    """执行用户提交的受控情景；每一点都经由同一个定价回调重估。"""
    rows: list[dict[str, Any]] = []
    for scenario in scenarios:
        if not isinstance(scenario, Mapping) or not isinstance(scenario.get("name"), str):
            raise ValueError("scenarios每项必须含name")
        local = market
        if "spot_shift" in scenario:
            spot_shift = scenario["spot_shift"]
            if not isinstance(spot_shift, Mapping) or set(spot_shift) != set(underlyings):
                raise ValueError("情景spot_shift必须逐一且只覆盖合同标的：" + ",".join(underlyings))
            values = tuple(float(spot_shift[asset]) for asset in underlyings)
            if len(values) > 1 and any(abs(value - values[0]) > 1e-12 for value in values[1:]):
                raise ValueError("当前多标的情景只支持所有标的相同的平行比例变动")
            local = replace(local, spot=local.spot * (1.0 + values[0]))
        if "volatility_shift" in scenario:
            local = replace(local, volatility=max(1e-8, local.volatility + float(scenario["volatility_shift"])))
        if "rate_shift" in scenario:
            local = replace(local, risk_free_rate=local.risk_free_rate + float(scenario["rate_shift"]))
        priced = _risk_price(price_one, local, maturity_years)
        result, reason = priced
        rows.append({
            "name": scenario["name"],
            "pv_percent": None if result is None else result.pv_percent, "pv_points_100": None if result is None else result.pv_points_100,
            "standard_error_points_100": None if result is None else result.standard_error_points_100,
            "standard_error_percent": None if result is None else result.standard_error_percent,
            "value_basis": None if result is None else result.value_basis, "method": method,
            "status": "not_applicable" if reason else "ok", "reason": reason,
            "shifts": {key: value for key, value in scenario.items() if key != "name"},
            "greeks": {name.capitalize(): _greek(priced, name) for name in ("delta", "gamma", "theta", "vega", "rho")},
            "path_pv_sha256_float64": None if result is None else result.diagnostics.get("path_pv_sha256_float64"),
            "random_source": _random_source(priced),
        })
    return rows


def _time_grid(maturity_years: float) -> tuple[float, ...]:
    # Theta本身由基座按约定的一日滚动计算。风险面板只取有经济意义的
    # 剩余期限分位点，不能额外塞入固定“两日”点：周末或节假日前该点
    # 没有第二个真实交易session，会迫使离散路径伪造观察日。
    theta_floor = 2.0 / 365.0
    if maturity_years <= theta_floor:
        return (maturity_years,)
    values = tuple(sorted({maturity_years * 0.50, maturity_years * 0.75, maturity_years}))
    return values


def _raw_spot(normalized_spot: float, reference_price: float) -> float:
    return normalized_spot * reference_price / 100.0


def _risk_price(price_one: PriceOne, market: Any, maturity_years: float) -> tuple[Any | None, str | None]:
    try:
        return price_one(market, maturity_years), None
    except ValueError as error:
        message = str(error)
        if "OptionReg路径MC注入交易sessions未覆盖完整剩余合同期限" not in message:
            raise
        return None, "风险点剩余期限内不足两个真实交易session，标记not_applicable；未补造weekday。"


def _greek(priced: tuple[Any | None, str | None], name: str) -> float | None:
    result, _reason = priced
    if result is None:
        return None
    value = result.greeks.get(name)
    return value.value if value is not None else None


def _random_source(priced: tuple[Any | None, str | None]) -> dict[str, Any] | None:
    result, _reason = priced
    if result is None:
        return None
    source = result.diagnostics.get("random_source")
    return None if source is None else dict(source)


def _curve(key: str, name: str, x_name: str, x_unit: str, y_name: str, y_unit: str, xs: list[float], priced: list[tuple[Any | None, str | None]], greek: str, method: str) -> dict[str, Any]:
    first_value = next(
        (result.greeks[greek] for result, _reason in priced if result is not None and greek in result.greeks),
        None,
    )
    return {
        "key": key, "name": name,
        "x_axis": {"name": x_name, "unit": x_unit},
        "y_axis": {"name": y_name, "unit": y_unit if first_value is None else first_value.unit},
        "method": method,
        "points": [
            {
                "x": x,
                "y": _greek(value, greek),
                "status": "not_applicable" if value[1] else "ok",
                "reason": value[1],
                "random_source": _random_source(value),
            }
            for x, value in zip(xs, priced, strict=True)
        ],
    }


def _surface(key: str, name: str, z_unit: str, spots: list[float], times: list[float], grid: list[list[Any]], greek: str, method: str, spot_factor: str) -> dict[str, Any]:
    data = []
    for time_index, row in enumerate(grid):
        for spot_index, priced in enumerate(row):
            result, reason = priced
            data.append({
                "value": [spot_index, time_index, _greek(priced, greek)],
                "standard_error_points_100": None if result is None else result.standard_error_points_100,
                "path_pv_sha256_float64": None if result is None else result.diagnostics.get("path_pv_sha256_float64"),
                "status": "not_applicable" if reason else "ok",
                "reason": reason,
                "random_source": _random_source(priced),
            })
    first_value = next(
        (result.greeks[greek] for row in grid for result, _reason in row if result is not None and greek in result.greeks),
        None,
    )
    return {
        "key": key, "name": name, "method": method,
        "x_axis": {"name": spot_factor, "unit": "raw_spot", "values": spots},
        "y_axis": {"name": "剩余期限", "unit": "calendar_days", "values": times},
        "z_axis": {"name": name.removesuffix("曲面"), "unit": z_unit if first_value is None else first_value.unit},
        "data": data,
    }


def _spot_time_scenarios(grid: list[list[Any]], shifts: tuple[float, ...], time_days: list[float], spots: list[float], method: str, spot_factor: str) -> list[dict[str, Any]]:
    time_indices = tuple(sorted({0, len(time_days) // 2, len(time_days) - 1}))
    rows = []
    for time_index in time_indices:
        for spot_index in (0, 2, 4):
            result, reason = grid[time_index][spot_index]
            rows.append({
                "spot_shift": shifts[spot_index], "spot": spots[spot_index], "spot_factor": spot_factor, "remaining_days": time_days[time_index],
                "pv_points_100": None if result is None else result.pv_points_100,
                "pv_percent": None if result is None else result.pv_percent,
                "standard_error_points_100": None if result is None else result.standard_error_points_100,
                "standard_error_percent": None if result is None else result.standard_error_percent,
                "value_basis": None if result is None else result.value_basis, "method": method,
                "status": "not_applicable" if reason else "ok", "reason": reason,
                "greeks": {name.capitalize(): _greek((result, reason), name) for name in ("delta", "gamma", "theta", "vega", "rho")},
                "path_pv_sha256_float64": None if result is None else result.diagnostics.get("path_pv_sha256_float64"),
                "random_source": _random_source((result, reason)),
            })
    return rows


__all__ = ("scenario_values", "vanilla_risk_outputs")
