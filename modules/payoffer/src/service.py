"""Python版Payoff模块的本机服务。"""

from __future__ import annotations

import json
import os
from hashlib import sha256
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from uuid import uuid4

from runtime.bootstrap import bootstrap_runtime
from runtime.contracts.contract_api import (
    ContractResolutionError,
    ResolvedContract,
)
from runtime.ports.result_store import ResultStorePort
from runtime.protocol.module_host import ModuleHostContext

from .asset_resolver import DefaultAssetError, figure_asset_paths, verify_contract_snapshot_binding
from .config import DEFAULT_MAINTENANCE_ENV, DEFAULT_PORT, HOST, PORT_ENV
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
DESIGNER_TOKEN_CSS = RUNTIME_PATHS.project_root / (
    "assets/designer/themes/designer-token-vars.css"
    if RUNTIME_PATHS.mode == "release"
    else "modules/designer/assets/themes/designer-token-vars.css"
)
PORT = int(os.environ.get(PORT_ENV, str(DEFAULT_PORT)))


def _product_for_request(product_id: object) -> tuple[str, dict[str, Any]]:
    identifier = str(product_id or "").strip()
    product = load_registry().get("products", {}).get(identifier)
    if not isinstance(product, dict):
        raise PayoffEngineError("必须提供有效的product_id")
    return identifier, product


def _maintenance_enabled() -> bool:
    """默认资产写权限只在开发仓库的显式管理员维护进程中存在。"""
    return RUNTIME_PATHS.mode == "development" and os.environ.get(DEFAULT_MAINTENANCE_ENV, "").strip() == "1"


def _maintenance_manager():
    if not _maintenance_enabled():
        raise PayoffEngineError("默认资产维护未启用；请从Desk管理员高级操作进入")
    from modules.payoffer.maintenance import default_asset_manager

    return default_asset_manager


def default_example_payload(product_id: object) -> dict[str, Any]:
    """读取固定默认SVG；该路径不构造本次合同，也不写任何资产。"""
    identifier, product = _product_for_request(product_id)
    name_zh = str(product["identity"]["name_zh"])
    paths = figure_asset_paths(name_zh)
    try:
        svg = paths["svg"].read_text(encoding="utf-8")
        svg_hash = sha256(paths["svg"].read_bytes()).hexdigest()
        json_hash = sha256(paths["json"].read_bytes()).hexdigest()
    except OSError as error:
        raise PayoffEngineError(f"缺少{name_zh}的固定默认资产") from error
    return {
        "ok": True,
        "module": "payoffer",
        "view_mode": "default_example",
        "read_only": True,
        "product_id": identifier,
        "name_zh": name_zh,
        "svg": svg,
        "default_asset": {"json_hash": json_hash, "svg_hash": svg_hash},
    }


def default_maintenance_plan(product_id: object, reason: object) -> dict[str, Any]:
    identifier, product = _product_for_request(product_id)
    confirmed_reason = str(reason or "").strip()
    manager = _maintenance_manager()
    try:
        updates = manager.plan_default_asset_refresh(
            approved_product_id=identifier,
            approval_reason=confirmed_reason,
        )
    except manager.DefaultAssetMaintenanceError as error:
        raise PayoffEngineError(str(error)) from error
    selected = [update for update in updates if update["product_id"] == identifier]
    if updates and len(selected) != len(updates):
        raise PayoffEngineError("检测到其他产品的默认资产差异；请逐产品完成审核")
    return {
        "ok": True,
        "module": "payoffer",
        "product_id": identifier,
        "name_zh": str(product["identity"]["name_zh"]),
        "maintenance_enabled": True,
        "has_changes": bool(selected),
        "changes": [
            {
                "changed_fields": list(update["changed_fields"]),
                "before_json_hash": update["before_json_hash"],
                "before_svg_hash": update["before_svg_hash"],
                "after_json_hash": sha256(update["content"]).hexdigest(),
                "after_svg_hash": sha256(update["svg_content"]).hexdigest(),
            }
            for update in selected
        ],
        "confirmation_text": f"确认更新默认示例：{product['identity']['name_zh']}",
    }


def publish_default_example(body: Mapping[str, Any]) -> dict[str, Any]:
    identifier, product = _product_for_request(body.get("product_id"))
    reason = str(body.get("reason", "")).strip()
    approval_id = str(body.get("approval_id", "")).strip()
    expected = f"确认更新默认示例：{product['identity']['name_zh']}"
    if str(body.get("confirmation", "")).strip() != expected:
        raise PayoffEngineError(f"二次确认文字不一致；请输入：{expected}")
    manager = _maintenance_manager()
    try:
        record = manager.publish_default_assets(
            approval_id,
            approved_product_id=identifier,
            approval_reason=reason,
        )
    except manager.DefaultAssetMaintenanceError as error:
        raise PayoffEngineError(str(error)) from error
    return {
        "ok": True,
        "module": "payoffer",
        "status": "default_example_updated",
        "product_id": identifier,
        "approval_id": record["approval_id"],
        "updated_assets": len(record["updates"]),
    }


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
    action = str(body.pop("action", "catalog")).strip().lower()
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
            if parsed.path == "/date_input.js":
                self._respond(HTTPStatus.OK, (PAGE_DIR / "date_input.js").read_bytes(), "application/javascript; charset=utf-8")
                return
            if parsed.path == "/ui/style.css":
                self._respond(HTTPStatus.OK, (PAYOFF_UI_DIR / "style.css").read_bytes(), "text/css; charset=utf-8")
                return
            if parsed.path == "/ui/controls.css":
                self._respond(HTTPStatus.OK, (PAYOFF_UI_DIR / "controls.css").read_bytes(), "text/css; charset=utf-8")
                return
            if parsed.path == "/designer/themes/designer-token-vars.css":
                self._respond(HTTPStatus.OK, DESIGNER_TOKEN_CSS.read_bytes(), "text/css; charset=utf-8")
                return
            if parsed.path == "/api/status":
                self._json(HTTPStatus.OK, {
                    "ok": True,
                    "module": "payoffer",
                    "engine": "python_registry_dsl",
                    "port": PORT,
                    "default_maintenance_enabled": _maintenance_enabled(),
                })
                return
            if parsed.path == "/api/catalog":
                self._json(HTTPStatus.OK, catalog_payload())
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
            if parsed.path == "/api/default":
                self._json(HTTPStatus.OK, default_example_payload(body.get("product_id")))
                return
            if parsed.path == "/api/default-maintenance/plan":
                self._json(
                    HTTPStatus.OK,
                    default_maintenance_plan(body.get("product_id"), body.get("reason")),
                )
                return
            if parsed.path == "/api/default-maintenance/publish":
                self._json(HTTPStatus.OK, publish_default_example(body))
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
