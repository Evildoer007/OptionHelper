"""Explicit, deterministic market asset for packaged-App compute verification.

This module is only called by the macOS launcher when its
``--verification-fixture`` switch is present.  It neither changes normal App
startup nor exposes an HTTP import path.  The asset is placed through the
same App-owned DataStore and carries an ordinary tenant/principal binding, so
the subsequent calculator call still has to pass task binding, Module Host
authorization, ResultStore commit, and the verified Capability engine.
"""

from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import date, timedelta
from io import StringIO
import json
from typing import TYPE_CHECKING

from runtime.adapters.local_store import LocalDataStore

from .authorization.roles import Role
from .identity.identity_provider import LocalAuthenticationRequest

if TYPE_CHECKING:
    from .app_server import AppServer


FIXTURE_PRINCIPAL_LABEL = "artifact-verifier"
FIXTURE_ASSET_ID = "artifact-compute-market"
FIXTURE_ASSET = "000905.SH"
FIXTURE_ASSETS = (FIXTURE_ASSET, "000300.SH")
FIXTURE_ENTRY_DATE = "2022-01-04"
FIXTURE_PRICING_DATE = "2022-02-01"


def install_compute_verification_fixture(app: "AppServer") -> None:
    """Register one fixed market-history asset for the local verifier identity.

    The fixture intentionally contains synthetic, explicitly labelled test
    observations.  It is not a price quote and is never enabled by the normal
    desktop launcher invocation.
    """

    identity = app.identity_provider.authenticate(
        LocalAuthenticationRequest(role=Role.ADMIN, principal_label=FIXTURE_PRINCIPAL_LABEL),
    )
    # Default Backtester windows use three years of pre-entry history plus
    # the full product tenor.  Keep an eight-year historical buffer so the
    # packaged verifier covers that normal App default as well as its fixed
    # 2022 entry example.
    fixture_start = min(
        date.fromisoformat(FIXTURE_ENTRY_DATE),
        date.today() - timedelta(days=8 * 366),
    )
    # The packaged-App verifier exercises both historic backtests and a new
    # Payoffer contract that starts on the actual test date.  Keep enough
    # synthetic exchange sessions for the longest current product tenor so a
    # release test never falls through to the user's iFind credentials merely
    # because the calendar fixture aged out.
    sessions = _business_sessions(fixture_start, count=_fixture_session_count(fixture_start))
    store = LocalDataStore(app._capability_runtime_root / "data")
    fixture_asset_sets = ((FIXTURE_ASSET,), (FIXTURE_ASSETS[1],), FIXTURE_ASSETS)
    for asset_ids in fixture_asset_sets:
        suffix = "-".join(asset_ids)
        market_asset_id = FIXTURE_ASSET_ID if asset_ids == (FIXTURE_ASSET,) else f"artifact-compute-market-{suffix}"
        calendar_payload = json.dumps({
            "schema_id": "trading-calendar",
            "asset_ids": list(asset_ids),
            "sessions": list(sessions),
            "sessions_by_exchange": {"SSE": list(sessions)},
            "asset_exchange": {asset: "SSE" for asset in asset_ids},
            "requested_start_date": sessions[0],
            "requested_end_date": sessions[-1],
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        calendar_asset_id = "artifact-compute-calendar" if asset_ids == (FIXTURE_ASSET,) else f"artifact-compute-calendar-{suffix}"
        calendar = store.put_bytes(
            tenant_id=identity.tenant_id,
            data_asset_id=calendar_asset_id,
            payload=calendar_payload,
            media_type="application/json",
            schema_id="trading-calendar",
            asset_ids=asset_ids,
            normalized_fields=("session",),
            coverage={
                "start_date": sessions[0],
                "end_date": sessions[-1],
                "sessions": list(sessions),
                "calendar_id": "CN-SSE",
                "calendar_revision": "artifact-compute-fixture-v1",
            },
            row_count=len(sessions),
            price_convention={"contains_market_prices": False},
            lineage={"fixture": "packaged-app-compute-verification-v1", "synthetic": True},
            created_by=identity.principal_id,
        )
        app.data_assets.register(identity, asdict(calendar))
        reference = store.put_bytes(
            tenant_id=identity.tenant_id,
            data_asset_id=market_asset_id,
            payload=_market_payload(sessions, asset_ids),
            media_type="text/csv",
            schema_id="market-history",
            asset_ids=asset_ids,
            normalized_fields=("date", "asset_id", "open", "high", "low", "close", "adj_close", "volume"),
            coverage={
                "start": sessions[0],
                "end": sessions[-1],
                "sessions": sessions,
                "calendar_id": "CN-SSE",
                "calendar_revision": "artifact-compute-fixture-v1",
                "calendar_coverage_end": sessions[-1],
                "calendar_ref": {
                    "data_asset_id": calendar.data_asset_id,
                    "content_hash": calendar.content_hash,
                    "schema_id": "trading-calendar",
                    "media_type": "application/json",
                },
                "by_asset": {
                    asset: {
                        "start_date": sessions[0],
                        "end_date": sessions[-1],
                        "row_count": len(sessions),
                    }
                    for asset in asset_ids
                },
            },
            row_count=len(sessions) * len(asset_ids),
            price_convention={
                "frequency": "1d",
                "requested_adjustment": "auto",
                "timezone": "Asia/Shanghai",
                "field_adjustment_by_asset": {
                    asset: {"close": "unadjusted", "adj_close": "forward_adjusted"}
                    for asset in asset_ids
                },
                "asset_market_conventions": {
                    asset: {
                        "raw_close_retained": True,
                        "close_convention": "unadjusted_close",
                        "historical_return_field": "adj_close",
                    }
                    for asset in asset_ids
                },
                "hv_input_requirements_by_asset": {
                    asset: {
                        "return_price_field": "adj_close",
                        "return_type": "log_return",
                        "annualization_trading_days": 244,
                        "required_frequency": "1d",
                    }
                    for asset in asset_ids
                },
            },
            lineage={"fixture": "packaged-app-compute-verification-v1", "synthetic": True},
            created_by=identity.principal_id,
        )
        app.data_assets.register(identity, asdict(reference))


def _business_sessions(start: date, *, count: int) -> tuple[str, ...]:
    sessions: list[str] = []
    current = start
    while len(sessions) < count:
        if current.weekday() < 5:
            sessions.append(current.isoformat())
        current += timedelta(days=1)
    return tuple(sessions)


def _fixture_session_count(start: date) -> int:
    """Cover five years beyond the verification date using weekday sessions."""

    target = date.today() + timedelta(days=5 * 366)
    weekday_estimate = ((target - start).days * 5) // 7 + 20
    return max(1_500, weekday_estimate)


def _market_payload(sessions: tuple[str, ...], assets: tuple[str, ...]) -> bytes:
    stream = StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=("date", "asset_id", "open", "high", "low", "close", "adj_close", "volume"),
    )
    writer.writeheader()
    for asset_index, asset in enumerate(assets):
        for index, session in enumerate(sessions):
            close = 100.0 + asset_index * 10.0 + index * 0.03
            writer.writerow({
                "date": session,
                "asset_id": asset,
                "open": f"{close - 0.10:.4f}",
                "high": f"{close + 0.20:.4f}",
                "low": f"{close - 0.20:.4f}",
                "close": f"{close:.4f}",
                "adj_close": f"{close * (1.0 + index * 0.00001):.8f}",
                "volume": "1000000",
            })
    return stream.getvalue().encode("utf-8")
