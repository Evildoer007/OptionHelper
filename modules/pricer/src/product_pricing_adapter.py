"""ResolvedContract到Pricer内部统一数值入口的唯一产品适配边界。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Mapping

from runtime.contracts.contract_api import ResolvedContract

from .config import PricingConfig
from .calendar_policy import requires_future_trading_calendar
from .greeks import add_time_zero_cashflow, real_spot_greeks, scale_result
from .model_router import resolve_route
from .observation_schedule import actual_n_obs, bind_accumulator_remaining_count
from .observed_state import ObservedContractState
from .engines.pricing_core.engine.derivatives.results import GreekValue, PricingResult
from .engines.pricing_core.main import price_option


class ProductNotAvailable(ValueError):
    """冻结合同不能无歧义映射到正式基座时的显式结果。"""


@dataclass(frozen=True)
class ProductMapping:
    family: str
    structure: str
    status: str
    reason: str | None = None


_EXACT_MAPPINGS = {
    "1.1": ProductMapping("VANILLA", "EUROPEAN_VANILLA", "supported"),
    "1.2": ProductMapping("VANILLA", "EUROPEAN_VANILLA", "supported"),
    **{
        product_id: ProductMapping("AIRBAG", "AIRBAG", "supported")
        for product_id in ("2.1", "2.2", "2.3", "2.4", "3.1", "3.2", "3.3", "3.4")
    },
}

def product_mapping(product_id: str) -> ProductMapping:
    """OptionReg产品的唯一正式基座归属。"""
    product_id = str(product_id)
    if product_id in _EXACT_MAPPINGS:
        return _EXACT_MAPPINGS[product_id]
    route = resolve_route(product_id, ("monte_carlo",), "monte_carlo")
    if route is not None and route.capability.adapter == "optionreg_path":
        return ProductMapping("OPTIONREG", "OPTIONREG_PATH", "supported")
    return ProductMapping("UNCLASSIFIED", "UNCLASSIFIED", "not_available", "产品未登记为OptionReg可执行MC结构")


class ProductPricingAdapter:
    """唯一适配器：只编译参数并调用正式``main.price_option``入口。

    简单终值产品保留已有闭式路由；其他entry_status=True产品统一编译成
    ``OPTIONREG_PATH``并由同一离散路径、monitor、condition、cases/cash
    解释器估值。
    """

    _EXACT_PRODUCT_IDS = tuple(_EXACT_MAPPINGS)

    def __init__(
        self,
        contract: ResolvedContract,
        config: PricingConfig,
        market_snapshot: Mapping[str, Any] | None,
        observed_state: ObservedContractState,
    ) -> None:
        self.contract = contract
        self.config = config
        self.market_snapshot = dict(market_snapshot or {})
        self.observed_state = observed_state
        if observed_state.knocked_out:
            raise ProductNotAvailable("合同已敲出并终止；已实现结算现金流只审计，不重新模拟")
        if (
            contract.product_id == "7.1"
            and observed_state.accumulated_count > 0
            and observed_state.accumulated_quantity <= 0.0
        ):
            raise ProductNotAvailable("存续累购必须提供已累计数量accumulated_quantity；观察次数不能代替数量")
        allowed = tuple(str(value) for value in contract.terms.get("pricing_methods", ()))
        self.route = resolve_route(contract.product_id, allowed, config.model_method)
        mapping = product_mapping(contract.product_id)
        if mapping.status != "supported":
            raise ProductNotAvailable(mapping.reason or _unavailable_reason(contract, allowed, config.model_method))
        if self.route is None:
            raise ProductNotAvailable(_unavailable_reason(contract, allowed, config.model_method))
        if self.route.capability.adapter not in {"european_vanilla", "european_portfolio", "optionreg_path"}:
            raise ProductNotAvailable(f"{contract.product_id}没有已验证的{self.route.capability.adapter}参数适配")
        self.trading_calendar = _validated_trading_calendar(
            self.market_snapshot.get("trading_calendar"),
            as_of=_as_of(self.config.valuation_date),
            demo_mode=self.config.demo_mode,
        ) if self.requires_trading_calendar else None
        if self.route.capability.adapter == "european_vanilla":
            observed_state.validate_european_vanilla()
        if self.method == "monte_carlo":
            if self.config.path_count is None:
                raise ProductNotAvailable("Monte Carlo必须显式提供path_count")
            start_value = contract.identity.get("contract_start_date")
            observed_state.validate_path_events(
                contract_start_date=None if start_value is None else str(start_value),
                path_dependent=self.requires_trading_calendar,
                requires_accumulated_count="n_coupon" in contract.terms.get("monitor", {}),
                requires_accumulated_quantity="Q_acc" in contract.terms.get("monitor", {}),
                unsupported_midlife_aggregate="n_in" in contract.terms.get("monitor", {}),
            )
            if _is_midlife(observed_state, start_value) and contract.product_id == "9.4":
                raise ProductNotAvailable("存续方差互换缺少估值日前已实现方差，不能仅模拟剩余路径")
            if observed_state.occurred_events:
                history_sessions = _verified_state_sessions(self.market_snapshot, self.trading_calendar)
                observed_state.validate_observation_bounds(
                    contract_start_date=str(start_value),
                    contract_terms=contract.terms,
                    trading_sessions=history_sessions,
                )
        if self.method != "monte_carlo" and len(contract.underlyings) != 1:
            raise ProductNotAvailable("该闭式结构仅支持单标的")
        self.asset = contract.underlyings[0]
        references = contract.identity.get("reference_prices")
        if not isinstance(references, Mapping) or set(references) != set(contract.underlyings):
            raise ProductNotAvailable("定价必须提供合同起始参考价reference_prices")
        self.reference_price = _number(references[self.asset], "reference_prices")

    @property
    def family(self) -> str:
        return "OPTIONREG" if self.method == "monte_carlo" else self.route.capability.family

    @property
    def structure(self) -> str:
        return "OPTIONREG_PATH" if self.method == "monte_carlo" else self.route.capability.structure

    @property
    def method(self) -> str:
        return self.route.method

    @property
    def requires_trading_calendar(self) -> bool:
        """Use the one formal calendar policy for every adapter reprice."""
        return requires_future_trading_calendar(
            self.contract.product_id,
            self.contract.terms,
            self.method,
        )

    def reprice(self, *, spot: float | None = None, volatility: float | None = None, risk_free_rate: float | None = None, maturity_years: float | None = None, risk_greeks: frozenset[str] | None = None) -> PricingResult:
        """同一基座入口的受控重估；供PV、Greek和风险网格共同使用。"""
        parameters = self._parameters(
            spot=self._spot() if spot is None else spot,
            volatility=self._volatility() if volatility is None else volatility,
            risk_free_rate=float(self.config.risk_free_rate) if risk_free_rate is None else risk_free_rate,
            maturity_years=self._maturity() if maturity_years is None else maturity_years,
            risk_greeks=risk_greeks,
        )
        engine_method = "MONTE_CARLO_CPU" if self.method == "monte_carlo" else (
            "STATIC_REPLICATION" if self.route.capability.adapter == "european_portfolio" else "BLACK_SCHOLES"
        )
        run = price_option(self.family, self.structure, parameters, engine_method, output="NONE")
        result = _from_run(run)
        if self.method != "monte_carlo":
            quantity = (
                float(self.contract.terms["n_C"] if self.contract.product_id == "1.1" else self.contract.terms["n_P"])
                if self.route.capability.adapter == "european_vanilla"
                else 1.0
            )
            result = scale_result(result, quantity)
            result = add_time_zero_cashflow(
                result,
                _initial_cashflow(self.contract, quantity),
                _cashflow_basis(self.contract, self.reference_price)["cashflow_scale"],
            )
            result = real_spot_greeks(result, self.reference_price)
        result = _apply_value_basis(result, self.contract.product_id)
        # ``reprice`` is an internal base/Greek/risk-grid primitive. It records
        # neither a public precision classification nor a quotation decision.
        return replace(
            result,
            precision_status="not_assessed",
            quote_eligible=False,
            path_count=int(self.config.path_count) if self.method == "monte_carlo" else None,
        )

    def _parameters(self, *, spot: float, volatility: float, risk_free_rate: float, maturity_years: float, risk_greeks: frozenset[str] | None = None) -> dict[str, Any]:
        if spot <= 0.0 or volatility <= 0.0 or maturity_years <= 0.0:
            raise ValueError("受控重估的spot、volatility和maturity_years必须为正数")
        config = {"greek_bumps": {
            "spot_relative_bump": float(self.config.greek_bumps["spot"]),
            "volatility_absolute_bump": float(self.config.greek_bumps["volatility"]),
            "risk_free_rate_absolute_bump": float(self.config.greek_bumps["rate"]),
        }}
        if self.method == "monte_carlo":
            config.update({"paths": int(self.config.path_count), "seed": int(self.config.random_seed)})
            if risk_greeks is not None:
                config["diagnostics"] = {"requested_greeks": tuple(sorted(risk_greeks))}
        basis = _cashflow_basis(self.contract, self.reference_price)
        if self.method == "monte_carlo":
            contract = {
                "resolved_contract": _contract_with_maturity(
                self.contract,
                float(maturity_years) + self._elapsed_years(),
                remaining_years=float(maturity_years),
                trading_calendar=self.trading_calendar,
                as_of=_as_of(self.config.valuation_date),
                completed_observations=self._completed_accumulator_observations(),
                ),
                "basis": basis,
                "asset_spots": [_asset_number(self.config.spot, asset, "spot") for asset in self.contract.underlyings],
                "asset_volatilities": [self._asset_volatility(asset) for asset in self.contract.underlyings],
                "asset_dividend_yields": [_asset_number(self.config.dividend_yield, asset, "dividend_yield") for asset in self.contract.underlyings],
                "correlation": self.config.correlation,
                "trading_sessions": [] if self.trading_calendar is None else list(self.trading_calendar["sessions"]),
                "calendar_id": "" if self.trading_calendar is None else self.trading_calendar["calendar_id"],
                "calendar_revision": "" if self.trading_calendar is None else self.trading_calendar["calendar_revision"],
            }
        elif self.route.capability.adapter == "european_vanilla":
            direction = "CALL" if self.contract.product_id == "1.1" else "PUT"
            contract = {
                "strike": float(self.contract.terms["K"]),
                "maturity_years": float(maturity_years),
                "call_put": direction,
                "basis": basis,
            }
        else:
            contract = {
                "legs": [
                    {"weight": weight, "structure": "EUROPEAN_VANILLA", "method": "BLACK_SCHOLES", "label": label,
                     "contract": {"strike": strike, "maturity_years": float(maturity_years), "call_put": call_put, "basis": basis}}
                    for label, weight, call_put, strike in _portfolio_legs(self.contract)
                ],
                "basis": basis,
            }
        return {
            "contract": contract,
            "market": {
                "as_of": _as_of(self.config.valuation_date),
                "spot": spot if self.method == "monte_carlo" else spot / self.reference_price * float(self.contract.terms.get("S0", 100.0)),
                "volatility": float(volatility),
                "risk_free_rate": float(risk_free_rate),
                "dividend_yield": _asset_number(self.config.dividend_yield, self.asset, "dividend_yield"),
                "source": str(self.market_snapshot.get("source_ref") or self.market_snapshot.get("source") or "pricing_config"),
            },
            "config": config,
            "valuation_state": self.observed_state.valuation_state(
                contract_start_date=str(self.contract.identity.get("contract_start_date"))
            ) if self.method == "monte_carlo" else {},
        }

    def _spot(self) -> float:
        return _asset_number(self.config.spot, self.asset, "spot")

    def _volatility(self) -> float:
        value = self.config.volatility_override if self.config.volatility_override is not None else self.config.historical_volatility
        return _asset_number(value, self.asset, "volatility")

    def _asset_volatility(self, asset: str) -> float:
        value = self.config.volatility_override if self.config.volatility_override is not None else self.config.historical_volatility
        return _asset_number(value, asset, "volatility")

    def _maturity(self) -> float:
        return float(self.config.time_to_maturity if self.config.time_to_maturity is not None else self.contract.terms["T"])

    def _elapsed_years(self) -> float:
        if self.method != "monte_carlo":
            return 0.0
        state = self.observed_state.valuation_state(
            contract_start_date=str(self.contract.identity.get("contract_start_date"))
        )
        return (int(state["calendar_day"]) - 1) / 365.0

    def _completed_accumulator_observations(self) -> int:
        start_value = self.contract.identity.get("contract_start_date")
        if self.contract.product_id != "7.1" or not _is_midlife(self.observed_state, start_value):
            return 0
        sessions = _verified_state_sessions(self.market_snapshot, self.trading_calendar)
        return self.observed_state.completed_observation_count(
            contract_start_date=str(start_value),
            contract_terms=self.contract.terms,
            monitor_key="Q_acc",
            trading_sessions=sessions,
        )


def _from_run(run: Any) -> PricingResult:
    def greek(value: Any) -> GreekValue:
        return GreekValue(
            value=value.value, unit=value.unit, bump=value.bump, difference=value.difference,
            time_basis=value.time_basis, bump_details=dict(value.bump_details or {}),
            pv_amount_value=value.pv_amount_value, pv_amount_unit=value.pv_amount_unit,
            pv_percent_value=value.pv_percent_value, pv_percent_unit=value.pv_percent_unit,
            pv_points_100_value=value.pv_points_100_value, pv_points_100_unit=value.pv_points_100_unit,
            status=value.status.value.lower(),
        )
    return PricingResult(
        pv_amount=run.result.pv_amount, pv_percent=run.result.pv_percent,
        pv_points_100=run.result.pv_points_100, currency=run.result.currency,
        greeks={name.lower(): greek(value) for name, value in run.result.greeks.items()},
        extended_greeks={name.casefold().replace(" ", "_"): greek(value) for name, value in run.result.extended_risks.items()},
        method=run.result.method, implementation_id=run.result.implementation_id, warnings=run.result.warnings,
        diagnostics={**run.result.diagnostics, "pricing_run_id": run.run_id, "route_id": run.route_id, "request_fingerprint": run.request_fingerprint},
        engine_raw=run.result.engine_raw,
        standard_error=None if run.result.diagnostics.get("standard_error_points_100") is None else float(run.result.diagnostics["standard_error_points_100"]),
        standard_error_points_100=(
            None if run.result.diagnostics.get("standard_error_points_100") is None
            else float(run.result.diagnostics["standard_error_points_100"])
        ),
        standard_error_percent=(
            None if run.result.diagnostics.get("standard_error_points_100") is None
            else float(run.result.diagnostics["standard_error_points_100"]) / 100.0
        ),
    )


def _cashflow_basis(contract: ResolvedContract, reference_price: float) -> dict[str, Any]:
    """Build the only scale used to project a 100-point value into money.

    ``reference_price_basis`` is never a notional.  A contract without an
    explicit cashflow scale settles directly in normalized contract points:
    it deliberately has no private currency projection rather than inventing
    one from the start reference price.
    """
    terms = contract.terms
    if "N" in terms:
        scale = float(terms["N"])
        kind = "contract_notional"
    elif "Nvar" in terms:
        scale = float(terms["Nvar"])
        kind = "variance_notional"
    elif "Nvega" in terms:
        scale = float(terms["Nvega"])
        kind = "vega_notional"
    else:
        scale = None
        kind = "normalized_contract_points"
    return {
        "reference_price_basis": float(reference_price),
        "cashflow_scale": scale,
        "cashflow_scale_kind": kind,
        "currency": contract.currency,
    }


def _apply_value_basis(result: PricingResult, product_id: str) -> PricingResult:
    """Mark the only product whose 100 points are variance, not price points."""
    if product_id != "9.4":
        return replace(result, value_basis="pv_points_100")

    def variance_unit(unit: str | None) -> str | None:
        return None if unit is None else unit.replace("pv_points_100", "variance_points_100")

    def variance_percent_unit(unit: str | None) -> str | None:
        return None if unit is None else unit.replace("pv_percent", "variance_percent")

    def convert(value: GreekValue) -> GreekValue:
        return replace(
            value,
            unit=variance_unit(value.unit),
            pv_points_100_unit=variance_unit(value.pv_points_100_unit),
            pv_percent_unit=variance_percent_unit(value.pv_percent_unit),
        )

    return replace(
        result,
        value_basis="variance_points_100",
        greeks={name: convert(value) for name, value in result.greeks.items()},
        extended_greeks={name: convert(value) for name, value in result.extended_greeks.items()},
    )


def _unavailable_reason(contract: ResolvedContract, allowed: tuple[str, ...], requested_method: str) -> str:
    if contract.product_id in {"1.1", "1.2"}:
        return f"{contract.product_id}的正式香草基座只回归BLACK_SCHOLES；请求{requested_method}，OptionReg允许{list(allowed)}"
    if contract.product_id in _EXACT_MAPPINGS:
        return f"{contract.product_id}的显式欧式组合已回归BLACK_SCHOLES；请求{requested_method}，固定随机源组合MC尚未完成逐路径回归"
    return f"{contract.product_id}尚无同时满足OptionReg条款、显式观察日程和基座方法的适配器；不得用另一套模型替代"


def _portfolio_legs(contract: ResolvedContract) -> tuple[tuple[str, float, str, float], ...]:
    terms = contract.terms
    product_id = contract.product_id
    if product_id == "2.1": values = (("long_call_k1", 1.0, "CALL", "K1"), ("short_call_k2", -1.0, "CALL", "K2"))
    elif product_id == "2.2": values = (("long_put_k1", 1.0, "PUT", "K1"), ("short_put_k2", -1.0, "PUT", "K2"))
    elif product_id == "2.3": values = (("long_put_k2", 1.0, "PUT", "K2"), ("short_put_k1", -1.0, "PUT", "K1"))
    elif product_id == "2.4": values = (("short_call_k1", -1.0, "CALL", "K1"), ("long_call_k2", 1.0, "CALL", "K2"))
    elif product_id == "3.1": values = (("long_call", 1.0, "CALL", "K"), ("long_put", 1.0, "PUT", "K"))
    elif product_id == "3.2": values = (("long_put_kp", 1.0, "PUT", "Kp"), ("long_call_kc", 1.0, "CALL", "Kc"))
    elif product_id == "3.3": values = (("long_call_k1", 1.0, "CALL", "K1"), ("short_call_k2", -2.0, "CALL", "K2"), ("long_call_k3", 1.0, "CALL", "K3"))
    elif product_id == "3.4": values = (("long_call_k1", 1.0, "CALL", "K1"), ("short_call_k2", -1.0, "CALL", "K2"), ("short_call_k3", -1.0, "CALL", "K3"), ("long_call_k4", 1.0, "CALL", "K4"))
    else: raise ValueError(f"{product_id}没有欧式组合腿定义")
    return tuple((label, float(weight), call_put, float(terms[strike])) for label, weight, call_put, strike in values)


def _initial_cashflow(contract: ResolvedContract, quantity: float) -> float:
    """Contractual holder cashflow at time zero for closed-form structures."""
    if contract.product_id in {"1.1", "1.2"}:
        return -quantity * float(contract.terms.get("Pi_0", 0.0))
    if contract.product_id in _EXACT_MAPPINGS:
        return -float(contract.terms.get("P_net", 0.0))
    return 0.0


def _contract_with_maturity(
    contract: ResolvedContract,
    maturity_years: float,
    *,
    remaining_years: float | None = None,
    trading_calendar: Mapping[str, Any] | None = None,
    as_of: date | None = None,
    completed_observations: int = 0,
) -> ResolvedContract:
    """Create an immutable remaining-term view for controlled time revalue."""
    remaining_tenor = float(maturity_years if remaining_years is None else remaining_years)
    terms = {**contract.terms, "T": maturity_years}
    observations = contract.terms.get("n_obs")
    if observations is not None:
        if trading_calendar is None or as_of is None:
            raise ValueError("n_obs路径合同重估必须提供注入交易日历与估值日")
        session_values = trading_calendar.get("sessions")
        if isinstance(session_values, (str, bytes)) or not isinstance(session_values, (tuple, list)):
            raise ValueError("n_obs路径合同重估必须提供显式交易sessions")
        remaining_observations = actual_n_obs(
            contract,
            sessions=session_values,
            as_of=as_of,
            remaining_years=remaining_tenor,
        )
        terms["n_obs"] = remaining_observations + int(completed_observations)
        if contract.product_id == "7.1" and completed_observations:
            monitor = dict(terms.get("monitor", {}))
            monitor["Q_acc"] = bind_accumulator_remaining_count(
                str(monitor["Q_acc"]), remaining_observations,
            )
            terms["monitor"] = monitor
    hedge_schedule = terms.get("hedge_schedule")
    if trading_calendar is not None and as_of is not None and isinstance(hedge_schedule, (tuple, list)):
        session_values = trading_calendar.get("sessions")
        if isinstance(session_values, (tuple, list)):
            target = as_of.toordinal() + round(remaining_tenor * 365.0)
            available_months = _completed_monthly_observations(session_values, as_of, target)
            terms["hedge_schedule"] = [
                item for item in hedge_schedule
                if isinstance(item, (tuple, list)) and len(item) == 2 and int(item[0]) <= available_months
            ]
    return replace(contract, terms=terms, contract_fingerprint="")


def _completed_monthly_observations(sessions: object, as_of: date, target_ordinal: int) -> int:
    """只从注入sessions识别已经完整结束的观察月份。"""
    parsed = [date.fromisoformat(str(value)) for value in sessions]
    return sum(
        1
        for index, value in enumerate(parsed[:-1])
        if as_of <= value and value.toordinal() <= target_ordinal
        and (parsed[index + 1].year, parsed[index + 1].month) != (value.year, value.month)
    )


def _as_of(value: str | None) -> date:
    if value is None:
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("valuation_date必须为YYYY-MM-DD") from error


def _is_midlife(observed_state: ObservedContractState, start_value: object) -> bool:
    return (
        observed_state.lifecycle_status == "active"
        and observed_state.valuation_date is not None
        and start_value is not None
        and date.fromisoformat(observed_state.valuation_date) > date.fromisoformat(str(start_value))
    )


def _verified_state_sessions(
    market_snapshot: Mapping[str, Any],
    trading_calendar: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Combine verified history and future-calendar sessions for state bounds."""
    data_ref = market_snapshot.get("data_ref")
    coverage = data_ref.get("coverage") if isinstance(data_ref, Mapping) else None
    history = coverage.get("sessions") if isinstance(coverage, Mapping) else None
    future = trading_calendar.get("sessions") if isinstance(trading_calendar, Mapping) else None
    if isinstance(history, (str, bytes)) or not isinstance(history, (tuple, list)):
        raise ProductNotAvailable("存续路径状态校验需要已验证DataAssetRef.coverage.sessions")
    values = {str(value) for value in history}
    if isinstance(future, (tuple, list)):
        values.update(str(value) for value in future)
    return tuple(sorted(values))


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}必须为数值")
    result = float(value)
    if result <= 0.0:
        raise ValueError(f"{label}必须为正数")
    return result


def _asset_number(value: Any, asset: str, label: str) -> float:
    if isinstance(value, Mapping):
        if asset not in value:
            raise ValueError(f"{label}必须逐一覆盖合同标的")
        value = value[asset]
    result = _number(value, label) if label in {"spot", "volatility"} else float(value)
    return result


def _validated_trading_calendar(value: Any, *, as_of: date, demo_mode: bool) -> dict[str, Any]:
    """MC的日期只能来自受控资产，不以weekday或等距网格补造。"""
    if not isinstance(value, Mapping):
        raise ProductNotAvailable("离散路径MC必须提供独立trading-calendar资产的显式交易sessions")
    calendar_id = value.get("calendar_id")
    calendar_revision = value.get("calendar_revision")
    sessions = value.get("sessions")
    if not isinstance(calendar_id, str) or not calendar_id.strip() or not isinstance(calendar_revision, str) or not calendar_revision.strip():
        raise ProductNotAvailable("离散路径MC交易日历必须包含calendar_id和calendar_revision")
    if isinstance(sessions, (str, bytes)) or not isinstance(sessions, (tuple, list)):
        raise ProductNotAvailable("离散路径MC必须提供显式交易sessions，禁止weekday推断")
    parsed: list[date] = []
    try:
        parsed = [date.fromisoformat(str(item)) for item in sessions]
    except ValueError as error:
        raise ProductNotAvailable("离散路径MC交易sessions必须为YYYY-MM-DD") from error
    if not parsed or parsed != sorted(parsed) or len(set(parsed)) != len(parsed):
        raise ProductNotAvailable("离散路径MC交易sessions必须严格递增且不重复")
    if as_of not in parsed:
        raise ProductNotAvailable("估值日必须是注入交易日历中的真实session")
    verified_cn = bool(value.get("verified_cn_sessions"))
    source = str(value.get("source") or "")
    if not demo_mode and (not verified_cn or source != "host-injected"):
        raise ProductNotAvailable("正式离散路径MC必须注入经验证的中国交易sessions DataAssetRef")
    return {
        "calendar_id": calendar_id,
        "calendar_revision": calendar_revision,
        "sessions": tuple(item.isoformat() for item in parsed),
        "verified_cn_sessions": verified_cn,
        "source": source,
    }


__all__ = ("ProductMapping", "ProductNotAvailable", "ProductPricingAdapter", "product_mapping")
