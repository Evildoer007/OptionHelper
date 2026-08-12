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
from .models import DesignerInput
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


CARD_CONTENT_ORDER = ("recommendation", "pricing", "backtest", "risk")


def _normalise_input(value: DesignerInput | Mapping[str, Any]) -> DesignerInput:
    if isinstance(value, DesignerInput):
        return value
    if isinstance(value, Mapping):
        return DesignerInput.from_mapping(value)
    raise TypeError("render需要DesignerInput或Mapping。")


def _normalise_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a payload and add only non-financial presentation defaults."""

    result = deepcopy(dict(payload))
    meta = dict(result.get("meta") or {})
    if "layout" not in meta:
        meta["layout"] = "brief"
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
    for key in ("layout", "output_type", "format", "asset_mode", "design_system_version", "design_system_hash"):
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
    """Render the compact public research brief.

    Card is the concise reading surface of the same frozen facts as the
    detailed Report. It answers four reader questions only: what is
    recommended, why, how it is valued, and how the structure behaved in the
    verified backtest. Payoff figures, scenario tables, full detail tables
    and charts remain in Report as an expansion of this same evidence.
    """

    def card_unit(value: Any) -> str:
        unit = text(value)
        return {"CNY": "人民币", "RMB": "人民币"}.get(unit.upper(), unit)

    recommendation = as_dict(payload.get("recommendation"))
    blocks: list[str] = []
    headline = text(recommendation.get("headline") or recommendation.get("structure_name"))
    reason = text(recommendation.get("reason"))
    underlyings = text(recommendation.get("underlyings"))
    if headline or underlyings:
        blocks.append('<section class="card-conclusion"><p class="conclusion-band__label">推荐结构</p>')
        if headline:
            blocks.append(f"<h2>{esc(headline)}</h2>")
        if underlyings:
            blocks.append(f'<p class="card-underlying">挂钩标的：{esc(underlyings)}</p>')
        blocks.append("</section>")
    if reason:
        blocks.append(f'<section class="card-reasoning"><h2>推荐依据</h2><p>{rich_text(reason)}</p></section>')

    highlights = [as_dict(item) for item in as_list(payload.get("contract_highlights")) if as_dict(item)]
    if highlights:
        terms = []
        compact_units = {"年", "人民币", "份合同", "点", "次", "期"}
        for item in highlights:
            label, value, note = text(item.get("label")), text(item.get("value")), text(item.get("note"))
            if not label or not value:
                continue
            if note in compact_units:
                value_html = f"<strong>{esc(value)}{esc(note)}</strong>"
            else:
                note_html = f"<small>{esc(note)}</small>" if note else ""
                value_html = f"<strong>{esc(value)}</strong>{note_html}"
            terms.append(f'<div class="card-contract"><dt>{esc(label)}</dt><dd>{value_html}</dd></div>')
        if terms:
            blocks.append(
                '<section class="card-contract-summary" aria-labelledby="card-contract-summary-title">'
                '<div class="card-contract-summary__heading"><h2 id="card-contract-summary-title">关键条款</h2>'
                '<p>以下估值定价与历史回测均基于同一份已确认合同。</p></div>'
                f'<dl class="card-contract-grid">{"".join(terms)}</dl></section>'
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
    blocks.append('<div class="card-analysis"><h2>估值定价</h2>')
    if pricing_rows:
        blocks.append(_card_data_table(pricing_rows))
        valuation_date = text(pricing.get("valuation_date"))
        method = text(pricing.get("method"))
        if valuation_date or method:
            detail = "；".join(item for item in (f"估值日：{valuation_date}" if valuation_date else "", f"方法：{method}" if method else "") if item)
            blocks.append(f'<p class="card-data-note">{esc(detail)}</p>')
    else:
        blocks.append('<p class="empty-copy">本次尚未形成可引用的估值结果。</p>')
    blocks.append('</div><div class="card-analysis"><h2>历史回测</h2>')
    if backtest_rows:
        blocks.append(_card_data_table(backtest_rows))
        window = text(backtest.get("window"))
        if window:
            blocks.append(f'<p class="card-data-note">样本区间：{esc(window)}</p>')
    else:
        blocks.append('<p class="empty-copy">本次尚未形成可引用的回测结果。</p>')
    blocks.append('</div></section>')

    risk = as_dict(payload.get("risk"))
    risk_values = [text(item) for item in as_list(risk.get("items")) if text(item)][:2]
    if risk_values:
        blocks.append(
            '<section class="card-risk"><h2>风险提示</h2><p>'
            + rich_text("；".join(value.rstrip("。") for value in risk_values) + "。")
            + "</p></section>"
        )
    return "".join(blocks) or '<p class="empty-copy">当前没有可展示的Card内容。</p>'


def render_card_html(
    payload: Mapping[str, Any],
    *,
    config: DesignerConfig | Mapping[str, Any] | None = None,
) -> str:
    """Render the minimal Card from the same frozen payload as a Report."""

    safe_payload = _normalise_payload(payload)
    validate_payload(safe_payload)
    card_title = text(as_dict(safe_payload.get("meta")).get("title")) or "期权结构推荐卡片"
    theme = build_design_system()
    config = load_designer_config(config)
    template = config.read_template("card.html")
    return (
        template.replace("__TITLE__", html.escape(card_title))
        .replace("__REPORT_THEME__", config.read_report_theme())
        .replace("__BRAND__", html.escape(PUBLIC_BRAND))
        .replace("__DESIGN_SYSTEM_VERSION__", html.escape(theme.design_system_version))
        .replace("__CARD_BODY__", _card_body(safe_payload))
    )


def _pdf_bytes(html_content: str, base_url: Path) -> bytes:
    """Convert Designer's public HTML into a real self-contained PDF.

    ``base_url`` remains part of the stable renderer signature because callers
    bind the handoff to a controlled directory.  PDF conversion itself never
    follows local URLs: the public text/table projection is self-contained.
    """

    del base_url
    try:
        return render_pdf(html_content)
    except PdfRuntimeError as error:
        raise DesignerDependencyError(str(error)) from error


def _pdf_html_projection(html_content: str) -> str:
    """Remove browser-only chart machinery while retaining its text data.

    Chart figures already contain an accessibility summary and an exact data
    table.  PDF keeps those reader-visible facts but never carries a deferred
    script or an empty browser chart container into the delivery receipt.
    """

    value = re.sub(r"<script\b[^>]*>.*?</script\s*>", "", html_content, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r'<div\s+class="chart"\b[^>]*></div>', "", value, flags=re.IGNORECASE)


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
        declared_design_system_version=request.design_system_version,
    )
    payload = _normalise_payload(request.payload)
    # A detailed report is a governed public document, not a user-composed
    # dashboard. Its seven chapters are always present in canonical order;
    # an unavailable module renders its truthful status inside that chapter.
    if request.normalized_output_type == "report":
        payload["sections"] = list(SECTION_ORDER)
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
    layout = "brief"
    if as_dict(payload.get("meta")).get("layout") != layout:
        payload["meta"] = {**as_dict(payload.get("meta")), "layout": layout}
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
    if output_format == "pdf":
        html_content = _pdf_html_projection(html_content)
    result: dict[str, Any] = {
        "format": request.format,
        "output_type": request.normalized_output_type,
        "html": html_content,
        "design_system_version": theme.design_system_version,
        "design_system_hash": theme.token_hash,
        "layout": layout,
        "asset_mode": request.asset_mode,
    }
    result["format"] = output_format
    semantic_fact_hash = _stable_hash(_semantic_payload(request.payload))
    presentation_input_hash = _stable_hash(
        {
            "semantic_fact_hash": semantic_fact_hash,
            "output_type": request.normalized_output_type,
            "format": output_format,
            "layout": layout,
            "asset_mode": request.asset_mode,
            "design_system_version": theme.design_system_version,
            "design_system_hash": theme.token_hash,
        }
    )
    requires_echarts = '<script src=' in html_content
    if output_format == "pdf":
        result["pdf"] = _pdf_bytes(html_content, input_dir)
        result["mime_type"] = "application/pdf"
    else:
        result["mime_type"] = "text/html; charset=utf-8"
    if request.asset_mode == "portable" and output_format == "html":
        result["portable_assets"] = _portable_assets(config) if requires_echarts else []
    html_hash = sha256(html_content.encode("utf-8")).hexdigest()
    manifest: dict[str, Any] = {
        "schema": "optionhelper.designer-artifact-manifest/v1",
        "design_system_version": theme.design_system_version,
        "design_system_hash": theme.token_hash,
        "output_type": request.normalized_output_type,
        "format": output_format,
        "layout": layout,
        "html_sha256": html_hash,
        "semantic_fact_hash": semantic_fact_hash,
        "presentation_input_hash": presentation_input_hash,
        # Bind the receipt to the exact frozen handoff received from Reporter.
        # ``payload`` below contains Designer-only defaults and layout
        # normalisation, so hashing it would make an otherwise immutable Card
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
