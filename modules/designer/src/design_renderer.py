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
    canonical_greeks,
    display_text,
    esc,
    esc_rendered,
    render_html,
    render_presentation_content,
    rich_text,
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
    r"审计|文件路径|物理路径|绝对路径|(?:^|\s)[~\\/][^\s<]+|[a-z]:[\\/])"
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
        items.append(
            "<tr>"
            f"<td>{esc(label)}</td><td>{esc(value)}</td>"
            f"<td>{esc(unit)}</td>"
            "</tr>"
        )
    if not items:
        return ""
    return (
        '<div class="table-wrap card-table-wrap"><table class="card-data-table">'
        '<thead><tr><th scope="col">指标</th><th scope="col">数值</th><th scope="col">单位或口径</th></tr></thead>'
        f'<tbody>{"".join(items)}</tbody></table></div>'
    )


def _card_body(
    payload: Mapping[str, Any],
    sections: tuple[tuple[str, str, str], ...] | None = None,
    custom_content: Mapping[str, tuple[Mapping[str, Any], ...]] | None = None,
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
    custom_content = custom_content or {}
    appended_content = appended_content or {}

    def card_unit(value: Any) -> str:
        unit = text(value)
        return {"CNY": "人民币", "RMB": "人民币"}.get(unit.upper(), unit)

    recommendation = as_dict(payload.get("recommendation"))
    pricing = as_dict(payload.get("pricing"))
    backtest = as_dict(payload.get("backtest"))
    risk = as_dict(payload.get("risk"))

    def recommendation_block(title: str) -> str:
        headline = text(recommendation.get("headline") or recommendation.get("structure_name"))
        underlyings = text(recommendation.get("underlyings"))
        if not headline and not underlyings:
            return ""
        parts = [f'<section class="card-conclusion"><h2>{esc(title)}</h2>']
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
            label, value, note = text(item.get("label")), text(item.get("value")), text(item.get("note"))
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
            '<p>估值摘要与回测摘要均基于同一份冻结合同事实。</p></div>'
            f'<dl class="card-contract-grid">{"".join(terms)}</dl></section>'
        )

    def pricing_block(title: str) -> str:
        if text(pricing.get("status")).lower() != "ready":
            return ""
        rows: list[dict[str, Any]] = []
        for row in (as_dict(item) for item in as_list(pricing.get("metrics"))[:4]):
            value = display_text(row.get("value"), row.get("value_format"))
            if text(row.get("label")) and value:
                rows.append({"metric": text(row.get("label")), "value": value, "unit": card_unit(row.get("note"))})
        greeks = {text(row.get("label")): row for row in canonical_greeks(as_list(pricing.get("greeks")))}
        for label in ("Delta", "Gamma", "Vega", "Theta", "Rho"):
            row = greeks.get(label)
            if row is None:
                rows.append({"metric": label, "value": "—", "unit": ""})
                continue
            value = display_text(row.get("value"), row.get("value_format"))
            if value:
                rows.append({"metric": label, "value": value, "unit": card_unit(row.get("unit"))})
        table = _card_data_table(rows)
        if not table:
            return ""
        detail = "；".join(
            value for value in (
                f"估值日：{text(pricing.get('valuation_date'))}" if text(pricing.get("valuation_date")) else "",
                f"方法：{text(pricing.get('method'))}" if text(pricing.get("method")) else "",
            ) if value
        )
        return f'<div class="card-analysis"><h2>{esc(title)}</h2>{table}' + (f'<p class="card-data-note">{esc(detail)}</p>' if detail else "") + "</div>"

    def backtest_block(title: str) -> str:
        if text(backtest.get("status")).lower() != "ready":
            return ""
        metrics_by_label = {
            text(as_dict(item).get("label")): as_dict(item)
            for item in as_list(backtest.get("metrics")) if text(as_dict(item).get("label"))
        }
        for table in (as_dict(item) for item in as_list(backtest.get("detail_tables"))):
            if text(table.get("title")) == "公共回测统计":
                for item in as_list(table.get("rows")):
                    row = as_dict(item)
                    if text(row.get("label")):
                        metrics_by_label.setdefault(text(row.get("label")), row)
        rows: list[dict[str, Any]] = []
        for labels in (("样本数",), ("胜率", "历史正收益样本占比"), ("平均损益", "平均收益"), ("最差损益", "最大亏损")):
            row = next((metrics_by_label[label] for label in labels if metrics_by_label.get(label, {}).get("value") is not None), None)
            if row:
                rows.append({"metric": text(row.get("label")), "value": display_text(row.get("value"), row.get("value_format")), "unit": text(row.get("note"))})
        for row in (as_dict(item) for item in as_list(backtest.get("card_metrics"))[:4]):
            value = display_text(row.get("value"), row.get("value_format"))
            if text(row.get("label")) and value:
                rows.append({"metric": text(row.get("label")), "value": value, "unit": text(row.get("note"))})
        table = _card_data_table(rows)
        if not table:
            return ""
        details = [
            f"样本区间：{text(backtest.get('window'))}" if text(backtest.get("window")) else "",
            f"入场规则：{text(backtest.get('entry_rule'))}" if text(backtest.get("entry_rule")) else "",
        ]
        detail = "；".join(item for item in details if item)
        return f'<div class="card-analysis"><h2>{esc(title)}</h2>{table}' + (f'<p class="card-data-note">{esc(detail)}</p>' if detail else "") + "</div>"

    def risk_block(title: str) -> str:
        values = [text(item) for item in as_list(risk.get("items")) if text(item)][:2]
        if not values:
            return ""
        content = "；".join(value.rstrip("。") for value in values) + "。"
        return f'<section class="card-risk"><h2>{esc(title)}</h2><p>{rich_text(content)}</p></section>'

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
        if block.startswith("custom:"):
            body = render_presentation_content(custom_content.get(section_id, ()))
            return f'<section class="card-custom"><h2>{esc(title)}</h2>{body}</section>' if body else ""
        body = renderers[block](title)
        appended = render_presentation_content(appended_content.get(section_id, ()))
        if appended:
            body = body + f'<div class="card-appended">{appended}</div>'
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
    custom_content: Mapping[str, tuple[Mapping[str, Any], ...]] | None = None,
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
        template.replace("__TITLE__", html.escape(CARD_DELIVERY_TITLE))
        .replace("__REPORT_THEME__", config.read_report_theme())
        .replace("__BRAND__", html.escape(PUBLIC_BRAND))
        .replace("__DESIGN_SYSTEM_ID__", html.escape(theme.design_system_id))
        .replace("__CARD_BODY__", _card_body(safe_payload, section_definition, custom_content, appended_content))
    )


def _quote_table(group: Mapping[str, Any]) -> str:
    """Render one product-specific quote table with the shared three-line rule."""

    columns = [as_dict(item) for item in as_list(group.get("columns"))]
    rows = [as_dict(item) for item in as_list(group.get("rows"))]
    head = "".join(f'<th scope="col">{esc(column.get("label"))}</th>' for column in columns)
    body_rows: list[str] = []
    for row in rows:
        cells: list[str] = []
        for column in columns:
            value = display_text(row.get(text(column.get("key"))), column.get("format") or "text")
            cells.append(f"<td>{rich_text(value)}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        '<div class="table-wrap quote-table-wrap"><table class="quote-table">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>"
    )


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
    custom_content: Mapping[str, tuple[Mapping[str, Any], ...]] | None = None,
    appended_content: Mapping[str, tuple[Mapping[str, Any], ...]] | None = None,
    template_shell: str = "quote.html",
) -> str:
    """Render explicit, grouped reference quotes without any inferred prices."""

    safe_payload = _normalise_payload(payload, output_type="quote")
    config = load_designer_config(config)
    definition = section_definition or (("reference-quote", QUOTE_DELIVERY_TITLE, "reference_quote"),)
    custom_content = custom_content or {}
    appended_content = appended_content or {}
    theme = build_design_system()
    template = config.read_template(template_shell)
    meta = as_dict(safe_payload.get("meta"))
    title = text(meta.get("quote_title")) or QUOTE_DELIVERY_TITLE
    quote = _reference_quote(safe_payload)
    blocks: list[str] = []
    for section_id, section_title, block in definition:
        if block == "reference_quote":
            body = _quote_body(safe_payload)
        elif block.startswith("custom:"):
            body = render_presentation_content(custom_content.get(section_id, ()))
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
    template = load_template_definition(
        config,
        request.template_id or default_template_id(request.normalized_output_type),
        request.normalized_output_type,
    )
    presentation = apply_presentation_patch(payload, template.sections, request.presentation_patch)
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
    if request.normalized_output_type == "card":
        html_content = render_card_html(
            payload,
            config=config,
            section_definition=tuple((item.id, item.title, item.block) for item in presentation.sections),
            custom_content=presentation.custom_content,
            appended_content=presentation.appended_content,
            template_shell=template.shell,
        )
    elif request.normalized_output_type == "quote":
        html_content = render_quote_html(
            payload,
            config=config,
            section_definition=tuple((item.id, item.title, item.block) for item in presentation.sections),
            custom_content=presentation.custom_content,
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
            custom_content=dict(presentation.custom_content),
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
