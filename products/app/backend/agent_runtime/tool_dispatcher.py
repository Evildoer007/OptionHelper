"""App task runtime that persists real Capability module runs."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Iterator, Mapping

from ..errors import AuthorizationError, UnavailableCapabilityError, UserActionError, ValidationError
from ..identity.session_identity import SessionIdentity
from ..stores.data_store import DataStore
from ..stores.result_store import ResultStore
from ..task_runtime.job_runner import JobRunner
from ..task_runtime.idempotency_store import ToolIdempotencyStore
from ..task_runtime.task_service import TaskService
from ..tool_gateway import ToolGateway
from .durability_checkpoint import DurabilityCheckpointStore, stable_operation_id


@dataclass
class _LockEntry:
    lock: Lock = field(default_factory=Lock)
    users: int = 0


class _CommittedRunPendingError(RuntimeError):
    pass


class ToolDispatcher:
    def __init__(
        self,
        gateway: ToolGateway,
        jobs: JobRunner,
        tasks: TaskService,
        results: ResultStore,
        data_assets: DataStore,
        idempotency: ToolIdempotencyStore | None = None,
    ) -> None:
        self._gateway = gateway
        self._jobs = jobs
        self._tasks = tasks
        self._results = results
        self._data_assets = data_assets
        self._idempotency = idempotency or ToolIdempotencyStore(tasks._state._root / "tool-idempotency.sqlite3")
        self._dispatch_checkpoints = DurabilityCheckpointStore(tasks.session_event_log)
        self._idempotency_locks: dict[tuple[str, str, str, str], _LockEntry] = {}
        self._idempotency_guard = Lock()

    def dispatch(
        self,
        tool_name: str,
        payload: dict[str, Any],
        identity: SessionIdentity,
        *,
        module_context: object | None = None,
        request_id: str = "",
        agent_proxy: bool = False,
        candidate_variant: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        action = str(payload.get("action", "")).strip().lower()
        compute_read_action = tool_name in {"payoffer", "pricer", "backtester"} and action not in {"", "run"}
        if action in {"catalog", "status", "list_assets", "list_report_sources"}:
            _, result = self._jobs.run(lambda: self._gateway.dispatch(
                tool_name, payload, identity, module_context=module_context, request_id=request_id,
                agent_proxy=agent_proxy, candidate_variant=candidate_variant,
            ))
            return result
        if compute_read_action:
            # Only declared ``run`` reaches the task/contract/result pipeline.
            # A non-run action remains a formal module concern: success is a
            # read-only response; ``ok=False`` is a module rejection, not an
            # App-level silent success.  Future writing actions must be added
            # explicitly to the run-class gate rather than relying on this.
            _, result = self._jobs.run(lambda: self._gateway.dispatch(
                tool_name, payload, identity, module_context=module_context, request_id=request_id,
                agent_proxy=agent_proxy, candidate_variant=candidate_variant,
            ))
            if result.get("ok") is False:
                raise ValidationError(str(result.get("message", f"Capability tool {tool_name} rejected the request")))
            return result
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValidationError("task_id is required for a Capability module run")
        self._tasks.get(identity, task_id)
        idempotency_key = None
        input_hash = None
        if tool_name in {"payoffer", "pricer", "backtester"}:
            clean_payload = dict(payload)
            supplied_key = clean_payload.pop("idempotency_key", None)
            input_hash = _tool_input_hash(clean_payload)
            payload = clean_payload
            operation_id = stable_operation_id(
                identity.tenant_id, task_id, tool_name, input_hash,
                request_id or supplied_key or f"session:{identity.session_id}",
            )
            job_id = stable_operation_id(operation_id, "job")
            if not supplied_key and not request_id:
                return self._dispatch_mutating(
                    tool_name, payload, identity, task_id,
                    module_context=module_context,
                    request_id=request_id,
                    agent_proxy=agent_proxy,
                    operation_id=operation_id,
                    candidate_variant=candidate_variant,
                )
            idempotency_key = _idempotency_key(supplied_key or request_id)
            with self._idempotency_lock(identity, task_id, tool_name, idempotency_key):
                claim_state, replay = self._idempotency.claim(
                    tenant_id=identity.tenant_id,
                    task_id=task_id,
                    module=tool_name,
                    action="run",
                    idempotency_key=idempotency_key,
                    input_hash=input_hash,
                    operation_id=operation_id,
                    job_id=job_id,
                )
                if claim_state == "completed" and replay is not None:
                    return self._replay(identity, replay)
                if claim_state == "uncertain":
                    raise ValidationError("此前运行可能已提交但缺少最终ModuleRunRef索引，禁止自动重算；需按恢复记录人工核验")
                if claim_state == "busy":
                    raise ValidationError("相同计算请求正在执行，完成后重试将返回原ModuleRunRef")
                try:
                    return self._dispatch_mutating(
                        tool_name, payload, identity, task_id,
                        module_context=module_context,
                        request_id=request_id,
                        agent_proxy=agent_proxy,
                        idempotency_key=idempotency_key,
                        input_hash=input_hash,
                        operation_id=operation_id,
                        candidate_variant=candidate_variant,
                    )
                except _CommittedRunPendingError as error:
                    self._idempotency.mark_uncertain(
                        tenant_id=identity.tenant_id, task_id=task_id, module=tool_name, action="run",
                        idempotency_key=idempotency_key, input_hash=input_hash,
                    )
                    raise ValidationError(str(error)) from error
                except Exception:
                    self._idempotency.abandon(
                        tenant_id=identity.tenant_id, task_id=task_id, module=tool_name, action="run",
                        idempotency_key=idempotency_key, input_hash=input_hash,
                    )
                    raise
        return self._dispatch_mutating(
            tool_name, payload, identity, task_id,
            module_context=module_context,
            request_id=request_id,
            agent_proxy=agent_proxy,
            candidate_variant=candidate_variant,
        )

    def _dispatch_mutating(
        self,
        tool_name: str,
        payload: dict[str, Any],
        identity: SessionIdentity,
        task_id: str,
        *,
        module_context: object | None,
        request_id: str,
        agent_proxy: bool,
        idempotency_key: str | None = None,
        input_hash: str | None = None,
        operation_id: str | None = None,
        candidate_variant: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        gateway_payload = dict(payload)
        if tool_name in {"payoffer", "pricer", "backtester"}:
            # task_id is a Host routing fact, not part of the three business
            # input contracts consumed by the Capability.
            gateway_payload.pop("task_id", None)
        resolved_operation_id = operation_id or stable_operation_id(
            identity.tenant_id, task_id, tool_name,
            input_hash or _tool_input_hash(gateway_payload),
            request_id or idempotency_key or f"session:{identity.session_id}",
        )
        checkpoint = self._dispatch_checkpoints.checkpoint(
            self._tasks.conversation_session_id(identity, task_id),
            operation_id=resolved_operation_id,
            operation_kind="side_effect_tool",
            safe_metadata={"module": tool_name},
        )
        try:
            execution, result = self._jobs.run(lambda: self._gateway.dispatch(
                tool_name, gateway_payload, identity, module_context=module_context, request_id=request_id,
                agent_proxy=agent_proxy, candidate_variant=candidate_variant,
            ), task_id=task_id if tool_name in {"payoffer", "pricer", "backtester"} else None,
                module=tool_name if tool_name in {"payoffer", "pricer", "backtester"} else None,
                job_id=stable_operation_id(checkpoint.operation_id, "job") if tool_name in {"payoffer", "pricer", "backtester"} else None,
                tenant_id=identity.tenant_id,
                owner_id=identity.principal_id,
                operation_id=checkpoint.operation_id,
                defer_success=tool_name in {"payoffer", "pricer", "backtester"})
        except Exception:
            self._dispatch_checkpoints.failed(checkpoint)
            raise
        try:
            return self._finalize_mutating_result(
                tool_name, result, identity, task_id, execution, checkpoint,
                idempotency_key=idempotency_key, input_hash=input_hash,
            )
        except _CommittedRunPendingError:
            if tool_name in {"payoffer", "pricer", "backtester"}:
                self._jobs.interrupt_job(execution.job_id)
            self._dispatch_checkpoints.interrupted_unknown(checkpoint)
            raise
        except Exception:
            if tool_name in {"payoffer", "pricer", "backtester"}:
                self._jobs.fail_job(execution.job_id)
            self._dispatch_checkpoints.failed(checkpoint)
            raise

    def _finalize_mutating_result(
        self,
        tool_name: str,
        result: dict[str, Any],
        identity: SessionIdentity,
        task_id: str,
        execution: Any,
        checkpoint: Any,
        *,
        idempotency_key: str | None,
        input_hash: str | None,
    ) -> dict[str, Any]:
        if result.get("status") == "unavailable":
            raise UnavailableCapabilityError(f"Capability tool {tool_name}", str(result.get("reason", "the Capability reported unavailable")))
        if result.get("ok") is False:
            if tool_name == "datafetcher":
                raise UserActionError("datafetcher_request_rejected", _datafetcher_failure_message(result))
            if tool_name == "pricer":
                raise UserActionError(
                    "pricer_request_rejected",
                    _pricer_failure_message(result),
                    stage="compute",
                    next_step="请检查定价方法、Monte Carlo路径数、合同期限和市场参数后重新运行。",
                )
            raise ValidationError(str(result.get("message", f"Capability tool {tool_name} rejected the request")))

        data_asset_ref = result.get("data_asset_ref")
        if tool_name == "datafetcher":
            if not isinstance(data_asset_ref, dict):
                raise ValidationError("successful DataFetcher response requires data_asset_ref")
            self._data_assets.register(identity, data_asset_ref)
            if isinstance(task_id, str) and task_id:
                self._tasks.append_data_asset_ref(identity, task_id, data_asset_ref)
            # DataFetcher produces DataAssetRef, never a calculation ModuleRun.
            self._dispatch_checkpoints.succeeded(checkpoint)
            return {**result, "job": execution.__dict__}
        if tool_name == "reporter":
            # Reporter commits its distinct App-owned ReportRun inside the
            # formal adapter; it must never be relabelled as ModuleRun.
            self._dispatch_checkpoints.succeeded(checkpoint)
            return {**result, "job": execution.__dict__}
        if tool_name not in {"payoffer", "pricer", "backtester"}:
            self._dispatch_checkpoints.succeeded(checkpoint)
            return {**result, "job": execution.__dict__}

        existing = result.get("module_run_ref")
        if isinstance(existing, dict):
            if existing.get("module") != tool_name or existing.get("task_id") != task_id or existing.get("tenant_id") != identity.tenant_id:
                raise ValidationError("Capability ModuleRunRef does not match authenticated App scope")
            self._results.resolve_module_run(identity, existing)
            reference = dict(existing)
        else:
            reference = self._results.commit_module_run(identity, task_id, tool_name, result)
        # The immutable Core-backed proof is durable before mutable Task and
        # idempotency projections are updated.
        self._jobs.record_module_run_proof(execution.job_id, reference)
        try:
            self._tasks.append_run_ref(identity, task_id, reference)
        except Exception:
            try:
                self._tasks.append_run_ref(identity, task_id, reference)
            except Exception as retry_error:
                raise _CommittedRunPendingError("ModuleRun已提交，但任务索引写入失败；已禁止自动重算") from retry_error
        if idempotency_key is not None and input_hash is not None:
            try:
                self._idempotency.complete(
                    tenant_id=identity.tenant_id,
                    task_id=task_id,
                    module=tool_name,
                    action="run",
                    idempotency_key=idempotency_key,
                    input_hash=input_hash,
                    reference=reference,
                )
            except Exception as error:
                raise _CommittedRunPendingError("ModuleRun已提交，但幂等索引完成写入失败；已禁止自动重算") from error
        self._jobs.succeed_job(execution.job_id, reference)
        self._dispatch_checkpoints.succeeded(checkpoint)
        return {**result, "module_run_ref": reference, "job": execution.__dict__}

    @contextmanager
    def _idempotency_lock(
        self, identity: SessionIdentity, task_id: str, module: str, key: str,
    ) -> Iterator[None]:
        scope = (identity.tenant_id, task_id, module, key)
        with self._idempotency_guard:
            entry = self._idempotency_locks.setdefault(scope, _LockEntry())
            entry.users += 1
        entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            with self._idempotency_guard:
                entry.users -= 1
                if entry.users == 0 and self._idempotency_locks.get(scope) is entry:
                    self._idempotency_locks.pop(scope, None)

    def _replay(self, identity: SessionIdentity, reference: dict[str, str]) -> dict[str, Any]:
        self._results.resolve_owned_module_run(identity, reference)
        record = self._results.resolve_module_run(identity, reference)
        result = record.get("result")
        if not isinstance(result, dict):
            raise ValidationError("持久幂等索引指向无效ModuleRun")
        return {**result, "module_run_ref": reference, "idempotent_replay": True}

    def dispatch_for_conversation(
        self,
        tool_name: str,
        payload: dict[str, Any],
        identity: SessionIdentity,
        *,
        module_context: object,
        request_id: str,
        candidate_variant: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a server-originated OptChat tool under proxy, not Desk, authority."""
        _require_task_data_scope(self._tasks, identity, tool_name, payload)
        return self.dispatch(
            tool_name,
            payload,
            identity,
            module_context=module_context,
            request_id=request_id,
            agent_proxy=True,
            candidate_variant=candidate_variant,
        )


def _require_task_data_scope(tasks: TaskService, identity: SessionIdentity, tool_name: str, payload: Mapping[str, Any]) -> None:
    """Bind conversation calculations to DataAssetRefs already attached to this task."""
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValidationError("conversation tool requires task_id")
    task = tasks.get(identity, task_id)
    if tool_name not in {"pricer", "backtester"}:
        return
    permitted = {
        str(item.get("data_asset_id")): str(item.get("content_hash"))
        for item in task.get("data_asset_refs", []) if isinstance(item, Mapping)
        if isinstance(item.get("data_asset_id"), str) and isinstance(item.get("content_hash"), str)
    }
    if not permitted:
        raise ValidationError("当前任务尚无可用于计算的受控DataAssetRef")
    candidate = payload.get("market_data_refs") if tool_name == "pricer" else payload.get("historical_data")
    if tool_name == "pricer":
        if not isinstance(candidate, list) or len(candidate) != 1:
            raise ValidationError("OptChat Pricer必须指定当前任务的唯一DataAssetRef")
        candidate = candidate[0]
    asset_id, content_hash = _task_data_asset_ref(candidate)
    if asset_id not in permitted or content_hash not in {None, permitted[asset_id]}:
        raise AuthorizationError("conversation.tool.run", "DataAssetRef不属于当前任务")
    if tool_name == "pricer" and payload.get("trading_calendar_ref") is not None:
        calendar_id, calendar_hash = _task_data_asset_ref(payload["trading_calendar_ref"])
        if calendar_id not in permitted or calendar_hash not in {None, permitted[calendar_id]}:
            raise AuthorizationError("conversation.tool.run", "交易日历DataAssetRef不属于当前任务")


def _task_data_asset_ref(value: object) -> tuple[str, str | None]:
    if isinstance(value, str):
        return value, None
    if isinstance(value, Mapping) and set(value).issubset({"data_asset_id", "content_hash"}) and isinstance(value.get("data_asset_id"), str):
        raw_hash = value.get("content_hash")
        return str(value["data_asset_id"]), str(raw_hash) if isinstance(raw_hash, str) else None
    raise ValidationError("OptChat计算只能引用当前任务的DataAssetRef")


def _datafetcher_failure_message(result: Mapping[str, Any]) -> str:
    """Return only a safe, actionable DataFetcher failure for the App UI."""
    error = result.get("error")
    if not isinstance(error, Mapping):
        return "数据请求未完成，请检查标的、日期与数据服务后重试。"
    code = str(error.get("code", "")).strip().lower()
    fixed_messages = {
        "unauthorized": "iFind凭据不可用，请在设置中心重新保存Refresh Token后重试。",
        "provider_unavailable": "iFind连接暂不可用，请检查网络和数据服务状态后重试。",
        "quota_exceeded": "本次数据请求超过当前服务限额，请缩短区间或稍后重试。",
    }
    if code in fixed_messages:
        return fixed_messages[code]
    message = str(error.get("message", "")).strip()
    forbidden = ("/", "\\\\", "token", "secret", "password", "credential")
    if message and len(message) <= 240 and not any(item in message.lower() for item in forbidden):
        return message
    return "数据请求未完成，请检查标的、日期与数据服务后重试。"


def _pricer_failure_message(result: Mapping[str, Any]) -> str:
    """Project the Pricer's reviewed financial-input explanation to the UI."""

    candidates: list[object] = [result.get("message")]
    pricing = result.get("pricing")
    if isinstance(pricing, Mapping):
        messages = pricing.get("messages")
        if isinstance(messages, list):
            candidates.extend(messages)
    limitations = result.get("limitations")
    if isinstance(limitations, list):
        candidates.extend(limitations)
    forbidden = ("/", "\\", "token", "secret", "password", "credential", "traceback")
    safe = []
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        message = candidate.strip()
        if message and len(message) <= 240 and not any(item in message.lower() for item in forbidden):
            safe.append(message)
    if safe:
        return "；".join(dict.fromkeys(safe))
    return "定价请求未完成，请检查定价方法、Monte Carlo路径数、合同期限和市场参数后重试。"


def _tool_input_hash(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValidationError("Tool输入必须是可稳定哈希的JSON对象") from error
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _idempotency_key(value: object) -> str:
    key = str(value or "").strip()
    if not key or len(key) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in key):
        raise ValidationError("idempotency_key必须是1至128字符的可打印字符串")
    return key
