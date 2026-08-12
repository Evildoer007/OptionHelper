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
from typing import Any
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
        reference = self._commit_files(identity, task_id, module, run_id, files, normalized_result)
        return reference

    def bind_module_store(self, identity: SessionIdentity, task_id: str) -> "_BoundModuleResultStore":
        """Return a Core ResultStorePort scoped to one authenticated App task."""
        self._require_task_owner(identity, task_id)
        return _BoundModuleResultStore(self, identity, task_id)

    def _commit_files(
        self,
        identity: SessionIdentity,
        task_id: str,
        module: str,
        run_id: str,
        files: dict[str, Any],
        normalized_result: dict[str, Any],
    ) -> dict[str, str]:
        if module not in {"payoffer", "pricer", "backtester"}:
            raise ValidationError("ModuleRun module is invalid")
        self._require_task_owner(identity, task_id)
        record_key = _module_run_key(module, run_id)
        records = self._state.read("results")
        if record_key in records:
            raise ValidationError("ModuleRun records are immutable; create a new run_id")
        expected_hash = _expected_core_result_hash(files)
        pending_reference = {
            "module": module,
            "tenant_id": identity.tenant_id,
            "task_id": task_id,
            "run_id": run_id,
            "expected_semantic_result_hash": expected_hash,
        }
        record = {
            **pending_reference,
            "reference_version": "module-run-ref/v1.2",
            "anchor_state": "pending_commit",
            "created_by": identity.principal_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
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
        if pending_reference != {
            "module": core_ref.module, "tenant_id": core_ref.tenant_id, "task_id": core_ref.task_id,
            "run_id": core_ref.run_id, "expected_semantic_result_hash": core_ref.expected_semantic_result_hash,
        }:
            raise ValidationError("Core ModuleRun返回引用与预提交恢复记录不一致")
        reference = _module_run_ref_dict(core_ref)
        record.update(reference)
        record["anchor_state"] = "anchored"
        self._replace_recovery(record_key, record)

        def update(value: dict[str, Any]) -> dict[str, Any]:
            legacy = value.get(run_id)
            if record_key in value or (isinstance(legacy, dict) and legacy.get("module") == module):
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
                if record.get("reference_version") != "module-run-ref/v1.2" or record.get("anchor_state") != "anchored":
                    # Never bless a new or historical unanchored run from the
                    # mutable store bytes during automatic startup recovery.
                    continue
                ref = ModuleRunRef(**{key: record[key] for key in current_fields})
                self._core.resolve_module_run(ref, tenant_id=ref.tenant_id)
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

    def resolve_owned_module_run(self, identity: SessionIdentity, reference: dict[str, str]) -> Path:
        """Resolve one Core ModuleRun only after App ownership re-authorization."""
        self.resolve_module_run(identity, reference)
        ref = ModuleRunRef(
            module=str(reference["module"]), tenant_id=str(reference["tenant_id"]), task_id=str(reference["task_id"]), run_id=str(reference["run_id"]),
            expected_semantic_result_hash=str(reference["expected_semantic_result_hash"]),
            expected_artifact_manifest_hash=str(reference["expected_artifact_manifest_hash"]),
        )
        try:
            return self._core.resolve_module_run(ref, tenant_id=identity.tenant_id)
        except (StoreError, FileNotFoundError, PermissionError) as error:
            raise ValidationError(f"Core ModuleRun不可用：{error}") from error

    def resolve_module_run(self, identity: SessionIdentity, reference: dict[str, str]) -> dict[str, Any]:
        run_id = reference.get("run_id")
        record = _stored_module_run(self._state.read("results"), reference.get("module"), run_id)
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

    def verified_fact_summary(self, identity: SessionIdentity, reference: dict[str, str]) -> dict[str, Any]:
        """Project selected numerical facts from one still-verifiable ModuleRun.

        This is intentionally a presentation projection, not a second pricer
        or backtester.  The Core commit marker and semantic hash are resolved
        before the App reads the immutable indexed result.  Neither tenant,
        principal, paths, raw payloads nor arbitrary fields leave this method.
        """
        run_directory = self.resolve_owned_module_run(identity, reference)
        record = self.resolve_module_run(identity, reference)
        try:
            result = json.loads((run_directory / "result.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValidationError("已验证ModuleRun缺少可读取结果") from error
        if not isinstance(result, dict) or semantic_hash(result) != record.get("expected_semantic_result_hash"):
            raise ValidationError("ModuleRun事实哈希不匹配")
        run_ref = {
            "module": str(record["module"]),
            "run_id": str(record["run_id"]),
            "result_hash": str(record["expected_semantic_result_hash"]),
        }
        facts = _result_facts(str(record["module"]), result, run_ref["result_hash"])
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
            if not isinstance(record, dict) or record.get("tenant_id") != tenant_id:
                continue
            if record.get("module") not in {"payoffer", "pricer", "backtester"} or not _completed(record.get("result")):
                continue
            if task_id is not None and record.get("task_id") != task_id:
                continue
            if not self._verified_report_record(record, tenant_id):
                continue
            candidate = _report_candidate(record)
            if needle and needle not in json.dumps(candidate, ensure_ascii=False, sort_keys=True).lower():
                continue
            source_id = _source_id(tenant_id, str(record["task_id"]), candidate["analysis_case_id"])
            source = grouped.setdefault(source_id, {
                "source_id": source_id,
                "label": f"{candidate['product_name']} · {record['task_id']}",
                "tenant_id": tenant_id,
                "task_id": record["task_id"],
                "analysis_case_id": candidate["analysis_case_id"],
                "catalog_version": candidate["product_version"],
                "candidates": {},
            })
            row = source["candidates"].setdefault(candidate["candidate_id"], candidate)
            row["module_run_refs"][_report_module(str(record["module"]))] = {
                "module": record["module"], "tenant_id": record["tenant_id"], "task_id": record["task_id"], "run_id": record["run_id"],
                "expected_semantic_result_hash": record["expected_semantic_result_hash"],
                "expected_artifact_manifest_hash": record["expected_artifact_manifest_hash"], "status": "succeeded",
            }
        sources: list[dict[str, Any]] = []
        for grouped_source in grouped.values():
            source = {**grouped_source, "candidates": list(grouped_source["candidates"].values())}
            source["source_refs"] = build_host_selection_source_refs(source, tenant_id)
            sources.append(source)
        return {"tenant_id": tenant_id, "sources": sources}

    def _verified_report_record(self, record: dict[str, Any], tenant_id: str) -> bool:
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
            self._core.resolve_module_run(reference, tenant_id=tenant_id)
        except (KeyError, TypeError, ValueError, StoreError, FileNotFoundError, PermissionError):
            return False
        return True

    def list_owned_report_sources(self, identity: SessionIdentity, *, task_id: str | None = None, query: str | None = None) -> dict[str, Any]:
        if task_id is not None:
            self._require_task_owner(identity, task_id)
        catalog = self.list_report_sources(tenant_id=identity.tenant_id, task_id=task_id, query=query)
        owned = []
        for source in catalog["sources"]:
            refs = [ref for candidate in source["candidates"] for ref in candidate["module_run_refs"].values()]
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
    ) -> dict[str, str]:
        """Commit an immutable ReportRun and its manifest-declared bytes.

        The App never accepts a filesystem path.  HTML/PDF bytes are held under
        the App's private state store and can later be read only by their exact
        manifest name and only after tenant/principal authorization.
        """
        self._require_task_owner(identity, task_id)
        if not isinstance(report_request, dict) or not isinstance(artifacts, list) or not artifacts:
            raise ValidationError("report_request and at least one artifact are required for a ReportRun")
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
            "status": "succeeded",
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

    def __init__(self, owner: ResultStore, identity: SessionIdentity, task_id: str) -> None:
        self._owner = owner
        self._identity = identity
        self._task_id = task_id

    def commit_module_run(
        self, *, module: str, tenant_id: str, task_id: str, run_id: str, files: dict[str, Any],
    ) -> ModuleRunRef:
        if tenant_id != self._identity.tenant_id or task_id != self._task_id:
            raise AuthorizationError("result.write", "ModuleRun scope does not match authenticated App task")
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
            analysis_case_id = str(manifest.get("analysis_case_id") or "").strip()
            raw_contract = committed_files.get("resolved_contract.json")
            if isinstance(raw_contract, dict):
                committed_contract = dict(raw_contract)
                identity = committed_contract.get("identity")
                identity = dict(identity) if isinstance(identity, dict) else {}
                if analysis_case_id and not committed_contract.get("analysis_basis_id"):
                    committed_contract["analysis_basis_id"] = analysis_case_id
                raw_convention = committed_contract.get("price_convention", identity.get("price_convention"))
                if isinstance(raw_convention, dict):
                    committed_contract["price_convention"] = dict(raw_convention)
                elif isinstance(raw_convention, str) and raw_convention.strip():
                    committed_contract["price_convention"] = {"convention": raw_convention.strip()}
                else:
                    committed_contract["price_convention"] = {}
                committed_files["resolved_contract.json"] = committed_contract
                if isinstance(committed_result.get("resolved_contract"), dict):
                    committed_result["resolved_contract"] = dict(committed_contract)
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
            normalized_result = {**committed_result, "status": lifecycle_status}
        else:
            normalized_result = {
            "ok": False,
            "module": module,
            "status": "failed",
            "error": files.get("error.json", {"message": "module did not provide result.json"}),
            }
        reference = self._owner._commit_files(
            self._identity, task_id, module, _store_id(run_id, "run"), committed_files, normalized_result,
        )
        return ModuleRunRef(**reference)

    def resolve_module_run(self, ref: ModuleRunRef, *, tenant_id: str) -> Path:
        if tenant_id != self._identity.tenant_id or ref.task_id != self._task_id:
            raise AuthorizationError("result.read", "ModuleRun scope does not match authenticated App task")
        return self._owner.resolve_owned_module_run(self._identity, {
            "module": ref.module,
            "tenant_id": ref.tenant_id,
            "task_id": ref.task_id,
            "run_id": ref.run_id,
            "expected_semantic_result_hash": ref.expected_semantic_result_hash,
            "expected_artifact_manifest_hash": ref.expected_artifact_manifest_hash,
        })


_PRICING_FACTS = (("pv", "PV"), ("standard_error", "标准误"))
_GREEK_NAMES = {"delta": "Delta", "gamma": "Gamma", "vega": "Vega", "theta": "Theta", "rho": "Rho"}
_BACKTEST_FACTS = (
    ("sample_count", "样本数"), ("skipped_count", "跳过样本数"), ("win_rate", "胜率"),
    ("average_pnl", "平均损益"), ("median_pnl", "中位损益"), ("minimum_pnl", "最小损益"),
    ("maximum_pnl", "最大损益"), ("max_loss", "最大损失"), ("average_return", "平均收益率"),
    ("median_return", "中位收益率"), ("minimum_return", "最小收益率"), ("maximum_return", "最大收益率"),
)


def _result_facts(module: str, result: dict[str, Any], result_hash: str) -> list[dict[str, Any]]:
    """Select a small stable display vocabulary from a verified result."""
    source: dict[str, Any]
    selected: list[tuple[str, str, object, str | None]] = []
    if module == "pricer":
        source = result.get("pricing") if isinstance(result.get("pricing"), dict) else {}
        currency = str(source.get("currency") or result.get("currency") or "") or None
        for key, label in _PRICING_FACTS:
            value = _number(source.get(key))
            if value is not None:
                selected.append((key, label, value, currency if key == "pv" else None))
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
        for key, label in _BACKTEST_FACTS:
            value = _number(metrics.get(key)) if isinstance(metrics, dict) else None
            if value is not None:
                selected.append((key, label, value, "ratio" if "rate" in key or "return" in key else None))
    else:
        selected = []
    return [
        {
            "fact_ref": _fact_ref(result_hash, path, value),
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
    direct = _number(value)
    if direct is not None:
        return direct, None
    if not isinstance(value, dict):
        return None, None
    for key in ("value", "amount", "pv_amount_value"):
        numeric = _number(value.get(key))
        if numeric is not None:
            unit = value.get("unit") or value.get("pv_amount_unit")
            return numeric, str(unit) if isinstance(unit, str) and unit else None
    return None, None


def _fact_ref(result_hash: str, path: str, value: object) -> str:
    payload = json.dumps({"result_hash": result_hash, "path": path, "value": value}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"fact_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _completed(value: object) -> bool:
    return isinstance(value, dict) and value.get("ok") is not False and str(value.get("status", "")).lower() in {"succeeded", "completed", "complete", "priced"}


def _store_id(value: object, prefix: str) -> str:
    text = str(value or "")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", text) and text not in {".", ".."}:
        return text
    return f"{prefix}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]}"


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
    if not isinstance(record, dict):
        legacy = records.get(run_id)
        record = legacy if isinstance(legacy, dict) and legacy.get("module") == module else None
    return record


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
    status_raw = str(value.get("status", "")).lower()
    status = "succeeded" if _completed(value) else status_raw if status_raw in {"partial", "failed", "unsupported", "cancelled", "timed_out"} else "failed"
    contract = value.get("resolved_contract") if isinstance(value.get("resolved_contract"), dict) else value.get("contract")
    resolved_contract = dict(contract) if isinstance(contract, dict) else {}
    analysis_case_id = _store_id(value.get("analysis_case_id") or task_id, "case")
    contract_fingerprint = str(resolved_contract.get("contract_fingerprint") or value.get("contract_fingerprint") or "")
    candidate_id = _store_id(
        value.get("candidate_id") or f"candidate_{hashlib.sha256((contract_fingerprint or safe_run_id).encode('utf-8')).hexdigest()[:24]}",
        "candidate",
    )
    value["candidate_id"] = candidate_id
    execution_fingerprint = value.get("execution_fingerprint")
    if not isinstance(execution_fingerprint, str) or not execution_fingerprint:
        execution_fingerprint = semantic_hash({"module": module, "task_id": task_id, "run_id": safe_run_id, "result": value})
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


def _expected_core_result_hash(files: dict[str, Any]) -> str:
    manifest = files.get("manifest.json")
    if not isinstance(manifest, dict):
        raise ValidationError("ModuleRun manifest无效")
    status = str(manifest.get("status", ""))
    if "result.json" in files:
        value = files["result.json"]
        if not isinstance(value, dict):
            raise ValidationError("ModuleRun result无效")
        return semantic_hash(value)
    error = files.get("error.json")
    if not isinstance(error, dict):
        raise ValidationError("ModuleRun error无效")
    return semantic_hash({"status": status, "error": error})


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
    return {key: record[key] for key in ("tenant_id", "task_id", "report_run_id", "status", "created_at", "report_request", "artifact_manifest", "expected_semantic_fact_hash", "expected_artifact_manifest_hash")}


def _report_module(module: str) -> str:
    return {"payoffer": "payoff", "pricer": "pricing", "backtester": "backtest"}[module]


def _source_id(tenant_id: str, task_id: str, analysis_case_id: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}:{task_id}:{analysis_case_id}".encode("utf-8")).hexdigest()[:24]
    return f"source_{digest}"


def _report_candidate(record: dict[str, Any]) -> dict[str, Any]:
    result = record["result"]
    contract = result.get("resolved_contract") if isinstance(result.get("resolved_contract"), dict) else result.get("contract", {})
    identity = contract.get("identity", {}) if isinstance(contract, dict) else {}
    product_id = str(identity.get("product_id") or result.get("product_id") or "unknown-product")
    product_name = str(identity.get("name_zh") or result.get("product_name") or product_id)
    fingerprint = str(contract.get("contract_fingerprint") or result.get("contract_fingerprint") or record["expected_semantic_result_hash"])
    analysis_case_id = str(result.get("analysis_case_id") or record["task_id"])
    candidate_id = str(result.get("candidate_id") or f"candidate_{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:24]}")
    return {
        # Reuse the candidate identity frozen in the submitted result.  A
        # fallback only serves older App-indexed runs that predate it.
        "candidate_id": candidate_id,
        "product_id": product_id,
        "product_name": product_name,
        "product_version": str(contract.get("product_version") or result.get("product_version") or "unversioned"),
        "contract_fingerprint": fingerprint,
        "analysis_basis_id": str(contract.get("analysis_basis_id") or result.get("analysis_basis_id") or analysis_case_id),
        "analysis_case_id": analysis_case_id,
        "underlyings": list(identity.get("underlyings") or result.get("underlyings") or []),
        "currency": str(identity.get("currency") or result.get("currency") or "CNY"),
        "price_convention": dict(contract.get("price_convention") or result.get("price_convention") or {}),
        # A report initiated from a user-selected module result has no
        # Recommender candidate to inherit.  Preserve that distinction in a
        # concise, public explanation instead of fabricating a market view or
        # downgrading an otherwise verified run to a legacy record.
        "rank": 1,
        "reason": "本报告围绕当前已选结构，依据已验证的模块结果整理。",
        "suitable_for": [],
        "not_suitable_for": [],
        "main_risks": [],
        "library_status": "ready",
        "key_terms": [],
        "evidence_refs": [],
        "module_run_refs": {},
    }
