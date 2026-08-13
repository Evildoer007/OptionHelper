"""Pricer唯一正式估值入口。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import Mapping

import pandas as pd

from .config import PricingConfig
from .market_resolver import market_snapshot_from_history
from .calendar_policy import requires_future_trading_calendar
from .models import (
    HistoricalData,
    PricingInput,
    TradingCalendarData,
    validate_market_data_asset,
    validate_trading_calendar_asset,
)
from .engines.pricing_core.optionhelper_core import PricingResult, price as _price


def price(pricing_input: PricingInput) -> PricingResult:
    """把冻结合同、配置、市场快照和已观测状态交给正式数值核心。"""
    if not isinstance(pricing_input, PricingInput):
        raise TypeError("price只接受PricingInput")
    if not isinstance(pricing_input.pricing_config, PricingConfig):
        raise TypeError("PricingInput.pricing_config必须是PricingConfig")
    if pricing_input.historical_data is not None and not isinstance(pricing_input.historical_data, HistoricalData):
        raise TypeError("PricingInput.historical_data必须是HistoricalData")
    if pricing_input.trading_calendar_data is not None and not isinstance(pricing_input.trading_calendar_data, TradingCalendarData):
        raise TypeError("PricingInput.trading_calendar_data必须是TradingCalendarData")
    if len(pricing_input.market_data_refs) > 1:
        raise ValueError("当前正式Pricer仅支持一个覆盖全部合同标的的market_data_ref")
    snapshot: Mapping[str, object] | None = None
    config = pricing_input.pricing_config
    historical = pricing_input.historical_data
    data_ref = pricing_input.market_data_refs[0] if pricing_input.market_data_refs else None
    calendar_data = pricing_input.trading_calendar_data
    calendar_ref = pricing_input.trading_calendar_ref
    trading_calendar: Mapping[str, object] | None = None
    if (calendar_data is None) != (calendar_ref is None):
        raise ValueError("TradingCalendarData必须与trading_calendar_ref同时提供")
    if _requires_future_trading_calendar(pricing_input.contract, config) and calendar_data is None:
        raise ValueError("路径型Pricer必须绑定独立trading-calendar交易日历")
    if calendar_data is not None and calendar_ref is not None:
        trading_calendar = validate_trading_calendar_asset(
            calendar_ref, calendar_data, pricing_input.contract.underlyings,
        )
        identity = pricing_input.contract.identity
        for field in ("calendar_id", "calendar_version"):
            if identity.get(field) != trading_calendar[field]:
                raise ValueError(f"冻结ResolvedContract.{field}与Host验证交易日历不一致")
    if historical is not None:
        if data_ref is not None:
            validate_market_data_asset(data_ref, historical, pricing_input.contract.underlyings)
        elif historical.storage_mode != "local-development":
            raise ValueError("Host注入HistoricalData必须同时提供已验证DataAssetRef")
        snapshot = market_snapshot_from_history(
            pd.DataFrame(historical.rows),
            pricing_input.contract.underlyings,
            valuation_date=config.valuation_date,
            hv_window=config.hv_window,
            risk_free_rate=config.risk_free_rate,
            dividend_yield=config.dividend_yield,
            trading_calendar=trading_calendar,
        )
        requested_valuation_date = str(config.valuation_date or snapshot["valuation_date"])
        market_as_of_date = str(snapshot["valuation_date"])
        effective_valuation_session = market_as_of_date
        effective_maturity_session: str | None = None
        if trading_calendar is not None:
            effective_valuation_session, effective_maturity_session = _calendar_sessions_for_contract(
                trading_calendar,
                requested_valuation_date=requested_valuation_date,
                contract=pricing_input.contract,
                configured_time_to_maturity=config.time_to_maturity,
                available_market_date=market_as_of_date,
            )
        snapshot = {
            **snapshot,
            # Keep the request, its market-data-effective trading session,
            # and the last usable market quote explicit.  When no current
            # close exists, effective valuation must fall back to the quote
            # date, otherwise path-state validation would invent unobserved
            # contract history.
            "valuation_date": effective_valuation_session,
            "requested_valuation_date": requested_valuation_date,
            "effective_valuation_session": effective_valuation_session,
            "effective_maturity_session": effective_maturity_session,
            "market_as_of_date": market_as_of_date,
            "source_ref": historical.source_ref,
            "data_lineage": {
                "content_hash": historical.content_hash,
                "schema_id": historical.schema_id,
                "storage_mode": historical.storage_mode,
                "asset_ids": list(historical.asset_ids),
            },
        }
        config = replace(
            config,
            valuation_date=effective_valuation_session,
            spot=config.spot if config.spot is not None else snapshot["spot"],
            # A verified market-history asset owns the HV estimate. Manual
            # model volatility is allowed only through volatility_override,
            # whose provenance is separately disclosed by the result.
            historical_volatility=snapshot["historical_volatility"],
        )
    elif data_ref is not None:
        raise ValueError("DataAssetRef必须与已加载HistoricalData一同传入Pricer")
    if data_ref is not None:
        # 保留协议对象的可审计字段，避免在正式链路中把DataAssetRef降格为任意dict。
        reference = {
            "data_asset_id": data_ref.data_asset_id,
            "storage_ref": data_ref.storage_ref,
            "content_hash": data_ref.content_hash,
            "schema_id": data_ref.schema_id,
            "asset_ids": list(data_ref.asset_ids),
            "normalized_fields": list(data_ref.normalized_fields),
            "coverage": dict(data_ref.coverage),
        }
        snapshot = {**dict(snapshot or {}), "data_ref": reference}
    if calendar_ref is not None:
        calendar_reference = {
            "data_asset_id": calendar_ref.data_asset_id,
            "storage_ref": calendar_ref.storage_ref,
            "content_hash": calendar_ref.content_hash,
            "schema_id": calendar_ref.schema_id,
            "asset_ids": list(calendar_ref.asset_ids),
            "coverage": dict(calendar_ref.coverage),
        }
        snapshot = {**dict(snapshot or {}), "trading_calendar_ref": calendar_reference}
    if config.time_to_maturity is None and config.valuation_date:
        start = pricing_input.contract.identity.get("contract_start_date")
        if start:
            elapsed_days = (date.fromisoformat(config.valuation_date) - date.fromisoformat(str(start))).days
            if elapsed_days < 0:
                raise ValueError("contract_start_date不得晚于估值日")
            remaining = float(pricing_input.contract.terms["T"]) - elapsed_days / 365.0
            if remaining <= 0:
                raise ValueError("合同在估值日已到期")
            config = replace(config, time_to_maturity=remaining)
    result = _price(
        pricing_input.contract,
        config,
        market_snapshot=snapshot,
        observed_contract_state=pricing_input.observed_contract_state,
    )
    if snapshot:
        provenance = {
            key: snapshot[key]
            for key in (
                "source_ref", "history_start_date", "history_end_date", "spot_price_field", "hv_price_field",
                "return_method", "annualization_trading_days", "data_ref", "trading_calendar",
                "trading_calendar_ref", "data_lineage", "requested_valuation_date",
                "effective_valuation_session", "effective_maturity_session", "market_as_of_date",
            )
            if key in snapshot
        }
        result = replace(result, market_snapshot={**result.market_snapshot, **provenance})
    return result


def _requires_future_trading_calendar(contract: object, config: PricingConfig) -> bool:
    """Require sessions only when the selected route actually simulates paths."""
    product_id = getattr(contract, "product_id", None)
    terms = getattr(contract, "terms", {})
    return requires_future_trading_calendar(product_id, terms, config.model_method)


def _calendar_sessions_for_contract(
    calendar: Mapping[str, object],
    *,
    requested_valuation_date: str,
    contract: object,
    configured_time_to_maturity: float | None,
    available_market_date: str | None = None,
) -> tuple[str, str]:
    requested = date.fromisoformat(requested_valuation_date)
    sessions = tuple(date.fromisoformat(str(value)) for value in calendar.get("sessions", ()))
    prior = tuple(value for value in sessions if value <= requested)
    if not prior:
        raise ValueError("交易日历未覆盖估值日或最近前一中国交易日")
    effective = prior[-1]
    if available_market_date is not None:
        available = date.fromisoformat(available_market_date)
        if available not in prior:
            raise ValueError("历史行情最后有效日期不是交易日历中的中国交易日")
        if available > effective:
            raise ValueError("市场行情日期不得晚于有效估值交易日")
        effective = available
    identity = getattr(contract, "identity", {})
    terms = getattr(contract, "terms", {})
    raw_end = identity.get("contract_end_date") if isinstance(identity, Mapping) else None
    if raw_end:
        maturity = date.fromisoformat(str(raw_end))
    else:
        years = configured_time_to_maturity
        if years is None and isinstance(terms, Mapping):
            years = float(terms.get("T", 0.0))
        if years is None or float(years) <= 0:
            raise ValueError("路径定价缺少合同到期日或有效剩余期限")
        maturity = effective + timedelta(days=round(float(years) * 365.0))
    eligible = tuple(value for value in sessions if effective <= value <= maturity)
    if len(eligible) < 2:
        raise ValueError("交易日历未覆盖估值日至合同到期日前最后一个交易日")
    terminal = eligible[-1]
    if (maturity - terminal).days > 14:
        raise ValueError("交易日历未完整覆盖合同到期日前最后一个中国交易日")
    return effective.isoformat(), terminal.isoformat()


__all__ = ("price",)
