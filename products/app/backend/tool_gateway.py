"""Single authorized App-to-Capability Tool boundary.

The gateway imports the embedded, verified Capability at call time.  It does
not duplicate or reinterpret module business logic and it returns genuine
Capability failures rather than synthetic financial output.
"""

from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path
import re
from typing import Any, Callable, Mapping
from uuid import uuid4

from .authorization.policy import AuthorizationPolicy
from .capability_service import capability_import_scope, load_verified_capability_service
from .datafetcher_adapter import DataFetcherAdapter
from .errors import AuthorizationError, UnavailableCapabilityError, UserActionError, ValidationError
from .identity.session_identity import SessionIdentity
from .page_registry import PAGE_MODULES, PageRegistry
from .reporter_adapter import ReporterAdapter
from .stores.result_store import ResultStore
from .stores.data_store import DataStore
from .stores.contract_store import ContractStore
from runtime.contracts.input_adapter import explicit_demo_pricing_error, is_explicit_demo_pricing_config
from runtime.protocol.module_host import HostObjectRef, ModuleHostContextError, require_host_bound_run_contract
from runtime.protocol.models import CallerContext
from runtime.ports.tool_gateway import ToolGatewayPort
from modules.pricer.calendar_policy import requires_future_trading_calendar_for_protocol


class ToolGateway:
    def __init__(
        self,
        registry: PageRegistry,
        policy: AuthorizationPolicy,
        reporter: ReporterAdapter | None = None,
        datafetcher: DataFetcherAdapter | None = None,
        results: ResultStore | None = None,
        data_assets: DataStore | None = None,
        contracts: ContractStore | None = None,
        capability_runtime_root: str | Path | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._reporter = reporter
        self._datafetcher = datafetcher
        self._results = results
        self._data_assets = data_assets
        self._contracts = contracts
        self._capability_runtime_root = (
            Path(capability_runtime_root).expanduser().resolve()
            if capability_runtime_root is not None else None
        )

    def bind_internal(
        self,
        caller_context: SessionIdentity,
        context_for: Callable[[str, Mapping[str, Any]], object],
    ) -> ToolGatewayPort:
        """Bind an authenticated Host caller for in-process module orchestration.

        Recommender receives only the resulting Core ``ToolGatewayPort``.  It
        cannot choose a principal, capability token or replay id, and it does
        not need to know an App HTTP route.
        """
        if not callable(context_for):
            raise TypeError("context_for must be callable")
        return _BoundInternalToolGateway(self, caller_context, context_for)

    def dispatch(
        self,
        tool_name: str,
        payload: dict[str, Any],
        caller_context: SessionIdentity,
        *,
        module_context: object | None = None,
        request_id: str = "",
        agent_proxy: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValidationError("Tool payload must be an object")
        if tool_name not in PAGE_MODULES and tool_name != "recommender":
            raise ValidationError(f"Unknown Capability tool: {tool_name}")
        read_only_action = _is_read_only_action(payload)
        if agent_proxy:
            self._policy.require(caller_context.role, "conversation.tool.run")
            _require_conversation_tool_scope(caller_context, tool_name, payload)
            granted_capability = "conversation.tool.run"
        elif tool_name == "reporter" and not read_only_action:
            kind = payload.get("kind") if isinstance(payload, dict) else None
            granted_capability = "report.card.request" if kind == "card" else "report.full.request"
            self._policy.require(caller_context.role, granted_capability)
        else:
            granted_capability = "module.catalog" if read_only_action else "module.run"
            self._policy.require(caller_context.role, granted_capability)
        _reject_untrusted_host_fields(payload)
        _reject_path_payload(payload)
        compute_run = tool_name in {"payoffer", "pricer", "backtester"} and _is_run_action(payload)
        verified_context = None
        if tool_name in PAGE_MODULES:
            if module_context is None:
                raise ValidationError("Module Host context is required")
            verified_context = self._registry.validate_host_request(caller_context, tool_name, module_context, request_id)
            required_policy = (
                "conversation.tool.run" if agent_proxy
                else "module.run" if tool_name == "reporter" and not read_only_action
                else granted_capability
            )
            if required_policy not in verified_context.request_policy:
                raise AuthorizationError(required_policy, "Module Host context is not issued for this invocation")
            _validate_host_scope(payload, verified_context)
        if tool_name == "reporter":
            if self._reporter is None:
                raise UnavailableCapabilityError("reporter", "App ReporterAdapter is not configured")
            return self._reporter.dispatch(payload, caller_context)
        if tool_name == "datafetcher":
            if self._datafetcher is None:
                raise UnavailableCapabilityError("datafetcher", "App DataFetcherAdapter is not configured")
            return self._datafetcher.dispatch(payload, caller_context, request_id=request_id)
        self._registry.assert_execution_integrity()
        scripts_root = str(self._registry.capability_root / "scripts")
        with capability_import_scope(
            scripts_root,
            runtime_root=self._capability_runtime_root,
        ):
            try:
                tool_entry = self._load_tool_entry(scripts_root)
                # ``tool_entry.call_tool`` imports this exact name internally.
                # Preloading and verifying it inside the same isolated scope
                # prevents a development/Eval ``modules.*`` cache from being
                # reused by an otherwise verified Capability.
                load_verified_capability_service(tool_name, scripts_root)
                effective_payload = payload
                effective_context = verified_context
                bound_data_store = None
                if compute_run:
                    if not verified_context.task_id:
                        raise ValidationError("计算模块必须绑定App任务")
                    if not callable(getattr(tool_entry, "prepare_compute_request", None)):
                        raise ValidationError("Capability缺少正式计算输入编译器")
                    if self._contracts is None:
                        raise ValidationError("App ContractStore未配置，无法冻结ResolvedContract")
                    if tool_name in {"pricer", "backtester"}:
                        if self._datafetcher is None:
                            raise UnavailableCapabilityError(
                                f"{tool_name}.data_store", "App DataFetcherAdapter is not configured",
                            )
                        bound_data_store = self._datafetcher.bind_data_store(caller_context)
                    friendly_payload = _business_payload(payload)
                    existing = self._contracts.get(caller_context, str(verified_context.task_id))
                    data_refs = self._resolve_compute_data_refs(
                        tool_name,
                        friendly_payload,
                        caller_context,
                        existing,
                        request_id=request_id,
                    )
                    prepared = tool_entry.prepare_compute_request(
                        tool_name,
                        friendly_payload,
                        data_refs=tuple(data_refs),
                        resolved_contract=existing.get("resolved_contract") if existing else None,
                        data_store=bound_data_store,
                    )
                    if not isinstance(prepared, Mapping) or not isinstance(prepared.get("request"), Mapping):
                        raise ValidationError("Capability返回的正式计算输入无效")
                    effective_payload = dict(prepared["request"])
                    binding = self._contracts.bind(
                        caller_context,
                        str(verified_context.task_id),
                        prepared,
                        catalog_version=str(self._registry.manifest["catalog_version"]),
                    )
                    effective_context = self._bind_contract_context(caller_context, verified_context, binding)
                _reject_path_payload(effective_payload)
                bound_store = None
                if compute_run:
                    if self._results is None or effective_context.task_id is None:
                        raise ValidationError("计算模块必须绑定App任务与ResultStore")
                    bound_store = self._results.bind_module_store(caller_context, effective_context.task_id)
                result = tool_entry.call_tool(
                    tool_name,
                    effective_payload,
                    caller_context=CallerContext(
                        tenant_id=caller_context.tenant_id,
                        principal_id=caller_context.principal_id,
                        role=caller_context.role.value,
                        capabilities=(granted_capability,),
                        session_id=caller_context.session_id,
                        audience=caller_context.audience,
                        request_id=request_id,
                    ),
                    host_context=effective_context,
                    result_store=bound_store,
                    data_store=bound_data_store,
                )
                if compute_run:
                    if not isinstance(result, Mapping):
                        raise ValidationError("Capability计算结果必须为对象")
                    result = _bind_compute_result(result, effective_context)
            except UserActionError:
                # This class is only created by an App-owned preflight.  Its
                # message is a fixed, reviewed next step and contains no
                # capability path, exception trace or credential detail.
                raise
            except Exception as error:
                raise UnavailableCapabilityError(
                    f"Capability tool {tool_name}",
                    "the verified Capability rejected or could not execute this request",
                ) from error
        if not isinstance(result, dict):
            raise ValidationError("Capability tool must return a JSON object")
        if compute_run and str(result.get("status", "")).lower() in {"succeeded", "partial"}:
            try:
                require_host_bound_run_contract(result, effective_context)
            except ModuleHostContextError as error:
                raise ValidationError(str(error)) from error
        return result

    def _resolve_compute_data_refs(
        self,
        module: str,
        payload: Mapping[str, Any],
        identity: SessionIdentity,
        existing_contract: Mapping[str, Any] | None = None,
        *,
        request_id: str = "",
    ) -> list[dict[str, Any]]:
        if module == "payoffer":
            return []
        if self._data_assets is None:
            raise ValidationError("App DataStore未配置，无法绑定正式计算数据")
        raw_identity = payload.get("identity", {})
        if not isinstance(raw_identity, Mapping):
            raise ValidationError("identity必须为对象")
        frozen = existing_contract.get("resolved_contract") if isinstance(existing_contract, Mapping) else None
        frozen_identity = frozen.get("identity", {}) if isinstance(frozen, Mapping) else {}
        underlyings = frozen_identity.get("underlyings", raw_identity.get("underlyings", []))
        if isinstance(underlyings, str):
            underlyings = [item.strip() for item in underlyings.split(",") if item.strip()]
        if not isinstance(underlyings, (list, tuple)) or not underlyings:
            raise ValidationError("计算请求必须填写underlyings")
        requested: object = None
        if module == "pricer":
            raw_refs = payload.get("market_data_refs")
            if raw_refs is not None:
                if not isinstance(raw_refs, list) or len(raw_refs) != 1:
                    raise ValidationError("当前Pricer页面只能选择一个DataAssetRef")
                requested = raw_refs[0]
            else:
                config = payload.get("pricing_config")
                if isinstance(config, Mapping) and config.get("demo_mode") is True:
                    error = explicit_demo_pricing_error(config)
                    if error is not None:
                        raise UserActionError("invalid_demo_market_snapshot", error)
                    if is_explicit_demo_pricing_config(config):
                        demo_product_id = frozen_identity.get("product_id", payload.get("product_id"))
                        if str(demo_product_id) != "2.1":
                            raise UserActionError(
                                "invalid_demo_market_snapshot",
                                "无行情数据的MC10演示仅支持2.1看涨期权；其他结构请先在数据获取中绑定真实日频行情。",
                            )
                        return []
        else:
            requested = payload.get("historical_data", payload.get("history_reference"))
        history_ref = self._data_assets.resolve_for_compute(
            identity,
            requested,
            asset_ids=tuple(map(str, underlyings)),
            schema_id="market-history-v1",
        )
        if history_ref is None:
            raise ValidationError("正式计算缺少market-history-v1行情资产")
        refs = [history_ref]
        if module == "pricer":
            requested_calendar = payload.get("trading_calendar_ref")
            requires_calendar = _requires_path_calendar(frozen, payload)
            has_requested_calendar = requested_calendar is not None and requested_calendar != ""
            if requires_calendar or has_requested_calendar:
                try:
                    calendar_ref = self._data_assets.resolve_for_compute(
                        identity,
                        requested_calendar,
                        asset_ids=tuple(map(str, underlyings)),
                        schema_id="trading-calendar",
                        optional=False,
                    )
                except UserActionError:
                    if has_requested_calendar or not requires_calendar:
                        raise
                    if self._datafetcher is None:
                        raise UnavailableCapabilityError(
                            "pricer.trading_calendar", "App DataFetcherAdapter is not configured",
                        )
                    calendar_result = self._datafetcher.dispatch(
                        _calendar_fetch_request(payload, frozen, tuple(map(str, underlyings))),
                        identity,
                        request_id=f"{request_id}-calendar" if request_id else f"calendar-{uuid4().hex}",
                    )
                    calendar_asset = calendar_result.get("data_asset_ref")
                    if calendar_result.get("ok") is not True or not isinstance(calendar_asset, dict):
                        raise UserActionError(
                            "trading_calendar_unavailable",
                            "交易日历暂不可用，且没有完整覆盖本次区间的已验证缓存。",
                        )
                    self._data_assets.register(identity, calendar_asset)
                    calendar_ref = self._data_assets.resolve_for_compute(
                        identity,
                        calendar_asset["data_asset_id"],
                        asset_ids=tuple(map(str, underlyings)),
                        schema_id="trading-calendar",
                    )
                if calendar_ref is not None:
                    refs.append(calendar_ref)
        return refs

    def _bind_contract_context(self, identity: SessionIdentity, context: Any, binding: Mapping[str, Any]) -> Any:
        fingerprint = binding.get("contract_fingerprint")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise ValidationError("ResolvedContract缺少有效contract_fingerprint")
        task_id = context.task_id
        catalog_version = context.catalog_version or str(binding["catalog_version"])
        contract_ref = binding.get("contract_ref")
        if not isinstance(contract_ref, Mapping):
            raise ValidationError("Host contract_ref绑定无效")
        return self._registry.bind_contract_context(
            identity,
            context,
            analysis_case_id=context.analysis_case_id or f"case-{fingerprint[:24]}",
            candidate_id=context.candidate_id or f"candidate-{fingerprint[:24]}",
            catalog_version=catalog_version,
            contract_fingerprint=fingerprint,
            contract_ref=HostObjectRef.from_payload(contract_ref, "contract_ref"),
        )

    def _load_tool_entry(self, _scripts_root: str) -> Any:
        path = self._registry.capability_root / "scripts" / "tool_entry.py"
        if not path.is_file():
            raise UnavailableCapabilityError("ToolGateway", "verified Capability does not provide scripts/tool_entry.py")
        spec = importlib.util.spec_from_file_location("option_helper_embedded_tool_entry", path)
        if spec is None or spec.loader is None:
            raise UnavailableCapabilityError("ToolGateway", "cannot load embedded Capability tool entry")
        module = importlib.util.module_from_spec(spec)
        # The embedded Capability is read-only.  Its source must not acquire
        # ``__pycache__`` artefacts merely because App dispatches a tool.
        spec.loader.exec_module(module)
        return module


class _BoundInternalToolGateway:
    def __init__(
        self,
        gateway: ToolGateway,
        caller_context: SessionIdentity,
        context_for: Callable[[str, Mapping[str, Any]], object],
    ) -> None:
        self._gateway = gateway
        self._caller_context = caller_context
        self._context_for = context_for

    def call(self, module: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(module, str) or not module or not isinstance(request, Mapping):
            raise ValidationError("internal ToolGateway call requires module and request")
        payload = dict(request)
        module_context = self._context_for(module, payload)
        return self._gateway.dispatch(
            module,
            payload,
            self._caller_context,
            module_context=module_context,
            request_id=f"internal_{uuid4().hex}",
        )


_RAW_PATH_FIELDS = {"requestpath", "outputroot", "historyreference", "resultdir", "directory", "path", "filepath", "localcsv", "csvpath"}
_HOST_PRIVATE_FIELDS = {
    "tenantid", "principalid", "sessionid", "callercontext", "hostcontext",
    "capabilitytoken", "datastore", "datastoreport",
}


def _normalized_payload_key(key: object) -> str:
    return "".join(character for character in str(key).casefold() if character.isalnum())


def _reject_path_payload(value: object) -> None:
    """Reject browser-supplied physical path knobs before Capability dispatch."""
    if isinstance(value, dict):
        for key, item in value.items():
            if _normalized_payload_key(key) in _RAW_PATH_FIELDS:
                raise ValidationError(f"{key} is not accepted by the App Host; use an opaque Store reference")
            _reject_path_payload(item)
    elif isinstance(value, list):
        for item in value:
            _reject_path_payload(item)


def _reject_untrusted_host_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _normalized_payload_key(key) in _HOST_PRIVATE_FIELDS:
                raise ValidationError(f"{key}由App Host管理，页面不得提交")
            _reject_untrusted_host_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_untrusted_host_fields(item)


def _is_read_only_action(payload: Mapping[str, Any]) -> bool:
    action = payload.get("action", "run")
    return isinstance(action, str) and action.strip().lower() in {"catalog", "status", "list_assets"}


def _validate_host_scope(payload: dict[str, Any], context: Any) -> None:
    """Reject scope conflicts without polluting a module's business input."""
    for field in ("task_id", "analysis_case_id", "candidate_id", "catalog_version", "contract_fingerprint"):
        bound = getattr(context, field)
        supplied = payload.get(field)
        if bound is None:
            if supplied is not None:
                raise ValidationError(f"{field} is not granted by this Module Host context")
            continue
        if supplied is not None and supplied != bound:
            raise ValidationError(f"{field} does not match the Module Host context")


def _business_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    for field in ("task_id", "analysis_case_id", "candidate_id", "catalog_version", "contract_fingerprint"):
        value.pop(field, None)
    return value


def _bind_compute_result(result: Mapping[str, Any], context: Any) -> dict[str, Any]:
    """Attach the already verified Host binding before immutable run storage."""

    bound = {
        "analysis_case_id": getattr(context, "analysis_case_id", None),
        "candidate_id": getattr(context, "candidate_id", None),
        "catalog_version": getattr(context, "catalog_version", None),
        "contract_fingerprint": getattr(context, "contract_fingerprint", None),
    }
    if any(not isinstance(value, str) or not value for value in bound.values()):
        raise ValidationError("计算结果缺少Host绑定的候选合同标识")
    value = dict(result)
    for field, expected in bound.items():
        supplied = value.get(field)
        if supplied is not None and supplied != expected:
            raise ValidationError(f"Capability计算结果{field}与Host绑定冲突")
        value[field] = expected
    return value


def _is_run_action(payload: Mapping[str, Any]) -> bool:
    """Only a declared ``run`` receives the App's task/store execution gate.

    Catalogs and any other module-declared read-only action still pass through
    a verified Host context, role policy and request id.  The Capability itself
    remains the sole authority for whether that action is supported.
    """

    return str(payload.get("action", "run")).strip().lower() == "run"


def _requires_path_calendar(resolved_contract: object, payload: Mapping[str, Any]) -> bool:
    """Match the formal Pricer's selected-route calendar requirement."""

    config = payload.get("pricing_config")
    method = config.get("model_method", "auto") if isinstance(config, Mapping) else "auto"
    return requires_future_trading_calendar_for_protocol(resolved_contract, method)


def _calendar_fetch_request(
    payload: Mapping[str, Any],
    resolved_contract: object,
    underlyings: tuple[str, ...],
) -> dict[str, Any]:
    config = payload.get("pricing_config")
    raw_valuation = config.get("valuation_date") if isinstance(config, Mapping) else None
    try:
        valuation = date.fromisoformat(str(raw_valuation or date.today().isoformat()))
    except ValueError as error:
        raise ValidationError("valuation_date必须为YYYY-MM-DD") from error
    identity = resolved_contract.get("identity", {}) if isinstance(resolved_contract, Mapping) else {}
    terms = resolved_contract.get("terms", {}) if isinstance(resolved_contract, Mapping) else {}
    raw_end = identity.get("contract_end_date") if isinstance(identity, Mapping) else None
    if raw_end:
        try:
            maturity = date.fromisoformat(str(raw_end))
        except ValueError as error:
            raise ValidationError("contract_end_date必须为YYYY-MM-DD") from error
    else:
        years = terms.get("T", 1.0) if isinstance(terms, Mapping) else 1.0
        try:
            maturity = valuation + timedelta(days=round(float(years) * 365.0))
        except (TypeError, ValueError) as error:
            raise ValidationError("路径型定价需要有效合同期限") from error
    return {
        "action": "fetch_calendar",
        "asset_ids": list(underlyings),
        "start_date": (valuation - timedelta(days=14)).isoformat(),
        "end_date": (maturity + timedelta(days=14)).isoformat(),
    }


def _require_conversation_tool_scope(identity: SessionIdentity, tool_name: str, payload: Mapping[str, Any]) -> None:
    """Second, non-UI permission barrier for the OptChat proxy path.

    This is deliberately narrower than ``module.run``.  The model can request
    a documented action only; it cannot choose a path, arbitrary provider
    routing, broader data range, data asset outside its task, or report type
    outside the role policy.  Task-owned DataAssetRef validation happens in
    :class:`ToolDispatcher` because it owns the task index.
    """
    action = str(payload.get("action", "run")).strip().lower()
    allowed = {
        "datafetcher": {"status", "fetch", "fetch_calendar"}, "payoffer": {"run"}, "pricer": {"run"},
        "backtester": {"run"}, "reporter": {"run"},
    }
    if tool_name not in allowed or action not in allowed[tool_name]:
        raise AuthorizationError("conversation.tool.run", f"tool={tool_name}, action={action} is not allowed")
    if tool_name == "datafetcher":
        _require_conversation_data_request(action, payload)
    elif tool_name in {"payoffer", "pricer", "backtester"}:
        _require_conversation_compute_request(tool_name, payload)
    if tool_name == "reporter":
        _require_conversation_report_request(identity, payload)


_ASSET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_DATA_FIELDS = frozenset({"open", "high", "low", "close", "adj_close", "volume", "amount"})


def _require_conversation_data_request(action: str, payload: Mapping[str, Any]) -> None:
    allowed = {"action", "task_id"} if action == "status" else {
        "action", "task_id", "asset_id", "asset_ids", "start_date", "end_date", "fields", "provider", "frequency", "adjustment", "cache_policy", "offline",
    }
    _require_scope_fields(payload, allowed)
    if action == "status":
        return
    assets = payload.get("asset_ids", payload.get("asset_id"))
    if isinstance(assets, str):
        assets = [assets]
    if not isinstance(assets, list) or not 1 <= len(assets) <= 3 or any(not _ASSET_ID.fullmatch(str(item)) for item in assets):
        raise AuthorizationError("conversation.tool.run", "data asset scope is invalid")
    fields = payload.get("fields")
    if action != "fetch_calendar":
        if not isinstance(fields, list) or not fields or len(fields) > len(_DATA_FIELDS) or any(str(item) not in _DATA_FIELDS for item in fields):
            raise AuthorizationError("conversation.tool.run", "data field scope is invalid")
    try:
        start = date.fromisoformat(str(payload.get("start_date", "")))
        end = date.fromisoformat(str(payload.get("end_date", "")))
    except ValueError as error:
        raise AuthorizationError("conversation.tool.run", "data date scope is invalid") from error
    if end < start or (end - start).days > 366 * 5:
        raise AuthorizationError("conversation.tool.run", "data date scope exceeds conversation limit")
    if payload.get("provider") not in {None, "", "ifind_http"}:
        raise AuthorizationError("conversation.tool.run", "data provider is not allowed for conversation")
    if payload.get("frequency", "1d") != "1d" or payload.get("cache_policy", "force_refresh") != "force_refresh":
        raise AuthorizationError("conversation.tool.run", "data request mode is not allowed for conversation")
    if payload.get("offline") not in {None, False}:
        raise AuthorizationError("conversation.tool.run", "offline data is not allowed for conversation")


def _require_conversation_compute_request(tool_name: str, payload: Mapping[str, Any]) -> None:
    allowed = {"action", "task_id", "product_id", "identity", "term_overrides"}
    if tool_name == "pricer":
        allowed.update({"pricing_config", "market_data_refs", "trading_calendar_ref"})
    elif tool_name == "backtester":
        allowed.update({"backtest_config", "historical_data"})
    _require_scope_fields(payload, allowed)
    if not isinstance(payload.get("product_id"), str) or not str(payload["product_id"]).strip():
        raise AuthorizationError("conversation.tool.run", "compute product scope is invalid")
    if not isinstance(payload.get("identity", {}), Mapping) or not isinstance(payload.get("term_overrides", {}), Mapping):
        raise AuthorizationError("conversation.tool.run", "compute input scope is invalid")
    if tool_name == "pricer" and not isinstance(payload.get("pricing_config"), Mapping):
        raise AuthorizationError("conversation.tool.run", "pricing configuration is required")
    if tool_name == "backtester" and not isinstance(payload.get("backtest_config"), Mapping):
        raise AuthorizationError("conversation.tool.run", "backtest configuration is required")


def _require_conversation_report_request(identity: SessionIdentity, payload: Mapping[str, Any]) -> None:
    _require_scope_fields(payload, {"action", "task_id", "kind", "selection"})
    kind = str(payload.get("kind", "")).strip().lower()
    selection = payload.get("selection")
    if kind not in {"card", "report"} or not isinstance(selection, Mapping) or selection.get("output_type") != kind:
        raise AuthorizationError("conversation.tool.run", "report output type is invalid")
    if kind == "report" and identity.role.value != "admin":
        raise AuthorizationError("conversation.tool.run", "full reports require administrator approval")
    _require_scope_fields(selection, {
        "source_id", "candidate_ids", "selected_modules", "delivery_mode", "output_type", "format", "html_report_layout", "audience", "report_run_id", "metadata",
    })


def _require_scope_fields(value: Mapping[str, Any], allowed: set[str]) -> None:
    if set(value).difference(allowed):
        raise AuthorizationError("conversation.tool.run", "request contains fields outside the conversation scope")
