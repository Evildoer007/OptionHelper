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
from typing import Any, Callable, Mapping
from uuid import uuid4

import pandas as pd

from runtime.bootstrap import bootstrap_runtime
from runtime.adapters.local_store import LocalResultStore
from runtime.contracts.contract_api import (
    ContractResolutionError,
    RESOLVED_CONTRACT_SCHEMA_ID,
    ResolvedContract,
    verify_product_snapshot_binding,
)
from runtime.contracts.input_adapter import (
    bind_candidate_target_spec,
    recompile_candidate_contract,
    validate_initial_pricing_time_context,
    verified_trading_calendar,
)
from runtime.contracts.contract_types import semantic_hash
from runtime.knowledger import load_registry
from runtime.protocol.models import (
    DataAssetRef,
    ObservedContractState as ProtocolObservedContractState,
    PricingInput as ProtocolPricingInput,
    PricingObjective,
)
from runtime.protocol.module_host import ModuleHostContext, require_host_bound_run_contract

from .config import PricingConfig
from .models import HistoricalData, PricingInput, TradingCalendarData, validate_market_data_asset
from .engines.pricing_core.optionhelper_core import capability_for
from .engines.pricing_core.engine.derivatives.results import (
    project_public_percent,
    redact_public_money_compatibility,
)
from .product_pricing_adapter import ProductNotAvailable, ProductPricingAdapter, product_mapping
from .valuation_solver import price
from .fair_parameter import (
    encode_public_value,
    eligibility_for_target,
    private_capability_package,
    public_capability_package,
    target_capability,
)
from .fair_solver import (
    FairParameterSolveError,
    FairParameterSolver,
    ScalarValuation,
    deterministic_uncertainty,
    monte_carlo_uncertainty,
)
from .observed_state import ObservedContractState, ObservedStateError
from .input_defaults import (
    PricerInputDefaultError,
    local_valuation_date,
    validate_frozen_contract_reference,
)
from .market_resolver import load_market_history_bytes, market_snapshot_from_history


MODULE_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_PATHS = bootstrap_runtime(MODULE_ROOT)
PROJECT_ROOT = RUNTIME_PATHS.project_root
RESULT_ROOT = RUNTIME_PATHS.result_root
PAGE_DIR = RUNTIME_PATHS.module_page_dir("pricer")
PAGE = PAGE_DIR / "pricer.html"
UI_DIR = PAGE_DIR / "ui"
VENDOR_DIR = PAGE_DIR / "vendor"
BROWSER_DIR = PROJECT_ROOT / "core" / "src" / "runtime" / "browser"
ICON_DIR = PROJECT_ROOT / "assets" / "icons"
HOST = "127.0.0.1"
PORT = int(os.environ.get("OPTIONHELPER_PRICER_PORT", "4280"))
_TEST_ONLY_PRICING_FIELDS = frozenset({"demo_mode", "demo_calendar"})
_NON_EDITABLE_CONTRACT_TERMS = frozenset({
    "monitor",
    "pricing_methods",
    "constraints",
    "derived_terms",
    "S0",
    "S0Vec",
    "N",
    "Nvar",
    "Nvega",
    # These are frozen product facts, not pricing knobs.  The current catalog
    # settles in cash and Core rejects margin_call overrides by contract.
    "settlement",
    "margin_call",
})
_QUOTE_TERM_EXCLUSIONS = _NON_EDITABLE_CONTRACT_TERMS | frozenset({
    "exercise_style",
    "observation_price",
    "event_priority",
    "hedge_ki_history_policy",
    "payoff_figure_basis",
    "annualization_days",
})
_QUOTE_TERM_LABELS = {
    "Pi_0": "期权费",
    "P_net": "净期权费",
}
_SCHEDULE_LABELS = {
    "daily": "每个交易日",
    "monthly_last": "每月最后交易日",
    "maturity": "到期日",
}
_QUOTE_PRICE_CONVENTIONS = frozenset({"normalized_100", "absolute_market"})
_QUOTE_TEXT_UNITS = frozenset({"enum", "flag", "schedule", "schedule_selector", "unit"})


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


def _quote_price_convention(contract: ResolvedContract) -> str:
    value = contract.identity.get("price_convention")
    if not isinstance(value, str) or value not in _QUOTE_PRICE_CONVENTIONS:
        raise PricerWebInputError("ResolvedContract.price_convention不是Pricer正式报价口径")
    return value


def _quote_public_unit(catalog_unit: str, price_convention: str) -> str:
    if catalog_unit == "price":
        return "price_normalized_percent" if price_convention == "normalized_100" else "market_price"
    if catalog_unit in {"normalized_point", "rate", "volatility"}:
        return "percent"
    if catalog_unit == "year":
        return "year"
    if catalog_unit in {"day", "count", "observation_count"}:
        return "count"
    if catalog_unit in _QUOTE_TEXT_UNITS:
        return "text"
    raise PricerWebInputError(f"报价条款单位不受支持：{catalog_unit}")


def _quote_display_value(value: Any, catalog_unit: str, price_convention: str) -> str:
    if isinstance(value, (list, tuple)):
        return "、".join(_quote_display_value(item, catalog_unit, price_convention) for item in value)
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, bool):
        return "是" if value else "否"
    if catalog_unit == "year" and isinstance(value, (int, float)):
        months = float(value) * 12.0
        if abs(months - round(months)) < 1e-9 and round(months) < 12:
            return f"{round(months)}个月"
        return f"{float(value):g}年"
    if catalog_unit == "price" and isinstance(value, (int, float)):
        suffix = "%" if price_convention == "normalized_100" else ""
        return f"{float(value):g}{suffix}"
    if catalog_unit == "normalized_point" and isinstance(value, (int, float)):
        return f"{float(value):g}%"
    if catalog_unit in {"rate", "volatility"} and isinstance(value, (int, float)):
        return f"{float(value) * 100:g}%"
    if catalog_unit == "day" and isinstance(value, (int, float)):
        return f"{float(value):g}天"
    if catalog_unit in {"count", "observation_count"} and isinstance(value, (int, float)):
        return f"{float(value):g}次"
    if catalog_unit == "schedule_selector":
        return _SCHEDULE_LABELS.get(str(value), str(value))
    return str(value)


def _reporter_quote_fact(
    *,
    contract: ResolvedContract,
    pricing: Mapping[str, Any],
    data_ref: DataAssetRef,
) -> dict[str, Any] | None:
    """Freeze the exact contract and valuation facts consumed by Reporter Quote."""
    if pricing.get("quote_eligible") is not True:
        return None
    price_convention = _quote_price_convention(contract)
    registry = load_registry()
    catalog = registry["term_catalog"]
    terms: list[dict[str, str]] = []
    for key, value in contract.terms.items():
        if key in _QUOTE_TERM_EXCLUSIONS:
            continue
        if key not in catalog:
            raise PricerWebInputError(f"报价条款未注册：{key}")
        metadata = catalog[key]
        symbol = metadata.get("symbol")
        catalog_unit = metadata.get("unit")
        name = metadata.get("name_zh")
        if not isinstance(symbol, str) or not symbol.strip():
            raise PricerWebInputError(f"报价条款{key}缺少受控symbol")
        if not isinstance(catalog_unit, str) or not catalog_unit:
            raise PricerWebInputError(f"报价条款{key}缺少受控unit")
        if not isinstance(name, str) or not name.strip():
            raise PricerWebInputError(f"报价条款{key}缺少中文名称")
        terms.append({
            "symbol": symbol,
            "label": _QUOTE_TERM_LABELS.get(key, name),
            "value": _quote_display_value(value, catalog_unit, price_convention),
            "unit": _quote_public_unit(catalog_unit, price_convention),
        })
    if not terms:
        raise PricerWebInputError("正式报价没有可公开的受控条款")
    lineage = data_ref.lineage
    provider = str(lineage.get("provider") or lineage.get("source") or "受控市场数据")
    applicable_date = str(pricing.get("market_snapshot", {}).get("valuation_date") or "")
    try:
        date.fromisoformat(applicable_date)
    except ValueError as error:
        raise PricerWebInputError("正式报价缺少有效估值日") from error
    precision_status = pricing.get("precision_status")
    if not isinstance(precision_status, str) or not precision_status:
        raise PricerWebInputError("正式报价缺少precision_status")
    return {
        "schema": "optionhelper.reference-quote-fact",
        "source": {"provider": provider, "reference_id": data_ref.data_asset_id},
        "applicable_date": applicable_date,
        "quote_eligible": True,
        "precision_status": precision_status,
        "price_convention": price_convention,
        "terms": terms,
    }


class PricerWebInputError(ContractResolutionError):
    """Pricer请求未满足合同或定价输入边界。"""

    code = "pricer_input_invalid"
    status = "needs_input"
    stage = "pricer_input"
    retryable = False

    def to_protocol_dict(self) -> dict[str, Any]:
        """Stable module-owned classification for Host error projection."""
        return {
            "ok": False,
            "module": "pricer",
            "status": self.status,
            "failure_code": self.code,
            "stage": self.stage,
            "message": str(self),
            "retryable": self.retryable,
        }


def static_assets() -> dict[str, tuple[Path, str]]:
    """页面在开发仓库和发行Skill均使用的根资源路径。"""
    return {
        "/icons/optionhelper-app-icon-tile-light.svg": (ICON_DIR / "optionhelper-app-icon-tile-light.svg", "image/svg+xml"),
        "/assets/icons/optionhelper-app-icon-tile-light.svg": (ICON_DIR / "optionhelper-app-icon-tile-light.svg", "image/svg+xml"),
        "/ui/style.css": (UI_DIR / "style.css", "text/css; charset=utf-8"),
        "/ui/controls.css": (UI_DIR / "controls.css", "text/css; charset=utf-8"),
        "/ui/greeks-workbench.css": (UI_DIR / "greeks-workbench.css", "text/css; charset=utf-8"),
        "/date-input-control.js": (BROWSER_DIR / "date_input_control.js", "application/javascript; charset=utf-8"),
        "/date-input-control.css": (BROWSER_DIR / "date_input_control.css", "text/css; charset=utf-8"),
        "/ui/greeks-workbench.js": (UI_DIR / "greeks-workbench.js", "application/javascript; charset=utf-8"),
        "/plotly-chart-system.js": (BROWSER_DIR / "plotly_chart_system.js", "application/javascript; charset=utf-8"),
        "/vendor/plotly-optionhelper.min.js": (BROWSER_DIR / "vendor" / "plotly-optionhelper.min.js", "application/javascript; charset=utf-8"),
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
        fair_package = public_capability_package()
        fair_by_product = {
            item["product_id"]: item
            for item in fair_package["products"]
        }
        products: list[dict[str, Any]] = []
        for product_id, product in registry["products"].items():
            terms = product["terms"]
            capability = capability_for(product_id)
            mapping = product_mapping(product_id)
            fields = []
            for key, value in terms.items():
                if key in _NON_EDITABLE_CONTRACT_TERMS:
                    continue
                # OptionReg may carry formula metadata used by the shared
                # evaluator. Internal normalized coordinates and settlement
                # bases stay in ResolvedContract; only public term_catalog
                # fields are projected as user-editable page inputs.
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
            fair_product = fair_by_product.get(product_id, {})
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
                "fair_parameter_status": fair_product.get("fair_parameter_status", "valuation_only"),
                "fair_parameter_targets": fair_product.get("targets", []),
            })
        return {
            "ok": True,
            "module": self.module_name,
            "products": products,
            "config_fields": [
                name for name in PricingConfig.__dataclass_fields__
                if name not in _TEST_ONLY_PRICING_FIELDS
            ],
            "fair_parameter_capability": fair_package,
        }

    def eligibility(
        self,
        request: ResolvedContract | Mapping[str, Any] | None = None,
        *,
        contract: ResolvedContract | None = None,
        target_id: str | None = None,
        method: str | None = None,
        host_context: ModuleHostContext | None = None,
    ) -> dict[str, Any]:
        """Read-only contract/target/method pre-qualification.

        This method intentionally has no data, pricing-config, observed-state
        or calendar parameter.  It therefore cannot fetch market bytes or run
        a numerical solver as part of the pre-qualification action.
        """
        if request is not None:
            if isinstance(request, ResolvedContract):
                if contract is not None:
                    raise PricerWebInputError("eligibility不得重复提交contract")
                contract = request
            elif isinstance(request, Mapping):
                values = dict(request)
                allowed = {"contract", "target_id", "method"}
                unknown = set(values) - allowed
                if unknown:
                    raise PricerWebInputError("eligibility含未知字段：" + "、".join(sorted(unknown)))
                if contract is not None or target_id is not None or method is not None:
                    raise PricerWebInputError("eligibility请求字段不得与关键字参数重复")
                contract_value = values.get("contract")
                contract = _resolved_contract_value(contract_value)
                target_id = values.get("target_id")
                method = values.get("method")
            else:
                raise PricerWebInputError("eligibility.contract必须为完整ResolvedContract")
        if not isinstance(contract, ResolvedContract):
            raise PricerWebInputError("eligibility必须由Host注入完整ResolvedContract")
        if not isinstance(host_context, ModuleHostContext):
            raise PricerWebInputError("eligibility必须由Host注入ModuleHostContext")
        _require_host_eligibility_contract(contract, host_context)
        if not isinstance(target_id, str) or not target_id.strip():
            raise PricerWebInputError("eligibility.target_id必须为非空标识符")
        if not isinstance(method, str) or not method.strip():
            raise PricerWebInputError("eligibility.method必须为analytical或monte_carlo")
        decision = eligibility_for_target(contract, target_id.strip(), method.strip())
        package = public_capability_package()
        return {
            "ok": True,
            "module": "pricer",
            "action": "eligibility",
            "status": decision["status"],
            "eligible": decision["eligible"],
            "reason": decision["reason"],
            "product_id": contract.product_id,
            "target_id": decision["target_id"],
            "method": decision["method"],
            "capability_version": package["capability_version"],
            "capability_hash": package["capability_hash"],
        }

    def run_formal(
        self,
        request: ProtocolPricingInput | Mapping[str, Any],
        *,
        host_context: ModuleHostContext | None = None,
        tenant_id: str | None = None,
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        """正式Tool入口：直接消费Host冻结的ResolvedContract和DataAssetRef。"""
        protocol_input, observed_state = _formal_pricing_input(request)
        contract = protocol_input.contract
        if not isinstance(contract, ResolvedContract):
            raise PricerWebInputError("正式PricingInput.contract必须为ResolvedContract")
        if not isinstance(host_context, ModuleHostContext):
            raise PricerWebInputError("正式Pricer调用必须由Host注入ModuleHostContext")
        _require_host_contract(contract, host_context)
        objective = protocol_input.pricing_objective
        if objective is not None and objective.mode == "fair_parameter":
            return self._run_fair_parameter(
                protocol_input,
                observed_state,
                host_context,
                tenant_id=tenant_id,
                cancelled=cancelled,
                progress=progress,
            )
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
        private_pricing_audit = pricing_result.to_dict()
        pricing_payload = redact_public_money_compatibility(pricing_result.to_public_percent_dict())
        pricing_payload["price_convention"] = _quote_price_convention(contract)
        quote_fact = _reporter_quote_fact(
            contract=contract,
            pricing=pricing_payload,
            data_ref=data_ref,
        )
        if quote_fact is not None:
            pricing_payload["reporter_quote_fact"] = quote_fact
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
            "pricing": pricing_payload,
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
            private_pricing_audit=private_pricing_audit,
        )
        require_host_bound_run_contract(output, host_context)
        reference = self.result_store.commit_module_run(
            module="pricer",
            tenant_id=effective_tenant,
            task_id=task_id,
            run_id=run_id,
            files=files,
        )
        stored_run = self.result_store.read_module_run_bundle(
            reference,
            tenant_id=effective_tenant,
        )
        output.update({
            "module_run_ref": asdict(reference),
            "artifact_manifest": _json_object_bytes(
                stored_run["artifacts/artifact_manifest.json"],
                "ModuleRun产物清单",
            ),
            "commit_marker": _json_object_bytes(
                stored_run["commit_marker.json"],
                "ModuleRun提交标记",
            ),
        })
        return redact_public_money_compatibility(project_public_percent(output))

    def _run_fair_parameter(
        self,
        protocol_input: ProtocolPricingInput,
        observed_state_payload: Mapping[str, Any] | None,
        host_context: ModuleHostContext,
        *,
        tenant_id: str | None,
        cancelled: Callable[[], bool] | None,
        progress: Callable[..., Any] | None,
    ) -> dict[str, Any]:
        """Solve one registered fair parameter without entering ordinary PV.

        The fair path is intentionally separate from the ordinary valuation
        branch above.  It freezes the authenticated inputs once, recompiles
        only the candidate contract through Core, and invokes a fresh
        ``ProductPricingAdapter`` for each solver point.  No page-provided
        target rule or recursive formal request is accepted here.
        """

        contract = protocol_input.contract
        objective = protocol_input.pricing_objective
        target_id = None if objective is None else objective.target_id
        if not isinstance(target_id, str) or not target_id.strip():
            raise PricerWebInputError("fair_parameter必须提供受控target_id")
        target_id = target_id.strip()

        try:
            config = _with_default_valuation_date(
                PricingConfig.from_mapping(protocol_input.pricing_config),
            )
        except (TypeError, ValueError, PricerInputDefaultError) as error:
            raise PricerWebInputError(str(error)) from error

        target = target_capability(contract.product_id, target_id)
        if target is None:
            raise _fair_parameter_input_error("目标未登记", "unsupported")
        if target.support_status != "supported":
            raise _fair_parameter_input_error(
                target.unsupported_reason or "目标未获得公平参数支持",
                target.support_status,
            )
        method = config.model_method
        if method is None:
            method = "analytical" if "analytical" in target.allowed_methods else "monte_carlo"
        if method not in target.allowed_methods:
            raise _fair_parameter_input_error(
                f"目标不支持定价方法：{method}",
                "method_mismatch",
            )
        if len(target.controlled_term_keys) != 1:
            raise _fair_parameter_input_error(
                "当前公平参数服务只接受单一受控参数目标",
                "unsupported",
            )

        # The browser can only select an identifier.  The signed directory and
        # Core binding own all rules used below.
        try:
            capability_directory = private_capability_package()
            target_binding = bind_candidate_target_spec(
                capability_directory,
                contract.product_id,
                target_id,
            )
        except ContractResolutionError as error:
            raise PricerWebInputError(f"公平参数能力目录绑定失败：{error}") from error
        if (
            target_binding.target_spec_hash != target.target_spec_hash
            or target_binding.capability_hash != capability_directory.capability_hash
        ):
            raise PricerWebInputError("公平参数能力目录与Pricer目标规范不一致")

        # This is deliberately before every DataAssetRef read.  A historical
        # observed state can never be silently turned into a new-issuance run.
        state = {} if observed_state_payload is None else dict(observed_state_payload)
        first_observation = _first_contract_observation_date(contract)
        try:
            validate_initial_pricing_time_context(
                target.initial_pricing_time_rule or {},
                valuation_date=config.valuation_date,
                contract_start_date=contract.identity.get("contract_start_date"),
                lifecycle_status=state.get("lifecycle_status", "initial"),
                occurred_events=state.get("occurred_events", ()),
                realized_cashflows=state.get("realized_cashflows", ()),
                first_economic_observation_date=first_observation,
            )
        except (ContractResolutionError, TypeError, ValueError) as error:
            raise _fair_parameter_input_error(str(error), "initial_pricing_time_invalid") from error

        market_refs = protocol_input.market_data_refs
        if len(market_refs) != 1:
            raise PricerWebInputError("公平参数Pricer当前要求唯一受控market_data_ref")
        if not self._host_data_port:
            raise PricerWebInputError("公平参数Pricer必须由Host注入只读DataAssetRef端口")
        market_ref = market_refs[0]
        effective_tenant = tenant_id or market_ref.tenant_id
        if effective_tenant != market_ref.tenant_id:
            raise PricerWebInputError("DataAssetRef.tenant_id与Host tenant不一致")

        # Each asset is read exactly once.  The Core helper returns a sealed
        # calendar binding and exposes only its authenticated sessions, which
        # is enough for both Core recompilation and adapter validation.
        calendar_ref = protocol_input.trading_calendar_ref
        if contract.resolved_schedules and calendar_ref is None:
            raise PricerWebInputError("含观察日程的公平参数合同必须绑定交易日历")
        calendar_binding = None
        calendar_data: TradingCalendarData | None = None
        if calendar_ref is not None:
            if effective_tenant != calendar_ref.tenant_id:
                raise PricerWebInputError("trading_calendar_ref.tenant_id与Host tenant不一致")
            try:
                # Read the authenticated bytes once. Core still performs the
                # formal calendar verification, but receives an in-memory
                # view so the subsequent ordinary price() call can consume
                # the same frozen calendar without a second DataAsset read.
                calendar_payload = self.data_port.read_bytes(
                    calendar_ref,
                    tenant_id=calendar_ref.tenant_id,
                )
                if not isinstance(calendar_payload, bytes):
                    raise TypeError("DataPort必须返回交易日历原始字节")
                calendar_binding = verified_trading_calendar(
                    _data_ref_dict(calendar_ref),
                    _FrozenCalendarDataPort(calendar_ref, calendar_payload),
                )
                calendar_data = _calendar_data_from_verified_payload(
                    calendar_ref,
                    calendar_payload,
                    contract.underlyings,
                )
            except (ContractResolutionError, OSError, PermissionError, TypeError, ValueError) as error:
                raise PricerWebInputError(f"trading_calendar_ref无法由Core验证：{error}") from error

        historical, data_ref = self._load_data_asset(market_ref, contract.underlyings)
        if data_ref.tenant_id != effective_tenant:
            raise PricerWebInputError("market_data_ref.tenant_id与Host tenant不一致")
        try:
            validate_frozen_contract_reference(
                contract,
                historical.rows,
                valuation_date=str(config.valuation_date),
            )
            market_metadata = validate_market_data_asset(
                data_ref,
                historical,
                contract.underlyings,
            )
            calendar_snapshot = None
            if calendar_binding is not None:
                if (
                    contract.identity.get("calendar_id") != calendar_binding.calendar_id
                    or contract.identity.get("calendar_revision") != calendar_binding.calendar_revision
                ):
                    raise ValueError("冻结ResolvedContract与Host验证交易日历不一致")
                calendar_snapshot = {
                    "calendar_id": calendar_binding.calendar_id,
                    "calendar_revision": calendar_binding.calendar_revision,
                    "sessions": list(calendar_binding.sessions),
                    "verified_cn_sessions": True,
                    "source": "host-injected",
                }
            market_snapshot = market_snapshot_from_history(
                pd.DataFrame(historical.rows),
                contract.underlyings,
                valuation_date=str(config.valuation_date),
                hv_window=config.hv_window,
                risk_free_rate=config.risk_free_rate,
                dividend_yield=config.dividend_yield,
                trading_calendar=calendar_snapshot,
                hv_fields_by_asset=market_metadata["hv_fields_by_asset"],
            )
            if str(market_snapshot.get("valuation_date")) != str(config.valuation_date):
                raise ValueError("公平参数估值日必须由行情实际覆盖")
        except (TypeError, ValueError, PricerInputDefaultError) as error:
            raise _fair_parameter_input_error(str(error), "data") from error

        try:
            local_state = ObservedContractState.from_value(
                state or None,
                valuation_date=str(config.valuation_date),
            )
        except (ObservedStateError, TypeError, ValueError) as error:
            raise _fair_parameter_input_error(str(error), "initial_pricing_time_invalid") from error

        # Fill only missing numerical inputs from the one frozen snapshot.
        # Explicit caller values remain untouched.
        frozen_config = replace(
            config,
            model_method=method,
            spot=config.spot if config.spot is not None else market_snapshot["spot"],
            historical_volatility=(
                config.historical_volatility
                if config.historical_volatility is not None
                else market_snapshot["historical_volatility"]
            ),
        )
        if method == "monte_carlo" and frozen_config.path_count is None:
            raise _fair_parameter_input_error(
                "Monte Carlo公平参数求解必须显式提供path_count",
                "ineligible",
            )
        registry = load_registry()
        controlled_key = target_binding.controlled_term_keys[0]
        if controlled_key not in contract.terms:
            raise _fair_parameter_input_error("基础合同缺少目标参数", "ineligible")
        try:
            base_value = float(contract.terms[controlled_key])
        except (TypeError, ValueError) as error:
            raise _fair_parameter_input_error("基础目标参数必须为有限数", "ineligible") from error
        if not pd.notna(base_value) or not float("-inf") < base_value < float("inf"):
            raise _fair_parameter_input_error("基础目标参数必须为有限数", "ineligible")

        evaluations: dict[float, dict[str, Any]] = {}

        def emit_progress(payload: Mapping[str, Any]) -> None:
            if progress is None:
                return
            progress({"stage": "fair_parameter", **dict(payload)})

        def evaluate(candidate_value: float) -> ScalarValuation:
            candidate_value = float(candidate_value)
            if cancelled is not None and cancelled():
                raise FairParameterSolveError("cancelled", "用户取消公平参数反解")
            cached = evaluations.get(candidate_value)
            if cached is not None:
                return ScalarValuation(
                    value=float(cached["quote_value"]),
                    standard_error=cached.get("standard_error"),
                    private={"contract_fingerprint": cached["contract_fingerprint"]},
                )
            try:
                candidate_contract = recompile_candidate_contract(
                    contract,
                    {controlled_key: candidate_value},
                    registry=registry,
                    target_spec=target_binding,
                    calendar_binding=calendar_binding,
                )
                adapter = ProductPricingAdapter(
                    candidate_contract,
                    frozen_config,
                    market_snapshot,
                    local_state,
                )
                candidate_result = adapter.reprice()
                quote_value = _fair_quote_value(candidate_result, target.quote_value_basis)
                if quote_value is None:
                    raise ValueError(
                        f"候选定价结果缺少{target.quote_value_basis}"
                    )
                record = {
                    "parameter": candidate_value,
                    "quote_value": float(quote_value),
                    "standard_error": candidate_result.standard_error_percent,
                    "residual": float(quote_value) - float(target.quote_target_descriptor["target_value"]),
                    "contract_fingerprint": candidate_contract.contract_fingerprint,
                    "resolved_schedule_hash": semantic_hash(candidate_contract.resolved_schedules),
                    "implementation_id": candidate_result.implementation_id,
                }
                evaluations[candidate_value] = record
                return ScalarValuation(
                    value=record["quote_value"],
                    standard_error=record["standard_error"],
                    private={"contract_fingerprint": record["contract_fingerprint"]},
                )
            except (ContractResolutionError, ProductNotAvailable, TypeError, ValueError) as error:
                raise FairParameterSolveError(
                    "data",
                    f"公平参数候选{candidate_value:g}估值失败：{error}",
                    details={"parameter": candidate_value},
                ) from error

        solver = FairParameterSolver(
            target,
            evaluator=evaluate,
            base_value=base_value,
            base_contract=contract,
            registry=registry,
            method=method,
            cancelled=cancelled,
            progress=emit_progress,
        )
        fair_run_id = f"fair-{uuid4().hex[:12]}"
        try:
            solve_result = solver.solve()
        except FairParameterSolveError as error:
            raise _fair_parameter_solve_error(error) from error
        if not solve_result.converged or solve_result.solution is None:
            raise _fair_parameter_solve_error(
                FairParameterSolveError("non_converged", "公平参数求解未收敛")
            )
        final_evaluation = evaluations.get(float(solve_result.solution))
        if final_evaluation is None:
            try:
                evaluate(float(solve_result.solution))
            except FairParameterSolveError as error:
                raise _fair_parameter_solve_error(error) from error
            final_evaluation = evaluations[float(solve_result.solution)]

        try:
            evaluated_contract = recompile_candidate_contract(
                contract,
                {controlled_key: float(solve_result.solution)},
                registry=registry,
                target_spec=target_binding,
                calendar_binding=calendar_binding,
            )
        except ContractResolutionError as error:
            raise _fair_parameter_solve_error(
                FairParameterSolveError("data", f"最终公平参数合同重编译失败：{error}")
            ) from error
        if evaluated_contract.contract_fingerprint != final_evaluation["contract_fingerprint"]:
            raise PricerWebInputError("公平参数最终合同与候选估值不一致")

        # The solve callback intentionally prices only the scalar quote
        # function. Revalue the final Core-recompiled contract once through
        # the existing ordinary price() entry with the same frozen inputs so
        # the public result has the exact ordinary PricingResult projection,
        # including precision, Greeks and risk output.
        try:
            final_pricing_result = price(PricingInput(
                contract=evaluated_contract,
                pricing_config=frozen_config,
                historical_data=historical,
                market_data_refs=(data_ref,),
                trading_calendar_data=calendar_data,
                trading_calendar_ref=calendar_ref,
                observed_contract_state=local_state,
            ))
        except (ContractResolutionError, ProductNotAvailable, TypeError, ValueError) as error:
            raise _fair_parameter_solve_error(
                FairParameterSolveError("data", f"最终公平参数合同估值失败：{error}")
            ) from error
        final_valuation = redact_public_money_compatibility(
            final_pricing_result.to_public_percent_dict()
        )
        final_valuation["price_convention"] = _quote_price_convention(evaluated_contract)
        if any(
            field in final_valuation
            for field in ("reference_quote_fact", "reporter_quote_fact")
        ):
            raise PricerWebInputError("公平参数final_valuation不得包含普通报价事实")

        method_label = "Analytical" if method == "analytical" else "Monte Carlo"
        public_solution = _fair_public_parameter_value(
            target,
            float(solve_result.solution),
            contract,
        )
        public_base_value = _fair_public_parameter_value(target, base_value, contract)
        final_parameter_snapshot = {
            "schema": "optionhelper.fair-parameter-snapshot",
            "target_id": target.target_id,
            "controlled_term_keys": list(target_binding.controlled_term_keys),
            "internal_parameter_values": {
                controlled_key: float(solve_result.solution),
            },
            "public_parameter_values": {
                controlled_key: public_solution,
            },
            "base_parameter": {
                "internal": base_value,
                "public": public_base_value,
            },
            "base_contract_fingerprint": contract.contract_fingerprint,
            "evaluated_contract_fingerprint": evaluated_contract.contract_fingerprint,
            "target_spec_hash": target_binding.target_spec_hash,
        }
        final_parameter_snapshot_hash = semantic_hash(final_parameter_snapshot)
        final_valuation_hash = semantic_hash(final_valuation)
        run_identity = {
            "fair_run_id": fair_run_id,
            "base_contract_fingerprint": contract.contract_fingerprint,
            "target_spec_hash": target_binding.target_spec_hash,
            "capability_directory_hash": capability_directory.capability_hash,
            "model_id": "pricer-product-adapter",
            "model_version": "1",
        }
        if method == "analytical":
            solution_uncertainty = deterministic_uncertainty(
                solve_result,
                target,
                bound_evidence=None,
                current_identity=run_identity,
                fair_run_id=fair_run_id,
                base_contract_fingerprint=contract.contract_fingerprint,
                target_spec_hash=target_binding.target_spec_hash,
                model_id="pricer-product-adapter",
                model_version="1",
                capability_directory_hash=capability_directory.capability_hash,
            )
        else:
            solution_uncertainty = monte_carlo_uncertainty(
                solve_result,
                (),
                target,
                path_count=int(frozen_config.path_count),
                target_spec_hash=target_binding.target_spec_hash,
                current_identity=run_identity,
                fair_run_id=fair_run_id,
                base_contract_fingerprint=contract.contract_fingerprint,
                model_id="pricer-product-adapter",
                model_version="1",
                capability_directory_hash=capability_directory.capability_hash,
            )
        # The uncertainty helpers deliberately return only the numerical
        # precision contract. Keep the current run identity beside it so a
        # future trusted evidence issuer cannot be substituted from another
        # fair run or contract.
        solution_uncertainty = {
            **solution_uncertainty,
            "identity": dict(run_identity),
            "evaluated_contract_fingerprint": evaluated_contract.contract_fingerprint,
        }
        fair_result = {
            "schema": "optionhelper.pricer-fair-parameter-result",
            "result_kind": "fair_parameter_solution",
            "status": "solved",
            "product_id": contract.product_id,
            "target_id": target.target_id,
            "method": method_label,
            "quote_basis": target.quote_basis,
            "quote_value_basis": target.quote_value_basis,
            "valuation_perspective": target.valuation_perspective,
            "cashflow_sign_convention": target.cashflow_sign_convention,
            "value_basis": target.quote_value_basis,
            "target_value": float(target.quote_target_descriptor["target_value"]),
            "solution": public_solution,
            "base_parameter": public_base_value,
            "path_count": frozen_config.path_count if method == "monte_carlo" else None,
            "residual": solve_result.residual,
            "iterations": solve_result.iterations,
            "quote_eligible": False,
            "precision_status": "research_only",
            "solution_uncertainty": solution_uncertainty,
            "final_parameter_snapshot_hash": final_parameter_snapshot_hash,
            "final_valuation": final_valuation,
            "final_valuation_hash": final_valuation_hash,
            "evaluated_contract_fingerprint": evaluated_contract.contract_fingerprint,
            "formal_quote_status": "research_only",
            "formal_quote_reason": "当前没有可信的产品级参数误差门槛",
            "identity": {
                **run_identity,
                "evaluated_contract_fingerprint": evaluated_contract.contract_fingerprint,
            },
        }
        output = {
            "ok": True,
            "module": "pricer",
            "status": "succeeded",
            "task_id": str(host_context.task_id),
            "run_id": fair_run_id,
            "analysis_case_id": str(host_context.analysis_case_id),
            "tenant_id": effective_tenant,
            "created_by": data_ref.created_by,
            "access_scope": list(data_ref.access_scope),
            "candidate_id": str(host_context.candidate_id),
            "catalog_version": str(host_context.catalog_version),
            "execution_fingerprint": semantic_hash({
                "contract": contract.to_protocol_dict(),
                "pricing_config": frozen_config.to_dict(),
                "market_data_refs": [_data_ref_dict(data_ref)],
                "trading_calendar_ref": None if calendar_ref is None else _data_ref_dict(calendar_ref),
                "observed_contract_state": state,
                "pricing_objective": objective.to_protocol_dict() if objective is not None else None,
            }),
            "contract_fingerprint": contract.contract_fingerprint,
            "resolved_contract": redact_public_money_compatibility(contract.to_protocol_dict()),
            "data_refs": [
                _data_ref_dict(data_ref),
                *([] if calendar_ref is None else [_data_ref_dict(calendar_ref)]),
            ],
            "input_snapshot": redact_public_money_compatibility({
                "contract": contract.to_protocol_dict(),
                "pricing_config": frozen_config.to_dict(),
                "market_data_refs": [_data_ref_dict(data_ref)],
                "trading_calendar_ref": None if calendar_ref is None else _data_ref_dict(calendar_ref),
                "observed_contract_state": state,
                "pricing_objective": objective.to_protocol_dict() if objective is not None else None,
            }),
            "limitations": [
                "公平参数求解结果仅限研究，不具备正式报价资格",
                "参数误差门槛尚未完成产品级认证",
            ],
            "market_snapshot": redact_public_money_compatibility(market_snapshot),
            "pricing": fair_result,
            "fair_parameter": fair_result,
        }
        output = redact_public_money_compatibility(project_public_percent(output))
        private_audit = {
            "schema": "optionhelper.private-fair-parameter-audit",
            "fair_run_id": fair_run_id,
            "base_contract_fingerprint": contract.contract_fingerprint,
            "evaluated_contract_fingerprint": evaluated_contract.contract_fingerprint,
        "target_spec_hash": target_binding.target_spec_hash,
        "capability_directory_hash": capability_directory.capability_hash,
        "model_id": "pricer-product-adapter",
        "model_version": "1",
        "method": method,
        "final_parameter_snapshot_hash": final_parameter_snapshot_hash,
        "final_valuation_hash": final_valuation_hash,
        "solver": solve_result.to_private_dict(),
        "evaluations": list(evaluations.values()),
        }
        files = _fair_module_run_files(
            output,
            controlled_contract=contract.to_controlled_snapshot(),
            evaluated_contract=evaluated_contract.to_controlled_snapshot(),
            final_parameter_snapshot=final_parameter_snapshot,
            private_audit=private_audit,
        )
        require_host_bound_run_contract(output, host_context)
        try:
            reference = self.result_store.commit_module_run(
                module="pricer",
                tenant_id=effective_tenant,
                task_id=str(host_context.task_id),
                run_id=fair_run_id,
                files=files,
            )
            stored_run = self.result_store.read_module_run_bundle(
                reference,
                tenant_id=effective_tenant,
            )
            stored_fair = _json_object_bytes(
                stored_run["artifacts/fair_parameter_result.json"],
                "公平参数结果",
            )
            stored_evaluated = _json_object_bytes(
                stored_run["private/evaluated_resolved_contract.json"],
                "公平参数候选合同",
            )
            stored_snapshot = _json_object_bytes(
                stored_run["private/final_parameter_snapshot.json"],
                "公平参数最终参数快照",
            )
            _verify_fair_bundle_identity(
                stored_fair,
                stored_evaluated,
                stored_snapshot,
                fair_result,
                evaluated_contract.contract_fingerprint,
                final_parameter_snapshot,
            )
        except (OSError, TypeError, ValueError, ContractResolutionError) as error:
            raise PricerWebInputError(f"公平参数ModuleRun提交验真失败：{error}") from error
        output.update({
            "module_run_ref": asdict(reference),
            "artifact_manifest": _json_object_bytes(
                stored_run["artifacts/artifact_manifest.json"],
                "ModuleRun产物清单",
            ),
            "commit_marker": _json_object_bytes(
                stored_run["commit_marker.json"],
                "ModuleRun提交标记",
            ),
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
        calendar = _calendar_data_from_verified_payload(ref, payload, underlyings)
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
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[..., Any] | None = None,
) -> Mapping[str, Any]:
    """正式Agent Tool只接受共享PricingInput；页面协议不在此兼容。"""
    if isinstance(request, Mapping):
        values = dict(request)
        action = str(values.pop("action", "run")).strip().lower()
        if action == "catalog":
            return PricerRuntime().catalog()
        if action == "eligibility":
            # This branch intentionally forwards no DataAssetRef, config or
            # observed state.  Eligibility is a read-only contract/target/
            # method check and must not touch the supplied DataStore.
            return PricerRuntime(data_port=data_store, result_store=result_store).eligibility(
                values,
                host_context=host_context,
            )
        if action != "run":
            return {
                "ok": False,
                "module": "pricer",
                "status": "unsupported",
                "message": f"Pricer不支持action={action}；仅支持catalog、run。",
            }
        request = values
    if isinstance(request, ProtocolPricingInput):
        return PricerRuntime(data_port=data_store, result_store=result_store).run_formal(
            request,
            host_context=host_context,
            tenant_id=tenant_id,
            cancelled=cancelled,
            progress=progress,
        )
    if not isinstance(request, Mapping) or not {"contract", "pricing_config", "market_data_refs"}.issubset(request):
        raise PricerWebInputError("正式Pricer Tool只接受PricingInput={contract,pricing_config,market_data_refs,trading_calendar_ref?}")
    return PricerRuntime(data_port=data_store, result_store=result_store).run_formal(
        request,
        host_context=host_context,
        tenant_id=tenant_id,
        cancelled=cancelled,
        progress=progress,
    )


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
        "observed_contract_state", "pricing_objective",
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
    objective_value = values.get("pricing_objective")
    if objective_value is None:
        objective = None
    else:
        try:
            objective = PricingObjective.from_value(objective_value)
        except (TypeError, ValueError) as error:
            raise PricerWebInputError(f"PricingInput.pricing_objective无效：{error}") from error
    return ProtocolPricingInput(
        contract=contract,
        pricing_config=dict(config),
        market_data_refs=tuple(_data_asset_ref(value) for value in refs),
        trading_calendar_ref=(
            None if values.get("trading_calendar_ref") is None
            else _data_asset_ref(values["trading_calendar_ref"])
        ),
        observed_contract_state=observed_state,
        pricing_objective=objective,
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


def _require_host_eligibility_contract(contract: ResolvedContract, context: ModuleHostContext) -> None:
    """Check the Host contract binding without requiring a runnable ModuleRun."""
    if context.module != "pricer" or not {
        "module.catalog", "module.run", "conversation.tool.run",
    }.intersection(context.request_policy):
        raise PricerWebInputError("ModuleHostContext未授权Pricer eligibility")
    if context.contract_fingerprint != contract.contract_fingerprint:
        raise PricerWebInputError("ModuleHostContext.contract_fingerprint与ResolvedContract不一致")
    if context.contract_ref is None or context.contract_ref.schema_id != RESOLVED_CONTRACT_SCHEMA_ID:
        raise PricerWebInputError("eligibility缺少Core验证的ResolvedContract引用")
    if context.contract_ref.content_hash != contract.contract_fingerprint:
        raise PricerWebInputError("eligibility.contract_ref与ResolvedContract不一致")


def _resolved_contract_value(value: Any) -> ResolvedContract:
    if isinstance(value, ResolvedContract):
        return value
    if not isinstance(value, Mapping):
        raise PricerWebInputError("eligibility.contract必须为完整ResolvedContract")
    try:
        return ResolvedContract.from_controlled_snapshot(value)
    except (TypeError, ValueError, ContractResolutionError) as error:
        raise PricerWebInputError(f"eligibility.contract无效：{error}") from error


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


class _FrozenCalendarDataPort:
    """Expose one already-read calendar payload to Core without another read."""

    def __init__(self, reference: DataAssetRef, payload: bytes) -> None:
        self._reference = reference
        self._payload = payload

    def read_bytes(self, reference: DataAssetRef, *, tenant_id: str) -> bytes:
        if reference != self._reference or tenant_id != self._reference.tenant_id:
            raise PermissionError("冻结交易日历端口不接受其他资产")
        return self._payload


def _calendar_data_from_verified_payload(
    reference: DataAssetRef,
    payload: bytes,
    underlyings: tuple[str, ...],
) -> TradingCalendarData:
    """Parse the exact calendar bytes already authenticated by Core."""
    try:
        document = json.loads(payload)
        if not isinstance(document, Mapping):
            raise TypeError("calendar document must be an object")
        calendar = TradingCalendarData(
            source_ref=reference.storage_ref,
            asset_ids=tuple(str(item) for item in document["asset_ids"]),
            sessions=tuple(str(item) for item in document["sessions"]),
            sessions_by_exchange={
                str(key): tuple(str(item) for item in values)
                for key, values in dict(document["sessions_by_exchange"]).items()
            },
            asset_exchange={str(key): str(item) for key, item in dict(document["asset_exchange"]).items()},
            requested_start_date=str(document["requested_start_date"]),
            requested_end_date=str(document["requested_end_date"]),
            content_hash=reference.content_hash,
            schema_id=str(document.get("schema_id", "")),
            storage_mode="host-injected",
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise PricerWebInputError(f"trading-calendar内容无效：{error}") from error
    if set(calendar.asset_ids) != set(underlyings):
        raise PricerWebInputError("交易日历未逐一覆盖ResolvedContract标的")
    return calendar


def _fair_parameter_input_error(message: str, code: str) -> PricerWebInputError:
    error = PricerWebInputError(message)
    error.code = f"fair_parameter_{code}"
    error.stage = "fair_parameter"
    return error


def _fair_parameter_solve_error(error: FairParameterSolveError) -> PricerWebInputError:
    """Map numerical failures to the module's stable controlled error shape."""
    mapped = PricerWebInputError(f"公平参数求解失败[{error.code}]：{error}")
    mapped.code = f"fair_parameter_{error.code}"
    mapped.stage = "fair_parameter"
    if error.code == "cancelled":
        mapped.status = "cancelled"
    elif error.code in {"unsupported", "solve_semantics_blocked", "ineligible", "method_mismatch"}:
        mapped.status = "unsupported"
    else:
        mapped.status = "failed"
    return mapped


def _first_contract_observation_date(contract: ResolvedContract) -> str | None:
    dates: list[str] = []
    schedules = contract.resolved_schedules
    if isinstance(schedules, Mapping):
        for schedule in schedules.values():
            if not isinstance(schedule, Mapping):
                continue
            values = schedule.get("dates", ())
            if isinstance(values, (list, tuple)):
                dates.extend(str(value) for value in values)
    if not dates:
        return None
    try:
        return min(date.fromisoformat(value) for value in dates).isoformat()
    except ValueError:
        # The ResolvedContract constructor normally prevents this.  Returning
        # the first raw value keeps the Core rule as the final validator rather
        # than silently correcting a malformed contract.
        return dates[0]


def _fair_quote_value(pricing_result: Any, quote_value_basis: str) -> float | None:
    if quote_value_basis == "pv_percent":
        value = getattr(pricing_result, "pv_percent", None)
    elif quote_value_basis == "variance_percent":
        value = getattr(pricing_result, "variance_percent", None)
        if value is None:
            value = pricing_result.to_public_percent_dict().get("variance_percent")
    else:
        value = None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value == value and abs(value) != float("inf") else None


def _fair_public_parameter_value(
    target: Any,
    value: float,
    contract: ResolvedContract,
) -> float:
    internal_basis = getattr(target, "internal_value_basis", {})
    catalog_unit = internal_basis.get("catalog_unit") if isinstance(internal_basis, Mapping) else None
    reference_price = None
    if catalog_unit == "price":
        references = contract.identity.get("reference_prices")
        if isinstance(references, Mapping) and contract.underlyings:
            reference_price = references.get(contract.underlyings[0])
    try:
        return float(encode_public_value(
            value,
            catalog_unit=str(catalog_unit),
            reference_price_basis=reference_price,
        ))
    except (TypeError, ValueError) as error:
        raise PricerWebInputError(f"公平参数公开参数编码失败：{error}") from error


def _fair_module_run_files(
    output: Mapping[str, Any],
    *,
    controlled_contract: Mapping[str, Any],
    evaluated_contract: Mapping[str, Any],
    final_parameter_snapshot: Mapping[str, Any],
    private_audit: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the fair-result Store envelope with private evaluation evidence."""
    output = redact_public_money_compatibility(output)
    fair_result = output.get("fair_parameter")
    if not isinstance(fair_result, Mapping):
        raise PricerWebInputError("公平参数结果缺少公开fair_parameter对象")
    manifest = {
        "module": "pricer",
        "status": output["status"],
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
        "result": "result.json",
        "artifacts": [
            {
                "name": "fair_parameter_result.json",
                "media_type": "application/json",
                "schema_id": "optionhelper.pricer-fair-parameter-result",
            },
        ],
        "artifact_manifest": "artifacts/artifact_manifest.json",
        "contract_fingerprint": output["contract_fingerprint"],
        "error": None,
        "metadata": {
            "created_at": _now(),
            "quote_eligible": False,
            "precision_status": "research_only",
            "delivery": "fair_parameter",
        },
    }
    return {
        "manifest.json": manifest,
        "input_snapshot.json": output["input_snapshot"],
        "resolved_contract.json": dict(controlled_contract),
        "data_refs.json": {"data_refs": output["data_refs"]},
        "limitations.json": {"limitations": output["limitations"]},
        "artifacts/fair_parameter_result.json": dict(fair_result),
        "private/evaluated_resolved_contract.json": dict(evaluated_contract),
        "private/final_parameter_snapshot.json": dict(final_parameter_snapshot),
        "private/audit_fair_parameter.json": dict(private_audit),
        "result.json": dict(output),
    }


def _verify_fair_bundle_identity(
    stored_fair: Mapping[str, Any],
    stored_evaluated: Mapping[str, Any],
    stored_snapshot: Mapping[str, Any],
    expected_fair: Mapping[str, Any],
    evaluated_contract_fingerprint: str,
    expected_snapshot: Mapping[str, Any],
) -> None:
    stored_identity = stored_fair.get("identity")
    expected_identity = expected_fair.get("identity")
    if not isinstance(stored_identity, Mapping) or not isinstance(expected_identity, Mapping):
        raise ValueError("公平参数结果缺少身份绑定")
    for key in (
        "fair_run_id",
        "base_contract_fingerprint",
        "evaluated_contract_fingerprint",
        "target_spec_hash",
        "capability_directory_hash",
        "model_id",
        "model_version",
    ):
        if stored_identity.get(key) != expected_identity.get(key):
            raise ValueError(f"公平参数结果身份字段{key}在Store提交后发生变化")
    if stored_identity.get("evaluated_contract_fingerprint") != evaluated_contract_fingerprint:
        raise ValueError("公平参数结果与私有候选合同指纹不一致")
    if stored_evaluated.get("contract_fingerprint") != evaluated_contract_fingerprint:
        raise ValueError("私有候选合同缺少匹配的contract_fingerprint")
    for field in (
        "evaluated_contract_fingerprint",
        "final_parameter_snapshot_hash",
        "final_valuation_hash",
    ):
        if not isinstance(stored_fair.get(field), str) or not stored_fair[field].strip():
            raise ValueError(f"公平参数结果缺少{field}")
    if not isinstance(stored_snapshot, Mapping):
        raise ValueError("公平参数私有快照必须为对象")
    if semantic_hash(stored_snapshot) != stored_fair["final_parameter_snapshot_hash"]:
        raise ValueError("公平参数final_parameter_snapshot_hash校验失败")
    final_valuation = stored_fair.get("final_valuation")
    if not isinstance(final_valuation, Mapping):
        raise ValueError("公平参数结果缺少final_valuation")
    if semantic_hash(final_valuation) != stored_fair["final_valuation_hash"]:
        raise ValueError("公平参数final_valuation_hash校验失败")
    if stored_fair["evaluated_contract_fingerprint"] != evaluated_contract_fingerprint:
        raise ValueError("公平参数evaluated_contract_fingerprint校验失败")
    if dict(stored_snapshot) != dict(expected_snapshot):
        raise ValueError("公平参数最终参数快照在Store提交后发生变化")
    if stored_snapshot.get("evaluated_contract_fingerprint") != evaluated_contract_fingerprint:
        raise ValueError("公平参数最终参数快照与候选合同指纹不一致")
    if stored_fair.get("final_valuation") != expected_fair.get("final_valuation"):
        raise ValueError("公平参数最终估值在Store提交后发生变化")
    if stored_fair.get("quote_eligible") is not False:
        raise ValueError("公平参数结果不得具备正式报价资格")
    if stored_fair.get("precision_status") != "research_only":
        raise ValueError("公平参数结果必须标记research_only")


def _module_run_files(
    output: Mapping[str, Any],
    *,
    controlled_contract: Mapping[str, Any] | None = None,
    private_pricing_audit: Mapping[str, Any] | None = None,
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
    if private_pricing_audit is not None:
        files["private/audit_pricing_result.json"] = dict(private_pricing_audit)
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


def _json_object_bytes(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label}必须为有效JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须为JSON对象")
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
