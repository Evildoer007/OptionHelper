"""Immutable App task binding for Core-resolved contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

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
        fingerprint = prepared.get("contract_fingerprint")
        contract = prepared.get("resolved_contract")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or not isinstance(contract, Mapping):
            raise ValidationError("Core ResolvedContract绑定事实无效")
        key = f"{identity.tenant_id}:{task_id}"
        record = {
            "tenant_id": identity.tenant_id,
            "task_id": task_id,
            "created_by": identity.principal_id,
            "catalog_version": catalog_version,
            "contract_fingerprint": fingerprint,
            "contract_ref": {
                "reference_id": f"contract-{fingerprint[:24]}",
                "schema_id": "optionhelper.resolved-contract/v1",
                "content_hash": fingerprint,
            },
            "resolved_contract": dict(contract),
            "product_version": prepared.get("product_version"),
            "registry_snapshot_hash": prepared.get("registry_snapshot_hash"),
            "product_snapshot_hash": prepared.get("product_snapshot_hash"),
            "product_paths_hash": prepared.get("product_paths_hash"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

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

    def get(self, identity: SessionIdentity, task_id: str) -> dict[str, Any] | None:
        value = self._state.read("contracts").get(f"{identity.tenant_id}:{task_id}")
        if value is None:
            return None
        if not isinstance(value, dict) or value.get("created_by") != identity.principal_id:
            raise AuthorizationError("contract.read", "contract is not owned by current caller")
        return value


__all__ = ("ContractStore",)
