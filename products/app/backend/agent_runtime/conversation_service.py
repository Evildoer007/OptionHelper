"""Conversation persistence plus an explicit ModelGateway handoff."""

from __future__ import annotations

from threading import Lock
from typing import Any, Protocol

from ..errors import UnavailableCapabilityError
from ..identity.session_identity import SessionIdentity
from ..model_gateway.gateway import ModelGateway
from ..settings.settings_models import ModelSelection
from ..task_runtime.task_service import TaskService
from .redaction import redact_text


class AgentLoopPort(Protocol):
    def run(self, identity: SessionIdentity, task_id: str, message: str, *, selection: ModelSelection | None = None) -> dict[str, Any]: ...


class ConversationService:
    def __init__(self, task_service: TaskService, gateway: ModelGateway, *, agent_loop: AgentLoopPort | None = None) -> None:
        self._task_service = task_service
        self._gateway = gateway
        self._agent_loop = agent_loop
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
    ) -> dict[str, Any]:
        """Serialize one task and replay an identical HTTP retry.

        The lock prevents two different request ids from concurrently reaching
        the model or a non-idempotent tool.  The TaskService journal makes a
        same-id retry return the original safe response after the lock is
        released, including after an App restart.
        """
        with self._task_lock(identity, task_id):
            replay_key = _replay_key(message, selection)
            replay = self._task_service.get_conversation_response(identity, task_id, request_id, replay_key)
            if replay is not None:
                return replay
            if self._task_service.is_cancelled(identity, task_id):
                response = _cancelled_response()
                self._task_service.record_conversation_response(identity, task_id, request_id, replay_key, response)
                return response
            recorded = self._task_service.append_message(identity, task_id, "user", message, status="pending_model")
            if self._agent_loop is not None:
                response = (
                    self._agent_loop.run(identity, task_id, message)
                    if selection is None
                    else self._agent_loop.run(identity, task_id, message, selection=selection)
                )
                response = _safe_response(response, identity)
                assistant = self._task_service.append_message(
                    identity,
                    task_id,
                    "assistant",
                    str(response.get("text", "当前未生成答复。")),
                    status=str(response.get("status", "recorded")),
                )
                completed = {**response, "message": recorded, "assistant_message": assistant}
                self._task_service.record_conversation_response(identity, task_id, request_id, replay_key, completed)
                return completed
            try:
                assistant_text = redact_text(self._gateway.complete_for(identity, task_id, message, selection=selection), identity, limit=4_000)
            except UnavailableCapabilityError as error:
                unavailable = {
                    "status": "unavailable",
                    "message": recorded,
                    "error": {"capability": error.capability, "next_step": error.next_step},
                    "state": {"code": "unavailable", "terminal": True, "retryable": True},
                }
                self._task_service.record_conversation_response(identity, task_id, request_id, replay_key, unavailable)
                return unavailable
            except Exception:
                unavailable = {
                    "status": "unavailable",
                    "message": recorded,
                    "error": {"capability": "model", "next_step": "模型服务暂不可用，请稍后重试。"},
                    "state": {"code": "unavailable", "terminal": True, "retryable": True},
                }
                self._task_service.record_conversation_response(identity, task_id, request_id, replay_key, unavailable)
                return unavailable
            assistant = self._task_service.append_message(identity, task_id, "assistant", assistant_text, status="completed")
            completed = {
                "status": "completed", "message": recorded, "assistant_message": assistant,
                "state": {"code": "completed", "terminal": True, "retryable": False},
            }
            self._task_service.record_conversation_response(identity, task_id, request_id, replay_key, completed)
            return completed

    def _task_lock(self, identity: SessionIdentity, task_id: str) -> Lock:
        key = (identity.tenant_id, identity.principal_id, task_id)
        with self._locks_guard:
            return self._locks.setdefault(key, Lock())


def _cancelled_response() -> dict[str, Any]:
    return {
        "status": "cancelled", "text": "本次任务已取消；请新建任务后继续。", "observations": [], "rounds": 0,
        "state": {"code": "cancelled", "terminal": True, "retryable": False},
    }


def _replay_key(message: str, selection: ModelSelection | None) -> str:
    """Bind idempotency to both user text and the selected model identity."""

    if selection is None:
        return message
    return f"{message}\x1f{selection.provider_id}\x1f{selection.model_id}"


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
