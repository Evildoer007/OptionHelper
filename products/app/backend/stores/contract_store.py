"""Immutable App task binding for Core-resolved contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from runtime.protocol.version import RESOLVED_CONTRACT_SCHEMA_ID

from ..errors import AuthorizationError, ValidationError
from ..identity.session_identity import SessionIdentity
from . import _LocalDocumentStore


class ContractStore:
    def __init__(self, state: _LocalDocumentStore) -> None:
        self._state = state

    def bind(
        self,
        identity: SessionIdentity,
        task_id: str,
        prepared: Mapping[str, Any],
        *,
        catalog_version: str,
    ) -> dict[str, Any]:
        task = self._state.read("tasks").get(task_id)
        if not isinstance(task, dict) or task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
            raise AuthorizationError("contract.bind", "task is not owned by current caller")
        key = f"{identity.tenant_id}:{task_id}"
        record = self._record(identity, task_id, prepared, catalog_version=catalog_version)

        def update(value: dict[str, Any]) -> dict[str, Any]:
            existing = value.get(key)
            if existing is not None:
                if not isinstance(existing, dict) or any(existing.get(field) != record[field] for field in (
                    "tenant_id", "task_id", "created_by", "catalog_version", "contract_fingerprint",
                    "product_version", "registry_snapshot_hash", "product_snapshot_hash", "product_paths_hash",
                )):
                    raise ValidationError("当前任务已绑定另一份ResolvedContract；请新建任务")
                return value
            value[key] = record
            return value

        return self._state.update("contracts", update)[key]

    def activate_new_variant(
        self,
        identity: SessionIdentity,
        task_id: str,
        prepared: Mapping[str, Any],
        *,
        catalog_version: str,
        expected_contract_fingerprint: str,
    ) -> dict[str, Any]:
        """Activate a newly compiled scheme while retaining the prior contract.

        A research task may contain several product schemes.  Each completed
        module run already carries its own ResolvedContract fingerprint, so
        switching the *active* scheme must archive the former binding instead
        of overwriting it.  The compare-and-swap fingerprint prevents a stale
        module page from replacing a scheme selected in another window.
        """

        task = self._state.read("tasks").get(task_id)
        if not isinstance(task, dict) or task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
            raise AuthorizationError("contract.variant", "task is not owned by current caller")
        if not isinstance(expected_contract_fingerprint, str) or len(expected_contract_fingerprint) != 64:
            raise ValidationError("当前ResolvedContract指纹无效")
        replacement = self._record(identity, task_id, prepared, catalog_version=catalog_version)
        if replacement["contract_fingerprint"] == expected_contract_fingerprint:
            raise ValidationError("新方案必须使用不同的ResolvedContract")
        key = f"{identity.tenant_id}:{task_id}"

        def update(value: dict[str, Any]) -> dict[str, Any]:
            existing = value.get(key)
            if not isinstance(existing, dict):
                raise ValidationError("当前任务没有可切换的ResolvedContract")
            if existing.get("contract_fingerprint") != expected_contract_fingerprint:
                raise ValidationError("当前任务的ResolvedContract已变化；请刷新后重试")
            history = existing.get("contract_history", [])
            if not isinstance(history, list) or not all(isinstance(item, Mapping) for item in history):
                raise ValidationError("当前任务的ResolvedContract历史无效")
            value[key] = {
                **replacement,
                "active_variant_id": _variant_id(replacement),
                "contract_history": [*history, _active_snapshot(existing)],
                "variant_activated_at": datetime.now(timezone.utc).isoformat(),
            }
            return value

        return self._state.update("contracts", update)[key]

    def upgrade_legacy_calendar_evidence(
        self,
        identity: SessionIdentity,
        task_id: str,
        prepared: Mapping[str, Any],
        *,
        catalog_version: str,
        expected_contract_fingerprint: str,
    ) -> dict[str, Any]:
        """Replace only an early unverified calendar declaration.

        Old Desk builds could freeze a vanilla contract before historical data
        carried provider-verified calendar provenance.  That missing evidence
        must not make a current, otherwise identical task permanently
        unusable.  This narrow migration accepts a newly compiled contract
        only when every economic fact is unchanged and the old contract has no
        resolved observation schedule.  Structured contracts therefore stay
        immutable and require a new research task instead of silently moving
        observation dates.
        """

        task = self._state.read("tasks").get(task_id)
        if not isinstance(task, dict) or task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
            raise AuthorizationError("contract.calendar_upgrade", "task is not owned by current caller")
        if not isinstance(expected_contract_fingerprint, str) or len(expected_contract_fingerprint) != 64:
            raise ValidationError("旧ResolvedContract指纹无效")
        replacement = self._record(identity, task_id, prepared, catalog_version=catalog_version)
        key = f"{identity.tenant_id}:{task_id}"

        def update(value: dict[str, Any]) -> dict[str, Any]:
            existing = value.get(key)
            if not isinstance(existing, dict):
                raise ValidationError("当前任务没有可升级的ResolvedContract")
            if existing.get("contract_fingerprint") != expected_contract_fingerprint:
                raise ValidationError("当前任务的ResolvedContract已变化；请刷新后重试")
            _require_calendar_evidence_only_change(existing, replacement)
            value[key] = {
                **replacement,
                "created_at": existing.get("created_at", replacement["created_at"]),
                "active_variant_id": existing.get("active_variant_id", _variant_id(replacement)),
                "contract_history": existing.get("contract_history", []),
                "calendar_evidence_migration": {
                    "from_contract_fingerprint": expected_contract_fingerprint,
                    "from_calendar_id": _calendar_fact(existing, "calendar_id"),
                    "from_calendar_revision": _calendar_fact(existing, "calendar_revision"),
                    "migrated_at": datetime.now(timezone.utc).isoformat(),
                },
            }
            return value

        return self._state.update("contracts", update)[key]

    @staticmethod
    def _record(
        identity: SessionIdentity,
        task_id: str,
        prepared: Mapping[str, Any],
        *,
        catalog_version: str,
    ) -> dict[str, Any]:
        fingerprint = prepared.get("contract_fingerprint")
        contract = prepared.get("resolved_contract")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or not isinstance(contract, Mapping):
            raise ValidationError("Core ResolvedContract绑定事实无效")
        return {
            "tenant_id": identity.tenant_id,
            "task_id": task_id,
            "created_by": identity.principal_id,
            "catalog_version": catalog_version,
            "contract_fingerprint": fingerprint,
            "contract_ref": {
                "reference_id": f"contract-{fingerprint[:24]}",
                "schema_id": RESOLVED_CONTRACT_SCHEMA_ID,
                "content_hash": fingerprint,
            },
            "resolved_contract": dict(contract),
            "product_version": prepared.get("product_version"),
            "registry_snapshot_hash": prepared.get("registry_snapshot_hash"),
            "product_snapshot_hash": prepared.get("product_snapshot_hash"),
            "product_paths_hash": prepared.get("product_paths_hash"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def get(self, identity: SessionIdentity, task_id: str) -> dict[str, Any] | None:
        value = self._state.read("contracts").get(f"{identity.tenant_id}:{task_id}")
        if value is None:
            return None
        if not isinstance(value, dict) or value.get("created_by") != identity.principal_id:
            raise AuthorizationError("contract.read", "contract is not owned by current caller")
        return value

    def get_current(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        catalog_version: str,
    ) -> dict[str, Any] | None:
        """Return a task binding only when it belongs to the current protocol.

        Conversation history can outlive a Capability upgrade.  Such a task
        remains readable, but its frozen contract must not be injected into a
        newer Module Host context.  The module therefore opens unbound and the
        user can create a new formal result under the current catalog.
        """

        value = self.get(identity, task_id)
        if value is None or value.get("catalog_version") != catalog_version:
            return None
        contract_ref = value.get("contract_ref")
        if not isinstance(contract_ref, Mapping) or contract_ref.get("schema_id") != RESOLVED_CONTRACT_SCHEMA_ID:
            return None
        return value


_CALENDAR_FACTS = frozenset({"calendar_id", "calendar_revision"})


def _require_calendar_evidence_only_change(existing: Mapping[str, Any], replacement: Mapping[str, Any]) -> None:
    """Fail closed unless a vanilla contract changes only its calendar facts."""

    shared_fields = (
        "tenant_id", "task_id", "created_by", "catalog_version", "product_version",
        "registry_snapshot_hash", "product_snapshot_hash", "product_paths_hash",
    )
    if any(existing.get(field) != replacement.get(field) for field in shared_fields):
        raise ValidationError("旧ResolvedContract与新目录快照不一致；请新建研究任务")
    source = existing.get("resolved_contract")
    target = replacement.get("resolved_contract")
    if not isinstance(source, Mapping) or not isinstance(target, Mapping):
        raise ValidationError("旧ResolvedContract内容无效")
    source_identity = source.get("identity")
    target_identity = target.get("identity")
    if not isinstance(source_identity, Mapping) or not isinstance(target_identity, Mapping):
        raise ValidationError("旧ResolvedContract identity无效")
    if not _has_unverified_calendar(source_identity):
        raise ValidationError("当前任务不需要升级交易日历证据")
    if not _has_verified_calendar(target_identity):
        raise ValidationError("新交易日历证据无效")
    if any(
        source_identity.get(field) != target_identity.get(field)
        for field in set(source_identity) | set(target_identity)
        if field not in _CALENDAR_FACTS
    ):
        raise ValidationError("交易日历升级不得改变产品或合同身份")
    if any(source.get(field) != target.get(field) for field in ("terms", "term_sources", "paths", "resolved_schedules")):
        raise ValidationError("交易日历升级不得改变产品条款、收益路径或观察日")


def _calendar_fact(record: Mapping[str, Any], field: str) -> object:
    contract = record.get("resolved_contract")
    identity = contract.get("identity") if isinstance(contract, Mapping) else None
    return identity.get(field) if isinstance(identity, Mapping) else None


def _has_unverified_calendar(identity: Mapping[str, Any]) -> bool:
    calendar_id = identity.get("calendar_id")
    revision = identity.get("calendar_revision")
    return (
        not isinstance(calendar_id, str)
        or not calendar_id.startswith("CN-")
        or not isinstance(revision, str)
        or not revision.strip()
        or revision.casefold() in {"unverified", "unknown", "derived", "frame-sessions"}
    )


def _has_verified_calendar(identity: Mapping[str, Any]) -> bool:
    return not _has_unverified_calendar(identity)


def _variant_id(record: Mapping[str, Any]) -> str:
    return f"candidate-{str(record['contract_fingerprint'])[:24]}"


def _active_snapshot(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the active binding without recursively embedding its history."""

    snapshot = {key: value for key, value in record.items() if key != "contract_history"}
    snapshot["variant_id"] = str(record.get("active_variant_id") or _variant_id(record))
    return snapshot


__all__ = ("ContractStore",)
