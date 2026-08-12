"""将已验证证据冻结为ContractReportUnit或ProductKnowledgeUnit。

本文件只组织上游已经给出的事实，不估值、不回测、不计算Payoff，也不重算合同
指纹。对组合交付，仍然是一合同一ContractReportUnit，外层只保存单位顺序。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import re
from typing import Any, Mapping

from runtime.knowledger.registry_loader import load_term_catalog

from .models import (
    MODULE_TO_RUN,
    ReporterError,
    ReportRequest,
    SCHEMA_REPORT_UNIT,
    stable_hash,
)


DISCLAIMER = "风险提示：以上内容仅供结构说明和情景测算，不构成投资建议、收益承诺或正式报价。期权及结构化产品具有较高风险，可能发生部分或全部本金损失；实际结果以合同条款、市场报价和交易确认为准。"

_LIMITATION_TEXT = {
    "exchange_calendar_not_exposed_by_data_source_weekday_validation_only": "数据源未提供可核验的交易日历，本次仅按工作日校验样本日期。",
    "no_nav_curve_is_generated": "本次回测按到期损益统计，未生成持有期净值曲线。",
    "payment_calendar_not_exposed_by_shared_contract_core": "合同事实未提供独立付款日历，现金流日期按当前合同约定处理。",
    "coverage_does_not_claim_unobserved_scenarios_were_economically_exercised": "情景覆盖仅表示样本中观察到的路径，不代表未观察情景已实际发生。",
    "in_memory_historical_data_without_persisted_data_asset": "历史数据仅绑定本次运行，未形成可复用的数据资产。",
}
_TERM_LABELS = {
    "S0": "期初标的价格", "S0Vec": "期初标的价格", "K": "行权价", "T": "期限（年）",
    "K1": "执行价1", "K2": "执行价2", "P_net": "净权利金",
    "Pi_0": "期初权利金", "n_C": "合约数量", "notional": "名义本金", "coupon": "票息",
    "coupon_rate": "票息率", "knock_in_barrier": "敲入水平", "knock_out_barrier": "敲出水平",
    "barrier": "障碍水平", "exercise_style": "行权方式", "settlement": "结算方式",
    "observation_price": "观察价格", "margin_call": "追加保证金", "monitor": "观察设置",
    "pricing_methods": "适用估值方法", "constraints": "合同约束", "derived_terms": "派生条款",
}
_TERM_SYMBOLS = {
    "S0": "S₀", "S0Vec": "S₀", "K": "K", "T": "T", "Pi_0": "Π₀", "n_C": "nᶜ",
    "K1": "K_1", "K2": "K_2", "P_net": "P_net",
    "notional": "N", "coupon": "C", "coupon_rate": "c", "knock_in_barrier": "Hₖᵢ",
    "knock_out_barrier": "Hₖₒ", "barrier": "H",
}
_TERM_VALUE_TEXT = {
    "black_scholes": "Black-Scholes", "monte_carlo": "蒙特卡洛", "cash": "现金结算",
    "european": "欧式", "american": "美式", "close": "收盘价", "monthly": "每月",
    "daily": "每日", "weekly": "每周",
    "cny_per_spot": "人民币/标的价格点",
    "cny_per_spot_squared": "人民币/标的价格点²",
    "cny_per_1pct_volatility": "人民币/波动率变化1个百分点",
    "cny_per_1pct_vol": "人民币/波动率变化1个百分点",
    "cny_per_volatility_point": "人民币/波动率点",
    "cny_per_year": "人民币/年",
    "cny_per_calendar_day": "人民币/日",
    "cny_per_1pct_rate": "人民币/利率变化1个百分点",
    "cny": "人民币", "rmb": "人民币", "points": "点", "point": "点",
}
_MODULE_ONLY_TERMS = {
    "payoff_input": {"monitor"},
    "pricing_input": {"pricing_methods"},
    "backtest_input": set(),
}


_NUMBER_TEXT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _decimal_text(value: int | float | str) -> str:
    """Render public numerical values without scientific notation or noise."""

    numeric = float(value)
    if numeric == 0:
        return "0"
    absolute = abs(numeric)
    precision = 2 if absolute >= 10_000 else 4 if absolute >= 1 else 6 if absolute >= 0.01 else 8
    rendered = f"{numeric:,.{precision}f}".rstrip("0").rstrip(".")
    return "0" if rendered in {"-0", "-0.0"} else rendered


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _decimal_text(value)
    if isinstance(value, (list, tuple)):
        return "、".join(filter(None, (_text(item) for item in value)))
    if isinstance(value, Mapping):
        if not value:
            return "未设置"
        return "；".join(f"{key}：{_text(item)}" for key, item in value.items())
    if isinstance(value, str):
        known = _TERM_VALUE_TEXT.get(value.casefold())
        if known:
            return known
        return _decimal_text(value) if _NUMBER_TEXT.fullmatch(value.strip()) else value
    return str(value)


def _public_limitation(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text in _LIMITATION_TEXT:
        return _LIMITATION_TEXT[text]
    if re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+){2,}", text):
        return "上游分析存在未公开说明的技术限制，相关结论仅在已列示证据范围内适用。"
    return text


_PUBLIC_CHART_FIELDS = (
    "id", "title", "type", "x", "y", "data", "series",
    "x_axis_name", "y_axis_name", "z_axis_name", "value_format",
    "source_note", "accessibility_summary",
)


def public_chart_specs(value: Any) -> list[dict[str, Any]]:
    """Whitelist frozen chart facts before they enter a public ReportUnit.

    Charts are presentation specifications, not arbitrary upstream JSON.  This
    projection intentionally preserves numerical vectors and reader-facing
    labels while excluding provenance, run metadata and filesystem fields.
    """

    result: list[dict[str, Any]] = []
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, Mapping):
            continue
        chart: dict[str, Any] = {}
        for field in _PUBLIC_CHART_FIELDS:
            item = raw.get(field)
            if item is None:
                continue
            if field in {"id", "title", "type", "x_axis_name", "y_axis_name", "z_axis_name", "value_format", "source_note", "accessibility_summary"}:
                text = _text(item)
                if text:
                    chart[field] = text
            elif field in {"x", "y", "data"} and isinstance(item, list):
                chart[field] = deepcopy(item)
            elif field == "series" and isinstance(item, list):
                series: list[dict[str, Any]] = []
                for row in item:
                    if not isinstance(row, Mapping) or not isinstance(row.get("data"), list):
                        continue
                    public_row = {"data": deepcopy(row["data"])}
                    for key in ("name", "type", "value_format"):
                        text = _text(row.get(key))
                        if text:
                            public_row[key] = text
                    series.append(public_row)
                if series:
                    chart[field] = series
        if chart.get("id") and chart.get("title") and chart.get("type"):
            result.append(chart)
    return result


def _math_text(value: Any) -> str:
    """Preserve formula source notation for Designer's safe MathML renderer."""

    return str(value or "")


def _payoff_math_text(value: Any) -> str:
    text = str(value or "").strip()
    cursor = 0
    while True:
        start = text.find("cash(", cursor)
        if start < 0:
            break
        depth = 0
        comma = -1
        end = -1
        for index in range(start + 5, len(text)):
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    end = index
                    break
                depth -= 1
            elif char == "," and depth == 0 and comma < 0:
                comma = index
        if comma < 0 or end < 0:
            break
        timing = text[start + 5:comma].strip()
        amount = text[comma + 1:end].strip().replace("*", "×")
        label = "期初现金流" if timing == "0" else "到期现金流" if timing == "T" else f"{timing}时点现金流"
        replacement = f"{label}（{amount}）"
        text = text[:start] + replacement + text[end + 1:]
        cursor = start + len(replacement)
    return _math_text(text.replace("*", "×"))


def _percent_text(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return _text(value)
    return _decimal_text(float(value) * 100) + "%"


def _metric(label: str, value: Any, note: str = "", *, value_format: str = "number") -> dict[str, Any]:
    """Project a frozen fact for Designer without pre-rounding it."""

    row: dict[str, Any] = {"label": label, "value": deepcopy(value), "note": _text(note)}
    if value_format != "number":
        row["value_format"] = value_format
    return row


_HIGHLIGHT_TERM_ORDER = (
    "T", "N", "Nvar", "Nvega", "K", "K1", "K2", "K3", "K4", "Kp", "Kc", "Kd", "Ku", "Ksig",
    "H_KO", "H_KO_regular", "H_KO_final", "H_KO_1", "H_KO_2",
    "H_KI", "H_KI_1", "H_KI_2", "B", "Htouch", "Hc", "Hlow", "Hup", "Hfloor", "Hreset", "Bbuf",
    "Pi_0", "P_net", "p", "A", "c", "c_reset", "c_hedge", "c1", "c2", "c_g", "c_e", "c_max",
    "r_out", "r_mat", "r_cap", "alpha", "alpha_u", "alpha_d", "eta_u", "eta_d", "eta", "g",
    "exercise_style", "settlement", "observation_price", "n_C", "n_P", "asset_unit", "q", "m",
)
_SCHEDULE_TEXT = {
    "daily": "每个交易日",
    "monthly_last": "每月最后一个交易日",
}


def _currency_text(contract: Mapping[str, Any] | None) -> str:
    identity = contract.get("identity") if isinstance(contract, Mapping) and isinstance(contract.get("identity"), Mapping) else {}
    currency = _text(identity.get("currency"))
    return {"CNY": "人民币", "RMB": "人民币"}.get(currency.upper(), currency or "合同币种")


def _highlight_row(symbol: str, value: Any, contract: Mapping[str, Any], catalog: Mapping[str, Any]) -> dict[str, str]:
    definition = catalog.get(symbol) if isinstance(catalog.get(symbol), Mapping) else {}
    label = _text(definition.get("name_zh")) or symbol
    unit = _text(definition.get("unit"))
    if symbol == "T":
        label, note = "期限", "年"
    elif symbol == "N":
        label, note = "名义本金", _currency_text(contract)
    elif symbol == "c":
        return {"label": "年化票息", "value": _percent_text(value), "note": "年化，ACT/365"}
    elif unit in {"rate", "volatility"}:
        return {"label": label, "value": _percent_text(value), "note": "比例" if unit == "rate" else "年化波动率"}
    elif unit == "price":
        convention = contract.get("price_convention") if isinstance(contract.get("price_convention"), Mapping) else {}
        note = "S₀=100标准化水平" if convention.get("contract_basis") == "normalized_100" else _currency_text(contract)
    elif unit == "currency":
        note = _currency_text(contract)
    elif unit == "unit":
        note = "份合同"
    elif unit in {"count", "observation_count"}:
        note = "期"
    else:
        note = ""
    return {"label": label, "value": _text(value), "note": note}


def _contract_highlights(contract: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Select at most six public terms from the frozen contract.

    Shared term_catalog owns term names and units.  Reporter only selects and
    formats a compact public projection; Designer never interprets a contract.
    """

    if not isinstance(contract, Mapping):
        return []
    terms = contract.get("terms") if isinstance(contract.get("terms"), Mapping) else {}
    if "T" not in terms:
        raise ReporterError("正式单产品交付缺少合同期限T，不能生成关键条款")
    catalog = load_term_catalog()
    priority = _HIGHLIGHT_TERM_ORDER
    if any(symbol in terms for symbol in ("O_KO", "O_KI")) and "H_KO" in terms and "H_KI" in terms:
        priority = ("T", "N", "H_KO", "H_KI", "c", *[symbol for symbol in _HIGHLIGHT_TERM_ORDER if symbol not in {"T", "N", "H_KO", "H_KI", "c"}])
    rows = [
        _highlight_row(symbol, terms[symbol], contract, catalog)
        for symbol in priority
        if symbol in terms
    ]
    schedules = []
    for symbol, prefix in (("O_KO", "敲出"), ("O_KI", "敲入")):
        if symbol in terms:
            schedule = _SCHEDULE_TEXT.get(str(terms[symbol]), _text(terms[symbol]))
            schedules.append(f"{prefix}：{schedule}")
    if schedules:
        observation = {"label": "观察频率", "value": "；".join(schedules), "note": "仅交易日"}
        return [*rows[:5], observation]
    return rows[:6]


_GREEK_ORDER = ("Delta", "Gamma", "Vega", "Theta", "Rho")
_GREEK_KEYS = {name.casefold(): name for name in _GREEK_ORDER}
_RISK_CURVE_TITLES = {
    "delta_spot": "Delta风险曲线",
    "gamma_spot": "Gamma风险曲线",
    "theta_time": "Theta风险曲线",
    "vega_volatility": "Vega风险曲线",
}
_RISK_SURFACE_TITLES = {
    "delta_surface": "Delta曲面",
    "gamma_surface": "Gamma曲面",
    "theta_surface": "Theta曲面",
    "vega_surface": "Vega曲面",
}
_BACKTEST_LABELS = {
    "sample_count": "样本数", "skipped_count": "跳过样本数", "win_rate": "胜率",
    "average_pnl": "平均损益", "median_pnl": "中位损益", "minimum_pnl": "最小损益",
    "maximum_pnl": "最大损益", "max_loss": "最大亏损", "average_return": "平均合同现金流收益率",
    "median_return": "中位合同现金流收益率", "minimum_return": "最小合同现金流收益率", "maximum_return": "最大合同现金流收益率",
    "return_not_applicable_count": "收益率不适用样本数", "trigger_count": "触发次数",
    "trigger_rate": "触发比例", "average_days": "平均触发天数", "median_days": "中位触发天数",
    "true_count": "为真次数", "false_count": "为假次数", "true_rate": "为真比例",
    "average": "平均值", "median": "中位数", "minimum": "最小值", "maximum": "最大值",
    "positive_count": "正收益样本数", "flat_count": "零收益样本数", "negative_count": "负收益样本数",
    "positive_rate": "正收益比例", "negative_rate": "负收益比例",
    "knock_in_count": "敲入样本数", "knock_in_then_no_knock_out_count": "敲入未敲出样本数",
    "knock_in_then_no_knock_out_rate": "敲入未敲出比例",
    "knock_in_then_knock_out_count": "敲入后敲出样本数",
    "knock_in_then_knock_out_rate": "敲入后敲出比例",
    "paid_observation_count": "已支付观察期数", "scheduled_observation_count": "约定观察期数",
    "payment_rate": "派息比例", "unpaid_observation_count": "未支付观察期数",
    "non_negative_terminal_rate": "非负到期表现比例", "below_strike_count": "低于执行水平样本数",
    "at_or_above_strike_count": "不低于执行水平样本数", "below_strike_rate": "低于执行水平比例",
    "strike_volatility": "执行波动率", "spread": "与执行波动率之差",
    "events": "事件", "terminal_performance": "到期表现", "terminal_return_sign": "到期收益符号",
    "terminal_segments": "到期情景", "three_outcome_summary": "三类路径结果", "conditional_summary": "条件路径结果",
    "trigger_vs_untriggered": "触发与未触发结果", "knock_in_outcomes": "敲入结果",
    "buffer_outcomes": "缓冲结果", "accumulated_quantity": "累计数量", "pnl_per_accumulated_unit": "单位累计数量损益",
    "periodic_purchase_cashflows": "定期买入现金流", "realized_volatility": "实现波动率",
    "realized_variance": "实现方差", "realized_volatility_vs_strike": "实现波动率与执行水平",
    "volatility_buckets": "波动率区间", "variance_pnl": "方差收益损益", "range_observations": "区间观察",
    "in_range_observation_ratio": "区间内观察比例", "range_accrual_pnl": "区间计息损益",
    "profile_id": "", "metric_coverage": "",
}
_EVENT_LABELS = {
    "tau_out": "敲出", "tau_out_1": "第一敲出", "tau_out_2": "第二敲出", "tau_in": "敲入",
    "tau_touch": "触碰", "tau_reset": "重置", "tau_hedge": "避险触发",
}
_MONITOR_LABELS = {
    "Q_acc": "累计数量", "n_coupon": "累计派息期数", "n_in": "区间内观察次数",
    "n_obs_actual": "实际观察次数", "sigma_realized": "实现波动率", "S_out": "触发时价格",
}
_CARD_PROFILE_PATHS = {
    "terminal_payoff": (
        ("正收益比例", "terminal_return_sign.positive_rate", "percent"),
        ("负收益比例", "terminal_return_sign.negative_rate", "percent"),
        ("平均到期表现", "terminal_performance.average", "number"),
        ("最差到期表现", "terminal_performance.minimum", "number"),
    ),
    "dual_knock_autocall": (
        ("敲出比例", "events.tau_out.trigger_rate", "percent"),
        ("敲入比例", "events.tau_in.trigger_rate", "percent"),
        ("敲入未敲出比例", "conditional_summary.knock_in_then_no_knock_out_rate", "percent"),
        ("敲入后敲出比例", "conditional_summary.knock_in_then_knock_out_rate", "percent"),
    ),
    "coupon_autocall": (
        ("敲出比例", "events.tau_out.trigger_rate", "percent"),
        ("敲入比例", "events.tau_in.trigger_rate", "percent"),
        ("平均派息比例", "coupon_payment.payment_rate.average", "percent"),
        ("平均已支付观察期数", "coupon_payment.paid_observation_count.average", "number"),
    ),
    "single_knock_out": (
        ("敲出比例", "events.tau_out.trigger_rate", "percent"),
        ("触发比例", "trigger_vs_untriggered.triggered_rate", "percent"),
        ("触发平均损益", "trigger_vs_untriggered.triggered_pnl.average", "number"),
        ("未触发平均损益", "trigger_vs_untriggered.untriggered_pnl.average", "number"),
    ),
    "single_knock_out_autocall": (
        ("敲出比例", "events.tau_out.trigger_rate", "percent"),
        ("触发比例", "trigger_vs_untriggered.triggered_rate", "percent"),
        ("触发平均损益", "trigger_vs_untriggered.triggered_pnl.average", "number"),
        ("未触发平均损益", "trigger_vs_untriggered.untriggered_pnl.average", "number"),
    ),
    "single_knock_in": (
        ("敲入比例", "events.tau_in.trigger_rate", "percent"),
        ("敲入平均损益", "knock_in_outcomes.knock_in_pnl.average", "number"),
        ("未敲入平均损益", "knock_in_outcomes.not_knock_in_pnl.average", "number"),
        ("敲入平均到期表现", "knock_in_outcomes.knock_in_terminal_performance.average", "number"),
    ),
    "touch_binary": (
        ("触碰比例", "events.tau_touch.trigger_rate", "percent"),
        ("触发比例", "touch_vs_untouched.triggered_rate", "percent"),
        ("触发平均损益", "touch_vs_untouched.triggered_pnl.average", "number"),
        ("未触发平均损益", "touch_vs_untouched.untriggered_pnl.average", "number"),
    ),
    "airbag": (
        ("敲入比例", "events.tau_in.trigger_rate", "percent"),
        ("非负到期表现比例", "buffer_outcomes.non_negative_terminal_rate", "percent"),
        ("负到期表现比例", "buffer_outcomes.negative_terminal_rate", "percent"),
        ("平均到期表现", "buffer_outcomes.terminal_performance.average", "number"),
    ),
    "accumulator": (
        ("敲出比例", "events.tau_out.trigger_rate", "percent"),
        ("平均累计数量", "accumulated_quantity.Q_acc.average", "number"),
        ("单位累计数量平均损益", "pnl_per_accumulated_unit.average", "number"),
        ("合同买入价格", "contract_purchase_price", "number"),
    ),
    "variance_swap": (
        ("平均实现波动率", "realized_volatility.sigma_realized.average", "percent"),
        ("平均实现方差", "realized_variance.average", "number"),
        ("平均波动率差", "realized_volatility_vs_strike.spread.average", "percent"),
        ("低于执行波动率比例", "volatility_buckets.below_strike_rate", "percent"),
    ),
    "range_accrual": (
        ("平均区间内观察次数", "range_observations.n_in.average", "number"),
        ("平均实际观察次数", "range_observations.n_obs_actual.average", "number"),
        ("平均区间内观察比例", "in_range_observation_ratio.average", "percent"),
        ("平均区间计息损益", "range_accrual_pnl.average", "number"),
    ),
    "shark_fin": (
        ("敲出比例", "events.tau_out.trigger_rate", "percent"),
        ("触发比例", "trigger_vs_untriggered.triggered_rate", "percent"),
        ("平均到期表现", "terminal_performance.average", "number"),
        ("最差到期表现", "terminal_performance.minimum", "number"),
    ),
}


def _public_value_format(key: str) -> str:
    return "percent" if key.endswith(("_rate", "_return", "_percent")) else "number"


def _path_value(source: Mapping[str, Any], path: str) -> Any:
    value: Any = source
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _public_greek_rows(greeks: Any) -> list[dict[str, Any]]:
    values = greeks if isinstance(greeks, Mapping) else {}
    result: list[dict[str, Any]] = []
    for label in _GREEK_ORDER:
        raw = values.get(label, values.get(label.casefold())) if isinstance(values, Mapping) else None
        if isinstance(raw, Mapping):
            value = raw.get("pv_amount_value")
            unit = raw.get("pv_amount_unit")
            if value is None:
                value, unit = raw.get("value"), raw.get("unit")
            if value is None:
                value, unit = raw.get("pv_percent_value"), raw.get("pv_percent_unit")
            status = _text(raw.get("status"))
        else:
            value, unit, status = raw, "", ""
        result.append({
            "label": label,
            "value": deepcopy(value) if value is not None else "不适用",
            "unit": _text(unit),
            "status": status or ("not_applicable" if value is None else "available"),
        })
    return result


def _pricing_curve_charts(source: Any) -> list[dict[str, Any]]:
    charts: list[dict[str, Any]] = []
    for raw in source if isinstance(source, list) else []:
        item = raw if isinstance(raw, Mapping) else {}
        points = item.get("points") if isinstance(item.get("points"), list) else []
        x_values = [point.get("x") for point in points if isinstance(point, Mapping)]
        y_values = [point.get("y") for point in points if isinstance(point, Mapping)]
        if not x_values or len(x_values) != len(y_values):
            continue
        key = str(item.get("key", ""))
        charts.append({
            "id": key or f"pricing-curve-{len(charts) + 1}",
            "title": _RISK_CURVE_TITLES.get(key, _text(item.get("name")) or "风险曲线"),
            "type": "line", "x": x_values,
            "series": [{"name": _text(item.get("y_axis", {}).get("name") if isinstance(item.get("y_axis"), Mapping) else "数值"), "data": y_values}],
            "x_axis_name": _text(item.get("x_axis", {}).get("name") if isinstance(item.get("x_axis"), Mapping) else "横轴"),
            "y_axis_name": _text(item.get("y_axis", {}).get("name") if isinstance(item.get("y_axis"), Mapping) else "数值"),
            "source_note": "数据来源：本次估值结果。",
            "accessibility_summary": f"{_RISK_CURVE_TITLES.get(key, _text(item.get('name')) or '风险曲线')}展示本次估值中的敏感性变化。",
        })
    return charts


def _pricing_surface_charts(source: Any) -> list[dict[str, Any]]:
    charts: list[dict[str, Any]] = []
    for raw in source if isinstance(source, list) else []:
        item = raw if isinstance(raw, Mapping) else {}
        x_axis = item.get("x_axis") if isinstance(item.get("x_axis"), Mapping) else {}
        y_axis = item.get("y_axis") if isinstance(item.get("y_axis"), Mapping) else {}
        z_axis = item.get("z_axis") if isinstance(item.get("z_axis"), Mapping) else {}
        x_values = list(x_axis.get("values", [])) if isinstance(x_axis.get("values"), list) else []
        y_values = list(y_axis.get("values", [])) if isinstance(y_axis.get("values"), list) else []
        points = item.get("data", []) if isinstance(item.get("data"), list) else []
        if not x_values:
            x_values = list(dict.fromkeys(
                point.get("x") for point in points
                if isinstance(point, Mapping) and point.get("x") is not None
            ))
        if not y_values:
            y_values = list(dict.fromkeys(
                point.get("y") for point in points
                if isinstance(point, Mapping) and point.get("y") is not None
            ))
        cells: list[list[Any]] = []
        for point in points:
            if isinstance(point, Mapping):
                packed = point.get("value")
                if isinstance(packed, list) and len(packed) == 3:
                    cells.append(list(packed))
                elif all(key in point for key in ("x", "y", "z")):
                    try:
                        cells.append([x_values.index(point["x"]), y_values.index(point["y"]), point["z"]])
                    except ValueError:
                        pass
        if not x_values or not y_values or not cells:
            continue
        key = str(item.get("key", ""))
        charts.append({
            "id": key or f"pricing-surface-{len(charts) + 1}",
            "title": _RISK_SURFACE_TITLES.get(key, _text(item.get("name")) or "Greek曲面"),
            "type": "heatmap", "x": x_values, "y": y_values, "data": cells,
            "x_axis_name": _text(x_axis.get("name") or "标的价格"),
            "y_axis_name": _text(y_axis.get("name") or "剩余期限"),
            "z_axis_name": _text(z_axis.get("name") or "数值"),
            "source_note": "数据来源：本次估值结果。",
            "accessibility_summary": f"{_RISK_SURFACE_TITLES.get(key, _text(item.get('name')) or 'Greek曲面')}按标的价格与剩余期限展示本次估值敏感性。",
        })
    return charts


def _specialized_rows(source: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expose known product-specific facts without leaking machine keys."""

    rows: list[dict[str, Any]] = []

    def visit(value: Any, prefix: str = "") -> None:
        if not isinstance(value, Mapping):
            return
        for key, raw in value.items():
            key_text = str(key)
            if key_text in {"profile_id", "metric_coverage", "status", "gaps", "code", "name", "condition", "domain"}:
                continue
            label = _BACKTEST_LABELS.get(key_text) or _EVENT_LABELS.get(key_text) or _MONITOR_LABELS.get(key_text)
            if not label:
                continue
            full_label = f"{prefix}{label}" if prefix else label
            if isinstance(raw, Mapping):
                visit(raw, f"{full_label}：")
            elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
                rows.append({"label": full_label, "value": raw, "value_format": _public_value_format(key_text)})
            elif isinstance(raw, bool):
                rows.append({"label": full_label, "value": raw, "value_format": "text"})
        for key, raw in value.items():
            if not isinstance(raw, list):
                continue
            for item in raw:
                if not isinstance(item, Mapping):
                    continue
                item_label = _text(item.get("label"))
                if not item_label:
                    continue
                for field in ("count", "rate", "average_pnl"):
                    if item.get(field) is not None:
                        public = {"count": "样本数", "rate": "占比", "average_pnl": "平均损益"}[field]
                        rows.append({
                            "label": f"{prefix}{item_label}{public}", "value": item.get(field),
                            "value_format": "percent" if field == "rate" else "number",
                        })

    visit(source)
    return rows


def _card_metric_note(path: str, value_format: str, currency: str) -> str:
    if path.startswith("conditional_summary.knock_in_then_"):
        return "占已敲入样本"
    if path.endswith(("trigger_rate", "triggered_rate", "positive_rate", "negative_rate", "below_strike_rate", "non_negative_terminal_rate")):
        return "占有效入场样本"
    if "pnl" in path or path == "contract_purchase_price":
        return f"{currency}/份合同"
    if "days" in path:
        return "自然日"
    if "observation_count" in path:
        return "期"
    if "volatility" in path and value_format == "percent":
        return "年化波动率"
    return "口径未提供"


def _card_metrics(profile_id: str, specialized: Mapping[str, Any], *, currency: str) -> list[dict[str, Any]]:
    priorities = _CARD_PROFILE_PATHS.get(profile_id, ())
    return [
        {
            "label": label,
            "value": deepcopy(value) if (value := _path_value(specialized, path)) is not None else "未提供",
            "value_format": value_format if value is not None else "text",
            "note": _card_metric_note(path, value_format, currency),
        }
        for label, path, value_format in priorities
    ]


def _backtest_standard_charts(
    common: Mapping[str, Any],
    outcomes: list[dict[str, Any]],
    annual: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create visual specifications from existing frozen summary fields only."""

    charts: list[dict[str, Any]] = []
    raw_distribution = common.get("return_distribution")
    if isinstance(raw_distribution, Mapping):
        distribution_x = list(raw_distribution)
        distribution_y = list(raw_distribution.values())
    elif isinstance(raw_distribution, list):
        rows = [item for item in raw_distribution if isinstance(item, Mapping) and item.get("label") is not None]
        distribution_x = [row.get("label") for row in rows]
        distribution_y = [row.get("count") for row in rows]
    else:
        distribution_x, distribution_y = [], []
    if distribution_x:
        charts.append({
            "id": "backtest-return-distribution", "title": "收益率分布", "type": "bar",
            "x": distribution_x, "series": [{"name": "样本数", "data": distribution_y}],
            "x_axis_name": "收益区间", "y_axis_name": "样本数", "source_note": "数据来源：本次历史回测结果。",
            "accessibility_summary": "按收益区间统计本次回测样本数量。",
        })
    if outcomes:
        charts.append({
            "id": "backtest-outcome-distribution", "title": "路径结果分布", "type": "bar",
            "x": [row["label"] for row in outcomes], "series": [{"name": "样本占比", "data": [row.get("rate") for row in outcomes]}],
            "x_axis_name": "路径结果", "y_axis_name": "样本占比", "value_format": "percent",
            "source_note": "数据来源：本次历史回测结果。", "accessibility_summary": "按路径结果统计本次回测样本占比。",
        })
    if annual:
        charts.append({
            "id": "backtest-annual-performance", "title": "年度合同现金流表现", "type": "bar",
            "x": [row["year"] for row in annual], "series": [{"name": "平均合同现金流收益率", "data": [row.get("average_return") for row in annual]}],
            "x_axis_name": "年份", "y_axis_name": "平均合同现金流收益率", "value_format": "percent",
            "source_note": "数据来源：本次历史回测合同条款现金流，未扣外部成本。", "accessibility_summary": "按入场年份展示本次回测平均合同现金流收益率，未扣外部成本。",
        })
    if events:
        charts.append({
            "id": "backtest-event-rate", "title": "事件触发率", "type": "bar",
            "x": [row["event"] for row in events], "series": [{"name": "触发比例", "data": [row.get("trigger_rate") for row in events]}],
            "x_axis_name": "路径事件", "y_axis_name": "触发比例", "value_format": "percent",
            "source_note": "数据来源：本次历史回测结果。", "accessibility_summary": "展示本次回测中各路径事件触发比例。",
        })
    return charts


def _artifact_rows(module: Mapping[str, Any]) -> list[dict[str, str]]:
    manifest = module.get("artifact_manifest")
    if not isinstance(manifest, Mapping):
        return []
    values = manifest.get("artifacts", [])
    if not isinstance(values, list):
        return []
    rows = []
    for raw in values:
        if not isinstance(raw, Mapping):
            continue
        name, storage_ref, media_type, content_hash = (raw.get("name"), raw.get("storage_ref"), raw.get("media_type"), raw.get("content_hash"))
        if all(isinstance(value, str) and value for value in (name, storage_ref, media_type, content_hash)):
            rows.append({"name": name, "storage_ref": storage_ref, "media_type": media_type, "content_hash": content_hash})
    return rows


def _formula(result: Mapping[str, Any]) -> dict[str, str]:
    formula_mathml = result.get("formula_mathml")
    formula = result.get("formula")
    if isinstance(formula_mathml, str) and formula_mathml.strip():
        return {"formula_mathml": formula_mathml.strip()}
    if isinstance(formula, str) and formula.strip():
        return {"formula": formula.strip()}
    return {}


def _payoff_content(module: Mapping[str, Any]) -> dict[str, Any]:
    status = str(module.get("status", "not_run"))
    value: dict[str, Any] = {"status": status, "note": _text(module.get("note"))}
    if status != "ready":
        return value
    result = module.get("result") if isinstance(module.get("result"), Mapping) else {}
    value.update(_formula(result))
    paths = result.get("path_panels", result.get("paths", []))
    scenarios: list[dict[str, str]] = []
    if isinstance(paths, list):
        for path_index, raw in enumerate(paths, start=1):
            if not isinstance(raw, Mapping):
                continue
            path_title = _text(raw.get("title") or raw.get("name") or f"路径{path_index}")
            condition = _math_text(raw.get("condition_tex") or raw.get("condition") or raw.get("domain"))
            payoff = _payoff_math_text(raw.get("payoff_tex") or raw.get("description"))
            pieces = raw.get("piece_summaries")
            if isinstance(pieces, list) and pieces:
                for piece_index, piece in enumerate(pieces, start=1):
                    if not isinstance(piece, Mapping):
                        continue
                    piece_condition = _math_text(piece.get("condition_tex") or piece.get("condition") or piece.get("domain"))
                    piece_payoff = _payoff_math_text(piece.get("payoff_tex") or piece.get("payoff") or piece.get("description"))
                    if piece_condition or piece_payoff:
                        scenarios.append({
                            "title": f"{path_title}·情形{piece_index}",
                            "rule": "；".join(filter(None, (f"条件：{piece_condition}" if piece_condition else "", f"收益：{piece_payoff}" if piece_payoff else ""))),
                        })
            elif condition or payoff:
                scenarios.append({"title": path_title, "rule": "；".join(filter(None, (f"条件：{condition}" if condition else "", f"收益：{payoff}" if payoff else "")))})
    value["scenarios"] = scenarios[:5]
    value["artifacts"] = _artifact_rows(module)
    return value


def _pricing_content(module: Mapping[str, Any]) -> dict[str, Any]:
    status = str(module.get("status", "not_run"))
    value: dict[str, Any] = {"status": status, "note": _text(module.get("note"))}
    if status != "ready":
        return value
    result = module.get("result") if isinstance(module.get("result"), Mapping) else {}
    pricing = result.get("pricing") if isinstance(result.get("pricing"), Mapping) else result
    value["method"] = _text(pricing.get("method"))
    market_snapshot = pricing.get("market_snapshot") if isinstance(pricing.get("market_snapshot"), Mapping) else {}
    value["valuation_date"] = _text(pricing.get("valuation_date") or market_snapshot.get("valuation_date"))
    metrics: list[dict[str, Any]] = []
    for key, label in (("pv_amount", "现值"), ("pv_percent", "现值占比"), ("pv_points_100", "百点现值"), ("pv", "估值"), ("price", "估值"), ("standard_error", "标准误")):
        if pricing.get(key) is not None:
            unit = _text(pricing.get("currency")) if key in {"pv_amount", "pv", "price", "standard_error"} else "" if key == "pv_percent" else "点"
            metrics.append(_metric(label, pricing[key], unit, value_format="percent" if key == "pv_percent" else "number"))
    value["metrics"] = metrics
    value["greeks"] = _public_greek_rows(pricing.get("greeks", {}))
    value["assumptions"] = list(pricing.get("assumptions", [])) if isinstance(pricing.get("assumptions"), list) else []
    risk_curves = pricing.get("risk_curves", result.get("risk_curves", []))
    risk_surfaces = pricing.get("risk_surfaces", result.get("risk_surfaces", []))
    value["charts"] = public_chart_specs([
        *_pricing_curve_charts(risk_curves),
        *_pricing_surface_charts(risk_surfaces),
        *(pricing.get("charts", []) if isinstance(pricing.get("charts"), list) else []),
    ])
    scenario_rows = []
    risk_scenarios = pricing.get("risk_scenarios", result.get("risk_scenarios", []))
    for index, raw in enumerate(risk_scenarios if isinstance(risk_scenarios, list) else [], start=1):
        item = raw if isinstance(raw, Mapping) else {}
        if item.get("pv") is None:
            continue
        shift = item.get("spot_shift")
        days = item.get("remaining_days")
        scenario_rows.append({
            "scenario": _text(item.get("name")) or f"情景{index}",
            "spot": item.get("spot"), "time": days, "pv": item.get("pv"),
            "spot_format": "number", "time_format": "number", "pv_format": "number",
        })
    value["scenario_rows"] = scenario_rows
    value["artifacts"] = _artifact_rows(module)
    return value


def _economic_convention_status(backtest: Mapping[str, Any]) -> str:
    """Confirm the frozen Backtester convention before making public claims.

    Reporter never infers a client-level return from a contract cashflow.  The
    formal Backtester declares this convention in its result JSON; older or
    incomplete JSON is retained with a deliberately narrower disclosure.
    """

    convention = backtest.get("economic_convention")
    if not isinstance(convention, Mapping):
        return "absent"
    if (
        convention.get("pnl_basis") == "contract_cashflow_before_external_costs"
        and convention.get("external_costs_modelled") is False
        and convention.get("client_net_pnl_status") == "not_modelled"
        and convention.get("win_rate_numerator") == "contract_cashflow_pnl_gt_zero"
        and convention.get("win_rate_denominator") == "valid_trade_count"
    ):
        return "formal"
    return "inconsistent"


def _return_note(config: Mapping[str, Any], *, formal_economic_convention: bool) -> str:
    denominator = str(config.get("return_denominator") or "").strip().lower()
    basis = {
        "notional": "按名义本金计算",
        "margin": "按保证金计算",
        "premium": "按权利金计算",
    }.get(denominator, "收益率分母未提供")
    if formal_economic_convention:
        return f"{basis}；合约毛收益率（合同条款现金流）口径，未扣除交易费、资金成本及税费；客户净损益不可得"
    return f"{basis}；合同条款现金流口径，未扣除交易费、资金成本及税费，不等同客户净收益"


def _backtest_metric_note(
    key: str,
    *,
    currency: str,
    config: Mapping[str, Any],
    sample_count: int | None = None,
    formal_economic_convention: bool = False,
) -> str:
    if key == "sample_count":
        return "个有效入场样本"
    if key in {"skipped_count", "return_not_applicable_count"} or key.endswith("_count"):
        return "个入场样本"
    if key == "win_rate":
        denominator = f"分母为{sample_count}个有效入场样本" if sample_count is not None else "分母为回测结果所列有效入场样本"
        if formal_economic_convention:
            return f"合约毛损益大于0的历史样本占比，{denominator}；客户净损益不可得，不代表未来获利概率"
        return f"合同条款现金流损益大于0的历史样本占比，{denominator}；不等同客户净收益，不代表未来获利概率"
    if key.endswith("_rate"):
        return "占有效入场样本"
    if key.endswith("_return"):
        return _return_note(config, formal_economic_convention=formal_economic_convention)
    if "pnl" in key or key == "max_loss":
        if formal_economic_convention:
            return f"{currency}/份合同；合约毛损益（合同条款现金流）口径，未扣除交易费、资金成本及税费；客户净损益不可得"
        return f"{currency}/份合同；合同条款现金流口径，未扣除交易费、资金成本及税费"
    if key.endswith("_days"):
        return "自然日"
    return "口径未提供"


def _backtest_content(module: Mapping[str, Any], contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
    status = str(module.get("status", "not_run"))
    value: dict[str, Any] = {"status": status, "note": _text(module.get("note"))}
    if status != "ready":
        return value
    result = module.get("result") if isinstance(module.get("result"), Mapping) else {}
    backtest = result.get("backtest") if isinstance(result.get("backtest"), Mapping) else result
    market = backtest.get("market_data") if isinstance(backtest.get("market_data"), Mapping) else {}
    coverage = backtest.get("data_coverage") if isinstance(backtest.get("data_coverage"), Mapping) else {}
    by_asset = coverage.get("by_asset") if isinstance(coverage.get("by_asset"), Mapping) else {}
    config = backtest.get("backtest_config") if isinstance(backtest.get("backtest_config"), Mapping) else {}
    economic_convention_status = _economic_convention_status(backtest)
    formal_economic_convention = economic_convention_status == "formal"
    currency = _currency_text(contract)
    starts = [_text(item.get("start_date")) for item in by_asset.values() if isinstance(item, Mapping) and item.get("start_date")]
    ends = [_text(item.get("end_date")) for item in by_asset.values() if isinstance(item, Mapping) and item.get("end_date")]
    start = _text(market.get("date_start")) or (min(starts) if starts else "")
    end = _text(market.get("date_end")) or (max(ends) if ends else "")
    value["window"] = "至".join(item for item in (start, end) if item)
    value["entry_rule"] = _text(config.get("entry_rule"))
    common = backtest.get("common_metrics") if isinstance(backtest.get("common_metrics"), Mapping) else backtest
    sample_count = common.get("sample_count")
    if not isinstance(sample_count, int) or isinstance(sample_count, bool):
        sample_count = None
    core = (
        ("sample_count", "样本数", "number"),
        ("win_rate", "胜率", "percent"),
        ("average_return", "平均合同现金流收益率", "percent"),
        ("minimum_pnl", "最差损益", "number"),
    )
    value["metrics"] = [
        _metric(
            label,
            common.get(key, common.get("max_loss") if key == "minimum_pnl" else None),
            _backtest_metric_note(
                key, currency=currency, config=config, sample_count=sample_count,
                formal_economic_convention=formal_economic_convention,
            ),
            value_format=value_format,
        )
        for key, label, value_format in core
    ]

    detail_tables: list[dict[str, Any]] = []
    overview_rows = []
    for key in ("sample_count", "skipped_count", "win_rate", "average_pnl", "median_pnl", "minimum_pnl", "maximum_pnl", "average_return", "median_return", "minimum_return", "maximum_return", "return_not_applicable_count"):
        if common.get(key) is not None:
            overview_rows.append({
                "label": _BACKTEST_LABELS[key],
                "value": common[key],
                "value_format": _public_value_format(key),
                "note": _backtest_metric_note(
                    key, currency=currency, config=config, sample_count=sample_count,
                    formal_economic_convention=formal_economic_convention,
                ),
            })
    if overview_rows:
        detail_tables.append({"title": "公共回测统计", "columns": [("label", "指标"), ("value", "数值")], "rows": overview_rows})

    coverage_limitations: list[Any] = []
    if economic_convention_status == "inconsistent":
        coverage_limitations.append("本次回测的经济口径声明不一致，未将历史损益解释为合约毛损益或客户净损益。")
    branch = backtest.get("branch_coverage") if isinstance(backtest.get("branch_coverage"), Mapping) else {}
    declared = branch.get("declared_pair_count")
    observed = branch.get("observed_pair_count")
    uncovered = branch.get("uncovered_pair_count")
    coverage_status = _text(branch.get("status")).lower()
    counts_are_valid = (
        all(isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in (declared, observed, uncovered))
        and observed + uncovered == declared
        and ((coverage_status == "complete" and uncovered == 0) or (coverage_status == "partial" and uncovered > 0))
    )
    if counts_are_valid:
        detail_tables.append({
            "title": "分支覆盖",
            "columns": [("label", "指标"), ("value", "数量")],
            "rows": [
                {
                    "label": "已观察分支",
                    "value": observed,
                    "value_format": "number",
                    "note": f"共{declared}个合同声明分支；仅统计回测样本实际落入的分支",
                },
                {
                    "label": "未观察分支",
                    "value": uncovered,
                    "value_format": "number",
                    "note": "不代表未观察分支已实际发生，也不代表其经济结果已验证",
                },
            ],
        })
        if isinstance(branch.get("limitations"), list):
            coverage_limitations.extend(branch["limitations"])
    elif branch:
        coverage_limitations.append("本次回测的分支覆盖统计不一致，未作为覆盖结论展示。")

    event_rows = []
    event_summary = backtest.get("event_summary") if isinstance(backtest.get("event_summary"), Mapping) else {}
    for key, raw in event_summary.items():
        item = raw if isinstance(raw, Mapping) else {}
        label = _text(item.get("label")) or _EVENT_LABELS.get(str(key))
        if not label:
            continue
        event_rows.append({
            "event": label,
            "sample_count": item.get("sample_count", item.get("count")),
            "trigger_count": item.get("trigger_count", item.get("count")),
            "trigger_rate": item.get("trigger_rate", item.get("rate")), "trigger_rate_format": "percent",
            "average_days": item.get("average_days"), "median_days": item.get("median_days"),
        })
    if event_rows:
        detail_tables.append({
            "title": "路径事件统计",
            "columns": [("event", "事件"), ("sample_count", "样本数"), ("trigger_count", "触发次数"), ("trigger_rate", "触发比例"), ("average_days", "平均触发天数"), ("median_days", "中位触发天数")],
            "rows": event_rows,
        })

    monitor_rows = []
    monitor_summary = backtest.get("monitor_summary") if isinstance(backtest.get("monitor_summary"), Mapping) else {}
    for key, raw in monitor_summary.items():
        item = raw if isinstance(raw, Mapping) else {}
        label = _text(item.get("label")) or _MONITOR_LABELS.get(str(key))
        if not label:
            continue
        monitor_rows.append({
            "monitor": label, "sample_count": item.get("sample_count"), "average": item.get("average"),
            "median": item.get("median"), "minimum": item.get("minimum"), "maximum": item.get("maximum"),
            "true_rate": item.get("true_rate"), "true_rate_format": "percent",
        })
    if monitor_rows:
        detail_tables.append({
            "title": "监控统计",
            "columns": [("monitor", "观察项"), ("sample_count", "样本数"), ("average", "平均值"), ("median", "中位数"), ("minimum", "最小值"), ("maximum", "最大值"), ("true_rate", "为真比例")],
            "rows": monitor_rows,
        })

    outcome_rows = []
    for raw in backtest.get("outcome_summary", []) if isinstance(backtest.get("outcome_summary"), list) else []:
        item = raw if isinstance(raw, Mapping) else {}
        if not _text(item.get("label")):
            continue
        outcome_rows.append({
            "label": _text(item.get("label")), "count": item.get("count"), "rate": item.get("rate"), "rate_format": "percent",
            "average_pnl": item.get("average_pnl"), "note": _text(item.get("domain") or item.get("note")),
        })
    if outcome_rows:
        detail_tables.append({
            "title": "路径结果统计",
            "columns": [("label", "路径结果"), ("count", "样本数"), ("rate", "占比"), ("average_pnl", "平均损益"), ("note", "条件")],
            "rows": outcome_rows,
        })

    annual_rows = []
    for raw in backtest.get("annual_summary", []) if isinstance(backtest.get("annual_summary"), list) else []:
        item = raw if isinstance(raw, Mapping) else {}
        if item.get("year") is not None:
            annual_rows.append({
                "year": item.get("year"), "sample_count": item.get("sample_count"), "win_rate": item.get("win_rate"), "win_rate_format": "percent",
                "average_pnl": item.get("average_pnl"), "average_return": item.get("average_return"), "average_return_format": "percent",
            })
    if annual_rows:
        detail_tables.append({
            "title": "年度统计",
            "columns": [("year", "年份"), ("sample_count", "样本数"), ("win_rate", "胜率"), ("average_pnl", "平均损益"), ("average_return", "平均收益")],
            "rows": annual_rows,
        })

    underlying_rows = []
    raw_performance = backtest.get("underlying_performance")
    if isinstance(raw_performance, Mapping):
        by_underlying = raw_performance.get("by_asset") if isinstance(raw_performance.get("by_asset"), Mapping) else raw_performance
        performance_rows = [
            {"underlying": asset, **dict(raw)}
            for asset, raw in by_underlying.items()
            if isinstance(raw, Mapping)
        ] if isinstance(by_underlying, Mapping) else []
    elif isinstance(raw_performance, list):
        performance_rows = [dict(item) for item in raw_performance if isinstance(item, Mapping)]
    else:
        performance_rows = []
    for item in performance_rows:
        underlying = item.get("underlying", item.get("asset"))
        if underlying is not None:
            underlying_rows.append({
                "underlying": _text(underlying), "average": item.get("average", item.get("average_return")), "average_format": "percent",
                "median": item.get("median", item.get("median_return")), "median_format": "percent", "minimum": item.get("minimum", item.get("minimum_return")), "minimum_format": "percent",
                "maximum": item.get("maximum", item.get("maximum_return")), "maximum_format": "percent",
            })
    if underlying_rows:
        detail_tables.append({
            "title": "标的表现",
            "columns": [("underlying", "标的"), ("average", "平均收益"), ("median", "中位收益"), ("minimum", "最小收益"), ("maximum", "最大收益")],
            "rows": underlying_rows,
        })

    specialized = backtest.get("specialized_metrics") if isinstance(backtest.get("specialized_metrics"), Mapping) else {}
    specialized_rows = _specialized_rows(specialized)
    if specialized_rows:
        detail_tables.append({"title": "产品专属统计", "columns": [("label", "指标"), ("value", "数值")], "rows": specialized_rows})
    value["detail_tables"] = detail_tables
    value["event_statistics"] = []
    profile_id = _text((backtest.get("metric_profile") or {}).get("profile_id")) if isinstance(backtest.get("metric_profile"), Mapping) else _text(specialized.get("profile_id"))
    value["card_metrics"] = _card_metrics(profile_id, specialized, currency=currency)
    raw_limitations: list[Any] = []
    if isinstance(module.get("limitations"), list):
        raw_limitations.extend(module["limitations"])
    if isinstance(backtest.get("limitations"), list):
        raw_limitations.extend(backtest["limitations"])
    raw_limitations.extend(coverage_limitations)
    value["limitations"] = list(dict.fromkeys(filter(None, (_public_limitation(item) for item in raw_limitations))))
    value["charts"] = public_chart_specs([
        *_backtest_standard_charts(common, outcome_rows, annual_rows, event_rows),
        *(backtest.get("charts", []) if isinstance(backtest.get("charts"), list) else []),
    ])
    value["artifacts"] = _artifact_rows(module)
    return value


def _parameter_rows(contract: Mapping[str, Any] | None, *, names: set[str] | None = None, exclude: set[str] | None = None) -> list[dict[str, Any]]:
    if not isinstance(contract, Mapping):
        return []
    terms = contract.get("terms") if isinstance(contract.get("terms"), Mapping) else {}
    term_sources = contract.get("term_sources") if isinstance(contract.get("term_sources"), Mapping) else {}
    source_map = {"default": "template_default", "override": "user_override", "market": "market_fixing", "schedule": "schedule_derived"}
    rows = []
    for symbol, value in terms.items():
        if names is not None and symbol not in names:
            continue
        if exclude is not None and symbol in exclude:
            continue
        source = str(term_sources.get(symbol, "user_selection")).lower()
        rows.append({
            "cn": _TERM_LABELS.get(str(symbol), str(symbol)), "en": str(symbol), "symbol": _TERM_SYMBOLS.get(str(symbol), ""),
            "value": deepcopy(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else _text(value),
            "source": source_map.get(source, source if source in {"template_default", "user_override", "user_selection", "market_fixing", "schedule_derived"} else "user_selection"),
        })
    return rows


def _parameter_groups(contract: Mapping[str, Any] | None) -> dict[str, list[dict[str, str]]]:
    module_only = set().union(*_MODULE_ONLY_TERMS.values())
    result = {"common_input": _parameter_rows(contract, exclude=module_only)}
    result.update({name: _parameter_rows(contract, names=terms) for name, terms in _MODULE_ONLY_TERMS.items()})
    return result


def normalize_parameter_groups(value: Any) -> dict[str, list[dict[str, Any]]]:
    """Normalize legacy frozen rows for public display without changing facts.

    Older ReportUnits may already contain rows whose Chinese label is the raw
    contract key and whose symbol is blank.  Reporter keeps the frozen unit
    untouched and applies the same canonical label/symbol map used when new
    units are built.
    """

    if not isinstance(value, Mapping):
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    for group, raw_rows in value.items():
        rows: list[dict[str, Any]] = []
        for raw in raw_rows if isinstance(raw_rows, list) else []:
            if not isinstance(raw, Mapping):
                continue
            row = deepcopy(dict(raw))
            key = str(row.get("en") or row.get("symbol") or row.get("cn") or "").strip()
            if key:
                row["cn"] = _TERM_LABELS.get(key, str(row.get("cn") or key))
                row["symbol"] = _TERM_SYMBOLS.get(key, str(row.get("symbol") or ""))
            rows.append(row)
        result[str(group)] = rows
    return result


def _module_summary(name: str, module: Mapping[str, Any]) -> dict[str, Any]:
    record = module.get("run") if isinstance(module.get("run"), Mapping) else {}
    result = {
        "module": name,
        "status": str(module.get("status", "not_run")),
        "reason": _text(module.get("note")),
        "run_ref": {
            "module": getattr(module.get("ref"), "module", None),
            "tenant_id": getattr(module.get("ref"), "tenant_id", None),
            "task_id": getattr(module.get("ref"), "task_id", None),
            "run_id": getattr(module.get("ref"), "run_id", None),
            "expected_semantic_result_hash": getattr(module.get("ref"), "expected_semantic_result_hash", None),
            "expected_artifact_manifest_hash": getattr(module.get("ref"), "expected_artifact_manifest_hash", None),
        } if module.get("ref") is not None else None,
        "run": {key: value for key, value in record.items() if key != "_run_dir"},
        "artifacts": _artifact_rows(module),
        "limitations": list(module.get("limitations", [])) if isinstance(module.get("limitations"), list) else [],
    }
    return result


def _contract_report_unit(request: ReportRequest, candidate_evidence: Mapping[str, Any], recommender: Mapping[str, Any]) -> dict[str, Any]:
    candidate = deepcopy(dict(candidate_evidence["candidate"]))
    modules = candidate_evidence["modules"]
    contract = deepcopy(candidate_evidence.get("contract")) if isinstance(candidate_evidence.get("contract"), Mapping) else None
    module_content = {
        "payoff": _payoff_content(modules["payoff"]),
        "pricing": _pricing_content(modules["pricing"]),
        "backtest": _backtest_content(modules["backtest"], contract),
    }
    parameter_groups = _parameter_groups(contract)
    if not candidate.get("key_terms"):
        candidate["key_terms"] = [
            {"label": row["cn"], "value": row["value"], "note": "已确认合同条款"}
            for row in parameter_groups["common_input"][:7]
        ]
    source_refs = {
        "catalog_version_ref": deepcopy(dict(request.source_refs.get("catalog_version_ref", {}))),
        "product_version_ref": deepcopy(dict((request.source_refs.get("product_version_refs", {}) or {}).get(candidate["candidate_id"], {}))),
        "recommendation_source": {key: value for key, value in recommender.items() if key in {"source", "run_id", "semantic_result_hash", "status"}},
    }
    selected_compute = [name for name in MODULE_TO_RUN if name in request.selected_modules]
    selected_statuses = [str(modules[name].get("status")) for name in selected_compute]
    if recommender.get("status") != "ready" or any(status == "legacy_unverified" for status in selected_statuses):
        evidence_status = "unverified"
    elif selected_compute and all(status == "ready" for status in selected_statuses):
        evidence_status = "verified"
    else:
        evidence_status = "partial"
    unit = {
        "schema": SCHEMA_REPORT_UNIT,
        "unit_type": "ContractReportUnit",
        "tenant_id": request.tenant_id,
        "task_id": request.task_id,
        "analysis_case_id": request.analysis_case_id,
        "subject": {"candidate_id": candidate["candidate_id"], "product_id": candidate.get("product_id"), "product_name": candidate.get("product_name"), "underlyings": candidate.get("underlyings", [])},
        "candidate": candidate,
        "contract": contract,
        "evidence_status": evidence_status,
        "selected_modules": list(request.selected_modules),
        "modules": {name: _module_summary(name, modules[name]) for name in MODULE_TO_RUN},
        "content": {
            "recommendation": candidate,
            "contract_highlights": _contract_highlights(contract),
            **module_content,
            "parameters": parameter_groups,
            "audit": {
                "recommender": {key: value for key, value in recommender.items() if key != "legacy_errors"},
                "source_refs": source_refs,
                "limitations": [
                    *(list(recommender.get("legacy_errors", {}).values()) if isinstance(recommender.get("legacy_errors"), Mapping) else []),
                    *([] if selected_compute else ["本次未包含收益、估值或历史回测的已验证计算结果，不能作为完整量化结论。"]),
                ],
            },
            "risk": {"items": candidate.get("main_risks", []), "disclaimer": DISCLAIMER},
        },
    }
    unit["semantic_fact_hash"] = stable_hash(unit)
    return unit


def _product_unit(request: ReportRequest, candidate_evidence: Mapping[str, Any]) -> dict[str, Any]:
    candidate = deepcopy(dict(candidate_evidence["candidate"]))
    unit = {
        "schema": SCHEMA_REPORT_UNIT,
        "unit_type": "ProductKnowledgeUnit",
        "tenant_id": request.tenant_id,
        "task_id": request.task_id,
        "analysis_case_id": request.analysis_case_id,
        "subject": {
            "product_id": candidate["product_id"], "product_name": candidate["product_name"],
            "product_version": candidate["product_version"], "underlyings": candidate.get("underlyings", []),
        },
        "candidate": candidate,
        "contract": None,
        "evidence_status": "partial",
        "selected_modules": list(request.selected_modules),
        "modules": {name: {"module": name, "status": "not_requested", "reason": "产品知识单元不读取计算ModuleRun。", "run_ref": None, "run": {}, "artifacts": []} for name in MODULE_TO_RUN},
        "content": {
            "recommendation": candidate,
            "payoff": {"status": "not_requested", "note": "产品知识单元不展示本次Payoff图。"},
            "pricing": {"status": "not_requested", "note": "产品知识单元不展示本次估值。"},
            "backtest": {"status": "not_requested", "note": "产品知识单元不展示本次回测。"},
            "parameters": {"payoff_input": [], "pricing_input": [], "backtest_input": []},
            "audit": {
                "recommender": {"source": "subject_ref.product_ref"},
                "source_refs": {
                    "catalog_version_ref": deepcopy(dict(request.source_refs.get("catalog_version_ref", {}))),
                    "product_version_ref": deepcopy(dict((request.source_refs.get("product_version_refs", {}) or {}).get(candidate["product_id"], {}))),
                },
                "limitations": ["本交付仅含产品知识与风险说明，未包含本次收益、估值或历史回测计算结果。"],
            },
            "risk": {"items": candidate.get("main_risks", []), "disclaimer": DISCLAIMER},
        },
    }
    unit["semantic_fact_hash"] = stable_hash(unit)
    return unit


def build_report_units(request: ReportRequest, evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """按一合同一单位原则建立冻结单位。"""

    candidate_evidence = evidence.get("candidate_evidence")
    if not isinstance(candidate_evidence, Mapping):
        raise ReporterError("证据解析结果缺少candidate_evidence")
    if request.subject_type == "product":
        return [_product_unit(request, next(iter(candidate_evidence.values())))]
    recommender = evidence.get("recommender") if isinstance(evidence.get("recommender"), Mapping) else {}
    return [_contract_report_unit(request, candidate_evidence[candidate_id], recommender) for candidate_id in request.candidate_ids]


def build_report_document(request: ReportRequest, units: list[Mapping[str, Any]]) -> dict[str, Any]:
    """把一或多个冻结单位封装为唯一ReportUnit事实来源。"""

    if not units:
        raise ReporterError("没有可冻结的ReportUnit")
    if request.delivery_mode == "single":
        document = deepcopy(dict(units[0]))
    else:
        document = {
            "schema": "optionhelper.report-bundle/v2",
            "unit_type": "ReportBundle",
            "tenant_id": request.tenant_id,
            "task_id": request.task_id,
            "analysis_case_id": request.analysis_case_id,
            "report_units": [deepcopy(dict(unit)) for unit in units],
        }
        document["semantic_fact_hash"] = stable_hash(document)
    return document


__all__ = ["DISCLAIMER", "build_report_document", "build_report_units", "normalize_parameter_groups", "public_chart_specs"]
