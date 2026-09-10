"""将已验证证据冻结为ContractReportUnit。

本文件只组织上游已经给出的事实，不估值、不回测、不计算Payoff，也不重新定义
产品依赖。对组合交付，仍然是一合同一ContractReportUnit，外层只保存单位顺序。
"""

from __future__ import annotations

from copy import deepcopy
from html import escape
import math
import re
from typing import Any, Mapping
from xml.etree import ElementTree

from runtime.knowledger.registry_loader import load_term_catalog
from runtime.contracts.term_presentation import INTERNAL_SCALE, build_term_fields

from .models import (
    MODULE_TO_RUN,
    ReporterError,
    ReportRequest,
    SCHEMA_REPORT_BUNDLE,
    SCHEMA_REPORT_UNIT,
    stable_hash,
)
from .artifact_validator import is_public_module_artifact


DISCLAIMER = "风险提示：以上内容仅供结构说明和情景测算，不构成投资建议、收益承诺或正式报价。期权及结构化产品具有较高风险，可能发生部分或全部本金损失；实际结果以合同条款、市场报价和交易确认为准。"
DEFAULT_RISK_ITEMS = ["结构收益取决于合同条款与市场路径，可能发生部分或全部本金损失。"]

_TERM_VALUE_TEXT = {
    "analytical": "Analytical", "monte_carlo": "Monte Carlo", "cash": "现金结算",
    "european": "仅到期日可行权", "american": "存续期内可行权",
    "bermudan": "约定观察日可行权", "close": "收盘价", "monthly": "每月",
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
    "percentage": "百分比",
}
_MODULE_ONLY_TERMS = {
    "payoff_input": {"monitor"},
    "pricing_input": {"pricing_methods"},
    "backtest_input": set(),
}
# Compiler and rendering metadata are not contract clauses. Real derived
# terms still use the shared presentation protocol and remain visible.
_PUBLIC_EXCLUDED_TERM_SYMBOLS = {
    "N", "notional", "monitor", "constraints", "derived_terms", "pricing_methods", "payoff_figure_basis",
}


_NUMBER_TEXT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _safe_number(value: Any) -> float | None:
    """Return a finite numeric fact, never coercing bools or strings."""

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _decimal_text(value: int | float | str) -> str:
    """Render public numerical values without scientific notation or noise."""

    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return ""
    if not math.isfinite(numeric):
        return ""
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
    if _safe_number(value) is not None:
        return _decimal_text(value)
    if isinstance(value, (list, tuple)):
        return "、".join(filter(None, (_text(item) for item in value)))
    if isinstance(value, Mapping):
        # 映射对象必须经过字段白名单投影，不能直接以内部键和值拼成公开文字。
        return ""
    if isinstance(value, str):
        known = _TERM_VALUE_TEXT.get(value.casefold())
        if known:
            return known
        return _decimal_text(value) if _NUMBER_TEXT.fullmatch(value.strip()) else value
    return str(value)


def _risk_items(candidate: Mapping[str, Any]) -> list[str]:
    """Retain declared risks, with one factual baseline when none is supplied."""

    items = candidate.get("main_risks")
    public = [_text(item) for item in items if isinstance(item, str) and _text(item)] if isinstance(items, list) else []
    return list(dict.fromkeys(public)) or list(DEFAULT_RISK_ITEMS)


_PUBLIC_CHART_FIELDS = (
    "id", "title", "type", "x", "y", "data", "series",
    "x_axis_name", "y_axis_name", "z_axis_name", "value_format",
    "source_note", "accessibility_summary",
)
_FORBIDDEN_PUBLIC_UNIT_TEXT = (
    "cny", "人民币", "名义本金", "amount", "pnl", "cashflow", "现金流",
    "每100份合同", "points_100", "点数",
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


def _safe_backtest_charts(backtest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expose only explicitly percentage-encoded backtest chart specifications."""

    verified: list[dict[str, Any]] = []
    for chart in public_chart_specs(backtest.get("charts")):
        if chart.get("value_format") != "percent":
            continue
        series = chart.get("series")
        if isinstance(series, list) and any(row.get("value_format") != "percent" for row in series if isinstance(row, Mapping)):
            continue
        visible = " ".join(
            str(chart.get(key, ""))
            for key in ("title", "x_axis_name", "y_axis_name", "z_axis_name", "source_note", "accessibility_summary")
        ).casefold()
        if any(term.casefold() in visible for term in _FORBIDDEN_PUBLIC_UNIT_TEXT):
            continue
        verified.append(chart)
    return verified


def _math_text(value: Any) -> str:
    """Preserve formula source notation for Designer's safe MathML renderer."""

    return str(value or "")


def _percent_text(value: Any) -> str:
    numeric = _safe_number(value)
    if numeric is None:
        return _text(value)
    return _decimal_text(numeric * 100) + "%"


def _metric(label: str, value: Any, note: str = "", *, value_format: str = "number") -> dict[str, Any]:
    """Project a frozen fact for Designer without pre-rounding it."""

    row: dict[str, Any] = {"label": label, "value": deepcopy(value), "note": _text(note)}
    if value_format != "number":
        row["value_format"] = value_format
    return row


_HIGHLIGHT_TERM_ORDER = (
    "T", "K", "K1", "K2", "K3", "K4", "Kp", "Kc", "Kd", "Ku", "Ksig",
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


def _tenor_display_parts(value: Any) -> tuple[str, str]:
    """Choose exact whole units for the reader; the frozen value stays in years."""

    numeric = _safe_number(value)
    if numeric is not None and numeric > 0:
        for scale, unit in ((1, "年"), (12, "个月"), (365, "天")):
            count = numeric * scale
            integer = round(count)
            if integer > 0 and math.isclose(count, integer, rel_tol=0, abs_tol=1e-9):
                return str(integer), unit
    return _text(value), "年"


def normalize_contract_highlights(value: Any, parameters: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Refresh a saved tenor label from its unrounded parameter fact."""

    rows = [deepcopy(dict(row)) for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []
    tenor = next((row.get("value") for group in parameters.values() for row in group
                  if isinstance(row, Mapping) and row.get("en") == "T"), None)
    if _safe_number(tenor) is not None:
        amount, unit = _tenor_display_parts(tenor)
        for row in rows:
            if row.get("label") == "期限":
                row.update(value=amount, note=unit)
    return rows


def _highlight_row(symbol: str, value: Any, contract: Mapping[str, Any], catalog: Mapping[str, Any]) -> dict[str, str]:
    definition = build_term_fields({symbol: value}, catalog)["contract_fields"][0]
    label = definition["label"]
    unit = str(definition.get("unit") or "").strip()
    if symbol == "T":
        amount, note = _tenor_display_parts(value)
        return {"label": "期限", "value": amount, "note": note}
    elif symbol == "exercise_style":
        label, note = "到期行权方式", "仅适用于持有人行权结构"
    elif unit == "premium_percent_s0_100":
        return {"label": label, "value": f"{_decimal_text(value)}%", "note": "S₀=100"}
    elif unit in {"rate", "volatility", "percentage"}:
        return {"label": label, "value": _percent_text(value), "note": "年化波动率" if unit == "volatility" else "比例"}
    elif unit == "price":
        note = "标的价格水平"
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
        priority = ("T", "H_KO", "H_KI", "c", *[symbol for symbol in _HIGHLIGHT_TERM_ORDER if symbol not in {"T", "H_KO", "H_KI", "c"}])
    rows = [
        _highlight_row(symbol, terms[symbol], contract, catalog)
        for symbol in priority
        if symbol in terms and symbol not in _PUBLIC_EXCLUDED_TERM_SYMBOLS
    ]
    schedules = []
    for symbol, prefix in (
        ("O_KO", "敲出"), ("O_KI", "敲入"), ("Oc", "派息"),
        ("Otouch", "触碰"), ("Orange", "区间"), ("Ovar", "方差"),
        ("Ohedge", "避险"), ("Oreset", "重置"),
    ):
        if symbol in terms:
            schedule = _term_display_value(terms[symbol])
            if schedule:
                schedules.append(f"{prefix}：{schedule}")
    if schedules:
        rows = [row for symbol, row in zip(
            [symbol for symbol in priority if symbol in terms and symbol not in _PUBLIC_EXCLUDED_TERM_SYMBOLS],
            rows,
        ) if symbol != "exercise_style"]
        observation = {"label": "观察频率", "value": "；".join(schedules), "note": "仅交易日"}
        return [*rows[:5], observation]
    return rows[:6]


_BACKTEST_LABELS = {
    "sample_count": "样本数", "valid_return_sample_count": "有效收益样本数",
    "positive_return_count": "正收益样本数", "zero_return_count": "持平样本数",
    "negative_return_count": "负收益样本数", "skipped_count": "跳过样本数",
    "positive_return_rate": "历史正收益样本占比",
    "average_contract_settlement_return": "平均合同结算收益率",
    "median_contract_settlement_return": "中位合同结算收益率",
    "minimum_contract_settlement_return": "最低合同结算收益率",
    "maximum_contract_settlement_return": "最高合同结算收益率",
    "historical_loss_sample_covered": "历史损失样本覆盖",
    "trigger_count": "触发次数",
    "trigger_rate": "触发比例", "average_days": "平均触发天数", "median_days": "中位触发天数",
    "true_count": "为真次数", "false_count": "为假次数", "true_rate": "为真比例",
}
_GREEK_ORDER = ("Delta", "Gamma", "Vega", "Theta", "Rho")
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
_EVENT_LABELS = {
    "tau_out": "敲出", "tau_out_1": "第一敲出", "tau_out_2": "第二敲出", "tau_in": "敲入",
    "tau_touch": "触碰", "tau_reset": "重置", "tau_hedge": "避险触发",
}
_MONITOR_LABELS = {
    "Q_acc": "累计数量", "n_coupon": "累计派息期数", "n_in": "区间内观察次数",
    "n_obs_actual": "实际观察次数", "sigma_realized": "实现波动率", "S_out": "触发时价格",
}


_SPECIALIZED_PROFILE_ROOTS = {
    "terminal_payoff": {"terminal_performance", "terminal_performance_sign", "terminal_segments"},
    "single_knock_out": {"events", "trigger_vs_untriggered"},
    "single_knock_in": {"events", "knock_in_outcomes"},
    "touch_binary": {"events", "touch_vs_untouched"},
    "airbag": {"events", "buffer_outcomes", "knock_in_outcomes"},
    "accumulator": {"events", "accumulated_quantity", "knock_out_vs_full_term", "contract_purchase_price", "quantity_multiplier"},
    "dual_knock_autocall": {"events", "three_outcome_summary", "conditional_summary"},
    "coupon_autocall": {"events", "coupon_observations", "coupon_payment"},
    "single_knock_out_autocall": {"events", "trigger_vs_untriggered"},
    "shark_fin": {"events", "trigger_vs_untriggered", "terminal_performance"},
    "variance_swap": {"realized_volatility", "realized_variance", "realized_volatility_vs_strike", "volatility_buckets"},
    "range_accrual": {"range_observations", "in_range_observation_ratio"},
}
# Exact paths, labels and units are the public contract. Nested distributions,
# conditional diagnostics and future fields stay in the frozen run for audit.
_SPECIALIZED_PUBLIC_METRICS = (
    (("terminal_performance", "average"), "平均到期表现", "percent", "百分比；标的到期表现，不是合同结算收益率"),
    (("terminal_performance_sign", "positive_count"), "到期正表现样本数", "number", "个样本"),
    (("terminal_performance_sign", "flat_count"), "到期持平样本数", "number", "个样本"),
    (("terminal_performance_sign", "negative_count"), "到期负表现样本数", "number", "个样本"),
    (("terminal_performance_sign", "positive_rate"), "到期正表现占比", "percent", "百分比；占具有到期表现记录的样本"),
    (("terminal_performance_sign", "negative_rate"), "到期负表现占比", "percent", "百分比；占具有到期表现记录的样本"),
    (("buffer_outcomes", "non_negative_terminal_rate"), "非负到期表现占比", "percent", "百分比；占具有到期表现记录的样本"),
    (("buffer_outcomes", "negative_terminal_rate"), "负到期表现占比", "percent", "百分比；占具有到期表现记录的样本"),
    (("accumulated_quantity", "Q_acc", "average"), "平均累计数量", "number", "份"),
    (("knock_out_vs_full_term", "knock_out_count"), "敲出样本数", "number", "个样本"),
    (("knock_out_vs_full_term", "full_term_count"), "完整期限样本数", "number", "个样本"),
    (("knock_out_vs_full_term", "knock_out_rate"), "敲出样本占比", "percent", "百分比；占有效入场样本"),
    (("quantity_multiplier",), "数量倍数", "number", "倍"),
    (("coupon_payment", "paid_observation_count", "average"), "平均派息观察期数", "number", "期"),
    (("coupon_payment", "unpaid_observation_count", "average"), "平均未派息观察期数", "number", "期"),
    (("coupon_payment", "observation_hit_rate", "average"), "平均票息观察命中比例", "percent", "百分比；已派息期数占计划观察期数的比例均值"),
    (("realized_volatility", "sigma_realized", "average"), "平均实现波动率", "number", "百分点"),
    (("realized_volatility_vs_strike", "strike_volatility"), "执行波动率", "number", "百分点"),
    (("realized_volatility_vs_strike", "spread", "average"), "平均实现波动率与执行波动率之差", "number", "百分点"),
    (("volatility_buckets", "below_strike_count"), "低于执行波动率样本数", "number", "个样本"),
    (("volatility_buckets", "below_strike_rate"), "低于执行波动率样本占比", "percent", "百分比；占具有实现波动率记录的样本"),
    (("range_observations", "n_in", "average"), "平均区间内观察次数", "number", "次"),
    (("range_observations", "n_obs_actual", "average"), "平均实际观察次数", "number", "次"),
    (("in_range_observation_ratio", "average"), "区间内观察比例", "percent", "百分比；区间内观察次数占实际观察次数的比例均值"),
)


def _specialized_value_format(path: tuple[str, ...]) -> str:
    # Leaf semantics take precedence over the enclosing metric family: a
    # count inside terminal_performance_sign is still a count, not a return.
    leaf = path[-1] if path else ""
    if leaf == "count" or leaf.endswith("_count"):
        return "number"
    if leaf == "rate" or leaf.endswith("_rate"):
        return "percent"
    joined = ".".join(path)
    if any(token in joined for token in ("_rate", "gross_return", "terminal_performance", "variance", "ratio")):
        return "percent"
    return "number"


def _specialized_metric_rows(backtest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read only explicitly named business metrics, never flatten diagnostics."""

    specialized = backtest.get("specialized_metrics")
    if not isinstance(specialized, Mapping):
        return []
    profile_id = str(specialized.get("profile_id", ""))
    allowed_roots = _SPECIALIZED_PROFILE_ROOTS.get(profile_id)
    if not allowed_roots:
        return []
    specs = list(_SPECIALIZED_PUBLIC_METRICS)
    event_specs = [
        (("events", event, key), label + suffix, value_format, unit)
        for event in ("tau_out", "tau_in", "tau_out_1", "tau_out_2", "tau_touch", "tau_reset", "tau_hedge")
        for label in (_EVENT_LABELS[event],)
        for key, suffix, value_format, unit in (
            ("trigger_count", "样本数", "number", "个样本"),
            ("trigger_rate", "样本占比", "percent", "百分比；占有效入场样本"),
        )
    ]
    rows: list[dict[str, Any]] = []
    labels: set[str] = set()
    for path, label, value_format, note in [*event_specs, *specs]:
        if path[0] not in allowed_roots or label in labels:
            continue
        value: Any = specialized
        for key in path:
            value = value.get(key) if isinstance(value, Mapping) else None
        if _safe_number(value) is None:
            continue
        rows.append(_metric(label, value, note, value_format=value_format))
        labels.add(label)
    return rows


def _public_value_format(key: str) -> str:
    return "percent" if key.endswith(("_rate", "_return", "_percent")) else "number"


def _public_percent_unit(value: Any) -> str:
    unit = str(value or "").strip().casefold()
    return unit if unit.startswith(("pv_percent", "variance_percent")) else ""


_PERCENT_GREEK_UNITS = {
    "": "百分比",
    "_per_spot": "百分比/标的价格点",
    "_per_spot_squared": "百分比/标的价格点²",
    "_per_calendar_day": "百分比/日",
    "_per_1pct_volatility": "百分比/波动率变化1个百分点",
    "_per_1pct_volatility_squared": "百分比/波动率变化1个百分点²",
    "_per_1pct_rate": "百分比/利率变化1个百分点",
    "_per_spot_per_1pct_volatility": "百分比/标的价格点/波动率变化1个百分点",
}
PUBLIC_NUMERIC_UNITS = frozenset({"百分比敏感度", *_PERCENT_GREEK_UNITS.values()})


def _percent_greek_unit(value: Any) -> str:
    unit = _public_percent_unit(value)
    if not unit:
        return ""
    if unit.startswith("variance_percent"):
        return "百分比敏感度"
    return _PERCENT_GREEK_UNITS.get(unit.removeprefix("pv_percent"), "百分比敏感度")


def _public_greek_rows(greeks: Any) -> list[dict[str, Any]]:
    """Project only verified percentage Greeks to the public delivery."""

    values = greeks if isinstance(greeks, Mapping) else {}
    rows: list[dict[str, Any]] = []
    for label in _GREEK_ORDER:
        raw = values.get(label, values.get(label.casefold())) if isinstance(values, Mapping) else None
        if not isinstance(raw, Mapping):
            continue
        percent = _safe_number(raw.get("value"))
        named_percent = _safe_number(raw.get("pv_percent_value"))
        percent_unit = _percent_greek_unit(raw.get("unit") or raw.get("pv_percent_unit"))
        if percent is None or not percent_unit:
            continue
        if named_percent is not None and abs(percent - named_percent) > 1e-12:
            continue
        rows.append({
            "label": label,
            "value": deepcopy(percent),
            "value_format": "percent",
            "unit": percent_unit,
            "status": _text(raw.get("status")) or "available",
        })
    return rows


def _safe_pricing_charts(pricing: Mapping[str, Any], result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Keep only upstream public percentage risk sensitivities."""

    charts: list[dict[str, Any]] = []
    curves = pricing.get("risk_curves", result.get("risk_curves", []))
    for raw in curves if isinstance(curves, list) else []:
        item = raw if isinstance(raw, Mapping) else {}
        axis = item.get("y_axis") if isinstance(item.get("y_axis"), Mapping) else {}
        unit = _public_percent_unit(axis.get("unit"))
        points = item.get("points") if isinstance(item.get("points"), list) else []
        values: list[tuple[float, float]] = []
        for point in points:
            if not isinstance(point, Mapping):
                values = []
                break
            x_value, y_value = _safe_number(point.get("x")), _safe_number(point.get("y"))
            if x_value is None or y_value is None:
                values = []
                break
            values.append((x_value, y_value))
        if not unit or not values:
            continue
        key = str(item.get("key") or "")
        charts.append({
            "id": key or f"pricing-curve-{len(charts) + 1}",
            "title": _RISK_CURVE_TITLES.get(key, _text(item.get("name")) or "风险曲线"),
            "type": "line", "x": [x for x, _ in values],
            "series": [{"name": _text(axis.get("name") or "敏感性"), "data": [y for _, y in values], "value_format": "percent"}],
            "x_axis_name": _text((item.get("x_axis") or {}).get("name") if isinstance(item.get("x_axis"), Mapping) else "横轴"),
            "y_axis_name": _text(axis.get("name") or "敏感性") + "（百分比敏感度）",
            "value_format": "percent",
            "source_note": "本次估值结果，按标准化百分比敏感度表示。",
        })
    surfaces = pricing.get("risk_surfaces", result.get("risk_surfaces", []))
    for raw in surfaces if isinstance(surfaces, list) else []:
        item = raw if isinstance(raw, Mapping) else {}
        z_axis = item.get("z_axis") if isinstance(item.get("z_axis"), Mapping) else {}
        unit = _public_percent_unit(z_axis.get("unit"))
        x_axis = item.get("x_axis") if isinstance(item.get("x_axis"), Mapping) else {}
        y_axis = item.get("y_axis") if isinstance(item.get("y_axis"), Mapping) else {}
        x_values = x_axis.get("values") if isinstance(x_axis.get("values"), list) else []
        y_values = y_axis.get("values") if isinstance(y_axis.get("values"), list) else []
        cells: list[list[float]] = []
        for point in item.get("data", []) if isinstance(item.get("data"), list) else []:
            cell = point.get("value") if isinstance(point, Mapping) else None
            if not isinstance(cell, list) or len(cell) != 3:
                cells = []
                break
            numeric_cell = [_safe_number(value) for value in cell]
            if any(value is None for value in numeric_cell):
                cells = []
                break
            cells.append([value for value in numeric_cell if value is not None])
        numeric_x_values = [_safe_number(value) for value in x_values]
        numeric_y_values = [_safe_number(value) for value in y_values]
        if (
            not unit
            or not cells
            or not x_values
            or not y_values
            or any(value is None for value in (*numeric_x_values, *numeric_y_values))
        ):
            continue
        indexed_cells: list[list[float]] = []
        for x_value, y_value, z_value in cells:
            x_index, y_index = int(x_value), int(y_value)
            if (
                x_value == x_index
                and y_value == y_index
                and 0 <= x_index < len(numeric_x_values)
                and 0 <= y_index < len(numeric_y_values)
            ):
                indexed_cells.append([x_index, y_index, z_value])
            else:
                indexed_cells = []
                break
        if not indexed_cells:
            continue
        key = str(item.get("key") or "")
        charts.append({
            "id": key or f"pricing-surface-{len(charts) + 1}",
            "title": _RISK_SURFACE_TITLES.get(key, _text(item.get("name")) or "敏感性曲面"),
            "type": "heatmap", "x": [value for value in numeric_x_values if value is not None], "y": [value for value in numeric_y_values if value is not None],
            "data": indexed_cells,
            "x_axis_name": _text(x_axis.get("name") or "横轴"), "y_axis_name": _text(y_axis.get("name") or "纵轴"),
            "z_axis_name": _text(z_axis.get("name") or "敏感性") + "（百分比敏感度）",
            "value_format": "percent",
            "source_note": "本次估值结果，按标准化百分比敏感度表示。",
        })
    return charts


def _safe_optional_number(values: Mapping[str, Any], key: str) -> float | None:
    """Read an optional finite number; return None for absent and invalid values."""

    if key not in values or values.get(key) is None:
        return None
    return _safe_number(values.get(key))


_RETIRED_PRICER_FIELDS = frozenset({
    "pv_points_100", "standard_error_points_100", "pv_points_100_value", "pv_points_100_unit",
    "pv_amount", "pv_amount_value", "pv_amount_unit", "notional", "engine_raw",
})


def _contains_retired_pricer_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).casefold() in _RETIRED_PRICER_FIELDS or _contains_retired_pricer_field(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_retired_pricer_field(item) for item in value)
    return False


def _public_greeks_are_verified(greeks: Any) -> bool:
    if not isinstance(greeks, Mapping):
        return False
    for raw in greeks.values():
        if not isinstance(raw, Mapping) or _contains_retired_pricer_field(raw):
            return False
        status = str(raw.get("status") or "available").casefold()
        value = raw.get("value")
        if value is None and status not in {"not_applicable", "unavailable", "not_implemented"}:
            return False
        if value is not None and _safe_number(value) is None:
            return False
        named_value = raw.get("pv_percent_value")
        if named_value is not None and _safe_number(named_value) is None:
            return False
        if value is not None and named_value is not None and abs(float(value) - float(named_value)) > 1e-12:
            return False
        if value is not None and not _percent_greek_unit(raw.get("unit") or raw.get("pv_percent_unit")):
            return False
    return True


def _pricing_scenarios_are_verified(pricing: Mapping[str, Any], result: Mapping[str, Any]) -> bool:
    source = pricing.get("risk_scenarios", result.get("risk_scenarios", []))
    return isinstance(source, list) and all(
        isinstance(item, Mapping)
        and str(item.get("value_basis") or "pv_percent") == "pv_percent"
        and _safe_number(item.get("pv_percent")) is not None
        and (item.get("standard_error_percent") is None or _safe_number(item.get("standard_error_percent")) is not None)
        for item in source
    )


def _public_pricing_scenarios(pricing: Mapping[str, Any], result: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    source = pricing.get("risk_scenarios", result.get("risk_scenarios", []))
    for index, raw in enumerate(source if isinstance(source, list) else [], start=1):
        item = raw if isinstance(raw, Mapping) else {}
        percent = item.get("pv_percent")
        if _safe_number(percent) is None:
            continue
        spot, remaining_days = _safe_optional_number(item, "spot"), _safe_optional_number(item, "remaining_days")
        if (
            ("spot" in item and item.get("spot") is not None and spot is None)
            or ("remaining_days" in item and item.get("remaining_days") is not None and remaining_days is None)
        ):
            continue
        rows.append({
            "scenario": _text(item.get("name")) or f"情景{index}",
            "spot": spot, "time": remaining_days, "pv": percent,
            "spot_format": "number", "time_format": "number", "pv_format": "percent",
        })
    return rows


def _artifact_rows(module: Mapping[str, Any]) -> list[dict[str, str]]:
    manifest = module.get("artifact_manifest")
    if not isinstance(manifest, Mapping):
        return []
    values = manifest.get("artifacts", [])
    if not isinstance(values, list):
        return []
    run = module.get("run") if isinstance(module.get("run"), Mapping) else {}
    run_module = _text(run.get("module") or module.get("module"))
    public_module = "backtest" if run_module == "backtester" else run_module
    rows = []
    for raw in values:
        if not isinstance(raw, Mapping):
            continue
        name, storage_ref, media_type, content_hash = (raw.get("name"), raw.get("storage_ref"), raw.get("media_type"), raw.get("content_hash"))
        if all(isinstance(value, str) and value for value in (name, storage_ref, media_type, content_hash)):
            if not is_public_module_artifact(storage_ref, module=public_module):
                continue
            rows.append({"name": name, "storage_ref": storage_ref, "media_type": media_type, "content_hash": content_hash})
    return rows


_FORMULA_TOKEN = re.compile(r"\s*(?:(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)|(?P<identifier>[A-Za-z][A-Za-z0-9_]*|Π)|(?P<operator>[+\-*/(),]))")
_FORMULA_OPERATORS = {"+", "-", "*", "/", "(", ")", ","}
_FORMULA_MATHML_TAGS = frozenset({"math", "mrow", "mi", "mn", "mo", "msub", "msup", "mfrac", "mtext"})
_FORMULA_MATHML_BINARY_TAGS = frozenset({"msub", "msup", "mfrac"})


def _normalize_formula_mathml(raw_mathml: str) -> str:
    """Accept only the small MathML subset Reporter may hand to Designer.

    Payoffer may supply a pre-rendered formula, but it is still untrusted input
    at this public handoff.  The report formula grammar needs no attributes,
    foreign namespaces or presentation extensions, so reject them instead of
    relying on Designer to strip dangerous markup after the fact.
    """

    text = raw_mathml.strip()
    if not text or re.search(r"<!|<\\?", text):
        raise ReporterError("Payoffer公式MathML不属于当前支持的正式表达式，不能公开展示")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        raise ReporterError("Payoffer公式MathML不属于当前支持的正式表达式，不能公开展示") from error
    if root.tag != "math" or root.attrib or len(root) != 1 or root[0].tag != "mrow":
        raise ReporterError("Payoffer公式MathML不属于当前支持的正式表达式，不能公开展示")

    for element in root.iter():
        if not isinstance(element.tag, str) or element.tag not in _FORMULA_MATHML_TAGS or element.attrib:
            raise ReporterError("Payoffer公式MathML不属于当前支持的正式表达式，不能公开展示")
        if element.tail and not element.tail.isspace():
            raise ReporterError("Payoffer公式MathML不属于当前支持的正式表达式，不能公开展示")
        children = list(element)
        if element.tag in {"mi", "mn", "mo", "mtext"}:
            if children or not (element.text or "").strip():
                raise ReporterError("Payoffer公式MathML不属于当前支持的正式表达式，不能公开展示")
        elif element.tag in _FORMULA_MATHML_BINARY_TAGS:
            if len(children) != 2 or (element.text and not element.text.isspace()):
                raise ReporterError("Payoffer公式MathML不属于当前支持的正式表达式，不能公开展示")
        elif element.tag in {"math", "mrow"} and element.text and not element.text.isspace():
            raise ReporterError("Payoffer公式MathML不属于当前支持的正式表达式，不能公开展示")
    return ElementTree.tostring(root, encoding="unicode", short_empty_elements=True)


def _formula_identifier_mathml(identifier: str) -> str:
    base, separator, suffix = identifier.partition("_")
    if separator and base and suffix and "_" not in suffix:
        return f"<msub><mi>{escape(base)}</mi><mi>{escape(suffix)}</mi></msub>"
    return f"<mi>{escape(identifier)}</mi>"


def _validate_formula_syntax(tokens: list[tuple[str, str]]) -> None:
    """Accept a small arithmetic grammar instead of merely tokenizing text."""

    cursor = 0

    def current() -> tuple[str, str] | None:
        return tokens[cursor] if cursor < len(tokens) else None

    def consume(expected: str | None = None) -> tuple[str, str]:
        nonlocal cursor
        token = current()
        if token is None or (expected is not None and token[1] != expected):
            raise ReporterError("Payoffer公式不属于当前支持的正式表达式，不能公开展示")
        cursor += 1
        return token

    def factor() -> None:
        token = current()
        if token is None:
            raise ReporterError("Payoffer公式不属于当前支持的正式表达式，不能公开展示")
        if token[0] == "operator" and token[1] in {"+", "-"}:
            consume()
            factor()
            return
        if token[0] == "number":
            consume()
            return
        if token[0] == "identifier":
            identifier = consume()[1]
            if current() is not None and current()[1] == "(":
                if identifier not in {"max", "min"}:
                    raise ReporterError("Payoffer公式不属于当前支持的正式表达式，不能公开展示")
                consume("(")
                expression()
                while current() is not None and current()[1] == ",":
                    consume(",")
                    expression()
                consume(")")
            return
        if token[0] == "operator" and token[1] == "(":
            consume("(")
            expression()
            consume(")")
            return
        raise ReporterError("Payoffer公式不属于当前支持的正式表达式，不能公开展示")

    def term() -> None:
        factor()
        while current() is not None and current()[1] in {"*", "/"}:
            consume()
            factor()

    def expression() -> None:
        term()
        while current() is not None and current()[1] in {"+", "-"}:
            consume()
            term()

    expression()
    if current() is not None:
        raise ReporterError("Payoffer公式不属于当前支持的正式表达式，不能公开展示")


def _raw_formula_mathml(raw_formula: str) -> str:
    """Convert the formal Payoffer expression subset to deterministic MathML.

    Reporter never forwards raw formula source to Designer.  The compact lexical
    grammar intentionally accepts only identifiers, finite decimal literals and
    ordinary arithmetic/function punctuation; any other notation needs a
    Payoffer-provided MathML projection before it can cross the public boundary.
    """

    text = raw_formula.strip()
    if not text:
        raise ReporterError("Payoffer公式不能为空")
    cursor = 0
    fragments: list[str] = []
    tokens: list[tuple[str, str]] = []
    while cursor < len(text):
        token = _FORMULA_TOKEN.match(text, cursor)
        if token is None:
            raise ReporterError("Payoffer公式不属于当前支持的正式表达式，不能公开展示")
        cursor = token.end()
        if token.group("number"):
            value = token.group("number")
            tokens.append(("number", value))
            fragments.append(f"<mn>{escape(value)}</mn>")
        elif token.group("identifier"):
            value = token.group("identifier")
            tokens.append(("identifier", value))
            fragments.append(_formula_identifier_mathml(value))
        else:
            operator = token.group("operator")
            if operator not in _FORMULA_OPERATORS:
                raise ReporterError("Payoffer公式不属于当前支持的正式表达式，不能公开展示")
            tokens.append(("operator", operator))
            fragments.append(f"<mo>{escape(operator)}</mo>")
    if cursor != len(text) or not fragments:
        raise ReporterError("Payoffer公式不属于当前支持的正式表达式，不能公开展示")
    _validate_formula_syntax(tokens)
    return "<math><mrow>" + "".join(fragments) + "</mrow></math>"


def _formula(result: Mapping[str, Any]) -> dict[str, str]:
    formula_mathml = result.get("formula_mathml")
    formula = result.get("formula")
    if isinstance(formula_mathml, str) and formula_mathml.strip():
        return {"formula_mathml": _normalize_formula_mathml(formula_mathml)}
    if isinstance(formula, str) and formula.strip():
        return {"formula_mathml": _raw_formula_mathml(formula)}
    return {}


def _payoff_content(module: Mapping[str, Any]) -> dict[str, Any]:
    status = str(module.get("status", "not_run"))
    value: dict[str, Any] = {"status": status, "note": _text(module.get("note"))}
    if status != "ready":
        return value
    result = module.get("result") if isinstance(module.get("result"), Mapping) else {}
    facts = result.get("reporter_payoff_facts")
    if not isinstance(facts, Mapping) or (
        facts.get("schema") != "optionhelper.reporter-payoff-facts"
        or facts.get("projection_status") != "controlled_percent_machine_facts"
        or facts.get("source") != "runtime.contracts.evaluate_contract"
        or facts.get("discounting") != "not_applied"
        or facts.get("payoff_unit") != "percent"
    ):
        raise ReporterError("Payoffer reporter_payoff_facts不满足正式公开协议")
    paths = facts.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ReporterError("Payoffer reporter_payoff_facts.paths不能为空")
    scenarios: list[dict[str, str]] = []
    endpoint = {"open", "closed", "not_a_domain_boundary"}
    for path_index, raw in enumerate(paths, start=1):
        if not isinstance(raw, Mapping):
            raise ReporterError("reporter_payoff_facts.paths必须为对象数组")
        path_title = _text(raw.get("title") or f"路径{path_index}")
        axis = raw.get("axis") if isinstance(raw.get("axis"), Mapping) else {}
        axis_label = _text(axis.get("label") or axis.get("unit") or "横轴")
        segments = raw.get("scenario_segments")
        if not isinstance(segments, list) or not segments:
            raise ReporterError("reporter_payoff_facts路径缺少scenario_segments")
        for segment_index, segment in enumerate(segments, start=1):
            if not isinstance(segment, Mapping):
                raise ReporterError("reporter_payoff_facts.scenario_segments必须为对象数组")
            domain = segment.get("domain") if isinstance(segment.get("domain"), Mapping) else {}
            payoff = segment.get("payoff") if isinstance(segment.get("payoff"), Mapping) else {}
            lower, upper = _safe_number(domain.get("lower")), _safe_number(domain.get("upper"))
            minimum, maximum = _safe_number(payoff.get("minimum_percent")), _safe_number(payoff.get("maximum_percent"))
            if lower is None or upper is None or lower > upper or minimum is None or maximum is None or minimum > maximum or payoff.get("unit") != "percent":
                raise ReporterError("reporter_payoff_facts分段定义域或收益范围无效")
            lower_kind, upper_kind = str(domain.get("lower_endpoint")), str(domain.get("upper_endpoint"))
            if lower_kind not in endpoint or upper_kind not in endpoint:
                raise ReporterError("reporter_payoff_facts端点类型无效")
            lower_text, upper_text = _decimal_text(lower), _decimal_text(upper)
            if lower_kind == "not_a_domain_boundary" and upper_kind == "not_a_domain_boundary":
                condition = f"{axis_label}覆盖全部有效区间"
            elif upper_kind == "not_a_domain_boundary":
                operator = "≥" if lower_kind == "closed" else ">"
                condition = f"{axis_label}{operator}{lower_text}"
            elif lower_kind == "not_a_domain_boundary":
                operator = "≤" if upper_kind == "closed" else "<"
                condition = f"{axis_label}{operator}{upper_text}"
            else:
                left_bracket = "[" if lower_kind == "closed" else "("
                right_bracket = "]" if upper_kind == "closed" else ")"
                condition = f"{axis_label}∈{left_bracket}{lower_text},{upper_text}{right_bracket}"
            payoff_text = (
                f"收益率为{_decimal_text(minimum)}%"
                if math.isclose(minimum, maximum, rel_tol=0.0, abs_tol=1e-12)
                else f"收益率区间为{_decimal_text(minimum)}%至{_decimal_text(maximum)}%"
            )
            scenarios.append({
                "title": f"{path_title}·情景{segment_index}",
                "rule": f"{condition}；{payoff_text}。",
            })
    value["scenarios"] = scenarios
    value["artifacts"] = _artifact_rows(module)
    return value


def _contract_product_id(contract: Mapping[str, Any] | None) -> str:
    if not isinstance(contract, Mapping):
        return ""
    identity = contract.get("identity") if isinstance(contract.get("identity"), Mapping) else {}
    return _text(identity.get("product_id") or contract.get("product_id"))


def _contract_initial_premium_percent(contract: Mapping[str, Any] | None) -> str:
    if not isinstance(contract, Mapping):
        return ""
    terms = contract.get("terms") if isinstance(contract.get("terms"), Mapping) else {}
    # p is a decimal ratio; Pi_0 is already percent-of-S0=100 points.
    # Preserve the declared encoding even outside a product's admissible range.
    for symbol, percent_scale in (("p", 100), ("Pi_0", 1)):
        if symbol in terms:
            premium = _safe_number(terms[symbol])
            return "" if premium is None else _decimal_text(premium * percent_scale) + "%"
    return ""


def _pricing_content(module: Mapping[str, Any], contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
    status = str(module.get("status", "not_run"))
    value: dict[str, Any] = {"status": status, "note": _text(module.get("note"))}
    if status != "ready":
        return value
    result = module.get("result") if isinstance(module.get("result"), Mapping) else {}
    pricing = result.get("pricing") if isinstance(result.get("pricing"), Mapping) else result
    if pricing.get("result_kind") == "fair_parameter_solution":
        return _fair_parameter_content(module, pricing)
    standard_error_percent = pricing.get("standard_error_percent")
    percent = pricing.get("pv_percent")
    valid_pv = _safe_number(percent) is not None
    valid_error = standard_error_percent is None or _safe_number(standard_error_percent) is not None
    if (
        pricing.get("value_basis") != "pv_percent"
        or _contains_retired_pricer_field(pricing)
        or not valid_pv
        or not valid_error
        or not _public_greeks_are_verified(pricing.get("greeks"))
        or not _pricing_scenarios_are_verified(pricing, result)
    ):
        raise ReporterError("估值结果不满足当前百分比正式协议")
    value["method"] = _text(pricing.get("method"))
    market_snapshot = pricing.get("market_snapshot") if isinstance(pricing.get("market_snapshot"), Mapping) else {}
    value["valuation_date"] = _text(
        pricing.get("valuation_date")
        or market_snapshot.get("valuation_date")
        or market_snapshot.get("market_as_of_date")
    )
    value["metrics"] = [
        _metric("估值PV", percent, "标准化百分比口径", value_format="percent"),
    ]
    if standard_error_percent is not None:
        value["metrics"].append(
            _metric("估值标准误", standard_error_percent, "标准化百分比口径", value_format="percent")
        )
    # 点数仅用于内部一致性校验；公开交付只显示标准化百分比。
    value["greeks"] = _public_greek_rows(pricing.get("greeks", {}))
    value["assumptions"] = []
    if _contract_product_id(contract) == "9.3":
        premium = _contract_initial_premium_percent(contract)
        observed = pricing.get("observed_contract_state") if isinstance(pricing.get("observed_contract_state"), Mapping) else {}
        lifecycle = _text(observed.get("lifecycle_status")).lower()
        if lifecycle == "active":
            value["assumptions"].append(
                f"期初{premium}期权费已实现；存续期估值仅反映剩余合同价值，不重复扣除期权费。"
            )
        else:
            value["assumptions"].append(
                f"期初{premium}期权费计入合同起始时点价值；进入存续期后不得重复扣除期权费。"
            )
    value["charts"] = public_chart_specs(_safe_pricing_charts(pricing, result))
    value["scenario_rows"] = _public_pricing_scenarios(pricing, result)
    value["artifacts"] = _artifact_rows(module)
    precision_status = _text(pricing.get("precision_status"))
    quote_eligible = pricing.get("quote_eligible") is True
    value["precision_status"] = precision_status or "未声明"
    value["quote_eligible"] = quote_eligible
    value["limitations"] = []
    if not quote_eligible or precision_status in {"", "demo_only", "not_assessed", "not_priced"}:
        value["status"] = "partial"
        value["note"] = "本次估值为测试或受限精度结果，不可用于正式报价。"
        value["limitations"].append("本次估值未通过正式报价资格或精度门槛，不可用于正式报价。")
    return value


_FAIR_VALUE_BASIS_LABELS = {
    "pv_percent": "标准化现值百分比",
    "variance_percent": "方差百分比",
}
_FAIR_CONVERGENCE_LABELS = {
    "solved": "已收敛",
    "converged": "已收敛",
    "insufficient": "证据不足",
    "not_converged": "未收敛",
    "failed": "求解失败",
}
_FAIR_QUOTE_BASIS_LABELS = {
    "incremental_net_value": "增量净价值",
    "contract_target_value": "合同目标价值",
    "direct_value": "直接价值",
    "target_value": "目标价值",
}
_FAIR_FORBIDDEN_TARGET_TOKENS = (
    "implied_volatility", "implied-volatility", "implied volatility",
    "volatility_surface", "volatility-surface", "volatility surface",
    "隐含波动率", "波动率曲面",
)


def _fair_target_metadata(pricing: Mapping[str, Any], target_id: str) -> tuple[str, str]:
    del pricing
    definition = load_term_catalog().get(target_id)
    if not isinstance(definition, Mapping):
        raise ReporterError("公平条款反解目标不在当前term_catalog")
    label = (_text(definition.get("name_zh")) or target_id).replace("点数", "百分比")
    catalog_unit = _text(definition.get("unit"))
    unit = {
        "normalized_point": "percentage",
        "rate": "percentage",
        "volatility": "percentage",
        "premium_percent_s0_100": "percentage",
        "price": "percentage",
        "year": "year",
        "count": "count",
    }.get(catalog_unit, catalog_unit or "number")
    return label, unit
_FAIR_PUBLIC_UNCERTAINTY_FIELDS = frozenset({
    "status", "precision_status", "quote_eligible", "quote_delivery_status",
    "formal_quote_status", "formal_quote_reason", "absolute_error_upper_bound",
    "lower", "upper", "confidence_level", "reason", "quote_reason",
    "parameter_error_gate", "independent_batch_count", "path_count",
    "random_paths_generated", "slope_stability_status", "primary_batch_included",
})


def _fair_public_uncertainty(value: Mapping[str, Any]) -> dict[str, Any]:
    """Retain only reader-facing uncertainty facts in the frozen ReportUnit."""

    result: dict[str, Any] = {}
    for key in _FAIR_PUBLIC_UNCERTAINTY_FIELDS:
        if key not in value or value[key] is None:
            continue
        item = value[key]
        if key in {"absolute_error_upper_bound", "lower", "upper", "confidence_level"}:
            numeric = _safe_number(item)
            if numeric is not None:
                result[key] = numeric
        elif key in {"quote_eligible", "primary_batch_included"}:
            if isinstance(item, bool):
                result[key] = item
        elif key in {"independent_batch_count", "path_count", "random_paths_generated"}:
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
                result[key] = item
        elif isinstance(item, str) and item.strip():
            result[key] = item
    interval = value.get("certified_interval")
    if isinstance(interval, Mapping):
        lower, upper = _safe_number(interval.get("lower")), _safe_number(interval.get("upper"))
        if lower is not None and upper is not None and lower <= upper:
            result["certified_interval"] = {"lower": lower, "upper": upper}
    return result


def _fair_public_final_valuation(value: Mapping[str, Any], *, value_basis: str) -> dict[str, Any]:
    """Keep final value and verified Greek rows, excluding contract/proof fields."""

    result: dict[str, Any] = {"value_basis": value_basis, value_basis: float(value[value_basis])}
    for key in ("status", "method", "valuation_date", "precision_status"):
        if isinstance(value.get(key), str) and value[key].strip():
            result[key] = value[key]
    standard_error = _safe_number(value.get("standard_error_percent"))
    if standard_error is not None:
        result["standard_error_percent"] = standard_error
    result["greeks"] = _public_greek_rows(value.get("greeks", {}))
    return result


def _fair_parameter_content(module: Mapping[str, Any], pricing: Mapping[str, Any]) -> dict[str, Any]:
    """Render the verified fair solve as a research estimate, never as a quote."""

    value_basis = str(pricing.get("value_basis") or pricing.get("quote_value_basis") or "").strip()
    if value_basis not in _FAIR_VALUE_BASIS_LABELS:
        raise ReporterError("公平参数结果缺少受控价值口径")
    if pricing.get("quote_eligible") is not False:
        raise ReporterError("公平参数研究结果不得具备正式报价资格")
    if pricing.get("precision_status") != "research_only":
        raise ReporterError("公平参数结果必须标记research_only")
    for key in ("formal_quote_status", "quote_delivery_status"):
        if pricing.get(key) not in (None, "research_only"):
            raise ReporterError(f"公平参数结果{key}必须标记research_only")
    final = pricing.get("final_valuation") if isinstance(pricing.get("final_valuation"), Mapping) else {}
    final_value: float | None = None
    if final:
        if final.get("value_basis") != value_basis:
            raise ReporterError("公平参数final_valuation与目标价值口径不一致")
        final_value = _safe_number(final.get(value_basis))
        if final_value is None:
            raise ReporterError("公平参数final_valuation缺少受控价值")
        if not _public_greeks_are_verified(final.get("greeks")):
            raise ReporterError("公平参数final_valuation的Greeks未通过公开校验")
    uncertainty_value = pricing.get("solution_uncertainty")
    if uncertainty_value is not None and not isinstance(uncertainty_value, Mapping):
        raise ReporterError("公平参数solution_uncertainty必须为对象")
    uncertainty = uncertainty_value if isinstance(uncertainty_value, Mapping) else {}
    if any(str(key) in {"identity", "bound_evidence", "bound_sources", "bound_source_details", "evaluations"} for key in uncertainty):
        raise ReporterError("公平参数solution_uncertainty包含内部证明字段")

    quote_basis = str(pricing.get("quote_basis") or pricing.get("quote_value_basis") or "").strip()
    quote_basis_label = _FAIR_QUOTE_BASIS_LABELS.get(quote_basis, quote_basis or "目标口径")
    value_basis_label = _FAIR_VALUE_BASIS_LABELS[value_basis]
    target_id = _text(pricing.get("target_id")) or "目标参数"
    normalized_target = " ".join((
        target_id, _text(pricing.get("target_label")), _text(pricing.get("target_unit")),
    )).casefold()
    if (
        target_id.casefold() in {"iv", "sigma"}
        or any(token in normalized_target for token in _FAIR_FORBIDDEN_TARGET_TOKENS)
    ):
        raise ReporterError("公平条款反解不接受隐含波动率或波动率曲面反解")
    target_label, target_unit = _fair_target_metadata(pricing, target_id)
    target_value = _safe_number(pricing.get("target_value"))
    solution = _safe_number(pricing.get("solution"))
    base_parameter = _safe_number(pricing.get("base_parameter"))
    residual = _safe_number(pricing.get("residual"))
    if any(item is None for item in (target_value, solution, base_parameter, residual)):
        raise ReporterError("公平参数结果缺少有限目标、参数解或残差")

    public_final = _fair_public_final_valuation(final, value_basis=value_basis) if final else {}
    public_uncertainty = _fair_public_uncertainty(uncertainty)
    precision_status = (
        _text(pricing.get("precision_status"))
        or _text(public_uncertainty.get("precision_status"))
        or "research_only"
    )
    raw_convergence = str(pricing.get("convergence_status") or pricing.get("status") or "")
    convergence_status = _FAIR_CONVERGENCE_LABELS.get(raw_convergence)
    if convergence_status is None:
        convergence_status = raw_convergence if raw_convergence in _FAIR_CONVERGENCE_LABELS.values() else "未确认"
    target_value_format = "percent" if target_unit == "percentage" else "number"
    metrics = [
        _metric("求解条款", f"{target_label}（{target_id}）", value_format="text"),
        _metric("反解值", solution, value_format=target_value_format),
        _metric("单位", "%" if target_unit == "percentage" else _text(target_unit), value_format="text"),
        _metric("原值", base_parameter, value_format=target_value_format),
        _metric("目标价值", target_value, f"目标口径：{quote_basis_label}", value_format="percent" if value_basis.endswith("_percent") else "number"),
        _metric("求解残差", residual, f"目标口径：{quote_basis_label}", value_format="percent" if value_basis.endswith("_percent") else "number"),
        _metric("收敛状态", convergence_status, value_format="text"),
    ]
    if final_value is not None:
        metrics.append(_metric(
            f"最终估值（{value_basis_label}）",
            final_value,
            "最终候选合同的实际价值口径",
            value_format="percent" if value_basis.endswith("_percent") else "number",
        ))
    metrics.extend([
        _metric("研究用途", "仅限研究估计", "不得解释为可执行报价", value_format="text"),
        _metric("报价资格", "不具备", _text(pricing.get("formal_quote_reason")) or "尚未取得正式报价资格", value_format="text"),
    ])
    source_limitations = [
        item.strip()
        for item in module.get("limitations", [])
        if isinstance(item, str) and item.strip()
    ] if isinstance(module.get("limitations"), list) else []
    limitations = list(dict.fromkeys([
        *source_limitations,
        _text(pricing.get("formal_quote_reason")) or "公平参数结果尚未取得正式报价资格。",
        "本结果为研究估计，不得使用普通报价事实生成Quote。",
    ]))
    value: dict[str, Any] = {
        "status": "ready",
        "kind": "fair_parameter",
        "note": "公平条款反解已完成，仅限研究；报价资格与限制见结果末项。",
        "method": _text(pricing.get("method")),
        "target_id": target_id,
        "target_label": target_label,
        "target_unit": target_unit,
        "convergence_status": convergence_status,
        "target_value": target_value,
        "solution": solution,
        "base_parameter": base_parameter,
        "residual": residual,
        "quote_basis": quote_basis,
        "quote_value_basis": _text(pricing.get("quote_value_basis")) or value_basis,
        "value_basis": value_basis,
        "formal_quote_status": _text(pricing.get("formal_quote_status")) or "research_only",
        "quote_delivery_status": _text(pricing.get("quote_delivery_status")) or "research_only",
        "formal_quote_reason": _text(pricing.get("formal_quote_reason")),
        "precision_status": precision_status,
        "quote_eligible": False,
        **({"solution_uncertainty": public_uncertainty} if public_uncertainty else {}),
        **({"final_valuation": public_final} if public_final else {}),
        "metrics": metrics,
        "greeks": deepcopy(public_final.get("greeks", [])),
        "assumptions": [
            f"目标口径：{quote_basis_label}；价值口径：{value_basis_label}。",
            "最终估值的资格不等同于公平参数结果的正式报价资格。",
        ],
        "charts": public_chart_specs(_safe_pricing_charts(final, final)) if final else [],
        "scenario_rows": _public_pricing_scenarios(final, final) if final and value_basis == "pv_percent" else [],
        "artifacts": _artifact_rows(module),
        "limitations": limitations,
    }
    absolute_error = _safe_number(uncertainty.get("absolute_error_upper_bound"))
    if absolute_error is not None:
        value["metrics"].append(_metric("参数绝对误差上界", absolute_error, "solution_uncertainty给出的上界", value_format="number"))
    interval = uncertainty.get("certified_interval")
    if isinstance(interval, Mapping) and _safe_number(interval.get("lower")) is not None and _safe_number(interval.get("upper")) is not None:
        value["metrics"].append(_metric(
            "认证参数区间",
            f"{_decimal_text(interval['lower'])}至{_decimal_text(interval['upper'])}",
            "仅作研究范围展示",
            value_format="text",
        ))
    standard_error = _safe_number(final.get("standard_error_percent")) if final else None
    if standard_error is not None:
        value["metrics"].append(_metric(
            f"最终估值标准误（{value_basis_label}）",
            standard_error,
            "最终估值结果的标准误，不是参数误差门槛",
            value_format="percent",
        ))
    return value


def _economic_convention_status(backtest: Mapping[str, Any]) -> str:
    """Accept only the Backtester public decimal-percentage convention."""

    convention = backtest.get("economic_convention")
    if not isinstance(convention, Mapping):
        return "invalid"
    canonical = (
        convention.get("basis") == "declared_contract_cashflows_over_contract_scale"
        and convention.get("display_unit") == "percentage"
        and convention.get("value_encoding") == "decimal_ratio"
        and convention.get("positive_return_rate_numerator") == "positive_return_count"
        and convention.get("positive_return_rate_denominator") == "valid_return_sample_count"
    )
    premium_status = convention.get("contractual_premium_status")
    premium_explanation = convention.get("contractual_premium_explanation")
    excluded_costs = convention.get("excluded_external_costs")
    if (
        canonical
        and convention.get("external_costs_modelled") is False
        and premium_status in {
            "included_in_contract_cashflows", "fixed_zero",
            "blocked_unrepresented", "no_separate_premium_cashflow",
        }
        and isinstance(premium_explanation, str) and premium_explanation.strip()
        and excluded_costs == [
            "funding_cost", "transaction_cost", "tax", "hedging_cost", "slippage",
        ]
    ):
        return "formal"
    return "invalid"


_FORMAL_LEDGER_RETURN_CONVENTION = {
    "basis": "declared_contract_cashflows_over_contract_scale",
    "display_unit": "percentage",
    "value_encoding": "decimal_ratio",
}
_FORMAL_LEDGER_ALLOWED_FIELDS = frozenset({
    "trade_id", "entry_date", "exit_date", "actual_calendar_days", "actual_time_years",
    "historical_resolved_contract", "entry_market_spots", "entry_normalized_spots",
    "settlement_market_spots", "underlying_performances", "path_id", "case_id",
    "settlement_type", "events", "contract_settlement_return", "contract_settlement_return_convention",
    "terminal_performance", "entry_features",
    "data_flags", "limitations",
})


def _has_forbidden_ledger_field(value: Any) -> bool:
    """Reject retired monetary fields anywhere in a public ledger record."""

    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = re.sub(r"[^a-z0-9]+", "", str(raw_key).casefold())
            if (
                "points" in key
                or "cashflow" in key
                or "amount" in key
                or "notional" in key
                or ("pnl" in key and key != "clientnetpnl")
            ):
                return True
            if _has_forbidden_ledger_field(nested):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_has_forbidden_ledger_field(item) for item in value)
    return False


def _formal_trade_ledger(backtest: Mapping[str, Any], *, valid_return_sample_count: int) -> bool:
    """Verify, but never project, every formal Backtester ledger record."""

    ledger = backtest.get("trade_ledger")
    if not isinstance(ledger, list) or len(ledger) != valid_return_sample_count:
        return False
    for row in ledger:
        if not isinstance(row, Mapping):
            return False
        if set(row) - _FORMAL_LEDGER_ALLOWED_FIELDS or _has_forbidden_ledger_field(row):
            return False
        canonical_return = _safe_number(row.get("contract_settlement_return"))
        convention = row.get("contract_settlement_return_convention")
        normalization = convention.get("normalization") if isinstance(convention, Mapping) else None
        if (
            canonical_return is None
            or not isinstance(convention, Mapping)
            or not all(convention.get(key) == expected for key, expected in _FORMAL_LEDGER_RETURN_CONVENTION.items())
            or not isinstance(normalization, Mapping)
            or set(normalization) != {"source", "contract_base"}
            or not all(isinstance(item, str) and item.strip() for item in normalization.values())
        ):
            return False
    return True


_RETURN_PAIR_KEYS = (
    "average_contract_settlement_return",
    "median_contract_settlement_return",
    "minimum_contract_settlement_return",
    "maximum_contract_settlement_return",
)

def _valid_return_pairs(common: Mapping[str, Any]) -> bool:
    """Validate Backtester public return ratios without retired point fields."""

    if any(key.endswith("_points") for key in common):
        return False
    return all(
        return_key in common and _safe_number(common.get(return_key)) is not None
        for return_key in _RETURN_PAIR_KEYS
    )


def _safe_count(value: Any) -> int | None:
    """Accept only finite non-negative integer counts from a frozen run."""

    numeric = _safe_number(value)
    if numeric is None or numeric < 0 or not numeric.is_integer():
        return None
    return int(numeric)


def _safe_ratio(value: Any) -> float | None:
    """Accept a finite probability/rate on its declared unit interval."""

    numeric = _safe_number(value)
    return numeric if numeric is not None and 0.0 <= numeric <= 1.0 else None


def _safe_optional(
    values: Mapping[str, Any],
    key: str,
    reader: Any,
) -> tuple[bool, Any]:
    """Read one optional public field without coercion.

    ``False`` means the upstream result supplied the field but it is not a
    valid finite value for its declared type.  Callers then drop the whole
    optional row instead of passing malformed facts to Designer.
    """

    if key not in values or values.get(key) is None:
        return True, None
    value = reader(values.get(key))
    return value is not None, value


def _verified_backtest_common(common: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate the public summary without recalculating any backtest fact."""

    normalized = deepcopy(dict(common))
    sample_count = _safe_count(normalized.get("sample_count"))
    valid_count = _safe_count(normalized.get("valid_return_sample_count"))
    positive_count = _safe_count(normalized.get("positive_return_count"))
    positive_rate = _safe_ratio(normalized.get("positive_return_rate"))
    zero_count = _safe_count(normalized.get("zero_return_count"))
    negative_count = _safe_count(normalized.get("negative_return_count"))
    historical_loss_sample_covered = normalized.get("historical_loss_sample_covered")
    if (
        sample_count is None or valid_count is None or positive_count is None or positive_rate is None
        or valid_count == 0 or positive_count > valid_count or valid_count > sample_count
        or not math.isclose(positive_rate, positive_count / valid_count, rel_tol=0.0, abs_tol=1e-12)
        or zero_count is None or negative_count is None
        or positive_count + zero_count + negative_count != valid_count
        or not isinstance(historical_loss_sample_covered, bool)
        or historical_loss_sample_covered != (negative_count > 0)
        or not _valid_return_pairs(normalized)
    ):
        return None
    return {
        **normalized,
        "sample_count": sample_count,
        "valid_return_sample_count": valid_count,
        "positive_return_count": positive_count,
        "positive_return_rate": positive_rate,
        "historical_loss_sample_covered": historical_loss_sample_covered,
    }


def _public_return_percent(source: Mapping[str, Any], key: str) -> float | None:
    """Read one Backtester decimal return ratio, rejecting retired point fields."""

    if f"{key}_points" in source:
        return None
    return _safe_number(source.get(key))


def _backtest_metric_note(
    key: str,
    *,
    valid_sample_count: int | None = None,
) -> str:
    if key in {"sample_count", "valid_return_sample_count", "positive_return_count", "zero_return_count", "negative_return_count", "skipped_count"}:
        return "个有效入场样本"
    if key == "positive_return_rate":
        denominator = f"分母为{valid_sample_count}个有效收益样本" if valid_sample_count is not None else "分母为回测结果所列有效收益样本"
        return f"合同结算收益率严格大于零的历史样本占比，{denominator}；不代表未来获利概率"
    if key == "historical_loss_sample_covered":
        return "是否在本次历史样本中出现负合同结算收益"
    if key.endswith("_contract_settlement_return") or "contract_settlement_return" in key:
        return "百分比；按合同约定收支计算，未包含外部资金与交易成本"
    if key.endswith("_rate"):
        return "占有效入场样本"
    if key.endswith("_days"):
        return "自然日"
    return "口径未提供"


_PREMIUM_STATUS_NOTES = {
    "included_in_contract_cashflows": "期初期权费已纳入合同结算收益率，不应重复扣除。",
    "fixed_zero": "本次合同的期权费固定为零。",
    "no_separate_premium_cashflow": "本次合同未声明独立的期权费收支。",
    "blocked_unrepresented": "本次冻结结果的期权费尚未完整表达，不能视为已扣除期权费的收益。",
}


def _backtest_content(module: Mapping[str, Any], contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
    status = str(module.get("status", "not_run"))
    value: dict[str, Any] = {"status": status, "note": _text(module.get("note"))}
    if status != "ready":
        return value
    result = module.get("result") if isinstance(module.get("result"), Mapping) else {}
    backtest = result.get("backtest") if isinstance(result.get("backtest"), Mapping) else result
    market = backtest.get("market_data") if isinstance(backtest.get("market_data"), Mapping) else {}
    config = backtest.get("backtest_config") if isinstance(backtest.get("backtest_config"), Mapping) else {}
    raw_common = backtest.get("common_metrics") if isinstance(backtest.get("common_metrics"), Mapping) else backtest
    common = _verified_backtest_common(raw_common)
    formal_ledger_required = "trade_ledger" in backtest or isinstance(module.get("artifact_manifest"), Mapping)
    if (
        _economic_convention_status(backtest) != "formal"
        or common is None
        or (
            formal_ledger_required
            and not _formal_trade_ledger(backtest, valid_return_sample_count=common["valid_return_sample_count"])
        )
    ):
        raise ReporterError("回测结果不满足当前百分比正式协议或逐笔账本校验")
    start = _text(market.get("date_start"))
    end = _text(market.get("date_end"))
    value["window"] = "至".join(item for item in (start, end) if item)
    value["as_of_date"] = end
    value["entry_rule"] = _text(config.get("entry_rule"))
    valid_sample_count = common["valid_return_sample_count"]
    public_keys = (
        "sample_count", "valid_return_sample_count", "positive_return_count", "zero_return_count",
        "negative_return_count", "positive_return_rate", "average_contract_settlement_return",
        "median_contract_settlement_return", "minimum_contract_settlement_return",
        "maximum_contract_settlement_return", "historical_loss_sample_covered",
    )
    value["metrics"] = [
        _metric(
            _BACKTEST_LABELS[key], ("已覆盖" if common[key] else "未覆盖") if key == "historical_loss_sample_covered" else common[key],
            _backtest_metric_note(key, valid_sample_count=valid_sample_count),
            value_format="text" if key == "historical_loss_sample_covered" else _public_value_format(key),
        )
        for key in public_keys if common.get(key) is not None
    ]
    value["historical_loss_sample_covered"] = common["historical_loss_sample_covered"]

    detail_tables: list[dict[str, Any]] = []
    if value["metrics"]:
        detail_tables.append({"title": "公共回测统计", "columns": [("label", "指标"), ("value", "数值")], "rows": deepcopy(value["metrics"])})

    coverage_limitations: list[Any] = []
    branch = backtest.get("branch_coverage") if isinstance(backtest.get("branch_coverage"), Mapping) else {}
    declared = _safe_count(branch.get("declared_pair_count"))
    observed = _safe_count(branch.get("observed_pair_count"))
    uncovered = _safe_count(branch.get("uncovered_pair_count"))
    declared_loss = _safe_count(branch.get("declared_loss_pair_count"))
    observed_loss = _safe_count(branch.get("observed_loss_pair_count"))
    uncovered_loss = _safe_count(branch.get("uncovered_loss_pair_count"))
    coverage_status = _text(branch.get("status")).lower()
    counts_are_valid = (
        all(item is not None for item in (declared, observed, uncovered))
        and observed + uncovered == declared
        and ((coverage_status == "complete" and uncovered == 0) or (coverage_status == "partial" and uncovered > 0))
    )
    unclassified_loss = _safe_count(branch.get("unclassified_pair_count"))
    loss_counts_are_valid = (
        branch.get("loss_classification_status") == "complete"
        and unclassified_loss == 0
        and all(item is not None for item in (declared_loss, observed_loss, uncovered_loss))
        and observed_loss + uncovered_loss == declared_loss
        and observed_loss <= observed
        and declared_loss <= declared
        and uncovered_loss <= uncovered
    ) if counts_are_valid else False
    if counts_are_valid:
        detail_tables.append({
            "title": "分支覆盖",
            "columns": [("label", "指标"), ("value", "数量")],
            "rows": [
                {
                    "label": "已观察分支",
                    "value": observed,
                    "value_format": "number",
                    "note": f"合同声明分支覆盖{observed}/{declared}；仅统计回测样本实际落入的分支",
                },
                {
                    "label": "未观察分支",
                    "value": uncovered,
                    "value_format": "number",
                    "note": "不代表未观察分支已实际发生，也不代表其经济结果已验证",
                },
                *([{
                    "label": "已观察损失分支",
                    "value": observed_loss,
                    "value_format": "number",
                    "note": f"损失分支覆盖{observed_loss}/{declared_loss}",
                }, {
                    "label": "未观察损失分支",
                    "value": uncovered_loss,
                    "value_format": "number",
                    "note": "仅陈述历史样本覆盖，不代表未来损失概率",
                }] if loss_counts_are_valid else []),
            ],
        })
        if isinstance(branch.get("limitations"), list) and branch["limitations"]:
            coverage_limitations.append("分支覆盖存在额外限制，相关结论仅适用于已列示样本范围。")
    elif branch:
        coverage_limitations.append("本次回测的分支覆盖统计不一致，未作为覆盖结论展示。")

    event_rows = []
    event_summary = backtest.get("event_summary") if isinstance(backtest.get("event_summary"), Mapping) else {}
    for key, raw in event_summary.items():
        item = raw if isinstance(raw, Mapping) else {}
        label = _text(item.get("label")) or _EVENT_LABELS.get(str(key))
        if not label:
            continue
        valid_sample, sample_count = _safe_optional(item, "sample_count", _safe_count)
        if "sample_count" not in item:
            valid_sample, sample_count = _safe_optional(item, "count", _safe_count)
        valid_trigger, trigger_count = _safe_optional(item, "trigger_count", _safe_count)
        if "trigger_count" not in item:
            valid_trigger, trigger_count = _safe_optional(item, "count", _safe_count)
        valid_rate, trigger_rate = _safe_optional(item, "trigger_rate", _safe_ratio)
        if "trigger_rate" not in item:
            valid_rate, trigger_rate = _safe_optional(item, "rate", _safe_ratio)
        valid_average, average_days = _safe_optional(item, "average_days", _safe_number)
        valid_median, median_days = _safe_optional(item, "median_days", _safe_number)
        if not all((valid_sample, valid_trigger, valid_rate, valid_average, valid_median)):
            continue
        event_rows.append({
            "event": label, "sample_count": sample_count, "trigger_count": trigger_count,
            "trigger_rate": trigger_rate, "trigger_rate_format": "percent",
            "average_days": average_days, "median_days": median_days,
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
        valid_sample, sample_count = _safe_optional(item, "sample_count", _safe_count)
        valid_average, average = _safe_optional(item, "average", _safe_number)
        valid_median, median = _safe_optional(item, "median", _safe_number)
        valid_minimum, minimum = _safe_optional(item, "minimum", _safe_number)
        valid_maximum, maximum = _safe_optional(item, "maximum", _safe_number)
        valid_true_rate, true_rate = _safe_optional(item, "true_rate", _safe_ratio)
        if not all((valid_sample, valid_average, valid_median, valid_minimum, valid_maximum, valid_true_rate)):
            continue
        monitor_rows.append({
            "monitor": label, "sample_count": sample_count, "average": average,
            "median": median, "minimum": minimum, "maximum": maximum,
            "true_rate": true_rate, "true_rate_format": "percent",
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
        valid_count, count = _safe_optional(item, "count", _safe_count)
        valid_rate, rate = _safe_optional(item, "rate", _safe_ratio)
        if not valid_count or not valid_rate:
            continue
        row = {"label": _text(item.get("label")), "count": count, "rate": rate, "rate_format": "percent"}
        average_return = _public_return_percent(item, "average_contract_settlement_return")
        if average_return is not None:
            row.update({"average_contract_settlement_return": average_return, "average_contract_settlement_return_format": "percent"})
        outcome_rows.append(row)
    if outcome_rows:
        outcome_columns = [("label", "路径结果"), ("count", "样本数"), ("rate", "占比")]
        if any("average_contract_settlement_return" in row for row in outcome_rows):
            outcome_columns.append(("average_contract_settlement_return", "平均合同结算收益率"))
        detail_tables.append({
            "title": "路径结果统计",
            "columns": outcome_columns,
            "rows": outcome_rows,
        })

    annual_rows = []
    for raw in backtest.get("annual_summary", []) if isinstance(backtest.get("annual_summary"), list) else []:
        item = raw if isinstance(raw, Mapping) else {}
        valid_year, year = _safe_optional(item, "year", _safe_count)
        valid_samples, sample_count = _safe_optional(item, "sample_count", _safe_count)
        valid_positive_rate, positive_rate = _safe_optional(item, "positive_return_rate", _safe_ratio)
        if item.get("year") is not None and valid_year and valid_samples and valid_positive_rate:
            row = {
                "year": year, "sample_count": sample_count, "positive_return_rate": positive_rate, "positive_return_rate_format": "percent",
            }
            average_return = _public_return_percent(item, "average_contract_settlement_return")
            if average_return is not None:
                row.update({"average_contract_settlement_return": average_return, "average_contract_settlement_return_format": "percent"})
            annual_rows.append(row)
    if annual_rows:
        annual_columns = [("year", "年份"), ("sample_count", "样本数"), ("positive_return_rate", "历史正收益样本占比")]
        if any("average_contract_settlement_return" in row for row in annual_rows):
            annual_columns.append(("average_contract_settlement_return", "平均合同结算收益率"))
        detail_tables.append({
            "title": "年度统计",
            "columns": annual_columns,
            "rows": annual_rows,
        })
    specialized_rows = _specialized_metric_rows(backtest)
    if specialized_rows:
        detail_tables.append({
            "title": "产品专属统计",
            "columns": [("label", "指标"), ("value", "统计值")],
            "rows": deepcopy(specialized_rows),
        })
    value["detail_tables"] = detail_tables
    value["event_statistics"] = []
    value["card_metrics"] = specialized_rows[:4]
    # Economics come only from this verified frozen result. Neither the current
    # page contract nor today's catalog may replace its premium status or rate.
    convention = deepcopy(dict(backtest["economic_convention"]))
    value["economic_convention"] = convention
    coverage_note = (
        f"合同声明分支覆盖{observed}/{declared}"
        if counts_are_valid else "合同声明分支覆盖无法由本次冻结结果确认"
    )
    loss_branch_note = (
        f"损失分支覆盖{observed_loss}/{declared_loss}"
        if loss_counts_are_valid else (
            f"损失分支尚有{unclassified_loss}个未判定"
            if unclassified_loss is not None and unclassified_loss > 0
            else "损失分支覆盖无法由本次冻结结果确认"
        )
    )
    loss_sample_note = "已覆盖" if common["historical_loss_sample_covered"] else "未覆盖"
    value["summary_notes"] = [
        f"正收益样本{common['positive_return_count']}/{valid_sample_count}；有效收益样本共{valid_sample_count}个，"
        f"正收益{common['positive_return_count']}个、持平{common['zero_return_count']}个、负收益{common['negative_return_count']}个。",
        (
            "本次历史样本未出现负收益，不代表未来获利概率"
            if common["negative_return_count"] == 0
            else "历史正收益样本占比仅描述本次有效收益样本，不代表未来获利概率。"
        ),
        f"{coverage_note}；{loss_branch_note}；历史损失样本覆盖：{loss_sample_note}。",
        _PREMIUM_STATUS_NOTES[convention["contractual_premium_status"]],
        "合同结算收益率按合同约定收支计算；资金成本、交易费、税费、对冲和滑点未建模。",
    ]
    value["limitations"] = [
        *value["summary_notes"],
        *[item for item in coverage_limitations if isinstance(item, str)],
    ]
    value["charts"] = _safe_backtest_charts(backtest)
    value["artifacts"] = _artifact_rows(module)
    return value


def _term_display_value(value: Any) -> Any:
    """Keep numerical facts; render observation selectors as trading dates."""

    if _safe_number(value) is not None:
        return deepcopy(value)
    if isinstance(value, str):
        if value in _SCHEDULE_TEXT:
            return _SCHEDULE_TEXT[value]
        monthly = re.fullmatch(r"monthly_(\d+)(?:st|nd|rd|th)", value)
        if monthly:
            return f"每月第{int(monthly[1])}个交易日"
    return _text(value)


def _present_parameter_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Use the same names, units and value encoding as all three module pages.

    Metadata changes display only. In particular, a decimal ratio and a
    premium already encoded as percent of S0=100 must never share scaling.
    """

    keyed = [(str(row.get("en") or row.get("symbol") or row.get("cn") or "").strip(), row) for row in rows]
    terms = {key: row.get("value") for key, row in keyed if key}
    catalog = load_term_catalog()
    fields = {field["key"]: field for field in build_term_fields(terms, catalog)["contract_fields"]}
    result = []
    for key, raw in keyed:
        field = fields.get(key, {})
        if key in _PUBLIC_EXCLUDED_TERM_SYMBOLS or field.get("editability") == INTERNAL_SCALE:
            continue
        value = _term_display_value(raw.get("value"))
        if value is None or value == "":
            continue
        row = deepcopy(dict(raw))
        label = field.get("label", key)
        if key not in catalog and label == key:
            label = raw.get("cn") or key
        row.update({"cn": label, "en": key,
                    "symbol": field.get("symbol", raw.get("symbol") or ""), "value": value})
        encoding = field.get("value_encoding")
        if encoding == "percentage_input_decimal_internal":
            row["value_format"] = "percent"
        elif encoding in {"percent_of_s0_100", "percentage_points_internal"}:
            row["value_format"] = "percent_points"
        display_unit = field.get("display_unit")
        if display_unit and display_unit != "%":
            row["value_suffix"] = "年" if encoding == "year_month_calendar_day_to_act365" else display_unit
        if encoding == "year_month_calendar_day_to_act365":
            # Keep the source number and its year unit; only the visible text
            # uses exact whole months/days, consistently with key terms.
            amount, unit = _tenor_display_parts(value)
            row["value_display"] = amount + unit
        result.append(row)
    return result


def _parameter_rows(contract: Mapping[str, Any] | None, *, names: set[str] | None = None, exclude: set[str] | None = None) -> list[dict[str, Any]]:
    if not isinstance(contract, Mapping):
        return []
    terms = contract.get("terms") if isinstance(contract.get("terms"), Mapping) else {}
    term_sources = contract.get("term_sources") if isinstance(contract.get("term_sources"), Mapping) else {}
    source_map = {"default": "template_default", "override": "user_override", "market": "market_fixing", "schedule": "schedule_derived"}
    rows = []
    for symbol, value in terms.items():
        if symbol in _PUBLIC_EXCLUDED_TERM_SYMBOLS:
            continue
        if names is not None and symbol not in names:
            continue
        if exclude is not None and symbol in exclude:
            continue
        source = str(term_sources.get(symbol, "user_selection")).lower()
        rows.append({
            "en": str(symbol), "value": deepcopy(value),
            "source": source_map.get(source, source if source in {"template_default", "user_override", "user_selection", "market_fixing", "schedule_derived"} else "user_selection"),
        })
    return _present_parameter_rows(rows)


def _parameter_groups(contract: Mapping[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    module_only = set().union(*_MODULE_ONLY_TERMS.values())
    result = {"common_input": _parameter_rows(contract, exclude=module_only)}
    result.update({name: _parameter_rows(contract, names=terms) for name, terms in _MODULE_ONLY_TERMS.items()})
    return result


def normalize_parameter_groups(value: Any) -> dict[str, list[dict[str, Any]]]:
    """Apply the public parameter label and symbol projection to frozen facts."""

    if not isinstance(value, Mapping):
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    for group, raw_rows in value.items():
        rows = [row for row in raw_rows if isinstance(row, Mapping) and row.get("cn") != "名义本金"] if isinstance(raw_rows, list) else []
        result[str(group)] = _present_parameter_rows(rows)
    return result


def _module_summary(name: str, module: Mapping[str, Any]) -> dict[str, Any]:
    record = module.get("run") if isinstance(module.get("run"), Mapping) else {}
    public_run_fields = {
        "module", "task_id", "run_id", "analysis_case_id", "candidate_id",
        "product_id", "rule_revision",
    }
    result = {
        "module": name,
        "status": str(module.get("status", "not_run")),
        "reason": _text(module.get("note")),
        "run_ref": {
            "module": getattr(module.get("ref"), "module", None),
            "tenant_id": getattr(module.get("ref"), "tenant_id", None),
            "task_id": getattr(module.get("ref"), "task_id", None),
            "run_id": getattr(module.get("ref"), "run_id", None),
        } if module.get("ref") is not None else None,
        "run": {
            key: deepcopy(value)
            for key, value in record.items()
            if key in public_run_fields
        },
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
        "pricing": _pricing_content(modules["pricing"], contract),
        "backtest": _backtest_content(modules["backtest"], contract),
    }
    parameter_groups = _parameter_groups(contract)
    if not candidate.get("key_terms"):
        candidate["key_terms"] = [
            {"label": row["cn"], "value": row["value"], "note": "已确认合同条款"}
            for row in parameter_groups["common_input"][:7]
        ]
    source_refs = {
        "recommendation_source": {
            key: value
            for key, value in recommender.items()
            if key in {"source", "run_id", "status"}
        },
    }
    selected_compute = [name for name in MODULE_TO_RUN if name in request.selected_modules]
    complete_statuses = [str(module_content[name].get("status")) for name in selected_compute]
    if selected_compute and all(status == "ready" for status in complete_statuses):
        evidence_status = "verified"
    else:
        evidence_status = "partial"
    module_summaries = {name: _module_summary(name, modules[name]) for name in MODULE_TO_RUN}
    unit = {
        "schema": SCHEMA_REPORT_UNIT,
        "unit_type": "ContractReportUnit",
        "tenant_id": request.tenant_id,
        "task_id": request.task_id,
        "analysis_case_id": request.analysis_case_id,
        "subject": {
            "candidate_id": candidate["candidate_id"],
            "product_id": candidate.get("product_id"),
            "rule_revision": candidate.get("rule_revision"),
            "product_name": candidate.get("product_name"),
            "underlyings": candidate.get("underlyings", []),
        },
        "product_dependency": {
            "product_id": candidate.get("product_id"),
            "rule_revision": candidate.get("rule_revision"),
        },
        "candidate": candidate,
        "contract": contract,
        "evidence_status": evidence_status,
        "selected_modules": list(request.selected_modules),
        "modules": module_summaries,
        "content": {
            "recommendation": candidate,
            "contract_highlights": _contract_highlights(contract),
            **module_content,
            "parameters": parameter_groups,
            **({"quote_fact": deepcopy(candidate_evidence["quote_fact"])} if isinstance(candidate_evidence.get("quote_fact"), Mapping) else {}),
            **({"supplemental_sections": deepcopy(candidate["supplemental_sections"])} if isinstance(candidate.get("supplemental_sections"), list) and candidate["supplemental_sections"] else {}),
            "audit": {
                "recommender": dict(recommender),
                "source_refs": source_refs,
                "limitations": [
                    *([] if selected_compute else ["本次未包含收益、估值或历史回测的已验证计算结果，不能作为完整量化结论。"]),
                    *[
                        limitation
                        for module_value in module_content.values()
                        for limitation in (module_value.get("limitations", []) if isinstance(module_value.get("limitations"), list) else [])
                        if isinstance(limitation, str) and limitation
                    ],
                ],
            },
            "risk": {"items": _risk_items(candidate), "disclaimer": DISCLAIMER},
        },
    }
    unit["semantic_fact_hash"] = stable_hash(unit)
    return unit


def build_report_units(request: ReportRequest, evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """按一合同一单位原则建立冻结单位。"""

    if request.output_type == "quote":
        quote_evidence = evidence.get("quote_evidence")
        if not isinstance(quote_evidence, list):
            raise ReporterError("Quote证据解析结果缺少quote_evidence")
        recommender = evidence.get("recommender") if isinstance(evidence.get("recommender"), Mapping) else {}
        units: list[dict[str, Any]] = []
        for item in quote_evidence:
            if not isinstance(item, Mapping):
                raise ReporterError("Quote含无效合同快照")
            unit = _contract_report_unit(request, item, recommender)
            unit["selected_modules"] = [str(item.get("quote_module"))]
            unit["evidence_status"] = "verified"
            unit.pop("semantic_fact_hash", None)
            unit["semantic_fact_hash"] = stable_hash(unit)
            units.append(unit)
        return units
    candidate_evidence = evidence.get("candidate_evidence")
    if not isinstance(candidate_evidence, Mapping):
        raise ReporterError("证据解析结果缺少candidate_evidence")
    recommender = evidence.get("recommender") if isinstance(evidence.get("recommender"), Mapping) else {}
    return [_contract_report_unit(request, candidate_evidence[candidate_id], recommender) for candidate_id in request.candidate_ids]


def build_report_document(request: ReportRequest, units: list[Mapping[str, Any]]) -> dict[str, Any]:
    """把一或多个冻结单位封装为唯一ReportUnit事实来源。"""

    if not units:
        raise ReporterError("没有可冻结的ReportUnit")
    if request.output_type != "quote" and request.delivery_mode == "single":
        document = deepcopy(dict(units[0]))
    else:
        document = {
            "schema": SCHEMA_REPORT_BUNDLE,
            "unit_type": "ReportBundle",
            "tenant_id": request.tenant_id,
            "task_id": request.task_id,
            "analysis_case_id": request.analysis_case_id,
            "report_units": [deepcopy(dict(unit)) for unit in units],
        }
        document["semantic_fact_hash"] = stable_hash(document)
    return document


__all__ = [
    "DISCLAIMER", "PUBLIC_NUMERIC_UNITS", "build_report_document", "build_report_units",
    "normalize_parameter_groups", "public_chart_specs",
]
