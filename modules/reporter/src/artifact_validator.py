"""Reporter交付物的唯一格式、路径与哈希校验。"""

from __future__ import annotations

import base64
import binascii
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .config import DEFAULT_REPORTER_CONFIG
from .models import (
    SCHEMA_DESIGN_BRIEF,
    SCHEMA_DESIGNER_ARTIFACT_MANIFEST,
    SCHEMA_DESIGNER_PAYLOAD,
    SCHEMA_MANIFEST,
    SCHEMA_REPORT_BUNDLE,
    SCHEMA_REPORT_UNIT,
    SCHEMA_REQUEST,
    ReporterError,
    read_json,
    require_text,
    stable_hash,
)
from .payoff_report_figure import PROFILE as PAYOFF_REPORT_FIGURE_PROFILE, validate_report_payoff_svg


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_artifact_path(root: Path, reference: object, label: str) -> Path:
    """将清单中的相对路径限制在一个受控目录内。"""

    relative = PurePosixPath(require_text(reference, label))
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ReporterError(f"{label}不是受控相对路径")
    base = root.resolve()
    result = (base / Path(*relative.parts)).resolve(strict=False)
    if result != base and base not in result.parents:
        raise ReporterError(f"{label}越出受控目录")
    return result


_BACKTEST_PUBLIC_ARTIFACTS = frozenset({
    "result.json",
    "artifacts/backtest_result.json",
    "artifacts/trade_ledger.csv",
    "artifacts/branch_coverage.json",
})


def is_public_module_artifact(reference: object, *, module: str | None = None) -> bool:
    """Whether a verified ModuleRun file may be copied into a public ReportRun.

    Core hashes every submitted file, including input snapshots and private
    audit material.  Hash verification does not grant publication rights:
    only the canonical public result and files explicitly placed below
    ``artifacts/`` may cross this boundary.
    """

    try:
        relative = PurePosixPath(require_text(reference, "ArtifactRef.storage_ref"))
    except ReporterError:
        return False
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        return False
    normalized = relative.as_posix()
    if module == "backtest":
        return normalized in _BACKTEST_PUBLIC_ARTIFACTS
    return relative == PurePosixPath("result.json") or relative.parts[:1] == ("artifacts",)


def validate_hashed_artifact(root: Path, reference: object, expected_hash: object, label: str) -> Path:
    """验证一个已存在的受控产物，并返回其绝对路径。"""

    path = resolve_artifact_path(root, reference, label)
    if not path.is_file() or path.is_symlink():
        raise ReporterError(f"{label}不存在或不是普通文件")
    actual_hash = sha256_file(path)
    if actual_hash != require_text(expected_hash, f"{label}.content_hash"):
        raise ReporterError(f"{label}内容哈希不一致")
    return path


def validate_artifact_bytes(
    payload: bytes,
    *,
    output_format: str,
    expected_hash: object | None = None,
    label: str,
) -> str:
    """验证Designer或已写入文件的格式、内容哈希及PDF头。"""

    if output_format not in {"html", "pdf"}:
        raise ReporterError(f"{label}格式不支持：{output_format}")
    if not payload:
        raise ReporterError(f"{label}不能为空")
    if output_format == "pdf" and DEFAULT_REPORTER_CONFIG.require_pdf_header and not payload.startswith(b"%PDF"):
        raise ReporterError(f"{label}PDF文件头无效")
    content_hash = sha256(payload).hexdigest()
    if expected_hash is not None and content_hash != require_text(expected_hash, f"{label}.content_hash"):
        raise ReporterError(f"{label}内容哈希不一致")
    return content_hash


def validate_written_artifact(
    path: Path,
    *,
    output_format: str,
    expected_hash: object | None = None,
    label: str,
) -> str:
    if not path.is_file() or path.is_symlink():
        raise ReporterError(f"{label}不存在或不是普通文件")
    return validate_artifact_bytes(path.read_bytes(), output_format=output_format, expected_hash=expected_hash, label=label)


def validate_designer_artifact(
    artifact: Mapping[str, Any],
    *,
    output_format: str,
    output_type: str,
    expected_frozen_payload_hash: str,
    expected_asset_mode: str,
) -> list[dict[str, Any]]:
    """按公开Designer Tool契约验证返回产物，拒绝伪报成功。"""

    if artifact.get("format") != output_format:
        raise ReporterError("Designer Tool产物format与ReportRequest不一致")
    if artifact.get("output_type") != output_type:
        raise ReporterError("Designer Tool产物output_type与ReportRequest不一致")
    if "layout" in artifact:
        raise ReporterError("Designer Tool产物不接受版式字段")
    if artifact.get("asset_mode") != expected_asset_mode:
        raise ReporterError("Designer Tool产物asset_mode与请求不一致")
    manifest = artifact.get("artifact_manifest")
    if not isinstance(manifest, Mapping):
        raise ReporterError("Designer Tool产物缺少artifact_manifest")
    if manifest.get("schema") != SCHEMA_DESIGNER_ARTIFACT_MANIFEST:
        raise ReporterError("Designer artifact_manifest.schema不是当前协议")
    if "layout" in manifest:
        raise ReporterError("Designer artifact_manifest不接受版式字段")
    for field, expected in (("format", output_format), ("output_type", output_type)):
        if manifest.get(field) != expected:
            raise ReporterError(f"Designer artifact_manifest.{field}与请求不一致")
    for field in ("design_system_id", "design_system_hash", "semantic_fact_hash", "presentation_input_hash", "artifact_hash"):
        if not isinstance(artifact.get(field), str) or not artifact[field]:
            raise ReporterError(f"Designer Tool产物缺少{field}")
        if artifact.get(field) != manifest.get(field):
            raise ReporterError(f"Designer artifact_manifest.{field}与产物不一致")
    if manifest.get("content_sha256") != expected_frozen_payload_hash:
        raise ReporterError("Designer artifact_manifest未绑定冻结designer-input事实哈希")
    payload = artifact.get("pdf") if output_format == "pdf" else artifact.get("html")
    if not isinstance(payload, bytes if output_format == "pdf" else str):
        raise ReporterError("Designer Tool产物内容类型不正确")
    raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    content_hash = validate_artifact_bytes(raw, output_format=output_format, expected_hash=manifest.get("artifact_hash"), label="Designer Tool产物")
    hash_field = "pdf_sha256" if output_format == "pdf" else "html_sha256"
    if manifest.get(hash_field) != content_hash:
        raise ReporterError("Designer artifact_manifest内容哈希与产物不一致")
    return _validate_portable_assets(
        artifact,
        manifest,
        output_format=output_format,
        expected_asset_mode=expected_asset_mode,
    )


def _validate_portable_assets(
    artifact: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    output_format: str,
    expected_asset_mode: str,
) -> list[dict[str, Any]]:
    """验证Designer返回的portable资源包并解码为待落盘字节。"""

    raw_assets = artifact.get("portable_assets", [])
    if expected_asset_mode == "portable" and output_format == "html" and not isinstance(artifact.get("portable_assets"), list):
        raise ReporterError("Designer Tool未返回portable_assets清单")
    if not isinstance(raw_assets, list):
        raise ReporterError("Designer Tool的portable_assets必须为数组")
    if output_format != "html" and raw_assets:
        raise ReporterError("非HTML Designer产物不得携带portable_assets")

    decoded: list[dict[str, Any]] = []
    summary: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_assets, start=1):
        if not isinstance(raw, Mapping) or set(raw) != {"path", "media_type", "sha256", "content_base64"}:
            raise ReporterError(f"portable_assets[{index}]字段不完整或含未知字段")
        path_text = require_text(raw.get("path"), f"portable_assets[{index}].path")
        relative = PurePosixPath(path_text)
        if "\\" in path_text or relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ReporterError(f"portable_assets[{index}].path不是受控相对路径")
        if relative.as_posix() in seen:
            raise ReporterError("portable_assets含重复路径")
        seen.add(relative.as_posix())
        media_type = require_text(raw.get("media_type"), f"portable_assets[{index}].media_type")
        expected_hash = require_text(raw.get("sha256"), f"portable_assets[{index}].sha256")
        try:
            content = base64.b64decode(require_text(raw.get("content_base64"), f"portable_assets[{index}].content_base64"), validate=True)
        except (ValueError, TypeError, binascii.Error) as error:
            raise ReporterError(f"portable_assets[{index}]不是有效Base64") from error
        if not content or sha256(content).hexdigest() != expected_hash:
            raise ReporterError(f"portable_assets[{index}]内容哈希不一致")
        row = {"path": relative.as_posix(), "media_type": media_type, "sha256": expected_hash}
        summary.append(row)
        decoded.append({**row, "content_hash": expected_hash, "content": content})

    manifest_portable = manifest.get("portable_assets", [])
    if manifest_portable != summary:
        raise ReporterError("Designer artifact_manifest.portable_assets与资源包不一致")
    manifest_assets = manifest.get("assets", [])
    if not isinstance(manifest_assets, list):
        raise ReporterError("Designer artifact_manifest.assets必须为数组")
    asset_summary = [
        {key: item.get(key) for key in ("path", "media_type", "sha256")}
        for item in manifest_assets if isinstance(item, Mapping)
    ]
    if len(asset_summary) != len(manifest_assets) or asset_summary != summary:
        raise ReporterError("Designer artifact_manifest.assets与portable资源包不一致")
    return decoded


def _validate_report_unit(unit: Mapping[str, Any]) -> None:
    if unit.get("schema") != SCHEMA_REPORT_UNIT:
        raise ReporterError("report-unit.json的Schema不是当前协议")
    if unit.get("unit_type") != "ContractReportUnit":
        raise ReporterError("report-unit.json的unit_type不是当前正式类型")
    expected = stable_hash({key: value for key, value in unit.items() if key != "semantic_fact_hash"})
    if unit.get("semantic_fact_hash") != expected:
        raise ReporterError("report-unit.json的semantic_fact_hash不对应冻结事实")


def _validate_report_bundle(bundle: Mapping[str, Any]) -> None:
    if bundle.get("schema") != SCHEMA_REPORT_BUNDLE:
        raise ReporterError("ReportBundle的Schema不是当前协议")
    if bundle.get("unit_type") != "ReportBundle":
        raise ReporterError("ReportBundle的unit_type不是当前正式类型")
    expected = stable_hash({key: value for key, value in bundle.items() if key != "semantic_fact_hash"})
    if bundle.get("semantic_fact_hash") != expected:
        raise ReporterError("ReportBundle的semantic_fact_hash不对应冻结事实")


def _frozen_evidence_status(unit: Mapping[str, Any]) -> str:
    """Return the only status an individual frozen ContractReportUnit permits."""

    status = unit.get("evidence_status")
    if status not in {"verified", "partial"}:
        raise ReporterError("冻结ReportUnit.evidence_status无效")
    return str(status)


def _validated_json(root: Path, reference: object, label: str) -> tuple[Path, dict[str, Any]]:
    path = resolve_artifact_path(root, reference, label)
    if not path.is_file() or path.is_symlink():
        raise ReporterError(f"{label}不存在或不是普通文件")
    value = read_json(path, label)
    return path, value


def _validate_derived_payoff_figures(
    root: Path,
    manifest: Mapping[str, Any],
    request: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> None:
    """Verify the Report-only SVG projection and its source-hash binding."""

    artifacts = manifest.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise ReporterError("run_manifest.json的artifacts必须为数组")
    source_rows: set[tuple[str, str, str, str]] = set()
    source_paths: set[str] = set()
    for index, raw in enumerate(artifacts, start=1):
        if not isinstance(raw, Mapping):
            raise ReporterError(f"artifacts[{index}]不是对象")
        path = require_text(raw.get("path"), f"artifacts[{index}].path")
        content_hash = require_text(raw.get("content_hash"), f"artifacts[{index}].content_hash")
        validate_hashed_artifact(root, path, content_hash, f"artifacts[{index}].path")
        source_paths.add(path)
        if raw.get("module") == "payoff" and raw.get("media_type") == "image/svg+xml":
            source_rows.add((
                require_text(raw.get("candidate_id"), f"artifacts[{index}].candidate_id"),
                "payoff", path, content_hash,
            ))

    derived = manifest.get("derived_artifacts", [])
    if not isinstance(derived, list):
        raise ReporterError("run_manifest.json的derived_artifacts必须为数组")
    is_report = request.get("output_type") == "report"
    if not is_report and derived:
        raise ReporterError("Card不得声明Payoffer报告派生图")
    derived_paths: set[str] = set()
    for index, raw in enumerate(derived, start=1):
        if not isinstance(raw, Mapping):
            raise ReporterError(f"derived_artifacts[{index}]不是对象")
        expected_keys = {
            "candidate_id", "module", "kind", "profile", "source_path", "source_content_hash",
            "path", "media_type", "content_hash",
        }
        if set(raw) != expected_keys:
            raise ReporterError(f"derived_artifacts[{index}]字段不完整或含未知字段")
        candidate_id = require_text(raw.get("candidate_id"), f"derived_artifacts[{index}].candidate_id")
        source_path = require_text(raw.get("source_path"), f"derived_artifacts[{index}].source_path")
        source_hash = require_text(raw.get("source_content_hash"), f"derived_artifacts[{index}].source_content_hash")
        source_key = (candidate_id, "payoff", source_path, source_hash)
        if raw.get("module") != "payoff" or raw.get("kind") != "payoffer_report_figure":
            raise ReporterError("derived_artifacts只能声明Payoffer报告派生图")
        if raw.get("profile") != PAYOFF_REPORT_FIGURE_PROFILE or raw.get("media_type") != "image/svg+xml":
            raise ReporterError("derived_artifacts的报告图配置不一致")
        if source_key not in source_rows:
            raise ReporterError("derived_artifacts未绑定已声明的Payoffer源ArtifactRef")
        path = require_text(raw.get("path"), f"derived_artifacts[{index}].path")
        if path in source_paths or path in derived_paths:
            raise ReporterError("derived_artifacts路径与源产物或其他派生图冲突")
        derived_paths.add(path)
        derived_path = validate_hashed_artifact(root, path, raw.get("content_hash"), f"derived_artifacts[{index}].path")
        validate_report_payoff_svg(derived_path.read_bytes(), expected_source_hash=source_hash)
    payoff = payload.get("payoff") if isinstance(payload.get("payoff"), Mapping) else {}
    report_svg_path = payoff.get("report_svg_path") if isinstance(payoff, Mapping) else None
    comparison = payload.get("comparison") if isinstance(payload.get("comparison"), Mapping) else {}
    comparison_paths = {
        str(path)
        for candidate in comparison.get("candidates", [])
        if isinstance(candidate, Mapping)
        for facts in [candidate.get("facts")]
        if isinstance(facts, Mapping)
        for module in [facts.get("payoff")]
        if isinstance(module, Mapping)
        for path in [module.get("report_svg_path")]
        if isinstance(path, str) and path
    }
    declared_payload_paths = ({str(report_svg_path)} if report_svg_path else set()) | comparison_paths
    if not is_report and declared_payload_paths:
        raise ReporterError("Card的Designer输入不得包含Payoffer SVG路径")
    if not declared_payload_paths:
        if derived_paths:
            raise ReporterError("未嵌入报告图时不得声明Payoffer派生产物")
        return
    if not is_report or declared_payload_paths != derived_paths:
        raise ReporterError("Designer输入的Payoffer SVG必须完整指向本ReportRun派生图")


def validate_report_run_directory(
    output_dir: Path,
    *,
    expected_request: Mapping[str, Any] | None = None,
    child_candidate_id: str | None = None,
) -> dict[str, Any]:
    """完整验证一个Reporter输出目录及其候选子报告。

    这是脚本校验和App正式提交前复核共用的唯一实现。它不重算金融结果，
    只验证冻结请求、事实JSON、Designer回执、渲染文件和候选集合的完整性。
    """

    root = output_dir.resolve()
    request_path, request = _validated_json(root, "report-request.json", "report-request.json")
    unit_path, unit = _validated_json(root, "report-unit.json", "report-unit.json")
    payload_path, payload = _validated_json(root, "designer-input.json", "designer-input.json")
    brief_path, brief = _validated_json(root, "design-brief.json", "design-brief.json")
    _, manifest = _validated_json(root, "run_manifest.json", "run_manifest.json")

    if request.get("schema") != SCHEMA_REQUEST:
        raise ReporterError("report-request.json的Schema不是当前协议")
    if manifest.get("schema") != SCHEMA_MANIFEST:
        raise ReporterError("run_manifest.json的Schema不是当前协议")
    if payload.get("schema") != SCHEMA_DESIGNER_PAYLOAD:
        raise ReporterError("designer-input.json的Schema不是当前协议")
    if brief.get("schema") != SCHEMA_DESIGN_BRIEF:
        raise ReporterError("design-brief.json的Schema不是当前协议")

    if expected_request is not None and request != dict(expected_request):
        raise ReporterError("report-request.json与受控selection重建结果不一致")
    if request.get("subject_type") not in {"contract", "bundle", "comparison"}:
        raise ReporterError("report-request.json的subject_type不是当前正式类型")
    for field in ("tenant_id", "task_id", "report_run_id", "analysis_case_id"):
        if manifest.get(field) != request.get(field):
            raise ReporterError(f"run_manifest.json的{field}与report-request.json不一致")
    if manifest.get("request_hash") != stable_hash(request):
        raise ReporterError("run_manifest.json的request_hash不一致")

    if unit.get("unit_type") == "ReportBundle":
        _validate_report_bundle(unit)
    else:
        _validate_report_unit(unit)
    report_unit_ref = manifest.get("report_unit")
    if not isinstance(report_unit_ref, Mapping) or report_unit_ref.get("path") != unit_path.name:
        raise ReporterError("run_manifest.json未声明report-unit.json")
    if report_unit_ref.get("file_hash") != sha256_file(unit_path):
        raise ReporterError("run_manifest.json的report_unit文件哈希不一致")
    if report_unit_ref.get("semantic_fact_hash") != unit.get("semantic_fact_hash"):
        raise ReporterError("run_manifest.json的report_unit事实哈希不一致")

    payload_hash = stable_hash(payload)
    designer_input_ref = manifest.get("designer_input")
    if not isinstance(designer_input_ref, Mapping) or designer_input_ref.get("path") != payload_path.name or designer_input_ref.get("hash") != payload_hash:
        raise ReporterError("run_manifest.json的designer_input哈希不一致")
    brief_hash = stable_hash(brief)
    brief_ref = manifest.get("design_brief")
    if not isinstance(brief_ref, Mapping) or brief_ref.get("path") != brief_path.name or brief_ref.get("hash") != brief_hash:
        raise ReporterError("run_manifest.json的design_brief哈希不一致")
    if brief.get("frozen_payload_hash") != payload_hash:
        raise ReporterError("design-brief.json不对应designer-input.json")

    rendered = manifest.get("rendered")
    receipt_ref = manifest.get("designer_artifact_manifest")
    if not isinstance(rendered, Mapping) or not isinstance(receipt_ref, Mapping):
        raise ReporterError("run_manifest.json缺少Designer渲染清单")
    receipt_path = validate_hashed_artifact(
        root, receipt_ref.get("path"), receipt_ref.get("file_hash"), "designer_artifact_manifest.path",
    )
    receipt = read_json(receipt_path, "designer-artifact-manifest.json")
    if receipt.get("schema") != SCHEMA_DESIGNER_ARTIFACT_MANIFEST:
        raise ReporterError("designer-artifact-manifest.json的Schema不是当前协议")
    if receipt_ref.get("frozen_payload_hash") != payload_hash or receipt.get("content_sha256") != payload_hash:
        raise ReporterError("Designer回执未绑定冻结designer-input事实哈希")
    designer = rendered.get("designer")
    if not isinstance(designer, Mapping) or designer.get("artifact_manifest") != receipt:
        raise ReporterError("run_manifest.json中的Designer回执与持久化清单不一致")
    if rendered.get("format") != request.get("format") or receipt.get("format") != request.get("format"):
        raise ReporterError("Designer回执的format与report-request.json不一致")
    if "layout" in receipt or "layout" in designer:
        raise ReporterError("Designer回执不接受版式字段")
    for field, expected in (("output_type", request.get("output_type")),):
        if receipt.get(field) != expected or designer.get(field) != expected:
            raise ReporterError(f"Designer回执的{field}与report-request.json不一致")
    rendered_path = validate_hashed_artifact(root, rendered.get("path"), rendered.get("content_hash"), "rendered.path")
    validate_written_artifact(
        rendered_path,
        output_format=str(rendered.get("format", "")),
        expected_hash=rendered.get("content_hash"),
        label="渲染产物",
    )
    if receipt.get("artifact_hash") != designer.get("source_artifact_hash"):
        raise ReporterError("Designer回执的源产物哈希与run_manifest.json不一致")
    if designer.get("asset_mode") != "portable":
        raise ReporterError("run_manifest.json未记录portable Designer交付模式")
    portable_assets = rendered.get("portable_assets", [])
    if not isinstance(portable_assets, list):
        raise ReporterError("run_manifest.json的portable_assets必须为数组")
    portable_summary: list[dict[str, str]] = []
    for index, raw in enumerate(portable_assets, start=1):
        if not isinstance(raw, Mapping):
            raise ReporterError(f"portable_assets[{index}]不是对象")
        path = validate_hashed_artifact(root, raw.get("path"), raw.get("content_hash"), f"portable_assets[{index}].path")
        media_type = require_text(raw.get("media_type"), f"portable_assets[{index}].media_type")
        portable_summary.append({"path": path.relative_to(root).as_posix(), "media_type": media_type, "sha256": str(raw.get("content_hash"))})
    if receipt.get("portable_assets", []) != portable_summary:
        raise ReporterError("持久化portable_assets与Designer回执不一致")

    _validate_derived_payoff_figures(root, manifest, request, payload)

    subject_ref = request.get("subject_ref")
    if not isinstance(subject_ref, Mapping):
        raise ReporterError("report-request.json缺少subject_ref")
    candidate_ids = subject_ref.get("candidate_ids")
    if not isinstance(candidate_ids, list) or any(not isinstance(item, str) or not item for item in candidate_ids):
        raise ReporterError("report-request.json的candidate_ids无效")
    delivery_mode = subject_ref.get("delivery_mode")
    if child_candidate_id is not None:
        subject = unit.get("subject")
        if child_candidate_id not in candidate_ids or not isinstance(subject, Mapping) or subject.get("candidate_id") != child_candidate_id:
            raise ReporterError("候选子报告的ReportUnit与受控selection不一致")
        report_unit_hashes = [str(unit.get("semantic_fact_hash"))]
        frozen_evidence_statuses = [_frozen_evidence_status(unit)]
    elif delivery_mode == "single":
        subject = unit.get("subject")
        if len(candidate_ids) != 1 or not isinstance(subject, Mapping) or subject.get("candidate_id") != candidate_ids[0]:
            raise ReporterError("根ReportUnit候选与受控selection不一致")
        report_unit_hashes = [str(unit.get("semantic_fact_hash"))]
        frozen_evidence_statuses = [_frozen_evidence_status(unit)]
    else:
        report_units = unit.get("report_units")
        if not isinstance(report_units, list) or any(not isinstance(item, Mapping) for item in report_units):
            raise ReporterError("ReportBundle缺少独立ReportUnit")
        bundle_candidate_ids: list[str] = []
        report_unit_hashes = []
        frozen_evidence_statuses = []
        for item in report_units:
            _validate_report_unit(item)
            subject = item.get("subject")
            if not isinstance(subject, Mapping) or not isinstance(subject.get("candidate_id"), str):
                raise ReporterError("ReportBundle含无效候选ReportUnit")
            bundle_candidate_ids.append(str(subject["candidate_id"]))
            report_unit_hashes.append(str(item.get("semantic_fact_hash")))
            frozen_evidence_statuses.append(_frozen_evidence_status(item))
        if request.get("output_type") == "quote":
            if not request.get("quote_items"):
                raise ReporterError("Quote缺少受控合同快照选择")
            fingerprints = []
            for item in report_units:
                contract = item.get("contract") if isinstance(item.get("contract"), Mapping) else {}
                candidate_id = str((item.get("subject") or {}).get("candidate_id", ""))
                fingerprint = contract.get("contract_fingerprint")
                if not isinstance(fingerprint, str) or not fingerprint:
                    raise ReporterError("Quote合同快照缺少contract_fingerprint")
                fingerprints.append((candidate_id, fingerprint))
            if len(set(fingerprints)) != len(fingerprints):
                raise ReporterError("Quote不得重复冻结同一合同快照")
        elif bundle_candidate_ids != candidate_ids:
            raise ReporterError("ReportBundle候选集合或顺序与受控selection不一致")
    if brief.get("report_unit_semantic_fact_hashes") != report_unit_hashes:
        raise ReporterError("design-brief.json未绑定全部ReportUnit事实哈希")
    if "report_unit_semantic_fact_hash" in payload or "report_unit_semantic_fact_hashes" in payload:
        raise ReporterError("designer-input.json不得包含内部ReportUnit事实哈希")
    expected_realization = {
        "combined": "collection_index_with_candidate_reports",
        "batch": "collection_index_with_candidate_reports",
        "comparison": "comparison_summary",
    }.get(str(delivery_mode))
    if request.get("output_type") == "quote":
        expected_realization = "quote_collection"
    if child_candidate_id is None:
        if expected_realization is None and manifest.get("delivery_realization") not in {None, "single_contract"}:
            raise ReporterError("run_manifest.json的single交付形态不一致")
        if expected_realization is not None and manifest.get("delivery_realization") != expected_realization:
            raise ReporterError("run_manifest.json的交付形态与report-request.json不一致")
    children_raw = manifest.get("children", [])
    if not isinstance(children_raw, list):
        raise ReporterError("run_manifest.json的children必须为数组")

    if child_candidate_id is not None:
        if children_raw:
            raise ReporterError("候选独立报告不得继续声明子报告")
        expected_children: list[str] = []
    else:
        expected_children = (
            [] if request.get("output_type") == "quote"
            else list(candidate_ids) if delivery_mode in {"combined", "batch"} else []
        )

    child_ids: list[str] = []
    children: list[dict[str, Any]] = []
    for index, raw_child in enumerate(children_raw, start=1):
        if not isinstance(raw_child, Mapping):
            raise ReporterError(f"children[{index}]不是对象")
        candidate_id = require_text(raw_child.get("candidate_id"), f"children[{index}].candidate_id")
        if candidate_id in child_ids:
            raise ReporterError("run_manifest.json含重复候选子报告")
        child_ids.append(candidate_id)
        directory = resolve_artifact_path(root, raw_child.get("directory"), f"children[{index}].directory")
        if not directory.is_dir() or directory.is_symlink():
            raise ReporterError(f"children[{index}].directory不存在或不是普通目录")
        validation = validate_report_run_directory(
            directory, expected_request=request, child_candidate_id=candidate_id,
        )
        children.append({"candidate_id": candidate_id, "directory": directory, "validation": validation})
    if child_ids != expected_children:
        raise ReporterError("run_manifest.json的候选子报告集合或顺序与受控selection不一致")

    child_evidence_statuses = [
        status
        for child in children
        for status in child["validation"]["frozen_evidence_statuses"]
    ]
    all_evidence_statuses = [*frozen_evidence_statuses, *child_evidence_statuses]
    expected_delivery_status = "succeeded" if all(status == "verified" for status in all_evidence_statuses) else "partial"
    if manifest.get("status") != expected_delivery_status:
        raise ReporterError("run_manifest.json的status必须由冻结ReportUnit.evidence_status推导")

    return {
        "root": root,
        "request": request,
        "unit": unit,
        "payload": payload,
        "brief": brief,
        "manifest": manifest,
        "receipt": receipt,
        "rendered_path": rendered_path,
        "children": children,
        "frozen_evidence_statuses": all_evidence_statuses,
        "delivery_status": expected_delivery_status,
    }


__all__ = [
    "resolve_artifact_path", "sha256_file", "validate_artifact_bytes", "validate_designer_artifact",
    "validate_hashed_artifact", "validate_report_run_directory", "validate_written_artifact",
]
