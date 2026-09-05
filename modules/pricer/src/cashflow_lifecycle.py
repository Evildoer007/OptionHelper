"""合同现金流生命周期的统一估值过滤器。

数值引擎只负责给出现金流现值。本模块把合同起始现金流从未来价值中拆出，
再按估值日决定其属于当日现金流还是已实现现金流，Analytical与Monte Carlo
共同使用这一套规则。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
import math
import re
from typing import Any, Mapping

from runtime.contracts.contract_api import ResolvedContract

from .greeks import add_time_zero_cashflow


_TIME_ZERO_CASHFLOW = re.compile(r"\bcash\s*\(\s*0(?:\.0+)?\s*,")


def contractual_initial_cashflow(contract: ResolvedContract, quantity: float = 1.0) -> float:
    """返回持有人视角的合同起始现金流，单位与合同现金流一致。"""

    terms = contract.terms
    if contract.product_id in {
        "1.1", "1.2", "4.1", "4.2", "4.3", "4.4", "4.5", "4.6",
        "4.7", "4.8", "5.1", "5.2", "5.3", "5.4", "5.5", "5.6",
    }:
        return -float(quantity) * float(terms.get("Pi_0", 0.0))
    if contract.product_id in {
        "6.1", "6.2", "6.3", "9.1", "9.3", "9.5", "9.6", "9.7", "9.8",
    }:
        return -float(terms.get("N", 0.0)) * float(terms.get("p", 0.0))
    if contract.product_id in {"2.2", "2.4"}:
        return float(terms.get("P_net", 0.0))
    if contract.product_id in {"2.1", "2.3", "3.1", "3.2", "3.3", "3.4"}:
        return -float(terms.get("P_net", 0.0))
    if contract.product_id == "8.16":
        return (
            -float(terms.get("N", 0.0))
            * float(terms.get("p", 0.0))
            * float(terms.get("T", 0.0))
        )
    return 0.0


def paths_include_initial_cashflow(contract: ResolvedContract) -> bool:
    """判断共享路径解释器的数值结果是否已包含``cash(0, ...)``。"""

    return any(
        _TIME_ZERO_CASHFLOW.search(str(case.get("pnl", ""))) is not None
        for path in contract.paths
        for case in path.get("cases", ())
        if isinstance(case, Mapping)
    )


def apply_cashflow_lifecycle(
    result: Any,
    *,
    contract: ResolvedContract,
    valuation_date: str | None,
    observed_state: Any,
    initial_cashflow: float,
    cashflow_scale: float | None,
    numerical_value_includes_initial: bool,
) -> Any:
    """返回只按本次估值日分类后的当前价值和现金流分解。

    ``remaining_value``始终不含合同起始现金流。估值日在起始日时，起始现金流
    计入当前价值；估值日晚于起始日时，它只进入``realized_cashflows``，不会
    再次影响当前剩余价值。
    """

    start_value = contract.identity.get("contract_start_date")
    relation = _valuation_relation(valuation_date, start_value)
    future_result = result
    if numerical_value_includes_initial and initial_cashflow != 0.0:
        future_result = add_time_zero_cashflow(
            future_result,
            -initial_cashflow,
            cashflow_scale,
        )

    include_initial_now = relation in {"at_contract_start", "implicit_contract_start"}
    current_result = future_result
    if include_initial_now and initial_cashflow != 0.0:
        current_result = add_time_zero_cashflow(
            current_result,
            initial_cashflow,
            cashflow_scale,
        )

    realized = _normalized_realized_cashflows(observed_state, cashflow_scale)
    current_cashflows: list[dict[str, Any]] = []
    initial_record = _cashflow_record(
        cashflow_id="contract-initial-cashflow",
        payment_date=None if start_value in {None, ""} else str(start_value),
        cashflow=initial_cashflow,
        cashflow_scale=cashflow_scale,
        status=(
            "included_at_valuation"
            if include_initial_now
            else "realized_before_valuation"
            if relation == "after_contract_start"
            else "not_yet_due"
        ),
        source="resolved_contract",
    )
    if initial_cashflow != 0.0:
        if include_initial_now:
            current_cashflows.append(initial_record)
        elif relation == "after_contract_start" and not _contains_equivalent_cashflow(
            realized,
            initial_record,
        ):
            realized.append(initial_record)

    remaining_value = _result_value(future_result)
    current_value = _result_value(current_result)
    realized_percent = sum(
        float(item.get("pv_percent", 0.0))
        for item in realized
        if isinstance(item.get("pv_percent"), (int, float))
    )
    current_cashflow_percent = sum(
        float(item.get("pv_percent", 0.0))
        for item in current_cashflows
        if isinstance(item.get("pv_percent"), (int, float))
    )
    remaining_percent = remaining_value.get("pv_percent")
    cumulative_percent = (
        None
        if remaining_percent is None
        else realized_percent + current_cashflow_percent + float(remaining_percent)
    )
    lifecycle = {
        "schema": "optionhelper.pricer-cashflow-lifecycle.v1",
        "valuation_relation": relation,
        "initial_cashflow": initial_record if initial_cashflow != 0.0 else None,
        "realized_cashflows": realized,
        "current_cashflows": current_cashflows,
        "remaining_value": remaining_value,
        "current_value": current_value,
        "cumulative_pnl": {
            "realized_cashflows": {"pv_percent": realized_percent},
            "current_cashflows": {"pv_percent": current_cashflow_percent},
            "remaining_value": {"pv_percent": remaining_percent},
            "total": {"pv_percent": cumulative_percent},
        },
    }
    return replace(
        current_result,
        cashflow_lifecycle=lifecycle,
        diagnostics={
            **dict(current_result.diagnostics),
            "cashflow_lifecycle": {
                "valuation_relation": relation,
                "numerical_value_includes_initial": bool(numerical_value_includes_initial),
                "headline_value_basis": (
                    "current_value_including_contract_start_cashflow"
                    if include_initial_now
                    else "remaining_value_excluding_realized_cashflows"
                ),
            },
        },
    )


def _valuation_relation(valuation_value: object, start_value: object) -> str:
    if start_value in {None, ""}:
        return "implicit_contract_start"
    start = _date(start_value, "contract_start_date")
    if valuation_value in {None, ""}:
        return "implicit_contract_start"
    valuation = _date(valuation_value, "valuation_date")
    if valuation < start:
        raise ValueError("valuation_date不得早于contract_start_date")
    return "at_contract_start" if valuation == start else "after_contract_start"


def _normalized_realized_cashflows(observed_state: Any, cashflow_scale: float | None) -> list[dict[str, Any]]:
    raw = getattr(observed_state, "realized_cashflows", ())
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        amount = item.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(float(amount)):
            continue
        result.append(_cashflow_record(
            cashflow_id=str(item.get("cashflow_id") or f"observed-{index + 1}"),
            payment_date=None if item.get("payment_date") is None else str(item.get("payment_date")),
            cashflow=float(amount),
            cashflow_scale=cashflow_scale,
            status=str(item.get("status") or "realized"),
            source="observed_contract_state",
        ))
    return result


def _cashflow_record(
    *,
    cashflow_id: str,
    payment_date: str | None,
    cashflow: float,
    cashflow_scale: float | None,
    status: str,
    source: str,
) -> dict[str, Any]:
    points = float(cashflow) if cashflow_scale is None else float(cashflow) / float(cashflow_scale) * 100.0
    return {
        "cashflow_id": cashflow_id,
        "payment_date": payment_date,
        "status": status,
        "source": source,
        "pv_points_100": points,
        "pv_percent": points / 100.0,
    }


def _result_value(result: Any) -> dict[str, float | None]:
    return {
        "pv_amount": None if result.pv_amount is None else float(result.pv_amount),
        "pv_points_100": None if result.pv_points_100 is None else float(result.pv_points_100),
        "pv_percent": None if result.pv_percent is None else float(result.pv_percent),
    }


def _contains_equivalent_cashflow(
    realized: list[dict[str, Any]],
    candidate: Mapping[str, Any],
) -> bool:
    return any(
        item.get("payment_date") == candidate.get("payment_date")
        and math.isclose(
            float(item.get("pv_percent", math.nan)),
            float(candidate.get("pv_percent", math.inf)),
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        for item in realized
    )


def _date(value: object, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise ValueError(f"{label}必须为YYYY-MM-DD") from error


__all__ = (
    "apply_cashflow_lifecycle",
    "contractual_initial_cashflow",
    "paths_include_initial_cashflow",
)
