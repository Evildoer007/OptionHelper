"""Selective cleanup when the current OptionReg rule revision advances."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .attachments import AttachmentStore
from .errors import ValidationError
from .stores import _LocalDocumentStore


def reconcile_product_rule_revisions(
    state: _LocalDocumentStore,
    current_revisions: Mapping[str, int],
) -> dict[str, int]:
    """Remove outputs derived from a non-current product rule revision.

    Tasks and human-authored messages remain.  Product-generated messages,
    candidates, ModuleRun projections and whole reports are removed only when
    their explicit ``product_id`` and ``rule_revision`` no longer match the
    current OptionReg entry.
    """

    revisions = _validated_revisions(current_revisions)
    summary = {
        "removed_runs": 0,
        "removed_candidates": 0,
        "removed_reports": 0,
        "removed_product_messages": 0,
        "reclaimed_attachments": 0,
    }

    def update_tasks(tasks: dict[str, Any]) -> dict[str, Any]:
        for task_id, raw_task in list(tasks.items()):
            if not isinstance(raw_task, dict):
                continue
            task = dict(raw_task)
            task["messages"], removed_messages = _filter_messages(task.get("messages"), revisions)
            task["run_refs"], removed_runs = _filter_records(task.get("run_refs"), revisions)
            task["candidates"], removed_candidates = _filter_records(task.get("candidates"), revisions)
            summary["removed_product_messages"] += removed_messages
            summary["removed_runs"] += removed_runs
            summary["removed_candidates"] += removed_candidates
            if isinstance(task.get("recommendation_state"), dict):
                task["recommendation_state"], removed = _clean_recommendation_state(
                    task["recommendation_state"], revisions,
                )
                summary["removed_candidates"] += removed
            tasks[task_id] = task
        return tasks

    state.update("tasks", update_tasks)

    def update_results(results: dict[str, Any]) -> dict[str, Any]:
        kept: dict[str, Any] = {}
        removed = 0
        for key, value in results.items():
            if _contains_stale_dependency(value, revisions):
                removed += 1
            else:
                kept[key] = value
        summary["removed_runs"] = max(summary["removed_runs"], removed)
        return kept

    state.update("results", update_results)

    def update_reports(reports: dict[str, Any]) -> dict[str, Any]:
        kept: dict[str, Any] = {}
        for key, value in reports.items():
            if _contains_stale_dependency(value, revisions):
                summary["removed_reports"] += 1
            else:
                kept[key] = value
        return kept

    state.update("reports", update_reports)
    state.update("product_rule_revisions", lambda _value: dict(revisions))

    live_attachment_ids = _attachment_ids(state.read("tasks"))
    attachments = AttachmentStore(state._root / "attachments")
    attachments.orphan_retention_seconds = 0
    reclaimed = attachments.collect_orphans(live_attachment_ids)
    summary["reclaimed_attachments"] = int(reclaimed.get("reclaimed", 0))
    return summary


def _validated_revisions(values: Mapping[str, int]) -> dict[str, int]:
    if not isinstance(values, Mapping) or not values:
        raise ValidationError("Current product rule revisions are required")
    result: dict[str, int] = {}
    for raw_product_id, revision in values.items():
        product_id = str(raw_product_id).strip()
        if (
            not product_id
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision <= 0
        ):
            raise ValidationError("Product rule revisions must be positive integers")
        result[product_id] = revision
    return result


def _is_stale(value: object, revisions: Mapping[str, int]) -> bool:
    if not isinstance(value, Mapping):
        return False
    product_id = value.get("product_id")
    if not isinstance(product_id, str) or product_id not in revisions:
        return False
    revision = value.get("rule_revision")
    return (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision != revisions[product_id]
    )


def _contains_stale_dependency(value: object, revisions: Mapping[str, int]) -> bool:
    if _is_stale(value, revisions):
        return True
    if isinstance(value, Mapping):
        return any(_contains_stale_dependency(item, revisions) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_stale_dependency(item, revisions) for item in value)
    return False


def _filter_records(value: object, revisions: Mapping[str, int]) -> tuple[list[Any], int]:
    if not isinstance(value, list):
        return ([] if value is None else list(value) if isinstance(value, tuple) else []), 0
    kept = [item for item in value if not _contains_stale_dependency(item, revisions)]
    return kept, len(value) - len(kept)


def _filter_messages(value: object, revisions: Mapping[str, int]) -> tuple[list[Any], int]:
    if not isinstance(value, list):
        return [], 0
    kept: list[Any] = []
    removed = 0
    for item in value:
        generated = isinstance(item, Mapping) and item.get("message_kind") == "product_dependency"
        if generated and _contains_stale_dependency(item, revisions):
            removed += 1
        else:
            kept.append(item)
    return kept, removed


def _clean_recommendation_state(
    value: dict[str, Any],
    revisions: Mapping[str, int],
) -> tuple[dict[str, Any], int]:
    result = dict(value)
    removed = 0
    contracts = result.get("candidate_contracts")
    if isinstance(contracts, dict):
        kept = {
            key: item for key, item in contracts.items()
            if not _contains_stale_dependency(item, revisions)
        }
        removed = len(contracts) - len(kept)
        result["candidate_contracts"] = kept
        for field in ("candidate_ids", "approved_candidate_ids"):
            identifiers = result.get(field)
            if isinstance(identifiers, list):
                result[field] = [item for item in identifiers if item in kept]
    return result, removed


def _attachment_ids(value: object) -> set[str]:
    result: set[str] = set()
    if isinstance(value, Mapping):
        attachment_id = value.get("attachment_id")
        if isinstance(attachment_id, str) and attachment_id.startswith("sha256:"):
            result.add(attachment_id)
        for item in value.values():
            result.update(_attachment_ids(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            result.update(_attachment_ids(item))
    return result


__all__ = ("reconcile_product_rule_revisions",)
