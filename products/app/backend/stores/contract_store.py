"""Task-local, append-only contract schemes for Core-resolved contracts.

A task is a research container, not a contract lock.  The store therefore
keeps every immutable contract version and separate per-module view
preferences.  The small latest-record projection is retained only so older
result and recommendation readers can be migrated without losing evidence;
normal module execution never treats it as an active contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping

from runtime.contracts.contract_api import ContractResolutionError, ResolvedContract
from runtime.protocol.module_host import HostObjectRef
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
        return self.save_version(
            identity,
            task_id,
            prepared,
            catalog_version=catalog_version,
        )

    def save_version(
        self,
        identity: SessionIdentity,
        task_id: str,
        prepared: Mapping[str, Any],
        *,
        catalog_version: str,
        module: str | None = None,
        parent_contract_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Append one immutable version and optionally update a module view.

        The operation intentionally has no compare-and-swap against a task-wide
        pointer.  Two editors may derive sibling versions from the same parent;
        neither replaces the other.
        """

        self._require_task_owner(identity, task_id, action="contract.version.write")
        if module is not None and module not in _MODULE_SELECTION_NAMES:
            raise ValidationError("合同版本模块选择无效")
        if parent_contract_fingerprint is not None:
            _require_contract_fingerprint(parent_contract_fingerprint, "基础合同版本指纹无效")
        record = self._record(identity, task_id, prepared, catalog_version=catalog_version)
        key = f"{identity.tenant_id}:{task_id}"
        fingerprint = str(record["contract_fingerprint"])

        def update(value: dict[str, Any]) -> dict[str, Any]:
            existing = value.get(key)
            if existing is not None and not isinstance(existing, Mapping):
                raise ValidationError("当前任务合同版本索引无效")
            versions = _contract_versions(existing)
            if parent_contract_fingerprint is not None and parent_contract_fingerprint not in versions:
                raise ValidationError("基础合同版本不存在或不属于当前任务")
            stored = versions.get(fingerprint)
            if stored is not None:
                _require_same_contract_version(stored, record)
                version = dict(stored)
            else:
                parent = versions.get(parent_contract_fingerprint) if parent_contract_fingerprint else None
                scheme_id = (
                    str(parent.get("scheme_id"))
                    if isinstance(parent, Mapping) and isinstance(parent.get("scheme_id"), str)
                    else f"scheme-{fingerprint[:24]}"
                )
                version = {
                    **record,
                    "scheme_id": scheme_id,
                    "version_id": f"version-{fingerprint[:24]}",
                    "parent_contract_fingerprint": parent_contract_fingerprint,
                }
                versions[fingerprint] = version
            selections = _module_selections(existing)
            if module is not None:
                selections[module] = fingerprint
            history = _legacy_contract_history(existing)
            if isinstance(existing, Mapping) and _is_active_contract(existing):
                old_fingerprint = str(existing.get("contract_fingerprint", ""))
                if old_fingerprint and old_fingerprint != fingerprint and not any(
                    item.get("contract_fingerprint") == old_fingerprint for item in history
                ):
                    history.append(_active_snapshot(existing))
            candidates = _candidate_variants(existing) if isinstance(existing, Mapping) else {}
            value[key] = {
                **version,
                "record_kind": _CONTRACT_INDEX_KIND,
                "latest_contract_fingerprint": fingerprint,
                "contract_versions": versions,
                "module_selections": selections,
                "contract_history": history,
                "candidate_contract_variants": candidates,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            value[key].pop("active_variant_id", None)
            return value

        stored_index = self._state.update("contracts", update)[key]
        return dict(_contract_versions(stored_index)[fingerprint])

    def list_versions(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        catalog_version: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return every saved version without choosing one for the task."""

        record = self._get_task_contract(identity, task_id)
        if record is None:
            return []
        versions = [dict(item) for item in _contract_versions(record).values()]
        if catalog_version is not None:
            versions = [item for item in versions if item.get("catalog_version") == catalog_version]
        return sorted(versions, key=lambda item: (str(item.get("created_at", "")), str(item.get("contract_fingerprint", ""))))

    def get_module_selection(
        self,
        identity: SessionIdentity,
        task_id: str,
        module: str,
        *,
        catalog_version: str | None = None,
    ) -> dict[str, Any] | None:
        if module not in _MODULE_SELECTION_NAMES:
            raise ValidationError("合同版本模块选择无效")
        record = self._get_task_contract(identity, task_id)
        if record is None:
            return None
        fingerprint = _module_selections(record).get(module)
        selected = _contract_versions(record).get(fingerprint) if fingerprint else None
        if selected is None or (catalog_version is not None and selected.get("catalog_version") != catalog_version):
            return None
        return dict(selected)

    def select_module_version(
        self,
        identity: SessionIdentity,
        task_id: str,
        module: str,
        contract_fingerprint: str,
    ) -> dict[str, Any]:
        """Persist a UI preference; it has no authorization semantics."""

        self._require_task_owner(identity, task_id, action="contract.selection.write")
        if module not in _MODULE_SELECTION_NAMES:
            raise ValidationError("合同版本模块选择无效")
        _require_contract_fingerprint(contract_fingerprint, "合同版本指纹无效")
        key = f"{identity.tenant_id}:{task_id}"

        def update(value: dict[str, Any]) -> dict[str, Any]:
            record = value.get(key)
            if not isinstance(record, Mapping):
                raise ValidationError("当前任务没有合同版本")
            if contract_fingerprint not in _contract_versions(record):
                raise ValidationError("合同版本不存在或不属于当前任务")
            selections = _module_selections(record)
            selections[module] = contract_fingerprint
            value[key] = {**record, "module_selections": selections, "updated_at": datetime.now(timezone.utc).isoformat()}
            return value

        stored = self._state.update("contracts", update)[key]
        return dict(_contract_versions(stored)[contract_fingerprint])

    def preview_initial(
        self,
        identity: SessionIdentity,
        task_id: str,
        prepared: Mapping[str, Any],
        *,
        catalog_version: str,
    ) -> dict[str, Any]:
        """Validate a first preview without creating an active task contract."""

        self._require_task_owner(identity, task_id, action="contract.preview")
        return self._record(identity, task_id, prepared, catalog_version=catalog_version)

    def activate_new_variant(
        self,
        identity: SessionIdentity,
        task_id: str,
        prepared: Mapping[str, Any],
        *,
        catalog_version: str,
        expected_contract_fingerprint: str,
    ) -> dict[str, Any]:
        """Compatibility alias for appending a child contract version."""

        self.preview_new_variant(
            identity,
            task_id,
            prepared,
            catalog_version=catalog_version,
            expected_contract_fingerprint=expected_contract_fingerprint,
        )
        return self.save_version(
            identity,
            task_id,
            prepared,
            catalog_version=catalog_version,
            parent_contract_fingerprint=expected_contract_fingerprint,
        )

    def preview_new_variant(
        self,
        identity: SessionIdentity,
        task_id: str,
        prepared: Mapping[str, Any],
        *,
        catalog_version: str,
        expected_contract_fingerprint: str,
    ) -> dict[str, Any]:
        """Validate a new scheme without changing the task's active contract.

        Calculators need the new contract identity before execution, but a
        failed calculation must not replace the last successful scheme.  The
        caller may use this immutable preview for authorization and result
        binding, then call :meth:`activate_new_variant` only after a successful
        durable calculation.
        """

        self._require_task_owner(identity, task_id, action="contract.variant")
        _require_contract_fingerprint(expected_contract_fingerprint, "基础合同版本指纹无效")
        existing = self._state.read("contracts").get(f"{identity.tenant_id}:{task_id}")
        if not isinstance(existing, Mapping) or expected_contract_fingerprint not in _contract_versions(existing):
            raise ValidationError("基础合同版本不存在或不属于当前任务")
        replacement = self._record(identity, task_id, prepared, catalog_version=catalog_version)
        if replacement["contract_fingerprint"] == expected_contract_fingerprint:
            raise ValidationError("新方案必须使用不同的ResolvedContract")
        parent = _contract_versions(existing)[expected_contract_fingerprint]
        return {
            **replacement,
            "scheme_id": str(parent.get("scheme_id") or f"scheme-{expected_contract_fingerprint[:24]}"),
            "version_id": f"version-{replacement['contract_fingerprint'][:24]}",
            "parent_contract_fingerprint": expected_contract_fingerprint,
        }

    def refresh_calendar_evidence(
        self,
        identity: SessionIdentity,
        task_id: str,
        prepared: Mapping[str, Any],
        *,
        catalog_version: str,
        expected_contract_fingerprint: str,
    ) -> dict[str, Any]:
        """Refresh only the calendar evidence of an economically unchanged contract.

        A wider historical backtest can require a longer provider-verified
        calendar snapshot than the one originally frozen into a vanilla
        contract.  This narrow refresh accepts a newly compiled contract only
        when every economic fact is unchanged and the old contract has no
        resolved observation schedule.  Structured contracts therefore stay
        immutable and never move observation dates silently.
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
            versions = _contract_versions(existing)
            source = versions.get(expected_contract_fingerprint)
            if not isinstance(source, Mapping):
                raise ValidationError("基础合同版本不存在或不属于当前任务")
            _require_calendar_evidence_only_change(source, replacement)
            fingerprint = str(replacement["contract_fingerprint"])
            refreshed_version = {
                **replacement,
                "scheme_id": str(source.get("scheme_id") or f"scheme-{expected_contract_fingerprint[:24]}"),
                "version_id": f"version-{fingerprint[:24]}",
                "parent_contract_fingerprint": expected_contract_fingerprint,
            }
            versions[fingerprint] = refreshed_version
            value[key] = {
                **refreshed_version,
                "record_kind": _CONTRACT_INDEX_KIND,
                "latest_contract_fingerprint": fingerprint,
                "contract_versions": versions,
                "module_selections": _module_selections(existing),
                "contract_history": _legacy_contract_history(existing),
                "candidate_contract_variants": _candidate_variants(existing),
                "calendar_evidence_refresh": {
                    "from_contract_fingerprint": expected_contract_fingerprint,
                    "from_calendar_id": _calendar_fact(existing, "calendar_id"),
                    "from_calendar_revision": _calendar_fact(existing, "calendar_revision"),
                    "to_calendar_id": _calendar_fact(replacement, "calendar_id"),
                    "to_calendar_revision": _calendar_fact(replacement, "calendar_revision"),
                    "refreshed_at": datetime.now(timezone.utc).isoformat(),
                },
                "calendar_evidence_migration": {
                    "from_contract_fingerprint": expected_contract_fingerprint,
                    "from_calendar_id": _calendar_fact(existing, "calendar_id"),
                    "from_calendar_revision": _calendar_fact(existing, "calendar_revision"),
                    "migrated_at": datetime.now(timezone.utc).isoformat(),
                },
            }
            value[key].pop("active_variant_id", None)
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
        """Compatibility alias for callers using the retired migration name."""

        return self.refresh_calendar_evidence(
            identity,
            task_id,
            prepared,
            catalog_version=catalog_version,
            expected_contract_fingerprint=expected_contract_fingerprint,
        )

    def put_candidate_variant(
        self,
        identity: SessionIdentity,
        task_id: str,
        candidate_key: str,
        candidate_version_id: str,
        prepared: Mapping[str, Any],
        *,
        catalog_version: str,
        parent_version_id: str | None = None,
        revision: int = 0,
    ) -> dict[str, Any]:
        """Persist an immutable pre-selection contract without activating it.

        Recommendation candidates are evaluated before the user has selected a
        contract.  They therefore belong beside, rather than in place of, the
        task's active contract binding.  A version id is task-scoped: it can
        be written again only for the exact same candidate contract.
        """

        self._require_task_owner(identity, task_id, action="contract.candidate_variant")
        _require_candidate_identifier("candidate_key", candidate_key)
        _require_candidate_identifier("candidate_version_id", candidate_version_id)
        if parent_version_id is not None:
            _require_candidate_identifier("parent_version_id", parent_version_id)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValidationError("候选合同revision无效")
        candidate = {
            **self._record(identity, task_id, prepared, catalog_version=catalog_version),
            "candidate_key": candidate_key,
            "candidate_version_id": candidate_version_id,
            "parent_version_id": parent_version_id,
            "revision": revision,
            "candidate_saved_at": datetime.now(timezone.utc).isoformat(),
        }
        key = f"{identity.tenant_id}:{task_id}"

        def update(value: dict[str, Any]) -> dict[str, Any]:
            task_contract = value.get(key)
            if task_contract is None:
                task_contract = _candidate_container(identity, task_id)
            elif not isinstance(task_contract, dict) or not (
                _is_active_contract(task_contract) or _is_candidate_container(task_contract)
            ):
                raise ValidationError("当前任务合同记录无效")
            variants = _candidate_variants(task_contract)
            _assert_candidate_version_fingerprint(variants, candidate_version_id, candidate["contract_fingerprint"])
            candidate_bucket = variants.setdefault(candidate_key, {})
            existing = candidate_bucket.get(candidate_version_id)
            if existing is not None:
                if not isinstance(existing, Mapping) or not _same_candidate_variant(existing, candidate):
                    raise ValidationError("候选合同版本已存在且内容不一致")
                return value
            candidate_bucket[candidate_version_id] = candidate
            value[key] = {**task_contract, "candidate_contract_variants": variants}
            return value

        stored = self._state.update("contracts", update)[key]
        return _read_candidate_variant(stored, candidate_key, candidate_version_id)

    def get_candidate_variant(
        self,
        identity: SessionIdentity,
        task_id: str,
        candidate_key: str,
        candidate_version_id: str,
    ) -> dict[str, Any] | None:
        """Read one candidate version by its task-local immutable identity."""

        _require_candidate_identifier("candidate_key", candidate_key)
        _require_candidate_identifier("candidate_version_id", candidate_version_id)
        task_contract = self._get_task_contract(identity, task_id)
        if task_contract is None:
            return None
        variants = _candidate_variants(task_contract)
        candidate = variants.get(candidate_key, {}).get(candidate_version_id)
        return dict(candidate) if isinstance(candidate, Mapping) else None

    def get_candidate_variant_by_fingerprint(
        self,
        identity: SessionIdentity,
        task_id: str,
        contract_fingerprint: str,
    ) -> dict[str, Any] | None:
        """Read a stored candidate by a Host-context contract fingerprint.

        This lookup is deliberately read-only.  ToolGateway may use it to
        resolve a signed Host context without accidentally switching the task's
        active contract.
        """

        _require_contract_fingerprint(contract_fingerprint, "候选ResolvedContract指纹无效")
        task_contract = self._get_task_contract(identity, task_id)
        if task_contract is None:
            return None
        matches: list[Mapping[str, Any]] = []
        variants = _candidate_variants(task_contract)
        for candidate_key in sorted(variants):
            bucket = variants[candidate_key]
            for candidate_version_id in sorted(bucket):
                candidate = bucket[candidate_version_id]
                if isinstance(candidate, Mapping) and candidate.get("contract_fingerprint") == contract_fingerprint:
                    matches.append(candidate)
        if not matches:
            return None
        return dict(matches[0])

    def anchor_candidate_ranking_spec(
        self,
        identity: SessionIdentity,
        task_id: str,
        candidate_key: str,
        candidate_version_id: str,
        ranking_spec: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Attach one write-once deterministic ranking anchor to a candidate.

        Candidate economics remain immutable.  This narrow sidecar is a
        provenance anchor added at confirmation preparation; it may be written
        once and subsequently must match byte-for-byte after normalization.
        """

        self._require_task_owner(identity, task_id, action="contract.anchor_candidate_ranking_spec")
        _require_candidate_identifier("candidate_key", candidate_key)
        _require_candidate_identifier("candidate_version_id", candidate_version_id)
        anchor = _ranking_spec_anchor(ranking_spec)
        key = f"{identity.tenant_id}:{task_id}"

        def update(value: dict[str, Any]) -> dict[str, Any]:
            record = value.get(key)
            if not isinstance(record, dict):
                raise ValidationError("当前任务没有候选合同版本")
            variants = _candidate_variants(record)
            candidate = variants.get(candidate_key, {}).get(candidate_version_id)
            if not isinstance(candidate, dict):
                raise ValidationError("候选合同版本不存在")
            existing = candidate.get("ranking_spec_anchor")
            if existing is not None:
                if not isinstance(existing, Mapping) or _ranking_spec_anchor(existing) != anchor:
                    raise ValidationError("候选合同已绑定不同RankingSpec")
                return value
            candidate["ranking_spec_anchor"] = anchor
            variants[candidate_key][candidate_version_id] = candidate
            value[key] = {**record, "candidate_contract_variants": variants}
            return value

        stored = self._state.update("contracts", update)[key]
        candidate = _read_candidate_variant(stored, candidate_key, candidate_version_id)
        if candidate is None:
            raise ValidationError("候选合同版本不存在")
        return candidate

    def activate_candidate_variant(
        self,
        identity: SessionIdentity,
        task_id: str,
        candidate_key: str,
        candidate_version_id: str,
        *,
        expected_active_contract_fingerprint: str | None,
        approval_operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Prepare one candidate activation without exposing it to normal modules.

        An approval spans TaskService and ContractStore.  When an operation id
        is supplied, the new active binding stays ``approval_prepared`` until
        ``commit_candidate_variant_activation`` runs.  ``get`` and
        ``get_current`` deliberately hide that binding, so a crash cannot make
        an uncommitted recommendation available to ordinary module calls.
        """

        self._require_task_owner(identity, task_id, action="contract.activate_candidate_variant")
        _require_candidate_identifier("candidate_key", candidate_key)
        _require_candidate_identifier("candidate_version_id", candidate_version_id)
        if expected_active_contract_fingerprint is not None:
            _require_contract_fingerprint(expected_active_contract_fingerprint, "当前ResolvedContract指纹无效")
        if approval_operation_id is not None:
            _require_candidate_identifier("approval_operation_id", approval_operation_id)
        key = f"{identity.tenant_id}:{task_id}"

        def update(value: dict[str, Any]) -> dict[str, Any]:
            existing = value.get(key)
            if not isinstance(existing, dict):
                raise ValidationError("当前任务没有候选合同版本")
            has_active_contract = _is_active_contract(existing)
            if not has_active_contract and not _is_candidate_container(existing):
                raise ValidationError("当前任务合同记录无效")
            candidate = _read_candidate_variant(existing, candidate_key, candidate_version_id)
            if candidate is None:
                raise ValidationError("候选合同版本不存在")
            if (
                has_active_contract
                and existing.get("active_candidate_key") == candidate_key
                and existing.get("active_candidate_version_id") == candidate_version_id
                and existing.get("contract_fingerprint") == candidate.get("contract_fingerprint")
            ):
                prepared_operation = existing.get("approval_operation_id")
                if prepared_operation is not None and prepared_operation != approval_operation_id:
                    raise ValidationError("当前候选合同正由另一确认操作提交")
                return value
            variants = _candidate_variants(existing)
            versions = _contract_versions(existing)
            if expected_active_contract_fingerprint is not None and expected_active_contract_fingerprint not in versions:
                raise ValidationError("候选合同的基础版本不存在或不属于当前任务")
            candidate_record = _contract_record_from_candidate(candidate)
            fingerprint = str(candidate_record["contract_fingerprint"])
            parent = versions.get(expected_active_contract_fingerprint or "")
            version = {
                **candidate_record,
                "scheme_id": str(parent.get("scheme_id")) if isinstance(parent, Mapping) else f"scheme-{fingerprint[:24]}",
                "version_id": f"version-{fingerprint[:24]}",
                "parent_contract_fingerprint": expected_active_contract_fingerprint,
            }
            versions[fingerprint] = version
            value[key] = {
                **version,
                "record_kind": _CONTRACT_INDEX_KIND,
                "latest_contract_fingerprint": fingerprint,
                "contract_versions": versions,
                "module_selections": _module_selections(existing),
                "active_candidate_key": candidate_key,
                "active_candidate_version_id": candidate_version_id,
                "contract_history": _legacy_contract_history(existing),
                "candidate_contract_variants": variants,
                "variant_activated_at": datetime.now(timezone.utc).isoformat(),
                **(
                    {
                        "activation_state": "approval_prepared",
                        "approval_operation_id": approval_operation_id,
                    }
                    if approval_operation_id is not None else {}
                ),
            }
            value[key].pop("active_variant_id", None)
            return value

        return self._state.update("contracts", update)[key]

    def commit_candidate_variant_activation(
        self,
        identity: SessionIdentity,
        task_id: str,
        operation_id: str,
    ) -> dict[str, Any] | None:
        """Make a prepared candidate activation visible after Task approval.

        The method is idempotent.  It is used both on the normal path and by
        startup/request recovery after a crash between the two Store commits.
        """

        self._require_task_owner(identity, task_id, action="contract.commit_candidate_variant_activation")
        _require_candidate_identifier("approval_operation_id", operation_id)
        key = f"{identity.tenant_id}:{task_id}"

        def update(value: dict[str, Any]) -> dict[str, Any]:
            existing = value.get(key)
            if not isinstance(existing, dict) or not _is_active_contract(existing):
                raise ValidationError("候选合同确认记录不存在")
            state = existing.get("activation_state")
            prepared_operation = existing.get("approval_operation_id")
            if state is None and prepared_operation is None:
                return value
            if state != "approval_prepared" or prepared_operation != operation_id:
                raise ValidationError("候选合同确认操作不匹配")
            value[key] = {
                **existing,
                "activation_state": "active",
                "activation_committed_at": datetime.now(timezone.utc).isoformat(),
            }
            value[key].pop("approval_operation_id", None)
            return value

        stored = self._state.update("contracts", update).get(key)
        return dict(stored) if isinstance(stored, Mapping) else None

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
        """Compatibility projection of the newest stored version.

        New code must use ``list_versions`` or ``get_module_selection``.  This
        method does not confer active-contract semantics.
        """
        value = self._get_task_contract(identity, task_id)
        return None if value is None or _is_candidate_container(value) or _activation_is_prepared(value) else value

    def _get_task_contract(self, identity: SessionIdentity, task_id: str) -> dict[str, Any] | None:
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
        if value is None:
            return None
        fingerprint = value.get("latest_contract_fingerprint", value.get("contract_fingerprint"))
        selected = _contract_versions(value).get(str(fingerprint))
        if selected is None or selected.get("catalog_version") != catalog_version:
            return None
        contract_ref = selected.get("contract_ref")
        if not isinstance(contract_ref, Mapping) or contract_ref.get("schema_id") != RESOLVED_CONTRACT_SCHEMA_ID:
            return None
        return dict(selected)

    def resolve_ref(
        self,
        identity: SessionIdentity,
        task_id: str,
        reference: HostObjectRef,
        *,
        catalog_version: str,
    ) -> dict[str, Any]:
        """Resolve one signed Host reference to an owned, valid Core contract."""

        if not isinstance(reference, HostObjectRef):
            raise ValidationError("Host contract_ref无效")
        index = self._get_task_contract(identity, task_id)
        if index is None:
            raise ValidationError("合同版本引用不存在")
        value = next(
            (
                item for item in _contract_versions(index).values()
                if item.get("catalog_version") == catalog_version
                and isinstance(item.get("contract_ref"), Mapping)
                and dict(item["contract_ref"]) == reference.to_payload()
            ),
            None,
        )
        if value is None:
            raise ValidationError("合同版本引用不存在或不属于当前任务")
        stored_ref = value.get("contract_ref")
        snapshot = value.get("resolved_contract")
        try:
            contract = ResolvedContract.from_controlled_snapshot(snapshot)
        except ContractResolutionError as error:
            raise ValidationError("Host contract_ref指向的Core合同无效") from error
        if (
            contract.contract_fingerprint != reference.content_hash
            or value.get("contract_fingerprint") != contract.contract_fingerprint
            or value.get("product_version") != contract.product_version
            or value.get("registry_snapshot_hash") != contract.registry_snapshot_hash
            or value.get("product_snapshot_hash") != contract.product_snapshot_hash
            or value.get("product_paths_hash") != contract.product_paths_hash
        ):
            raise ValidationError("Host contract_ref与Core合同快照绑定不一致")
        return {**value, "resolved_contract": contract.to_controlled_snapshot()}

    def _require_task_owner(self, identity: SessionIdentity, task_id: str, *, action: str) -> None:
        task = self._state.read("tasks").get(task_id)
        if not isinstance(task, dict) or task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
            raise AuthorizationError(action, "task is not owned by current caller")


_CALENDAR_FACTS = frozenset({"calendar_id", "calendar_revision"})
_CANDIDATE_CONTAINER_KIND = "candidate-contract-container"
_CONTRACT_INDEX_KIND = "contract-version-index-v2"
_MODULE_SELECTION_NAMES = frozenset({"payoffer", "pricer", "backtester"})
_CANDIDATE_RECORD_FIELDS = (
    "tenant_id", "task_id", "created_by", "catalog_version", "contract_fingerprint",
    "contract_ref", "resolved_contract", "product_version", "registry_snapshot_hash",
    "product_snapshot_hash", "product_paths_hash", "candidate_key", "candidate_version_id",
    "parent_version_id", "revision",
)


def _candidate_container(identity: SessionIdentity, task_id: str) -> dict[str, Any]:
    return {
        "record_kind": _CANDIDATE_CONTAINER_KIND,
        "tenant_id": identity.tenant_id,
        "task_id": task_id,
        "created_by": identity.principal_id,
        "candidate_contract_variants": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _legacy_contract_history(record: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(record, Mapping):
        return []
    raw = record.get("contract_history", [])
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise ValidationError("当前任务的合同版本历史无效")
    return [dict(item) for item in raw]


def _module_selections(record: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(record, Mapping):
        return {}
    raw = record.get("module_selections", {})
    if not isinstance(raw, Mapping):
        raise ValidationError("合同版本模块选择索引无效")
    result: dict[str, str] = {}
    for module, fingerprint in raw.items():
        if module not in _MODULE_SELECTION_NAMES or not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise ValidationError("合同版本模块选择索引无效")
        result[str(module)] = fingerprint
    return result


def _version_from_legacy(record: Mapping[str, Any]) -> dict[str, Any] | None:
    if not _is_active_contract(record):
        return None
    fingerprint = str(record["contract_fingerprint"])
    return {
        **{
            key: value for key, value in record.items()
            if key not in {
                "contract_versions", "module_selections", "contract_history",
                "candidate_contract_variants", "latest_contract_fingerprint",
                "record_kind", "updated_at", "active_variant_id",
            }
        },
        "scheme_id": str(record.get("scheme_id") or f"scheme-{fingerprint[:24]}"),
        "version_id": str(record.get("version_id") or f"version-{fingerprint[:24]}"),
        "parent_contract_fingerprint": record.get("parent_contract_fingerprint"),
    }


def _contract_versions(record: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Read the v2 index and losslessly project legacy active/history records."""

    if not isinstance(record, Mapping):
        return {}
    raw = record.get("contract_versions", {})
    if not isinstance(raw, Mapping):
        raise ValidationError("当前任务合同版本索引无效")
    versions: dict[str, dict[str, Any]] = {}
    for fingerprint, item in raw.items():
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or not isinstance(item, Mapping):
            raise ValidationError("当前任务合同版本索引无效")
        if item.get("contract_fingerprint") != fingerprint:
            raise ValidationError("合同版本索引与合同指纹不一致")
        versions[fingerprint] = dict(item)
    latest_by_product: dict[str, str] = {}
    for fingerprint, item in sorted(
        versions.items(), key=lambda pair: (str(pair[1].get("created_at", "")), pair[0]),
    ):
        product_id = _version_product_id(item)
        if product_id:
            latest_by_product[product_id] = fingerprint
    legacy_records = [*_legacy_contract_history(record)]
    if _is_active_contract(record):
        # Keep the raw legacy shape here.  `_version_from_legacy` supplies a
        # default scheme id, so appending an already projected current record
        # would hide that it still needs to be linked after same-product
        # history during migration.
        legacy_records.append(dict(record))
    for item in legacy_records:
        projected = _version_from_legacy(item)
        if projected is None:
            continue
        fingerprint = str(projected["contract_fingerprint"])
        product_id = _version_product_id(projected)
        if product_id and fingerprint not in versions and "scheme_id" not in item:
            parent_fingerprint = latest_by_product.get(product_id)
            parent = versions.get(parent_fingerprint) if parent_fingerprint else None
            projected["scheme_id"] = (
                str(parent.get("scheme_id"))
                if isinstance(parent, Mapping)
                else f"scheme-{fingerprint[:24]}"
            )
            projected["parent_contract_fingerprint"] = parent_fingerprint
        versions.setdefault(fingerprint, projected)
        if product_id:
            latest_by_product[product_id] = fingerprint
    return versions


def _version_product_id(version: Mapping[str, Any]) -> str:
    contract = version.get("resolved_contract")
    identity = contract.get("identity") if isinstance(contract, Mapping) else None
    product_id = identity.get("product_id") if isinstance(identity, Mapping) else None
    return str(product_id) if isinstance(product_id, str) else ""


def _require_same_contract_version(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> None:
    immutable = (
        "tenant_id", "task_id", "created_by", "catalog_version", "contract_fingerprint",
        "contract_ref", "resolved_contract", "product_version", "registry_snapshot_hash",
        "product_snapshot_hash", "product_paths_hash",
    )
    if any(existing.get(field) != incoming.get(field) for field in immutable):
        raise ValidationError("相同合同指纹对应的合同版本内容不一致")


def _is_candidate_container(record: object) -> bool:
    return (
        isinstance(record, Mapping)
        and record.get("record_kind") == _CANDIDATE_CONTAINER_KIND
        and "contract_fingerprint" not in record
        and "resolved_contract" not in record
    )


def _is_active_contract(record: object) -> bool:
    if not isinstance(record, Mapping):
        return False
    fingerprint = record.get("contract_fingerprint")
    contract_ref = record.get("contract_ref")
    return (
        isinstance(fingerprint, str)
        and len(fingerprint) == 64
        and isinstance(record.get("resolved_contract"), Mapping)
        and isinstance(contract_ref, Mapping)
        and contract_ref.get("schema_id") == RESOLVED_CONTRACT_SCHEMA_ID
    )


def _require_candidate_identifier(field: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"候选合同{field}无效")


def _require_contract_fingerprint(value: object, message: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValidationError(message)


def _candidate_variants(record: Mapping[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """Return the task-local candidate index, rejecting malformed persistence."""

    raw = record.get("candidate_contract_variants", {})
    if not isinstance(raw, Mapping):
        raise ValidationError("候选合同版本索引无效")
    variants: dict[str, dict[str, dict[str, Any]]] = {}
    for candidate_key, raw_bucket in raw.items():
        if not isinstance(candidate_key, str) or not isinstance(raw_bucket, Mapping):
            raise ValidationError("候选合同版本索引无效")
        bucket: dict[str, dict[str, Any]] = {}
        for candidate_version_id, raw_candidate in raw_bucket.items():
            if not isinstance(candidate_version_id, str) or not isinstance(raw_candidate, Mapping):
                raise ValidationError("候选合同版本索引无效")
            if raw_candidate.get("candidate_key") != candidate_key or raw_candidate.get("candidate_version_id") != candidate_version_id:
                raise ValidationError("候选合同版本身份无效")
            bucket[candidate_version_id] = dict(raw_candidate)
        variants[candidate_key] = bucket
    return variants


def _assert_candidate_version_fingerprint(
    variants: Mapping[str, Mapping[str, Mapping[str, Any]]],
    candidate_version_id: str,
    contract_fingerprint: str,
) -> None:
    for bucket in variants.values():
        candidate = bucket.get(candidate_version_id)
        if candidate is not None and candidate.get("contract_fingerprint") != contract_fingerprint:
            raise ValidationError("同一候选合同版本不能绑定不同的ResolvedContract")


def _same_candidate_variant(existing: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    return all(existing.get(field) == candidate.get(field) for field in _CANDIDATE_RECORD_FIELDS)


def _read_candidate_variant(
    record: Mapping[str, Any], candidate_key: str, candidate_version_id: str,
) -> dict[str, Any] | None:
    candidate = _candidate_variants(record).get(candidate_key, {}).get(candidate_version_id)
    return dict(candidate) if isinstance(candidate, Mapping) else None


def _contract_record_from_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: candidate[key]
        for key in (
            "tenant_id", "task_id", "created_by", "catalog_version", "contract_fingerprint",
            "contract_ref", "resolved_contract", "product_version", "registry_snapshot_hash",
            "product_snapshot_hash", "product_paths_hash", "created_at",
        )
    }


def _require_calendar_evidence_only_change(existing: Mapping[str, Any], replacement: Mapping[str, Any]) -> None:
    """Fail closed unless a vanilla contract changes only its calendar facts."""

    shared_fields = (
        "tenant_id", "task_id", "created_by", "catalog_version", "product_version",
        "registry_snapshot_hash", "product_snapshot_hash", "product_paths_hash",
    )
    if any(existing.get(field) != replacement.get(field) for field in shared_fields):
        raise ValidationError("基础合同版本与当前目录快照不一致；请按当前目录重新编译合同版本")
    source = existing.get("resolved_contract")
    target = replacement.get("resolved_contract")
    if not isinstance(source, Mapping) or not isinstance(target, Mapping):
        raise ValidationError("旧ResolvedContract内容无效")
    source_identity = source.get("identity")
    target_identity = target.get("identity")
    if not isinstance(source_identity, Mapping) or not isinstance(target_identity, Mapping):
        raise ValidationError("旧ResolvedContract identity无效")
    if not _has_verified_calendar(target_identity):
        raise ValidationError("新交易日历证据无效")
    source_calendar_id = source_identity.get("calendar_id")
    target_calendar_id = target_identity.get("calendar_id")
    source_calendar_revision = source_identity.get("calendar_revision")
    target_calendar_revision = target_identity.get("calendar_revision")
    if _has_verified_calendar(source_identity) and source_calendar_id != target_calendar_id:
        raise ValidationError("交易日历证据刷新不得切换交易所日历")
    if source_calendar_id == target_calendar_id and source_calendar_revision == target_calendar_revision:
        raise ValidationError("当前任务的交易日历证据没有变化")
    if any(
        source_identity.get(field) != target_identity.get(field)
        for field in set(source_identity) | set(target_identity)
        if field not in _CALENDAR_FACTS
    ):
        raise ValidationError("交易日历升级不得改变产品或合同身份")
    source_schedules = source.get("resolved_schedules")
    if isinstance(source_schedules, Mapping) and source_schedules:
        raise ValidationError("含冻结观察日的合同不得自动刷新交易日历证据")
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


def _activation_is_prepared(record: Mapping[str, Any]) -> bool:
    """Whether a recommender activation is intentionally still invisible."""

    return (
        record.get("activation_state") == "approval_prepared"
        and isinstance(record.get("approval_operation_id"), str)
        and bool(str(record["approval_operation_id"]).strip())
    )


def _ranking_spec_anchor(value: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize a RankingSpec and verify its content-bound identity."""

    if not isinstance(value, Mapping) or set(value) != {
        "ranking_spec_id", "ranking_spec_fingerprint", "hard_constraints", "sort_keys", "tie_break_policy",
    }:
        raise ValidationError("RankingSpec锚点无效")
    ranking_spec_id = value.get("ranking_spec_id")
    fingerprint = value.get("ranking_spec_fingerprint")
    constraints = value.get("hard_constraints")
    sort_keys = value.get("sort_keys")
    tie_break_policy = value.get("tie_break_policy")
    if (
        not isinstance(ranking_spec_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", ranking_spec_id)
        or not isinstance(fingerprint, str)
        or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
        or not isinstance(constraints, Mapping)
        or isinstance(sort_keys, (str, bytes))
        or not isinstance(sort_keys, (list, tuple))
        or not sort_keys
        or tie_break_policy != "candidate_key"
    ):
        raise ValidationError("RankingSpec锚点无效")
    normalized_keys: list[dict[str, str]] = []
    for item in sort_keys:
        if not isinstance(item, Mapping) or set(item) != {"metric", "direction", "missing_policy"}:
            raise ValidationError("RankingSpec锚点无效")
        metric = item.get("metric")
        direction = item.get("direction")
        missing_policy = item.get("missing_policy")
        if (
            not isinstance(metric, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", metric)
            or direction not in {"asc", "desc"}
            or missing_policy not in {"exclude", "first", "last"}
        ):
            raise ValidationError("RankingSpec锚点无效")
        normalized_keys.append({"metric": metric, "direction": direction, "missing_policy": missing_policy})
    if len({item["metric"] for item in normalized_keys}) != len(normalized_keys):
        raise ValidationError("RankingSpec锚点无效")
    try:
        normalized_constraints = json.loads(json.dumps(constraints, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError) as error:
        raise ValidationError("RankingSpec锚点无效") from error
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
    computed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if fingerprint != computed or ranking_spec_id != f"{ranking_spec_id.rsplit('.', 1)[0]}.{fingerprint}":
        raise ValidationError("RankingSpec锚点与规则内容不一致")
    return {
        "ranking_spec_id": ranking_spec_id,
        "ranking_spec_fingerprint": fingerprint,
        "hard_constraints": normalized_constraints,
        "sort_keys": normalized_keys,
        "tie_break_policy": "candidate_key",
    }


def _active_snapshot(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the active binding without recursively embedding its history."""

    snapshot = {
        key: value
        for key, value in record.items()
        if key not in {"contract_history", "candidate_contract_variants"}
    }
    snapshot["variant_id"] = str(record.get("active_variant_id") or _variant_id(record))
    return snapshot


__all__ = ("ContractStore",)
