"""Validated display-only adjustments applied after Reporter freezes facts.

The standard templates remain unchanged. A patch may reorder, rename or hide
their blocks, and may insert an explicitly frozen supplemental block. It never
creates or rewrites a financial fact and never persists a new default.
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
    "hide_section": frozenset({"op", "section"}),
    "add_section": frozenset({"op", "section", "position"}),
    "append_notice": frozenset({"op", "section", "notice"}),
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
        if op in {"rename_section", "move_section", "hide_section", "add_section", "append_notice"}:
            section_id = _plain_text(operation.get("section"), f"operations[{index}].section")
            if not _SECTION_ID.fullmatch(section_id):
                raise ValueError("presentation_patch章节id只能使用小写字母、数字和连字符。")
        if op == "rename_section":
            _plain_text(operation.get("title"), f"operations[{index}].title")
        if op in {"move_section", "add_section"} and "position" in operation:
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


def validated_supplemental_sections(
    payload: Mapping[str, Any],
) -> dict[str, tuple[str, tuple[Mapping[str, Any], ...]]]:
    """Read Reporter-frozen optional sections without accepting presentation code."""

    raw_sections = payload.get("supplemental_sections")
    if raw_sections is None:
        return {}
    if not isinstance(raw_sections, list):
        raise ValueError("supplemental_sections必须是数组。")
    result: dict[str, tuple[str, tuple[Mapping[str, Any], ...]]] = {}
    for index, raw in enumerate(raw_sections):
        if not isinstance(raw, Mapping):
            raise ValueError(f"supplemental_sections[{index}]必须是对象。")
        unknown = set(raw).difference({"id", "title", "content"})
        if unknown:
            raise ValueError(f"supplemental_sections[{index}]包含不支持字段。")
        section_id = _plain_text(raw.get("id"), f"supplemental_sections[{index}].id")
        if not _SECTION_ID.fullmatch(section_id):
            raise ValueError("supplemental_sections章节id只能使用小写字母、数字和连字符。")
        if section_id in result:
            raise ValueError(f"supplemental_sections存在重复章节：{section_id}。")
        title = _plain_text(raw.get("title"), f"supplemental_sections[{index}].title")
        content = raw.get("content")
        if not isinstance(content, list) or not content:
            raise ValueError(f"supplemental_sections[{index}].content必须是非空数组。")
        nodes: list[Mapping[str, Any]] = []
        for node_index, node in enumerate(content):
            if not isinstance(node, Mapping):
                raise ValueError(f"supplemental_sections[{index}].content[{node_index}]必须是对象。")
            node_type = str(node.get("type") or "").strip().lower()
            if node_type not in {"paragraph", "metrics", "table", "formula", "chart"}:
                raise ValueError(f"supplemental_sections[{index}]包含不支持内容类型：{node_type or '空'}。")
            _reject_visual_keys(node, f"supplemental_sections[{index}].content[{node_index}]")
            nodes.append(deepcopy(dict(node)))
        result[section_id] = (title, tuple(nodes))
    return result


def apply_presentation_patch(
    payload: Mapping[str, Any],
    sections: Sequence[TemplateSection],
    patch: Mapping[str, Any] | None,
    *,
    output_type: str,
) -> EffectivePresentation:
    if output_type not in {"card", "report", "quote"}:
        raise ValueError(f"不支持的交付类型：{output_type}。")
    safe_patch = validate_presentation_patch(patch)
    result = deepcopy(dict(payload))
    effective_sections = list(sections)
    appended: dict[str, list[Mapping[str, Any]]] = {}
    supplemental = validated_supplemental_sections(result)
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
            effective_sections[position] = TemplateSection(current.id, title, current.block)
            changes.append({"op": op, "section": current.id, "before": current.title, "after": title})
        elif op == "move_section":
            position = _section_index(effective_sections, str(operation["section"]))
            current = effective_sections.pop(position)
            destination = min(int(operation.get("position", len(effective_sections))), len(effective_sections))
            effective_sections.insert(destination, current)
            changes.append({"op": op, "section": current.id, "position": destination})
        elif op == "hide_section":
            position = _section_index(effective_sections, str(operation["section"]))
            current = effective_sections.pop(position)
            changes.append({"op": op, "section": current.id})
        elif op == "add_section":
            section_id = str(operation["section"])
            if any(item.id == section_id for item in effective_sections):
                raise ValueError(f"presentation_patch章节已存在：{section_id}。")
            if section_id not in supplemental:
                raise ValueError(f"presentation_patch找不到冻结补充章节：{section_id}。")
            title, content = supplemental[section_id]
            if output_type in {"card", "quote"} and any(
                str(node.get("type")).lower() == "chart" for node in content
            ):
                raise ValueError("Card和Quote的补充章节不支持图表。")
            destination = min(int(operation.get("position", len(effective_sections))), len(effective_sections))
            effective_sections.insert(destination, TemplateSection(section_id, title, "supplemental"))
            appended[section_id] = list(content)
            changes.append({"op": op, "section": section_id, "position": destination})
        elif op == "append_notice":
            section_id = str(operation["section"])
            _section_index(effective_sections, section_id)
            notice = str(operation["notice"]).strip()
            appended.setdefault(section_id, []).append({"type": "paragraph", "text": _NOTICE_TEXT[notice]})
            changes.append({"op": op, "section": section_id, "notice": notice})

    if not effective_sections:
        raise ValueError("presentation_patch不能隐藏全部章节。")

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
