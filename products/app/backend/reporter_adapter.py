"""App authorization adapter for the formal Reporter and Core ResultStore."""

from __future__ import annotations

import base64
import hashlib
import re
import json
import sys
import tempfile
from datetime import datetime
from math import isfinite
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

_ROOT = Path(__file__).resolve().parents[3]
for _source in (_ROOT / "core" / "src", _ROOT / "modules" / "reporter" / "src", _ROOT / "modules" / "designer" / "src"):
    if str(_source) not in sys.path:
        sys.path.insert(0, str(_source))

from modules.designer.service import call_tool as designer_call_tool
from modules.reporter.artifact_validator import validate_report_run_directory
from modules.reporter.export_service import rerender_frozen_delivery
from modules.reporter.models import ReporterError
from modules.reporter.evidence_resolver import report_contracts_compatible
from modules.reporter.service import build_selected_request, call_tool as reporter_call_tool
from runtime.protocol.models import ModuleRunRef

from .errors import AuthorizationError, UnavailableCapabilityError, ValidationError
from .identity.session_identity import SessionIdentity
from .stores.result_store import ResultStore, _report_evidence_key



def standalone_report_html(content: bytes, artifact_name: str,
                           read_asset: Callable[[str], tuple[bytes, str]]) -> bytes:
    """Export one offline HTML without changing the frozen ReportRun artifact.

    Only the report's own verified chart asset is resolved. Both desktop hosts
    receive the same bytes, including for reports saved before this fix.
    """
    html = content.decode("utf-8")
    script = re.compile(r'<script\b(?P<attrs>[^>]*)>\s*</script\s*>', re.IGNORECASE)
    source = re.compile(r'\bsrc\s*=\s*([\'"])(.*?)\1', re.IGNORECASE)

    def embed(match: re.Match[str]) -> str:
        reference = source.search(match.group("attrs"))
        if reference is None:
            return match.group(0)
        relative = PurePosixPath(reference.group(2))
        if relative != PurePosixPath("assets/echarts.min.js"):
            raise ValidationError("HTML包含未识别的外部脚本，无法生成离线文件")
        name = str(PurePosixPath(artifact_name).parent / relative)
        asset, media_type = read_asset(name)
        if media_type.split(";", 1)[0] not in {"application/javascript", "text/javascript"} or not asset:
            raise ValidationError("报告的离线图表资源无效")
        encoded = base64.b64encode(asset).decode("ascii")
        return ('<script data-optionhelper-runtime="echarts">'
                '(()=>{const bytes=Uint8Array.from(atob("' + encoded + '"),c=>c.charCodeAt(0));'
                'const runtime=document.createElement("script");'
                'runtime.textContent=new TextDecoder().decode(bytes);document.head.appendChild(runtime);})();'
                '</script>')

    return script.sub(embed, html).encode("utf-8")


def _source_display_terms(contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read the frozen contractual tenor without guessing missing terms."""
    terms = contract.get("terms")
    years = terms.get("T") if isinstance(terms, Mapping) else None
    if isinstance(years, bool) or not isinstance(years, (int, float)) or not isfinite(years) or years <= 0:
        return []
    return [{"label": "期限", "value": years, "unit": "年"}]


def _source_run_display(manifest: Mapping[str, Any], contract: Mapping[str, Any], *, saved_created_at: Any = None) -> dict[str, Any]:
    """Use verified manifest time, then the saved index creation time; never the clock."""
    display: dict[str, Any] = {"terms": _source_display_terms(contract)}
    metadata = manifest.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    for field in ("completed_at", "finished_at", "created_at", "started_at"):
        for prefix, record in (("", manifest), ("metadata.", metadata)):
            value = record.get(field)
            if not isinstance(value, str):
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                continue
            display.update({"run_time": value, "time_field": prefix + field})
            return display
    if isinstance(saved_created_at, str):
        try:
            parsed = datetime.fromisoformat(saved_created_at.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            if parsed.tzinfo is not None:
                display.update(run_time=saved_created_at, time_field="saved_record.created_at")
    return display


class _DesignerPort:
    """The one public Designer ModulePort used by App-hosted Reporter."""

    def call_tool(self, request: dict[str, Any]) -> dict[str, Any]:
        return dict(designer_call_tool(request))


class _ScopedResultPorts:
    """Re-authorize and verify every Reporter source for one principal."""

    def __init__(self, results: ResultStore, principal: SessionIdentity, tasks=None) -> None:
        self._results, self._principal, self._tasks = results, principal, tasks

    def close(self) -> None:
        return None

    def _reference(self, ref: ModuleRunRef, *, tenant_id: str) -> dict[str, str]:
        if tenant_id != self._principal.tenant_id:
            raise PermissionError("ModuleRunRef跨租户访问被拒绝")
        return {
            "module": ref.module, "tenant_id": ref.tenant_id, "task_id": ref.task_id, "run_id": ref.run_id,
            "expected_result_file_hash": ref.expected_result_file_hash,
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
        loader = getattr(self._results, "list_owned_report_sources_with_evidence", None)
        if callable(loader):
            catalog, evidence = loader(self._principal, task_id=task_id, query=query)
        else:
            catalog = self._results.list_owned_report_sources(self._principal, task_id=task_id, query=query)
            evidence = None
        diagnostics: dict[str, int] = {}
        sources = [
            verified
            for source in catalog["sources"]
            if (verified := self._verified_source(source, evidence=evidence, diagnostics=diagnostics)) is not None
        ]
        source_diagnostics = dict(catalog.get("source_diagnostics", {}))
        failure_codes = dict(source_diagnostics.get("failure_codes", {}))
        for code, count in diagnostics.items():
            failure_codes[code] = failure_codes.get(code, 0) + count
        source_diagnostics["failure_codes"] = failure_codes
        source_diagnostics["excluded_run_count"] = sum(failure_codes.values())
        return {"tenant_id": tenant_id, "sources": sources, "source_diagnostics": source_diagnostics}

    def get_report_source(self, *, tenant_id: str, source_id: str) -> dict[str, Any]:
        if tenant_id != self._principal.tenant_id:
            raise PermissionError("报告来源跨租户访问被拒绝")
        source = self._results.get_owned_report_source(self._principal, source_id)
        verified = self._verified_source(source)
        if verified is None:
            raise KeyError(source_id)
        return verified

    def _verified_source(
        self,
        source: Mapping[str, Any],
        *,
        evidence: Mapping[tuple[str, ...], Mapping[str, Any]] | None = None,
        diagnostics: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        """Only expose refs that Core can currently resolve as committed runs."""

        def reject_binding() -> None:
            if diagnostics is not None:
                diagnostics["report_binding_invalid"] = diagnostics.get("report_binding_invalid", 0) + 1

        candidates: list[dict[str, Any]] = []
        pending = self._tasks.pending_recommendation(self._principal, str(source["task_id"])) if self._tasks else None
        recommendation_index = {
            item["candidate_id"]: {**item, "rank": rank} for rank, item in enumerate((pending or {}).get("candidates", []), 1)
        }
        for raw_candidate in source.get("candidates", []):
            if not isinstance(raw_candidate, Mapping):
                reject_binding()
                continue
            raw_options = raw_candidate.get("module_run_options")
            if not isinstance(raw_options, Mapping):
                reject_binding()
                continue
            snapshot_groups: list[dict[str, Any]] = []
            for module, source_options in raw_options.items():
                if module not in {"payoff", "pricing", "backtest"} or not isinstance(source_options, list):
                    reject_binding()
                    continue
                expected_run_module = {"payoff": "payoffer", "pricing": "pricer", "backtest": "backtester"}[module]
                for raw_ref in source_options:
                    if not isinstance(raw_ref, Mapping):
                        reject_binding()
                        continue
                    try:
                        run_module = str(raw_ref["module"])
                        if run_module != expected_run_module:
                            reject_binding()
                            continue
                        ref = ModuleRunRef(
                            module=run_module, tenant_id=str(raw_ref["tenant_id"]), task_id=str(raw_ref["task_id"]), run_id=str(raw_ref["run_id"]),
                            expected_result_file_hash=str(raw_ref["expected_result_file_hash"]),
                            expected_artifact_manifest_hash=str(raw_ref["expected_artifact_manifest_hash"]),
                        )
                        reference = self._reference(ref, tenant_id=ref.tenant_id)
                        saved_created_at = None
                        if evidence is None:
                            self._results.verify_owned_module_run(self._principal, reference)
                            manifest = json.loads(self._results.read_owned_module_run_file(
                                self._principal, reference, "manifest.json",
                            ))
                            contract = json.loads(self._results.read_owned_module_run_file(
                                self._principal, reference, "resolved_contract.json",
                            ))
                        else:
                            verified_files = evidence.get(_report_evidence_key(reference))
                            if not isinstance(verified_files, Mapping):
                                raise KeyError(ref.run_id)
                            saved_created_at = verified_files.get("saved_created_at")
                            manifest = verified_files.get("manifest")
                            contract = verified_files.get("resolved_contract")
                        if not isinstance(manifest, Mapping) or not isinstance(contract, Mapping):
                            reject_binding()
                            continue
                        identity = contract.get("identity")
                        if not isinstance(identity, Mapping):
                            reject_binding()
                            continue
                        product_id = identity.get("product_id")
                        rule_revision = identity.get("rule_revision")
                        source_candidate_id = str(manifest.get("candidate_id") or "")
                        catalog_candidate_id = str(
                            raw_candidate.get("source_candidate_id") or raw_candidate.get("candidate_id") or ""
                        )
                        if (
                            not isinstance(product_id, str)
                            or not product_id
                            or isinstance(rule_revision, bool)
                            or not isinstance(rule_revision, int)
                            or rule_revision <= 0
                            or manifest.get("product_id") != product_id
                            or manifest.get("rule_revision") != rule_revision
                            or not source_candidate_id
                            or source_candidate_id != catalog_candidate_id
                            or raw_candidate.get("product_id", product_id) != product_id
                            or raw_candidate.get("rule_revision", rule_revision) != rule_revision
                        ):
                            reject_binding()
                            continue
                    except (KeyError, TypeError, ValueError, PermissionError, ValidationError, OSError, UnicodeDecodeError, json.JSONDecodeError):
                        reject_binding()
                        continue
                    group = next(
                        (
                            item
                            for item in snapshot_groups
                            if item["source_candidate_id"] == source_candidate_id
                            and (
                                item["contract"] == dict(contract)
                                or (module not in item["options"] and all(
                                    report_contracts_compatible(existing, contract, existing_module, module)
                                    for existing_module, existing in item["contracts"].items()
                                ))
                            )
                        ),
                        None,
                    )
                    if group is None:
                        group = {
                            "source_candidate_id": source_candidate_id,
                            "contract": dict(contract),
                            "options": {},
                            "contracts": {},
                            "run_display": {},
                        }
                        snapshot_groups.append(group)
                    group["contracts"][module] = dict(contract)
                    # Prefer a dated market contract for the report subject.
                    if module == "pricing" or (module == "backtest" and "pricing" not in group["options"]):
                        group["contract"] = dict(contract)
                    group["options"].setdefault(module, []).append({
                        **reference,
                        "status": "succeeded",
                    })
                    group["run_display"][ref.run_id] = _source_run_display(
                        manifest, contract, saved_created_at=saved_created_at,
                    )

            if not snapshot_groups:
                continue
            snapshot_groups.sort(
                key=lambda item: min(
                    str(ref["run_id"])
                    for rows in item["options"].values()
                    for ref in rows
                )
            )
            for group_index, group in enumerate(snapshot_groups, start=1):
                source_candidate_id = group["source_candidate_id"]
                contract = group["contract"]
                identity = contract.get("identity") if isinstance(contract.get("identity"), Mapping) else {}
                price_convention = identity.get("price_convention")
                if isinstance(price_convention, str):
                    price_convention = {"spot": "close", "contract_basis": price_convention}
                if not isinstance(price_convention, Mapping):
                    reject_binding()
                    continue
                variant_id = str(raw_candidate.get("candidate_id") or source_candidate_id)
                if len(snapshot_groups) > 1:
                    first_run_id = min(
                        str(ref["run_id"])
                        for rows in group["options"].values()
                        for ref in rows
                    )
                    variant_id = f"{variant_id[:96]}-run-{first_run_id[:24]}-{group_index}"
                verified_options = {
                    module: sorted(rows, key=lambda item: (item["run_id"], item["expected_result_file_hash"]))
                    for module, rows in group["options"].items()
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
                    "rule_revision": identity.get("rule_revision"),
                    "analysis_basis_id": f"basis-{min(ref['run_id'] for rows in verified_options.values() for ref in rows)}",
                    "underlyings": list(identity.get("underlyings", [])),
                    "currency": identity.get("currency"),
                    "price_convention": dict(price_convention),
                    "resolved_contract_snapshot": contract,
                    "display_terms": _source_display_terms(contract),
                    "run_display": group["run_display"],
                    "module_run_options": verified_options,
                    "module_run_refs": single_refs,
                }
                recommended = recommendation_index.get(source_candidate_id)
                if isinstance(recommended, Mapping) and (
                    recommended.get("product_id") == candidate["product_id"]
                    and recommended.get("rule_revision") == candidate["rule_revision"]
                    and recommended.get("underlyings") == candidate["underlyings"]
                ):
                    inputs = recommended.get("current_inputs", {})
                    overrides = inputs.get("term_overrides", {}) if isinstance(inputs, Mapping) else {}
                    if isinstance(overrides, Mapping) and all(contract.get("terms", {}).get(key) == value for key, value in overrides.items()):
                        candidate["recommendation_bound"] = True
                        candidate["rank"] = recommended["rank"]
                        candidate["is_primary"] = recommended["rank"] == 1
                        projection = recommended.get("public_projection", {})
                        for field in ("reason", "suitable_for", "not_suitable_for", "main_risks", "key_terms"):
                            if field in projection:
                                candidate[field] = projection[field]
                candidates.append(candidate)
        if not candidates:
            return None
        verified = {
            key: source[key]
            for key in ("source_id", "label", "tenant_id", "task_id", "analysis_case_id")
            if key in source
        }
        verified["candidates"] = candidates
        verified["source_refs"] = {
            "module_run_refs": {
                candidate["candidate_id"]: candidate["module_run_options"]
                for candidate in candidates
            },
        }
        return verified


class ReporterAdapter:
    """The sole App entry for a Reporter page selection and ReportRun commit."""

    def __init__(self, results: ResultStore, tasks=None) -> None:
        self._results, self._tasks = results, tasks
        self._designer = _DesignerPort()

    def dispatch(self, request: dict[str, Any], principal: SessionIdentity) -> dict[str, Any]:
        ports = _ScopedResultPorts(self._results, principal, self._tasks)
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
                rerender_frozen_delivery(
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
    diagnostics = catalog.get("source_diagnostics")
    public = {"tenant_id": catalog["tenant_id"], "sources": sources}
    if isinstance(diagnostics, Mapping):
        failure_codes = diagnostics.get("failure_codes")
        if isinstance(failure_codes, Mapping):
            cleaned_codes = {
                str(code): count
                for code, count in failure_codes.items()
                if isinstance(code, str)
                and isinstance(count, int)
                and not isinstance(count, bool)
                and count > 0
            }
            public["source_diagnostics"] = {
                "excluded_run_count": sum(cleaned_codes.values()),
                "failure_codes": cleaned_codes,
            }
    return public


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
