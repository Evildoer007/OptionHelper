"""Python版Payoff模块的本机服务。"""

from __future__ import annotations

import json
import math
import os
from hmac import compare_digest
from hashlib import sha256
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from secrets import token_urlsafe
from threading import Lock
from time import monotonic
from typing import Any, Mapping
from urllib.parse import urlparse
from uuid import uuid4

from runtime.bootstrap import bootstrap_runtime
from runtime.contracts.contract_api import (
    ContractResolutionError,
    RESOLVED_CONTRACT_SCHEMA_ID,
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
    payoff_fixed_term_fields,
    preview_result,
    render_payoff as render_payoff,
    run_payoff,
    run_runtime,
)
from .impl.svg_renderer import normalize_render_options
from .models import PayoffInput, PayoffResult


MODULE_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_PATHS = bootstrap_runtime(MODULE_ROOT)
PAGE_DIR = RUNTIME_PATHS.module_page_dir("payoffer")
PAGE = PAGE_DIR / "payoffer.html"
PAYOFF_UI_DIR = PAGE_DIR / "ui"
BROWSER_DIR = RUNTIME_PATHS.project_root / "core" / "src" / "runtime" / "browser"
BRAND_ASSET_DIR = RUNTIME_PATHS.project_root / "assets" / "icons"
DESIGNER_TOKEN_CSS = RUNTIME_PATHS.project_root / (
    "assets/designer/themes/designer-token-vars.css"
    if RUNTIME_PATHS.mode == "release"
    else "modules/designer/assets/themes/designer-token-vars.css"
)
PORT = int(os.environ.get(PORT_ENV, str(DEFAULT_PORT)))
_MAINTENANCE_COOKIE = "OptionHelperPayofferMaintenance"
_MAINTENANCE_TOKEN = token_urlsafe(32)
_MAINTENANCE_PLAN_TTL_SECONDS = 600.0
_MAINTENANCE_PLAN_LOCK = Lock()
_MAINTENANCE_PLANS: dict[str, dict[str, Any]] = {}
_FILE_PREVIEW_API_PATHS = frozenset({
    "/api/status",
    "/api/catalog",
    "/api/default",
    "/api/preview",
    "/api/run",
})
_HOSTED_EDIT_REQUEST_FIELDS = (
    "product_id",
    "underlyings",
    "term_overrides",
    "base_contract_ref",
)


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
        "path_summaries": _path_summaries(product),
        "default_asset": {"json_hash": json_hash, "svg_hash": svg_hash},
    }


def _path_summaries(product: Mapping[str, Any]) -> list[dict[str, str]]:
    """仅投影左栏需要的路径名称与条件，不暴露收益表达式。"""
    summaries: list[dict[str, str]] = []
    for index, path in enumerate(product.get("paths", ()), start=1):
        condition = str(path.get("condition", "")).strip()
        summaries.append({
            "title": f"路径{index}",
            "condition": "全部情形" if not condition or condition.casefold() == "true" else condition,
        })
    return summaries


def _maintenance_changes(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "changed_fields": list(update["changed_fields"]),
            "before_json_hash": update["before_json_hash"],
            "before_svg_hash": update["before_svg_hash"],
            "after_json_hash": sha256(update["content"]).hexdigest(),
            "after_svg_hash": sha256(update["svg_content"]).hexdigest(),
        }
        for update in updates
    ]


def _maintenance_plan_digest(product_id: str, reason: str, changes: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        {"product_id": product_id, "reason": reason, "changes": changes},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _planned_updates(manager: Any, product_id: str, reason: str) -> list[dict[str, Any]]:
    try:
        updates = manager.plan_default_asset_refresh(
            approved_product_id=product_id,
            approval_reason=reason,
        )
    except manager.DefaultAssetMaintenanceError as error:
        raise PayoffEngineError(str(error)) from error
    selected = [update for update in updates if update["product_id"] == product_id]
    if updates and len(selected) != len(updates):
        raise PayoffEngineError("检测到其他产品的默认资产差异；请逐产品完成审核")
    return selected


def default_maintenance_plan(product_id: object, reason: object) -> dict[str, Any]:
    identifier, product = _product_for_request(product_id)
    confirmed_reason = str(reason or "").strip()
    manager = _maintenance_manager()
    selected = _planned_updates(manager, identifier, confirmed_reason)
    changes = _maintenance_changes(selected)
    approval_id = f"payoffer-default-{uuid4().hex}"
    if changes:
        with _MAINTENANCE_PLAN_LOCK:
            now = monotonic()
            expired = [key for key, value in _MAINTENANCE_PLANS.items() if value["expires_at"] <= now]
            for key in expired:
                _MAINTENANCE_PLANS.pop(key, None)
            _MAINTENANCE_PLANS[approval_id] = {
                "product_id": identifier,
                "reason": confirmed_reason,
                "digest": _maintenance_plan_digest(identifier, confirmed_reason, changes),
                "expires_at": now + _MAINTENANCE_PLAN_TTL_SECONDS,
            }
    return {
        "ok": True,
        "module": "payoffer",
        "product_id": identifier,
        "name_zh": str(product["identity"]["name_zh"]),
        "maintenance_enabled": True,
        "approval_id": approval_id,
        "has_changes": bool(selected),
        "changes": changes,
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
    selected = _planned_updates(manager, identifier, reason)
    changes = _maintenance_changes(selected)
    current_digest = _maintenance_plan_digest(identifier, reason, changes)
    with _MAINTENANCE_PLAN_LOCK:
        plan = _MAINTENANCE_PLANS.get(approval_id)
        if (
            not isinstance(plan, Mapping)
            or plan.get("expires_at", 0) <= monotonic()
            or plan.get("product_id") != identifier
            or plan.get("reason") != reason
            or plan.get("digest") != current_digest
        ):
            raise PayoffEngineError("默认图维护计划不存在、已过期或与当前产品及资产不一致；请重新生成计划")
        _MAINTENANCE_PLANS.pop(approval_id, None)
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
        observation_price_options: list[dict[str, str]] = []
        if "observation_price" in product["terms"]:
            # This is deliberately a product capability, not the broad
            # OptionReg enum.  The current runtime accepts only close; adding
            # another observation price requires an explicit engine capability
            # change before it can appear in the page.
            if product["terms"]["observation_price"] != "close":
                raise PayoffEngineError(
                    f"{product_id}的观察价为未支持值{product['terms']['observation_price']}；当前Payoffer仅支持close"
                )
            observation_price_options.append({"value": "close", "label": "收盘价"})
        development_identity = _development_preview_identity(product)
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
                "path_summaries": _path_summaries(product),
                "payoff_fields": fields,
                "fixed_fields": payoff_fixed_term_fields(product, registry),
                # Empty Desk tasks do not yet have a Host task_contract from
                # which the page can obtain its edit schema.  Publish the
                # same Core-derived payoff fields with the only formal
                # Operation request shape, so the page can collect a first
                # contract request without exposing identity or rule terms.
                "edit_schema": {
                    "request_fields": list(_HOSTED_EDIT_REQUEST_FIELDS),
                    "overridable_term_keys": [field["key"] for field in fields],
                },
                "observation_price_options": observation_price_options,
                # This identity is a labelled local-development fixture.  It
                # is intentionally absent from Host/Desk runtime requests.
                "development_preview_identity": development_identity,
            }
        )
    return {
        "ok": True,
        "module": "payoffer",
        "products": products,
    }


def _preview_response(
    name_zh: str,
    term_overrides: Mapping[str, Any] | None,
    identity: Mapping[str, Any] | None,
    render_options: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """消费引擎同一次计算产出的payload与SVG，服务层绝不重复渲染。"""
    return _rendered_preview_response(preview_result(name_zh, term_overrides, identity, render_options))


def _rendered_preview_response(result: Any) -> tuple[dict[str, Any], str]:
    """将一次引擎渲染结果投影为页面预览，禁止二次计算。"""
    if not isinstance(result.payload, Mapping):
        raise PayoffEngineError("Payoffer预览引擎未返回有效payload")
    payload = dict(result.payload)
    if not isinstance(result.svg, str) or "<svg" not in result.svg:
        raise PayoffEngineError("Payoffer预览引擎未返回本次计算或只读默认的SVG")
    return payload, result.svg


def _preview_request(
    body: Mapping[str, Any],
) -> tuple[str, str, Mapping[str, Any], Mapping[str, Any] | None, Mapping[str, Any] | None, bool]:
    """开发预览只接受当前页面的product_id、term_overrides与identity输入。"""
    allowed = {
        "product_id", "term_overrides", "identity", "task_id", "run_id",
        "base_contract_ref", "development_preview", "render_options",
    }
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
    development_preview = body.get("development_preview", False)
    if not isinstance(development_preview, bool):
        raise PayoffEngineError("development_preview必须是布尔值")
    return product_id, name_zh, product, term_overrides, supplied_identity, development_preview


def _render_options(body: Mapping[str, Any]) -> dict[str, int] | None:
    if "render_options" not in body:
        return None
    try:
        return normalize_render_options(body.get("render_options"))
    except ValueError as error:
        raise PayoffEngineError(str(error)) from error


def _require_host_market_identity(
    identity: Mapping[str, Any] | None,
    *,
    product_id: str,
) -> None:
    """Desk预览只能消费Host绑定的真实标的与参考价。"""
    if not isinstance(identity, Mapping):
        raise PayoffEngineError("Desk Runtime预览必须由App Host提供真实标的与参考价")
    underlyings = identity.get("underlyings")
    references = identity.get("reference_prices")
    if not isinstance(underlyings, (list, tuple)) or not underlyings or not isinstance(references, Mapping):
        raise PayoffEngineError("Desk Runtime预览必须提供逐标的真实价格参考")
    assets = [str(value).strip() for value in underlyings]
    placeholder_prefixes = ("UNDERLYING", "DEFAULT.UNDERLYING", "LOCAL_DEVELOPMENT_ASSET")
    if any(
        not value or any(value == prefix or value.startswith(f"{prefix}_") for prefix in placeholder_prefixes)
        for value in assets
    ):
        raise PayoffEngineError("Desk Runtime预览拒绝开发占位标的身份")
    if set(references) != set(assets):
        raise PayoffEngineError("Desk Runtime预览的价格参考必须逐一覆盖真实标的")
    try:
        prices = [float(references[asset]) for asset in assets]
    except (TypeError, ValueError) as error:
        raise PayoffEngineError("Desk Runtime预览的价格参考必须为正数") from error
    if any(not math.isfinite(price) or price <= 0 for price in prices):
        raise PayoffEngineError("Desk Runtime预览的价格参考必须为正数")
    if str(identity.get("product_id", "")).strip() != product_id:
        raise PayoffEngineError("Desk Runtime预览的产品必须与Host绑定合同一致")


def _hosted_preview_input(body: Mapping[str, Any], host_context: ModuleHostContext) -> PayoffInput:
    """恢复Host受控合同并核验不可由页面替换的合同绑定。"""
    if set(body) not in ({"product_id", "resolved_contract"}, {"product_id", "resolved_contract", "render_options"}):
        raise PayoffEngineError("Desk Runtime预览只接受product_id、Host注入的resolved_contract与图片尺寸")
    if host_context.module != "payoffer" or "module.catalog" not in host_context.request_policy:
        raise PayoffEngineError("Desk Runtime预览必须由Host授权payoffer module.catalog")
    required = {
        "candidate_id": host_context.candidate_id,
        "catalog_version": host_context.catalog_version,
        "contract_fingerprint": host_context.contract_fingerprint,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise PayoffEngineError(f"Desk Runtime预览的Host上下文缺少：{','.join(missing)}")
    contract_ref = host_context.contract_ref
    if contract_ref is None or contract_ref.schema_id != RESOLVED_CONTRACT_SCHEMA_ID:
        raise PayoffEngineError("Desk Runtime预览缺少当前ResolvedContract引用")
    payoff_input = _formal_payoff_input({"contract": body["resolved_contract"]})
    contract = payoff_input.contract
    product_id = str(body["product_id"]).strip()
    if not product_id or contract.product_id != product_id:
        raise PayoffEngineError("Desk Runtime预览的产品与Host受控合同不一致")
    if (
        host_context.contract_fingerprint != contract.contract_fingerprint
        or contract_ref.content_hash != contract.contract_fingerprint
    ):
        raise PayoffEngineError("Desk Runtime预览的Host合同引用与ResolvedContract不一致")
    return payoff_input


def _require_development_preview(development_preview: bool, action: str) -> None:
    if not development_preview:
        raise PayoffEngineError(f"独立Payoffer{action}必须显式声明development_preview=true")


def _development_preview_identity(product: Mapping[str, Any]) -> dict[str, Any]:
    assets = (
        [f"LOCAL_DEVELOPMENT_ASSET_{index + 1}" for index, _ in enumerate(product["terms"]["S0Vec"])]
        if "S0Vec" in product["terms"]
        else ["LOCAL_DEVELOPMENT_ASSET"]
    )
    prices = (
        [float(value) for value in product["terms"]["S0Vec"]]
        if "S0Vec" in product["terms"]
        else [float(product["terms"].get("S0", 100.0))]
    )
    return {"underlyings": assets, "reference_prices": dict(zip(assets, prices))}


def _require_local_development_identity(
    identity: Mapping[str, Any] | None,
    product: Mapping[str, Any],
) -> None:
    if not isinstance(identity, Mapping):
        raise PayoffEngineError("独立开发预览必须提供Catalog下发的LOCAL_DEVELOPMENT_ASSET身份")
    underlyings = identity.get("underlyings")
    references = identity.get("reference_prices")
    if not isinstance(underlyings, (list, tuple)) or not underlyings or not isinstance(references, Mapping):
        raise PayoffEngineError("独立开发预览必须提供LOCAL_DEVELOPMENT_ASSET及逐标的参考价")
    assets = [str(value).strip() for value in underlyings]
    try:
        prices = [float(references[asset]) for asset in assets]
    except (KeyError, TypeError, ValueError) as error:
        raise PayoffEngineError("独立开发预览的参考价必须为正数") from error
    if any(not math.isfinite(price) or price <= 0 for price in prices):
        raise PayoffEngineError("独立开发预览的参考价必须为正数")
    expected = _development_preview_identity(product)
    if assets != expected["underlyings"] or dict(references) != expected["reference_prices"]:
        raise PayoffEngineError("独立开发预览身份必须与当前产品Catalog下发值完全一致")


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
    if action == "default":
        return default_example_payload(body.get("product_id"))
    if action == "preview":
        if isinstance(host_context, ModuleHostContext):
            payoff_input = _hosted_preview_input(body, host_context)
            rendered = render_payoff(payoff_input, _render_options(body))
            payload, svg = _rendered_preview_response(PayoffResult(
                payload={**dict(rendered.payload), "runtime_status": "enabled"},
                svg=rendered.svg,
            ))
        else:
            product_id, name_zh, product, term_overrides, identity, development_preview = _preview_request(body)
            _require_development_preview(development_preview, "预览")
            _require_local_development_identity(identity, product)
            payload, svg = _preview_response(name_zh, term_overrides, identity, _render_options(body))
        return {"ok": True, "module": "payoffer", "preview": payload, "svg": svg}
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
            render_options=_render_options(body),
        )
    return {
        "ok": False,
        "module": "payoffer",
        "status": "unsupported",
        "message": f"Payoffer不支持action={action}；仅支持catalog、default、preview、run。",
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
    try:
        _require_host_market_identity(contract.identity, product_id=contract.product_id)
    except PayoffEngineError as error:
        raise PayoffEngineError(f"正式Payoffer合同身份无效：{error}") from error
    return PayoffInput(contract=contract)


class Handler(BaseHTTPRequestHandler):
    server_version = "OptionHelperPayoff"

    def log_message(self, format: str, *args: object) -> None:
        print(f"[Payoff] {format % args}")

    def _respond(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        response_headers = dict(self._file_preview_cors_headers())
        response_headers.update(headers or {})
        for name, value in response_headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(
        self,
        status: int,
        payload: object,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise PayoffEngineError("Payoffer公开JSON包含不安全或不可序列化的值") from error
        self._respond(status, encoded, "application/json; charset=utf-8", headers=headers)

    def _file_preview_cors_headers(self) -> dict[str, str]:
        """仅允许保留的file://开发页面访问普通预览接口。"""
        if self.headers.get("Origin") != "null":
            return {}
        if urlparse(self.path).path not in _FILE_PREVIEW_API_PATHS:
            return {}
        return {"Access-Control-Allow-Origin": "null", "Vary": "Origin"}

    def _maintenance_request_authorized(self) -> bool:
        """维护写入口只接受当前本机页面建立的同源会话。"""
        try:
            if not ip_address(self.client_address[0]).is_loopback:
                return False
        except ValueError:
            return False
        origin = self.headers.get("Origin", "")
        port = self.server.server_address[1]
        if origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            return False
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        token = cookie.get(_MAINTENANCE_COOKIE)
        return token is not None and compare_digest(token.value, _MAINTENANCE_TOKEN)

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
        if self._file_preview_cors_headers():
            self._respond(
                HTTPStatus.NO_CONTENT,
                b"",
                "text/plain; charset=utf-8",
                headers={
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                    "Access-Control-Max-Age": "600",
                },
            )
            return
        self._json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "不允许跨源访问本机Payoffer服务"})

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
            if parsed.path == "/date-input-control.js":
                self._respond(HTTPStatus.OK, (BROWSER_DIR / "date_input_control.js").read_bytes(), "application/javascript; charset=utf-8")
                return
            if parsed.path == "/date-input-control.css":
                self._respond(HTTPStatus.OK, (BROWSER_DIR / "date_input_control.css").read_bytes(), "text/css; charset=utf-8")
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
                maintenance_enabled = _maintenance_enabled()
                response_headers = None
                if maintenance_enabled:
                    response_headers = {
                        "Set-Cookie": (
                            f"{_MAINTENANCE_COOKIE}={_MAINTENANCE_TOKEN}; "
                            "HttpOnly; SameSite=Strict; Path=/api/default-maintenance"
                        )
                    }
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "module": "payoffer",
                        "engine": "python_registry_dsl",
                        "port": PORT,
                        "default_maintenance_enabled": maintenance_enabled,
                    },
                    headers=response_headers,
                )
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
            if parsed.path.startswith("/api/default-maintenance/") and not self._maintenance_request_authorized():
                self._json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "默认资产维护授权无效"})
                return
            body = self._request_json()
            if parsed.path == "/api/preview":
                _, name_zh, product, term_overrides, identity, development_preview = _preview_request(body)
                _require_development_preview(development_preview, "预览")
                _require_local_development_identity(identity, product)
                payload, svg = _preview_response(name_zh, term_overrides, identity, _render_options(body))
                self._json(HTTPStatus.OK, {"ok": True, "preview": payload, "svg": svg})
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
                _, name_zh, product, term_overrides, identity, development_preview = _preview_request(body)
                _require_development_preview(development_preview, "运行")
                _require_local_development_identity(identity, product)
                result = run_runtime(
                    name_zh,
                    term_overrides,
                    identity,
                    str(body.get("task_id", "")),
                    str(body.get("run_id", "")),
                    _render_options(body),
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
