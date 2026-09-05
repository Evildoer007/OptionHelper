"""65产品合同结算收益口径目录。"""

from __future__ import annotations

from typing import Any


_ALL_PRODUCT_IDS = tuple(
    ["1.1", "1.2"]
    + [f"2.{index}" for index in range(1, 5)]
    + [f"3.{index}" for index in range(1, 5)]
    + [f"4.{index}" for index in range(1, 9)]
    + [f"5.{index}" for index in range(1, 7)]
    + ["6.1", "6.2", "6.3", "7.1"]
    + [f"8.{index}" for index in range(1, 30)]
    + [f"9.{index}" for index in range(1, 9)]
)

_PREMIUM_INCLUDED = frozenset({
    "1.1", "1.2", "2.1", "2.2", "2.3", "2.4", "3.1", "3.2", "3.3", "3.4",
    "4.1", "4.2", "4.3", "4.4", "4.5", "4.6", "4.7", "4.8",
    "5.1", "5.2", "5.3", "5.4", "5.5", "5.6", "6.1", "8.16", "9.1",
    "9.3", "9.5", "9.6", "9.7", "9.8",
})
_PREMIUM_FIXED_ZERO = frozenset({"6.2", "6.3", "9.2"})
_NO_SEPARATE_PREMIUM = frozenset(_ALL_PRODUCT_IDS) - _PREMIUM_INCLUDED - _PREMIUM_FIXED_ZERO

assert not (_PREMIUM_INCLUDED & _PREMIUM_FIXED_ZERO)
assert (_PREMIUM_INCLUDED | _PREMIUM_FIXED_ZERO | _NO_SEPARATE_PREMIUM) == frozenset(_ALL_PRODUCT_IDS)


def economic_convention(product_id: str) -> dict[str, Any]:
    """返回产品级合同现金流审计结论和公共收益定义。"""
    if product_id not in _ALL_PRODUCT_IDS:
        raise KeyError(f"未登记产品经济口径:{product_id}")
    if product_id == "9.3":
        premium_status = "included_in_contract_cashflows"
        premium_explanation = "OptionReg将p作为独立可编辑的期初期权费率，并以CF0=-N*p计入合同现金流；该费用已计入合同结算收益率。"
    elif product_id in _PREMIUM_INCLUDED:
        premium_status = "included_in_contract_cashflows"
        premium_explanation = "OptionReg已将期权费声明为合同现金流，合同结算收益率已包含该现金流。"
    elif product_id in _PREMIUM_FIXED_ZERO:
        premium_status = "fixed_zero"
        premium_explanation = "该产品登记的期权费固定为零，不产生独立期权费现金流。"
    else:
        premium_status = "no_separate_premium_cashflow"
        premium_explanation = "该产品没有独立期权费现金流。"
    return {
        "metric": "contract_settlement_return",
        "display_name": "合同结算收益率",
        "basis": "declared_contract_cashflows_over_contract_scale",
        "display_unit": "percentage",
        "value_encoding": "decimal_ratio",
        "contractual_premium_status": premium_status,
        "contractual_premium_explanation": premium_explanation,
        "external_costs_modelled": False,
        "excluded_external_costs": ["funding_cost", "transaction_cost", "tax", "hedging_cost", "slippage"],
        "positive_return_rate_numerator": "positive_return_count",
        "positive_return_rate_denominator": "valid_return_sample_count",
    }


def economic_convention_catalog() -> dict[str, dict[str, Any]]:
    return {product_id: economic_convention(product_id) for product_id in _ALL_PRODUCT_IDS}
