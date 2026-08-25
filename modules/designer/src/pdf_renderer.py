"""Small, deterministic PDF renderer for public Designer deliveries.

The App and Skill need a real PDF path that is independent of a browser or a
running desktop session.  This module intentionally consumes the already
sanitised Designer HTML, keeps only reader-visible material, and writes a
paginated PDF with ReportLab's built-in Chinese CID font mapping.  It does not
read result directories, perform financial calculations, or alter facts.
"""

from __future__ import annotations

import base64
from html import escape as html_escape
from html.parser import HTMLParser
import importlib.metadata
import math
from pathlib import Path
import re
from typing import Any, Literal, Mapping
from xml.etree import ElementTree

from .design_tokens import TOKENS


REPORTLAB_VERSION = "5.0.0"
TOKEN_COLORS = TOKENS.colors


class PdfRuntimeError(RuntimeError):
    """Raised when the pinned local PDF runtime is not usable."""


BlockKind = Literal["heading", "paragraph", "item", "caption", "table", "metric_grid", "chart", "svg"]
PdfBlock = tuple[BlockKind, int, str | list[list[str]]]


def runtime_status() -> dict[str, str | bool]:
    """Return a non-secret probe result for the pinned local PDF runtime."""

    try:
        actual = importlib.metadata.version("reportlab")
    except importlib.metadata.PackageNotFoundError:
        return {
            "available": False,
            "runtime": "reportlab",
            "required_version": REPORTLAB_VERSION,
            "message": f"PDF运行组件ReportLab {REPORTLAB_VERSION}未安装。",
        }
    if actual != REPORTLAB_VERSION:
        return {
            "available": False,
            "runtime": "reportlab",
            "required_version": REPORTLAB_VERSION,
            "actual_version": actual,
            "message": f"PDF运行组件ReportLab版本不匹配：需要{REPORTLAB_VERSION}，当前{actual}。",
        }
    try:
        from reportlab.pdfbase import pdfmetrics  # noqa: F401
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # noqa: F401
        from reportlab.pdfgen.canvas import Canvas  # noqa: F401
    except Exception as error:  # pragma: no cover - host package damage
        return {
            "available": False,
            "runtime": "reportlab",
            "required_version": REPORTLAB_VERSION,
            "message": f"PDF运行组件不可用：{type(error).__name__}。",
        }
    return {
        "available": True,
        "runtime": "reportlab",
        "required_version": REPORTLAB_VERSION,
        "actual_version": actual,
    }


def _require_runtime() -> None:
    status = runtime_status()
    if status["available"] is not True:
        raise PdfRuntimeError(str(status["message"]))


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


_SUB_OPEN, _SUB_CLOSE = "\ue000", "\ue001"
_SUPER_OPEN, _SUPER_CLOSE = "\ue002", "\ue003"
_SCIENTIFIC_SUPERSCRIPT_RUN = re.compile(r"(?<=×10)[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+")
_SCIENTIFIC_SUPERSCRIPT_ASCII = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")


def _mathml_text(markup: str) -> str:
    """Project safe inline MathML to readable PDF text.

    ReportLab paragraphs do not understand MathML.  Private markers preserve
    its hierarchy until ``_mixed_markup`` converts them to ReportLab's safe
    inline ``sub`` and ``super`` tags.  No TeX underscore is exposed.
    """

    try:
        root = ElementTree.fromstring(markup)
    except ElementTree.ParseError:
        return _clean_text(re.sub(r"<[^>]+>", "", markup))

    def local_name(element: ElementTree.Element) -> str:
        return element.tag.rsplit("}", 1)[-1].lower()

    def content(element: ElementTree.Element) -> str:
        tag = local_name(element)
        children = list(element)
        if tag == "msub" and len(children) >= 2:
            return content(children[0]) + _SUB_OPEN + content(children[1]) + _SUB_CLOSE
        if tag == "msup" and len(children) >= 2:
            return content(children[0]) + _SUPER_OPEN + content(children[1]) + _SUPER_CLOSE
        if tag == "msubsup" and len(children) >= 3:
            return (
                content(children[0])
                + _SUB_OPEN + content(children[1]) + _SUB_CLOSE
                + _SUPER_OPEN + content(children[2]) + _SUPER_CLOSE
            )
        if tag == "mfrac" and len(children) >= 2:
            return f"({content(children[0])})/({content(children[1])})"
        if tag == "msqrt" and children:
            return f"√({content(children[0])})"
        if tag == "mroot" and len(children) >= 2:
            return f"root[{content(children[1])}]({content(children[0])})"
        if tag == "mtable":
            return "[" + "; ".join(content(child) for child in children) + "]"
        if tag == "mtr":
            return ", ".join(content(child) for child in children)
        if tag == "mtd":
            return "".join(content(child) for child in children)
        value = element.text or ""
        for child in children:
            value += content(child)
            if child.tail:
                value += child.tail
        return value

    return _clean_text(content(root))


class _PublicHtmlParser(HTMLParser):
    """Extract public headings, prose, tables and compact Card facts."""

    _TEXT_TAGS = {"h1", "h2", "h3", "h4", "p", "li", "figcaption", "dt", "dd", "small", "caption"}
    _SKIPPED_TAGS = {"script", "style", "noscript", "title", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[PdfBlock] = []
        self._ignored = 0
        self._active: list[tuple[str, list[str]]] = []
        self._table_rows: list[list[str]] | None = None
        self._table_row: list[str] | None = None
        self._cell: list[str] | None = None
        self._metric_grid: list[dict[str, str]] | None = None
        self._metric_item: dict[str, str] | None = None
        self._metric_field_name: str | None = None
        self._metric_strip: list[dict[str, str]] | None = None
        self._module_state: list[str] | None = None
        self._math_parts: list[str] | None = None
        self._chart_figure_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._SKIPPED_TAGS:
            self._ignored += 1
            return
        if self._ignored:
            return
        if tag == "math":
            self._math_parts = ["<math>"]
            return
        if self._math_parts is not None:
            self._math_parts.append(f"<{tag}>")
            return
        if tag == "img":
            attributes = dict(attrs)
            src = attributes.get("src") or ""
            if src.startswith("data:image/svg+xml;base64,"):
                try:
                    svg = base64.b64decode(src.partition(",")[2], validate=True).decode("utf-8")
                except (UnicodeDecodeError, ValueError):
                    svg = ""
                if svg:
                    self.blocks.append(("svg", 0, svg))
                    return
            alt = _clean_text(attributes.get("alt") or "")
            if alt:
                self.blocks.append(("caption", 0, f"图示：{alt}"))
            return
        if tag == "figure" and "chart-figure" in (dict(attrs).get("class") or "").split():
            self._chart_figure_depth += 1
            return
        if tag == "br":
            if self._cell is not None:
                self._cell.append(" ")
            for _, parts in self._active:
                parts.append(" ")
            return
        if tag == "table":
            self._table_rows = []
            return
        if tag == "tr" and self._table_rows is not None:
            self._table_row = []
            return
        if tag in {"th", "td"} and self._table_row is not None:
            self._cell = []
            return
        class_name = dict(attrs).get("class") or ""
        if tag == "div" and "chart" in class_name.split():
            chart_id = _clean_text(dict(attrs).get("id") or "")
            if chart_id:
                self.blocks.append(("chart", 0, chart_id))
            return
        if tag == "figcaption" and self._chart_figure_depth:
            # The static chart Flowable prints the title as part of the same
            # unbreakable visual, preventing a caption at one page bottom and
            # its chart on the next page.
            self._active.append(("skip-caption", []))
            return
        if tag == "div" and "module-state" in class_name:
            self._module_state = []
            self._active.append(("module-state", self._module_state))
            return
        if tag == "div" and "metric-strip" in class_name:
            self._metric_strip = []
            return
        if tag == "div" and self._metric_strip is not None and "metric" in class_name and "metric__" not in class_name:
            self._metric_item = {}
            return
        if tag == "div" and self._metric_item is not None:
            metric_field = next(
                (name for marker, name in (("metric__value", "value"), ("metric__label", "label"), ("metric__note", "note")) if marker in class_name),
                None,
            )
            if metric_field:
                self._metric_field_name = metric_field
                self._active.append(("metric-field", []))
                return
        if tag == "dl" and ("card-metric-grid" in class_name or "card-contract-grid" in class_name):
            self._metric_grid = []
            return
        if tag == "div" and self._metric_grid is not None and ("card-metric" in class_name or "card-contract" in class_name):
            self._metric_item = {}
            return
        if tag in self._TEXT_TAGS:
            self._active.append((tag, []))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIPPED_TAGS:
            if self._ignored:
                self._ignored -= 1
            return
        if self._ignored:
            return
        if self._math_parts is not None:
            if tag == "math":
                self._math_parts.append("</math>")
                value = _mathml_text("".join(self._math_parts))
                self._math_parts = None
                attached = False
                if self._cell is not None:
                    self._cell.append(value)
                    attached = True
                for _, parts in self._active:
                    parts.append(value)
                    attached = True
                if value and not attached:
                    self.blocks.append(("paragraph", 0, value))
            else:
                self._math_parts.append(f"</{tag}>")
            return
        if tag == "figure" and self._chart_figure_depth:
            self._chart_figure_depth -= 1
            return
        if tag == "figcaption" and self._active and self._active[-1][0] == "skip-caption":
            self._active.pop()
            return
        if tag in {"th", "td"} and self._cell is not None:
            if self._table_row is not None:
                self._table_row.append(_clean_text("".join(self._cell)))
            self._cell = None
            return
        if tag == "div" and self._metric_field_name is not None:
            for index in range(len(self._active) - 1, -1, -1):
                active_tag, parts = self._active[index]
                if active_tag == "metric-field":
                    self._active.pop(index)
                    assert self._metric_item is not None
                    self._metric_item[self._metric_field_name] = _clean_text("".join(parts))
                    self._metric_field_name = None
                    return
        if tag == "div" and self._metric_item is not None and self._metric_strip is not None:
            if self._metric_item.get("label"):
                self._metric_strip.append(dict(self._metric_item))
            self._metric_item = None
            return
        if tag == "div" and self._metric_strip is not None:
            metrics = self._metric_strip
            self._metric_strip = None
            if metrics:
                self.blocks.append(("metric_grid", 0, [
                    [item.get("label", ""), item.get("value", ""), item.get("note", "")]
                    for item in metrics
                ]))
            return
        if tag == "div" and self._module_state is not None:
            for index in range(len(self._active) - 1, -1, -1):
                active_tag, parts = self._active[index]
                if active_tag == "module-state":
                    self._active.pop(index)
                    value = _clean_text("".join(parts))
                    self._module_state = None
                    if value:
                        self.blocks.append(("caption", 0, value))
                    return
        if self._metric_item is not None and tag in {"dt", "dd", "small"}:
            for index in range(len(self._active) - 1, -1, -1):
                active_tag, parts = self._active[index]
                if active_tag == tag:
                    self._active.pop(index)
                    self._metric_item[{"dt": "label", "dd": "value", "small": "note"}[tag]] = _clean_text("".join(parts))
                    return
        if tag == "div" and self._metric_item is not None:
            if self._metric_item.get("label"):
                assert self._metric_grid is not None
                self._metric_grid.append(dict(self._metric_item))
            self._metric_item = None
            return
        if tag == "dl" and self._metric_grid is not None:
            metrics = self._metric_grid
            self._metric_grid = None
            if metrics:
                self.blocks.append(("metric_grid", 0, [
                    [item.get("label", ""), item.get("value", ""), item.get("note", "")]
                    for item in metrics
                ]))
            return
        if tag == "tr" and self._table_row is not None:
            if any(_clean_text(item) for item in self._table_row):
                assert self._table_rows is not None
                self._table_rows.append(list(self._table_row))
            self._table_row = None
            return
        if tag == "table" and self._table_rows is not None:
            rows = self._table_rows
            self._table_rows = None
            if rows:
                self.blocks.append(("table", 0, rows))
            return
        for index in range(len(self._active) - 1, -1, -1):
            active_tag, parts = self._active[index]
            if active_tag != tag:
                continue
            self._active.pop(index)
            value = _clean_text("".join(parts))
            if not value:
                return
            if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
                self.blocks.append(("heading", int(tag[1]), value))
            elif tag == "li":
                self.blocks.append(("item", 0, value))
            elif tag in {"caption", "figcaption"}:
                self.blocks.append(("caption", 0, value))
            else:
                self.blocks.append(("paragraph", 0, value))
            return

    def handle_data(self, data: str) -> None:
        if self._ignored:
            return
        if self._math_parts is not None:
            self._math_parts.append(html_escape(data))
            return
        if self._cell is not None:
            self._cell.append(data)
        for _, parts in self._active:
            parts.append(data)


class _ComparisonMatrixParser(HTMLParser):
    """Project a MultiCard comparison matrix into PDF candidate columns."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.candidates: list[str] = []
        self.rows: list[tuple[str, list[str]]] = []
        self._candidate_index: int | None = None
        self._candidate_depth = 0
        self._candidate_parts: list[str] = []
        self._row_active = False
        self._row_depth = 0
        self._row_label: list[str] = []
        self._row_cells: list[str] = []
        self._cell_index: int | None = None
        self._cell_depth = 0
        self._cell_parts: list[str] = []
        self._label_active = False
        self._label_depth = 0

    @staticmethod
    def _class_names(attrs: list[tuple[str, str | None]]) -> set[str]:
        return set((dict(attrs).get("class") or "").split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._class_names(attrs)
        raw = self.get_starttag_text() or f"<{tag}>"
        if "comparison-matrix__candidate" in classes:
            self._candidate_index = len(self.candidates)
            self._candidate_parts = []
            self._candidate_depth = 0
            return
        if "comparison-matrix__row" in classes:
            self._row_active = True
            self._row_depth = 0
            self._row_label = []
            self._row_cells = []
            return
        if "comparison-matrix__section-label" in classes and self._row_active:
            self._label_active = True
            self._label_depth = 0
            return
        if "comparison-matrix__cell" in classes and self._row_active:
            self._cell_index = len(self._row_cells)
            self._cell_parts = []
            self._cell_depth = 0
            return
        if self._candidate_index is not None:
            self._candidate_parts.append(raw)
            self._candidate_depth += 1
        elif self._cell_index is not None:
            self._cell_parts.append(raw)
            self._cell_depth += 1
        elif self._label_active:
            self._label_depth += 1
        elif self._row_active:
            self._row_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or f"<{tag}/>"
        if self._candidate_index is not None:
            self._candidate_parts.append(raw)
        elif self._cell_index is not None:
            self._cell_parts.append(raw)

    def handle_endtag(self, tag: str) -> None:
        if self._candidate_index is not None:
            if self._candidate_depth:
                self._candidate_parts.append(f"</{tag}>")
                self._candidate_depth -= 1
            else:
                self.candidates.append("".join(self._candidate_parts))
                self._candidate_index = None
                self._candidate_parts = []
            return
        if self._cell_index is not None:
            if self._cell_depth:
                self._cell_parts.append(f"</{tag}>")
                self._cell_depth -= 1
            else:
                self._row_cells.append("".join(self._cell_parts))
                self._cell_index = None
                self._cell_parts = []
            return
        if self._label_active:
            if self._label_depth:
                self._label_depth -= 1
            else:
                self._label_active = False
            return
        if self._row_active:
            if self._row_depth:
                self._row_depth -= 1
                return
            self.rows.append(("".join(self._row_label).strip(), self._row_cells))
            self._row_active = False
            self._row_depth = 0
            self._row_label = []
            self._row_cells = []

    def handle_data(self, data: str) -> None:
        if self._candidate_index is not None:
            self._candidate_parts.append(data)
        elif self._cell_index is not None:
            self._cell_parts.append(data)
        elif self._label_active:
            self._row_label.append(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")

    def result(self) -> dict[str, Any]:
        return {"candidates": list(self.candidates), "rows": list(self.rows)}


def _comparison_matrix_groups(html_content: str) -> list[dict[str, Any]]:
    matrices = re.findall(
        r'<section\b[^>]*class=["\'][^"\']*\bcomparison-matrix\b[^"\']*["\'][^>]*>.*?</section>',
        html_content,
        flags=re.DOTALL | re.IGNORECASE,
    )
    groups: list[dict[str, Any]] = []
    for matrix in matrices:
        parser = _ComparisonMatrixParser()
        parser.feed(matrix)
        parser.close()
        group = parser.result()
        if len(group["candidates"]) >= 2:
            groups.append(group)
    return groups


def _extract_blocks(html_content: str) -> list[PdfBlock]:
    parser = _PublicHtmlParser()
    parser.feed(html_content)
    parser.close()
    return parser.blocks


def _register_fonts() -> tuple[object, str, str]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    latin_font = "Helvetica"
    latin_candidates = (
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/msttcorefonts/Arial.ttf"),
    )
    for path in latin_candidates:
        if not path.is_file():
            continue
        latin_font = "Designer-Arial"
        if latin_font not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(latin_font, str(path)))
        break

    # Chinese follows the host system font. PDF embeds the selected face so
    # the delivery remains readable on another computer. The CID fallback
    # keeps minimal Linux test environments functional.
    cjk_candidates = (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    )
    for path in cjk_candidates:
        if not path.is_file():
            continue
        cjk_font = "Designer-CJK"
        if cjk_font not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(cjk_font, str(path), subfontIndex=0))
        return pdfmetrics, latin_font, cjk_font
    cjk_font = "STSong-Light"
    if cjk_font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(cjk_font))
    return pdfmetrics, latin_font, cjk_font


def _font_for_char(char: str, latin_font: str, cjk_font: str) -> str:
    return latin_font if char.isascii() else cjk_font


def _mixed_markup(value: str, *, latin_font: str, cjk_font: str) -> str:
    """Keep English and numbers in Arial while Chinese uses the CJK face.

    Public numeric formatting uses Unicode superscripts for scientific
    notation. Some CJK PDF faces silently drop the superscript minus, so
    scientific exponents are converted to ReportLab ``super`` markup before
    font assignment. The exponent itself then uses the embedded Latin face.
    """

    if not value:
        return ""
    value = _SCIENTIFIC_SUPERSCRIPT_RUN.sub(
        lambda match: _SUPER_OPEN + match.group(0).translate(_SCIENTIFIC_SUPERSCRIPT_ASCII) + _SUPER_CLOSE,
        value,
    )

    def font_runs(text: str) -> str:
        runs: list[tuple[str, list[str]]] = []
        for char in text:
            font = _font_for_char(char, latin_font, cjk_font)
            if not runs or runs[-1][0] != font:
                runs.append((font, [char]))
            else:
                runs[-1][1].append(char)
        return "".join(
            f'<font name="{font}">{html_escape("".join(chars)).replace(chr(10), "<br/>")}</font>'
            for font, chars in runs
        )

    result: list[str] = []
    plain: list[str] = []

    def flush_plain() -> None:
        if plain:
            result.append(font_runs("".join(plain)))
            plain.clear()

    index = 0
    markers = {
        _SUB_OPEN: (_SUB_CLOSE, "sub"),
        _SUPER_OPEN: (_SUPER_CLOSE, "super"),
    }
    while index < len(value):
        marker = markers.get(value[index])
        if marker is None:
            plain.append(value[index])
            index += 1
            continue
        close, tag = marker
        end = value.find(close, index + 1)
        if end < 0:
            plain.append(value[index])
            index += 1
            continue
        flush_plain()
        result.append(f"<{tag}>{font_runs(value[index + 1:end])}</{tag}>")
        index = end + 1
    flush_plain()
    return "".join(result)


def _paragraph(value: str, style: object, *, latin_font: str, cjk_font: str) -> object:
    from reportlab.platypus import Paragraph

    return Paragraph(_mixed_markup(value, latin_font=latin_font, cjk_font=cjk_font), style)


def _rich_paragraph(markup: str, style: object) -> object:
    """Create a Paragraph from already escaped Designer font markup."""

    from reportlab.platypus import Paragraph

    return Paragraph(markup, style)


def _table_flowable(
    rows: list[list[str]],
    *,
    content_width: float,
    styles: dict[str, object],
    latin_font: str,
    cjk_font: str,
    compact: bool,
) -> object:
    """Render factual public tables with a stable warm-red header."""

    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    row_count = max((len(row) for row in rows), default=1)
    normalized = [row + [""] * (row_count - len(row)) for row in rows]
    data = [
        [_paragraph(cell, styles["table_head"] if index == 0 else styles["table"], latin_font=latin_font, cjk_font=cjk_font)
         for cell in row]
        for index, row in enumerate(normalized)
    ]
    table = Table(data, colWidths=[content_width / row_count] * row_count, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(TOKEN_COLORS["brand_red_soft"])),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor(TOKEN_COLORS["table_head_ink"])),
        ("LINEABOVE", (0, 0), (-1, 0), 1.2, colors.HexColor(TOKEN_COLORS["brand_red"])),
        ("LINEBELOW", (0, 0), (-1, -1), 0.35, colors.HexColor(TOKEN_COLORS["rule"])),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6 if compact else 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6 if compact else 7),
        ("TOPPADDING", (0, 0), (-1, -1), 4 if compact else 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4 if compact else 5),
    ]))
    return table


def _metric_grid_flowable(
    rows: list[list[str]],
    *,
    content_width: float,
    styles: dict[str, object],
    latin_font: str,
    cjk_font: str,
) -> object:
    """Keep Card facts in compact columns instead of a long vertical list."""

    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    columns = 3 if len(rows) >= 3 else max(1, len(rows))
    cells: list[object] = []
    for label, value, note in rows:
        line = _mixed_markup(label, latin_font=latin_font, cjk_font=cjk_font)
        line += "<br/>" + _mixed_markup(value or "未提供", latin_font=latin_font, cjk_font=cjk_font)
        if note:
            line += "<br/>" + _mixed_markup(note, latin_font=latin_font, cjk_font=cjk_font)
        cells.append(_rich_paragraph(line, styles["metric"]))
    while len(cells) % columns:
        cells.append(_rich_paragraph("", styles["metric"]))
    data = [cells[index:index + columns] for index in range(0, len(cells), columns)]
    table = Table(data, colWidths=[content_width / columns] * columns, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), 0.65, colors.HexColor(TOKEN_COLORS["rule_strong"])),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor(TOKEN_COLORS["rule"])),
        ("LINEBEFORE", (1, 0), (-1, -1), 0.3, colors.HexColor(TOKEN_COLORS["rule"])),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _card_page_height(flowables: list[object], *, content_width: float, top: float, bottom: float) -> float:
    """Size a Card PDF page to its real reader-visible content.

    A Card is a 210 mm-wide compact sheet, not a report preview cropped to an
    arbitrary fraction of A4.  Its height grows with the frozen text and
    tables.  A small guard band absorbs ReportLab paragraph spacing without
    introducing a report-sized blank tail.
    """

    content_height = 0.0
    for flowable in flowables:
        _width, height = flowable.wrap(content_width, 100_000)
        content_height += max(0.0, height)
    return content_height + top + bottom + 200


def _svg_number(value: str | None, fallback: float = 0.0) -> float:
    if not value:
        return fallback
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", value)
    return float(match.group(0)) if match else fallback


def _svg_styles(root: ElementTree.Element) -> dict[str, dict[str, str]]:
    """Read the restricted class palette emitted by the report-only SVG.

    Payoffer owns the source SVG.  Designer only translates common public SVG
    primitives into ReportLab vector calls so PDF retains the supplied visual
    evidence without browser execution or an external converter.
    """

    styles: dict[str, dict[str, str]] = {}
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].lower() != "style":
            continue
        for class_name, declaration in re.findall(r"\.([\w-]+)\s*\{([^}]*)\}", element.text or ""):
            styles[class_name] = {
                key.strip(): value.strip()
                for key, value in re.findall(r"([\w-]+)\s*:\s*([^;]+)", declaration)
            }
    return styles


def _svg_paint(element: ElementTree.Element, styles: Mapping[str, Mapping[str, str]], inherited: Mapping[str, str]) -> dict[str, str]:
    paint = dict(inherited)
    for class_name in (element.get("class") or "").split():
        paint.update(styles.get(class_name, {}))
    for name in ("fill", "stroke", "stroke-width", "stroke-dasharray", "font-size", "font-weight", "opacity", "fill-opacity", "stroke-opacity"):
        if element.get(name) is not None:
            paint[name] = str(element.get(name))
    paint.update({key.strip(): value.strip() for key, value in re.findall(r"([\w-]+)\s*:\s*([^;]+)", element.get("style") or "")})
    return paint


def _svg_color(value: str | None, *, fallback: str | None = None) -> str | None:
    candidate = (value or fallback or "").strip()
    if not candidate or candidate.lower() in {"none", "transparent"}:
        return None
    if candidate.startswith("url("):
        # Payoffer's report SVG uses its governed red-gold title gradient.
        # ReportLab has no SVG gradient decoder; retain the canonical brand-red
        # visual rather than dropping the supplied title band.
        return TOKEN_COLORS["brand_red"]
    if re.fullmatch(r"#[0-9A-Fa-f]{3}", candidate):
        return "#" + "".join(char * 2 for char in candidate[1:])
    return candidate if re.fullmatch(r"#[0-9A-Fa-f]{6}", candidate) else fallback


def _svg_path_commands(raw: str) -> list[tuple[str, list[float]]]:
    tokens = re.findall(r"[MmLlHhVvCcQqZz]|[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", raw)
    size = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "Q": 4, "Z": 0}
    commands: list[tuple[str, list[float]]] = []
    index = 0
    active = ""
    while index < len(tokens):
        if tokens[index].isalpha():
            active = tokens[index]
            index += 1
            if active.upper() == "Z":
                commands.append((active, []))
                active = ""
                continue
        if not active:
            break
        count = size.get(active.upper())
        if count is None or index + count > len(tokens) or any(token.isalpha() for token in tokens[index:index + count]):
            break
        values = [float(token) for token in tokens[index:index + count]]
        index += count
        commands.append((active, values))
        if active in {"M", "m"}:
            active = "L" if active == "M" else "l"
    return commands


def _svg_flowable(markup: str, *, content_width: float, latin_font: str, cjk_font: str) -> object:
    """Return a bounded vector Flowable for a Reporter-provided payoff SVG."""

    from reportlab.lib import colors
    from reportlab.platypus import Flowable

    try:
        root = ElementTree.fromstring(markup)
    except ElementTree.ParseError as error:
        raise PdfRuntimeError("收益图SVG格式无效，无法生成等价PDF图形。") from error
    view_box = (root.get("viewBox") or "").replace(",", " ").split()
    source_width = _svg_number(root.get("width"), 1200.0)
    source_height = _svg_number(root.get("height"), 566.0)
    if len(view_box) == 4:
        offset_x, offset_y, source_width, source_height = (float(value) for value in view_box)
    else:
        offset_x = offset_y = 0.0
    if source_width <= 0 or source_height <= 0:
        raise PdfRuntimeError("收益图SVG缺少有效画布尺寸。")
    maximum_height = 310.0

    def fitted_size(available_width: float) -> tuple[float, float]:
        """Fit the SVG without changing its aspect ratio."""

        width_limit = max(1.0, min(content_width, available_width))
        scale = min(width_limit / source_width, maximum_height / source_height)
        return source_width * scale, source_height * scale

    width, height = fitted_size(content_width)
    styles = _svg_styles(root)

    class _PayoffSvg(Flowable):
        def __init__(self) -> None:
            super().__init__()
            self.width = width
            self.height = height

        def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
            # Ignore the current page's remaining height.  A too-tall figure
            # should move to the next page rather than shrink into the leftover
            # strip at the bottom of the current one.
            self.width, self.height = fitted_size(available_width)
            return self.width, self.height

        def draw(self) -> None:
            canvas = self.canv
            scale_x = self.width / source_width
            scale_y = self.height / source_height

            def point(x: float, y: float) -> tuple[float, float]:
                return ((x - offset_x) * scale_x, self.height - (y - offset_y) * scale_y)

            def set_paint(paint: Mapping[str, str]) -> tuple[bool, bool]:
                fill = _svg_color(paint.get("fill"), fallback=TOKEN_COLORS["ink"])
                stroke = _svg_color(paint.get("stroke"))
                opacity = max(0.0, min(1.0, _svg_number(paint.get("opacity"), 1.0)))
                if fill:
                    canvas.setFillColor(colors.HexColor(fill), alpha=opacity * _svg_number(paint.get("fill-opacity"), 1.0))
                if stroke:
                    canvas.setStrokeColor(colors.HexColor(stroke), alpha=opacity * _svg_number(paint.get("stroke-opacity"), 1.0))
                    canvas.setLineWidth(max(0.2, _svg_number(paint.get("stroke-width"), 1.0) * (scale_x + scale_y) / 2))
                    dash = [number for number in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", paint.get("stroke-dasharray", ""))]
                    canvas.setDash([float(number) * scale_x for number in dash] if dash else [])
                else:
                    canvas.setDash()
                return bool(fill), bool(stroke)

            def draw_path(raw: str, paint: Mapping[str, str]) -> None:
                path = canvas.beginPath()
                current = (0.0, 0.0)
                origin = (0.0, 0.0)
                for command, values in _svg_path_commands(raw):
                    absolute = command.isupper()
                    name = command.upper()
                    if name == "M":
                        x, y = values
                        current = (x, y) if absolute else (current[0] + x, current[1] + y)
                        origin = current
                        path.moveTo(*point(*current))
                    elif name == "L":
                        x, y = values
                        current = (x, y) if absolute else (current[0] + x, current[1] + y)
                        path.lineTo(*point(*current))
                    elif name == "H":
                        x = values[0] if absolute else current[0] + values[0]
                        current = (x, current[1])
                        path.lineTo(*point(*current))
                    elif name == "V":
                        y = values[0] if absolute else current[1] + values[0]
                        current = (current[0], y)
                        path.lineTo(*point(*current))
                    elif name == "C":
                        coords = [(values[index], values[index + 1]) for index in range(0, 6, 2)]
                        if not absolute:
                            coords = [(current[0] + x, current[1] + y) for x, y in coords]
                        path.curveTo(*point(*coords[0]), *point(*coords[1]), *point(*coords[2]))
                        current = coords[2]
                    elif name == "Q":
                        # ReportLab has no quadratic primitive.  Preserve the
                        # factual end point as a straight segment rather than
                        # inventing a different curve.
                        x, y = values[-2:]
                        current = (x, y) if absolute else (current[0] + x, current[1] + y)
                        path.lineTo(*point(*current))
                    elif name == "Z":
                        path.close()
                        current = origin
                fill, stroke = set_paint(paint)
                canvas.drawPath(path, stroke=int(stroke), fill=int(fill))

            def walk(element: ElementTree.Element, inherited: Mapping[str, str]) -> None:
                tag = element.tag.rsplit("}", 1)[-1].lower()
                if tag in {"defs", "style", "title", "desc", "clippath", "lineargradient", "stop"}:
                    return
                paint = _svg_paint(element, styles, inherited)
                if tag == "rect":
                    fill, stroke = set_paint(paint)
                    x, y = point(_svg_number(element.get("x")), _svg_number(element.get("y")) + _svg_number(element.get("height")))
                    canvas.rect(x, y, _svg_number(element.get("width")) * scale_x, _svg_number(element.get("height")) * scale_y, stroke=int(stroke), fill=int(fill))
                elif tag == "line":
                    _fill, stroke = set_paint(paint)
                    if stroke:
                        canvas.line(*point(_svg_number(element.get("x1")), _svg_number(element.get("y1"))), *point(_svg_number(element.get("x2")), _svg_number(element.get("y2"))))
                elif tag == "circle":
                    fill, stroke = set_paint(paint)
                    x, y = point(_svg_number(element.get("cx")), _svg_number(element.get("cy")))
                    radius = _svg_number(element.get("r")) * (scale_x + scale_y) / 2
                    canvas.circle(x, y, radius, stroke=int(stroke), fill=int(fill))
                elif tag in {"path", "polyline", "polygon"}:
                    raw = element.get("d") or ""
                    if tag != "path":
                        numbers = [float(value) for value in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", element.get("points") or "")]
                        raw = "M" + " L".join(f"{numbers[index]},{numbers[index + 1]}" for index in range(0, len(numbers) - 1, 2))
                        if tag == "polygon":
                            raw += " Z"
                    if raw:
                        draw_path(raw, paint)
                elif tag == "text":
                    content = _clean_text("".join(element.itertext()))
                    if content:
                        _fill, _stroke = set_paint(paint)
                        font_size = max(5.0, _svg_number(paint.get("font-size"), 11.0) * scale_y)
                        canvas.setFont(cjk_font if any(not char.isascii() for char in content) else latin_font, font_size)
                        x, y = point(_svg_number(element.get("x")), _svg_number(element.get("y")))
                        anchor = element.get("text-anchor")
                        if anchor == "middle":
                            canvas.drawCentredString(x, y, content)
                        elif anchor == "end":
                            canvas.drawRightString(x, y, content)
                        else:
                            canvas.drawString(x, y, content)
                for child in list(element):
                    walk(child, paint)

            canvas.saveState()
            walk(root, {"fill": TOKEN_COLORS["ink"]})
            canvas.restoreState()

    return _PayoffSvg()


def _chart_flowable(spec: Mapping[str, Any], *, content_width: float, latin_font: str, cjk_font: str) -> object:
    """Render frozen ECharts facts as a controlled static PDF chart.

    This uses the exact chart specification that powers the offline HTML
    visual.  The PDF does not execute browser JavaScript but retains a true
    graphical projection alongside the existing accessible data table.
    """

    from reportlab.lib import colors
    from reportlab.platypus import Flowable

    chart_type = str(spec.get("type") or "line")
    x_values = list(spec.get("x") or [])
    y_values = list(spec.get("y") or [])
    series = list(spec.get("series") or [])
    width = content_width
    height = min(max(190.0, width * 0.45), 270.0)
    palette = (
        TOKEN_COLORS["brand_red"],
        TOKEN_COLORS["blue_gray"],
        TOKEN_COLORS["risk_gold"],
        TOKEN_COLORS["chart_gray"],
    )

    def number(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    def interpolate_hex(start: str, end: str, ratio: float) -> str:
        """Interpolate two Designer colors without introducing a new palette."""

        def rgb(value: str) -> tuple[int, int, int]:
            value = value.removeprefix("#")
            return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))

        ratio = max(0.0, min(1.0, ratio))
        start_rgb, end_rgb = rgb(start), rgb(end)
        channels = [round(left + (right - left) * ratio) for left, right in zip(start_rgb, end_rgb)]
        return "#" + "".join(f"{channel:02X}" for channel in channels)

    class _Chart(Flowable):
        def __init__(self) -> None:
            super().__init__()
            self.width = width
            self.height = height

        def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
            self.width = min(width, available_width)
            return self.width, self.height

        def draw(self) -> None:
            canvas = self.canv
            left, right, top, bottom = 44.0, 16.0, 20.0, 34.0
            plot_width, plot_height = max(1.0, self.width - left - right), max(1.0, self.height - top - bottom)
            canvas.saveState()
            canvas.setFillColor(colors.HexColor(TOKEN_COLORS["paper"]))
            canvas.setStrokeColor(colors.HexColor(TOKEN_COLORS["rule_strong"]))
            canvas.setLineWidth(.55)
            canvas.rect(0, 0, self.width, self.height, fill=1, stroke=1)
            canvas.setStrokeColor(colors.HexColor(TOKEN_COLORS["chart_gray"]))
            canvas.setLineWidth(.45)
            canvas.line(left, bottom, left, bottom + plot_height)
            canvas.line(left, bottom, left + plot_width, bottom)
            if chart_type == "heatmap":
                data = [row for row in list(spec.get("data") or []) if isinstance(row, list) and len(row) == 3 and number(row[2]) is not None]
                values = [number(row[2]) for row in data]
                low, high = (min(values), max(values)) if values else (0.0, 1.0)
                x_count, y_count = max(1, len(x_values)), max(1, len(y_values))
                for row in data:
                    try:
                        x_index, y_index = int(row[0]), int(row[1])
                    except (TypeError, ValueError):
                        continue
                    value = number(row[2])
                    ratio = 0.5 if high == low else max(0.0, min(1.0, (value - low) / (high - low)))
                    if ratio <= 0.5:
                        fill = interpolate_hex(TOKEN_COLORS["heatmap_low"], TOKEN_COLORS["brand_red"], ratio * 2)
                    else:
                        fill = interpolate_hex(TOKEN_COLORS["brand_red"], TOKEN_COLORS["blue_gray"], (ratio - 0.5) * 2)
                    canvas.setFillColor(colors.HexColor(fill))
                    canvas.rect(left + x_index * plot_width / x_count, bottom + y_index * plot_height / y_count, plot_width / x_count, plot_height / y_count, fill=1, stroke=0)
            else:
                values = [number(item) for entry in series for item in list(entry.get("data") or [])]
                finite = [item for item in values if item is not None]
                low, high = (min(finite), max(finite)) if finite else (0.0, 1.0)
                if chart_type == "bar":
                    # Bar charts express a magnitude against zero.  Include
                    # that baseline even when every frozen observation shares
                    # one sign, otherwise the bars extend beyond the frame.
                    low, high = min(low, 0.0), max(high, 0.0)
                if low == high:
                    low, high = low - 1.0, high + 1.0
                for tick in range(5):
                    y = bottom + plot_height * tick / 4
                    canvas.setStrokeColor(colors.HexColor(TOKEN_COLORS["rule"]))
                    canvas.setDash(2, 2)
                    canvas.line(left, y, left + plot_width, y)
                canvas.setDash()
                count = max(1, len(x_values) - 1)
                for series_index, entry in enumerate(series):
                    points = []
                    for index, item in enumerate(list(entry.get("data") or [])):
                        value = number(item)
                        if value is None:
                            continue
                        x = left + plot_width * min(index, count) / count
                        y = bottom + (value - low) / (high - low) * plot_height
                        points.append((x, y))
                    color = colors.HexColor(palette[series_index % len(palette)])
                    canvas.setStrokeColor(color)
                    canvas.setFillColor(color)
                    canvas.setLineWidth(1.6)
                    if chart_type == "bar":
                        bar_width = max(2.0, plot_width / max(1, len(x_values)) / max(1, len(series)))
                        for point_index, (x, y) in enumerate(points):
                            baseline = bottom + (0 - low) / (high - low) * plot_height
                            center_offset = (series_index - (len(series) - 1) / 2) * bar_width
                            canvas.rect(x + center_offset - bar_width * .4, min(baseline, y), bar_width * .8, abs(y - baseline), fill=1, stroke=0)
                    else:
                        path = canvas.beginPath()
                        for point_index, (x, y) in enumerate(points):
                            (path.moveTo if point_index == 0 else path.lineTo)(x, y)
                        canvas.drawPath(path, stroke=1, fill=0)
                        for x, y in points:
                            canvas.circle(x, y, 1.8, stroke=0, fill=1)
                canvas.setFont(latin_font, 6.8)
                canvas.setFillColor(colors.HexColor(TOKEN_COLORS["muted"]))
                for tick in range(5):
                    value = low + (high - low) * tick / 4
                    canvas.drawRightString(left - 5, bottom + plot_height * tick / 4 - 2, f"{value:.2g}")
                if x_values:
                    labels = [str(x_values[index]) for index in range(0, len(x_values), max(1, math.ceil(len(x_values) / 6)))]
                    for index, label in enumerate(labels):
                        x = left + plot_width * index * max(1, math.ceil(len(x_values) / 6)) / count
                        canvas.setFont(cjk_font if any(not char.isascii() for char in label) else latin_font, 6.8)
                        canvas.drawCentredString(x, bottom - 12, label[:12])
            title = str(spec.get("title") or "图表")
            canvas.setFont(cjk_font if any(not char.isascii() for char in title) else latin_font, 8.5)
            canvas.setFillColor(colors.HexColor(TOKEN_COLORS["ink_soft"]))
            canvas.drawString(left, self.height - 12, title)
            canvas.restoreState()

    return _Chart()


def render_pdf(html_content: str, *, chart_specs: Mapping[str, Mapping[str, Any]] | None = None) -> bytes:
    """Render public HTML to a self-contained PDF.

    Report uses A4. Card keeps the same 210 mm width while its PDF page height
    follows the rendered facts, so a short card does not carry a report-sized
    blank tail.
    """

    _require_runtime()
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (
        BaseDocTemplate,
        CondPageBreak,
        Frame,
        HRFlowable,
        KeepTogether,
        PageTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    _pdfmetrics, latin_font, cjk_font = _register_fonts()
    comparison_groups = _comparison_matrix_groups(html_content)
    document_html = html_content
    if comparison_groups:
        document_html = re.sub(
            r'<section\b[^>]*class=["\'][^"\']*\bcomparison-matrix\b[^"\']*["\'][^>]*>.*?</section>',
            "",
            html_content,
            flags=re.DOTALL | re.IGNORECASE,
        )
    blocks = _extract_blocks(document_html)
    if not blocks:
        raise PdfRuntimeError("PDF输入未包含可交付的公开内容。")

    # The shared stylesheet contains selectors such as
    # ``[data-output-type=\"card\"]`` even in a detailed Report.  Only a real
    # document element may select the compact one-page PDF geometry.
    is_card = bool(re.search(r'<(?:body|main|article)\b[^>]*\bdata-output-type\s*=\s*["\']card["\']', html_content, re.IGNORECASE))
    title = next(
        (str(value) for kind, level, value in blocks if kind == "heading" and level == 1),
        "单个期权结构推荐报告",
    )
    if not is_card and blocks and blocks[0][0] == "paragraph" and str(blocks[0][2]) == "光大证券 金融创新业务总部":
        # The institutional identity lives in the fixed PDF header on every
        # report page, so do not render it twice before the title.
        blocks = blocks[1:]

    page_width, report_page_height = A4
    left, right, top, bottom = ((18, 18, 18, 16) if is_card else (42, 42, 52, 36))
    content_width = page_width - left - right
    compact = is_card
    body_size = 9.1 if compact else 10.1
    styles = {
        "title": ParagraphStyle("title", fontName=cjk_font, fontSize=16 if compact else 20, leading=20 if compact else 25,
                                textColor=colors.HexColor(TOKEN_COLORS["ink"]), spaceAfter=7 if compact else 13, alignment=TA_LEFT),
        "section": ParagraphStyle("section", fontName=cjk_font, fontSize=10.5 if compact else 14, leading=13 if compact else 18,
                                  textColor=colors.HexColor(TOKEN_COLORS["brand_red_deep"]), spaceBefore=7 if compact else 13, spaceAfter=4),
        "subsection": ParagraphStyle("subsection", fontName=cjk_font, fontSize=9.2 if compact else 11.5, leading=12 if compact else 15,
                                     textColor=colors.HexColor(TOKEN_COLORS["ink_soft"]), spaceBefore=6, spaceAfter=3),
        "body": ParagraphStyle("body", fontName=cjk_font, fontSize=body_size, leading=body_size * 1.58,
                               textColor=colors.HexColor(TOKEN_COLORS["ink"]), spaceAfter=4),
        "item": ParagraphStyle("item", fontName=cjk_font, fontSize=body_size, leading=body_size * 1.52,
                               textColor=colors.HexColor(TOKEN_COLORS["ink"]), leftIndent=10, firstLineIndent=-8, spaceAfter=2),
        "caption": ParagraphStyle("caption", fontName=cjk_font, fontSize=8.6 if compact else 9.3, leading=12,
                                  textColor=colors.HexColor(TOKEN_COLORS["blue_gray"]), spaceBefore=4, spaceAfter=3),
        "table": ParagraphStyle("table", fontName=cjk_font, fontSize=8.0 if compact else 8.8, leading=10.5 if compact else 12,
                                textColor=colors.HexColor(TOKEN_COLORS["ink_soft"])),
        "table_head": ParagraphStyle("table_head", fontName=cjk_font, fontSize=8.1 if compact else 8.9,
                                     leading=10.5 if compact else 12, textColor=colors.HexColor(TOKEN_COLORS["table_head_ink"])),
        "metric": ParagraphStyle("metric", fontName=cjk_font, fontSize=8.2, leading=10.4,
                                 textColor=colors.HexColor(TOKEN_COLORS["ink_soft"])),
    }

    def block_flowables(source_blocks: list[PdfBlock], *, available_width: float) -> list[object]:
        result: list[object] = []
        for kind, level, raw in source_blocks:
            if kind == "heading":
                if level == 1:
                    result.append(_paragraph(str(raw), styles["title"], latin_font=latin_font, cjk_font=cjk_font))
                    result.append(HRFlowable(width="100%", thickness=1.25, color=colors.HexColor(TOKEN_COLORS["brand_red"]), spaceAfter=4))
                elif level == 2:
                    if not compact:
                        result.append(CondPageBreak(72))
                    heading = _paragraph(str(raw), styles["section"], latin_font=latin_font, cjk_font=cjk_font)
                    rule = HRFlowable(width="100%", thickness=.45, color=colors.HexColor(TOKEN_COLORS["rule_strong"]), spaceAfter=5)
                    result.append(KeepTogether([heading, rule]) if not compact else heading)
                    if compact:
                        result.append(rule)
                else:
                    result.append(_paragraph(str(raw), styles["subsection"], latin_font=latin_font, cjk_font=cjk_font))
                continue
            if kind == "metric_grid":
                rows = raw if isinstance(raw, list) else []
                result.append(_metric_grid_flowable(rows, content_width=available_width, styles=styles, latin_font=latin_font, cjk_font=cjk_font))
                result.append(Spacer(1, 4))
                continue
            if kind == "table":
                rows = raw if isinstance(raw, list) else []
                if rows:
                    result.append(_table_flowable(rows, content_width=available_width, styles=styles, latin_font=latin_font, cjk_font=cjk_font, compact=compact))
                    result.append(Spacer(1, 5))
                continue
            if kind == "svg":
                result.append(_svg_flowable(str(raw), content_width=available_width, latin_font=latin_font, cjk_font=cjk_font))
                result.append(Spacer(1, 5))
                continue
            if kind == "chart":
                spec = (chart_specs or {}).get(str(raw))
                if spec is None:
                    raise PdfRuntimeError(f"报告图表{raw}缺少受控静态图规格。")
                result.append(_chart_flowable(spec, content_width=available_width, latin_font=latin_font, cjk_font=cjk_font))
                result.append(Spacer(1, 4))
                continue
            if kind == "item":
                result.append(_paragraph("• " + str(raw), styles["item"], latin_font=latin_font, cjk_font=cjk_font))
                continue
            style = styles["caption"] if kind == "caption" else styles["body"]
            result.append(_paragraph(str(raw), style, latin_font=latin_font, cjk_font=cjk_font))
        return result

    flowables = block_flowables(blocks, available_width=content_width)
    for comparison_group in comparison_groups:
        comparison_candidates = list(comparison_group["candidates"])
        comparison_rows = list(comparison_group["rows"])
        columns = len(comparison_candidates)
        label_width = 25 * 72 / 25.4
        cell_width = (content_width - label_width) / columns
        header_cells = [[]]
        header_cells.extend(
            block_flowables(_extract_blocks(fragment), available_width=cell_width - 12)
            for fragment in comparison_candidates
        )
        header_table = Table(
            [header_cells],
            colWidths=[label_width, *([cell_width] * columns)],
            hAlign="LEFT",
            splitByRow=1,
        )
        header_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEABOVE", (0, 0), (-1, 0), 1.0, colors.HexColor(TOKEN_COLORS["brand_red"])),
            ("LINEBELOW", (0, 0), (-1, 0), .55, colors.HexColor(TOKEN_COLORS["rule_strong"])),
            ("LINEBEFORE", (1, 0), (-1, 0), .35, colors.HexColor(TOKEN_COLORS["rule"])),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        flowables.append(header_table)
        for label, cells in comparison_rows:
            row_cells = [[
                [_paragraph(label, styles["subsection"], latin_font=latin_font, cjk_font=cjk_font)],
                *[
                    block_flowables(
                        _extract_blocks(cells[index] if index < len(cells) else ""),
                        available_width=cell_width - 12,
                    )
                    for index in range(columns)
                ],
            ]]
            row_table = Table(
                row_cells,
                colWidths=[label_width, *([cell_width] * columns)],
                hAlign="LEFT",
                splitByRow=1,
            )
            row_style = [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), .35, colors.HexColor(TOKEN_COLORS["rule"])),
                ("LINEBEFORE", (1, 0), (-1, 0), .35, colors.HexColor(TOKEN_COLORS["rule"])),
                ("LINEAFTER", (0, 0), (0, 0), .55, colors.HexColor(TOKEN_COLORS["rule_strong"])),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
            if label == "主要风险":
                row_style.append(("LINEABOVE", (0, 0), (-1, 0), .9, colors.HexColor(TOKEN_COLORS["brand_red"])))
            row_table.setStyle(TableStyle(row_style))
            flowables.append(row_table)

    page_height = _card_page_height(flowables, content_width=content_width, top=top, bottom=bottom) if compact else report_page_height
    buffer = BytesIO()
    document = BaseDocTemplate(
        buffer,
        pagesize=(page_width, page_height),
        leftMargin=left,
        rightMargin=right,
        topMargin=top,
        bottomMargin=bottom,
        title=title,
        author="研究交付",
        subject="单个期权结构推荐交付",
        pageCompression=1,
    )

    def on_page(canvas: object, doc: object) -> None:
        canvas.saveState()
        canvas.setFillColor(colors.HexColor(TOKEN_COLORS["paper"]))
        canvas.rect(0, 0, page_width, page_height, stroke=0, fill=1)
        if compact:
            canvas.setStrokeColor(colors.HexColor(TOKEN_COLORS["brand_red"]))
            canvas.setLineWidth(2.6)
            canvas.line(left, page_height - 10, page_width - right, page_height - 10)
        else:
            canvas.setFillColor(colors.HexColor(TOKEN_COLORS["blue_gray"]))
            canvas.setFont(cjk_font, 8.2)
            canvas.drawString(left, page_height - 23, "光大证券 金融创新业务总部")
            canvas.setStrokeColor(colors.HexColor(TOKEN_COLORS["risk_gold"]))
            canvas.setLineWidth(.7)
            canvas.line(left, page_height - 30, page_width - right, page_height - 30)
            canvas.setStrokeColor(colors.HexColor(TOKEN_COLORS["brand_red"]))
            canvas.setLineWidth(1.8)
            canvas.line(left, page_height - 31.2, left + 122, page_height - 31.2)
            canvas.setStrokeColor(colors.HexColor(TOKEN_COLORS["rule_strong"]))
            canvas.setLineWidth(.45)
            canvas.line(left, 23, page_width - right, 23)
            footer = f"第{getattr(doc, 'page', 1)}页"
            canvas.setFillColor(colors.HexColor(TOKEN_COLORS["muted"]))
            canvas.setFont(cjk_font, 8)
            canvas.drawRightString(page_width - right, 12, footer)
        canvas.restoreState()

    frame = Frame(left, bottom, content_width, page_height - top - bottom, id="content", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    document.addPageTemplates([PageTemplate(id="designer", frames=[frame], onPage=on_page)])

    document.build(flowables)
    result = buffer.getvalue()
    if not result.startswith(b"%PDF-") or len(result) < 1024:
        raise PdfRuntimeError("PDF运行组件未生成有效文件。")
    return result


__all__ = ["PdfRuntimeError", "REPORTLAB_VERSION", "render_pdf", "runtime_status"]
