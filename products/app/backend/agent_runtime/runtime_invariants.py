"""Shared fail-closed invariants for durable App runtime state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import ValidationError


CURRENT_SESSION_FORMAT_VERSION = 1
CURRENT_EVENT_VERSION = 1

KNOWN_SESSION_EVENT_TYPES = frozenset({
    "conversation.message",
    "conversation.summary",
    "tool.call",
    "tool.result",
    "workflow_run.started",
    "workflow_run.child_started",
    "workflow_run.child_settled",
    "workflow_run.finish_requested",
    "workflow_run.finished",
    "agent_run.started",
    "agent_run.model_requested",
    "agent_run.finished",
    "agent_run.caller_settled",
    "agent_run.cancel_requested",
    "agent_run.start_failed",
    "agent_run.disposal_deferred",
    "agent_run.disposal_persistence_failed",
    "agent_run.disposal_retry_attempted",
    "agent_run.disposal_retry_exhausted",
    "agent_run.disposal_retry_succeeded",
    "agent_run.provider_quiesced",
    "agent_run.disposed",
    "agent_run.restart_recovery_started",
    "dispatch_checkpointed",
    "dispatch.succeeded",
    "dispatch.failed",
    "dispatch.interrupted_unknown",
})

JOB_STATES = frozenset({
    "queued", "running", "succeeded", "failed", "cancel_requested",
    "cancelled", "interrupted",
})
JOB_TRANSITIONS = {
    None: frozenset({"queued"}),
    "queued": frozenset({"running", "cancel_requested", "cancelled", "interrupted"}),
    "running": frozenset({"succeeded", "failed", "cancel_requested", "cancelled", "interrupted"}),
    "cancel_requested": frozenset({"cancelled", "succeeded", "failed", "interrupted"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "interrupted": frozenset({"succeeded"}),
}


class RuntimeInvariantValidator:
    """One validator used by storage, projections and recovery paths."""

    def validate_header(self, *, format_version: int, revision: int) -> None:
        if format_version != CURRENT_SESSION_FORMAT_VERSION:
            raise ValidationError("Unsupported session format version")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValidationError("Session revision must be a non-negative integer")

    def validate_event(
        self,
        *,
        event_type: str,
        event_version: int,
        ignorable: bool,
        data: Mapping[str, Any],
    ) -> bool:
        if not event_type.strip() or not isinstance(data, Mapping):
            raise ValidationError("Session event type and data are required")
        if event_version != CURRENT_EVENT_VERSION:
            if ignorable:
                return False
            raise ValidationError("Unsupported required session event version")
        if event_type not in KNOWN_SESSION_EVENT_TYPES:
            if ignorable:
                return False
            raise ValidationError("Unknown required session event type")
        return True

    def validate_replay(self, events: Sequence[Any]) -> None:
        expected = 1
        checkpoints: set[str] = set()
        dispatch_terminals: set[str] = set()
        workflow_events: list[Any] = []
        dispatch_states: dict[str, str] = {}
        for event in events:
            if int(event.seq) != expected:
                raise ValidationError("Session event sequence is not contiguous")
            known = self.validate_event(
                event_type=str(event.event_type),
                event_version=int(event.event_version),
                ignorable=bool(event.ignorable),
                data=event.data,
            )
            if not known:
                expected += 1
                continue
            if event.event_type == "dispatch_checkpointed":
                operation_id = str(event.data.get("operation_id", ""))
                if not operation_id or operation_id in checkpoints:
                    raise ValidationError("Dispatch checkpoint identity is missing or duplicated")
                checkpoints.add(operation_id)
            elif event.event_type in {
                "dispatch.succeeded", "dispatch.failed", "dispatch.interrupted_unknown",
            }:
                operation_id = str(event.data.get("operation_id", ""))
                if operation_id not in checkpoints:
                    raise ValidationError("Dispatch terminal state requires an earlier checkpoint")
                previous = dispatch_states.get(operation_id)
                if previous is not None and not (
                    previous == "dispatch.interrupted_unknown"
                    and event.event_type == "dispatch.succeeded"
                ):
                    raise ValidationError("Dispatch terminal state transition is invalid")
                dispatch_states[operation_id] = event.event_type
                dispatch_terminals.add(operation_id)
            if event.event_type.startswith("workflow_run."):
                workflow_events.append(event)
            expected += 1
        self.validate_workflow_lifecycle(workflow_events)

    def validate_workflow_recovery(self, events: Sequence[Any], workflow_run_id: str) -> None:
        self.validate_workflow_lifecycle(events, workflow_run_id=workflow_run_id)

    def validate_workflow_lifecycle(
        self, events: Sequence[Any], *, workflow_run_id: str | None = None,
    ) -> None:
        lifecycle: dict[str, list[str]] = {}
        for event in events:
            if not str(event.event_type).startswith("workflow_run."):
                continue
            identifier = str(event.data.get("workflow_run_id", ""))
            if not identifier:
                raise ValidationError("Workflow event requires workflow_run_id")
            if workflow_run_id is not None and identifier != workflow_run_id:
                continue
            lifecycle.setdefault(identifier, []).append(str(event.event_type))
        for types in lifecycle.values():
            if "workflow_run.finished" in types and "workflow_run.finish_requested" not in types:
                raise ValidationError("Workflow cannot finish before finish is requested")
            if types.count("workflow_run.started") > 1 or types.count("workflow_run.finished") > 1:
                raise ValidationError("Workflow lifecycle terminal events must be idempotent")

    def validate_job_transition(self, previous: str | None, target: str) -> None:
        if target not in JOB_STATES or previous not in JOB_TRANSITIONS:
            raise ValidationError("Unknown job state")
        if target not in JOB_TRANSITIONS[previous]:
            raise ValidationError(f"Invalid job transition: {previous or 'new'} -> {target}")

    def validate_job_record(self, record: Any) -> None:
        if record.state not in JOB_STATES:
            raise ValidationError("Persisted job has an unknown state")
        if record.module not in {"payoffer", "pricer", "backtester"}:
            raise ValidationError("Persisted job has an unknown module")
        if not all(str(getattr(record, field, "")).strip() for field in (
            "job_id", "tenant_id", "owner_id", "task_id", "operation_id",
        )):
            raise ValidationError("Persisted job identity is incomplete")
        reference = record.module_run_ref
        if reference is not None:
            required = {
                "module", "tenant_id", "task_id", "run_id",
                "expected_semantic_result_hash", "expected_artifact_manifest_hash",
            }
            if not isinstance(reference, Mapping) or set(reference) != required:
                raise ValidationError("Persisted job ModuleRunRef is invalid")
            if (
                reference.get("module") != record.module
                or reference.get("tenant_id") != record.tenant_id
                or reference.get("task_id") != record.task_id
            ):
                raise ValidationError("Persisted job ModuleRunRef scope is invalid")

    def validate_surface_operation(
        self, event_type: str, value: object, surface_types: frozenset[str],
    ) -> str | Mapping[str, int] | None:
        if event_type not in surface_types:
            if value is not None:
                raise ValidationError("Non-surface event cannot carry a surface operation")
            return None
        if value == "append":
            return "append"
        if isinstance(value, Mapping) and set(value) == {"start", "end"}:
            start, end = value["start"], value["end"]
            if all(isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in (start, end)):
                return {"start": int(start), "end": int(end)}
        raise ValidationError("Surface event requires a valid append or replace operation")

    def validate_source_event_seqs(self, values: Sequence[int]) -> tuple[int, ...]:
        result = tuple(values)
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in result):
            raise ValidationError("Source event sequences must be positive integers")
        if len(set(result)) != len(result):
            raise ValidationError("Source event sequences must be unique")
        return result

    def validate_tool_pairs(self, events: Sequence[Any], *, require_closed: bool = False) -> None:
        calls: set[str] = set()
        results: set[str] = set()
        for event in events:
            if event.event_type == "tool.call":
                call_id = str(event.data.get("call_id", ""))
                if not call_id or call_id in calls:
                    raise ValidationError("Tool call identity is missing or duplicated")
                calls.add(call_id)
            elif event.event_type == "tool.result":
                call_id = str(event.data.get("call_id", ""))
                if call_id not in calls:
                    raise ValidationError("Tool result must follow its matching tool call")
                if call_id in results:
                    raise ValidationError("Tool call may have only one result")
                results.add(call_id)
        if require_closed and calls != results:
            raise ValidationError("Terminal surface requires every tool call to have one result")

    def validate_pair_boundary(
        self, events: Sequence[Any], included_seqs: set[int], *, replacement: Any | None = None,
    ) -> None:
        calls = {str(event.data.get("call_id", "")): event.seq for event in events if event.event_type == "tool.call"}
        results = {str(event.data.get("call_id", "")): event.seq for event in events if event.event_type == "tool.result"}
        for call_id, call_seq in calls.items():
            result_seq = results.get(call_id)
            if result_seq is None or ((call_seq in included_seqs) == (result_seq in included_seqs)):
                continue
            if (
                replacement is not None and result_seq in included_seqs
                and replacement.event_type == "tool.result"
                and str(replacement.data.get("call_id", "")) == call_id
            ):
                continue
            raise ValidationError("Surface operation cannot split a tool call and result pair")

    def validate_tool_result_replacement(self, replacement: Any, shadowed: Sequence[Any]) -> None:
        if replacement.event_type != "tool.result":
            return
        if len(shadowed) != 1 or shadowed[0].event_type != "tool.result":
            raise ValidationError("Tool result replacement must target one tool result")
        if any(replacement.data.get(key) != shadowed[0].data.get(key) for key in ("call_id", "name", "is_error")):
            raise ValidationError("Tool result replacement may change content only")


RUNTIME_INVARIANTS = RuntimeInvariantValidator()


__all__ = (
    "CURRENT_EVENT_VERSION",
    "CURRENT_SESSION_FORMAT_VERSION",
    "KNOWN_SESSION_EVENT_TYPES",
    "RUNTIME_INVARIANTS",
    "RuntimeInvariantValidator",
)
