"""隔离回测夹具：两个真实独立标的的交易日close与adj_close路径。"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import tempfile
from typing import Any, Iterator

import numpy as np
import pandas as pd


MODULEHOST_V2_TEST_SECRET = b"backtester-modulehost-v2-test-secret"


def two_asset_history() -> pd.DataFrame:
    """覆盖65个登记结构最长期限的确定性双标的测试历史。"""
    dates = pd.date_range("2021-01-04", periods=1_450, freq="B")
    rows: list[dict[str, object]] = []
    for asset_id, base, slope, phase in (("000905.SH", 100.0, 0.025, 0.0), ("000300.SH", 95.0, 0.018, 0.7)):
        index = np.arange(len(dates), dtype=float)
        close = base + slope * index + 1.8 * np.sin(index / 23.0 + phase)
        adj_close = close * (1.04 + 0.00003 * index)
        rows.extend({"date": date, "asset_id": asset_id, "close": float(raw), "adj_close": float(adjusted)} for date, raw, adjusted in zip(dates, close, adj_close))
    return pd.DataFrame(rows)


def single_asset_market_payload(asset_id: str = "000905.SH") -> bytes:
    """Return deterministic test-only CSV bytes, never a repository market file."""

    frame = two_asset_history()
    frame = frame.loc[frame["asset_id"] == "000905.SH"].copy()
    frame["asset_id"] = asset_id
    frame["open"] = frame["close"] - .1
    frame["high"] = frame["close"] + .2
    frame["low"] = frame["close"] - .2
    frame["volume"] = 1_000_000.0
    return frame[["date", "asset_id", "open", "high", "low", "close", "adj_close", "volume"]].to_csv(
        index=False,
    ).encode("utf-8")


@contextmanager
def temporary_market_layout() -> Iterator[tuple[Path, Path, str]]:
    """Create an isolated local-development data root for path-boundary tests."""

    with tempfile.TemporaryDirectory(prefix="backtester-test-data-") as temporary:
        project_root = Path(temporary)
        data_root = project_root / "data"
        data_root.mkdir()
        relative = "data/market-fixture.csv"
        (project_root / relative).write_bytes(single_asset_market_payload())
        yield project_root, data_root, relative


def modulehost_v2_scope(
    contract: Any | None = None,
    *,
    request_policy: tuple[str, ...] = ("module.run",),
) -> tuple[Any, Any, bytes]:
    """签发与CallerContext身份、模块和合同范围完全匹配的v2测试上下文。"""
    from runtime.protocol.models import CallerContext
    from runtime.protocol.module_host import HostObjectRef, ModuleHostContext, issue_capability_token

    caller = CallerContext(
        tenant_id="tenant-a",
        principal_id="backtester-test-principal",
        role="admin",
        capabilities=request_policy,
        session_id="backtester-test-session",
        audience="option-helper-app",
        request_id="backtester-test-request",
    )
    scoped = {
        "analysis_case_id": "case-a" if contract is not None else None,
        "task_id": "task-a" if contract is not None else None,
        "candidate_id": "candidate-a" if contract is not None else None,
        "catalog_version": contract.registry_snapshot_hash if contract is not None else None,
        "contract_fingerprint": contract.contract_fingerprint if contract is not None else None,
        "contract_ref": HostObjectRef(
            reference_id="resolved-contract:test",
            schema_id="optionhelper.resolved-contract/v1",
            content_hash=contract.contract_fingerprint,
        ) if contract is not None else None,
    }
    token_fields = {
        "session_id": caller.session_id,
        "principal_id": caller.principal_id,
        "session_ref": "session:backtester-v2-test",
        "module": "backtester",
        "expires_at": 9_999_999_999,
        "context_id": "mhc_backtester_v2_test_0001",
        "page_hash": "2" * 64,
        "audience": caller.audience,
        "host_kind": "app",
        "request_policy": request_policy,
        "capability_version": "12.1",
        "protocol_version": "module-host/v2",
        **scoped,
    }
    token = issue_capability_token(token_secret=MODULEHOST_V2_TEST_SECRET, **token_fields)
    context = ModuleHostContext(
        session_ref=token_fields["session_ref"],
        capability_token=token,
        module="backtester",
        page_hash=token_fields["page_hash"],
        capability_version=token_fields["capability_version"],
        protocol_version=token_fields["protocol_version"],
        context_id=token_fields["context_id"],
        host_kind="app",
        request_policy=request_policy,
        **scoped,
    )
    return caller, context, MODULEHOST_V2_TEST_SECRET
