"""登记产品的受控历史回放矩阵。

此文件只把已有的单产品正式回放结果整理为审计矩阵，不重算路径、现金流或指标。
"""

from __future__ import annotations

import csv
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from runtime.contracts.contract_api import load_registry, resolve_contract
from runtime.protocol.models import DataAssetRef, ModuleRunRef
from runtime.protocol.module_host import ModuleHostContext

from .historical_data import HistoricalData
from .impl.config import BacktestConfig
from .impl.engine import backtest
from .models import BacktestInput


MATRIX_COLUMNS = (
    "product_id", "product_name", "rule_revision", "strike_terms", "data_asset_ref", "entry_rule", "complete_tenor",
    "smoke", "formal_evidence", "metric_profile", "specialized_metrics", "metric_coverage",
    "sample_count", "skipped_count", "skipped_entries", "skipped_reason_counts", "observed_events",
    "observed_path_cases", "ledger", "branch_coverage", "contract_settlement_return",
    "positive_return_rate",
)

# OptionReg的执行价TermCatalog键。障碍、缓冲、初始价虽也以price表示，均不属于执行价。
_STRIKE_TERM_KEYS = frozenset({"K", "K1", "K2", "K3", "K4", "Kp", "Kc", "Kd", "Ku"})


def replay_catalog_matrix(historical_data: HistoricalData, config: BacktestConfig) -> list[dict[str, Any]]:
    """逐个回放OptionReg登记结构，分开记录烟测、分支覆盖与正式Run证据。"""

    if not isinstance(historical_data, HistoricalData):
        raise TypeError("historical_data必须为HistoricalData")
    if not isinstance(config, BacktestConfig):
        raise TypeError("config必须为BacktestConfig")

    registry = load_registry()
    rows: list[dict[str, Any]] = []
    for product_id, product in registry["products"].items():
        product_name = str(product["identity"]["name_zh"])
        underlyings = _underlyings_for_product(product)
        base = _base_row(
            product_id,
            product_name,
            int(product["identity"]["rule_revision"]),
            historical_data,
            config,
        )
        try:
            contract = resolve_contract(
                product_id,
                identity={
                    "underlyings": underlyings,
                    "reference_prices": {underlying: 100.0 for underlying in underlyings},
                    "calendar_id": historical_data.calendar_id,
                    "calendar_revision": historical_data.calendar_revision,
                },
                registry=registry,
                trading_dates=historical_data.trading_sessions,
            )
            payload = backtest(BacktestInput(contract, config, historical_data)).to_dict()
        except Exception as error:  # 产品级阻断必须留在矩阵，不能因为单项失败丢行。
            smoke: dict[str, Any] = {"status": "blocked", "reason": f"{type(error).__name__}:{error}"}
            if hasattr(error, "code"):
                smoke["error_code"] = str(error.code)
            if hasattr(error, "details"):
                smoke["details"] = dict(error.details)
            rows.append({
                **base,
                "smoke": smoke,
            })
            continue
        rows.append(_completed_row(base, payload, contract))
    return rows


def formal_catalog_evidence_matrix(
    config: BacktestConfig,
    *,
    single_asset_ref: DataAssetRef,
    multi_asset_ref: DataAssetRef,
    data_store: Any,
    result_store: Any,
    host_context_factory: Callable[[Any], ModuleHostContext],
    tenant_id: str,
) -> list[dict[str, Any]]:
    """逐产品执行正式Host、DataStore与ResultStore回环并返回证据矩阵。"""

    from .service import BacktesterRuntime

    if not isinstance(config, BacktestConfig):
        raise TypeError("config必须为BacktestConfig")
    registry = load_registry()
    runtime = BacktesterRuntime(data_store=data_store, result_store=result_store, tenant_id=tenant_id)
    rows: list[dict[str, Any]] = []
    for product_id, product in registry["products"].items():
        data_ref = multi_asset_ref if "S0Vec" in product["terms"] else single_asset_ref
        underlyings = list(data_ref.asset_ids)
        try:
            contract = resolve_contract(
                product_id,
                identity={
                    "underlyings": underlyings,
                    "calendar_id": str(data_ref.coverage["calendar_id"]),
                    "calendar_revision": str(data_ref.coverage["calendar_revision"]),
                },
                registry=registry,
                trading_dates=tuple(data_ref.coverage["sessions"]),
            )
            host_context = host_context_factory(contract)
            output = runtime.run_formal({
                "contract": contract.to_protocol_dict(),
                "backtest_config": config.to_dict(),
                "historical_data": data_ref,
            }, host_context=host_context)
            rows.append(_formal_evidence_row(
                product_id=product_id,
                product_name=str(product["identity"]["name_zh"]),
                contract=contract,
                data_ref=data_ref,
                output=output,
                host_context=host_context,
                result_store=result_store,
                tenant_id=tenant_id,
            ))
        except Exception as error:
            rows.append({
                "product_id": product_id,
                "product_name": str(product["identity"]["name_zh"]),
                "rule_revision": int(product["identity"]["rule_revision"]),
                "formal_evidence_status": "blocked",
                "run_status": "not_started",
                "error": {"type": type(error).__name__, "message": str(error)},
                "data_asset_id": data_ref.data_asset_id,
                "data_content_hash": data_ref.content_hash,
                "host_context_bound": False,
                "data_store_verified": False,
                "module_run_ref": None,
                "module_run_resolved": False,
                "commit_marker_verified": False,
                "public_artifacts": [],
                "sample_count": 0,
                "branch_coverage_status": "not_executed",
            })
    return rows


def _formal_evidence_row(
    *,
    product_id: str,
    product_name: str,
    contract: Any,
    data_ref: DataAssetRef,
    output: Mapping[str, Any],
    host_context: ModuleHostContext,
    result_store: Any,
    tenant_id: str,
) -> dict[str, Any]:
    reference = ModuleRunRef(**dict(output["module_run_ref"]))
    run_directory = result_store.resolve_module_run(reference, tenant_id=tenant_id)
    stored_manifest = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))
    stored_input = json.loads((run_directory / "input_snapshot.json").read_text(encoding="utf-8"))
    marker = json.loads((run_directory / "commit_marker.json").read_text(encoding="utf-8"))
    run_status = str(output["status"])
    backtest_payload = output.get("backtest") if isinstance(output.get("backtest"), Mapping) else {}
    public_artifacts = list(stored_manifest.get("artifacts", []))
    host_bound = all((
        output.get("task_id") == host_context.task_id,
        output.get("analysis_case_id") == host_context.analysis_case_id,
        output.get("candidate_id") == host_context.candidate_id,
        output.get("product_id") == contract.product_id == host_context.product_id,
        output.get("rule_revision") == contract.identity["rule_revision"] == host_context.rule_revision,
    ))
    output_refs = output.get("data_refs")
    output_ref = output_refs[0] if isinstance(output_refs, list) and len(output_refs) == 1 else {}
    input_ref = stored_input["historical_data"]
    data_verified = all(
        isinstance(reference, Mapping)
        and reference.get("data_asset_id") == data_ref.data_asset_id
        and reference.get("content_hash") == data_ref.content_hash
        for reference in (input_ref, output_ref)
    )
    committed = marker.get("committed") is True
    sample_count = int(backtest_payload.get("sample_count", 0))
    branch_coverage_status = dict(backtest_payload.get("branch_coverage", {})).get("status", "not_available")
    evidence_verified = all((
        host_bound,
        data_verified,
        committed,
        run_status == "succeeded",
        sample_count >= 1,
        branch_coverage_status in {"complete", "partial"},
    ))
    return {
        "product_id": product_id,
        "product_name": product_name,
        "rule_revision": int(contract.identity["rule_revision"]),
        "formal_evidence_status": "verified" if evidence_verified else "invalid",
        "run_status": run_status,
        "error": output.get("error"),
        "data_asset_id": data_ref.data_asset_id,
        "data_content_hash": data_ref.content_hash,
        "host_context_bound": host_bound,
        "data_store_verified": data_verified,
        "module_run_ref": dict(output["module_run_ref"]),
        "module_run_resolved": True,
        "commit_marker_verified": committed,
        "public_artifacts": public_artifacts,
        "sample_count": sample_count,
        "branch_coverage_status": branch_coverage_status,
    }


def write_catalog_replay_matrix(rows: Sequence[Mapping[str, Any]], directory: Path) -> tuple[Path, Path]:
    """从同一内存行集写出JSON和CSV，不写入仓库默认result目录。"""

    normalized = [_normalize_row(row) for row in rows]
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "backtester_catalog_replay_matrix.json"
    csv_path = target / "backtester_catalog_replay_matrix.csv"
    json_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=MATRIX_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for row in normalized:
            writer.writerow({
                column: _csv_value(row[column])
                for column in MATRIX_COLUMNS
            })
    return json_path, csv_path


def _base_row(
    product_id: str,
    product_name: str,
    rule_revision: int,
    historical_data: HistoricalData,
    config: BacktestConfig,
) -> dict[str, Any]:
    return {
        "product_id": product_id,
        "product_name": product_name,
        "rule_revision": rule_revision,
        "strike_terms": {},
        "data_asset_ref": deepcopy(historical_data.data_asset_ref),
        "entry_rule": config.entry_rule,
        "complete_tenor": config.complete_tenor,
        "smoke": {"status": "blocked", "reason": "not_run"},
        "formal_evidence": _formal_evidence(historical_data),
        "metric_profile": None,
        "specialized_metrics": None,
        "metric_coverage": {"status": "blocked", "gaps": ["product_replay_not_completed"]},
        "sample_count": 0,
        "skipped_count": 0,
        "skipped_entries": [],
        "skipped_reason_counts": {},
        "observed_events": {},
        "observed_path_cases": [],
        "ledger": [],
        "branch_coverage": {"status": "blocked"},
        "contract_settlement_return": {
            "basis": "declared_contract_cashflows_over_contract_scale",
            "display_unit": "percentage",
            "value_encoding": "decimal_ratio",
            "external_costs_modelled": False,
            "total": None,
            "average": None,
        },
        "positive_return_rate": {
            "numerator": "positive_return_count",
            "numerator_count": 0,
            "denominator": "valid_return_sample_count",
            "denominator_count": 0,
            "rate": None,
        },
    }


def _completed_row(
    base: Mapping[str, Any],
    payload: Mapping[str, Any],
    contract: Any,
) -> dict[str, Any]:
    common = dict(payload["common_metrics"])
    ledger = list(payload["trade_ledger"])
    coverage = dict(payload["branch_coverage"])
    skipped = list(payload["skipped_entries"])
    specialized = deepcopy(payload["specialized_metrics"])
    return {
        **base,
        "smoke": {
            "status": "passed",
            "reason": None,
            "sample_status": "partial" if coverage["status"] != "complete" or skipped else "complete",
        },
        "metric_profile": deepcopy(payload["metric_profile"]),
        "specialized_metrics": specialized,
        "metric_coverage": deepcopy(specialized["metric_coverage"]),
        "strike_terms": _strike_terms(contract),
        "data_asset_ref": deepcopy(payload["data_asset_ref"]),
        "sample_count": int(payload["sample_count"]),
        "skipped_count": int(payload["skipped_count"]),
        "skipped_entries": skipped,
        "skipped_reason_counts": dict(sorted(Counter(item["reason"] for item in skipped).items())),
        "observed_events": deepcopy(payload["event_summary"]),
        "observed_path_cases": deepcopy(coverage["observed_pairs"]),
        "ledger": ledger,
        "branch_coverage": coverage,
        "contract_settlement_return": {
            "basis": payload["economic_convention"]["basis"],
            "display_unit": payload["economic_convention"]["display_unit"],
            "value_encoding": payload["economic_convention"]["value_encoding"],
            "external_costs_modelled": payload["economic_convention"]["external_costs_modelled"],
            "total": float(sum(float(trade["contract_settlement_return"]) for trade in ledger)),
            "average": common["average_contract_settlement_return"],
        },
        "positive_return_rate": {
            "numerator": "positive_return_count",
            "numerator_count": common["positive_return_count"],
            "denominator": "valid_return_sample_count",
            "denominator_count": common["valid_return_sample_count"],
            "rate": common["positive_return_rate"],
        },
    }


def _formal_evidence(historical_data: HistoricalData) -> dict[str, Any]:
    """目录矩阵仅做开发烟测，正式资产、Host和Store证据必须单独取得。"""
    data_ref = historical_data.data_asset_ref
    if str(data_ref.get("storage_ref")) == "in_memory":
        reason = "in_memory_historical_data_is_development_smoke_only"
    elif not historical_data.calendar_source_declared:
        reason = "data_asset_calendar_not_source_declared"
    else:
        reason = "formal_host_datastore_run_not_requested"
    return {
        "status": "not_executed",
        "reason": reason,
        "host_context_bound": False,
        "data_store_verified": False,
        "result_store_committed": False,
        "module_run_ref": None,
    }


def _underlyings_for_product(product: Mapping[str, Any]) -> list[str]:
    return ["000905.SH", "000300.SH"] if "S0Vec" in product["terms"] else ["000905.SH"]


def _strike_terms(contract: Any) -> dict[str, float]:
    """只记录OptionReg明确登记的执行价，不把障碍或缓冲价格混入。"""
    return {
        key: float(value)
        for key, value in contract.terms.items()
        if key in _STRIKE_TERM_KEYS
    }


def _normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(row) - set(MATRIX_COLUMNS)
    missing = set(MATRIX_COLUMNS) - set(row)
    if unknown or missing:
        raise ValueError(f"matrix row字段不一致：missing={sorted(missing)} unknown={sorted(unknown)}")
    return {column: deepcopy(row[column]) for column in MATRIX_COLUMNS}


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return value


__all__ = (
    "MATRIX_COLUMNS",
    "formal_catalog_evidence_matrix",
    "replay_catalog_matrix",
    "write_catalog_replay_matrix",
)
