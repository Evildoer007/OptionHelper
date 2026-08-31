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


SCHEMA_REQUEST = "optionhelper.report-request"
SCHEMA_REPORT_UNIT = "optionhelper.contract-report-unit"
SCHEMA_REPORT_BUNDLE = "optionhelper.report-bundle"
SCHEMA_MANIFEST = "optionhelper.report-run-manifest"
SCHEMA_DESIGNER_PAYLOAD = "optionhelper.designer-payload"
SCHEMA_DESIGN_BRIEF = "optionhelper.design-brief"
SCHEMA_DESIGNER_ARTIFACT_MANIFEST = "optionhelper.designer-artifact-manifest"
REPORT_SECTION_ORDER = (
    "conclusion", "recommendation", "parameters",
    "payoff", "pricing", "backtest", "risk",
)
CARD_SECTION_ORDER = (
    "recommendation", "reason", "contract_highlights", "pricing", "backtest", "risk",
)
DISPLAY_MODULES = ("recommender", "payoff", "pricing", "backtest")
MODULE_TO_RUN = {"payoff": "payoffer", "pricing": "pricer", "backtest": "backtester"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\\\/]")
_PHYSICAL_PATH_KEYS = {
    "path", "directory", "result_dir", "input_dir", "output_dir", "request_path", "output_path",
}


class ReporterError(ValueError):
    """Reporter请求、证据或交付物不满足受控契约。"""


class ReporterUnavailableError(ReporterError):
    """所选正式证据不足以生成请求的公开交付。"""


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
    path.write_text(canonical_json(value) + "\n", encoding="utf-8", newline="")


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


def quote_module_run_ref(value: Any, field: str, *, tenant_id: str) -> ModuleRunRef:
    """Parse a Quote row reference without forcing it into the delivery task.

    A Quote is a collection of already frozen contracts.  Its delivery task is
    only the place where the resulting table is saved; every row retains the
    task of the source run it selected.
    """

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
    semantic_result_hash = require_text(raw.get("expected_semantic_result_hash"), f"{field}.expected_semantic_result_hash")
    artifact_manifest_hash = require_text(raw.get("expected_artifact_manifest_hash"), f"{field}.expected_artifact_manifest_hash")
    if not re.fullmatch(r"[0-9a-f]{64}", semantic_result_hash):
        raise ReporterError(f"{field}.expected_semantic_result_hash必须为64位小写SHA-256")
    if not re.fullmatch(r"[0-9a-f]{64}", artifact_manifest_hash):
        raise ReporterError(f"{field}.expected_artifact_manifest_hash必须为64位小写SHA-256")
    ref = ModuleRunRef(
        module=require_identifier(raw.get("module"), f"{field}.module"),
        tenant_id=require_identifier(raw.get("tenant_id"), f"{field}.tenant_id"),
        task_id=require_identifier(raw.get("task_id"), f"{field}.task_id"),
        run_id=require_identifier(raw.get("run_id"), f"{field}.run_id"),
        expected_semantic_result_hash=semantic_result_hash,
        expected_artifact_manifest_hash=artifact_manifest_hash,
    )
    if ref.tenant_id != tenant_id:
        raise ReporterError(f"{field}不属于当前租户")
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
    audience: str
    metadata: Mapping[str, Any]
    quote_items: tuple[Mapping[str, Any], ...] = ()

    @property
    def delivery_mode(self) -> str:
        return str(self.subject_ref.get("delivery_mode", "single")).lower()

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        values = self.subject_ref.get("candidate_ids", ())
        return tuple(require_identifier(item, "subject_ref.candidate_ids[]") for item in as_list(values, "subject_ref.candidate_ids", allow_empty=True))

    @property
    def selected_modules(self) -> tuple[str, ...]:
        if self.output_type == "quote":
            return tuple(dict.fromkeys(str(item["module"]) for item in self.quote_items))
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
        reject_physical_paths(raw)
        allowed = {
            "schema", "tenant_id", "task_id", "report_run_id", "analysis_case_id", "subject_type", "subject_ref",
            "source_refs", "output_type", "format", "audience", "metadata", "quote_items",
        }
        unknown = set(raw).difference(allowed)
        if unknown:
            raise ReporterError(f"ReportRequest含未知字段：{','.join(sorted(unknown))}")
        if raw.get("schema") != SCHEMA_REQUEST:
            raise ReporterError(f"ReportRequest.schema必须为{SCHEMA_REQUEST}")
        output_type = require_text(raw.get("output_type"), "output_type").lower()
        output_format = str(raw.get("format") or "html").strip().lower()
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
            audience=require_text(raw.get("audience"), "audience"),
            metadata=as_mapping(raw.get("metadata", {}), "metadata"),
            quote_items=tuple(
                as_mapping(item, "quote_items[]")
                for item in as_list(raw.get("quote_items", []), "quote_items", allow_empty=True)
            ),
        )
        request._validate()
        return request

    def _validate(self) -> None:
        if self.subject_type not in {"contract", "bundle", "comparison"}:
            raise ReporterError("subject_type仅支持contract、bundle、comparison")
        if self.delivery_mode not in {"single", "combined", "batch", "comparison", "quote"}:
            raise ReporterError("subject_ref.delivery_mode仅支持single、combined、batch、comparison、quote")
        candidate_ids = self.candidate_ids
        if self.output_type == "quote":
            if self.subject_type != "bundle" or self.delivery_mode != "quote":
                raise ReporterError("Quote必须使用bundle subject_type和quote delivery_mode")
            if "selected_modules" in self.subject_ref:
                raise ReporterError("Quote不接受selected_modules，请逐行使用quote_items选择已保存运行")
            if not self.quote_items:
                raise ReporterError("组合报价至少需要一行quote_items")
            quote_sources = self.source_refs.get("quote_sources")
            if not isinstance(quote_sources, Mapping) or not quote_sources:
                raise ReporterError("Quote必须包含每行来源的冻结证据")
            seen: set[tuple[str, str, str, str]] = set()
            for index, item in enumerate(self.quote_items, start=1):
                if set(item) != {"source_id", "candidate_id", "module", "module_run_ref"}:
                    raise ReporterError(f"quote_items[{index}]字段必须为source_id、candidate_id、module、module_run_ref")
                source_id = require_identifier(item.get("source_id"), f"quote_items[{index}].source_id")
                if source_id not in quote_sources:
                    raise ReporterError(f"quote_items[{index}]来源不在当前交付事实集中")
                candidate_id = require_identifier(item.get("candidate_id"), f"quote_items[{index}].candidate_id")
                module = require_text(item.get("module"), f"quote_items[{index}].module").lower()
                if module != "pricing":
                    raise ReporterError(f"quote_items[{index}]：Quote只接受Pricer冻结的报价事实")
                ref = quote_module_run_ref(
                    item.get("module_run_ref"), f"quote_items[{index}].module_run_ref",
                    tenant_id=self.tenant_id,
                )
                if ref.module != MODULE_TO_RUN[module]:
                    raise ReporterError(f"quote_items[{index}].module_run_ref.module与模块不一致")
                signature = (source_id, candidate_id, module, ref.run_id)
                if signature in seen:
                    raise ReporterError("quote_items不得重复选择同一运行")
                seen.add(signature)
        elif self.quote_items:
            raise ReporterError("只有Quote允许quote_items")
        if self.output_type != "quote" and self.delivery_mode == "single" and len(candidate_ids) != 1:
            raise ReporterError("single必须且只能选择一个candidate_id")
        if self.output_type != "quote" and self.delivery_mode != "single" and len(candidate_ids) < 2:
            raise ReporterError("combined、batch和comparison至少选择两个candidate_id")
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ReporterError("subject_ref.candidate_ids不得重复")
        if self.output_type not in {"card", "quote", "report"}:
            raise ReporterError("output_type仅支持card、quote、report")
        if self.output_type != "quote":
            self.selected_modules
        if self.format not in {"html", "pdf"}:
            raise ReporterError("format仅支持html、pdf")
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
        result = {
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
            "audience": self.audience,
            "metadata": dict(self.metadata),
        }
        if self.quote_items:
            result["quote_items"] = [dict(item) for item in self.quote_items]
        return result


__all__ = [
    "DISPLAY_MODULES", "MODULE_TO_RUN",
    "CARD_SECTION_ORDER", "REPORT_SECTION_ORDER", "ReporterError", "ReporterUnavailableError", "ReportRequest",
    "SCHEMA_DESIGN_BRIEF", "SCHEMA_DESIGNER_ARTIFACT_MANIFEST", "SCHEMA_DESIGNER_PAYLOAD",
    "SCHEMA_MANIFEST", "SCHEMA_REPORT_BUNDLE", "SCHEMA_REPORT_UNIT", "SCHEMA_REQUEST", "as_list", "as_mapping", "module_run_ref",
    "quote_module_run_ref", "read_json", "read_json_value", "reject_physical_paths", "require_identifier", "require_text", "stable_hash", "write_json",
]
