"""将已验证证据冻结为ContractReportUnit。

本文件只组织上游已经给出的事实，不估值、不回测、不计算Payoff，也不重算合同
指纹。对组合交付，仍然是一合同一ContractReportUnit，外层只保存单位顺序。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from html import escape
import math
import re
from typing import Any, Mapping
from xml.etree import ElementTree

from runtime.knowledger.registry_loader import load_term_catalog

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

_LIMITATION_TEXT = {
    "exchange_calendar_not_exposed_by_data_source_weekday_validation_only": "数据源未提供可核验的交易日历，本次仅按工作日校验样本日期。",
    "no_nav_curve_is_generated": "本次回测按到期损益统计，未生成持有期净值曲线。",
    "payment_calendar_not_exposed_by_shared_contract_core": "合同事实未提供独立付款日历，相关日期按当前合同约定处理。",
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
_PUBLIC_EXCLUDED_TERM_SYMBOLS = {"N", "notional", "Pi_0", "P_net"}


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


def _public_limitation(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text in _LIMITATION_TEXT:
        return _LIMITATION_TEXT[text]
    if re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+){2,}", text):
        return "上游分析存在未公开说明的技术限制，相关结论仅在已列示证据范围内适用。"
    return text


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
        label = "期初收益构成" if timing == "0" else "到期收益构成" if timing == "T" else f"{timing}时点收益构成"
        replacement = f"{label}（{amount}）"
        text = text[:start] + replacement + text[end + 1:]
        cursor = start + len(replacement)
    return _math_text(text.replace("*", "×"))


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


def _highlight_row(symbol: str, value: Any, contract: Mapping[str, Any], catalog: Mapping[str, Any]) -> dict[str, str]:
    definition = catalog.get(symbol) if isinstance(catalog.get(symbol), Mapping) else {}
    label = _text(definition.get("name_zh")) or symbol
    unit = _text(definition.get("unit"))
    if symbol == "T":
        label, note = "期限", "年"
    elif symbol == "c":
        return {"label": "年化票息", "value": _percent_text(value), "note": "年化，ACT/365"}
    elif unit in {"rate", "volatility"}:
        return {"label": label, "value": _percent_text(value), "note": "比例" if unit == "rate" else "年化波动率"}
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
    for symbol, prefix in (("O_KO", "敲出"), ("O_KI", "敲入")):
        if symbol in terms:
            schedule = _SCHEDULE_TEXT.get(str(terms[symbol]), _text(terms[symbol]))
            schedules.append(f"{prefix}：{schedule}")
    if schedules:
        observation = {"label": "观察频率", "value": "；".join(schedules), "note": "仅交易日"}
        return [*rows[:5], observation]
    return rows[:6]


_BACKTEST_LABELS = {
    "sample_count": "样本数", "valid_return_sample_count": "有效收益样本数",
    "positive_return_count": "正收益样本数", "skipped_count": "跳过样本数", "win_rate": "胜率",
    "average_gross_return": "平均收益",
    "median_gross_return": "中位收益",
    "minimum_gross_return": "最差收益",
    "maximum_gross_return": "最佳收益",
    "max_loss_gross_return": "最大亏损",
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


def _specialized_metric_rows(backtest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Select up to four supplied, percentage-only profile facts.

    Backtester owns each profile calculation.  Reporter merely exposes a small
    declared subset of its percentage ratios, never derives a new statistic
    from trades, cashflows or raw event records.
    """

    specialized = backtest.get("specialized_metrics")
    if not isinstance(specialized, Mapping):
        return []
    labels = {
        "terminal_return_sign.positive_rate": "到期正表现占比",
        "terminal_return_sign.negative_rate": "到期负表现占比",
        "coupon_payment.payment_rate.average": "票息支付比例",
        "non_negative_terminal_rate": "非负到期表现占比",
        "in_range_observation_ratio.average": "区间内观察比例",
    }
    rows: list[dict[str, Any]] = []
    for path, label in labels.items():
        value: Any = specialized
        for part in path.split("."):
            value = value.get(part) if isinstance(value, Mapping) else None
        percent = _safe_ratio(value)
        if percent is not None:
            rows.append(_metric(label, percent, "Backtester产品专属统计", value_format="percent"))
    return rows[:4]
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
    value["valuation_date"] = _text(pricing.get("valuation_date") or market_snapshot.get("valuation_date"))
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
    value["charts"] = public_chart_specs(_safe_pricing_charts(pricing, result))
    value["scenario_rows"] = _public_pricing_scenarios(pricing, result)
    value["artifacts"] = _artifact_rows(module)
    return value


def _economic_convention_status(backtest: Mapping[str, Any]) -> str:
    """Accept only the Backtester public decimal-percentage convention."""

    convention = backtest.get("economic_convention")
    if not isinstance(convention, Mapping):
        return "invalid"
    if (
        convention.get("gross_return_basis") == "contract_cashflow_before_external_costs"
        and convention.get("gross_return_display_unit") == "percentage"
        and convention.get("gross_return_value_encoding") == "decimal_ratio"
        and convention.get("external_costs_modelled") is False
        and convention.get("client_net_pnl_status") == "not_modelled"
        and convention.get("client_net_return_status") == "not_modelled"
        and convention.get("win_rate_numerator") == "positive_gross_contract_return_count"
        and convention.get("win_rate_denominator") == "valid_return_sample_count"
    ):
        return "formal"
    return "invalid"


_FORMAL_LEDGER_RETURN_CONVENTION = {
    "basis": "contract_cashflow_before_external_costs",
    "display_unit": "percentage",
    "value_encoding": "decimal_ratio",
}
_FORMAL_LEDGER_ALLOWED_FIELDS = frozenset({
    "trade_id", "entry_date", "exit_date", "actual_calendar_days", "actual_time_years",
    "historical_resolved_contract", "entry_market_spots", "entry_normalized_spots",
    "settlement_market_spots", "underlying_performances", "path_id", "case_id",
    "settlement_type", "events", "gross_contract_return", "gross_return_convention",
    "client_net_return", "client_net_pnl", "terminal_performance", "entry_features",
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
        convention = row.get("gross_return_convention")
        if (
            _safe_number(row.get("gross_contract_return")) is None
            or not isinstance(convention, Mapping)
            or any(convention.get(key) != expected for key, expected in _FORMAL_LEDGER_RETURN_CONVENTION.items())
        ):
            return False
        for key in ("client_net_return", "client_net_pnl"):
            client_value = row.get(key)
            if (
                not isinstance(client_value, Mapping)
                or client_value.get("status") != "not_modelled"
                or client_value.get("value") is not None
            ):
                return False
    return True


_RETURN_PAIR_KEYS = (
    "average_gross_return",
    "median_gross_return",
    "minimum_gross_return",
    "maximum_gross_return",
    "max_loss_gross_return",
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

    sample_count = _safe_count(common.get("sample_count"))
    valid_count = _safe_count(common.get("valid_return_sample_count"))
    positive_count = _safe_count(common.get("positive_return_count"))
    win_rate = _safe_ratio(common.get("win_rate"))
    if (
        sample_count is None or valid_count is None or positive_count is None or win_rate is None
        or valid_count == 0 or positive_count > valid_count or valid_count > sample_count
        or not math.isclose(win_rate, positive_count / valid_count, rel_tol=0.0, abs_tol=1e-12)
        or not _valid_return_pairs(common)
    ):
        return None
    return {
        **deepcopy(dict(common)),
        "sample_count": sample_count,
        "valid_return_sample_count": valid_count,
        "positive_return_count": positive_count,
        "win_rate": win_rate,
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
    if key in {"sample_count", "valid_return_sample_count", "positive_return_count", "skipped_count"}:
        return "个有效入场样本"
    if key == "win_rate":
        denominator = f"分母为{valid_sample_count}个有效收益样本" if valid_sample_count is not None else "分母为回测结果所列有效收益样本"
        return f"正合约毛收益样本占比，{denominator}；客户净收益未建模，不代表未来获利概率"
    if key.endswith("_gross_return"):
        return "合约毛收益率；客户净收益未建模"
    if key.endswith("_rate"):
        return "占有效入场样本"
    if key.endswith("_days"):
        return "自然日"
    return "口径未提供"


def _backtest_content(module: Mapping[str, Any], contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
    del contract
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
    value["entry_rule"] = _text(config.get("entry_rule"))
    valid_sample_count = common["valid_return_sample_count"]
    public_keys = (
        "sample_count", "valid_return_sample_count", "positive_return_count", "win_rate",
        "average_gross_return", "median_gross_return", "minimum_gross_return",
        "maximum_gross_return", "max_loss_gross_return",
    )
    value["metrics"] = [
        _metric(
            _BACKTEST_LABELS[key], common[key],
            _backtest_metric_note(key, valid_sample_count=valid_sample_count),
            value_format=_public_value_format(key),
        )
        for key in public_keys if common.get(key) is not None
    ]

    detail_tables: list[dict[str, Any]] = []
    if value["metrics"]:
        detail_tables.append({"title": "公共回测统计", "columns": [("label", "指标"), ("value", "数值")], "rows": deepcopy(value["metrics"])})

    coverage_limitations: list[Any] = []
    branch = backtest.get("branch_coverage") if isinstance(backtest.get("branch_coverage"), Mapping) else {}
    declared = _safe_count(branch.get("declared_pair_count"))
    observed = _safe_count(branch.get("observed_pair_count"))
    uncovered = _safe_count(branch.get("uncovered_pair_count"))
    coverage_status = _text(branch.get("status")).lower()
    counts_are_valid = (
        all(item is not None for item in (declared, observed, uncovered))
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
        average_return = _public_return_percent(item, "average_gross_return")
        if average_return is not None:
            row.update({"average_gross_return": average_return, "average_gross_return_format": "percent"})
        outcome_rows.append(row)
    if outcome_rows:
        outcome_columns = [("label", "路径结果"), ("count", "样本数"), ("rate", "占比")]
        if any("average_gross_return" in row for row in outcome_rows):
            outcome_columns.append(("average_gross_return", "平均合约毛收益"))
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
        valid_win_rate, win_rate = _safe_optional(item, "win_rate", _safe_ratio)
        if item.get("year") is not None and valid_year and valid_samples and valid_win_rate:
            row = {
                "year": year, "sample_count": sample_count, "win_rate": win_rate, "win_rate_format": "percent",
            }
            average_return = _public_return_percent(item, "average_gross_return")
            if average_return is not None:
                row.update({"average_gross_return": average_return, "average_gross_return_format": "percent"})
            annual_rows.append(row)
    if annual_rows:
        annual_columns = [("year", "年份"), ("sample_count", "样本数"), ("win_rate", "胜率")]
        if any("average_gross_return" in row for row in annual_rows):
            annual_columns.append(("average_gross_return", "平均合约毛收益"))
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
    value["card_metrics"] = specialized_rows
    value["limitations"] = [
        "合约毛收益率按合同条款计算；客户净收益未建模，未扣除交易费、资金成本、税费、对冲及滑点。",
        *[item for item in coverage_limitations if isinstance(item, str)],
    ]
    value["charts"] = _safe_backtest_charts(backtest)
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
        if symbol in _PUBLIC_EXCLUDED_TERM_SYMBOLS:
            continue
        if names is not None and symbol not in names:
            continue
        if exclude is not None and symbol in exclude:
            continue
        source = str(term_sources.get(symbol, "user_selection")).lower()
        rows.append({
            "cn": _TERM_LABELS.get(str(symbol), str(symbol)), "en": str(symbol), "symbol": _TERM_SYMBOLS.get(str(symbol), ""),
            "value": deepcopy(value) if _safe_number(value) is not None else _text(value),
            "source": source_map.get(source, source if source in {"template_default", "user_override", "user_selection", "market_fixing", "schedule_derived"} else "user_selection"),
        })
    return rows


def _parameter_groups(contract: Mapping[str, Any] | None) -> dict[str, list[dict[str, str]]]:
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
        rows: list[dict[str, Any]] = []
        for raw in raw_rows if isinstance(raw_rows, list) else []:
            if not isinstance(raw, Mapping):
                continue
            row = deepcopy(dict(raw))
            key = str(row.get("en") or row.get("symbol") or row.get("cn") or "").strip()
            if key in _PUBLIC_EXCLUDED_TERM_SYMBOLS or str(row.get("cn") or "") == "名义本金":
                continue
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
    complete_compute = [name for name in MODULE_TO_RUN]
    complete_statuses = [str(module_content[name].get("status")) for name in complete_compute]
    if all(status == "ready" for status in complete_statuses):
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
        "subject": {"candidate_id": candidate["candidate_id"], "product_id": candidate.get("product_id"), "product_name": candidate.get("product_name"), "underlyings": candidate.get("underlyings", [])},
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
            "audit": {
                "recommender": dict(recommender),
                "source_refs": source_refs,
                "limitations": [
                    *([] if selected_compute else ["本次未包含收益、估值或历史回测的已验证计算结果，不能作为完整量化结论。"]),
                ],
            },
            "risk": {"items": _risk_items(candidate), "disclaimer": DISCLAIMER},
        },
    }
    unit["semantic_fact_hash"] = stable_hash(unit)
    return unit


def build_report_units(request: ReportRequest, evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """按一合同一单位原则建立冻结单位。"""

    candidate_evidence = evidence.get("candidate_evidence")
    if not isinstance(candidate_evidence, Mapping):
        raise ReporterError("证据解析结果缺少candidate_evidence")
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
            "schema": SCHEMA_REPORT_BUNDLE,
            "unit_type": "ReportBundle",
            "tenant_id": request.tenant_id,
            "task_id": request.task_id,
            "analysis_case_id": request.analysis_case_id,
            "report_units": [deepcopy(dict(unit)) for unit in units],
        }
        document["semantic_fact_hash"] = stable_hash(document)
    return document


__all__ = ["DISCLAIMER", "build_report_document", "build_report_units", "normalize_parameter_groups", "public_chart_specs"]
