"""Small, deterministic PDF renderer for public Designer deliveries.

The App and Skill need a real PDF path that is independent of a browser or a
running desktop session.  This module intentionally consumes the already
sanitised Designer HTML, keeps only reader-visible material, and writes a
paginated PDF with ReportLab's built-in Chinese CID font mapping.  It does not
read result directories, perform financial calculations, or alter facts.
"""

from __future__ import annotations

from html import escape as html_escape
from html.parser import HTMLParser
import importlib.metadata
from pathlib import Path
import re
from typing import Literal
from xml.etree import ElementTree

from .design_tokens import TOKENS


REPORTLAB_VERSION = "5.0.0"
TOKEN_COLORS = TOKENS.colors


class PdfRuntimeError(RuntimeError):
    """Raised when the pinned local PDF runtime is not usable."""


BlockKind = Literal["heading", "paragraph", "item", "caption", "table", "metric_grid"]
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
        self._math_parts: list[str] | None = None

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
            alt = _clean_text(dict(attrs).get("alt") or "")
            if alt:
                self.blocks.append(("caption", 0, f"图示：{alt}"))
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
        if tag in {"th", "td"} and self._cell is not None:
            if self._table_row is not None:
                self._table_row.append(_clean_text("".join(self._cell)))
            self._cell = None
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
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
        Path("C:/Windows/Fonts/msyh.ttc"),
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


def render_pdf(html_content: str) -> bytes:
    """Render public HTML to a self-contained PDF.

    Card and Report share a fixed A4-width PDF surface. Card remains compact,
    but continues on later pages when complete frozen facts need more space.
    """

    _require_runtime()
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import BaseDocTemplate, CondPageBreak, HRFlowable, KeepTogether, PageTemplate, Spacer, Frame

    _pdfmetrics, latin_font, cjk_font = _register_fonts()
    blocks = _extract_blocks(html_content)
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

    page_width, page_height = A4
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

    buffer = BytesIO()
    document = BaseDocTemplate(
        buffer,
        pagesize=A4,
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

    flowables: list[object] = []
    for kind, level, raw in blocks:
        if kind == "heading":
            if level == 1:
                flowables.append(_paragraph(str(raw), styles["title"], latin_font=latin_font, cjk_font=cjk_font))
                flowables.append(HRFlowable(width="100%", thickness=1.25, color=colors.HexColor(TOKEN_COLORS["brand_red"]), spaceAfter=4))
            elif level == 2:
                # Do not strand a report chapter title at the bottom of a
                # page. Reserve room for its rule and first line of evidence;
                # ReportLab then moves the complete opening to the next A4
                # page when the current page cannot hold that minimum.
                flowables.append(CondPageBreak(48 if compact else 72))
                heading = _paragraph(str(raw), styles["section"], latin_font=latin_font, cjk_font=cjk_font)
                rule = HRFlowable(width="100%", thickness=.45, color=colors.HexColor(TOKEN_COLORS["rule_strong"]), spaceAfter=5)
                flowables.append(KeepTogether([heading, rule]))
            else:
                flowables.append(_paragraph(str(raw), styles["subsection"], latin_font=latin_font, cjk_font=cjk_font))
            continue
        if kind == "metric_grid":
            rows = raw if isinstance(raw, list) else []
            flowables.append(_metric_grid_flowable(rows, content_width=content_width, styles=styles, latin_font=latin_font, cjk_font=cjk_font))
            flowables.append(Spacer(1, 4))
            continue
        if kind == "table":
            rows = raw if isinstance(raw, list) else []
            if rows:
                flowables.append(_table_flowable(rows, content_width=content_width, styles=styles, latin_font=latin_font, cjk_font=cjk_font, compact=compact))
                flowables.append(Spacer(1, 5))
            continue
        if kind == "item":
            flowables.append(_paragraph("• " + str(raw), styles["item"], latin_font=latin_font, cjk_font=cjk_font))
            continue
        style = styles["caption"] if kind == "caption" else styles["body"]
        flowables.append(_paragraph(str(raw), style, latin_font=latin_font, cjk_font=cjk_font))

    document.build(flowables)
    result = buffer.getvalue()
    if not result.startswith(b"%PDF-") or len(result) < 1024:
        raise PdfRuntimeError("PDF运行组件未生成有效文件。")
    return result


__all__ = ["PdfRuntimeError", "REPORTLAB_VERSION", "render_pdf", "runtime_status"]
