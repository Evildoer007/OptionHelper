"""Reporter的最小领域模型和确定性序列化工具。

这里不重复定义跨模块协议：运行引用和ResultStore均直接使用Core的
``ModuleRunRef``与``ResultStorePort``。Reporter只拥有其请求、冻结内容和
导出清单。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from runtime.contracts.contract_types import canonical_json, semantic_hash
from runtime.protocol.models import ModuleRunRef


SCHEMA_REQUEST = "optionhelper.report-request/v2"
SCHEMA_REPORT_UNIT = "optionhelper.contract-report-unit/v2"
SCHEMA_MANIFEST = "optionhelper.report-run-manifest/v2"
DISPLAY_MODULES = ("recommender", "payoff", "pricing", "backtest")
MODULE_TO_RUN = {"payoff": "payoffer", "pricing": "pricer", "backtest": "backtester"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\\\/]")
_PHYSICAL_PATH_KEYS = {
    "path", "directory", "result_dir", "input_dir", "output_dir", "request_path", "output_path",
}


class ReporterError(ValueError):
    """Reporter请求、证据或交付物不满足受控契约。"""


def require_text(value: Any, field: str) -> str:
    result = str(value).strip() if value is not None else ""
    if not result:
        raise ReporterError(f"{field}不能为空")
    return result


def require_identifier(value: Any, field: str) -> str:
    result = require_text(value, field)
    if not _IDENTIFIER.fullmatch(result) or result in {".", ".."}:
        raise ReporterError(f"{field}只能使用字母、数字、下划线、点和连字符")
    return result


def as_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReporterError(f"{field}必须为对象")
    return {str(key): item for key, item in value.items()}


def as_list(value: Any, field: str, *, allow_empty: bool = False) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ReporterError(f"{field}必须为数组")
    result = list(value)
    if not allow_empty and not result:
        raise ReporterError(f"{field}不能为空")
    return result


def reject_physical_paths(value: Any, field: str = "ReportRequest") -> None:
    """拒绝请求选择对象中的物理路径，运行位置只能由ResultStore解析。"""

    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key).lower()
            if name in _PHYSICAL_PATH_KEYS or name.endswith("_path") or name.endswith("_dir"):
                raise ReporterError(f"{field}.{key}不接受物理路径")
            reject_physical_paths(item, f"{field}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            reject_physical_paths(item, f"{field}[{index}]")
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith(("/", "~")) or _WINDOWS_ABSOLUTE_PATH.match(text):
            raise ReporterError(f"{field}不接受物理路径")


def stable_hash(value: Any) -> str:
    """Reporter事实对象的语义哈希，不用于重算合同指纹。"""

    return semantic_hash(value)


def read_json(path: Path, label: str) -> dict[str, Any]:
    value = read_json_value(path, label)
    return as_mapping(value, label)


def read_json_value(path: Path, label: str) -> Any:
    if not path.is_file() or path.is_symlink():
        raise ReporterError(f"{label}不存在或不是普通文件")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReporterError(f"{label}不是有效JSON：{error}") from error
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def module_run_ref(value: Any, field: str, *, tenant_id: str, task_id: str) -> ModuleRunRef:
    raw = as_mapping(value, field)
    forbidden = {"result_dir", "path", "latest"}.intersection(raw)
    if forbidden:
        raise ReporterError(f"{field}不接受物理路径字段：{','.join(sorted(forbidden))}")
    allowed = {
        "module", "tenant_id", "task_id", "run_id",
        "expected_semantic_result_hash", "expected_artifact_manifest_hash",
    }
    unknown = set(raw).difference(allowed)
    if unknown:
        raise ReporterError(f"{field}含未知字段：{','.join(sorted(unknown))}")
    semantic_hash = require_text(raw.get("expected_semantic_result_hash"), f"{field}.expected_semantic_result_hash")
    manifest_hash = require_text(raw.get("expected_artifact_manifest_hash"), f"{field}.expected_artifact_manifest_hash")
    if not re.fullmatch(r"[0-9a-f]{64}", semantic_hash):
        raise ReporterError(f"{field}.expected_semantic_result_hash必须为64位小写SHA-256")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_hash):
        raise ReporterError(f"{field}.expected_artifact_manifest_hash必须为64位小写SHA-256")
    ref = ModuleRunRef(
        module=require_identifier(raw.get("module"), f"{field}.module"),
        tenant_id=require_identifier(raw.get("tenant_id"), f"{field}.tenant_id"),
        task_id=require_identifier(raw.get("task_id"), f"{field}.task_id"),
        run_id=require_identifier(raw.get("run_id"), f"{field}.run_id"),
        expected_semantic_result_hash=semantic_hash,
        expected_artifact_manifest_hash=manifest_hash,
    )
    if ref.tenant_id != tenant_id or ref.task_id != task_id:
        raise ReporterError(f"{field}的tenant_id/task_id必须与ReportRequest一致")
    return ref


@dataclass(frozen=True)
class ReportRequest:
    """正式Reporter请求。

    ``subject_ref``承载单结构、组合、批量或横向对比的交付组织方式；它不改变
    ``output_type``与``format``的语义。
    """

    tenant_id: str
    task_id: str
    report_run_id: str
    analysis_case_id: str
    subject_type: str
    subject_ref: Mapping[str, Any]
    source_refs: Mapping[str, Any]
    output_type: str
    format: str
    html_report_layout: str | None
    audience: str
    metadata: Mapping[str, Any]

    @property
    def delivery_mode(self) -> str:
        return str(self.subject_ref.get("delivery_mode", "single")).lower()

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        values = self.subject_ref.get("candidate_ids", ())
        return tuple(require_identifier(item, "subject_ref.candidate_ids[]") for item in as_list(values, "subject_ref.candidate_ids", allow_empty=True))

    @property
    def selected_modules(self) -> tuple[str, ...]:
        if "selected_modules" not in self.subject_ref:
            raise ReporterError("subject_ref.selected_modules必须显式指定")
        values = self.subject_ref.get("selected_modules")
        result = tuple(str(item).strip().lower() for item in as_list(values, "subject_ref.selected_modules"))
        if len(set(result)) != len(result) or any(item not in DISPLAY_MODULES for item in result):
            raise ReporterError("subject_ref.selected_modules只能包含recommender、payoff、pricing、backtest且不得重复")
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReportRequest":
        raw = as_mapping(value, "ReportRequest")
        forbidden = {"report_level", "delivery_mode", "selected_modules", "candidate_ids", "module_runs", "result_dir", "request_path"}.intersection(raw)
        if forbidden:
            raise ReporterError(
                "ReportRequest v2不接受旧字段：" + ",".join(sorted(forbidden)) + "；请使用subject_ref与source_refs.module_run_refs。"
            )
        reject_physical_paths(raw)
        allowed = {
            "schema", "tenant_id", "task_id", "report_run_id", "analysis_case_id", "subject_type", "subject_ref",
            "source_refs", "output_type", "format", "html_report_layout", "audience", "metadata",
        }
        unknown = set(raw).difference(allowed)
        if unknown:
            raise ReporterError(f"ReportRequest含未知字段：{','.join(sorted(unknown))}")
        if raw.get("schema") != SCHEMA_REQUEST:
            raise ReporterError(f"ReportRequest.schema必须为{SCHEMA_REQUEST}")
        output_type = require_text(raw.get("output_type"), "output_type").lower()
        output_format = str(raw.get("format") or "html").strip().lower()
        raw_layout = raw.get("html_report_layout")
        if raw_layout is None and output_type == "report" and output_format == "html":
            raw_layout = "continuous"
        request = cls(
            tenant_id=require_identifier(raw.get("tenant_id"), "tenant_id"),
            task_id=require_identifier(raw.get("task_id"), "task_id"),
            report_run_id=require_identifier(raw.get("report_run_id"), "report_run_id"),
            analysis_case_id=require_identifier(raw.get("analysis_case_id"), "analysis_case_id"),
            subject_type=require_text(raw.get("subject_type"), "subject_type").lower(),
            subject_ref=as_mapping(raw.get("subject_ref"), "subject_ref"),
            source_refs=as_mapping(raw.get("source_refs"), "source_refs"),
            output_type=output_type,
            format=output_format,
            html_report_layout=str(raw_layout).lower() if raw_layout is not None else None,
            audience=require_text(raw.get("audience"), "audience"),
            metadata=as_mapping(raw.get("metadata", {}), "metadata"),
        )
        request._validate()
        return request

    def _validate(self) -> None:
        if self.subject_type not in {"product", "contract", "bundle", "comparison"}:
            raise ReporterError("subject_type仅支持product、contract、bundle、comparison")
        if self.delivery_mode not in {"single", "combined", "batch", "comparison"}:
            raise ReporterError("subject_ref.delivery_mode仅支持single、combined、batch、comparison")
        candidate_ids = self.candidate_ids
        if self.subject_type == "product":
            if self.delivery_mode != "single":
                raise ReporterError("product仅支持single交付")
            if candidate_ids:
                raise ReporterError("product不应提供candidate_ids；请使用subject_ref.product_ref")
            product_ref = self.subject_ref.get("product_ref")
            if not isinstance(product_ref, Mapping):
                raise ReporterError("product需要subject_ref.product_ref")
        else:
            if self.delivery_mode == "single" and len(candidate_ids) != 1:
                raise ReporterError("single必须且只能选择一个candidate_id")
            if self.delivery_mode != "single" and len(candidate_ids) < 2:
                raise ReporterError("combined、batch和comparison至少选择两个candidate_id")
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ReporterError("subject_ref.candidate_ids不得重复")
        self.selected_modules
        if self.output_type not in {"card", "report"}:
            raise ReporterError("output_type仅支持card、report")
        if self.format not in {"html", "pdf"}:
            raise ReporterError("format仅支持html、pdf")
        if self.format == "html":
            if self.output_type == "report":
                if self.html_report_layout != "continuous":
                    raise ReporterError("Report HTML仅支持连续版，html_report_layout必须为continuous")
            elif self.html_report_layout is not None:
                raise ReporterError("Card HTML不支持报告目录布局，html_report_layout必须为null")
        elif self.html_report_layout is not None:
            raise ReporterError("PDF为无目录页式，html_report_layout必须为null")
        required_source_keys = {"product_version_refs", "catalog_version_ref", "evidence_refs", "module_run_refs"}
        missing = required_source_keys.difference(self.source_refs)
        if missing:
            raise ReporterError(f"source_refs缺少：{','.join(sorted(missing))}")
        if not isinstance(self.source_refs.get("product_version_refs"), Mapping):
            raise ReporterError("source_refs.product_version_refs必须为对象")
        if not isinstance(self.source_refs.get("evidence_refs"), Mapping):
            raise ReporterError("source_refs.evidence_refs必须为对象")
        if not isinstance(self.source_refs.get("module_run_refs"), Mapping):
            raise ReporterError("source_refs.module_run_refs必须为对象")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_REQUEST,
            "tenant_id": self.tenant_id,
            "task_id": self.task_id,
            "report_run_id": self.report_run_id,
            "analysis_case_id": self.analysis_case_id,
            "subject_type": self.subject_type,
            "subject_ref": dict(self.subject_ref),
            "source_refs": dict(self.source_refs),
            "output_type": self.output_type,
            "format": self.format,
            "html_report_layout": self.html_report_layout,
            "audience": self.audience,
            "metadata": dict(self.metadata),
        }


__all__ = [
    "DISPLAY_MODULES", "MODULE_TO_RUN", "ReporterError", "ReportRequest",
    "SCHEMA_MANIFEST", "SCHEMA_REPORT_UNIT", "SCHEMA_REQUEST", "as_list", "as_mapping", "module_run_ref",
    "read_json", "read_json_value", "reject_physical_paths", "require_identifier", "require_text", "stable_hash", "write_json",
]
