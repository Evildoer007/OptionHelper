"""Payoffer、Pricer与Backtester共用的合同条款展示协议。

本模块只描述合同字段如何被编辑和展示，不参与合同求值。页面提交的值仍由
``ResolvedContract``编译器验证，避免展示层成为第二套合同规则。
"""

from __future__ import annotations

import ast
from typing import Any, Mapping


EDITABLE = "editable"
MARKET_BOUND = "market_bound"
DERIVED = "derived"
FIXED_PRODUCT_RULE = "fixed_product_rule"
INTERNAL_SCALE = "internal_scale"

_MARKET_BOUND_KEYS = frozenset({"S0", "S0Vec"})
_DERIVED_KEYS = frozenset({"monitor", "constraints", "derived_terms"})
_INTERNAL_SCALE_KEYS = frozenset({
    "N", "Nvar", "Nvega", "G", "payoff_figure_basis", "payoff_normalizer",
})
_FIXED_RULE_KEYS = frozenset({"pricing_methods", "settlement", "margin_call"})

_DISPLAY_LABELS = {
    "O_KO": "敲出观察频率",
    "O_KI": "敲入观察频率",
    "Oc": "派息观察频率",
    "Otouch": "触碰观察频率",
    "Orange": "区间观察频率",
    "Ovar": "方差观察频率",
    "Ohedge": "避险观察频率",
    "Oreset": "重置观察频率",
    "exercise_style": "到期行权方式",
}

_EDITABILITY_REASONS = {
    MARKET_BOUND: "由估值日或历史入场日行情冻结，不能作为独立合同条款修改。",
    DERIVED: "由其他合同条款和正式编译规则派生，修改依赖字段后自动重算。",
    FIXED_PRODUCT_RULE: "属于已验证产品规则；当前产品引擎不支持独立修改。",
    INTERNAL_SCALE: "仅用于内部标准化、现金流归一或风险计算，不是对客合同条款。",
}


def build_term_fields(
    terms: Mapping[str, Any],
    term_catalog: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """返回稳定的可编辑与只读字段目录。"""
    constraints = terms.get("constraints", ())
    fields = [
        _field_spec(key, value, term_catalog.get(key), terms=terms, constraints=constraints)
        for key, value in terms.items()
    ]
    return {
        "payoff_fields": [field for field in fields if field["editability"] == EDITABLE],
        "fixed_fields": [field for field in fields if field["editability"] != EDITABLE],
        "contract_fields": fields,
    }


def _field_spec(
    key: str,
    value: Any,
    metadata: Mapping[str, Any] | None,
    *,
    terms: Mapping[str, Any],
    constraints: Any,
) -> dict[str, Any]:
    metadata = metadata if isinstance(metadata, Mapping) else {}
    unit = str(metadata.get("unit") or _fallback_unit(value))
    editability, constraint_reason = _editability(key, metadata, constraints)
    reason = None if editability == EDITABLE else constraint_reason or _EDITABILITY_REASONS[editability]
    return {
        "key": key,
        "label": _DISPLAY_LABELS.get(key, str(metadata.get("name_zh") or key)),
        "symbol": str(metadata.get("symbol") or key),
        "value_type": str(metadata.get("value_type") or _fallback_type(value)),
        "unit": unit,
        "domain": _presentation_domain(metadata.get("domain")),
        "default_value": value,
        "editability": editability,
        "editability_reason": reason,
        "display_unit": _display_unit(key, unit),
        "value_encoding": _value_encoding(key, unit),
        "dependency_group": _dependency_group(str(metadata.get("symbol") or key), constraints),
        "tenor_role": _tenor_role(key, terms),
    }


def _editability(
    key: str,
    metadata: Mapping[str, Any],
    constraints: Any,
) -> tuple[str, str | None]:
    if key in _MARKET_BOUND_KEYS:
        return MARKET_BOUND, None
    if key in _DERIVED_KEYS:
        return DERIVED, None
    if key in _INTERNAL_SCALE_KEYS:
        return INTERNAL_SCALE, None
    if key in _FIXED_RULE_KEYS or not metadata:
        return FIXED_PRODUCT_RULE, None
    domain = metadata.get("domain")
    if isinstance(domain, Mapping) and isinstance(domain.get("enum"), (list, tuple)) and len(domain["enum"]) <= 1:
        return FIXED_PRODUCT_RULE, "当前产品只有一个已验证取值，不支持独立修改。"
    symbol = str(metadata.get("symbol") or key)
    ambiguous_reason = _ambiguous_constraint_dependency(symbol, constraints)
    if ambiguous_reason is not None:
        return FIXED_PRODUCT_RULE, ambiguous_reason
    binding = _direct_constraint_binding(symbol, constraints)
    if binding is not None:
        expression, depends_on_terms = binding
        if depends_on_terms:
            return DERIVED, f"由产品约束{expression}确定；修改其依赖条款时由合同编译器联动校验。"
        return FIXED_PRODUCT_RULE, f"由产品固定约束{expression}确定，不能独立修改。"
    return EDITABLE, None


def _direct_constraint_binding(symbol: str, constraints: Any) -> tuple[str, bool] | None:
    """识别``字段 == 表达式``形式的非独立条款。

    这里只收紧展示层的独立编辑能力，不改变Core的约束求值。复杂的联立约束仍
    通过``dependency_group``展示并由Core校验，避免UI自行发明求解规则。
    """

    if not isinstance(constraints, (list, tuple)):
        return None
    bindings: list[tuple[str, bool]] = []
    for raw in constraints:
        expression = str(raw)
        try:
            root = ast.parse(expression, mode="eval").body
        except SyntaxError:
            continue
        if (
            not isinstance(root, ast.Compare)
            or len(root.ops) != 1
            or not isinstance(root.ops[0], ast.Eq)
            or len(root.comparators) != 1
            or not isinstance(root.left, ast.Name)
            or root.left.id != symbol
        ):
            continue
        names = {
            node.id
            for node in ast.walk(root.comparators[0])
            if isinstance(node, ast.Name)
        }
        bindings.append((expression, bool(names)))
    return bindings[0] if len(bindings) == 1 else None


def _ambiguous_constraint_dependency(symbol: str, constraints: Any) -> str | None:
    """锁定无法由单一赋值规则安全联动的重复等式依赖。"""

    if not isinstance(constraints, (list, tuple)):
        return None
    assignments: dict[str, list[tuple[str, set[str]]]] = {}
    for raw in constraints:
        expression = str(raw)
        try:
            root = ast.parse(expression, mode="eval").body
        except SyntaxError:
            continue
        if (
            not isinstance(root, ast.Compare)
            or len(root.ops) != 1
            or not isinstance(root.ops[0], ast.Eq)
            or len(root.comparators) != 1
            or not isinstance(root.left, ast.Name)
        ):
            continue
        names = {
            node.id
            for node in ast.walk(root.comparators[0])
            if isinstance(node, ast.Name)
        }
        assignments.setdefault(root.left.id, []).append((expression, names))
    ambiguous = {
        left: rows
        for left, rows in assignments.items()
        if len(rows) > 1
    }
    for left, rows in ambiguous.items():
        if symbol == left or any(symbol in names for _, names in rows):
            expressions = "；".join(expression for expression, _ in rows)
            return f"受联立产品规则{expressions}共同约束，当前引擎不支持独立修改。"
    return None


def _display_unit(key: str, unit: str) -> str:
    if key == "T":
        return "年/月/自然日"
    if unit in {"rate", "volatility", "percentage"}:
        return "%"
    if unit == "premium_percent_s0_100":
        return "%"
    if unit == "price":
        return "点，S₀=100"
    if unit == "normalized_point":
        return "合同单位，S₀=100"
    if unit in {"day", "calendar_day"}:
        return "自然日"
    if unit in {"count", "observation_count"}:
        return "次"
    if unit in {"unit", "quantity"}:
        return "份"
    if unit == "year":
        return "年"
    return "" if unit in {"none", "enum", "boolean", "object", "flag", "schedule", "schedule_selector"} else unit


def _presentation_domain(raw_domain: Any) -> dict[str, Any]:
    """把观察规则作为可选项展示，同时保持底层存储值不变。"""

    domain = dict(raw_domain) if isinstance(raw_domain, Mapping) else {}
    selectors = domain.get("selectors")
    if domain.get("format") == "observation_schedule" and isinstance(selectors, (list, tuple)):
        domain["enum"] = list(selectors)
    return domain


def _value_encoding(key: str, unit: str) -> str:
    if key == "T":
        return "year_month_calendar_day_to_act365"
    if unit in {"rate", "volatility", "percentage"}:
        return "percentage_input_decimal_internal"
    if unit == "premium_percent_s0_100":
        return "percent_of_s0_100"
    if unit == "price":
        return "normalized_price_s0_100"
    if unit == "normalized_point":
        return "points_per_100_contract_units"
    if unit in {"count", "observation_count", "day", "calendar_day"}:
        return "integer"
    return "native"


def _dependency_group(key: str, constraints: Any) -> str | None:
    if not isinstance(constraints, (list, tuple)):
        return None
    related = [str(item) for item in constraints if key in str(item)]
    return "；".join(related) or None


def _tenor_role(key: str, terms: Mapping[str, Any]) -> str | None:
    if key == "T":
        return "civil_act365_with_observation_endpoint" if "n_obs" in terms else "civil_act365"
    if key == "n_obs":
        return "observation_session_count"
    return None


def _fallback_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    return "object"


def _fallback_unit(value: Any) -> str:
    return "boolean" if isinstance(value, bool) else "object"
