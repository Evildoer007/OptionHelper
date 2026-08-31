"""Compile App-friendly calculator requests into the three formal inputs.

This is the only page/agent request adapter for calculator runs.  Product
semantics remain owned by the Registry Loader and ``resolve_contract``; the
App supplies only authenticated data references and Host scope.
"""

from __future__ import annotations

from copy import deepcopy
import csv
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from io import StringIO
import json
from math import isclose, isfinite
from typing import Any, Mapping, Sequence

from .contract_engine import (
    ContractResolutionError,
    ResolvedContract,
    derive_contract_end_date,
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
_OBSERVATION_TERM_KEYS = frozenset({"O_KO", "O_KI", "Oc", "Otouch", "Orange", "Ovar", "Ohedge", "Oreset"})


@dataclass(frozen=True)
class ComputeDataRequirements:
    """Core-owned market and calendar demand for one calculator request."""

    history_required: bool
    future_calendar_required: bool
    historical_fields: tuple[str, ...]
    observation_price: str
    tenor_years: float
    observation_count: int | None


def compile_compute_data_requirements(
    module: str,
    request: Mapping[str, Any],
    resolved_contract: Mapping[str, Any] | None = None,
) -> ComputeDataRequirements:
    if module not in _CALCULATORS or not isinstance(request, Mapping):
        raise ContractResolutionError("计算数据需求必须绑定正式计算模块")
    if isinstance(resolved_contract, Mapping):
        terms = resolved_contract.get("terms")
    else:
        product_id = request.get("product_id")
        product = load_registry().get("products", {}).get(product_id) if isinstance(product_id, str) else None
        registered = product.get("terms") if isinstance(product, Mapping) else None
        overrides = request.get("term_overrides", {})
        if not isinstance(registered, Mapping) or not isinstance(overrides, Mapping):
            terms = {}
        else:
            terms = {**registered, **dict(overrides)}
    terms = terms if isinstance(terms, Mapping) else {}
    observation_price = str(terms.get("observation_price", "close"))
    if observation_price not in {"close", "open", "high", "low"}:
        raise ContractResolutionError("合同observation_price必须为close、open、high或low")
    fields = ["close", "adj_close"]
    if observation_price not in fields:
        fields.append(observation_price)
    if "T" not in terms:
        raise ContractResolutionError("合同条款必须显式提供期限T")
    try:
        tenor = float(terms["T"])
    except (TypeError, ValueError) as error:
        raise ContractResolutionError("合同期限T必须为有效数字") from error
    if not isfinite(tenor) or tenor <= 0:
        raise ContractResolutionError("合同期限T必须为正有限数值")
    raw_observation_count = terms.get("n_obs")
    if raw_observation_count is None:
        observation_count = None
    else:
        try:
            numeric_observation_count = float(raw_observation_count)
        except (TypeError, ValueError) as error:
            raise ContractResolutionError("合同n_obs必须为正整数") from error
        if (
            not isfinite(numeric_observation_count)
            or numeric_observation_count <= 0
            or not numeric_observation_count.is_integer()
        ):
            raise ContractResolutionError("合同n_obs必须为正整数")
        observation_count = int(numeric_observation_count)
    has_observations = bool(_OBSERVATION_TERM_KEYS.intersection(terms)) or bool(terms.get("monitor"))
    return ComputeDataRequirements(
        history_required=True,
        future_calendar_required=module == "payoffer" and has_observations or module == "pricer" and has_observations,
        historical_fields=tuple(fields),
        observation_price=observation_price,
        tenor_years=tenor,
        observation_count=observation_count,
    )


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
        allowed.add("historical_data")
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
    if resolved_contract is None and (module in {"pricer", "backtester"} or history_refs):
        values = _bind_data_backed_contract_identity(module, values, history_refs, data_store)
        identity = values.get("identity", {})
        overrides = values.get("term_overrides", {})
    if resolved_contract is None:
        values = _freeze_first_contract_end_date(module, values)
        identity = values.get("identity", {})
        overrides = values.get("term_overrides", {})
    if len(calendar_refs) > 1:
        # One formal contract has exactly one authenticated observation
        # calendar.  Accepting the first of several supplied assets would make
        # the frozen schedule depend on caller ordering and leave unbound data
        # in the request, including for Payoffer.
        raise ContractResolutionError("计算请求最多绑定一个交易日历DataAssetRef")
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
        if "demo_mode" in config or "demo_calendar" in config:
            raise ContractResolutionError("正式PricingInput不接受demo_mode或demo_calendar")
        if len(history_refs) != 1 or len(calendar_refs) > 1:
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


def _bind_data_backed_contract_identity(
    module: str,
    request: Mapping[str, Any],
    history_refs: Sequence[Mapping[str, Any]],
    data_store: DataStoreReadPort | None,
) -> dict[str, Any]:
    if len(history_refs) != 1 or data_store is None or not callable(getattr(data_store, "read_bytes", None)):
        raise ContractResolutionError(f"{module}首次合同必须由Host绑定唯一market-history DataAssetRef")
    reference = DataAssetRef(**dict(history_refs[0]))
    try:
        payload = data_store.read_bytes(reference, tenant_id=reference.tenant_id)
    except (OSError, PermissionError, TypeError, ValueError) as error:
        raise ContractResolutionError("Host无法读取首次合同的历史行情") from error
    if not isinstance(payload, bytes) or sha256(payload).hexdigest() != reference.content_hash:
        raise ContractResolutionError("首次合同历史行情字节与DataAssetRef哈希不一致")
    rows = _market_history_rows(payload)
    value = deepcopy(dict(request))
    raw_identity = value.get("identity", {})
    if not isinstance(raw_identity, Mapping):
        raise ContractResolutionError("identity必须为对象")
    identity = deepcopy(dict(raw_identity))
    underlyings = identity.get("underlyings")
    if isinstance(underlyings, str):
        assets = tuple(item.strip() for item in underlyings.split(",") if item.strip())
    elif isinstance(underlyings, (list, tuple)):
        assets = tuple(str(item).strip() for item in underlyings if str(item).strip())
    else:
        assets = ()
    if not assets or len(set(assets)) != len(assets):
        raise ContractResolutionError("identity.underlyings必须为无重复标的列表")
    if module == "payoffer":
        supplied_start = identity.get("contract_start_date")
        if supplied_start not in {None, ""}:
            start = _iso_date(supplied_start, "identity.contract_start_date")
        else:
            coverage_end = reference.coverage.get("end_date")
            start = _latest_common_history_date(
                rows,
                assets,
                _iso_date(coverage_end, "DataAssetRef.coverage.end_date"),
            )
    elif module == "pricer":
        config = value.get("pricing_config")
        if not isinstance(config, Mapping):
            raise ContractResolutionError("pricing_config必须为对象")
        valuation = _iso_date(config.get("valuation_date"), "pricing_config.valuation_date")
        auto_start = value.pop("auto_contract_start_date", False)
        if not isinstance(auto_start, bool):
            raise ContractResolutionError("auto_contract_start_date必须为布尔值")
        supplied_start = identity.get("contract_start_date")
        if auto_start and supplied_start not in {None, ""}:
            raise ContractResolutionError("自动合同起始日不得同时提交具体日期")
        start = _iso_date(supplied_start or valuation, "identity.contract_start_date")
        if start > valuation:
            raise ContractResolutionError("合同起始日不得晚于请求估值日")
        if auto_start:
            start = _latest_common_history_date(rows, assets, valuation)
    else:
        config = value.get("backtest_config")
        config = config if isinstance(config, Mapping) else {}
        start_value = first_backtest_entry(config, reference.coverage)
        start = _iso_date(start_value, "identity.contract_start_date")
        supplied_start = identity.get("contract_start_date")
        if supplied_start not in {None, ""} and _iso_date(
            supplied_start, "identity.contract_start_date",
        ) != start:
            raise ContractResolutionError(
                "identity.contract_start_date与正式回测入场区间不一致"
            )
    expected = _reference_prices(rows, assets, start)
    supplied = identity.get("reference_prices")
    if supplied is not None:
        if not isinstance(supplied, Mapping) or set(supplied) != set(expected):
            raise ContractResolutionError("identity.reference_prices必须逐一覆盖标的")
        for asset, expected_value in expected.items():
            try:
                actual = float(supplied[asset])
            except (TypeError, ValueError) as error:
                raise ContractResolutionError(f"reference_prices.{asset}必须为正数") from error
            if actual <= 0 or not isclose(actual, expected_value, rel_tol=1e-10, abs_tol=1e-8):
                raise ContractResolutionError(f"reference_prices.{asset}与Host验证的未复权close不一致")
    identity["contract_start_date"] = start
    identity["reference_prices"] = expected
    value["identity"] = identity
    return value


def _freeze_first_contract_end_date(module: str, request: Mapping[str, Any]) -> dict[str, Any]:
    """Close a first formal contract with Core's sole civil-tenor rule."""

    value = deepcopy(dict(request))
    raw_identity = value.get("identity", {})
    if not isinstance(raw_identity, Mapping):
        raise ContractResolutionError("identity必须为对象")
    identity = deepcopy(dict(raw_identity))
    start = identity.get("contract_start_date")
    if start in {None, ""} or identity.get("contract_end_date") not in {None, ""}:
        return value
    requirements = compile_compute_data_requirements(module, value)
    identity["contract_end_date"] = derive_contract_end_date(start, requirements.tenor_years)
    value["identity"] = identity
    return value


def _market_history_rows(payload: bytes) -> tuple[dict[str, Any], ...]:
    try:
        text = payload.decode("utf-8-sig")
        reader = csv.DictReader(StringIO(text))
    except UnicodeDecodeError as error:
        raise ContractResolutionError("历史行情CSV编码无效") from error
    required = {"date", "asset_id", "close", "adj_close"}
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        raise ContractResolutionError("历史行情必须含date、asset_id、close、adj_close")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in reader:
        try:
            day = date.fromisoformat(str(raw["date"]).strip()).isoformat()
            asset = str(raw["asset_id"]).strip()
            close = float(raw["close"])
            adjusted = float(raw["adj_close"])
        except (TypeError, ValueError) as error:
            raise ContractResolutionError("历史行情含无效date、asset_id、close或adj_close") from error
        if not asset or not isfinite(close) or not isfinite(adjusted) or close <= 0 or adjusted <= 0 or (day, asset) in seen:
            raise ContractResolutionError("历史行情含无效或重复的date、asset_id、close、adj_close")
        seen.add((day, asset))
        rows.append({"date": day, "asset_id": asset, "close": close})
    if not rows:
        raise ContractResolutionError("历史行情为空")
    return tuple(sorted(rows, key=lambda item: (item["date"], item["asset_id"])))


def _latest_common_history_date(rows: Sequence[Mapping[str, Any]], assets: Sequence[str], cutoff: str) -> str:
    dates_by_asset = {
        asset: {str(row["date"]) for row in rows if row["asset_id"] == asset and str(row["date"]) <= cutoff}
        for asset in assets
    }
    common = set.intersection(*(values for values in dates_by_asset.values())) if dates_by_asset else set()
    if not common:
        raise ContractResolutionError("DataAssetRef未覆盖全部标的在请求估值日或此前的共同未复权close")
    return max(common)


def _reference_prices(rows: Sequence[Mapping[str, Any]], assets: Sequence[str], cutoff: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for asset in assets:
        matches = [row for row in rows if row["asset_id"] == asset and str(row["date"]) <= cutoff]
        if not matches:
            raise ContractResolutionError(f"DataAssetRef未覆盖{asset}在合同起始日或此前的未复权close")
        values[asset] = float(matches[-1]["close"])
    return values


def first_backtest_entry(config: Mapping[str, Any], coverage: Mapping[str, Any]) -> object:
    """Return the first declared backtest entry from formal request or coverage fields."""

    entries = config.get("entry_dates")
    candidates = list(entries) if isinstance(entries, list) else []
    candidates.extend((config.get("start_date"), coverage.get("start_date")))
    return next((item for item in candidates if item not in {None, ""}), None)


def _iso_date(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ContractResolutionError(f"{label}必须为YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ContractResolutionError(f"{label}必须为YYYY-MM-DD") from error


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
    declared_sessions = coverage.get("sessions")
    if declared_sessions is not None and (
        isinstance(declared_sessions, (str, bytes))
        or not isinstance(declared_sessions, Sequence)
        or tuple(str(item) for item in declared_sessions) != normalized
    ):
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
    sessions = _history_sessions_in_calendar(coverage, calendar)
    coverage.update({
        "calendar_id": calendar["calendar_id"],
        "calendar_revision": calendar["calendar_revision"],
        "sessions": list(sessions),
        # Coverage boundaries are canonicalized to actual observed sessions.
        # A request or metadata boundary may be a weekend/holiday and must not
        # be treated as a required trading session.
        "start_date": sessions[0],
        "end_date": sessions[-1],
        "calendar_coverage_end": sessions[-1],
        "calendar_ref": calendar["calendar_ref"],
    })
    return {**history, "coverage": coverage}


def _history_sessions_in_calendar(
    coverage: Mapping[str, Any],
    calendar: Mapping[str, Any],
) -> tuple[str, ...]:
    """Validate a history session set without treating metadata dates as sessions.

    The persisted history asset is authoritative for which sessions were
    actually observed.  The verified calendar must contain that set, and it
    must contain every calendar session between the observed first and last
    session.  This accepts non-trading request/metadata boundaries while still
    rejecting an internal missing trading day.
    """

    raw_history_sessions = coverage.get("sessions")
    if isinstance(raw_history_sessions, (str, bytes)) or not isinstance(raw_history_sessions, (list, tuple)):
        raise ContractResolutionError("历史行情DataAssetRef.coverage必须声明实际交易sessions")
    try:
        history_sessions = tuple(date.fromisoformat(str(value)).isoformat() for value in raw_history_sessions)
    except (TypeError, ValueError) as error:
        raise ContractResolutionError("历史行情DataAssetRef.coverage.sessions必须为YYYY-MM-DD") from error
    if not history_sessions or history_sessions != tuple(sorted(history_sessions)) or len(set(history_sessions)) != len(history_sessions):
        raise ContractResolutionError("历史行情DataAssetRef.coverage.sessions必须严格递增且不重复")
    calendar_sessions = tuple(str(value) for value in calendar.get("sessions", ()))
    if any(day not in set(calendar_sessions) for day in history_sessions):
        raise ContractResolutionError("Host验证交易日历未覆盖历史行情实际交易日")
    expected = tuple(day for day in calendar_sessions if history_sessions[0] <= day <= history_sessions[-1])
    if history_sessions != expected:
        raise ContractResolutionError("Host验证交易日历未完整覆盖历史行情实际交易日集合")
    return history_sessions


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
    history_sessions = _history_sessions_in_calendar(coverage, calendar)
    if (
        coverage.get("calendar_id") != calendar["calendar_id"]
        or coverage.get("calendar_revision") != calendar["calendar_revision"]
        or tuple(str(value) for value in coverage.get("sessions", ())) != history_sessions
        or coverage.get("calendar_coverage_end") != history_sessions[-1]
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
    "compile_compute_data_requirements",
    "ComputeDataRequirements",
    "prepare_compute_request",
    "verified_trading_calendar",
)
