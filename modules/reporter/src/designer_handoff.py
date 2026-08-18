"""Reporter到Designer的唯一交接。

Reporter在本文件中只把冻结事实投影为Designer已发布的payload，然后通过宿主
注入的公开Designer ModulePort调用`action=render`。不维护CSS、配色、字体或自制HTML。
"""

from __future__ import annotations

import base64
from copy import deepcopy
from math import isfinite
from numbers import Real
from pathlib import Path
import re
from typing import Any, Mapping

from runtime.ports.module import ModulePort

from .artifact_validator import validate_designer_artifact
from .models import (
    CARD_SECTION_ORDER,
    REPORT_SECTION_ORDER,
    SCHEMA_DESIGN_BRIEF,
    SCHEMA_DESIGNER_PAYLOAD,
    ReporterError,
    ReportRequest,
    stable_hash,
)
from .report_unit_builder import normalize_parameter_groups, public_chart_specs


DESIGNER_PAYLOAD_SCHEMA = SCHEMA_DESIGNER_PAYLOAD
DESIGN_BRIEF_SCHEMA = SCHEMA_DESIGN_BRIEF
DESIGNER_CONSTRAINTS = [
    "Designer只处理字体、颜色、组件、间距、图表和版式，不得新增或改写金融事实。",
    "不得删除上游运行状态、来源、限制或风险提示。",
    "未运行、失败、不支持和未验证模块不得包装为结论或默认图。",
    "HTML报告使用离线ECharts；公式只以MathML或数学文本呈现，不使用代码块。",
]
_DISPLAY_STATUS = {"ready", "not_run", "failed", "unsupported", "partial"}
_INTERNAL_DELIVERY_TEXT = re.compile(
    r"(?i)(optionhelper|app\s+host|reporter|designer|reportunit|modulerun|审计|run[_ ]?id|runref|source[_ ]?id|manifest|hash|文件路径|物理路径|module[_ ]?run)"
)
_FORBIDDEN_PUBLIC_ECONOMICS = re.compile(
    r"(?i)(cny|人民币|名义本金|amount|\bpnl\b|p\s*&\s*l|cashflow|现金流|每100份合同|points_100|点数)"
)
_PUBLIC_MODULE_NAMES = {"payoff": "收益结构", "pricing": "估值定价", "backtest": "历史回测"}
_DISPLAY_MODULE_FIELDS = {
    "payoff": ("formula", "formula_mathml", "scenarios"),
    "pricing": ("method", "valuation_date", "metrics", "greeks", "assumptions", "scenario_rows", "charts"),
    "backtest": ("window", "entry_rule", "metrics", "card_metrics", "detail_tables", "event_statistics", "charts"),
}
_FORBIDDEN_MODULE_FIELD_FRAGMENTS = (
    "amount", "pnl", "cashflow", "points_100", "point_value", "currency",
    "source_path", "storage_ref", "run_id", "runref", "manifest", "hash", "audit",
    "file_path", "directory", "result_dir", "path",
    "candidate_id", "product_id", "task_id", "tenant_id", "analysis_case_id",
    "source_id", "execution_fingerprint", "contract_fingerprint", "catalog_version",
    "product_version",
)
_PUBLIC_VALUE_KEYS = {
    "label", "value", "note", "value_format", "title", "rule", "formula", "formula_mathml",
    "id", "method", "valuation_date", "name", "type", "data", "x", "y", "series", "columns", "rows",
    "event", "monitor", "sample_count", "valid_return_sample_count", "positive_return_count", "win_rate",
    "average_gross_return", "median_gross_return", "minimum_gross_return", "maximum_gross_return",
    "max_loss_gross_return", "trigger_count", "trigger_rate", "average_days", "median_days", "count",
    "rate", "year", "average", "median", "minimum", "maximum", "true_rate", "condition", "payoff",
    "source", "cn", "en", "symbol", "description", "accessibility_summary", "source_note",
    "x_axis_name", "y_axis_name", "z_axis_name",
}
_PUBLIC_LIMITATION_TEXT = {
    "exchange_calendar_not_exposed_by_data_source_weekday_validation_only": "数据源未提供可核验的交易日历，本次仅按工作日校验样本日期。",
    "no_nav_curve_is_generated": "本次回测按到期损益统计，未生成持有期净值曲线。",
    "payment_calendar_not_exposed_by_shared_contract_core": "合同事实未提供独立付款日历，相关日期按当前合同约定处理。",
    "coverage_does_not_claim_unobserved_scenarios_were_economically_exercised": "情景覆盖仅表示样本中观察到的路径，不代表未观察情景已实际发生。",
    "in_memory_historical_data_without_persisted_data_asset": "历史数据仅绑定本次运行，未形成可复用的数据资产。",
}
_PUBLIC_CANDIDATE_INDEX = ("", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十")
_QUOTE_EXCLUDED_TERMS = {"Delta", "Gamma", "Vega", "Theta", "Rho"}
_QUOTE_PREMIUM_TERMS = {
    "Pi_0": "期权费",
    "P_net": "净期权费",
    "p": "期权费率",
}
_QUOTE_TERM_PRIORITY = {
    "期限": 10,
    "执行价": 20,
    "执行价1": 21,
    "执行价2": 22,
    "执行价3": 23,
    "期权费": 30,
    "净期权费": 31,
    "期权费率": 32,
    "敲入水平": 40,
    "敲出水平": 41,
    "障碍水平": 42,
    "票息": 50,
    "票息率": 51,
    "参与率": 60,
    "观察设置": 70,
    "结算方式": 80,
}
_GENERIC_RECOMMENDATION_TEXT = frozenset({
    "与已确认市场判断和风险边界相符。",
    "可承受结构化产品风险。",
    "候选排序和适用边界来自冻结的推荐结果。",
})


def _public_text(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    if _FORBIDDEN_PUBLIC_ECONOMICS.search(text):
        return fallback
    text = re.sub(r"(?i)optionhelper", "", text).strip(" -_/|：:")
    return text or fallback


def _public_strings(value: Any) -> list[str]:
    """Keep reader facts while dropping delivery/protocol vocabulary."""

    if not isinstance(value, list):
        return []
    return [
        text for item in value
        if isinstance(item, str)
        and not _INTERNAL_DELIVERY_TEXT.search(item)
        and not _FORBIDDEN_PUBLIC_ECONOMICS.search(item)
        and (text := _public_text(item))
    ]


def _public_recommendation(value: Any) -> dict[str, Any]:
    """Whitelist only reader-facing recommendation facts for Designer."""

    raw = value if isinstance(value, Mapping) else {}
    underlyings = _public_strings(raw.get("underlyings"))
    product_name = _public_text(raw.get("product_name"))
    reason = _public_text(raw.get("reason"))
    if reason in _GENERIC_RECOMMENDATION_TEXT:
        reason = ""
    result = {
        "product_name": product_name,
        "headline": _public_text(raw.get("headline") or product_name),
        "structure_name": _public_text(raw.get("structure_name") or product_name),
        "underlyings": "、".join(underlyings),
        "reason": reason,
        "suitable_for": [item for item in _public_strings(raw.get("suitable_for")) if item not in _GENERIC_RECOMMENDATION_TEXT],
        "not_suitable_for": _public_strings(raw.get("not_suitable_for")),
        "main_risks": _public_strings(raw.get("main_risks")),
    }
    return {
        key: item for key, item in result.items()
        if item != "" and item != []
    }


def _public_limitations(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for item in values:
        if (
            not isinstance(item, str) or not item.strip()
            or _INTERNAL_DELIVERY_TEXT.search(item)
            or _FORBIDDEN_PUBLIC_ECONOMICS.search(item)
        ):
            continue
        value = item.strip()
        prefixed = re.match(r"(?i)^(payoff|pricing|backtest)\s*[：:]\s*(.+)$", value)
        if prefixed:
            value = prefixed.group(2).strip()
        value = _PUBLIC_LIMITATION_TEXT.get(value, value)
        if re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+){2,}", value):
            value = "上游分析存在未公开说明的技术限制，相关结论仅在已列示证据范围内适用。"
        for internal, public in _PUBLIC_MODULE_NAMES.items():
            value = re.sub(rf"(?i)^{internal}\s*[：:]\s*", f"{public}：", value)
        result.append(value)
    return result


def _public_module_value(value: Any) -> Any:
    """Remove any non-reader economics from a selected module field."""

    if isinstance(value, str):
        return _public_text(value)
    if isinstance(value, list):
        return [item for raw in value if (item := _public_module_value(raw)) not in (None, "", [], {})]
    if isinstance(value, Mapping):
        return {
            str(key): item
            for key, raw in value.items()
            if str(key) in _PUBLIC_VALUE_KEYS
            if not any(fragment in str(key).casefold() for fragment in _FORBIDDEN_MODULE_FIELD_FRAGMENTS)
            if (item := _public_module_value(raw)) not in (None, "", [], {})
        }
    return deepcopy(value)


def _display_module(name: str, raw: Mapping[str, Any], *, artifact_paths: Mapping[str, str]) -> dict[str, Any]:
    source = raw if isinstance(raw, Mapping) else {}
    value = {
        "status": str(source.get("status", "not_run")),
        **{
            field: _public_module_value(source[field])
            for field in _DISPLAY_MODULE_FIELDS[name]
            if field in source and _public_module_value(source[field]) not in (None, "", [], {})
        },
    }
    status = value["status"]
    if status in {"cancelled", "timed_out"}:
        value["status"] = "failed"
    elif status not in _DISPLAY_STATUS:
        # 冻结ReportUnit保留原始状态供受控清单追溯；公开Designer投影只使用
        # 已发布的展示状态和自然语言说明，不携带内部状态枚举。
        value["status"] = "not_run"
    # A missing module is not reader content.  Only an upstream-provided,
    # public explanation remains eligible for Designer to show as a genuine
    # partial/failed state; Reporter never manufactures an "未运行" paragraph.
    note = _public_text(source.get("note"))
    if note:
        value["note"] = note
    artifacts = source.get("artifacts", [])
    value["charts"] = public_chart_specs(value.get("charts"))
    if value.get("status") == "ready":
        for artifact in artifacts if isinstance(artifacts, list) else []:
            if not isinstance(artifact, Mapping):
                continue
            if artifact.get("media_type") == "image/svg+xml":
                key = str(artifact.get("content_hash", ""))
                svg_path = artifact_paths.get(key)
                if svg_path:
                    value["report_svg_path"] = svg_path
                    break
    return value


def _sections(unit: Mapping[str, Any], request: ReportRequest) -> list[str]:
    if request.output_type == "quote":
        return []
    if request.output_type == "report":
        # A Report is a complete reader document.  Modules without usable
        # evidence retain their own truthful state in the corresponding
        # chapter rather than disappearing from the fixed reader structure.
        return list(REPORT_SECTION_ORDER)
    return list(CARD_SECTION_ORDER)


def _report_title(unit: Mapping[str, Any], request: ReportRequest) -> str:
    if request.output_type == "quote":
        return "推荐结构及参考报价"
    subject = unit.get("subject") if isinstance(unit.get("subject"), Mapping) else {}
    product_name = _public_text(subject.get("product_name"))
    raw_underlyings = subject.get("underlyings")
    underlyings = [
        _public_text(item)
        for item in raw_underlyings
        if _public_text(item)
    ] if isinstance(raw_underlyings, (list, tuple)) else []
    if not product_name:
        raise ReporterError("正式交付必须包含产品名称")
    if not underlyings:
        raise ReporterError("正式单产品交付必须包含标的")
    suffix = "推荐卡片" if request.output_type == "card" else "推荐报告"
    return f"{'、'.join(underlyings)}{product_name}{suffix}"


def _public_unit_limitations(unit: Mapping[str, Any]) -> list[str]:
    """仅把已冻结限制投影为读者可见文字，不交接运行记录或来源哈希。"""

    content = unit.get("content") if isinstance(unit.get("content"), Mapping) else {}
    original = content.get("audit") if isinstance(content.get("audit"), Mapping) else {}
    modules = unit.get("modules") if isinstance(unit.get("modules"), Mapping) else {}
    limitations = list(original.get("limitations", [])) if isinstance(original.get("limitations"), list) else []
    for value in modules.values():
        raw = value if isinstance(value, Mapping) else {}
        for limitation in raw.get("limitations", []) if isinstance(raw.get("limitations"), list) else []:
            if isinstance(limitation, str) and limitation:
                limitations.append(limitation)
    return _public_limitations(limitations)


def _report_as_of_date(request: ReportRequest, content: Mapping[str, Any]) -> str:
    supplied = str(request.metadata.get("as_of_date", "")).strip()
    if supplied:
        return supplied
    pricing = content.get("pricing") if isinstance(content.get("pricing"), Mapping) else {}
    valuation_date = str(pricing.get("valuation_date") or "").strip()
    if valuation_date:
        return valuation_date
    return ""


def _rows_by_label(rows: Any) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("label")): deepcopy(dict(row))
        for row in rows if isinstance(row, Mapping) and str(row.get("label", "")).strip()
    } if isinstance(rows, list) else {}


def _structured_conclusion(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project a concise digest from frozen public facts without filler."""

    recommendation = payload.get("recommendation") if isinstance(payload.get("recommendation"), Mapping) else {}
    pricing = payload.get("pricing") if isinstance(payload.get("pricing"), Mapping) else {}
    backtest = payload.get("backtest") if isinstance(payload.get("backtest"), Mapping) else {}
    risk = payload.get("risk") if isinstance(payload.get("risk"), Mapping) else {}

    reasons: list[str] = []
    reason = _public_text(recommendation.get("reason"))
    if reason:
        reasons.append(reason)
    suitable = recommendation.get("suitable_for")
    if isinstance(suitable, list):
        reasons.extend(_public_text(item) for item in suitable if _public_text(item))

    valuation_summary = []
    if pricing.get("status") == "ready" and isinstance(pricing.get("metrics"), list):
        valuation_summary = [deepcopy(dict(row)) for row in pricing["metrics"] if isinstance(row, Mapping)][:3]
    greeks = _rows_by_label(pricing.get("greeks")) if pricing.get("status") == "ready" else {}
    greeks_summary = [greeks[label] for label in ("Delta", "Vega") if label in greeks]

    backtest_rows = _rows_by_label(backtest.get("metrics")) if backtest.get("status") == "ready" else {}
    if backtest.get("status") == "ready":
        for table in backtest.get("detail_tables", []) if isinstance(backtest.get("detail_tables"), list) else []:
            if not isinstance(table, Mapping) or table.get("title") != "公共回测统计":
                continue
            for label, row in _rows_by_label(table.get("rows")).items():
                backtest_rows.setdefault(label, row)
    backtest_summary = [
        backtest_rows[label]
        for label in (
            "样本数", "胜率", "历史正收益样本占比",
            "平均收益", "最大亏损",
        )
        if label in backtest_rows and backtest_rows[label].get("value") is not None
    ]

    risk_values = [
        _public_text(item) for item in risk.get("items", [])
        if isinstance(item, str) and _public_text(item)
    ] if isinstance(risk.get("items"), list) else []
    if not risk_values and isinstance(recommendation.get("main_risks"), list):
        risk_values = [_public_text(item) for item in recommendation["main_risks"] if _public_text(item)]
    return {
        "structure_name": _public_text(recommendation.get("structure_name") or recommendation.get("product_name")),
        "underlyings": _public_text(recommendation.get("underlyings")),
        "reasons": list(dict.fromkeys(reasons))[:3],
        "valuation_summary": valuation_summary,
        "greeks_summary": greeks_summary,
        "backtest_summary": backtest_summary[:4],
        "risk_summary": list(dict.fromkeys(risk_values))[:2],
    }


def _quote_term_value(row: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return one public, reader-facing contract term for a Quote row."""

    label = _public_text(row.get("label"))
    value = _public_text(row.get("value"))
    note = _public_text(row.get("note"))
    if not label or not value or label in _QUOTE_EXCLUDED_TERMS:
        return None
    return label, f"{value}{note}" if label == "期限" and note else value


def _quote_premium_value(symbol: str, value: Any) -> str:
    """Format only the contract's normalized premium convention for Quote."""

    if isinstance(value, Real) and not isinstance(value, bool) and isfinite(float(value)):
        percent = float(value) * 100 if symbol == "p" else float(value)
        return f"{percent:,.2f}".rstrip("0").rstrip(".") + "%"
    return _public_text(value)


def _quote_terms(unit: Mapping[str, Any], content: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Return basic contract terms only, including the normalized option premium."""

    highlights = content.get("contract_highlights") if isinstance(content.get("contract_highlights"), list) else []
    terms = [item for highlight in highlights if isinstance(highlight, Mapping) and (item := _quote_term_value(highlight))]
    labels = {label for label, _ in terms}
    contract = unit.get("contract") if isinstance(unit.get("contract"), Mapping) else {}
    raw_terms = contract.get("terms") if isinstance(contract.get("terms"), Mapping) else {}
    for symbol, label in _QUOTE_PREMIUM_TERMS.items():
        if label in labels or symbol not in raw_terms:
            continue
        value = _quote_premium_value(symbol, raw_terms[symbol])
        if value:
            terms.append((label, value))
            labels.add(label)
    return terms


def _quote_from_units(units: list[Mapping[str, Any]], request: ReportRequest) -> dict[str, Any]:
    """Build the standard Quote table from explicitly selected frozen runs."""

    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for unit in units:
        content = unit.get("content") if isinstance(unit.get("content"), Mapping) else {}
        recommendation = _public_recommendation(content.get("recommendation"))
        subject = unit.get("subject") if isinstance(unit.get("subject"), Mapping) else {}
        structure = _public_text(
            recommendation.get("structure_name")
            or recommendation.get("product_name")
            or subject.get("product_name"),
            "期权结构",
        )
        underlyings = tuple(_public_strings(subject.get("underlyings")))
        if not underlyings:
            raise ReporterError("Quote合同快照缺少真实挂钩标的")
        terms = _quote_terms(unit, content)
        if not any(label == "期限" for label, _ in terms) or len(terms) < 2:
            raise ReporterError("Quote合同快照缺少期限或可展示的基本条款，请重新选择有效运行结果")
        group = grouped.setdefault(underlyings, {"rows": [], "labels": []})
        row = {"structure": structure}
        for label, value in _quote_terms(unit, content):
            if label in row:
                continue
            row[label] = value
            if label not in group["labels"]:
                group["labels"].append(label)
        group["rows"].append(row)

    groups: list[dict[str, Any]] = []
    for underlyings, group in grouped.items():
        labels = sorted(
            group["labels"],
            key=lambda label: (_QUOTE_TERM_PRIORITY.get(label, 999), group["labels"].index(label)),
        )
        columns = [{"key": "structure", "label": "结构类型", "format": "text"}]
        label_keys: dict[str, str] = {}
        for index, label in enumerate(labels, start=1):
            key = f"term_{index}"
            label_keys[label] = key
            columns.append({"key": key, "label": label, "format": "text"})
        rows = []
        for raw_row in group["rows"]:
            row = {"structure": raw_row["structure"]}
            for label in labels:
                row[label_keys[label]] = raw_row.get(label, "—")
            rows.append(row)
        title = f"挂钩标的：{'、'.join(underlyings)}" if len(underlyings) == 1 else f"挂钩标的组合：{'、'.join(underlyings)}"
        groups.append({"title": title, "columns": columns, "rows": rows})

    if not groups:
        raise ReporterError("参考报价缺少可展示的已选结构")
    as_of_date = str(request.metadata.get("as_of_date", "")).strip()
    return {
        **({"quote_date": as_of_date} if as_of_date else {}),
        "note": "本表按本次明确选择的结构及参数运行结果整理。实际交易前请与交易台确认最终报价。",
        "groups": groups,
    }


def build_designer_payload(
    unit: Mapping[str, Any],
    request: ReportRequest,
    *,
    artifact_paths: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """从一个不可变ReportUnit派生Designer可渲染负载。"""

    content = unit.get("content") if isinstance(unit.get("content"), Mapping) else {}
    artifact_paths = artifact_paths or {}
    if request.output_type == "quote":
        return {
            "schema": DESIGNER_PAYLOAD_SCHEMA,
            "meta": {
                "title": _report_title(unit, request),
                "as_of_date": _report_as_of_date(request, content),
            },
            "reference_quote": _quote_from_units([unit], request),
        }
    meta = {
        "title": _report_title(unit, request),
        "as_of_date": _report_as_of_date(request, content),
    }
    recommendation = _public_recommendation(content.get("recommendation"))
    public_limitations = _public_unit_limitations(unit)
    payload = {
        "schema": DESIGNER_PAYLOAD_SCHEMA,
        "meta": meta,
        "sections": _sections(unit, request),
        "recommendation": recommendation,
        "contract_highlights": [
            _public_module_value(item)
            for item in content.get("contract_highlights", [])
            if isinstance(item, Mapping) and _public_module_value(item)
        ] if isinstance(content.get("contract_highlights"), list) else [],
        "payoff": _display_module("payoff", content.get("payoff", {}) if isinstance(content.get("payoff"), Mapping) else {}, artifact_paths=artifact_paths),
        "pricing": _display_module("pricing", content.get("pricing", {}) if isinstance(content.get("pricing"), Mapping) else {}, artifact_paths=artifact_paths),
        "backtest": _display_module("backtest", content.get("backtest", {}) if isinstance(content.get("backtest"), Mapping) else {}, artifact_paths=artifact_paths),
        "parameters": normalize_parameter_groups(content.get("parameters", {})),
        "risk": {
            "items": _public_strings((content.get("risk") or {}).get("items")) if isinstance(content.get("risk"), Mapping) else [],
            "disclaimer": _public_text((content.get("risk") or {}).get("disclaimer")) if isinstance(content.get("risk"), Mapping) else "",
        },
    }
    if request.output_type == "card":
        # Designer原生从pricing/backtest.metrics渲染Card指标。Reporter只补Payoff
        # 情景摘要，避免把内部运行记录带入对外交付物。
        recommendation = payload["recommendation"] if isinstance(payload["recommendation"], Mapping) else {}
        payload["recommendation"] = dict(recommendation)
        reason_points = [
            value for value in (
                _public_text(recommendation.get("reason")),
                *(_public_strings(recommendation.get("suitable_for"))),
            ) if value
        ]
        if reason_points:
            payload["recommendation"]["reason_points"] = list(dict.fromkeys(reason_points))[:3]
        else:
            payload["recommendation"]["reason_points"] = ["本次冻结推荐结果没有可公开的市场观点与结构适配依据。"]
        risk = payload["risk"] if isinstance(payload["risk"], Mapping) else {}
        payload["risk"] = {
            **dict(risk),
            "limitations": public_limitations,
            "not_suitable_for": list(recommendation.get("not_suitable_for", [])) if isinstance(recommendation.get("not_suitable_for"), list) else [],
        }
        payload["card_modules"] = [name for name in ("payoff", "pricing", "backtest") if name in request.selected_modules]
    elif isinstance(payload.get("risk"), Mapping):
        # 章节标题是Designer的固定交付契约；Reporter不得覆写、重排或
        # 以旧版“执行摘要/附录”等名称污染公开报告。
        payload["meta"] = meta
        payload["risk"] = {
            **dict(payload["risk"]),
            "limitations": public_limitations,
            "suitable_for": list(recommendation.get("suitable_for", [])) if isinstance(recommendation.get("suitable_for"), list) else [],
            "not_suitable_for": list(recommendation.get("not_suitable_for", [])) if isinstance(recommendation.get("not_suitable_for"), list) else [],
        }
        payload["conclusion"] = _structured_conclusion(payload)
    return payload


def _public_candidate_title(index: int, name: object) -> str:
    marker = _PUBLIC_CANDIDATE_INDEX[index] if index < len(_PUBLIC_CANDIDATE_INDEX) else str(index)
    return f"候选{marker}：{_public_text(name, f'候选{marker}')}"


def _comparison_tags(unit: Mapping[str, Any], candidate: Mapping[str, Any]) -> list[str]:
    """Project per-candidate comparable facts without merging contracts."""

    subject = unit.get("subject") if isinstance(unit.get("subject"), Mapping) else {}
    underlyings = candidate.get("underlyings", subject.get("underlyings", []))
    tags: list[str] = []
    names = _public_strings(underlyings)
    if names:
        tags.append(f"标的：{'、'.join(names)}")
    content = unit.get("content") if isinstance(unit.get("content"), Mapping) else {}
    highlights = content.get("contract_highlights") if isinstance(content.get("contract_highlights"), list) else []
    for row in highlights[:3]:
        if not isinstance(row, Mapping):
            continue
        label, value = _public_text(row.get("label")), _public_text(row.get("value"))
        note = _public_text(row.get("note"))
        if label and value:
            tags.append(f"{label}：{value}{note}" if note else f"{label}：{value}")
    return tags


def build_collection_payload(units: list[Mapping[str, Any]], request: ReportRequest) -> dict[str, Any]:
    """组合/批量索引/比较的同页Designer内容。

    这不是Reporter自制页面：候选比较作为Designer的结构推荐内容渲染。
    详细单合同图与指标仍以单元附件输出，避免跨候选混写。
    """

    if request.output_type == "quote":
        return {
            "schema": DESIGNER_PAYLOAD_SCHEMA,
            "meta": {"title": "推荐结构及参考报价", "as_of_date": str(request.metadata.get("as_of_date", ""))},
            "reference_quote": _quote_from_units(units, request),
        }
    alternatives: list[dict[str, Any]] = []
    risk_items: list[str] = []
    for index, unit in enumerate(units, start=1):
        content = unit.get("content") if isinstance(unit.get("content"), Mapping) else {}
        candidate = unit.get("candidate") if isinstance(unit.get("candidate"), Mapping) else {}
        name = str(candidate.get("product_name") or unit.get("subject", {}).get("product_name") or f"候选{index}")
        public_title = _public_candidate_title(index, name)
        alternatives.append({
            "title": public_title,
            "summary": _public_text(candidate.get("reason")),
            "tags": _comparison_tags(unit, candidate),
        })
        risk_items.extend(item for item in (content.get("risk", {}) or {}).get("items", []) if isinstance(item, str))
    is_card = request.output_type == "card"
    is_comparison = request.delivery_mode == "comparison"
    next_report_note = (
        "该比较页不以任一候选的图替代其他候选；完整结果需另行生成对应单结构报告。"
        if is_comparison
        else "组合首页不以任一候选的图替代其他候选；完整参数图见各候选独立报告。"
    )
    result_note = (
        "该比较页不混合不同合同的估值或回测指标；完整结果需另行生成对应单结构报告。"
        if is_comparison
        else "组合首页不混合不同合同的估值指标。"
    )
    risk_limitation = (
        "本页仅比较推荐逻辑与适用边界；完整收益、估值和回测结果需另行生成对应单结构报告。"
        if is_comparison
        else "组合页只汇总候选结论；各候选的收益、估值和回测结果分别列示，避免跨合同混用。"
    )
    return {
        "schema": DESIGNER_PAYLOAD_SCHEMA,
        "meta": {
            "title": _public_text(request.metadata.get("title"), "结构化产品候选比较"),
            "as_of_date": str(request.metadata.get("as_of_date", "")),
        },
        "sections": list(CARD_SECTION_ORDER) if is_card else list(REPORT_SECTION_ORDER),
        "conclusion": {
            "structure_name": "候选结构比较",
            "underlyings": "",
            "reasons": ["候选按冻结推荐结果独立列示，不混合不同合同的收益、估值或回测统计。"],
            "valuation_summary": [], "greeks_summary": [], "backtest_summary": [],
            "risk_summary": list(dict.fromkeys(risk_items))[:2],
        },
        "recommendation": {"headline": "候选结构", "reason": "候选排序和适用边界来自冻结的推荐结果。", "alternatives": alternatives},
        "payoff": {"status": "not_run", "note": next_report_note},
        "pricing": {"status": "not_run", "note": result_note},
        "backtest": {"status": "not_run", "note": result_note},
        "parameters": {},
        "risk": {
            "items": list(dict.fromkeys(risk_items)),
            "limitations": [risk_limitation],
            "disclaimer": "风险提示：候选比较不构成投资建议或正式报价；各候选的合同条款与运行状态须独立核对。",
        },
        **({"card_modules": []} if is_card else {}),
    }


def build_design_brief(payload: Mapping[str, Any], request: ReportRequest, *, report_unit_hashes: list[str]) -> dict[str, Any]:
    return {
        "schema": DESIGN_BRIEF_SCHEMA,
        "report_run_id": request.report_run_id,
        "output_type": request.output_type,
        "format": request.format,
        "audience": request.audience,
        "language": "zh-CN",
        "frozen_payload_hash": stable_hash(payload),
        "report_unit_semantic_fact_hashes": report_unit_hashes,
        "constraints": list(DESIGNER_CONSTRAINTS),
    }


def render_with_designer(
    payload: Mapping[str, Any],
    request: ReportRequest,
    output_dir: Path,
    *,
    designer_port: ModulePort,
) -> dict[str, Any]:
    """通过Designer公开Tool接口渲染冻结负载。"""

    tool_request = {
        "action": "render",
        "payload": payload,
        "output_type": request.output_type,
        "format": request.format,
        "asset_mode": "portable",
        "input_dir": str(output_dir),
    }
    template_id = str(request.metadata.get("template_id", "")).strip()
    if template_id:
        tool_request["template_id"] = template_id
    try:
        response = designer_port.call_tool(tool_request)
    except Exception as error:
        raise ReporterError(f"Designer Tool调用失败：{error}") from error
    if not isinstance(response, Mapping) or response.get("ok") is not True:
        message = response.get("message") if isinstance(response, Mapping) else "无结构化响应"
        error_code = response.get("error") if isinstance(response, Mapping) else "designer_tool_error"
        raise ReporterError(f"Designer渲染未完成：{error_code}：{message}")
    artifact = response.get("artifact")
    if not isinstance(artifact, Mapping):
        raise ReporterError("Designer Tool未返回artifact")
    result = dict(artifact)
    if request.format == "pdf":
        encoded = result.pop("pdf_base64", None)
        if not isinstance(encoded, str):
            raise ReporterError("Designer Tool未返回PDF字节")
        try:
            result["pdf"] = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise ReporterError("Designer Tool返回的PDF不是有效Base64") from error
    elif not isinstance(result.get("html"), str):
        raise ReporterError("Designer Tool未返回HTML")
    result["_validated_portable_assets"] = validate_designer_artifact(
        result,
        output_format=request.format,
        output_type=request.output_type,
        expected_frozen_payload_hash=stable_hash(payload),
        expected_asset_mode="portable",
    )
    return result


__all__ = [
    "DESIGNER_CONSTRAINTS", "DESIGNER_PAYLOAD_SCHEMA", "DESIGN_BRIEF_SCHEMA",
    "build_collection_payload", "build_design_brief", "build_designer_payload", "render_with_designer",
]
