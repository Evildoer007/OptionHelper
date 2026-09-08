"""Conversation persistence plus an explicit ModelGateway handoff."""

from __future__ import annotations

from threading import Lock
from typing import Any, Protocol

from ..errors import UnavailableCapabilityError, ValidationError
from ..report_delivery import delivery_request
from ..identity.session_identity import SessionIdentity
from ..model_gateway.gateway import ModelGateway
from ..settings.settings_models import ModelSelection
from ..task_runtime.task_service import TaskService
from .redaction import redact_text
from .durability_checkpoint import DurabilityCheckpointStore, stable_operation_id


class AgentLoopPort(Protocol):
    def run(
        self, identity: SessionIdentity, task_id: str, message: str, *,
        selection: ModelSelection | None = None, attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...

    def run_with_execution(
        self, identity: SessionIdentity, task_id: str, message: str, *,
        selection: ModelSelection | None = None, execution_ids: dict[str, str],
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...


class ConversationService:
    def __init__(self, task_service: TaskService, gateway: ModelGateway, *, agent_loop: AgentLoopPort | None = None, report_delivery=None) -> None:
        self._task_service = task_service
        self._gateway = gateway
        self._agent_loop = agent_loop
        self._report_delivery = report_delivery
        self._dispatch_checkpoints = DurabilityCheckpointStore(task_service.session_event_log)
        self._locks: dict[tuple[str, str, str], Lock] = {}
        self._locks_guard = Lock()

    def respond(
        self,
        identity: SessionIdentity,
        task_id: str,
        message: str,
        *,
        request_id: str | None = None,
        selection: ModelSelection | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Serialize one task and replay an identical HTTP retry.

        The lock prevents two different request ids from concurrently reaching
        the model or a non-idempotent tool.  The TaskService journal makes a
        same-id retry return the original safe response after the lock is
        released, including after an App restart.
        """
        with self._task_lock(identity, task_id):
            replay_key = _replay_key(message, selection, attachments)
            replay = self._task_service.get_conversation_response(identity, task_id, request_id, replay_key)
            if replay is not None:
                return replay
            request_state = self._task_service.begin_conversation_request(
                identity, task_id, request_id, replay_key,
            )
            was_cancelled = self._task_service.is_cancelled(identity, task_id)
            recorded = self._task_service.append_message(
                identity,
                task_id,
                "user",
                message,
                status="pending_model",
                message_id=request_state.get("user_message_id") if request_state else None,
                created_at=request_state.get("user_message_created_at") if request_state else None,
                attachments=attachments,
            )
            if request_state and request_state.get("status") in {"execution_started", "recovery_required"}:
                response = request_state.get("recovery_response")
                if not isinstance(response, dict):
                    response = _interrupted_execution_response()
                    request_state = self._task_service.mark_conversation_recovery_required(
                        identity, task_id, str(request_id), replay_key, response,
                    )
                assistant = self._task_service.append_message(
                    identity,
                    task_id,
                    "assistant",
                    str(response["text"]),
                    status="recovery_required",
                    message_id=request_state.get("assistant_message_id"),
                    created_at=request_state.get("assistant_message_created_at") or request_state.get("recovery_required_at"),
                )
                self._emit_process_event(identity, task_id, request_id, "terminal", "failed", "本次请求已中断，等待你重新发起。")
                return {**response, "message": recorded, "assistant_message": assistant}
            if request_state and request_state.get("status") == "model_settled":
                raw_response = request_state.get("model_response")
                if not isinstance(raw_response, dict):
                    raise RuntimeError("Settled conversation request has no model response")
                response = dict(raw_response)
            else:
                if was_cancelled:
                    response = _cancelled_response()
                else:
                    request_state = self._task_service.mark_conversation_execution_started(
                        identity, task_id, request_id, replay_key,
                    ) or request_state
                    delivery = delivery_request(message) if self._report_delivery else None
                    report_id = self._report_delivery.current_document(identity, task_id) if delivery and delivery['export_only'] and not delivery['multiple'] else None
                    previous_reports = {row['report_run_id'] for row in self._report_delivery.results.list_report_runs(identity, task_id)} if self._report_delivery else set()
                    if report_id:
                        response = {'status': 'completed'}
                    else:
                        response = self._run_model(
                            identity, task_id, message, selection,
                            execution_ids=_execution_ids(request_state),
                            attachments=attachments or [],
                        )
                    created = []
                    if self._report_delivery:
                        created = [row['report_run_id'] for row in self._report_delivery.results.list_report_runs(identity, task_id) if row['report_run_id'] not in previous_reports]
                    if delivery:
                        try:
                            response = self._report_delivery.complete(identity, task_id, message, response,
                                request_id=request_id, report_id=report_id or (created[-1] if created else None))
                        except ValidationError as error:
                            response = {'status':'needs_input','text':str(error)}
                    if self._report_delivery and created:
                        response = self._report_delivery.attach_documents(identity, task_id, response, created)

                settled = self._task_service.settle_conversation_model(
                    identity, task_id, request_id, replay_key, response,
                )
                if settled is not None:
                    request_state = settled
            self._emit_process_event(identity, task_id, request_id, "answer", "completed", "正在整理并写入本轮答复。")
            assistant = None
            assistant_blocks = response.pop("_assistant_blocks", None)
            user_question = response.pop("_user_question", None)
            if isinstance(user_question, dict):
                assistant_blocks = [
                    *(assistant_blocks if isinstance(assistant_blocks, list) else []),
                    user_question,
                ]
            assistant_text = response.get("text")
            if isinstance(assistant_text, str) and assistant_text.strip():
                assistant = self._task_service.append_message(
                    identity,
                    task_id,
                    "assistant",
                    assistant_text,
                    status=str(response.get("status", "recorded")),
                    message_id=request_state.get("assistant_message_id") if request_state else None,
                    created_at=request_state.get("assistant_message_created_at") if request_state else None,
                    content_blocks=assistant_blocks if isinstance(assistant_blocks, list) else None,
                )
            completed = {**response, "message": recorded}
            if assistant is not None:
                completed["assistant_message"] = assistant
            self._task_service.complete_conversation_request(
                identity, task_id, request_id, replay_key, completed,
            )
            terminal_status = "cancelled" if response.get("status") == "cancelled" else (
                "failed" if response.get("status") in {"unavailable", "timed_out", "blocked"} else "completed"
            )
            terminal_summary = "本次处理已取消。" if terminal_status == "cancelled" else (
                "本次处理未能完成，请按提示补充或重试。" if terminal_status == "failed" else "本轮处理已完成。"
            )
            self._emit_process_event(identity, task_id, request_id, "terminal", terminal_status, terminal_summary)
            return completed

    def _run_model(
        self,
        identity: SessionIdentity,
        task_id: str,
        message: str,
        selection: ModelSelection | None,
        *,
        execution_ids: dict[str, str],
        attachments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self._task_service.is_cancelled(identity, task_id):
            return _cancelled_response()
        if self._agent_loop is not None:
            controlled_run = getattr(self._agent_loop, "run_with_execution", None)
            if callable(controlled_run):
                response = controlled_run(
                    identity, task_id, message, selection=selection,
                    execution_ids=execution_ids, attachments=attachments,
                )
            else:
                optional: dict[str, Any] = {}
                if selection is not None:
                    optional["selection"] = selection
                if attachments:
                    optional["attachments"] = attachments
                response = self._agent_loop.run(identity, task_id, message, **optional)
            return _safe_response(response, identity)
        try:
            self._emit_process_event(identity, task_id, execution_ids.get("request_id"), "routing", "completed", "已识别为常规对话，交由主Agent处理。")
            self._emit_process_event(identity, task_id, execution_ids.get("request_id"), "agent_run", "started", "主Agent正在分析需求。")
            session_id = self._task_service.conversation_session_id(identity, task_id)
            checkpoint = self._dispatch_checkpoints.checkpoint(
                session_id,
                operation_id=stable_operation_id(execution_ids.get("model_step_id", task_id), "fallback_model"),
                operation_kind="main_model",
                safe_metadata={"task_id_hash": stable_operation_id(task_id)},
            )
            text = redact_text(
                self._gateway.complete_for(identity, task_id, message, selection=selection),
                identity,
                limit=4_000,
            )
            self._dispatch_checkpoints.succeeded(checkpoint)
            self._emit_process_event(identity, task_id, execution_ids.get("request_id"), "agent_run", "completed", "主Agent已完成本轮分析。")
            return {
                "status": "completed",
                "text": text,
                "state": {"code": "completed", "terminal": True, "retryable": False},
            }
        except UnavailableCapabilityError as error:
            if "checkpoint" in locals():
                self._dispatch_checkpoints.failed(checkpoint)
            return {
                "status": "unavailable",
                "error": {"capability": error.capability, "next_step": error.next_step},
                "state": {"code": "unavailable", "terminal": True, "retryable": True},
            }
        except Exception:
            if "checkpoint" in locals():
                self._dispatch_checkpoints.failed(checkpoint)
            return {
                "status": "unavailable",
                "error": {"capability": "model", "next_step": "模型服务暂不可用，请稍后重试。"},
                "state": {"code": "unavailable", "terminal": True, "retryable": True},
            }

    def _task_lock(self, identity: SessionIdentity, task_id: str) -> Lock:
        key = (identity.tenant_id, identity.principal_id, task_id)
        with self._locks_guard:
            return self._locks.setdefault(key, Lock())

    def _emit_process_event(
        self,
        identity: SessionIdentity,
        task_id: str,
        request_id: str | None,
        event_type: str,
        status: str,
        summary: str,
    ) -> None:
        self._task_service.append_visible_process_event(
            identity, task_id, request_id, event_type, status, summary,
        )


def _cancelled_response() -> dict[str, Any]:
    return {
        "status": "cancelled", "text": "本次运行已取消；可在当前任务中调整后重新运行。", "observations": [], "rounds": 0,
        "state": {"code": "cancelled", "terminal": True, "retryable": False},
    }


def _execution_ids(request_state: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(request_state, dict):
        return {}
    return {
        key: str(request_state[key])
        for key in (
            "execution_id", "workflow_run_id", "step_id_namespace",
            "model_step_id", "recommender_step_id", "tool_step_id_namespace", "request_id",
        )
        if isinstance(request_state.get(key), str) and str(request_state[key]).strip()
    }


def _interrupted_execution_response() -> dict[str, Any]:
    return {
        "status": "recovery_required",
        "text": "上次执行在外部调用完成状态落盘前中断。为避免重复调用，本请求不会自动重放；请新建一次请求后重试。",
        "error": {
            "capability": "conversation_execution_recovery",
            "next_step": "确认后使用新的请求标识重试；原请求将保留用于审计。",
        },
        "state": {"code": "recovery_required", "terminal": True, "retryable": True},
    }


def _replay_key(
    message: str, selection: ModelSelection | None, attachments: list[dict[str, Any]] | None = None,
) -> str:
    """Bind idempotency to both user text and the selected model identity."""

    selected = "" if selection is None else f"{selection.provider_id}\x1f{selection.model_id}"
    attachment_key = ",".join(
        str(item.get("attachment_id", ""))
        for item in (attachments or []) if isinstance(item, dict)
    )
    return f"{message}\x1f{selected}\x1f{attachment_key}"


def _safe_response(response: dict[str, Any], identity: SessionIdentity) -> dict[str, Any]:
    """Defence in depth for a custom AgentLoop implementation in tests/apps."""
    safe = dict(response)
    if isinstance(safe.get("text"), str):
        safe["text"] = redact_text(safe["text"], identity, limit=4_000)
    # Module facts and references stay in ResultStore and are re-projected by
    # ContextBuilder on the next model turn. They are not public conversation
    # fields, even as opaque references.
    observations = safe.get("observations")
    if isinstance(observations, list):
        # The UI only needs the user-facing response text.  Recommendation and
        # tool identifiers are server-side orchestration state, so returning
        # them here would reveal opaque candidate IDs even though no result
        # reference is otherwise exposed.
        safe["observations"] = [
            {key: value for key, value in observation.items() if key in {"status", "message"}}
            for observation in observations if isinstance(observation, dict)
        ]
    return safe
