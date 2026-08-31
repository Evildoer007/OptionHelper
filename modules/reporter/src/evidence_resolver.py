"""显式引用的Reporter证据解析。

Reporter从不接收结果目录，也不搜索最近一次运行。所有计算模块均通过Core
``ResultStorePort``验真并读取不可变字节包；物理目录不会跨越正式端口。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from runtime.ports.result_store import ResultStorePort

from .models import (
    MODULE_TO_RUN,
    ReporterError,
    ReporterUnavailableError,
    ReportRequest,
    as_list,
    as_mapping,
    module_run_ref,
    require_identifier,
    require_text,
    stable_hash,
)


RECOMMENDATION_SCHEMA = "optionhelper.recommendation-set"
READY_STATUSES = {"succeeded"}
TERMINAL_NONREADY = {"failed", "unsupported", "cancelled", "timed_out", "partial"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ARTIFACT = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-/")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_QUOTE_FACT_SCHEMA = "optionhelper.reference-quote-fact"
_QUOTE_TERM_UNITS = {
    "price_normalized_percent", "market_price", "percent", "year",
    "date", "count", "text",
}
_PAYOFF_FACT_SCHEMA = "optionhelper.reporter-payoff-facts"
_SUPPLEMENTAL_ID = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_SUPPLEMENTAL_NODE_TYPES = {"paragraph", "metrics", "table", "formula", "chart"}
_SUPPLEMENTAL_FORBIDDEN_KEYS = {"html", "script", "javascript", "css", "style", "color", "font", "src", "href"}
_SUPPLEMENTAL_NODE_FIELDS = {
    "paragraph": {"type", "text"},
    "metrics": {"type", "items"},
    "table": {"type", "columns", "rows", "caption"},
    "formula": {"type", "formula"},
    "chart": {"type", "chart_type", "title", "x", "y", "data", "series", "accessibility_summary", "x_axis_name", "y_axis_name"},
}
_SUPPLEMENTAL_METRIC_FIELDS = {"label", "value", "value_format", "note"}
_SUPPLEMENTAL_COLUMN_FIELDS = {"key", "label"}
_SUPPLEMENTAL_SERIES_FIELDS = {"name", "data"}


def _safe_relative(value: Any, field: str) -> Path:
    text = require_text(value, field)
    if any(char not in _SAFE_ARTIFACT for char in text):
        raise ReporterError(f"{field}不是受控相对路径")
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ReporterError(f"{field}不是受控相对路径")
    return Path(*path.parts)


def _bundle_bytes(bundle: Mapping[str, bytes], value: Any, field: str) -> bytes:
    name = _safe_relative(value, field).as_posix()
    payload = bundle.get(name)
    if not isinstance(payload, bytes):
        raise ReporterError(f"{field}未由ResultStore返回受控字节")
    return payload


def _bundle_json(bundle: Mapping[str, bytes], value: Any, field: str) -> dict[str, Any]:
    payload = _bundle_bytes(bundle, value, field)
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReporterError(f"{field}不是有效JSON：{error}") from error
    return as_mapping(raw, field)


def _bundle_json_value(bundle: Mapping[str, bytes], value: Any, field: str) -> Any:
    payload = _bundle_bytes(bundle, value, field)
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReporterError(f"{field}不是有效JSON：{error}") from error


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


def _terminal_error(bundle: Mapping[str, bytes], manifest: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """读取Core正式error.json，保留失败原因但不读取任何结果数值。"""

    if isinstance(manifest.get("error"), Mapping):
        return dict(manifest["error"])
    relative_value = manifest.get("error", "error.json")
    if not isinstance(relative_value, str):
        return None
    try:
        return _bundle_json(bundle, relative_value, "error.json")
    except ReporterError:
        return None


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
        if value.get("candidate_version_id") is not None:
            candidate["candidate_version_id"] = require_identifier(
                value.get("candidate_version_id"), "RecommendationCandidate.candidate_version_id",
            )
        if value.get("source_candidate_id") is not None:
            candidate["source_candidate_id"] = require_identifier(
                value.get("source_candidate_id"), "RecommendationCandidate.source_candidate_id",
            )
        if value.get("supplemental_sections") is not None:
            candidate["supplemental_sections"] = validate_supplemental_sections(value.get("supplemental_sections"))
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


def _reject_supplemental_markup(value: Any, field: str) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SUPPLEMENTAL_FORBIDDEN_KEYS or normalized.endswith("_color"):
                raise ReporterError(f"{field}不接受视觉、HTML或脚本字段")
            _reject_supplemental_markup(nested, f"{field}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_supplemental_markup(nested, f"{field}[{index}]")
    elif isinstance(value, str) and (re.search(r"<\s*/?\s*[A-Za-z]", value) or "javascript:" in value.casefold()):
        raise ReporterError(f"{field}不接受HTML或脚本")


def _validate_supplemental_node(node: Mapping[str, Any], field: str) -> None:
    def public_scalar(value: Any, scalar_field: str) -> None:
        if isinstance(value, str) and value.strip():
            return
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            return
        raise ReporterError(f"{scalar_field}必须是公开标量")

    node_type = str(node.get("type", "")).strip().lower()
    allowed = _SUPPLEMENTAL_NODE_FIELDS.get(node_type)
    if allowed is None:
        raise ReporterError("supplemental_sections含不受支持的内容类型")
    if set(node).difference(allowed):
        raise ReporterError(f"{field}含未知字段")
    if node_type == "metrics":
        for index, raw in enumerate(as_list(node.get("items"), f"{field}.items")):
            item = as_mapping(raw, f"{field}.items[{index}]")
            if set(item).difference(_SUPPLEMENTAL_METRIC_FIELDS):
                raise ReporterError(f"{field}.items[{index}]含未知字段")
            if not isinstance(item.get("label"), str) or not item["label"].strip() or "value" not in item:
                raise ReporterError(f"{field}.items[{index}]缺少公开标签或数值")
            for key, value in item.items():
                public_scalar(value, f"{field}.items[{index}].{key}")
    elif node_type == "table":
        if "caption" in node and (not isinstance(node["caption"], str) or not node["caption"].strip()):
            raise ReporterError(f"{field}.caption必须是非空字符串")
        column_keys: set[str] = set()
        for index, raw in enumerate(as_list(node.get("columns"), f"{field}.columns")):
            column = as_mapping(raw, f"{field}.columns[{index}]")
            if set(column).difference(_SUPPLEMENTAL_COLUMN_FIELDS):
                raise ReporterError(f"{field}.columns[{index}]含未知字段")
            column_keys.add(require_text(column.get("key"), f"{field}.columns[{index}].key"))
            require_text(column.get("label"), f"{field}.columns[{index}].label")
        for index, raw in enumerate(as_list(node.get("rows"), f"{field}.rows")):
            row = as_mapping(raw, f"{field}.rows[{index}]")
            if set(row) != column_keys:
                raise ReporterError(f"{field}.rows[{index}]必须完整对应已声明列")
            for key, value in row.items():
                public_scalar(value, f"{field}.rows[{index}].{key}")
    elif node_type == "chart":
        for key in ("chart_type", "title", "accessibility_summary", "x_axis_name", "y_axis_name"):
            if key in node and (not isinstance(node[key], str) or not node[key].strip()):
                raise ReporterError(f"{field}.{key}必须是非空字符串")
        for key in ("x", "y"):
            if key not in node:
                continue
            for index, value in enumerate(as_list(node[key], f"{field}.{key}", allow_empty=True)):
                if not isinstance(value, str) and (
                    not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value))
                ):
                    raise ReporterError(f"{field}.{key}[{index}]必须是文字或有限数值")
        for index, point in enumerate(as_list(node["data"], f"{field}.data", allow_empty=True) if "data" in node else []):
            if not isinstance(point, list) or len(point) != 3:
                raise ReporterError(f"{field}.data[{index}]必须是三元数值数组")
            for point_index, value in enumerate(point):
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
                    raise ReporterError(f"{field}.data[{index}][{point_index}]必须是有限数值")
        for index, raw in enumerate(as_list(node["series"], f"{field}.series", allow_empty=True) if "series" in node else []):
            series = as_mapping(raw, f"{field}.series[{index}]")
            if set(series).difference(_SUPPLEMENTAL_SERIES_FIELDS):
                raise ReporterError(f"{field}.series[{index}]含未知字段")
            if not isinstance(series.get("name"), str) or not series["name"].strip():
                raise ReporterError(f"{field}.series[{index}].name不能为空")
            for value_index, value in enumerate(as_list(series.get("data"), f"{field}.series[{index}].data", allow_empty=True)):
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
                    raise ReporterError(f"{field}.series[{index}].data[{value_index}]必须是有限数值")
    elif node_type == "paragraph":
        if not isinstance(node.get("text"), str) or not node["text"].strip():
            raise ReporterError(f"{field}.text不能为空")
    elif node_type == "formula":
        if not isinstance(node.get("formula"), str) or not node["formula"].strip():
            raise ReporterError(f"{field}.formula不能为空")


def validate_supplemental_sections(value: Any) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(as_list(value, "RecommendationCandidate.supplemental_sections", allow_empty=True)):
        section = as_mapping(raw, f"RecommendationCandidate.supplemental_sections[{index}]")
        if set(section).difference({"id", "title", "content"}):
            raise ReporterError("supplemental_sections含未知字段")
        section_id = require_text(section.get("id"), f"supplemental_sections[{index}].id")
        if not _SUPPLEMENTAL_ID.fullmatch(section_id) or section_id in seen:
            raise ReporterError("supplemental_sections.id无效或重复")
        if section_id in {"conclusion", "recommendation", "parameters", "payoff", "pricing", "backtest", "risk"}:
            raise ReporterError("补充章节不得覆盖7个核心章节")
        content = as_list(section.get("content"), f"supplemental_sections[{index}].content")
        for node_index, raw_node in enumerate(content):
            node = as_mapping(raw_node, f"supplemental_sections[{index}].content[{node_index}]")
            _validate_supplemental_node(node, f"supplemental_sections[{index}].content[{node_index}]")
            _reject_supplemental_markup(node, f"supplemental_sections[{index}].content[{node_index}]")
        seen.add(section_id)
        sections.append({
            "id": section_id,
            "title": require_text(section.get("title"), f"supplemental_sections[{index}].title"),
            "content": deepcopy(content),
        })
    return sections


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
    bundle: Mapping[str, bytes],
    manifest: Mapping[str, Any],
    ref_hash: str,
    ref_manifest_hash: str,
    module: str,
) -> tuple[dict[str, Any], str]:
    # Core LocalResultStore的正式布局固定为artifacts/artifact_manifest.json，并由
    # commit_marker.json把语义哈希和清单文件哈希绑定。上游模块可回填同一相对路径，
    # 但Reporter不要求不存在的manifest.artifact_manifest_hash字段。
    relative = _safe_relative(manifest.get("artifact_manifest", "artifacts/artifact_manifest.json"), "manifest.artifact_manifest")
    payload = _bundle_bytes(bundle, relative.as_posix(), "manifest.artifact_manifest")
    actual_manifest_hash = sha256(payload).hexdigest()
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReporterError(f"artifact_manifest不是有效JSON：{error}") from error
    if not isinstance(raw, dict):
        raise ReporterError("artifact_manifest必须为对象")
    marker = _bundle_json(bundle, "commit_marker.json", "commit_marker.json")
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
    expected_bundle_names = set(str(name) for name in file_hashes) | {relative.as_posix(), "commit_marker.json"}
    if set(bundle) != expected_bundle_names:
        raise ReporterError("ResultStore返回的ModuleRun字节包与产物清单不一致")
    for name, expected_hash in file_hashes.items():
        if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
            raise ReporterError(f"artifact_manifest.file_hashes.{name}不是有效SHA-256")
        reference = _safe_relative(name, f"artifact_manifest.file_hashes.{name}").as_posix()
        if sha256(_bundle_bytes(bundle, reference, f"artifact_manifest.file_hashes.{name}")).hexdigest() != expected_hash:
            raise ReporterError(f"artifact_manifest.file_hashes.{name}内容哈希不一致")
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
        result_store.verify_module_run(ref, tenant_id=request.tenant_id)
        bundle = result_store.read_module_run_bundle(ref, tenant_id=request.tenant_id)
    except Exception as error:
        raise ReporterError(f"无法通过ResultStore验真并读取{display_module}的ModuleRunRef：{error}") from error
    if not isinstance(bundle, Mapping) or not bundle or any(not isinstance(name, str) or not isinstance(payload, bytes) for name, payload in bundle.items()):
        raise ReporterError(f"ResultStore未返回有效{display_module}运行字节包")
    manifest = _bundle_json(bundle, "manifest.json", "manifest.json")
    if manifest.get("module") != run_module:
        raise ReporterError(f"{display_module}运行模块不一致：期望{run_module}")
    checks = {
        "tenant_id": request.tenant_id,
        "task_id": request.task_id,
        "run_id": ref.run_id,
        "analysis_case_id": request.analysis_case_id,
        "candidate_id": candidate.get("source_candidate_id", candidate["candidate_id"]),
        "semantic_result_hash": ref.expected_semantic_result_hash,
    }
    for key, expected in checks.items():
        if manifest.get(key) != expected:
            raise ReporterError(f"{display_module} manifest.{key}与请求/引用不一致")
    lifecycle = str(manifest.get("lifecycle_status", manifest.get("status", ""))).lower()
    if lifecycle in TERMINAL_NONREADY:
        error = _terminal_error(bundle, manifest)
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
        contract = _contract_fields(
            _bundle_json(bundle, manifest.get("resolved_contract", "resolved_contract.json"), "resolved_contract.json"),
            "resolved_contract",
        )
    except ReporterError as error:
        raise ReporterError(f"{display_module}运行不满足正式协议：{error}") from error
    # 以下均为已声明对象之间的冲突或受控文件篡改，必须拒绝生成，不能降级混用。
    if require_text(manifest.get("contract_fingerprint"), "manifest.contract_fingerprint") != str(candidate.get("contract_fingerprint")):
        raise ReporterError("manifest.contract_fingerprint与候选合同快照不一致")
    candidate_version_id = candidate.get("candidate_version_id")
    manifest_candidate_version_id = manifest.get("candidate_version_id")
    if candidate_version_id is not None or manifest_candidate_version_id is not None:
        if (
            require_identifier(candidate_version_id, "RecommendationCandidate.candidate_version_id")
            != require_identifier(manifest_candidate_version_id, "manifest.candidate_version_id")
        ):
            raise ReporterError("manifest.candidate_version_id与RecommendationCandidate不一致")
    result = _bundle_json(bundle, manifest.get("result", "result.json"), "result.json")
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
        bundle, manifest, ref.expected_semantic_result_hash, ref.expected_artifact_manifest_hash, run_module,
    )
    public_result = deepcopy(result)
    if display_module == "payoff":
        facts = _payoff_facts(bundle, artifact_manifest, result)
        public_result.pop("path_panels", None)
        public_result.pop("paths", None)
        public_result["reporter_payoff_facts"] = facts
    limitations_name = _safe_relative(manifest.get("limitations", "limitations.json"), "manifest.limitations").as_posix()
    limitations_raw = _bundle_json_value(bundle, limitations_name, "limitations.json") if limitations_name.endswith(".json") else []
    return {
        "status": "ready",
        "note": "已按显式ModuleRunRef、合同快照和产物清单完成验证。",
        "ref": ref,
        "run": {
            **{key: manifest.get(key) for key in ("module", "task_id", "run_id", "analysis_case_id", "candidate_id", "semantic_result_hash", "contract_fingerprint", "product_version", "execution_fingerprint")},
            "artifact_manifest_hash": artifact_manifest_hash,
        },
        "contract": contract,
        "result": public_result,
        "artifact_manifest": artifact_manifest,
        "limitations": limitations_raw if isinstance(limitations_raw, list) else [],
        "_bundle": dict(bundle),
    }


def _payoff_facts(
    bundle: Mapping[str, bytes],
    artifact_manifest: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Read only Payoffer's controlled machine-fact projection."""

    raw: Any = None
    file_hashes = artifact_manifest.get("file_hashes")
    if isinstance(file_hashes, Mapping) and "artifacts/payoff.json" in file_hashes:
        artifact = _bundle_json(bundle, "artifacts/payoff.json", "artifacts/payoff.json")
        raw = artifact.get("reporter_payoff_facts")
        declared_hash = artifact.get("reporter_payoff_facts_hash")
    else:
        raw = result.get("reporter_payoff_facts")
        declared_hash = result.get("reporter_payoff_facts_hash")
    facts = as_mapping(raw, "reporter_payoff_facts")
    if (
        facts.get("schema") != _PAYOFF_FACT_SCHEMA
        or facts.get("projection_status") != "controlled_percent_machine_facts"
        or facts.get("payoff_unit") != "percent"
        or facts.get("source") != "runtime.contracts.evaluate_contract"
        or facts.get("discounting") != "not_applied"
        or not isinstance(facts.get("paths"), list)
        or not facts["paths"]
    ):
        raise ReporterError("Payoffer reporter_payoff_facts不满足正式公开协议")
    if not isinstance(declared_hash, str) or declared_hash != stable_hash(facts):
        raise ReporterError("Payoffer reporter_payoff_facts_hash与冻结事实不一致")
    return deepcopy(facts)


def _quote_fact(result: Mapping[str, Any], contract: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one explicit Pricer quote fact; never infer price terms from contracts."""

    pricing = result.get("pricing") if isinstance(result.get("pricing"), Mapping) else result
    precision_status = require_text(pricing.get("precision_status"), "pricing.precision_status")
    if pricing.get("quote_eligible") is not True or precision_status in {"demo_only", "not_assessed", "not_priced"}:
        raise ReporterUnavailableError("所选估值未通过正式报价资格或精度门槛，当前Quote不可用")
    raw = pricing.get("reporter_quote_fact", result.get("reporter_quote_fact"))
    if not isinstance(raw, Mapping):
        raise ReporterUnavailableError("所选Pricer结果没有冻结报价事实，当前Quote不可用")
    fact = dict(raw)
    if fact.get("schema") != _QUOTE_FACT_SCHEMA or fact.get("quote_eligible") is not True:
        raise ReporterUnavailableError("冻结报价事实缺少正式资格，当前Quote不可用")
    if require_text(fact.get("precision_status"), "reporter_quote_fact.precision_status") != precision_status:
        raise ReporterError("冻结报价事实的precision_status与Pricer不一致")
    pricing_basis = require_text(pricing.get("price_convention"), "pricing.price_convention")
    fact_basis = require_text(fact.get("price_convention"), "reporter_quote_fact.price_convention")
    contract_convention = as_mapping(contract.get("price_convention"), "ResolvedContract.price_convention")
    contract_basis = require_text(contract_convention.get("contract_basis"), "ResolvedContract.price_convention.contract_basis")
    if pricing_basis not in {"normalized_100", "absolute_market"}:
        raise ReporterError("pricing.price_convention不是正式价格口径")
    if pricing_basis != fact_basis or pricing_basis != contract_basis:
        raise ReporterError("报价事实、Pricing与合同的price_convention不一致")
    applicable_date = require_text(fact.get("applicable_date"), "reporter_quote_fact.applicable_date")
    if not _DATE.fullmatch(applicable_date):
        raise ReporterError("reporter_quote_fact.applicable_date必须为YYYY-MM-DD")
    source = as_mapping(fact.get("source"), "reporter_quote_fact.source")
    source_fact = {
        "provider": require_text(source.get("provider"), "reporter_quote_fact.source.provider"),
        "reference_id": require_text(source.get("reference_id"), "reporter_quote_fact.source.reference_id"),
    }
    terms: list[dict[str, Any]] = []
    for index, raw_term in enumerate(as_list(fact.get("terms"), "reporter_quote_fact.terms"), start=1):
        term = as_mapping(raw_term, f"reporter_quote_fact.terms[{index}]")
        if set(term).difference({"symbol", "label", "value", "unit", "note"}):
            raise ReporterError("reporter_quote_fact.terms含未受控字段")
        value = term.get("value")
        if isinstance(value, (Mapping, list, tuple, bool)) or value is None:
            raise ReporterError("reporter_quote_fact.terms.value必须为明确标量")
        unit = require_text(term.get("unit"), f"reporter_quote_fact.terms[{index}].unit")
        if unit not in _QUOTE_TERM_UNITS:
            raise ReporterError("reporter_quote_fact.terms.unit不是正式公开单位")
        if (
            (unit == "price_normalized_percent" and pricing_basis != "normalized_100")
            or (unit == "market_price" and pricing_basis != "absolute_market")
        ):
            raise ReporterError("reporter_quote_fact条款单位与价格口径不一致")
        terms.append({
            "symbol": require_text(term.get("symbol"), f"reporter_quote_fact.terms[{index}].symbol"),
            "label": require_text(term.get("label"), f"reporter_quote_fact.terms[{index}].label"),
            "value": value,
            "unit": unit,
            **({"note": require_text(term.get("note"), f"reporter_quote_fact.terms[{index}].note")} if term.get("note") else {}),
        })
    return {
        "schema": _QUOTE_FACT_SCHEMA,
        "source": source_fact,
        "applicable_date": applicable_date,
        "quote_eligible": True,
        "precision_status": precision_status,
        "price_convention": pricing_basis,
        "terms": terms,
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


def _resolve_quote_evidence(
    request: ReportRequest,
    result_store: ResultStorePort,
) -> list[dict[str, Any]]:
    """Resolve each explicitly selected Quote row without assuming the candidate default terms.

    Quote may select several saved parameter variants for the same recommendation
    candidate.  Product identity, task, tenant, underlying and registry checks stay
    identical to a normal Report; only the candidate's default contract fingerprint
    is deliberately not used as a selection gate.
    """

    result: list[dict[str, Any]] = []
    source_definitions = as_mapping(request.source_refs.get("quote_sources"), "source_refs.quote_sources")
    source_requests: dict[str, ReportRequest] = {}
    seen_contracts: set[str] = set()
    for index, item in enumerate(request.quote_items, start=1):
        source_id = require_identifier(item.get("source_id"), f"quote_items[{index}].source_id")
        raw_source = as_mapping(source_definitions.get(source_id), f"source_refs.quote_sources.{source_id}")
        if source_id not in source_requests:
            source_refs = as_mapping(raw_source.get("source_refs"), f"quote_sources.{source_id}.source_refs")
            source_task_id = require_identifier(raw_source.get("task_id"), f"quote_sources.{source_id}.task_id")
            source_case_id = require_identifier(raw_source.get("analysis_case_id"), f"quote_sources.{source_id}.analysis_case_id")
            source_requests[source_id] = replace(
                request,
                task_id=source_task_id,
                analysis_case_id=source_case_id,
                source_refs=source_refs,
                subject_ref={"delivery_mode": "single", "candidate_ids": []},
                quote_items=(),
            )
        source_request = source_requests[source_id]
        candidates, _ = _recommendation_set(source_request)
        candidate_id = str(item["candidate_id"])
        if candidate_id not in candidates:
            raise ReporterError(f"quote_items[{index}]候选不属于所选来源")
        candidate = candidates[candidate_id]
        module = str(item["module"])
        if module != "pricing":
            raise ReporterError(f"quote_items[{index}]：Quote只接受Pricer冻结的报价事实")
        evidence = _load_module_run(
            source_request,
            result_store,
            candidate=candidate,
            display_module=module,
            raw_ref=as_mapping(item["module_run_ref"], f"quote_items[{index}].module_run_ref"),
        )
        if evidence.get("status") != "ready" or not isinstance(evidence.get("contract"), Mapping):
            raise ReporterError(f"quote_items[{index}]必须选择已完成且可验证的运行结果")
        contract = dict(evidence["contract"])
        quote_fact = _quote_fact(evidence["result"], contract)
        signature = require_text(contract.get("contract_fingerprint"), "ResolvedContract.contract_fingerprint")
        if signature in seen_contracts:
            raise ReporterError("Quote不得为同一合同选择多个Pricing运行")
        seen_contracts.add(signature)
        variant = deepcopy(dict(candidate))
        variant["contract_fingerprint"] = signature
        modules = {
            name: evidence if name == module else {"status": "not_requested", "note": _status_note("not_requested")}
            for name in MODULE_TO_RUN
        }
        result.append({
            "candidate": variant,
            "modules": modules,
            "contract": contract,
            "quote_module": module,
            "quote_fact": quote_fact,
        })
    if not result:
        raise ReporterError("组合报价没有可展示的已验证合同快照")
    return result


def resolve_evidence(request: ReportRequest, result_store: ResultStorePort) -> dict[str, Any]:
    """解析一份正式请求的所有证据，返回仅供Reporter内部使用的对象。"""

    if request.output_type == "quote":
        return {
            "recommender": {"status": "ready", "source": "quote-snapshots"},
            "candidates": {},
            "quote_evidence": _resolve_quote_evidence(request, result_store),
        }
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
