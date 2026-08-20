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
from .artifact_validator import (
    is_public_module_artifact,
    resolve_artifact_path,
    sha256_file,
    validate_hashed_artifact,
    validate_report_run_directory,
    validate_written_artifact,
)
from .models import ReporterError, ReportRequest, SCHEMA_MANIFEST, read_json, require_identifier, stable_hash, write_json
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
        output_path.write_text(str(artifact["html"]), encoding="utf-8", newline="")
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
    if request.output_type == "quote" and filename == "report":
        filename = "quote"
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
            main_name = "quote" if request.output_type == "quote" else "index" if request.delivery_mode == "batch" else "report"
            rendered = _write_rendered(stage, summary_payload, request, f"{main_name}.{'pdf' if request.format == 'pdf' else 'html'}", designer_port=designer_port)
            designer_artifact_manifest = _persist_designer_manifest(stage, rendered, summary_payload)
            child_outputs = []
            if request.output_type != "quote" and request.delivery_mode in {"combined", "batch"}:
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
                    "quote_collection" if request.output_type == "quote"
                    else "collection_index_with_candidate_reports" if request.delivery_mode in {"combined", "batch"}
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


def _copy_declared_artifacts(
    source_root: Path,
    source_manifest: Mapping[str, Any],
    stage: Path,
    *,
    include_derived: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Copy only the public files already declared by a verified ReportRun.

    A format conversion must not resolve ModuleRuns again.  It may, however,
    need the same report-only SVG projection that the original Designer input
    references.  The source run manifest is the sole authority for those
    files, and every copied byte is re-checked against its saved hash.
    """

    copied: list[dict[str, Any]] = []
    for field in ("artifacts", "derived_artifacts"):
        if field == "derived_artifacts" and not include_derived:
            continue
        rows = source_manifest.get(field, [])
        if not isinstance(rows, list):
            raise ReporterError(f"源ReportRun的{field}必须为数组")
        for index, raw in enumerate(rows, start=1):
            if not isinstance(raw, Mapping):
                raise ReporterError(f"源ReportRun的{field}[{index}]不是对象")
            row = dict(raw)
            reference = row.get("path")
            content_hash = row.get("content_hash")
            source = validate_hashed_artifact(
                source_root,
                reference,
                content_hash,
                f"源ReportRun的{field}[{index}]",
            )
            destination = resolve_artifact_path(stage, reference, f"源ReportRun的{field}[{index}].path")
            if destination.exists() or destination.is_symlink():
                raise ReporterError(f"源ReportRun的{field}存在重复路径：{reference}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            validate_hashed_artifact(stage, reference, content_hash, f"复制后的{field}[{index}]")
            copied.append((field, row))
    return (
        [row for field, row in copied if field == "artifacts"],
        [row for field, row in copied if field == "derived_artifacts"],
    )


def _source_report_directory(
    output_root: Path,
    *,
    tenant_id: str,
    task_id: object,
    report_run_id: object,
) -> Path:
    task = require_identifier(task_id, "source.task_id")
    run = require_identifier(report_run_id, "source.report_run_id")
    root = output_root.resolve()
    directory = (root / task / run).resolve()
    if root not in directory.parents or not directory.is_dir() or directory.is_symlink():
        raise ReporterError("源ReportRun不存在")
    validation = validate_report_run_directory(directory)
    request = validation.get("request") if isinstance(validation, Mapping) else {}
    if not isinstance(request, Mapping) or request.get("tenant_id") != tenant_id:
        raise ReporterError("源ReportRun不属于当前租户")
    return directory


def _write_reissued_contents(
    stage: Path,
    *,
    source_validation: Mapping[str, Any],
    target_request: ReportRequest,
    source_request: ReportRequest,
    designer_port: ModulePort,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Write one root or child reissue directory from validated frozen files."""

    source_root = source_validation.get("root")
    unit = source_validation.get("unit")
    source_payload = source_validation.get("payload")
    source_manifest = source_validation.get("manifest")
    source_brief = source_validation.get("brief")
    if not isinstance(source_root, Path) or not all(
        isinstance(value, Mapping) for value in (unit, source_payload, source_manifest, source_brief)
    ):
        raise ReporterError("源ReportRun冻结内容无效")
    artifacts, derived_artifacts = _copy_declared_artifacts(
        source_root,
        source_manifest,
        stage,
        include_derived=target_request.output_type == "report",
    )
    report_unit_hashes = source_brief.get("report_unit_semantic_fact_hashes")
    if not isinstance(report_unit_hashes, list) or not all(isinstance(item, str) and item for item in report_unit_hashes):
        raise ReporterError("源ReportRun缺少冻结ReportUnit事实哈希")
    if target_request.output_type == source_request.output_type:
        payload = dict(source_payload)
    else:
        if source_request.output_type == "quote" or target_request.output_type == "quote":
            raise ReporterError("Quote只能由已选择的合同快照集合生成，不能转换为Card或Report")
        routes = {
            str(item["source_content_hash"]): str(item["path"])
            for item in derived_artifacts
            if isinstance(item, Mapping)
            and item.get("kind") == "payoffer_report_figure"
            and isinstance(item.get("source_content_hash"), str)
            and isinstance(item.get("path"), str)
        }
        payload = build_designer_payload(unit, target_request, artifact_paths=routes)
    brief = build_design_brief(payload, target_request, report_unit_hashes=list(report_unit_hashes))

    write_json(stage / "report-request.json", target_request.to_dict())
    write_json(stage / "report-unit.json", dict(unit))
    write_json(stage / "designer-input.json", dict(payload))
    write_json(stage / "design-brief.json", brief)
    if target_request.output_type == source_request.output_type:
        source_report_name = str((source_manifest.get("rendered") or {}).get("path") or "report.html")
        filename = f"{Path(source_report_name).stem}.{target_request.format}"
    else:
        filename = f"{'card' if target_request.output_type == 'card' else 'report'}.{target_request.format}"
    rendered = _write_rendered(stage, payload, target_request, filename, designer_port=designer_port)
    designer_artifact_manifest = _persist_designer_manifest(stage, rendered, payload)
    manifest = {
        "schema": SCHEMA_MANIFEST,
        "tenant_id": target_request.tenant_id,
        "task_id": target_request.task_id,
        "report_run_id": target_request.report_run_id,
        "analysis_case_id": target_request.analysis_case_id,
        "status": source_validation["delivery_status"],
        "delivery_realization": source_manifest.get("delivery_realization", "single_contract"),
        "request_hash": stable_hash(target_request.to_dict()),
        "source_refs": dict(target_request.source_refs),
        "report_unit": {
            "path": "report-unit.json",
            "semantic_fact_hash": unit.get("semantic_fact_hash"),
            "file_hash": sha256_file(stage / "report-unit.json"),
        },
        "designer_input": {"path": "designer-input.json", "hash": stable_hash(payload)},
        "design_brief": {"path": "design-brief.json", "hash": stable_hash(brief)},
        "designer_artifact_manifest": designer_artifact_manifest,
        "source_runs": list(source_manifest.get("source_runs", [])),
        "artifacts": artifacts,
        "derived_artifacts": derived_artifacts,
        "artifact_delivery_mode": "copied_from_saved_report_run",
        "rendered": rendered,
        "derived_from": {
            "task_id": source_request.task_id,
            "report_run_id": source_request.report_run_id,
            "designer_input_hash": stable_hash(payload),
        },
        "children": [],
        "generated_at": datetime.now(UTC).isoformat(),
    }
    return manifest, {
        "status": manifest["status"],
        "report_unit": "report-unit.json",
        "designer_input": "designer-input.json",
        "design_brief": "design-brief.json",
        "report": filename,
        "manifest": "run_manifest.json",
    }


def reissue_saved_report(
    *,
    output_root: Path,
    tenant_id: str,
    source: Mapping[str, Any],
    report_run_id: object,
    output_format: object,
    output_type: object | None = None,
    designer_port: ModulePort,
) -> dict[str, Any]:
    """Render a saved Contract ReportRun as Card, Report or another format.

    The original run remains immutable.  This creates a separate ReportRun
    from its verified frozen ``ReportUnit`` and declared public assets.  No
    ResultStore, model, pricing or backtest access is part of this path.
    """

    if not isinstance(source, Mapping) or set(source) != {"task_id", "report_run_id"}:
        raise ReporterError("source必须且只能包含task_id和report_run_id")
    target_run_id = require_identifier(report_run_id, "report_run_id")
    target_format = str(output_format or "").strip().lower()
    if target_format not in {"html", "pdf"}:
        raise ReporterError("format仅支持html或pdf")

    source_root = _source_report_directory(
        output_root,
        tenant_id=tenant_id,
        task_id=source.get("task_id"),
        report_run_id=source.get("report_run_id"),
    )
    source_validation = validate_report_run_directory(source_root)
    source_request = ReportRequest.from_mapping(source_validation["request"])
    if source_request.task_id != source.get("task_id"):
        raise ReporterError("源ReportRun任务标识不一致")
    if source_request.report_run_id == target_run_id:
        raise ReporterError("转换交付必须使用新的report_run_id，原HTML不会被覆盖")

    target_type = str(output_type or source_request.output_type).strip().lower()
    if target_type not in {"card", "report", "quote"}:
        raise ReporterError("output_type仅支持card、report或quote")
    if source_request.output_type == "quote" and target_type != "quote":
        raise ReporterError("请在报价结构清单中选定一条合同快照后生成简单报告或详细报告")
    if source_request.output_type != "quote" and target_type == "quote":
        raise ReporterError("请在报价结构清单中加入一个或多个已保存合同快照后生成参考报价")
    target_request_data = source_request.to_dict()
    target_request_data.update({"report_run_id": target_run_id, "format": target_format, "output_type": target_type})
    target_request = ReportRequest.from_mapping(target_request_data)
    target_root = output_root.resolve() / target_request.task_id / target_request.report_run_id
    if target_root.exists():
        raise ReporterError(f"ReportRun输出目录已存在，拒绝覆盖：{target_root}")
    target_root.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target_request.report_run_id}-", dir=target_root.parent))
    try:
        manifest, outcome = _write_reissued_contents(
            stage,
            source_validation=source_validation,
            target_request=target_request,
            source_request=source_request,
            designer_port=designer_port,
        )
        children: list[dict[str, Any]] = []
        for raw_child in source_validation.get("children", []):
            if not isinstance(raw_child, Mapping):
                raise ReporterError("源ReportRun子报告清单无效")
            candidate_id = require_identifier(raw_child.get("candidate_id"), "source.children.candidate_id")
            child_validation = raw_child.get("validation")
            if not isinstance(child_validation, Mapping):
                raise ReporterError("源ReportRun子报告验证无效")
            child_stage = stage / f"candidate_{candidate_id}"
            child_stage.mkdir()
            child_manifest, child_outcome = _write_reissued_contents(
                child_stage,
                source_validation=child_validation,
                target_request=target_request,
                source_request=source_request,
                designer_port=designer_port,
            )
            write_json(child_stage / "run_manifest.json", child_manifest)
            children.append({"candidate_id": candidate_id, "directory": child_stage.name, **child_outcome})
        manifest["children"] = children
        write_json(stage / "run_manifest.json", manifest)
        os.replace(stage, target_root)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return {
        "directory": str(target_root),
        **outcome,
        "children": children,
    }


def rerender_frozen_delivery(
    *,
    output_root: Path,
    source_request: Mapping[str, Any],
    report_unit: Mapping[str, Any],
    source_report_run_id: object,
    report_run_id: object,
    output_type: object,
    output_format: object,
    designer_port: ModulePort,
) -> dict[str, Any]:
    """Create a Card or Report from an App-saved frozen delivery fact set.

    Unlike format conversion from a local ReportRun directory, App persistence
    stores the immutable ReportUnit and verified audit record rather than a
    staging directory.  This path deliberately has no ResultStore argument:
    rendering a second delivery cannot trigger another payoff, pricing or
    backtest run.
    """

    source = ReportRequest.from_mapping(source_request)
    unit = dict(report_unit)
    if source.output_type == "quote" or unit.get("unit_type") != "ContractReportUnit":
        raise ReporterError("简单报告或详细报告使用单合同交付事实集；参考报价使用报价结构清单中的已选快照")
    target_type = str(output_type or "").strip().lower()
    if target_type not in {"card", "report"}:
        raise ReporterError("请选择Card或Report；Quote由报价结构清单的已选快照生成")
    target_format = str(output_format or "").strip().lower()
    if target_format not in {"html", "pdf"}:
        raise ReporterError("format仅支持html或pdf")
    source_run_id = require_identifier(source_report_run_id, "source_report_run_id")
    target_run_id = require_identifier(report_run_id, "report_run_id")
    if source_run_id == target_run_id:
        raise ReporterError("再次生成必须使用新的report_run_id，原交付不会被覆盖")
    target_data = source.to_dict()
    target_data.update({"report_run_id": target_run_id, "output_type": target_type, "format": target_format})
    target = ReportRequest.from_mapping(target_data)
    target_root = output_root.resolve() / target.task_id / target.report_run_id
    if target_root.exists():
        raise ReporterError("ReportRun输出目录已存在，拒绝覆盖")
    target_root.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.report_run_id}-", dir=target_root.parent))
    try:
        outcome = _write_unit_directory(
            stage,
            unit,
            target,
            {"candidate_evidence": {}},
            filename="card" if target.output_type == "card" else "report",
            designer_port=designer_port,
        )
        manifest_path = stage / "run_manifest.json"
        manifest = read_json(manifest_path, "run_manifest.json")
        manifest["derived_from"] = {
            "report_run_id": source_run_id,
            "semantic_fact_hash": unit.get("semantic_fact_hash"),
        }
        write_json(manifest_path, manifest)
        os.replace(stage, target_root)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return {"directory": str(target_root), **outcome}


__all__ = ["reissue_saved_report", "rerender_frozen_delivery", "write_report_run"]
