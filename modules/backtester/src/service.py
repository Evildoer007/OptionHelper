"""Backtester本机服务：解析页面输入并调用唯一正式入口。"""

from __future__ import annotations

import csv
from dataclasses import asdict
from hashlib import sha256
from io import StringIO
from io import BytesIO
import json
import os
import re
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from runtime.bootstrap import bootstrap_runtime
from runtime.adapters.local_store import LocalResultStore
from runtime.contracts.contract_api import (
    ContractResolutionError,
    RESOLVED_CONTRACT_SCHEMA_ID,
    ResolvedContract,
    resolve_contract,
    verify_product_snapshot_binding,
)
from runtime.knowledger import load_registry
from runtime.protocol.models import BacktestInput as ProtocolBacktestInput, DataAssetRef, ModuleRunRef
from runtime.protocol.module_host import ModuleHostContext, require_host_bound_run_contract
from runtime.protocol.version import PUBLIC_VERSION
import pandas as pd

from .historical_data import (
    DataFetcherPortUnavailable,
    HistoricalData,
    HistoricalDataError,
    load_local_historical_data,
    load_port_historical_data,
    validate_data_asset_ref,
)
from .entry_generator import BacktestInputError, ZeroValidSamplesError
from .impl.config import BacktestConfig
from .impl.engine import backtest
from .models import BacktestInput


MODULE_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_PATHS = bootstrap_runtime(MODULE_ROOT)
PROJECT_ROOT = RUNTIME_PATHS.project_root
RESULT_ROOT = RUNTIME_PATHS.result_root
PAGE_DIR = RUNTIME_PATHS.module_page_dir("backtester")
PAGE = PAGE_DIR / "backtester.html"
UI_DIR = PAGE_DIR / "ui"
VENDOR_DIR = PAGE_DIR / "vendor"
BRAND_ASSET_DIR = PROJECT_ROOT / "assets" / "icons"
HOST = "127.0.0.1"
PORT = int(os.environ.get("OPTIONHELPER_BACKTESTER_PORT", "4281"))
MAX_BODY_BYTES = 1_500_000
WEB_UI_VERSION = "2026.08.07.2"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")


class BacktesterWebInputError(ValueError):
    """页面请求不满足Backtester输入边界。"""


def page_asset_path(request_path: str) -> Path | None:
    """五页面统一以../../icons引用公共Logo；开发Host只暴露白名单资源。"""
    filename = request_path.removeprefix("/icons/")
    if request_path.startswith("/icons/") and filename in {"optionhelper-logo.svg", "optionhelper-app-icon-tile-light.svg"}:
        return BRAND_ASSET_DIR / filename
    return None


class BacktesterRuntime:
    """页面适配器；DataFetcher只能经显式注入端口接入。"""

    module_name = "backtester"

    def __init__(
        self,
        *,
        historical_data_port: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        data_store: Any | None = None,
        result_store: Any | None = None,
        tenant_id: str = "local",
    ) -> None:
        self._historical_data_port = historical_data_port
        self._data_store = data_store
        self._result_store_port = result_store
        self._tenant_id = _identifier(tenant_id, "tenant")

    def catalog(self) -> dict[str, Any]:
        registry = load_registry()
        catalog = registry["term_catalog"]
        products: list[dict[str, Any]] = []
        for product_id, product in registry["products"].items():
            terms = product["terms"]
            fields = [{
                "key": key,
                "label": catalog[key]["name_zh"],
                "symbol": catalog[key]["symbol"],
                "value_type": catalog[key]["value_type"],
                "unit": catalog[key]["unit"],
                "domain": catalog[key]["domain"],
                "default_value": value,
            } for key, value in terms.items() if key not in {
                "monitor", "pricing_methods", "constraints", "derived_terms", "margin_call", "payoff_figure_basis",
                "payoff_normalizer",
            }]
            products.append({
                "product_id": product_id,
                "canonical_name": product["identity"]["name_zh"],
                "entry_status": product["identity"]["entry_status"],
                "underlying_scope": "multi_underlying" if "S0Vec" in terms else "single_underlying",
                "payoff_fields": fields,
                "pricing_methods": terms["pricing_methods"],
            })
        return {
            "ok": True,
            "module": self.module_name,
            "products": products,
            "config_fields": list(BacktestConfig.__dataclass_fields__),
            "ports": {
                "historical_data_port": {"configured": self._historical_data_port is not None, "required_for": "data_request"},
                "data_store": {"configured": self._data_store is not None, "required_for": "opaque DataAssetRef payload"},
                "result_store": {"configured": True, "owner": "Core LocalResultStore"},
            },
        }

    def run(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """受控本地开发入口；先取得历史日历，再解析同一份观察合同。"""
        product_id = _text(body.get("product_id"), "product_id")
        history = self._historical_data(body)
        identity = _identity(body)
        identity.setdefault("calendar_id", history.calendar_id)
        identity.setdefault("calendar_version", history.calendar_version)
        contract = resolve_contract(
            product_id,
            identity=identity,
            term_overrides=_term_overrides(body),
            registry=load_registry(),
            trading_dates=history.trading_sessions,
        )
        config = _backtest_config(body)
        result = backtest(BacktestInput(contract, config, history))
        task_id = _identifier(body.get("task_id"), "task")
        run_id = _identifier(body.get("run_id"), "run")
        analysis_case_id = _identifier(body.get("analysis_case_id"), "analysis-case")
        candidate_id = _identifier(body.get("candidate_id"), "candidate")
        created_at = _now()
        backtest_payload = result.to_dict()
        private_audit_ledger = result.audit_ledger()
        data_ref = validate_data_asset_ref(backtest_payload["data_asset_ref"])
        backtest_payload["data_asset_ref"] = data_ref
        data_refs = [data_ref]
        limitations = list(backtest_payload["limitations"])
        output = {
            "ok": True,
            "schema_version": "backtester.run.v1.0.0",
            "module": self.module_name,
            "status": "succeeded",
            "task_id": task_id,
            "run_id": run_id,
            "analysis_case_id": analysis_case_id,
            "candidate_id": candidate_id,
            "catalog_version": PUBLIC_VERSION,
            "contract_fingerprint": contract.contract_fingerprint,
            "created_at": created_at,
            "resolved_contract": contract.to_protocol_dict(),
            "data_refs": data_refs,
            "limitations": limitations,
            "execution_fingerprint": backtest_payload["execution_fingerprint"],
            "backtest": backtest_payload,
        }
        input_snapshot = {
            "schema_version": "backtester.input-snapshot.v1.0.0",
            "product_id": product_id,
            "identity": identity,
            "term_overrides": _term_overrides(body),
            "backtest_config": config.to_dict(),
            "data_refs": data_refs,
            "created_at": created_at,
        }
        module_run_ref = _write_run(
            result_store=self._result_store(),
            tenant_id=self._tenant_id,
            task_id=task_id,
            run_id=run_id,
            input_snapshot=input_snapshot,
            output=output,
            private_audit_ledger=private_audit_ledger,
        )
        return {**output, "module_run_ref": module_run_ref}

    def run_formal(
        self,
        request: ProtocolBacktestInput | Mapping[str, Any],
        *,
        host_context: ModuleHostContext,
    ) -> dict[str, Any]:
        """正式Tool入口：消费Host冻结合同、回测配置和唯一DataAssetRef。"""
        protocol_input = _formal_backtest_input(request)
        contract = protocol_input.contract
        _require_host_contract(contract, host_context)
        result_store = self._formal_result_store()
        task_id = str(host_context.task_id)
        run_id = f"run-{uuid4().hex[:12]}"
        created_at = _now()
        data_ref = protocol_input.historical_data
        input_snapshot = _formal_input_snapshot(contract, protocol_input.backtest_config, data_ref)
        data_refs: list[dict[str, Any]] = []
        limitations: list[str] = ["formal_run_failed_before_result"]
        try:
            data_ref_dict = validate_data_asset_ref(asdict(data_ref))
        except HistoricalDataError as error:
            return self._formal_failure(
                contract=contract, task_id=task_id, run_id=run_id, created_at=created_at,
                analysis_case_id=host_context.analysis_case_id, candidate_id=host_context.candidate_id,
                result_store=result_store, input_snapshot=input_snapshot, data_refs=data_refs, limitations=limitations,
                stage="historical_data", error_code="historical_data_reference_invalid", error=error,
            )
        data_refs = [data_ref_dict]
        input_snapshot = _formal_input_snapshot(contract, protocol_input.backtest_config, data_ref_dict)
        try:
            _validate_formal_data_ref(data_ref, tenant_id=self._tenant_id)
            store = self._formal_data_store()
            payload = store.read_bytes(data_ref, tenant_id=self._tenant_id)
            if not isinstance(payload, bytes):
                raise BacktesterWebInputError("data_store.read_bytes必须返回bytes")
            if sha256(payload).hexdigest() != data_ref.content_hash:
                raise BacktesterWebInputError("DataStore返回原始字节与DataAssetRef.content_hash不一致")
            history = HistoricalData.from_frame(pd.read_csv(BytesIO(payload)), data_asset_ref=asdict(data_ref))
            if not history.calendar_source_declared:
                raise BacktesterWebInputError("正式完整期限回测要求DataAssetRef显式声明交易日calendar_id、calendar_version和sessions")
            _require_contract_calendar(contract, history)
        except (BacktesterWebInputError, HistoricalDataError, OSError, PermissionError, TypeError, ValueError) as error:
            return self._formal_failure(
                contract=contract, task_id=task_id, run_id=run_id, created_at=created_at,
                analysis_case_id=host_context.analysis_case_id, candidate_id=host_context.candidate_id,
                result_store=result_store, input_snapshot=input_snapshot, data_refs=data_refs, limitations=limitations,
                stage="historical_data", error_code="historical_data_unavailable", error=error,
            )
        try:
            config = BacktestConfig.from_mapping(protocol_input.backtest_config)
            if not config.complete_tenor:
                raise BacktesterWebInputError("正式Backtester运行必须要求complete_tenor=true；partial coverage不能标记为succeeded")
        except ValueError as error:
            return self._formal_failure(
                contract=contract, task_id=task_id, run_id=run_id, created_at=created_at,
                analysis_case_id=host_context.analysis_case_id, candidate_id=host_context.candidate_id,
                result_store=result_store, input_snapshot=input_snapshot, data_refs=data_refs, limitations=limitations,
                stage="backtest_config", error_code="backtest_config_invalid", error=error,
            )
        input_snapshot = _formal_input_snapshot(contract, config.to_dict(), data_ref_dict)
        try:
            result = backtest(BacktestInput(contract, config, history))
        except (BacktestInputError, HistoricalDataError, ValueError) as error:
            error_code = "zero_valid_samples" if isinstance(error, ZeroValidSamplesError) else "backtest_execution_failed"
            return self._formal_failure(
                contract=contract, task_id=task_id, run_id=run_id, created_at=created_at,
                analysis_case_id=host_context.analysis_case_id, candidate_id=host_context.candidate_id,
                result_store=result_store, input_snapshot=input_snapshot, data_refs=data_refs, limitations=limitations,
                stage="path_replay", error_code=error_code, error=error,
            )
        backtest_payload = result.to_dict()
        private_audit_ledger = result.audit_ledger()
        data_ref_dict = validate_data_asset_ref(backtest_payload["data_asset_ref"])
        output = {
            "ok": True,
            "schema_version": "backtester.run.v1.0.0",
            "module": self.module_name,
            "status": "succeeded",
            "task_id": task_id,
            "run_id": run_id,
            "analysis_case_id": host_context.analysis_case_id,
            "candidate_id": host_context.candidate_id,
            "catalog_version": host_context.catalog_version,
            "contract_fingerprint": contract.contract_fingerprint,
            "created_at": created_at,
            "resolved_contract": contract.to_protocol_dict(),
            "data_refs": [data_ref_dict],
            "limitations": list(backtest_payload["limitations"]),
            "execution_fingerprint": backtest_payload["execution_fingerprint"],
            "backtest": backtest_payload,
        }
        require_host_bound_run_contract(output, host_context)
        module_run_ref = _write_run(
            result_store=result_store,
            tenant_id=self._tenant_id,
            task_id=task_id,
            run_id=run_id,
            input_snapshot=input_snapshot,
            output=output,
            private_audit_ledger=private_audit_ledger,
        )
        return {**output, "module_run_ref": module_run_ref}

    def _formal_data_store(self) -> Any:
        store = self._data_store
        if store is None:
            raise BacktesterWebInputError("正式Backtester运行必须由Host注入data_store")
        if not callable(getattr(store, "read_bytes", None)):
            raise BacktesterWebInputError("data_store必须实现read_bytes")
        return store

    def _formal_result_store(self) -> Any:
        store = self._result_store_port
        if store is None:
            raise BacktesterWebInputError("正式Backtester运行必须由Host注入result_store")
        if not callable(getattr(store, "commit_module_run", None)):
            raise BacktesterWebInputError("result_store必须实现commit_module_run")
        return store

    def _formal_failure(
        self,
        *,
        contract: ResolvedContract,
        task_id: str,
        run_id: str,
        created_at: str,
        analysis_case_id: str,
        candidate_id: str,
        result_store: Any,
        input_snapshot: Mapping[str, Any],
        data_refs: list[dict[str, Any]],
        limitations: list[str],
        stage: str,
        error_code: str,
        error: Exception,
    ) -> dict[str, Any]:
        message = _safe_formal_error_message(error)
        failure = {
            "error_code": error_code,
            "stage": stage,
            "message": message,
            "retryable": stage == "historical_data",
            "missing_inputs": ["historical_data"] if stage == "historical_data" else [],
            "upstream_refs": data_refs,
        }
        output = {
            "ok": False,
            "schema_version": "backtester.run.v1.0.0",
            "module": self.module_name,
            "status": "failed",
            "task_id": task_id,
            "run_id": run_id,
            "analysis_case_id": analysis_case_id,
            "candidate_id": candidate_id,
            "catalog_version": PUBLIC_VERSION,
            "contract_fingerprint": contract.contract_fingerprint,
            "created_at": created_at,
            "resolved_contract": contract.to_protocol_dict(),
            "data_refs": data_refs,
            "limitations": [*limitations, f"formal_failure_stage:{stage}"],
            "error": failure,
        }
        module_run_ref = _write_failed_run(
            result_store=result_store, tenant_id=self._tenant_id, task_id=task_id, run_id=run_id,
            input_snapshot=input_snapshot, output=output,
        )
        return {**output, "module_run_ref": module_run_ref}

    def _result_store(self) -> Any:
        store = self._result_store_port or LocalResultStore(RESULT_ROOT)
        if not callable(getattr(store, "commit_module_run", None)):
            raise BacktesterWebInputError("result_store必须实现commit_module_run")
        return store

    def _historical_data(self, body: Mapping[str, Any]) -> HistoricalData:
        if body.get("data_request") is not None:
            request = body["data_request"]
            if not isinstance(request, Mapping):
                raise BacktesterWebInputError("data_request必须为对象")
            return load_port_historical_data(
                request,
                project_root=PROJECT_ROOT,
                data_root=RUNTIME_PATHS.data_root,
                call_port=self._historical_data_port,
                data_store=self._data_store,
            )
        reference = _history_reference(body)
        if reference is None:
            raise BacktesterWebInputError("请先由Host绑定真实DataAssetRef；Backtester不使用默认本地行情。")
        return load_local_historical_data(
            reference,
            project_root=PROJECT_ROOT,
            data_root=RUNTIME_PATHS.data_root,
        )


def call_tool(
    request: Mapping[str, Any],
    *,
    host_context: ModuleHostContext | None = None,
    result_store: Any | None = None,
    tenant_id: str | None = None,
    data_store: Any | None = None,
) -> Mapping[str, Any]:
    """Agent Host薄适配：仅暴露目录与正式回测。"""
    body = dict(request)
    if any(_normalized_request_key(key) in {"datastore", "datastoreport"} for key in body):
        raise BacktesterWebInputError("data_store只能作为Host私有依赖注入，禁止进入正式请求")
    action = str(body.pop("action", "run" if "contract" in body else "catalog")).strip().lower()
    formal_fields = {"contract", "backtest_config", "historical_data"}
    formal_input: ProtocolBacktestInput | None = None
    if data_store is not None:
        if action != "run" or set(body) != formal_fields:
            raise BacktesterWebInputError("data_store仅允许Host为完整正式BacktestInput运行注入")
        if not isinstance(host_context, ModuleHostContext):
            raise BacktesterWebInputError("data_store正式运行必须由Host注入ModuleHostContext")
        formal_input = _formal_backtest_input(body)
        _require_host_contract(formal_input.contract, host_context)
    runtime = BacktesterRuntime(data_store=data_store, result_store=result_store, tenant_id=tenant_id or "local")
    if action == "catalog":
        if host_context is not None and "module.catalog" not in host_context.request_policy:
            raise BacktesterWebInputError("ModuleHostContext未授权module.catalog")
        return runtime.catalog()
    if action == "run":
        try:
            if formal_input is not None:
                return runtime.run_formal(formal_input, host_context=host_context)
            if formal_fields.issubset(body):
                if host_context is None:
                    raise BacktesterWebInputError("正式Backtester调用必须由Host注入ModuleHostContext")
                return runtime.run_formal(body, host_context=host_context)
            return runtime.run(body)
        except DataFetcherPortUnavailable as error:
            return _port_failure(error)
    return {"ok": False, "module": "backtester", "status": "unsupported", "message": f"不支持action={action}"}


def _normalized_request_key(key: object) -> str:
    return "".join(character for character in str(key).casefold() if character.isalnum())


def _formal_backtest_input(value: ProtocolBacktestInput | Mapping[str, Any]) -> ProtocolBacktestInput:
    if isinstance(value, ProtocolBacktestInput):
        contract, config, data_ref = value.contract, value.backtest_config, value.historical_data
    elif isinstance(value, Mapping):
        values = dict(value)
        allowed = {"contract", "backtest_config", "historical_data"}
        if set(values) != allowed:
            raise BacktesterWebInputError("正式BacktestInput字段必须为contract、backtest_config、historical_data")
        raw_contract = values["contract"]
        try:
            contract = raw_contract if isinstance(raw_contract, ResolvedContract) else ResolvedContract(**dict(raw_contract))
        except (TypeError, ValueError, ContractResolutionError) as error:
            raise BacktesterWebInputError(f"BacktestInput.contract无效：{error}") from error
        config = values["backtest_config"]
        raw_ref = values["historical_data"]
        try:
            data_ref = raw_ref if isinstance(raw_ref, DataAssetRef) else DataAssetRef(**dict(raw_ref))
        except (TypeError, ValueError) as error:
            raise BacktesterWebInputError(f"BacktestInput.historical_data无效：{error}") from error
    else:
        raise BacktesterWebInputError("BacktestInput必须为正式协议对象")
    if not isinstance(config, Mapping):
        raise BacktesterWebInputError("BacktestInput.backtest_config必须为对象")
    try:
        verify_product_snapshot_binding(contract, load_registry())
    except ContractResolutionError as error:
        raise BacktesterWebInputError(f"BacktestInput.contract产品快照无效：{error}") from error
    return ProtocolBacktestInput(contract=contract, backtest_config=dict(config), historical_data=data_ref)


def _require_host_contract(contract: ResolvedContract, context: ModuleHostContext) -> None:
    run_policies = {"module.run", "conversation.tool.run"}
    if context.module != "backtester" or not run_policies.intersection(context.request_policy):
        raise BacktesterWebInputError("ModuleHostContext未授权Backtester正式运行")
    missing = [name for name in ("task_id", "analysis_case_id", "candidate_id", "catalog_version", "contract_fingerprint") if not getattr(context, name)]
    if missing:
        raise BacktesterWebInputError("ModuleHostContext缺少正式运行字段：" + ",".join(missing))
    if context.contract_fingerprint != contract.contract_fingerprint:
        raise BacktesterWebInputError("ModuleHostContext.contract_fingerprint与ResolvedContract不一致")
    if context.contract_ref is None or context.contract_ref.schema_id != RESOLVED_CONTRACT_SCHEMA_ID:
        raise BacktesterWebInputError("Backtester正式运行缺少Core验证的ResolvedContract引用")
    if context.contract_ref.content_hash != contract.contract_fingerprint:
        raise BacktesterWebInputError("ModuleHostContext.contract_ref与ResolvedContract不一致")


def _formal_input_snapshot(
    contract: ResolvedContract,
    backtest_config: Mapping[str, Any],
    historical_data: DataAssetRef | Mapping[str, Any],
) -> dict[str, Any]:
    """正式Run快照只保存可验证合同、配置及经净化的资产引用。"""
    raw_reference = asdict(historical_data) if isinstance(historical_data, DataAssetRef) else dict(historical_data)
    try:
        reference = validate_data_asset_ref(raw_reference)
    except HistoricalDataError:
        reference = {
            "status": "invalid_data_asset_ref",
            "data_asset_id": _safe_snapshot_identifier(raw_reference.get("data_asset_id")),
            "content_hash": _safe_snapshot_hash(raw_reference.get("content_hash")),
        }
    return {
        "contract": contract.to_protocol_dict(),
        "backtest_config": dict(backtest_config),
        "historical_data": reference,
    }


def _safe_snapshot_identifier(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text if _IDENTIFIER.fullmatch(text) else None


def _safe_snapshot_hash(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else None


def _validate_formal_data_ref(reference: DataAssetRef, *, tenant_id: str) -> None:
    """先验证逻辑资产授权，再让Host私有DataStore读取原始字节。"""
    if reference.tenant_id != tenant_id:
        raise BacktesterWebInputError("DataAssetRef租户与Host调用方不一致")
    if "read" not in reference.access_scope:
        raise BacktesterWebInputError("DataAssetRef.access_scope缺少read权限")
    if reference.schema_id != "market-history-v1" or reference.media_type != "text/csv":
        raise BacktesterWebInputError("正式回测只接受market-history-v1 text/csv历史资产")


def _require_contract_calendar(contract: ResolvedContract, history: HistoricalData) -> None:
    """一笔正式回测只能使用与冻结合同完全相同的已验证交易日历。"""
    identity = contract.identity
    if (
        identity.get("calendar_id") != history.calendar_id
        or identity.get("calendar_version") != history.calendar_version
    ):
        raise BacktesterWebInputError("ResolvedContract交易日历与DataAssetRef.calendar_id/calendar_version不一致")


def _safe_formal_error_message(error: Exception) -> str:
    """失败Run保留阶段语义，不把外置Store的物理路径带入公共错误。"""
    message = str(error).strip()
    if not message or "/" in message or "\\" in message:
        return "正式历史数据读取或校验失败"
    return message


def _identity(body: Mapping[str, Any]) -> dict[str, Any]:
    supplied = body.get("identity", {})
    if not isinstance(supplied, Mapping):
        raise BacktesterWebInputError("identity必须为对象")
    underlyings = supplied.get("underlyings", ["000905.SH"])
    if isinstance(underlyings, str):
        underlyings = [item.strip() for item in underlyings.split(",") if item.strip()]
    if not isinstance(underlyings, list):
        raise BacktesterWebInputError("identity.underlyings必须为列表")
    identity = {
        "contract_id": supplied.get("contract_id", body.get("contract_id")),
        "underlyings": underlyings,
        "currency": supplied.get("currency", "CNY"),
        "contract_start_date": supplied.get("contract_start_date"),
    }
    return {key: value for key, value in identity.items() if value is not None}


def _term_overrides(body: Mapping[str, Any]) -> dict[str, Any]:
    value = body.get("term_overrides", {})
    if not isinstance(value, Mapping):
        raise BacktesterWebInputError("term_overrides必须为对象")
    return dict(value)


def _backtest_config(body: Mapping[str, Any]) -> BacktestConfig:
    supplied = body.get("backtest_config", {})
    if not isinstance(supplied, Mapping):
        raise BacktesterWebInputError("backtest_config必须为对象")
    return BacktestConfig.from_mapping({key: value for key, value in supplied.items() if value is not None})


def _history_reference(body: Mapping[str, Any]) -> Any:
    return body.get("history_reference")


def _text(value: Any, name: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise BacktesterWebInputError(f"{name}不能为空")
    return text


def _identifier(value: Any, prefix: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        return f"{prefix}-{uuid4().hex[:12]}"
    if not _IDENTIFIER.fullmatch(text):
        raise BacktesterWebInputError(f"{prefix}_id只能使用字母、数字、.、_、-")
    return text


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _write_run(
    *,
    result_store: Any,
    tenant_id: str,
    task_id: str,
    run_id: str,
    input_snapshot: Mapping[str, Any],
    output: Mapping[str, Any],
    private_audit_ledger: list[dict[str, Any]],
) -> dict[str, str]:
    """把唯一正式结果交给Core原子提交，模块不再直接写运行目录。"""
    backtest_payload = output["backtest"]
    files: dict[str, Any] = {
        "manifest.json": {
            "schema_version": "backtester.module-run.v1.0.0",
            "module": "backtester",
            "tenant_id": tenant_id,
            "task_id": task_id,
            "run_id": run_id,
            "analysis_case_id": output.get("analysis_case_id"),
            "candidate_id": output.get("candidate_id"),
            "catalog_version": output.get("catalog_version"),
            "status": "succeeded",
            "created_at": output["created_at"],
            "contract_fingerprint": backtest_payload["contract_fingerprint"],
            "execution_fingerprint": output["execution_fingerprint"],
            "artifacts": ["artifacts/backtest_result.json", "artifacts/trade_ledger.csv", "artifacts/branch_coverage.json"],
        },
        "input_snapshot.json": dict(input_snapshot),
        "resolved_contract.json": output["resolved_contract"],
        "data_refs.json": _json_text(output["data_refs"]),
        "limitations.json": _json_text(output["limitations"]),
        "result.json": dict(output),
        "artifacts/backtest_result.json": backtest_payload,
        "artifacts/trade_ledger.csv": _trade_ledger_csv(backtest_payload["trade_ledger"]),
        "artifacts/branch_coverage.json": backtest_payload["branch_coverage"],
        "private/audit_trade_ledger.json": _json_text(private_audit_ledger),
    }
    reference = result_store.commit_module_run(
        module="backtester",
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=run_id,
        files=files,
    )
    if not isinstance(reference, ModuleRunRef):
        raise BacktesterWebInputError("result_store未返回正式ModuleRunRef")
    return asdict(reference)


def _write_failed_run(
    *,
    result_store: Any,
    tenant_id: str,
    task_id: str,
    run_id: str,
    input_snapshot: Mapping[str, Any],
    output: Mapping[str, Any],
) -> dict[str, str]:
    """提交可解析的失败ModuleRun；不伪造result.json或公开计算结果。"""
    files: dict[str, Any] = {
        "manifest.json": {
            "schema_version": "backtester.module-run.v1.0.0",
            "module": "backtester",
            "tenant_id": tenant_id,
            "task_id": task_id,
            "run_id": run_id,
            "analysis_case_id": output.get("analysis_case_id"),
            "candidate_id": output.get("candidate_id"),
            "catalog_version": output.get("catalog_version"),
            "status": "failed",
            "created_at": output["created_at"],
            "contract_fingerprint": output["contract_fingerprint"],
            "artifacts": [],
        },
        "input_snapshot.json": dict(input_snapshot),
        "resolved_contract.json": output["resolved_contract"],
        "data_refs.json": _json_text(output["data_refs"]),
        "limitations.json": _json_text(output["limitations"]),
        "error.json": dict(output["error"]),
    }
    reference = result_store.commit_module_run(
        module="backtester",
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=run_id,
        files=files,
    )
    if not isinstance(reference, ModuleRunRef):
        raise BacktesterWebInputError("result_store未返回正式ModuleRunRef")
    return asdict(reference)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def _trade_ledger_csv(trades: Any) -> str:
    output = StringIO(newline="")
    columns = (
        "trade_id", "entry_date", "exit_date", "path_id", "case_id", "gross_contract_return",
        "trade_json",
    )
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for trade in trades:
        writer.writerow({
            **{name: trade.get(name) for name in columns[:-1]},
            "trade_json": json.dumps(trade, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False),
        })
    return output.getvalue()


def _port_failure(error: DataFetcherPortUnavailable) -> dict[str, Any]:
    message = str(error)
    data_store_missing = "DataStore" in message or "read_bytes" in message
    return {
        "ok": False,
        "schema_version": "backtester.error.v1.0.0",
        "module": "backtester",
        "status": "failed",
        "error": {
            "error_code": "data_store_port_unavailable" if data_store_missing else "historical_data_port_unavailable",
            "stage": "historical_data",
            "message": message,
            "retryable": True,
            "missing_inputs": ["data_store.read_bytes" if data_store_missing else "historical_data_port"],
            "upstream_refs": [],
        },
        "required_ports": ["historical_data_port", "data_store"],
    }


class Handler(BaseHTTPRequestHandler):
    runtime = BacktesterRuntime()

    def log_message(self, *_: Any) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        assets = {
            "/backtester.css": (PAGE_DIR / "backtester.css", "text/css; charset=utf-8"),
            "/backtester.js": (PAGE_DIR / "backtester.js", "application/javascript; charset=utf-8"),
            "/ui/style.css": (UI_DIR / "style.css", "text/css; charset=utf-8"),
            "/ui/controls.css": (UI_DIR / "controls.css", "text/css; charset=utf-8"),
            "/vendor/echarts.min.js": (VENDOR_DIR / "echarts.min.js", "application/javascript; charset=utf-8"),
        }
        if self.path == "/api/status":
            return self._json(HTTPStatus.OK, {"ok": True, "module": "backtester", "web_ui_version": WEB_UI_VERSION})
        if self.path == "/api/catalog":
            return self._json(HTTPStatus.OK, self.runtime.catalog())
        if self.path in {"/", "/backtester.html"}:
            return self._file(PAGE, "text/html; charset=utf-8")
        brand_asset = page_asset_path(self.path)
        if brand_asset is not None:
            return self._file(brand_asset, "image/svg+xml")
        if self.path in assets:
            return self._file(*assets[self.path])
        return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "未找到资源"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/run":
            return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "未找到接口"})
        return self._json(HTTPStatus.CONFLICT, {
            "ok": False,
            "module": "backtester",
            "status": "host_binding_required",
            "message": "独立页面不执行回测；请在OptionHelper App Host中绑定ResolvedContract、DataAssetRef、DataStore和ResultStore后运行。",
        })

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
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    run_host()


if __name__ == "__main__":
    main()
