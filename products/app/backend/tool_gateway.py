"""Single authorized App-to-Capability Tool boundary.

The gateway imports the embedded, verified Capability at call time.  It does
not duplicate or reinterpret module business logic and it returns genuine
Capability failures rather than synthetic financial output.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import importlib
from io import BytesIO
from pathlib import Path
import re
from typing import Any, Callable, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from .authorization.policy import AuthorizationPolicy
from .capability_service import capability_import_scope, load_verified_capability_service
from .datafetcher_adapter import DataFetcherAdapter, _datafetcher_failure_fields
from .errors import AuthorizationError, UnavailableCapabilityError, UserActionError, ValidationError
from .identity.session_identity import SessionIdentity
from .page_registry import PAGE_MODULES, PageRegistry
from .reporter_adapter import ReporterAdapter
from .stores.result_store import ResultStore
from .stores.data_store import DataStore, market_history_bounds
from .task_runtime.compute_process import (
    ComputeProcessSupervisor,
    PreparedComputeExecution,
    decode_draft_files,
    encode_snapshot,
)
from runtime.contracts.contract_api import ResolvedContract
from runtime.contracts.input_adapter import (
    ContractResolutionError,
    compile_compute_data_requirements,
    first_backtest_entry,
)
from runtime.capability_import import load_verified_source_module, verified_capability_modules
from runtime.protocol.module_host import ModuleHostContextError, require_host_bound_run_contract
from runtime.protocol.models import CallerContext, DataAssetRef, ModuleRunRef
from runtime.ports.tool_gateway import ToolGatewayPort


class ToolGateway:
    def __init__(
        self,
        registry: PageRegistry,
        policy: AuthorizationPolicy,
        reporter: ReporterAdapter | None = None,
        datafetcher: DataFetcherAdapter | None = None,
        results: ResultStore | None = None,
        data_assets: DataStore | None = None,
        tasks: object | None = None,
        capability_runtime_root: str | Path | None = None,
        compute_supervisor: ComputeProcessSupervisor | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._reporter = reporter
        self._datafetcher = datafetcher
        self._results = results
        self._data_assets = data_assets
        self._tasks = tasks
        self._compute_supervisor = compute_supervisor
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
        cancellation_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValidationError("Tool payload must be an object")
        if tool_name not in PAGE_MODULES and tool_name != "recommender":
            raise ValidationError(f"Unknown Capability tool: {tool_name}")
        read_only_action = _is_read_only_action(payload)
        if agent_proxy:
            self._policy.require(caller_context.role, "conversation.tool.run")
            _require_conversation_tool_scope(caller_context, tool_name, payload)
            if tool_name == "reporter" and not read_only_action:
                self._policy.require(caller_context.role, _report_request_capability(payload))
            granted_capability = "conversation.tool.run"
        elif tool_name == "reporter" and not read_only_action:
            granted_capability = _report_request_capability(payload)
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
                if str(payload.get("action", "")).strip().lower() == "list_report_sources":
                    task_id = payload.get("task_id")
                    if not isinstance(task_id, str) or not task_id:
                        raise ValidationError("Reporter来源列表必须绑定当前App任务")
                return self._reporter.dispatch(payload, caller_context)
            if verified_context is None:
                raise ValidationError("Reporter运行缺少已验证Module Host context")
            if str(payload.get("action", "")).strip().lower() == "rerender":
                return self._reporter.dispatch(
                    self._controlled_reporter_rerender_request(payload, caller_context, verified_context),
                    caller_context,
                )
            return self._reporter.dispatch(
                self._controlled_reporter_request(payload, caller_context, verified_context),
                caller_context,
            )
        if tool_name == "datafetcher":
            if self._datafetcher is None:
                raise UnavailableCapabilityError("datafetcher", "App DataFetcherAdapter is not configured")
            controlled_request = _controlled_datafetcher_request(payload, verified_context)
            calendar_ref = self._single_day_datafetch_calendar(
                controlled_request, caller_context, request_id=request_id,
            )
            return self._datafetcher.dispatch(
                controlled_request,
                caller_context,
                request_id=request_id,
                trading_calendar_ref=calendar_ref,
            )
        if tool_name == "backtester" and str(payload.get("action", "")).strip().lower() == "preview":
            return self._dispatch_backtest_preview(
                payload,
                caller_context,
                verified_context,
                request_id=request_id,
                agent_proxy=agent_proxy,
            )
        if compute_run and self._compute_supervisor is not None:
            return self._dispatch_isolated_compute(
                tool_name,
                payload,
                caller_context,
                verified_context,
                granted_capability=granted_capability,
                request_id=request_id,
                agent_proxy=agent_proxy,
                cancellation_check=cancellation_check,
            )
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
                    effective_payload = (
                        _business_payload(payload)
                        if tool_name == "payoffer" and str(payload.get("action", "")).strip().lower() == "default"
                        else payload
                    )
                    effective_context = verified_context
                    bound_data_store = None
                    compiler_data_store = None
                    backtest_window = None
                    if (
                        tool_name == "payoffer"
                        and str(payload.get("action", "")).strip().lower() == "preview"
                    ):
                        effective_payload, effective_context = self._prepare_hosted_payoffer_preview(
                            payload,
                            verified_context,
                        )
                    if compute_run:
                        if not verified_context.task_id:
                            raise ValidationError("计算模块必须绑定App任务")
                        if not callable(getattr(tool_entry, "prepare_compute_request", None)):
                            raise ValidationError("Capability缺少正式计算输入编译器")
                        friendly_payload = _business_payload(payload)
                        friendly_payload = _controlled_desk_compute_payload(
                            tool_name, friendly_payload, agent_proxy=agent_proxy,
                        )
                        friendly_payload, render_options = _extract_payoffer_render_options(
                            tool_name, friendly_payload, agent_proxy=agent_proxy,
                        )
                        _run_fair_parameter_preflight(
                            tool_entry,
                            tool_name,
                            friendly_payload,
                        )
                        data_refs = self._resolve_compute_data_refs(
                            tool_name,
                            friendly_payload,
                            caller_context,
                            request_id=request_id,
                        )
                        # The shared input compiler must verify every attached
                        # DataAssetRef before it freezes a contract. Payoffer
                        # never receives this port, but an observation product
                        # still needs the Host-owned calendar to resolve its
                        # immutable schedule before the formal PayoffInput is
                        # created.
                        if data_refs:
                            if self._datafetcher is None:
                                raise UnavailableCapabilityError(
                                    f"{tool_name}.data_store", "App DataFetcherAdapter is not configured",
                                )
                            compiler_data_store = self._datafetcher.bind_data_store(
                                caller_context,
                                data_refs=tuple(data_refs),
                            )
                            bound_data_store = compiler_data_store
                        friendly_payload = _bind_current_input_to_history(
                            tool_name,
                            friendly_payload,
                            data_refs=tuple(data_refs),
                        )
                        prepared, friendly_payload, backtest_window = _prepare_compute_with_backtest_window(
                            tool_entry,
                            tool_name,
                            friendly_payload,
                            data_refs=tuple(data_refs),
                            data_store=compiler_data_store,
                        )
                        if not isinstance(prepared, Mapping) or not isinstance(prepared.get("request"), Mapping):
                            raise ValidationError("Capability返回的正式计算输入无效")
                        effective_payload = dict(prepared["request"])
                        if render_options is not None:
                            effective_payload["render_options"] = render_options
                        snapshot = self._validated_current_snapshot(
                            caller_context,
                            str(verified_context.task_id),
                            prepared,
                        )
                        effective_context = self._bind_product_context(
                            caller_context,
                            verified_context,
                            snapshot,
                        )
                    _reject_path_payload(effective_payload)
                    bound_store = None
                    if compute_run:
                        if self._results is None or effective_context.task_id is None:
                            raise ValidationError("计算模块必须绑定App任务与ResultStore")
                        bound_store = self._results.bind_module_store(
                            caller_context,
                            effective_context.task_id,
                            tool_name,
                            resolved_contract_snapshot=snapshot,
                            defer_publication=True,
                        )
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
                    if tool_name == "payoffer" and str(payload.get("action", "")).strip().lower() == "preview":
                        _validate_hosted_payoffer_preview_result(result)
                    if compute_run:
                        if not isinstance(result, Mapping):
                            raise ValidationError("Capability计算结果必须为对象")
                        result = _bind_compute_result(
                            result,
                            effective_context,
                            resolved_contract_snapshot=snapshot,
                        )
                        if tool_name == "backtester" and backtest_window is not None:
                            result["backtest_window"] = backtest_window
                        if result.get("ok") is True and result.get("status") in {"succeeded", "partial"}:
                            self._results.publish_module_run(
                                caller_context, result.get("module_run_ref", {}),
                            )
            except (UserActionError, UnavailableCapabilityError):
                # This class is only created by an App-owned preflight.  Its
                # message is a fixed, reviewed next step and contains no
                # capability path, exception trace or credential detail.
                raise
            except (AuthorizationError, ValidationError):
                # These are already classified App/Capability boundary
                # failures.  Rewrapping them as an engine outage loses the
                # caller's actionable category and next step.
                raise
            except ContractResolutionError as error:
                # The shared input compiler only emits reviewed, user-facing
                # field and contract messages.  Keeping these as a validation
                # response lets Desk point to the actual missing input rather
                # than misreporting a calculator outage.
                raise _public_contract_resolution_error(error) from error
            except ValueError as error:
                # Capability services use ValueError for reviewed input and
                # rendering constraints. Preserve that business message so the
                # page can point to the actual field instead of reporting an
                # unrelated engine outage.
                raise ValidationError(str(error)) from error
            except Exception as error:
                raise UnavailableCapabilityError(
                    f"Capability tool {tool_name}",
                    "计算内核未能完成本次请求。请检查产品条款、行情数据和交易日历后重试。",
                    failure_code="capability_execution_failed",
                    stage="compute",
                ) from error
        if not isinstance(result, dict):
            raise ValidationError("Capability tool must return a JSON object")
        if compute_run and str(result.get("status", "")).lower() in {"succeeded", "partial"}:
            try:
                require_host_bound_run_contract(result, effective_context)
            except ModuleHostContextError as error:
                raise ValidationError(str(error)) from error
        return result

    def _dispatch_backtest_preview(
        self,
        payload: Mapping[str, Any],
        caller_context: SessionIdentity,
        verified_context: Any,
        *,
        request_id: str,
        agent_proxy: bool,
    ) -> dict[str, Any]:
        """只读预检：复用正式编译、历史数据、日历和完整期限窗口规划。"""
        if verified_context is None or not verified_context.task_id:
            raise ValidationError("Backtester日期预检必须绑定App任务")
        scripts_root = str(self._registry.capability_root / "scripts")
        with capability_import_scope(scripts_root, runtime_root=self._capability_runtime_root):
            self._registry.assert_execution_integrity()
            content_hashes = self._registry.manifest.get("content_hashes")
            if not isinstance(content_hashes, Mapping):
                raise UnavailableCapabilityError("ToolGateway", "Capability content hashes are unavailable")
            with verified_capability_modules(scripts_root, content_hashes):
                tool_entry = self._load_tool_entry(scripts_root, content_hashes)
                load_verified_capability_service("backtester", scripts_root)
                friendly_payload = _business_payload(payload)
                friendly_payload["action"] = "run"
                friendly_payload = _controlled_desk_compute_payload(
                    "backtester", friendly_payload, agent_proxy=agent_proxy,
                )
                data_refs = self._resolve_compute_data_refs(
                    "backtester",
                    friendly_payload,
                    caller_context,
                    request_id=request_id,
                )
                compiler_data_store = (
                    self._datafetcher.bind_data_store(
                        caller_context,
                        data_refs=tuple(data_refs),
                    )
                    if data_refs and self._datafetcher is not None
                    else None
                )
                friendly_payload = _bind_current_input_to_history(
                    "backtester",
                    friendly_payload,
                    data_refs=tuple(data_refs),
                )
                requested_config = friendly_payload.get("backtest_config")
                requested_config = dict(requested_config) if isinstance(requested_config, Mapping) else {}
                requested_window = {
                    "start_date": requested_config.get("start_date"),
                    "end_date": requested_config.get("end_date"),
                    "entry_dates": list(requested_config.get("entry_dates") or ()),
                }
                try:
                    prepared, _, window = _prepare_compute_with_backtest_window(
                        tool_entry,
                        "backtester",
                        friendly_payload,
                        data_refs=tuple(data_refs),
                        data_store=compiler_data_store,
                    )
                except UserActionError as error:
                    details = dict(error.details) if isinstance(error.details, Mapping) else {}
                    suggested = details.get("suggested_end_date") or details.get("latest_complete_entry_session")
                    return {
                        "ok": True,
                        "module": "backtester",
                        "action": "preview",
                        "status": "invalid",
                        "request_entry_window": requested_window,
                        "market_as_of_session": details.get("market_as_of_session"),
                        "latest_complete_entry_session": details.get("latest_complete_entry_session"),
                        "suggested_start_date": details.get("suggested_start_date"),
                        "suggested_end_date": suggested,
                        "reason": details.get("reason"),
                        "effective_entry_window": None,
                        "tenor_basis": _backtest_tenor_basis(friendly_payload, None),
                        "data_coverage_status": "insufficient_or_invalid_window",
                        "failure_code": error.code,
                        "message": str(error),
                        "next_step": error.next_step,
                    }
                contract = prepared.get("request", {}).get("contract") if isinstance(prepared, Mapping) else None
                return {
                    "ok": True,
                    "module": "backtester",
                    "action": "preview",
                    "status": window.get("status", "ready") if window else "ready",
                    "request_entry_window": requested_window,
                    "market_as_of_session": window.get("market_as_of_session") if window else None,
                    "latest_complete_entry_session": window.get("latest_complete_entry_session") if window else None,
                    "suggested_end_date": window.get("end_date") if window else None,
                    "effective_entry_window": window,
                    "tenor_basis": _backtest_tenor_basis(friendly_payload, None, contract=contract),
                    "data_coverage_status": (
                        "complete_subset" if window and window.get("status") == "ready_with_exclusions" else "complete"
                    ),
                    "product_id": prepared.get("product_id") if isinstance(prepared, Mapping) else None,
                    "rule_revision": prepared.get("rule_revision") if isinstance(prepared, Mapping) else None,
                }

    def _dispatch_isolated_compute(
        self,
        tool_name: str,
        payload: dict[str, Any],
        caller_context: SessionIdentity,
        verified_context: Any,
        *,
        granted_capability: str,
        request_id: str,
        agent_proxy: bool,
        cancellation_check: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        """Freeze one compute request, execute it outside the App process, then commit.

        The capability import lock is held only while Core compiles and binds
        the immutable request.  The expensive calculator and all numeric
        imports live in a worker interpreter; only this method can commit its
        returned draft to the authenticated ResultStore.
        """

        if verified_context is None or not verified_context.task_id:
            raise ValidationError("计算模块必须绑定App任务")
        if self._compute_supervisor is None:
            raise UnavailableCapabilityError("compute supervisor", "App计算进程池未配置")
        scripts_root = str(self._registry.capability_root / "scripts")
        task_id = str(verified_context.task_id)
        prepared_contract: Mapping[str, Any] | None = None
        effective_context = verified_context
        bound_data_store = None
        backtest_window = None
        content_hashes: Mapping[str, str]
        try:
            with capability_import_scope(scripts_root, runtime_root=self._capability_runtime_root):
                self._registry.assert_execution_integrity()
                content_hashes = self._registry.manifest.get("content_hashes")
                if not isinstance(content_hashes, Mapping):
                    raise UnavailableCapabilityError("ToolGateway", "Capability content hashes are unavailable")
                with verified_capability_modules(scripts_root, content_hashes):
                    tool_entry = self._load_tool_entry(scripts_root, content_hashes)
                    load_verified_capability_service(tool_name, scripts_root)
                    if not callable(getattr(tool_entry, "prepare_compute_request", None)):
                        raise ValidationError("Capability缺少正式计算输入编译器")
                    friendly_payload = _business_payload(payload)
                    friendly_payload = _controlled_desk_compute_payload(
                        tool_name, friendly_payload, agent_proxy=agent_proxy,
                    )
                    friendly_payload, render_options = _extract_payoffer_render_options(
                        tool_name, friendly_payload, agent_proxy=agent_proxy,
                    )
                    _run_fair_parameter_preflight(
                        tool_entry,
                        tool_name,
                        friendly_payload,
                    )
                    data_refs = self._resolve_compute_data_refs(
                        tool_name,
                        friendly_payload,
                        caller_context,
                        request_id=request_id,
                    )
                    if data_refs and self._datafetcher is None:
                        raise UnavailableCapabilityError(
                            f"{tool_name}.data_store", "App DataFetcherAdapter is not configured",
                        )
                    compiler_data_store = (
                        self._datafetcher.bind_data_store(
                            caller_context,
                            data_refs=tuple(data_refs),
                        )
                        if data_refs
                        else None
                    )
                    bound_data_store = compiler_data_store
                    friendly_payload = _bind_current_input_to_history(
                        tool_name,
                        friendly_payload,
                        data_refs=tuple(data_refs),
                    )
                    prepared_contract, friendly_payload, backtest_window = _prepare_compute_with_backtest_window(
                        tool_entry,
                        tool_name,
                        friendly_payload,
                        data_refs=tuple(data_refs),
                        data_store=compiler_data_store,
                    )
                    if not isinstance(prepared_contract, Mapping) or not isinstance(prepared_contract.get("request"), Mapping):
                        raise ValidationError("Capability返回的正式计算输入无效")
                    effective_payload = dict(prepared_contract["request"])
                    if render_options is not None:
                        effective_payload["render_options"] = render_options
                    snapshot = self._validated_current_snapshot(
                        caller_context,
                        task_id,
                        prepared_contract,
                    )
                    effective_context = self._bind_product_context(
                        caller_context,
                        verified_context,
                        snapshot,
                    )
            _reject_path_payload(effective_payload)
            snapshots = tuple(
                encode_snapshot(
                    reference,
                    bound_data_store.read_bytes(DataAssetRef(**dict(reference)), tenant_id=caller_context.tenant_id),
                )
                for reference in data_refs
            )
            execution = PreparedComputeExecution.create(
                task_id=task_id,
                module=tool_name,
                tenant_id=caller_context.tenant_id,
                request=effective_payload,
                caller_context={
                    "tenant_id": caller_context.tenant_id,
                    "principal_id": caller_context.principal_id,
                    "role": caller_context.role.value,
                    "capabilities": (granted_capability,),
                    "session_id": caller_context.session_id,
                    "audience": caller_context.audience,
                    "request_id": request_id,
                },
                host_context=effective_context.to_payload(),
                data_snapshots=snapshots,
                scripts_root=scripts_root,
                # The worker receives no App Store root. It binds the verified
                # Capability to its own private temporary runtime directory.
                runtime_root=None,
                content_hashes=content_hashes,
            )
            compute_status = getattr(cancellation_check, "compute_status", None)
            worker_response = self._compute_supervisor.execute(
                execution,
                cancelled=cancellation_check,
                status_callback=compute_status if callable(compute_status) else None,
            )
            _raise_if_compute_cancelled(cancellation_check)
            if (
                worker_response.get("capability_hash") != execution.capability_hash
                or worker_response.get("execution_token") != execution.execution_token
            ):
                raise ValidationError("计算结果的Capability或执行指纹不匹配")
            worker_result = worker_response.get("result")
            draft = worker_response.get("draft")
            if not isinstance(worker_result, Mapping) or not isinstance(draft, Mapping):
                raise ValidationError("计算结果或ModuleRunDraft无效")
            if (
                draft.get("module") != tool_name
                or draft.get("tenant_id") != caller_context.tenant_id
                or draft.get("task_id") != task_id
            ):
                raise ValidationError("ModuleRunDraft超出冻结执行范围")
            files = decode_draft_files(draft.get("files"))
            if self._results is None:
                raise ValidationError("计算模块必须绑定App ResultStore")
            bound_store: Any = self._results.bind_module_store(
                caller_context,
                task_id,
                tool_name,
                resolved_contract_snapshot=snapshot,
                defer_publication=True,
            )
            _raise_if_compute_cancelled(cancellation_check)
            commit = lambda: bound_store.commit_module_run(
                module=tool_name,
                tenant_id=caller_context.tenant_id,
                task_id=task_id,
                run_id=str(draft.get("run_id", "")),
                files=files,
            )
            commit_if_active = getattr(cancellation_check, "commit_if_active", None)
            reference = commit_if_active(commit) if callable(commit_if_active) else commit()
            result = dict(worker_result)
            result["module_run_ref"] = _module_run_ref_payload(reference)
            result = _bind_compute_result(
                result,
                effective_context,
                resolved_contract_snapshot=snapshot,
            )
            if tool_name == "backtester" and backtest_window is not None:
                result["backtest_window"] = backtest_window
            if result.get("ok") is True and result.get("status") in {"succeeded", "partial"}:
                self._results.publish_module_run(caller_context, reference)
            if str(result.get("status", "")).lower() in {"succeeded", "partial"}:
                require_host_bound_run_contract(result, effective_context)
            return result
        except (UserActionError, UnavailableCapabilityError, AuthorizationError, ValidationError):
            raise
        except ContractResolutionError as error:
            raise _public_contract_resolution_error(error) from error
        except Exception as error:
            raise UnavailableCapabilityError(
                f"Capability tool {tool_name}",
                "计算进程未能完成本次请求。请检查产品条款、行情数据和交易日历后重试。",
                failure_code="compute_service_unavailable",
                stage="compute",
            ) from error

    def _resolve_compute_data_refs(
        self,
        module: str,
        payload: Mapping[str, Any],
        identity: SessionIdentity,
        *,
        request_id: str = "",
        preview_only: bool = False,
    ) -> list[dict[str, Any]]:
        raw_identity = payload.get("identity", {})
        if not isinstance(raw_identity, Mapping):
            raise ValidationError("identity必须为对象")
        # Payoffer is a normalized contract-shape calculation. Observation
        # selectors use Core's deterministic structural grid and never require
        # a security, quote, history asset or exchange calendar.
        if module == "payoffer":
            return []
        if self._data_assets is None:
            raise ValidationError("App DataStore未配置，无法绑定正式计算数据")
        underlyings = raw_identity.get("underlyings", [])
        if isinstance(underlyings, str):
            underlyings = [item.strip() for item in underlyings.split(",") if item.strip()]
        if not isinstance(underlyings, (list, tuple)) or not underlyings:
            raise ValidationError("计算请求必须填写underlyings")
        assets = tuple(map(str, underlyings))
        requested: object = None
        if module == "pricer":
            raw_refs = payload.get("market_data_refs")
            if raw_refs is not None:
                if not isinstance(raw_refs, list) or len(raw_refs) != 1:
                    raise ValidationError("当前Pricer页面只能选择一个DataAssetRef")
                requested = raw_refs[0]
        else:
            requested = payload.get("historical_data")
        history_request = _history_fetch_request(module, payload, None, assets)
        if module == "backtester":
            return self._resolve_backtest_data_refs(
                history_request, requested, identity, request_id=request_id,
            )
        history_ref = None
        covering_history_resolver = getattr(self._data_assets, "resolve_market_history_covering", None)
        if history_ref is None and _empty_data_reference(requested) and callable(covering_history_resolver):
            history_ref = covering_history_resolver(
                identity,
                asset_ids=assets,
                start_date=str(history_request["start_date"]),
                end_date=str(history_request["end_date"]),
                fields=tuple(str(item) for item in history_request["fields"]),
                frequency=str(history_request["frequency"]),
                adjustment=str(history_request["adjustment"]),
                allow_available_end=False,
                require_verified_calendar=False,
            )
        if history_ref is None:
            try:
                history_ref = self._data_assets.resolve_for_compute(
                    identity,
                    requested,
                    asset_ids=assets,
                    schema_id="market-history",
                )
            except UserActionError:
                history_ref = None

        if (
            history_ref is not None
            and not _history_ref_covers_request(history_ref, history_request)
        ):
            # A local selection is a reuse preference, not a reason to block
            # the calculator.  When its validated interval is too short, the
            # Host transparently fetches and binds a complete replacement.
            history_ref = None

        if history_ref is None:
            history_ref = self._complete_pricing_history(
                history_request,
                requested,
                identity,
                request_id=request_id,
            )
        refs = [history_ref]
        if module == "pricer":
            requested_calendar = payload.get("trading_calendar_ref")
            calendar_ref = self._resolve_or_fetch_trading_calendar(
                payload,
                assets,
                identity,
                request_id=request_id,
                required=_requires_path_calendar(None, payload),
                requested_calendar=requested_calendar,
                market_history_ref=history_ref,
            )
            if calendar_ref is not None:
                refs.append(calendar_ref)
        return refs

    def _complete_pricing_history(
        self,
        request: dict[str, Any],
        requested_history: object,
        identity: SessionIdentity,
        *,
        request_id: str,
    ) -> dict[str, Any]:
        """Prove civil boundaries before deciding which observed bars to fill."""

        assets = tuple(request["asset_ids"])
        as_of = _shanghai_now().date()
        calendar_ref = None
        try:
            covering = getattr(self._data_assets, "resolve_trading_calendar_covering", None)
            if callable(covering):
                calendar_ref = covering(
                    identity, asset_ids=assets,
                    start_date=request["start_date"], end_date=request["end_date"],
                )
            else:
                calendar_ref = self._data_assets.resolve_for_compute(
                    identity, None, asset_ids=assets, schema_id="trading-calendar",
                )
        except UserActionError:
            pass
        sessions = _pricing_history_sessions(request, calendar_ref, as_of=as_of)
        if not sessions:
            calendar_ref = self._fetch_and_bind_history_calendar(request, identity, request_id=request_id)
            sessions = _pricing_history_sessions(request, calendar_ref, as_of=as_of)
        if not sessions:
            raise UserActionError(
                "trading_calendar_coverage_insufficient",
                "交易日历未能证明请求行情区间的实际交易日，请检查数据接口后重试。",
                stage="data",
            )
        observed_request = {**request, "start_date": sessions[0], "end_date": sessions[-1]}
        history_ref = None
        covering_history = getattr(self._data_assets, "resolve_market_history_covering", None)
        if _empty_data_reference(requested_history) and callable(covering_history):
            history_ref = covering_history(
                identity, asset_ids=assets,
                start_date=sessions[0], end_date=sessions[-1],
                fields=tuple(request["fields"]), frequency=request["frequency"], adjustment=request["adjustment"],
                allow_available_end=False, require_verified_calendar=False,
            )
        if history_ref is None:
            try:
                history_ref = self._data_assets.resolve_for_compute(
                    identity, requested_history, asset_ids=assets, schema_id="market-history",
                )
            except UserActionError:
                pass
        if history_ref is not None and _history_ref_covers_request(
            history_ref, observed_request, required_sessions=sessions,
        ):
            return history_ref

        # extend_only and formal calendar evidence let DataFetcher retain all
        # valid local bars while acquiring missing sessions from the provider.
        history_ref = self._fetch_and_bind_history(
            request, identity, request_id=request_id, trading_calendar_ref=calendar_ref,
        )
        if _history_ref_covers_request(history_ref, observed_request, required_sessions=sessions):
            return history_ref
        # A fresh DataFetcher result may legitimately await today's daily bar.
        # Every earlier session is still mandatory; an old request timestamp
        # cannot turn a historical gap into this current-day exception.
        prior_sessions = tuple(day for day in sessions if day < as_of.isoformat())
        if sessions[-1] == as_of.isoformat() and prior_sessions and _history_ref_covers_request(
            history_ref, {**observed_request, "end_date": prior_sessions[-1]}, required_sessions=prior_sessions,
        ):
            return history_ref
        raise UserActionError(
            "pricing_history_coverage_insufficient",
            "自动补取的行情仍缺少已完成交易日，无法按请求估值日定价。请检查数据接口后重试。",
            stage="data",
        )

    def _resolve_backtest_data_refs(
        self,
        request: dict[str, Any],
        requested_history: object,
        identity: SessionIdentity,
        *,
        request_id: str,
    ) -> list[dict[str, Any]]:
        """Resolve an executable market horizon before choosing cached prices.

        A civil maturity on a closed day needs a later verified session to
        close Backtester's calendar. History commits only observed sessions,
        so that witness session must also be acquired. Future maturities and
        blank windows instead stop at a calendar-verified current boundary.
        """

        assets = tuple(request["asset_ids"])
        as_of = _shanghai_now().date()
        calendar_ref = None
        try:
            covering_calendar = getattr(self._data_assets, "resolve_trading_calendar_covering", None)
            if callable(covering_calendar):
                calendar_ref = covering_calendar(
                    identity, asset_ids=assets,
                    start_date=request["start_date"], end_date=request["end_date"],
                )
            else:
                calendar_ref = self._data_assets.resolve_for_compute(
                    identity, None, asset_ids=assets, schema_id="trading-calendar",
                )
        except UserActionError:
            pass
        history_request = _backtest_history_request_for_calendar(request, calendar_ref, as_of=as_of)
        if history_request is None:
            # Buffer only the calendar lookup. Prices end at the first actual
            # closing session, not at an arbitrary number of buffer days.
            calendar_end = min(date.fromisoformat(request["end_date"]) + timedelta(days=14), as_of)
            calendar_ref = self._fetch_and_bind_backtest_calendar(
                {**request, "end_date": calendar_end.isoformat()}, identity, request_id=request_id,
            )
            history_request = _backtest_history_request_for_calendar(request, calendar_ref, as_of=as_of)
            if history_request is None and calendar_end < as_of:
                # An unusually long closure is resolved from evidence too;
                # fourteen days is a query bound, never a calendar assumption.
                calendar_ref = self._fetch_and_bind_backtest_calendar(
                    {**request, "end_date": as_of.isoformat()}, identity, request_id=request_id,
                )
                history_request = _backtest_history_request_for_calendar(request, calendar_ref, as_of=as_of)
        if history_request is None:
            raise UserActionError(
                "trading_calendar_coverage_insufficient",
                "交易日历未能证明本次回测到期边界或最新交易日，请检查数据接口后重试。",
                stage="data",
            )

        history_ref = None
        covering_history = getattr(self._data_assets, "resolve_market_history_covering", None)
        if _empty_data_reference(requested_history) and callable(covering_history):
            history_ref = covering_history(
                identity, asset_ids=assets,
                start_date=history_request["start_date"], end_date=history_request["end_date"],
                fields=tuple(history_request["fields"]), frequency=history_request["frequency"],
                adjustment=history_request["adjustment"],
                allow_available_end=False, require_verified_calendar=True,
            )
        if history_ref is None:
            try:
                history_ref = self._data_assets.resolve_for_compute(
                    identity, requested_history, asset_ids=assets, schema_id="market-history",
                )
            except UserActionError:
                pass
        if history_ref is not None and _history_ref_supports_automatic_backtest_cutoff(history_ref, history_request):
            # Calendar freshness was established above. Reuse the original
            # immutable binding rather than swapping its signed revision.
            bound_calendar = _calendar_reference_bound_to_history(history_ref)
            try:
                original_calendar = self._data_assets.resolve_for_compute(
                    identity, bound_calendar, asset_ids=assets, schema_id="trading-calendar",
                ) if bound_calendar else calendar_ref
            except UserActionError:
                original_calendar = None
            if original_calendar is not None and _calendar_ref_closes_market_history(original_calendar, history_ref):
                coverage = original_calendar.get("coverage", {})
                if all(history_ref["coverage"].get(key) == coverage.get(key) for key in ("calendar_id", "calendar_revision")):
                    return [history_ref, original_calendar]

        history_ref = self._fetch_and_bind_history(
            history_request, identity, request_id=request_id, trading_calendar_ref=calendar_ref,
        )
        if not _history_ref_supports_automatic_backtest_cutoff(history_ref, history_request):
            # A fresh current-day request may precede its final daily bar.
            # Missing past sessions must never become a clipped entry window.
            prior_sessions = [
                session for session in calendar_ref["coverage"]["sessions"]
                if session < as_of.isoformat()
            ]
            if not (
                history_request["end_date"] == as_of.isoformat()
                and prior_sessions
                and _history_ref_supports_automatic_backtest_cutoff(
                    history_ref, {**history_request, "end_date": max(prior_sessions)},
                )
            ):
                raise UserActionError(
                    "backtest_history_coverage_insufficient",
                    "自动补取的行情仍缺少已发生的合同终点，请检查数据接口后重试。",
                    stage="data",
                )
        return [history_ref, calendar_ref]

    def _resolve_or_fetch_trading_calendar(
        self,
        payload: Mapping[str, Any],
        underlyings: tuple[str, ...],
        identity: SessionIdentity,
        *,
        request_id: str,
        required: bool,
        requested_calendar: object = None,
        requested_calendar_is_internal: bool = False,
        market_history_ref: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Resolve a covering global calendar, fetching a wider one when needed."""

        has_requested_calendar = requested_calendar is not None and requested_calendar != ""
        if not required and not has_requested_calendar:
            return None
        calendar_request = _calendar_fetch_request(
            payload,
            None,
            underlyings,
            market_history_ref=market_history_ref,
        )
        counted_calendar = compile_compute_data_requirements("pricer", payload).observation_count is not None
        try:
            if (
                not has_requested_calendar
                and not counted_calendar
                and callable(getattr(self._data_assets, "resolve_trading_calendar_covering", None))
            ):
                # Do not let a more recently registered short calendar hide a
                # pre-existing longer snapshot.  The exact interval is known
                # before the first observed contract is compiled.
                calendar_ref = self._data_assets.resolve_trading_calendar_covering(
                    identity,
                    asset_ids=underlyings,
                    start_date=str(calendar_request["start_date"]),
                    end_date=str(calendar_request["end_date"]),
                )
            else:
                calendar_ref = self._data_assets.resolve_for_compute(
                    identity,
                    requested_calendar,
                    asset_ids=underlyings,
                    schema_id="trading-calendar",
                    optional=False,
                )
        except UserActionError:
            if (has_requested_calendar and not requested_calendar_is_internal) or not required:
                raise
            calendar_ref = None

        backtest_window = "backtest_config" in payload
        pricing_config = payload.get("pricing_config", {})
        contract_identity = payload.get("identity", {})
        history_window = {} if backtest_window else {
            "valuation_date": str(pricing_config.get("valuation_date") or _shanghai_now().date().isoformat())
            if isinstance(pricing_config, Mapping) else _shanghai_now().date().isoformat(),
            "contract_start_date": (contract_identity.get("contract_start_date") or None)
            if isinstance(contract_identity, Mapping) else None,
        }
        calendar_covers_request = (
            calendar_ref is not None and _calendar_ref_covers_request(calendar_ref, calendar_request, payload=payload)
        )
        calendar_supports_market_cutoff = (
            calendar_ref is not None
            and backtest_window
            and _calendar_ref_supports_automatic_backtest_cutoff(
                calendar_ref,
                calendar_request,
                market_history_ref,
            )
        )
        if calendar_ref is not None and not (
            calendar_covers_request or calendar_supports_market_cutoff
        ):
            if has_requested_calendar and not requested_calendar_is_internal:
                raise UserActionError(
                    "trading_calendar_coverage_insufficient",
                    "所选交易日历未完整覆盖本次合约期限。请重新获取覆盖完整期限的交易日历后重试。",
                    stage="data",
                    next_step="请重新获取覆盖完整期限的交易日历后重试。",
                )
            calendar_ref = None

        if calendar_ref is not None and not _calendar_ref_closes_market_history(
            calendar_ref, market_history_ref, **history_window,
        ):
            if has_requested_calendar and not requested_calendar_is_internal:
                raise UserActionError(
                    "trading_calendar_history_mismatch",
                    "所选交易日历与本次行情证据不一致，无法建立完整定价输入。",
                    stage="data",
                    next_step="请选择与历史行情交易所一致、覆盖合同历史观察区间和估值边界的交易日历后重试。",
                )
            # A cached calendar must preserve every completed observation and
            # the valuation boundary. Fetch a wider snapshot while preserving
            # the history asset as independent provenance.
            calendar_ref = None

        if calendar_ref is None:
            if self._datafetcher is None:
                raise UnavailableCapabilityError(
                    "trading_calendar", "App DataFetcherAdapter is not configured",
                )
            calendar_result = self._datafetcher.dispatch(
                calendar_request,
                identity,
                request_id=f"{request_id}-calendar" if request_id else f"calendar-{uuid4().hex}",
            )
            calendar_asset = calendar_result.get("data_asset_ref")
            if calendar_result.get("ok") is not True or not isinstance(calendar_asset, dict):
                _raise_datafetch_failure(
                    calendar_result,
                    default_code="trading_calendar_unavailable",
                    default_message="交易日历暂不可用，且没有完整覆盖本次区间的已验证缓存。",
                    default_next_step="请检查数据接口配置，或在数据获取中补取交易日历后重试。",
                )
            self._data_assets.register(identity, calendar_asset)
            calendar_ref = self._data_assets.resolve_for_compute(
                identity,
                calendar_asset["data_asset_id"],
                asset_ids=underlyings,
                schema_id="trading-calendar",
            )
            if not _calendar_ref_covers_request(calendar_ref, calendar_request, payload=payload):
                raise UserActionError(
                    "trading_calendar_coverage_insufficient",
                    "自动取得的交易日历未完整覆盖本次合约期限，请检查数据接口后重试。",
                    stage="data",
                    next_step="请检查数据接口并重新获取覆盖完整期限的交易日历后重试。",
                )
            if not _calendar_ref_closes_market_history(calendar_ref, market_history_ref, **history_window):
                raise UserActionError(
                    "trading_calendar_history_mismatch",
                    "自动取得的交易日历未完整覆盖合同历史观察区间或估值边界，无法建立完整定价输入。",
                    stage="data",
                    next_step="请重新获取与历史行情交易所一致、覆盖合同历史观察区间与剩余期限的交易日历。",
                )
        if calendar_ref is None:
            raise ValidationError("交易日历未能登记为正式DataAssetRef")
        return calendar_ref

    def _fetch_and_bind_history(
        self,
        request: dict[str, Any],
        identity: SessionIdentity,
        *,
        request_id: str,
        trading_calendar_ref: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._datafetcher is None:
            raise UnavailableCapabilityError(
                "market_data",
                "App DataFetcherAdapter is not configured",
                failure_code="market_data_unavailable",
                stage="data",
            )
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
            _raise_datafetch_failure(
                result,
                default_code="market_data_unavailable",
                default_message="未能自动取得所需行情，请检查数据接口配置或缩短数据区间后重试。",
                default_next_step="请检查数据接口配置，或在数据获取中补取日频行情后重试。",
            )
        self._data_assets.register(identity, asset)
        resolved = self._data_assets.resolve_for_compute(
            identity,
            asset.get("data_asset_id"),
            asset_ids=tuple(request["asset_ids"]),
            schema_id="market-history",
        )
        if resolved is None:
            raise ValidationError("自动获取的行情未能登记为正式DataAssetRef")
        return resolved

    def _single_day_datafetch_calendar(
        self,
        request: Mapping[str, Any],
        identity: SessionIdentity,
        *,
        request_id: str,
    ) -> dict[str, Any] | None:
        """Bind verified prior-session evidence for a remote single-day fetch."""

        if self._datafetcher is None or not self._datafetcher.requires_ifind(request):
            return None
        action = str(request.get("action", "fetch")).strip().lower()
        source = request.get("request", request.get("data_request", request))
        if action != "fetch" or not isinstance(source, Mapping):
            return None
        start_date = source.get("start_date")
        end_date = source.get("end_date")
        if not isinstance(start_date, str) or start_date != end_date:
            return None
        raw_assets = source.get("asset_ids", source.get("asset_id"))
        if isinstance(raw_assets, str):
            asset_ids = (raw_assets,)
        elif isinstance(raw_assets, (list, tuple)):
            asset_ids = tuple(str(item) for item in raw_assets)
        else:
            return None
        if not asset_ids:
            return None
        task_id = request.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValidationError("DataFetcher单日行情必须绑定当前App任务")
        try:
            requested_day = date.fromisoformat(start_date)
        except ValueError as error:
            raise ValidationError("DataFetcher单日行情日期必须为YYYY-MM-DD") from error
        coverage_start = _years_before(requested_day, 1).isoformat()
        calendar_ref = self._data_assets.resolve_trading_calendar_covering(
            identity,
            asset_ids=asset_ids,
            start_date=coverage_start,
            end_date=start_date,
        ) if self._data_assets is not None else None
        if calendar_ref is not None:
            return calendar_ref
        result = self._datafetcher.dispatch(
            {
                "action": "fetch_calendar",
                "task_id": task_id,
                "asset_ids": list(asset_ids),
                "start_date": coverage_start,
                "end_date": start_date,
            },
            identity,
            request_id=f"{request_id}-calendar" if request_id else f"calendar-{uuid4().hex}",
        )
        asset = result.get("data_asset_ref")
        if result.get("ok") is not True or not isinstance(asset, dict):
            _raise_datafetch_failure(
                result,
                default_code="trading_calendar_unavailable",
                default_message="单日行情缺少可验证交易日历。",
                default_next_step="请验证数据连接后重新获取该日期行情。",
            )
        if self._data_assets is None:
            raise ValidationError("App DataStore未配置，无法登记自动交易日历")
        self._data_assets.register(identity, asset)
        calendar_ref = self._data_assets.resolve_trading_calendar_covering(
            identity,
            asset_ids=asset_ids,
            start_date=coverage_start,
            end_date=start_date,
        )
        if calendar_ref is None:
            raise ValidationError("自动获取的交易日历未能登记为正式DataAssetRef")
        return calendar_ref

    def _fetch_and_bind_backtest_calendar(
        self,
        history_request: Mapping[str, Any],
        identity: SessionIdentity,
        *,
        request_id: str,
    ) -> dict[str, Any]:
        """Bind the calendar used by the backtest history acquisition hook."""

        return self._fetch_and_bind_history_calendar(history_request, identity, request_id=request_id)

    def _fetch_and_bind_history_calendar(
        self,
        history_request: Mapping[str, Any],
        identity: SessionIdentity,
        *,
        request_id: str,
    ) -> dict[str, Any]:
        """Fetch verified sessions before completing observed history."""

        if self._datafetcher is None:
            raise UnavailableCapabilityError(
                "history.trading_calendar",
                "App DataFetcherAdapter is not configured",
                failure_code="trading_calendar_unavailable",
                stage="data",
            )
        if self._data_assets is None:
            raise ValidationError("App DataStore未配置，无法登记自动交易日历")
        calendar_result = self._datafetcher.dispatch(
            _backtest_calendar_fetch_request(history_request),
            identity,
            request_id=f"{request_id}-calendar" if request_id else f"calendar-{uuid4().hex}",
        )
        calendar_asset = calendar_result.get("data_asset_ref")
        if calendar_result.get("ok") is not True or not isinstance(calendar_asset, dict):
            _raise_datafetch_failure(
                calendar_result,
                default_code="trading_calendar_unavailable",
                default_message="交易日历暂不可用，无法为本次计算准备已验证行情。请检查数据接口后重试。",
                default_next_step="请检查数据接口配置，或在数据获取中补取交易日历后重试。",
            )
        self._data_assets.register(identity, calendar_asset)
        calendar_ref = self._data_assets.resolve_for_compute(
            identity,
            calendar_asset.get("data_asset_id"),
            asset_ids=tuple(str(item) for item in history_request["asset_ids"]),
            schema_id="trading-calendar",
        )
        if calendar_ref is None:
            raise ValidationError("自动获取的交易日历未能登记为正式DataAssetRef")
        return calendar_ref

    def _prepare_hosted_payoffer_preview(
        self,
        payload: Mapping[str, Any],
        context: Any,
    ) -> tuple[dict[str, Any], Any]:
        """Validate one structural page edit without acquiring market data."""

        if context is None or not context.task_id:
            raise ValidationError("Hosted Payoffer预览必须绑定当前任务")
        friendly = _business_payload(payload)
        if "identity" in friendly or "underlyings" in friendly:
            raise ValidationError("Payoffer预览只接受产品、收益结构条款和图形尺寸")
        render_options = friendly.pop("render_options", None)
        action = str(friendly.pop("action", "preview")).strip().lower()
        if action != "preview":
            raise ValidationError("Payoffer预览请求的action必须为preview")
        unknown = set(friendly) - {"product_id", "term_overrides"}
        if unknown:
            raise ValidationError(f"Payoffer预览含无关字段：{','.join(sorted(unknown))}")
        product_id = friendly.get("product_id")
        if not isinstance(product_id, str) or not product_id:
            raise ValidationError("Payoffer预览缺少产品标识")
        effective_payload = {
            "action": "preview",
            "product_id": product_id,
        }
        if "term_overrides" in friendly:
            effective_payload["term_overrides"] = friendly["term_overrides"]
        if render_options is not None:
            effective_payload["render_options"] = render_options
        return effective_payload, context

    def _bind_product_context(
        self,
        identity: SessionIdentity,
        context: Any,
        snapshot: Mapping[str, Any],
    ) -> Any:
        snapshot_identity = snapshot.get("identity")
        product_id = snapshot_identity.get("product_id") if isinstance(snapshot_identity, Mapping) else None
        rule_revision = snapshot_identity.get("rule_revision") if isinstance(snapshot_identity, Mapping) else None
        if not isinstance(product_id, str) or not product_id:
            raise ValidationError("ResolvedContract缺少product_id")
        if not isinstance(rule_revision, int) or isinstance(rule_revision, bool) or rule_revision <= 0:
            raise ValidationError("ResolvedContract缺少正整数rule_revision")
        catalog_version = context.catalog_version or str(self._registry.manifest["catalog_version"])
        candidate_id = context.candidate_id or f"candidate-{uuid4().hex[:24]}"
        return self._registry.bind_product_context(
            identity,
            context,
            analysis_case_id=context.analysis_case_id or f"case-{uuid4().hex[:24]}",
            candidate_id=candidate_id,
            catalog_version=catalog_version,
            product_id=product_id,
            rule_revision=rule_revision,
        )

    def _validated_current_snapshot(
        self,
        identity: SessionIdentity,
        task_id: str,
        prepared: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate one freshly compiled current input without storing contract state."""

        if self._tasks is not None:
            get_task = getattr(self._tasks, "get", None)
            if not callable(get_task):
                raise ValidationError("App任务服务不可用")
            get_task(identity, task_id)
        snapshot = prepared.get("resolved_contract")
        if not isinstance(snapshot, Mapping):
            raise ValidationError("正式计算输入缺少ResolvedContract快照")
        try:
            contract = ResolvedContract.from_controlled_snapshot(snapshot)
        except ContractResolutionError as error:
            raise ValidationError(str(error)) from error
        if not isinstance(contract.product_id, str) or not contract.product_id:
            raise ValidationError("ResolvedContract快照缺少product_id")
        revision = contract.identity.get("rule_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise ValidationError("ResolvedContract快照缺少正整数rule_revision")
        return contract.to_controlled_snapshot()

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
        # Selection and rendering must use the same verified candidate variants.
        verified_catalog = self._reporter.dispatch({"action": "list_report_sources", "task_id": task_id}, identity)
        source = next((item for item in verified_catalog.get("sources", []) if item.get("source_id") == source_id), None)
        if source is None:
            raise ValidationError("Reporter source没有可验证的当前证据")
        if _scope_text(context, "analysis_case_id") and source.get("analysis_case_id") != context.analysis_case_id:
            raise ValidationError("Reporter source与Module Host analysis_case_id不一致")
        if _scope_text(context, "catalog_version") and source.get("catalog_version") != context.catalog_version:
            raise ValidationError("Reporter source与Module Host catalog_version不一致")
        if str(selection.get("output_type", "")).strip().lower() == "quote":
            return {
                **dict(payload),
                "task_id": task_id,
                "selection": self._controlled_quote_selection(selection, source, identity, task_id),
            }
        candidate_ids = selection.get("candidate_ids")
        if (
            not isinstance(candidate_ids, list)
            or not candidate_ids
            or len(candidate_ids) > 128
            or any(not isinstance(candidate_id, str) or not candidate_id.strip() for candidate_id in candidate_ids)
            or len(set(candidate_ids)) != len(candidate_ids)
        ):
            raise ValidationError("Reporter运行必须按顺序显式选择不重复的candidate_ids")
        if _scope_text(context, "candidate_id") and candidate_ids != [context.candidate_id]:
            raise ValidationError("Reporter候选集合与Module Host candidate_id绑定不一致")
        selected_modules = selection.get("selected_modules")
        if (
            not isinstance(selected_modules, list)
            or not selected_modules
            or len(set(selected_modules)) != len(selected_modules)
            or any(module not in {"recommender", "payoff", "pricing", "backtest"} for module in selected_modules)
        ):
            raise ValidationError("Reporter必须显式选择不重复的计算模块")
        raw_all_refs = selection.get("module_run_refs")
        if not isinstance(raw_all_refs, Mapping) or set(raw_all_refs) != set(candidate_ids):
            raise ValidationError("Reporter必须为每个candidate显式提供ModuleRunRef映射")
        source_candidates = {
            str(item.get("candidate_id")): item
            for item in source.get("candidates", ())
            if isinstance(item, Mapping) and isinstance(item.get("candidate_id"), str)
        }
        controlled_all_refs: dict[str, dict[str, dict[str, str] | None]] = {}
        for candidate_id in candidate_ids:
            candidate = source_candidates.get(candidate_id)
            if candidate is None:
                raise ValidationError("Reporter candidate不属于当前任务source")
            if (
                _scope_text(context, "product_id")
                and candidate.get("product_id") != context.product_id
            ):
                raise ValidationError("Reporter candidate与Module Host product_id不一致")
            if context.rule_revision is not None and candidate.get("rule_revision") != context.rule_revision:
                raise ValidationError("Reporter candidate与Module Host rule_revision不一致")
            if "recommender" in selected_modules and not candidate.get("recommendation_bound"):
                raise ValidationError("Reporter候选没有对应的已保存推荐依据")
            compute_modules = [module for module in selected_modules if module != "recommender"]
            raw_refs = raw_all_refs.get(candidate_id)
            if not isinstance(raw_refs, Mapping) or set(raw_refs) != set(compute_modules):
                raise ValidationError("Reporter每个候选和已选模块必须显式提供完整ModuleRunRef或null")
            source_options = candidate.get("module_run_options")
            if not isinstance(source_options, Mapping):
                raise ValidationError("Reporter source缺少已验证ModuleRunRef选项")
            controlled_refs: dict[str, dict[str, str] | None] = {}
            for display_module in compute_modules:
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
                    self._results.verify_owned_module_run(identity, _module_run_ref_payload(reference))
                except (KeyError, ValidationError, AuthorizationError, OSError) as error:
                    raise ValidationError("Reporter ModuleRunRef已失效或不再属于当前主体") from error
                controlled_refs[display_module] = _module_run_ref_payload(reference)
            controlled_all_refs[candidate_id] = controlled_refs
        return {
            **dict(payload),
            "task_id": task_id,
            "selection": {
                **dict(selection),
                "source_id": source_id,
                "candidate_ids": list(candidate_ids),
                "selected_modules": list(selected_modules),
                "module_run_refs": controlled_all_refs,
            },
        }

    def _controlled_reporter_rerender_request(
        self,
        payload: Mapping[str, Any],
        identity: SessionIdentity,
        context: Any,
    ) -> dict[str, Any]:
        """Authorize a PDF re-render against one immutable report delivery.

        Re-rendering is not a new analysis request.  The browser may name only
        the source ReportRun and the new delivery identifier; ResultStore
        confirms ownership and task scope before Reporter receives the request.
        """

        if self._results is None:
            raise ValidationError("Reporter另存交付缺少App ResultStore")
        task_id = getattr(context, "task_id", None)
        if not isinstance(task_id, str) or not task_id:
            raise ValidationError("Reporter另存交付必须绑定当前任务")
        allowed = {"action", "task_id", "source_report_run_id", "output_type", "format", "report_run_id"}
        unknown = set(payload).difference(allowed)
        if unknown:
            raise ValidationError(f"Reporter另存交付含未知字段：{','.join(sorted(unknown))}")
        source_run_id = payload.get("source_report_run_id")
        if not isinstance(source_run_id, str) or not source_run_id:
            raise ValidationError("Reporter另存交付缺少source_report_run_id")
        try:
            source = self._results.get_owned_report_run(identity, source_run_id)
        except (KeyError, AuthorizationError) as error:
            raise ValidationError("源报告不存在或不可访问") from error
        if source.get("task_id") != task_id:
            raise ValidationError("源报告不属于当前任务")
        source_request = source.get("report_request")
        source_kind = str(source_request.get("output_type", "")).strip().lower() if isinstance(source_request, Mapping) else ""
        if source_kind not in {"card", "quote", "report"}:
            raise ValidationError("源交付类型不支持另存")
        output_type = str(payload.get("output_type", "")).strip().lower()
        if output_type != source_kind:
            raise ValidationError("另存交付类型必须与源报告一致")
        return {
            "action": "rerender",
            "task_id": task_id,
            "source_report_run_id": source_run_id,
            "output_type": source_kind,
            "format": payload.get("format"),
            "report_run_id": payload.get("report_run_id"),
        }

    def _controlled_quote_selection(
        self,
        selection: Mapping[str, Any],
        source: Mapping[str, Any],
        identity: SessionIdentity,
        task_id: str,
    ) -> dict[str, Any]:
        """Verify each requested Quote row against its saved task evidence.

        Quote is intentionally different from Card and Report: every row can
        come from another saved task, while this task remains the destination
        for the generated table.  The browser still sends opaque RunRefs only;
        each row is re-authorized against its own saved source.
        """

        raw_items = selection.get("quote_items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValidationError("组合报价至少需要选择一行已保存运行结果")
        controlled_items: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        expected_modules = {"payoff": "payoffer", "pricing": "pricer", "backtest": "backtester"}
        for index, raw_item in enumerate(raw_items, start=1):
            if not isinstance(raw_item, Mapping) or set(raw_item) != {"source_id", "candidate_id", "module", "module_run_ref"}:
                raise ValidationError(f"Quote第{index}行必须包含候选、来源模块和完整运行引用")
            item_source_id = raw_item.get("source_id")
            if not isinstance(item_source_id, str) or not item_source_id:
                raise ValidationError(f"Quote第{index}行缺少已保存合同快照来源")
            if item_source_id == source.get("source_id"):
                item_source = source
            else:
                try:
                    item_source = self._results.get_owned_report_source(identity, item_source_id)
                except (KeyError, AuthorizationError) as error:
                    raise ValidationError(f"Quote第{index}行来源不可访问") from error
            item_task_id = item_source.get("task_id")
            if not isinstance(item_task_id, str) or not item_task_id:
                raise ValidationError(f"Quote第{index}行来源任务无效")
            candidates = {
                str(item.get("candidate_id")): item
                for item in item_source.get("candidates", ())
                if isinstance(item, Mapping) and isinstance(item.get("candidate_id"), str)
            }
            candidate_id = raw_item.get("candidate_id")
            module = str(raw_item.get("module", "")).strip().lower()
            candidate = candidates.get(candidate_id) if isinstance(candidate_id, str) else None
            if candidate is None or module not in expected_modules:
                raise ValidationError(f"Quote第{index}行不属于当前任务的可选合同")
            raw_ref = raw_item.get("module_run_ref")
            if not isinstance(raw_ref, Mapping):
                raise ValidationError(f"Quote第{index}行缺少完整运行引用")
            try:
                reference = ModuleRunRef(**dict(raw_ref))
            except (TypeError, ValueError) as error:
                raise ValidationError(f"Quote第{index}行运行引用无效") from error
            if (
                reference.module != expected_modules[module]
                or reference.tenant_id != identity.tenant_id
                or reference.task_id != item_task_id
            ):
                raise ValidationError(f"Quote第{index}行运行引用超出当前租户、任务或模块范围")
            options = candidate.get("module_run_options")
            allowed = options.get(module) if isinstance(options, Mapping) else None
            if not isinstance(allowed, list) or not any(
                _same_module_run_ref(reference, option) for option in allowed if isinstance(option, Mapping)
            ):
                raise ValidationError(f"Quote第{index}行不是当前ResultStore已验证的参数版本")
            signature = (item_source_id, candidate_id, module, reference.run_id)
            if signature in seen:
                raise ValidationError("Quote不得重复选择同一参数运行")
            seen.add(signature)
            try:
                self._results.verify_owned_module_run(identity, _module_run_ref_payload(reference))
            except (KeyError, ValidationError, AuthorizationError, OSError) as error:
                raise ValidationError(f"Quote第{index}行运行结果已失效或不再属于当前主体") from error
            controlled_items.append({
                "source_id": item_source_id,
                "candidate_id": candidate_id,
                "module": module,
                "module_run_ref": _module_run_ref_payload(reference),
            })
        return {
            key: selection[key]
            for key in ("source_id", "output_type", "format", "audience", "report_run_id", "metadata")
            if key in selection
        } | {"source_id": str(source["source_id"]), "output_type": "quote", "quote_items": controlled_items}

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
        "default",
        "preview",
        "status",
        "list_assets",
        "list_report_sources",
    }


def _raise_if_compute_cancelled(cancellation_check: Callable[[], bool] | None) -> None:
    if cancellation_check is not None and cancellation_check():
        raise UserActionError(
            "compute_cancelled",
            "本次计算已取消。",
            stage="compute",
            next_step="如需结果，请重新运行。",
        )


def _validate_hosted_payoffer_preview_result(result: object) -> None:
    if not isinstance(result, Mapping) or result.get("ok") is not True:
        raise ValidationError("Hosted Payoffer预览未返回有效结果")
    preview = result.get("preview")
    display = preview.get("display_contract") if isinstance(preview, Mapping) else None
    product_id = preview.get("product_id") if isinstance(preview, Mapping) else None
    rule_revision = preview.get("rule_revision") if isinstance(preview, Mapping) else None
    if (
        not isinstance(display, Mapping)
        or not isinstance(product_id, str)
        or not product_id
        or isinstance(rule_revision, bool)
        or not isinstance(rule_revision, int)
        or rule_revision <= 0
    ):
        raise ValidationError("Hosted Payoffer预览未返回当前产品规则与结构条款")


_HOST_SCOPE_FIELDS = (
    "task_id", "analysis_case_id", "candidate_id", "catalog_version",
)


def _validate_host_scope(payload: dict[str, Any], context: Any) -> None:
    """Reject scope conflicts without polluting a module's business input."""
    for field in _HOST_SCOPE_FIELDS:
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
    for field in _HOST_SCOPE_FIELDS:
        value.pop(field, None)
    return _canonicalize_app_dates(value)


def _controlled_desk_compute_payload(
    tool_name: str,
    payload: Mapping[str, Any],
    *,
    agent_proxy: bool,
) -> dict[str, Any]:
    """Translate the Payoffer Desk edit request without trusting identity fields."""

    value = dict(payload)
    # ModuleRun identity is always generated by the server.  A stale page or
    # caller-provided identifier must not leak into the calculator protocol or
    # make a repeated run collide with an earlier immutable result.
    value.pop("run_id", None)
    if tool_name == "pricer":
        config = value.get("pricing_config")
        if isinstance(config, Mapping) and config.get("valuation_date") in {None, ""}:
            # Match the Desk's initial valuation date before data acquisition
            # and Core compilation, so both stages use the same date.
            value["pricing_config"] = {**dict(config), "valuation_date": _shanghai_now().date().isoformat()}
        contract_identity = value.get("identity", {})
        if agent_proxy and isinstance(contract_identity, Mapping) and not contract_identity.get("contract_start_date"):
            # A newly recommended contract starts at the verified latest close,
            # including before today's close and on non-trading days.
            value.setdefault("auto_contract_start_date", True)
    if tool_name != "payoffer":
        return value
    if "identity" in value:
        raise ValidationError(
            "Payoffer不接受合同identity；请只提交产品和影响收益结构的合同条款"
        )
    # Payoffer plots normalized contract settlement returns.  The selected
    # market instrument does not change that structural curve.
    value.pop("underlyings", None)
    return value


def _extract_payoffer_render_options(
    tool_name: str,
    payload: Mapping[str, Any],
    *,
    agent_proxy: bool,
) -> tuple[dict[str, Any], dict[str, int] | None]:
    """将Desk图片尺寸与合同编译输入分离，并在Worker请求中重新绑定。"""

    value = dict(payload)
    raw = value.pop("render_options", None)
    if raw is None:
        return value, None
    if tool_name != "payoffer" or agent_proxy:
        raise ValidationError("图片尺寸只允许由OptDesk收益结构页面提交")
    if not isinstance(raw, Mapping) or set(raw) != {"width", "height"}:
        raise ValidationError("render_options必须且只能包含width与height")
    normalized: dict[str, int] = {}
    for key, minimum, maximum, label in (
        ("width", 240, 6000, "图片宽度"),
        ("height", 180, 6000, "图片高度"),
    ):
        item = raw.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
            raise ValidationError(f"{label}必须是{minimum}至{maximum}之间的整数像素值")
        normalized[key] = item
    return value, normalized


def _canonicalize_app_dates(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate App calendar-control values at the presentation boundary."""

    value = dict(payload)
    for section_name, fields in (
        ("pricing_config", ("valuation_date",)),
        ("identity", ("contract_start_date", "contract_end_date")),
        ("backtest_config", ("start_date", "end_date")),
    ):
        section = value.get(section_name)
        if not isinstance(section, Mapping):
            continue
        normalized = dict(section)
        for field in fields:
            if field in normalized and normalized[field] not in {None, ""}:
                normalized[field] = _canonical_app_date(normalized[field], f"{section_name}.{field}")
        if section_name == "backtest_config" and "entry_dates" in normalized:
            entry_dates = normalized["entry_dates"]
            if entry_dates is None:
                pass
            elif not isinstance(entry_dates, (list, tuple)):
                raise ValidationError("backtest_config.entry_dates必须为日期数组")
            else:
                normalized["entry_dates"] = [
                    _canonical_app_date(item, "backtest_config.entry_dates") for item in entry_dates
                ]
        value[section_name] = normalized
    return value


def _canonical_app_date(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label}必须为YYYY-MM-DD")
    candidate = value.strip()
    if re.fullmatch(r"\d{4}/\d{2}/\d{2}", candidate):
        candidate = candidate.replace("/", "-")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        raise ValidationError(f"{label}必须为YYYY-MM-DD")
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError as error:
        raise ValidationError(f"{label}必须为有效日期") from error


def _controlled_datafetcher_request(payload: Mapping[str, Any], context: Any) -> dict[str, Any]:
    """Separate signed Host scope from the strict DataFetcher business request."""

    action = str(payload.get("action", "fetch")).strip().lower()
    wrappers = {field for field in ("request", "data_request") if field in payload}
    if len(wrappers) > 1:
        raise ValidationError("DataFetcher请求只能使用一个DataRequest入口")
    allowed = set(_HOST_SCOPE_FIELDS) | {"action"}
    if wrappers:
        allowed.update(wrappers)
    if wrappers or action in {"status", "catalog", "list_assets"}:
        unexpected = set(payload) - allowed
        if unexpected:
            raise ValidationError(f"DataFetcher请求包含不允许的字段：{', '.join(sorted(unexpected))}")
    if action in {"fetch", "fetch_calendar"} and (
        context is None or not isinstance(context.task_id, str) or not context.task_id
    ):
        raise ValidationError("DataFetcher fetch必须绑定当前App任务")
    value = _business_payload(payload)
    if context is not None and context.task_id is not None:
        value["task_id"] = context.task_id
    return value


def _run_fair_parameter_preflight(
    tool_entry: Any,
    tool_name: str,
    payload: Mapping[str, Any],
) -> None:
    """Run the verified Core preflight before any App data resolution."""
    if tool_name != "pricer" or payload.get("pricing_objective") is None:
        return
    preflight = getattr(tool_entry, "preflight_fair_parameter_request", None)
    if not callable(preflight):
        raise ValidationError("Capability缺少公平参数运行前预检")
    try:
        preflight(tool_name, payload)
    except (ContractResolutionError, TypeError, ValueError) as error:
        raise ValidationError(str(error)) from error


def _public_contract_resolution_error(error: ContractResolutionError) -> UserActionError | ValidationError:
    """Expose the current-input compiler's reviewed validation message."""

    return ValidationError(str(error))


def _bind_compute_result(
    result: Mapping[str, Any],
    context: Any,
    *,
    resolved_contract_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach the already verified Host binding before immutable run storage."""

    bound = {
        "analysis_case_id": getattr(context, "analysis_case_id", None),
        "candidate_id": getattr(context, "candidate_id", None),
        "catalog_version": getattr(context, "catalog_version", None),
        "product_id": getattr(context, "product_id", None),
    }
    if any(not isinstance(value, str) or not value for value in bound.values()):
        raise ValidationError("计算结果缺少Host绑定的候选合同标识")
    value = dict(result)
    for field, expected in bound.items():
        supplied = value.get(field)
        if supplied is not None and supplied != expected:
            raise ValidationError(f"Capability计算结果{field}与Host绑定冲突")
        value[field] = expected
    rule_revision = getattr(context, "rule_revision", None)
    if not isinstance(rule_revision, int) or isinstance(rule_revision, bool) or rule_revision <= 0:
        raise ValidationError("计算结果缺少Host绑定的rule_revision")
    supplied_revision = value.get("rule_revision")
    if supplied_revision is not None and supplied_revision != rule_revision:
        raise ValidationError("Capability计算结果rule_revision与Host绑定冲突")
    value["rule_revision"] = rule_revision
    value["resolved_contract"] = dict(resolved_contract_snapshot)
    return value


def _prepare_compute_with_backtest_window(
    tool_entry: Any,
    module: str,
    payload: Mapping[str, Any],
    *,
    data_refs: tuple[Mapping[str, Any], ...],
    data_store: object | None,
) -> tuple[Mapping[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Compile once for contract facts, then freeze Backtester's actual window.

    Window acceptance is deliberately deferred until the Host has read the
    authenticated market-history bytes and verified calendar. The final
    request is recompiled through the same Core entry so no provisional date
    or module-private object enters the immutable contract snapshot.
    """

    compilation_payload = dict(payload)
    if module == "backtester":
        # This temporary contract supplies product/tenor facts to the window
        # planner. Starting it at a late requested entry can fail schedule
        # compilation before the planner can explain missing full-tenor data.
        # Use verified history for this temporary anchor, then recompile the
        # actual first entry below. The requested window remains untouched.
        provisional_identity = dict(payload.get("identity") or {})
        for key in ("contract_start_date", "contract_end_date", "reference_prices"):
            provisional_identity.pop(key, None)
        compilation_payload["identity"] = provisional_identity
        compilation_payload["backtest_config"] = {
            **dict(payload.get("backtest_config") or {}),
            "start_date": None, "end_date": None,
            "entry_rule": "daily", "entry_dates": None,
        }
    prepared = tool_entry.prepare_compute_request(
        module,
        compilation_payload,
        data_refs=data_refs,
        data_store=data_store,
    )
    value = dict(payload)
    if module != "backtester":
        return prepared, value, None
    value, window = _plan_backtest_window(
        value,
        prepared,
        data_refs=data_refs,
        data_store=data_store,
    )
    final = tool_entry.prepare_compute_request(
        module,
        value,
        data_refs=data_refs,
        data_store=data_store,
    )
    return final, value, window


def _plan_backtest_window(
    payload: Mapping[str, Any],
    prepared: Mapping[str, Any],
    *,
    data_refs: tuple[Mapping[str, Any], ...],
    data_store: object | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Freeze one full-tenor entry window from verified price/calendar facts.

    Backtester's ``resolve_effective_backtest_window`` remains the sole owner
    of T/n_obs, observation and maturity rules. This Host layer only supplies
    authenticated bytes and persists its returned effective bounds.
    """

    formal = prepared.get("request")
    if not isinstance(formal, Mapping) or not isinstance(formal.get("contract"), Mapping):
        raise ValidationError("Backtester窗口规划缺少正式ResolvedContract")
    raw_history = formal.get("historical_data")
    if not isinstance(raw_history, Mapping):
        raise ValidationError("Backtester窗口规划缺少正式历史DataAssetRef")
    calendar_ref = next(
        (dict(item) for item in data_refs if isinstance(item, Mapping) and item.get("schema_id") == "trading-calendar"),
        None,
    )
    if calendar_ref is None:
        raise ValidationError("Backtester窗口规划必须绑定已验证交易日历DataAssetRef")
    if data_store is None or not callable(getattr(data_store, "read_bytes", None)):
        raise ValidationError("Backtester窗口规划必须使用Host受控DataStore")
    entry_module = importlib.import_module("modules.backtester.entry_generator")
    try:
        history_ref = DataAssetRef(**dict(raw_history))
        raw_bytes = data_store.read_bytes(history_ref, tenant_id=history_ref.tenant_id)
        if not isinstance(raw_bytes, bytes) or hashlib.sha256(raw_bytes).hexdigest() != history_ref.content_hash:
            raise ValidationError("Backtester历史行情字节与DataAssetRef不一致")
        pandas = importlib.import_module("pandas")
        backtester = importlib.import_module("modules.backtester")
        contract = ResolvedContract(**dict(formal["contract"]))
        history = backtester.HistoricalData.from_frame(
            pandas.read_csv(BytesIO(raw_bytes)),
            data_asset_ref=dict(raw_history),
        )
        raw_config = payload.get("backtest_config")
        config_value = dict(raw_config) if isinstance(raw_config, Mapping) else {}
        try:
            config = backtester.BacktestConfig.from_mapping(config_value)
        except backtester.BacktestConfigError:
            # A user may edit the start before updating the end. Diagnose
            # reversed bounds against the same verified history; do not turn
            # this input error into a misleading historical-data failure.
            start = config_value.get("start_date")
            end = config_value.get("end_date")
            if not (isinstance(start, str) and isinstance(end, str) and start > end):
                raise
            if date.fromisoformat(start).isoformat() != start or date.fromisoformat(end).isoformat() != end:
                raise
            diagnostic_config = backtester.BacktestConfig.from_mapping({
                **config_value, "start_date": None, "end_date": None,
            })
            available = backtester.plan_backtest_window(contract, diagnostic_config, history)
            latest = available.latest_complete_entry_session
            start_after_latest = start > latest
            raise entry_module.BacktestWindowError(
                "入场起始日晚于最新完整入场日，请调整请求区间。" if start_after_latest
                else "入场起始日不得晚于入场截止日。",
                code="no_complete_entry_session" if start_after_latest else "invalid_entry_window",
                details={
                    "requested_start": start, "requested_end": end,
                    "market_as_of_session": available.market_as_of_session,
                    "latest_complete_entry_session": latest,
                    "reason": "requested_window_starts_after_latest_complete_entry_session"
                    if start_after_latest else "requested_start_after_end",
                },
            )
        effective = backtester.plan_backtest_window(
            contract,
            config,
            history,
        )
    except entry_module.BacktestWindowError as error:
        details = {
            str(key): str(value)
            for key, value in error.details.items()
            if value is not None and isinstance(value, (str, int, float))
        }
        latest_complete = details.get("latest_complete_entry_session")
        requested_start = details.get("requested_start")
        market_as_of = details.get("market_as_of_session")
        message = str(error)
        if latest_complete:
            details["suggested_end_date"] = latest_complete
        if latest_complete and requested_start and requested_start > latest_complete:
            details["suggested_start_date"] = latest_complete
            next_step = (
                f"请将入场起始日和截止日调整到{latest_complete}或更早，且起始日不晚于截止日；"
                f"可采用{latest_complete}至{latest_complete}。"
            )
        elif latest_complete:
            next_step = f"请将入场截止日改为{latest_complete}或更早的实际交易日，且不早于起始日。"
        else:
            next_step = "请获取更长的实际历史行情与对应交易日历后重试。"
        if market_as_of:
            message += f"实际行情截止日为{market_as_of}。"
        # The existing invalid-preview surface renders message directly.
        # Keep the actionable start/end advice there as well as in next_step.
        message += next_step
        raise UserActionError(
            str(error.code),
            message,
            stage="data",
            next_step=next_step,
            details=details,
        ) from error
    except (OSError, PermissionError, TypeError, ValueError) as error:
        if isinstance(error, ValidationError):
            raise
        raise UserActionError(
            "backtest_history_invalid",
            "已取得的历史行情或交易日历无法形成受控回测窗口。",
            stage="data",
            next_step="请重新获取覆盖全部标的、合同观察字段和完整期限的历史行情与交易日历。",
        ) from error
    config = dict(raw_config) if isinstance(raw_config, Mapping) else {}
    effective_window = effective.to_dict()
    start_text = str(effective_window["start_date"])
    end_text = str(effective_window["end_date"])
    # Preserve explicit bounds so the Worker can reproduce and disclose the
    # same incomplete-entry exclusions seen during preview. Filling only an
    # automatic window must not turn an explicit request into a clipped one.
    if not config.get("start_date") and config.get("entry_rule") != "explicit":
        config["start_date"] = start_text
    if not config.get("end_date") and config.get("entry_rule") != "explicit":
        config["end_date"] = end_text

    value = dict(payload)
    value["backtest_config"] = config
    identity = value.get("identity")
    identity = dict(identity) if isinstance(identity, Mapping) else {}
    # Historical acquisition deliberately starts before the requested entry
    # window, while every current-input snapshot binds to its first effective
    # entry for both automatic and user-confirmed explicit windows.
    identity["contract_start_date"] = start_text
    value["identity"] = identity
    calendar_coverage = calendar_ref.get("coverage")
    calendar_coverage = calendar_coverage if isinstance(calendar_coverage, Mapping) else {}
    window = {
        **effective_window,
        "calendar_ref": {
            "data_asset_id": str(calendar_ref.get("data_asset_id", "")),
            "schema_id": "trading-calendar",
            "content_hash": str(calendar_ref.get("content_hash", "")),
            "calendar_id": str(calendar_coverage.get("calendar_id", history.calendar_id)),
            "calendar_revision": str(calendar_coverage.get("calendar_revision", history.calendar_revision)),
        },
    }
    if any(not value for value in window["calendar_ref"].values()):
        raise ValidationError("Backtester公开窗口缺少可验证交易日历引用")
    return value, window


def _stable_semantic_value(value: Any) -> Any:
    """把预检输入规范化为与键顺序无关的可哈希值。"""
    if isinstance(value, Mapping):
        return tuple(
            (str(key), _stable_semantic_value(item))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        )
    if isinstance(value, (list, tuple)):
        return tuple(_stable_semantic_value(item) for item in value)
    return value


def _backtest_tenor_basis(
    payload: Mapping[str, Any],
    existing_contract: Mapping[str, Any] | None,
    *,
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """公开T与n_obs的独立角色，不把观察日数改写成合同年限。"""
    terms: dict[str, Any] = {}
    existing_resolved = existing_contract.get("resolved_contract") if isinstance(existing_contract, Mapping) else None
    source = contract if isinstance(contract, Mapping) else existing_resolved
    if isinstance(source, Mapping) and isinstance(source.get("terms"), Mapping):
        terms.update(source["terms"])
    overrides = payload.get("term_overrides")
    if isinstance(overrides, Mapping):
        terms.update(overrides)
    return {
        "contract_tenor": {
            "key": "T",
            "basis": "ACT/365",
            "years": terms.get("T"),
        },
        "observation_endpoint": {
            "key": "n_obs" if "n_obs" in terms else None,
            "basis": "observation_session_count" if "n_obs" in terms else "civil_maturity",
            "count": terms.get("n_obs"),
        },
    }


def _scope_text(context: Any, field: str) -> str | None:
    value = getattr(context, field, None)
    return value if isinstance(value, str) and value else None


def _module_run_ref_payload(reference: ModuleRunRef) -> dict[str, str]:
    return {
        "module": reference.module,
        "tenant_id": reference.tenant_id,
        "task_id": reference.task_id,
        "run_id": reference.run_id,
        "expected_result_file_hash": reference.expected_result_file_hash,
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
    return compile_compute_data_requirements(
        "pricer", payload, resolved_contract if isinstance(resolved_contract, Mapping) else None,
    ).future_calendar_required


def _empty_data_reference(value: object) -> bool:
    return value is None or value == ""


def _bind_current_input_to_history(
    module: str,
    payload: Mapping[str, Any],
    *,
    data_refs: tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    """Bind the current input to Host-owned market and calendar evidence."""
    value = dict(payload)
    if module not in {"payoffer", "pricer", "backtester"}:
        return value
    if module == "payoffer":
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
            stage="data",
            next_step="请重新获取附带已验证交易日历的日频行情后重试。",
        )
    # Historical price provenance and a future observation schedule have
    # different roles.  A path Pricer may correctly bind old close history and
    # a newer calendar that reaches the contract's maturity.  Backtester, by
    # contrast, replays that history and must retain its exact calendar.
    calendar_coverage = coverage
    if module == "pricer":
        future_calendar = next(
            (reference for reference in data_refs if reference.get("schema_id") == "trading-calendar"),
            None,
        )
        candidate_coverage = future_calendar.get("coverage") if isinstance(future_calendar, Mapping) else None
        if isinstance(candidate_coverage, Mapping):
            calendar_coverage = candidate_coverage
    calendar_id = calendar_coverage.get("calendar_id")
    calendar_revision = calendar_coverage.get("calendar_revision")
    if not isinstance(calendar_id, str) or not calendar_id or not isinstance(calendar_revision, str) or not calendar_revision:
        raise UserActionError(
            "market_history_calendar_missing",
            "自动获取的行情缺少交易日历信息，无法建立本次研究合同。请重新获取数据后重试。",
            stage="data",
            next_step="请重新获取附带已验证交易日历的日频行情后重试。",
        )
    raw_identity = value.get("identity", {})
    if not isinstance(raw_identity, Mapping):
        raise ValidationError("identity必须为对象")
    identity = dict(raw_identity)
    if module == "pricer":
        # The Host-selected future calendar is the frozen path-simulation
        # evidence. Browser input may identify a stale task calendar, but it
        # must never override the verified DataAssetRef selected above.
        identity["calendar_id"] = calendar_id
        identity["calendar_revision"] = calendar_revision
    else:
        identity.setdefault("calendar_id", calendar_id)
        identity.setdefault("calendar_revision", calendar_revision)
    if module == "backtester":
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
    candidate = first_backtest_entry(config, coverage)
    if candidate not in {None, ""}:
        try:
            return date.fromisoformat(str(candidate)).isoformat()
        except ValueError:
            pass
    raise UserActionError(
        "backtest_start_date_missing",
        "回测需要有效的入场起始日。请填写入场日期后重试。",
    )


def _history_fetch_request(
    module: str,
    payload: Mapping[str, Any],
    resolved_contract: object,
    underlyings: tuple[str, ...],
) -> dict[str, Any]:
    requirements = compile_compute_data_requirements(
        module, payload, resolved_contract if isinstance(resolved_contract, Mapping) else None,
    )
    if module == "payoffer":
        end = _shanghai_now().date()
        start = _years_before(end, 3)
    elif module == "pricer":
        config = payload.get("pricing_config")
        raw_end = config.get("valuation_date") if isinstance(config, Mapping) else None
        end = _iso_date_or_default(raw_end, _shanghai_now().date(), "valuation_date")
        start = _years_before(end, 3)
    elif module == "backtester":
        config = payload.get("backtest_config")
        config = config if isinstance(config, Mapping) else {}
        tenor_days = max(0, round(requirements.tenor_years * 365.0))
        latest = _shanghai_now().date()
        entries = config.get("entry_dates")
        explicit_entries = (
            tuple(_iso_date_or_default(item, latest, "backtest_config.entry_dates") for item in entries)
            if isinstance(entries, (list, tuple)) else ()
        )
        raw_start = config.get("start_date")
        if raw_start not in {None, ""}:
            entry_start = _iso_date_or_default(raw_start, latest, "backtest_config.start_date")
        elif explicit_entries:
            entry_start = min(explicit_entries)
        elif config.get("end_date") not in {None, ""}:
            entry_start = _years_before(
                _iso_date_or_default(config.get("end_date"), latest, "backtest_config.end_date"),
                3,
            )
        else:
            # This is only a bounded acquisition horizon. Acceptance is based
            # later on actual common price sessions and Backtester's own full-
            # tenor locator, never on this calendar-day estimate.
            entry_start = _years_before(latest - timedelta(days=tenor_days), 3)
        requested_ceiling: list[date] = list(explicit_entries)
        raw_end = config.get("end_date")
        if raw_end not in {None, ""}:
            requested_ceiling.append(
                _iso_date_or_default(raw_end, latest, "backtest_config.end_date")
            )
        # Explicit windows only need history through their final contract
        # maturity. Automatic windows still acquire through today so the
        # Backtester can locate the latest complete entry from actual bars.
        # This civil-date bound never asserts that the endpoint is a session.
        if requested_ceiling and requirements.observation_count is None:
            end = min(max(requested_ceiling) + timedelta(days=tenor_days), latest)
        else:
            # n_obs is a count of verified exchange sessions, not calendar
            # days. Acquire through the latest available boundary and let
            # Backtester locate the nth session from the returned calendar.
            end = latest
        # The preliminary contract must span its tenor before the planner
        # can diagnose late entries. A short recent snapshot cannot provide
        # that contract even when it covers the requested entry start.
        # This bounds acquisition only; complete entries are still located
        # from verified sessions, and explicit user dates remain unchanged.
        planning_start = (
            min(entry_start, end - timedelta(days=tenor_days))
            if requirements.observation_count is None else entry_start
        )
        # Also cover a prior observed close for weekend/holiday starts.
        start = planning_start - timedelta(days=14)
    else:
        raise ValidationError("只有正式计算模块可以自动准备行情")
    if start > end:
        raise ValidationError("自动行情开始日期不得晚于结束日期")
    return {
        "action": "fetch",
        "asset_ids": list(underlyings),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "fields": list(requirements.historical_fields),
        "provider": "ifind_http",
        "frequency": "1d",
        "adjustment": "auto",
        "cache_policy": "extend_only",
        "offline": False,
        "persistence_mode": "volatile",
    }


def _pricing_history_sessions(
    request: Mapping[str, Any],
    calendar_reference: Mapping[str, Any] | None,
    *,
    as_of: date,
) -> tuple[str, ...]:
    """Resolve actual historical sessions without weekday or cache inference."""

    coverage = calendar_reference.get("coverage") if isinstance(calendar_reference, Mapping) else None
    if not isinstance(coverage, Mapping):
        return ()
    if (
        not str(coverage.get("calendar_id", "")).startswith("CN-")
        or str(coverage.get("calendar_revision", "")).casefold()
        in {"", "unverified", "unknown", "derived", "frame-sessions"}
        or not isinstance(coverage.get("sessions"), (list, tuple))
    ):
        return ()
    try:
        start, end = date.fromisoformat(request["start_date"]), date.fromisoformat(request["end_date"])
        if date.fromisoformat(coverage["start_date"]) > start or date.fromisoformat(coverage["end_date"]) < end:
            return ()
        sessions = sorted({date.fromisoformat(str(day)) for day in coverage["sessions"]})
    except (KeyError, TypeError, ValueError):
        return ()
    return tuple(day.isoformat() for day in sessions if start <= day <= min(end, as_of))


def _backtest_history_request_for_calendar(
    request: Mapping[str, Any],
    calendar_reference: Mapping[str, Any] | None,
    *,
    as_of: date,
) -> dict[str, Any] | None:
    """Translate the civil horizon using controlled, current-enough sessions."""

    if not isinstance(calendar_reference, Mapping):
        return None
    coverage = calendar_reference.get("coverage")
    if not isinstance(coverage, Mapping):
        return None
    if (
        not str(coverage.get("calendar_id", "")).startswith("CN-")
        or str(coverage.get("calendar_revision", "")).casefold()
        in {"", "unverified", "unknown", "derived", "frame-sessions"}
        or not isinstance(coverage.get("sessions"), (list, tuple))
    ):
        return None
    try:
        start = date.fromisoformat(str(request["start_date"]))
        end = date.fromisoformat(str(request["end_date"]))
        calendar_start = date.fromisoformat(str(coverage["start_date"]))
        calendar_end = date.fromisoformat(str(coverage["end_date"]))
        sessions = sorted({date.fromisoformat(str(value)) for value in coverage["sessions"]})
    except (KeyError, TypeError, ValueError):
        return None
    if calendar_start > start or calendar_end < end:
        return None
    sessions = [session for session in sessions if start <= session <= min(as_of, calendar_end)]
    if not sessions:
        return None
    following = next((session for session in sessions if session >= end), None)
    if following is None and calendar_end < as_of:
        # No later session in an old calendar is not proof of a market cutoff.
        return None
    return {
        **request,
        "start_date": sessions[0].isoformat(),
        "end_date": (following or sessions[-1]).isoformat(),
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


def _raise_datafetch_failure(
    result: Mapping[str, Any],
    *,
    default_code: str,
    default_message: str,
    default_next_step: str,
) -> None:
    failure = _datafetcher_failure_fields(result)
    if failure is None:
        code = default_code
        message = default_message
        next_step = default_next_step
    else:
        code = str(failure["failure_code"])
        message = str(failure["message"])
        next_step = str(failure["next_step"])
    raise UserActionError(
        code,
        message,
        stage="data",
        next_step=next_step,
    )


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
    *,
    market_history_ref: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if "backtest_config" in payload:
        history_request = _history_fetch_request("backtester", payload, resolved_contract, underlyings)
        return {
            "action": "fetch_calendar",
            "asset_ids": list(underlyings),
            # This is the nominal horizon. Backtest acquisition separately
            # resolves its closing session before selecting or fetching bars.
            "start_date": str(history_request["start_date"]),
            "end_date": str(history_request["end_date"]),
        }
    config = payload.get("pricing_config")
    raw_valuation = config.get("valuation_date") if isinstance(config, Mapping) else None
    try:
        valuation = date.fromisoformat(str(raw_valuation or _shanghai_now().date().isoformat()))
    except ValueError as error:
        raise ValidationError("valuation_date必须为YYYY-MM-DD") from error
    identity = resolved_contract.get("identity", {}) if isinstance(resolved_contract, Mapping) else payload.get("identity", {})
    requirements = compile_compute_data_requirements(
        "pricer", payload, resolved_contract if isinstance(resolved_contract, Mapping) else None,
    )
    raw_start = identity.get("contract_start_date") if isinstance(identity, Mapping) else None
    observation_start = _iso_date_or_default(raw_start, valuation, "contract_start_date")
    raw_end = identity.get("contract_end_date") if isinstance(identity, Mapping) else None
    if raw_end:
        try:
            maturity = date.fromisoformat(str(raw_end))
        except ValueError as error:
            raise ValidationError("contract_end_date必须为YYYY-MM-DD") from error
    else:
        maturity = observation_start + timedelta(days=round(requirements.tenor_years * 365.0))
    # Midlife monitors need the entire historical contract window, including
    # daily KI/reset observations before today's simulation calendar.
    calendar_start = min(valuation, observation_start)
    if requirements.observation_count is not None:
        # This is an acquisition bound only. Acceptance locates the nth
        # selected session from returned evidence, never from this estimate.
        maturity = max(maturity, observation_start + timedelta(days=requirements.calendar_lookahead_days))
    history_coverage = market_history_ref.get("coverage") if isinstance(market_history_ref, Mapping) else None
    history_sessions = history_coverage.get("sessions") if isinstance(history_coverage, Mapping) else None
    if isinstance(history_sessions, (list, tuple)) and history_sessions:
        try:
            market_as_of = max(date.fromisoformat(str(value)) for value in history_sessions)
        except ValueError as error:
            raise ValidationError("market-history coverage.sessions必须为YYYY-MM-DD") from error
        # A path calculation consumes two different pieces of evidence: the
        # latest observed close and the future simulation calendar.  The
        # frozen calendar must close that boundary by containing the actual
        # market-as-of session as well as every future contract session.
        calendar_start = min(calendar_start, market_as_of)
    return {
        "action": "fetch_calendar",
        "asset_ids": list(underlyings),
        "start_date": calendar_start.isoformat(),
        "end_date": maturity.isoformat(),
    }


def _calendar_ref_covers_request(
    reference: Mapping[str, Any], request: Mapping[str, Any], *, payload: Mapping[str, Any] | None = None,
) -> bool:
    coverage = reference.get("coverage")
    if payload is not None and "backtest_config" not in payload:
        requirements = compile_compute_data_requirements("pricer", payload)
        if requirements.observation_count is not None:
            sessions = coverage.get("sessions") if isinstance(coverage, Mapping) else None
            if not isinstance(sessions, (list, tuple)):
                return False
            identity = payload.get("identity", {})
            start = identity.get("contract_start_date") if isinstance(identity, Mapping) else None
            try:
                endpoint = requirements.observation_end_date(sessions, str(start or request["start_date"]))
            except (ValueError, TypeError):
                return False
            if endpoint is None:
                return False
            request = {**request, "end_date": endpoint}
    if not isinstance(coverage, Mapping):
        # Capability validation will reject an incomplete real reference. Keep
        # this Host selection helper compatible with narrow protocol doubles.
        return True
    start = coverage.get("start_date", coverage.get("start"))
    end = coverage.get("end_date", coverage.get("end"))
    if start is None and end is None:
        return True
    try:
        available_start = date.fromisoformat(str(start))
        available_end = date.fromisoformat(str(end))
        required_start = date.fromisoformat(str(request["start_date"]))
        required_end = date.fromisoformat(str(request["end_date"]))
    except (KeyError, TypeError, ValueError):
        return False
    return available_start <= required_start and available_end >= required_end


def _calendar_ref_supports_automatic_backtest_cutoff(
    reference: Mapping[str, Any],
    request: Mapping[str, Any],
    market_history_reference: Mapping[str, Any] | None,
) -> bool:
    """Accept a shorter civil bound only with current calendar proof."""

    planned = _backtest_history_request_for_calendar(request, reference, as_of=_shanghai_now().date())
    history_coverage = (
        market_history_reference.get("coverage")
        if isinstance(market_history_reference, Mapping)
        else None
    )
    if planned is None or not isinstance(history_coverage, Mapping):
        return False
    try:
        required_start = date.fromisoformat(str(planned["start_date"]))
        required_end = date.fromisoformat(str(planned["end_date"]))
        history_start = date.fromisoformat(str(history_coverage["start_date"]))
        history_end = date.fromisoformat(str(history_coverage["end_date"]))
    except (KeyError, TypeError, ValueError):
        return False
    return (
        history_start <= required_start
        and history_end >= required_end
        and _calendar_ref_closes_market_history(reference, market_history_reference)
    )


def _calendar_ref_closes_market_history(
    calendar_reference: Mapping[str, Any],
    market_history_reference: Mapping[str, Any] | None,
    *,
    valuation_date: str | None = None,
    contract_start_date: str | None = None,
) -> bool:
    """Verify the path calendar preserves the valuation boundary and history.

    Pricer deliberately keeps historical prices and its future observation
    calendar as separate immutable assets. Their revisions need not match, but
    the calendar must include the last price observed by the valuation date
    and every already-observed session during this contract's life.
    """

    if not isinstance(market_history_reference, Mapping):
        return True
    history_coverage = market_history_reference.get("coverage")
    calendar_coverage = calendar_reference.get("coverage")
    if not isinstance(history_coverage, Mapping) or not isinstance(calendar_coverage, Mapping):
        return True
    history_sessions = history_coverage.get("sessions")
    calendar_sessions = calendar_coverage.get("sessions")
    history_is_formal = isinstance(market_history_reference.get("data_asset_id"), str) \
        and bool(market_history_reference.get("data_asset_id"))
    calendar_is_formal = isinstance(calendar_reference.get("data_asset_id"), str) \
        and bool(calendar_reference.get("data_asset_id"))
    if not isinstance(history_sessions, (list, tuple)) or not history_sessions:
        return not history_is_formal
    if not isinstance(calendar_sessions, (list, tuple)) or not calendar_sessions:
        # Narrow protocol doubles may only declare an interval. Formal App
        # DataAssetRefs must prove the boundary with their actual sessions.
        return not calendar_is_formal
    try:
        observed_sessions = {date.fromisoformat(str(value)) for value in history_sessions}
        if valuation_date is not None:
            valuation = date.fromisoformat(valuation_date)
            observed_sessions = {value for value in observed_sessions if value <= valuation}
        if not observed_sessions:
            return False
        required_sessions = {max(observed_sessions)}
        if contract_start_date is not None:
            start = date.fromisoformat(contract_start_date)
            required_sessions.update(value for value in observed_sessions if value >= start)
        available_sessions = {date.fromisoformat(str(value)) for value in calendar_sessions}
    except (TypeError, ValueError):
        return False
    return required_sessions.issubset(available_sessions)


def _history_ref_covers_request(
    reference: Mapping[str, Any], request: Mapping[str, Any], *, required_sessions: tuple[str, ...] = (),
) -> bool:
    """Check actual observed coverage, optionally against verified sessions.

    Request dates describe acquisition intent only. Civil boundaries must be
    resolved from a calendar before accepting a shorter observed interval.
    """

    coverage = reference.get("coverage")
    if not isinstance(coverage, Mapping):
        return False
    normalized_fields = reference.get("normalized_fields")
    required_fields = request.get("fields")
    if (
        not isinstance(normalized_fields, (list, tuple))
        or not isinstance(required_fields, (list, tuple))
        or not set(map(str, required_fields)).issubset(set(map(str, normalized_fields)))
    ):
        return False
    convention = reference.get("price_convention")
    if not isinstance(convention, Mapping):
        return False
    requested_frequency = request.get("frequency")
    requested_adjustment = request.get("adjustment")
    if requested_frequency not in {None, ""} and convention.get("frequency") != requested_frequency:
        return False
    if requested_adjustment not in {None, ""} and convention.get("requested_adjustment") != requested_adjustment:
        return False
    bounds = market_history_bounds(reference)
    if bounds is None:
        return False
    available_start, available_end = bounds
    try:
        required_start = date.fromisoformat(str(request["start_date"]))
        required_end = date.fromisoformat(str(request["end_date"]))
    except (KeyError, TypeError, ValueError):
        return False
    if required_sessions:
        observed = coverage.get("sessions")
        if not isinstance(observed, (list, tuple)) or not set(required_sessions).issubset(set(observed)):
            return False
    return available_start <= required_start and available_end >= required_end


def _history_ref_supports_automatic_backtest_cutoff(
    reference: Mapping[str, Any],
    request: Mapping[str, Any],
) -> bool:
    """Require actual bars through the calendar-resolved acquisition endpoint.

    A cache tail is not a market cutoff. Callers first resolve the endpoint
    against a calendar that closes maturity or proves the current last session.
    """

    coverage = reference.get("coverage")
    if (
        not isinstance(coverage, Mapping)
        or not _has_verified_backtest_calendar(reference)
        or not _history_ref_covers_request(reference, request)
    ):
        return False
    try:
        available_end = date.fromisoformat(str(coverage["end_date"]))
        required_end = date.fromisoformat(str(request["end_date"]))
        by_asset = coverage.get("by_asset")
        if isinstance(by_asset, Mapping):
            available_end = min(
                available_end,
                *(date.fromisoformat(str(by_asset[asset]["end_date"])) for asset in request["asset_ids"]),
            )
    except (KeyError, TypeError, ValueError):
        return False
    return available_end >= required_end


def _calendar_reference_bound_to_history(history_ref: Mapping[str, Any]) -> dict[str, str] | None:
    """Return the calendar identity already committed into a history asset.

    Backtester must use the exact calendar that signed its historical price
    reference.  Picking the newest global calendar can make a legitimate
    history asset fail formal validation merely because another task later
    fetched a newer revision.
    """

    coverage = history_ref.get("coverage")
    if not isinstance(coverage, Mapping):
        return None
    calendar_ref = coverage.get("calendar_ref")
    if not isinstance(calendar_ref, Mapping):
        return None
    return _data_asset_selector(calendar_ref)


def _data_asset_selector(reference: Mapping[str, Any]) -> dict[str, str] | None:
    """Reduce an internal DataAsset record to the browser-safe selector."""

    data_asset_id = reference.get("data_asset_id")
    content_hash = reference.get("content_hash")
    if not isinstance(data_asset_id, str) or not data_asset_id:
        return None
    value = {"data_asset_id": data_asset_id}
    if isinstance(content_hash, str) and content_hash:
        value["content_hash"] = content_hash
    return value


def _require_conversation_tool_scope(identity: SessionIdentity, tool_name: str, payload: Mapping[str, Any]) -> None:
    """Authorize documented OptChat actions independently of Desk access.

    Business input validation belongs to the same module used by Desk. Host
    signing, owned references and path rejection remain mandatory below.
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


def _require_conversation_data_request(action: str, payload: Mapping[str, Any]) -> None:
    # Do not impose an OptChat-only offline, date-range or asset-count limit.
    # The DataFetcher protocol validates values and provider availability.
    if action == "status":
        _require_scope_fields(payload, {"action", "task_id"})
        return
    fields = {
        "asset_id", "asset_ids", "start_date", "end_date", "fields", "provider",
        "frequency", "adjustment", "source_priority", "cache_policy", "offline",
        "quota_limit", "persistence_mode",
    }
    wrappers = {name for name in ("request", "data_request") if name in payload}
    if wrappers:
        if len(wrappers) != 1:
            raise ValidationError("DataFetcher请求只能使用一个DataRequest入口")
        _require_scope_fields(payload, {"action", "task_id"} | wrappers)
        value = payload[next(iter(wrappers))]
        if not isinstance(value, Mapping):
            raise ValidationError("DataRequest必须为对象")
        _require_scope_fields(value, fields)
    else:
        _require_scope_fields(payload, {"action", "task_id"} | fields)


def _require_conversation_compute_request(tool_name: str, payload: Mapping[str, Any]) -> None:
    allowed = {"action", "task_id", "product_id", "identity", "term_overrides"}
    if tool_name == "pricer":
        allowed.update({"pricing_config", "market_data_refs", "trading_calendar_ref", "pricing_objective"})
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


def _report_request_capability(payload: Mapping[str, Any]) -> str:
    """Map a requested delivery to its policy capability, without role names."""

    kind = str(payload.get("kind") or payload.get("output_type") or "").strip().lower()
    return {
        "card": "report.card.request",
        "quote": "report.quote.request",
        "report": "report.full.request",
    }.get(kind, "report.full.request")


def _require_conversation_report_request(identity: SessionIdentity, payload: Mapping[str, Any]) -> None:
    _require_scope_fields(payload, {"action", "task_id", "kind", "selection"})
    kind = str(payload.get("kind", "")).strip().lower()
    selection = payload.get("selection")
    if kind not in {"card", "quote", "report"} or not isinstance(selection, Mapping) or selection.get("output_type") != kind:
        raise AuthorizationError("conversation.tool.run", "report output type is invalid")
    if kind == "quote":
        _require_scope_fields(selection, {
            "source_id", "quote_items", "output_type", "format", "audience", "report_run_id", "metadata",
        })
    else:
        _require_scope_fields(selection, {
            "source_id", "candidate_ids", "selected_modules", "module_run_refs", "delivery_mode", "output_type", "format", "audience", "report_run_id", "metadata",
        })


def _require_scope_fields(value: Mapping[str, Any], allowed: set[str]) -> None:
    if set(value).difference(allowed):
        raise AuthorizationError("conversation.tool.run", "request contains fields outside the conversation scope")
