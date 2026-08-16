"""Single authorized App-to-Capability Tool boundary.

The gateway imports the embedded, verified Capability at call time.  It does
not duplicate or reinterpret module business logic and it returns genuine
Capability failures rather than synthetic financial output.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import importlib
from pathlib import Path
import re
from typing import Any, Callable, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

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
from runtime.contracts.input_adapter import (
    ContractResolutionError,
    explicit_demo_pricing_error,
    is_explicit_demo_pricing_config,
)
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
                    # Payoffer preview is a non-persistent display request.  The
                    # browser bridge attaches Host scope so the App can verify
                    # the request, but those fields are not part of the
                    # Capability's preview schema.  Passing them through only
                    # fails after a task has an active contract, where the
                    # bridge includes catalog and contract identifiers.
                    effective_payload = (
                        _business_payload(payload)
                        if tool_name == "payoffer" and str(payload.get("action", "")).strip().lower() == "preview"
                        else payload
                    )
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
                        new_contract_variant = _new_contract_variant_requested(friendly_payload)
                        existing = self._current_task_contract(caller_context, str(verified_context.task_id))
                        if new_contract_variant and existing is None:
                            raise ValidationError("当前任务尚未建立合同，不能切换方案版本")
                        active_contract = None if new_contract_variant else existing
                        friendly_payload = _reuse_task_contract_identity(friendly_payload, active_contract)
                        data_refs = self._resolve_compute_data_refs(
                            tool_name,
                            friendly_payload,
                            caller_context,
                            active_contract,
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
                        friendly_payload = _bind_new_contract_to_history(
                            tool_name,
                            friendly_payload,
                            data_refs=tuple(data_refs),
                            existing_contract=active_contract,
                        )
                        legacy_calendar_upgrade = _needs_legacy_backtest_calendar_upgrade(
                            tool_name,
                            active_contract,
                            data_refs=tuple(data_refs),
                        )
                        if legacy_calendar_upgrade:
                            friendly_payload = _legacy_calendar_upgrade_payload(
                                friendly_payload,
                                existing,
                                data_refs=tuple(data_refs),
                            )
                        if tool_name in {"pricer", "backtester"} and active_contract is None:
                            friendly_payload = _compile_new_contract_identity_from_history(
                                tool_name,
                                friendly_payload,
                                data_refs=tuple(data_refs),
                                data_store=bound_data_store,
                            )
                        prepared = tool_entry.prepare_compute_request(
                            tool_name,
                            friendly_payload,
                            data_refs=tuple(data_refs),
                            resolved_contract=None if legacy_calendar_upgrade else active_contract.get("resolved_contract") if active_contract else None,
                            data_store=bound_data_store,
                        )
                        if not isinstance(prepared, Mapping) or not isinstance(prepared.get("request"), Mapping):
                            raise ValidationError("Capability返回的正式计算输入无效")
                        effective_payload = dict(prepared["request"])
                        if new_contract_variant:
                            binding = self._contracts.activate_new_variant(
                                caller_context,
                                str(verified_context.task_id),
                                prepared,
                                catalog_version=str(self._registry.manifest["catalog_version"]),
                                expected_contract_fingerprint=str(existing["contract_fingerprint"]),
                            )
                        elif legacy_calendar_upgrade:
                            binding = self._contracts.upgrade_legacy_calendar_evidence(
                                caller_context,
                                str(verified_context.task_id),
                                prepared,
                                catalog_version=str(self._registry.manifest["catalog_version"]),
                                expected_contract_fingerprint=str(existing["contract_fingerprint"]),
                            )
                        else:
                            binding = self._contracts.bind(
                                caller_context,
                                str(verified_context.task_id),
                                prepared,
                                catalog_version=str(self._registry.manifest["catalog_version"]),
                            )
                        effective_context = self._bind_contract_context(
                            caller_context,
                            verified_context,
                            binding,
                            new_candidate=new_contract_variant,
                        )
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
                    if (
                        read_only_action
                        and tool_name in {"pricer", "backtester"}
                        and str(payload.get("action", "")).strip().lower() == "catalog"
                        and verified_context is not None
                        and verified_context.task_id
                        and self._contracts is not None
                    ):
                        binding = self._current_task_contract(caller_context, str(verified_context.task_id))
                        task_contract = _catalog_task_contract(binding)
                        if task_contract is not None:
                            result = {**dict(result), "task_contract": task_contract}
                    if compute_run:
                        if not isinstance(result, Mapping):
                            raise ValidationError("Capability计算结果必须为对象")
                        result = _bind_compute_result(result, effective_context)
            except (UserActionError, UnavailableCapabilityError):
                # This class is only created by an App-owned preflight.  Its
                # message is a fixed, reviewed next step and contains no
                # capability path, exception trace or credential detail.
                raise
            except ContractResolutionError as error:
                # The shared input compiler only emits reviewed, user-facing
                # field and contract messages.  Keeping these as a validation
                # response lets Desk point to the actual missing input rather
                # than misreporting a calculator outage.
                raise _public_contract_resolution_error(error) from error
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
            history_ref = None

        if module == "backtester" and (
            history_ref is None or not _has_verified_backtest_calendar(history_ref)
        ):
            if history_ref is not None and not _empty_data_reference(requested):
                raise UserActionError(
                    "backtest_calendar_evidence_required",
                    "所选历史行情未附已验证交易日历。请在数据获取中重新获取覆盖本次回测区间的行情后重试。",
                )
            if not task_id:
                raise ValidationError("自动历史回测必须绑定当前App任务")
            calendar_ref = self._fetch_and_bind_backtest_calendar(
                history_request,
                identity,
                request_id=request_id,
                task_id=task_id,
            )
            history_ref = self._fetch_and_bind_history(
                history_request,
                identity,
                request_id=request_id,
                task_id=task_id,
                trading_calendar_ref=calendar_ref,
            )
        else:
            if history_ref is None:
                if not task_id:
                    raise ValidationError("自动获取行情必须绑定当前App任务")
                history_ref = self._fetch_and_bind_history(
                    history_request,
                    identity,
                    request_id=request_id,
                    task_id=task_id,
                )
            else:
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
        trading_calendar_ref: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._datafetcher is None:
            raise UnavailableCapabilityError("market_data", "App DataFetcherAdapter is not configured")
        if self._data_assets is None:
            raise ValidationError("App DataStore未配置，无法登记自动获取的行情")
        dispatch_kwargs: dict[str, Any] = {
            "request_id": f"{request_id}-market" if request_id else f"market-{uuid4().hex}",
        }
        if trading_calendar_ref is not None:
            dispatch_kwargs["trading_calendar_ref"] = trading_calendar_ref
        result = self._datafetcher.dispatch(request, identity, **dispatch_kwargs)
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

    def _fetch_and_bind_backtest_calendar(
        self,
        history_request: Mapping[str, Any],
        identity: SessionIdentity,
        *,
        request_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        """Fetch verified sessions before creating automatic backtest history."""

        if self._datafetcher is None:
            raise UnavailableCapabilityError("backtester.trading_calendar", "App DataFetcherAdapter is not configured")
        if self._data_assets is None:
            raise ValidationError("App DataStore未配置，无法登记自动交易日历")
        calendar_result = self._datafetcher.dispatch(
            _backtest_calendar_fetch_request(history_request),
            identity,
            request_id=f"{request_id}-calendar" if request_id else f"calendar-{uuid4().hex}",
        )
        calendar_asset = calendar_result.get("data_asset_ref")
        if calendar_result.get("ok") is not True or not isinstance(calendar_asset, dict):
            raise UserActionError(
                "trading_calendar_unavailable",
                "交易日历暂不可用，无法为完整期限历史回测准备已验证行情。请检查数据接口后重试。",
            )
        self._data_assets.register(identity, calendar_asset)
        self._attach_data_ref(identity, task_id, calendar_asset)
        calendar_ref = self._data_assets.resolve_for_compute(
            identity,
            calendar_asset.get("data_asset_id"),
            asset_ids=tuple(str(item) for item in history_request["asset_ids"]),
            schema_id="trading-calendar",
        )
        if calendar_ref is None:
            raise ValidationError("自动获取的交易日历未能登记为正式DataAssetRef")
        return calendar_ref

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

    def _current_task_contract(self, identity: SessionIdentity, task_id: str) -> dict[str, Any] | None:
        """Return only a binding made under this Capability catalog.

        ModuleHost context issuance and execution must make the same decision:
        a binding from an earlier catalog cannot be ignored for context creation
        and then rediscovered by ``ContractStore.bind`` during execution.
        """

        if self._contracts is None:
            return None
        catalog_version = str(self._registry.manifest["catalog_version"])
        current = self._contracts.get_current(identity, task_id, catalog_version=catalog_version)
        if current is not None:
            return current
        stale = self._contracts.get(identity, task_id)
        if stale is not None:
            raise UserActionError(
                "stale_task_contract",
                "当前任务的合同来自旧产品目录。请新建研究任务后继续估值或历史回测。",
            )
        return None

    def _bind_contract_context(
        self,
        identity: SessionIdentity,
        context: Any,
        binding: Mapping[str, Any],
        *,
        new_candidate: bool = False,
    ) -> Any:
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
            candidate_id=f"candidate-{fingerprint[:24]}" if new_candidate else context.candidate_id or f"candidate-{fingerprint[:24]}",
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


def _new_contract_variant_requested(payload: dict[str, Any]) -> bool:
    """Consume the explicit browser intent before Core input compilation.

    This flag does not relax the normal compiler or Host-context checks.  It
    merely tells the App ContractStore to retain the active binding as a prior
    scheme and atomically activate the freshly compiled contract instead.
    """

    requested = payload.pop("new_contract_variant", False)
    if not isinstance(requested, bool):
        raise ValidationError("new_contract_variant必须为布尔值")
    return requested


def _reuse_task_contract_identity(
    payload: Mapping[str, Any], existing_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Keep page convenience defaults from redefining a task-frozen contract.

    Product and underlyings remain explicit and are still checked by the shared
    contract compiler.  Contract dates, reference closes and price convention
    are Host-derived on a first run and must not be reintroduced by Pricer or
    Backtester page defaults on subsequent runs.
    """

    if not isinstance(existing_contract, Mapping):
        return dict(payload)
    identity = payload.get("identity")
    if not isinstance(identity, Mapping):
        return dict(payload)
    value = dict(payload)
    reused = dict(identity)
    for field in ("contract_start_date", "contract_end_date", "reference_prices", "price_convention"):
        reused.pop(field, None)
    value["identity"] = reused
    return value


_RESOLVABLE_CONTRACT_IDENTITY_FIELDS = frozenset({
    "contract_id", "underlyings", "currency", "contract_start_date", "contract_end_date",
    "reference_prices", "price_convention", "calendar_id", "calendar_revision",
})
_UNVERIFIED_CALENDAR_REVISIONS = frozenset({"unverified", "unknown", "derived", "frame-sessions"})


def _needs_legacy_backtest_calendar_upgrade(
    module: str,
    existing_contract: Mapping[str, Any] | None,
    *,
    data_refs: tuple[Mapping[str, Any], ...],
) -> bool:
    """Allow one audited calendar-evidence repair for legacy vanilla tasks."""

    if module != "backtester" or not isinstance(existing_contract, Mapping):
        return False
    contract = existing_contract.get("resolved_contract")
    identity = contract.get("identity") if isinstance(contract, Mapping) else None
    if not isinstance(identity, Mapping) or not _has_unverified_contract_calendar(identity):
        return False
    history = next((ref for ref in data_refs if ref.get("schema_id") == "market-history"), None)
    if history is None or not _has_verified_backtest_calendar(history):
        return False
    schedules = contract.get("resolved_schedules") if isinstance(contract, Mapping) else None
    if not isinstance(schedules, Mapping):
        raise UserActionError(
            "legacy_calendar_contract",
            "当前任务的早期交易日历记录不完整，无法安全恢复；请新建研究任务后重新运行回测。",
        )
    if schedules:
        raise UserActionError(
            "legacy_calendar_contract",
            "当前任务包含已冻结观察日，不能替换其早期交易日历；请新建研究任务后重新运行回测。",
        )
    return True


def _legacy_calendar_upgrade_payload(
    payload: Mapping[str, Any],
    existing_contract: Mapping[str, Any] | None,
    *,
    data_refs: tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    """Recompile a legacy vanilla contract with only verified calendar facts changed."""

    binding = existing_contract if isinstance(existing_contract, Mapping) else {}
    contract = binding.get("resolved_contract")
    source_identity = contract.get("identity") if isinstance(contract, Mapping) else None
    terms = contract.get("terms") if isinstance(contract, Mapping) else None
    term_sources = contract.get("term_sources") if isinstance(contract, Mapping) else None
    if not isinstance(source_identity, Mapping) or not isinstance(terms, Mapping) or not isinstance(term_sources, Mapping):
        raise ContractResolutionError("旧任务ResolvedContract不完整，无法升级交易日历证据")
    source_product_id = source_identity.get("product_id")
    if not isinstance(source_product_id, str) or not source_product_id:
        raise ContractResolutionError("旧任务ResolvedContract缺少产品标识")
    supplied_product_id = payload.get("product_id")
    if supplied_product_id is not None and supplied_product_id != "" and supplied_product_id != source_product_id:
        raise ContractResolutionError("当前任务已绑定另一产品的ResolvedContract")
    supplied_identity = payload.get("identity", {})
    if not isinstance(supplied_identity, Mapping):
        raise ContractResolutionError("identity必须为对象")
    for field in _RESOLVABLE_CONTRACT_IDENTITY_FIELDS - {"calendar_id", "calendar_revision"}:
        if field in supplied_identity and supplied_identity[field] != source_identity.get(field):
            raise ContractResolutionError(f"identity.{field}与任务已冻结ResolvedContract冲突")
    supplied_overrides = payload.get("term_overrides", {})
    if not isinstance(supplied_overrides, Mapping):
        raise ContractResolutionError("term_overrides必须为对象")
    for field, value in supplied_overrides.items():
        if terms.get(field) != value:
            raise ContractResolutionError(f"本次条款{field}与任务已冻结ResolvedContract冲突")
    unsupported_sources = {
        str(source) for source in term_sources.values()
        if str(source) not in {"default", "override"}
    }
    if unsupported_sources:
        raise UserActionError(
            "legacy_calendar_contract",
            "当前任务包含无法安全重建的历史条款来源；请新建研究任务后重新运行回测。",
        )
    history = next((ref for ref in data_refs if ref.get("schema_id") == "market-history"), None)
    coverage = history.get("coverage") if isinstance(history, Mapping) else None
    if not isinstance(coverage, Mapping) or not _has_verified_backtest_calendar(history):
        raise ContractResolutionError("正式历史行情未附可验证交易日历")
    identity = {
        field: source_identity.get(field)
        for field in _RESOLVABLE_CONTRACT_IDENTITY_FIELDS
        if field in source_identity
    }
    identity["calendar_id"] = coverage["calendar_id"]
    identity["calendar_revision"] = coverage["calendar_revision"]
    overrides = {
        field: terms[field]
        for field, source in term_sources.items()
        if source == "override" and field in terms
    }
    value = dict(payload)
    value.update({
        "product_id": source_product_id,
        "identity": identity,
        "term_overrides": overrides,
    })
    return value


def _has_unverified_contract_calendar(identity: Mapping[str, Any]) -> bool:
    calendar_id = identity.get("calendar_id")
    revision = identity.get("calendar_revision")
    return (
        not isinstance(calendar_id, str)
        or not calendar_id.startswith("CN-")
        or not isinstance(revision, str)
        or not revision.strip()
        or revision.casefold() in _UNVERIFIED_CALENDAR_REVISIONS
    )


def _catalog_task_contract(binding: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the small, page-visible portion of an already frozen contract.

    Product and contract terms are shown by Desk after a task has been bound,
    so its controls cannot invite the user to submit a different contract and
    receive a generic rejection.  This is derived only from ContractStore,
    never from a browser value or a private Store reference.
    """

    if not isinstance(binding, Mapping):
        return None
    contract = binding.get("resolved_contract")
    if not isinstance(contract, Mapping):
        return None
    identity = contract.get("identity")
    terms = contract.get("terms")
    product_id = identity.get("product_id") if isinstance(identity, Mapping) else None
    if not isinstance(product_id, str) or not product_id or not isinstance(identity, Mapping) or not isinstance(terms, Mapping):
        return None
    return {
        "product_id": product_id,
        "identity": dict(identity),
        "terms": dict(terms),
        "contract_fingerprint": str(binding.get("contract_fingerprint") or ""),
    }


def _public_contract_resolution_error(error: ContractResolutionError) -> UserActionError | ValidationError:
    """Translate task-binding conflicts without exposing compiler internals."""

    message = str(error)
    if "已冻结ResolvedContract" in message or "当前任务已绑定另一产品" in message:
        return UserActionError(
            "task_contract_conflict",
            "当前任务已绑定产品与条款。若需更换产品、标的或条款，请新建研究任务；定价和回测会沿用当前任务已保存的条款。",
        )
    return ValidationError(message)


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


def _bind_new_contract_to_history(
    module: str,
    payload: Mapping[str, Any],
    *,
    data_refs: tuple[Mapping[str, Any], ...],
    existing_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Bind a first-run compute contract to Host-verified market provenance.

    A Desk page may run Pricer or Backtester before Payoffer.  In that case the
    browser only names the product and underlying; the App Host already owns
    the authenticated historical ``DataAssetRef`` it just fetched.  Calendar
    facts therefore come from that reference, never from browser input.
    """
    value = dict(payload)
    if module not in {"pricer", "backtester"} or existing_contract is not None:
        return value
    history = next(
        (dict(reference) for reference in data_refs if reference.get("schema_id") == "market-history"),
        None,
    )
    if history is None:
        return value
    coverage = history.get("coverage")
    if not isinstance(coverage, Mapping):
        raise UserActionError(
            "market_history_calendar_missing",
            "自动获取的行情缺少交易日历信息，无法建立本次研究合同。请重新获取数据后重试。",
        )
    calendar_id = coverage.get("calendar_id")
    calendar_revision = coverage.get("calendar_revision")
    if not isinstance(calendar_id, str) or not calendar_id or not isinstance(calendar_revision, str) or not calendar_revision:
        raise UserActionError(
            "market_history_calendar_missing",
            "自动获取的行情缺少交易日历信息，无法建立本次研究合同。请重新获取数据后重试。",
        )
    raw_identity = value.get("identity", {})
    if not isinstance(raw_identity, Mapping):
        raise ValidationError("identity必须为对象")
    identity = dict(raw_identity)
    identity.setdefault("calendar_id", calendar_id)
    identity.setdefault("calendar_revision", calendar_revision)
    if module == "backtester" and not identity.get("contract_start_date"):
        identity["contract_start_date"] = _backtest_contract_start_date(value, coverage)
    value["identity"] = identity
    return value


def _backtest_contract_start_date(payload: Mapping[str, Any], coverage: Mapping[str, Any]) -> str:
    """Choose the first requested entry date for a new backtest contract.

    Backtester later freezes an independent historical contract for every
    entry.  This initial date only establishes a complete task-level contract
    identity, so it must come from the user's entry window or verified data
    coverage rather than a fabricated quote date.
    """
    config = payload.get("backtest_config")
    config = config if isinstance(config, Mapping) else {}
    entries = config.get("entry_dates")
    candidates: list[object] = []
    if isinstance(entries, list):
        candidates.extend(entries)
    candidates.extend((config.get("start_date"), coverage.get("start")))
    for candidate in candidates:
        if candidate in {None, ""}:
            continue
        try:
            return date.fromisoformat(str(candidate)).isoformat()
        except ValueError:
            continue
    raise UserActionError(
        "backtest_start_date_missing",
        "回测需要有效的入场起始日。请填写入场日期后重试。",
    )


def _compile_new_contract_identity_from_history(
    module: str,
    payload: Mapping[str, Any],
    *,
    data_refs: tuple[Mapping[str, Any], ...],
    data_store: object | None,
) -> dict[str, Any]:
    """Freeze a first-run calculator contract from verified market history.

    The Host has already authenticated the sole history reference and bound it
    to the current task. Reuse the frozen Pricer Capability's official market
    compiler so contract start date and reference closes cannot come from the
    page, an environment default, or a second App-side pricing rule. Backtest
    uses the same contract identity rule before it becomes task-frozen.
    """
    try:
        defaults = importlib.import_module("modules.pricer.input_defaults")
        compile_defaults = getattr(defaults, "compile_pricer_input_defaults")
        input_error = getattr(defaults, "PricerInputDefaultError")
    except (ImportError, AttributeError) as error:
        raise UnavailableCapabilityError(
            "pricer.input_defaults",
            "the verified Pricer Capability does not provide its formal input compiler",
        ) from error
    try:
        source = dict(payload)
        if module == "backtester":
            identity = source.get("identity")
            if not isinstance(identity, Mapping) or not identity.get("contract_start_date"):
                raise ValidationError("回测合同缺少由历史数据确定的起始日")
            # The compiler only needs a valuation date to derive the legal
            # historical reference close. Do not carry this temporary pricing
            # configuration into the BacktestInput.
            source = {
                "product_id": source.get("product_id"),
                "identity": dict(identity),
                "pricing_config": {"valuation_date": identity["contract_start_date"]},
            }
        compiled = compile_defaults(
            source,
            data_refs=tuple(data_refs),
            data_store=data_store,
        )
    except input_error as error:
        raise UserActionError("pricer_market_input_invalid", str(error)) from error
    if not isinstance(compiled, Mapping):
        raise ValidationError("Pricer正式输入编译器返回无效结果")
    if module == "pricer":
        return dict(compiled)
    result = dict(payload)
    identity = compiled.get("identity")
    if not isinstance(identity, Mapping):
        raise ValidationError("正式合同输入缺少已验证identity")
    result["identity"] = dict(identity)
    return result


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
        end = _iso_date_or_latest_market_day(raw_end, "valuation_date")
        start = _years_before(end, 3)
    elif module == "backtester":
        config = payload.get("backtest_config")
        config = config if isinstance(config, Mapping) else {}
        try:
            tenor_days = max(0, round(float(terms.get("T", 1.0)) * 366.0))
        except (TypeError, ValueError) as error:
            raise ValidationError("合同期限T必须为有效数字") from error
        latest = _latest_market_day()
        # 未填写窗口时，默认给完整期限回测留出合同到期所需的历史区间。
        # 因此回测无需先手工运行数据获取，也不会请求未来行情。
        requested_entry_end = _iso_date_or_default(
            config.get("end_date"), latest - timedelta(days=tenor_days), "backtest_config.end_date",
        )
        # A complete-tenor backtest needs observations through every selected
        # contract's maturity.  Never turn a manually entered end date into a
        # future market-data request: cap the last entry date to the latest
        # available market day minus the contract tenor.
        entry_end = min(requested_entry_end, latest - timedelta(days=tenor_days))
        start = _iso_date_or_default(config.get("start_date"), _years_before(entry_end, 3), "backtest_config.start_date")
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


def _backtest_calendar_fetch_request(history_request: Mapping[str, Any]) -> dict[str, Any]:
    """Derive a calendar request from the Host-owned historical data scope."""

    asset_ids = history_request.get("asset_ids")
    start_date = history_request.get("start_date")
    end_date = history_request.get("end_date")
    if (
        not isinstance(asset_ids, list)
        or not asset_ids
        or any(not isinstance(item, str) or not item for item in asset_ids)
        or not isinstance(start_date, str)
        or not isinstance(end_date, str)
    ):
        raise ValidationError("自动回测交易日历缺少受控行情区间")
    return {
        "action": "fetch_calendar",
        "asset_ids": list(asset_ids),
        "start_date": start_date,
        "end_date": end_date,
    }


def _has_verified_backtest_calendar(reference: Mapping[str, Any]) -> bool:
    """Whether a market-history reference can support a formal full-tenor run."""

    coverage = reference.get("coverage")
    if not isinstance(coverage, Mapping):
        return False
    calendar_id = coverage.get("calendar_id")
    calendar_revision = coverage.get("calendar_revision")
    sessions = coverage.get("sessions")
    calendar_coverage_end = coverage.get("calendar_coverage_end")
    if (
        not isinstance(calendar_id, str)
        or not calendar_id.startswith("CN-")
        or not isinstance(calendar_revision, str)
        or not calendar_revision.strip()
        or calendar_revision.casefold() in {"unverified", "unknown", "derived", "frame-sessions"}
        or not isinstance(sessions, list)
        or not sessions
        or not isinstance(calendar_coverage_end, str)
    ):
        return False
    try:
        declared_end = date.fromisoformat(calendar_coverage_end)
        last_session = date.fromisoformat(str(sessions[-1]))
    except ValueError:
        return False
    return declared_end <= last_session


def _shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _latest_market_day() -> date:
    """Return the latest completed weekday suitable for daily OHLC requests.

    A calendar can legitimately contain the current trading day before its
    close, but a daily OHLC source cannot.  Reserve a post-close buffer and
    use the preceding weekday until then.  Exchange holidays remain a
    provider/calendar concern.
    """

    now = _shanghai_now()
    value = now.date()
    if now.time() < time(18):
        value -= timedelta(days=1)
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return value


def _iso_date_or_latest_market_day(value: object, label: str) -> date:
    latest = _latest_market_day()
    # Inputs commonly come from a calendar control.  If it lands on a weekend
    # or a future date, use the latest observable daily-market date instead of
    # sending an impossible request to the provider.
    return min(_iso_date_or_default(value, latest, label), latest)


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
