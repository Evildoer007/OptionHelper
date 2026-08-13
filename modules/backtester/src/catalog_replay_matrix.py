"""登记产品的受控历史回放矩阵。

此文件只把已有的单产品正式回放结果整理为审计矩阵，不重算路径、现金流或指标。
"""

from __future__ import annotations

import csv
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from runtime.contracts.contract_api import load_registry, resolve_contract

from .historical_data import HistoricalData
from .impl.config import BacktestConfig
from .impl.engine import backtest
from .models import BacktestInput


MATRIX_COLUMNS = (
    "product_id", "product_name", "contract_fingerprint", "strike_terms", "data_asset_ref", "entry_rule", "complete_tenor",
    "smoke", "formal_evidence",
    "sample_count", "skipped_count", "skipped_entries", "skipped_reason_counts", "observed_events",
    "observed_path_cases", "ledger", "ledger_hash", "branch_coverage", "gross_contract_return",
    "win_rate", "client_net_return", "client_net_pnl",
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
        base = _base_row(product_id, product_name, historical_data, config)
        try:
            contract = resolve_contract(
                product_id,
                identity={
                    "underlyings": underlyings,
                    "reference_prices": {underlying: 100.0 for underlying in underlyings},
                    "calendar_id": historical_data.calendar_id,
                    "calendar_version": historical_data.calendar_version,
                },
                registry=registry,
                trading_dates=historical_data.trading_sessions,
            )
            payload = backtest(BacktestInput(contract, config, historical_data)).to_dict()
        except Exception as error:  # 产品级阻断必须留在矩阵，不能因为单项失败丢行。
            rows.append({
                **base,
                "smoke": {"status": "blocked", "reason": f"{type(error).__name__}:{error}"},
            })
            continue
        rows.append(_completed_row(base, payload, contract))
    return rows


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
    historical_data: HistoricalData,
    config: BacktestConfig,
) -> dict[str, Any]:
    return {
        "product_id": product_id,
        "product_name": product_name,
        "contract_fingerprint": None,
        "strike_terms": {},
        "data_asset_ref": deepcopy(historical_data.data_asset_ref),
        "entry_rule": config.entry_rule,
        "complete_tenor": config.complete_tenor,
        "smoke": {"status": "blocked", "reason": "not_run"},
        "formal_evidence": _formal_evidence(historical_data),
        "sample_count": 0,
        "skipped_count": 0,
        "skipped_entries": [],
        "skipped_reason_counts": {},
        "observed_events": {},
        "observed_path_cases": [],
        "ledger": [],
        "ledger_hash": None,
        "branch_coverage": {"status": "blocked"},
        "gross_contract_return": {
            "basis": "contract_cashflow_before_external_costs",
            "display_unit": "percentage",
            "value_encoding": "decimal_ratio",
            "external_costs_modelled": False,
            "total": None,
            "average": None,
        },
        "win_rate": {
            "numerator": "positive_gross_contract_return_count",
            "numerator_count": 0,
            "denominator": "valid_return_sample_count",
            "denominator_count": 0,
            "rate": None,
        },
        "client_net_return": {"status": "not_modelled", "value": None},
        "client_net_pnl": {"status": "not_modelled", "value": None},
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
    return {
        **base,
        "smoke": {
            "status": "passed",
            "reason": None,
            "sample_status": "partial" if coverage["status"] != "complete" or skipped else "complete",
        },
        "contract_fingerprint": payload["contract_fingerprint"],
        "strike_terms": _strike_terms(contract),
        "data_asset_ref": deepcopy(payload["data_asset_ref"]),
        "sample_count": int(payload["sample_count"]),
        "skipped_count": int(payload["skipped_count"]),
        "skipped_entries": skipped,
        "skipped_reason_counts": dict(sorted(Counter(item["reason"] for item in skipped).items())),
        "observed_events": deepcopy(payload["event_summary"]),
        "observed_path_cases": deepcopy(coverage["observed_pairs"]),
        "ledger": ledger,
        "ledger_hash": payload["ledger_hash"],
        "branch_coverage": coverage,
        "gross_contract_return": {
            "basis": payload["economic_convention"]["gross_return_basis"],
            "display_unit": payload["economic_convention"]["gross_return_display_unit"],
            "value_encoding": payload["economic_convention"]["gross_return_value_encoding"],
            "external_costs_modelled": payload["economic_convention"]["external_costs_modelled"],
            "total": float(sum(float(trade["gross_contract_return"]) for trade in ledger)),
            "average": common["average_gross_return"],
        },
        "win_rate": {
            "numerator": "positive_gross_contract_return_count",
            "numerator_count": common["positive_return_count"],
            "denominator": "valid_return_sample_count",
            "denominator_count": common["valid_return_sample_count"],
            "rate": common["win_rate"],
        },
        "client_net_return": {"status": "not_modelled", "value": None},
        "client_net_pnl": {"status": "not_modelled", "value": None},
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


__all__ = ("MATRIX_COLUMNS", "replay_catalog_matrix", "write_catalog_replay_matrix")
