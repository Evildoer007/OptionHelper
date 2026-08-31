"""App authorization adapter for the formal Reporter and Core ResultStore."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

_ROOT = Path(__file__).resolve().parents[3]
for _source in (_ROOT / "core" / "src", _ROOT / "modules" / "reporter" / "src", _ROOT / "modules" / "designer" / "src"):
    if str(_source) not in sys.path:
        sys.path.insert(0, str(_source))

from modules.designer.service import call_tool as designer_call_tool
from modules.reporter.artifact_validator import validate_report_run_directory
from modules.reporter.export_service import rerender_frozen_delivery
from modules.reporter.models import ReporterError
from modules.reporter.selection_facts import build_host_selection_source_refs
from modules.reporter.service import build_selected_request, call_tool as reporter_call_tool
from runtime.protocol.models import ModuleRunRef

from .errors import AuthorizationError, UnavailableCapabilityError, ValidationError
from .identity.session_identity import SessionIdentity
from .stores.result_store import ResultStore


class _DesignerPort:
    """The one public Designer ModulePort used by App-hosted Reporter."""

    def call_tool(self, request: dict[str, Any]) -> dict[str, Any]:
        return dict(designer_call_tool(request))


class _ScopedResultPorts:
    """Re-authorize and verify every Reporter source for one principal."""

    def __init__(self, results: ResultStore, principal: SessionIdentity) -> None:
        self._results, self._principal = results, principal

    def close(self) -> None:
        return None

    def _reference(self, ref: ModuleRunRef, *, tenant_id: str) -> dict[str, str]:
        if tenant_id != self._principal.tenant_id:
            raise PermissionError("ModuleRunRef跨租户访问被拒绝")
        return {
            "module": ref.module, "tenant_id": ref.tenant_id, "task_id": ref.task_id, "run_id": ref.run_id,
            "expected_semantic_result_hash": ref.expected_semantic_result_hash,
            "expected_artifact_manifest_hash": ref.expected_artifact_manifest_hash,
        }

    def verify_module_run(self, ref: ModuleRunRef, *, tenant_id: str) -> None:
        self._results.verify_owned_module_run(self._principal, self._reference(ref, tenant_id=tenant_id))

    def read_module_run_file(self, ref: ModuleRunRef, name: str, *, tenant_id: str) -> bytes:
        return self._results.read_owned_module_run_file(
            self._principal, self._reference(ref, tenant_id=tenant_id), name,
        )

    def read_module_run_bundle(self, ref: ModuleRunRef, *, tenant_id: str) -> dict[str, bytes]:
        return self._results.read_owned_module_run_bundle(
            self._principal, self._reference(ref, tenant_id=tenant_id),
        )

    def list_report_sources(self, *, tenant_id: str, task_id: str | None = None, query: str | None = None) -> dict[str, Any]:
        if tenant_id != self._principal.tenant_id:
            raise PermissionError("报告来源跨租户访问被拒绝")
        catalog = self._results.list_owned_report_sources(self._principal, task_id=task_id, query=query)
        sources = [verified for source in catalog["sources"] if (verified := self._verified_source(source)) is not None]
        return {"tenant_id": tenant_id, "sources": sources}

    def get_report_source(self, *, tenant_id: str, source_id: str) -> dict[str, Any]:
        if tenant_id != self._principal.tenant_id:
            raise PermissionError("报告来源跨租户访问被拒绝")
        source = self._results.get_owned_report_source(self._principal, source_id)
        verified = self._verified_source(source)
        if verified is None:
            raise KeyError(source_id)
        return verified

    def _verified_source(self, source: Mapping[str, Any]) -> dict[str, Any] | None:
        """Only expose refs that Core can currently resolve as committed runs."""

        candidates: list[dict[str, Any]] = []
        for raw_candidate in source.get("candidates", []):
            if not isinstance(raw_candidate, Mapping):
                continue
            raw_options = raw_candidate.get("module_run_options")
            if not isinstance(raw_options, Mapping):
                continue
            versions: dict[tuple[str, str, str], dict[str, Any]] = {}
            for module, source_options in raw_options.items():
                if module not in {"payoff", "pricing", "backtest"} or not isinstance(source_options, list):
                    continue
                expected_run_module = {"payoff": "payoffer", "pricing": "pricer", "backtest": "backtester"}[module]
                for raw_ref in source_options:
                    if not isinstance(raw_ref, Mapping):
                        continue
                    try:
                        run_module = str(raw_ref["module"])
                        if run_module != expected_run_module:
                            continue
                        ref = ModuleRunRef(
                            module=run_module, tenant_id=str(raw_ref["tenant_id"]), task_id=str(raw_ref["task_id"]), run_id=str(raw_ref["run_id"]),
                            expected_semantic_result_hash=str(raw_ref["expected_semantic_result_hash"]),
                            expected_artifact_manifest_hash=str(raw_ref["expected_artifact_manifest_hash"]),
                        )
                        self._results.verify_owned_module_run(self._principal, {
                            "module": ref.module, "tenant_id": ref.tenant_id, "task_id": ref.task_id,
                            "run_id": ref.run_id,
                            "expected_semantic_result_hash": ref.expected_semantic_result_hash,
                            "expected_artifact_manifest_hash": ref.expected_artifact_manifest_hash,
                        })
                        reference = {
                            "module": ref.module, "tenant_id": ref.tenant_id, "task_id": ref.task_id,
                            "run_id": ref.run_id,
                            "expected_semantic_result_hash": ref.expected_semantic_result_hash,
                            "expected_artifact_manifest_hash": ref.expected_artifact_manifest_hash,
                        }
                        manifest = json.loads(self._results.read_owned_module_run_file(
                            self._principal, reference, "manifest.json",
                        ))
                        contract = json.loads(self._results.read_owned_module_run_file(
                            self._principal, reference, "resolved_contract.json",
                        ))
                        if not isinstance(manifest, Mapping) or not isinstance(contract, Mapping):
                            continue
                        fingerprint = str(contract.get("contract_fingerprint") or "")
                        source_candidate_id = str(manifest.get("candidate_id") or "")
                        candidate_version_id = str(manifest.get("candidate_version_id") or "")
                        catalog_candidate_id = str(
                            raw_candidate.get("source_candidate_id") or raw_candidate.get("candidate_id") or ""
                        )
                        if (
                            len(fingerprint) != 64
                            or manifest.get("contract_fingerprint") != fingerprint
                            or not source_candidate_id
                            or source_candidate_id != catalog_candidate_id
                            or contract.get("registry_snapshot_hash") != source.get("catalog_content_hash")
                        ):
                            continue
                    except (KeyError, TypeError, ValueError, PermissionError, ValidationError, OSError, UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    key = (candidate_version_id, fingerprint, source_candidate_id)
                    version = versions.setdefault(key, {"contract": dict(contract), "options": {}})
                    version["options"].setdefault(module, []).append({
                        "module": ref.module, "tenant_id": ref.tenant_id, "task_id": ref.task_id, "run_id": ref.run_id,
                        "expected_semantic_result_hash": ref.expected_semantic_result_hash,
                        "expected_artifact_manifest_hash": ref.expected_artifact_manifest_hash, "status": "succeeded",
                    })

            if not versions:
                continue
            distinct_contracts = {key[1] for key in versions}
            for (candidate_version_id, fingerprint, source_candidate_id), version in sorted(versions.items()):
                # Multiple contract snapshots are selectable only when the
                # upstream run froze an explicit candidate version.  An
                # unversioned variant is omitted instead of becoming a UI
                # option that the formal contract gate must later reject.
                if len(distinct_contracts) > 1 and not candidate_version_id:
                    continue
                contract = version["contract"]
                identity = contract.get("identity") if isinstance(contract.get("identity"), Mapping) else {}
                price_convention = contract.get("price_convention", identity.get("price_convention"))
                if isinstance(price_convention, str):
                    price_convention = {"spot": "close", "contract_basis": price_convention}
                if not isinstance(price_convention, Mapping):
                    continue
                variant_id = str(raw_candidate.get("candidate_id") or source_candidate_id)
                if len(versions) > 1:
                    suffix = hashlib.sha256(f"{candidate_version_id}:{fingerprint}".encode("utf-8")).hexdigest()[:16]
                    variant_id = f"{variant_id[:100]}-{suffix}"
                verified_options = {
                    module: sorted(rows, key=lambda item: (item["run_id"], item["expected_semantic_result_hash"]))
                    for module, rows in version["options"].items()
                }
                single_refs = {
                    module: rows[0]
                    for module, rows in verified_options.items()
                    if len(rows) == 1
                }
                candidate = {
                    **dict(raw_candidate),
                    "candidate_id": variant_id,
                    "source_candidate_id": source_candidate_id,
                    "product_id": identity.get("product_id"),
                    "product_name": identity.get("name_zh"),
                    "product_version": contract.get("product_version"),
                    "product_version_content_hash": contract.get("product_snapshot_hash"),
                    "contract_fingerprint": fingerprint,
                    "analysis_basis_id": contract.get(
                        "analysis_basis_id", identity.get("analysis_basis_id", f"basis-{fingerprint[:20]}"),
                    ),
                    "underlyings": list(identity.get("underlyings", [])),
                    "currency": identity.get("currency"),
                    "price_convention": dict(price_convention),
                    "module_run_options": verified_options,
                    "module_run_refs": single_refs,
                }
                if candidate_version_id:
                    candidate["candidate_version_id"] = candidate_version_id
                candidates.append(candidate)
        if not candidates:
            return None
        verified = {key: value for key, value in source.items() if key != "candidates"}
        verified["candidates"] = candidates
        try:
            verified["source_refs"] = build_host_selection_source_refs(verified, self._principal.tenant_id)
        except ReporterError:
            return None
        return verified


class ReporterAdapter:
    """The sole App entry for a Reporter page selection and ReportRun commit."""

    def __init__(self, results: ResultStore) -> None:
        self._results = results
        self._designer = _DesignerPort()

    def dispatch(self, request: dict[str, Any], principal: SessionIdentity) -> dict[str, Any]:
        ports = _ScopedResultPorts(self._results, principal)
        try:
            return self._dispatch_with_ports(request, principal, ports)
        finally:
            ports.close()

    def _dispatch_with_ports(
        self,
        request: dict[str, Any],
        principal: SessionIdentity,
        ports: _ScopedResultPorts,
    ) -> dict[str, Any]:
        action = str(request.get("action", "status")).strip().lower()
        if action in {"status", "catalog"}:
            return dict(reporter_call_tool({"action": action}, result_store=ports, designer_port=self._designer, selection_port=ports, tenant_id=principal.tenant_id))
        if action in {"list_sources", "list_report_sources"}:
            task_id = _optional_identifier(request.get("task_id"), "task_id")
            catalog = ports.list_report_sources(tenant_id=principal.tenant_id, task_id=task_id, query=_optional_text(request.get("query")))
            return {"ok": True, "module": "reporter", **_public_catalog(catalog)}
        if action == "rerender":
            return self._rerender(request, principal)
        if action != "run":
            raise ValidationError("Reporter action must be status, catalog, list_sources, run or rerender")
        selection = request.get("selection")
        if not isinstance(selection, Mapping):
            raise ValidationError("Reporter run requires one controlled selection object")
        unknown = set(request).difference({"action", "task_id", "kind", "selection"})
        if unknown:
            raise ValidationError(f"Reporter request contains unsupported fields: {','.join(sorted(unknown))}")
        source_id = _identifier(selection.get("source_id"), "selection.source_id")
        source = ports.get_report_source(tenant_id=principal.tenant_id, source_id=source_id)
        task_id = _identifier(source.get("task_id"), "source.task_id")
        host_task = _optional_identifier(request.get("task_id"), "task_id")
        if host_task is not None and host_task != task_id:
            raise ValidationError("task_id is not part of the selected source")
        host_kind = request.get("kind")
        if host_kind is not None and host_kind != selection.get("output_type"):
            raise ValidationError("host kind does not match selection.output_type")
        try:
            expected_request = build_selected_request(
                selection, tenant_id=principal.tenant_id, selection_port=ports,
            )
        except ReporterError as error:
            raise ValidationError(str(error)) from error
        with tempfile.TemporaryDirectory(prefix="report-", dir=self._staging_root()) as temporary:
            response = dict(reporter_call_tool(
                {"action": "run", "selection": dict(selection)}, result_store=ports, designer_port=self._designer,
                selection_port=ports, tenant_id=principal.tenant_id, output_root=Path(temporary) / "runs",
            ))
            if response.get("ok") is not True:
                raise UnavailableCapabilityError("reporter.run", str(response.get("message") or response.get("error") or "Reporter service failed"))
            report_root = _report_root(Path(temporary) / "runs", task_id, selection.get("report_run_id"))
            delivery = _verified_report_delivery(report_root, expected_request=expected_request)
            report_request = delivery["request"]
            committed_request = {**dict(report_request), "reporter_audit": delivery["audit"]}
            report_ref = self._results.commit_report_run(
                principal,
                task_id,
                committed_request,
                delivery["artifacts"],
                status=delivery["status"],
            )
        base = _artifact_url(report_ref["report_run_id"], delivery["report"])
        children = [{
            "candidate_id": item["candidate_id"], "report": item["artifact_name"],
            "display_name": item["display_name"],
            "preview_url": _artifact_url(report_ref["report_run_id"], item["artifact_name"]),
            "download_url": _artifact_url(report_ref["report_run_id"], item["artifact_name"], download=True),
        } for item in delivery["children"]]
        deliveries = [{
            "role": "primary", "display_name": delivery["display_name"], "report": delivery["report"],
            "preview_url": base,
            "download_url": _artifact_url(report_ref["report_run_id"], delivery["report"], download=True),
        }, *({**item, "role": "candidate"} for item in children)]
        return {
            "ok": True, "module": "reporter",
            # The App's longstanding API uses completed for a fully verified
            # delivery.  Keep partial explicit so incomplete reports are
            # never promoted to completed.
            "status": "completed" if delivery["status"] == "succeeded" else "partial", "report_run_ref": report_ref,
            "output": {
                "report": delivery["report"], "children": children, "deliveries": deliveries,
                "delivery_mode": delivery["delivery_mode"], "format": delivery["format"],
                "can_save_pdf": delivery["can_save_pdf"],
            },
            "preview_url": base, "download_url": _artifact_url(report_ref["report_run_id"], delivery["report"], download=True),
        }

    def _rerender(self, request: Mapping[str, Any], principal: SessionIdentity) -> dict[str, Any]:
        """Render another supported format from a saved delivery fact set only."""

        allowed = {"action", "task_id", "source_report_run_id", "output_type", "format", "report_run_id"}
        unknown = set(request).difference(allowed)
        if unknown:
            raise ValidationError(f"Reporter re-render contains unsupported fields: {','.join(sorted(unknown))}")
        source_run_id = _identifier(request.get("source_report_run_id"), "source_report_run_id")
        target_run_id = _identifier(request.get("report_run_id"), "report_run_id")
        output_type = str(request.get("output_type", "")).strip().lower()
        output_format = str(request.get("format", "")).strip().lower()
        if output_type not in {"card", "quote", "report"} or output_format not in {"html", "pdf"}:
            raise ValidationError("冻结交付只能生成HTML或PDF格式的Card、Report或Quote")
        try:
            source = self._results.get_owned_report_run(principal, source_run_id)
        except (KeyError, AuthorizationError) as error:
            raise ValidationError("源交付不存在或不可访问") from error
        task_id = _identifier(source.get("task_id"), "source.task_id")
        host_task = _optional_identifier(request.get("task_id"), "task_id")
        if host_task is not None and host_task != task_id:
            raise ValidationError("目标任务必须与冻结交付所属任务一致")
        source_request = source.get("report_request")
        audit = source_request.get("reporter_audit") if isinstance(source_request, Mapping) else None
        root = audit.get("root") if isinstance(audit, Mapping) else None
        unit = root.get("report_unit") if isinstance(root, Mapping) else None
        if not isinstance(source_request, Mapping) or not isinstance(unit, Mapping):
            raise ValidationError("源交付缺少可验证的冻结事实集")
        original_request = {key: value for key, value in source_request.items() if key != "reporter_audit"}
        with tempfile.TemporaryDirectory(prefix="report-rerender-", dir=self._staging_root()) as temporary:
            try:
                outcome = rerender_frozen_delivery(
                    output_root=Path(temporary) / "runs",
                    source_request=original_request,
                    report_unit=unit,
                    source_report_run_id=source_run_id,
                    report_run_id=target_run_id,
                    output_type=output_type,
                    output_format=output_format,
                    designer_port=self._designer,
                )
                report_root = _report_root(Path(temporary) / "runs", task_id, target_run_id)
                delivery = _verified_report_delivery(report_root, expected_request=None)
            except ReporterError as error:
                raise ValidationError(str(error)) from error
            committed_request = {**dict(delivery["request"]), "reporter_audit": delivery["audit"]}
            report_ref = self._results.commit_report_run(
                principal, task_id, committed_request, delivery["artifacts"], status=delivery["status"],
            )
        return {
            "ok": True,
            "module": "reporter",
            "status": "completed" if delivery["status"] == "succeeded" else "partial",
            "report_run_ref": report_ref,
            "output": {
                "report": delivery["report"], "children": [],
                "deliveries": [{
                    "role": "primary", "display_name": delivery["display_name"], "report": delivery["report"],
                    "preview_url": _artifact_url(report_ref["report_run_id"], delivery["report"]),
                    "download_url": _artifact_url(report_ref["report_run_id"], delivery["report"], download=True),
                }],
                "delivery_mode": delivery["delivery_mode"], "format": delivery["format"],
                "can_save_pdf": delivery["can_save_pdf"],
            },
            "preview_url": _artifact_url(report_ref["report_run_id"], delivery["report"]),
            "download_url": _artifact_url(report_ref["report_run_id"], delivery["report"], download=True),
        }

    def _staging_root(self) -> str:
        root = self._results._state._root / "report-staging"
        root.mkdir(parents=True, exist_ok=True)
        return str(root)


def _report_root(output_root: Path, task_id: str, report_run_id: object) -> Path:
    run_id = _identifier(report_run_id, "selection.report_run_id")
    root = (output_root.resolve() / task_id / run_id).resolve()
    if output_root.resolve() not in root.parents or not root.is_dir() or root.is_symlink():
        raise ValidationError("Reporter did not return a controlled ReportRun")
    return root


def _verified_report_delivery(root: Path, *, expected_request: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only Manifest-declared rendered files; preserve all audit JSON in ReportRun facts."""

    try:
        validation = validate_report_run_directory(root, expected_request=expected_request)
    except ReporterError as error:
        raise ValidationError(f"Reporter ReportRun完整性校验失败：{error}") from error
    artifacts: list[dict[str, Any]] = []
    root_artifact = _add_rendered(artifacts, validation, artifact_prefix="")
    request = validation["request"]
    subject_ref = request.get("subject_ref") if isinstance(request.get("subject_ref"), Mapping) else {}
    delivery_mode = str(subject_ref.get("delivery_mode", "single"))
    output_format = str(request.get("format", "html"))
    metadata = request.get("metadata") if isinstance(request.get("metadata"), Mapping) else {}
    display_name = str(metadata.get("title") or "报告")
    can_save_pdf = output_format == "html" and delivery_mode in {"single", "comparison", "quote"}
    audit = {"schema": "optionhelper.app-reporter-audit", "root": _audit_record(validation), "children": []}
    children = []
    for child in validation["children"]:
        candidate_id = child["candidate_id"]
        child_validation = child["validation"]
        artifact_name = _add_rendered(
            artifacts, child_validation, artifact_prefix=_child_artifact_prefix(candidate_id),
        )
        unit = child_validation.get("unit") if isinstance(child_validation.get("unit"), Mapping) else {}
        subject = unit.get("subject") if isinstance(unit.get("subject"), Mapping) else {}
        child_display_name = f"{subject.get('product_name') or candidate_id}独立报告"
        audit["children"].append({
            "candidate_id": candidate_id, "artifact_name": artifact_name,
            "display_name": child_display_name, **_audit_record(child_validation),
        })
        children.append({
            "candidate_id": candidate_id, "artifact_name": artifact_name,
            "display_name": child_display_name,
        })
    audit["public_delivery"] = {
        "delivery_mode": delivery_mode, "format": output_format, "can_save_pdf": can_save_pdf,
        "deliveries": [
            {"role": "primary", "display_name": display_name, "artifact_name": root_artifact},
            *({"role": "candidate", **item} for item in children),
        ],
    }
    return {
        "request": request, "artifacts": artifacts, "report": root_artifact,
        "display_name": display_name, "children": children, "audit": audit,
        "delivery_mode": delivery_mode, "format": output_format, "can_save_pdf": can_save_pdf,
        "status": validation["delivery_status"],
    }


def _audit_record(validation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_manifest": dict(validation["manifest"]),
        "report_unit": dict(validation["unit"]),
        "designer_input": dict(validation["payload"]),
        "design_brief": dict(validation["brief"]),
        "designer_artifact_manifest": dict(validation["receipt"]),
    }


def _add_rendered(artifacts: list[dict[str, Any]], validation: Mapping[str, Any], *, artifact_prefix: str) -> str:
    manifest = validation["manifest"]
    rendered = manifest.get("rendered")
    if not isinstance(rendered, Mapping):
        raise ValidationError("Reporter rendered manifest is missing")
    original = rendered.get("path")
    output_format = rendered.get("format")
    if not isinstance(original, str) or Path(original).name != original or output_format not in {"html", "pdf"}:
        raise ValidationError("Reporter rendered artifact is invalid")
    path = validation["rendered_path"]
    content = path.read_bytes()
    name = f"{artifact_prefix}{original}"
    if any(item["name"] == name for item in artifacts):
        raise ValidationError("Reporter rendered artifact name is not unique")
    artifacts.append({"name": name, "content_type": "application/pdf" if output_format == "pdf" else "text/html; charset=utf-8", "content": content})
    root = validation.get("root")
    portable = rendered.get("portable_assets", [])
    if not isinstance(root, Path) or not isinstance(portable, list):
        raise ValidationError("Reporter portable artifact bundle is invalid")
    for index, item in enumerate(portable, start=1):
        if not isinstance(item, Mapping):
            raise ValidationError(f"Reporter portable artifact {index} is invalid")
        relative = item.get("path")
        media_type = item.get("media_type")
        if not isinstance(relative, str) or not isinstance(media_type, str):
            raise ValidationError(f"Reporter portable artifact {index} is invalid")
        source = (root / relative).resolve()
        if root.resolve() not in source.parents or not source.is_file() or source.is_symlink():
            raise ValidationError(f"Reporter portable artifact {index} escapes its ReportRun")
        portable_name = f"{artifact_prefix}{Path(relative).as_posix()}"
        if any(item["name"] == portable_name for item in artifacts):
            raise ValidationError("Reporter portable artifact name is not unique")
        artifacts.append({"name": portable_name, "content_type": media_type, "content": source.read_bytes()})
    return name


def _child_artifact_prefix(candidate_id: str) -> str:
    return f"candidate-{hashlib.sha256(candidate_id.encode()).hexdigest()[:20]}/"


def _public_catalog(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Project the browser catalog without internal pricing-basis metadata."""
    sources: list[dict[str, Any]] = []
    for raw_source in catalog["sources"]:
        source = {key: value for key, value in raw_source.items() if key not in {"source_refs", "candidates"}}
        source["candidates"] = [
            {key: value for key, value in candidate.items() if key not in {"currency", "price_convention"}}
            for candidate in raw_source.get("candidates", [])
            if isinstance(candidate, Mapping)
        ]
        sources.append(source)
    return {"tenant_id": catalog["tenant_id"], "sources": sources}


def _artifact_url(report_run_id: str, artifact_name: str, *, download: bool = False) -> str:
    suffix = "?download=1" if download else ""
    return f"/api/reports/{report_run_id}/artifacts/{artifact_name}{suffix}"


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} is required for Reporter")
    return value


def _optional_identifier(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, field)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("query must be a string")
    return value
