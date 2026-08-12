"""Independent STANDARD Monte Carlo engine for Autocall structures."""

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
    from ..enums import AutocallKind, PricingMethod
    from ..instruments import AutocallOption
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
        from derivatives.enums import AutocallKind, PricingMethod
        from derivatives.instruments import AutocallOption
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


_VERSION = "standard-autocall-3"
_SEED = 20240101
_MAX_PATHS = 2000


@njit(parallel=True)
def _path_kernel(
    rand_arr, nsim, s, k, ki, floor, vol, r, tday, ki_flag0,
    final_tday, final_rebate_dis, final_dis_factor,
    cpn_idx_init, cpn_amt_dis, cpn_barrier, cpn_tday,
    call_idx_init, call_amt_dis, call_barrier, call_tday,
    forward_div, margin, dis_factor, enhanced_strike, participation,
    is_call, out,
):
    dt = float32(1 / 244)
    rand_arg1 = float32(math.exp(-0.5 * vol ** 2 * dt))
    rand_arg2 = float32(vol * math.sqrt(dt))
    if is_call:
        for i in prange(nsim):
            st = s
            cpn_idx = cpn_idx_init
            call_idx = call_idx_init
            knocked_in = ki_flag0
            pv = 0
            for t in range(tday, final_tday + 1):
                knocked_in = knocked_in or (st <= ki)
                if cpn_idx < len(cpn_tday) and t == cpn_tday[cpn_idx]:
                    if st >= cpn_barrier[cpn_idx]:
                        pv += cpn_amt_dis[cpn_idx]
                    cpn_idx += 1
                if call_idx < len(call_tday) and t == call_tday[call_idx]:
                    if st >= call_barrier[call_idx]:
                        pv += call_amt_dis[call_idx] + dis_factor[call_idx] * max(st - enhanced_strike, 0) * participation
                        break
                    call_idx += 1
                if t == final_tday:
                    if knocked_in:
                        pv += final_dis_factor * (max(min(st - k, 0), min(floor - k, 0)) + margin) - margin
                    else:
                        pv += final_rebate_dis
                    break
                rand = rand_arr[i, t - tday]
                st *= forward_div[t - tday] * rand_arg1 * math.exp(rand_arg2 * rand)
            out[i] = pv
    else:
        for i in prange(nsim):
            st = s
            cpn_idx = cpn_idx_init
            call_idx = call_idx_init
            knocked_in = ki_flag0
            pv = 0
            for t in range(tday, final_tday + 1):
                knocked_in = knocked_in or (st >= ki)
                if cpn_idx < len(cpn_tday) and t == cpn_tday[cpn_idx]:
                    if st <= cpn_barrier[cpn_idx]:
                        pv += cpn_amt_dis[cpn_idx]
                    cpn_idx += 1
                if call_idx < len(call_tday) and t == call_tday[call_idx]:
                    if st <= call_barrier[call_idx]:
                        pv += call_amt_dis[call_idx] + dis_factor[call_idx] * max(enhanced_strike - st, 0) * participation
                        break
                    call_idx += 1
                if t == final_tday:
                    if knocked_in:
                        pv += final_dis_factor * (max(min(k - st, 0), min(k - floor, 0)) + margin) - margin
                    else:
                        pv += final_rebate_dis
                    break
                rand = rand_arr[i, t - tday]
                st *= forward_div[t - tday] * rand_arg1 * math.exp(rand_arg2 * rand)
            out[i] = pv


def _state(value: ValuationState | None) -> ValuationState:
    return value if value is not None else ValuationState()


def _strict_schedule(schedule, label: str) -> None:
    if not schedule:
        raise ValueError(f"{label}不能为空")
    previous_trading = None
    previous_calendar = None
    for point in schedule:
        if previous_trading is not None and point.trading_day <= previous_trading:
            raise ValueError(f"{label}交易日必须严格递增")
        if previous_calendar is not None and point.calendar_day <= previous_calendar:
            raise ValueError(f"{label}自然日必须严格递增")
        previous_trading = point.trading_day
        previous_calendar = point.calendar_day


def _validate(instrument: AutocallOption, config: ValuationConfig, state: ValuationState) -> int:
    if config.method is not PricingMethod.MONTE_CARLO_CPU:
        raise ValueError("Autocall STANDARD只支持MONTE_CARLO_CPU")
    if not isinstance(instrument.kind, AutocallKind):
        raise ValueError("kind必须是AutocallKind")
    if config.paths > _MAX_PATHS:
        raise ValueError(f"paths不得超过{_MAX_PATHS}")
    if config.seed != _SEED:
        raise ValueError("Autocall STANDARD只接受seed=20240101")
    if state.knocked_out:
        raise ValueError("已敲出的Autocall必须提供待结算现金流，不能按存续合约重新模拟")
    _strict_schedule(instrument.call_schedule, "call_schedule")
    _strict_schedule(instrument.coupon_schedule, "coupon_schedule")
    if instrument.kind is not AutocallKind.PHOENIX:
        explicit_coupon_amounts = [
            point.amount for point in instrument.coupon_schedule
            if point.amount is not None
        ]
        if any(amount != 0.0 for amount in explicit_coupon_amounts):
            raise ValueError(
                "Snowball和Trigger不支付存续coupon，coupon_schedule.amount必须为空或为0"
            )
    final_call = instrument.call_schedule[-1]
    if final_call.trading_day != instrument.final_trading_day or final_call.calendar_day != instrument.final_calendar_day:
        raise ValueError("call_schedule末日必须与final trading/calendar一致")
    final_coupon = instrument.coupon_schedule[-1]
    if final_coupon.trading_day > instrument.final_trading_day:
        raise ValueError("coupon_schedule交易日不得晚于final trading day")
    if final_coupon.calendar_day > instrument.final_calendar_day:
        raise ValueError("coupon_schedule自然日不得晚于final calendar day")
    if state.trading_day >= instrument.final_trading_day or state.calendar_day >= instrument.final_calendar_day:
        raise ValueError("估值日必须严格早于Autocall终止日")
    if not any(point.trading_day >= state.trading_day for point in instrument.call_schedule):
        raise ValueError("Autocall没有估值日之后的观察日")
    return instrument.final_trading_day - state.trading_day


def _optional_values(schedule, attribute: str, label: str) -> list[float] | None:
    values = [getattr(point, attribute) for point in schedule]
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError(f"{label}不能混合None与具体值，必须全部为空或全部完整")
    return values


def _effective_terms(instrument: AutocallOption, coupon_override: float | None = None):
    call_barriers = _optional_values(instrument.call_schedule, "barrier", "call_barrier")
    if call_barriers is None:
        step = instrument.knock_out_step_down
        call_barriers = [
            instrument.knock_out if step is None else instrument.knock_out + index * step
            for index in range(len(instrument.call_schedule))
        ]
    else:
        call_barriers = list(call_barriers)
    if instrument.parachute:
        call_barriers[-1] = instrument.knock_in

    coupon_barriers = _optional_values(instrument.coupon_schedule, "barrier", "coupon_barrier")
    if coupon_barriers is None:
        coupon_barriers = [instrument.knock_in] * len(instrument.coupon_schedule)

    call_amounts = _optional_values(instrument.call_schedule, "amount", "call_amount")
    if call_amounts is None:
        if instrument.kind is AutocallKind.SNOWBALL:
            call_amounts = [
                instrument.coupon * point.calendar_day / 365
                for point in instrument.call_schedule
            ]
        elif instrument.kind is AutocallKind.PHOENIX:
            call_amounts = [0] * len(instrument.call_schedule)
        else:
            call_amounts = [instrument.coupon] * len(instrument.call_schedule)

    explicit_coupon_amounts = _optional_values(
        instrument.coupon_schedule, "amount", "coupon_amount"
    )
    if instrument.kind is AutocallKind.PHOENIX:
        coupon_amounts = explicit_coupon_amounts
        if coupon_amounts is None:
            coupon_amounts = [instrument.coupon / 12] * len(instrument.coupon_schedule)
    else:
        coupon_amounts = [0] * len(instrument.coupon_schedule)

    final_rebate = instrument.final_rebate
    if coupon_override is not None:
        if instrument.kind is AutocallKind.SNOWBALL:
            call_amounts = [coupon_override * point.calendar_day / 365 for point in instrument.call_schedule]
            final_rebate = call_amounts[-1]
        elif instrument.kind is AutocallKind.PHOENIX:
            coupon_amounts = [coupon_override / 12] * len(instrument.coupon_schedule)
        elif instrument.kind is AutocallKind.TRIGGER:
            call_amounts = [coupon_override] * len(instrument.call_schedule)
            final_rebate = coupon_override
    return call_amounts, coupon_amounts, call_barriers, coupon_barriers, final_rebate


def _warnings(config: ValuationConfig) -> list[str]:
    warnings = []
    if config.paths in (10, 50):
        warnings.append(f"{config.paths}条路径仅用于快速回归，不代表正式报价精度")
    return warnings


def _forward_div(instrument: AutocallOption, market: MarketState, state: ValuationState) -> np.ndarray:
    count = instrument.final_trading_day - state.trading_day + 1
    indices = list(range(count))
    if market.forward_curve:
        adjusted = [
            market.spot
            + (price - market.spot) * instrument.forward_curve_weight
            for price, _ in market.forward_curve
        ]
        values = np.interp(indices, [0] + [day for _, day in market.forward_curve], [market.spot] + adjusted)
    else:
        carry = (
            market.risk_free_rate - market.dividend_yield
            if market.carry is None
            else market.carry
        )
        values = np.interp(
            indices,
            [0, 244, 244 * 2, 244 * 3],
            [market.spot, market.spot * np.exp(carry), market.spot * np.exp(2 * carry), market.spot * np.exp(3 * carry)],
        )
    rounded = np.array([round(value, 4) for value in values]).astype(np.float32)
    return rounded[1:] / rounded[:-1]


def _simulate(
    instrument: AutocallOption,
    market: MarketState,
    config: ValuationConfig,
    state: ValuationState,
    coupon_override: float | None = None,
) -> tuple[np.ndarray, NpyRandomSource, int]:
    used_steps = _validate(instrument, config, state)
    monte_carlo = config.monte_carlo
    source = monte_carlo.random_source
    if not isinstance(source, NpyRandomSource):
        raise ValueError("random_source必须是NpyRandomSource")
    if source.info.seed != _SEED:
        raise ValueError("随机数源seed与配置不一致")
    matrix = source.load(paths=monte_carlo.paths, steps=used_steps)

    call_amounts, coupon_amounts, call_barriers, coupon_barriers, final_rebate = (
        _effective_terms(instrument, coupon_override)
    )
    call_tday = np.array([point.trading_day for point in instrument.call_schedule])
    call_nday = [point.calendar_day for point in instrument.call_schedule]
    coupon_tday = np.array([point.trading_day for point in instrument.coupon_schedule])
    coupon_nday = [point.calendar_day for point in instrument.coupon_schedule]
    call_index = np.where(state.trading_day <= call_tday)[0][0].astype(np.int16)
    future_coupon_indices = np.where(state.trading_day <= coupon_tday)[0]
    coupon_index = np.int16(
        future_coupon_indices[0] if len(future_coupon_indices) else len(coupon_tday)
    )
    rate = market.risk_free_rate
    coupon_discounted = [amount * np.exp(-rate * (day - state.calendar_day) / 365) for amount, day in zip(coupon_amounts, coupon_nday)]
    call_discounted = [(amount + instrument.margin) * np.exp(-rate * (day - state.calendar_day) / 365) - instrument.margin for amount, day in zip(call_amounts, call_nday)]
    discount_factors = [np.exp(-rate * (day - state.calendar_day) / 365) for day in call_nday]
    final_discount = np.exp(-rate * (instrument.final_calendar_day - state.calendar_day) / 365)
    final_rebate_discounted = final_discount * (final_rebate + instrument.margin) - instrument.margin
    out = np.zeros(monte_carlo.paths, dtype=np.float32)
    with numba_thread_scope(monte_carlo.threads):
        _path_kernel(
            matrix, int(monte_carlo.paths), float32(market.spot), float32(instrument.strike),
            float32(instrument.knock_in), float32(instrument.floor), float32(market.volatility),
            float32(rate), int16(state.trading_day), 1 if state.knocked_in else 0,
            int16(instrument.final_trading_day), float32(final_rebate_discounted), float32(final_discount),
            int16(coupon_index), np.array(coupon_discounted).astype(np.float32),
            np.array(coupon_barriers).astype(np.float32),
            coupon_tday.astype(np.int16), int16(call_index), np.array(call_discounted).astype(np.float32),
            np.array(call_barriers).astype(np.float32),
            call_tday.astype(np.int16), _forward_div(instrument, market, state), float32(instrument.margin),
            np.array(discount_factors).astype(np.float32),
            float32(instrument.knock_out if instrument.enhanced_strike is None else instrument.enhanced_strike),
            float32(instrument.participation), instrument.call_put.name == "CALL", out,
        )
    return out, source, used_steps


def simulate_autocall_paths(
    instrument: AutocallOption,
    market: MarketState,
    config: ValuationConfig,
    valuation_state: ValuationState | None = None,
) -> np.ndarray:
    """Return the deterministic float32 per-path PV vector."""
    return _simulate(instrument, market, config, _state(valuation_state))[0]


def _random_diagnostics(config, source, used_steps):
    return {
        "paths": config.paths, "seed": config.seed, "threads": config.threads,
        "random_source": source.info.path, "random_sha256": source.info.sha256,
        "random_shape": source.info.shape, "random_dtype": source.info.dtype,
        "used_paths": config.paths, "used_steps": used_steps,
    }


def _contract_diagnostics(instrument: AutocallOption):
    call_amounts, coupon_amounts, call_barriers, coupon_barriers, final_rebate = (
        _effective_terms(instrument)
    )
    return {
        "observation_days": {
            "call_tday": [point.trading_day for point in instrument.call_schedule],
            "call_nday": [point.calendar_day for point in instrument.call_schedule],
            "coupon_tday": [point.trading_day for point in instrument.coupon_schedule],
            "coupon_nday": [point.calendar_day for point in instrument.coupon_schedule],
            "final_tday": instrument.final_trading_day,
            "final_nday": instrument.final_calendar_day,
        },
        "cashflow_summary": {
            "call_amounts": call_amounts,
            "coupon_amounts": coupon_amounts,
            "call_barriers": call_barriers,
            "coupon_barriers": coupon_barriers,
            "final_rebate": final_rebate,
            "margin": instrument.margin,
        },
    }


def price_autocall_standard(instrument, market, config, valuation_state=None) -> PricingResult:
    state = _state(valuation_state)
    paths, source, used_steps = _simulate(instrument, market, config, state)
    raw = float(np.mean(paths))
    converted = convert_points_100(float(raw), instrument.basis)
    if converted.pv_points_100 is None:
        raise ValueError("Autocall STANDARD Greek要求可转换为pv_points_100的basis")

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
    digest = hashlib.sha256(paths.tobytes(order="C")).hexdigest()
    contract_diagnostics = _contract_diagnostics(instrument)
    warnings = list(converted.warnings)
    warnings.extend(_warnings(config))
    return PricingResult(
        pv_amount=converted.pv_amount, pv_percent=converted.pv_percent,
        pv_points_100=converted.pv_points_100, currency=converted.currency,
        greeks=greeks, method=PricingMethod.MONTE_CARLO_CPU, version=_VERSION,
        extended_greeks=extended_greeks,
        warnings=tuple(warnings),
        diagnostics={
            "engine": "standard.autocall", "runtime": "STANDARD_ONLY",
            **_random_diagnostics(config, source, used_steps),
            "path_pv_sha256_float32": digest,
            "standard_greeks": greek_diagnostics,
            **contract_diagnostics,
            "forward_curve_weight": instrument.forward_curve_weight,
        },
        engine_raw={"price": raw, "unit": "POINTS_100"},
    )


def solve_autocall_standard(instrument, market, config, target, valuation_state=None) -> SolveResult:
    if target.variable != "coupon":
        raise ValueError("Autocall当前只支持反解coupon")
    if any(
        point.amount is not None
        for point in instrument.call_schedule + instrument.coupon_schedule
    ):
        raise ValueError(
            "反解coupon时call_schedule和coupon_schedule不得锁定amount；"
            "现金流金额必须由待求coupon生成"
        )
    state = _state(valuation_state)
    latest_raw = None
    latest_source = None
    latest_steps = None

    def objective(coupon):
        nonlocal latest_raw, latest_source, latest_steps
        paths, latest_source, latest_steps = _simulate(instrument, market, config, state, coupon)
        latest_raw = float(np.mean(paths))
        return (
            raw_price_to_points_100(latest_raw, instrument.basis)
            - target.target_pv
        )

    lower_bound = -100.0 if target.lower_bound is None else target.lower_bound
    upper_bound = 100.0 if target.upper_bound is None else target.upper_bound
    root = optimize.root_scalar(
        objective,
        bracket=(lower_bound, upper_bound),
        method="brentq",
        xtol=target.solver_absolute_tolerance,
        maxiter=target.maximum_iterations,
    )
    value = float(root.root)
    residual = float(objective(value))
    assert latest_source is not None and latest_steps is not None
    target_met = abs(residual) <= target.target_pv_absolute_tolerance
    contract_patch = {"coupon": value}
    if instrument.kind is AutocallKind.SNOWBALL:
        contract_patch["final_rebate"] = (
            value * instrument.call_schedule[-1].calendar_day / 365
        )
    elif instrument.kind is AutocallKind.TRIGGER:
        contract_patch["final_rebate"] = value
    return SolveResult(
        value=value, variable=target.variable, target_pv=target.target_pv,
        converged=bool(root.converged) and target_met,
        method=PricingMethod.MONTE_CARLO_CPU,
        version=_VERSION,
        warnings=tuple(_warnings(config)),
        diagnostics={
            "engine": "standard.autocall", **_random_diagnostics(config, latest_source, latest_steps),
            "solver": "scipy.optimize.root_scalar.brentq",
            "lower_bound": lower_bound,
            "upper_bound": upper_bound,
            "iterations": root.iterations,
            "function_calls": root.function_calls + 1,
            "residual": residual,
            "solver_absolute_tolerance": target.solver_absolute_tolerance,
            "target_pv_absolute_tolerance": target.target_pv_absolute_tolerance,
            "target_met": target_met,
            "contract_patch": contract_patch,
            "maximum_iterations": target.maximum_iterations,
        },
        engine_raw={"value": value, "target_pv_points_100": target.target_pv},
    )


__all__ = [
    "price_autocall_standard", "simulate_autocall_paths", "solve_autocall_standard",
]
