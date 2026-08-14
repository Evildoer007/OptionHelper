"""Designer-owned definitions for selecting and ordering reader content.

The HTML shells and theme remain the only visual implementation.  A template
definition is deliberately small: it can choose known content blocks and their
order, but cannot carry executable HTML, CSS, formulas or financial values.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .config import DesignerConfig, DesignerConfigurationError


TEMPLATE_SCHEMA = "optionhelper.designer-template"
_TEMPLATE_ID = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_OUTPUT_BLOCKS = {
    "report": frozenset({"conclusion", "recommendation", "parameters", "payoff", "pricing", "backtest", "risk"}),
    "card": frozenset({"recommendation", "reason", "contract_highlights", "pricing", "backtest", "risk"}),
    "quote": frozenset({"reference_quote"}),
}


@dataclass(frozen=True)
class TemplateSection:
    """One visible reader section backed by exactly one known content block."""

    id: str
    title: str
    block: str


@dataclass(frozen=True)
class TemplateDefinition:
    """Validated visual-content composition owned by Designer."""

    id: str
    output_type: str
    shell: str
    sections: tuple[TemplateSection, ...]


def _require_text(value: Any, field: str) -> str:
    result = str(value).strip() if value is not None else ""
    if not result:
        raise DesignerConfigurationError(f"模板{field}不能为空。")
    return result


def _read_definition(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DesignerConfigurationError(f"Designer模板定义无效：{path.name}：{error}") from error
    if not isinstance(value, Mapping):
        raise DesignerConfigurationError(f"Designer模板定义必须是对象：{path.name}")
    return value


def load_template_definition(config: DesignerConfig, template_id: str, output_type: str) -> TemplateDefinition:
    """Load one shipped or user-authored managed template definition.

    Definitions are read only from ``assets/templates``.  ``template_id`` is an
    identifier, never a caller-provided path, so a custom template cannot make
    Designer read arbitrary filesystem content.
    """

    requested_id = _require_text(template_id, "id")
    if not _TEMPLATE_ID.fullmatch(requested_id):
        raise DesignerConfigurationError("模板id只能使用小写字母、数字和连字符。")
    path = config.template_definition_path(requested_id)
    raw = _read_definition(path)
    if raw.get("schema") != TEMPLATE_SCHEMA:
        raise DesignerConfigurationError(f"模板{requested_id}必须使用{TEMPLATE_SCHEMA}。")
    if raw.get("id") != requested_id:
        raise DesignerConfigurationError(f"模板文件名与模板id不一致：{requested_id}。")
    if raw.get("output_type") != output_type:
        raise DesignerConfigurationError(f"模板{requested_id}不适用于{output_type}。")
    shell = _require_text(raw.get("shell"), "shell")
    if shell not in {"card.html", "quote.html", "report.html"}:
        raise DesignerConfigurationError(f"模板{requested_id}引用了不受支持的HTML壳。")
    raw_sections = raw.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise DesignerConfigurationError(f"模板{requested_id}必须包含非空sections数组。")
    sections: list[TemplateSection] = []
    seen_ids: set[str] = set()
    seen_blocks: set[str] = set()
    allowed_blocks = _OUTPUT_BLOCKS[output_type]
    for index, raw_section in enumerate(raw_sections, start=1):
        if not isinstance(raw_section, Mapping):
            raise DesignerConfigurationError(f"模板{requested_id}第{index}个章节必须是对象。")
        section_id = _require_text(raw_section.get("id"), f"sections[{index}].id")
        title = _require_text(raw_section.get("title"), f"sections[{index}].title")
        block = _require_text(raw_section.get("block"), f"sections[{index}].block")
        if section_id in seen_ids:
            raise DesignerConfigurationError(f"模板{requested_id}存在重复章节id：{section_id}。")
        if block not in allowed_blocks:
            raise DesignerConfigurationError(f"模板{requested_id}不支持内容块：{block}。")
        if block in seen_blocks:
            raise DesignerConfigurationError(f"模板{requested_id}重复使用内容块：{block}。")
        seen_ids.add(section_id)
        seen_blocks.add(block)
        sections.append(TemplateSection(id=section_id, title=title, block=block))
    return TemplateDefinition(
        id=requested_id,
        output_type=output_type,
        shell=shell,
        sections=tuple(sections),
    )


def default_template_id(output_type: str) -> str:
    return f"{output_type}-standard"


__all__ = [
    "TEMPLATE_SCHEMA",
    "TemplateDefinition",
    "TemplateSection",
    "default_template_id",
    "load_template_definition",
]
