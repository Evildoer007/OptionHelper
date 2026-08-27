"""Shared discrete-path Monte Carlo for every executable OptionReg contract.

The simulation belongs to the formal Pricer internal base. Product
semantics do not: they are interpreted from immutable OptionReg paths by the
shared contract engine, so a new registry product does not introduce a new
pricing formula or engine branch.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
from datetime import date, timedelta
import math
from typing import Any

import numpy as np

from ..basis import convert_points_100
from ..enums import PricingMethod
from ..instruments import OptionRegPathOption
from ..models import MarketState, ValuationConfig, ValuationState
from ..results import PricingResult
from .risk import ThetaNotApplicableError, ThetaRollValue, calculate_standard_greeks


_IMPLEMENTATION_ID = "standard-optionreg-discrete-mc"


def _session_dates(
    instrument: OptionRegPathOption,
    market: MarketState,
    valuation_state: ValuationState | None = None,
) -> tuple[tuple[date, ...], np.ndarray]:
    """Use injected sessions for observed paths, or exact endpoints for terminal MC."""
    elapsed = _elapsed_years(valuation_state)
    remaining = float(instrument.resolved_contract.terms["T"]) - elapsed
    if remaining <= 0.0:
        raise ValueError("合同在估值日已无剩余期限")
    if not instrument.resolved_contract.terms.get("monitor") and not instrument.trading_sessions:
        # Terminal-only structures have no contract observation date.  Use
        # valuation and contractual maturity endpoints only; do not invent
        # exchange sessions between them.  If a verified calendar was
        # explicitly supplied, keep its complete path instead of discarding
        # those sessions merely because the payoff reads only the terminal.
        return (
            (market.as_of, market.as_of + timedelta(days=round(remaining * 365.0))),
            np.asarray((0.0, remaining), dtype=float),
        )
    if not instrument.calendar_id or not instrument.calendar_revision:
        raise ValueError("OptionReg路径MC缺少calendar_id或calendar_revision")
    try:
        all_sessions = tuple(date.fromisoformat(str(value)) for value in instrument.trading_sessions)
    except ValueError as error:
        raise ValueError("OptionReg路径MC trading_sessions必须为YYYY-MM-DD") from error
    if not all_sessions or all_sessions != tuple(sorted(all_sessions)) or len(set(all_sessions)) != len(all_sessions):
        raise ValueError("OptionReg路径MC trading_sessions必须严格递增且不重复")
    try:
        start = all_sessions.index(market.as_of)
    except ValueError as error:
        raise ValueError("OptionReg路径MC估值日必须位于注入trading_sessions") from error
    target = market.as_of + timedelta(days=round(remaining * 365.0))
    values = tuple(value for value in all_sessions[start:] if value <= target)
    if len(values) < 2 or (target - values[-1]).days > 14:
        raise ValueError("OptionReg路径MC注入交易sessions未覆盖完整剩余合同期限")
    # 模拟路径始终保留完整交易所日历。daily、monthly_last等合同观察规则
    # 由OptionReg解释器在完整PricePath上选择，不允许用n_obs平均抽样替代交易日。
    if len(values) - 1 > 800:
        raise ValueError("合同期限超出冻结随机源支持的1至800交易日")
    times = np.asarray([(value - market.as_of).days / 365.0 for value in values], dtype=float)
    if not np.all(np.diff(times) > 0.0):
        raise ValueError("OptionReg路径MC交易sessions必须形成严格递增ACT/365时间轴")
    return values, times


def _asset_inputs(instrument: OptionRegPathOption, market: MarketState) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    contract = instrument.resolved_contract
    asset_count = len(contract.underlyings)
    raw_spots = tuple(instrument.asset_spots) if hasattr(instrument, "asset_spots") else ()
    raw_volatilities = tuple(instrument.asset_volatilities) if hasattr(instrument, "asset_volatilities") else ()
    raw_dividend_yields = tuple(instrument.asset_dividend_yields) if hasattr(instrument, "asset_dividend_yields") else ()
    base_spots = np.asarray(raw_spots or (market.spot,) * asset_count, dtype=float)
    base_volatilities = np.asarray(raw_volatilities or (market.volatility,) * asset_count, dtype=float)
    base_dividend_yields = np.asarray(raw_dividend_yields or (market.dividend_yield,) * asset_count, dtype=float)
    if (base_spots <= 0.0).any() or (base_volatilities <= 0.0).any():
        raise ValueError("OptionReg路径MC的标的spot和volatility必须逐一为正数")
    # Standard risk shocks enter MarketState.  Preserve the adapter's asset
    # vector ratios while applying a parallel market shock, so CRN Greeks do
    # not silently become zero for multi-asset contracts.
    spots = base_spots * (market.spot / base_spots[0])
    # Vega and scenario panels shock the lead asset through ``MarketState``.
    # Apply that same absolute shock to every asset, but retain a positive
    # numerical floor for a low-volatility secondary asset.  Previously a
    # legitimate lead-asset downward bump could turn another asset negative,
    # making a multi-underlying product fail only while building its risk
    # panel even though its base PV was valid.
    volatilities = np.maximum(
        base_volatilities + (market.volatility - base_volatilities[0]),
        1e-8,
    )
    dividend_yields = base_dividend_yields + (market.dividend_yield - base_dividend_yields[0])
    if (
        spots.shape != (asset_count,)
        or volatilities.shape != (asset_count,)
        or dividend_yields.shape != (asset_count,)
        or (spots <= 0).any()
        or (volatilities <= 0).any()
        or not np.isfinite(dividend_yields).all()
    ):
        raise ValueError("OptionReg路径MC的标的spot和volatility必须逐一为正数")
    raw_correlation = getattr(instrument, "correlation", None)
    correlation = np.eye(asset_count, dtype=float) if raw_correlation is None else np.asarray(raw_correlation, dtype=float)
    if correlation.shape != (asset_count, asset_count) or not np.allclose(correlation, correlation.T, atol=1e-12) or not np.allclose(np.diag(correlation), 1.0, atol=1e-12):
        raise ValueError("OptionReg路径MC相关矩阵维度、对称性和对角线必须有效")
    eigenvalues = np.linalg.eigvalsh(correlation)
    if eigenvalues.min() < -1e-12:
        raise ValueError("OptionReg路径MC相关矩阵必须半正定")
    return spots, volatilities, dividend_yields, correlation


def _contract_with_maturity(
    contract: Any,
    maturity_years: float,
    *,
    trading_sessions: tuple[str, ...] | list[str],
    as_of: date,
    remaining_years: float,
) -> Any:
    """Keep count-based terms aligned with actual selected calendar dates."""
    terms = {**contract.terms, "T": maturity_years}
    if contract.terms.get("n_obs") is not None:
        from modules.pricer.observation_schedule import actual_n_obs, bind_accumulator_remaining_count

        remaining_observations = actual_n_obs(
            contract,
            sessions=trading_sessions,
            as_of=as_of,
            remaining_years=remaining_years,
        )
        if contract.product_id == "7.1" and "Q_acc" in contract.terms.get("monitor", {}):
            monitor = dict(terms.get("monitor", {}))
            monitor["Q_acc"] = bind_accumulator_remaining_count(
                str(monitor["Q_acc"]), remaining_observations,
            )
            terms["monitor"] = monitor
        else:
            terms["n_obs"] = remaining_observations
    return replace(contract, terms=terms, contract_fingerprint="")


def _raw_path_values(
    instrument: OptionRegPathOption,
    market: MarketState,
    config: ValuationConfig,
    valuation_state: ValuationState | None = None,
) -> np.ndarray:
    if config.method is not PricingMethod.MONTE_CARLO_CPU:
        raise ValueError("OptionReg路径结构只支持MONTE_CARLO_CPU")
    contract = _contract_for_state(
        instrument.resolved_contract,
        valuation_state,
        instrument.trading_sessions,
    )
    dates, relative_times = _session_dates(instrument, market, valuation_state)
    elapsed = _elapsed_years(valuation_state)
    times = relative_times + elapsed
    steps = len(dates) - 1
    source = config.monte_carlo.random_source
    normals = source.load(config.paths, steps * len(contract.underlyings))
    spots, volatilities, dividend_yields, correlation = _asset_inputs(instrument, market)
    asset_count = len(spots)
    if asset_count > 1:
        draws = normals.reshape(config.paths, steps, asset_count)
        eigenvalues, eigenvectors = np.linalg.eigh(correlation)
        draws = draws @ ((eigenvectors * np.sqrt(np.clip(eigenvalues, 0.0, None))) @ eigenvectors.T).T
    else:
        draws = normals[:, :, None]
    dt = np.diff(relative_times)
    carry = market.risk_free_rate - dividend_yields if market.carry is None else market.carry
    increments = (
        (carry - 0.5 * volatilities ** 2)[None, None, :] * dt[None, :, None]
        + volatilities[None, None, :] * np.sqrt(dt)[None, :, None] * draws
    )
    paths = np.empty((config.paths, steps + 1, asset_count), dtype=float)
    # PricePath holds actual market prices. The shared OptionReg interpreter
    # applies the contract-reference normalization exactly once, so its
    # bump-and-revalue Greeks are already in actual-price coordinates.
    paths[:, 0, :] = spots
    paths[:, 1:, :] = spots * np.exp(np.cumsum(increments, axis=1))
    discounted = np.empty(config.paths, dtype=float)
    # ``runtime`` is an OptionHelper integration dependency, not a dependency
    # of the frozen nine-structure base.  Import it only when this explicit
    # OPTIONREG_PATH route has been selected.
    from runtime.contracts.contract_engine import PricePath, evaluate_contract
    for index, values in enumerate(paths):
        settlement = evaluate_contract(
            contract,
            PricePath.from_values(values, times=times, dates=dates, asset_ids=contract.underlyings),
        )
        discounted[index] = sum(
            flow.amount * math.exp(-market.risk_free_rate * max(0.0, flow.time - elapsed))
            for flow in settlement.cashflows
        )
    return discounted


def _price_points(
    instrument: OptionRegPathOption,
    market: MarketState,
    config: ValuationConfig,
    valuation_state: ValuationState | None = None,
) -> tuple[float, np.ndarray]:
    raw_values = _raw_path_values(instrument, market, config, valuation_state)
    basis = instrument.basis
    if basis.cashflow_scale is None:
        # Products without N/Nvar/Nvega settle directly in the shared
        # dimensionless contract-point semantics.  S0Raw has already been
        # consumed by the Core path normalization and must not become a
        # surrogate money denominator here.
        return float(raw_values.mean()), raw_values
    if basis.cashflow_scale_kind == "variance_notional":
        return float(raw_values.mean() / basis.cashflow_scale), raw_values
    return float(raw_values.mean() / basis.cashflow_scale * 100.0), raw_values


def price_optionreg_path_monte_carlo(
    instrument: OptionRegPathOption,
    market: MarketState,
    config: ValuationConfig,
    valuation_state: ValuationState | None = None,
) -> PricingResult:
    """Price OptionReg cashflow paths and calculate CRN bump-and-revalue Greeks."""
    if valuation_state is not None and valuation_state.knocked_out:
        raise ValueError("已终止合同必须提供历史结算现金流，不能重新模拟")
    priced_sessions, _ = _session_dates(instrument, market, valuation_state)
    base_points, raw_values = _price_points(instrument, market, config, valuation_state)

    def price_market(local_market: MarketState) -> float:
        return _price_points(instrument, local_market, config, valuation_state)[0]

    def theta_roll(convention) -> ThetaRollValue:
        maturity = float(instrument.resolved_contract.terms["T"])
        rolled_maturity = maturity - convention.theta_calendar_day_shift / 365.0
        if rolled_maturity <= 0.0:
            raise ThetaNotApplicableError("合同剩余期限不足以计算Theta")
        rolled_contract = _contract_with_maturity(
            instrument.resolved_contract,
            rolled_maturity,
            trading_sessions=instrument.trading_sessions,
            as_of=market.as_of,
            remaining_years=rolled_maturity - _elapsed_years(valuation_state),
        )
        rolled_points, _ = _price_points(
            replace(instrument, resolved_contract=rolled_contract), market, config, valuation_state
        )
        return ThetaRollValue(
            price_points_100=rolled_points,
            calendar_day_shift=convention.theta_calendar_day_shift,
            trading_day_shift=convention.theta_trading_day_shift,
            description="OptionReg离散路径同随机源缩短一个自然日重估",
        )

    requested_greeks = dict(config.diagnostics).get("requested_greeks")
    if requested_greeks is not None and not isinstance(requested_greeks, tuple):
        raise ValueError("requested_greeks必须为内部冻结tuple")
    greeks, extended, risk_diagnostics = calculate_standard_greeks(
        base_price_points_100=base_points,
        market=market,
        config=config,
        basis=instrument.basis,
        price_market=price_market,
        theta_roll=theta_roll,
        requested_greeks=requested_greeks,
    )
    converted = convert_points_100(base_points, instrument.basis)
    if len(raw_values) <= 1:
        standard_error_points = 0.0
    elif instrument.basis.cashflow_scale is None:
        standard_error_points = float(raw_values.std(ddof=1) / math.sqrt(len(raw_values)))
    elif instrument.basis.cashflow_scale_kind == "variance_notional":
        standard_error_points = float(
            raw_values.std(ddof=1) / math.sqrt(len(raw_values))
            / instrument.basis.cashflow_scale
        )
    else:
        standard_error_points = float(
            raw_values.std(ddof=1) / math.sqrt(len(raw_values))
            / instrument.basis.cashflow_scale * 100.0
        )
    random_info = config.monte_carlo.random_source.info
    return PricingResult(
        pv_amount=converted.pv_amount,
        pv_percent=converted.pv_percent,
        pv_points_100=converted.pv_points_100,
        currency=converted.currency,
        greeks=greeks,
        extended_greeks=extended,
        method=PricingMethod.MONTE_CARLO_CPU,
        implementation_id=_IMPLEMENTATION_ID,
        warnings=(*converted.warnings,),
        diagnostics={
            "standard_error_points_100": standard_error_points,
            "paths": len(raw_values),
            "steps": len(priced_sessions) - 1,
            "asset_count": len(instrument.resolved_contract.underlyings),
            "path_coordinate": "actual market price; shared interpreter normalizes once",
            "spot_risk_factor": "single_asset" if len(instrument.resolved_contract.underlyings) == 1 else "parallel_proportional_all_assets",
            "shared_contract_interpreter": "runtime.contracts.contract_engine.evaluate_contract",
            **({
                "calendar": {
                    "calendar_id": instrument.calendar_id,
                    "calendar_revision": instrument.calendar_revision,
                    "session_start": priced_sessions[0].isoformat(),
                    "session_end": priced_sessions[-1].isoformat(),
                    "session_count": len(priced_sessions),
                    "time_basis": "ACT/365 from injected trading sessions",
                },
            } if instrument.calendar_id else {
                "path_grid": {
                    "mode": "valuation_and_contractual_maturity_endpoints",
                    "start": priced_sessions[0].isoformat(),
                    "end": priced_sessions[-1].isoformat(),
                    "time_basis": "ACT/365 contractual maturity",
                },
            }),
            "random_source": {
                "sha256": random_info.sha256,
                "seed": random_info.seed,
                "requested_seed": config.seed,
                "origin": random_info.origin,
                "shape": list(random_info.shape),
                "dtype": random_info.dtype,
                "source_path": random_info.path,
                "paths": config.paths,
            },
            "path_pv_sha256_float64": hashlib.sha256(np.ascontiguousarray(raw_values).tobytes()).hexdigest(),
            "valuation_state_applied": {
                "knocked_in": bool(valuation_state and valuation_state.knocked_in),
                "observation_stage": None if valuation_state is None else valuation_state.observation_stage,
                "accumulated_count": 0 if valuation_state is None else valuation_state.accumulated_count,
                "accumulated_quantity": 0.0 if valuation_state is None else valuation_state.accumulated_quantity,
                "elapsed_years": _elapsed_years(valuation_state),
                "realized_cashflows_included": False,
            },
            **risk_diagnostics,
        },
        engine_raw={"discounted_cashflow_mean": float(raw_values.mean()), "discounted_cashflow_std": float(raw_values.std(ddof=1)) if len(raw_values) > 1 else 0.0},
    )


__all__ = ("price_optionreg_path_monte_carlo",)


def _elapsed_years(valuation_state: ValuationState | None) -> float:
    return 0.0 if valuation_state is None else (valuation_state.calendar_day - 1) / 365.0




def _contract_for_state(
    contract: Any,
    valuation_state: ValuationState | None,
    trading_sessions: tuple[str, ...] | list[str] | None = None,
) -> Any:
    """将已发生KI事实注入剩余期monitor，不重放或伪造历史价格路径。"""
    if valuation_state is None:
        return contract
    terms = dict(contract.terms)
    if trading_sessions:
        # The shared interpreter restarts ordinals at one on the remaining
        # path. Rebase every ordinal term from the same injected sessions;
        # never infer observations from weekdays or elapsed calendar days.
        selectors = {
            "Llock": "O_KO",
            "coupon_switch_observation": "O_KO",
            "ko_barrier_schedule": "O_KO",
            "hedge_schedule": "Ohedge",
        }
        for key, selector_key in selectors.items():
            completed = _completed_selector_observations(
                contract,
                valuation_state,
                trading_sessions,
                selector_key,
            )
            value = terms.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                terms[key] = max(0, value - completed)
            elif isinstance(value, (tuple, list)):
                terms[key] = [
                    (int(item[0]) - completed, item[1])
                    for item in value
                    if isinstance(item, (tuple, list)) and len(item) == 2 and int(item[0]) > completed
                ]
    monitor = dict(contract.terms.get("monitor", {}))
    if valuation_state.knocked_in:
        if "tau_in" not in monitor:
            raise ValueError("合同状态声明已KI，但该结构没有tau_in监测语义")
        monitor["tau_in"] = "0.0"
    if valuation_state.accumulated_quantity and "Q_acc" in monitor:
        monitor["Q_acc"] = f"({monitor['Q_acc']}) + {valuation_state.accumulated_quantity}"
    if valuation_state.accumulated_count and "n_coupon" in monitor:
        monitor["n_coupon"] = f"({monitor['n_coupon']}) + {valuation_state.accumulated_count}"
    if monitor == dict(contract.terms.get("monitor", {})) and terms == dict(contract.terms):
        return contract
    return replace(contract, terms={**terms, "monitor": monitor}, contract_fingerprint="")


def _completed_selector_observations(
    contract: Any,
    valuation_state: ValuationState,
    trading_sessions: tuple[str, ...] | list[str],
    selector_key: str,
) -> int:
    start_value = contract.identity.get("contract_start_date")
    selector = str(contract.terms.get(selector_key, ""))
    if start_value is None or not selector:
        return 0
    start = date.fromisoformat(str(start_value))
    as_of = start + timedelta(days=max(0, valuation_state.calendar_day - 1))
    sessions = [
        date.fromisoformat(str(value))
        for value in trading_sessions
        if start <= date.fromisoformat(str(value)) <= as_of
    ]
    if selector == "daily":
        return len(sessions)
    if selector.startswith("monthly_"):
        all_sessions = [date.fromisoformat(str(value)) for value in trading_sessions if date.fromisoformat(str(value)) >= start]
        grouped: dict[tuple[int, int], list[date]] = {}
        for session in all_sessions:
            grouped.setdefault((session.year, session.month), []).append(session)
        selected: list[date] = []
        for month_sessions in grouped.values():
            if selector == "monthly_last":
                selected.append(month_sessions[-1])
            else:
                try:
                    ordinal = int(selector.removeprefix("monthly_"))
                except ValueError:
                    return 0
                if 1 <= ordinal <= len(month_sessions):
                    selected.append(month_sessions[ordinal - 1])
        return sum(start <= value <= as_of for value in selected)
    return 0
