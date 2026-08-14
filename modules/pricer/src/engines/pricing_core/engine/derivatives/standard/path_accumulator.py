"""Independent STANDARD engine for the path-dependent Accumulator."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import hashlib
import math
from pathlib import Path
import sys

from numba import float32, int16, njit, prange
import numpy as np
from scipy import optimize

try:
    from ..basis import convert_points_100
    from ..enums import PricingMethod
    from ..instruments import PathAccumulatorOption
    from ..models import MarketState, SolveTarget, ValuationConfig, ValuationState
    from ..random_source import NpyRandomSource
    from ..results import PricingResult, SolveResult
    from ._numba_runtime import numba_thread_scope
    from .risk import ThetaRollValue, calculate_standard_greeks, raw_price_to_points_100
except ImportError:  # Direct file loading used by the audit harness.
    _engine_path = str(Path(__file__).resolve().parents[2])
    sys.path.insert(0, _engine_path)
    try:
        from derivatives.basis import convert_points_100
        from derivatives.enums import PricingMethod
        from derivatives.instruments import PathAccumulatorOption
        from derivatives.models import MarketState, SolveTarget, ValuationConfig, ValuationState
        from derivatives.random_source import NpyRandomSource
        from derivatives.results import PricingResult, SolveResult
        from derivatives.standard._numba_runtime import numba_thread_scope
        from derivatives.standard.risk import (
            ThetaRollValue,
            calculate_standard_greeks,
            raw_price_to_points_100,
        )
    finally:
        if sys.path and sys.path[0] == _engine_path:
            sys.path.pop(0)


_IMPLEMENTATION_ID = "standard-path-accumulator"
_MAX_PATHS = 2000


@njit(parallel=True)
def _price_paths_ko_terminates(
    rand_arr, nsim, call, s, ko_levels, k, vol, tday, ko_tday, ko_idx_init,
    ko_begin_tday, mult, r, forward_div, acc_num0, lock_tday, ko_flag0, out,
):
    i = int16(0)
    t = int16(0)
    st = float32(0)
    pv = float32(0)
    dt = float32(1 / 244)
    rand_arg1 = float32(math.exp(-0.5 * vol ** 2 * dt))
    rand_arg2 = float32(vol * math.sqrt(dt))
    tenor_tday = ko_tday[-1]

    for i in prange(nsim):
        st = s
        pv = 0
        ko_idx = ko_idx_init
        ko_flag = ko_flag0
        single_times = 0
        mult_times = 0
        if not ko_flag:
            for t in range(tday, tenor_tday + 1):
                if t == ko_tday[ko_idx]:
                    ko = ko_levels[ko_idx]
                    if call:
                        if t >= ko_begin_tday and st >= ko:
                            ko_flag = True
                            break
                        if st > k:
                            single_times += 1
                        else:
                            mult_times += 1
                    else:
                        if t >= ko_begin_tday and st <= ko:
                            ko_flag = True
                            break
                        if st < k:
                            single_times += 1
                        else:
                            mult_times += 1
                    ko_idx += 1
                if t < tenor_tday:
                    rand = rand_arr[i, t - tday]
                    st *= forward_div[t - tday] * rand_arg1 * math.exp(rand_arg2 * rand)
        else:
            t = tday

        if ko_flag and t <= lock_tday:
            for tt in range(ko_idx, len(ko_tday)):
                if ko_tday[tt] <= lock_tday:
                    single_times += 1
        acc_num = acc_num0 + single_times + mult * mult_times
        pv = math.exp(-r * (t - tday) / 244) * acc_num * (st - k)
        if not call:
            pv *= -1
        out[i] = pv


@njit(parallel=True)
def _price_paths_ko_does_not_terminate(
    rand_arr, nsim, call, s, ko_levels, k, vol, tday, ko_tday, ko_idx_init,
    ko_begin_tday, mult, r, forward_div, acc_num0, out,
):
    i = int16(0)
    t = int16(0)
    st = float32(0)
    pv = float32(0)
    dt = float32(1 / 244)
    rand_arg1 = float32(math.exp(-0.5 * vol ** 2 * dt))
    rand_arg2 = float32(vol * math.sqrt(dt))
    tenor_tday = ko_tday[-1]

    for i in prange(nsim):
        st = s
        pv = 0
        ko_idx = ko_idx_init
        single_times = 0
        mult_times = 0
        for t in range(tday, tenor_tday + 1):
            if t == ko_tday[ko_idx]:
                ko = ko_levels[ko_idx]
                if call:
                    if t < ko_begin_tday or st <= ko:
                        if st > k:
                            single_times += 1
                        else:
                            mult_times += 1
                else:
                    if t < ko_begin_tday or st >= ko:
                        if st < k:
                            single_times += 1
                        else:
                            mult_times += 1
                ko_idx += 1
            if t < tenor_tday:
                rand = rand_arr[i, t - tday]
                st *= forward_div[t - tday] * rand_arg1 * math.exp(rand_arg2 * rand)
        acc_num = acc_num0 + single_times + mult * mult_times
        pv = math.exp(-r * (t - tday) / 244) * acc_num * (st - k)
        if not call:
            pv *= -1
        out[i] = pv


def _state(value: ValuationState | None) -> ValuationState:
    return value if value is not None else ValuationState()


def _validate(
    instrument: PathAccumulatorOption,
    config: ValuationConfig,
    state: ValuationState,
) -> int:
    if config.method is not PricingMethod.MONTE_CARLO_CPU:
        raise ValueError("路径累购STANDARD只支持MONTE_CARLO_CPU")
    if config.paths > _MAX_PATHS:
        raise ValueError(f"paths不得超过{_MAX_PATHS}")
    if not instrument.observation_schedule:
        raise ValueError("observation_schedule不能为空")
    previous_trading = None
    previous_calendar = None
    for point in instrument.observation_schedule:
        if previous_trading is not None and point.trading_day <= previous_trading:
            raise ValueError("observation_schedule交易日必须严格递增")
        if previous_calendar is not None and point.calendar_day <= previous_calendar:
            raise ValueError("observation_schedule自然日必须严格递增")
        previous_trading = point.trading_day
        previous_calendar = point.calendar_day
    barriers = [point.barrier for point in instrument.observation_schedule]
    if any(value is None for value in barriers) and not all(
        value is None for value in barriers
    ):
        raise ValueError("observation_schedule.barrier必须全部为空或全部明确")
    final_day = instrument.observation_schedule[-1].trading_day
    if state.trading_day > final_day:
        raise ValueError("估值交易日不得晚于最后观察日")
    if not any(point.trading_day >= state.trading_day for point in instrument.observation_schedule):
        raise ValueError("路径累购没有估值日之后的观察日")
    return final_day - state.trading_day


def _forward_div(
    instrument: PathAccumulatorOption,
    market: MarketState,
    state: ValuationState,
) -> np.ndarray:
    final_day = instrument.observation_schedule[-1].trading_day
    indices = list(range(final_day - state.trading_day + 1))
    if market.forward_curve:
        adjusted = [
            market.spot
            + (price - market.spot) * instrument.forward_curve_weight
            for price, _ in market.forward_curve
        ]
        values = np.interp(
            indices,
            [0] + [day for _, day in market.forward_curve],
            [market.spot] + adjusted,
        )
    else:
        carry = (
            market.risk_free_rate - market.dividend_yield
            if market.carry is None
            else market.carry
        )
        values = np.interp(
            indices,
            [0, 244, 244 * 2, 244 * 3],
            [
                market.spot,
                market.spot * np.exp(carry),
                market.spot * np.exp(2 * carry),
                market.spot * np.exp(3 * carry),
            ],
        )
    rounded = np.array([round(value, 4) for value in values]).astype(np.float32)
    return rounded[1:] / rounded[:-1]


def _simulate(
    instrument: PathAccumulatorOption,
    market: MarketState,
    config: ValuationConfig,
    state: ValuationState,
) -> tuple[np.ndarray, NpyRandomSource, int]:
    used_steps = _validate(instrument, config, state)
    monte_carlo = config.monte_carlo
    source = monte_carlo.random_source
    if not isinstance(source, NpyRandomSource):
        raise ValueError("random_source必须是NpyRandomSource")
    matrix = source.load(paths=monte_carlo.paths, steps=used_steps)

    ko_tday = np.array([point.trading_day for point in instrument.observation_schedule])
    explicit_barriers = [point.barrier for point in instrument.observation_schedule]
    ko_levels = np.array(
        [instrument.knock_out] * len(explicit_barriers)
        if all(value is None for value in explicit_barriers)
        else explicit_barriers,
        dtype=np.float32,
    )
    ko_idx_init = np.where(state.trading_day <= ko_tday)[0][0].astype(np.int16)
    forward_div = _forward_div(instrument, market, state)
    out = np.zeros(monte_carlo.paths, dtype=np.float32)

    with numba_thread_scope(monte_carlo.threads):
        if instrument.ko_terminates:
            _price_paths_ko_terminates(
                matrix,
                int(monte_carlo.paths),
                instrument.call_put.name == "CALL",
                float32(market.spot),
                ko_levels,
                float32(instrument.strike),
                float32(market.volatility),
                int16(state.trading_day),
                ko_tday,
                int16(ko_idx_init),
                int16(instrument.ko_begin_trading_day),
                float32(instrument.multiplier),
                float32(market.risk_free_rate),
                forward_div,
                int16(state.accumulated_count),
                int16(instrument.lock_trading_days),
                state.knocked_out,
                out,
            )
        else:
            _price_paths_ko_does_not_terminate(
                matrix,
                int(monte_carlo.paths),
                instrument.call_put.name == "CALL",
                float32(market.spot),
                ko_levels,
                float32(instrument.strike),
                float32(market.volatility),
                int16(state.trading_day),
                ko_tday,
                int16(ko_idx_init),
                int16(instrument.ko_begin_trading_day),
                float32(instrument.multiplier),
                float32(market.risk_free_rate),
                forward_div,
                int16(state.accumulated_count),
                out,
            )
    return out, source, used_steps


def simulate_path_accumulator_paths(
    instrument: PathAccumulatorOption,
    market: MarketState,
    config: ValuationConfig,
    valuation_state: ValuationState | None = None,
) -> np.ndarray:
    """Return the deterministic float32 per-path PV vector."""
    return _simulate(instrument, market, config, _state(valuation_state))[0]


def _warnings(instrument: PathAccumulatorOption, config: ValuationConfig) -> tuple[str, ...]:
    warnings = []
    if config.paths in (10, 50):
        warnings.append(f"{config.paths}条路径仅用于快速回归，不代表正式报价精度")
    return tuple(warnings)


def _diagnostics(
    instrument: PathAccumulatorOption,
    config: ValuationConfig,
    state: ValuationState,
    source: NpyRandomSource,
    used_steps: int,
    paths: np.ndarray,
) -> dict:
    return {
        "engine": "standard_path_accumulator_monte_carlo_cpu",
        "paths": config.paths,
        "seed": source.info.seed,
        "requested_seed": config.seed,
        "threads": config.threads,
        "random_source": source.info.path,
        "random_sha256": source.info.sha256,
        "random_origin": source.info.origin,
        "random_shape": source.info.shape,
        "random_dtype": source.info.dtype,
        "used_paths": config.paths,
        "used_steps": used_steps,
        "path_pv_sha256_float32": hashlib.sha256(paths.tobytes()).hexdigest(),
        "schedule": {
            "observation_count": len(instrument.observation_schedule),
            "first_trading_day": instrument.observation_schedule[0].trading_day,
            "last_trading_day": instrument.observation_schedule[-1].trading_day,
            "barriers": [
                instrument.knock_out if point.barrier is None else point.barrier
                for point in instrument.observation_schedule
            ],
        },
        "cashflow_basis": "终止日累计数量乘以终止标的价与行权价之差",
        "quantity_basis": instrument.quantity_basis.name,
        "accumulated_count": state.accumulated_count,
        "knocked_out": state.knocked_out,
        "ko_terminates": instrument.ko_terminates,
        "ko_begin_trading_day": instrument.ko_begin_trading_day,
        "forward_curve_weight": instrument.forward_curve_weight,
    }


def price_path_accumulator_standard(
    instrument: PathAccumulatorOption,
    market: MarketState,
    config: ValuationConfig,
    valuation_state: ValuationState | None = None,
) -> PricingResult:
    state = _state(valuation_state)
    paths, source, used_steps = _simulate(instrument, market, config, state)
    raw = float(np.mean(paths))
    converted = convert_points_100(raw, instrument.basis)
    if converted.pv_points_100 is None:
        raise ValueError("Path Accumulator Greek要求可转换为pv_points_100的basis")

    def price_market(changed_market: MarketState) -> float:
        changed_paths, _, _ = _simulate(
            instrument, changed_market, config, state
        )
        return raw_price_to_points_100(float(np.mean(changed_paths)), instrument.basis)

    def theta_roll(convention) -> ThetaRollValue:
        calendar_shift = convention.theta_calendar_day_shift
        trading_shift = convention.theta_trading_day_shift
        rolled_state = replace(
            state,
            trading_day=state.trading_day + trading_shift,
            calendar_day=state.calendar_day + calendar_shift,
        )
        rolled_market = replace(
            market,
            as_of=market.as_of + timedelta(days=calendar_shift),
        )
        rolled_paths, _, _ = _simulate(
            instrument, rolled_market, config, rolled_state
        )
        return ThetaRollValue(
            price_points_100=raw_price_to_points_100(
                float(np.mean(rolled_paths)), instrument.basis
            ),
            calendar_day_shift=calendar_shift,
            trading_day_shift=trading_shift,
            description="ValuationState按显式交易日和自然日同步前推",
        )

    greeks, extended_greeks, greek_diagnostics = calculate_standard_greeks(
        base_price_points_100=converted.pv_points_100,
        market=market,
        config=config,
        basis=instrument.basis,
        price_market=price_market,
        theta_roll=theta_roll,
    )
    warnings = tuple(dict.fromkeys((*converted.warnings, *_warnings(instrument, config))))
    diagnostics = _diagnostics(
        instrument, config, state, source, used_steps, paths
    )
    diagnostics["standard_greeks"] = greek_diagnostics
    return PricingResult(
        pv_amount=converted.pv_amount,
        pv_percent=converted.pv_percent,
        pv_points_100=converted.pv_points_100,
        currency=converted.currency,
        greeks=greeks,
        method=PricingMethod.MONTE_CARLO_CPU,
        implementation_id=_IMPLEMENTATION_ID,
        extended_greeks=extended_greeks,
        warnings=warnings,
        diagnostics=diagnostics,
        engine_raw={"price": raw, "unit": "POINTS_100"},
    )


def solve_path_accumulator_standard(
    instrument: PathAccumulatorOption,
    market: MarketState,
    config: ValuationConfig,
    target: SolveTarget,
    valuation_state: ValuationState | None = None,
) -> SolveResult:
    if target.variable != "strike":
        raise ValueError("路径累购STANDARD求解仅支持strike")
    state = _state(valuation_state)
    latest_source = None
    latest_steps = None

    def objective(strike: float):
        nonlocal latest_source, latest_steps
        paths, latest_source, latest_steps = _simulate(
            replace(instrument, strike=strike), market, config, state
        )
        return (
            raw_price_to_points_100(float(np.mean(paths)), instrument.basis)
            - target.target_pv
        )

    lower_bound = (
        max(1e-8, market.spot * 0.01)
        if target.lower_bound is None
        else target.lower_bound
    )
    upper_bound = (
        market.spot * 3.0
        if target.upper_bound is None
        else target.upper_bound
    )
    root = optimize.root_scalar(
        objective,
        bracket=(lower_bound, upper_bound),
        method="brentq",
        xtol=target.solver_absolute_tolerance,
        maxiter=target.maximum_iterations,
    )
    solved = float(root.root)
    residual = float(objective(solved))
    assert latest_source is not None and latest_steps is not None
    target_met = abs(residual) <= target.target_pv_absolute_tolerance
    return SolveResult(
        value=solved,
        variable="strike",
        target_pv=target.target_pv,
        converged=bool(root.converged) and target_met,
        method=PricingMethod.MONTE_CARLO_CPU,
        implementation_id=_IMPLEMENTATION_ID,
        warnings=_warnings(instrument, config),
        diagnostics={
            "engine": "standard_path_accumulator_monte_carlo_cpu",
            "paths": config.paths,
            "seed": latest_source.info.seed,
            "requested_seed": config.seed,
            "threads": config.threads,
            "random_source": latest_source.info.path,
            "random_sha256": latest_source.info.sha256,
            "random_origin": latest_source.info.origin,
            "random_shape": latest_source.info.shape,
            "random_dtype": latest_source.info.dtype,
            "used_paths": config.paths,
            "used_steps": latest_steps,
            "solver": "scipy.optimize.root_scalar.brentq",
            "lower_bound": lower_bound,
            "upper_bound": upper_bound,
            "iterations": root.iterations,
            "function_calls": root.function_calls + 1,
            "solver_absolute_tolerance": target.solver_absolute_tolerance,
            "target_pv_absolute_tolerance": target.target_pv_absolute_tolerance,
            "target_met": target_met,
            "contract_patch": {"strike": solved},
            "maximum_iterations": target.maximum_iterations,
            "residual": residual,
            "quantity_basis": instrument.quantity_basis.name,
        },
        engine_raw={
            "value": solved,
            "target_pv": target.target_pv,
            "quantity_basis": instrument.quantity_basis.name,
            "residual": residual,
        },
    )


__all__ = [
    "simulate_path_accumulator_paths",
    "price_path_accumulator_standard",
    "solve_path_accumulator_standard",
]
