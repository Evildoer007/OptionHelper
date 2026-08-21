"""Persistent App task and conversation state.

Tasks are App indices only.  They can retain messages and references, but never
copy Capability financial results or become a second result store.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..errors import AuthorizationError, ValidationError
from ..identity.session_identity import SessionIdentity
from ..stores import _LocalDocumentStore


class TaskService:
    def __init__(self, state: _LocalDocumentStore) -> None:
        self._state = state

    def create(self, identity: SessionIdentity, subject: str) -> dict[str, Any]:
        subject = subject.strip()
        if not subject or len(subject) > 160:
            raise ValidationError("Task subject must contain 1 to 160 characters")
        now = datetime.now(timezone.utc).isoformat()
        task = {
            "task_id": str(uuid4()),
            "tenant_id": identity.tenant_id,
            "created_by": identity.principal_id,
            "subject": subject,
            "status": "created",
            "created_at": now,
            "updated_at": now,
            "messages": [],
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
        return _public_task(task)

    def list(self, identity: SessionIdentity) -> list[dict[str, Any]]:
        tasks = self._state.read("tasks").values()
        result = [task for task in tasks if isinstance(task, dict) and task.get("tenant_id") == identity.tenant_id and task.get("created_by") == identity.principal_id]
        return [_public_task(task) for task in sorted(result, key=lambda task: str(task.get("updated_at", "")), reverse=True)]

    def get(self, identity: SessionIdentity, task_id: str) -> dict[str, Any]:
        return _public_task(self._get_raw(identity, task_id))

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

        return _public_task(self._state.update("tasks", update)[task_id])

    def delete(self, identity: SessionIdentity, task_id: str) -> dict[str, Any]:
        """Remove one owned App task index and its conversation history.

        Capability-owned module results, reports, and data assets deliberately
        remain outside this index and are never deleted by this operation.
        """

        removed: dict[str, Any] | None = None

        def update(value: dict[str, Any]) -> dict[str, Any]:
            nonlocal removed
            task = value.get(task_id)
            if not isinstance(task, dict):
                raise KeyError(task_id)
            if task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
                raise AuthorizationError("conversation.write", "task is not owned by current caller")
            removed = value.pop(task_id)
            return value

        self._state.update("tasks", update)
        assert removed is not None
        return _public_task(removed)

    def _get_raw(self, identity: SessionIdentity, task_id: str) -> dict[str, Any]:
        task = self._state.read("tasks").get(task_id)
        if not isinstance(task, dict):
            raise KeyError(task_id)
        if task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
            raise AuthorizationError("task.read", "task is not owned by current caller")
        return task

    def append_message(self, identity: SessionIdentity, task_id: str, role: str, content: str, status: str = "recorded") -> dict[str, Any]:
        if role not in {"user", "assistant", "system"}:
            raise ValidationError("Unsupported conversation role")
        content = content.strip()
        if not content or len(content) > 12000:
            raise ValidationError("Message must contain 1 to 12000 characters")
        message = {
            "message_id": str(uuid4()),
            "role": role,
            "content": content,
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        def update(value: dict[str, Any]) -> dict[str, Any]:
            task = value.get(task_id)
            if not isinstance(task, dict):
                raise KeyError(task_id)
            if task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
                raise AuthorizationError("conversation.write", "task is not owned by current caller")
            messages = task.setdefault("messages", [])
            if not isinstance(messages, list):
                raise ValidationError("Task messages are invalid")
            messages.append(message)
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

        return _public_task(self._state.update("tasks", update)[task_id])

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
            # A malformed private state is not execution authority.  Treat it
            # as absent rather than attempting a best-effort continuation.
            return None
        return copy.deepcopy(state) if state["status"] in {"pending_approval", "approved"} else None

    def approve_pending_recommendation(self, identity: SessionIdentity, task_id: str) -> dict[str, Any] | None:
        """Atomically persist an explicit candidate confirmation.

        Choosing a product structure and choosing a deliverable are separate
        customer decisions.  An approved state may therefore have no delivery
        yet; it is never computation authority until one is selected.
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
                state["status"] = "approved"
                state["approved_at"] = datetime.now(timezone.utc).isoformat()
                task["recommendation_state"] = state
                task["updated_at"] = state["approved_at"]
            if state["status"] == "approved":
                selected = copy.deepcopy(state)
            return tasks

        self._state.update("tasks", update)
        return selected

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
                state["confirmed_constraints"] = copy.deepcopy(confirmed_constraints)
                state = _pending_recommendation(state, allow_approved=True)
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
        response = record.get("response")
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
        response = record.get("response")
        if not isinstance(response, dict):
            raise ValidationError("Task conversation replay is invalid")
        return copy.deepcopy(response)

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
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "response": copy.deepcopy(response),
            }
            task["updated_at"] = datetime.now(timezone.utc).isoformat()
            return value

        self._state.update("tasks", update)

def _request_id(value: object) -> str:
    request_id = str(value or "").strip()
    if not request_id or len(request_id) > 128:
        raise ValidationError("Conversation request id is invalid")
    return request_id


def _content_hash(content: object) -> str:
    return hashlib.sha256(str(content or "").strip().encode("utf-8")).hexdigest()


def _public_task(value: dict[str, Any]) -> dict[str, Any]:
    """Never return the private retry journal from task read/list endpoints."""
    return {
        key: copy.deepcopy(item)
        for key, item in value.items()
        if key not in {"conversation_requests", "recommendation_state"}
    }


def _owned_task(tasks: dict[str, Any], identity: SessionIdentity, task_id: str, capability: str) -> dict[str, Any]:
    task = tasks.get(task_id)
    if not isinstance(task, dict):
        raise KeyError(task_id)
    if task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
        raise AuthorizationError(capability, "task is not owned by current caller")
    return task


def _pending_recommendation(value: dict[str, Any], *, allow_approved: bool = False) -> dict[str, Any]:
    """Validate the only continuation payload the Task index is allowed to retain."""

    allowed = {
        "status", "analysis_case_id", "catalog_version", "confirmed_constraints", "delivery", "candidate",
        "workflow_mode", "approved_at", "completed_at",
    }
    if set(value).difference(allowed):
        raise ValidationError("Recommendation continuation contains unsupported fields")
    status = str(value.get("status", "")).strip()
    permitted = {"pending_approval"}
    if allow_approved:
        permitted.update({"approved", "completed"})
    if status not in permitted:
        raise ValidationError("Recommendation continuation status is invalid")
    text_fields = ("analysis_case_id", "catalog_version")
    result: dict[str, Any] = {"status": status}
    for field in text_fields:
        text = str(value.get(field, "")).strip()
        if not text or len(text) > 160:
            raise ValidationError(f"Recommendation continuation {field} is invalid")
        result[field] = text
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
    for field in ("approved_at", "completed_at"):
        if field in value:
            timestamp = str(value[field]).strip()
            if not timestamp or len(timestamp) > 80:
                raise ValidationError("Recommendation continuation timestamp is invalid")
            result[field] = timestamp
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
