"""按真实逐笔结果记录合同声明的path/case覆盖证据。"""

from __future__ import annotations

from typing import Any, Sequence


def branch_coverage(contract: Any, trades: Sequence[Any]) -> dict[str, Any]:
    """只报告已观察结果与缺口，不合成行情或推断未发生分支。"""
    product_id = str(contract.product_id)
    declared = [
        {
            "product_id": product_id,
            "path_id": path_id,
            "case_id": case_id,
            "code": f"path_{path_id + 1}_case_{case_id + 1}",
            "condition": str(path["condition"]),
            "domain": str(case["domain"]),
        }
        for path_id, path in enumerate(contract.paths)
        for case_id, case in enumerate(path["cases"])
    ]
    counts: dict[tuple[int, int], int] = {}
    for trade in trades:
        key = (int(trade.path_id), int(trade.case_id))
        counts[key] = counts.get(key, 0) + 1
    observed = [
        {**item, "trade_count": counts[(item["path_id"], item["case_id"])]}
        for item in declared
        if (item["path_id"], item["case_id"]) in counts
    ]
    uncovered = [
        {**item, "reason": "not_observed_in_backtest_sample"}
        for item in declared
        if (item["path_id"], item["case_id"]) not in counts
    ]
    return {
        "schema_version": "backtester.branch-coverage.v1",
        "scope": "observed_trade_outcomes",
        "product_id": product_id,
        "status": "complete" if not uncovered else "partial",
        "declared_pair_count": len(declared),
        "observed_pair_count": len(observed),
        "uncovered_pair_count": len(uncovered),
        "declared_pairs": declared,
        "observed_pairs": observed,
        "uncovered_pairs": uncovered,
        "limitations": ["coverage_does_not_claim_unobserved_scenarios_were_economically_exercised"],
    }


__all__ = ("branch_coverage",)
