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
_PATH_BATCH_SIZE = 2048
_CACHED_DRAW_LIMIT = 2_000_000
_PREPARED_SESSION_LIMIT = 4


def _observation_history_key(history: Any) -> Any:
    if history is None:
        return None
    return (
        tuple(history["dates"]), tuple(history["asset_ids"]),
        tuple(tuple(row) for row in history["close"]),
        tuple((name, tuple(tuple(row) for row in values)) for name, values in sorted(history["price_fields"].items())),
    )


def _remember_session(
    sessions: dict[tuple[Any, ...], "_PathPricingSession"],
    key: tuple[Any, ...],
    session: "_PathPricingSession",
) -> None:
    sessions[key] = session
    while len(sessions) > _PREPARED_SESSION_LIMIT:
        sessions.pop(next(iter(sessions)))


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
    if instrument.resolved_contract.terms.get("n_obs") is not None:
        # The independent count schedule owns the path horizon. T remains
        # the cashflow/settlement parameter and must not resample this grid.
        target = max(
            date.fromisoformat(str(day))
            for schedule in instrument.resolved_contract.resolved_schedules.values()
            for day in schedule["dates"]
        )
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
    """Use the adapter's immutable time view without rebinding n_obs."""
    from modules.pricer.product_pricing_adapter import _contract_with_maturity as time_view

    return time_view(
        contract, maturity_years, remaining_years=remaining_years,
        trading_calendar={"sessions": trading_sessions}, as_of=as_of,
    )


class _PathPricingSession:
    """Prepared contract, calendar and CRN draws shared by one risk revaluation."""

    def __init__(
        self,
        instrument: OptionRegPathOption,
        market: MarketState,
        config: ValuationConfig,
        valuation_state: ValuationState | None,
    ) -> None:
        if config.method is not PricingMethod.MONTE_CARLO_CPU:
            raise ValueError("OptionReg路径结构只支持MONTE_CARLO_CPU")
        self.instrument = instrument
        self.config = config
        self.valuation_state = valuation_state
        self.contract = _contract_for_state(
            instrument.resolved_contract,
            valuation_state,
            instrument.trading_sessions,
        )
        self.dates, self.relative_times = _session_dates(instrument, market, valuation_state)
        self.elapsed = _elapsed_years(valuation_state)
        self.times = self.relative_times + self.elapsed
        self.history = None if valuation_state is None else valuation_state.observation_history
        self.evaluation_dates = self.dates
        self.evaluation_times = self.times
        if self.history is not None:
            history_dates = tuple(date.fromisoformat(day) for day in self.history["dates"])
            if history_dates[-1] != market.as_of or tuple(self.history["asset_ids"]) != self.contract.underlyings:
                raise ValueError("历史观察路径必须覆盖同一标的并截止于当前估值日")
            start_date = date.fromisoformat(str(self.contract.identity["contract_start_date"]))
            expected_dates = tuple(
                date.fromisoformat(str(day)) for day in instrument.trading_sessions
                if start_date <= date.fromisoformat(str(day)) <= market.as_of
            )
            if history_dates != expected_dates:
                raise ValueError("历史观察路径必须逐日覆盖冻结交易日历，不能跳过已完成观察")
            self.evaluation_dates = history_dates + self.dates[1:]
            self.evaluation_times = np.asarray([(day - start_date).days / 365 for day in self.evaluation_dates])
        self.steps = len(self.dates) - 1
        _, _, _, correlation = _asset_inputs(instrument, market)
        self.asset_count = len(self.contract.underlyings)
        if self.asset_count > 1:
            eigenvalues, eigenvectors = np.linalg.eigh(correlation)
            self.correlation_root = (
                eigenvectors * np.sqrt(np.clip(eigenvalues, 0.0, None))
            ) @ eigenvectors.T
        else:
            self.correlation_root = None
        self.dt = np.diff(self.relative_times)
        self.sqrt_dt = np.sqrt(self.dt)
        # ``runtime`` remains an OptionHelper integration dependency.  Import
        # the public prepared evaluator only when the OPTIONREG_PATH plugin is
        # instantiated, so the frozen nine-structure base stays standalone.
        from runtime.contracts.contract_api import prepare_contract_evaluator
        self.prepared_evaluator = prepare_contract_evaluator(
            self.contract,
            times=self.evaluation_times,
            dates=self.evaluation_dates,
            asset_ids=self.contract.underlyings,
            valuation_date=self.evaluation_dates[0],
        )
        draw_count = config.paths * self.steps * self.asset_count
        self.cached_draws = None
        if draw_count <= _CACHED_DRAW_LIMIT:
            batches = tuple(self._draw_batches())
            self.cached_draws = (
                batches[0]
                if len(batches) == 1
                else np.concatenate(batches, axis=0)
            )

    def _draw_batches(self, *, batch_size: int = _PATH_BATCH_SIZE):
        source = self.config.monte_carlo.random_source
        for normals in source.iter_batches(
            self.config.paths,
            self.steps * self.asset_count,
            batch_size=batch_size,
        ):
            draws = normals.reshape(len(normals), self.steps, self.asset_count)
            if self.correlation_root is not None:
                draws = draws @ self.correlation_root.T
            yield draws

    def raw_values(self, market: MarketState) -> np.ndarray:
        return self.raw_values_many((market,))[0]

    def raw_values_many(self, markets: tuple[MarketState, ...]) -> tuple[np.ndarray, ...]:
        if not markets:
            return ()
        discounted = tuple(np.empty(self.config.paths, dtype=float) for _ in markets)
        start = 0
        per_market_batch = max(1, _PATH_BATCH_SIZE // len(markets))
        batches = (
            (
                self.cached_draws[index:index + per_market_batch]
                for index in range(0, len(self.cached_draws), per_market_batch)
            )
            if self.cached_draws is not None
            else self._draw_batches(batch_size=per_market_batch)
        )
        from runtime.contracts.contract_api import evaluate_contract_batch
        for draws in batches:
            count = len(draws)
            path_blocks = []
            for market in markets:
                spots, volatilities, dividend_yields, _ = _asset_inputs(self.instrument, market)
                carry = market.risk_free_rate - dividend_yields if market.carry is None else market.carry
                drift = (carry - 0.5 * volatilities ** 2)[None, None, :] * self.dt[None, :, None]
                diffusion_scale = volatilities[None, None, :] * self.sqrt_dt[None, :, None]
                paths = np.empty((count, self.steps + 1, self.asset_count), dtype=float)
                # PricePath holds actual market prices. The shared interpreter
                # applies reference normalization exactly once.
                paths[:, 0, :] = spots
                paths[:, 1:, :] = spots * np.exp(np.cumsum(drift + diffusion_scale * draws, axis=1))
                path_blocks.append(paths)
            combined_paths = np.concatenate(path_blocks, axis=0)
            if not np.isfinite(combined_paths).all() or np.any(combined_paths <= 0.0):
                raise ValueError("模拟价格路径必须为有限正数")
            price_fields = None
            if self.history is not None:
                future_paths = combined_paths[:, 1:, :]
                observed = np.asarray(self.history["close"], dtype=float)
                combined_paths = np.concatenate((np.broadcast_to(observed, (len(combined_paths), *observed.shape)), future_paths), axis=1)
                price_fields = {
                    name: np.concatenate((np.broadcast_to(np.asarray(values), (len(combined_paths), *observed.shape)), future_paths), axis=1)
                    for name, values in self.history["price_fields"].items()
                }
            settlements = evaluate_contract_batch(
                self.prepared_evaluator,
                combined_paths,
                batch_size=len(combined_paths),
                price_fields=price_fields,
            ).evaluations
            for market_index, market in enumerate(markets):
                block_start = market_index * count
                for offset, settlement in enumerate(settlements[block_start:block_start + count]):
                    discounted[market_index][start + offset] = sum(
                        flow.amount * math.exp(
                            -market.risk_free_rate * max(0.0, flow.time - self.elapsed)
                        )
                        for flow in settlement.cashflows
                        # CF0 is classified once by the lifecycle filter.
                        # Other settled flows must not enter remaining PV.
                        if self.history is None or flow.time == 0.0 or flow.time > self.elapsed + 1e-12
                    )
            start += count
        return discounted


def _raw_path_values(
    instrument: OptionRegPathOption,
    market: MarketState,
    config: ValuationConfig,
    valuation_state: ValuationState | None = None,
) -> np.ndarray:
    return _PathPricingSession(instrument, market, config, valuation_state).raw_values(market)


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


def _session_price_points(
    session: _PathPricingSession,
    market: MarketState,
) -> tuple[float, np.ndarray]:
    raw_values = session.raw_values(market)
    basis = session.instrument.basis
    if basis.cashflow_scale is None:
        return float(raw_values.mean()), raw_values
    if basis.cashflow_scale_kind == "variance_notional":
        return float(raw_values.mean() / basis.cashflow_scale), raw_values
    return float(raw_values.mean() / basis.cashflow_scale * 100.0), raw_values


def _session_price_points_many(
    session: _PathPricingSession,
    markets: tuple[MarketState, ...],
) -> tuple[float, ...]:
    raw_sets = session.raw_values_many(markets)
    basis = session.instrument.basis
    if basis.cashflow_scale is None:
        return tuple(float(values.mean()) for values in raw_sets)
    if basis.cashflow_scale_kind == "variance_notional":
        return tuple(float(values.mean() / basis.cashflow_scale) for values in raw_sets)
    return tuple(float(values.mean() / basis.cashflow_scale * 100.0) for values in raw_sets)


def price_optionreg_path_monte_carlo(
    instrument: OptionRegPathOption,
    market: MarketState,
    config: ValuationConfig,
    valuation_state: ValuationState | None = None,
    *,
    prepared_sessions: dict[tuple[Any, ...], _PathPricingSession] | None = None,
) -> PricingResult:
    """Price OptionReg cashflow paths and calculate CRN bump-and-revalue Greeks."""
    if valuation_state is not None and valuation_state.knocked_out:
        raise ValueError("已终止合同必须提供历史结算现金流，不能重新模拟")
    session_key = (
        float(instrument.resolved_contract.terms["T"]),
        market.as_of,
        config.paths,
        config.seed,
        config.monte_carlo.random_source.info.sha256,
        tuple(instrument.asset_spots),
        tuple(instrument.asset_volatilities),
        tuple(instrument.asset_dividend_yields),
        instrument.correlation,
        instrument.trading_sessions,
        instrument.calendar_id,
        instrument.calendar_revision,
        None if valuation_state is None else (
            valuation_state.trading_day,
            valuation_state.calendar_day,
            valuation_state.knocked_in,
            valuation_state.knocked_out,
            valuation_state.accumulated_count,
            valuation_state.accumulated_quantity,
            valuation_state.observation_stage,
            _observation_history_key(valuation_state.observation_history),
        ),
    )
    session = None if prepared_sessions is None else prepared_sessions.get(session_key)
    if session is None:
        session = _PathPricingSession(instrument, market, config, valuation_state)
        if prepared_sessions is not None:
            _remember_session(prepared_sessions, session_key, session)
    priced_sessions = session.dates
    base_points, raw_values = _session_price_points(session, market)

    def price_market(local_market: MarketState) -> float:
        return _session_price_points(session, local_market)[0]

    def price_markets(local_markets: tuple[MarketState, ...]) -> tuple[float, ...]:
        return _session_price_points_many(session, local_markets)

    def theta_roll(convention) -> ThetaRollValue:
        maturity = float(instrument.resolved_contract.terms["T"])
        rolled_maturity = maturity - convention.theta_calendar_day_shift / 365.0
        rolled_remaining = rolled_maturity - _elapsed_years(valuation_state)
        # T is measured from contract inception, not from this valuation.
        # Check the civil-day horizon used by the time view; cancellation
        # error after a long elapsed period must not create a zero-day roll.
        if round(rolled_remaining * 365.0) <= 0:
            raise ThetaNotApplicableError("合同剩余期限不足以计算Theta")
        if valuation_state is not None and valuation_state.observation_history is not None:
            from modules.pricer.product_pricing_adapter import _contract_with_maturity as rebuild_time_view
            rolled_contract = rebuild_time_view(
                instrument.resolved_contract, rolled_maturity,
                trading_calendar={"sessions": instrument.trading_sessions},
                as_of=market.as_of,
                remaining_years=rolled_remaining,
                replay_history=True,
            )
        else:
            rolled_contract = _contract_with_maturity(
                instrument.resolved_contract,
                rolled_maturity,
                trading_sessions=instrument.trading_sessions,
                as_of=market.as_of,
                remaining_years=rolled_remaining,
            )
        rolled_instrument = replace(instrument, resolved_contract=rolled_contract)
        if prepared_sessions is None:
            rolled_points, _ = _price_points(
                rolled_instrument, market, config, valuation_state
            )
        else:
            rolled_session_key = (
                rolled_maturity,
                *session_key[1:],
            )
            rolled_session = prepared_sessions.get(rolled_session_key)
            if rolled_session is None:
                rolled_session = _PathPricingSession(
                    rolled_instrument, market, config, valuation_state
                )
                _remember_session(prepared_sessions, rolled_session_key, rolled_session)
            rolled_points, _ = _session_price_points(rolled_session, market)
        # A time-risk view must not reprice the original upfront payment.
        # In particular, 8.16's annualized premium changes numerically when
        # its temporary T changes; only future contingent cashflows roll.
        from modules.pricer.cashflow_lifecycle import contractual_initial_cashflow

        initial_difference = (
            contractual_initial_cashflow(instrument.resolved_contract)
            - contractual_initial_cashflow(rolled_contract)
        )
        scale = instrument.basis.cashflow_scale
        rolled_points += initial_difference if scale is None else initial_difference / scale * 100.0
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
        price_markets=price_markets,
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
            "shared_contract_interpreter": "runtime.contracts.contract_api.evaluate_contract_batch",
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
    if valuation_state is None or valuation_state.observation_history is not None:
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
    return replace(
        contract,
        terms={**terms, "monitor": monitor},
        path_case_applicability=None,
    )


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
