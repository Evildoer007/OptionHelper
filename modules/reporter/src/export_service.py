"""受控ReportRun目录写入与Designer产物落盘。"""

from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping

from runtime.ports.module import ModulePort

from .designer_handoff import build_collection_payload, build_design_brief, build_designer_payload, render_with_designer
from .artifact_validator import is_public_module_artifact, resolve_artifact_path, sha256_file, validate_hashed_artifact, validate_written_artifact
from .models import ReporterError, ReportRequest, SCHEMA_MANIFEST, stable_hash, write_json
from .payoff_report_figure import PROFILE as PAYOFF_REPORT_FIGURE_PROFILE, derive_report_payoff_svg


def _destination_name(candidate_id: str, module: str, name: str) -> Path:
    clean_name = Path(name).name
    if not clean_name or clean_name in {".", ".."}:
        raise ReporterError("ArtifactRef.name无效")
    return Path("artifacts") / candidate_id / module / clean_name


def _write_artifacts(
    stage: Path,
    evidence: Mapping[str, Any],
    request: ReportRequest,
    *,
    derive_payoff_figure: bool,
) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, Any]]]:
    """复制已验证源产物，并仅为Report生成安全的Payoffer派生图。

    ``artifacts``始终记录不变的ModuleRun源文件副本；``derived_artifacts``
    仅记录本ReportRun内的新文件。Card不会获得任何SVG路径，因此不会使用
    图形，即使同一Run携带了Payoffer源产物。
    """

    candidate_evidence = evidence.get("candidate_evidence")
    if not isinstance(candidate_evidence, Mapping):
        return {}, [], []
    routes: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    derived_rows: list[dict[str, Any]] = []
    for candidate_id, raw_candidate in candidate_evidence.items():
        if not isinstance(raw_candidate, Mapping):
            continue
        modules = raw_candidate.get("modules") if isinstance(raw_candidate.get("modules"), Mapping) else {}
        for module, raw_module in modules.items():
            if not isinstance(raw_module, Mapping) or raw_module.get("status") != "ready":
                continue
            run_dir = raw_module.get("_run_dir")
            manifest = raw_module.get("artifact_manifest") if isinstance(raw_module.get("artifact_manifest"), Mapping) else {}
            artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), list) else []
            if not isinstance(run_dir, Path):
                raise ReporterError("已验证ModuleRun缺少ResultStore临时解析目录")
            for artifact in artifacts:
                if not isinstance(artifact, Mapping):
                    continue
                storage_ref = artifact.get("storage_ref")
                content_hash = artifact.get("content_hash")
                name = artifact.get("name")
                if not all(isinstance(item, str) and item for item in (storage_ref, content_hash, name)):
                    raise ReporterError("ArtifactRef不完整")
                source = validate_hashed_artifact(run_dir, storage_ref, content_hash, f"ArtifactRef源文件：{storage_ref}")
                # 所有ModuleRun文件都会被Core哈希验真，但只有result.json和
                # artifacts/**是公开交付物；快照、数据引用与private/**绝不复制。
                if not is_public_module_artifact(storage_ref, module=str(module)):
                    continue
                relative = _destination_name(str(candidate_id), str(module), name)
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise ReporterError(f"报告产物中ArtifactRef重名：{relative}")
                shutil.copyfile(source, destination)
                validate_hashed_artifact(stage, relative.as_posix(), content_hash, f"复制后的ArtifactRef：{relative}")
                rows.append({
                    "candidate_id": candidate_id, "module": module, "name": name, "path": relative.as_posix(),
                    "media_type": artifact.get("media_type"), "content_hash": content_hash,
                })
                if module != "payoff" or artifact.get("media_type") != "image/svg+xml":
                    continue
                if not derive_payoff_figure:
                    # Card只交付文字情景；不向Designer暴露可嵌入的SVG路径。
                    continue
                report_name = f"report-payoff-{content_hash[:16]}.svg"
                report_relative = relative.parent / report_name
                report_path = stage / report_relative
                if report_path.exists() or report_path.is_symlink():
                    raise ReporterError(f"报告Payoffer派生图路径冲突：{report_relative}")
                derived = derive_report_payoff_svg(destination.read_bytes(), expected_source_hash=content_hash)
                report_path.write_bytes(derived)
                report_hash = sha256_file(report_path)
                routes.setdefault(content_hash, report_relative.as_posix())
                derived_rows.append({
                    "candidate_id": candidate_id,
                    "module": module,
                    "kind": "payoffer_report_figure",
                    "profile": PAYOFF_REPORT_FIGURE_PROFILE,
                    "source_path": relative.as_posix(),
                    "source_content_hash": content_hash,
                    "path": report_relative.as_posix(),
                    "media_type": "image/svg+xml",
                    "content_hash": report_hash,
                })
    return routes, rows, derived_rows


def _source_records(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    units = document.get("report_units") if isinstance(document.get("report_units"), list) else [document]
    records: list[dict[str, Any]] = []
    for unit in units:
        if not isinstance(unit, Mapping):
            continue
        subject = unit.get("subject") if isinstance(unit.get("subject"), Mapping) else {}
        modules = unit.get("modules") if isinstance(unit.get("modules"), Mapping) else {}
        for name, raw in modules.items():
            if not isinstance(raw, Mapping):
                continue
            ref = raw.get("run_ref") if isinstance(raw.get("run_ref"), Mapping) else None
            if ref:
                records.append({"candidate_id": subject.get("candidate_id"), "module": name, "status": raw.get("status"), "module_run_ref": ref, "run": raw.get("run", {})})
    return records


def _write_rendered(stage: Path, payload: Mapping[str, Any], request: ReportRequest, filename: str, *, designer_port: ModulePort) -> dict[str, Any]:
    artifact = render_with_designer(payload, request, stage, designer_port=designer_port)
    validated_assets = artifact.pop("_validated_portable_assets", [])
    if not isinstance(validated_assets, list):
        raise ReporterError("已验证Designer产物的portable_assets无效")
    portable_assets: list[dict[str, str]] = []
    for index, asset in enumerate(validated_assets, start=1):
        if not isinstance(asset, Mapping) or not isinstance(asset.get("content"), bytes):
            raise ReporterError(f"portable_assets[{index}]缺少已验证字节")
        path = resolve_artifact_path(stage, asset.get("path"), f"portable_assets[{index}].path")
        if path == stage / filename or path.exists() or path.is_symlink():
            raise ReporterError(f"portable_assets[{index}].path与已有产物冲突")
        path.parent.mkdir(parents=True, exist_ok=True)
        if any(parent.is_symlink() for parent in path.parents if parent != stage.parent):
            raise ReporterError(f"portable_assets[{index}].path包含符号链接目录")
        path.write_bytes(asset["content"])
        validate_hashed_artifact(stage, asset.get("path"), asset.get("content_hash"), f"portable_assets[{index}].path")
        portable_assets.append({
            "path": str(asset["path"]),
            "media_type": str(asset["media_type"]),
            "content_hash": str(asset["content_hash"]),
        })
    output_path = stage / filename
    if request.format == "pdf":
        output_path.write_bytes(artifact["pdf"])
        delivery_mode = "self_contained_pdf"
    else:
        output_path.write_text(str(artifact["html"]), encoding="utf-8")
        delivery_mode = "portable_html"
    designer_manifest = artifact.get("artifact_manifest") if isinstance(artifact.get("artifact_manifest"), Mapping) else {}
    content_hash = validate_written_artifact(
        output_path,
        output_format=request.format,
        expected_hash=designer_manifest.get("artifact_hash"),
        label="Designer报告文件",
    )
    return {
        "path": filename,
        "content_hash": content_hash,
        "format": request.format,
        "delivery_mode": delivery_mode,
        "portable_assets": portable_assets,
        "designer": {
            "design_system_id": artifact.get("design_system_id"),
            "design_system_hash": artifact.get("design_system_hash"),
            "output_type": artifact.get("output_type"),
            "asset_mode": artifact.get("asset_mode"),
            "source_artifact_hash": designer_manifest.get("artifact_hash"),
            "artifact_manifest": dict(designer_manifest),
        },
    }


def _persist_designer_manifest(stage: Path, rendered: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, str]:
    """持久化Designer回执，并显式绑定本次冻结输入而非临时工作目录。"""

    designer = rendered.get("designer") if isinstance(rendered.get("designer"), Mapping) else {}
    artifact_manifest = designer.get("artifact_manifest") if isinstance(designer.get("artifact_manifest"), Mapping) else None
    if artifact_manifest is None:
        raise ReporterError("已验证Designer产物缺少artifact_manifest")
    path = stage / "designer-artifact-manifest.json"
    write_json(path, dict(artifact_manifest))
    return {
        "path": path.name,
        "file_hash": sha256_file(path),
        "frozen_payload_hash": stable_hash(payload),
    }


def _write_unit_directory(
    stage: Path,
    unit: Mapping[str, Any],
    request: ReportRequest,
    evidence: Mapping[str, Any],
    *,
    filename: str = "report",
    designer_port: ModulePort,
) -> dict[str, Any]:
    routes, artifacts, derived_artifacts = _write_artifacts(
        stage,
        evidence,
        request,
        derive_payoff_figure=request.output_type == "report",
    )
    payload = build_designer_payload(unit, request, artifact_paths=routes)
    brief = build_design_brief(payload, request, report_unit_hashes=[str(unit.get("semantic_fact_hash", ""))])
    write_json(stage / "report-request.json", request.to_dict())
    write_json(stage / "report-unit.json", dict(unit))
    write_json(stage / "designer-input.json", payload)
    write_json(stage / "design-brief.json", brief)
    report_file = f"{filename}.{'pdf' if request.format == 'pdf' else 'html'}"
    rendered = _write_rendered(stage, payload, request, report_file, designer_port=designer_port)
    designer_artifact_manifest = _persist_designer_manifest(stage, rendered, payload)
    manifest = {
        "schema": SCHEMA_MANIFEST,
        "tenant_id": request.tenant_id,
        "task_id": request.task_id,
        "report_run_id": request.report_run_id,
        "analysis_case_id": request.analysis_case_id,
        "status": "succeeded" if unit.get("evidence_status") == "verified" else "partial",
        "request_hash": stable_hash(request.to_dict()),
        "source_refs": dict(request.source_refs),
        "report_unit": {"path": "report-unit.json", "semantic_fact_hash": unit.get("semantic_fact_hash"), "file_hash": sha256_file(stage / "report-unit.json")},
        "designer_input": {"path": "designer-input.json", "hash": stable_hash(payload)},
        "design_brief": {"path": "design-brief.json", "hash": stable_hash(brief)},
        "designer_artifact_manifest": designer_artifact_manifest,
        "source_runs": _source_records(unit),
        "artifacts": artifacts,
        "derived_artifacts": derived_artifacts,
        "artifact_delivery_mode": "copied_into_report_run",
        "rendered": rendered,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    write_json(stage / "run_manifest.json", manifest)
    return {
        "status": manifest["status"],
        "report_unit": "report-unit.json",
        "designer_input": "designer-input.json",
        "design_brief": "design-brief.json",
        "report": report_file,
        "manifest": "run_manifest.json",
    }


def _candidate_evidence(evidence: Mapping[str, Any], candidate_id: str) -> dict[str, Any]:
    all_evidence = evidence.get("candidate_evidence") if isinstance(evidence.get("candidate_evidence"), Mapping) else {}
    if candidate_id not in all_evidence:
        raise ReporterError(f"候选{candidate_id}缺少已解析证据")
    return {
        "candidate_evidence": {candidate_id: all_evidence[candidate_id]},
        "recommender": evidence.get("recommender", {}),
    }


def write_report_run(
    *,
    output_root: Path,
    request: ReportRequest,
    document: Mapping[str, Any],
    units: list[Mapping[str, Any]],
    evidence: Mapping[str, Any],
    designer_port: ModulePort,
) -> dict[str, Any]:
    """原子写入一个ReportRun及其真实Designer交付物。"""

    root = output_root.resolve() / request.task_id / request.report_run_id
    if root.exists():
        raise ReporterError(f"ReportRun输出目录已存在，拒绝覆盖：{root}")
    root.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{request.report_run_id}-", dir=root.parent))
    try:
        write_json(stage / "report-request.json", request.to_dict())
        if request.delivery_mode == "single":
            outcome = _write_unit_directory(stage, units[0], request, evidence, designer_port=designer_port)
        else:
            write_json(stage / "report-unit.json", dict(document))
            routes, artifacts, derived_artifacts = _write_artifacts(
                stage,
                evidence,
                request,
                derive_payoff_figure=False,
            )
            summary_payload = build_collection_payload(units, request)
            brief = build_design_brief(summary_payload, request, report_unit_hashes=[str(item.get("semantic_fact_hash", "")) for item in units])
            write_json(stage / "designer-input.json", summary_payload)
            write_json(stage / "design-brief.json", brief)
            main_name = "index" if request.delivery_mode == "batch" else "report"
            rendered = _write_rendered(stage, summary_payload, request, f"{main_name}.{'pdf' if request.format == 'pdf' else 'html'}", designer_port=designer_port)
            designer_artifact_manifest = _persist_designer_manifest(stage, rendered, summary_payload)
            child_outputs = []
            if request.delivery_mode in {"combined", "batch"}:
                for unit in units:
                    candidate_id = str((unit.get("subject") or {}).get("candidate_id"))
                    child_stage = stage / f"candidate_{candidate_id}"
                    child_stage.mkdir()
                    child_outputs.append({
                        "candidate_id": candidate_id,
                        "directory": f"candidate_{candidate_id}",
                        **_write_unit_directory(child_stage, unit, request, _candidate_evidence(evidence, candidate_id), designer_port=designer_port),
                    })
            manifest = {
                "schema": SCHEMA_MANIFEST,
                "tenant_id": request.tenant_id,
                "task_id": request.task_id,
                "report_run_id": request.report_run_id,
                "analysis_case_id": request.analysis_case_id,
                "status": "succeeded" if all(unit.get("evidence_status") == "verified" for unit in units) else "partial",
                "delivery_realization": (
                    "collection_index_with_candidate_reports" if request.delivery_mode in {"combined", "batch"}
                    else "comparison_summary" if request.delivery_mode == "comparison" else "single_contract"
                ),
                "request_hash": stable_hash(request.to_dict()),
                "source_refs": dict(request.source_refs),
                "report_unit": {"path": "report-unit.json", "semantic_fact_hash": document.get("semantic_fact_hash"), "file_hash": sha256_file(stage / "report-unit.json")},
                "designer_input": {"path": "designer-input.json", "hash": stable_hash(summary_payload)},
                "design_brief": {"path": "design-brief.json", "hash": stable_hash(brief)},
                "designer_artifact_manifest": designer_artifact_manifest,
                "source_runs": _source_records(document),
                "artifacts": artifacts,
                "derived_artifacts": derived_artifacts,
                "artifact_delivery_mode": "copied_into_report_run",
                "rendered": rendered,
                "children": child_outputs,
                "generated_at": datetime.now(UTC).isoformat(),
            }
            write_json(stage / "run_manifest.json", manifest)
            outcome = {
                "directory": str(stage),
                "status": manifest["status"],
                "report_unit": "report-unit.json",
                "designer_input": "designer-input.json",
                "design_brief": "design-brief.json",
                "report": rendered["path"],
                "manifest": "run_manifest.json",
                "children": child_outputs,
            }
        os.replace(stage, root)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    outcome["directory"] = str(root)
    return outcome


__all__ = ["write_report_run"]
