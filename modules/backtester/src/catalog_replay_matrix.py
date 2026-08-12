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
    "product_id", "product_name", "status", "data_asset_ref", "entry_rule", "complete_tenor",
    "sample_count", "skipped_count", "skipped_entries", "skipped_reason_counts", "observed_events",
    "observed_path_cases", "ledger", "ledger_hash", "branch_coverage", "contract_cashflow_pnl",
    "return_denominator", "win_rate", "client_net_pnl", "reason",
)


def replay_catalog_matrix(historical_data: HistoricalData, config: BacktestConfig) -> list[dict[str, Any]]:
    """逐个回放OptionReg登记结构，并准确保留complete、partial或blocked状态。"""

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
            contract = resolve_contract(product_id, identity={"underlyings": underlyings}, registry=registry)
            payload = backtest(BacktestInput(contract, config, historical_data)).to_dict()
        except Exception as error:  # 产品级阻断必须留在矩阵，不能因为单项失败丢行。
            rows.append({
                **base,
                "status": "blocked",
                "reason": f"{type(error).__name__}:{error}",
            })
            continue
        rows.append(_completed_row(base, payload))
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
        "status": "blocked",
        "data_asset_ref": deepcopy(historical_data.data_asset_ref),
        "entry_rule": config.entry_rule,
        "complete_tenor": config.complete_tenor,
        "sample_count": 0,
        "skipped_count": 0,
        "skipped_entries": [],
        "skipped_reason_counts": {},
        "observed_events": {},
        "observed_path_cases": [],
        "ledger": [],
        "ledger_hash": None,
        "branch_coverage": {"status": "blocked"},
        "contract_cashflow_pnl": {
            "basis": "contract_cashflow_before_external_costs",
            "external_costs_modelled": False,
            "total": None,
            "average": None,
        },
        "return_denominator": {
            "convention": config.return_denominator,
            "values": [],
            "not_applicable_count": 0,
        },
        "win_rate": {
            "numerator": "contract_cashflow_pnl_gt_zero_count",
            "numerator_count": 0,
            "denominator": "valid_trade_count",
            "denominator_count": 0,
            "rate": None,
        },
        "client_net_pnl": {"status": "not_modelled", "value": None},
        "reason": None,
    }


def _completed_row(base: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    common = dict(payload["common_metrics"])
    ledger = list(payload["trade_ledger"])
    coverage = dict(payload["branch_coverage"])
    skipped = list(payload["skipped_entries"])
    denominator_values = sorted({
        float(trade["return_denominator"])
        for trade in ledger
        if trade.get("return_denominator") is not None
    })
    status_reasons = []
    if coverage["status"] != "complete":
        status_reasons.append("uncovered_branch_pairs")
    if skipped:
        status_reasons.append("skipped_entries")
    status = "partial" if status_reasons else "complete"
    return {
        **base,
        "status": status,
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
        "contract_cashflow_pnl": {
            "basis": payload["economic_convention"]["pnl_basis"],
            "external_costs_modelled": payload["economic_convention"]["external_costs_modelled"],
            "total": float(sum(float(trade["contract_cashflow_pnl"]) for trade in ledger)),
            "average": common["average_pnl"],
        },
        "return_denominator": {
            "convention": payload["backtest_config"]["return_denominator"],
            "values": denominator_values,
            "not_applicable_count": common["return_not_applicable_count"],
        },
        "win_rate": {
            "numerator": "contract_cashflow_pnl_gt_zero_count",
            "numerator_count": int(sum(float(trade["contract_cashflow_pnl"]) > 0.0 for trade in ledger)),
            "denominator": "valid_trade_count",
            "denominator_count": len(ledger),
            "rate": common["win_rate"],
        },
        "client_net_pnl": {"status": "not_modelled", "value": None},
        "reason": ",".join(status_reasons) if status_reasons else None,
    }


def _underlyings_for_product(product: Mapping[str, Any]) -> list[str]:
    return ["000905.SH", "000300.SH"] if "S0Vec" in product["terms"] else ["000905.SH"]


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
