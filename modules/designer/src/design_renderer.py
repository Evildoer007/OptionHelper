"""Public ``render(DesignerInput)`` entry point.

Reporter passes frozen content to this module. The renderer chooses Card or
Report and HTML or PDF; it never reads a module result directory or changes a
financial field.
"""

from __future__ import annotations

from copy import deepcopy
import base64
from hashlib import sha256
import html
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from .config import DesignerConfig, load_designer_config
from .comparison_renderer import render_multicard_html, render_multireport_html
from .design_system_builder import build_design_system
from .models import DESIGNER_ARTIFACT_MANIFEST_SCHEMA, DesignerInput
from .pdf_renderer import PdfRuntimeError, render_pdf
from .presentation_patch import apply_presentation_patch
from .template_definition import default_template_id, load_template_definition
from .renderer import (
    PUBLIC_BRAND,
    SECTION_ORDER,
    as_dict,
    as_list,
    backtest_summary_rows,
    canonical_greeks,
    display_basis,
    display_text,
    esc,
    esc_rendered,
    render_html,
    render_presentation_content,
    rich_text,
    table_colgroup,
    table_column_role,
    table_density,
    text,
    validate_payload,
)


class DesignerDependencyError(RuntimeError):
    """Raised when a requested output format lacks its optional converter."""


CARD_CONTENT_ORDER = ("recommendation", "reason", "contract_highlights", "pricing", "backtest", "risk")
CARD_DELIVERY_TITLE = "场外衍生品结构推荐卡片"
QUOTE_CONTENT_ORDER = ("reference_quote",)
QUOTE_DELIVERY_TITLE = "推荐结构及参考报价"
_QUOTE_COLUMN_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_QUOTE_VALUE_FORMATS = frozenset({"text", "number", "percent"})


def _normalise_input(value: DesignerInput | Mapping[str, Any]) -> DesignerInput:
    if isinstance(value, DesignerInput):
        return value
    if isinstance(value, Mapping):
        return DesignerInput.from_mapping(value)
    raise TypeError("render需要DesignerInput或Mapping。")


def _reference_quote(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return one validated, explicit reference-quote fact block.

    Quote delivery is intentionally unavailable without a frozen quote.  It
    must not infer a client-facing quote from valuation, payoff or backtest
    evidence, because those facts use different economic meanings.
    """

    quote = as_dict(payload.get("reference_quote"))
    quality_status = text(quote.get("quality_status")).lower()
    precision = text(quote.get("precision")).lower()
    precision_status = text(quote.get("precision_status")).lower()
    if (
        quote.get("is_demo") is True
        or quote.get("quote_eligible") is False
        or quality_status in {"demo", "unverified", "invalid", "failed", "low_precision"}
        or precision in {"demo", "low", "low_precision", "unverified"}
        or precision_status in {"demo_only", "not_assessed", "not_priced"}
    ):
        raise ValueError("Quote只能渲染Reporter已验证的正式报价事实。")
    groups = as_list(quote.get("groups"))
    if not quote or not groups:
        raise ValueError("Quote需要包含至少一组明确reference_quote报价事实。")
    for group_index, raw_group in enumerate(groups, start=1):
        group = as_dict(raw_group)
        if not text(group.get("title")):
            raise ValueError(f"reference_quote.groups[{group_index}]必须包含分组标题。")
        columns = as_list(group.get("columns"))
        rows = as_list(group.get("rows"))
        if not columns or not rows:
            raise ValueError(f"reference_quote.groups[{group_index}]必须同时包含columns和rows。")
        keys: list[str] = []
        for column_index, raw_column in enumerate(columns, start=1):
            column = as_dict(raw_column)
            key = text(column.get("key"))
            label = text(column.get("label"))
            value_format = text(column.get("format") or "text").lower()
            if not _QUOTE_COLUMN_KEY.fullmatch(key):
                raise ValueError(
                    f"reference_quote.groups[{group_index}].columns[{column_index}].key不符合约定。"
                )
            if not label:
                raise ValueError(
                    f"reference_quote.groups[{group_index}].columns[{column_index}]必须包含列标题。"
                )
            if value_format not in _QUOTE_VALUE_FORMATS:
                raise ValueError(
                    f"reference_quote.groups[{group_index}].columns[{column_index}].format不支持。"
                )
            if key in keys:
                raise ValueError(f"reference_quote.groups[{group_index}]存在重复列：{key}。")
            keys.append(key)
        for row_index, raw_row in enumerate(rows, start=1):
            row = as_dict(raw_row)
            for key in keys:
                value = row.get(key)
                if value is None or (isinstance(value, str) and not value.strip()):
                    raise ValueError(
                        f"reference_quote.groups[{group_index}].rows[{row_index}]缺少{key}。"
                    )
    return quote


def _normalise_payload(payload: Mapping[str, Any], *, output_type: str) -> dict[str, Any]:
    """Copy a payload and add only non-financial presentation defaults."""

    result = deepcopy(dict(payload))
    if output_type == "report":
        supplied_sections = result.get("sections")
        if supplied_sections is not None and tuple(as_list(supplied_sections)) != SECTION_ORDER:
            raise ValueError("Report sections必须严格为核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示。")
        if "research" in result:
            raise ValueError("Report不接受research字段；研究逻辑不属于公开报告。")
        if "section_titles" in as_dict(result.get("meta")):
            raise ValueError("Report不接受自定义section_titles；公开章节标题固定。")
    if output_type == "quote":
        _reference_quote(result)
    meta = dict(result.get("meta") or {})
    if "layout" in meta:
        raise ValueError("payload.meta不接受layout字段；Report版式由Designer固定。")
    result["meta"] = meta
    result.setdefault("sections", list(SECTION_ORDER))
    for key in ("conclusion", "recommendation", "parameters", "payoff", "pricing", "backtest", "risk"):
        result.setdefault(key, {})
    return result


def _stable_hash(value: Any) -> str:
    """Hash JSON content without changing or evaluating its values."""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return sha256(encoded).hexdigest()


def _semantic_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Remove presentation-only metadata before recording the fact hash."""

    value = deepcopy(dict(payload))
    meta = dict(value.get("meta") or {})
    for key in ("output_type", "format", "asset_mode", "design_system_id", "design_system_hash"):
        meta.pop(key, None)
    value["meta"] = meta
    return value


_VISIBLE_INTERNAL_TEXT = re.compile(
    r"(?i)(?:(?<![a-z0-9_])optionhelper(?![a-z0-9_])|"
    r"(?<![a-z0-9_])(?:app\s+host|reporter|designer|reportunit|modulerun)(?![a-z0-9_])|"
    r"(?<![a-z0-9_])(?:run[_\s-]?(?:ref|id)|source[_\s-]?id|"
    r"(?:artifact|semantic|presentation|content)[_\s-]?hash|manifest)(?![a-z0-9_])|"
    r"(?:audit|private)[_\s-]*(?:trail|id|hash|manifest|payload|metadata)|"
    r"protocol[_\s-]*(?:object|payload|schema)|内部(?:路径|字段|协议对象)|"
    r"文件路径|物理路径|绝对路径|(?:^|\s)[~\\/][^\s<]+|[a-z]:[\\/])"
)


def _assert_public_delivery_text(html_content: str) -> None:
    """Reject execution metadata if it would become customer-visible text.

    Internal receipts remain available to Reporter as structured data.  This
    narrow final gate protects public HTML without rewriting frozen facts.
    """

    visible = re.sub(r"<!--.*?-->|<(?:style|script)\b[^>]*>.*?</(?:style|script)\s*>", " ", html_content, flags=re.DOTALL | re.IGNORECASE)
    visible = html.unescape(re.sub(r"<[^>]+>", " ", visible))
    if _VISIBLE_INTERNAL_TEXT.search(visible):
        raise ValueError("公开交付内容包含内部执行字段，请在Reporter投影阶段移除。")


def _card_data_table(rows: list[Mapping[str, Any]]) -> str:
    """Render complete Card facts in the shared red three-line table."""

    items: list[str] = []
    for row in rows:
        label = text(row.get("metric"))
        value = text(row.get("value"))
        unit = text(row.get("unit"))
        if not label or not value:
            continue
        unit_cell = (
            f'<td class="table-cell table-cell--narrative{" table-cell--long" if len(unit) >= 24 else ""}">{esc(unit)}</td>'
            if unit else "<td></td>"
        )
        items.append(
            "<tr>"
            f'<td class="table-cell table-cell--narrative{" table-cell--long" if len(label) >= 24 else ""}">{esc(label)}</td>'
            f'<td class="table-cell table-cell--numeric">{rich_text(value)}</td>'
            f'{unit_cell}'
            "</tr>"
        )
    if not items:
        return ""
    return (
        '<div class="table-wrap card-table-wrap"><table class="card-data-table" data-table-density="normal">'
        '<thead><tr><th scope="col">指标</th><th scope="col">数值</th><th scope="col">单位或口径</th></tr></thead>'
        f'<tbody>{"".join(items)}</tbody></table></div>'
    )


def _card_body(
    payload: Mapping[str, Any],
    sections: tuple[tuple[str, str, str], ...] | None = None,
    appended_content: Mapping[str, tuple[Mapping[str, Any], ...]] | None = None,
) -> str:
    """Render the concise view of the same frozen facts as a Report.

    A Card has no payoff/chart renderer.  Its definition can select or reorder
    its six known blocks, while the shared shell and CSS remain unchanged.
    """

    sections = sections or tuple((key, title, key) for key, title in (
        ("recommendation", "结构推荐"), ("reason", "推荐理由"),
        ("contract-highlights", "关键合同条款"), ("pricing", "估值摘要"),
        ("backtest", "回测摘要"), ("risk", "主要风险"),
    ))
    appended_content = appended_content or {}

    def card_unit(value: Any) -> str:
        unit = text(value)
        return {"CNY": "人民币", "RMB": "人民币"}.get(unit.upper(), unit)

    recommendation = as_dict(payload.get("recommendation"))
    pricing = as_dict(payload.get("pricing"))
    backtest = as_dict(payload.get("backtest"))
    risk = as_dict(payload.get("risk"))

    def module_state(module: Mapping[str, Any]) -> str:
        status = text(module.get("status") or "pending").lower()
        labels = {
            "ready": "已完成", "partial": "部分完成", "failed": "运行失败",
            "not_run": "未运行", "unsupported": "不适用", "pending": "待处理",
        }
        note = text(module.get("note"))
        return (
            f'<div class="module-state" data-status="{esc(status)}">'
            f'<strong>{esc(labels.get(status, status))}</strong>'
            + (f'<span>{esc(note)}</span>' if note else "")
            + "</div>"
        )

    def recommendation_block(title: str) -> str:
        if recommendation.get("has_recommendation") is False:
            title = "研究结构"
        headline = text(recommendation.get("headline") or recommendation.get("structure_name"))
        underlyings = text(recommendation.get("underlyings"))
        if not headline and not underlyings:
            return ""
        parts = [f'<section class="card-conclusion"><h2 class="conclusion-heading">{esc(title)}</h2>']
        if headline:
            parts.append(f'<p class="card-structure">{esc(headline)}</p>')
        if underlyings:
            parts.append(f'<p class="card-underlying">挂钩标的：{esc(underlyings)}</p>')
        return "".join(parts) + "</section>"

    def reason_block(title: str) -> str:
        reasons = [text(item) for item in as_list(recommendation.get("reason_points")) if text(item)]
        if not reasons and text(recommendation.get("reason")):
            reasons = [text(recommendation.get("reason"))]
        if not reasons:
            return ""
        content = "；".join(item.rstrip("。；") for item in reasons) + "。"
        return f'<section class="card-reasoning"><h2>{esc(title)}</h2><p>{rich_text(content)}</p></section>'

    def contract_block(title: str) -> str:
        terms: list[str] = []
        compact_units = {"年", "人民币", "份合同", "点", "次", "期"}
        for item in (as_dict(value) for value in as_list(payload.get("contract_highlights"))):
            label, value, note = text(item.get("label")), display_text(item.get("value"), item.get("value_format")), text(item.get("note"))
            if not label or not value:
                continue
            if note in compact_units:
                value_html = f"<strong>{rich_text(value)}{esc(note)}</strong>"
            else:
                value_html = f"<strong>{rich_text(value)}</strong>" + (f"<small>{esc(note)}</small>" if note else "")
            terms.append(f'<div class="card-contract"><dt>{esc(label)}</dt><dd>{value_html}</dd></div>')
        if not terms:
            return ""
        return (
            '<section class="card-contract-summary" aria-labelledby="card-contract-summary-title">'
            f'<div class="card-contract-summary__heading"><h2 id="card-contract-summary-title">{esc(title)}</h2>'
            '</div>'
            f'<dl class="card-contract-grid">{"".join(terms)}</dl></section>'
        )

    def pricing_block(title: str) -> str:
        if text(pricing.get("status")).lower() not in {"ready", "partial", "failed"}:
            return ""
        status = text(pricing.get("status") or "pending").lower()
        rows: list[dict[str, Any]] = []
        if status in {"ready", "partial"}:
            for row in (as_dict(item) for item in as_list(pricing.get("metrics"))[:4]):
                value = display_text(row.get("value"), row.get("value_format"))
                if text(row.get("label")) and value:
                    rows.append({"metric": text(row.get("label")), "value": value, "unit": card_unit(display_basis(row))})
            greeks = {text(row.get("label")): row for row in canonical_greeks(as_list(pricing.get("greeks")))}
            for label in ("Delta", "Gamma", "Vega", "Theta", "Rho"):
                row = greeks.get(label)
                if row is None:
                    continue
                value = display_text(row.get("value"), row.get("value_format"))
                if value:
                    rows.append({"metric": label, "value": value, "unit": card_unit(display_basis(row))})
        table = _card_data_table(rows)
        state = module_state(pricing) if status == "partial" else ""
        if not table and not state:
            return ""
        detail = "；".join(
            value for value in (
                f"估值日：{text(pricing.get('valuation_date'))}" if text(pricing.get("valuation_date")) else "",
                f"方法：{text(pricing.get('method'))}" if text(pricing.get("method")) else "",
            ) if value
        )
        return f'<div class="card-analysis"><h2>{esc(title)}</h2>{state}{table}' + (f'<p class="card-data-note">{esc(detail)}</p>' if detail else "") + "</div>"

    def backtest_block(title: str) -> str:
        if text(backtest.get("status")).lower() not in {"ready", "partial", "failed"}:
            return ""
        status = text(backtest.get("status") or "pending").lower()
        rows: list[dict[str, Any]] = []
        if status in {"ready", "partial"}:
            for row in backtest_summary_rows(backtest):
                value = display_text(row.get("value"), row.get("value_format"))
                if text(row.get("label")) and value:
                    rows.append({"metric": text(row.get("label")), "value": value, "unit": card_unit(display_basis(row))})
        table = _card_data_table(rows)
        state = module_state(backtest) if status == "partial" else ""
        if not table and not state:
            return ""
        details = [
            f"样本区间：{text(backtest.get('window'))}" if text(backtest.get("window")) else "",
            f"入场规则：{text(backtest.get('entry_rule'))}" if text(backtest.get("entry_rule")) else "",
        ]
        detail = "；".join(item for item in details if item)
        disclosures = "".join(
            f'<p class="card-data-note">{esc(note)}</p>'
            for note in as_list(backtest.get("summary_notes")) if text(note)
        ) if status in {"ready", "partial"} else ""
        return f'<div class="card-analysis"><h2>{esc(title)}</h2>{state}{table}' + (f'<p class="card-data-note">{esc(detail)}</p>' if detail else "") + disclosures + "</div>"

    def risk_block(title: str) -> str:
        values = [text(item) for item in as_list(risk.get("items")) if text(item)][:2]
        shown_notes = {text(item) for item in as_list(backtest.get("summary_notes"))}
        limitations = list(dict.fromkeys(text(item) for item in as_list(risk.get("limitations"))
                                       if text(item) and text(item) not in shown_notes))
        if not values and not limitations:
            return ""
        content = "；".join(value.rstrip("。") for value in values) + "。" if values else ""
        notes = "".join(f'<p class="card-data-note">{rich_text(note)}</p>' for note in limitations)
        return f'<section class="card-risk"><h2>{esc(title)}</h2>' + (f'<p>{rich_text(content)}</p>' if content else "") + notes + '</section>'

    renderers = {
        "recommendation": recommendation_block,
        "reason": reason_block,
        "contract_highlights": contract_block,
        "pricing": pricing_block,
        "backtest": backtest_block,
        "risk": risk_block,
    }
    blocks: list[str] = []

    def render_section(section_id: str, title: str, block: str) -> str:
        body = "" if block == "supplemental" else renderers[block](title)
        appended = render_presentation_content(appended_content.get(section_id, ()))
        if appended:
            body = (
                f'<section class="card-appended"><h2>{esc(title)}</h2>{appended}</section>'
                if block == "supplemental"
                else body + f'<div class="card-appended">{appended}</div>'
            )
        return body

    position = 0
    while position < len(sections):
        section_id, title, block = sections[position]
        if block == "pricing" and position + 1 < len(sections) and sections[position + 1][2] == "backtest":
            next_id, next_title, next_block = sections[position + 1]
            left, right = render_section(section_id, title, block), render_section(next_id, next_title, next_block)
            if left or right:
                blocks.append('<section class="card-analysis-grid">' + left + right + "</section>")
            position += 2
            continue
        rendered = render_section(section_id, title, block)
        if rendered:
            blocks.append(rendered if block not in {"pricing", "backtest"} else '<section class="card-analysis-grid">' + rendered + "</section>")
        position += 1
    return "".join(blocks)


def render_card_html(
    payload: Mapping[str, Any],
    *,
    config: DesignerConfig | Mapping[str, Any] | None = None,
    section_definition: tuple[tuple[str, str, str], ...] | None = None,
    appended_content: Mapping[str, tuple[Mapping[str, Any], ...]] | None = None,
    template_shell: str = "card.html",
) -> str:
    """Render the minimal Card from the same frozen payload as a Report."""

    safe_payload = _normalise_payload(payload, output_type="card")
    validate_payload(safe_payload)
    theme = build_design_system()
    config = load_designer_config(config)
    template = config.read_template(template_shell)
    return (
        template.replace("__TITLE__", html.escape("场外衍生品结构研究卡片" if as_dict(payload.get("recommendation")).get("has_recommendation") is False else CARD_DELIVERY_TITLE))
        .replace("__REPORT_THEME__", config.read_report_theme())
        .replace("__BRAND__", html.escape(PUBLIC_BRAND))
        .replace("__DESIGN_SYSTEM_ID__", html.escape(theme.design_system_id))
        .replace("__CARD_BODY__", _card_body(safe_payload, section_definition, appended_content))
    )


_QUOTE_PANEL_TITLES = {
    "default": "报价条款",
    "identifier": "基础条款",
    "numeric": "执行与价格条款",
    "narrative": "观察与结算条款",
}
_QUOTE_SHARED_KEYS = frozenset({
    "asset", "asset_id", "code", "product", "structure", "structure_name", "structure_type", "underlying",
})
_QUOTE_EMPTY_VALUES = frozenset({"", "-", "—", "–", "na", "n/a", "不适用"})
_QUOTE_COMPARISON_LABEL_TOKENS = (
    "行权",
    "执行价",
    "期权费",
    "权利金",
    "报价",
    "价格",
    "票息",
    "收益",
    "估值",
)


def _quote_has_value(value: Any, value_format: Any = "text") -> bool:
    rendered = display_text(value, value_format or "text").strip().casefold()
    return rendered not in _QUOTE_EMPTY_VALUES


def _quote_common_terms(
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Lift identical non-comparison facts out of a multi-structure matrix."""

    if len(rows) < 2:
        return [], columns
    common: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for index, column in enumerate(columns):
        key = text(column.get("key"))
        label = text(column.get("label"))
        value_format = column.get("format") or "text"
        values = [display_text(row.get(key), value_format).strip() for row in rows]
        may_lift = (
            index > 0
            and key not in _QUOTE_SHARED_KEYS
            and not any(token in label for token in _QUOTE_COMPARISON_LABEL_TOKENS)
            and all(_quote_has_value(row.get(key), value_format) for row in rows)
            and len(set(values)) == 1
        )
        (common if may_lift else remaining).append(column)
    return common, remaining


def _quote_common_terms_html(columns: list[dict[str, Any]], rows: list[dict[str, Any]]) -> str:
    if not columns or not rows:
        return ""
    items = "；".join(
        f"<span><b>{esc(column.get('label'))}</b>"
        f"{rich_text(display_text(rows[0].get(text(column.get('key'))), column.get('format') or 'text'))}</span>"
        for column in columns
    )
    return f'<p class="quote-common-terms"><strong>共同条款</strong>{items}</p>'


def _quote_panel_columns(columns: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Split a wide quote into readable semantic panels without losing columns."""

    if len(columns) <= 8:
        return [("", columns)]
    shared = [
        column for index, column in enumerate(columns)
        if index == 0 or text(column.get("key")) in _QUOTE_SHARED_KEYS
    ]
    shared_keys = {text(column.get("key")) for column in shared}
    remaining = [column for column in columns if text(column.get("key")) not in shared_keys]
    if not remaining:
        shared = []
        remaining = columns

    buckets: list[tuple[str, list[dict[str, Any]]]] = []
    seen_roles: set[str] = set()
    for column in remaining:
        role = _quote_column_role(column)
        if role not in seen_roles:
            seen_roles.add(role)
            buckets.append((role, []))
        next(item for item in buckets if item[0] == role)[1].append(column)

    panels: list[tuple[str, list[dict[str, Any]]]] = []
    # Four non-repeated columns keep a panel useful on both A4 and desktop;
    # this avoids a trailing one-column panel when a structure has five or
    # six price terms.
    capacity = max(1, 5 - len(shared))
    if not buckets:
        buckets = [("default", remaining)]
    for role, bucket in buckets:
        for offset in range(0, len(bucket), capacity):
            chunk = bucket[offset:offset + capacity]
            suffix = "" if offset == 0 else "（续）"
            panels.append((_QUOTE_PANEL_TITLES.get(role, _QUOTE_PANEL_TITLES["default"]) + suffix, [*shared, *chunk]))
    return panels


def _quote_column_role(column: Mapping[str, Any]) -> str:
    """Apply Quote-specific business grouping over shared table semantics."""

    key = text(column.get("key")).casefold()
    label = text(column.get("label"))
    if key in {"maturity", "maturity_date", "term", "tenor"} or "期限" in label:
        return "identifier"
    if any(token in label for token in ("行权", "观察", "结算", "敲出", "敲入")):
        return "narrative"
    return table_column_role(key, label)


def _quote_table_panel(columns: list[dict[str, Any]], rows: list[dict[str, Any]], panel_title: str) -> str:
    specs = [(text(column.get("key")), text(column.get("label"))) for column in columns]
    density = table_density(specs, rows)
    head = "".join(f'<th scope="col">{esc(column.get("label"))}</th>' for column in columns)
    body_rows: list[str] = []
    for row in rows:
        cells: list[str] = []
        for column in columns:
            key = text(column.get("key"))
            label = text(column.get("label"))
            value = display_text(row.get(key), column.get("format") or "text")
            role = table_column_role(key, label)
            long_class = " table-cell--long" if len(value) >= 24 else ""
            cells.append(
                f'<td class="table-cell table-cell--{role}{long_class}" data-label="{esc(label)}">{rich_text(value)}</td>'
            )
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    title = f'<p class="quote-table-panel__title">{esc(panel_title)}</p>' if panel_title else ""
    return (
        f'<div class="table-wrap quote-table-wrap quote-table-panel">{title}'
        f'<table class="quote-table table-density-{density}" data-table-density="{density}">'
        f'{table_colgroup(specs)}<thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table></div>'
    )


def _quote_table(group: Mapping[str, Any]) -> str:
    """Render all facts compactly, without repeating inapplicable structures."""

    columns = [as_dict(item) for item in as_list(group.get("columns"))]
    rows = [as_dict(item) for item in as_list(group.get("rows"))]
    common_columns, table_columns = _quote_common_terms(columns, rows)
    panels: list[str] = []
    for panel_title, panel_columns in _quote_panel_columns(table_columns):
        identity_keys = {
            text(column.get("key"))
            for index, column in enumerate(panel_columns)
            if index == 0 or text(column.get("key")) in _QUOTE_SHARED_KEYS
        }
        fact_columns = [
            column for column in panel_columns
            if text(column.get("key")) not in identity_keys
        ]
        panel_rows = [
            row for row in rows
            if not panel_title or not fact_columns or any(
                _quote_has_value(row.get(text(column.get("key"))), column.get("format") or "text")
                for column in fact_columns
            )
        ]
        if panel_rows:
            panels.append(_quote_table_panel(panel_columns, panel_rows, panel_title))
    return _quote_common_terms_html(common_columns, rows) + "".join(panels)


def _quote_identity(quote: Mapping[str, Any]) -> str:
    rows = (
        ("报价日期", quote.get("quote_date")),
        ("报价有效期", quote.get("valid_until")),
    )
    return "".join(
        f"<div><dt>{esc(label)}</dt><dd>{esc_rendered(text(value))}</dd></div>"
        for label, value in rows
        if text(value)
    )


def _quote_body(payload: Mapping[str, Any]) -> str:
    quote = _reference_quote(payload)
    groups = [as_dict(item) for item in as_list(quote.get("groups"))]
    rendered_groups = "".join(
        '<section class="quote-group">'
        f"<h2>{esc(group.get('title'))}</h2>{_quote_table(group)}"
        "</section>"
        for group in groups
    )
    note = text(quote.get("note")) or "以上为参考报价，实际以交易台正式报价为准。"
    return rendered_groups + f'<p class="quote-note">{rich_text(note)}</p>'


def render_quote_html(
    payload: Mapping[str, Any],
    *,
    config: DesignerConfig | Mapping[str, Any] | None = None,
    section_definition: tuple[tuple[str, str, str], ...] | None = None,
    appended_content: Mapping[str, tuple[Mapping[str, Any], ...]] | None = None,
    template_shell: str = "quote.html",
) -> str:
    """Render explicit, grouped reference quotes without any inferred prices."""

    safe_payload = _normalise_payload(payload, output_type="quote")
    config = load_designer_config(config)
    definition = section_definition or (("reference-quote", QUOTE_DELIVERY_TITLE, "reference_quote"),)
    appended_content = appended_content or {}
    theme = build_design_system()
    template = config.read_template(template_shell)
    meta = as_dict(safe_payload.get("meta"))
    title = text(meta.get("title")) or text(meta.get("quote_title")) or QUOTE_DELIVERY_TITLE
    quote = _reference_quote(safe_payload)
    blocks: list[str] = []
    for section_id, section_title, block in definition:
        if block == "reference_quote":
            body = _quote_body(safe_payload)
        elif block == "supplemental":
            body = ""
        else:
            raise ValueError(f"Quote模板不支持内容块：{block}。")
        body += render_presentation_content(appended_content.get(section_id, ()))
        if body:
            blocks.append(
                body if block == "reference_quote" and section_title == QUOTE_DELIVERY_TITLE else
                f'<section class="quote-custom"><h2>{esc(section_title)}</h2>{body}</section>'
            )
    return (
        template.replace("__TITLE__", esc(title))
        .replace("__REPORT_THEME__", config.read_report_theme())
        .replace("__BRAND__", esc(PUBLIC_BRAND))
        .replace("__DESIGN_SYSTEM_ID__", esc(theme.design_system_id))
        .replace("__IDENTITY__", _quote_identity(quote))
        .replace("__QUOTE_BODY__", "".join(blocks))
    )


def _chart_specs_from_html(html_content: str) -> dict[str, dict[str, Any]]:
    """Recover the frozen chart specs already embedded in Designer HTML.

    HTML and PDF share this exact public specification.  The PDF renderer uses
    it for a controlled static projection rather than executing browser code.
    """

    match = re.search(r"const chartSpecs=(\[.*?\]);", html_content, re.DOTALL)
    if not match:
        return {}
    try:
        values = json.loads(match.group(1).replace("<\\/", "</"))
    except json.JSONDecodeError as error:
        raise DesignerDependencyError("报告图表规格无效，无法生成PDF。") from error
    return {
        str(item.get("id")): dict(item)
        for item in values
        if isinstance(item, Mapping) and str(item.get("id") or "")
    }


def _pdf_bytes(html_content: str, base_url: Path, *, chart_specs: Mapping[str, Mapping[str, Any]] | None = None) -> bytes:
    """Convert Designer's public HTML into a real self-contained PDF.

    ``base_url`` remains part of the stable renderer signature because callers
    bind the handoff to a controlled directory.  PDF conversion itself never
    follows local URLs: the public text/table projection is self-contained.
    """

    del base_url
    try:
        return render_pdf(html_content, chart_specs=chart_specs)
    except PdfRuntimeError as error:
        raise DesignerDependencyError(str(error)) from error


def _pdf_html_projection(html_content: str) -> str:
    """Retain public visual facts while removing executable browser scripts.

    ``render_pdf`` consumes report-only SVG data and the frozen ECharts
    specification before this projection is converted.  Removing scripts is
    therefore safe: the reader receives equivalent static figures plus the
    same captions and full data tables.
    """

    without_scripts = re.sub(
        r"<script\b[^>]*>.*?</script\s*>", "", html_content, flags=re.IGNORECASE | re.DOTALL
    )
    return re.sub(
        r'<aside\s+class="report-toc"\s+aria-label="报告目录">.*?</aside\s*>',
        "",
        without_scripts,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _portable_assets(config: DesignerConfig) -> list[dict[str, str]]:
    source = config.echarts_asset_path
    if not source.is_file():
        raise DesignerDependencyError(f"找不到portable离线ECharts文件：{source}")
    raw = source.read_bytes()
    return [{
        "path": "assets/echarts.min.js",
        "media_type": "application/javascript",
        "sha256": sha256(raw).hexdigest(),
        "content_base64": base64.b64encode(raw).decode("ascii"),
    }]


def materialize_portable_assets(assets: list[Mapping[str, Any]], destination_dir: Path) -> list[Path]:
    """Write a verified portable manifest without permitting path traversal."""

    destination_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for item in assets:
        relative = PurePosixPath(str(item.get("path", "")))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError(f"portable资源路径不安全：{relative}")
        try:
            raw = base64.b64decode(str(item.get("content_base64", "")), validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError(f"portable资源内容不是有效Base64：{relative}") from error
        digest = sha256(raw).hexdigest()
        if digest != str(item.get("sha256", "")):
            raise ValueError(f"portable资源哈希不匹配：{relative}")
        output = destination_dir.joinpath(*relative.parts)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(raw)
        written.append(output)
    return written


def render(
    designer_input: DesignerInput | Mapping[str, Any],
    *,
    config: DesignerConfig | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Render a frozen handoff and return a serializable artifact envelope."""

    request = _normalise_input(designer_input)
    config = load_designer_config(config)
    config.validate_request(
        asset_mode=request.asset_mode,
        output_format=request.normalized_format,
    )
    payload = _normalise_payload(request.payload, output_type=request.normalized_output_type)
    # A template controls the order of known fact blocks, never their values.
    # Empty blocks are omitted by the shared renderers.
    sections_by_output = {
        "report": SECTION_ORDER,
        "card": CARD_CONTENT_ORDER,
        "quote": QUOTE_CONTENT_ORDER,
    }
    payload["sections"] = list(sections_by_output[request.normalized_output_type])
    theme = build_design_system()
    comparison_mode = isinstance(payload.get("comparison"), Mapping)
    automatic_template_id = (
        "multicard-standard" if comparison_mode and request.normalized_output_type == "card"
        else "multireport-standard" if comparison_mode and request.normalized_output_type == "report"
        else default_template_id(request.normalized_output_type)
    )
    template = load_template_definition(
        config,
        request.template_id or automatic_template_id,
        request.normalized_output_type,
    )
    if comparison_mode:
        required_shell = "multicard.html" if request.normalized_output_type == "card" else "multireport.html"
        if request.normalized_output_type in {"card", "report"} and template.shell != required_shell:
            raise ValueError(f"多结构{request.normalized_output_type}模板必须使用{required_shell}壳。")
    presentation = apply_presentation_patch(
        payload,
        template.sections,
        request.presentation_patch,
        output_type=request.normalized_output_type,
    )
    payload = dict(presentation.payload)
    if request.design_system_id and request.design_system_id != theme.design_system_id:
        raise ValueError(
            f"设计系统标识不匹配：输入={request.design_system_id}，当前={theme.design_system_id}"
        )
    input_dir = request.input_dir or Path.cwd()
    echarts_path = (
        "assets/echarts.min.js"
        if request.asset_mode == "portable"
        else config.relative_echarts_path(request.output_dir or input_dir)
    )
    if comparison_mode and request.normalized_output_type == "card":
        html_content = render_multicard_html(
            payload,
            config=config,
            template_shell=template.shell,
            section_definition=tuple((item.id, item.title, item.block) for item in presentation.sections),
            appended_content=presentation.appended_content,
        )
    elif comparison_mode and request.normalized_output_type == "report":
        html_content = render_multireport_html(
            payload,
            input_dir,
            echarts_path,
            config=config,
            template_shell=template.shell,
            section_definition=tuple((item.id, item.title, item.block) for item in presentation.sections),
            appended_content=presentation.appended_content,
        )
    elif request.normalized_output_type == "card":
        html_content = render_card_html(
            payload,
            config=config,
            section_definition=tuple((item.id, item.title, item.block) for item in presentation.sections),
            appended_content=presentation.appended_content,
            template_shell=template.shell,
        )
    elif request.normalized_output_type == "quote":
        html_content = render_quote_html(
            payload,
            config=config,
            section_definition=tuple((item.id, item.title, item.block) for item in presentation.sections),
            appended_content=presentation.appended_content,
            template_shell=template.shell,
        )
    else:
        html_content = render_html(
            payload,
            input_dir,
            echarts_path,
            design_system_id=theme.design_system_id,
            config=config,
            section_definition=tuple((item.id, item.title, item.block) for item in presentation.sections),
            appended_content=dict(presentation.appended_content),
            template_shell=template.shell,
        )
    _assert_public_delivery_text(html_content)
    output_format = request.normalized_format
    pdf_chart_specs = _chart_specs_from_html(html_content) if output_format == "pdf" else None
    if output_format == "pdf":
        html_content = _pdf_html_projection(html_content)
    result: dict[str, Any] = {
        "format": request.format,
        "output_type": request.normalized_output_type,
        "html": html_content,
        "design_system_id": theme.design_system_id,
        "design_system_hash": theme.token_hash,
        "template_id": template.id,
        "asset_mode": request.asset_mode,
    }
    result["format"] = output_format
    semantic_fact_hash = _stable_hash(_semantic_payload(request.payload))
    presentation_input_hash = _stable_hash(
        {
            "semantic_fact_hash": semantic_fact_hash,
            "output_type": request.normalized_output_type,
            "format": output_format,
            "asset_mode": request.asset_mode,
            "design_system_id": theme.design_system_id,
            "design_system_hash": theme.token_hash,
            "template_id": template.id,
            "presentation_patch": request.presentation_patch,
        }
    )
    requires_echarts = '<script src=' in html_content
    if output_format == "pdf":
        result["pdf"] = _pdf_bytes(html_content, input_dir, chart_specs=pdf_chart_specs)
        result["mime_type"] = "application/pdf"
    else:
        result["mime_type"] = "text/html; charset=utf-8"
    if request.asset_mode == "portable" and output_format == "html":
        result["portable_assets"] = _portable_assets(config) if requires_echarts else []
    html_hash = sha256(html_content.encode("utf-8")).hexdigest()
    manifest: dict[str, Any] = {
        "schema": DESIGNER_ARTIFACT_MANIFEST_SCHEMA,
        "design_system_id": theme.design_system_id,
        "design_system_hash": theme.token_hash,
        "template_id": template.id,
        "output_type": request.normalized_output_type,
        "format": output_format,
        "html_sha256": html_hash,
        "semantic_fact_hash": semantic_fact_hash,
        "presentation_input_hash": presentation_input_hash,
        # Bind the receipt to the exact frozen handoff received from Reporter.
        # ``payload`` below contains Designer-only defaults and fixed document
        # presentation, so hashing it would make an otherwise immutable Card
        # handoff fail Reporter verification.
        "content_sha256": _stable_hash(request.payload),
        "assets": [],
    }
    if requires_echarts and output_format == "html":
        asset_bytes = config.echarts_asset_path.read_bytes()
        manifest["assets"] = [{
            "asset_id": "echarts",
            "path": "assets/echarts.min.js" if request.asset_mode == "portable" else echarts_path,
            "media_type": "application/javascript",
            "sha256": sha256(asset_bytes).hexdigest(),
        }]
    if output_format == "pdf":
        manifest["pdf_sha256"] = sha256(result["pdf"]).hexdigest()
    if "portable_assets" in result:
        manifest["portable_assets"] = [
            {key: item[key] for key in ("path", "media_type", "sha256")}
            for item in result["portable_assets"]
        ]
    manifest["artifact_hash"] = manifest.get("pdf_sha256", html_hash)
    result["semantic_fact_hash"] = semantic_fact_hash
    result["presentation_input_hash"] = presentation_input_hash
    result["artifact_hash"] = manifest["artifact_hash"]
    result["artifact_manifest"] = manifest
    if presentation.receipt is not None:
        result["presentation_receipt"] = dict(presentation.receipt)
    return result


__all__ = [
    "DesignerDependencyError",
    "materialize_portable_assets",
    "render",
    "render_card_html",
    "render_quote_html",
    "render_html",
    "validate_payload",
]
