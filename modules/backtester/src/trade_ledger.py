"""Backtester逐笔合同冻结、现金流账本与可审计TradeResult。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

import numpy as np
import pandas as pd

from runtime.contracts.contract_api import Cashflow as ContractCashflow
from runtime.contracts.contract_api import ResolvedContract, resolve_schedules
from runtime.contracts.contract_types import deep_thaw

from .historical_data import HistoricalData
from .impl.config import BacktestConfig
from .path_replay import ReplayedPath


@dataclass(frozen=True)
class HistoricalResolvedContract:
    """某一入场日冻结的合同事实，含共享解释器解析出的实际观察日期。"""

    trade_contract_id: str
    entry_date: str
    start_date: str
    end_date: str
    reference_prices: Mapping[str, float]
    reference_price_provenance: Mapping[str, Any]
    resolved_schedules: Mapping[str, Any]
    product_version: str
    template_contract_fingerprint: str
    trade_contract_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_contract_id": self.trade_contract_id,
            "entry_date": self.entry_date,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "reference_prices": dict(self.reference_prices),
            "reference_price_provenance": deep_thaw(self.reference_price_provenance),
            "resolved_schedules": deep_thaw(self.resolved_schedules),
            "product_version": self.product_version,
            "template_contract_fingerprint": self.template_contract_fingerprint,
            "trade_contract_fingerprint": self.trade_contract_fingerprint,
        }


@dataclass(frozen=True)
class TradeResult:
    """一笔完整历史合同的唯一账本事实。"""

    trade_id: str
    historical_contract: HistoricalResolvedContract
    exit_date: str
    actual_calendar_days: int
    actual_time_years: float
    entry_market_spots: Mapping[str, float]
    entry_normalized_spots: Mapping[str, float]
    settlement_market_spots: Mapping[str, float]
    underlying_performances: Mapping[str, float]
    path_id: int
    case_id: int
    settlement_type: str
    events: Mapping[str, Any]
    cashflows: tuple[Mapping[str, Any], ...]
    pnl: float
    gross_contract_return: float
    return_normalization: Mapping[str, str]
    terminal_performance: float | None
    entry_features: Mapping[str, Any]
    data_flags: Mapping[str, Any]
    limitations: tuple[str, ...] = ()

    @property
    def entry_date(self) -> str:
        return self.historical_contract.entry_date

    @property
    def trade_contract_fingerprint(self) -> str:
        return self.historical_contract.trade_contract_fingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "entry_date": self.entry_date,
            "exit_date": self.exit_date,
            "actual_calendar_days": self.actual_calendar_days,
            "actual_time_years": self.actual_time_years,
            "historical_resolved_contract": self.historical_contract.to_dict(),
            "entry_market_spots": dict(self.entry_market_spots),
            "entry_normalized_spots": dict(self.entry_normalized_spots),
            "settlement_market_spots": dict(self.settlement_market_spots),
            "underlying_performances": dict(self.underlying_performances),
            "path_id": self.path_id,
            "case_id": self.case_id,
            "settlement_type": self.settlement_type,
            "events": deep_thaw(self.events),
            "gross_contract_return": self.gross_contract_return,
            "gross_return_convention": {
                "basis": "contract_cashflow_before_external_costs",
                "display_unit": "percentage",
                "value_encoding": "decimal_ratio",
                "normalization": dict(self.return_normalization),
            },
            "client_net_return": {
                "status": "not_modelled",
                "value": None,
                "reasons": ["option_premium", "funding", "fees", "taxes", "hedging", "slippage_not_modelled"],
            },
            "client_net_pnl": {
                "status": "not_modelled",
                "value": None,
                "reasons": ["option_premium", "funding", "fees", "taxes", "hedging", "slippage_not_modelled"],
            },
            "terminal_performance": self.terminal_performance,
            "entry_features": deep_thaw(self.entry_features),
            "data_flags": deep_thaw(self.data_flags),
            "limitations": list(self.limitations),
        }

    def to_audit_dict(self) -> dict[str, Any]:
        """仅供ResultStore私有审计产物使用的原始现金流账本。"""
        return {
            **self.to_dict(),
            "cashflows": [dict(item) for item in self.cashflows],
            "audit_contract_cashflow_amount": self.pnl,
        }


def freeze_trade_contract(
    contract: ResolvedContract,
    *,
    entry_date: str,
    trading_dates: pd.DatetimeIndex,
    entry_spots: Mapping[str, float],
) -> tuple[ResolvedContract, HistoricalResolvedContract]:
    """以该笔真实交易日和参考价生成不可变ResolvedContract。"""
    identity = deep_thaw(contract.identity)
    identity.update({
        "contract_id": f"{identity['contract_id']}@{entry_date}",
        "contract_start_date": entry_date,
        "contract_end_date": _date_text(trading_dates[-1]),
        "reference_prices": dict(entry_spots),
        "reference_price_provenance": _reference_price_provenance(entry_spots),
    })
    schedules = _resolved_schedule_snapshot(contract, trading_dates)
    trade_contract = replace(
        contract,
        identity=identity,
        resolved_schedules=schedules,
        contract_fingerprint="",
    )
    historical = HistoricalResolvedContract(
        trade_contract_id=str(identity["contract_id"]),
        entry_date=entry_date,
        start_date=entry_date,
        end_date=_date_text(trading_dates[-1]),
        reference_prices=dict(entry_spots),
        reference_price_provenance=_reference_price_provenance(entry_spots),
        resolved_schedules=schedules,
        product_version=trade_contract.product_version,
        template_contract_fingerprint=contract.contract_fingerprint,
        trade_contract_fingerprint=trade_contract.contract_fingerprint,
    )
    return trade_contract, historical


def build_trade_result(
    *,
    trade_id: str,
    contract: ResolvedContract,
    historical_contract: HistoricalResolvedContract,
    replay: ReplayedPath,
    historical_data: HistoricalData,
    config: BacktestConfig,
    entry_features: Mapping[str, Any],
) -> TradeResult:
    """把共享解释器结果转为一笔可复算账本，不再解释产品公式。"""
    cashflows = tuple(_dated_cashflow(flow, replay.dates, replay.times) for flow in replay.outcome.cashflows)
    entry_spots = {asset: float(replay.values[0, index]) for index, asset in enumerate(contract.underlyings)}
    settlement_spots = {asset: float(replay.values[-1, index]) for index, asset in enumerate(contract.underlyings)}
    performances = {asset: settlement_spots[asset] / entry_spots[asset] - 1.0 for asset in contract.underlyings}
    pnl = float(sum(float(item["amount"]) for item in cashflows))
    gross_return, normalization = _gross_contract_return(contract, pnl)
    return TradeResult(
        trade_id=trade_id,
        historical_contract=historical_contract,
        exit_date=_date_text(replay.dates[-1]),
        actual_calendar_days=int((replay.dates[-1] - replay.dates[0]).days),
        actual_time_years=float((replay.dates[-1] - replay.dates[0]).days) / 365.0,
        entry_market_spots=entry_spots,
        entry_normalized_spots=_entry_normalized_spots(contract, entry_spots),
        settlement_market_spots=settlement_spots,
        underlying_performances=performances,
        path_id=replay.outcome.selected_path,
        case_id=replay.outcome.selected_case,
        settlement_type=f"path_{replay.outcome.selected_path + 1}_case_{replay.outcome.selected_case + 1}",
        events=_event_dates(replay.outcome.monitor_values, replay.dates, replay.times),
        cashflows=cashflows,
        pnl=pnl,
        gross_contract_return=gross_return,
        return_normalization=normalization,
        terminal_performance=min(performances.values()) if "S0Vec" in contract.terms else performances[contract.underlyings[0]],
        entry_features=entry_features,
        data_flags={
            "contract_price_field": historical_data.contract_price_field,
            "contract_adjustment": historical_data.contract_adjustment,
            "entry_hv_price_field": historical_data.hv_price_field,
            "entry_hv_adjustment": historical_data.hv_adjustment,
            "data_asset_id": historical_data.data_asset_ref["data_asset_id"],
            "content_hash": historical_data.data_asset_ref["content_hash"],
            "complete_tenor_requested": config.complete_tenor,
            "path_end_date": _date_text(replay.dates[-1]),
            "entry_reference": {
                "field": "close",
                "adjustment": "unadjusted",
                "source": "latest_available_close_proxy",
                "raw_spots": entry_spots,
            },
        },
        limitations=tuple(historical_data.limitations) + ("payment_calendar_not_exposed_by_shared_contract_core",),
    )


def _resolved_schedule_snapshot(contract: ResolvedContract, trading_dates: pd.DatetimeIndex) -> dict[str, Any]:
    actual = resolve_schedules(contract.terms, trading_dates)
    return {
        key: {
            "selector": deep_thaw(contract.terms[key]),
            "status": "resolved",
            "dates": [_date_text(value) for value in dates],
        }
        for key, dates in actual.items()
    }


def _entry_normalized_spots(contract: ResolvedContract, entry_spots: Mapping[str, float]) -> dict[str, float]:
    return {asset: 100.0 for asset in contract.underlyings} if "S0" in contract.terms or "S0Vec" in contract.terms else dict(entry_spots)


def _reference_price_provenance(entry_spots: Mapping[str, float]) -> dict[str, Any]:
    return {
        "role": "S0Raw_proxy",
        "field": "close",
        "adjustment": "unadjusted",
        "source": "latest_available_close_proxy",
        "raw_spots": dict(entry_spots),
        "freeze": "per_trade_at_entry",
    }


def _dated_cashflow(flow: ContractCashflow, dates: pd.DatetimeIndex, times: np.ndarray) -> dict[str, Any]:
    position = int(np.abs(times - float(flow.time)).argmin())
    return {"date": _date_text(dates[position]), "time": float(flow.time), "amount": float(flow.amount)}


def _event_dates(monitors: Mapping[str, Any], dates: pd.DatetimeIndex, times: np.ndarray) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in monitors.items():
        if isinstance(value, (int, float, np.number)) and not np.isfinite(float(value)):
            result[name] = None
        elif name.startswith("tau_") and isinstance(value, (int, float, np.number)):
            position = int(np.abs(times - float(value)).argmin())
            result[name] = {"time": float(value), "date": _date_text(dates[position])}
        else:
            result[name] = value.item() if isinstance(value, np.generic) else deep_thaw(value)
    return result


def _gross_contract_return(contract: ResolvedContract, pnl: float) -> tuple[float, dict[str, str]]:
    """将解释器现金流映射为无货币单位的每100合同单位收益。

    ``N``、``Nvar``等仅是合同公式的规模变量，而非用户可选择的收益率分母。
    没有规模变量的价格型合同本身已在内部100基准上结算；累购以首期约定数量
    乘100基准作为一个合同单位。每个路径都返回收益，不能因缺少``N``而失效。
    """
    terms = contract.terms
    if contract.product_id == "9.4":
        variance_notional = float(terms.get("Nvar", 0.0))
        if variance_notional > 0.0:
            return pnl / (variance_notional * 10_000.0), {
                "source": "variance_percentage_squared_contract_scale",
                "contract_base": "100",
            }
    for key, source in (("N", "declared_contract_scale"), ("Nvar", "declared_variance_contract_scale"), ("Nvega", "declared_vega_contract_scale")):
        value = float(terms.get(key, 0.0))
        if value > 0.0:
            return pnl / value, {"source": source, "contract_base": "100"}
    if contract.product_id == "7.1":
        quantity = float(terms.get("q", 0.0))
        observations = float(terms.get("n_obs", 0.0))
        if quantity > 0.0 and observations > 0.0:
            return pnl / (quantity * observations * 100.0), {
                "source": "accumulator_full_term_contractual_purchase_scale",
                "contract_base": "100",
            }
    return pnl / 100.0, {"source": "normalized_unit_contract", "contract_base": "100"}


def _date_text(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")
