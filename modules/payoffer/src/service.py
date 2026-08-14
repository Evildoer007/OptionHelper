"""Python版Payoff模块的本机服务。"""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from runtime.bootstrap import bootstrap_runtime
from runtime.contracts.contract_api import (
    ContractResolutionError,
    ResolvedContract,
)
from runtime.ports.result_store import ResultStorePort
from runtime.protocol.module_host import ModuleHostContext

from .asset_resolver import DefaultAssetError, figure_asset_paths, verify_contract_snapshot_binding
from .impl.engine import (
    PayoffEngineError,
    load_registry,
    payoff_term_fields,
    preview_payload,
    render_payoff as render_payoff,
    run_payoff,
    run_runtime,
)
from .impl.svg_renderer import render_svg
from .models import PayoffInput


MODULE_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_PATHS = bootstrap_runtime(MODULE_ROOT)
PAGE_DIR = RUNTIME_PATHS.module_page_dir("payoffer")
PAGE = PAGE_DIR / "payoffer.html"
PAYOFF_UI_DIR = PAGE_DIR / "ui"
BRAND_ASSET_DIR = RUNTIME_PATHS.project_root / "assets" / "icons"
HOST = "127.0.0.1"
PORT = int(os.environ.get("OPTIONHELPER_PAYOFF_PYTHON_PORT", "4181"))


def catalog_payload() -> dict[str, object]:
    registry = load_registry()
    catalog = registry.get("term_catalog", {})
    if not isinstance(catalog, dict):
        raise PayoffEngineError("optionreg.py缺少term_catalog")
    products = []
    for product_id, product in registry["products"].items():
        fields = payoff_term_fields(product, registry)
        products.append(
            {
                "product_id": product_id,
                "canonical_name": product["identity"]["name_zh"],
                "name_zh": product["identity"]["name_zh"],
                "underlying_scope": "multi_underlying" if "S0Vec" in product["terms"] else "single_underlying",
                "underlying_count": len(product["terms"].get("S0Vec", ())) or 1,
                "runtime_status": "enabled" if product["identity"]["entry_status"] else "blocked",
                "formula_mode": "shared_cashflow_interpreter",
                "path_count": len(product["paths"]),
                "payoff_fields": fields,
            }
        )
    return {
        "ok": True,
        "module": "payoffer",
        "products": products,
    }


def _preview_svg(name_zh: str, payload: dict) -> str:
    if payload.get("runtime_status") == "enabled":
        return render_svg({"name_zh": name_zh, "paths": payload["path_panels"]})
    path = figure_asset_paths(name_zh)["svg"]
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise PayoffEngineError(f"缺少{name_zh}的正式默认SVG") from error


def _preview_request(body: Mapping[str, Any]) -> tuple[str, Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """开发预览只接受当前页面的product_id、term_overrides与identity输入。"""
    allowed = {"product_id", "term_overrides", "identity", "task_id", "run_id"}
    unknown = set(body) - allowed
    if unknown:
        raise PayoffEngineError(f"开发预览请求含未登记字段：{'、'.join(sorted(unknown))}；请使用product_id")
    product_id = str(body.get("product_id", "")).strip()
    product = load_registry().get("products", {}).get(product_id)
    if not isinstance(product, dict):
        raise PayoffEngineError("必须提供有效的product_id")
    identity = product.get("identity", {})
    name_zh = str(identity.get("name_zh", "")).strip() if isinstance(identity, dict) else ""
    if not name_zh:
        raise PayoffEngineError(f"{product_id}缺少中文产品名称")
    term_overrides = body.get("term_overrides")
    if term_overrides is not None and not isinstance(term_overrides, Mapping):
        raise PayoffEngineError("term_overrides必须是对象")
    supplied_identity = body.get("identity")
    if supplied_identity is not None and not isinstance(supplied_identity, Mapping):
        raise PayoffEngineError("identity必须是对象")
    return name_zh, term_overrides, supplied_identity


def page_asset_path(request_path: str) -> Path | None:
    """五页面统一以../../icons引用公共Logo；Host只暴露白名单品牌资源。"""
    filename = request_path.removeprefix("/icons/")
    if request_path.startswith("/icons/") and filename in {"optionhelper-logo.svg", "optionhelper-app-icon-tile-light.svg"}:
        return BRAND_ASSET_DIR / filename
    return None


def call_tool(
    request: Mapping[str, Any],
    *,
    host_context: ModuleHostContext | None = None,
    result_store: ResultStorePort | None = None,
    tenant_id: str | None = None,
) -> Mapping[str, Any]:
    """正式run由Host注入上下文与Store；页面payload不能提供Host事实。"""
    body = dict(request)
    action = str(body.pop("action", "run" if "contract" in body else "catalog")).strip().lower()
    if action == "catalog":
        return catalog_payload()
    if action == "preview":
        name_zh, term_overrides, identity = _preview_request(body)
        payload = preview_payload(name_zh, term_overrides, identity)
        return {"ok": True, "module": "payoffer", "preview": payload, "svg": _preview_svg(name_zh, payload)}
    if action == "run":
        if not isinstance(host_context, ModuleHostContext) or result_store is None or not tenant_id:
            raise PayoffEngineError("正式Payoffer调用必须由Host注入ModuleHostContext、ResultStorePort与tenant_id")
        payoff_input = _formal_payoff_input(body.get("payoff_input"))
        requested_run_id = body.get("run_id")
        run_id = requested_run_id.strip() if isinstance(requested_run_id, str) and requested_run_id.strip() else f"run-{uuid4().hex[:12]}"
        return run_payoff(
            payoff_input,
            run_id=run_id,
            host_context=host_context,
            result_store=result_store,
            tenant_id=tenant_id,
        )
    return {
        "ok": False,
        "module": "payoffer",
        "status": "unsupported",
        "message": f"Payoffer不支持action={action}；仅支持catalog、preview、run。",
    }


def _formal_payoff_input(value: Any) -> PayoffInput:
    if isinstance(value, PayoffInput):
        contract = value.contract
    elif isinstance(value, Mapping) and set(value) == {"contract"}:
        contract_value = value["contract"]
        if isinstance(contract_value, ResolvedContract):
            contract = contract_value
        elif isinstance(contract_value, Mapping):
            try:
                contract = ResolvedContract.from_controlled_snapshot(contract_value)
            except (TypeError, ValueError, ContractResolutionError) as error:
                raise PayoffEngineError(f"PayoffInput.contract必须是Core受控ResolvedContract快照：{error}") from error
        else:
            raise PayoffEngineError("PayoffInput.contract必须为ResolvedContract或完整协议对象")
    else:
        raise PayoffEngineError("正式Payoffer只接受PayoffInput={contract}")
    try:
        verify_contract_snapshot_binding(contract)
    except DefaultAssetError as error:
        raise PayoffEngineError(f"PayoffInput.contract产品快照无效：{error}") from error
    if {"S0", "S0Vec"} & set(contract.terms):
        references = contract.identity.get("reference_prices")
        if not isinstance(references, Mapping) or set(references) != set(contract.underlyings):
            raise PayoffEngineError("正式Payoffer必须提供逐标的价格参考")
    return PayoffInput(contract=contract)


class Handler(BaseHTTPRequestHandler):
    server_version = "OptionHelperPayoff"

    def log_message(self, format: str, *args: object) -> None:
        print(f"[Payoff] {format % args}")

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: object) -> None:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise PayoffEngineError("Payoffer公开JSON包含不安全或不可序列化的值") from error
        self._respond(status, encoded, "application/json; charset=utf-8")

    def _request_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise PayoffEngineError("请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PayoffEngineError("请求体必须是UTF-8 JSON对象") from error
        if not isinstance(value, dict):
            raise PayoffEngineError("请求体必须是JSON对象")
        return value

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            asset = page_asset_path(parsed.path)
            if asset is not None:
                self._respond(HTTPStatus.OK, asset.read_bytes(), "image/svg+xml")
                return
            if parsed.path in {"/", "/payoffer.html"}:
                self._respond(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8")
                return
            if parsed.path == "/payoffer.css":
                self._respond(HTTPStatus.OK, (PAGE_DIR / "payoffer.css").read_bytes(), "text/css; charset=utf-8")
                return
            if parsed.path == "/payoffer.js":
                self._respond(HTTPStatus.OK, (PAGE_DIR / "payoffer.js").read_bytes(), "application/javascript; charset=utf-8")
                return
            if parsed.path == "/date_input.js":
                self._respond(HTTPStatus.OK, (PAGE_DIR / "date_input.js").read_bytes(), "application/javascript; charset=utf-8")
                return
            if parsed.path in {"/style.css", "/ui/style.css"}:
                self._respond(HTTPStatus.OK, (PAYOFF_UI_DIR / "style.css").read_bytes(), "text/css; charset=utf-8")
                return
            if parsed.path in {"/controls.css", "/ui/controls.css"}:
                self._respond(HTTPStatus.OK, (PAYOFF_UI_DIR / "controls.css").read_bytes(), "text/css; charset=utf-8")
                return
            if parsed.path == "/api/status":
                self._json(HTTPStatus.OK, {"ok": True, "module": "payoffer", "engine": "python_registry_dsl", "port": PORT})
                return
            if parsed.path == "/api/catalog":
                self._json(HTTPStatus.OK, catalog_payload())
                return
            if parsed.path == "/api/preview.svg":
                product_id = parse_qs(parsed.query).get("product_id", [""])[0]
                name_zh, _, _ = _preview_request({"product_id": product_id})
                payload = preview_payload(name_zh)
                self._respond(HTTPStatus.OK, _preview_svg(name_zh, payload).encode("utf-8"), "image/svg+xml; charset=utf-8")
                return
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "未找到接口"})
        except PayoffEngineError as error:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"ok": False, "message": str(error)})
        except Exception as error:  # pragma: no cover - unexpected service errors
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "message": str(error)})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            body = self._request_json()
            if parsed.path == "/api/preview":
                name_zh, term_overrides, identity = _preview_request(body)
                payload = preview_payload(name_zh, term_overrides, identity)
                self._json(HTTPStatus.OK, {"ok": True, "preview": payload, "svg": _preview_svg(name_zh, payload)})
                return
            if parsed.path == "/api/run":
                name_zh, term_overrides, identity = _preview_request(body)
                result = run_runtime(
                    name_zh,
                    term_overrides,
                    identity,
                    str(body.get("task_id", "")),
                    str(body.get("run_id", "")),
                )
                self._json(HTTPStatus.OK, result)
                return
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "未找到接口"})
        except PayoffEngineError as error:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"ok": False, "message": str(error)})
        except Exception as error:  # pragma: no cover - unexpected service errors
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "message": str(error)})


def run_host(*, host: str = HOST, port: int = PORT) -> None:
    """供core.module_host调用；不改变原有页面API。"""
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Payoffer页面已启动：http://{host}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    run_host(host=HOST, port=PORT)


if __name__ == "__main__":
    main()
