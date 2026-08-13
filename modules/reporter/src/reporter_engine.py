"""Reporter正式编排入口。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from runtime.ports.result_store import ResultStorePort
from runtime.ports.module import ModulePort

from .evidence_resolver import resolve_evidence
from .export_service import write_report_run
from .models import ReporterError, ReportRequest, read_json, stable_hash
from .report_unit_builder import build_report_document, build_report_units


def validate_request(value: Mapping[str, Any]) -> ReportRequest:
    """解析当前正式请求。"""

    return ReportRequest.from_mapping(value)


def build_report(
    value: Mapping[str, Any] | ReportRequest,
    *,
    result_store: ResultStorePort,
    designer_port: ModulePort,
    output_root: Path,
) -> dict[str, Any]:
    """将显式证据冻结、交给Designer并原子写入ReportRun。"""

    request = value if isinstance(value, ReportRequest) else validate_request(value)
    evidence = resolve_evidence(request, result_store)
    units = build_report_units(request, evidence)
    document = build_report_document(request, units)
    return write_report_run(
        output_root=output_root,
        request=request,
        document=document,
        units=units,
        evidence=evidence,
        designer_port=designer_port,
    )


__all__ = ["ReporterError", "build_report", "read_json", "stable_hash", "validate_request"]
