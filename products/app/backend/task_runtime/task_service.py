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
from typing import Any, Mapping
from uuid import uuid4

from ..attachments import AttachmentStore
from ..errors import AuthorizationError, ValidationError
from ..identity.session_identity import SessionIdentity
from ..stores import _LocalDocumentStore
from ..agent_runtime.session_context import (
    ModelVisibleSurface,
    RunId,
    SessionEventLog,
    SessionId,
    transcript_messages,
)
from ..agent_runtime.projection_cache import ProjectionCache
from ..agent_runtime.redaction import redact_text


PENDING_RECOMMENDATION_SCHEMA_ID = "optionhelper.pending-recommendation"


class TaskService:
    def __init__(self, state: _LocalDocumentStore, event_log: SessionEventLog | None = None) -> None:
        self._state = state
        self._event_log = event_log or SessionEventLog(state._root / "session-events.sqlite3")
        self._projection_cache = ProjectionCache(self._event_log)
        self._attachments = AttachmentStore(state._root / "attachments")

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
            # A bounded, private idempotency journal.  It records only a
            # completed OptChat response projection, never provider payloads
            # or hidden model reasoning.
            "conversation_requests": {},
            # Upload grants are private and task-scoped.  A content-addressed
            # attachment cannot be attached to a message merely by knowing its
            # global hash; it must first have been uploaded for this Task.
            "pending_attachment_uploads": {},
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
        self._reclaim_orphaned_attachments()
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
                "pending_attachment_uploads", "data_asset_refs",
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
        content_blocks: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if role not in {"user", "assistant", "system"}:
            raise ValidationError("Unsupported conversation role")
        content = content.strip()
        safe_attachments = _validated_attachment_references(attachments or [])
        if (not content and not safe_attachments) or len(content) > 12000:
            raise ValidationError("Message must contain text or attachments")
        message = {
            "message_id": str(message_id or uuid4()),
            "role": role,
            "content": content,
            "status": status,
            "created_at": str(created_at or datetime.now(timezone.utc).isoformat()),
        }
        if safe_attachments:
            message["attachments"] = safe_attachments
            message["content_blocks"] = [
                *([{"type": "text", "text": content}] if content else []),
                *[
                    {
                        "type": "image" if item["kind"] == "image" else "document-ref",
                        "attachment_id": item["attachment_id"],
                        "media_type": item["media_type"],
                        "name": item.get("name", ""),
                    }
                    for item in safe_attachments
                ],
            ]
        if role == "assistant" and content_blocks:
            safe_blocks: list[dict[str, Any]] = []
            for block in content_blocks:
                if not isinstance(block, Mapping):
                    continue
                block_type = str(block.get("type", "")).replace("_", "-")
                block_text = str(block.get("text", ""))
                if block_type == "document":
                    report_id = str(block.get("report_run_id", ""))
                    if not report_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for c in report_id):
                        continue
                    safe_blocks.append({key: str(block.get(key, "")) for key in ("type", "report_run_id", "title", "format", "preview_url", "download_url")})
                    continue
                if block_type == "question":
                    question = _validated_question_block(block)
                    # A repeated prompt is a new question in a new assistant message.
                    # Keep the identity stable when the same message is replayed.
                    question["question_id"] = hashlib.sha256(
                        f"{message['message_id']}\x1f{question['question_id']}".encode("utf-8")
                    ).hexdigest()[:24]
                    safe_blocks.append(question)
                    continue
                if block_type not in {"text", "reasoning"} or not block_text:
                    continue
                limit = 64_000 if block_type == "reasoning" else 12_000
                safe_blocks.append({"type": block_type, "text": block_text[:limit]})
            if safe_blocks:
                message["content_blocks"] = safe_blocks

        task = self._get_raw(identity, task_id)
        session_id = SessionId(str(task["conversation_session_id"]))
        prior_messages = transcript_messages(self._event_log.replay(session_id))
        answered_question_id = _latest_open_question_id(prior_messages) if role == "user" else None
        if role == "user" and safe_attachments:
            self._require_pending_attachment_uploads(task, session_id, safe_attachments)
        self._event_log.append(
            session_id,
            "conversation.message",
            {"message": message},
            event_id=f"message:{message['message_id']}",
            surface_operation="append",
        )
        question = next(
            (
                block for block in message.get("content_blocks", [])
                if isinstance(block, Mapping) and block.get("type") == "question"
            ),
            None,
        )
        if isinstance(question, Mapping):
            self._event_log.append(
                session_id,
                "user_question.requested",
                {"question": dict(question), "message_id": message["message_id"]},
                event_id=f"question:{question['question_id']}:requested",
                ignorable=True,
            )
        elif answered_question_id is not None:
            self._event_log.append(
                session_id,
                "user_question.answered",
                {"question_id": answered_question_id, "message_id": message["message_id"]},
                event_id=f"question:{answered_question_id}:answered:{message['message_id']}",
                ignorable=True,
            )

        def update(value: dict[str, Any]) -> dict[str, Any]:
            task = value.get(task_id)
            if not isinstance(task, dict):
                raise KeyError(task_id)
            if task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
                raise AuthorizationError("conversation.write", "task is not owned by current caller")
            task["message_count"] = len(transcript_messages(self._event_log.replay(session_id)))
            task["updated_at"] = message["created_at"]
            if role == "user" and safe_attachments:
                pending = task.setdefault("pending_attachment_uploads", {})
                if not isinstance(pending, dict):
                    raise ValidationError("Task attachment upload state is invalid")
                for reference in safe_attachments:
                    pending.pop(reference["attachment_id"], None)
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

    def register_attachment_uploads(
        self,
        identity: SessionIdentity,
        task_id: str,
        references: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Authorize immutable attachment references for one future Task message."""

        safe_references = _validated_attachment_references(references)
        if not safe_references:
            raise ValidationError("Attachment upload did not produce any references")
        uploaded_at = _utc_now()

        def update(value: dict[str, Any]) -> dict[str, Any]:
            task = _owned_task(value, identity, task_id, "conversation.write")
            pending = task.setdefault("pending_attachment_uploads", {})
            if not isinstance(pending, dict):
                raise ValidationError("Task attachment upload state is invalid")
            for reference in safe_references:
                pending[reference["attachment_id"]] = {
                    "reference": copy.deepcopy(reference),
                    "uploaded_at": uploaded_at,
                }
            if len(pending) > 100:
                ordered = sorted(
                    pending.items(),
                    key=lambda item: str(item[1].get("uploaded_at", ""))
                    if isinstance(item[1], Mapping) else "",
                )
                for attachment_id, _ in ordered[:len(pending) - 100]:
                    pending.pop(attachment_id, None)
            task["updated_at"] = uploaded_at
            return value

        self._state.update("tasks", update)
        self._reclaim_orphaned_attachments()
        return safe_references

    def pending_attachment_uploads(
        self,
        identity: SessionIdentity,
        task_id: str,
    ) -> list[dict[str, Any]]:
        task = _owned_task(self._state.read("tasks"), identity, task_id, "conversation.read")
        pending = task.get("pending_attachment_uploads", {})
        if not isinstance(pending, Mapping):
            raise ValidationError("Task attachment upload state is invalid")
        ordered = sorted(
            (item for item in pending.values() if isinstance(item, Mapping)),
            key=lambda item: str(item.get("uploaded_at", "")),
        )
        return _validated_attachment_references([
            dict(item["reference"])
            for item in ordered
            if isinstance(item.get("reference"), Mapping)
        ])

    def discard_attachment_uploads(
        self,
        identity: SessionIdentity,
        task_id: str,
        attachment_ids: list[str],
    ) -> int:
        if not attachment_ids or len(attachment_ids) > 20 or any(
            not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value)
            for value in attachment_ids
        ):
            raise ValidationError("Attachment draft identifiers are invalid")
        removed = 0

        def update(value: dict[str, Any]) -> dict[str, Any]:
            nonlocal removed
            task = _owned_task(value, identity, task_id, "conversation.write")
            pending = task.setdefault("pending_attachment_uploads", {})
            if not isinstance(pending, dict):
                raise ValidationError("Task attachment upload state is invalid")
            for attachment_id in dict.fromkeys(attachment_ids):
                if pending.pop(attachment_id, None) is not None:
                    removed += 1
            task["updated_at"] = _utc_now()
            return value

        self._state.update("tasks", update)
        self._reclaim_orphaned_attachments()
        return removed

    def _reclaim_orphaned_attachments(self) -> None:
        """Collect only after marking every surviving Task reference as live."""

        try:
            self._attachments.collect_orphans(self._live_attachment_ids())
        except (OSError, ValidationError, ValueError):
            # Disk governance is best effort. If durable Task/event state is
            # unavailable or malformed, preserve every attachment for a later
            # successful audit rather than making an incomplete mark decision.
            return

    def reclaim_orphaned_attachments(self) -> None:
        """Run the conservative mark-and-sweep before accepting more bytes."""

        self._reclaim_orphaned_attachments()

    def _live_attachment_ids(self) -> set[str]:
        task_state = self._state.read("tasks")
        result = _attachment_ids_in(task_state)
        for task in task_state.values():
            if not isinstance(task, Mapping):
                continue
            session_id = str(task.get("conversation_session_id", "")).strip()
            if not session_id:
                continue
            messages = transcript_messages(self._event_log.replay(SessionId(session_id)))
            result.update(_attachment_ids_in(messages))
        return result

    def _require_pending_attachment_uploads(
        self,
        task: Mapping[str, Any],
        session_id: SessionId,
        references: list[dict[str, Any]],
    ) -> None:
        pending = task.get("pending_attachment_uploads", {})
        if not isinstance(pending, Mapping):
            raise ValidationError("Task attachment upload state is invalid")
        bound: dict[str, dict[str, Any]] = {}
        for message in transcript_messages(self._event_log.replay(session_id)):
            message_attachments = message.get("attachments", [])
            if not isinstance(message_attachments, list):
                continue
            for item in message_attachments:
                if isinstance(item, Mapping):
                    bound[str(item.get("attachment_id", ""))] = dict(item)
        for reference in references:
            grant = pending.get(reference["attachment_id"])
            granted_reference = grant.get("reference") if isinstance(grant, Mapping) else None
            if (
                (not isinstance(granted_reference, Mapping) or dict(granted_reference) != reference)
                and bound.get(reference["attachment_id"]) != reference
            ):
                raise AuthorizationError(
                    "conversation.write",
                    "attachment was not uploaded for this task",
                )

    def attachment_reference(
        self, identity: SessionIdentity, task_id: str, attachment_id: str,
    ) -> dict[str, Any]:
        """Resolve an attachment only when it is referenced by an owned Task message."""

        task = self._get_raw(identity, task_id)
        session_id = SessionId(str(task["conversation_session_id"]))
        for message in reversed(transcript_messages(self._event_log.replay(session_id))):
            attachments = message.get("attachments", [])
            if not isinstance(attachments, list):
                continue
            for reference in attachments:
                if isinstance(reference, Mapping) and reference.get("attachment_id") == attachment_id:
                    return dict(reference)
        raise KeyError(attachment_id)

    def is_cancelled(self, identity: SessionIdentity, task_id: str) -> bool:
        return str(self.get(identity, task_id).get("status", "")) == "cancelled"

    def cancel(self, identity: SessionIdentity, task_id: str) -> dict[str, Any]:
        """Cancel future Agent decisions for one owned task.

        Cancellation is a task state only.  It never deletes an existing
        ModuleRun or changes a completed financial result.
        """

        task = self._get_raw(identity, task_id)
        session_id = SessionId(str(task["conversation_session_id"]))
        question_id = _latest_open_question_id(transcript_messages(self._event_log.replay(session_id)))

        def update(value: dict[str, Any]) -> dict[str, Any]:
            task = value.get(task_id)
            if not isinstance(task, dict):
                raise KeyError(task_id)
            if task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
                raise AuthorizationError("task.write", "task is not owned by current caller")
            task["status"] = "cancelled"
            task["updated_at"] = datetime.now(timezone.utc).isoformat()
            return value

        cancelled = self._public_task(self._state.update("tasks", update)[task_id])
        if question_id is not None:
            self._event_log.append(
                session_id,
                "user_question.cancelled",
                {"question_id": question_id},
                event_id=f"question:{question_id}:cancelled",
                ignorable=True,
            )
        return cancelled

    def append_run_ref(self, identity: SessionIdentity, task_id: str, reference: dict[str, str]) -> None:
        required = {
            "module", "tenant_id", "task_id", "run_id",
            "expected_result_file_hash", "expected_artifact_manifest_hash",
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
        """Retired compatibility entrypoint.

        DataAsset belongs to the caller's global data library.  Older callers
        may still invoke this method during migration, but it deliberately
        performs no task mutation.
        """
        self._get_raw(identity, task_id)
        if not isinstance(reference, dict) or not isinstance(reference.get("data_asset_id"), str):
            raise ValidationError("DataAssetRef is invalid")

    def append_agent_event(self, identity: SessionIdentity, task_id: str, event: dict[str, Any]) -> None:
        """Compatibility entrypoint that appends to the child session log."""

        allowed = {
            "event", "timestamp", "workflow_run_id", "agent_run_id", "role", "preset_id", "preset_revision",
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
        """Keep the ordered current candidate inputs awaiting confirmation."""

        state = _pending_recommendation(value)

        def update(tasks: dict[str, Any]) -> dict[str, Any]:
            task = _owned_task(tasks, identity, task_id, "conversation.write")
            if any(
                row["tenant_id"] != identity.tenant_id or row["task_id"] != task_id
                for candidate in state["candidates"]
                for row in candidate["module_run_refs"]
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
        state = _pending_recommendation(value, allow_approved=True)
        return copy.deepcopy(state) if state["status"] in {"pending_approval", "approved"} else None

    def approve_pending_recommendation(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        approved_candidate_ids: list[str],
    ) -> dict[str, Any] | None:
        """Approve current candidate inputs atomically without contract activation."""

        selected_ids = _approved_candidate_ids(approved_candidate_ids)
        selected: dict[str, Any] | None = None

        def update(tasks: dict[str, Any]) -> dict[str, Any]:
            nonlocal selected
            task = _owned_task(tasks, identity, task_id, "conversation.write")
            raw = task.get("recommendation_state")
            if not isinstance(raw, dict):
                return tasks
            state = _pending_recommendation(raw, allow_approved=True)
            if state["status"] == "pending_approval":
                known_ids = {candidate["candidate_id"] for candidate in state["candidates"]}
                if any(candidate_id not in known_ids for candidate_id in selected_ids):
                    raise ValidationError("Recommendation approval contains unknown candidate_id")
                if not selected_ids:
                    raise ValidationError("Recommendation approval requires candidate_id")
                state["status"] = "approved"
                state["approved_candidate_ids"] = selected_ids
                state["approved_at"] = _utc_now()
                task["recommendation_state"] = state
                task["updated_at"] = state["approved_at"]
            elif state["approved_candidate_ids"] != selected_ids:
                raise ValidationError("Recommendation approval candidate order does not match current selection")
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
                if _canonical_value(confirmed_constraints) != _canonical_value(state["confirmed_constraints"]):
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
            sources, next_seq = _visible_process_event_cursor_state(record, events)
            appended = {
                "seq": next_seq,
                "type": normalized_type,
                "status": normalized_status,
                "summary": normalized_summary,
                "created_at": _utc_now(),
            }
            events.append(appended)
            sources[f"local:{next_seq}"] = next_seq
            record["visible_process_event_sources"] = sources
            record["visible_process_event_next_seq"] = next_seq + 1
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
        workflow_id = str(record.get("workflow_run_id", "")).strip()
        runtime_events: list[tuple[str, dict[str, Any]]] = []
        if workflow_id:
            session_id = SessionId(str(task.get("conversation_session_id", "")))
            for row in self._event_log.replay(session_id):
                if row.event_type != "runtime.event" or not isinstance(row.data, Mapping):
                    continue
                runtime_event = row.data.get("runtime_event")
                if not isinstance(runtime_event, Mapping) or str(runtime_event.get("workflow_id", "")) != workflow_id:
                    continue
                projected = _browser_runtime_event(runtime_event)
                if projected is not None:
                    runtime_events.append((f"runtime:{row.event_id}", projected))

        def update_cursor(tasks: dict[str, Any]) -> dict[str, Any]:
            current = _owned_task(tasks, identity, task_id, "conversation.read")
            requests = current.get("conversation_requests", {})
            if not isinstance(requests, dict):
                raise ValidationError("Task conversation requests are invalid")
            current_record = requests.get(normalized_id)
            if not isinstance(current_record, dict):
                raise KeyError(normalized_id)
            current_events = current_record.get("visible_process_events", [])
            if not isinstance(current_events, list):
                raise ValidationError("Conversation process events are invalid")
            sources, next_seq = _visible_process_event_cursor_with_runtime(
                current_record, current_events, runtime_events,
            )
            if current_record.get("visible_process_event_sources") != sources:
                current_record["visible_process_event_sources"] = sources
            if current_record.get("visible_process_event_next_seq") != next_seq:
                current_record["visible_process_event_next_seq"] = next_seq
            return tasks

        # The document-store lock makes source allocation atomic with local
        # event appends. Runtime rows are immutable and are assigned once by
        # their persisted event id, so a late row never renumbers old rows.
        self._state.update("tasks", update_cursor)
        task = self._get_raw(identity, task_id)
        record = task.get("conversation_requests", {}).get(normalized_id)
        if not isinstance(record, dict):
            raise KeyError(normalized_id)
        raw_events = record.get("visible_process_events", [])
        if not isinstance(raw_events, list):
            raise ValidationError("Conversation process events are invalid")
        sources, next_seq = _visible_process_event_cursor_with_runtime(
            record, raw_events, runtime_events,
        )
        merged: list[tuple[int, dict[str, Any]]] = []
        used_sequences: set[int] = set()
        for raw in raw_events:
            if not isinstance(raw, dict):
                raise ValidationError("Conversation process event sequence is invalid")
            sequence = raw.get("seq")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
                raise ValidationError("Conversation process event sequence is invalid")
            if sources.get(f"local:{sequence}") != sequence or sequence in used_sequences:
                raise ValidationError("Conversation process event sequence is invalid")
            event = {
                "type": _visible_event_type(raw.get("type")),
                "status": _visible_event_status(raw.get("status")),
                "summary": _visible_event_summary(raw.get("summary")),
                "created_at": _visible_event_time(raw.get("created_at")),
            }
            merged.append((sequence, event))
            used_sequences.add(sequence)
        for source_key, event in runtime_events:
            sequence = sources.get(source_key)
            if not isinstance(sequence, int) or sequence < 1 or sequence in used_sequences:
                raise ValidationError("Conversation process event sequence is invalid")
            merged.append((sequence, event))
            used_sequences.add(sequence)
        merged.sort(key=lambda item: item[0])
        events: list[dict[str, Any]] = []
        for sequence, event in merged:
            if sequence > after_seq:
                events.append({"seq": sequence, **event})
        return {
            "events": events,
            "next_seq": max((sequence for sequence, _event in merged), default=next_seq - 1),
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
                "visible_process_event_sources": {"local:1": 1},
                "visible_process_event_next_seq": 2,
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


def _visible_process_event_cursor_state(
    record: Mapping[str, Any], events: list[object],
) -> tuple[dict[str, int], int]:
    """Return and validate the durable source-to-cursor map for one request.

    Older request records only contain local events with their original
    sequence. Those sequences become the initial immutable cursor values. New
    runtime rows are added by persisted event id and never sorted by time.
    """

    raw_sources = record.get("visible_process_event_sources")
    if raw_sources is None:
        sources: dict[str, int] = {}
    elif not isinstance(raw_sources, dict):
        raise ValidationError("Conversation process event cursor map is invalid")
    else:
        sources = {}
        used: dict[int, str] = {}
        for raw_key, raw_value in raw_sources.items():
            key = str(raw_key).strip()
            if not key or len(key) > 256:
                raise ValidationError("Conversation process event cursor map is invalid")
            if isinstance(raw_value, bool) or not isinstance(raw_value, int) or raw_value < 1:
                raise ValidationError("Conversation process event cursor map is invalid")
            previous = used.get(raw_value)
            if previous is not None and previous != key:
                raise ValidationError("Conversation process event cursor map is invalid")
            sources[key] = raw_value
            used[raw_value] = key

    used = {sequence: key for key, sequence in sources.items()}
    maximum = max(sources.values(), default=0)
    local_sequences: set[int] = set()
    for raw in events:
        if not isinstance(raw, dict):
            raise ValidationError("Conversation process event sequence is invalid")
        sequence = raw.get("seq")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValidationError("Conversation process event sequence is invalid")
        if sequence in local_sequences:
            raise ValidationError("Conversation process event sequence is invalid")
        local_sequences.add(sequence)
        source_key = f"local:{sequence}"
        mapped = sources.get(source_key)
        if mapped is not None and mapped != sequence:
            raise ValidationError("Conversation process event cursor map is invalid")
        previous = used.get(sequence)
        if previous is not None and previous != source_key:
            raise ValidationError("Conversation process event cursor map is invalid")
        sources[source_key] = sequence
        used[sequence] = source_key
        maximum = max(maximum, sequence)

    raw_next = record.get("visible_process_event_next_seq")
    if raw_next is not None and (isinstance(raw_next, bool) or not isinstance(raw_next, int) or raw_next < 1):
        raise ValidationError("Conversation process event cursor is invalid")
    next_seq = max(maximum + 1, int(raw_next or 1))
    return sources, next_seq


def _visible_process_event_cursor_with_runtime(
    record: Mapping[str, Any],
    events: list[object],
    runtime_events: list[tuple[str, dict[str, Any]]],
) -> tuple[dict[str, int], int]:
    sources, next_seq = _visible_process_event_cursor_state(record, events)
    used = set(sources.values())
    for source_key, _event in runtime_events:
        if not isinstance(source_key, str) or not source_key.strip():
            raise ValidationError("Conversation process event source is invalid")
        existing = sources.get(source_key)
        if existing is not None:
            continue
        while next_seq in used:
            next_seq += 1
        sources[source_key] = next_seq
        used.add(next_seq)
        next_seq += 1
    return sources, next_seq


def _browser_runtime_event(value: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project a persisted RuntimeEvent without exposing its authority fields."""

    event_type = str(value.get("type", "")).strip().lower()
    timestamp = str(value.get("timestamp", "")).strip()
    payload = value.get("payload")
    if not event_type or not timestamp or not isinstance(payload, Mapping):
        return None
    status = str(payload.get("status", "")).strip().lower()
    if status not in {
        "queued", "starting", "running", "waiting_tool", "waiting_parent",
        "completed", "failed", "cancelled", "interrupted", "recovered", "outcome_unknown", "timed_out",
    }:
        suffix = event_type.rsplit(".", 1)[-1]
        status = {
            "started": "started", "ready": "started", "completed": "completed",
            "failed": "failed", "cancelled": "cancelled", "interrupted": "interrupted",
            "recovered": "recovered", "closed": "completed",
        }.get(suffix, "running")
    safe_payload: dict[str, Any] = {}
    runtime_scope = str(payload.get("runtime_scope", "")).strip().lower()
    if runtime_scope not in {"main_agent", "recommender"}:
        runtime_scope = ""
    if event_type in {"assistant.text_delta", "assistant.reasoning_delta"}:
        delta = payload.get("delta", payload.get("text", ""))
        if isinstance(delta, str):
            safe_payload["delta"] = delta[:64_000]
        if event_type == "assistant.reasoning_delta":
            safe_payload["available"] = bool(safe_payload.get("delta")) or payload.get("available") is True
            raw_chars = payload.get("chars", len(str(delta)))
            safe_payload["chars"] = min(
                raw_chars if isinstance(raw_chars, int) and not isinstance(raw_chars, bool) and raw_chars >= 0 else len(str(delta)),
                512_000,
            )
            if payload.get("truncated") is True:
                safe_payload["truncated"] = True
            provider_chars = payload.get("provider_chars")
            if isinstance(provider_chars, int) and not isinstance(provider_chars, bool) and provider_chars >= safe_payload["chars"]:
                safe_payload["provider_chars"] = min(provider_chars, 10_000_000)
        block_index = payload.get("index")
        if isinstance(block_index, int) and not isinstance(block_index, bool) and block_index >= 0:
            safe_payload["index"] = block_index
    elif event_type in {"assistant.block_started", "assistant.block_completed"}:
        block_index = payload.get("index")
        if not isinstance(block_index, int) or isinstance(block_index, bool) or block_index < 0:
            return None
        safe_payload["index"] = block_index
        if event_type == "assistant.block_started":
            block_type = str(payload.get("block_type", "")).replace("_", "-")
            if block_type not in {"text", "reasoning", "tool-call"}:
                return None
            safe_payload["block_type"] = block_type
        else:
            block = payload.get("block")
            if not isinstance(block, Mapping):
                return None
            block_type = str(block.get("type", "")).replace("_", "-")
            if block_type in {"text", "reasoning"}:
                safe_payload["block"] = {"type": block_type, "text": str(block.get("text", ""))[:64_000]}
            elif block_type == "tool-call":
                safe_payload["block"] = {"type": "tool-call", "name": str(block.get("name", ""))[:80]}
            else:
                return None
    elif event_type == "assistant.message":
        text = payload.get("text")
        if isinstance(text, str):
            safe_payload["text"] = text[:64_000]
        blocks = payload.get("blocks")
        if isinstance(blocks, list):
            safe_blocks: list[dict[str, str]] = []
            for block in blocks:
                if not isinstance(block, Mapping):
                    continue
                block_type = str(block.get("type", "")).replace("_", "-")
                if block_type in {"text", "reasoning"}:
                    safe_blocks.append({"type": block_type, "text": str(block.get("text", ""))[:64_000]})
                elif block_type == "tool-call":
                    safe_blocks.append({"type": "tool-call", "name": str(block.get("name", ""))[:80]})
            safe_payload["blocks"] = safe_blocks
    elif event_type.startswith("agent."):
        safe_payload = {
            "role_id": str(payload.get("role_id", payload.get("role", "Agent")))[:80],
            "status": status,
        }
        model = payload.get("model_id")
        if isinstance(model, str) and model:
            safe_payload["model_id"] = model[:160]
    elif event_type.startswith("tool."):
        safe_payload = {
            "tool_name": str(payload.get("tool_name", payload.get("module", "")))[:80],
            "status": status,
        }
    elif event_type == "usage.updated":
        usage = payload.get("usage", payload)
        if isinstance(usage, Mapping):
            safe_payload["usage"] = {
                str(key): int(item)
                for key, item in usage.items()
                if isinstance(item, int) and not isinstance(item, bool) and item >= 0
            }
    elif event_type.startswith("compaction."):
        safe_payload = {"status": status}
    role = str(safe_payload.get("role_id", ""))
    tool = str(safe_payload.get("tool_name", ""))
    summary = (
        f"{role or 'Agent'}正在处理本轮分工。" if event_type.startswith("agent.")
        else f"{tool or '研究模块'}正在运行。" if event_type.startswith("tool.")
        else "正在生成答复。" if event_type.startswith("assistant.")
        else "正在整理上下文。" if event_type.startswith("compaction.")
        else "研究流程正在运行。"
    )
    if event_type.startswith("tool."):
        outcome = {
            "completed": "已完成。", "succeeded": "已完成。", "failed": "执行失败。",
            "outcome_unknown": "已中断，结果待确认。", "interrupted": "已中断，结果待确认。",
            "cancelled": "已停止。", "timed_out": "响应超时。",
        }.get(status, "正在运行。")
        summary = f"{tool or '研究模块'}{outcome}"
        error = payload.get("error")
        result = payload.get("result")
        if isinstance(result, Mapping):
            error = result.get("message", error)
        if status in {"failed", "outcome_unknown", "interrupted", "timed_out"} and isinstance(error, str):
            safe_payload["message"] = redact_text(error, limit=1_000)
    projected = {
        "type": event_type,
        "status": status,
        "summary": summary,
        "created_at": timestamp,
        "agent_run_id": str(value.get("agent_run_id") or "")[:160],
        "turn": value.get("turn") if isinstance(value.get("turn"), int) else None,
        "step": value.get("step") if isinstance(value.get("step"), int) else None,
        "payload": safe_payload,
    }
    if runtime_scope:
        projected["scope"] = runtime_scope
    return projected


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


def _attachment_ids_in(value: object) -> set[str]:
    """Find attachment ids without trusting a single Task schema revision."""

    result: set[str] = set()
    if isinstance(value, Mapping):
        attachment_id = value.get("attachment_id")
        if isinstance(attachment_id, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", attachment_id):
            result.add(attachment_id)
        for item in value.values():
            result.update(_attachment_ids_in(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_attachment_ids_in(item))
    return result


def _pending_recommendation(value: dict[str, Any], *, allow_approved: bool = False) -> dict[str, Any]:
    """Validate the only continuation payload the Task index is allowed to retain."""

    allowed = {
        "status", "analysis_case_id", "confirmed_constraints", "delivery", "workflow_mode",
        "approved_at", "completed_at", "state_schema", "preset_id", "ranking_spec",
        "candidates", "approved_candidate_ids", "requested_outputs",
    }
    if set(value).difference(allowed):
        raise ValidationError("Recommendation continuation contains unsupported fields")
    status = str(value.get("status", "")).strip()
    permitted = {"pending_approval"}
    if allow_approved:
        permitted.update({"approved", "completed"})
    if status not in permitted:
        raise ValidationError("Recommendation continuation status is invalid")
    state_schema = PENDING_RECOMMENDATION_SCHEMA_ID
    if value.get("state_schema") != state_schema:
        raise ValidationError("Recommendation continuation is not an ordered candidate approval")
    result: dict[str, Any] = {
        "status": status,
        "state_schema": state_schema,
        "analysis_case_id": _safe_identifier(
            value.get("analysis_case_id"), "analysis_case_id", maximum=160,
        ),
        "preset_id": _safe_identifier(value.get("preset_id"), "preset_id", maximum=80),
    }
    workflow_mode = str(value.get("workflow_mode", "")).strip()
    if workflow_mode not in {"single_agent", "multi_agent", "degraded_single_agent"}:
        raise ValidationError("Recommendation continuation workflow_mode is invalid")
    result["workflow_mode"] = workflow_mode
    constraints = value.get("confirmed_constraints")
    if not isinstance(constraints, dict) or set(constraints).difference({
        "underlying", "horizon", "market_view", "max_loss", "principal_fluctuation",
        "output_type", "format", "path_count", "backtest_range",
    }):
        raise ValidationError("Recommendation continuation constraints are invalid")
    result["confirmed_constraints"] = copy.deepcopy(constraints)
    delivery = _recommendation_delivery(value.get("delivery"), required=False)
    result["delivery"] = delivery
    raw_candidates = value.get("candidates")
    if (
        isinstance(raw_candidates, (str, bytes))
        or not isinstance(raw_candidates, list)
        or not raw_candidates
        or len(raw_candidates) > 10
    ):
        raise ValidationError("Recommendation continuation candidates are invalid")
    candidates = [_pending_candidate_snapshot(item) for item in raw_candidates]
    candidate_ids = [item["candidate_id"] for item in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValidationError("Recommendation continuation candidate_ids are duplicated")
    approved_candidate_ids = _approved_candidate_ids(value.get("approved_candidate_ids"))
    if any(candidate_id not in candidate_ids for candidate_id in approved_candidate_ids):
        raise ValidationError("Recommendation approval contains unknown candidate_id")
    if status == "pending_approval" and approved_candidate_ids:
        raise ValidationError("Pending recommendation cannot contain approved_candidate_ids")
    if status != "pending_approval" and not approved_candidate_ids:
        raise ValidationError("Recommendation approval requires approved_candidate_ids")
    result["candidates"] = candidates
    result["approved_candidate_ids"] = approved_candidate_ids
    ranking_spec = value.get("ranking_spec")
    result["ranking_spec"] = None if ranking_spec is None else _json_snapshot(
        ranking_spec, "Recommendation continuation ranking_spec", maximum_bytes=64_000,
    )
    requested_outputs = value.get("requested_outputs", [])
    if isinstance(requested_outputs, (str, bytes)) or not isinstance(requested_outputs, list):
        raise ValidationError("Recommendation continuation requested_outputs are invalid")
    outputs = [str(item).strip().lower() for item in requested_outputs]
    if len(outputs) > 6 or len(outputs) != len(set(outputs)) or any(
        item not in {"payoff", "pricing", "backtest", "card", "quote", "report"}
        for item in outputs
    ):
        raise ValidationError("Recommendation continuation requested_outputs are invalid")
    result["requested_outputs"] = outputs
    for field in ("approved_at", "completed_at"):
        if field in value:
            timestamp = str(value[field]).strip()
            if not timestamp or len(timestamp) > 80:
                raise ValidationError("Recommendation continuation timestamp is invalid")
            result[field] = timestamp
    return result


def _approved_candidate_ids(value: object) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, list) or len(value) > 128:
        raise ValidationError("Recommendation continuation approved_candidate_ids are invalid")
    result = [_safe_identifier(item, "approved_candidate_id", maximum=160) for item in value]
    if len(set(result)) != len(result):
        raise ValidationError("Recommendation continuation approved_candidate_ids are duplicated")
    return result


def _pending_candidate_snapshot(value: object) -> dict[str, Any]:
    fields = {
        "candidate_id", "product_id", "rule_revision", "product_name", "underlyings",
        "library_status", "current_inputs", "module_run_refs", "public_projection",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError("Recommendation continuation candidate snapshot is invalid")
    candidate_id = _safe_identifier(value.get("candidate_id"), "candidate_id", maximum=160)
    product_id = _safe_identifier(value.get("product_id"), "product_id", maximum=80)
    revision = value.get("rule_revision")
    product_name = str(value.get("product_name", "")).strip()
    underlyings = value.get("underlyings")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or len(product_name) > 160
        or not isinstance(underlyings, list)
        or not underlyings
        or any(not isinstance(item, str) or not item.strip() or len(item) > 80 for item in underlyings)
        or value.get("library_status") != "ready"
    ):
        raise ValidationError("Recommendation continuation candidate is invalid")
    current_inputs = _json_snapshot(
        value.get("current_inputs"), "Recommendation continuation current_inputs", maximum_bytes=128_000,
    )
    if not isinstance(current_inputs, dict) or _contains_retired_candidate_state(current_inputs):
        raise ValidationError("Recommendation continuation current_inputs contain retired state")
    return {
        "candidate_id": candidate_id,
        "product_id": product_id,
        "rule_revision": revision,
        "product_name": product_name,
        "underlyings": [item.strip() for item in underlyings],
        "library_status": "ready",
        "current_inputs": current_inputs,
        "module_run_refs": _pending_module_run_refs(value.get("module_run_refs")),
        "public_projection": _pending_public_candidate_projection(value.get("public_projection")),
    }


def _json_snapshot(value: object, field_name: str, *, maximum_bytes: int) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValidationError(f"{field_name} is not valid JSON") from error
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise ValidationError(f"{field_name} exceeds size limit")
    return decoded


def _contains_retired_candidate_state(value: object) -> bool:
    retired = {
        "resolved_" + "contract", "candidate_" + "version", "catalog_" + "version",
        "product_" + "version", "contract_" + "fingerprint", "term_overrides_" + "fingerprint",
    }
    if isinstance(value, Mapping):
        return any(
            str(key) in retired
            or str(key).endswith("_fingerprint")
            or str(key).endswith("_hash")
            or _contains_retired_candidate_state(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_retired_candidate_state(item) for item in value)
    return False


def _pending_public_candidate_projection(value: object) -> dict[str, Any]:
    fields = {"reason", "suitable_for", "not_suitable_for", "main_risks", "key_terms"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError("Recommendation continuation public candidate projection is invalid")
    reason = str(value.get("reason", "")).strip()
    if not reason or len(reason) > 800:
        raise ValidationError("Recommendation continuation public candidate reason is invalid")
    result: dict[str, Any] = {"reason": reason}
    for field in ("suitable_for", "not_suitable_for", "main_risks"):
        rows = value.get(field)
        if (
            not isinstance(rows, list)
            or len(rows) > 16
            or any(not isinstance(item, str) or not item.strip() or len(item) > 800 for item in rows)
        ):
            raise ValidationError("Recommendation continuation public candidate text is invalid")
        result[field] = [item.strip() for item in rows]
    key_terms = value.get("key_terms")
    if not isinstance(key_terms, list) or len(key_terms) > 64:
        raise ValidationError("Recommendation continuation public candidate terms are invalid")
    normalized_terms: list[dict[str, str]] = []
    for row in key_terms:
        if not isinstance(row, dict) or set(row) != {"label", "value"}:
            raise ValidationError("Recommendation continuation public candidate terms are invalid")
        label = str(row.get("label", "")).strip()
        display = str(row.get("value", "")).strip()
        if not label or not display or len(label) > 120 or len(display) > 240:
            raise ValidationError("Recommendation continuation public candidate terms are invalid")
        normalized_terms.append({"label": label, "value": display})
    result["key_terms"] = normalized_terms
    return result


def _canonical_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _safe_identifier(value: object, field_name: str, *, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", text):
        raise ValidationError(f"Recommendation continuation {field_name} is invalid")
    return text


def _validated_attachment_references(value: object) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, list) or len(value) > 20:
        raise ValidationError("Message attachments are invalid")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_bytes = 0
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValidationError("Message attachment reference is invalid")
        allowed = {
            "attachment_id", "kind", "media_type", "bytes", "name", "width", "height",
            "original_dimensions", "extraction_status", "text_chars",
        }
        if set(raw).difference(allowed):
            raise ValidationError("Message attachment reference has unknown fields")
        attachment_id = str(raw.get("attachment_id", ""))
        kind = str(raw.get("kind", ""))
        media_type = str(raw.get("media_type", ""))
        size = raw.get("bytes")
        if (
            not re.fullmatch(r"sha256:[0-9a-f]{64}", attachment_id)
            or kind not in {"image", "document"}
            or not media_type
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
            or size > 20 * 1024 * 1024
            or attachment_id in seen
        ):
            raise ValidationError("Message attachment reference is invalid")
        if kind == "image" and not media_type.startswith("image/"):
            raise ValidationError("Image attachment media type is invalid")
        if kind == "document" and media_type.startswith("image/"):
            raise ValidationError("Document attachment media type is invalid")
        total_bytes += size
        if total_bytes > 200 * 1024 * 1024:
            raise ValidationError("Message attachments exceed the 200MB total limit")
        seen.add(attachment_id)
        result.append(copy.deepcopy(dict(raw)))
    return result


def _validated_question_block(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"type", "question_id", "prompt", "options", "allow_free_text"}
    if set(value).difference(allowed):
        raise ValidationError("Conversation question contains unsupported fields")
    question_id = str(value.get("question_id", ""))
    prompt = str(value.get("prompt", "")).strip()
    options = value.get("options", [])
    allow_free_text = value.get("allow_free_text", True)
    if (
        not re.fullmatch(r"[0-9a-f]{24}", question_id)
        or not prompt
        or len(prompt) > 2_000
        or not isinstance(options, list)
        or len(options) > 3
        or not isinstance(allow_free_text, bool)
    ):
        raise ValidationError("Conversation question is invalid")
    safe_options: list[dict[str, Any]] = []
    seen: set[str] = set()
    for option in options:
        if not isinstance(option, Mapping) or set(option).difference({
            "value", "label", "description", "recommended",
        }):
            raise ValidationError("Conversation question option is invalid")
        answer = str(option.get("value", "")).strip()
        label = str(option.get("label", "")).strip()
        description = str(option.get("description", "")).strip()
        recommended = option.get("recommended", False)
        if (
            not answer
            or len(answer) > 500
            or not label
            or len(label) > 80
            or len(description) > 240
            or not isinstance(recommended, bool)
            or answer in seen
        ):
            raise ValidationError("Conversation question option is invalid")
        seen.add(answer)
        safe_options.append({
            "value": answer,
            "label": label,
            "description": description,
            "recommended": recommended,
        })
    if not safe_options and not allow_free_text:
        raise ValidationError("Conversation question has no answer path")
    return {
        "type": "question",
        "question_id": question_id,
        "prompt": prompt,
        "options": safe_options,
        "allow_free_text": allow_free_text,
    }


def _latest_open_question_id(messages: list[dict[str, Any]]) -> str | None:
    if not messages or messages[-1].get("role") != "assistant":
        return None
    blocks = messages[-1].get("content_blocks", [])
    if not isinstance(blocks, list):
        return None
    for block in reversed(blocks):
        if not isinstance(block, Mapping) or block.get("type") != "question":
            continue
        question_id = str(block.get("question_id", ""))
        return question_id if re.fullmatch(r"[0-9a-f]{24}", question_id) else None
    return None


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


def _pending_module_run_refs(value: object) -> list[dict[str, str]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, list) or len(value) > 16:
        raise ValidationError("Recommendation continuation module_run_refs are invalid")
    fields = {
        "module", "tenant_id", "task_id", "run_id",
        "expected_result_file_hash", "expected_artifact_manifest_hash",
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
            or not _sha256(row["expected_result_file_hash"])
            or not _sha256(row["expected_artifact_manifest_hash"])
        ):
            raise ValidationError("Recommendation continuation module_run_refs are invalid")
        identity = (row["module"], row["tenant_id"], row["task_id"], row["run_id"])
        if identity in seen:
            raise ValidationError("Recommendation continuation module_run_refs are duplicated")
        seen.add(identity)
        result.append(row)
    return sorted(result, key=lambda item: (item["module"], item["run_id"]))


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
