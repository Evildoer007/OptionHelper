"""Render frozen multi-candidate Card and Report deliveries.

The comparison payload already contains one public fact projection per
candidate.  This module only aligns those projections for reading; it never
selects a product, recalculates a metric, or merges contracts.
"""

from __future__ import annotations

import re
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import DesignerConfig
from .design_system_builder import build_design_system
from .renderer import (
    PUBLIC_BRAND,
    PARAMETER_SOURCE_LABELS,
    SECTION_ORDER,
    as_dict,
    as_list,
    backtest_summary_rows,
    canonical_greeks,
    display_text,
    detail_tables,
    esc,
    esc_rendered,
    item_list,
    parameter_table,
    public_parameter_rows,
    public_specialized_rows,
    render_html,
    render_presentation_content,
    rich_text,
    simple_table,
    text,
    validate_payload,
)
from .presentation_patch import validated_supplemental_sections


MULTICARD_TITLE = "多结构研究简报"
MULTIREPORT_TITLE = "多结构完整研究报告"
_MODULE_LABELS = {"ready": "已完成", "partial": "部分完成", "failed": "运行失败", "not_run": "未运行", "unsupported": "不适用", "pending": "待处理"}
_MODULE_STATUSES = frozenset(_MODULE_LABELS)


def _candidates(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    comparison = as_dict(payload.get("comparison"))
    rows = [as_dict(item) for item in as_list(comparison.get("candidates"))]
    if len(rows) < 2:
        raise ValueError("comparison至少需要两个候选。")
    for index, row in enumerate(rows, start=1):
        if not text(row.get("label")) or not text(row.get("title")) or not as_dict(row.get("facts")):
            raise ValueError(f"comparison.candidates[{index}]缺少公开标签、标题或冻结事实。")
        facts = as_dict(row.get("facts"))
        validate_payload(facts)
        validated_supplemental_sections(facts)
    return sorted(rows, key=lambda row: (int(row.get("rank")) if isinstance(row.get("rank"), int) else 10_000))


def _identity(meta: Mapping[str, Any]) -> str:
    rows = (("报告日期", meta.get("as_of_date")), ("候选数量", meta.get("candidate_count")))
    return "".join(
        f"<div><dt>{esc(label)}</dt><dd>{esc_rendered(display_text(value))}</dd></div>"
        for label, value in rows if text(value)
    )


def _metric_rows(module: Mapping[str, Any], *, include_greeks: bool = False) -> list[dict[str, Any]]:
    rows = [as_dict(item) for item in as_list(module.get("metrics"))]
    if include_greeks:
        rows.extend(canonical_greeks(as_list(module.get("greeks"))))
    return [row for row in rows if text(row.get("label")) and row.get("value") is not None]


def _card_pricing_rows(module: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Keep the compact-card metric ceiling without dropping supplied Greeks."""

    metrics = [as_dict(item) for item in as_list(module.get("metrics"))[:4]]
    return [
        row for row in [*metrics, *canonical_greeks(as_list(module.get("greeks")))]
        if text(row.get("label")) and row.get("value") is not None
    ]


def _card_backtest_rows(module: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Use the same complete evidence summary as a single Card."""

    return backtest_summary_rows(module)


def _term_rows(facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    highlights = [as_dict(item) for item in as_list(facts.get("contract_highlights"))]
    if highlights:
        return [row for row in highlights if text(row.get("label")) and text(row.get("value"))]
    parameters = as_dict(facts.get("parameters"))
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in ("common_input", "payoff_input"):
        for raw in as_list(parameters.get(group)):
            row = as_dict(raw)
            label = text(row.get("cn"))
            if label and label not in seen and text(row.get("value")):
                result.append({**row, "label": label})
                seen.add(label)
    return result


def _module_status(module: Mapping[str, Any]) -> str:
    status = text(module.get("status") or "pending").lower()
    label = _MODULE_LABELS.get(status, status)
    note = text(module.get("note"))
    return f"{label}：{note}" if note else label


def _aggregate_module_state(candidates: Sequence[Mapping[str, Any]], module_name: str) -> tuple[str, str]:
    """Summarise candidate states without promoting incomplete work to ready."""

    statuses = [
        text(as_dict(as_dict(candidate.get("facts")).get(module_name)).get("status") or "pending").lower()
        for candidate in candidates
    ]
    statuses = [status if status in _MODULE_STATUSES else "pending" for status in statuses]
    unique = set(statuses)
    aggregate = statuses[0] if len(unique) == 1 else "partial"
    counts = [
        f"{statuses.count(status)}个{_MODULE_LABELS[status]}"
        for status in _MODULE_LABELS
        if status in unique
    ]
    return aggregate, f"候选状态：{'，'.join(counts)}。"


def _candidate_supplements(
    payload: Mapping[str, Any],
    candidate: Mapping[str, Any],
    displayed_supplements: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Return candidate facts not already inserted by the effective patch."""

    del payload
    displayed = [tuple(nodes) for nodes in (displayed_supplements or {}).values()]
    candidate_sections = validated_supplemental_sections(as_dict(candidate.get("facts")))
    result: list[dict[str, Any]] = []
    for section_id, (title, content) in candidate_sections.items():
        if content in displayed:
            continue
        result.append({"id": section_id, "title": title, "content": list(content)})
    return result


def _candidate_supplement_html(
    payload: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    allow_charts: bool,
    displayed_supplements: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> str:
    blocks: list[str] = []
    for section in _candidate_supplements(payload, candidate, displayed_supplements):
        content = as_list(section.get("content"))
        if not allow_charts and any(text(as_dict(node).get("type")).lower() == "chart" for node in content):
            raise ValueError("多结构研究简报的候选补充章节不支持图表。")
        rendered = render_presentation_content(content)
        if rendered:
            blocks.append(
                '<div class="comparison-candidate-supplement">'
                f'<h4>{esc(candidate.get("label"))}：{esc(section.get("title"))}</h4>{rendered}</div>'
            )
    return "".join(blocks)


def _card_pricing_detail(facts: Mapping[str, Any], pricing: Mapping[str, Any]) -> str:
    blocks: list[str] = []
    metadata = [
        value for value in (
            f"估值日：{text(pricing.get('valuation_date'))}" if text(pricing.get("valuation_date")) else "",
            f"估值方法：{text(pricing.get('method'))}" if text(pricing.get("method")) else "",
        ) if value
    ]
    if metadata:
        blocks.append(f'<p class="card-data-note">{esc("；".join(metadata))}</p>')
    parameters = as_list(as_dict(facts.get("parameters")).get("pricing_input"))
    if parameters:
        pricing_parameter_table = parameter_table(parameters, "估值参数")
        if pricing_parameter_table:
            blocks.append("<h4>估值参数</h4>" + pricing_parameter_table)
    scenario_rows = as_list(pricing.get("scenario_rows"))
    if scenario_rows:
        blocks.append(simple_table(
            scenario_rows,
            [("scenario", "情景"), ("spot", "标的价格"), ("time", "剩余期限"), ("pv", "现值")],
            "定价情景",
        ))
    blocks.append(detail_tables(as_list(pricing.get("detail_tables"))))
    assumptions = item_list(as_list(pricing.get("assumptions")))
    if assumptions:
        blocks.append(f"<h4>估值假设</h4>{assumptions}")
    return "".join(block for block in blocks if block)


def _card_backtest_detail(facts: Mapping[str, Any], backtest: Mapping[str, Any]) -> str:
    blocks: list[str] = []
    if text(backtest.get("entry_rule")):
        blocks.append(f'<p class="card-data-note">入场规则：{esc(backtest.get("entry_rule"))}</p>')
    parameters = as_list(as_dict(facts.get("parameters")).get("backtest_input"))
    if parameters:
        backtest_parameter_table = parameter_table(parameters, "回测参数")
        if backtest_parameter_table:
            blocks.append("<h4>回测参数</h4>" + backtest_parameter_table)
    tables: list[dict[str, Any]] = []
    for raw in as_list(backtest.get("detail_tables")):
        table = as_dict(raw)
        title = text(table.get("title"))
        if title == "公共回测统计":
            continue
        if title == "产品专属统计":
            rows = public_specialized_rows(as_list(table.get("rows")))
            if not rows:
                continue
            table = {
                **table,
                "title": "结构统计",
                "columns": [("label", "指标"), ("value", "统计值")],
                "rows": rows,
            }
        tables.append(table)
    blocks.append(detail_tables(tables))
    limitations = item_list(as_list(backtest.get("limitations")))
    if limitations:
        blocks.append(f"<h4>数据与方法限制</h4>{limitations}")
    return "".join(block for block in blocks if block)


def _card_candidate_sections(
    payload: Mapping[str, Any],
    candidate: Mapping[str, Any],
    displayed_supplements: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    facts = as_dict(candidate.get("facts"))
    recommendation = as_dict(facts.get("recommendation"))
    pricing = as_dict(facts.get("pricing"))
    backtest = as_dict(facts.get("backtest"))
    risk = as_dict(facts.get("risk"))
    label = text(candidate.get("label"))
    preferred = bool(candidate.get("is_primary"))
    reason_points = [item for item in as_list(recommendation.get("reason_points")) if text(item)]
    if text(recommendation.get("reason")) and recommendation.get("reason") not in reason_points:
        reason_points.insert(0, recommendation.get("reason"))
    reason = "".join(f"<li>{rich_text(item)}</li>" for item in reason_points[:3] if text(item))
    terms = _term_rows(facts)
    pricing_status = text(pricing.get("status")).lower()
    backtest_status = text(backtest.get("status")).lower()
    pricing_rows = _card_pricing_rows(pricing) if pricing_status in {"ready", "partial"} else []
    backtest_rows = _card_backtest_rows(backtest) if backtest_status in {"ready", "partial"} else []
    if text(backtest.get("window")):
        backtest_rows.insert(0, {"label": "回测区间", "value": backtest.get("window")})
    risks = [item for item in as_list(risk.get("items")) if text(item)][:2]
    sections: dict[str, str] = {}
    if reason:
        sections["reason"] = f'<ul class="plain-list">{reason}</ul>'
    if terms:
        sections["contract_highlights"] = simple_table(
            terms, [("label", "条款"), ("value", "取值"), ("note", "说明")], f"{label}关键合同条款"
        )
    pricing_body = (
        simple_table(pricing_rows, [("label", "指标"), ("value", "数值")], f"{label}估值摘要")
        if pricing_rows
        else ""
    )
    if pricing_status in {"ready", "partial"}:
        pricing_body += _card_pricing_detail(facts, pricing)
    if pricing_status in {"partial", "failed"}:
        pricing_body = f'<p class="comparison-state" data-status="{esc(pricing_status or "pending")}">{esc(_module_status(pricing))}</p>' + pricing_body
    if pricing_body:
        sections["pricing"] = pricing_body
    backtest_body = (
        simple_table(backtest_rows, [("label", "指标"), ("value", "数值")], f"{label}回测摘要")
        if backtest_rows
        else ""
    )
    if backtest_status in {"ready", "partial"}:
        backtest_body += _card_backtest_detail(facts, backtest)
    if backtest_status in {"partial", "failed"}:
        backtest_body = f'<p class="comparison-state" data-status="{esc(backtest_status or "pending")}">{esc(_module_status(backtest))}</p>' + backtest_body
    if backtest_body:
        sections["backtest"] = backtest_body
    if risks:
        sections["risk"] = f'<ul class="risk-list">{"".join(f"<li>{rich_text(item)}</li>" for item in risks)}</ul>'
    return {
        "label": label,
        "title": text(candidate.get("title")),
        "underlyings": text(candidate.get("underlyings")),
        "preferred": preferred,
        "sections": sections,
        "supplement": _candidate_supplement_html(
            payload,
            candidate,
            allow_charts=False,
            displayed_supplements=displayed_supplements,
        ),
    }


def render_multicard_html(
    payload: Mapping[str, Any],
    *,
    config: DesignerConfig,
    template_shell: str,
    section_definition: Sequence[tuple[str, str, str]],
    appended_content: Mapping[str, Sequence[Mapping[str, Any]]],
) -> str:
    candidates = _candidates(payload)
    meta = as_dict(payload.get("meta"))
    title = text(meta.get("title")) or MULTICARD_TITLE
    template = config.read_template(template_shell)
    cards = []
    for candidate in candidates:
        item = _card_candidate_sections(payload, candidate, appended_content)
        blocks = []
        for section_id, label, key in section_definition:
            content = item["sections"].get(key, "")
            if key != "recommendation" and content:
                content = re.sub(r'<caption\b[^>]*>.*?</caption>', '', content, flags=re.S)
                blocks.append(f'<section><h3>{esc(label)}</h3>{content}</section>')
        cards.append(f'<article class="comparison-product-card"><header><p>{esc(item["underlyings"])}</p><h2>{esc(item["title"])}</h2></header>' + "".join(blocks) + item["supplement"] + "</article>")
    body = '<section class="comparison-product-cards">' + "".join(cards) + "</section>"
    for section_id, section_title, _block in section_definition:
        content = render_presentation_content(appended_content.get(section_id, ()))
        if content:
            body += f'<section class="card-appended"><h2>{esc(section_title)}</h2>{content}</section>'
    return (
        template.replace("__TITLE__", esc(title))
        .replace("__REPORT_THEME__", config.read_report_theme())
        .replace("__BRAND__", esc(PUBLIC_BRAND))
        .replace("__IDENTITY__", _identity({**meta, "candidate_count": len(candidates)}))
        .replace("__COMPARISON_BODY__", body)
    )


def _comparison_table(candidates: Sequence[Mapping[str, Any]], rows_by_candidate: Sequence[list[Mapping[str, Any]]], title: str) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for start in range(0, len(candidates), 3):
        group = candidates[start:start + 3]
        group_rows = rows_by_candidate[start:start + 3]
        keys: list[str] = []
        values: list[dict[str, Mapping[str, Any]]] = []
        for candidate_index, rows in enumerate(group_rows):
            mapped: dict[str, Mapping[str, Any]] = {}
            for raw in rows:
                row = as_dict(raw)
                label = text(row.get("label") or row.get("cn"))
                if not label:
                    continue
                existing = mapped.get(label)
                if existing is not None:
                    if (
                        existing.get("value") != row.get("value")
                        or text(existing.get("value_format")) != text(row.get("value_format"))
                    ):
                        candidate_label = text(group[candidate_index].get("label"))
                        raise ValueError(f"{candidate_label}存在同名但取值冲突的指标：{label}。")
                    continue
                mapped[label] = row
                if label not in keys:
                    keys.append(label)
            values.append(mapped)
        columns = [{"key": "metric", "label": "指标"}]
        for index, candidate in enumerate(group):
            columns.append({"key": f"c{index}", "label": text(candidate.get("label"))})
        table_rows = []
        for label in keys:
            row: dict[str, Any] = {"metric": label}
            for index, mapped in enumerate(values):
                item = mapped.get(label)
                row[f"c{index}"] = display_text(item.get("value"), item.get("value_format")) + text(item.get("value_suffix")) if item else "—"
            table_rows.append(row)
        if table_rows:
            tables.append({"title": title, "columns": columns, "rows": table_rows})
    return tables


def _compatible_chart_groups(candidates: Sequence[Mapping[str, Any]], module_name: str) -> list[dict[str, Any]]:
    groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    facets: list[dict[str, Any]] = []
    for candidate in candidates:
        module = as_dict(as_dict(candidate.get("facts")).get(module_name))
        if text(module.get("status")).lower() != "ready":
            continue
        for raw in as_list(module.get("charts")):
            spec = as_dict(raw)
            chart_type = text(spec.get("type") or "line").lower()
            if chart_type == "heatmap":
                facet = deepcopy(spec)
                facet["title"] = f'{text(candidate.get("label"))}：{text(spec.get("title") or "曲面")}'
                facet["id"] = f'{module_name}-{len(facets) + 1}-facet'
                facets.append(facet)
                continue
            signature = json.dumps({
                "title": text(spec.get("title")), "type": chart_type, "x": as_list(spec.get("x")),
                "x_axis_name": text(spec.get("x_axis_name")), "y_axis_name": text(spec.get("y_axis_name")),
                "value_format": text(spec.get("value_format") or "number"), "value_suffix": text(spec.get("value_suffix")),
                "source_note": text(spec.get("source_note")),
            }, ensure_ascii=False, sort_keys=True)
            groups.setdefault(signature, []).append((text(candidate.get("label")), spec))
    merged: list[dict[str, Any]] = []
    for signature, items in groups.items():
        base = json.loads(signature)
        for offset in range(0, len(items), 4):
            chunk = items[offset:offset + 4]
            spec = {**base, "id": f'{module_name}-comparison-{len(merged) + 1}', "series": []}
            for label, source in chunk:
                for series in as_list(source.get("series")):
                    item = as_dict(series)
                    if text(item.get("name")) and isinstance(item.get("data"), list):
                        spec["series"].append({"name": f'{label}·{text(item.get("name"))}', "data": deepcopy(item["data"])})
            if spec["series"]:
                spec["accessibility_summary"] = f'对比{"、".join(label for label, _ in chunk)}的同口径数据。'
                merged.append(spec)
    return [*merged, *facets]


def _parameter_detail_table(
    candidate: Mapping[str, Any],
    facts: Mapping[str, Any],
    group: str,
    title: str,
) -> list[dict[str, Any]]:
    rows = public_parameter_rows(as_list(as_dict(facts.get("parameters")).get(group)))
    if not rows:
        return []
    visible_rows = [
        {
            **as_dict(row),
            "source": PARAMETER_SOURCE_LABELS.get(text(as_dict(row).get("source")), text(as_dict(row).get("source"))),
        }
        for row in rows
    ]
    return [{
        "title": f'{text(candidate.get("label"))}：{title}',
        "columns": [("cn", "条款"), ("symbol", "符号"), ("value", "取值"), ("source", "来源")],
        "rows": visible_rows,
    }]


def _attributed_detail_tables(
    candidate: Mapping[str, Any],
    module: Mapping[str, Any],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    label = text(candidate.get("label"))
    for raw in as_list(module.get("detail_tables")):
        table = deepcopy(as_dict(raw))
        if not text(table.get("title")) or not as_list(table.get("columns")) or not as_list(table.get("rows")):
            continue
        if text(table.get("title")) == "产品专属统计":
            rows = public_specialized_rows(as_list(table.get("rows")))
            if not rows:
                continue
            table["title"] = "结构统计"
            table["rows"] = rows
            table["columns"] = [("label", "指标"), ("value", "统计值")]
        table["title"] = f'{label}：{text(table.get("title"))}'
        result.append(table)
    return result


def _candidate_pricing_tables(candidate: Mapping[str, Any], facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    pricing = as_dict(facts.get("pricing"))
    result = _parameter_detail_table(candidate, facts, "pricing_input", "估值参数")
    scenario_rows = as_list(pricing.get("scenario_rows"))
    if scenario_rows:
        result.append({
            "title": f'{text(candidate.get("label"))}：定价情景',
            "columns": [("scenario", "情景"), ("spot", "标的价格"), ("time", "剩余期限"), ("pv", "现值")],
            "rows": deepcopy(scenario_rows),
        })
    result.extend(_attributed_detail_tables(candidate, pricing))
    assumptions = [text(item) for item in as_list(pricing.get("assumptions")) if text(item)]
    if assumptions:
        result.append({
            "title": f'{text(candidate.get("label"))}：估值假设',
            "columns": [("item", "假设")],
            "rows": [{"item": item} for item in assumptions],
        })
    return result


def _candidate_backtest_tables(candidate: Mapping[str, Any], facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    backtest = as_dict(facts.get("backtest"))
    result = _parameter_detail_table(candidate, facts, "backtest_input", "回测参数")
    result.extend(_attributed_detail_tables(candidate, backtest))
    limitations = [text(item) for item in as_list(backtest.get("limitations")) if text(item)]
    if limitations:
        result.append({
            "title": f'{text(candidate.get("label"))}：数据与方法限制',
            "columns": [("item", "限制")],
            "rows": [{"item": item} for item in limitations],
        })
    return result


def _comparison_candidate_recommendations(
    payload: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    displayed_supplements: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        recommendation = as_dict(as_dict(candidate.get("facts")).get("recommendation"))
        result.append({
            "label": text(candidate.get("label")),
            "title": text(candidate.get("title")),
            "underlyings": text(candidate.get("underlyings")),
            "is_primary": bool(candidate.get("is_primary")),
            "reason": recommendation.get("reason"),
            "reason_points": deepcopy(as_list(recommendation.get("reason_points"))),
            "suitable_for": deepcopy(as_list(recommendation.get("suitable_for"))),
            "not_suitable_for": deepcopy(as_list(recommendation.get("not_suitable_for"))),
            "tradeoffs": deepcopy(as_list(recommendation.get("tradeoffs"))),
            "supplemental_sections": _candidate_supplements(
                payload,
                candidate,
                displayed_supplements,
            ),
        })
    return result


def _aggregate_report(
    payload: Mapping[str, Any],
    displayed_supplements: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    candidates = _candidates(payload)
    meta = dict(as_dict(payload.get("meta")))
    has_recommendation = any(
        as_dict(as_dict(item.get("facts")).get("recommendation")).get("has_recommendation") is not False
        for item in candidates
    )
    preferred = next((item for item in candidates if item.get("is_primary")), None)
    underlyings = list(dict.fromkeys(text(item.get("underlyings")) for item in candidates if text(item.get("underlyings"))))
    risk_items: list[str] = []
    term_rows: list[list[Mapping[str, Any]]] = []
    parameter_detail_tables: list[dict[str, Any]] = []
    pricing_rows: list[list[Mapping[str, Any]]] = []
    backtest_rows: list[list[Mapping[str, Any]]] = []
    payoff_rows: list[list[Mapping[str, Any]]] = []
    payoff_figures: list[dict[str, Any]] = []
    payoff_formulas: list[dict[str, Any]] = []
    pricing_detail_tables: list[dict[str, Any]] = []
    backtest_detail_tables: list[dict[str, Any]] = []
    for candidate in candidates:
        facts = as_dict(candidate.get("facts"))
        term_rows.append(_term_rows(facts))
        for group, title in (("common_input", "合同参数"), ("payoff_input", "收益结构参数")):
            parameter_detail_tables.extend(_parameter_detail_table(candidate, facts, group, title))
        pricing = as_dict(facts.get("pricing"))
        pricing_status = text(pricing.get("status")).lower()
        concise_pricing_rows = [
            {key: value for key, value in row.items() if key not in {"unit", "note", "basis"}}
            for row in _metric_rows(pricing, include_greeks=True)
        ] if pricing_status in {"ready", "partial"} else []
        pricing_rows.append([
            {"label": "模块状态", "value": _module_status(pricing)},
            *([{"label": "估值方法", "value": pricing.get("method")}] if text(pricing.get("method")) else []),
            *([{"label": "估值日", "value": pricing.get("valuation_date")}] if text(pricing.get("valuation_date")) else []),
            *concise_pricing_rows,
        ])
        if pricing_status in {"ready", "partial"}:
            pricing_detail_tables.extend(_candidate_pricing_tables(candidate, facts))
        backtest = as_dict(facts.get("backtest"))
        backtest_status = text(backtest.get("status")).lower()
        rows = (
            _metric_rows(backtest)
            + public_specialized_rows(as_list(backtest.get("card_metrics")))
            + public_specialized_rows(as_list(backtest.get("event_statistics")))
            if backtest_status in {"ready", "partial"} else []
        )
        if backtest_status in {"ready", "partial"} and text(backtest.get("window")):
            rows.insert(0, {"label": "回测区间", "value": backtest.get("window")})
        if backtest_status in {"ready", "partial"} and text(backtest.get("entry_rule")):
            rows.insert(1 if rows and text(rows[0].get("label")) == "回测区间" else 0, {
                "label": "入场规则", "value": backtest.get("entry_rule"),
            })
        backtest_rows.append([{"label": "模块状态", "value": _module_status(backtest)}, *rows])
        if backtest_status in {"ready", "partial"}:
            backtest_detail_tables.extend(_candidate_backtest_tables(candidate, facts))
        payoff = as_dict(facts.get("payoff"))
        payoff_status = text(payoff.get("status")).lower()
        payoff_facts = [
            {"label": text(item.get("title")) or "情景", "value": text(item.get("rule"))}
            for item in (as_dict(raw) for raw in as_list(payoff.get("scenarios")))
            if payoff_status in {"ready", "partial"} and (text(item.get("title")) or text(item.get("rule")))
        ]
        payoff_rows.append([{"label": "模块状态", "value": _module_status(payoff)}, *payoff_facts])
        if payoff_status in {"ready", "partial"} and text(payoff.get("report_svg_path")):
            payoff_figures.append({"label": text(candidate.get("label")), "path": payoff.get("report_svg_path")})
        if payoff_status in {"ready", "partial"} and (
            text(payoff.get("formula_mathml")) or text(payoff.get("formula"))
        ):
            payoff_formulas.append({
                "label": text(candidate.get("label")),
                "formula_mathml": payoff.get("formula_mathml"),
                "formula": payoff.get("formula"),
            })
        risk_items.extend(f'{text(candidate.get("label"))}：{text(item)}' for item in as_list(as_dict(facts.get("risk")).get("items")) if text(item))
    payoff_status, payoff_note = _aggregate_module_state(candidates, "payoff")
    pricing_status, pricing_note = _aggregate_module_state(candidates, "pricing")
    backtest_status, backtest_note = _aggregate_module_state(candidates, "backtest")
    return {
        "schema": payload.get("schema"),
        "meta": {**meta, "title": text(meta.get("title")) or MULTIREPORT_TITLE},
        "sections": list(SECTION_ORDER),
        "conclusion": {
            "has_recommendation": has_recommendation,
            "structure_name": f'{len(candidates)}个候选结构对比',
            "underlyings": "、".join(underlyings),
            "reasons": [f'{"按冻结排名" if has_recommendation else "按所选顺序"}列示{len(candidates)}个候选，合同、估值和回测事实分别绑定。'] + ([f'首选方案为{text(preferred.get("label"))}：{text(preferred.get("title"))}。'] if preferred else []),
            "risk_summary": risk_items[:2],
        },
        "recommendation": {
            "has_recommendation": has_recommendation,
            "headline": "候选结构",
            "comparison_candidates": _comparison_candidate_recommendations(
                payload,
                candidates,
                displayed_supplements,
            ),
        },
        "parameters": {"detail_tables": [*_comparison_table(candidates, term_rows, "关键条款"), *parameter_detail_tables]},
        "payoff": {
            "status": payoff_status, "note": payoff_note, "comparison_view": True,
            "report_svg_paths": payoff_figures,
            "comparison_formulas": payoff_formulas,
            "detail_tables": _comparison_table(candidates, payoff_rows, "收益情景对比"),
        },
        "pricing": {
            "status": pricing_status, "note": pricing_note, "comparison_view": True,
            "detail_tables": [
                *_comparison_table(candidates, pricing_rows, "估值定价对比"),
                *pricing_detail_tables,
            ],
            "charts": _compatible_chart_groups(candidates, "pricing"),
        },
        "backtest": {
            "status": backtest_status, "note": backtest_note, "comparison_view": True,
            "detail_tables": [
                *_comparison_table(candidates, backtest_rows, "历史回测对比"),
                *backtest_detail_tables,
            ],
            "charts": _compatible_chart_groups(candidates, "backtest"),
        },
        "risk": {"items": risk_items},
    }


def render_multireport_html(
    payload: Mapping[str, Any],
    input_dir: Path,
    echarts_path: str,
    *,
    config: DesignerConfig,
    template_shell: str,
    section_definition: Sequence[tuple[str, str, str]],
    appended_content: Mapping[str, Sequence[Mapping[str, Any]]],
) -> str:
    overview = _aggregate_report(payload, appended_content)
    overview["parameters"]["detail_tables"] = _comparison_table(_candidates(payload),
        [_term_rows(as_dict(item.get("facts"))) for item in _candidates(payload)], "关键条款")
    for module in ("payoff", "pricing", "backtest"):
        overview[module] = {"status": "not_run"}
    overview_sections = [item for item in section_definition if item[2] != "recommendation"]
    return render_html(
        overview, input_dir, echarts_path,
        design_system_id=build_design_system().design_system_id,
        config=config,
        section_definition=overview_sections,
        appended_content=appended_content,
        template_shell=template_shell,
        product_sections=_candidates(payload),
    )


__all__ = ["MULTICARD_TITLE", "MULTIREPORT_TITLE", "render_multicard_html", "render_multireport_html"]
