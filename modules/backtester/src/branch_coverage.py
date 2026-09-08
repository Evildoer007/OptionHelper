"""按真实逐笔结果记录合同声明的path/case及损失分支覆盖证据。"""

from __future__ import annotations

from math import isfinite
from typing import Any, Mapping, Sequence


def branch_coverage(contract: Any, trades: Sequence[Any]) -> dict[str, Any]:
    """只报告已观察结果与缺口，不合成行情或推断未发生分支。"""
    product_id = str(contract.product_id)
    negative_pairs = _negative_settlement_pairs(trades)
    declared = [
        {
            "product_id": product_id,
            "path_id": path_id,
            "case_id": case_id,
            "code": f"path_{path_id + 1}_case_{case_id + 1}",
            "condition": str(path["condition"]),
            "domain": str(case["domain"]),
            **_loss_capability(case, observed_negative=(path_id, case_id) in negative_pairs),
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
    unclassified = [item for item in declared if item["loss_capable"] is None]
    limitations = ["coverage_does_not_claim_unobserved_scenarios_were_economically_exercised"]
    if unclassified:
        limitations.append("potential_loss_classification_incomplete")
    if any(item.get("loss_metadata_conflict") for item in declared):
        limitations.append("loss_metadata_conflicts_with_observed_settlement")
    declared_loss = [item for item in declared if item["loss_capable"] is True]
    observed_loss = [item for item in observed if item["loss_capable"] is True]
    uncovered_loss = [item for item in uncovered if item["loss_capable"] is True]
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
        "loss_classification_status": "partial" if unclassified else "complete",
        "unclassified_pair_count": len(unclassified),
        "unclassified_pairs": unclassified,
        "declared_loss_pair_count": len(declared_loss),
        "observed_loss_pair_count": len(observed_loss),
        "uncovered_loss_pair_count": len(uncovered_loss),
        "declared_loss_pairs": declared_loss,
        "observed_loss_pairs": observed_loss,
        "uncovered_loss_pairs": uncovered_loss,
        "limitations": limitations,
    }


def _negative_settlement_pairs(trades: Sequence[Any]) -> set[tuple[int, int]]:
    """Use finite, actually settled returns as evidence of historical loss."""
    pairs: set[tuple[int, int]] = set()
    for trade in trades:
        try:
            value = float(trade.contract_settlement_return)
            key = (int(trade.path_id), int(trade.case_id))
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
        if isfinite(value) and value < 0.0:
            pairs.add(key)
    return pairs


def historical_loss_sample_covered(contract: Any, trades: Sequence[Any]) -> bool:
    """A negative settlement in a valid contract branch proves historical loss."""
    declared_pairs = {
        (path_id, case_id)
        for path_id, path in enumerate(contract.paths)
        for case_id, _case in enumerate(path["cases"])
    }
    return bool(declared_pairs & _negative_settlement_pairs(trades))


def _loss_capability(case: Any, *, observed_negative: bool) -> dict[str, Any]:
    """Keep explicit classifications; never infer a loss bound from punctuation."""
    metadata = case.get("settlement_metadata", {}) if isinstance(case, Mapping) else {}
    explicit = None
    for source, value in (
        ("case.loss_capable", case.get("loss_capable") if isinstance(case, Mapping) else None),
        ("case.loss_branch", case.get("loss_branch") if isinstance(case, Mapping) else None),
        ("case.settlement_metadata.loss_capable", metadata.get("loss_capable") if isinstance(metadata, Mapping) else None),
    ):
        if isinstance(value, bool):
            explicit = {"loss_capable": value, "loss_capability_source": source}
            break
    if observed_negative:
        result = {"loss_capable": True, "loss_capability_source": "observed_negative_settlement"}
        if explicit is not None and explicit["loss_capable"] is False:
            result["loss_metadata_conflict"] = True
        return result
    return explicit or {"loss_capable": None, "loss_capability_source": "not_proven"}


__all__ = ("branch_coverage", "historical_loss_sample_covered")
