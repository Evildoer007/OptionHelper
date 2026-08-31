"""Tenant-scoped immutable module-run metadata and result persistence."""

from __future__ import annotations

import hashlib
import json
import base64
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from uuid import uuid4

_CORE_SRC = Path(__file__).resolve().parents[4] / "core" / "src"
if str(_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(_CORE_SRC))

from runtime.adapters.local_store import LocalResultStore, StoreError
from runtime.contracts.contract_types import semantic_hash
from runtime.protocol.models import ModuleRunRef
from modules.reporter.selection_facts import build_host_selection_source_refs

from ..errors import AuthorizationError, ValidationError
from ..identity.session_identity import SessionIdentity
from . import _LocalDocumentStore


MODULE_RUN_REF_SCHEMA = "optionhelper.module-run-ref"


class ResultStore:
    """Stores verified module responses, never user-selected result paths."""

    def __init__(self, state: _LocalDocumentStore) -> None:
        self._state = state
        # Core owns the physical ModuleRun layout, manifest and atomic commit.
        # App owns only the tenant/principal index layered over that RunRef.
        self._core = LocalResultStore(state._root / "module-runs")
        self.reconcile_pending_module_runs()

    def commit_module_run(self, identity: SessionIdentity, task_id: str, module: str, result: dict[str, Any]) -> dict[str, str]:
        if not task_id or module not in {"payoffer", "pricer", "backtester"} or not isinstance(result, dict):
            raise ValidationError("task_id, module and result are required for a ModuleRun")
        self._require_task_owner(identity, task_id)
        run_id = _store_id(result.get("run_id") or uuid4(), "run")
        files, normalized_result = _module_run_files(identity, task_id, module, run_id, result)
        reference = self._commit_files(
            identity, task_id, module, run_id, files, normalized_result,
            defer_publication=False,
        )
        return reference

    def bind_module_store(
        self,
        identity: SessionIdentity,
        task_id: str,
        module: str,
        *,
        defer_publication: bool = False,
    ) -> "_BoundModuleResultStore":
        """Return a Core ResultStorePort scoped to one authenticated module run."""
        self._require_task_owner(identity, task_id)
        if module not in {"payoffer", "pricer", "backtester"}:
            raise ValidationError("ModuleRun module is invalid")
        if not isinstance(defer_publication, bool):
            raise ValidationError("ModuleRun publication mode is invalid")
        return _BoundModuleResultStore(self, identity, task_id, module, defer_publication)

    def _commit_files(
        self,
        identity: SessionIdentity,
        task_id: str,
        module: str,
        run_id: str,
        files: dict[str, Any],
        normalized_result: dict[str, Any],
        *,
        defer_publication: bool,
    ) -> dict[str, str]:
        if module not in {"payoffer", "pricer", "backtester"}:
            raise ValidationError("ModuleRun module is invalid")
        self._require_task_owner(identity, task_id)
        record_key = _module_run_key(module, run_id)
        records = self._state.read("results")
        if record_key in records:
            raise ValidationError("ModuleRun records are immutable; create a new run_id")
        try:
            predicted_ref = self._core.predict_module_run_ref(
                module=module, tenant_id=identity.tenant_id, task_id=task_id, run_id=run_id, files=files,
            )
        except StoreError as error:
            raise ValidationError(f"Core ModuleRun预提交校验失败：{error}") from error
        pending_reference = _module_run_ref_dict(predicted_ref)
        manifest = files.get("manifest.json")
        manifest = manifest if isinstance(manifest, dict) else {}
        analysis_case_id = _store_id(
            normalized_result.get("analysis_case_id") or manifest.get("analysis_case_id") or task_id,
            "case",
        )
        record = {
            **pending_reference,
            "reference_schema": MODULE_RUN_REF_SCHEMA,
            "anchor_state": "prepared",
            "created_by": identity.principal_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "analysis_case_id": analysis_case_id,
            "status": str(normalized_result.get("status") or manifest.get("status") or "failed"),
            "result": normalized_result,
        }
        self._write_recovery(record_key, record)
        try:
            core_ref = self._core.commit_module_run(
                module=module, tenant_id=identity.tenant_id, task_id=task_id, run_id=run_id, files=files,
            )
        except (StoreError, FileExistsError) as error:
            self._remove_recovery(record_key)
            raise ValidationError(f"Core ModuleRun提交失败：{error}") from error
        if pending_reference != _module_run_ref_dict(core_ref):
            raise ValidationError("Core ModuleRun返回引用与预提交恢复记录不一致")
        reference = _module_run_ref_dict(core_ref)
        record.update(reference)
        record["anchor_state"] = "staged" if defer_publication else "anchored"
        self._replace_recovery(record_key, record)

        def update(value: dict[str, Any]) -> dict[str, Any]:
            if record_key in value:
                raise ValidationError("ModuleRun records are immutable; create a new run_id")
            value[record_key] = record
            return value

        try:
            self._state.update("results", update)
        except Exception as error:
            if self.reconcile_pending_module_runs() == 0:
                raise ValidationError("Core ModuleRun已提交，但App索引待恢复；下次启动将自动对账") from error
        self._remove_recovery(record_key)
        return reference

    def reconcile_pending_module_runs(self) -> int:
        """Idempotently rebuild App indices after a completed Core commit."""
        repaired = 0
        pending = self._state.read("result_recovery")
        for record_key, record in list(pending.items()):
            if not isinstance(record, dict):
                self._remove_recovery(record_key)
                continue
            try:
                current_fields = (
                    "module", "tenant_id", "task_id", "run_id", "expected_semantic_result_hash",
                    "expected_artifact_manifest_hash",
                )
                if record.get("reference_schema") != MODULE_RUN_REF_SCHEMA:
                    # Recovery data from another protocol is not a current
                    # RunRef and must never be reinterpreted as one.
                    self._remove_recovery(record_key)
                    continue
                if record.get("anchor_state") not in {"prepared", "staged", "anchored"}:
                    self._remove_recovery(record_key)
                    continue
                ref = ModuleRunRef(**{key: record[key] for key in current_fields})
                self._core.verify_module_run(ref, tenant_id=ref.tenant_id)
            except (KeyError, TypeError, ValueError, StoreError, FileNotFoundError, PermissionError):
                self._remove_recovery(record_key)
                continue

            def update(value: dict[str, Any]) -> dict[str, Any]:
                existing = value.get(record_key)
                if existing is not None and existing != record:
                    raise ValidationError("App ModuleRun索引与恢复记录冲突")
                value[record_key] = record
                return value

            try:
                self._state.update("results", update)
            except Exception:
                continue
            self._remove_recovery(record_key)
            repaired += 1
        return repaired

    def publish_module_run(
        self, identity: SessionIdentity, reference: ModuleRunRef | Mapping[str, str],
    ) -> None:
        """Make one verified staged run visible after its contract CAS succeeds."""

        payload = _module_run_ref_dict(reference) if isinstance(reference, ModuleRunRef) else dict(reference)
        ref = self._owned_module_run_ref(identity, payload, self._state.read("results"))
        try:
            self._core.verify_module_run(ref, tenant_id=identity.tenant_id)
        except (StoreError, FileNotFoundError, PermissionError) as error:
            raise ValidationError(f"Core ModuleRun不可发布：{error}") from error
        record_key = _module_run_key(ref.module, ref.run_id)

        def update(value: dict[str, Any]) -> dict[str, Any]:
            record = value.get(record_key)
            if not isinstance(record, dict) or record.get("anchor_state") != "staged":
                raise ValidationError("ModuleRun不处于待发布状态")
            record["anchor_state"] = "anchored"
            return value

        self._state.update("results", update)

    def _write_recovery(self, key: str, record: dict[str, Any]) -> None:
        def update(value: dict[str, Any]) -> dict[str, Any]:
            if key in value:
                raise ValidationError("ModuleRun恢复记录已存在")
            value[key] = record
            return value
        self._state.update("result_recovery", update)

    def _replace_recovery(self, key: str, record: dict[str, Any]) -> None:
        def update(value: dict[str, Any]) -> dict[str, Any]:
            if key not in value:
                raise ValidationError("ModuleRun恢复记录不存在")
            value[key] = record
            return value
        self._state.update("result_recovery", update)

    def _remove_recovery(self, key: str) -> None:
        def update(value: dict[str, Any]) -> dict[str, Any]:
            value.pop(key, None)
            return value
        self._state.update("result_recovery", update)

    def verify_owned_module_run(self, identity: SessionIdentity, reference: dict[str, str]) -> None:
        """Re-authorize and verify one immutable Core run without exposing its path."""

        ref = self._owned_module_run_ref(identity, reference, self._state.read("results"))
        try:
            self._core.verify_module_run(ref, tenant_id=identity.tenant_id)
        except (StoreError, FileNotFoundError, PermissionError) as error:
            raise ValidationError(f"Core ModuleRun不可用：{error}") from error

    def resolve_owned_module_run(self, identity: SessionIdentity, reference: dict[str, str]) -> None:
        """Current Agent dispatcher verification hook; intentionally returns no path."""

        self.verify_owned_module_run(identity, reference)

    def verify_owned_module_runs(
        self,
        requests: Iterable[tuple[SessionIdentity, dict[str, str]]],
    ) -> None:
        """Verify a bounded group of stored runs against one result-index snapshot.

        Startup recovery may need to validate every succeeded calculation job.
        Reading the immutable proof index once avoids reparsing the complete JSON
        document for each job while preserving the per-run Core file checks.
        The snapshot is local to this call and is never retained as a cache.
        """

        pending = tuple(requests)
        if not pending:
            return
        records = self._state.read("results")
        for identity, reference in pending:
            ref = self._owned_module_run_ref(identity, reference, records)
            try:
                self._core.verify_module_run(ref, tenant_id=identity.tenant_id)
            except (StoreError, FileNotFoundError, PermissionError) as error:
                raise ValidationError(f"Core ModuleRun不可用：{error}") from error

    def _owned_module_run_ref(
        self,
        identity: SessionIdentity,
        reference: dict[str, str],
        records: dict[str, Any],
    ) -> ModuleRunRef:
        self._resolve_module_run(identity, reference, records)
        return ModuleRunRef(
            module=str(reference["module"]), tenant_id=str(reference["tenant_id"]), task_id=str(reference["task_id"]), run_id=str(reference["run_id"]),
            expected_semantic_result_hash=str(reference["expected_semantic_result_hash"]),
            expected_artifact_manifest_hash=str(reference["expected_artifact_manifest_hash"]),
        )

    def read_owned_module_run_file(self, identity: SessionIdentity, reference: dict[str, str], name: str) -> bytes:
        ref = self._owned_module_run_ref(identity, reference, self._state.read("results"))
        try:
            return self._core.read_module_run_file(ref, name, tenant_id=identity.tenant_id)
        except (StoreError, FileNotFoundError, PermissionError) as error:
            raise ValidationError(f"Core ModuleRun不可读取：{error}") from error

    def read_owned_module_run_bundle(self, identity: SessionIdentity, reference: dict[str, str]) -> dict[str, bytes]:
        ref = self._owned_module_run_ref(identity, reference, self._state.read("results"))
        try:
            return self._core.read_module_run_bundle(ref, tenant_id=identity.tenant_id)
        except (StoreError, FileNotFoundError, PermissionError) as error:
            raise ValidationError(f"Core ModuleRun不可读取：{error}") from error

    def resolve_module_run(self, identity: SessionIdentity, reference: dict[str, str]) -> dict[str, Any]:
        record = self._resolve_module_run(identity, reference, self._state.read("results"))
        try:
            semantic_name = "result.json" if record.get("status") in {"succeeded", "partial"} else "error.json"
            semantic_value = json.loads(self.read_owned_module_run_file(identity, reference, semantic_name))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValidationError("Core ModuleRun缺少可验证语义结果") from error
        if not isinstance(semantic_value, dict):
            raise ValidationError("Core ModuleRun语义结果无效")
        return {**record, "result": semantic_value}

    def _resolve_module_run(
        self,
        identity: SessionIdentity,
        reference: dict[str, str],
        records: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = reference.get("run_id")
        record = _stored_module_run(records, reference.get("module"), run_id)
        if not isinstance(record, dict):
            raise KeyError(str(run_id))
        if record.get("tenant_id") != identity.tenant_id:
            raise AuthorizationError("result.read", "module result belongs to another tenant")
        if record.get("task_id") != reference.get("task_id") or record.get("created_by") != identity.principal_id:
            raise AuthorizationError("result.read", "module result is not owned by current caller")
        if record.get("module") != reference.get("module"):
            raise ValidationError("ModuleRunRef module does not match stored result")
        expected = reference.get("expected_semantic_result_hash")
        if expected != record.get("expected_semantic_result_hash"):
            raise ValidationError("ModuleRunRef semantic hash does not match stored result")
        if reference.get("expected_artifact_manifest_hash") != record.get("expected_artifact_manifest_hash"):
            raise ValidationError("ModuleRunRef artifact manifest hash does not match stored result")
        return record

    def verified_fact_summary(
        self,
        identity: SessionIdentity,
        reference: dict[str, str],
        *,
        expected_binding: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Project selected numerical facts from one still-verifiable ModuleRun.

        This is intentionally a presentation projection, not a second pricer
        or backtester.  The Core commit marker and semantic hash are resolved
        before the App reads the immutable indexed result.  Neither tenant,
        principal, paths, raw payloads nor arbitrary fields leave this method.
        """
        record = self.resolve_module_run(identity, reference)
        try:
            result = json.loads(self.read_owned_module_run_file(identity, reference, "result.json"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValidationError("已验证ModuleRun缺少可读取结果") from error
        if not isinstance(result, dict) or semantic_hash(result) != record.get("expected_semantic_result_hash"):
            raise ValidationError("ModuleRun事实哈希不匹配")
        expected = dict(expected_binding or {})
        allowed_expected = {
            "analysis_case_id", "candidate_id", "catalog_version", "contract_fingerprint",
            "candidate_key", "candidate_version_id",
        }
        if set(expected).difference(allowed_expected):
            raise ValidationError("ModuleRun事实绑定包含未知字段")
        for field, value in expected.items():
            if not isinstance(value, str) or not value:
                raise ValidationError(f"ModuleRun预期绑定{field}无效")
            actual = record.get("analysis_case_id") if field == "analysis_case_id" else result.get(field)
            if actual != value:
                raise ValidationError(f"ModuleRun事实与预期{field}不一致")
        run_ref = {
            "module": str(record["module"]),
            "run_id": str(record["run_id"]),
            "result_hash": str(record["expected_semantic_result_hash"]),
            "contract_fingerprint": str(result.get("contract_fingerprint", "")),
        }
        facts = [
            {
                **fact,
                "module": run_ref["module"],
                "contract_fingerprint": run_ref["contract_fingerprint"],
            }
            for fact in _result_facts(str(record["module"]), result, run_ref["result_hash"])
        ]
        return {"run_ref": run_ref, "facts": facts}

    def list_report_sources(self, *, tenant_id: str, task_id: str | None = None, query: str | None = None) -> dict[str, Any]:
        """Return a tenant-scoped projection of completed calculation runs.

        This is the only App-side enumeration path.  It deliberately exposes
        RunRefs and display facts only: never document-store keys, directories
        or raw result payloads.
        """
        if not tenant_id:
            raise ValidationError("tenant_id is required")
        if task_id is not None and not task_id:
            raise ValidationError("task_id is invalid")
        needle = (query or "").strip().lower()
        grouped: dict[str, dict[str, Any]] = {}
        for record in self._state.read("results").values():
            if (
                not isinstance(record, dict)
                or record.get("tenant_id") != tenant_id
                or record.get("anchor_state") != "anchored"
            ):
                continue
            if record.get("module") not in {"payoffer", "pricer", "backtester"} or not _completed(record.get("result")):
                continue
            if task_id is not None and record.get("task_id") != task_id:
                continue
            verified_run = self._verified_report_record(record, tenant_id)
            if verified_run is None:
                continue
            try:
                verified_contract, verified_result = verified_run
                candidate = _report_candidate({**record, "result": verified_result}, verified_contract)
            except ValidationError:
                # Reporter只能消费带有Core合同快照哈希的正式结果。旧索引
                # 记录不能靠临时派生标签伪装成当前可交付来源。
                continue
            if needle and needle not in json.dumps(candidate, ensure_ascii=False, sort_keys=True).lower():
                continue
            source_id = _source_id(
                tenant_id,
                str(record["task_id"]),
                candidate["analysis_case_id"],
                candidate["catalog_content_hash"],
            )
            source = grouped.setdefault(source_id, {
                "source_id": source_id,
                "label": f"{candidate['product_name']} · {record['task_id']}",
                "tenant_id": tenant_id,
                "task_id": record["task_id"],
                "analysis_case_id": candidate["analysis_case_id"],
                "catalog_version": candidate["catalog_version"],
                "catalog_content_hash": candidate["catalog_content_hash"],
                "candidates": {},
            })
            # A candidate can have more than one saved contract snapshot:
            # for example after changing a strike and rerunning Pricing only.
            # Keep every verified run option on the same candidate; the
            # selected ModuleRunRef remains the authoritative snapshot and
            # Reporter checks compatibility before it renders a delivery.
            row = source["candidates"].setdefault(candidate["candidate_id"], candidate)
            display_module = _report_module(str(record["module"]))
            reference = {
                "module": record["module"], "tenant_id": record["tenant_id"], "task_id": record["task_id"], "run_id": record["run_id"],
                "expected_semantic_result_hash": record["expected_semantic_result_hash"],
                "expected_artifact_manifest_hash": record["expected_artifact_manifest_hash"],
            }
            options = row.setdefault("module_run_options", {}).setdefault(display_module, [])
            if not any(existing == reference for existing in options):
                options.append(reference)
        sources: list[dict[str, Any]] = []
        for grouped_source in grouped.values():
            candidates = []
            for candidate in grouped_source["candidates"].values():
                normalized = dict(candidate)
                options_by_module = normalized.get("module_run_options", {})
                if not isinstance(options_by_module, dict):
                    options_by_module = {}
                normalized["module_run_options"] = {
                    module: sorted(
                        options,
                        key=lambda ref: (
                            str(ref.get("run_id", "")),
                            str(ref.get("expected_semantic_result_hash", "")),
                        ),
                    )
                    for module, options in options_by_module.items()
                    if isinstance(options, list) and options
                }
                # 只有唯一已验证运行可以作为默认选择；多个重试运行必须
                # 由受控selection显式绑定，绝不以字典遍历顺序决定。
                normalized["module_run_refs"] = {
                    module: options[0]
                    for module, options in normalized["module_run_options"].items()
                    if len(options) == 1
                }
                candidates.append(normalized)
            source = {**grouped_source, "candidates": candidates}
            source["source_refs"] = build_host_selection_source_refs(source, tenant_id)
            sources.append(source)
        return {"tenant_id": tenant_id, "sources": sources}

    def _verified_report_record(self, record: dict[str, Any], tenant_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Only enumerate calculation runs whose Core commit remains intact."""
        try:
            reference = ModuleRunRef(
                module=str(record["module"]),
                tenant_id=str(record["tenant_id"]),
                task_id=str(record["task_id"]),
                run_id=str(record["run_id"]),
                expected_semantic_result_hash=str(record["expected_semantic_result_hash"]),
                expected_artifact_manifest_hash=str(record["expected_artifact_manifest_hash"]),
            )
            contract = json.loads(self._core.read_module_run_file(reference, "resolved_contract.json", tenant_id=tenant_id))
            result = json.loads(self._core.read_module_run_file(reference, "result.json", tenant_id=tenant_id))
            return (contract, result) if isinstance(contract, dict) and isinstance(result, dict) else None
        except (KeyError, TypeError, ValueError, StoreError, FileNotFoundError, PermissionError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def list_owned_report_sources(self, identity: SessionIdentity, *, task_id: str | None = None, query: str | None = None) -> dict[str, Any]:
        if task_id is not None:
            self._require_task_owner(identity, task_id)
        catalog = self.list_report_sources(tenant_id=identity.tenant_id, task_id=task_id, query=query)
        owned = []
        for source in catalog["sources"]:
            refs = [
                ref
                for candidate in source["candidates"]
                for options in candidate.get("module_run_options", {}).values()
                for ref in options
            ]
            if refs and all(self._owned_run(identity, ref) for ref in refs):
                owned.append(source)
        return {"tenant_id": identity.tenant_id, "sources": owned}

    def get_report_source(self, *, tenant_id: str, source_id: str) -> dict[str, Any]:
        if not isinstance(source_id, str) or not source_id.startswith("source_"):
            raise KeyError(source_id)
        for source in self.list_report_sources(tenant_id=tenant_id)["sources"]:
            if source["source_id"] == source_id:
                return source
        raise KeyError(source_id)

    def get_owned_report_source(self, identity: SessionIdentity, source_id: str) -> dict[str, Any]:
        for source in self.list_owned_report_sources(identity)["sources"]:
            if source["source_id"] == source_id:
                return source
        raise KeyError(source_id)


    def select_report_source(self, identity: SessionIdentity, task_id: str, source_id: str) -> dict[str, Any]:
        """Resolve a report source by opaque id and re-authorize it server-side."""
        source = self.get_report_source(tenant_id=identity.tenant_id, source_id=source_id)
        if source["task_id"] != task_id:
            raise AuthorizationError("result.select", "report source is not part of this task")
        self._require_task_owner(identity, task_id)
        return source

    def commit_report_run(
        self,
        identity: SessionIdentity,
        task_id: str,
        report_request: dict[str, Any],
        artifacts: list[dict[str, Any]],
        *,
        status: str = "succeeded",
    ) -> dict[str, str]:
        """Commit an immutable ReportRun and its manifest-declared bytes.

        The App never accepts a filesystem path.  HTML/PDF bytes are held under
        the App's private state store and can later be read only by their exact
        manifest name and only after tenant/principal authorization.
        """
        self._require_task_owner(identity, task_id)
        if not isinstance(report_request, dict) or not isinstance(artifacts, list) or not artifacts:
            raise ValidationError("report_request and at least one artifact are required for a ReportRun")
        if status not in {"succeeded", "partial"}:
            raise ValidationError("ReportRun status must be succeeded or partial")
        report_run_id = str(report_request.get("report_run_id") or uuid4())
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", report_run_id):
            raise ValidationError("report_run_id is invalid")
        normalized = [_artifact_record(item) for item in artifacts]
        if len(normalized) > 64 or sum(int(item["size"]) for item in normalized) > 32 * 1024 * 1024:
            raise ValidationError("ReportRun portable artifact bundle exceeds the App limit")
        names = [item["name"] for item in normalized]
        if len(set(names)) != len(names):
            raise ValidationError("ReportRun artifact names must be unique")
        manifest = [{key: item[key] for key in ("name", "content_type", "sha256", "size")} for item in normalized]
        artifact_manifest_hash = _sha256(manifest)
        semantic_fact_hash = _sha256(report_request)
        reference = {
            "tenant_id": identity.tenant_id,
            "task_id": task_id,
            "report_run_id": report_run_id,
            "expected_semantic_fact_hash": semantic_fact_hash,
            "expected_artifact_manifest_hash": artifact_manifest_hash,
        }
        record = {
            **reference,
            "status": status,
            "created_by": identity.principal_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "access_scope": {"audience": identity.audience, "roles": [identity.role.value]},
            "report_request": report_request,
            "artifact_manifest": manifest,
            "artifacts": normalized,
        }

        def update(value: dict[str, Any]) -> dict[str, Any]:
            if report_run_id in value:
                raise ValidationError("ReportRun records are immutable; create a new report_run_id")
            value[report_run_id] = record
            return value

        self._state.update("reports", update)
        return reference

    def list_report_runs(self, identity: SessionIdentity, task_id: str) -> list[dict[str, Any]]:
        self._require_task_owner(identity, task_id)
        rows = []
        for record in self._state.read("reports").values():
            if not isinstance(record, dict) or record.get("tenant_id") != identity.tenant_id or record.get("task_id") != task_id:
                continue
            if record.get("created_by") != identity.principal_id or not _report_visible(identity, record):
                continue
            rows.append(_report_projection(record))
        return sorted(rows, key=lambda item: item["created_at"], reverse=True)

    def resolve_report_run(self, identity: SessionIdentity, reference: dict[str, str]) -> dict[str, Any]:
        report_run_id = reference.get("report_run_id")
        record = self._state.read("reports").get(report_run_id)
        if not isinstance(record, dict):
            raise KeyError(str(report_run_id))
        self._authorize_report(identity, record)
        if reference.get("expected_semantic_fact_hash") != record.get("expected_semantic_fact_hash"):
            raise ValidationError("ReportRunRef semantic fact hash does not match stored report")
        if reference.get("expected_artifact_manifest_hash") != record.get("expected_artifact_manifest_hash"):
            raise ValidationError("ReportRunRef artifact manifest hash does not match stored report")
        return record

    def get_owned_report_run(self, identity: SessionIdentity, report_run_id: str) -> dict[str, Any]:
        """Read one immutable delivery for a fact-preserving re-render."""

        if not isinstance(report_run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", report_run_id):
            raise ValidationError("report_run_id is invalid")
        record = self._state.read("reports").get(report_run_id)
        if not isinstance(record, dict):
            raise KeyError(report_run_id)
        self._authorize_report(identity, record)
        return record

    def read_report_artifact(self, identity: SessionIdentity, report_run_id: str, artifact_name: str) -> tuple[bytes, str]:
        if not isinstance(report_run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", report_run_id):
            raise ValidationError("report_run_id is invalid")
        record = self._state.read("reports").get(report_run_id)
        if not isinstance(record, dict):
            raise KeyError(report_run_id)
        self._authorize_report(identity, record)
        artifact_name = _report_artifact_name(artifact_name)
        for artifact in record.get("artifacts", []):
            if isinstance(artifact, dict) and artifact.get("name") == artifact_name:
                encoded = artifact.get("content_base64")
                if not isinstance(encoded, str):
                    raise ValidationError("Report artifact is invalid")
                try:
                    content = base64.b64decode(encoded.encode("ascii"), validate=True)
                except (UnicodeEncodeError, ValueError) as error:
                    raise ValidationError("Report artifact is invalid") from error
                if hashlib.sha256(content).hexdigest() != artifact.get("sha256"):
                    raise ValidationError("Report artifact hash does not match manifest")
                return content, str(artifact["content_type"])
        raise KeyError(artifact_name)

    def _require_task_owner(self, identity: SessionIdentity, task_id: str) -> None:
        records = self._state.read("tasks")
        task = records.get(task_id)
        if not isinstance(task, dict) or task.get("tenant_id") != identity.tenant_id or task.get("created_by") != identity.principal_id:
            raise AuthorizationError("result.read", "task is not owned by current caller")

    def _owned_run(self, identity: SessionIdentity, reference: dict[str, Any]) -> bool:
        record = _stored_module_run(
            self._state.read("results"), reference.get("module"), reference.get("run_id"),
        )
        return (
            isinstance(record, dict)
            and record.get("module") == reference.get("module")
            and record.get("tenant_id") == identity.tenant_id
            and record.get("created_by") == identity.principal_id
        )

    def _authorize_report(self, identity: SessionIdentity, record: dict[str, Any]) -> None:
        if record.get("tenant_id") != identity.tenant_id or record.get("created_by") != identity.principal_id:
            raise AuthorizationError("report.read", "ReportRun is not owned by current caller")
        if not _report_visible(identity, record):
            raise AuthorizationError("report.read", "ReportRun audience does not allow current role")


class _BoundModuleResultStore:
    """ResultStorePort adapter that cannot escape its authenticated task."""

    def __init__(
        self,
        owner: ResultStore,
        identity: SessionIdentity,
        task_id: str,
        module: str,
        defer_publication: bool,
    ) -> None:
        self._owner = owner
        self._identity = identity
        self._task_id = task_id
        self._module = module
        self._defer_publication = defer_publication

    def predict_module_run_ref(
        self,
        *,
        module: str,
        tenant_id: str,
        task_id: str,
        run_id: str,
        files: Mapping[str, Any],
    ) -> ModuleRunRef:
        self._require_write_scope(module, tenant_id, task_id)
        try:
            return self._owner._core.predict_module_run_ref(
                module=module,
                tenant_id=tenant_id,
                task_id=task_id,
                run_id=run_id,
                files=files,
            )
        except (StoreError, FileNotFoundError, PermissionError) as error:
            raise ValidationError(f"Core ModuleRun无法预提交：{error}") from error

    def commit_module_run(
        self, *, module: str, tenant_id: str, task_id: str, run_id: str, files: dict[str, Any],
    ) -> ModuleRunRef:
        self._require_write_scope(module, tenant_id, task_id)
        committed_files = dict(files)
        raw_result = committed_files.get("result.json")
        manifest = committed_files.get("manifest.json")
        lifecycle_status = manifest.get("status") if isinstance(manifest, dict) else None
        if isinstance(raw_result, dict) and lifecycle_status in {"succeeded", "partial"}:
            # The Core ResultStore computes the only authoritative semantic
            # hash from its committed result.json.  Module implementations
            # may carry an old self-referential ``semantic_result_hash`` in
            # both files; remove that metadata and bind manifest to the exact
            # bytes being committed instead of accepting two hash schemes.
            committed_result = dict(raw_result)
            committed_result.pop("semantic_result_hash", None)
            committed_manifest = dict(manifest)
            committed_manifest["semantic_result_hash"] = semantic_hash(committed_result)
            committed_files["result.json"] = committed_result
            committed_files["manifest.json"] = committed_manifest
            # Core's immutable manifest, rather than a page response, is the
            # terminal-state authority.  Some compute modules write a pure
            # financial result.json without status; preserve its facts and
            # project the committed status only into the App index so it can
            # be selected by Reporter after Core verification.
            analysis_case_id = _store_id(manifest.get("analysis_case_id") or task_id, "case")
            normalized_result = {
                **committed_result,
                "analysis_case_id": analysis_case_id,
                "status": lifecycle_status,
            }
        elif lifecycle_status in {"failed", "unsupported", "cancelled", "timed_out"}:
            error_result = files.get("error.json")
            if not isinstance(error_result, Mapping):
                raise ValidationError("失败ModuleRun缺少正式error.json")
            normalized_result = {
                "ok": False,
                "module": module,
                "status": lifecycle_status,
                "error": dict(error_result),
            }
        else:
            raise ValidationError("ModuleRun manifest缺少正式终态")
        reference = self._owner._commit_files(
            self._identity,
            task_id,
            module,
            _store_id(run_id, "run"),
            committed_files,
            normalized_result,
            defer_publication=self._defer_publication,
        )
        return ModuleRunRef(**reference)

    def verify_module_run(self, ref: ModuleRunRef, *, tenant_id: str) -> None:
        """Verify a scoped RunRef without exposing a directory to new callers."""

        if ref.module != self._module:
            raise AuthorizationError("result.read", "ModuleRun module does not match authenticated App module")
        if tenant_id != self._identity.tenant_id or ref.task_id != self._task_id:
            raise AuthorizationError("result.read", "ModuleRun scope does not match authenticated App task")
        self._owner.verify_owned_module_run(self._identity, _module_run_ref_dict(ref))

    def read_module_run_file(self, ref: ModuleRunRef, name: str, *, tenant_id: str) -> bytes:
        if ref.module != self._module:
            raise AuthorizationError("result.read", "ModuleRun module does not match authenticated App module")
        if tenant_id != self._identity.tenant_id or ref.task_id != self._task_id:
            raise AuthorizationError("result.read", "ModuleRun scope does not match authenticated App task")
        self._owner.verify_owned_module_run(self._identity, _module_run_ref_dict(ref))
        try:
            return self._owner._core.read_module_run_file(ref, name, tenant_id=tenant_id)
        except (StoreError, FileNotFoundError, PermissionError) as error:
            raise ValidationError(f"Core ModuleRun不可读取：{error}") from error

    def read_module_run_bundle(self, ref: ModuleRunRef, *, tenant_id: str) -> dict[str, bytes]:
        if ref.module != self._module:
            raise AuthorizationError("result.read", "ModuleRun module does not match authenticated App module")
        if tenant_id != self._identity.tenant_id or ref.task_id != self._task_id:
            raise AuthorizationError("result.read", "ModuleRun scope does not match authenticated App task")
        return self._owner.read_owned_module_run_bundle(
            self._identity,
            _module_run_ref_dict(ref),
        )

    def _require_write_scope(self, module: str, tenant_id: str, task_id: str) -> None:
        if module != self._module:
            raise AuthorizationError("result.write", "ModuleRun module does not match authenticated App module")
        if tenant_id != self._identity.tenant_id or task_id != self._task_id:
            raise AuthorizationError("result.write", "ModuleRun scope does not match authenticated App task")


_PRICING_FACTS = (
    ("pv_percent", "PV(%)", "percent"),
)
_GREEK_NAMES = {"delta": "Delta", "gamma": "Gamma", "vega": "Vega", "theta": "Theta", "rho": "Rho"}
_BACKTEST_FACTS = (
    ("average_gross_return", "平均毛收益率", "percent"),
    ("median_gross_return", "中位毛收益率", "percent"),
    ("minimum_gross_return", "最小毛收益率", "percent"),
    ("maximum_gross_return", "最大毛收益率", "percent"),
    ("max_loss_gross_return", "最大损失毛收益率", "percent"),
    ("win_rate", "胜率", "percent"),
)


def _result_facts(module: str, result: dict[str, Any], result_hash: str) -> list[dict[str, Any]]:
    """Select a small stable display vocabulary from a verified result."""
    source: dict[str, Any]
    selected: list[tuple[str, str, object, str | None]] = []
    if module == "pricer":
        source = result.get("pricing") if isinstance(result.get("pricing"), dict) else {}
        for key, label, unit in _PRICING_FACTS:
            value = _number(source.get(key))
            if value is not None:
                selected.append((key, label, value, unit))
        greeks = source.get("greeks") if isinstance(source.get("greeks"), dict) else {}
        for raw_name, raw_value in greeks.items():
            name = str(raw_name).lower()
            if name not in _GREEK_NAMES:
                continue
            value, unit = _risk_number(raw_value)
            if value is not None:
                selected.append((f"greeks.{name}", _GREEK_NAMES[name], value, unit))
    elif module == "backtester":
        source = result.get("backtest") if isinstance(result.get("backtest"), dict) else result
        metrics = source.get("common_metrics") if isinstance(source.get("common_metrics"), dict) else source
        for key, label, unit in _BACKTEST_FACTS:
            value = _number(metrics.get(key)) if isinstance(metrics, dict) else None
            if value is not None:
                selected.append((key, label, value, unit))
    else:
        selected = []
    return [
        {
            "fact_ref": _fact_ref(result_hash, path, value),
            "metric": path,
            "label": label,
            "value": value,
            **({"unit": unit} if unit else {}),
        }
        for path, label, value, unit in selected[:24]
    ]


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return value


def _risk_number(value: object) -> tuple[int | float | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    numeric = _number(value.get("pv_percent_value"))
    return (numeric, "percent") if numeric is not None else (None, None)


def _fact_ref(result_hash: str, path: str, value: object) -> str:
    payload = json.dumps({"result_hash": result_hash, "path": path, "value": value}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"fact_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _completed(value: object) -> bool:
    return isinstance(value, dict) and value.get("ok") is not False and value.get("status") == "succeeded"


def _store_id(value: object, prefix: str) -> str:
    text = str(value or "")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", text) and text not in {".", ".."}:
        return text
    return f"{prefix}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]}"


def _candidate_variant_store_id(value: object, field: str) -> str:
    """Keep CandidateVersion identifiers stable under ContractStore's grammar."""

    text = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", text):
        raise ValidationError(f"ModuleRun{field}无效")
    return text


def _module_run_key(module: object, run_id: object) -> str:
    return f"{module}:{run_id}"


def _module_run_ref_dict(ref: ModuleRunRef) -> dict[str, str]:
    return {
        "module": ref.module,
        "tenant_id": ref.tenant_id,
        "task_id": ref.task_id,
        "run_id": ref.run_id,
        "expected_semantic_result_hash": ref.expected_semantic_result_hash,
        "expected_artifact_manifest_hash": ref.expected_artifact_manifest_hash,
    }


def _stored_module_run(records: dict[str, Any], module: object, run_id: object) -> dict[str, Any] | None:
    record = records.get(_module_run_key(module, run_id))
    return record if isinstance(record, dict) else None


def _module_run_files(
    identity: SessionIdentity, task_id: str, module: str, run_id: str, result: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the one Core ModuleRun file set from an already-authorized tool result.

    The browser never supplies filenames or an output directory.  Incomplete
    failed runs still get a terminal Core manifest; only verified successful
    contracts are later eligible for Reporter selection.
    """
    safe_run_id = _store_id(run_id, "run")
    value = dict(result)
    value["run_id"] = safe_run_id
    status_raw = value.get("status")
    if status_raw not in {"succeeded", "partial", "failed", "unsupported", "cancelled", "timed_out"}:
        raise ValidationError("ModuleRun.status不是正式状态")
    status = str(status_raw)
    contract = value.get("resolved_contract")
    resolved_contract = dict(contract) if isinstance(contract, dict) else {}
    if status in {"succeeded", "partial"} and not resolved_contract:
        raise ValidationError("成功ModuleRun缺少resolved_contract")
    raw_analysis_case_id = value.get("analysis_case_id")
    if status in {"succeeded", "partial"} and not isinstance(raw_analysis_case_id, str):
        raise ValidationError("成功ModuleRun缺少analysis_case_id")
    analysis_case_id = _store_id(raw_analysis_case_id, "case") if isinstance(raw_analysis_case_id, str) else ""
    contract_fingerprint = str(resolved_contract.get("contract_fingerprint") or value.get("contract_fingerprint") or "")
    raw_candidate_id = value.get("candidate_id")
    if status in {"succeeded", "partial"} and not isinstance(raw_candidate_id, str):
        raise ValidationError("成功ModuleRun缺少candidate_id")
    candidate_id = _store_id(raw_candidate_id, "candidate") if isinstance(raw_candidate_id, str) else ""
    if candidate_id:
        value["candidate_id"] = candidate_id
    candidate_key = value.get("candidate_key")
    candidate_version_id = value.get("candidate_version_id")
    if (candidate_key is None) != (candidate_version_id is None):
        raise ValidationError("ModuleRun候选版本身份必须同时包含candidate_key和candidate_version_id")
    if candidate_key is not None:
        value["candidate_key"] = _candidate_variant_store_id(candidate_key, "candidate_key")
        value["candidate_version_id"] = _candidate_variant_store_id(
            candidate_version_id,
            "candidate_version_id",
        )
    catalog_version = value.get("catalog_version")
    if not isinstance(catalog_version, str) or not catalog_version.strip():
        if status in {"succeeded", "partial"}:
            raise ValidationError("成功ModuleRun缺少catalog_version")
        catalog_version = ""
    else:
        catalog_version = catalog_version.strip()
    value["catalog_version"] = catalog_version
    execution_fingerprint = value.get("execution_fingerprint")
    if status in {"succeeded", "partial"} and (
        not isinstance(execution_fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", execution_fingerprint) is None
    ):
        raise ValidationError("成功ModuleRun缺少有效execution_fingerprint")
    if isinstance(execution_fingerprint, str) and execution_fingerprint:
        value["execution_fingerprint"] = execution_fingerprint
    semantic_result_hash = semantic_hash(value) if status in {"succeeded", "partial"} else ""
    limitations = value.get("limitations")
    if not isinstance(limitations, list) or any(not isinstance(item, str) for item in limitations):
        limitations = []
    data_refs = value.get("data_refs") if isinstance(value.get("data_refs"), list) else []
    manifest: dict[str, Any] = {
        "module": module,
        "tenant_id": identity.tenant_id,
        "task_id": task_id,
        "run_id": safe_run_id,
        "analysis_case_id": analysis_case_id,
        "candidate_id": candidate_id,
        "catalog_version": catalog_version,
        "status": status,
        "lifecycle_status": status,
        "contract_fingerprint": contract_fingerprint,
        "product_version": resolved_contract.get("product_version"),
        "execution_fingerprint": execution_fingerprint,
        "input_snapshot": "input_snapshot.json",
        "resolved_contract": "resolved_contract.json",
        "data_refs": "data_refs.json",
        "limitations": "limitations.json",
    }
    if candidate_key is not None:
        manifest["candidate_key"] = value["candidate_key"]
        manifest["candidate_version_id"] = value["candidate_version_id"]
    files: dict[str, Any] = {
        "input_snapshot.json": {"module": module, "task_id": task_id, "analysis_case_id": analysis_case_id},
        "resolved_contract.json": resolved_contract,
        "data_refs.json": json.dumps(data_refs, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "limitations.json": json.dumps(limitations, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    }
    if status in {"succeeded", "partial"}:
        manifest["semantic_result_hash"] = semantic_result_hash
        manifest["result"] = "result.json"
        files["result.json"] = value
    else:
        error = value.get("error") if isinstance(value.get("error"), dict) else {
            "error_code": str(value.get("error_code") or "module_failed"),
            "message": str(value.get("message") or value.get("reason") or "模块未返回可用结果。"),
        }
        manifest["error"] = "error.json"
        files["error.json"] = error
    files["manifest.json"] = manifest
    return files, value


def _sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _artifact_record(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Report artifact must be an object")
    name = value.get("name")
    content_type = value.get("content_type")
    content = value.get("content")
    name = _report_artifact_name(name)
    if content_type not in {
        "text/html; charset=utf-8", "application/pdf", "application/javascript", "text/javascript", "text/css",
    }:
        raise ValidationError("Report artifact content_type is not allowed")
    if not isinstance(content, bytes) or not content:
        raise ValidationError("Report artifact content must be non-empty bytes")
    digest = hashlib.sha256(content).hexdigest()
    return {"name": name, "content_type": content_type, "sha256": digest, "size": len(content), "content_base64": base64.b64encode(content).decode("ascii")}


def _report_artifact_name(value: object) -> str:
    if not isinstance(value, str) or "\\" in value or len(value) > 320:
        raise ValidationError("Report artifact name is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", part)
        for part in path.parts
    ):
        raise ValidationError("Report artifact name is invalid")
    return path.as_posix()


def _report_visible(identity: SessionIdentity, record: dict[str, Any]) -> bool:
    scope = record.get("access_scope")
    return isinstance(scope, dict) and identity.role.value in scope.get("roles", []) and identity.audience == scope.get("audience")


def _report_projection(record: dict[str, Any]) -> dict[str, Any]:
    request = record.get("report_request")
    request = request if isinstance(request, dict) else {}
    subject_ref = request.get("subject_ref")
    subject_ref = subject_ref if isinstance(subject_ref, dict) else {}
    delivery_mode = subject_ref.get("delivery_mode", request.get("delivery_mode"))
    delivery_mode = str(delivery_mode) if delivery_mode in {"single", "combined", "batch", "comparison", "quote"} else None
    artifacts = record.get("artifact_manifest")
    artifacts = artifacts if isinstance(artifacts, list) else []
    return {
        "task_id": record["task_id"],
        "report_run_id": record["report_run_id"],
        "status": record["status"],
        "created_at": record["created_at"],
        "output_type": request.get("output_type"),
        "delivery_mode": delivery_mode,
        "comparison": delivery_mode == "comparison",
        "format": request.get("format"),
        "artifacts": [
            {
                **{key: item.get(key) for key in ("name", "content_type", "size")},
                "display_name": _artifact_display_name(request, item),
            }
            for item in artifacts if isinstance(item, dict)
        ],
    }


def _artifact_display_name(request: Mapping[str, Any], artifact: Mapping[str, Any]) -> str:
    name = PurePosixPath(str(artifact.get("name", "artifact"))).name
    metadata = request.get("metadata")
    title = metadata.get("title") if isinstance(metadata, Mapping) else None
    content_type = str(artifact.get("content_type", ""))
    if (
        isinstance(title, str)
        and 0 < len(title.strip()) <= 120
        and not any(ord(character) < 32 for character in title)
        and content_type in {"text/html; charset=utf-8", "application/pdf"}
    ):
        suffix = ".pdf" if content_type == "application/pdf" else ".html"
        return f"{title.strip()}{suffix}"
    return name


def _report_module(module: str) -> str:
    return {"payoffer": "payoff", "pricer": "pricing", "backtester": "backtest"}[module]


def _source_id(tenant_id: str, task_id: str, analysis_case_id: str, catalog_content_hash: str) -> str:
    digest = hashlib.sha256(
        f"{tenant_id}:{task_id}:{analysis_case_id}:{catalog_content_hash}".encode("utf-8")
    ).hexdigest()[:24]
    return f"source_{digest}"


def _report_candidate(record: dict[str, Any], verified_contract: dict[str, Any]) -> dict[str, Any]:
    result = record["result"]
    if not isinstance(result, dict):
        raise ValidationError("formal report source result is invalid")
    contract = verified_contract
    identity = contract.get("identity") if isinstance(contract, dict) else None
    if not isinstance(identity, dict):
        raise ValidationError("formal report source is missing current contract identity")
    product_id = str(identity.get("product_id") or "").strip()
    if not product_id:
        raise ValidationError("formal report source is missing product_id")
    product_name = str(identity.get("name_zh") or product_id)
    fingerprint = str(contract.get("contract_fingerprint") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValidationError("formal report source is missing contract_fingerprint")
    if result.get("contract_fingerprint") != fingerprint:
        raise ValidationError("formal report source contract_fingerprint does not match current contract")
    analysis_case_id = str(record.get("analysis_case_id") or "").strip()
    if not analysis_case_id:
        raise ValidationError("formal report source is missing analysis_case_id")
    candidate_id = str(result.get("candidate_id") or "").strip()
    if not candidate_id:
        raise ValidationError("formal report source is missing candidate_id")
    price_convention = identity.get("price_convention")
    if isinstance(price_convention, str) and price_convention in {"normalized_100", "absolute_market"}:
        price_projection: dict[str, Any] = {"spot": "close", "contract_basis": price_convention}
    elif isinstance(price_convention, dict):
        price_projection = dict(price_convention)
    else:
        price_projection = {}
    # Core does not currently publish a separate analysis-basis id.  Use the
    # same immutable contract-derived basis used by Reporter verification,
    # never task_id or a mutable page value.
    analysis_basis_id = f"basis-{fingerprint[:20]}"
    catalog_content_hash = str(contract.get("registry_snapshot_hash") or "").strip()
    product_content_hash = str(contract.get("product_snapshot_hash") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", catalog_content_hash):
        raise ValidationError("formal report source is missing registry_snapshot_hash")
    if not re.fullmatch(r"[0-9a-f]{64}", product_content_hash):
        raise ValidationError("formal report source is missing product_snapshot_hash")
    product_version = str(contract.get("product_version") or "").strip()
    if not product_version:
        raise ValidationError("formal report source is missing product_version")
    catalog_version = str(result.get("catalog_version") or "").strip()
    if not catalog_version:
        raise ValidationError("formal report source is missing catalog_version")
    underlyings = identity.get("underlyings")
    if not isinstance(underlyings, list) or not all(isinstance(value, str) and value for value in underlyings):
        raise ValidationError("formal report source is missing contract underlyings")
    currency = str(identity.get("currency") or "").strip()
    if not currency:
        raise ValidationError("formal report source is missing contract currency")
    return {
        "candidate_id": candidate_id,
        "product_id": product_id,
        "product_name": product_name,
        "product_version": product_version,
        "product_version_content_hash": product_content_hash,
        "catalog_version": catalog_version,
        "catalog_content_hash": catalog_content_hash,
        "contract_fingerprint": fingerprint,
        "analysis_basis_id": analysis_basis_id,
        "analysis_case_id": analysis_case_id,
        "underlyings": list(underlyings),
        "currency": currency,
        "price_convention": price_projection,
        # A report initiated from a user-selected module result has no
        # Recommender candidate to inherit.  Preserve that distinction in a
        # concise, public explanation instead of fabricating a market view or
        # downgrading an otherwise verified run to a legacy record.
        "rank": 1,
        "reason": "本报告围绕当前已选结构，依据已验证的模块结果整理。",
        "suitable_for": [],
        "not_suitable_for": [],
        "main_risks": ["结构收益取决于合同条款与市场路径，可能发生部分或全部本金损失。"],
        "library_status": "ready",
        "key_terms": [],
        "evidence_refs": [],
        "module_run_refs": {}, "module_run_options": {},
    }
