"""Validated one-off presentation changes applied after frozen facts.

The patch changes only one delivery. It cannot alter visual tokens, execute
markup, or persist a new standard template.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Mapping, Sequence

from .template_definition import TemplateSection


PRESENTATION_PATCH_SCHEMA = "optionhelper.presentation-patch/v1.0.0"
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
_CONTENT_FIELDS = {
    "paragraph": frozenset({"type", "text"}),
    "metrics": frozenset({"type", "items"}),
    "table": frozenset({"type", "caption", "columns", "rows"}),
    "formula": frozenset({"type", "formula"}),
    "chart": frozenset({
        "type", "chart_type", "id", "title", "x", "y", "data", "series",
        "x_axis_name", "y_axis_name", "z_axis_name", "value_format",
        "value_suffix", "source_note", "accessibility_summary",
    }),
}
_OPERATION_FIELDS = {
    "add_section": frozenset({"op", "id", "title", "position", "content"}),
    "remove_section": frozenset({"op", "section"}),
    "rename_section": frozenset({"op", "section", "title"}),
    "move_section": frozenset({"op", "section", "position"}),
    "set_value": frozenset({"op", "path", "value"}),
    "remove_value": frozenset({"op", "path"}),
    "append_content": frozenset({"op", "section", "content"}),
}


@dataclass(frozen=True)
class EffectivePresentation:
    payload: Mapping[str, Any]
    sections: tuple[TemplateSection, ...]
    custom_content: Mapping[str, tuple[Mapping[str, Any], ...]]
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


def _validate_content(raw: Any, field: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"presentation_patch的{field}必须是非空内容数组。")
    nodes: list[Mapping[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"presentation_patch的{field}[{index}]必须是对象。")
        node = deepcopy(dict(item))
        node_type = str(node.get("type") or "").strip().lower()
        allowed = _CONTENT_FIELDS.get(node_type)
        if allowed is None:
            raise ValueError(f"presentation_patch不支持内容类型：{node_type or '空'}。")
        unknown = set(node).difference(allowed)
        if unknown:
            raise ValueError(f"presentation_patch内容包含不支持字段：{', '.join(sorted(map(str, unknown)))}。")
        if node_type == "paragraph":
            _plain_text(node.get("text"), f"{field}[{index}].text")
        elif node_type == "formula":
            _plain_text(node.get("formula"), f"{field}[{index}].formula")
        elif node_type == "metrics":
            if not isinstance(node.get("items"), list) or not node["items"]:
                raise ValueError("presentation_patch的metrics.items必须是非空数组。")
        elif node_type == "table":
            if not isinstance(node.get("columns"), list) or not isinstance(node.get("rows"), list):
                raise ValueError("presentation_patch的table必须包含columns和rows数组。")
        elif node_type == "chart":
            if str(node.get("chart_type") or "").lower() not in {"line", "bar", "heatmap"}:
                raise ValueError("presentation_patch的chart_type仅支持line、bar或heatmap。")
        _reject_visual_keys(node, f"presentation_patch.{field}[{index}]")
        nodes.append(node)
    return tuple(nodes)


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
        if op in {"add_section", "remove_section", "rename_section", "move_section", "append_content"}:
            key = "id" if op == "add_section" else "section"
            section_id = _plain_text(operation.get(key), f"operations[{index}].{key}")
            if not _SECTION_ID.fullmatch(section_id):
                raise ValueError("presentation_patch章节id只能使用小写字母、数字和连字符。")
        if op in {"add_section", "rename_section"}:
            _plain_text(operation.get("title"), f"operations[{index}].title")
        if op in {"add_section", "move_section"} and "position" in operation:
            if not isinstance(operation["position"], int) or isinstance(operation["position"], bool) or operation["position"] < 0:
                raise ValueError("presentation_patch.position必须是非负整数。")
        if op in {"add_section", "append_content"}:
            _validate_content(operation.get("content"), f"operations[{index}].content")
        if op in {"set_value", "remove_value"}:
            pointer = _plain_text(operation.get("path"), f"operations[{index}].path")
            if not pointer.startswith("/") or pointer in {"/schema", "/sections"}:
                raise ValueError("presentation_patch值路径必须是安全JSON Pointer。")
            segments = [_decode_pointer(segment) for segment in pointer.split("/")[1:]]
            if any(str(segment).lower().replace("-", "_") in _FORBIDDEN_KEYS for segment in segments):
                raise ValueError("presentation_patch不接受颜色、字体或样式路径。")
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


def _decode_pointer(value: str) -> str:
    return value.replace("~1", "/").replace("~0", "~")


def _pointer_parent(root: Any, pointer: str) -> tuple[Any, str | int]:
    parts = [_decode_pointer(part) for part in pointer.split("/")[1:]]
    if not parts:
        raise ValueError("presentation_patch不能替换整个Payload。")
    current = root
    for part in parts[:-1]:
        if isinstance(current, list):
            if not part.isdigit() or int(part) >= len(current):
                raise ValueError(f"presentation_patch路径不存在：{pointer}。")
            current = current[int(part)]
        elif isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            raise ValueError(f"presentation_patch路径不存在：{pointer}。")
    final: str | int = parts[-1]
    if isinstance(current, list):
        if not str(final).isdigit() or int(final) >= len(current):
            raise ValueError(f"presentation_patch路径不存在：{pointer}。")
        final = int(final)
    elif not isinstance(current, Mapping) or final not in current:
        raise ValueError(f"presentation_patch路径不存在：{pointer}。")
    return current, final


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
    custom: dict[str, tuple[Mapping[str, Any], ...]] = {}
    appended: dict[str, list[Mapping[str, Any]]] = {}
    if safe_patch is None:
        return EffectivePresentation(result, tuple(effective_sections), custom, {}, None)

    changes: list[dict[str, Any]] = []
    for index, raw_operation in enumerate(safe_patch["operations"]):
        operation = dict(raw_operation)
        op = str(operation["op"]).lower()
        if op == "set_value":
            parent, key = _pointer_parent(result, str(operation["path"]))
            previous = deepcopy(parent[key])
            parent[key] = deepcopy(operation["value"])
            changes.append({"op": op, "path": operation["path"], "before": previous, "after": deepcopy(operation["value"])})
        elif op == "remove_value":
            parent, key = _pointer_parent(result, str(operation["path"]))
            previous = deepcopy(parent[key])
            if isinstance(parent, list):
                parent.pop(int(key))
            else:
                del parent[key]
            changes.append({"op": op, "path": operation["path"], "before": previous})
        elif op == "remove_section":
            position = _section_index(effective_sections, str(operation["section"]))
            removed = effective_sections.pop(position)
            custom.pop(removed.id, None)
            appended.pop(removed.id, None)
            changes.append({"op": op, "section": removed.id})
        elif op == "rename_section":
            position = _section_index(effective_sections, str(operation["section"]))
            current = effective_sections[position]
            effective_sections[position] = TemplateSection(current.id, str(operation["title"]).strip(), current.block)
            changes.append({"op": op, "section": current.id, "before": current.title, "after": operation["title"]})
        elif op == "move_section":
            position = _section_index(effective_sections, str(operation["section"]))
            current = effective_sections.pop(position)
            destination = min(int(operation.get("position", len(effective_sections))), len(effective_sections))
            effective_sections.insert(destination, current)
            changes.append({"op": op, "section": current.id, "position": destination})
        elif op == "add_section":
            section_id = str(operation["id"])
            if any(section.id == section_id for section in effective_sections):
                raise ValueError(f"presentation_patch章节id重复：{section_id}。")
            destination = min(int(operation.get("position", len(effective_sections))), len(effective_sections))
            effective_sections.insert(destination, TemplateSection(section_id, str(operation["title"]).strip(), f"custom:{section_id}"))
            custom[section_id] = _validate_content(operation["content"], f"operations[{index}].content")
            changes.append({"op": op, "section": section_id, "position": destination})
        elif op == "append_content":
            section_id = str(operation["section"])
            _section_index(effective_sections, section_id)
            appended.setdefault(section_id, []).extend(_validate_content(operation["content"], f"operations[{index}].content"))
            changes.append({"op": op, "section": section_id})

    receipt = {
        "schema": PRESENTATION_PATCH_SCHEMA,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "changes": changes,
    }
    return EffectivePresentation(
        payload=result,
        sections=tuple(effective_sections),
        custom_content=custom,
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
