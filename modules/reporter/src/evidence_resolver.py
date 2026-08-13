"""显式引用的Reporter证据解析。

Reporter从不接收结果目录，也不搜索最近一次运行。所有计算模块均由Core
``ResultStorePort.resolve_module_run``解析；解析出的临时路径仅用于读取和复制
本次受控产物，绝不写入ReportUnit。
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from runtime.ports.result_store import ResultStorePort

from .models import (
    MODULE_TO_RUN,
    ReporterError,
    ReportRequest,
    as_list,
    as_mapping,
    module_run_ref,
    read_json,
    read_json_value,
    require_identifier,
    require_text,
    stable_hash,
)
from .artifact_validator import validate_hashed_artifact


RECOMMENDATION_SCHEMA = "optionhelper.recommendation-set/v1.0.0"
READY_STATUSES = {"succeeded"}
TERMINAL_NONREADY = {"failed", "unsupported", "cancelled", "timed_out", "partial"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ARTIFACT = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-/")


def _safe_relative(value: Any, field: str) -> Path:
    text = require_text(value, field)
    if any(char not in _SAFE_ARTIFACT for char in text):
        raise ReporterError(f"{field}不是受控相对路径")
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ReporterError(f"{field}不是受控相对路径")
    return Path(*path.parts)


def _inside(root: Path, value: Path, field: str) -> Path:
    root = root.resolve()
    target = (root / value).resolve(strict=False)
    if target != root and root not in target.parents:
        raise ReporterError(f"{field}越出ResultStore解析目录")
    if target.is_symlink():
        raise ReporterError(f"{field}不能是符号链接")
    return target


def _status_note(status: str, error: Mapping[str, Any] | None = None, reason: Any = None) -> str:
    if reason:
        return str(reason)
    if error:
        return str(error.get("message") or error.get("reason") or "上游模块未提供可展示结果。")
    labels = {
        "not_run": "本次报告未提供该模块的显式运行引用。",
        "failed": "上游模块运行失败，不能填充案例数值。",
        "unsupported": "上游模块声明当前不支持该请求。",
        "partial": "上游模块只完成部分计算，不能作为完整结论展示。",
        "not_requested": "本次未选择该模块。",
    }
    return labels.get(status, "该模块没有可展示的本次运行结果。")


def _terminal_error(run_dir: Path, manifest: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """读取Core正式error.json，保留失败原因但不读取任何结果数值。"""

    if isinstance(manifest.get("error"), Mapping):
        return dict(manifest["error"])
    relative_value = manifest.get("error", "error.json")
    if not isinstance(relative_value, str):
        return None
    try:
        path = _inside(run_dir, _safe_relative(relative_value, "manifest.error"), "manifest.error")
    except ReporterError:
        return None
    return read_json(path, "error.json") if path.is_file() and not path.is_symlink() else None


def _required_candidate(raw: Mapping[str, Any], *, request: ReportRequest) -> dict[str, Any]:
    """读取一条完整、可验证的正式推荐候选。"""

    value = dict(raw)
    try:
        candidate_id = require_identifier(value.get("candidate_id"), "RecommendationCandidate.candidate_id")
        product_id = require_text(value.get("product_id"), "RecommendationCandidate.product_id")
        product_name = require_text(value.get("product_name"), "RecommendationCandidate.product_name")
        product_version = require_text(value.get("product_version"), "RecommendationCandidate.product_version")
        contract_fingerprint = require_text(value.get("contract_fingerprint"), "RecommendationCandidate.contract_fingerprint")
        product_version_content_hash = require_text(
            value.get("product_version_content_hash"),
            "RecommendationCandidate.product_version_content_hash",
        )
        if not _SHA256.fullmatch(product_version_content_hash):
            raise ReporterError("RecommendationCandidate.product_version_content_hash必须是64位SHA-256")
        analysis_basis_id = require_text(value.get("analysis_basis_id"), "RecommendationCandidate.analysis_basis_id")
        underlyings = [require_text(item, "RecommendationCandidate.underlyings[]") for item in as_list(value.get("underlyings"), "RecommendationCandidate.underlyings")]
        currency = require_text(value.get("currency"), "RecommendationCandidate.currency")
        price_convention = as_mapping(value.get("price_convention"), "RecommendationCandidate.price_convention")
        reason = require_text(value.get("reason"), "RecommendationCandidate.reason")
        rank = int(value.get("rank"))
        if rank < 1:
            raise ReporterError("RecommendationCandidate.rank必须为正整数")
        for field in ("suitable_for", "not_suitable_for", "main_risks"):
            as_list(value.get(field), f"RecommendationCandidate.{field}", allow_empty=True)
        candidate = {
            "candidate_id": candidate_id,
            "product_id": product_id,
            "product_name": product_name,
            "product_version": product_version,
            "product_version_content_hash": product_version_content_hash,
            "contract_fingerprint": contract_fingerprint,
            "analysis_basis_id": analysis_basis_id,
            "underlyings": underlyings,
            "currency": currency,
            "price_convention": price_convention,
            "rank": rank,
            "reason": reason,
            "suitable_for": list(value.get("suitable_for", [])),
            "not_suitable_for": list(value.get("not_suitable_for", [])),
            "main_risks": list(value.get("main_risks", [])),
            "library_status": str(value.get("library_status", "unavailable")),
            "key_terms": list(value.get("key_terms", [])) if isinstance(value.get("key_terms", []), list) else [],
            "evidence_refs": list(value.get("evidence_refs", [])) if isinstance(value.get("evidence_refs", []), list) else [],
        }
        version_ref = as_mapping(request.source_refs["product_version_refs"].get(candidate_id), f"product_version_refs.{candidate_id}")
        if require_text(version_ref.get("product_id"), f"product_version_refs.{candidate_id}.product_id") != product_id:
            raise ReporterError(f"候选{candidate_id}的product_id与product_version_refs不一致")
        if require_text(version_ref.get("product_version"), f"product_version_refs.{candidate_id}.product_version") != product_version:
            raise ReporterError(f"候选{candidate_id}的product_version与product_version_refs不一致")
        version_content_hash = require_text(
            version_ref.get("content_hash"), f"product_version_refs.{candidate_id}.content_hash",
        )
        if not _SHA256.fullmatch(version_content_hash):
            raise ReporterError(f"product_version_refs.{candidate_id}.content_hash必须是64位小写SHA-256")
        if product_version_content_hash != version_content_hash:
            raise ReporterError(f"候选{candidate_id}的product_version_content_hash与product_version_refs不一致")
        return candidate
    except (ReporterError, TypeError, ValueError) as error:
        raise ReporterError(f"RecommendationCandidate不满足正式协议：{error}") from error


def _recommendation_set(request: ReportRequest) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    catalog_ref = as_mapping(request.source_refs.get("catalog_version_ref"), "source_refs.catalog_version_ref")
    catalog_version = require_text(catalog_ref.get("catalog_version"), "catalog_version_ref.catalog_version")
    catalog_content_hash = require_text(catalog_ref.get("content_hash"), "catalog_version_ref.content_hash")
    evidence_refs = as_mapping(request.source_refs.get("evidence_refs"), "source_refs.evidence_refs")
    envelope = as_mapping(evidence_refs.get("recommendation_set"), "source_refs.evidence_refs.recommendation_set")
    allowed = {"source_id", "run_id", "payload", "expected_semantic_result_hash"}
    unknown = set(envelope).difference(allowed)
    if unknown:
        raise ReporterError(f"recommendation_set引用含未知字段：{','.join(sorted(unknown))}")
    payload = as_mapping(envelope.get("payload"), "recommendation_set.payload")
    expected_hash = require_text(envelope.get("expected_semantic_result_hash"), "recommendation_set.expected_semantic_result_hash")
    if stable_hash(payload) != expected_hash:
        raise ReporterError("RecommendationSet语义哈希不一致")
    if payload.get("schema") != RECOMMENDATION_SCHEMA:
        raise ReporterError(f"RecommendationSet.schema必须为{RECOMMENDATION_SCHEMA}")
    if require_identifier(payload.get("tenant_id"), "RecommendationSet.tenant_id") != request.tenant_id:
        raise ReporterError("RecommendationSet.tenant_id与ReportRequest不一致")
    if require_identifier(payload.get("task_id"), "RecommendationSet.task_id") != request.task_id:
        raise ReporterError("RecommendationSet.task_id与ReportRequest不一致")
    if require_identifier(payload.get("analysis_case_id"), "RecommendationSet.analysis_case_id") != request.analysis_case_id:
        raise ReporterError("RecommendationSet.analysis_case_id与ReportRequest不一致")
    if require_text(payload.get("catalog_version"), "RecommendationSet.catalog_version") != catalog_version:
        raise ReporterError("RecommendationSet.catalog_version与catalog_version_ref不一致")
    if require_text(payload.get("catalog_content_hash"), "RecommendationSet.catalog_content_hash") != catalog_content_hash:
        raise ReporterError("RecommendationSet.catalog_content_hash与catalog_version_ref不一致")
    run_id = require_identifier(payload.get("run_id"), "RecommendationSet.run_id")
    if envelope.get("run_id") and require_identifier(envelope.get("run_id"), "recommendation_set.run_id") != run_id:
        raise ReporterError("RecommendationSet引用run_id与payload不一致")
    candidates: dict[str, dict[str, Any]] = {}
    for raw in as_list(payload.get("candidates"), "RecommendationSet.candidates"):
        candidate = _required_candidate(as_mapping(raw, "RecommendationSet.candidates[]"), request=request)
        candidate_id = candidate["candidate_id"]
        if candidate_id in candidates:
            raise ReporterError(f"RecommendationSet.candidates含重复candidate_id：{candidate_id}")
        candidates[candidate_id] = candidate
    selected = set(request.candidate_ids)
    missing = selected.difference(candidates)
    if missing:
        raise ReporterError(f"RecommendationSet未包含所选candidate_id：{','.join(sorted(missing))}")
    return candidates, {
        "status": "ready",
        "source": require_text(envelope.get("source_id"), "recommendation_set.source_id"),
        "run_id": run_id,
        "semantic_result_hash": expected_hash,
    }


def _contract_fields(raw: Mapping[str, Any], field: str) -> dict[str, Any]:
    contract = as_mapping(raw, field)
    identity = as_mapping(contract.get("identity"), f"{field}.identity")
    required = {
        "product_id": require_text(identity.get("product_id"), f"{field}.identity.product_id"),
        "product_name": require_text(identity.get("name_zh"), f"{field}.identity.name_zh"),
        "underlyings": [require_text(item, f"{field}.identity.underlyings[]") for item in as_list(identity.get("underlyings"), f"{field}.identity.underlyings")],
        "currency": require_text(identity.get("currency"), f"{field}.identity.currency"),
        "product_version": require_text(contract.get("product_version"), f"{field}.product_version"),
        "contract_fingerprint": require_text(contract.get("contract_fingerprint"), f"{field}.contract_fingerprint"),
        "resolved_schedules": contract.get("resolved_schedules"),
        "registry_snapshot_hash": require_text(contract.get("registry_snapshot_hash"), f"{field}.registry_snapshot_hash"),
        "product_snapshot_hash": require_text(contract.get("product_snapshot_hash"), f"{field}.product_snapshot_hash"),
    }
    if required["resolved_schedules"] is None:
        raise ReporterError(f"{field}.resolved_schedules不能为空；必须为ResolvedContract.to_protocol_dict()快照")
    if not _SHA256.fullmatch(required["registry_snapshot_hash"]) or not _SHA256.fullmatch(required["product_snapshot_hash"]):
        raise ReporterError(f"{field}缺少正式Registry/ProductVersion快照哈希")
    terms = as_mapping(contract.get("terms"), f"{field}.terms")
    term_sources = as_mapping(contract.get("term_sources"), f"{field}.term_sources")
    paths = as_list(contract.get("paths"), f"{field}.paths", allow_empty=True)
    analysis_basis_id = contract.get("analysis_basis_id", identity.get("analysis_basis_id"))
    if analysis_basis_id is None:
        analysis_basis_id = f"basis-{required['contract_fingerprint'][:20]}"
    price_convention = contract.get("price_convention", identity.get("price_convention"))
    required["analysis_basis_id"] = require_text(analysis_basis_id, f"{field}.analysis_basis_id")
    if isinstance(price_convention, str):
        if price_convention not in {"normalized_100", "absolute_market"}:
            raise ReporterError(f"{field}.price_convention字符串口径不受支持")
        required["price_convention"] = {
            "spot": "close",
            "contract_basis": price_convention,
        }
    else:
        required["price_convention"] = as_mapping(price_convention, f"{field}.price_convention")
    # Preserve the complete ResolvedContract.to_protocol_dict snapshot. The summary fields above are only
    # validation projections; the detailed report must remain auditable to the original terms and paths.
    required["identity"] = identity
    required["terms"] = terms
    required["term_sources"] = term_sources
    required["paths"] = paths
    return required


def _artifact_manifest(
    run_dir: Path,
    manifest: Mapping[str, Any],
    ref_hash: str,
    ref_manifest_hash: str,
    module: str,
) -> tuple[dict[str, Any], str]:
    # Core LocalResultStore的正式布局固定为artifacts/artifact_manifest.json，并由
    # commit_marker.json把语义哈希和清单文件哈希绑定。上游模块可回填同一相对路径，
    # 但Reporter不要求不存在的manifest.artifact_manifest_hash字段。
    relative = _safe_relative(manifest.get("artifact_manifest", "artifacts/artifact_manifest.json"), "manifest.artifact_manifest")
    path = _inside(run_dir, relative, "manifest.artifact_manifest")
    if not path.is_file() or path.is_symlink():
        raise ReporterError("artifact_manifest不是受控普通文件")
    payload = path.read_bytes()
    actual_manifest_hash = sha256(payload).hexdigest()
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReporterError(f"artifact_manifest不是有效JSON：{error}") from error
    if not isinstance(raw, dict):
        raise ReporterError("artifact_manifest必须为对象")
    marker_path = _inside(run_dir, Path("commit_marker.json"), "commit_marker")
    marker = read_json(marker_path, "commit_marker.json")
    if actual_manifest_hash != ref_manifest_hash:
        raise ReporterError("artifact_manifest与ModuleRunRef外部锚点不一致")
    if marker.get("committed") is not True or marker.get("artifact_manifest_hash") != actual_manifest_hash:
        raise ReporterError("commit_marker未绑定当前artifact_manifest")
    if marker.get("semantic_result_hash") != ref_hash:
        raise ReporterError("commit_marker.semantic_result_hash与ModuleRunRef不一致")
    if manifest.get("artifact_manifest_hash") and manifest.get("artifact_manifest_hash") != actual_manifest_hash:
        raise ReporterError("manifest.artifact_manifest_hash与文件不一致")
    if raw.get("module") != module:
        raise ReporterError("artifact_manifest.module与ModuleRun不一致")
    if raw.get("semantic_result_hash") != ref_hash:
        raise ReporterError("artifact_manifest.semantic_result_hash与ModuleRunRef不一致")
    file_hashes = raw.get("file_hashes")
    if not isinstance(file_hashes, Mapping) or not file_hashes:
        raise ReporterError("artifact_manifest必须列出受控file_hashes，至少覆盖result.json")
    if "result.json" not in file_hashes:
        raise ReporterError("artifact_manifest未覆盖result.json")
    for name, expected_hash in file_hashes.items():
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise ReporterError(f"artifact_manifest.file_hashes.{name}不是有效SHA-256")
        reference = _safe_relative(name, f"artifact_manifest.file_hashes.{name}").as_posix()
        validate_hashed_artifact(run_dir, reference, expected_hash, f"artifact_manifest.file_hashes.{name}")
    artifacts = raw.get("artifacts")
    if not isinstance(artifacts, list):
        # Core的LocalResultStore以file_hashes为规范账本。将其投影为无路径外泄的
        # ArtifactRef形状，便于Reporter复制已验证的本次SVG等产物。
        def media_type(name: str) -> str:
            suffix = Path(name).suffix.lower()
            return {".svg": "image/svg+xml", ".json": "application/json", ".pdf": "application/pdf", ".html": "text/html"}.get(suffix, "application/octet-stream")
        raw["artifacts"] = [
            {"name": Path(str(name)).name, "storage_ref": str(name), "media_type": media_type(str(name)), "content_hash": str(expected_hash)}
            for name, expected_hash in file_hashes.items()
        ]
    else:
        for index, artifact in enumerate(artifacts, start=1):
            item = as_mapping(artifact, f"artifact_manifest.artifacts[{index}]")
            storage_ref = str(item.get("storage_ref", ""))
            content_hash = str(item.get("content_hash", ""))
            if storage_ref not in file_hashes:
                raise ReporterError(f"artifact_manifest.artifacts[{index}]未被file_hashes覆盖")
            if file_hashes[storage_ref] != content_hash:
                raise ReporterError(f"artifact_manifest.artifacts[{index}]内容哈希与file_hashes不一致")
    return raw, actual_manifest_hash


def _load_module_run(
    request: ReportRequest,
    result_store: ResultStorePort,
    *,
    candidate: Mapping[str, Any],
    display_module: str,
    raw_ref: Mapping[str, Any],
) -> dict[str, Any]:
    run_module = MODULE_TO_RUN[display_module]
    ref = module_run_ref(raw_ref, f"module_run_refs.{candidate['candidate_id']}.{display_module}", tenant_id=request.tenant_id, task_id=request.task_id)
    try:
        run_dir = result_store.resolve_module_run(ref, tenant_id=request.tenant_id)
    except Exception as error:
        raise ReporterError(f"无法通过ResultStore解析{display_module}的ModuleRunRef：{error}") from error
    if not isinstance(run_dir, Path) or not run_dir.is_dir() or run_dir.is_symlink():
        raise ReporterError(f"ResultStore未返回有效{display_module}运行目录")
    manifest = read_json(_inside(run_dir, Path("manifest.json"), "manifest"), "manifest.json")
    if manifest.get("module") != run_module:
        raise ReporterError(f"{display_module}运行模块不一致：期望{run_module}")
    checks = {
        "tenant_id": request.tenant_id,
        "task_id": request.task_id,
        "run_id": ref.run_id,
        "analysis_case_id": request.analysis_case_id,
        "candidate_id": candidate["candidate_id"],
        "semantic_result_hash": ref.expected_semantic_result_hash,
    }
    for key, expected in checks.items():
        if manifest.get(key) != expected:
            raise ReporterError(f"{display_module} manifest.{key}与请求/引用不一致")
    lifecycle = str(manifest.get("lifecycle_status", manifest.get("status", ""))).lower()
    if lifecycle in TERMINAL_NONREADY:
        error = _terminal_error(run_dir, manifest)
        return {
            "status": lifecycle,
            "note": _status_note(lifecycle, error, manifest.get("reason")),
            "ref": ref,
            "run": {key: manifest.get(key) for key in ("module", "task_id", "run_id", "analysis_case_id", "candidate_id", "semantic_result_hash")},
        }
    if lifecycle not in READY_STATUSES:
        return {
            "status": "failed",
            "note": f"上游模块尚未完成，当前状态为{lifecycle or 'unknown'}。",
            "ref": ref,
            "run": {key: manifest.get(key) for key in ("module", "task_id", "run_id", "analysis_case_id", "candidate_id", "semantic_result_hash")},
        }
    try:
        execution_fingerprint = require_text(manifest.get("execution_fingerprint"), "manifest.execution_fingerprint")
        contract_path = _inside(run_dir, _safe_relative(manifest.get("resolved_contract", "resolved_contract.json"), "manifest.resolved_contract"), "manifest.resolved_contract")
        contract = _contract_fields(read_json(contract_path, "resolved_contract.json"), "resolved_contract")
    except ReporterError as error:
        raise ReporterError(f"{display_module}运行不满足正式协议：{error}") from error
    # 以下均为已声明对象之间的冲突或受控文件篡改，必须拒绝生成，不能降级混用。
    if require_text(manifest.get("contract_fingerprint"), "manifest.contract_fingerprint") != str(candidate.get("contract_fingerprint")):
        raise ReporterError("manifest.contract_fingerprint与候选合同快照不一致")
    result_path = _inside(run_dir, _safe_relative(manifest.get("result", "result.json"), "manifest.result"), "manifest.result")
    result = read_json(result_path, "result.json")
    if result.get("semantic_result_hash") and result.get("semantic_result_hash") != ref.expected_semantic_result_hash:
        raise ReporterError("result.semantic_result_hash与ModuleRunRef不一致")
    if stable_hash(result) != ref.expected_semantic_result_hash:
        raise ReporterError("result.json语义哈希与ModuleRunRef不一致")
    if result.get("execution_fingerprint") and result.get("execution_fingerprint") != execution_fingerprint:
        raise ReporterError("result.execution_fingerprint与manifest不一致")
    if contract["contract_fingerprint"] != manifest["contract_fingerprint"]:
        raise ReporterError("resolved_contract.contract_fingerprint与manifest不一致")
    if contract["product_id"] != candidate["product_id"] or contract["product_name"] != candidate["product_name"]:
        raise ReporterError("ResolvedContract产品编号或中文名称与RecommendationCandidate不一致")
    if contract["product_version"] != candidate["product_version"]:
        raise ReporterError("ResolvedContract.product_version与RecommendationCandidate不一致")
    if contract["product_snapshot_hash"] != candidate["product_version_content_hash"]:
        raise ReporterError("ResolvedContract.product_snapshot_hash与RecommendationCandidate不一致")
    if contract["registry_snapshot_hash"] != require_text(
        as_mapping(request.source_refs.get("catalog_version_ref"), "source_refs.catalog_version_ref").get("content_hash"),
        "catalog_version_ref.content_hash",
    ):
        raise ReporterError("ResolvedContract.registry_snapshot_hash与Catalog证据不一致")
    if contract["analysis_basis_id"] != candidate["analysis_basis_id"]:
        raise ReporterError("ResolvedContract.analysis_basis_id与RecommendationCandidate不一致")
    if contract["underlyings"] != candidate["underlyings"]:
        raise ReporterError("ResolvedContract.underlyings顺序与RecommendationCandidate不一致")
    if contract["currency"] != candidate["currency"]:
        raise ReporterError("ResolvedContract.currency与RecommendationCandidate不一致")
    if contract["price_convention"] != candidate["price_convention"]:
        raise ReporterError("ResolvedContract.price_convention与RecommendationCandidate不一致")
    artifact_manifest, artifact_manifest_hash = _artifact_manifest(
        run_dir, manifest, ref.expected_semantic_result_hash, ref.expected_artifact_manifest_hash, run_module,
    )
    limitations_path = _inside(run_dir, _safe_relative(manifest.get("limitations", "limitations.json"), "manifest.limitations"), "manifest.limitations")
    limitations_raw = read_json_value(limitations_path, "limitations.json") if limitations_path.suffix == ".json" and limitations_path.exists() else []
    return {
        "status": "ready",
        "note": "已按显式ModuleRunRef、合同快照和产物清单完成验证。",
        "ref": ref,
        "run": {
            **{key: manifest.get(key) for key in ("module", "task_id", "run_id", "analysis_case_id", "candidate_id", "semantic_result_hash", "contract_fingerprint", "product_version", "execution_fingerprint")},
            "artifact_manifest_hash": artifact_manifest_hash,
        },
        "contract": contract,
        "result": result,
        "artifact_manifest": artifact_manifest,
        "limitations": limitations_raw if isinstance(limitations_raw, list) else [],
        "_run_dir": run_dir,
    }


def _validate_candidate_contracts(candidate_id: str, modules: Mapping[str, Mapping[str, Any]]) -> dict[str, Any] | None:
    ready = [value for value in modules.values() if value.get("status") == "ready"]
    if not ready:
        return None
    baseline = dict(ready[0]["contract"])
    fields = ("product_id", "product_name", "product_version", "product_snapshot_hash", "registry_snapshot_hash", "contract_fingerprint", "analysis_basis_id", "underlyings", "currency", "price_convention")
    for evidence in ready[1:]:
        candidate = evidence["contract"]
        for field in fields:
            if candidate.get(field) != baseline.get(field):
                raise ReporterError(f"候选{candidate_id}的已完成ModuleRun合同不一致：{field}")
    return baseline


def resolve_evidence(request: ReportRequest, result_store: ResultStorePort) -> dict[str, Any]:
    """解析一份正式请求的所有证据，返回仅供Reporter内部使用的对象。"""

    candidates, recommender = _recommendation_set(request)
    raw_refs = as_mapping(request.source_refs.get("module_run_refs"), "source_refs.module_run_refs")
    candidate_evidence: dict[str, Any] = {}
    for candidate_id in request.candidate_ids:
        candidate = candidates[candidate_id]
        candidate_refs = as_mapping(raw_refs.get(candidate_id, {}), f"module_run_refs.{candidate_id}")
        unknown = set(candidate_refs).difference(MODULE_TO_RUN)
        if unknown:
            raise ReporterError(f"module_run_refs.{candidate_id}含未知模块：{','.join(sorted(unknown))}")
        modules: dict[str, Any] = {}
        for display_module in MODULE_TO_RUN:
            if display_module not in request.selected_modules:
                modules[display_module] = {"status": "not_requested", "note": _status_note("not_requested")}
            elif display_module not in candidate_refs:
                modules[display_module] = {"status": "not_run", "note": _status_note("not_run")}
            else:
                modules[display_module] = _load_module_run(
                    request, result_store, candidate=candidate, display_module=display_module,
                    raw_ref=as_mapping(candidate_refs[display_module], f"module_run_refs.{candidate_id}.{display_module}"),
                )
        candidate_evidence[candidate_id] = {
            "candidate": candidate,
            "modules": modules,
            "contract": _validate_candidate_contracts(candidate_id, modules),
        }
    return {"recommender": recommender, "candidates": candidates, "candidate_evidence": candidate_evidence}


__all__ = ["RECOMMENDATION_SCHEMA", "resolve_evidence"]
