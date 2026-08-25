"""Pricer页面目录与正式Module Host定价服务。"""

from __future__ import annotations

import csv
from dataclasses import asdict, replace
import hashlib
import io
import json
import os
from datetime import date, datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


from runtime.bootstrap import bootstrap_runtime
from runtime.adapters.local_store import LocalResultStore
from runtime.contracts.contract_api import (
    ContractResolutionError,
    RESOLVED_CONTRACT_SCHEMA_ID,
    ResolvedContract,
    verify_product_snapshot_binding,
)
from runtime.contracts.contract_types import semantic_hash
from runtime.knowledger import load_registry
from runtime.protocol.models import (
    DataAssetRef,
    ObservedContractState as ProtocolObservedContractState,
    PricingInput as ProtocolPricingInput,
)
from runtime.protocol.module_host import ModuleHostContext, require_host_bound_run_contract

from .config import PricingConfig
from .models import HistoricalData, PricingInput, TradingCalendarData, validate_market_data_asset
from .engines.pricing_core.optionhelper_core import capability_for
from .engines.pricing_core.engine.derivatives.results import (
    project_public_percent,
    redact_public_money_compatibility,
)
from .product_pricing_adapter import product_mapping
from .valuation_solver import price
from .input_defaults import (
    PricerInputDefaultError,
    local_valuation_date,
    validate_frozen_contract_reference,
)
from .market_resolver import load_market_history_bytes


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
_TEST_ONLY_PRICING_FIELDS = frozenset({"demo_mode", "demo_calendar"})


def _path_summaries(product: Mapping[str, Any]) -> list[dict[str, str]]:
    """仅投影产品路径名称与条件，避免向Catalog暴露收益表达式。"""
    summaries: list[dict[str, str]] = []
    for index, path in enumerate(product.get("paths", ()), start=1):
        condition = str(path.get("condition", "")).strip()
        summaries.append({
            "title": f"路径{index}",
            "condition": "全部情形" if not condition or condition.casefold() == "true" else condition,
        })
    return summaries


class PricerWebInputError(ValueError):
    """Pricer页面请求未满足合同或定价输入边界。"""


def static_assets() -> dict[str, tuple[Path, str]]:
    """页面在开发仓库和发行Skill均使用的根资源路径。"""
    return {
        "/icons/optionhelper-app-icon-tile-light.svg": (ICON_DIR / "optionhelper-app-icon-tile-light.svg", "image/svg+xml"),
        "/assets/icons/optionhelper-app-icon-tile-light.svg": (ICON_DIR / "optionhelper-app-icon-tile-light.svg", "image/svg+xml"),
        "/ui/style.css": (UI_DIR / "style.css", "text/css; charset=utf-8"),
        "/ui/controls.css": (UI_DIR / "controls.css", "text/css; charset=utf-8"),
        "/ui/greeks-workbench.css": (UI_DIR / "greeks-workbench.css", "text/css; charset=utf-8"),
        "/ui/date-control.js": (UI_DIR / "date-control.js", "application/javascript; charset=utf-8"),
        "/ui/greeks-workbench.js": (UI_DIR / "greeks-workbench.js", "application/javascript; charset=utf-8"),
        "/vendor/echarts.min.js": (VENDOR_DIR / "echarts.min.js", "application/javascript; charset=utf-8"),
        "/vendor/echarts-gl.min.js": (VENDOR_DIR / "echarts-gl.min.js", "application/javascript; charset=utf-8"),
    }


class PricerRuntime:
    """Pricer目录读取、正式输入编译、定价和结果落盘。"""

    module_name = "pricer"

    def __init__(self, *, data_port: Any | None = None, result_store: Any | None = None) -> None:
        # 正式行情和交易日历只能由App Host注入的DataAssetReadPort读取。
        self._host_data_port = data_port is not None
        self.data_port = data_port
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
                if key in {"monitor", "pricing_methods", "constraints", "derived_terms", "N", "Nvar", "Nvega"}:
                    continue
                # OptionReg may carry formula metadata used by the shared
                # evaluator (for example payoff_normalizer).  Only fields
                # declared in term_catalog are user-editable page inputs.
                metadata = catalog.get(key)
                if not isinstance(metadata, Mapping):
                    continue
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
                "path_count": len(product["paths"]),
                "path_summaries": _path_summaries(product),
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
            "config_fields": [
                name for name in PricingConfig.__dataclass_fields__
                if name not in _TEST_ONLY_PRICING_FIELDS
            ],
        }

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
        if len(protocol_input.market_data_refs) == 0:
            raise PricerWebInputError("正式Pricer当前要求唯一受控market_data_ref")
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
                market_data_refs=(data_ref,),
                trading_calendar_data=calendar_data,
                trading_calendar_ref=calendar_ref,
                observed_contract_state=observed_state,
            ))
        except (TypeError, ValueError, PricerInputDefaultError) as error:
            raise PricerWebInputError(str(error)) from error

        task_id = str(host_context.task_id)
        run_id = f"run-{uuid4().hex[:12]}"
        effective_tenant = tenant_id or data_ref.tenant_id
        if effective_tenant != data_ref.tenant_id:
            raise PricerWebInputError("DataAssetRef.tenant_id与Host tenant不一致")
        if calendar_ref is not None and effective_tenant != calendar_ref.tenant_id:
            raise PricerWebInputError("trading_calendar_ref.tenant_id与Host tenant不一致")
        data_refs = [_data_ref_dict(data_ref)]
        if calendar_ref is not None:
            data_refs.append(_data_ref_dict(calendar_ref))
        private_input_snapshot = {
            "contract": contract.to_protocol_dict(),
            "pricing_config": config.to_dict(),
            "market_data_refs": [_data_ref_dict(data_ref)],
            "trading_calendar_ref": None if calendar_ref is None else _data_ref_dict(calendar_ref),
            "observed_contract_state": observed_state,
        }
        limitations = list(pricing_result.limitations)
        status = "succeeded" if pricing_result.status == "priced" else "unsupported"
        execution_fingerprint = semantic_hash(private_input_snapshot)
        input_snapshot = redact_public_money_compatibility(private_input_snapshot)
        resolved_contract = redact_public_money_compatibility(contract.to_protocol_dict())
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
            "created_by": data_ref.created_by,
            "access_scope": list(data_ref.access_scope),
            "candidate_id": candidate_id,
            "catalog_version": catalog_version,
            "execution_fingerprint": execution_fingerprint,
            "contract_fingerprint": contract.contract_fingerprint,
            "resolved_contract": resolved_contract,
            "data_refs": data_refs,
            "input_snapshot": input_snapshot,
            "limitations": limitations,
            "market_snapshot": pricing_result.market_snapshot,
            "pricing": redact_public_money_compatibility(pricing_result.to_public_percent_dict()),
        }
        # DataAssetRef provenance is Host-controlled but may include arbitrary
        # metadata.  Project the whole public envelope once, after every
        # public component has been assembled, so a nested lineage field
        # cannot bypass the 100-point result contract.
        output = redact_public_money_compatibility(project_public_percent(output))
        # ResultStore verifies the frozen economic contract by recomputing its
        # fingerprint.  Keep that controlled snapshot separate from the public
        # percent-only envelope; writing the redacted projection here would
        # make a successful ModuleRun unverifiable.
        files = _module_run_files(
            output,
            controlled_contract=contract.to_protocol_dict(),
        )
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
        return redact_public_money_compatibility(project_public_percent(output))

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
        """DataAssetRef唯一loader：验证Host字节后交由统一资产校验入口。"""
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
        return historical, ref


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
        _validate_formal_pricing_config(request.pricing_config)
        verify_product_snapshot_binding(request.contract, load_registry())
        observed_state = request.observed_contract_state
        return request, (
            None if observed_state is None else observed_state.to_protocol_dict()
        )
    values = dict(request)
    allowed = {
        "contract", "pricing_config", "market_data_refs", "trading_calendar_ref",
        "observed_contract_state",
    }
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
    _validate_formal_pricing_config(config)
    if isinstance(refs, (str, bytes)) or not isinstance(refs, (tuple, list)):
        raise PricerWebInputError("PricingInput.market_data_refs必须为DataAssetRef列表")
    observed_state_value = values.get("observed_contract_state")
    if observed_state_value is None:
        observed_state = None
    elif isinstance(observed_state_value, ProtocolObservedContractState):
        observed_state = observed_state_value
    elif isinstance(observed_state_value, Mapping):
        try:
            observed_state = ProtocolObservedContractState.from_host_payload(observed_state_value)
        except (TypeError, ValueError) as error:
            raise PricerWebInputError(f"PricingInput.observed_contract_state无效：{error}") from error
    else:
        raise PricerWebInputError("PricingInput.observed_contract_state必须为Host冻结状态")
    return ProtocolPricingInput(
        contract=contract,
        pricing_config=dict(config),
        market_data_refs=tuple(_data_asset_ref(value) for value in refs),
        trading_calendar_ref=(
            None if values.get("trading_calendar_ref") is None
            else _data_asset_ref(values["trading_calendar_ref"])
        ),
        observed_contract_state=observed_state,
    ), None if observed_state is None else observed_state.to_protocol_dict()


def _validate_formal_pricing_config(config: Mapping[str, Any]) -> None:
    forbidden = _TEST_ONLY_PRICING_FIELDS.intersection(config)
    if forbidden:
        raise PricerWebInputError(
            "正式PricingInput.pricing_config不接受测试字段：" + "、".join(sorted(forbidden))
        )


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
    if context.contract_ref.schema_id != RESOLVED_CONTRACT_SCHEMA_ID:
        raise PricerWebInputError("ModuleHostContext.contract_ref不是当前ResolvedContract引用")
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


def _data_ref_dict(ref: DataAssetRef) -> dict[str, Any]:
    return asdict(ref)


def _module_run_files(
    output: Mapping[str, Any],
    *,
    controlled_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build public result artifacts plus the one Store-only contract snapshot."""
    output = redact_public_money_compatibility(output)
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
            {"name": "pricing_result.json", "media_type": "application/json"},
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
        # This file is consumed only by Core ResultStore integrity validation.
        # All user-facing JSON/CSV artifacts below retain the percent-only
        # projection in ``output``.
        "resolved_contract.json": (
            dict(controlled_contract)
            if controlled_contract is not None else output["resolved_contract"]
        ),
        "data_refs.json": {"data_refs": output["data_refs"]},
        "limitations.json": {"limitations": output["limitations"]},
        "artifacts/pricing_result.json": dict(output),
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
        "task_id", "run_id", "product_id", "status", "method", "value_basis",
        "pv_percent", "standard_error_percent", "quote_eligible", "precision_status",
        "contract_fingerprint",
    ))
    writer.writeheader()
    writer.writerow({
        "task_id": output["task_id"],
        "run_id": output["run_id"],
        "product_id": pricing.get("product_id"),
        "status": pricing.get("status"),
        "method": pricing.get("method"),
        "value_basis": pricing.get("value_basis"),
        "pv_percent": pricing.get("pv_percent"),
        "standard_error_percent": pricing.get("standard_error_percent"),
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


def _with_default_valuation_date(config: PricingConfig) -> PricingConfig:
    """Apply the device-local default once, before any DataAsset lookup."""
    return config if config.valuation_date is not None else replace(
        config, valuation_date=local_valuation_date(date.today()),
    )


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Handler(BaseHTTPRequestHandler):
    runtime = PricerRuntime()

    def log_message(self, *_: Any) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        assets = {
            **static_assets(),
        }
        if self.path == "/api/status":
            return self._json(HTTPStatus.OK, {"ok": True, "module": "pricer"})
        if self.path == "/api/catalog":
            return self._json(HTTPStatus.OK, self.runtime.catalog())
        if self.path in {"/", "/pricer.html"}:
            return self._file(PAGE, "text/html; charset=utf-8")
        if self.path in assets:
            return self._file(*assets[self.path])
        return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "未找到资源"})

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
    """提供页面预览、静态资源和产品目录；正式定价由Module Host调用。"""
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    run_host(host=HOST, port=PORT)


if __name__ == "__main__":
    main()
