"""Pricer人工页面的独立本机服务。

本文件只处理Pricer页面、Pricer输入和`output_pricing`运行记录；不导入
Backtester或Payoffer。产品合同与路径现金流仅通过共享Runtime Contract API读取。
"""

from __future__ import annotations

import csv
from dataclasses import asdict, replace
import hashlib
import io
import json
import os
import threading
from datetime import date, datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


from runtime.bootstrap import bootstrap_runtime
from runtime.adapters.local_store import LocalDataStore, LocalResultStore
from runtime.contracts.contract_api import (
    ContractResolutionError,
    ResolvedContract,
    resolve_contract,
    verify_product_snapshot_binding,
)
from runtime.contracts.contract_types import semantic_hash
from runtime.contracts.input_adapter import is_explicit_demo_pricing_config
from runtime.knowledger import load_registry
from runtime.protocol.models import DataAssetRef, PricingInput as ProtocolPricingInput
from runtime.protocol.module_host import ModuleHostContext, require_host_bound_run_contract

from .config import PricingConfig
from .models import HistoricalData, PricingInput, TradingCalendarData, validate_market_data_asset
from .engines.pricing_core.optionhelper_core import capability_for
from .product_pricing_adapter import product_mapping
from .valuation_solver import price
from .input_defaults import (
    PricerInputDefaultError,
    local_valuation_date,
    validate_frozen_contract_reference,
    with_data_backed_reference,
)
from .market_resolver import load_market_history, load_market_history_bytes


MODULE_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_PATHS = bootstrap_runtime(MODULE_ROOT)
PROJECT_ROOT = RUNTIME_PATHS.project_root
RESULT_ROOT = RUNTIME_PATHS.result_root
PAGE_DIR = RUNTIME_PATHS.module_page_dir("pricer")
PAGE = PAGE_DIR / "pricer.html"
UI_DIR = PAGE_DIR / "ui"
VENDOR_DIR = PAGE_DIR / "vendor"
ICON_DIR = PROJECT_ROOT / "assets" / "icons"
HOST = "127.0.0.1"
PORT = int(os.environ.get("OPTIONHELPER_PRICER_PORT", "4280"))
MAX_BODY_BYTES = 1_500_000
WEB_UI_VERSION = "2026.08.04.1"
RESULT_TABLE_NAME = "pricing_results.csv"
RESULT_TABLE_LOCK = threading.Lock()


class PricerWebInputError(ValueError):
    """Pricer页面请求未满足合同或定价输入边界。"""


def static_assets() -> dict[str, tuple[Path, str]]:
    """页面在开发仓库和发行Skill均使用的根资源路径。"""
    return {
        "/icons/optionhelper-logo.svg": (ICON_DIR / "optionhelper-logo.svg", "image/svg+xml"),
        "/assets/icons/optionhelper-logo.svg": (ICON_DIR / "optionhelper-logo.svg", "image/svg+xml"),
        "/icons/optionhelper-app-icon-tile-light.svg": (ICON_DIR / "optionhelper-app-icon-tile-light.svg", "image/svg+xml"),
        "/assets/icons/optionhelper-app-icon-tile-light.svg": (ICON_DIR / "optionhelper-app-icon-tile-light.svg", "image/svg+xml"),
        "/ui/style.css": (UI_DIR / "style.css", "text/css; charset=utf-8"),
        "/ui/controls.css": (UI_DIR / "controls.css", "text/css; charset=utf-8"),
        "/ui/date-control.js": (UI_DIR / "date-control.js", "application/javascript; charset=utf-8"),
        "/vendor/echarts.min.js": (VENDOR_DIR / "echarts.min.js", "application/javascript; charset=utf-8"),
    }


class PricerRuntime:
    """Pricer页面的目录读取、输入编译、定价和结果落盘。"""

    module_name = "pricer"

    def __init__(self, *, data_port: Any | None = None, result_store: Any | None = None) -> None:
        # The Host may inject a DataStorePort.  Local development uses the
        # same controlled DataStore root, never a DataFetcher checkout path.
        self._host_data_port = data_port is not None
        self.data_port = data_port or LocalDataStore(RUNTIME_PATHS.data_root)
        self.result_store = result_store or LocalResultStore(RESULT_ROOT)

    def catalog(self) -> dict[str, Any]:
        registry = load_registry()
        catalog = registry["term_catalog"]
        products: list[dict[str, Any]] = []
        for product_id, product in registry["products"].items():
            terms = product["terms"]
            capability = capability_for(product_id)
            mapping = product_mapping(product_id)
            fields = []
            for key, value in terms.items():
                if key in {"monitor", "pricing_methods", "constraints", "derived_terms"}:
                    continue
                metadata = catalog[key]
                fields.append({
                    "key": key,
                    "label": metadata["name_zh"],
                    "symbol": metadata["symbol"],
                    "value_type": metadata["value_type"],
                    "unit": metadata["unit"],
                    "domain": metadata["domain"],
                    "default_value": value,
                })
            products.append({
                "product_id": product_id,
                "canonical_name": product["identity"]["name_zh"],
                "entry_status": product["identity"]["entry_status"],
                "underlying_scope": "multi_underlying" if "S0Vec" in terms else "single_underlying",
                "payoff_fields": fields,
                "pricing_methods": terms["pricing_methods"],
                "pricer_status": "supported" if mapping.status == "supported" else "unsupported",
                "pricer_availability": mapping.status,
                "pricer_methods": list(capability.methods) if capability else [],
                "pricer_structure": "discrete_path_monte_carlo" if mapping.structure == "OPTIONREG_PATH" else mapping.structure.casefold(),
                "pricer_family": mapping.family,
                "pricer_reason": mapping.reason,
            })
        return {
            "ok": True,
            "module": self.module_name,
            "products": products,
            "config_fields": list(PricingConfig.__dataclass_fields__),
        }

    def run(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """页面适配入口：只在人工页面将product_id解析为冻结合同。"""
        product_id = _text(body.get("product_id"), "product_id")
        config = _with_default_valuation_date(_pricing_config(body))
        explicit_demo = (
            product_id == "2.1"
            and _history_reference(body) is None
            and is_explicit_demo_pricing_config(config.to_dict())
        )
        identity = _identity_with_pricing_reference(_identity(body), body)
        historical_data: HistoricalData | None = None
        data_ref: DataAssetRef | None = None
        coverage: Mapping[str, Any] | None = None
        if not explicit_demo:
            reference = _history_reference(body)
            if reference is None:
                raise PricerWebInputError("请先绑定真实DataAssetRef；绑定行情后自动填充S₀Raw。")
            history_path, data_ref = self._history_asset(_history_reference(body))
            history = load_market_history(history_path)
            try:
                identity = with_data_backed_reference(
                    identity, history, valuation_date=str(config.valuation_date),
                )
            except PricerInputDefaultError as error:
                raise PricerWebInputError(str(error)) from error
            historical_data = HistoricalData(
                source_ref=data_ref.storage_ref,
                rows=tuple(history.to_dict(orient="records")),
                content_hash=data_ref.content_hash,
                schema_id=data_ref.schema_id,
                asset_ids=tuple(data_ref.asset_ids),
                normalized_fields=tuple(data_ref.normalized_fields),
                coverage=dict(data_ref.coverage),
                storage_mode="local-development" if data_ref.created_by == "local-development" else "host-injected",
            )
            try:
                validate_market_data_asset(data_ref, historical_data, tuple(identity["underlyings"]))
            except ValueError as error:
                raise PricerWebInputError(str(error)) from error
            coverage = dict(data_ref.coverage)
            identity = {
                **identity,
                "calendar_id": coverage["calendar_id"],
                "calendar_version": coverage["calendar_version"],
            }
        elif "contract_reference_spots" not in identity:
            raise PricerWebInputError("MC10演示缺少显式demo spot对应的合同起始参考价")
        contract = resolve_contract(
            product_id,
            identity=identity,
            term_overrides=_term_overrides(body),
            registry=load_registry(),
            trading_dates=None if coverage is None else coverage["sessions"],
        )
        task_id = _identifier(body.get("task_id"), "task")
        run_id = _identifier(body.get("run_id"), "run")
        try:
            result = price(PricingInput(
                contract=contract,
                pricing_config=config,
                historical_data=historical_data,
                market_data_refs=() if data_ref is None else (data_ref,),
                observed_contract_state=body.get("observed_contract_state"),
            ))
        except (TypeError, ValueError) as error:
            raise PricerWebInputError(str(error)) from error
        if explicit_demo:
            result = replace(
                result,
                market_snapshot={
                    **result.market_snapshot,
                    "source": "explicit_demo_market_snapshot",
                    "data_lineage": {
                        "mode": "explicit_demo_only",
                        "data_asset_ref": None,
                        "statement": "市场快照与估值日历均由本次MC10演示输入显式提供；未读取或推断行情数据。",
                    },
                    "trading_calendar": dict(config.demo_calendar or {}),
                },
            )
        market = result.market_snapshot
        resolved_contract = contract.to_protocol_dict()
        output = {
            "ok": True,
            "module": self.module_name,
            "task_id": task_id,
            "run_id": run_id,
            "contract_fingerprint": contract.contract_fingerprint,
            "resolved_contract": resolved_contract,
            "market_snapshot": market,
            "pricing": result.to_dict(),
        }
        request = {
            "product_id": product_id,
            "identity": identity,
            "term_overrides": _term_overrides(body),
            "pricing_config": config.to_dict(),
            "observed_contract_state": body.get("observed_contract_state"),
            "history_reference": None if data_ref is None else data_ref.storage_ref,
            "created_at": _now(),
        }
        _write_run(task_id, run_id, request, output)
        return output

    def run_formal(
        self,
        request: ProtocolPricingInput | Mapping[str, Any],
        *,
        host_context: ModuleHostContext | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """正式Tool入口：直接消费Host冻结的ResolvedContract和DataAssetRef。"""
        protocol_input, observed_state = _formal_pricing_input(request)
        contract = protocol_input.contract
        if not isinstance(contract, ResolvedContract):
            raise PricerWebInputError("正式PricingInput.contract必须为ResolvedContract")
        if not isinstance(host_context, ModuleHostContext):
            raise PricerWebInputError("正式Pricer调用必须由Host注入ModuleHostContext")
        _require_host_contract(contract, host_context)
        explicit_demo = (
            len(protocol_input.market_data_refs) == 0
            and protocol_input.trading_calendar_ref is None
            and is_explicit_demo_pricing_config(protocol_input.pricing_config)
        )
        if len(protocol_input.market_data_refs) == 0:
            if not explicit_demo:
                raise PricerWebInputError("正式Pricer当前要求唯一受控market_data_ref")
            if contract.product_id != "2.1":
                raise PricerWebInputError("无行情数据的MC10演示仅支持2.1看涨期权；离散路径结构必须绑定真实DataAssetRef与交易日历")
            historical = None
            data_ref = None
        elif len(protocol_input.market_data_refs) == 1:
            if not self._host_data_port:
                raise PricerWebInputError("正式Pricer必须由Host注入只读DataAssetRef端口")
            historical, data_ref = self._load_data_asset(protocol_input.market_data_refs[0], contract.underlyings)
        else:
            raise PricerWebInputError("正式Pricer当前要求唯一受控market_data_ref")
        calendar_data: TradingCalendarData | None = None
        calendar_ref = protocol_input.trading_calendar_ref
        if calendar_ref is not None:
            if not self._host_data_port:
                raise PricerWebInputError("正式Pricer交易日历必须由Host注入只读DataAssetRef端口")
            calendar_data, calendar_ref = self._load_calendar_asset(calendar_ref, contract.underlyings)
        try:
            config = _with_default_valuation_date(PricingConfig.from_mapping(protocol_input.pricing_config))
            if historical is not None:
                validate_frozen_contract_reference(
                    contract, historical.rows, valuation_date=str(config.valuation_date),
                )
            pricing_result = price(PricingInput(
                contract=contract,
                pricing_config=config,
                historical_data=historical,
                market_data_refs=() if data_ref is None else (data_ref,),
                trading_calendar_data=calendar_data,
                trading_calendar_ref=calendar_ref,
                observed_contract_state=observed_state,
            ))
        except (TypeError, ValueError, PricerInputDefaultError) as error:
            raise PricerWebInputError(str(error)) from error

        if explicit_demo:
            pricing_result = replace(
                pricing_result,
                market_snapshot={
                    **pricing_result.market_snapshot,
                    "source": "explicit_demo_market_snapshot",
                    "data_lineage": {
                        "mode": "explicit_demo_only",
                        "data_asset_ref": None,
                        "statement": "市场快照与估值日历均由本次MC10演示输入显式提供；未读取或推断行情数据。",
                    },
                    "trading_calendar": dict(config.demo_calendar or {}),
                },
            )

        task_id = str(host_context.task_id)
        run_id = f"run-{uuid4().hex[:12]}"
        effective_tenant = tenant_id or (
            data_ref.tenant_id if data_ref is not None else
            calendar_ref.tenant_id if calendar_ref is not None else "local"
        )
        if data_ref is not None and effective_tenant != data_ref.tenant_id:
            raise PricerWebInputError("DataAssetRef.tenant_id与Host tenant不一致")
        if calendar_ref is not None and effective_tenant != calendar_ref.tenant_id:
            raise PricerWebInputError("trading_calendar_ref.tenant_id与Host tenant不一致")
        data_refs = [] if data_ref is None else [_data_ref_dict(data_ref)]
        if calendar_ref is not None:
            data_refs.append(_data_ref_dict(calendar_ref))
        input_snapshot = {
            "contract": contract.to_protocol_dict(),
            "pricing_config": config.to_dict(),
            "market_data_refs": [] if data_ref is None else [_data_ref_dict(data_ref)],
            "trading_calendar_ref": None if calendar_ref is None else _data_ref_dict(calendar_ref),
            "observed_contract_state": observed_state,
        }
        limitations = list(pricing_result.limitations)
        if explicit_demo:
            limitations.append("MC10演示仅使用显式输入的市场快照和估值日历，未绑定DataAssetRef，不可用于报价。")
        status = "succeeded" if pricing_result.status == "priced" else "unsupported"
        execution_fingerprint = semantic_hash(input_snapshot)
        analysis_case_id = str(host_context.analysis_case_id)
        catalog_version = str(host_context.catalog_version)
        candidate_id = str(host_context.candidate_id)
        output: dict[str, Any] = {
            "ok": status == "succeeded",
            "module": "pricer",
            "status": status,
            "task_id": task_id,
            "run_id": run_id,
            "analysis_case_id": analysis_case_id,
            "tenant_id": effective_tenant,
            "created_by": data_ref.created_by if data_ref is not None else "explicit-demo",
            "access_scope": list(data_ref.access_scope) if data_ref is not None else ["read"],
            "candidate_id": candidate_id,
            "catalog_version": catalog_version,
            "execution_fingerprint": execution_fingerprint,
            "contract_fingerprint": contract.contract_fingerprint,
            "resolved_contract": contract.to_protocol_dict(),
            "data_refs": data_refs,
            "input_snapshot": input_snapshot,
            "limitations": limitations,
            "market_snapshot": pricing_result.market_snapshot,
            "pricing": pricing_result.to_dict(),
        }
        files = _module_run_files(output)
        require_host_bound_run_contract(output, host_context)
        reference = self.result_store.commit_module_run(
            module="pricer",
            tenant_id=effective_tenant,
            task_id=task_id,
            run_id=run_id,
            files=files,
        )
        run_dir = self.result_store.resolve_module_run(reference, tenant_id=effective_tenant)
        output.update({
            "module_run_ref": asdict(reference),
            "artifact_manifest": _read_json(run_dir / "artifacts" / "artifact_manifest.json"),
            "commit_marker": _read_json(run_dir / "commit_marker.json"),
        })
        return output

    def _load_calendar_asset(
        self,
        value: DataAssetRef | Mapping[str, Any],
        underlyings: tuple[str, ...],
    ) -> tuple[TradingCalendarData, DataAssetRef]:
        ref = _data_asset_ref(value)
        if ref.schema_id != "trading-calendar" or ref.media_type != "application/json":
            raise PricerWebInputError("trading_calendar_ref必须是trading-calendar application/json")
        try:
            payload = self.data_port.read_bytes(ref, tenant_id=ref.tenant_id)
        except (TypeError, ValueError, OSError, PermissionError) as error:
            raise PricerWebInputError(f"trading_calendar_ref无法由DataPort验证：{error}") from error
        if not isinstance(payload, bytes) or hashlib.sha256(payload).hexdigest() != ref.content_hash:
            raise PricerWebInputError("trading_calendar_ref.content_hash与DataPort实际内容不一致")
        try:
            document = json.loads(payload)
            if not isinstance(document, Mapping):
                raise TypeError("calendar document must be an object")
            calendar = TradingCalendarData(
                source_ref=ref.storage_ref,
                asset_ids=tuple(str(item) for item in document["asset_ids"]),
                sessions=tuple(str(item) for item in document["sessions"]),
                sessions_by_exchange={
                    str(key): tuple(str(item) for item in values)
                    for key, values in dict(document["sessions_by_exchange"]).items()
                },
                asset_exchange={str(key): str(item) for key, item in dict(document["asset_exchange"]).items()},
                requested_start_date=str(document["requested_start_date"]),
                requested_end_date=str(document["requested_end_date"]),
                content_hash=ref.content_hash,
                schema_id=str(document.get("schema_id", "")),
                storage_mode="host-injected",
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise PricerWebInputError(f"trading-calendar内容无效：{error}") from error
        if set(calendar.asset_ids) != set(underlyings):
            raise PricerWebInputError("交易日历未逐一覆盖ResolvedContract标的")
        return calendar, ref

    def _load_data_asset(
        self,
        value: DataAssetRef | Mapping[str, Any],
        underlyings: tuple[str, ...],
    ) -> tuple[HistoricalData, DataAssetRef]:
        """DataAssetRef的唯一loader：在此解析别名、验证Store和加载行情。"""
        ref = _data_asset_ref(value)
        try:
            payload = self.data_port.read_bytes(ref, tenant_id=ref.tenant_id)
        except (TypeError, ValueError, OSError, PermissionError) as error:
            raise PricerWebInputError(f"DataAssetRef无法由DataPort验证：{error}") from error
        if not isinstance(payload, bytes):
            raise PricerWebInputError("DataPort必须返回DataAssetRef原始字节")
        if hashlib.sha256(payload).hexdigest() != ref.content_hash:
            raise PricerWebInputError("DataAssetRef.content_hash与DataPort实际内容不一致")
        history = load_market_history_bytes(payload)
        adjustment = _asset_adjustment(ref)
        historical = HistoricalData(
            source_ref=ref.storage_ref,
            rows=tuple(history.to_dict(orient="records")),
            content_hash=ref.content_hash,
            schema_id=ref.schema_id,
            asset_ids=tuple(ref.asset_ids),
            normalized_fields=tuple(ref.normalized_fields),
            coverage=dict(ref.coverage),
            storage_mode="host-injected",
        )
        try:
            validate_market_data_asset(ref, historical, underlyings)
        except ValueError as error:
            raise PricerWebInputError(str(error)) from error
        if adjustment not in {"none", "forward", "backward", "both", "close_and_adj_close"}:
            raise PricerWebInputError("DataAssetRef.adjustment口径无效")
        return historical, ref

    def _history_asset(self, value: Any) -> tuple[Path, DataAssetRef]:
        if isinstance(value, Mapping):
            try:
                ref = DataAssetRef(**dict(value))
                path = self.data_port.resolve(ref, tenant_id=ref.tenant_id)
            except (TypeError, ValueError, OSError, PermissionError) as error:
                raise PricerWebInputError(f"DataAssetRef无效：{error}") from error
            if not path.is_file():
                raise PricerWebInputError("DataAssetRef解析结果不是文件")
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual_hash != ref.content_hash:
                raise PricerWebInputError("DataAssetRef.content_hash与DataPort实际内容不一致")
            return path, ref
        path = _history_path(value)
        payload_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        history = load_market_history(path)
        sessions = tuple(sorted(history["date"].dt.strftime("%Y-%m-%d").unique()))
        asset_ids = tuple(sorted(history["asset_id"].unique()))
        coverage = {
            "start": sessions[0],
            "end": sessions[-1],
            "sessions": sessions,
            "calendar_id": "local-development",
            "calendar_version": payload_hash[:16],
        }
        return path, DataAssetRef(
            data_asset_id=f"local-development:{payload_hash[:16]}",
            storage_ref=_project_reference(path),
            media_type="text/csv",
            schema_id="market-history-v1",
            asset_ids=asset_ids,
            normalized_fields=("date", "asset_id", "close", "adj_close"),
            coverage=coverage,
            row_count=len(history),
            price_convention={"close": "unadjusted", "adj_close": "adjusted"},
            content_hash=payload_hash,
            lineage={"storage_mode": "local-development"},
            created_by="local-development",
        )


def call_tool(
    request: Mapping[str, Any],
    *,
    host_context: ModuleHostContext | None = None,
    result_store: Any | None = None,
    tenant_id: str | None = None,
    data_store: Any | None = None,
) -> Mapping[str, Any]:
    """正式Agent Tool只接受共享PricingInput；页面协议不在此兼容。"""
    if isinstance(request, Mapping):
        values = dict(request)
        action = str(values.pop("action", "run")).strip().lower()
        if action == "catalog":
            return PricerRuntime().catalog()
        if action != "run":
            return {
                "ok": False,
                "module": "pricer",
                "status": "unsupported",
                "message": f"Pricer不支持action={action}；仅支持catalog、run。",
            }
        request = values
    if isinstance(request, ProtocolPricingInput):
        return PricerRuntime(data_port=data_store, result_store=result_store).run_formal(request, host_context=host_context, tenant_id=tenant_id)
    if not isinstance(request, Mapping) or not {"contract", "pricing_config", "market_data_refs"}.issubset(request):
        raise PricerWebInputError("正式Pricer Tool只接受PricingInput={contract,pricing_config,market_data_refs,trading_calendar_ref?}")
    return PricerRuntime(data_port=data_store, result_store=result_store).run_formal(request, host_context=host_context, tenant_id=tenant_id)


def _formal_pricing_input(
    request: ProtocolPricingInput | Mapping[str, Any],
) -> tuple[ProtocolPricingInput, Any]:
    if isinstance(request, ProtocolPricingInput):
        config = dict(request.pricing_config)
        observed_state = config.pop("observed_contract_state", None)
        verify_product_snapshot_binding(request.contract, load_registry())
        return ProtocolPricingInput(
            contract=request.contract,
            pricing_config=config,
            market_data_refs=request.market_data_refs,
            trading_calendar_ref=request.trading_calendar_ref,
        ), observed_state
    values = dict(request)
    allowed = {"contract", "pricing_config", "market_data_refs", "trading_calendar_ref"}
    unknown = set(values) - allowed
    if unknown:
        raise PricerWebInputError("正式PricingInput含未知字段：" + "、".join(sorted(unknown)))
    contract_value = values.get("contract")
    if isinstance(contract_value, ResolvedContract):
        contract = contract_value
    elif isinstance(contract_value, Mapping):
        try:
            contract = ResolvedContract(**dict(contract_value))
        except (TypeError, ValueError, ContractResolutionError) as error:
            raise PricerWebInputError(f"PricingInput.contract无效：{error}") from error
    else:
        raise PricerWebInputError("PricingInput.contract必须为ResolvedContract或完整协议对象")
    try:
        verify_product_snapshot_binding(contract, load_registry())
    except ContractResolutionError as error:
        raise PricerWebInputError(f"PricingInput.contract产品快照无效：{error}") from error
    config = values.get("pricing_config")
    refs = values.get("market_data_refs")
    if not isinstance(config, Mapping):
        raise PricerWebInputError("PricingInput.pricing_config必须为对象")
    if isinstance(refs, (str, bytes)) or not isinstance(refs, (tuple, list)):
        raise PricerWebInputError("PricingInput.market_data_refs必须为DataAssetRef列表")
    config_values = dict(config)
    observed_state = config_values.pop("observed_contract_state", None)
    return ProtocolPricingInput(
        contract=contract,
        pricing_config=config_values,
        market_data_refs=tuple(_data_asset_ref(value) for value in refs),
        trading_calendar_ref=(
            None if values.get("trading_calendar_ref") is None
            else _data_asset_ref(values["trading_calendar_ref"])
        ),
    ), observed_state


def _require_host_contract(contract: ResolvedContract, context: ModuleHostContext) -> None:
    execution_permissions = {"module.run", "conversation.tool.run"}
    if context.module != "pricer" or not execution_permissions.intersection(context.request_policy):
        raise PricerWebInputError("ModuleHostContext未授权Pricer正式运行")
    missing = [name for name in ("task_id", "analysis_case_id", "candidate_id", "catalog_version", "contract_fingerprint") if not getattr(context, name)]
    if missing:
        raise PricerWebInputError("ModuleHostContext缺少正式运行字段：" + ",".join(missing))
    if context.contract_fingerprint != contract.contract_fingerprint:
        raise PricerWebInputError("ModuleHostContext.contract_fingerprint与ResolvedContract不一致")
    if context.contract_ref is None:
        raise PricerWebInputError("Pricer正式运行缺少Core验证的ResolvedContract引用")
    if context.contract_ref.schema_id != "optionhelper.resolved-contract/v1":
        raise PricerWebInputError("ModuleHostContext.contract_ref不是ResolvedContract v1引用")
    if context.contract_ref.content_hash != contract.contract_fingerprint:
        raise PricerWebInputError("ModuleHostContext.contract_ref与ResolvedContract不一致")


def _data_asset_ref(value: DataAssetRef | Mapping[str, Any]) -> DataAssetRef:
    if isinstance(value, DataAssetRef):
        return value
    if not isinstance(value, Mapping):
        raise PricerWebInputError("market_data_refs只能包含DataAssetRef")
    try:
        return DataAssetRef(**dict(value))
    except (TypeError, ValueError) as error:
        raise PricerWebInputError(f"DataAssetRef协议字段无效：{error}") from error


def _asset_adjustment(ref: DataAssetRef) -> str:
    """正式字段为price_convention.adjustment；旧close/adj_close只在loader内归一。"""
    convention = dict(ref.price_convention)
    explicit = convention.get("adjustment")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().lower()
    if {"close", "adj_close"}.issubset(ref.normalized_fields):
        return "close_and_adj_close"
    raise PricerWebInputError("DataAssetRef必须明确adjustment且normalized_fields含close、adj_close")


def _data_ref_dict(ref: DataAssetRef) -> dict[str, Any]:
    return asdict(ref)


def _module_run_files(output: Mapping[str, Any]) -> dict[str, Any]:
    status = str(output["status"])
    manifest = {
        "module": "pricer",
        "status": status,
        "task_id": output["task_id"],
        "run_id": output["run_id"],
        "analysis_case_id": output["analysis_case_id"],
        "tenant_id": output["tenant_id"],
        "created_by": output["created_by"],
        "access_scope": output["access_scope"],
        "candidate_id": output["candidate_id"],
        "catalog_version": output["catalog_version"],
        "execution_fingerprint": output["execution_fingerprint"],
        "input_snapshot": "input_snapshot.json",
        "data_refs": "data_refs.json",
        "limitations": "limitations.json",
        "result": "result.json" if status in {"succeeded", "partial"} else None,
        "artifacts": [
            {"name": "legacy_result.json", "media_type": "application/json"},
            {"name": "pricing_result.csv", "media_type": "text/csv"},
        ],
        "artifact_manifest": "artifacts/artifact_manifest.json",
        "contract_fingerprint": output["contract_fingerprint"],
        "error": None if status in {"succeeded", "partial"} else {
            "status": status,
            "limitations": output["limitations"],
        },
        "metadata": {"created_at": _now(), "quote_eligible": output["pricing"].get("quote_eligible")},
    }
    files: dict[str, Any] = {
        "manifest.json": manifest,
        "input_snapshot.json": output["input_snapshot"],
        "resolved_contract.json": output["resolved_contract"],
        "data_refs.json": {"data_refs": output["data_refs"]},
        "limitations.json": {"limitations": output["limitations"]},
        "artifacts/legacy_result.json": dict(output),
        "artifacts/pricing_result.csv": _pricing_csv(output),
    }
    if status in {"succeeded", "partial"}:
        files["result.json"] = dict(output)
    else:
        files["error.json"] = {
            "status": status,
            "limitations": output["limitations"],
            "pricing": output["pricing"],
        }
    return files


def _pricing_csv(output: Mapping[str, Any]) -> str:
    pricing = output.get("pricing", {})
    if not isinstance(pricing, Mapping):
        pricing = {}
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=(
        "task_id", "run_id", "product_id", "status", "method", "pv_amount",
        "currency", "quote_eligible", "precision_status", "contract_fingerprint",
    ))
    writer.writeheader()
    writer.writerow({
        "task_id": output["task_id"],
        "run_id": output["run_id"],
        "product_id": pricing.get("product_id"),
        "status": pricing.get("status"),
        "method": pricing.get("method"),
        "pv_amount": pricing.get("pv_amount"),
        "currency": pricing.get("currency"),
        "quote_eligible": pricing.get("quote_eligible"),
        "precision_status": pricing.get("precision_status"),
        "contract_fingerprint": output["contract_fingerprint"],
    })
    return "\ufeff" + stream.getvalue()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name}必须为JSON对象")
    return value


def _identity(body: Mapping[str, Any]) -> dict[str, Any]:
    supplied = body.get("identity", body.get("contract_context", {}))
    if not isinstance(supplied, Mapping):
        raise PricerWebInputError("identity必须为对象")
    underlyings = supplied.get("underlyings", body.get("underlyings", ["000905.SH"]))
    if isinstance(underlyings, str):
        underlyings = [item.strip() for item in underlyings.split(",") if item.strip()]
    if not isinstance(underlyings, list):
        raise PricerWebInputError("identity.underlyings必须为列表")
    identity = {
        "contract_id": supplied.get("contract_id", body.get("contract_id")),
        "underlyings": underlyings,
        "currency": supplied.get("currency", "CNY"),
        "contract_start_date": supplied.get("contract_start_date"),
    }
    if supplied.get("contract_reference_spots") is not None:
        identity["contract_reference_spots"] = supplied["contract_reference_spots"]
    return {key: value for key, value in identity.items() if value is not None}


def _identity_with_pricing_reference(identity: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(identity)
    if "contract_reference_spots" in result:
        return result
    supplied = body.get("pricing_config", body.get("pricing_input", {}))
    if not isinstance(supplied, Mapping):
        return result
    raw_single, raw_multi = supplied.get("S0Raw"), supplied.get("S0RawVec")
    assets = result["underlyings"]
    if raw_single is not None and len(assets) == 1:
        result["contract_reference_spots"] = {assets[0]: raw_single}
    elif isinstance(raw_multi, Mapping) and set(raw_multi) == set(assets):
        result["contract_reference_spots"] = dict(raw_multi)
    return result


def _term_overrides(body: Mapping[str, Any]) -> dict[str, Any]:
    value = body.get("term_overrides", body.get("payoff_input", {}))
    if not isinstance(value, Mapping):
        raise PricerWebInputError("term_overrides必须为对象")
    identity_only = {"U", "underlyingOrder", "priceBasis", "premiumSign", "exercisePolicy"}
    return {key: item for key, item in value.items() if key not in identity_only}


def _pricing_config(body: Mapping[str, Any]) -> PricingConfig:
    supplied = body.get("pricing_config", body.get("pricing_input", {}))
    if not isinstance(supplied, Mapping):
        raise PricerWebInputError("pricing_config必须为对象")
    aliases = {
        "tv": "valuation_date", "r": "risk_free_rate", "q": "dividend_yield",
        "tau": "time_to_maturity", "sigma": "volatility_override", "M": "path_count",
        "xi": "random_seed", "modelMethod": "model_method",
    }
    values = {aliases.get(key, key): value for key, value in dict(supplied).items()}
    legacy_model = values.pop("modelSpec", None)
    if isinstance(legacy_model, Mapping):
        values.setdefault("hv_window", legacy_model.get("hv_window"))
    legacy_bumps = values.pop("eps", None)
    if isinstance(legacy_bumps, Mapping):
        values.setdefault("greek_bumps", {
            "spot": legacy_bumps.get("epsilon_S", 0.01),
            "volatility": legacy_bumps.get("epsilon_sigma", 0.01),
            "time": legacy_bumps.get("epsilon_t", 1 / 365),
            "rate": legacy_bumps.get("epsilon_r", 0.0001),
        })
    for key in ("S0Raw", "S0RawVec", "hist", "histMulti"):
        values.pop(key, None)
    return PricingConfig.from_mapping({key: value for key, value in values.items() if value is not None})


def _with_default_valuation_date(config: PricingConfig) -> PricingConfig:
    """Apply the device-local default once, before any DataAsset lookup."""
    return config if config.valuation_date is not None else replace(
        config, valuation_date=local_valuation_date(date.today()),
    )


def _history_reference(body: Mapping[str, Any]) -> Any:
    refs = body.get("market_data_refs")
    if refs is not None:
        if not isinstance(refs, (list, tuple)) or len(refs) != 1:
            raise PricerWebInputError("Pricer页面只能绑定唯一DataAssetRef")
        return refs[0]
    if body.get("history_path"):
        return body["history_path"]
    supplied = body.get("pricing_config", body.get("pricing_input", {}))
    if isinstance(supplied, Mapping):
        return supplied.get("history_reference", supplied.get("hist", supplied.get("histMulti")))
    return None


def _history_path(value: Any) -> Path:
    if value is None:
        raise PricerWebInputError("请先绑定真实DataAssetRef；绑定行情后自动填充S₀Raw。")
    raw = Path(str(value))
    # A direct local CSV is accepted only from the module DataStore.  Relative
    # values are resolved below that root; absolute paths are allowed only if
    # they remain inside it after symlink resolution.
    path = raw if raw.is_absolute() else RUNTIME_PATHS.data_root / raw
    path = path.resolve()
    data_root = RUNTIME_PATHS.data_root.resolve()
    if path != data_root and data_root not in path.parents:
        raise PricerWebInputError("历史行情只能来自受控DataStore或Host注入的DataAssetRef")
    if not path.is_file():
        raise PricerWebInputError(f"历史行情文件不存在：{path}")
    return path


def _text(value: Any, name: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise PricerWebInputError(f"{name}不能为空")
    return text


def _identifier(value: Any, prefix: str) -> str:
    text = str(value).strip() if value is not None else ""
    return text or f"{prefix}-{uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _project_reference(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _greek_amount(value: Any) -> float | str:
    """结果落盘只读取正式GreekValue的金额口径。"""
    return value.get("pv_amount_value", "") if isinstance(value, Mapping) else ""


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _validated_output_contract_fingerprint(output: Mapping[str, Any]) -> str:
    fingerprint = output.get("contract_fingerprint")
    resolved_contract = output.get("resolved_contract")
    pricing = output.get("pricing")
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        raise ValueError("Pricer输出缺少有效contract_fingerprint")
    if not isinstance(resolved_contract, Mapping) or resolved_contract.get("contract_fingerprint") != fingerprint:
        raise ValueError("resolved_contract.contract_fingerprint与Pricer输出不一致")
    if not isinstance(pricing, Mapping) or pricing.get("contract_fingerprint") != fingerprint:
        raise ValueError("pricing.contract_fingerprint与Pricer输出不一致")
    return fingerprint


def _write_run(task_id: str, run_id: str, request: Mapping[str, Any], output: Mapping[str, Any]) -> None:
    contract_fingerprint = _validated_output_contract_fingerprint(output)
    folder = RESULT_ROOT / "output_pricing" / task_id / run_id
    folder.mkdir(parents=True, exist_ok=False)
    _write_json(folder / "input.json", request)
    _write_json(folder / "resolved_contract.json", output["resolved_contract"])
    _write_json(folder / "result.json", output)
    _write_json(folder / "manifest.json", {
        "module": "pricer",
        "task_id": task_id,
        "run_id": run_id,
        "contract_fingerprint": contract_fingerprint,
        "created_at": _now(),
        "files": ["input.json", "resolved_contract.json", "result.json", "manifest.json"],
    })
    _rebuild_pricing_results_table()


def _rebuild_pricing_results_table() -> None:
    """从所有Pricer运行明细重建产品结果总表，不替代任何运行目录。"""
    output_root = RESULT_ROOT / "output_pricing"
    headers = [
        "生成时间", "任务编号", "运行编号", "产品编号", "产品名称", "标的", "估值日", "定价方法",
        "PV", "币种", "Delta", "Gamma", "Vega", "Theta", "Rho", "波动率", "HV窗口",
        "无风险利率R_f", "分红率q", "标准误差", "结果目录",
    ]
    rows: list[dict[str, Any]] = []
    for result_path in output_root.glob("*/*/result.json"):
        manifest_path = result_path.with_name("manifest.json")
        input_path = result_path.with_name("input.json")
        try:
            output = json.loads(result_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            request = json.loads(input_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(output, Mapping) or not isinstance(request, Mapping) or output.get("module") != "pricer":
            continue
        contract = output.get("resolved_contract", {})
        identity = contract.get("identity", {}) if isinstance(contract, Mapping) else {}
        pricing = output.get("pricing", {})
        market = output.get("market_snapshot", {})
        if not isinstance(pricing, Mapping):
            pricing = {}
        if not isinstance(market, Mapping):
            market = {}
        greeks = pricing.get("greeks", {})
        if not isinstance(greeks, Mapping):
            greeks = {}
        rows.append({
            "生成时间": manifest.get("created_at", ""),
            "任务编号": output.get("task_id", ""),
            "运行编号": output.get("run_id", ""),
            "产品编号": pricing.get("product_id", output.get("product_id", request.get("product_id", ""))),
            "产品名称": identity.get("name_zh", ""),
            "标的": ",".join(identity.get("underlyings", [])),
            "估值日": market.get("valuation_date", ""),
            "定价方法": pricing.get("method", ""),
            "PV": pricing.get("pv", ""),
            "币种": pricing.get("currency", identity.get("currency", "")),
            "Delta": _greek_amount(greeks.get("delta")),
            "Gamma": _greek_amount(greeks.get("gamma")),
            "Vega": _greek_amount(greeks.get("vega")),
            "Theta": _greek_amount(greeks.get("theta")),
            "Rho": _greek_amount(greeks.get("rho")),
            "波动率": market.get("historical_volatility", ""),
            "HV窗口": market.get("hv_window", ""),
            "无风险利率R_f": market.get("risk_free_rate", ""),
            "分红率q": market.get("dividend_yield", ""),
            "标准误差": pricing.get("standard_error", ""),
            "结果目录": _project_reference(result_path.parent),
        })
    rows.sort(key=lambda row: (str(row["生成时间"]), str(row["任务编号"]), str(row["运行编号"])))
    table = io.StringIO(newline="")
    writer = csv.DictWriter(table, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    output_root.mkdir(parents=True, exist_ok=True)
    target = output_root / RESULT_TABLE_NAME
    temporary = target.with_suffix(".csv.tmp")
    with RESULT_TABLE_LOCK:
        temporary.write_text("\ufeff" + table.getvalue(), encoding="utf-8")
        temporary.replace(target)


class Handler(BaseHTTPRequestHandler):
    runtime = PricerRuntime()

    def log_message(self, *_: Any) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        assets = {
            "/pricer.css": (PAGE_DIR / "pricer.css", "text/css; charset=utf-8"),
            "/pricer.js": (PAGE_DIR / "pricer.js", "application/javascript; charset=utf-8"),
            **static_assets(),
        }
        if self.path == "/api/status":
            return self._json(HTTPStatus.OK, {"ok": True, "module": "pricer", "web_ui_version": WEB_UI_VERSION})
        if self.path == "/api/catalog":
            return self._json(HTTPStatus.OK, self.runtime.catalog())
        if self.path in {"/", "/pricer.html"}:
            return self._file(PAGE, "text/html; charset=utf-8")
        if self.path in assets:
            return self._file(*assets[self.path])
        return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "未找到资源"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/run":
            return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "未找到接口"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise PricerWebInputError("请求体大小无效")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, Mapping):
                raise PricerWebInputError("请求体必须为JSON对象")
            return self._json(HTTPStatus.OK, self.runtime.run(body))
        except Exception as error:
            return self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": str(error)})

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "文件不存在"})
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: HTTPStatus, data: Mapping[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def run_host(*, host: str = HOST, port: int = PORT) -> None:
    """供core.module_host调用；保留原有Pricer页面和API。"""
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    run_host(host=HOST, port=PORT)


if __name__ == "__main__":
    main()
