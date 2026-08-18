"""Compile App-friendly calculator requests into the three formal inputs.

This is the only page/agent request adapter for calculator runs.  Product
semantics remain owned by the Registry Loader and ``resolve_contract``; the
App supplies only authenticated data references and Host scope.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
from math import isfinite
from typing import Any, Mapping, Sequence

from .contract_engine import (
    ContractResolutionError,
    ResolvedContract,
    load_registry,
    resolve_contract,
    verify_product_snapshot_binding,
)
from .contract_types import semantic_hash
from runtime.protocol.models import DataAssetRef, ObservedContractState
from runtime.ports.data_store import DataStoreReadPort
from runtime.ports.product_snapshot import ProductSnapshotProvider, require_product_snapshot_provider


_CALCULATORS = frozenset({"payoffer", "pricer", "backtester"})
_COMMON_FIELDS = frozenset({
    "action", "product_id", "identity", "term_overrides", "run_id",
    # This is a first-run page intent, not a contract identity field.  The
    # Host consumes it while compiling the data-backed contract and never
    # forwards it to a formal calculator input.
    "auto_contract_start_date",
})
_DEMO_MARKET_REQUIRED = frozenset({
    "valuation_date", "spot", "volatility_override", "time_to_maturity",
    "risk_free_rate", "dividend_yield", "demo_calendar",
})


def explicit_demo_pricing_error(config: Mapping[str, Any] | object) -> str | None:
    """Validate the only zero-DataAssetRef Pricer exception.

    This is deliberately narrow: MC10 is a visible UI demonstration for the
    European call example, not a back-door market-data source.  It must carry
    its whole market assumption and a declaration that no discrete path
    calendar is being fabricated.  All other Pricer runs require one verified
    ``DataAssetRef``.
    """
    if not isinstance(config, Mapping) or config.get("demo_mode") is not True:
        return "正式定价必须绑定唯一DataAssetRef；MC10演示需显式demo_mode=true。"
    if config.get("model_method") != "monte_carlo" or config.get("path_count") != 10:
        return "无行情数据的演示仅允许model_method=monte_carlo、path_count=10和demo_mode=true。"
    missing = sorted(name for name in _DEMO_MARKET_REQUIRED if config.get(name) is None)
    if missing:
        return "MC10演示缺少显式市场快照字段：" + "、".join(missing)
    try:
        valuation_date = str(config["valuation_date"])
        date.fromisoformat(valuation_date)
    except (TypeError, ValueError):
        return "MC10演示valuation_date必须为YYYY-MM-DD。"
    for field, strictly_positive in (("spot", True), ("volatility_override", True), ("time_to_maturity", True), ("risk_free_rate", False), ("dividend_yield", False)):
        value = config[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
            return f"MC10演示{field}必须为有限数值。"
        if strictly_positive and float(value) <= 0:
            return f"MC10演示{field}必须为正数。"
    calendar = config["demo_calendar"]
    if not isinstance(calendar, Mapping):
        return "MC10演示demo_calendar必须为对象。"
    expected_calendar = {
        "calendar_id": "demo-european-vanilla",
        "calendar_revision": "single-session-demo",
        "sessions": [valuation_date],
        "discrete_path": False,
    }
    if dict(calendar) != expected_calendar:
        return "MC10演示demo_calendar必须声明仅估值日，且不得伪造离散路径交易日历。"
    return None


def is_explicit_demo_pricing_config(config: Mapping[str, Any] | object) -> bool:
    """Return whether a Pricer request can lawfully omit a DataAssetRef."""
    return explicit_demo_pricing_error(config) is None


def prepare_compute_request(
    module: str,
    request: Mapping[str, Any],
    *,
    data_refs: Sequence[Mapping[str, Any]] = (),
    resolved_contract: Mapping[str, Any] | None = None,
    data_store: DataStoreReadPort | None = None,
    product_snapshot_provider: ProductSnapshotProvider | None = None,
) -> dict[str, Any]:
    """Return one formal business request and its immutable contract facts."""
    if module not in _CALCULATORS or not isinstance(request, Mapping):
        raise ContractResolutionError("计算请求必须指定Payoffer、Pricer或Backtester")
    values = dict(request)
    action = str(values.get("action", "run")).strip().lower()
    if action != "run":
        raise ContractResolutionError("正式计算输入编译器只处理run")
    allowed = set(_COMMON_FIELDS)
    if module == "pricer":
        allowed.update({"pricing_config", "observed_contract_state"})
    elif module == "backtester":
        allowed.add("backtest_config")
    if module == "pricer":
        allowed.update({"market_data_refs", "trading_calendar_ref"})
    if module == "backtester":
        allowed.update({"historical_data", "history_reference"})
    unknown = set(values) - allowed
    if unknown:
        raise ContractResolutionError("计算请求含未知字段：" + ",".join(sorted(unknown)))

    product_id = values.get("product_id")
    if resolved_contract is None and (not isinstance(product_id, str) or not product_id.strip()):
        raise ContractResolutionError("product_id不能为空")
    identity = values.get("identity", {})
    overrides = values.get("term_overrides", {})
    if not isinstance(identity, Mapping) or not isinstance(overrides, Mapping):
        raise ContractResolutionError("identity与term_overrides必须为对象")

    refs = tuple(_canonical_data_ref(item) for item in data_refs)
    history_refs = tuple(item for item in refs if item.get("schema_id") == "market-history")
    calendar_refs = tuple(item for item in refs if item.get("schema_id") == "trading-calendar")
    unsupported_refs = tuple(
        item for item in refs
        if item.get("schema_id") not in {"market-history", "trading-calendar"}
    )
    if unsupported_refs:
        raise ContractResolutionError("计算请求包含不支持的DataAssetRef.schema_id")
    calendar = verified_trading_calendar(calendar_refs[0], data_store) if calendar_refs else None
    resolver_identity = dict(identity)
    if calendar is not None:
        for key in ("calendar_id", "calendar_revision"):
            supplied = resolver_identity.get(key)
            if supplied is not None and supplied != calendar[key]:
                raise ContractResolutionError(f"identity.{key}与Host验证交易日历不一致")
            resolver_identity[key] = calendar[key]

    if resolved_contract is None:
        contract = resolve_contract(
            product_id.strip(),
            identity=resolver_identity,
            term_overrides=dict(overrides),
            trading_dates=calendar["sessions"] if calendar is not None else None,
        )
    else:
        try:
            contract = ResolvedContract(**dict(resolved_contract))
            if contract.product_version.startswith("development:"):
                registry = load_registry()
                attested_product_version = None
            else:
                provider = require_product_snapshot_provider(product_snapshot_provider)
                registry = provider.load_registry_snapshot(
                    product_version=contract.product_version,
                    product_id=contract.product_id,
                    registry_snapshot_hash=contract.registry_snapshot_hash,
                )
                attested_product_version = contract.product_version
            verify_product_snapshot_binding(
                contract, registry, attested_product_version=attested_product_version,
            )
        except (TypeError, ValueError, ContractResolutionError) as error:
            raise ContractResolutionError(f"Host冻结ResolvedContract无效：{error}") from error
        requested_product = product_id.strip() if isinstance(product_id, str) else contract.product_id
        _verify_friendly_request(contract, requested_product, resolver_identity, overrides)
        if calendar is not None:
            _verify_calendar_matches_contract(contract, calendar)
    contract_payload = contract.to_protocol_dict()
    if module == "payoffer":
        formal: dict[str, Any] = {"action": "run", "payoff_input": {"contract": contract_payload}}
    elif module == "pricer":
        config = values.get("pricing_config")
        if not isinstance(config, Mapping):
            raise ContractResolutionError("pricing_config必须为对象")
        if len(history_refs) == 0 and not calendar_refs and is_explicit_demo_pricing_config(config):
            pass
        elif len(history_refs) != 1 or len(calendar_refs) > 1:
            raise ContractResolutionError("PricingInput必须绑定唯一DataAssetRef历史行情，交易日历最多一项")
        formal = {
            "action": "run",
            "contract": contract_payload,
            "pricing_config": deepcopy(dict(config)),
            "market_data_refs": list(history_refs),
        }
        observed_state = values.get("observed_contract_state")
        if observed_state is not None:
            try:
                frozen_state = ObservedContractState.from_host_payload(observed_state)
            except (TypeError, ValueError) as error:
                raise ContractResolutionError(f"Host冻结observed_contract_state无效：{error}") from error
            formal["observed_contract_state"] = frozen_state.to_protocol_dict()
        if calendar_refs:
            formal["trading_calendar_ref"] = calendar_refs[0]
    else:
        config = values.get("backtest_config")
        if not isinstance(config, Mapping):
            raise ContractResolutionError("backtest_config必须为对象")
        if len(history_refs) != 1 or len(calendar_refs) > 1:
            raise ContractResolutionError("BacktestInput必须绑定唯一历史DataAssetRef，交易日历最多一项")
        if calendar_refs:
            # The App must persist a calendar-bound history ref *before* this
            # formal compilation.  ``DataAssetRef.storage_ref`` commits every
            # coverage field, so mutating it here would invalidate the opaque
            # reference when Backtester reads the bytes.  We still verify the
            # separately supplied calendar against that persisted coverage in
            # order to freeze observation schedules from authenticated dates.
            _require_history_calendar_matches_verified_calendar(
                history_refs[0], calendar,
            )
            historical_data = history_refs[0]
        else:
            # A non-observation contract can use the calendar provenance already
            # authenticated in its one history asset.  This preserves the
            # complete BacktestInput shape for an offline release probe without
            # allowing any observation schedule to be frozen from those fields.
            _require_declared_history_calendar(history_refs[0])
            historical_data = history_refs[0]
        formal = {
            "action": "run",
            "contract": contract_payload,
            "backtest_config": deepcopy(dict(config)),
            "historical_data": historical_data,
        }
    run_id = values.get("run_id")
    if isinstance(run_id, str) and run_id.strip():
        formal["run_id"] = run_id.strip()
    return {
        "request": formal,
        "resolved_contract": contract_payload,
        "contract_fingerprint": contract.contract_fingerprint,
        "product_version": contract.product_version,
        "registry_snapshot_hash": contract.registry_snapshot_hash,
        "product_snapshot_hash": contract.product_snapshot_hash,
        "product_paths_hash": contract.product_paths_hash,
    }


def _canonical_data_ref(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractResolutionError("DataAssetRef必须为对象")
    required = {
        "data_asset_id", "storage_ref", "media_type", "schema_id", "asset_ids", "normalized_fields",
        "coverage", "row_count", "price_convention", "content_hash", "lineage", "tenant_id",
        "created_by", "access_scope", "partition_spec",
    }
    if set(value) != required:
        raise ContractResolutionError("DataAssetRef字段必须为" + ",".join(sorted(required)))
    return deepcopy(dict(value))


def verified_trading_calendar(
    value: Mapping[str, Any],
    data_store: DataStoreReadPort | None,
) -> dict[str, Any]:
    """Read one App-owned calendar asset and freeze only its authenticated facts.

    A browser may name a calendar asset but never supplies calendar identity or
    sessions.  The Host-provided DataStore is the only byte authority here.
    """

    if data_store is None or not callable(getattr(data_store, "read_bytes", None)):
        raise ContractResolutionError("正式交易日历必须由Host注入经验证的DataStorePort")
    try:
        reference = DataAssetRef(**dict(value))
        encoded = data_store.read_bytes(reference, tenant_id=reference.tenant_id)
    except (TypeError, ValueError, OSError, PermissionError) as error:
        raise ContractResolutionError("Host验证交易日历无法读取") from error
    if not isinstance(encoded, bytes) or sha256(encoded).hexdigest() != reference.content_hash:
        raise ContractResolutionError("Host验证交易日历内容哈希不一致")
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractResolutionError("Host验证交易日历不是有效JSON") from error
    if not isinstance(payload, Mapping) or payload.get("schema_id") != "trading-calendar":
        raise ContractResolutionError("Host验证交易日历协议无效")
    coverage = reference.coverage
    calendar_id = coverage.get("calendar_id")
    calendar_revision = coverage.get("calendar_revision")
    sessions = payload.get("sessions")
    if (
        reference.schema_id != "trading-calendar"
        or reference.media_type != "application/json"
        or not isinstance(calendar_id, str)
        or not calendar_id
        or not isinstance(calendar_revision, str)
        or not calendar_revision
        or not isinstance(sessions, list)
        or isinstance(sessions, (str, bytes))
    ):
        raise ContractResolutionError("Host验证交易日历缺少身份或交易日")
    if set(payload.get("asset_ids", ())) != set(reference.asset_ids):
        raise ContractResolutionError("Host验证交易日历标的与DataAssetRef不一致")
    try:
        normalized = tuple(date.fromisoformat(str(item)).isoformat() for item in sessions)
    except ValueError as error:
        raise ContractResolutionError("Host验证交易日历交易日必须为YYYY-MM-DD") from error
    if not normalized or normalized != tuple(sorted(normalized)) or len(set(normalized)) != len(normalized):
        raise ContractResolutionError("Host验证交易日历交易日必须严格递增且不重复")
    if coverage.get("sessions") not in (None, list(normalized)):
        raise ContractResolutionError("Host验证交易日历覆盖声明与内容不一致")
    return {
        "calendar_ref": deepcopy(dict(value)),
        "calendar_id": calendar_id,
        "calendar_revision": calendar_revision,
        "sessions": normalized,
    }


def bind_verified_calendar_to_history(
    history_ref: Mapping[str, Any],
    calendar_ref: Mapping[str, Any],
    data_store: DataStoreReadPort | None,
) -> dict[str, Any]:
    """Return a history ref whose calendar facts come only from verified bytes.

    DataFetcher may carry calendar metadata as lineage.  Backtester needs the
    same facts in the signed DataAssetRef coverage, so the Host replaces rather
    than trusts those fields.  The selected sessions are constrained to the
    history coverage; this never invents a date outside the supplied asset.
    """

    history = _canonical_data_ref(history_ref)
    if history["schema_id"] != "market-history":
        raise ContractResolutionError("历史行情必须使用market-history DataAssetRef")
    calendar = verified_trading_calendar(calendar_ref, data_store)
    coverage = deepcopy(dict(history["coverage"]))
    start = coverage.get("start_date", coverage.get("start"))
    end = coverage.get("end_date", coverage.get("end"))
    try:
        start_day = date.fromisoformat(str(start)).isoformat()
        end_day = date.fromisoformat(str(end)).isoformat()
    except ValueError as error:
        raise ContractResolutionError("历史行情DataAssetRef.coverage必须声明YYYY-MM-DD起止日") from error
    if start_day > end_day:
        raise ContractResolutionError("历史行情DataAssetRef.coverage起止日无效")
    sessions = tuple(day for day in calendar["sessions"] if start_day <= day <= end_day)
    if not sessions or sessions[0] != start_day or sessions[-1] != end_day:
        raise ContractResolutionError("Host验证交易日历未完整覆盖历史行情DataAssetRef")
    coverage.update({
        "calendar_id": calendar["calendar_id"],
        "calendar_revision": calendar["calendar_revision"],
        "sessions": list(sessions),
        "calendar_coverage_end": sessions[-1],
        "calendar_ref": calendar["calendar_ref"],
    })
    return {**history, "coverage": coverage}


def _require_history_calendar_matches_verified_calendar(
    history_ref: Mapping[str, Any],
    calendar: Mapping[str, Any] | None,
) -> None:
    """Require a persisted history reference to carry the verified calendar.

    This is intentionally a comparison rather than an in-memory mutation of
    the history ref.  A DataAssetRef is metadata-committed by its storage
    reference, therefore adding calendar facts after it was issued makes the
    otherwise correct CSV unreadable to the formal Backtester.
    """

    if calendar is None:
        raise ContractResolutionError("BacktestInput缺少Host验证交易日历")
    history = _canonical_data_ref(history_ref)
    coverage = history.get("coverage")
    if history.get("schema_id") != "market-history" or not isinstance(coverage, Mapping):
        raise ContractResolutionError("BacktestInput历史行情必须声明交易日历覆盖")
    start = coverage.get("start_date", coverage.get("start"))
    end = coverage.get("end_date", coverage.get("end"))
    try:
        start_day = date.fromisoformat(str(start)).isoformat()
        end_day = date.fromisoformat(str(end)).isoformat()
    except ValueError as error:
        raise ContractResolutionError("历史行情DataAssetRef.coverage必须声明YYYY-MM-DD起止日") from error
    expected_sessions = [
        session for session in calendar["sessions"]
        if start_day <= session <= end_day
    ]
    if (
        not expected_sessions
        or expected_sessions[0] != start_day
        or expected_sessions[-1] != end_day
        or coverage.get("calendar_id") != calendar["calendar_id"]
        or coverage.get("calendar_revision") != calendar["calendar_revision"]
        or coverage.get("sessions") != expected_sessions
        or coverage.get("calendar_coverage_end") != expected_sessions[-1]
    ):
        raise ContractResolutionError(
            "BacktestInput历史行情必须使用已持久化的Host验证交易日历；请重新获取该回测区间行情。"
        )


def _require_declared_history_calendar(history_ref: Mapping[str, Any]) -> None:
    """Require a self-contained historical DataAssetRef calendar declaration."""

    coverage = history_ref.get("coverage")
    if not isinstance(coverage, Mapping):
        raise ContractResolutionError("BacktestInput历史行情必须声明交易日历覆盖")
    sessions = coverage.get("sessions")
    calendar_id = coverage.get("calendar_id")
    calendar_revision = coverage.get("calendar_revision")
    if (
        not isinstance(sessions, list)
        or not sessions
        or not isinstance(calendar_id, str)
        or not calendar_id
        or not isinstance(calendar_revision, str)
        or not calendar_revision
    ):
        raise ContractResolutionError("BacktestInput历史行情必须显式声明calendar_id、calendar_revision和sessions")


def _verify_calendar_matches_contract(contract: ResolvedContract, calendar: Mapping[str, Any]) -> None:
    """An existing frozen contract may only be repriced on its own calendar."""

    identity = contract.identity
    if (
        identity.get("calendar_id") != calendar["calendar_id"]
        or identity.get("calendar_revision") != calendar["calendar_revision"]
    ):
        raise ContractResolutionError("Host验证交易日历与已冻结ResolvedContract不一致")
    sessions = set(calendar["sessions"])
    for schedule in contract.resolved_schedules.values():
        if any(day not in sessions for day in schedule["dates"]):
            raise ContractResolutionError("Host验证交易日历未覆盖已冻结合同观察日")


def _verify_friendly_request(
    contract: ResolvedContract,
    product_id: str,
    identity: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> None:
    if contract.product_id != product_id:
        raise ContractResolutionError("当前任务已绑定另一产品的ResolvedContract")
    for key, value in overrides.items():
        if key not in contract.terms or semantic_hash(contract.terms[key]) != semantic_hash(value):
            raise ContractResolutionError(f"本次条款{key}与任务已冻结ResolvedContract冲突")
    for key in ("underlyings", "currency", "contract_start_date", "contract_end_date", "price_convention"):
        if key in identity and identity[key] is not None:
            supplied = tuple(identity[key]) if key == "underlyings" else identity[key]
            existing = tuple(contract.identity.get(key, ())) if key == "underlyings" else contract.identity.get(key)
            if existing != supplied:
                raise ContractResolutionError(f"identity.{key}与任务已冻结ResolvedContract冲突")
    supplied_references = identity.get("reference_prices")
    if supplied_references is not None and dict(contract.identity.get("reference_prices") or {}) != dict(supplied_references):
        raise ContractResolutionError("identity.reference_prices与任务已冻结ResolvedContract冲突")


__all__ = (
    "bind_verified_calendar_to_history",
    "explicit_demo_pricing_error",
    "is_explicit_demo_pricing_config",
    "prepare_compute_request",
    "verified_trading_calendar",
)
