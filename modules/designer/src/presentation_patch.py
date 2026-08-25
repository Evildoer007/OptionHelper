"""Validated display-only adjustments applied after Reporter freezes facts.

The patch changes one delivery's reading order or approved labels.  It cannot
add, remove, hide or rewrite a financial fact, and it cannot persist a new
standard template.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Mapping, Sequence

from .template_definition import TemplateSection


PRESENTATION_PATCH_SCHEMA = "optionhelper.presentation-patch"
_SECTION_ID = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_FORBIDDEN_KEYS = frozenset({
    "color", "colors", "palette", "theme", "style", "styles", "css",
    "font", "fonts", "spacing", "radius", "shadow", "class", "html",
    "javascript", "script", "src", "href",
})
_MARKUP = re.compile(r"<\s*/?\s*[A-Za-z]|javascript\s*:", re.IGNORECASE)
# Payload may legitimately use terms such as ``exercise_style``. Reject only
# unambiguous visual keys here; presentation_patch itself uses the stricter
# list above because its vocabulary is fully controlled by Designer.
_PAYLOAD_VISUAL_KEYS = frozenset({"color", "colors", "palette", "theme", "css", "font", "fonts", "spacing"})
_OPERATION_FIELDS = {
    "rename_section": frozenset({"op", "section", "title"}),
    "move_section": frozenset({"op", "section", "position"}),
    "append_notice": frozenset({"op", "section", "notice"}),
}

# A caller may select a reader-facing synonym, but not write a factual claim
# into a chapter heading.  The block still comes from the shipped template.
_SECTION_TITLE_ALIASES = {
    "conclusion": frozenset({"核心结论", "结论摘要"}),
    "recommendation": frozenset({"结构推荐", "推荐结构"}),
    "parameters": frozenset({"合同参数", "合同条款"}),
    "payoff": frozenset({"收益结构", "收益结构分析"}),
    "pricing": frozenset({"估值定价", "估值摘要"}),
    "backtest": frozenset({"历史回测", "回测摘要"}),
    "risk": frozenset({"风险提示", "主要风险"}),
    "reason": frozenset({"推荐理由", "推荐依据"}),
    "contract_highlights": frozenset({"关键合同条款", "合同要点"}),
    "reference_quote": frozenset({"推荐结构及参考报价", "参考报价"}),
}

# These notices are Designer-owned reader guidance, not caller-supplied
# content.  They deliberately contain no product, contract or result value.
_NOTICE_TEXT = {
    "methodology": "本节内容以已冻结的合同条款、数据引用和模块计算结果为准。",
    "reader_note": "本说明仅用于阅读，不构成新的产品条款、估值结论或投资建议。",
}


@dataclass(frozen=True)
class EffectivePresentation:
    payload: Mapping[str, Any]
    sections: tuple[TemplateSection, ...]
    appended_content: Mapping[str, tuple[Mapping[str, Any], ...]]
    receipt: Mapping[str, Any] | None


def _plain_text(value: Any, field: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(f"presentation_patch的{field}不能为空。")
    if _MARKUP.search(text):
        raise ValueError(f"presentation_patch的{field}不接受HTML或脚本。")
    return text


def _reject_visual_keys(value: Any, trail: str = "presentation_patch") -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            if key in _FORBIDDEN_KEYS or key.endswith("_color"):
                raise ValueError(f"{trail}不接受颜色、字体或样式字段：{raw_key}。")
            _reject_visual_keys(item, f"{trail}.{raw_key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_visual_keys(item, f"{trail}[{index}]")
    elif isinstance(value, str) and _MARKUP.search(value):
        raise ValueError(f"{trail}不接受HTML或脚本。")


def validate_presentation_patch(value: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("presentation_patch必须是对象。")
    patch = deepcopy(dict(value))
    if patch.get("schema") != PRESENTATION_PATCH_SCHEMA:
        raise ValueError(f"presentation_patch.schema必须为{PRESENTATION_PATCH_SCHEMA}。")
    unknown = set(patch).difference({"schema", "operations"})
    if unknown:
        raise ValueError("presentation_patch包含不支持字段。")
    operations = patch.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("presentation_patch.operations必须是非空数组。")
    for index, raw_operation in enumerate(operations):
        if not isinstance(raw_operation, Mapping):
            raise ValueError(f"presentation_patch.operations[{index}]必须是对象。")
        operation = dict(raw_operation)
        op = str(operation.get("op") or "").strip().lower()
        allowed = _OPERATION_FIELDS.get(op)
        if allowed is None:
            raise ValueError(f"presentation_patch不支持操作：{op or '空'}。")
        unknown_fields = set(operation).difference(allowed)
        if unknown_fields:
            raise ValueError(f"presentation_patch操作包含不支持字段：{', '.join(sorted(map(str, unknown_fields)))}。")
        if op in {"rename_section", "move_section", "append_notice"}:
            section_id = _plain_text(operation.get("section"), f"operations[{index}].section")
            if not _SECTION_ID.fullmatch(section_id):
                raise ValueError("presentation_patch章节id只能使用小写字母、数字和连字符。")
        if op == "rename_section":
            _plain_text(operation.get("title"), f"operations[{index}].title")
        if op == "move_section" and "position" in operation:
            if not isinstance(operation["position"], int) or isinstance(operation["position"], bool) or operation["position"] < 0:
                raise ValueError("presentation_patch.position必须是非负整数。")
        if op == "append_notice" and str(operation.get("notice") or "").strip() not in _NOTICE_TEXT:
            raise ValueError("presentation_patch.notice仅支持受控说明类型。")
        _reject_visual_keys(operation, f"presentation_patch.operations[{index}]")
    return patch


def reject_payload_visual_overrides(value: Any, trail: str = "payload") -> None:
    """Reject caller-owned visual fields while allowing factual SVG/MathML."""

    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            if key in _PAYLOAD_VISUAL_KEYS or key.endswith("_color"):
                raise ValueError(f"{trail}不接受颜色、字体或样式字段：{raw_key}。")
            reject_payload_visual_overrides(item, f"{trail}.{raw_key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_payload_visual_overrides(item, f"{trail}[{index}]")


def _section_index(sections: Sequence[TemplateSection], section_id: str) -> int:
    for index, section in enumerate(sections):
        if section.id == section_id:
            return index
    raise ValueError(f"presentation_patch找不到章节：{section_id}。")


def apply_presentation_patch(
    payload: Mapping[str, Any],
    sections: Sequence[TemplateSection],
    patch: Mapping[str, Any] | None,
) -> EffectivePresentation:
    safe_patch = validate_presentation_patch(patch)
    result = deepcopy(dict(payload))
    effective_sections = list(sections)
    appended: dict[str, list[Mapping[str, Any]]] = {}
    if safe_patch is None:
        return EffectivePresentation(result, tuple(effective_sections), {}, None)

    changes: list[dict[str, Any]] = []
    for index, raw_operation in enumerate(safe_patch["operations"]):
        operation = dict(raw_operation)
        op = str(operation["op"]).lower()
        if op == "rename_section":
            position = _section_index(effective_sections, str(operation["section"]))
            current = effective_sections[position]
            title = str(operation["title"]).strip()
            if title not in _SECTION_TITLE_ALIASES.get(current.block, frozenset()):
                raise ValueError(f"presentation_patch章节{current.id}不支持该展示标题。")
            effective_sections[position] = TemplateSection(current.id, title, current.block)
            changes.append({"op": op, "section": current.id, "before": current.title, "after": title})
        elif op == "move_section":
            position = _section_index(effective_sections, str(operation["section"]))
            current = effective_sections.pop(position)
            destination = min(int(operation.get("position", len(effective_sections))), len(effective_sections))
            effective_sections.insert(destination, current)
            changes.append({"op": op, "section": current.id, "position": destination})
        elif op == "append_notice":
            section_id = str(operation["section"])
            _section_index(effective_sections, section_id)
            notice = str(operation["notice"]).strip()
            appended.setdefault(section_id, []).append({"type": "paragraph", "text": _NOTICE_TEXT[notice]})
            changes.append({"op": op, "section": section_id, "notice": notice})

    receipt = {
        "schema": PRESENTATION_PATCH_SCHEMA,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "changes": changes,
    }
    return EffectivePresentation(
        payload=result,
        sections=tuple(effective_sections),
        appended_content={key: tuple(value) for key, value in appended.items()},
        receipt=receipt,
    )


__all__ = [
    "EffectivePresentation",
    "PRESENTATION_PATCH_SCHEMA",
    "apply_presentation_patch",
    "reject_payload_visual_overrides",
    "validate_presentation_patch",
]
