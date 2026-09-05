"""按真实逐笔结果记录合同声明的path/case及损失分支覆盖证据。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


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
            **_loss_capability(case),
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
    declared_loss = [item for item in declared if item["loss_capable"]]
    observed_loss = [item for item in observed if item["loss_capable"]]
    uncovered_loss = [item for item in uncovered if item["loss_capable"]]
    return {
        "schema": "optionhelper.backtester.branch-coverage",
        "scope": "observed_trade_outcomes",
        "product_id": product_id,
        "status": "complete" if not uncovered else "partial",
        "declared_pair_count": len(declared),
        "observed_pair_count": len(observed),
        "uncovered_pair_count": len(uncovered),
        "declared_pairs": declared,
        "observed_pairs": observed,
        "uncovered_pairs": uncovered,
        "declared_loss_pair_count": len(declared_loss),
        "observed_loss_pair_count": len(observed_loss),
        "uncovered_loss_pair_count": len(uncovered_loss),
        "declared_loss_pairs": declared_loss,
        "observed_loss_pairs": observed_loss,
        "uncovered_loss_pairs": uncovered_loss,
        "limitations": ["coverage_does_not_claim_unobserved_scenarios_were_economically_exercised"],
    }


def historical_loss_sample_covered(contract: Any, trades: Sequence[Any]) -> bool:
    """仅当已声明损失分支被实际选中且结算收益为负时返回True。"""
    loss_pairs = {
        (path_id, case_id)
        for path_id, path in enumerate(contract.paths)
        for case_id, case in enumerate(path["cases"])
        if _loss_capability(case)["loss_capable"]
    }
    return any(
        (int(trade.path_id), int(trade.case_id)) in loss_pairs
        and float(trade.contract_settlement_return) < 0.0
        for trade in trades
    )


def _loss_capability(case: Any) -> dict[str, Any]:
    """优先消费显式元数据；旧合同仅作保守的结算表达式迁移识别。"""
    metadata = case.get("settlement_metadata", {}) if isinstance(case, Mapping) else {}
    for source, value in (
        ("case.loss_capable", case.get("loss_capable") if isinstance(case, Mapping) else None),
        ("case.loss_branch", case.get("loss_branch") if isinstance(case, Mapping) else None),
        ("case.settlement_metadata.loss_capable", metadata.get("loss_capable") if isinstance(metadata, Mapping) else None),
    ):
        if isinstance(value, bool):
            return {"loss_capable": value, "loss_capability_source": source}
    expression = str(case.get("pnl", "")) if isinstance(case, Mapping) else ""
    loss_capable = "-" in expression or "min(" in expression.casefold()
    return {
        "loss_capable": loss_capable,
        "loss_capability_source": "derived_from_settlement_expression",
    }


__all__ = ("branch_coverage", "historical_loss_sample_covered")
