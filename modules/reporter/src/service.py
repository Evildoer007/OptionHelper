"""Reporter受控服务入口。

请求只包含当前ReportRequest的显式引用。ResultStore由宿主注入，服务不会接受
``result_dir``、``request_path``或“最近一次运行”之类的物理定位信息。
"""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from runtime.bootstrap import bootstrap_runtime
from runtime.ports.module import ModulePort
from runtime.ports.result_selection import ResultSelectionPort, require_result_selection_port
from runtime.ports.result_store import ResultStorePort

from .artifact_validator import validate_hashed_artifact, validate_written_artifact
from .config import DEFAULT_REPORTER_CONFIG, default_report_output_root
from .export_service import reissue_saved_report
from .models import DISPLAY_MODULES, ReporterError, require_identifier, require_text
from .reporter_engine import build_report
from .selection_facts import build_host_selection_source_refs


MODULE_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_PATHS = bootstrap_runtime(MODULE_ROOT)
PAGE_DIR = RUNTIME_PATHS.module_page_dir("reporter")
PAGE = PAGE_DIR / "reporter.html"
LOGO = RUNTIME_PATHS.project_root / "assets" / "icons" / "optionhelper-logo.svg"
TILE_ICON = RUNTIME_PATHS.project_root / "assets" / "icons" / "optionhelper-app-icon-tile-light.svg"
HOST = "127.0.0.1"
PORT = int(os.environ.get("OPTIONHELPER_REPORTER_PORT", "4282"))
_FORBIDDEN = {"request_path", "result_dir", "output_root", "report_level", "delivery_mode", "module_runs"}
_RUN_REF_FIELDS = (
    "module", "tenant_id", "task_id", "run_id",
    "expected_semantic_result_hash", "expected_artifact_manifest_hash",
)


def capability(*, result_store_configured: bool = False, designer_port_configured: bool = False, selection_port_configured: bool = False) -> dict[str, Any]:
    return {
        "ok": True,
        "module": "reporter",
        "status": "available" if result_store_configured and designer_port_configured and selection_port_configured else "requires_dependencies",
        "request_schema": "optionhelper.report-request",
        "actions": ["status", "run", "derive"],
        "required": ["显式ModuleRunRef", "source_refs", "宿主注入ResultStorePort", "宿主注入Designer ModulePort", "宿主注入ResultSelectionPort"],
        "forbidden": sorted(_FORBIDDEN),
        "formats": ["html", "pdf"],
        "output_types": ["card", "quote", "report"],
        "delivery_modes": ["single", "comparison", "quote"],
    }


def _selection_catalog(selection_port: ResultSelectionPort | None, *, tenant_id: str, task_id: str | None, query: str | None) -> Mapping[str, Any]:
    if selection_port is None:
        raise ReporterError("当前Host未注入ResultSelectionPort，不能列举已保存运行结果。")
    try:
        value = selection_port.list_report_sources(tenant_id=tenant_id, task_id=task_id, query=query)
    except Exception as error:
        raise ReporterError(f"ResultSelectionPort无法列举报告证据：{error}") from error
    if not isinstance(value, Mapping):
        raise ReporterError("ResultSelectionPort返回的报告证据目录无效")
    if value.get("tenant_id") != tenant_id or not isinstance(value.get("sources"), list):
        raise ReporterError("ResultSelectionPort返回的报告证据目录不属于当前租户或缺少sources")
    sources = []
    for raw in value["sources"]:
        if not isinstance(raw, Mapping) or raw.get("tenant_id") != tenant_id:
            continue
        candidates = []
        for candidate in raw.get("candidates", []) if isinstance(raw.get("candidates"), list) else []:
            if not isinstance(candidate, Mapping):
                continue
            refs = candidate.get("module_run_refs") if isinstance(candidate.get("module_run_refs"), Mapping) else {}
            options = candidate.get("module_run_options") if isinstance(candidate.get("module_run_options"), Mapping) else {}
            def public_ref(ref: Any) -> dict[str, Any] | None:
                if not isinstance(ref, Mapping):
                    return None
                return {key: ref.get(key) for key in (
                    "module", "tenant_id", "task_id", "run_id",
                    "expected_semantic_result_hash", "expected_artifact_manifest_hash", "status",
                )}
            candidates.append({
                **{key: candidate.get(key) for key in ("candidate_id", "product_id", "product_name", "product_version", "contract_fingerprint", "analysis_basis_id", "underlyings", "currency", "price_convention")},
                "module_run_refs": {
                    name: projected for name, ref in refs.items()
                    if name in {"payoff", "pricing", "backtest"} and (projected := public_ref(ref)) is not None
                },
                "module_run_options": {
                    name: [projected for ref in values if (projected := public_ref(ref)) is not None]
                    for name, values in options.items()
                    if name in {"payoff", "pricing", "backtest"} and isinstance(values, list)
                },
            })
        sources.append({
            **{key: raw.get(key) for key in ("source_id", "label", "tenant_id", "task_id", "analysis_case_id", "catalog_version")},
            "candidates": candidates,
        })
    return {"ok": True, "tenant_id": tenant_id, "sources": sources}


def build_selected_request(selection: Mapping[str, Any], *, tenant_id: str, selection_port: ResultSelectionPort | None) -> dict[str, Any]:
    """服务器按受控选择投影重建ReportRequest，浏览器不提交source_refs或路径。"""

    if selection_port is None:
        raise ReporterError("当前Host未注入ResultSelectionPort，不能选择已保存运行结果。")
    allowed = {"source_id", "candidate_ids", "selected_modules", "module_run_refs", "quote_items", "delivery_mode", "output_type", "format", "audience", "report_run_id", "metadata"}
    unknown = set(selection).difference(allowed)
    if unknown:
        raise ReporterError(f"页面选择含未知字段：{','.join(sorted(unknown))}")
    source_id = require_identifier(selection.get("source_id"), "selection.source_id")
    try:
        source = selection_port.get_report_source(tenant_id=tenant_id, source_id=source_id)
    except Exception as error:
        raise ReporterError(f"ResultSelectionPort无法读取所选报告证据：{error}") from error
    if not isinstance(source, Mapping) or source.get("tenant_id") != tenant_id or source.get("source_id") != source_id:
        raise ReporterError("所选报告证据不属于当前租户")
    task_id = require_identifier(source.get("task_id"), "source.task_id")
    analysis_case_id = require_identifier(source.get("analysis_case_id"), "source.analysis_case_id")
    candidates = source.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ReporterError("所选报告证据没有可用候选")
    candidate_map = {str(item.get("candidate_id")): item for item in candidates if isinstance(item, Mapping) and item.get("candidate_id")}
    output_type = str(selection.get("output_type", "report")).lower()
    output_format = str(selection.get("format", "html")).lower()
    source_refs = build_host_selection_source_refs(source, tenant_id)
    if output_type == "quote":
        raw_items = selection.get("quote_items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ReporterError("组合报价至少需要选择一行已保存运行结果")
        quote_items: list[dict[str, Any]] = []
        quote_sources: dict[str, dict[str, Any]] = {}
        seen: set[tuple[str, str, str, str]] = set()
        for index, raw_item in enumerate(raw_items, start=1):
            if not isinstance(raw_item, Mapping) or set(raw_item) != {"source_id", "candidate_id", "module", "module_run_ref"}:
                raise ReporterError(f"quote_items[{index}]字段必须为source_id、candidate_id、module、module_run_ref")
            item_source_id = require_identifier(raw_item.get("source_id"), f"quote_items[{index}].source_id")
            try:
                item_source = selection_port.get_report_source(tenant_id=tenant_id, source_id=item_source_id)
            except Exception as error:
                raise ReporterError(f"quote_items[{index}]来源不可读取：{error}") from error
            if not isinstance(item_source, Mapping) or item_source.get("tenant_id") != tenant_id:
                raise ReporterError(f"quote_items[{index}]来源不属于当前租户")
            item_candidates = item_source.get("candidates")
            if not isinstance(item_candidates, list):
                raise ReporterError(f"quote_items[{index}]来源缺少候选")
            item_candidate_map = {
                str(item.get("candidate_id")): item
                for item in item_candidates
                if isinstance(item, Mapping) and item.get("candidate_id")
            }
            candidate_id = require_identifier(raw_item.get("candidate_id"), f"quote_items[{index}].candidate_id")
            module = str(raw_item.get("module", "")).strip().lower()
            if candidate_id not in item_candidate_map or module not in {"payoff", "pricing", "backtest"}:
                raise ReporterError(f"quote_items[{index}]候选或来源模块无效")
            chosen = raw_item.get("module_run_ref")
            if not isinstance(chosen, Mapping):
                raise ReporterError(f"quote_items[{index}].module_run_ref必须为对象")
            options = item_candidate_map[candidate_id].get("module_run_options")
            allowed_refs = options.get(module, []) if isinstance(options, Mapping) and isinstance(options.get(module), list) else []
            if not any(
                isinstance(option, Mapping) and all(option.get(key) == chosen.get(key) for key in _RUN_REF_FIELDS)
                for option in allowed_refs
            ):
                raise ReporterError(f"quote_items[{index}]的ModuleRunRef不属于当前候选")
            signature = (item_source_id, candidate_id, module, str(chosen.get("run_id", "")))
            if signature in seen:
                raise ReporterError("Quote不得重复选择同一运行")
            seen.add(signature)
            quote_sources.setdefault(item_source_id, {
                "task_id": require_identifier(item_source.get("task_id"), f"quote_items[{index}].source.task_id"),
                "analysis_case_id": require_identifier(item_source.get("analysis_case_id"), f"quote_items[{index}].source.analysis_case_id"),
                "source_refs": build_host_selection_source_refs(item_source, tenant_id),
            })
            quote_items.append({
                "source_id": item_source_id,
                "candidate_id": candidate_id,
                "module": module,
                "module_run_ref": {key: chosen.get(key) for key in _RUN_REF_FIELDS},
            })
        return {
            "schema": "optionhelper.report-request",
            "tenant_id": tenant_id,
            "task_id": task_id,
            "report_run_id": require_identifier(selection.get("report_run_id"), "selection.report_run_id"),
            "analysis_case_id": analysis_case_id,
            "subject_type": "bundle",
            "subject_ref": {"delivery_mode": "quote", "candidate_ids": []},
            "source_refs": {**dict(source_refs), "module_run_refs": {}, "quote_sources": quote_sources},
            "quote_items": quote_items,
            "output_type": "quote",
            "format": output_format,
            "audience": str(selection.get("audience", "professional")),
            "metadata": dict(selection.get("metadata", {})) if isinstance(selection.get("metadata", {}), Mapping) else {},
        }
    candidate_ids = selection.get("candidate_ids")
    if not isinstance(candidate_ids, list) or not candidate_ids:
        raise ReporterError("页面必须显式选择至少一个候选")
    selected_ids = [require_identifier(value, "selection.candidate_ids[]") for value in candidate_ids]
    if len(set(selected_ids)) != len(selected_ids) or any(value not in candidate_map for value in selected_ids):
        raise ReporterError("页面选择的候选不属于当前报告证据")
    selected_modules = selection.get("selected_modules")
    if not isinstance(selected_modules, list) or not selected_modules:
        raise ReporterError("页面必须显式选择报告模块")
    modules = [str(value).strip().lower() for value in selected_modules]
    if len(set(modules)) != len(modules) or any(value not in DISPLAY_MODULES for value in modules):
        raise ReporterError("页面选择的报告模块无效")
    delivery_mode = str(selection.get("delivery_mode", "single")).lower()
    if delivery_mode == "single" and len(selected_ids) != 1:
        raise ReporterError("单结构报告只能选择一个候选")
    if delivery_mode != "single" and len(selected_ids) < 2:
        raise ReporterError("批量、组合或对比报告至少选择两个候选")
    requested_refs = selection.get("module_run_refs")
    if not isinstance(requested_refs, Mapping):
        raise ReporterError("页面选择必须显式提交module_run_refs对象")
    module_refs: dict[str, dict[str, Any]] = {}
    for candidate_id in selected_ids:
        record = candidate_map[candidate_id]
        explicit = requested_refs.get(candidate_id, {})
        if not isinstance(explicit, Mapping):
            raise ReporterError("页面选择的候选ModuleRunRef必须为对象")
        options = record.get("module_run_options") if isinstance(record.get("module_run_options"), Mapping) else {}
        module_refs[candidate_id] = {}
        for name in modules:
            if name == "recommender":
                continue
            if name not in explicit:
                raise ReporterError(f"候选{candidate_id}的{name}必须显式选择ModuleRunRef或声明未运行")
            chosen = explicit[name]
            allowed_refs = options.get(name, []) if isinstance(options.get(name), list) else []
            if chosen is None:
                if allowed_refs:
                    raise ReporterError(f"候选{candidate_id}的{name}存在已验证运行，请明确选择ModuleRunRef")
                continue
            if not isinstance(chosen, Mapping):
                raise ReporterError("页面选择的ModuleRunRef必须为对象或null")
            if not any(
                isinstance(option, Mapping) and all(option.get(key) == chosen.get(key) for key in _RUN_REF_FIELDS)
                for option in allowed_refs
            ):
                raise ReporterError("页面选择的ModuleRunRef不属于当前候选")
            module_refs[candidate_id][name] = {key: chosen.get(key) for key in _RUN_REF_FIELDS}
    return {
        "schema": "optionhelper.report-request",
        "tenant_id": tenant_id,
        "task_id": task_id,
        "report_run_id": require_identifier(selection.get("report_run_id"), "selection.report_run_id"),
        "analysis_case_id": analysis_case_id,
        "subject_type": "contract" if delivery_mode == "single" else "bundle" if delivery_mode in {"combined", "batch"} else "comparison",
        "subject_ref": {"delivery_mode": delivery_mode, "candidate_ids": selected_ids, "selected_modules": modules},
        "source_refs": {**dict(source_refs), "module_run_refs": module_refs},
        "output_type": output_type,
        "format": output_format,
        "audience": str(selection.get("audience", "professional")),
        "metadata": dict(selection.get("metadata", {})) if isinstance(selection.get("metadata", {}), Mapping) else {},
    }


def _report_url(task_id: str, report_run_id: str, filename: str, *, candidate_id: str | None = None, download: bool = False) -> str:
    prefix = f"/api/report-artifact/{task_id}/{report_run_id}"
    if candidate_id:
        prefix += f"/candidate_{candidate_id}"
    return f"{prefix}/{filename}" + ("?download=1" if download else "")


def _generated_response(outcome: Mapping[str, Any], *, request: Mapping[str, Any]) -> dict[str, Any]:
    report_name = str(outcome.get("report", ""))
    task_id, report_run_id = str(request["task_id"]), str(request["report_run_id"])
    children = []
    for raw in outcome.get("children", []) if isinstance(outcome.get("children"), list) else []:
        if not isinstance(raw, Mapping):
            continue
        candidate_id, child_report = raw.get("candidate_id"), raw.get("report")
        if isinstance(candidate_id, str) and isinstance(child_report, str):
            children.append({
                "candidate_id": candidate_id, "report": child_report,
                "preview_url": _report_url(task_id, report_run_id, child_report, candidate_id=candidate_id),
                "download_url": _report_url(task_id, report_run_id, child_report, candidate_id=candidate_id, download=True),
            })
    delivery_status = "completed" if outcome.get("status") == "succeeded" else "partial"
    return {
        "ok": True,
        "module": "reporter",
        "status": delivery_status,
        "output": {"report": report_name, "children": children},
        "preview_url": _report_url(task_id, report_run_id, report_name),
        "download_url": _report_url(task_id, report_run_id, report_name, download=True),
    }


def run_report(
    request: Mapping[str, Any],
    *,
    result_store: ResultStorePort,
    designer_port: ModulePort,
    output_root: Path | None = None,
) -> dict[str, Any]:
    body = dict(request)
    forbidden = _FORBIDDEN.intersection(body)
    if forbidden:
        raise ReporterError(f"Reporter服务不接受：{','.join(sorted(forbidden))}")
    target = output_root or default_report_output_root(RUNTIME_PATHS)
    return build_report(body, result_store=result_store, designer_port=designer_port, output_root=target)


def call_tool(
    request: Mapping[str, Any],
    *,
    result_store: ResultStorePort | None = None,
    designer_port: ModulePort | None = None,
    selection_port: ResultSelectionPort | None = None,
    tenant_id: str = "local",
    output_root: Path | None = None,
) -> Mapping[str, Any]:
    """Module Host适配；Host必须通过关键字显式注入ResultStore。"""

    body = dict(request)
    if selection_port is not None:
        selection_port = require_result_selection_port(selection_port)
    action = str(body.pop("action", "status")).strip().lower()
    forbidden = _FORBIDDEN.intersection(body)
    if forbidden:
        return {"ok": False, "module": "reporter", "status": "rejected", "error": f"不接受字段：{','.join(sorted(forbidden))}"}
    selection_ready = selection_port is not None
    if action in {"status", "catalog"}:
        return {**capability(result_store_configured=result_store is not None, designer_port_configured=designer_port is not None, selection_port_configured=selection_ready), "action": action}
    if action == "list_report_sources":
        return _selection_catalog(selection_port, tenant_id=require_identifier(tenant_id, "tenant_id"), task_id=body.get("task_id"), query=body.get("query"))
    if action == "derive":
        if designer_port is None:
            return {
                **capability(result_store_configured=result_store is not None, designer_port_configured=False, selection_port_configured=selection_ready),
                "action": action,
                "ok": False,
                "error": "designer_port_not_injected",
                "message": "Host必须注入Designer ModulePort。",
            }
        if set(body).difference({"source", "report_run_id", "format", "output_type"}) or not isinstance(body.get("source"), Mapping):
            return {
                "ok": False,
                "module": "reporter",
                "status": "rejected",
                "error": "derive_request_invalid",
                "message": "转换必须提交source、report_run_id和format；可选指定card、quote或report。",
            }
        try:
            target = output_root or default_report_output_root(RUNTIME_PATHS)
            outcome = reissue_saved_report(
                output_root=target,
                tenant_id=require_identifier(tenant_id, "tenant_id"),
                source=body["source"],
                report_run_id=body["report_run_id"],
                output_format=body["format"],
                output_type=body.get("output_type"),
                designer_port=designer_port,
            )
            request = json.loads((Path(outcome["directory"]) / "report-request.json").read_text(encoding="utf-8"))
            return _generated_response(outcome, request=request)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ReporterError) as error:
            return {
                "ok": False,
                "module": "reporter",
                "status": "failed",
                "error": "derive_failed",
                "message": str(error),
            }
    if action != "run":
        return {**capability(result_store_configured=result_store is not None, designer_port_configured=designer_port is not None, selection_port_configured=selection_ready), "action": action, "ok": False, "error": "unsupported_action"}
    if result_store is None:
        return {**capability(result_store_configured=False, designer_port_configured=designer_port is not None, selection_port_configured=selection_ready), "action": action, "ok": False, "error": "result_store_not_injected", "message": "Host必须注入ResultStorePort；Reporter不会读取用户给出的目录。"}
    if designer_port is None:
        return {**capability(result_store_configured=True, designer_port_configured=False, selection_port_configured=selection_ready), "action": action, "ok": False, "error": "designer_port_not_injected", "message": "Host必须注入Designer ModulePort；Reporter不会直接导入Designer实现。"}
    selection = body.pop("selection", None)
    if not isinstance(selection, Mapping) or body:
        return {
            "ok": False,
            "module": "reporter",
            "status": "rejected",
            "error": "selection_request_invalid",
            "message": "生成正式交付必须提交一个受控selection对象。",
        }
    try:
        body = build_selected_request(selection, tenant_id=require_identifier(tenant_id, "tenant_id"), selection_port=selection_port)
    except ReporterError as error:
        return {"ok": False, "module": "reporter", "status": "failed", "error": "selection_validation_failed", "message": str(error)}
    try:
        outcome = run_report(body, result_store=result_store, designer_port=designer_port, output_root=output_root)
    except ReporterError as error:
        return {"ok": False, "module": "reporter", "status": "failed", "error": "report_validation_failed", "message": str(error)}
    return _generated_response(outcome, request=body)


class Handler(BaseHTTPRequestHandler):
    """本机页面入口，ResultStore根目录仅来自服务端环境配置。"""

    result_store: ResultStorePort | None = None
    designer_port: ModulePort | None = None
    selection_port: ResultSelectionPort | None = None
    output_root: Path | None = None
    tenant_id = "local"

    def log_message(self, *_: Any) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            return self._json(HTTPStatus.OK, capability(result_store_configured=self.result_store is not None, designer_port_configured=self.designer_port is not None, selection_port_configured=self.selection_port is not None))
        if parsed.path == "/api/report-sources":
            try:
                query = parse_qs(parsed.query)
                task_id = query.get("task_id", [None])[0] or None
                if task_id is not None:
                    task_id = require_identifier(task_id, "task_id")
                search = query.get("q", [None])[0] or None
                return self._json(HTTPStatus.OK, _selection_catalog(self.selection_port, tenant_id=self.tenant_id, task_id=task_id, query=search))
            except ReporterError as error:
                return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "status": "unavailable", "message": str(error)})
        if parsed.path == "/api/report-artifact":
            return self._serve_report_artifact(parse_qs(parsed.query))
        if parsed.path.startswith("/api/report-artifact/"):
            return self._serve_report_artifact_path(parsed.path, parse_qs(parsed.query))
        if parsed.path in {"/", "/reporter.html"}:
            return self._file(PAGE, "text/html; charset=utf-8")
        if parsed.path == "/reporter.css":
            return self._file(PAGE_DIR / "reporter.css", "text/css; charset=utf-8")
        if parsed.path == "/reporter.js":
            return self._file(PAGE_DIR / "reporter.js", "application/javascript; charset=utf-8")
        if parsed.path == "/icons/optionhelper-logo.svg":
            return self._file(LOGO, "image/svg+xml")
        if parsed.path == "/icons/optionhelper-app-icon-tile-light.svg":
            return self._file(TILE_ICON, "image/svg+xml")
        return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "未找到资源"})

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/run":
            return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "未找到接口"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > DEFAULT_REPORTER_CONFIG.max_request_bytes:
                raise ReporterError("请求体长度无效")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, Mapping):
                raise ReporterError("请求体必须为ReportRequest对象")
            if isinstance(body.get("derive"), Mapping):
                result = call_tool(
                    {"action": "derive", **dict(body["derive"])},
                    designer_port=self.designer_port,
                    tenant_id=self.tenant_id,
                    output_root=self.output_root,
                )
                if not result.get("ok"):
                    raise ReporterError(str(result.get("message") or "报告转换失败"))
            else:
                if self.result_store is None or self.designer_port is None:
                    raise ReporterError("Reporter服务缺少ResultStorePort或Designer ModulePort。")
                if not isinstance(body.get("selection"), Mapping):
                    raise ReporterError("页面接口只接受受控selection或derive；机器调用请使用Reporter Tool入口。")
                request = build_selected_request(body["selection"], tenant_id=self.tenant_id, selection_port=self.selection_port)
                result = _generated_response(
                    run_report(request, result_store=self.result_store, designer_port=self.designer_port, output_root=self.output_root),
                    request=request,
                )
            return self._json(HTTPStatus.OK, result)
        except (UnicodeDecodeError, json.JSONDecodeError, ReporterError) as error:
            return self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": str(error)})

    def _serve_report_artifact(self, query: Mapping[str, list[str]]) -> None:
        candidate_id = query.get("candidate_id", [None])[0]
        name = str(query.get("name", [""])[0])
        relative = f"candidate_{candidate_id}/{name}" if candidate_id else name
        self._serve_report_artifact_reference(
            str(query.get("task_id", [""])[0]),
            str(query.get("report_run_id", [""])[0]),
            relative,
            download=query.get("download", [""])[0] == "1",
        )

    def _serve_report_artifact_path(self, path: str, query: Mapping[str, list[str]]) -> None:
        parts = path.removeprefix("/api/report-artifact/").split("/")
        if len(parts) < 3:
            return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "报告资源路径无效"})
        self._serve_report_artifact_reference(
            parts[0], parts[1], "/".join(parts[2:]),
            download=query.get("download", [""])[0] == "1",
        )

    def _serve_report_artifact_reference(self, task_value: str, run_value: str, relative: str, *, download: bool) -> None:
        try:
            if self.output_root is None:
                raise ReporterError("Reporter输出目录未配置")
            task_id = require_identifier(task_value, "task_id")
            report_run_id = require_identifier(run_value, "report_run_id")
            run_dir = (self.output_root / task_id / report_run_id).resolve()
            root = self.output_root.resolve()
            if root not in run_dir.parents or not run_dir.is_dir() or run_dir.is_symlink():
                raise ReporterError("ReportRun不存在")
            manifest_path = run_dir / "run_manifest.json"
            if not manifest_path.is_file() or manifest_path.is_symlink():
                raise ReporterError("ReportRun清单不存在")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("tenant_id") != self.tenant_id:
                raise ReporterError("ReportRun不属于当前租户")
            target_dir = run_dir
            reference = relative
            first, separator, remainder = relative.partition("/")
            children = manifest.get("children") if isinstance(manifest.get("children"), list) else []
            child = next((item for item in children if isinstance(item, Mapping) and item.get("directory") == first), None)
            if child is not None:
                if not separator or not remainder or not isinstance(child.get("directory"), str):
                    raise ReporterError("候选报告资源路径无效")
                target_dir = (run_dir / child["directory"]).resolve()
                if run_dir not in target_dir.parents or not target_dir.is_dir() or target_dir.is_symlink():
                    raise ReporterError("候选报告目录无效")
                manifest_path = target_dir / "run_manifest.json"
                if not manifest_path.is_file() or manifest_path.is_symlink():
                    raise ReporterError("候选报告清单不存在")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("tenant_id") != self.tenant_id:
                    raise ReporterError("候选报告不属于当前租户")
                reference = remainder
            rendered = manifest.get("rendered") if isinstance(manifest.get("rendered"), Mapping) else {}
            if reference == rendered.get("path"):
                path = validate_hashed_artifact(target_dir, reference, rendered.get("content_hash"), "ReportRun渲染产物")
                content_type = "application/pdf" if rendered.get("format") == "pdf" else "text/html; charset=utf-8"
                validate_written_artifact(path, output_format=str(rendered.get("format", "")), expected_hash=rendered.get("content_hash"), label="ReportRun渲染产物")
            else:
                assets = rendered.get("portable_assets") if isinstance(rendered.get("portable_assets"), list) else []
                asset = next((item for item in assets if isinstance(item, Mapping) and item.get("path") == reference), None)
                if asset is None:
                    raise ReporterError("只能读取ReportRun清单声明的渲染产物或portable资源")
                path = validate_hashed_artifact(target_dir, reference, asset.get("content_hash"), "ReportRun portable资源")
                content_type = require_text(asset.get("media_type"), "portable_assets.media_type")
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ReporterError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": str(error)})

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面文件不存在"})
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: Mapping[str, Any]) -> None:
        body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_host(
    *,
    host: str = HOST,
    port: int = PORT,
    result_store: ResultStorePort | None = None,
    designer_port: ModulePort | None = None,
    selection_port: ResultSelectionPort | None = None,
    output_root: Path | None = None,
    tenant_id: str = "local",
    development_mode: bool = False,
) -> None:
    """显式启动开发Host；正式发行必须经App的principal授权与ReportRun路由。"""

    if not development_mode:
        raise ReporterError("独立Reporter HTTP仅限显式development_mode；正式发行请使用App Reporter入口。")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ReporterError("独立Reporter开发Host只能绑定本机回环地址。")
    if result_store is None or designer_port is None or selection_port is None:
        raise ReporterError("独立Reporter开发Host必须显式注入ResultStore、Designer和ResultSelection端口。")
    selection_port = require_result_selection_port(selection_port)
    Handler.result_store = result_store
    Handler.designer_port = designer_port
    Handler.selection_port = selection_port
    Handler.tenant_id = require_identifier(tenant_id, "tenant_id")
    Handler.output_root = output_root or (RUNTIME_PATHS.result_root / DEFAULT_REPORTER_CONFIG.output_directory_name)
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    raise SystemExit("独立Reporter HTTP不作为发行入口；请由App Host调用Reporter Tool。")


if __name__ == "__main__":
    main()
