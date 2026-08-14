#!/usr/bin/env python3
"""Render one modular, offline OptionHelper HTML research report from JSON."""

from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import html
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Sequence

from .components import render_formula, render_inline_formula
from .config import DesignerConfig, load_designer_config
from .design_system_builder import build_design_system
from .design_tokens import DESIGN_SYSTEM_ID, TOKENS


SECTION_ORDER = ("conclusion", "recommendation", "parameters", "payoff", "pricing", "backtest", "risk")
SECTION_TITLES = {
    "conclusion": "核心结论",
    "recommendation": "结构推荐",
    "parameters": "合同参数",
    "payoff": "收益结构",
    "pricing": "估值定价",
    "backtest": "历史回测",
    "risk": "风险提示",
}
PUBLIC_BRAND = "光大证券 金融创新业务总部"
STATUS_LABELS = {name: str(state["label"]) for name, state in TOKENS.states.items()}
CHART_TYPES = {"line", "bar", "heatmap"}
GREEK_ORDER = ("Delta", "Gamma", "Vega", "Theta", "Rho")
_GREEK_KEY = {name.casefold(): name for name in GREEK_ORDER}
_PUBLIC_VALUE_STATUS = {
    "not_applicable": "不适用",
    "unsupported": "不适用",
}
PARAMETER_SOURCES = {"template_default", "user_override", "user_selection", "market_fixing", "schedule_derived"}
PARAMETER_SOURCE_LABELS = {
    "template_default": "模板默认",
    "user_override": "用户覆盖",
    "user_selection": "用户选择",
    "market_fixing": "市场定盘",
    "schedule_derived": "合约日程推导",
}
ASSET_MODES = {"shared", "portable"}
_NUMBER_TEXT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_MATH_SUBSCRIPT_TOKEN = re.compile(
    r"[A-Za-zΠπ]+_[A-Za-z0-9]+|(?<![A-Za-z0-9_])(?:K[12]|S0)(?![A-Za-z0-9_])"
)
_MATH_EXPRESSION = re.compile(r"[A-Za-zΠπ]+(?:_[A-Za-z0-9]+)?|\d+(?:\.\d+)?|<=|>=|!=|[+\-×*/=(),<>]")
_SUPERSCRIPT_DIGITS = str.maketrans("-0123456789", "⁻⁰¹²³⁴⁵⁶⁷⁸⁹")
_PUBLIC_VALUE_TEXT = {
    "cny": "人民币",
    "rmb": "人民币",
    "points": "点",
    "point": "点",
    "cny_per_spot": "人民币/标的价格点",
    "cny_per_spot_squared": "人民币/标的价格点²",
    "cny_per_1pct_volatility": "人民币/波动率变化1个百分点",
    "cny_per_1pct_vol": "人民币/波动率变化1个百分点",
    "cny_per_volatility_point": "人民币/波动率点",
    "cny_per_year": "人民币/年",
    "cny_per_calendar_day": "人民币/日",
    "cny_per_1pct_rate": "人民币/利率变化1个百分点",
    "monthly": "每月",
    "weekly": "每周",
    "daily": "每日",
}


def _number_text(value: int | float | str) -> str:
    """Use one public number notation in every reader-facing surface."""

    numeric = float(value)
    if numeric == 0:
        return "0"
    if round(numeric, 2) == 0:
        mantissa, exponent = f"{numeric:.2e}".split("e")
        return f"{mantissa.rstrip('0').rstrip('.')}×10{str(int(exponent)).translate(_SUPERSCRIPT_DIGITS)}"
    rendered = f"{numeric:,.2f}".rstrip("0").rstrip(".")
    return "0" if rendered in {"-0", "-0.0"} else rendered


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _number_text(value)
    if isinstance(value, str):
        known = _PUBLIC_VALUE_TEXT.get(value.casefold())
        if known:
            return known
        # Frozen strings may be security identifiers, instrument codes or
        # already-public dates.  Never infer a numeric type from their shape:
        # for example, ``000300`` must not become ``300`` in a contract table.
        return value
    return str(value)


def display_text(value: Any, value_format: Any = "number") -> str:
    """Format public numeric facts while preserving identifiers and formulas.

    Values arrive as frozen facts.  The formatter only affects reader-facing
    number notation: two decimal places at most, grouped thousands and
    meaningful percent signs.  Strings are intentionally left unchanged so
    dates, codes, formulas and already public labels are never reinterpreted.
    """

    status = text(value).casefold() if isinstance(value, str) else ""
    if status in _PUBLIC_VALUE_STATUS:
        return _PUBLIC_VALUE_STATUS[status]
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return text(value)
    numeric = float(value)
    if str(value_format or "number").casefold() == "percent":
        numeric *= 100
        suffix = "%"
    else:
        suffix = ""
    return _number_text(numeric) + suffix


def _cell_text(row: dict[str, Any], key: str) -> str:
    """Format a table cell without treating dates, codes or formulas as numbers."""

    value = row.get(key)
    if key in {"date", "year", "code", "symbol", "formula"}:
        # Identifiers retain their source notation: a calendar year is
        # ``2025``, never ``2,025``; a leading-zero code must likewise stay
        # intact.  Only display metrics receive grouped numeric formatting.
        return "" if value is None else str(value)
    value_format = row.get(f"{key}_format")
    if value_format is None and key == "value":
        value_format = row.get("value_format")
    return display_text(value, value_format or "number")


def esc(value: Any) -> str:
    return html.escape(text(value), quote=True)


def esc_rendered(value: Any) -> str:
    """Escape presentation text without running numeric formatting twice."""

    return html.escape("" if value is None else str(value), quote=True)


def _is_formula_expression(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    tokens = "".join(_MATH_EXPRESSION.findall(compact))
    return bool(compact and "_" in compact and tokens.replace("×", "*") == compact.replace("×", "*"))


def rich_text(value: Any) -> str:
    """Escape prose while replacing compact subscript notation with MathML."""

    source = text(value)
    if not source:
        return ""
    if _is_formula_expression(source):
        return render_inline_formula(source)
    pieces: list[str] = []
    cursor = 0
    for match in _MATH_SUBSCRIPT_TOKEN.finditer(source):
        pieces.append(html.escape(source[cursor:match.start()], quote=True))
        token = match.group(0)
        if "_" not in token and token in {"K1", "K2", "S0"}:
            token = f"{token[0]}_{token[1:]}"
        pieces.append(render_inline_formula(token))
        cursor = match.end()
    pieces.append(html.escape(source[cursor:], quote=True))
    return "".join(pieces)


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def module_headline(value: Any, section: str) -> str:
    """Avoid repeating the current section heading inside its own content."""

    headline = text(value)
    return "" if headline == SECTION_TITLES[section] else headline


def parameter_key(row: dict[str, Any]) -> str:
    return text(row.get("symbol") or row.get("en") or row.get("cn")).strip()


def validate_chart(raw_spec: Any, location: str) -> None:
    spec = as_dict(raw_spec)
    required = ("title", "x_axis_name", "y_axis_name", "source_note")
    missing = [key for key in required if not text(spec.get(key))]
    if missing:
        raise ValueError(f"{location}缺少图表口径：{', '.join(missing)}")
    chart_type = text(spec.get("type") or "line").lower()
    if chart_type not in CHART_TYPES:
        raise ValueError(f"{location}图表类型仅支持：{', '.join(sorted(CHART_TYPES))}")
    x_values = as_list(spec.get("x"))
    if chart_type == "heatmap":
        missing = [key for key in ("z_axis_name",) if not text(spec.get(key))]
        if missing:
            raise ValueError(f"{location}缺少热力图口径：{', '.join(missing)}")
        y_values = as_list(spec.get("y"))
        cells = as_list(spec.get("data"))
        if not x_values or not y_values or not cells:
            raise ValueError(f"{location}热力图必须提供横轴、纵轴和二维数据。")
        for position, raw_cell in enumerate(cells, start=1):
            if not isinstance(raw_cell, list) or len(raw_cell) != 3:
                raise ValueError(f"{location}第{position}个热力图数据点必须为[x索引,y索引,数值]。")
            x_index, y_index, _ = raw_cell
            if not isinstance(x_index, int) or not isinstance(y_index, int) or not (0 <= x_index < len(x_values)) or not (0 <= y_index < len(y_values)):
                raise ValueError(f"{location}第{position}个热力图数据点索引超出坐标范围。")
        return
    series = as_list(spec.get("series"))
    if not x_values or not series:
        raise ValueError(f"{location}必须提供横轴和序列。")
    for position, raw_series in enumerate(series, start=1):
        item = as_dict(raw_series)
        if not text(item.get("name")) or not isinstance(item.get("data"), list):
            raise ValueError(f"{location}第{position}个序列缺少名称或数据。")
        if len(item["data"]) != len(x_values):
            raise ValueError(f"{location}第{position}个序列长度与横轴不一致。")


def validate_payload(payload: dict[str, Any]) -> None:
    highlights = as_list(payload.get("contract_highlights"))
    if len(highlights) > 6:
        raise ValueError("contract_highlights最多展示6项关键条款。")
    for position, raw in enumerate(highlights, start=1):
        item = as_dict(raw)
        if not text(item.get("label")) or not text(item.get("value")):
            raise ValueError(f"contract_highlights[{position}]必须包含条款名称和取值。")
    for module_name in ("payoff", "pricing", "backtest"):
        module = as_dict(payload.get(module_name))
        status = text(module.get("status") or "pending").lower()
        if status not in STATUS_LABELS:
            raise ValueError(f"{module_name}的status不支持：{status}")

    for module_name in ("pricing", "backtest"):
        module = as_dict(payload.get(module_name))
        if text(module.get("status") or "pending").lower() == "ready":
            for position, raw_chart in enumerate(as_list(module.get("charts")), start=1):
                validate_chart(raw_chart, f"{module_name}.charts[{position}]")

    parameters = as_dict(payload.get("parameters"))
    parameter_groups = {
        key: [as_dict(row) for row in as_list(parameters.get(key)) if as_dict(row)]
        for key in ("common_input", "payoff_input", "pricing_input", "backtest_input")
    }
    for group_name, rows in parameter_groups.items():
        for position, row in enumerate(rows, start=1):
            if text(row.get("source")) not in PARAMETER_SOURCES:
                raise ValueError(f"{group_name}[{position}]的source不符合约定。")
    # The public report keeps shared contract terms once and only retains
    # genuinely module-specific rows in the three module groups. Preserve the
    # subset invariant for inputs without the shared contract group.
    if not parameter_groups["common_input"]:
        payoff_keys = {parameter_key(row) for row in parameter_groups["payoff_input"] if parameter_key(row)}
        for group_name in ("pricing_input", "backtest_input"):
            group_keys = {parameter_key(row) for row in parameter_groups[group_name] if parameter_key(row)}
            if payoff_keys and group_keys and not payoff_keys.issubset(group_keys):
                missing = ", ".join(sorted(payoff_keys.difference(group_keys)))
                raise ValueError(f"PayoffInput不是{group_name}的子集，缺少：{missing}")


def status_box(module: dict[str, Any]) -> str:
    status = text(module.get("status") or "pending").lower()
    note = text(module.get("note"))
    if not note:
        return ""
    label = STATUS_LABELS.get(status, "")
    return (
        f'<div class="module-state" data-status="{esc(status)}" role="status">'
        f'<strong>{esc(label)}：</strong><span>{esc(note)}</span>'
        '</div>'
    )


def metric_strip(rows: list[Any]) -> str:
    items = []
    for row in rows:
        item = as_dict(row)
        label = text(item.get("label"))
        value = display_text(item.get("value"), item.get("value_format"))
        if not label or not value:
            continue
        note = text(item.get("note"))
        note_html = f'<div class="metric__note">{esc(note)}</div>' if note else ""
        items.append(
            '<div class="metric">'
            f'<div class="metric__value">{esc_rendered(value)}</div>'
            f'<div class="metric__label">{esc(label)}</div>'
            f'{note_html}'
            '</div>'
        )
    return f'<div class="metric-strip">{"".join(items)}</div>' if items else ""


def item_list(items: list[Any], class_name: str = "plain-list") -> str:
    rows = [f"<li>{rich_text(item)}</li>" for item in items if text(item)]
    return f'<ul class="{class_name}">{"".join(rows)}</ul>' if rows else ""


def parameter_table(rows: list[Any], caption: str) -> str:
    table_rows = []
    for row in rows:
        item = as_dict(row)
        if not item:
            continue
        source = text(item.get("source"))
        symbol = text(item.get("symbol"))
        value = _cell_text(item, "value")
        table_rows.append(
            "<tr>"
            f"<td>{esc(item.get('cn'))}</td>"
            f"<td class=\"symbol\">{render_inline_formula(symbol) if symbol else '-'}</td>"
            f"<td>{rich_text(value)}</td>"
            f"<td>{esc(PARAMETER_SOURCE_LABELS.get(source, source))}</td>"
            "</tr>"
        )
    if not table_rows:
        return ""
    return (
        '<div class="table-wrap table-wrap--parameters">'
        f'<table class="parameter-table"><colgroup>'
        '<col class="parameter-table__cn"><col class="parameter-table__symbol">'
        '<col class="parameter-table__value"><col class="parameter-table__source"></colgroup><caption>' + esc(caption) + "</caption>"
        '<thead><tr><th scope="col">条款</th><th scope="col">符号</th>'
        '<th scope="col">取值</th><th scope="col">来源</th></tr></thead>'
        f"<tbody>{''.join(table_rows)}</tbody></table></div>"
    )


def simple_table(rows: list[Any], columns: list[tuple[str, str]], caption: str = "", table_class: str = "result-table") -> str:
    rendered = []
    for row in rows:
        item = as_dict(row)
        if item:
            cells = []
            for key, _ in columns:
                value = _cell_text(item, key)
                # Calendar and identifier columns must keep their literal
                # source notation.  Passing ``2025`` through rich_text would
                # legitimately apply numeric grouping and produce ``2,025``.
                cell = esc_rendered(value) if key in {"date", "year", "code"} else rich_text(value)
                cells.append(f"<td>{cell}</td>")
            rendered.append("<tr>" + "".join(cells) + "</tr>")
    if not rendered:
        return ""
    head = "".join(f'<th scope="col">{esc(title)}</th>' for _, title in columns)
    caption_html = f"<caption>{esc(caption)}</caption>" if caption else ""
    return (
        '<div class="table-wrap">'
        f'<table class="{esc(table_class)}">{caption_html}<thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(rendered)}</tbody></table></div>'
    )


def chart_data_table(spec: dict[str, Any], x_values: list[Any], series: list[dict[str, Any]]) -> str:
    """Provide the complete visible data table for every chart.

    Reports are a4 documents.  The tabular equivalent is therefore
    rendered in full below its chart rather than hidden behind an expander.
    """

    if text(spec.get("type")).lower() == "heatmap":
        y_values = as_list(spec.get("y"))
        cells = {
            (int(item[0]), int(item[1])): item[2]
            for item in as_list(spec.get("data"))
            if isinstance(item, list) and len(item) == 3
        }
        headings = "".join(f'<th scope="col">{esc_rendered(display_text(value))}</th>' for value in x_values)
        rows = []
        for y_index, y_value in enumerate(y_values):
            values = "".join(
                f"<td>{esc_rendered(display_text(cells.get((x_index, y_index)), spec.get('value_format')))}</td>"
                for x_index in range(len(x_values))
            )
            rows.append(f'<tr><th scope="row">{esc_rendered(display_text(y_value))}</th>{values}</tr>')
        title = text(spec.get("title") or "图表")
        return (
            '<div class="chart-data">'
            f'<div class="table-wrap">'
            f'<table><caption>{esc(title)}数据</caption><thead><tr><th scope="col">'
            f'{esc(spec.get("y_axis_name") or "纵轴")}/{esc(spec.get("x_axis_name") or "横轴")}</th>{headings}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></div>'
        )
    headings = "".join(f'<th scope="col">{esc(item.get("name"))}</th>' for item in series)
    rows = []
    for index, x_value in enumerate(x_values):
        values = "".join(f"<td>{esc_rendered(display_text(item['data'][index], spec.get('value_format')))}</td>" for item in series)
        rows.append(f'<tr><th scope="row">{esc_rendered(display_text(x_value))}</th>{values}</tr>')
    title = text(spec.get("title") or "图表")
    return (
        '<div class="chart-data">'
        f'<div class="table-wrap">'
        f'<table><caption>{esc(title)}数据</caption><thead><tr><th scope="col">'
        f'{esc(spec.get("x_axis_name") or "横轴")}</th>{headings}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div></div>'
    )


def add_charts(charts: list[dict[str, Any]], section: str, specs: list[Any]) -> str:
    figures = []
    for position, raw_spec in enumerate(specs, start=1):
        spec = as_dict(raw_spec)
        x_values = as_list(spec.get("x"))
        chart_type = text(spec.get("type") or "line").lower()
        series = [as_dict(item) for item in as_list(spec.get("series")) if as_dict(item).get("name")]
        heatmap = chart_type == "heatmap"
        if not x_values or (not heatmap and not series):
            continue
        chart_id = text(spec.get("id")) or f"{section}-chart-{len(charts) + position}"
        clean_id = "".join(char if char.isalnum() or char in "-_" else "-" for char in chart_id)
        existing_ids = {item["id"] for item in charts}
        if clean_id in existing_ids:
            clean_id = f"{clean_id}-{len(charts) + 1}"
        chart = {
            "id": clean_id,
            "title": text(spec.get("title")),
            "type": chart_type,
            "x": deepcopy(x_values),
            "x_axis_name": text(spec.get("x_axis_name")),
            "y_axis_name": text(spec.get("y_axis_name")),
            "value_format": text(spec.get("value_format") or "number"),
            "value_suffix": text(spec.get("value_suffix")),
            "source_note": text(spec.get("source_note")),
            "accessibility_summary": text(spec.get("accessibility_summary")),
        }
        if heatmap:
            chart.update({
                "y": deepcopy(as_list(spec.get("y"))),
                "data": [list(item) for item in as_list(spec.get("data")) if isinstance(item, list) and len(item) == 3],
                "z_axis_name": text(spec.get("z_axis_name")),
            })
        else:
            chart["series"] = [
                {
                    "name": text(item.get("name")),
                    "data": item.get("data") if isinstance(item.get("data"), list) else [],
                }
                for item in series
            ]
        charts.append(chart)
        source_note = text(spec.get("source_note"))
        source_html = f'<p class="source-note">{esc(source_note)}</p>' if source_note else ""
        summary = text(spec.get("accessibility_summary")) or (
            f'{text(spec.get("title") or "图表")}，横轴为{text(spec.get("x_axis_name"))}，'
            f'纵轴为{text(spec.get("y_axis_name"))}'
            + (f'，数值为{text(spec.get("z_axis_name"))}。' if heatmap else "。")
        )
        summary_id = f"{clean_id}-summary"
        figures.append(
            '<figure class="chart-figure">'
            f'<figcaption>{esc(spec.get("title") or "图表")}</figcaption>'
            f'<div class="chart" id="{esc(clean_id)}" role="img" '
            f'aria-label="{esc(spec.get("title") or "图表")}" aria-describedby="{esc(summary_id)}"></div>'
            f'<p id="{esc(summary_id)}" class="chart-summary">{esc(summary)}</p>'
            '<noscript><p class="chart-error">浏览器已禁用JavaScript，请使用下方数据表读取图表数据。</p></noscript>'
            f'{chart_data_table(spec, x_values, series)}'
            f'{source_html}'
            "</figure>"
        )
    return "".join(figures)


def embedded_svg(svg_path: Any, input_dir: Path) -> str:
    """Embed only a Reporter-provided, report-only Payoffer figure."""

    candidate = text(svg_path)
    if not candidate:
        return ""
    path = Path(candidate)
    if path.is_absolute() or ".." in path.parts:
        return ""
    root = input_dir.resolve()
    unresolved = root / path
    if unresolved.is_symlink():
        return ""
    path = unresolved.resolve()
    if path != root and root not in path.parents:
        return ""
    if not path.is_file() or path.suffix.lower() != ".svg":
        return ""
    payload = path.read_bytes()
    if (
        b"data-report-figure-profile" not in payload
        or re.search(rb"<\s*(?:title|desc)\b", payload, re.IGNORECASE)
        or re.search(rb"optionhelper", payload, re.IGNORECASE)
    ):
        return ""
    encoded = base64.b64encode(payload).decode("ascii")
    return f'<figure class="payoff-figure"><img src="data:image/svg+xml;base64,{encoded}" alt="本次参数化收益图"></figure>'


def formula_block(module: dict[str, Any]) -> str:
    """Render a supplied formula as MathML without evaluating or rewriting it."""

    formula_mathml = text(module.get("formula_mathml"))
    formula = text(module.get("formula"))
    if not formula_mathml and not formula:
        return ""
    return render_formula(formula=formula or formula_mathml, formula_mathml=formula_mathml)


def canonical_greeks(rows: list[Any]) -> list[dict[str, Any]]:
    """Render the five core sensitivities in the contract's fixed reader order."""

    source: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = as_dict(raw)
        label = _GREEK_KEY.get(text(row.get("label")).casefold())
        if label and label not in source:
            source[label] = row
    ordered: list[dict[str, Any]] = []
    for label in GREEK_ORDER:
        if label not in source:
            continue
        row = dict(source[label])
        row["label"] = label
        status = text(row.get("status")).casefold()
        if row.get("value") is None or not text(row.get("value")):
            public_status = _PUBLIC_VALUE_STATUS.get(status, "")
            if not public_status:
                continue
            row["value"] = public_status
        ordered.append(row)
    return ordered


def detail_tables(tables: list[Any]) -> str:
    """Render Reporter-projected detail tables without inferring any metrics."""

    blocks: list[str] = []
    for raw in tables:
        table = as_dict(raw)
        title = text(table.get("title"))
        columns: list[tuple[str, str]] = []
        for item in as_list(table.get("columns")):
            if isinstance(item, (list, tuple)) and len(item) == 2:
                columns.append((text(item[0]), text(item[1])))
            elif isinstance(item, dict) and text(item.get("key")) and text(item.get("label")):
                columns.append((text(item["key"]), text(item["label"])))
        rows = as_list(table.get("rows"))
        if not title or not columns or not rows:
            continue
        blocks.append(f"<h3>{esc(title)}</h3>")
        blocks.append(simple_table(rows, columns, title, "result-table result-table--detail"))
    return "".join(block for block in blocks if block)


def render_recommendation(data: dict[str, Any]) -> str:
    module = as_dict(data.get("recommendation"))
    blocks = []
    headline = module_headline(module.get("headline"), "recommendation")
    if headline:
        blocks.append('<div class="recommendation-head">')
        blocks.append(f'<h3>{esc(headline)}</h3>')
        blocks.append("</div>")
    structure_name = text(module.get("structure_name"))
    underlyings = text(module.get("underlyings"))
    if structure_name or underlyings:
        identities = []
        if structure_name:
            identities.append(f'<div><dt>推荐结构</dt><dd>{esc(structure_name)}</dd></div>')
        if underlyings:
            identities.append(f'<div><dt>挂钩标的</dt><dd>{esc(underlyings)}</dd></div>')
        blocks.append(
            '<dl class="identity-ledger">'
            + "".join(identities)
            + "</dl>"
        )
    # This is the frozen recommendation rationale. Designer displays the
    # provided market-view reasoning without deriving or extending it.
    reason = text(module.get("reason"))
    if reason:
        blocks.append(f'<div class="recommendation-head"><p>{rich_text(reason)}</p></div>')
    suitable = item_list(as_list(module.get("suitable_for")))
    not_suitable = item_list(as_list(module.get("not_suitable_for")))
    tradeoffs = item_list(as_list(module.get("tradeoffs")))
    if suitable:
        blocks.append(f"<h3>适用条件</h3>{suitable}")
    if not_suitable:
        blocks.append(f"<h3>不适用情形</h3>{not_suitable}")
    if tradeoffs:
        blocks.append(f"<h3>主要权衡</h3>{tradeoffs}")
    scenarios = []
    for item in as_list(module.get("scenarios")):
        row = as_dict(item)
        title = text(row.get("title"))
        rule = text(row.get("rule"))
        if title or rule:
            scenarios.append(f'<div class="scenario"><h4>{esc(title)}</h4><p>{rich_text(rule)}</p></div>')
    if scenarios:
        blocks.append('<div class="scenario-grid">' + "".join(scenarios) + "</div>")
    alternatives = []
    for item in as_list(module.get("alternatives")):
        row = as_dict(item)
        title = text(row.get("title"))
        summary = text(row.get("summary"))
        tags = [f'<span>{esc(tag)}</span>' for tag in as_list(row.get("tags")) if text(tag)]
        if title or summary or tags:
            card = '<article class="alternative">' + f'<h3>{esc(title) if title else "备选结构"}</h3>'
            if summary:
                card += f'<p>{esc(summary)}</p>'
            if tags:
                card += '<div class="alternative__tags">' + "".join(tags) + "</div>"
            alternatives.append(card + "</article>")
    if alternatives:
        blocks.append('<div class="alternative-grid">' + "".join(alternatives) + "</div>")
    return "".join(block for block in blocks if block)


def render_payoff(data: dict[str, Any], input_dir: Path) -> str:
    module = as_dict(data.get("payoff"))
    if text(module.get("status") or "pending").lower() != "ready":
        return status_box(module)
    blocks = [embedded_svg(module.get("report_svg_path"), input_dir)]
    formula_html = formula_block(module)
    if formula_html:
        blocks.append(formula_html)
    scenarios = []
    for item in as_list(module.get("scenarios")):
        row = as_dict(item)
        if text(row.get("title")) or text(row.get("rule")):
            scenarios.append(f'<div class="scenario"><h3>{esc(row.get("title"))}</h3><p>{rich_text(row.get("rule"))}</p></div>')
    if scenarios:
        blocks.append('<div class="scenario-grid">' + "".join(scenarios) + "</div>")
    return "".join(block for block in blocks if block)


def render_pricing(data: dict[str, Any], charts: list[dict[str, Any]]) -> str:
    module = as_dict(data.get("pricing"))
    if text(module.get("status") or "pending").lower() != "ready":
        return status_box(module)
    blocks = []
    method = text(module.get("method"))
    valuation_date = text(module.get("valuation_date"))
    if method or valuation_date:
        ledger = []
        if method:
            ledger.append(f'<div><dt>估值方法</dt><dd>{esc(method)}</dd></div>')
        if valuation_date:
            ledger.append(f'<div><dt>估值日</dt><dd>{esc(valuation_date)}</dd></div>')
        blocks.append('<dl class="identity-ledger">' + "".join(ledger) + "</dl>")
    blocks.append(metric_strip(as_list(module.get("metrics"))))
    pricing_parameters = as_list(as_dict(data.get("parameters")).get("pricing_input"))
    if pricing_parameters:
        blocks.append("<h3>估值参数</h3>")
        blocks.append(parameter_table(pricing_parameters, "估值参数"))
    blocks.append(
        simple_table(
            canonical_greeks(as_list(module.get("greeks"))),
            [("label", "Greek"), ("value", "数值"), ("unit", "单位")],
            "Greeks",
            "result-table result-table--greeks",
        )
    )
    scenario_rows = as_list(module.get("scenario_rows"))
    if scenario_rows:
        blocks.append("<h3>定价情景</h3>")
        blocks.append(
            simple_table(
                scenario_rows,
                [("scenario", "情景"), ("spot", "标的价格"), ("time", "剩余期限"), ("pv", "现值")],
                "定价情景",
                "result-table result-table--scenarios",
            )
        )
    blocks.append(add_charts(charts, "pricing", as_list(module.get("charts"))))
    assumptions = item_list(as_list(module.get("assumptions")))
    if assumptions:
        blocks.append(f"<h3>估值假设</h3>{assumptions}")
    return "".join(block for block in blocks if block)


def render_backtest(data: dict[str, Any], charts: list[dict[str, Any]]) -> str:
    module = as_dict(data.get("backtest"))
    if text(module.get("status") or "pending").lower() != "ready":
        return status_box(module)
    blocks = []
    window = text(module.get("window"))
    entry_rule = text(module.get("entry_rule"))
    if window or entry_rule:
        ledger = []
        if window:
            ledger.append(f'<div><dt>样本区间</dt><dd>{esc(window)}</dd></div>')
        if entry_rule:
            ledger.append(f'<div><dt>入场规则</dt><dd>{esc(entry_rule)}</dd></div>')
        blocks.append('<dl class="identity-ledger">' + "".join(ledger) + "</dl>")
    blocks.append(metric_strip(as_list(module.get("metrics"))))
    backtest_parameters = as_list(as_dict(data.get("parameters")).get("backtest_input"))
    if backtest_parameters:
        blocks.append("<h3>回测参数</h3>")
        blocks.append(parameter_table(backtest_parameters, "回测参数"))
    card_metrics = as_list(module.get("card_metrics"))
    if card_metrics:
        blocks.append("<h3>产品专属统计</h3>")
        blocks.append(
            simple_table(
                card_metrics,
                [("label", "指标"), ("value", "统计值"), ("note", "口径")],
                "产品专属统计",
                "result-table result-table--specialized",
            )
        )
    blocks.append(add_charts(charts, "backtest", as_list(module.get("charts"))))
    blocks.append(
        simple_table(
            as_list(module.get("event_statistics")),
            [("label", "路径事件"), ("value", "统计值"), ("note", "口径")],
            "路径事件统计",
            "result-table result-table--events",
        )
    )
    blocks.append(detail_tables(as_list(module.get("detail_tables"))))
    blocks.append(item_list(as_list(module.get("limitations"))))
    return "".join(block for block in blocks if block)


def render_parameters(data: dict[str, Any]) -> str:
    module = as_dict(data.get("parameters"))
    seen: set[str] = set()

    def unique_rows(key: str) -> list[dict[str, Any]]:
        rows = []
        for raw in as_list(module.get(key)):
            row = as_dict(raw)
            marker = parameter_key(row) or json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            if row and marker not in seen:
                seen.add(marker)
                rows.append(row)
        return rows

    rows = [*unique_rows("common_input"), *unique_rows("payoff_input")]
    visible = parameter_table(rows, "合同参数")
    return visible


def render_risk(data: dict[str, Any]) -> str:
    module = as_dict(data.get("risk"))
    items = item_list(as_list(module.get("items")), "risk-list")
    disclaimer = text(module.get("disclaimer"))
    if not items and not disclaimer:
        return ""
    suitable = item_list(as_list(module.get("suitable_for")))
    not_suitable = item_list(as_list(module.get("not_suitable_for")))
    suitability_block = (f"<h3>适用条件</h3>{suitable}" if suitable else "") + (f"<h3>不适用情形</h3>{not_suitable}" if not_suitable else "")
    limitations = item_list(as_list(module.get("limitations")))
    limitation_block = f"<h3>数据与方法限制</h3>{limitations}" if limitations else ""
    disclaimer_block = f'<p class="disclaimer">{esc(disclaimer)}</p>' if disclaimer else ""
    return items + suitability_block + limitation_block + disclaimer_block


def render_conclusion(data: dict[str, Any]) -> str:
    """Render only the structured frozen digest supplied by Reporter."""

    module = as_dict(data.get("conclusion"))
    structure = text(module.get("structure_name"))
    underlyings = text(module.get("underlyings"))
    blocks: list[str] = ['<div class="conclusion-band">']
    if structure or underlyings:
        blocks.append('<p class="conclusion-band__label">推荐结论</p>')
        statement = f"推荐{structure}" if structure else ""
        if underlyings:
            statement += f"，挂钩{underlyings}"
        blocks.append(f"<p class=\"conclusion-band__statement\">{rich_text(statement + '。')}</p>")
    reasons = item_list(as_list(module.get("reasons")))
    if reasons:
        blocks.append(f"<h3>推荐理由</h3>{reasons}")
    valuation = [*as_list(module.get("valuation_summary")), *as_list(module.get("greeks_summary"))]
    if valuation:
        blocks.append("<h3>估值摘要</h3>" + metric_strip(valuation))
    backtest = as_list(module.get("backtest_summary"))
    if backtest:
        blocks.append("<h3>回测摘要</h3>" + metric_strip(backtest))
    risks = item_list(as_list(module.get("risk_summary")), "risk-list")
    if risks:
        blocks.append(f"<h3>风险边界</h3>{risks}")
    blocks.append("</div>")
    return "".join(blocks) if len(blocks) > 2 else ""


def load_report_theme(config: DesignerConfig | None = None) -> str:
    """Read report CSS through the single Designer runtime configuration."""

    return (config or load_designer_config()).read_report_theme()


def validate_generated_javascript(source: str) -> None:
    """Reject malformed offline chart bootstrap code before delivery."""

    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    quote = ""
    escaped = False
    for character in source:
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in {'"', "'", "`"}:
            quote = character
        elif character in "([{":
            stack.append(character)
        elif character in pairs:
            if not stack or stack.pop() != pairs[character]:
                raise ValueError("生成的图表JavaScript括号不匹配，已阻止交付。")
    if quote or stack:
        raise ValueError("生成的图表JavaScript结构不完整，已阻止交付。")
    node = shutil.which("node")
    if node is None:
        raise ValueError("当前环境缺少Node.js，无法完成图表JavaScript语法校验。")
    script = re.sub(r"^\s*<script>|</script>\s*$", "", source, flags=re.IGNORECASE)
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as stream:
        stream.write(script)
        candidate = Path(stream.name)
    try:
        checked = subprocess.run([node, "--check", str(candidate)], capture_output=True, text=True, check=False)
    finally:
        candidate.unlink(missing_ok=True)
    if checked.returncode:
        detail = checked.stderr.strip().splitlines()[-1] if checked.stderr.strip() else "未知语法错误"
        raise ValueError(f"生成的图表JavaScript语法无效，已阻止交付：{detail}")


def render_html(
    payload: dict[str, Any],
    input_dir: Path | None = None,
    echarts_path: str = "",
    *,
    design_system_id: str = DESIGN_SYSTEM_ID,
    config: DesignerConfig | None = None,
    section_definition: Sequence[tuple[str, str, str]] | None = None,
    template_shell: str = "report.html",
) -> str:
    validate_payload(payload)
    config = config or load_designer_config()
    input_dir = input_dir or Path.cwd()
    if not echarts_path:
        echarts_path = config.relative_echarts_path(input_dir)
    meta = as_dict(payload.get("meta"))
    # The seven chapter headings are immutable. The document title remains
    # the delivery title supplied by Reporter so a delivered report can be
    # recognised outside the system; use the institutional default only when
    # the request did not provide one.
    title = text(meta.get("title")) or "单个期权结构推荐报告"
    report_theme = load_report_theme(config)
    design_system = build_design_system()
    charts: list[dict[str, Any]] = []
    renderers = {
        "recommendation": lambda: render_recommendation(payload),
        "payoff": lambda: render_payoff(payload, input_dir),
        "pricing": lambda: render_pricing(payload, charts),
        "backtest": lambda: render_backtest(payload, charts),
        "risk": lambda: render_risk(payload),
        "conclusion": lambda: render_conclusion(payload),
        "parameters": lambda: render_parameters(payload),
    }
    section_definition = section_definition or tuple(
        (key, SECTION_TITLES[key], key) for key in SECTION_ORDER
    )
    sections = []
    for section_key, section_title, key in section_definition:
        body = renderers[key]()
        if not body:
            continue
        section_id = f"section-{section_key}"
        section_status = ""
        if key in {"payoff", "pricing", "backtest"}:
            section_status = text(as_dict(payload.get(key)).get("status") or "pending").lower()
        sections.append({
            "id": section_id,
            "title": section_title,
            "body": body,
            "status": section_status,
            "status_attr": f' data-status="{esc(section_status)}"' if section_status else "",
        })

    content = "".join(
        f'<section id="{item["id"]}" class="report-section"{item["status_attr"]}>'
        f'<h2><span class="section-title">{esc(item["title"])}</span></h2>{item["body"]}</section>'
        for item in sections
    )
    toc = (
        '<aside class="report-toc" aria-label="报告目录"><nav>'
        + "".join(
            f'<a href="#{esc(item["id"])}"><span>{position}.</span>{esc(item["title"])}</a>'
            for position, item in enumerate(sections, start=1)
        )
        + "</nav></aside>"
    )
    chart_json = json.dumps(charts, ensure_ascii=False).replace("</", "<\\/")
    report_identity = [("报告日期", meta.get("as_of_date"))]
    identity = "".join(
        f'<div><dt>{esc(label)}</dt><dd>{esc_rendered(value)}</dd></div>'
        for label, value in report_identity
        if text(value)
    )
    echarts_head = f'<script src="{esc(echarts_path)}" defer></script>' if charts else ""
    chart_script = "" if not charts else r"""<script>
	const chartSpecs=__CHARTS__;
	const chartInstances=[];
	const palette=__CHART_PALETTE__;
	const chartTheme=__CHART_THEME__;
	const lineTypes=chartTheme.lineTypes;
	const symbols=chartTheme.symbols;
	function markChartUnavailable(id,message){const el=document.getElementById(id);if(!el)return;el.classList.add("chart--unavailable");el.textContent=message||"图表资源加载失败，请使用下方完整数据表。";}
	function markChartsUnavailable(){chartSpecs.forEach(spec=>markChartUnavailable(spec.id));}
	function publicNumber(value,valueFormat){let number=Number(value);if(!Number.isFinite(number))return String(value??"");if(valueFormat==="percent")number*=100;if(number!==0&&Math.round(number*100)===0){const [mantissa,exponent]=number.toExponential(2).split("e");const superscript={"-":"⁻","0":"⁰","1":"¹","2":"²","3":"³","4":"⁴","5":"⁵","6":"⁶","7":"⁷","8":"⁸","9":"⁹"};return `${mantissa.replace(/0+$/,'').replace(/\.$/,'')}×10${[...String(Number(exponent))].map(char=>superscript[char]||char).join("")}${valueFormat==="percent"?"%":""}`;}return new Intl.NumberFormat("zh-CN",{maximumFractionDigits:2}).format(number)+(valueFormat==="percent"?"%":"");}
	function renderChart(spec){
	  const el=document.getElementById(spec.id);if(!el)return;
	  const isHeatmap=spec.type==="heatmap";const type=spec.type==="bar"?"bar":"line";
	  const points=spec.x.length;const series=Array.isArray(spec.series)?spec.series:[];
	  const hasLegend=!isHeatmap&&series.length>1;const chart=echarts.init(el,null,{renderer:"svg"});
	  if(isHeatmap){
	    chart.setOption({animation:false,color:palette,tooltip:{position:"top",formatter:item=>`${spec.x_axis_name||"横轴"}：${publicNumber(spec.x[item.data[0]],"number")}<br>${spec.y_axis_name||"纵轴"}：${publicNumber(spec.y[item.data[1]],"number")}<br>${spec.z_axis_name||"数值"}：${publicNumber(item.data[2],spec.value_format)}${spec.value_suffix||""}`},grid:{left:72,right:24,top:30,bottom:94},xAxis:{type:"category",name:spec.x_axis_name||"",nameLocation:"middle",nameGap:27,data:spec.x,axisLine:{lineStyle:{color:chartTheme.axis.line}},axisLabel:{color:chartTheme.axis.label,fontSize:chartTheme.textStyle.fontSize,formatter:value=>publicNumber(value,"number")}},yAxis:{type:"category",name:spec.y_axis_name||"",nameLocation:"middle",nameGap:48,data:spec.y,axisLine:{lineStyle:{color:chartTheme.axis.line}},axisLabel:{color:chartTheme.axis.label,fontSize:chartTheme.textStyle.fontSize,formatter:value=>publicNumber(value,"number")}},visualMap:{min:Math.min(...spec.data.map(item=>Number(item[2]))),max:Math.max(...spec.data.map(item=>Number(item[2]))),calculable:false,orient:"horizontal",left:"center",bottom:6,textStyle:{color:chartTheme.axis.label,fontSize:chartTheme.textStyle.fontSize},inRange:{color:[chartTheme.heatmap.low,palette[0],palette[1]]}},series:[{name:spec.z_axis_name||"数值",type:"heatmap",data:spec.data,label:{show:false},emphasis:{itemStyle:{shadowBlur:8}}}]});
	    chartInstances.push(chart);return;
	  }
	  chart.setOption({animation:false,color:palette,tooltip:{trigger:"axis",valueFormatter:value=>`${publicNumber(value,spec.value_format)}${spec.value_suffix||""}`},legend:{show:hasLegend,top:2,textStyle:{color:chartTheme.textStyle.color,fontFamily:chartTheme.textStyle.fontFamily,fontSize:chartTheme.textStyle.fontSize}},grid:{left:58,right:18,top:hasLegend?48:28,bottom:78},xAxis:{type:"category",name:spec.x_axis_name||"",nameLocation:"middle",nameGap:24,data:spec.x,axisLine:{lineStyle:{color:chartTheme.axis.line}},axisLabel:{color:chartTheme.axis.label,fontSize:chartTheme.textStyle.fontSize,interval:0,rotate:points>14?42:0,formatter:value=>publicNumber(value,"number")}},yAxis:{type:"value",name:spec.y_axis_name||"",nameTextStyle:{color:chartTheme.axis.label},axisLabel:{color:chartTheme.axis.label,fontSize:chartTheme.textStyle.fontSize,formatter:value=>publicNumber(value,spec.value_format)},splitLine:{lineStyle:{color:chartTheme.axis.split,type:"dashed"}}},series:series.map((item,seriesIndex)=>({name:item.name,type,smooth:false,symbol:type==="line"?symbols[seriesIndex%symbols.length]:"none",showSymbol:type==="line"&&points<=60,symbolSize:5,barMaxWidth:42,data:item.data,itemStyle:{color:palette[seriesIndex%palette.length]},lineStyle:{width:2,type:lineTypes[seriesIndex%lineTypes.length]}}))});
	  chartInstances.push(chart);
	}
	function initialiseCharts(){if(!window.echarts){markChartsUnavailable();return;}chartSpecs.forEach(spec=>{try{renderChart(spec);}catch(error){markChartUnavailable(spec.id,"图表初始化失败，请使用下方完整数据表。");console.error("图表初始化失败",spec.id,error);}});}
	window.addEventListener("DOMContentLoaded",initialiseCharts);let resizeTimer;window.addEventListener("resize",()=>{window.clearTimeout(resizeTimer);resizeTimer=window.setTimeout(()=>chartInstances.forEach(chart=>chart.resize()),150)});
</script>"""
    chart_script = (
        chart_script.replace("__CHARTS__", chart_json)
        .replace("__CHART_PALETTE__", json.dumps(list(design_system.tokens["chart_palette"])))
        .replace("__CHART_THEME__", json.dumps(design_system.echarts_theme, ensure_ascii=False))
    )
    if chart_script:
        validate_generated_javascript(chart_script)
    template = config.read_template(template_shell)
    return (
        template.replace("__TITLE__", esc(title))
        .replace("__ECHARTS_HEAD__", echarts_head)
        .replace("__REPORT_THEME__", report_theme)
        .replace("__BRAND__", esc(PUBLIC_BRAND))
        .replace("__DESIGN_SYSTEM_ID__", esc(design_system_id))
        .replace("__IDENTITY__", identity)
        .replace("__REPORT_TOC__", toc)
        .replace("__CONTENT__", content)
        .replace("__CHARTS__", chart_json)
        .replace("__CHART_PALETTE__", json.dumps(list(design_system.tokens["chart_palette"])))
        .replace("__CHART_THEME__", json.dumps(design_system.echarts_theme, ensure_ascii=False))
        .replace("__CHART_SCRIPT__", chart_script)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a modular OptionHelper HTML research report.")
    parser.add_argument("--input", required=True, type=Path, help="Report payload JSON path")
    parser.add_argument("--output", required=True, type=Path, help="Output filename, with or without .html")
    parser.add_argument("--output-type", choices=("card", "quote", "report"), default="report", help="Card, Quote or detailed Report")
    parser.add_argument("--format", choices=("html", "pdf"), help="Output format; .pdf also selects PDF")
    parser.add_argument(
        "--asset-mode",
        choices=tuple(sorted(ASSET_MODES)),
        default="shared",
        help="HTML asset mode: shared references the project ECharts file; portable copies it beside the HTML",
    )
    args = parser.parse_args()

    input_path = args.input.resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"找不到报告输入文件：{input_path}")
    with input_path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("报告输入必须是JSON对象。")
    requested_output = args.output.resolve()
    requested_format = args.format or ("pdf" if requested_output.suffix.lower() == ".pdf" else "html")
    if requested_format == "pdf" and requested_output.suffix.lower() != ".pdf":
        requested_output = requested_output.with_suffix(".pdf")
    output_path = (
        requested_output
        if requested_output.suffix.lower() == f".{requested_format}"
        else requested_output.with_suffix(f".{requested_format}")
    )
    asset_mode = args.asset_mode

    # Every CLI output uses the public handoff entry. Portable resources are
    # materialized only from the verified artifact manifest.
    from .design_renderer import materialize_portable_assets, render
    from .models import DesignerInput

    output_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = render(
        DesignerInput(
            payload=payload,
            output_type=args.output_type,
            format=requested_format,
            asset_mode=asset_mode,
            input_dir=input_path.parent,
            output_dir=output_path.parent,
        )
    )
    if requested_format == "pdf":
        output_path.write_bytes(artifact["pdf"])
        print(f"已生成PDF：{output_path}")
        return
    output_path.write_text(artifact["html"], encoding="utf-8")
    if asset_mode == "portable":
        materialize_portable_assets(artifact.get("portable_assets", []), output_path.parent)
    print(f"已生成HTML：{output_path}")


if __name__ == "__main__":
    main()
