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
    "9.5", "9.6", "9.7", "9.8",
})
_PREMIUM_FIXED_ZERO = frozenset({"6.2", "6.3", "9.2"})
_PREMIUM_ROLE_BLOCKED = frozenset({"9.3"})
_NO_SEPARATE_PREMIUM = frozenset(_ALL_PRODUCT_IDS) - _PREMIUM_INCLUDED - _PREMIUM_FIXED_ZERO - _PREMIUM_ROLE_BLOCKED

assert not (_PREMIUM_INCLUDED & _PREMIUM_FIXED_ZERO)
assert not (_PREMIUM_INCLUDED & _PREMIUM_ROLE_BLOCKED)
assert (
    _PREMIUM_INCLUDED | _PREMIUM_FIXED_ZERO | _PREMIUM_ROLE_BLOCKED | _NO_SEPARATE_PREMIUM
) == frozenset(_ALL_PRODUCT_IDS)


def economic_convention(product_id: str) -> dict[str, Any]:
    """返回产品级合同现金流审计结论和公共收益定义。"""
    if product_id not in _ALL_PRODUCT_IDS:
        raise KeyError(f"未登记产品经济口径:{product_id}")
    if product_id in _PREMIUM_INCLUDED:
        premium_status = "included_in_contract_cashflows"
        premium_explanation = "OptionReg已将期权费声明为合同现金流，合同结算收益率已包含该现金流。"
    elif product_id in _PREMIUM_FIXED_ZERO:
        premium_status = "fixed_zero"
        premium_explanation = "该产品登记的期权费固定为零，不产生独立期权费现金流。"
    elif product_id in _PREMIUM_ROLE_BLOCKED:
        premium_status = "blocked_unrepresented"
        premium_explanation = "产品存在期权费型经济描述，但OptionReg现金流未证明其独立经济角色；页面必须展示口径限制。"
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
        "client_net_return_status": "not_modelled",
        "client_net_pnl_status": "not_modelled",
        "positive_return_rate_numerator": "positive_contract_settlement_return_count",
        "positive_return_rate_denominator": "valid_return_sample_count",
        # v1兼容读取字段；页面和报告不得展示旧名称。
        "gross_return_basis": "contract_cashflow_before_external_costs",
        "gross_return_display_unit": "percentage",
        "gross_return_value_encoding": "decimal_ratio",
        "win_rate_numerator": "positive_gross_contract_return_count",
        "win_rate_denominator": "valid_return_sample_count",
    }


def economic_convention_catalog() -> dict[str, dict[str, Any]]:
    return {product_id: economic_convention(product_id) for product_id in _ALL_PRODUCT_IDS}
