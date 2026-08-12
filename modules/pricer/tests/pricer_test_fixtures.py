"""Pricer测试夹具：显式合成sessions，不依赖仓库行情文件或运行时数据源。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pandas as pd

from runtime.protocol.models import CallerContext, DataAssetRef
from runtime.adapters.local_store import LocalDataStore
from runtime.protocol.module_host import (
    HostObjectRef,
    ModuleHostContext,
    issue_capability_token,
    verify_module_host_context,
)
from modules.pricer import HistoricalData, PricingConfig, PricingInput
from modules.pricer.models import TradingCalendarData
from modules.pricer.market_resolver import market_snapshot_from_history


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def explicit_test_sessions() -> tuple[str, ...]:
    """Return deterministic test-only sessions without a repository market file."""

    return tuple(pd.bdate_range("2022-01-04", periods=1_500).strftime("%Y-%m-%d"))


def explicit_calendar_sessions() -> tuple[str, ...]:
    """Return deterministic synthetic sessions for path-mechanics tests only."""

    business_days = explicit_test_sessions()
    required = {"2022-01-04", "2023-07-28", "2024-01-02", "2026-08-07", "2026-08-11", "2026-08-12"}
    return tuple(value for index, value in enumerate(business_days) if index % 12 or value in required)


def market_csv_payload(asset_id: str = "000905.SH") -> bytes:
    """Build a self-contained market payload for Store/Host protocol tests."""

    sessions = explicit_test_sessions()
    valuation_index = sessions.index(_VALUATION_DATE)
    rows = []
    for index, session in enumerate(sessions):
        close = 6_043.2438 + (index - valuation_index) * .03
        rows.append({
            "date": session,
            "asset_id": asset_id,
            "open": close - .1,
            "high": close + .2,
            "low": close - .2,
            "close": close,
            "adj_close": close * (1.0 + index * .00001),
            "volume": 1_000_000.0,
        })
    return pd.DataFrame(rows).to_csv(index=False).encode("utf-8")


def stored_market_asset(root: Path, asset_id: str = "000905.SH") -> tuple[LocalDataStore, DataAssetRef]:
    """Put the synthetic payload behind the same opaque Store port used by Host calls."""

    payload = market_csv_payload(asset_id)
    sessions = explicit_test_sessions()
    store = LocalDataStore(root)
    reference = store.put_bytes(
        tenant_id="local",
        data_asset_id=f"pricer-test-{asset_id.replace('.', '-')}",
        payload=payload,
        media_type="text/csv",
        schema_id="market-history-v1",
        asset_ids=(asset_id,),
        normalized_fields=("date", "asset_id", "open", "high", "low", "close", "adj_close", "volume"),
        coverage={
            "start_date": sessions[0],
            "end_date": sessions[-1],
            "sessions": sessions,
            "calendar_id": "CN-SSE",
            "calendar_version": "synthetic-test-sessions",
            "by_asset": {asset_id: {"start_date": sessions[0], "end_date": sessions[-1], "row_count": len(sessions)}},
        },
        row_count=len(sessions),
        price_convention={
            "adjustment": "close_and_adj_close",
            "contract_close_field": "close",
            "contract_adjustment": "unadjusted",
            "hv_close_field": "adj_close",
            "hv_adjustment": "forward",
            "close_equals_adj_close": False,
        },
        lineage={"fixture": "self-contained-pricer-market"},
        created_by="host-test",
    )
    return store, reference


def calendar_asset(
    assets: tuple[str, ...],
    *,
    start_date: str | None = None,
) -> tuple[TradingCalendarData, DataAssetRef]:
    """Build the independent future-calendar pair required by path products."""

    minimum = start_date or _VALUATION_DATE
    source_sessions = explicit_calendar_sessions() if start_date is None else explicit_test_sessions()
    sessions = tuple(value for value in source_sessions if value >= minimum)
    exchanges = {asset: ("SZSE" if asset.endswith(".SZ") else "SSE") for asset in assets}
    sessions_by_exchange = {exchange: sessions for exchange in set(exchanges.values())}
    digest = hashlib.sha256("\n".join(sessions).encode("utf-8")).hexdigest()
    calendar = TradingCalendarData(
        source_ref="memory://pricer-test-calendar",
        asset_ids=assets,
        sessions=sessions,
        sessions_by_exchange=sessions_by_exchange,
        asset_exchange=exchanges,
        requested_start_date=sessions[0],
        requested_end_date=sessions[-1],
        content_hash=digest,
    )
    reference = DataAssetRef(
        data_asset_id="pricer-test-calendar",
        storage_ref=calendar.source_ref,
        media_type="application/json",
        schema_id="trading-calendar",
        asset_ids=assets,
        normalized_fields=("session",),
        coverage={
            "start_date": sessions[0],
            "end_date": sessions[-1],
            "sessions": sessions,
            "calendar_id": "CN-COMMON",
            "calendar_version": "synthetic-test-sessions",
        },
        row_count=len(sessions),
        price_convention={"contains_market_prices": False},
        content_hash=digest,
        lineage={"fixture": "self-contained-pricer-calendar"},
    )
    return calendar, reference


_DATES = explicit_test_sessions()
_VALUATION_DATE = "2023-07-28"
_HISTORY_DATES = tuple(value for value in _DATES if value <= _VALUATION_DATE)
_MODULEHOST_V2_TEST_SECRET = b"pricer-modulehost-v2-test-secret"


def market_asset(assets: tuple[str, ...] = ("000905.SH",)) -> tuple[HistoricalData, DataAssetRef]:
    rows = tuple(
        {
            "date": session,
            "asset_id": asset,
            "close": 100.0 + asset_index * 5.0 + row_index * .03,
            "adj_close": 100.0 * (1.0 + asset_index * .01) * (1.0005 ** row_index),
        }
        for asset_index, asset in enumerate(assets)
        for row_index, session in enumerate(_HISTORY_DATES)
    )
    coverage: dict[str, Any] = {
        "start_date": _DATES[0],
        "end_date": _DATES[-1],
        "sessions": _DATES,
        "calendar_id": "CN-SSE",
        "calendar_version": "fixture-000905-20260731",
        "by_asset": {
            asset: {"start": _HISTORY_DATES[0], "end": _HISTORY_DATES[-1]}
            for asset in assets
        },
    }
    historical = HistoricalData(
        "memory://pricer-test-market",
        rows,
        asset_ids=assets,
        coverage=coverage,
    )
    return historical, DataAssetRef(
        data_asset_id="pricer-test-market",
        storage_ref=historical.source_ref,
        media_type="text/csv",
        schema_id="market-history-v1",
        asset_ids=assets,
        normalized_fields=("date", "asset_id", "close", "adj_close"),
        coverage=coverage,
        row_count=len(rows),
        price_convention={},
        content_hash=historical.content_hash,
        lineage={"fixture": "synthetic-test-sessions"},
    )


def demo_config(assets: tuple[str, ...] = ("000905.SH",)) -> PricingConfig:
    multi = len(assets) > 1
    return PricingConfig(
        valuation_date=_VALUATION_DATE,
        spot={asset: 100.0 + index * 5.0 for index, asset in enumerate(assets)} if multi else 100.0,
        historical_volatility={asset: .20 for asset in assets} if multi else .20,
        dividend_yield={asset: 0.0 for asset in assets} if multi else 0.0,
        correlation=[[1.0 if left == right else .25 for right in range(len(assets))] for left in range(len(assets))] if multi else None,
        model_method="monte_carlo",
        path_count=10,
        demo_mode=True,
        risk_free_rate=.02,
    )


def demo_input(contract, assets: tuple[str, ...] | None = None) -> PricingInput:
    assets = assets or tuple(contract.underlyings)
    historical, ref = market_asset(assets)
    calendar, calendar_ref = calendar_asset(assets)
    return PricingInput(
        contract=contract,
        pricing_config=demo_config(assets),
        historical_data=historical,
        market_data_refs=(ref,),
        trading_calendar_data=calendar,
        trading_calendar_ref=calendar_ref,
    )


def adapter_market_snapshot(assets: tuple[str, ...] = ("000905.SH",)) -> dict[str, Any]:
    historical, ref = market_asset(assets)
    calendar, _calendar_ref = calendar_asset(assets)
    snapshot = market_snapshot_from_history(
        pd.DataFrame(historical.rows),
        assets,
        valuation_date=_VALUATION_DATE,
        hv_window=20,
        risk_free_rate=.02,
        dividend_yield={asset: 0.0 for asset in assets} if len(assets) > 1 else 0.0,
        trading_calendar={
            "calendar_id": ref.coverage["calendar_id"],
            "calendar_version": ref.coverage["calendar_version"],
            "sessions": calendar.sessions,
            "verified_cn_sessions": True,
            "source": "host-injected",
        },
    )
    return snapshot


def modulehost_v2_scope(
    contract: Any | None = None,
    *,
    request_policy: tuple[str, ...] = ("module.run",),
) -> tuple[CallerContext, ModuleHostContext]:
    """签发并验证与Pricer、Caller身份及冻结合同完全匹配的v2测试上下文。"""
    caller = CallerContext(
        tenant_id="local",
        principal_id="pricer-test-principal",
        role="test",
        capabilities=request_policy,
        session_id="pricer-test-session",
        audience="option-helper-app",
        request_id="pricer-test-request",
    )
    scoped = {
        "analysis_case_id": "case-pricer-formal" if contract is not None else None,
        "task_id": "task-pricer-formal" if contract is not None else None,
        "candidate_id": "candidate-pricer-formal" if contract is not None else None,
        "catalog_version": contract.registry_snapshot_hash if contract is not None else None,
        "contract_fingerprint": contract.contract_fingerprint if contract is not None else None,
        "contract_ref": HostObjectRef(
            reference_id="resolved-contract:pricer-formal",
            schema_id="optionhelper.resolved-contract/v1",
            content_hash=contract.contract_fingerprint,
        ) if contract is not None else None,
    }
    token_fields = {
        "session_id": caller.session_id,
        "principal_id": caller.principal_id,
        "session_ref": "session:pricer-v2-test",
        "module": "pricer",
        "expires_at": 9_999_999_999,
        "context_id": "mhc_pricer_v2_test_0001",
        "page_hash": "2" * 64,
        "audience": caller.audience,
        "host_kind": "app",
        "request_policy": request_policy,
        "capability_version": "12.1",
        "protocol_version": "module-host/v2",
        **scoped,
    }
    token = issue_capability_token(token_secret=_MODULEHOST_V2_TEST_SECRET, **token_fields)
    context = ModuleHostContext(
        session_ref=token_fields["session_ref"],
        capability_token=token,
        module="pricer",
        page_hash=token_fields["page_hash"],
        capability_version=token_fields["capability_version"],
        protocol_version=token_fields["protocol_version"],
        context_id=token_fields["context_id"],
        host_kind="app",
        request_policy=request_policy,
        **scoped,
    )
    verify_module_host_context(
        context,
        token_secret=_MODULEHOST_V2_TEST_SECRET,
        session_id=caller.session_id,
        principal_id=caller.principal_id,
        audience=caller.audience,
    )
    return caller, context


__all__ = (
    "adapter_market_snapshot",
    "demo_config",
    "demo_input",
    "market_asset",
    "modulehost_v2_scope",
)
