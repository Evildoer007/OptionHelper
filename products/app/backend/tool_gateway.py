"""Single authorized App-to-Capability Tool boundary.

The gateway imports the embedded, verified Capability at call time.  It does
not duplicate or reinterpret module business logic and it returns genuine
Capability failures rather than synthetic financial output.
"""

from __future__ import annotations

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
from runtime.capability_import import load_verified_source_module, verified_capability_modules
from runtime.protocol.module_host import HostObjectRef, ModuleHostContextError, require_host_bound_run_contract
from runtime.protocol.models import CallerContext, ModuleRunRef
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
        tasks: object | None = None,
        capability_runtime_root: str | Path | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._reporter = reporter
        self._datafetcher = datafetcher
        self._results = results
        self._data_assets = data_assets
        self._contracts = contracts
        self._tasks = tasks
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
        _reject_untrusted_host_fields(
            payload,
            allow_reporter_module_run_refs=(tool_name == "reporter" and isinstance(payload.get("selection"), Mapping)),
        )
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
            if read_only_action:
                return self._reporter.dispatch(payload, caller_context)
            if verified_context is None:
                raise ValidationError("Reporter运行缺少已验证Module Host context")
            return self._reporter.dispatch(
                self._controlled_reporter_request(payload, caller_context, verified_context),
                caller_context,
            )
        if tool_name == "datafetcher":
            if self._datafetcher is None:
                raise UnavailableCapabilityError("datafetcher", "App DataFetcherAdapter is not configured")
            return self._datafetcher.dispatch(payload, caller_context, request_id=request_id)
        scripts_root = str(self._registry.capability_root / "scripts")
        with capability_import_scope(
            scripts_root,
            runtime_root=self._capability_runtime_root,
        ):
            try:
                # The registry checks layout and manifest provenance.  Every
                # source is then reopened once with O_NOFOLLOW, hash-checked,
                # and compiled from that exact byte buffer below.  A rename
                # after this point therefore fails closed or cannot change the
                # code executing in this request.
                self._registry.assert_execution_integrity()
                content_hashes = self._registry.manifest.get("content_hashes")
                if not isinstance(content_hashes, Mapping):
                    raise UnavailableCapabilityError("ToolGateway", "Capability content hashes are unavailable")
                with verified_capability_modules(scripts_root, content_hashes):
                    tool_entry = self._load_tool_entry(scripts_root, content_hashes)
                    # ``tool_entry.call_tool`` imports this exact name internally.
                    # Preloading inside the verified loader prevents development
                    # or Eval ``modules.*`` objects from being reused.
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
                        friendly_payload = _business_payload(payload)
                        existing = self._contracts.get(caller_context, str(verified_context.task_id))
                        data_refs = self._resolve_compute_data_refs(
                            tool_name,
                            friendly_payload,
                            caller_context,
                            existing,
                            request_id=request_id,
                            task_id=str(verified_context.task_id),
                        )
                        if self._tasks is None or not callable(getattr(self._tasks, "get", None)):
                            raise ValidationError("App TaskService未配置，无法绑定正式计算DataAssetRef")
                        task = self._tasks.get(caller_context, str(verified_context.task_id))
                        task_refs = task.get("data_asset_refs") if isinstance(task, Mapping) else None
                        if not isinstance(task_refs, list):
                            raise ValidationError("当前任务缺少受控DataAssetRef索引")
                        attached = {
                            (str(item.get("data_asset_id")), str(item.get("content_hash")))
                            for item in task_refs if isinstance(item, Mapping)
                        }
                        if any(
                            (str(ref.get("data_asset_id")), str(ref.get("content_hash"))) not in attached
                            for ref in data_refs
                        ):
                            raise ValidationError("正式计算DataAssetRef必须已绑定当前App任务")
                        if tool_name in {"pricer", "backtester"} and data_refs:
                            if self._datafetcher is None:
                                raise UnavailableCapabilityError(
                                    f"{tool_name}.data_store", "App DataFetcherAdapter is not configured",
                                )
                            bound_data_store = self._datafetcher.bind_data_store(
                                caller_context,
                                task_id=str(verified_context.task_id),
                                data_refs=tuple(data_refs),
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
                    authorize = getattr(tool_entry, "_authorize_verified_app_call", None)
                    if not callable(authorize):
                        raise UnavailableCapabilityError(
                            "Capability protocol",
                            "the verified Capability does not implement the current App authorization handoff",
                        )
                    result = tool_entry.call_tool(
                        tool_name,
                        effective_payload,
                        authorization=authorize(
                            CallerContext(
                                tenant_id=caller_context.tenant_id,
                                principal_id=caller_context.principal_id,
                                role=caller_context.role.value,
                                capabilities=(granted_capability,),
                                session_id=caller_context.session_id,
                                audience=caller_context.audience,
                                request_id=request_id,
                            ),
                            effective_context,
                        ),
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
        task_id: str = "",
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
        assets = tuple(map(str, underlyings))
        history_request = _history_fetch_request(module, payload, frozen, assets)
        try:
            history_ref = self._data_assets.resolve_for_compute(
                identity,
                requested,
                asset_ids=assets,
                schema_id="market-history",
            )
        except UserActionError:
            if not task_id or not _empty_data_reference(requested):
                raise
            history_ref = self._fetch_and_bind_history(
                history_request, identity, request_id=request_id, task_id=task_id,
            )
        else:
            if history_ref is None:
                raise ValidationError("正式计算缺少market-history行情资产")
            self._attach_data_ref(identity, task_id, history_ref)
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
                    if self._tasks is None or not callable(getattr(self._tasks, "append_data_asset_ref", None)) or not task_id:
                        raise ValidationError("自动交易日历必须绑定当前App任务")
                    self._tasks.append_data_asset_ref(identity, task_id, calendar_asset)
                    calendar_ref = self._data_assets.resolve_for_compute(
                        identity,
                        calendar_asset["data_asset_id"],
                        asset_ids=tuple(map(str, underlyings)),
                        schema_id="trading-calendar",
                    )
                if calendar_ref is not None:
                    refs.append(calendar_ref)
        return refs

    def _fetch_and_bind_history(
        self,
        request: dict[str, Any],
        identity: SessionIdentity,
        *,
        request_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        if self._datafetcher is None:
            raise UnavailableCapabilityError("market_data", "App DataFetcherAdapter is not configured")
        if self._data_assets is None:
            raise ValidationError("App DataStore未配置，无法登记自动获取的行情")
        result = self._datafetcher.dispatch(
            request,
            identity,
            request_id=f"{request_id}-market" if request_id else f"market-{uuid4().hex}",
        )
        asset = result.get("data_asset_ref")
        if result.get("ok") is not True or not isinstance(asset, dict):
            raise UserActionError(
                "market_data_unavailable",
                "未能自动取得所需行情，请检查数据接口配置或缩短数据区间后重试。",
            )
        self._data_assets.register(identity, asset)
        self._attach_data_ref(identity, task_id, asset)
        resolved = self._data_assets.resolve_for_compute(
            identity,
            asset.get("data_asset_id"),
            asset_ids=tuple(request["asset_ids"]),
            schema_id="market-history",
        )
        if resolved is None:
            raise ValidationError("自动获取的行情未能登记为正式DataAssetRef")
        return resolved

    def _attach_data_ref(self, identity: SessionIdentity, task_id: str, reference: Mapping[str, Any]) -> None:
        if not task_id:
            return
        get_task = getattr(self._tasks, "get", None)
        append = getattr(self._tasks, "append_data_asset_ref", None)
        if not callable(get_task) or not callable(append):
            return
        task = get_task(identity, task_id)
        refs = task.get("data_asset_refs") if isinstance(task, Mapping) else None
        signature = (str(reference.get("data_asset_id")), str(reference.get("content_hash")))
        if isinstance(refs, list) and any(
            isinstance(item, Mapping)
            and (str(item.get("data_asset_id")), str(item.get("content_hash"))) == signature
            for item in refs
        ):
            return
        if not isinstance(refs, list):
            raise ValidationError("自动行情必须绑定当前App任务")
        append(identity, task_id, dict(reference))

    def _bind_contract_context(self, identity: SessionIdentity, context: Any, binding: Mapping[str, Any]) -> Any:
        fingerprint = binding.get("contract_fingerprint")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise ValidationError("ResolvedContract缺少有效contract_fingerprint")
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

    def _controlled_reporter_request(
        self,
        payload: Mapping[str, Any],
        identity: SessionIdentity,
        context: Any,
    ) -> dict[str, Any]:
        """Accept only an explicit current-task selection with complete RunRefs.

        The Agent and browser may choose an opaque source and candidate, but
        never fabricate, shorten or substitute a ModuleRunRef.  ResultStore is
        queried again immediately before Reporter so a stale, cross-task or
        hash-drifted run cannot survive a conversation turn.
        """

        if self._results is None:
            raise ValidationError("Reporter运行缺少App ResultStore")
        task_id = getattr(context, "task_id", None)
        if not isinstance(task_id, str) or not task_id:
            raise ValidationError("Reporter运行必须绑定当前任务")
        selection = payload.get("selection")
        if not isinstance(selection, Mapping):
            raise ValidationError("Reporter运行必须显式提交受控selection")
        source_id = selection.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            raise ValidationError("Reporter运行必须显式选择当前任务的source_id")
        try:
            source = self._results.select_report_source(identity, task_id, source_id)
        except (KeyError, AuthorizationError) as error:
            raise ValidationError("Reporter source不属于当前任务或已不再可用") from error
        if _scope_text(context, "analysis_case_id") and source.get("analysis_case_id") != context.analysis_case_id:
            raise ValidationError("Reporter source与Module Host analysis_case_id不一致")
        if _scope_text(context, "catalog_version") and source.get("catalog_version") != context.catalog_version:
            raise ValidationError("Reporter source与Module Host catalog_version不一致")
        candidate_ids = selection.get("candidate_ids")
        if not isinstance(candidate_ids, list) or len(candidate_ids) != 1 or not isinstance(candidate_ids[0], str):
            raise ValidationError("Reporter运行必须显式选择唯一candidate_id")
        candidate_id = candidate_ids[0]
        candidate = next(
            (item for item in source.get("candidates", ()) if isinstance(item, Mapping) and item.get("candidate_id") == candidate_id),
            None,
        )
        if candidate is None:
            raise ValidationError("Reporter candidate不属于当前任务source")
        if _scope_text(context, "candidate_id") and candidate_id != context.candidate_id:
            raise ValidationError("Reporter candidate与Module Host candidate_id不一致")
        if _scope_text(context, "contract_fingerprint") and candidate.get("contract_fingerprint") != context.contract_fingerprint:
            raise ValidationError("Reporter candidate与Module Host contract_fingerprint不一致")
        selected_modules = selection.get("selected_modules")
        if (
            not isinstance(selected_modules, list)
            or not selected_modules
            or len(set(selected_modules)) != len(selected_modules)
            or any(module not in {"payoff", "pricing", "backtest"} for module in selected_modules)
        ):
            raise ValidationError("Reporter必须显式选择不重复的计算模块")
        raw_all_refs = selection.get("module_run_refs")
        if not isinstance(raw_all_refs, Mapping) or set(raw_all_refs) != {candidate_id}:
            raise ValidationError("Reporter必须为唯一candidate显式提供ModuleRunRef映射")
        raw_refs = raw_all_refs.get(candidate_id)
        if not isinstance(raw_refs, Mapping) or set(raw_refs) != set(selected_modules):
            raise ValidationError("Reporter每个已选模块必须显式提供完整ModuleRunRef或null")
        source_options = candidate.get("module_run_options")
        if not isinstance(source_options, Mapping):
            raise ValidationError("Reporter source缺少已验证ModuleRunRef选项")
        controlled_refs: dict[str, dict[str, str] | None] = {}
        for display_module in selected_modules:
            raw_ref = raw_refs[display_module]
            if raw_ref is None:
                controlled_refs[display_module] = None
                continue
            if not isinstance(raw_ref, Mapping):
                raise ValidationError("Reporter ModuleRunRef必须为对象或null")
            try:
                reference = ModuleRunRef(**dict(raw_ref))
            except (TypeError, ValueError) as error:
                raise ValidationError("Reporter ModuleRunRef必须包含完整外部哈希锚") from error
            expected_module = {"payoff": "payoffer", "pricing": "pricer", "backtest": "backtester"}[display_module]
            if (
                reference.module != expected_module
                or reference.tenant_id != identity.tenant_id
                or reference.task_id != task_id
            ):
                raise ValidationError("Reporter ModuleRunRef超出当前租户、任务或模块范围")
            allowed = source_options.get(display_module)
            if not isinstance(allowed, list) or not any(
                _same_module_run_ref(reference, option) for option in allowed if isinstance(option, Mapping)
            ):
                raise ValidationError("Reporter ModuleRunRef不是当前ResultStore已验证选项")
            try:
                self._results.resolve_owned_module_run(identity, _module_run_ref_payload(reference))
            except (KeyError, ValidationError, AuthorizationError, OSError) as error:
                raise ValidationError("Reporter ModuleRunRef已失效或不再属于当前主体") from error
            controlled_refs[display_module] = _module_run_ref_payload(reference)
        return {
            **dict(payload),
            "task_id": task_id,
            "selection": {
                **dict(selection),
                "source_id": source_id,
                "candidate_ids": [candidate_id],
                "selected_modules": list(selected_modules),
                "module_run_refs": {candidate_id: controlled_refs},
            },
        }

    def _load_tool_entry(self, scripts_root: str, content_hashes: Mapping[str, str] | None = None) -> Any:
        """加载经清单校验的Capability入口，兼容受控内部调用。"""
        if content_hashes is None:
            content_hashes = self._registry.manifest.get("content_hashes")
        if not isinstance(content_hashes, Mapping):
            raise UnavailableCapabilityError("ToolGateway", "Capability content hashes are unavailable")
        return load_verified_source_module(
            "option_helper_embedded_tool_entry",
            scripts_root,
            "tool_entry.py",
            content_hashes,
        )


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


def _reject_untrusted_host_fields(value: object, *, allow_reporter_module_run_refs: bool = False) -> None:
    """Reject browser identity fields before dispatch.

    Reporter selections are the exception: a ModuleRunRef carries tenant and
    task anchors by protocol.  Those anchors are accepted only beneath its
    explicit ``module_run_refs`` field and are immediately re-authorized by
    ``_controlled_reporter_request`` before Reporter receives the payload.
    """

    if isinstance(value, dict):
        for key, item in value.items():
            child_is_reporter_ref = allow_reporter_module_run_refs or key == "module_run_refs"
            if _normalized_payload_key(key) in _HOST_PRIVATE_FIELDS and not allow_reporter_module_run_refs:
                raise ValidationError(f"{key}由App Host管理，页面不得提交")
            _reject_untrusted_host_fields(
                item,
                allow_reporter_module_run_refs=child_is_reporter_ref,
            )
    elif isinstance(value, list):
        for item in value:
            _reject_untrusted_host_fields(
                item,
                allow_reporter_module_run_refs=allow_reporter_module_run_refs,
            )


def _is_read_only_action(payload: Mapping[str, Any]) -> bool:
    action = payload.get("action", "run")
    return isinstance(action, str) and action.strip().lower() in {
        "catalog",
        "status",
        "list_assets",
        "list_report_sources",
    }


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


def _scope_text(context: Any, field: str) -> str | None:
    value = getattr(context, field, None)
    return value if isinstance(value, str) and value else None


def _module_run_ref_payload(reference: ModuleRunRef) -> dict[str, str]:
    return {
        "module": reference.module,
        "tenant_id": reference.tenant_id,
        "task_id": reference.task_id,
        "run_id": reference.run_id,
        "expected_semantic_result_hash": reference.expected_semantic_result_hash,
        "expected_artifact_manifest_hash": reference.expected_artifact_manifest_hash,
    }


def _same_module_run_ref(reference: ModuleRunRef, value: Mapping[str, Any]) -> bool:
    try:
        candidate = ModuleRunRef(**{
            field: value.get(field)
            for field in _module_run_ref_payload(reference)
        })
        return _module_run_ref_payload(reference) == _module_run_ref_payload(candidate)
    except (TypeError, ValueError):
        return False


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


def _empty_data_reference(value: object) -> bool:
    return value is None or value == ""


def _history_fetch_request(
    module: str,
    payload: Mapping[str, Any],
    resolved_contract: object,
    underlyings: tuple[str, ...],
) -> dict[str, Any]:
    contract = resolved_contract if isinstance(resolved_contract, Mapping) else {}
    terms = contract.get("terms", {}) if isinstance(contract.get("terms", {}), Mapping) else {}
    if module == "pricer":
        config = payload.get("pricing_config")
        raw_end = config.get("valuation_date") if isinstance(config, Mapping) else None
        end = _iso_date_or_today(raw_end, "valuation_date")
        start = _years_before(end, 3)
    elif module == "backtester":
        config = payload.get("backtest_config")
        config = config if isinstance(config, Mapping) else {}
        entry_end = _iso_date_or_today(config.get("end_date"), "backtest_config.end_date")
        start = _iso_date_or_default(config.get("start_date"), _years_before(entry_end, 3), "backtest_config.start_date")
        try:
            tenor_days = max(0, round(float(terms.get("T", 1.0)) * 366.0))
        except (TypeError, ValueError) as error:
            raise ValidationError("合同期限T必须为有效数字") from error
        end = entry_end + timedelta(days=tenor_days)
    else:
        raise ValidationError("只有定价与回测可以自动准备行情")
    if start > end:
        raise ValidationError("自动行情开始日期不得晚于结束日期")
    return {
        "action": "fetch",
        "asset_ids": list(underlyings),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "fields": ["close", "adj_close"],
        "provider": "ifind_http",
        "frequency": "1d",
        "adjustment": "auto",
        "cache_policy": "force_refresh",
        "offline": False,
    }


def _iso_date_or_today(value: object, label: str) -> date:
    return _iso_date_or_default(value, date.today(), label)


def _iso_date_or_default(value: object, default: date, label: str) -> date:
    if value in {None, ""}:
        return default
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise ValidationError(f"{label}必须为YYYY-MM-DD") from error


def _years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


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
        "source_id", "candidate_ids", "selected_modules", "module_run_refs", "delivery_mode", "output_type", "format", "audience", "report_run_id", "metadata",
    })


def _require_scope_fields(value: Mapping[str, Any], allowed: set[str]) -> None:
    if set(value).difference(allowed):
        raise AuthorizationError("conversation.tool.run", "request contains fields outside the conversation scope")
