"""以同一Pricer估值核生成风险曲线、曲面和Spot/Time情景。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from math import isfinite
from typing import Any, Callable, Mapping


PriceOne = Callable[..., Any]

_SURFACE_GREEKS = frozenset({"delta", "gamma", "theta", "vega", "rho"})
_SCENARIO_GREEKS = _SURFACE_GREEKS
_SPOT_CURVE_GREEKS = frozenset({"delta", "gamma"})
_VEGA_ONLY = frozenset({"vega"})
_RHO_ONLY = frozenset({"rho"})


@dataclass(frozen=True)
class RiskGridSpec:
    """由ResolvedContract、市场与真实日历共同确定的风险重估坐标。"""

    spot_curve_normalized: tuple[float, ...]
    spot_surface_normalized: tuple[float, ...]
    time_years: tuple[float, ...]
    volatilities: tuple[float, ...]
    risk_free_rates: tuple[float, ...]
    annotations: tuple[dict[str, Any], ...]
    coordinate_mode: str
    spot_axis_name: str
    spot_axis_unit: str
    mode: str


def build_risk_grid_spec(
    *,
    contract: Any,
    market: Any,
    maturity_years: float,
    reference_price: float,
    term_catalog: Mapping[str, Mapping[str, Any]],
    config: Any,
    path_dependent: bool,
    trading_sessions: tuple[str, ...] | list[str] = (),
) -> RiskGridSpec:
    """生成唯一条款感知网格；不读取公式文本，也不推断工作日。"""
    normalized_base = float(contract.terms.get("S0", 100.0))
    current_normalized = market.spot / reference_price * normalized_base
    annotations, scalar_levels, schedule_levels = _contract_price_annotations(
        contract,
        term_catalog,
        current_normalized,
    )
    landmarks = {normalized_base, current_normalized, *scalar_levels}
    if schedule_levels:
        ordered_schedule = sorted(schedule_levels)
        landmarks.update((
            ordered_schedule[0],
            ordered_schedule[len(ordered_schedule) // 2],
            ordered_schedule[-1],
        ))

    if config.mode == "custom":
        lower = float(config.spot_min_normalized)
        upper = float(config.spot_max_normalized)
        outside = sorted(value for value in landmarks if value < lower or value > upper)
        if outside:
            raise ValueError("自定义风险范围未覆盖合同关键价格：" + ",".join(f"{value:g}" for value in outside))
    else:
        lower = min(25.0, min(landmarks) - 15.0)
        upper = max(200.0, max(landmarks) + 15.0)
    lower = max(1e-6, lower)

    curve_spots = _anchored_grid(lower, upper, int(config.spot_curve_points), landmarks)
    surface_spots = _anchored_grid(lower, upper, int(config.spot_surface_points), landmarks)
    time_values = _time_grid(
        contract=contract,
        market_date=market.as_of,
        maturity_years=maturity_years,
        points=int(config.time_points),
        path_dependent=path_dependent,
        trading_sessions=trading_sessions,
    )
    volatility_levels = _volatility_landmarks(contract, term_catalog)
    volatility_lower = float(config.volatility_min) if config.volatility_min is not None else max(
        0.01,
        min(market.volatility * 0.50, market.volatility - 0.10),
    )
    volatility_upper = float(config.volatility_max) if config.volatility_max is not None else max(
        market.volatility * 1.50,
        market.volatility + 0.10,
    )
    volatility_anchors = {
        value
        for value in (market.volatility, *volatility_levels)
        if value >= 0.01
    }
    if any(value < volatility_lower or value > volatility_upper for value in volatility_anchors):
        volatility_lower = min(volatility_lower, min(volatility_anchors))
        volatility_upper = max(volatility_upper, max(volatility_anchors))
    volatilities = _anchored_grid(
        volatility_lower,
        volatility_upper,
        int(config.volatility_points),
        volatility_anchors,
    )
    rate_lower = float(config.risk_free_rate_min) if config.risk_free_rate_min is not None else min(
        -0.02,
        market.risk_free_rate - 0.05,
    )
    rate_upper = float(config.risk_free_rate_max) if config.risk_free_rate_max is not None else max(
        0.08,
        market.risk_free_rate + 0.05,
    )
    risk_free_rates = _anchored_grid(
        rate_lower,
        rate_upper,
        int(config.risk_free_rate_points),
        {market.risk_free_rate},
    )
    multi_asset = len(contract.underlyings) > 1
    return RiskGridSpec(
        spot_curve_normalized=curve_spots,
        spot_surface_normalized=surface_spots,
        time_years=time_values,
        volatilities=volatilities,
        risk_free_rates=risk_free_rates,
        annotations=annotations,
        coordinate_mode="normalized_parallel" if multi_asset else "raw_spot",
        spot_axis_name="多标的平行比例变动" if multi_asset else "单标的现价",
        spot_axis_unit="normalized_spot_percent" if multi_asset else "raw_spot",
        mode=str(config.mode),
    )


def vanilla_risk_outputs(
    *,
    price_one: PriceOne,
    market: Any,
    maturity_years: float,
    reference_price: float,
    method: str,
    risk_grid: RiskGridSpec,
    normalized_base: float = 100.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """在条款感知坐标逐点调用同一正式模型并复用固定MC随机源。"""
    curve_markets = [
        replace(market, spot=_raw_spot(value, reference_price, normalized_base))
        for value in risk_grid.spot_curve_normalized
    ]
    surface_markets = [
        replace(market, spot=_raw_spot(value, reference_price, normalized_base))
        for value in risk_grid.spot_surface_normalized
    ]
    time_values = risk_grid.time_years
    scenario_time_indices = _scenario_time_indices(len(time_values))
    current_surface_index = min(
        range(len(surface_markets)),
        key=lambda index: abs(surface_markets[index].spot - market.spot),
    )
    scenario_spot_indices = {0, current_surface_index, len(surface_markets) - 1}
    grid = [
        [
            _risk_price(
                price_one,
                local_market,
                time_value,
                _SCENARIO_GREEKS
                if time_index in scenario_time_indices and spot_index in scenario_spot_indices
                else _SURFACE_GREEKS,
            )
            for spot_index, local_market in enumerate(surface_markets)
        ]
        for time_index, time_value in enumerate(time_values)
    ]
    curve_results = [
        _risk_price(price_one, local_market, maturity_years, _SPOT_CURVE_GREEKS)
        for local_market in curve_markets
    ]
    curve_spot_values = _spot_axis_values(risk_grid.spot_curve_normalized, reference_price, normalized_base, risk_grid)
    surface_spot_values = _spot_axis_values(risk_grid.spot_surface_normalized, reference_price, normalized_base, risk_grid)
    time_days = [round(value * 365.0, 6) for value in time_values]
    annotations = _axis_annotations(risk_grid, reference_price, normalized_base)
    curve_domain = _grid_domain(curve_spot_values, risk_grid.mode, annotations)
    surface_domain = _grid_domain(surface_spot_values, risk_grid.mode, annotations)

    curves = [
        _curve("delta_spot", "Delta-Spot", risk_grid.spot_axis_name, risk_grid.spot_axis_unit, "Delta", "pv_points_100_per_spot", curve_spot_values, curve_results, "delta", method, curve_domain, annotations),
        _curve("gamma_spot", "Gamma-Spot", risk_grid.spot_axis_name, risk_grid.spot_axis_unit, "Gamma", "pv_points_100_per_spot_squared", curve_spot_values, curve_results, "gamma", method, curve_domain, annotations),
        _curve("theta_time", "Theta-Time", "剩余期限", "calendar_days", "Theta", "pv_points_100_per_calendar_day", time_days, [grid[index][current_surface_index] for index in range(len(time_values))], "theta", method, _grid_domain(time_days, risk_grid.mode, ()), ()),
    ]
    vol_markets = [replace(market, volatility=value) for value in risk_grid.volatilities]
    vol_results = [_risk_price(price_one, local_market, maturity_years, _VEGA_ONLY) for local_market in vol_markets]
    vol_values = [item.volatility for item in vol_markets]
    curves.append(_curve("vega_volatility", "Vega-Volatility", "波动率", "decimal", "Vega", "pv_points_100_per_1pct_volatility", vol_values, vol_results, "vega", method, _grid_domain(vol_values, risk_grid.mode, ()), ()))

    rate_markets = [replace(market, risk_free_rate=value) for value in risk_grid.risk_free_rates]
    rate_results = [_risk_price(price_one, local_market, maturity_years, _RHO_ONLY) for local_market in rate_markets]
    rate_values = [item.risk_free_rate for item in rate_markets]
    rate_annotations = ({
        "key": "risk_free_rate",
        "label": "当前无风险利率",
        "symbol": "Rₓ",
        "axis_value": market.risk_free_rate,
        "kind": "market",
    },)
    curves.append(_curve("rho_rate", "Rho-利率", "无风险利率", "decimal_rate", "Rho", "pv_points_100_per_1pct_rate", rate_values, rate_results, "rho", method, _grid_domain(rate_values, risk_grid.mode, rate_annotations), rate_annotations))

    surfaces = [_surface(name, label, unit, surface_spot_values, time_days, grid, greek, method, risk_grid.spot_axis_name, risk_grid.spot_axis_unit, surface_domain, annotations) for name, label, unit, greek in (
        ("delta_surface", "Delta曲面", "pv_points_100_per_spot", "delta"),
        ("gamma_surface", "Gamma曲面", "pv_points_100_per_spot_squared", "gamma"),
        ("theta_surface", "Theta曲面", "pv_points_100_per_calendar_day", "theta"),
        ("vega_surface", "Vega曲面", "pv_points_100_per_1pct_volatility", "vega"),
    )]
    surfaces.append(_surface(
        "rho_surface", "Rho曲面", "pv_points_100_per_1pct_rate",
        surface_spot_values, time_days, grid, "rho", method,
        risk_grid.spot_axis_name, risk_grid.spot_axis_unit,
        surface_domain, annotations,
    ))
    spot_shifts = tuple(round(local.spot / market.spot - 1.0, 12) for local in surface_markets)
    scenarios = _spot_time_scenarios(grid, spot_shifts, time_days, surface_spot_values, method, risk_grid.spot_axis_name, scenario_spot_indices)
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
        priced = _risk_price(price_one, local, maturity_years, _SCENARIO_GREEKS)
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


def _contract_price_annotations(
    contract: Any,
    term_catalog: Mapping[str, Mapping[str, Any]],
    current_normalized: float,
) -> tuple[tuple[dict[str, Any], ...], set[float], set[float]]:
    annotations: list[dict[str, Any]] = [{
        "key": "S0",
        "label": "合同基准S₀",
        "symbol": "S₀",
        "normalized_value": float(contract.terms.get("S0", 100.0)),
        "kind": "reference",
    }, {
        "key": "spot",
        "label": "当前现价",
        "symbol": "Spot",
        "normalized_value": float(current_normalized),
        "kind": "market",
    }]
    scalar_levels: set[float] = set()
    schedule_levels: set[float] = set()
    for key, definition in term_catalog.items():
        value = contract.terms.get(key)
        if definition.get("unit") == "price" and isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if not isfinite(number) or number <= 0 or key in {"S0", "S0Vec"}:
                continue
            scalar_levels.add(number)
            annotations.append({
                "key": key,
                "label": str(definition.get("name_zh") or key),
                "symbol": str(definition.get("symbol") or key),
                "normalized_value": number,
                "kind": "contract",
            })
        if definition.get("value_type") != "schedule" or not isinstance(value, (tuple, list)):
            continue
        for ordinal, item in enumerate(value, start=1):
            if not isinstance(item, (tuple, list)) or len(item) < 2:
                continue
            raw_level = item[-1]
            if isinstance(raw_level, bool) or not isinstance(raw_level, (int, float)):
                continue
            number = float(raw_level)
            if not isfinite(number) or number <= 0:
                continue
            schedule_levels.add(number)
            annotations.append({
                "key": key,
                "label": f"{definition.get('name_zh') or key}第{ordinal}期",
                "symbol": str(definition.get("symbol") or key),
                "normalized_value": number,
                "kind": "schedule",
                "ordinal": ordinal,
            })
    return tuple(annotations), scalar_levels, schedule_levels


def _volatility_landmarks(contract: Any, term_catalog: Mapping[str, Mapping[str, Any]]) -> set[float]:
    values: set[float] = set()
    for key, definition in term_catalog.items():
        value = contract.terms.get(key)
        if definition.get("unit") != "volatility" or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if isfinite(number) and number > 0:
            values.add(number)
    return values


def _anchored_grid(lower: float, upper: float, count: int, anchors: set[float]) -> tuple[float, ...]:
    required = {round(float(lower), 12), round(float(upper), 12)}
    required.update(round(float(value), 12) for value in anchors if lower <= value <= upper)
    if len(required) > count:
        raise ValueError(f"风险网格点数{count}不足以容纳{len(required)}个合同关键坐标")
    if count == 1 or abs(upper - lower) <= 1e-15:
        return (round(float(lower), 12),)
    candidates = [lower + (upper - lower) * index / (count - 1) for index in range(count)]
    selected = set(required)
    while len(selected) < count:
        available = [value for value in candidates if round(value, 12) not in selected]
        if not available:
            break
        next_value = max(
            available,
            key=lambda value: min(abs(value - chosen) for chosen in selected),
        )
        selected.add(round(float(next_value), 12))
    return tuple(sorted(selected))


def _time_grid(
    *,
    contract: Any,
    market_date: date,
    maturity_years: float,
    points: int,
    path_dependent: bool,
    trading_sessions: tuple[str, ...] | list[str],
) -> tuple[float, ...]:
    if not path_dependent:
        floor = min(maturity_years, 2.0 / 365.0)
        if maturity_years <= floor + 1e-15:
            return (maturity_years,)
        return tuple(round(floor + (maturity_years - floor) * index / (points - 1), 12) for index in range(points))

    target = market_date.toordinal() + round(maturity_years * 365.0)
    sessions = sorted({
        date.fromisoformat(str(value))
        for value in trading_sessions
        if market_date <= date.fromisoformat(str(value)) and date.fromisoformat(str(value)).toordinal() <= target
    })
    future = [value for value in sessions if (value - market_date).days >= 2]
    if not future:
        return (maturity_years,)
    observation_dates: list[date] = []
    for schedule in getattr(contract, "resolved_schedules", {}).values():
        for value in schedule.get("dates", ()):
            observed = date.fromisoformat(str(value))
            if market_date < observed <= future[-1]:
                observation_dates.append(observed)
    required_dates = {future[0], future[-1]}
    if observation_dates:
        ordered = sorted(set(observation_dates))
        required_dates.update((ordered[0], ordered[len(ordered) // 2], ordered[-1]))
    if len(required_dates) > points:
        required_dates = {future[0], future[-1]}
    candidates = future
    selected = set(required_dates)
    while len(selected) < min(points, len(candidates)):
        available = [value for value in candidates if value not in selected]
        if not available:
            break
        next_value = max(
            available,
            key=lambda value: min(abs((value - chosen).days) for chosen in selected),
        )
        selected.add(next_value)
    return tuple(round((value - market_date).days / 365.0, 12) for value in sorted(selected))


def _raw_spot(normalized_spot: float, reference_price: float, normalized_base: float = 100.0) -> float:
    return normalized_spot * reference_price / normalized_base


def _spot_axis_values(values: tuple[float, ...], reference_price: float, normalized_base: float, spec: RiskGridSpec) -> list[float]:
    if spec.coordinate_mode == "normalized_parallel":
        return list(values)
    return [_raw_spot(value, reference_price, normalized_base) for value in values]


def _axis_annotations(spec: RiskGridSpec, reference_price: float, normalized_base: float) -> tuple[dict[str, Any], ...]:
    rows = []
    for annotation in spec.annotations:
        normalized = float(annotation["normalized_value"])
        rows.append({
            **annotation,
            "axis_value": normalized if spec.coordinate_mode == "normalized_parallel" else _raw_spot(normalized, reference_price, normalized_base),
        })
    return tuple(rows)


def _grid_domain(values: list[float], mode: str, annotations: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    return {
        "mode": mode,
        "minimum": min(values),
        "maximum": max(values),
        "node_count": len(values),
        "annotations": len(annotations),
    }


def _risk_price(price_one: PriceOne, market: Any, maturity_years: float, risk_greeks: frozenset[str]) -> tuple[Any | None, str | None]:
    try:
        return price_one(market, maturity_years, risk_greeks=risk_greeks), None
    except ValueError as error:
        message = str(error)
        expected_calendar_errors = (
            "OptionReg路径MC注入交易sessions未覆盖完整剩余合同期限",
            "价格路径终点",
            "风险期限节点早于首个冻结观察日",
        )
        if not any(token in message for token in expected_calendar_errors):
            raise
        return None, "风险点不能与真实交易日历对齐，剩余期限内不足两个真实交易session或会截断合同路径，标记not_applicable；未补造weekday。"


def _greek(priced: tuple[Any | None, str | None], name: str) -> float | None:
    result, _reason = priced
    if result is None:
        return None
    value = result.greeks.get(name)
    return value.value if value is not None else None


def _greek_state(priced: tuple[Any | None, str | None], name: str) -> tuple[str, str | None]:
    result, point_reason = priced
    if result is None:
        return "not_applicable", point_reason
    value = result.greeks.get(name)
    if value is None or value.value is None or value.status != "available":
        return "not_applicable", None if value is None else value.reason
    return "ok", None


def _random_source(priced: tuple[Any | None, str | None]) -> dict[str, Any] | None:
    result, _reason = priced
    if result is None:
        return None
    source = result.diagnostics.get("random_source")
    return None if source is None else dict(source)


def _curve(
    key: str,
    name: str,
    x_name: str,
    x_unit: str,
    y_name: str,
    y_unit: str,
    xs: list[float],
    priced: list[tuple[Any | None, str | None]],
    greek: str,
    method: str,
    domain: Mapping[str, Any],
    annotations: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    first_value = next(
        (result.greeks[greek] for result, _reason in priced if result is not None and greek in result.greeks),
        None,
    )
    points = []
    for x, value in zip(xs, priced, strict=True):
        status, reason = _greek_state(value, greek)
        points.append({
            "x": x,
            "y": _greek(value, greek),
            "status": status,
            "reason": reason,
            "random_source": _random_source(value),
        })
    return {
        "key": key, "name": name,
        "x_axis": {"name": x_name, "unit": x_unit},
        "y_axis": {"name": y_name, "unit": y_unit if first_value is None else first_value.unit},
        "method": method,
        "domain": dict(domain),
        "annotations": [dict(value) for value in annotations],
        "points": points,
    }


def _surface(
    key: str,
    name: str,
    z_unit: str,
    spots: list[float],
    times: list[float],
    grid: list[list[Any]],
    greek: str,
    method: str,
    spot_factor: str,
    spot_unit: str,
    domain: Mapping[str, Any],
    annotations: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    data = []
    for time_index, row in enumerate(grid):
        for spot_index, priced in enumerate(row):
            result, reason = priced
            status, greek_reason = _greek_state(priced, greek)
            data.append({
                "value": [spot_index, time_index, _greek(priced, greek)],
                "standard_error_points_100": None if result is None else result.standard_error_points_100,
                "path_pv_sha256_float64": None if result is None else result.diagnostics.get("path_pv_sha256_float64"),
                "status": status,
                "reason": greek_reason,
                "random_source": _random_source(priced),
            })
    first_value = next(
        (result.greeks[greek] for row in grid for result, _reason in row if result is not None and greek in result.greeks),
        None,
    )
    return {
        "key": key, "name": name, "method": method,
        "x_axis": {"name": spot_factor, "unit": spot_unit, "values": spots},
        "y_axis": {"name": "剩余期限", "unit": "calendar_days", "values": times},
        "z_axis": {"name": name.removesuffix("曲面"), "unit": z_unit if first_value is None else first_value.unit},
        "domain": dict(domain),
        "annotations": [dict(value) for value in annotations],
        "data": data,
    }


def _spot_time_scenarios(
    grid: list[list[Any]],
    shifts: tuple[float, ...],
    time_days: list[float],
    spots: list[float],
    method: str,
    spot_factor: str,
    spot_indices: set[int],
) -> list[dict[str, Any]]:
    time_indices = _scenario_time_indices(len(time_days))
    rows = []
    for time_index in time_indices:
        for spot_index in sorted(spot_indices):
            result, reason = grid[time_index][spot_index]
            rows.append({
                "spot_shift": shifts[spot_index], "spot": spots[spot_index], "spot_factor": spot_factor, "remaining_days": time_days[time_index],
                "pv_points_100": None if result is None else result.pv_points_100,
                "pv_percent": None if result is None else result.pv_percent,
                "standard_error_points_100": None if result is None else result.standard_error_points_100,
                "standard_error_percent": None if result is None else result.standard_error_percent,
                "value_basis": None if result is None else result.value_basis, "method": method,
                "status": "not_applicable" if result is None else "ok",
                "reason": reason if result is None else None,
                "greeks": {name.capitalize(): _greek((result, reason), name) for name in ("delta", "gamma", "theta", "vega", "rho")},
                "path_pv_sha256_float64": None if result is None else result.diagnostics.get("path_pv_sha256_float64"),
                "random_source": _random_source((result, reason)),
            })
    return rows


def _scenario_time_indices(time_count: int) -> tuple[int, ...]:
    return tuple(sorted({0, time_count // 2, time_count - 1}))


__all__ = ("RiskGridSpec", "build_risk_grid_spec", "scenario_values", "vanilla_risk_outputs")
