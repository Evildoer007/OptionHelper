"""Persistent App task and conversation state.

Tasks are App indices only. They retain a conversation Session reference and
business projections, never raw history or copied Capability financial results.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..errors import AuthorizationError, ValidationError
from ..authorization.roles import Role
from ..identity.session_identity import SessionIdentity
from ..stores import _LocalDocumentStore
from ..agent_runtime.session_context import (
    ModelVisibleSurface,
    RunId,
    SessionEventLog,
    SessionId,
    SurfaceProjector,
    transcript_messages,
)
from ..agent_runtime.projection_cache import ProjectionCache


class TaskService:
    def __init__(self, state: _LocalDocumentStore, event_log: SessionEventLog | None = None) -> None:
        self._state = state
        self._event_log = event_log or SessionEventLog(state._root / "session-events.sqlite3")
        self._projection_cache = ProjectionCache(self._event_log)

    @property
    def session_event_log(self) -> SessionEventLog:
        return self._event_log

    def create(self, identity: SessionIdentity, subject: str) -> dict[str, Any]:
        subject = subject.strip()
        if not subject or len(subject) > 160:
            raise ValidationError("Task subject must contain 1 to 160 characters")
        now = datetime.now(timezone.utc).isoformat()
        conversation_session_id = SessionId(f"conversation-{uuid4().hex}")
        self._event_log.ensure_session(conversation_session_id, kind="conversation")
        task = {
            "task_id": str(uuid4()),
            "tenant_id": identity.tenant_id,
            "created_by": identity.principal_id,
            "subject": subject,
            "status": "created",
            "created_at": now,
            "updated_at": now,
            "conversation_session_id": str(conversation_session_id),
            "message_count": 0,
            "run_refs": [],
            "data_asset_refs": [],
            # A bounded, private idempotency journal.  It records only a
            # completed OptChat response projection, never provider payloads
            # or hidden model reasoning.
            "conversation_requests": {},
        }

        def update(value: dict[str, Any]) -> dict[str, Any]:
            value[task["task_id"]] = task
            return value

        self._state.update("tasks", update)
        return self._public_task(task)

    def list(self, identity: SessionIdentity) -> list[dict[str, Any]]:
        tasks = self._state.read("tasks").values()
        result = [
            self._migrate_legacy_messages(identity, str(task.get("task_id")))
            for task in tasks
            if isinstance(task, dict) and task.get("tenant_id") == identity.tenant_id and task.get("created_by") == identity.principal_id
        ]
        return [self._public_task(task) for task in sorted(result, key=lambda task: str(task.get("updated_at", "")), reverse=True)]

    def get(self, identity: SessionIdentity, task_id: str) -> dict[str, Any]:
        return self._public_task(self._get_raw(identity, task_id))

    def rename(self, identity: SessionIdentity, task_id: str, subject: str) -> dict[str, Any]:
        """Rename one owned App task without changing its module results."""

        subject = subject.strip()
        if not subject or len(subject) > 160:
            raise ValidationError("Task subject must contain 1 to 160 characters")

        def update(value: dict[str, Any]) -> dict[str, Any]:
            task = value.get(task_id)
            if not isinstance(task, dict):
                raise KeyError(task_id)
            if task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
                raise AuthorizationError("conversation.write", "task is not owned by current caller")
            task["subject"] = subject
            task["updated_at"] = datetime.now(timezone.utc).isoformat()
            return value

        return self._public_task(self._state.update("tasks", update)[task_id])

    def delete(self, identity: SessionIdentity, task_id: str) -> dict[str, Any]:
        """Recoverably remove one owned task after its full session tree.

        Capability-owned module results, reports, and data assets deliberately
        remain outside this index and are never deleted by this operation.
        """

        deleting: dict[str, Any] | None = None

        def mark_started(value: dict[str, Any]) -> dict[str, Any]:
            nonlocal deleting
            task = _owned_task(value, identity, task_id, "conversation.write")
            deletion = task.get("deletion")
            if not isinstance(deletion, dict) or deletion.get("status") != "delete_started":
                deletion = {
                    "status": "delete_started",
                    "started_at": _utc_now(),
                    "conversation_session_id": str(task.get("conversation_session_id", "")),
                }
                task["deletion"] = deletion
                task["status"] = "deleting"
                task["updated_at"] = deletion["started_at"]
            deleting = copy.deepcopy(task)
            return value

        self._state.update("tasks", mark_started)
        assert deleting is not None
        session_id = str(deleting.get("deletion", {}).get("conversation_session_id", "")).strip()
        if session_id:
            self._event_log.delete_session_tree(SessionId(session_id))

        removed: dict[str, Any] | None = None

        def complete_delete(value: dict[str, Any]) -> dict[str, Any]:
            nonlocal removed
            task = _owned_task(value, identity, task_id, "conversation.write")
            deletion = task.get("deletion")
            if not isinstance(deletion, dict) or deletion.get("status") != "delete_started":
                raise ValidationError("Task deletion journal is invalid")
            removed = copy.deepcopy(task)
            value.pop(task_id)
            return value

        self._state.update("tasks", complete_delete)
        assert removed is not None
        return self._public_task(removed)

    def _get_raw(self, identity: SessionIdentity, task_id: str) -> dict[str, Any]:
        task = self._state.read("tasks").get(task_id)
        if not isinstance(task, dict):
            raise KeyError(task_id)
        if task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
            raise AuthorizationError("task.read", "task is not owned by current caller")
        return self._migrate_legacy_messages(identity, task_id)

    def _migrate_legacy_messages(self, identity: SessionIdentity, task_id: str) -> dict[str, Any]:
        """Idempotently move legacy embedded messages into the session log."""

        current = self._state.read("tasks").get(task_id)
        if not isinstance(current, dict):
            raise KeyError(task_id)
        if current.get("tenant_id") != identity.tenant_id or current.get("created_by") != identity.principal_id:
            raise AuthorizationError("task.read", "task is not owned by current caller")
        raw_session_id = str(current.get("conversation_session_id", "")).strip()
        session_id = SessionId(raw_session_id or f"conversation-{uuid4().hex}")
        self._event_log.ensure_session(session_id, kind="conversation")
        legacy = current.get("messages")
        if isinstance(legacy, list):
            for index, item in enumerate(legacy):
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role", "user"))
                if role not in {"user", "assistant", "system"}:
                    role = "user"
                content = str(item.get("content", "")).strip()
                if not content:
                    continue
                message = {
                    "message_id": str(item.get("message_id") or f"legacy-{index}"),
                    "role": role,
                    "content": content,
                    "status": str(item.get("status", "recorded")),
                    "created_at": str(item.get("created_at") or current.get("created_at") or _utc_now()),
                }
                self._event_log.append(
                    session_id,
                    "conversation.message",
                    {"message": message},
                    event_id=f"legacy-message:{message['message_id']}:{index}",
                    surface_operation="append",
                )

        needs_projection_update = raw_session_id != str(session_id) or isinstance(legacy, list) or "message_count" not in current
        if needs_projection_update:
            migrated_at = _utc_now()

            def update(tasks: dict[str, Any]) -> dict[str, Any]:
                task = _owned_task(tasks, identity, task_id, "conversation.write")
                task["conversation_session_id"] = str(session_id)
                task["message_count"] = len(transcript_messages(self._event_log.replay(session_id)))
                task["legacy_messages_migrated_at"] = migrated_at
                task.pop("messages", None)
                task.pop("agent_events", None)
                return tasks

            current = self._state.update("tasks", update)[task_id]
        return current

    def _public_task(self, value: dict[str, Any]) -> dict[str, Any]:
        public = {
            key: copy.deepcopy(item)
            for key, item in value.items()
            if key not in {
                "conversation_requests", "recommendation_state", "agent_events",
                "conversation_session_id", "legacy_messages_migrated_at", "deletion",
            }
        }
        deletion = value.get("deletion")
        if isinstance(deletion, dict) and deletion.get("status") == "delete_started":
            public["deletion_status"] = "delete_started"
        session_id = str(value.get("conversation_session_id", "")).strip()
        public["messages"] = (
            transcript_messages(self._event_log.replay(SessionId(session_id)))
            if session_id else []
        )
        return public

    def append_message(
        self,
        identity: SessionIdentity,
        task_id: str,
        role: str,
        content: str,
        status: str = "recorded",
        *,
        message_id: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        if role not in {"user", "assistant", "system"}:
            raise ValidationError("Unsupported conversation role")
        content = content.strip()
        if not content or len(content) > 12000:
            raise ValidationError("Message must contain 1 to 12000 characters")
        message = {
            "message_id": str(message_id or uuid4()),
            "role": role,
            "content": content,
            "status": status,
            "created_at": str(created_at or datetime.now(timezone.utc).isoformat()),
        }

        task = self._get_raw(identity, task_id)
        session_id = SessionId(str(task["conversation_session_id"]))
        self._event_log.append(
            session_id,
            "conversation.message",
            {"message": message},
            event_id=f"message:{message['message_id']}",
            surface_operation="append",
        )

        def update(value: dict[str, Any]) -> dict[str, Any]:
            task = value.get(task_id)
            if not isinstance(task, dict):
                raise KeyError(task_id)
            if task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
                raise AuthorizationError("conversation.write", "task is not owned by current caller")
            task["message_count"] = len(transcript_messages(self._event_log.replay(session_id)))
            task["updated_at"] = message["created_at"]
            if status == "pending_model":
                task["status"] = "waiting_for_model"
            elif status in {
                "completed", "partial", "needs_input", "pending_approval", "blocked",
                "stopped_duplicate", "cancelled", "timed_out", "max_rounds", "unavailable",
            }:
                task["status"] = status
            return value

        self._state.update("tasks", update)
        return message

    def is_cancelled(self, identity: SessionIdentity, task_id: str) -> bool:
        return str(self.get(identity, task_id).get("status", "")) == "cancelled"

    def cancel(self, identity: SessionIdentity, task_id: str) -> dict[str, Any]:
        """Cancel future Agent decisions for one owned task.

        Cancellation is a task state only.  It never deletes an existing
        ModuleRun or changes a completed financial result.
        """

        def update(value: dict[str, Any]) -> dict[str, Any]:
            task = value.get(task_id)
            if not isinstance(task, dict):
                raise KeyError(task_id)
            if task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
                raise AuthorizationError("task.write", "task is not owned by current caller")
            task["status"] = "cancelled"
            task["updated_at"] = datetime.now(timezone.utc).isoformat()
            return value

        return self._public_task(self._state.update("tasks", update)[task_id])

    def append_run_ref(self, identity: SessionIdentity, task_id: str, reference: dict[str, str]) -> None:
        required = {
            "module", "tenant_id", "task_id", "run_id",
            "expected_semantic_result_hash", "expected_artifact_manifest_hash",
        }
        if set(reference) != required or reference["tenant_id"] != identity.tenant_id or reference["task_id"] != task_id:
            raise ValidationError("ModuleRunRef is invalid for this task")

        def update(value: dict[str, Any]) -> dict[str, Any]:
            task = value.get(task_id)
            if not isinstance(task, dict):
                raise KeyError(task_id)
            if task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
                raise AuthorizationError("task.write", "task is not owned by current caller")
            references = task.setdefault("run_refs", [])
            if not isinstance(references, list):
                raise ValidationError("Task run references are invalid")
            if reference not in references:
                references.append(reference)
            task["updated_at"] = datetime.now(timezone.utc).isoformat()
            return value

        self._state.update("tasks", update)

    def append_data_asset_ref(self, identity: SessionIdentity, task_id: str, reference: dict[str, Any]) -> None:
        """Attach one opaque DataAssetRef to its owning task context.

        The task index retains no physical storage path and no provider lineage;
        it only lets a later OptChat turn refer to the same controlled asset.
        """
        allowed = {"data_asset_id", "content_hash", "asset_ids", "schema_id", "coverage"}
        if not isinstance(reference, dict) or not isinstance(reference.get("data_asset_id"), str) or not isinstance(reference.get("content_hash"), str):
            raise ValidationError("DataAssetRef is invalid for this task")
        safe = {key: reference[key] for key in allowed if key in reference}

        def update(value: dict[str, Any]) -> dict[str, Any]:
            task = value.get(task_id)
            if not isinstance(task, dict):
                raise KeyError(task_id)
            if task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
                raise AuthorizationError("task.write", "task is not owned by current caller")
            refs = task.setdefault("data_asset_refs", [])
            if not isinstance(refs, list):
                raise ValidationError("Task data references are invalid")
            if safe not in refs:
                refs.append(safe)
            task["updated_at"] = datetime.now(timezone.utc).isoformat()
            return value

        self._state.update("tasks", update)

    def append_agent_event(self, identity: SessionIdentity, task_id: str, event: dict[str, Any]) -> None:
        """Compatibility entrypoint that appends to the child session log."""

        allowed = {
            "event", "timestamp", "workflow_run_id", "agent_run_id", "role", "preset_id", "preset_version",
            "model_selection", "model_selection_source", "input_hash", "output_hash", "status", "elapsed_seconds",
            "child_session_id", "seq", "parent_context_inherited", "one_shot", "provider_quiescent", "reason",
        }
        if not isinstance(event, dict) or set(event).difference(allowed):
            raise ValidationError("Agent event contains unsupported fields")
        name = str(event.get("event", "")).strip()
        workflow_run_id = str(event.get("workflow_run_id", "")).strip()
        if not name or len(name) > 80 or not workflow_run_id or len(workflow_run_id) > 96:
            raise ValidationError("Agent event is invalid")
        sequence = event.get("seq")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValidationError("Agent event sequence is invalid")
        for field in ("input_hash", "output_hash"):
            value = event.get(field)
            if value is not None and (not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value)):
                raise ValidationError("Agent event hash is invalid")
        selection = event.get("model_selection")
        if selection is not None:
            if not isinstance(selection, dict) or set(selection) != {"provider_id", "model_id"}:
                raise ValidationError("Agent event model selection is invalid")
            if any(not isinstance(value, str) or not value.strip() or len(value) > 160 for value in selection.values()):
                raise ValidationError("Agent event model selection is invalid")
        elapsed = event.get("elapsed_seconds")
        if elapsed is not None and (isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or elapsed < 0 or elapsed > 300):
            raise ValidationError("Agent event elapsed time is invalid")
        task = self._get_raw(identity, task_id)
        child_session_id = SessionId(str(event.get("child_session_id", "")).strip())
        if not str(child_session_id):
            raise ValidationError("Agent event child session is required")
        self._event_log.ensure_session(
            child_session_id,
            kind="child_agent",
            parent_session_id=SessionId(str(task["conversation_session_id"])),
            workflow_run_id=RunId(workflow_run_id),
            agent_run_id=RunId(str(event.get("agent_run_id", ""))),
        )
        safe = {
            key: copy.deepcopy(value)
            for key, value in event.items()
            if key not in {"event", "timestamp", "seq", "child_session_id"}
        }
        self._event_log.append(child_session_id, name, safe)

    def conversation_session_id(self, identity: SessionIdentity, task_id: str) -> SessionId:
        task = self._get_raw(identity, task_id)
        return SessionId(str(task["conversation_session_id"]))

    def model_surface(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        controlled_facts: dict[str, Any] | None = None,
    ) -> ModelVisibleSurface:
        session_id = self.conversation_session_id(identity, task_id)
        return self._projection_cache.project(
            session_id, controlled_facts=controlled_facts, mode="model",
        )

    def save_pending_recommendation(self, identity: SessionIdentity, task_id: str, value: dict[str, Any]) -> None:
        """Keep one private, non-financial candidate selection for a later confirmation.

        The task index never stores a ResolvedContract, module result, Provider
        response, or artifact path.  Those remain in their owning Stores.  This
        compact state merely prevents a natural-language confirmation from
        re-running Research/Critic and selecting a different candidate.
        """

        state = _pending_recommendation(value)

        def update(tasks: dict[str, Any]) -> dict[str, Any]:
            task = _owned_task(tasks, identity, task_id, "conversation.write")
            if any(
                row["tenant_id"] != identity.tenant_id or row["task_id"] != task_id
                for row in state["module_run_refs"]
            ):
                raise ValidationError("Recommendation continuation module_run_refs do not belong to this task")
            task["recommendation_state"] = state
            task["updated_at"] = datetime.now(timezone.utc).isoformat()
            return tasks

        self._state.update("tasks", update)

    def pending_recommendation(self, identity: SessionIdentity, task_id: str) -> dict[str, Any] | None:
        """Return the task-owned continuation state only to the App executor."""

        task = self._get_raw(identity, task_id)
        value = task.get("recommendation_state")
        if not isinstance(value, dict):
            return None
        try:
            state = _pending_recommendation(value, allow_approved=True)
        except ValidationError:
            # 旧版本没有候选版本、合同指纹等不可变绑定。保留原记录以便
            # 迁移审计，但绝不把它伪装为可续接的新状态，也不能静默重跑。
            return {"status": "invalid", "reason": "legacy_or_invalid_recommendation_state"}
        return copy.deepcopy(state) if state["status"] in {"pending_approval", "approval_prepared", "approved"} else None

    def prepare_pending_recommendation_approval(
        self, identity: SessionIdentity, task_id: str,
    ) -> dict[str, Any] | None:
        """Persist the durable half of a cross-store candidate confirmation.

        Contract activation lives in ``ContractStore`` and cannot share this
        document-store transaction.  We therefore never activate first: the
        task first records a resumable operation, then the caller activates the
        immutable CandidateVariant, and finally commits approval below.  A
        crash between those writes is recoverable by replaying the same
        operation, not by attempting a compensating contract rollback.
        """

        selected: dict[str, Any] | None = None

        def update(tasks: dict[str, Any]) -> dict[str, Any]:
            nonlocal selected
            task = _owned_task(tasks, identity, task_id, "conversation.write")
            raw = task.get("recommendation_state")
            if not isinstance(raw, dict):
                return tasks
            state = _pending_recommendation(raw, allow_approved=True)
            if state["status"] == "pending_approval":
                state["status"] = "approval_prepared"
                state["approval_operation_id"] = f"approval-{uuid4().hex}"
                state["approval_prepared_at"] = _utc_now()
                task["recommendation_state"] = state
                task["updated_at"] = state["approval_prepared_at"]
            if state["status"] in {"approval_prepared", "approved"}:
                selected = copy.deepcopy(state)
            return tasks

        self._state.update("tasks", update)
        return selected

    def commit_prepared_recommendation_approval(
        self, identity: SessionIdentity, task_id: str, operation_id: str,
    ) -> dict[str, Any] | None:
        """Commit an already prepared candidate approval idempotently."""

        operation_id = _safe_identifier(operation_id, "approval_operation_id", maximum=96)
        selected: dict[str, Any] | None = None

        def update(tasks: dict[str, Any]) -> dict[str, Any]:
            nonlocal selected
            task = _owned_task(tasks, identity, task_id, "conversation.write")
            raw = task.get("recommendation_state")
            if not isinstance(raw, dict):
                return tasks
            state = _pending_recommendation(raw, allow_approved=True)
            if state["status"] == "approval_prepared":
                if state.get("approval_operation_id") != operation_id:
                    raise ValidationError("Recommendation approval operation does not match")
                state["status"] = "approved"
                state["approved_at"] = _utc_now()
                task["recommendation_state"] = state
                task["updated_at"] = state["approved_at"]
            if state["status"] == "approved" and state.get("approval_operation_id") == operation_id:
                selected = copy.deepcopy(state)
            return tasks

        self._state.update("tasks", update)
        return selected

    def invalidate_prepared_recommendation_approval(
        self, identity: SessionIdentity, task_id: str, operation_id: str,
    ) -> None:
        """Close a prepared operation only when its CAS basis is no longer valid."""

        operation_id = _safe_identifier(operation_id, "approval_operation_id", maximum=96)

        def update(tasks: dict[str, Any]) -> dict[str, Any]:
            task = _owned_task(tasks, identity, task_id, "conversation.write")
            raw = task.get("recommendation_state")
            if not isinstance(raw, dict):
                return tasks
            state = _pending_recommendation(raw, allow_approved=True)
            if state["status"] in {"approval_prepared", "approved"} and state.get("approval_operation_id") == operation_id:
                state["status"] = "invalidated"
                state["invalidated_at"] = _utc_now()
                task["recommendation_state"] = state
                task["updated_at"] = state["invalidated_at"]
            return tasks

        self._state.update("tasks", update)

    def approve_pending_recommendation(self, identity: SessionIdentity, task_id: str) -> dict[str, Any] | None:
        """Compatibility helper for callers that do not activate a contract.

        Recommender confirmation must use prepare/activate/commit.  Keeping
        this method preserves the older TaskService API for isolated callers
        while using the same durable state shape.
        """

        prepared = self.prepare_pending_recommendation_approval(identity, task_id)
        if prepared is None:
            return None
        operation_id = prepared.get("approval_operation_id")
        if not isinstance(operation_id, str):
            return prepared if prepared.get("status") == "approved" else None
        return self.commit_prepared_recommendation_approval(identity, task_id, operation_id)

    def prepared_recommendation_approvals(self) -> list[tuple[SessionIdentity, str, dict[str, Any]]]:
        """Return durable confirmation operations needing cross-store recovery.

        This is an internal App recovery scan, not a browser-facing listing.
        Ownership checks in both TaskService and ContractStore still gate every
        individual write.  ``Role.ADMIN`` is only a recovery-session carrier:
        no role authorization is consulted by these owner-scoped Store calls.
        """

        recovered: list[tuple[SessionIdentity, str, dict[str, Any]]] = []
        for task_id, task in self._state.read("tasks").items():
            if not isinstance(task_id, str) or not isinstance(task, dict):
                continue
            tenant_id = task.get("tenant_id")
            principal_id = task.get("created_by")
            raw = task.get("recommendation_state")
            if not isinstance(tenant_id, str) or not tenant_id or not isinstance(principal_id, str) or not principal_id:
                continue
            if not isinstance(raw, dict):
                continue
            try:
                state = _pending_recommendation(raw, allow_approved=True)
            except ValidationError:
                continue
            if state["status"] not in {"approval_prepared", "approved"}:
                continue
            recovered.append((
                SessionIdentity(principal_id, tenant_id, Role.ADMIN, f"approval-recovery:{task_id}"),
                task_id,
                state,
            ))
        return recovered

    def choose_pending_recommendation_delivery(
        self,
        identity: SessionIdentity,
        task_id: str,
        delivery: dict[str, Any],
        *,
        confirmed_constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Bind one explicit, user-selected delivery to the stored candidate."""

        selected: dict[str, Any] | None = None
        normalized = _recommendation_delivery(delivery, required=True)
        assert normalized is not None

        def update(tasks: dict[str, Any]) -> dict[str, Any]:
            nonlocal selected
            task = _owned_task(tasks, identity, task_id, "conversation.write")
            raw = task.get("recommendation_state")
            if not isinstance(raw, dict):
                return tasks
            state = _pending_recommendation(raw, allow_approved=True)
            if state["status"] not in {"pending_approval", "approved"} or state["delivery"] is not None:
                return tasks
            if confirmed_constraints is not None:
                expected = _full_pending_constraints(state)
                if _canonical_value(confirmed_constraints) != _canonical_value(expected):
                    raise ValidationError("交付不得改写已确认候选的冻结条件")
            state["delivery"] = normalized
            task["recommendation_state"] = state
            task["updated_at"] = datetime.now(timezone.utc).isoformat()
            selected = copy.deepcopy(state)
            return tasks

        self._state.update("tasks", update)
        return selected

    def complete_pending_recommendation(self, identity: SessionIdentity, task_id: str) -> None:
        """Close a candidate continuation only after its delivery is committed."""

        def update(tasks: dict[str, Any]) -> dict[str, Any]:
            task = _owned_task(tasks, identity, task_id, "conversation.write")
            raw = task.get("recommendation_state")
            if not isinstance(raw, dict):
                return tasks
            state = _pending_recommendation(raw, allow_approved=True)
            if state["status"] != "approved":
                return tasks
            state["status"] = "completed"
            state["completed_at"] = datetime.now(timezone.utc).isoformat()
            task["recommendation_state"] = state
            task["updated_at"] = state["completed_at"]
            return tasks

        self._state.update("tasks", update)

    def get_conversation_response(
        self, identity: SessionIdentity, task_id: str, request_id: str | None, content: str,
    ) -> dict[str, Any] | None:
        """Return a prior response for an identical client retry.

        A request identifier is optional for backward-compatible local calls,
        but when supplied it is bound to the exact message body.  Reusing an
        identifier for different content is a client error rather than a new
        model invocation.
        """
        if request_id is None:
            return None
        request_id = _request_id(request_id)
        task = self._get_raw(identity, task_id)
        requests = task.get("conversation_requests", {})
        if not isinstance(requests, dict):
            raise ValidationError("Task conversation requests are invalid")
        record = requests.get(request_id)
        if record is None:
            return None
        if not isinstance(record, dict) or record.get("content_hash") != _content_hash(content):
            raise ValidationError("同一请求标识不能对应不同对话内容")
        status = record.get("status")
        response = record.get("recovery_response") if status == "recovery_required" else record.get("response")
        if status not in {None, "completed", "recovery_required"}:
            return None
        if not isinstance(response, dict):
            raise ValidationError("Task conversation replay is invalid")
        return copy.deepcopy(response)

    def get_conversation_response_by_id(
        self, identity: SessionIdentity, task_id: str, request_id: str,
    ) -> dict[str, Any] | None:
        """Read one caller-owned idempotency result without replaying the model.

        The response was already returned to the same authenticated caller when
        it was recorded.  This lookup lets a browser recover from a dropped
        response without guessing from message text or starting a second turn.
        """
        request_id = _request_id(request_id)
        task = self._get_raw(identity, task_id)
        requests = task.get("conversation_requests", {})
        if not isinstance(requests, dict):
            raise ValidationError("Task conversation requests are invalid")
        record = requests.get(request_id)
        if record is None:
            return None
        if not isinstance(record, dict):
            raise ValidationError("Task conversation replay is invalid")
        status = record.get("status")
        response = record.get("recovery_response") if status == "recovery_required" else record.get("response")
        if status not in {None, "completed", "recovery_required"}:
            return None
        if not isinstance(response, dict):
            raise ValidationError("Task conversation replay is invalid")
        return copy.deepcopy(response)

    def append_visible_process_event(
        self,
        identity: SessionIdentity,
        task_id: str,
        request_id: str | None,
        event_type: str,
        status: str,
        summary: str,
    ) -> dict[str, Any] | None:
        """Append one caller-visible, request-scoped execution event.

        The task/request envelope is the tenant, principal and task binding.
        The event payload itself therefore never copies opaque identifiers,
        model prompts, tool inputs or hidden reasoning into a browser-facing
        stream.
        """

        if request_id is None:
            return None
        normalized_id = _request_id(request_id)
        normalized_type = _visible_event_type(event_type)
        normalized_status = _visible_event_status(status)
        normalized_summary = _visible_event_summary(summary)
        appended: dict[str, Any] | None = None

        def update(tasks: dict[str, Any]) -> dict[str, Any]:
            nonlocal appended
            task = _owned_task(tasks, identity, task_id, "conversation.write")
            record = task.setdefault("conversation_requests", {}).get(normalized_id)
            if not isinstance(record, dict):
                raise ValidationError("Conversation request was not started")
            events = record.setdefault("visible_process_events", [])
            if not isinstance(events, list):
                raise ValidationError("Conversation process events are invalid")
            if len(events) >= 160:
                raise ValidationError("Conversation process event limit exceeded")
            appended = {
                "seq": len(events) + 1,
                "type": normalized_type,
                "status": normalized_status,
                "summary": normalized_summary,
                "created_at": _utc_now(),
            }
            events.append(appended)
            task["updated_at"] = appended["created_at"]
            return tasks

        self._state.update("tasks", update)
        return copy.deepcopy(appended)

    def visible_process_events(
        self,
        identity: SessionIdentity,
        task_id: str,
        request_id: str,
        *,
        after_seq: int = 0,
    ) -> dict[str, Any]:
        """Read one owned request's contiguous, caller-visible event suffix."""

        normalized_id = _request_id(request_id)
        if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
            raise ValidationError("Conversation process event cursor is invalid")
        task = self._get_raw(identity, task_id)
        requests = task.get("conversation_requests", {})
        if not isinstance(requests, dict):
            raise ValidationError("Task conversation requests are invalid")
        record = requests.get(normalized_id)
        if not isinstance(record, dict):
            raise KeyError(normalized_id)
        raw_events = record.get("visible_process_events", [])
        if not isinstance(raw_events, list):
            raise ValidationError("Conversation process events are invalid")
        events: list[dict[str, Any]] = []
        expected = 1
        for raw in raw_events:
            if not isinstance(raw, dict) or raw.get("seq") != expected:
                raise ValidationError("Conversation process event sequence is invalid")
            event = {
                "seq": expected,
                "type": _visible_event_type(raw.get("type")),
                "status": _visible_event_status(raw.get("status")),
                "summary": _visible_event_summary(raw.get("summary")),
                "created_at": _visible_event_time(raw.get("created_at")),
            }
            if expected > after_seq:
                events.append(event)
            expected += 1
        return {
            "events": events,
            "next_seq": expected - 1,
            "terminal": str(record.get("status", "")) in {"completed", "recovery_required"},
        }

    def record_conversation_response(
        self, identity: SessionIdentity, task_id: str, request_id: str | None, content: str, response: dict[str, Any],
    ) -> None:
        """Persist one safe response so a later HTTP retry cannot run again."""
        if request_id is None:
            return
        request_id = _request_id(request_id)
        if not isinstance(response, dict):
            raise ValidationError("Conversation response must be an object")
        content_hash = _content_hash(content)

        def update(value: dict[str, Any]) -> dict[str, Any]:
            task = value.get(task_id)
            if not isinstance(task, dict):
                raise KeyError(task_id)
            if task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
                raise AuthorizationError("conversation.write", "task is not owned by current caller")
            requests = task.setdefault("conversation_requests", {})
            if not isinstance(requests, dict):
                raise ValidationError("Task conversation requests are invalid")
            existing = requests.get(request_id)
            if existing is not None:
                if not isinstance(existing, dict) or existing.get("content_hash") != content_hash:
                    raise ValidationError("同一请求标识不能对应不同对话内容")
                return value
            requests[request_id] = {
                "content_hash": content_hash,
                "status": "completed",
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "response": copy.deepcopy(response),
            }
            task["updated_at"] = datetime.now(timezone.utc).isoformat()
            return value

        self._state.update("tasks", update)

    def begin_conversation_request(
        self,
        identity: SessionIdentity,
        task_id: str,
        request_id: str | None,
        content: str,
    ) -> dict[str, Any] | None:
        """Create or recover the durable state for one conversation request."""

        if request_id is None:
            return None
        normalized_id = _request_id(request_id)
        content_hash = _content_hash(content)
        recovered: dict[str, Any] | None = None

        def update(tasks: dict[str, Any]) -> dict[str, Any]:
            nonlocal recovered
            task = _owned_task(tasks, identity, task_id, "conversation.write")
            requests = task.setdefault("conversation_requests", {})
            if not isinstance(requests, dict):
                raise ValidationError("Task conversation requests are invalid")
            existing = requests.get(normalized_id)
            if existing is not None:
                if not isinstance(existing, dict) or existing.get("content_hash") != content_hash:
                    raise ValidationError("同一请求标识不能对应不同对话内容")
                if "status" not in existing and isinstance(existing.get("response"), dict):
                    existing["status"] = "completed"
                identity_prefix = hashlib.sha256(
                    f"{identity.tenant_id}\x1f{identity.principal_id}\x1f{task_id}\x1f{normalized_id}".encode("utf-8")
                ).hexdigest()[:24]
                existing.setdefault("execution_id", f"execution-{identity_prefix}")
                existing.setdefault("workflow_run_id", f"workflow-{identity_prefix}")
                existing.setdefault("step_id_namespace", f"step-{identity_prefix}")
                existing.setdefault("model_step_id", f"step-{identity_prefix}-model")
                existing.setdefault("recommender_step_id", f"step-{identity_prefix}-recommender")
                existing.setdefault("tool_step_id_namespace", f"step-{identity_prefix}-tool")
                existing.setdefault("request_id", normalized_id)
                recovered = copy.deepcopy(existing)
                return tasks
            now = _utc_now()
            identity_prefix = hashlib.sha256(
                f"{identity.tenant_id}\x1f{identity.principal_id}\x1f{task_id}\x1f{normalized_id}".encode("utf-8")
            ).hexdigest()[:24]
            record = {
                "request_id": normalized_id,
                "content_hash": content_hash,
                "status": "started",
                "started_at": now,
                "execution_id": f"execution-{identity_prefix}",
                "workflow_run_id": f"workflow-{identity_prefix}",
                "step_id_namespace": f"step-{identity_prefix}",
                "model_step_id": f"step-{identity_prefix}-model",
                "recommender_step_id": f"step-{identity_prefix}-recommender",
                "tool_step_id_namespace": f"step-{identity_prefix}-tool",
                "user_message_id": f"request-{normalized_id}-user",
                "assistant_message_id": f"request-{normalized_id}-assistant",
                "user_message_created_at": now,
                "visible_process_events": [{
                    "seq": 1,
                    "type": "request",
                    "status": "started",
                    "summary": "已接收请求，正在准备处理。",
                    "created_at": now,
                }],
            }
            requests[normalized_id] = record
            task["updated_at"] = now
            recovered = copy.deepcopy(record)
            return tasks

        self._state.update("tasks", update)
        assert recovered is not None
        return recovered

    def mark_conversation_execution_started(
        self,
        identity: SessionIdentity,
        task_id: str,
        request_id: str | None,
        content: str,
    ) -> dict[str, Any] | None:
        """Durably cross the boundary after which external work cannot be replayed."""

        if request_id is None:
            return None
        normalized_id = _request_id(request_id)
        content_hash = _content_hash(content)

        def update(tasks: dict[str, Any]) -> dict[str, Any]:
            task = _owned_task(tasks, identity, task_id, "conversation.write")
            record = task.setdefault("conversation_requests", {}).get(normalized_id)
            if not isinstance(record, dict) or record.get("content_hash") != content_hash:
                raise ValidationError("Conversation request was not started")
            if record.get("status") == "started":
                record["status"] = "execution_started"
                record["execution_started_at"] = _utc_now()
                task["updated_at"] = record["execution_started_at"]
            return tasks

        self._state.update("tasks", update)
        return self.begin_conversation_request(identity, task_id, normalized_id, content)

    def mark_conversation_recovery_required(
        self,
        identity: SessionIdentity,
        task_id: str,
        request_id: str,
        content: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a fail-loud outcome for an interrupted unsafe execution."""

        normalized_id = _request_id(request_id)
        content_hash = _content_hash(content)
        snapshot = copy.deepcopy(response)

        def update(tasks: dict[str, Any]) -> dict[str, Any]:
            task = _owned_task(tasks, identity, task_id, "conversation.write")
            record = task.setdefault("conversation_requests", {}).get(normalized_id)
            if not isinstance(record, dict) or record.get("content_hash") != content_hash:
                raise ValidationError("Conversation request was not started")
            if record.get("status") not in {"execution_started", "recovery_required"}:
                raise ValidationError("Conversation request is not awaiting recovery")
            existing = record.get("recovery_response")
            if existing is not None and existing != snapshot:
                raise ValidationError("Conversation recovery settlement conflict")
            record["status"] = "recovery_required"
            record["recovery_required_at"] = record.get("recovery_required_at") or _utc_now()
            record["recovery_response"] = snapshot
            task["updated_at"] = record["recovery_required_at"]
            return tasks

        self._state.update("tasks", update)
        recovered = self.begin_conversation_request(identity, task_id, normalized_id, content)
        assert recovered is not None
        return recovered

    def settle_conversation_model(
        self,
        identity: SessionIdentity,
        task_id: str,
        request_id: str | None,
        content: str,
        response: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Persist a safe model result before any assistant projection is appended."""

        if request_id is None:
            return None
        normalized_id = _request_id(request_id)
        content_hash = _content_hash(content)
        settled_at = _utc_now()
        snapshot = copy.deepcopy(response)

        def update(tasks: dict[str, Any]) -> dict[str, Any]:
            task = _owned_task(tasks, identity, task_id, "conversation.write")
            record = task.setdefault("conversation_requests", {}).get(normalized_id)
            if not isinstance(record, dict) or record.get("content_hash") != content_hash:
                raise ValidationError("Conversation request was not started")
            if record.get("status") == "completed":
                return tasks
            existing = record.get("model_response")
            if existing is not None and existing != snapshot:
                raise ValidationError("Conversation model settlement conflict")
            record["status"] = "model_settled"
            record["model_settled_at"] = settled_at
            record["assistant_message_created_at"] = record.get("assistant_message_created_at") or settled_at
            record["model_response"] = snapshot
            task["updated_at"] = settled_at
            return tasks

        self._state.update("tasks", update)
        return self.begin_conversation_request(identity, task_id, normalized_id, content)

    def complete_conversation_request(
        self,
        identity: SessionIdentity,
        task_id: str,
        request_id: str | None,
        content: str,
        response: dict[str, Any],
    ) -> None:
        if request_id is None:
            return
        normalized_id = _request_id(request_id)
        content_hash = _content_hash(content)

        def update(tasks: dict[str, Any]) -> dict[str, Any]:
            task = _owned_task(tasks, identity, task_id, "conversation.write")
            record = task.setdefault("conversation_requests", {}).get(normalized_id)
            if not isinstance(record, dict) or record.get("content_hash") != content_hash:
                raise ValidationError("Conversation request was not started")
            if record.get("status") == "completed":
                if record.get("response") != response:
                    raise ValidationError("Conversation completion conflict")
                return tasks
            if record.get("status") != "model_settled":
                raise ValidationError("Conversation model result is not settled")
            record["status"] = "completed"
            record["recorded_at"] = _utc_now()
            record["response"] = copy.deepcopy(response)
            record.pop("model_response", None)
            task["updated_at"] = record["recorded_at"]
            return tasks

        self._state.update("tasks", update)

def _request_id(value: object) -> str:
    request_id = str(value or "").strip()
    if not request_id or len(request_id) > 128:
        raise ValidationError("Conversation request id is invalid")
    return request_id


_VISIBLE_PROCESS_EVENT_TYPES = frozenset({
    "request", "routing", "agent_run", "host_module", "candidate_cycle", "answer", "terminal",
})
_VISIBLE_PROCESS_EVENT_STATUSES = frozenset({
    "started", "completed", "reselecting", "failed", "cancelled",
})
_VISIBLE_PROCESS_FORBIDDEN = re.compile(
    r"(?i)(reasoning|chain[_ -]?of[_ -]?thought|system[_ -]?prompt|prompt|secret|api[_ -]?key|"
    r"access[_ -]?token|refresh[_ -]?token|bearer|runref|module[_ -]?run[_ -]?ref|"
    r"candidate(?:[_ -]?(?:id|key|version)|-[A-Za-z0-9][A-Za-z0-9_-]*)|"
    r"(?:contract[_ -]?)?fingerprint|tool[_ -]?arguments?|arguments?|"
    r"task[_ -]?id|request[_ -]?id|tenant[_ -]?id|principal[_ -]?id|[a-f0-9]{24,})"
)


def _visible_event_type(value: object) -> str:
    event_type = str(value or "").strip()
    if event_type not in _VISIBLE_PROCESS_EVENT_TYPES:
        raise ValidationError("Conversation process event type is invalid")
    return event_type


def _visible_event_status(value: object) -> str:
    status = str(value or "").strip()
    if status not in _VISIBLE_PROCESS_EVENT_STATUSES:
        raise ValidationError("Conversation process event status is invalid")
    return status


def _visible_event_summary(value: object) -> str:
    summary = str(value or "").strip()
    if not summary or len(summary) > 120 or "\n" in summary or "\r" in summary:
        raise ValidationError("Conversation process event summary is invalid")
    if _VISIBLE_PROCESS_FORBIDDEN.search(summary):
        raise ValidationError("Conversation process event summary contains internal content")
    return summary


def _visible_event_time(value: object) -> str:
    timestamp = str(value or "").strip()
    if not timestamp or len(timestamp) > 64:
        raise ValidationError("Conversation process event timestamp is invalid")
    return timestamp


def _content_hash(content: object) -> str:
    return hashlib.sha256(str(content or "").strip().encode("utf-8")).hexdigest()


def _owned_task(tasks: dict[str, Any], identity: SessionIdentity, task_id: str, capability: str) -> dict[str, Any]:
    task = tasks.get(task_id)
    if not isinstance(task, dict):
        raise KeyError(task_id)
    if task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
        raise AuthorizationError(capability, "task is not owned by current caller")
    return task


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pending_recommendation(value: dict[str, Any], *, allow_approved: bool = False) -> dict[str, Any]:
    """Validate the only continuation payload the Task index is allowed to retain."""

    allowed = {
        "status", "analysis_case_id", "catalog_version", "confirmed_constraints", "delivery", "candidate",
        "workflow_mode", "approved_at", "completed_at", "state_schema", "preset", "candidate_version",
        "contract_fingerprint", "term_overrides", "term_overrides_fingerprint", "ranking_spec_id", "ranking_spec", "module_run_refs",
        "ranking_spec_fingerprint", "expected_active_contract_fingerprint", "approval_operation_id", "approval_prepared_at",
        "invalidated_at",
    }
    if set(value).difference(allowed):
        raise ValidationError("Recommendation continuation contains unsupported fields")
    status = str(value.get("status", "")).strip()
    permitted = {"pending_approval"}
    if allow_approved:
        permitted.update({"approval_prepared", "approved", "completed", "invalidated"})
    if status not in permitted:
        raise ValidationError("Recommendation continuation status is invalid")
    if value.get("state_schema") != "exact-candidate-v1":
        raise ValidationError("Recommendation continuation is not an exact candidate version")
    text_fields = ("analysis_case_id", "catalog_version", "contract_fingerprint", "term_overrides_fingerprint")
    result: dict[str, Any] = {"status": status, "state_schema": "exact-candidate-v1"}
    for field in text_fields:
        text = str(value.get(field, "")).strip()
        if not text or len(text) > 160:
            raise ValidationError(f"Recommendation continuation {field} is invalid")
        if field in {"contract_fingerprint", "term_overrides_fingerprint"} and not _sha256(text):
            raise ValidationError(f"Recommendation continuation {field} is invalid")
        result[field] = text
    expected_active = value.get("expected_active_contract_fingerprint")
    if expected_active is not None:
        expected_active = str(expected_active).strip()
        if not _sha256(expected_active):
            raise ValidationError("Recommendation continuation expected_active_contract_fingerprint is invalid")
    result["expected_active_contract_fingerprint"] = expected_active
    preset = value.get("preset")
    if not isinstance(preset, dict) or set(preset) != {"preset_id", "preset_version"}:
        raise ValidationError("Recommendation continuation preset is invalid")
    preset_id = _safe_identifier(preset.get("preset_id"), "preset_id", maximum=80)
    preset_version = _safe_identifier(preset.get("preset_version"), "preset_version", maximum=80)
    result["preset"] = {"preset_id": preset_id, "preset_version": preset_version}
    workflow_mode = str(value.get("workflow_mode", "")).strip()
    if workflow_mode not in {"single_agent", "multi_agent", "degraded_single_agent"}:
        raise ValidationError("Recommendation continuation workflow_mode is invalid")
    result["workflow_mode"] = workflow_mode
    constraints = value.get("confirmed_constraints")
    if not isinstance(constraints, dict) or set(constraints).difference({
        "underlying", "horizon", "market_view", "max_loss", "principal_fluctuation", "output_type", "format",
    }):
        raise ValidationError("Recommendation continuation constraints are invalid")
    result["confirmed_constraints"] = copy.deepcopy(constraints)
    term_overrides = _term_overrides(value.get("term_overrides"))
    if _term_overrides_fingerprint(term_overrides) != result["term_overrides_fingerprint"]:
        raise ValidationError("Recommendation continuation term_overrides fingerprint conflicts")
    result["term_overrides"] = term_overrides
    delivery = _recommendation_delivery(value.get("delivery"), required=False)
    result["delivery"] = delivery
    candidate = value.get("candidate")
    if not isinstance(candidate, dict) or set(candidate).difference({
        "candidate_id", "product_id", "product_name", "underlyings", "library_status",
    }):
        raise ValidationError("Recommendation continuation candidate is invalid")
    candidate_id = str(candidate.get("candidate_id", "")).strip()
    product_id = str(candidate.get("product_id", "")).strip()
    product_name = str(candidate.get("product_name", "")).strip()
    underlyings = candidate.get("underlyings")
    if (
        not candidate_id or len(candidate_id) > 160 or not product_id or len(product_id) > 80
        or len(product_name) > 160 or not isinstance(underlyings, list) or not underlyings
        or any(not isinstance(item, str) or not item.strip() or len(item) > 80 for item in underlyings)
        or candidate.get("library_status") != "ready"
    ):
        raise ValidationError("Recommendation continuation candidate is invalid")
    result["candidate"] = {
        "candidate_id": candidate_id,
        "product_id": product_id,
        "product_name": product_name,
        "underlyings": [item.strip() for item in underlyings],
        "library_status": "ready",
    }
    version = value.get("candidate_version")
    if not isinstance(version, dict) or set(version) != {
        "candidate_key", "candidate_version_id", "parent_candidate_version_id", "revision",
    }:
        raise ValidationError("Recommendation continuation candidate_version is invalid")
    candidate_key = _safe_identifier(version.get("candidate_key"), "candidate_key", maximum=160)
    candidate_version_id = _safe_identifier(version.get("candidate_version_id"), "candidate_version_id", maximum=160)
    parent_raw = version.get("parent_candidate_version_id")
    parent = None if parent_raw is None else _safe_identifier(parent_raw, "parent_candidate_version_id", maximum=160)
    revision = version.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1 or revision > 100:
        raise ValidationError("Recommendation continuation candidate_version revision is invalid")
    result["candidate_version"] = {
        "candidate_key": candidate_key,
        "candidate_version_id": candidate_version_id,
        "parent_candidate_version_id": parent,
        "revision": revision,
    }
    ranking_spec_id = value.get("ranking_spec_id")
    if ranking_spec_id is None:
        result["ranking_spec_id"] = None
        if value.get("ranking_spec_fingerprint") is not None:
            raise ValidationError("Recommendation continuation ranking_spec_fingerprint conflicts")
        result["ranking_spec_fingerprint"] = None
        if value.get("ranking_spec") is not None:
            raise ValidationError("Recommendation continuation ranking_spec conflicts")
        result["ranking_spec"] = None
    else:
        result["ranking_spec_id"] = _safe_identifier(ranking_spec_id, "ranking_spec_id", maximum=160)
        ranking_spec_fingerprint = str(value.get("ranking_spec_fingerprint", "")).strip()
        if not _sha256(ranking_spec_fingerprint):
            raise ValidationError("Recommendation continuation ranking_spec_fingerprint is invalid")
        if not result["ranking_spec_id"].endswith(f".{ranking_spec_fingerprint}"):
            raise ValidationError("Recommendation continuation ranking_spec_id is not content-bound")
        result["ranking_spec_fingerprint"] = ranking_spec_fingerprint
        ranking_spec = _pending_ranking_spec(value.get("ranking_spec"))
        if (
            ranking_spec["ranking_spec_id"] != result["ranking_spec_id"]
            or ranking_spec["ranking_spec_fingerprint"] != ranking_spec_fingerprint
        ):
            raise ValidationError("Recommendation continuation ranking_spec conflicts")
        result["ranking_spec"] = ranking_spec
    if result["preset"]["preset_id"] == "constraint-ranking" and result["ranking_spec_id"] is None:
        raise ValidationError("Constraint-ranking continuation requires a content-bound RankingSpec")
    if status in {"approval_prepared", "approved"}:
        operation_id = _safe_identifier(value.get("approval_operation_id"), "approval_operation_id", maximum=96)
        result["approval_operation_id"] = operation_id
        prepared_at = str(value.get("approval_prepared_at", "")).strip()
        if not prepared_at or len(prepared_at) > 80:
            raise ValidationError("Recommendation continuation approval_prepared_at is invalid")
        result["approval_prepared_at"] = prepared_at
    if status == "invalidated":
        invalidated_at = str(value.get("invalidated_at", "")).strip()
        if not invalidated_at or len(invalidated_at) > 80:
            raise ValidationError("Recommendation continuation invalidated_at is invalid")
        result["invalidated_at"] = invalidated_at
    run_refs = _pending_module_run_refs(value.get("module_run_refs"))
    result["module_run_refs"] = run_refs
    for field in ("approved_at", "completed_at"):
        if field in value:
            timestamp = str(value[field]).strip()
            if not timestamp or len(timestamp) > 80:
                raise ValidationError("Recommendation continuation timestamp is invalid")
            result[field] = timestamp
    return result


def _canonical_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _pending_ranking_spec(value: object) -> dict[str, Any]:
    """Validate the normalized deterministic RankingSpec persisted for recovery."""

    if not isinstance(value, dict) or set(value) != {
        "ranking_spec_id", "ranking_spec_fingerprint", "hard_constraints", "sort_keys", "tie_break_policy",
    }:
        raise ValidationError("Recommendation continuation ranking_spec is invalid")
    ranking_spec_id = _safe_identifier(value.get("ranking_spec_id"), "ranking_spec_id", maximum=160)
    fingerprint = str(value.get("ranking_spec_fingerprint", "")).strip()
    constraints = value.get("hard_constraints")
    sort_keys = value.get("sort_keys")
    if (
        not _sha256(fingerprint)
        or not ranking_spec_id.endswith(f".{fingerprint}")
        or not isinstance(constraints, dict)
        or isinstance(sort_keys, (str, bytes))
        or not isinstance(sort_keys, list)
        or not sort_keys
        or value.get("tie_break_policy") != "candidate_key"
    ):
        raise ValidationError("Recommendation continuation ranking_spec is invalid")
    normalized_keys: list[dict[str, str]] = []
    for item in sort_keys:
        if not isinstance(item, dict) or set(item) != {"metric", "direction", "missing_policy"}:
            raise ValidationError("Recommendation continuation ranking_spec is invalid")
        metric = _safe_identifier(item.get("metric"), "ranking_metric", maximum=80)
        direction = item.get("direction")
        missing_policy = item.get("missing_policy")
        if direction not in {"asc", "desc"} or missing_policy not in {"exclude", "first", "last"}:
            raise ValidationError("Recommendation continuation ranking_spec is invalid")
        normalized_keys.append({"metric": metric, "direction": direction, "missing_policy": missing_policy})
    if len({item["metric"] for item in normalized_keys}) != len(normalized_keys):
        raise ValidationError("Recommendation continuation ranking_spec is invalid")
    try:
        normalized_constraints = json.loads(json.dumps(constraints, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError) as error:
        raise ValidationError("Recommendation continuation ranking_spec is invalid") from error
    canonical = json.dumps(
        {
            "hard_constraints": normalized_constraints,
            "sort_keys": normalized_keys,
            "tie_break_policy": "candidate_key",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != fingerprint:
        raise ValidationError("Recommendation continuation ranking_spec fingerprint conflicts")
    return {
        "ranking_spec_id": ranking_spec_id,
        "ranking_spec_fingerprint": fingerprint,
        "hard_constraints": normalized_constraints,
        "sort_keys": normalized_keys,
        "tie_break_policy": "candidate_key",
    }


def _safe_identifier(value: object, field_name: str, *, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", text):
        raise ValidationError(f"Recommendation continuation {field_name} is invalid")
    return text


def _term_overrides(value: object) -> dict[str, str | int | float]:
    if not isinstance(value, dict) or len(value) > 64:
        raise ValidationError("Recommendation continuation term_overrides are invalid")
    result: dict[str, str | int | float] = {}
    for raw_key, raw_value in value.items():
        key = _safe_identifier(raw_key, "term_overrides key", maximum=80)
        if isinstance(raw_value, bool) or not isinstance(raw_value, (str, int, float)):
            raise ValidationError("Recommendation continuation term_overrides are invalid")
        if isinstance(raw_value, str) and (not raw_value.strip() or len(raw_value) > 160):
            raise ValidationError("Recommendation continuation term_overrides are invalid")
        result[key] = raw_value.strip() if isinstance(raw_value, str) else raw_value
    return dict(sorted(result.items()))


def _term_overrides_fingerprint(value: dict[str, str | int | float]) -> str:
    return hashlib.sha256(_canonical_value(value).encode("utf-8")).hexdigest()


def _pending_module_run_refs(value: object) -> list[dict[str, str]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, list) or len(value) > 16:
        raise ValidationError("Recommendation continuation module_run_refs are invalid")
    fields = {
        "module", "tenant_id", "task_id", "run_id",
        "expected_semantic_result_hash", "expected_artifact_manifest_hash",
    }
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != fields:
            raise ValidationError("Recommendation continuation module_run_refs are invalid")
        row = {field: str(item.get(field, "")).strip() for field in fields}
        if (
            row["module"] not in {"payoffer", "pricer", "backtester"}
            or any(not row[field] or len(row[field]) > 160 for field in fields)
            or not _sha256(row["expected_semantic_result_hash"])
            or not _sha256(row["expected_artifact_manifest_hash"])
        ):
            raise ValidationError("Recommendation continuation module_run_refs are invalid")
        identity = (row["module"], row["tenant_id"], row["task_id"], row["run_id"])
        if identity in seen:
            raise ValidationError("Recommendation continuation module_run_refs are duplicated")
        seen.add(identity)
        result.append(row)
    return sorted(result, key=lambda item: (item["module"], item["run_id"]))


def _full_pending_constraints(state: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(state["confirmed_constraints"])
    result["term_overrides"] = copy.deepcopy(state["term_overrides"])
    return result


def _recommendation_delivery(value: object, *, required: bool) -> dict[str, Any] | None:
    """Validate a delivery only when the user has actually chosen one."""

    if value is None and not required:
        return None
    if not isinstance(value, dict) or set(value) != {"kind", "format"}:
        raise ValidationError("Recommendation continuation delivery is invalid")
    kind = str(value.get("kind", "")).lower()
    output_format = str(value.get("format", "")).lower()
    if kind not in {"card", "quote", "report"} or output_format not in {"html", "pdf"}:
        raise ValidationError("Recommendation continuation delivery is invalid")
    return {"kind": kind, "format": output_format}
