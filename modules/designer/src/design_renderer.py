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
from .renderer import (
    PUBLIC_BRAND,
    SECTION_ORDER,
    STATUS_LABELS,
    as_dict,
    as_list,
    canonical_greeks,
    display_text,
    esc,
    metric_strip,
    render_html,
    rich_text,
    text,
    validate_payload,
)


class DesignerDependencyError(RuntimeError):
    """Raised when a requested output format lacks its optional converter."""


CARD_CONTENT_ORDER = ("recommendation", "reason", "contract_highlights", "pricing", "backtest", "risk")
CARD_DELIVERY_TITLE = "场外衍生品结构推荐卡片"


def _normalise_input(value: DesignerInput | Mapping[str, Any]) -> DesignerInput:
    if isinstance(value, DesignerInput):
        return value
    if isinstance(value, Mapping):
        return DesignerInput.from_mapping(value)
    raise TypeError("render需要DesignerInput或Mapping。")


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
    for key in ("output_type", "format", "asset_mode", "design_system_version", "design_system_hash"):
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
        if not label:
            continue
        items.append(
            "<tr>"
            f"<td>{esc(label)}</td><td>{esc(value) if value else '未提供'}</td>"
            f"<td>{esc(unit) if unit else '口径未提供'}</td>"
            "</tr>"
        )
    if not items:
        return ""
    return (
        '<div class="table-wrap card-table-wrap"><table class="card-data-table">'
        '<thead><tr><th scope="col">指标</th><th scope="col">数值</th><th scope="col">单位或口径</th></tr></thead>'
        f'<tbody>{"".join(items)}</tbody></table></div>'
    )


def _card_body(payload: Mapping[str, Any]) -> str:
    """Render the compact public recommendation Card.

    Card is the concise reading surface of the same frozen facts as the
    detailed Report. It answers four reader questions only: what is
    recommended, why, how it is valued, and how the structure behaved in the
    verified backtest. Payoff figures, scenario tables, full detail tables
    and charts remain in Report as an expansion of this same evidence.
    """

    def card_unit(value: Any) -> str:
        unit = text(value)
        return {"CNY": "人民币", "RMB": "人民币"}.get(unit.upper(), unit)

    def unavailable_module_copy(module: Mapping[str, Any], subject: str) -> str:
        status = text(module.get("status") or "pending").lower()
        label = STATUS_LABELS.get(status, "未提供")
        note = text(module.get("note")) or f"本次未形成可引用的{subject}结果。"
        message = note if note.startswith(label) else f"{label}：{note}"
        return f'<p class="empty-copy">{esc(message)}</p>'

    recommendation = as_dict(payload.get("recommendation"))
    blocks: list[str] = []
    headline = text(recommendation.get("headline") or recommendation.get("structure_name"))
    reason = text(recommendation.get("reason"))
    underlyings = text(recommendation.get("underlyings"))
    blocks.append('<section class="card-conclusion"><h2>结构推荐</h2>')
    blocks.append(f'<p class="card-structure">{esc(headline) if headline else "未提供"}</p>')
    blocks.append(
        f'<p class="card-underlying">挂钩标的：{esc(underlyings) if underlyings else "未提供"}</p>'
    )
    blocks.append("</section>")
    blocks.append('<section class="card-reasoning"><h2>推荐理由</h2>')
    blocks.append(f"<p>{rich_text(reason)}</p>" if reason else '<p class="empty-copy">未提供。</p>')
    blocks.append("</section>")

    highlights = [as_dict(item) for item in as_list(payload.get("contract_highlights")) if as_dict(item)]
    terms: list[str] = []
    if highlights:
        compact_units = {"年", "人民币", "份合同", "点", "次", "期"}
        for item in highlights:
            label, value, note = text(item.get("label")), text(item.get("value")), text(item.get("note"))
            if not label or not value:
                continue
            if note in compact_units:
                value_html = f"<strong>{rich_text(value)}{esc(note)}</strong>"
            else:
                note_html = f"<small>{esc(note)}</small>" if note else ""
                value_html = f"<strong>{rich_text(value)}</strong>{note_html}"
            terms.append(f'<div class="card-contract"><dt>{esc(label)}</dt><dd>{value_html}</dd></div>')
    blocks.append(
        '<section class="card-contract-summary" aria-labelledby="card-contract-summary-title">'
        '<div class="card-contract-summary__heading"><h2 id="card-contract-summary-title">关键合同条款</h2>'
        '<p>估值摘要与回测摘要均基于同一份冻结合同事实。</p></div>'
        + (f'<dl class="card-contract-grid">{"".join(terms)}</dl>' if terms else '<p class="empty-copy">未提供。</p>')
        + '</section>'
    )

    pricing_rows: list[dict[str, Any]] = []
    pricing = as_dict(payload.get("pricing"))
    if text(pricing.get("status")).lower() == "ready":
        # Card is a compact subset of Report, not a different fact view.
        # Keep up to four headline valuation metrics and always expose the
        # five canonical Greeks; missing values remain truthful public states.
        for row in [as_dict(item) for item in as_list(pricing.get("metrics"))][:4]:
            if text(row.get("label")):
                pricing_rows.append({
                    "metric": text(row.get("label")),
                    "value": display_text(row.get("value"), row.get("value_format")),
                    "unit": card_unit(row.get("note")),
                })
        for row in canonical_greeks(as_list(pricing.get("greeks"))):
            pricing_rows.append({
                "metric": text(row.get("label")),
                "value": display_text(row.get("value"), row.get("value_format")),
                "unit": card_unit(row.get("unit")),
            })

    backtest_rows: list[dict[str, Any]] = []
    backtest = as_dict(payload.get("backtest"))
    if text(backtest.get("status")).lower() == "ready":
        preferred_groups = (
            ("样本数",),
            ("胜率", "历史正收益样本占比"),
            ("平均损益", "平均收益"),
            ("最差损益", "最大亏损"),
        )
        metrics_by_label = {
            text(as_dict(item).get("label")): as_dict(item)
            for item in as_list(backtest.get("metrics"))
            if text(as_dict(item).get("label"))
        }
        for table in [as_dict(item) for item in as_list(backtest.get("detail_tables"))]:
            if text(table.get("title")) != "公共回测统计":
                continue
            for item in as_list(table.get("rows")):
                row = as_dict(item)
                label = text(row.get("label"))
                if label:
                    metrics_by_label.setdefault(label, row)
        for candidates in preferred_groups:
            label = next(
                (name for name in candidates if name in metrics_by_label and metrics_by_label[name].get("value") is not None),
                candidates[0],
            )
            row = metrics_by_label.get(label, {})
            backtest_rows.append({
                "metric": label,
                "value": display_text(row.get("value"), row.get("value_format")) if row else "未提供",
                "unit": text(row.get("note")) if row else "",
            })
        for row in [as_dict(item) for item in as_list(backtest.get("card_metrics"))][:4]:
            if text(row.get("label")):
                backtest_rows.append({
                    "metric": text(row.get("label")),
                    "value": display_text(row.get("value"), row.get("value_format")),
                    "unit": text(row.get("note")),
                })

    blocks.append('<section class="card-analysis-grid">')
    blocks.append('<div class="card-analysis"><h2>估值摘要</h2>')
    if pricing_rows:
        blocks.append(_card_data_table(pricing_rows))
        valuation_date = text(pricing.get("valuation_date"))
        method = text(pricing.get("method"))
        if valuation_date or method:
            detail = "；".join(item for item in (f"估值日：{valuation_date}" if valuation_date else "", f"方法：{method}" if method else "") if item)
            blocks.append(f'<p class="card-data-note">{esc(detail)}</p>')
    else:
        blocks.append(unavailable_module_copy(pricing, "估值"))
    blocks.append('</div><div class="card-analysis"><h2>回测摘要</h2>')
    if backtest_rows:
        blocks.append(_card_data_table(backtest_rows))
        window = text(backtest.get("window"))
        if window:
            blocks.append(f'<p class="card-data-note">样本区间：{esc(window)}</p>')
    else:
        blocks.append(unavailable_module_copy(backtest, "回测"))
    blocks.append('</div></section>')

    risk = as_dict(payload.get("risk"))
    risk_values = [text(item) for item in as_list(risk.get("items")) if text(item)][:2]
    blocks.append('<section class="card-risk"><h2>主要风险</h2>')
    blocks.append(
        "<p>" + rich_text("；".join(value.rstrip("。") for value in risk_values) + "。") + "</p>"
        if risk_values else '<p class="empty-copy">未提供。</p>'
    )
    blocks.append("</section>")
    return "".join(blocks)


def render_card_html(
    payload: Mapping[str, Any],
    *,
    config: DesignerConfig | Mapping[str, Any] | None = None,
) -> str:
    """Render the minimal Card from the same frozen payload as a Report."""

    safe_payload = _normalise_payload(payload, output_type="card")
    validate_payload(safe_payload)
    theme = build_design_system()
    config = load_designer_config(config)
    template = config.read_template("card.html")
    return (
        template.replace("__TITLE__", html.escape(CARD_DELIVERY_TITLE))
        .replace("__REPORT_THEME__", config.read_report_theme())
        .replace("__BRAND__", html.escape(PUBLIC_BRAND))
        .replace("__DESIGN_SYSTEM_VERSION__", html.escape(theme.design_system_version))
        .replace("__CARD_BODY__", _card_body(safe_payload))
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
    # The navigation rail is an HTML reading aid. PDF remains the same
    # continuous A4 research document, without an extra navigation column.
    return re.sub(
        r'<aside class="report-toc"[^>]*>.*?</aside\s*>', "", without_scripts, flags=re.IGNORECASE | re.DOTALL
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
    # A detailed report is a governed public document, not a user-composed
    # dashboard. Its seven chapters are always present in canonical order;
    # an unavailable module renders its truthful status inside that chapter.
    payload["sections"] = list(
        SECTION_ORDER if request.normalized_output_type == "report" else CARD_CONTENT_ORDER
    )
    theme = build_design_system()
    if request.design_system_version and request.design_system_version != theme.design_system_version:
        raise ValueError(
            f"设计系统版本不匹配：输入={request.design_system_version}，当前={theme.design_system_version}"
        )
    input_dir = request.input_dir or Path.cwd()
    echarts_path = (
        "assets/echarts.min.js"
        if request.asset_mode == "portable"
        else config.relative_echarts_path(request.output_dir or input_dir)
    )
    if request.normalized_output_type == "card":
        html_content = render_card_html(payload, config=config)
    else:
        html_content = render_html(
            payload,
            input_dir,
            echarts_path,
            design_system_version=theme.design_system_version,
            config=config,
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
        "design_system_version": theme.design_system_version,
        "design_system_hash": theme.token_hash,
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
            "design_system_version": theme.design_system_version,
            "design_system_hash": theme.token_hash,
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
        "design_system_version": theme.design_system_version,
        "design_system_hash": theme.token_hash,
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
    return result


__all__ = [
    "DesignerDependencyError",
    "materialize_portable_assets",
    "render",
    "render_card_html",
    "render_html",
    "validate_payload",
]
