"""Editable working documents with immutable original ReportRun evidence."""

from __future__ import annotations

import base64
from copy import deepcopy
from html import escape
from html.parser import HTMLParser
import json
from pathlib import PurePosixPath
import re
from typing import Any, Callable, Mapping
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

from modules.designer.config import load_designer_config
from modules.designer.design_renderer import DesignerDependencyError, _chart_specs_from_html
from modules.designer.pdf_renderer import PdfRuntimeError, render_pdf
from modules.designer.renderer import validate_chart

from .errors import AuthorizationError, UserActionError, ValidationError
from .identity.session_identity import SessionIdentity
from .stores.result_store import ResultStore
from .task_runtime.task_service import TaskService


_HTML_MEDIA_TYPE = "text/html; charset=utf-8"
_PDF_MEDIA_TYPE = "application/pdf"
_DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"})
_REPORT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_CHART_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,127}\Z")
_DROP_CONTENT_TAGS = frozenset({
    "script", "noscript", "iframe", "frame", "frameset", "object", "embed", "form",
    "input", "button", "textarea", "select", "option", "template", "portal", "foreignobject",
    "animate", "animatetransform", "animatemotion", "set", "discard",
})
_DROP_TAGS = frozenset({"base", "meta", "link"})
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "frame", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_URL_ATTRIBUTES = frozenset({"href", "src", "xlink:href", "action", "formaction", "poster", "data", "background"})
_UNSAFE_ATTRIBUTES = frozenset({
    "srcdoc", "srcset", "imagesrcset", "imagesizes", "manifest", "target", "download",
    "ping", "integrity", "crossorigin", "nonce", "xml:base",
})
# Only passive, literal CSS is retained. Escaped tokens and unknown functions
# are deliberately outside this editor's offline stylesheet subset.
_CSS_IDENTIFIER = re.compile(r"[-_a-zA-Z][-_a-zA-Z0-9]*")
_CSS_FUNCTIONS = frozenset({
    "rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab", "oklch", "color", "color-mix",
    "calc", "min", "max", "clamp", "var", "env", "attr", "counter", "counters",
    "linear-gradient", "radial-gradient", "conic-gradient", "repeating-linear-gradient",
    "repeating-radial-gradient", "repeating-conic-gradient", "minmax", "repeat", "fit-content",
    "translate", "translatex", "translatey", "translatez", "translate3d", "scale", "scalex",
    "scaley", "scalez", "scale3d", "rotate", "rotatex", "rotatey", "rotatez", "rotate3d",
    "skew", "skewx", "skewy", "matrix", "matrix3d", "perspective", "cubic-bezier", "steps",
    "blur", "brightness", "contrast", "drop-shadow", "grayscale", "hue-rotate", "invert",
    "opacity", "saturate", "sepia", "rect", "inset", "circle", "ellipse", "polygon", "path",
    "not", "is", "where", "has", "nth-child", "nth-last-child", "nth-of-type", "nth-last-of-type",
    "lang", "dir", "selector", "local", "format", "tech",
})
_CSS_AT_RULES = frozenset({"media", "supports", "page", "font-face", "keyframes", "-webkit-keyframes", "container", "layer"})
_SVG_PAINT_ATTRIBUTES = frozenset({"fill", "stroke", "filter", "clip-path", "mask", "marker", "marker-start", "marker-mid", "marker-end", "cursor"})
_TITLE = re.compile(r"<title\b[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)


class ReportEditor:
    """Load, sanitize and save manual copies without rebuilding report facts."""

    def __init__(
        self,
        results: ResultStore,
        tasks: TaskService,
        *,
        chart_presentation_source: str,
    ) -> None:
        self._results = results
        self._tasks = tasks
        self._chart_presentation_source = _validated_chart_presentation_source(chart_presentation_source)

    def import_document(self, identity: SessionIdentity, request: Mapping[str, Any], *, report_id: str | None = None, output_type: str = "report", comparison: bool = False) -> dict[str, Any]:
        if set(request) != {"task_id", "title", "html", "chart_specs"}:
            raise ValidationError("导入报告需要任务、标题、HTML正文和图表规格")
        task_id = str(request["task_id"])
        self._tasks.get(identity, task_id)
        title = _validated_title(request["title"])
        raw_html = request["html"]
        if not isinstance(raw_html, str) or not raw_html.strip() or len(raw_html.encode("utf-8")) > 12 * 1024 * 1024:
            raise ValidationError("HTML必须是非空且不超过12MB的文本")
        charts = _validated_chart_specs(request["chart_specs"])
        def no_external_resource(name):
            raise KeyError(name)
        sanitizer = _ReportHtmlSanitizer("report.html", no_external_resource)
        sanitizer.feed(raw_html)
        sanitizer.close()
        clean = _set_document_title(sanitizer.result(), title)
        report_id = report_id or "document-" + uuid4().hex
        try:
            existing = self.editor_payload(identity, report_id)
        except KeyError:
            existing = None
        if existing is not None:
            if existing["task_id"] != task_id:
                raise ValidationError("报告不属于当前任务")
            return {"report_run_id": report_id}
        reference = self._results.commit_report_run(identity, task_id,
            {"report_run_id": report_id, "task_id": task_id, "output_type": output_type, "format": "html", "comparison": comparison,
             "manual_edit": True, "chart_specs": charts, "metadata": {"title": title}},
            [{"name": "report.html", "content_type": _HTML_MEDIA_TYPE, "content": clean.encode("utf-8")}])
        result = self.save_edit(identity, report_id, {"title": title, "html": clean, "chart_specs": charts, "format": "html"})
        result["report_run_ref"] = reference
        result["import_notice"] = "已导入正文；外部脚本与未内嵌资源不会运行，请检查图表和图片。"
        return result

    def editor_payload(self, identity: SessionIdentity, report_run_id: str) -> dict[str, Any]:
        source = self._owned_source(identity, report_run_id)
        task_id = str(source["task_id"])
        self._tasks.get(identity, task_id)
        document = self._results.get_report_document(identity, report_run_id)
        if document:
            return {"ok": True, "task_id": task_id, "source_report_run_id": report_run_id,
                    "source_artifact_name": "report.html", "title": document["title"],
                    "html": document["html"], "chart_specs": document["chart_specs"]}
        html_record, artifact_name, raw_html = self._find_source_html(identity, source)
        source_request = source.get("report_request") if isinstance(source.get("report_request"), Mapping) else {}
        chart_specs = self._source_chart_specs(source_request, raw_html)
        html = self._sanitize_with_manifest(identity, html_record, artifact_name, raw_html)
        return {
            "ok": True,
            "task_id": task_id,
            "source_report_run_id": report_run_id,
            "source_artifact_name": artifact_name,
            "title": _report_title(source_request, raw_html),
            "html": html,
            "chart_specs": chart_specs,
        }

    def save_edit(
        self,
        identity: SessionIdentity,
        source_report_run_id: str,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        required = {"html", "title", "chart_specs", "format"}
        if not required.issubset(request) or set(request).difference(required | {"edit_request_id"}):
            raise ValidationError("报告编辑请求字段必须是html、title、chart_specs和format，可附带edit_request_id")
        edit_request_id = request.get("edit_request_id")
        if edit_request_id is not None and (not isinstance(edit_request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", edit_request_id)):
            raise ValidationError("edit_request_id必须是32位小写十六进制标识")
        source = self._owned_source(identity, source_report_run_id)
        task_id = str(source["task_id"])
        self._tasks.get(identity, task_id)
        title = _validated_title(request.get("title"))
        output_format = str(request.get("format") or "").strip().lower()
        if output_format not in {"html", "pdf", "docx"}:
            raise ValidationError("报告编辑格式必须是html、pdf或docx")
        raw_html = request.get("html")
        if not isinstance(raw_html, str) or not raw_html.strip() or len(raw_html.encode("utf-8")) > 12 * 1024 * 1024:
            raise ValidationError("报告编辑HTML必须是非空且不超过12MB的文本")
        chart_specs = _validated_chart_specs(request.get("chart_specs"))
        html_record, artifact_name, _ = self._find_source_html(identity, source)
        clean_html = self._sanitize_with_manifest(identity, html_record, artifact_name, raw_html)
        clean_html = _set_document_title(clean_html, title)
        artifacts: list[dict[str, Any]] = []
        vendor = b""
        vendor_source = ""
        if chart_specs:
            vendor = load_designer_config().echarts_asset_path.read_bytes()
            if not vendor:
                raise ValidationError("离线ECharts资源不可用")
            try:
                vendor_source = vendor.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValidationError("离线ECharts资源不是UTF-8脚本") from error
        saved_html = _attach_trusted_charts(
            clean_html,
            chart_specs,
            echarts_source=vendor_source,
            chart_presentation_source=self._chart_presentation_source,
        )
        artifacts.append({"name": "report.html", "content_type": _HTML_MEDIA_TYPE, "content": saved_html.encode("utf-8")})
        if chart_specs:
            artifacts.append({"name": "assets/echarts.min.js", "content_type": "application/javascript", "content": vendor})
        if output_format == "pdf":
            try:
                rendered = render_pdf(clean_html, chart_specs=chart_specs)
            except (PdfRuntimeError, ValueError) as error:
                raise UserActionError("report_pdf_export_failed", "PDF排版未完成，报告内容已保留。", stage="export", next_step="请重试导出，或先下载HTML、Word格式。", retryable=True) from error
            artifacts.append({"name": "report.pdf", "content_type": _PDF_MEDIA_TYPE, "content": rendered})
        elif output_format == "docx":
            rendered = _render_docx_bytes(clean_html, chart_specs)
            artifacts.append({"name": "report.docx", "content_type": _DOCX_MEDIA_TYPE, "content": rendered})

        self._results.save_report_document(identity, source_report_run_id,
            {"title": title, "html": clean_html, "chart_specs": chart_specs}, artifacts)
        base = f"/api/reports/{source_report_run_id}/document-artifacts/"
        return {"ok": True, "task_id": task_id, "report_run_id": source_report_run_id,
                "html_url": base + "report.html", "download_url": base + f"report.{output_format}?download=1#display_name=" + quote(f"{title}.{output_format}", safe=""),
                "format": output_format, "title": title}

    def _owned_source(self, identity: SessionIdentity, report_run_id: str) -> dict[str, Any]:
        if not isinstance(report_run_id, str) or not _REPORT_ID.fullmatch(report_run_id):
            raise ValidationError("report_run_id无效")
        return self._results.get_owned_report_run(identity, report_run_id)

    def _find_source_html(
        self,
        identity: SessionIdentity,
        source: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str, str]:
        task_id = str(source.get("task_id") or "")
        current = dict(source)
        visited: set[str] = set()
        for _ in range(12):
            current_id = str(current.get("report_run_id") or "")
            if current_id in visited:
                raise ValidationError("源报告引用形成循环")
            visited.add(current_id)
            artifact_name = _preferred_html_artifact(current)
            if artifact_name:
                content, content_type = self._results.read_report_artifact(identity, current_id, artifact_name)
                if content_type != _HTML_MEDIA_TYPE:
                    raise ValidationError("源报告HTML媒体类型无效")
                try:
                    return current, artifact_name, content.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise ValidationError("源报告HTML不是UTF-8文本") from error
            request = current.get("report_request") if isinstance(current.get("report_request"), Mapping) else {}
            parent_id = request.get("source_report_run_id")
            if not isinstance(parent_id, str) or not _REPORT_ID.fullmatch(parent_id):
                break
            parent = self._results.get_owned_report_run(identity, parent_id)
            if parent.get("task_id") != task_id:
                raise AuthorizationError("report.read", "源报告不属于同一任务")
            current = parent
        raise ValidationError("该报告及其明确源交付均不含可编辑HTML")

    def _source_chart_specs(self, request: Mapping[str, Any], html: str) -> dict[str, dict[str, Any]]:
        if request.get("manual_edit") is True:
            return _validated_chart_specs(request.get("chart_specs", {}))
        try:
            return _validated_chart_specs(_chart_specs_from_html(html))
        except (DesignerDependencyError, ValueError) as error:
            raise ValidationError(str(error)) from error

    def _sanitize_with_manifest(
        self,
        identity: SessionIdentity,
        record: Mapping[str, Any],
        html_artifact_name: str,
        html: str,
    ) -> str:
        report_run_id = str(record.get("report_run_id") or "")
        declared = {
            str(item.get("name")): str(item.get("content_type"))
            for item in record.get("artifact_manifest", [])
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        }

        def read_declared(name: str) -> tuple[bytes, str]:
            if name not in declared:
                raise KeyError(name)
            content, content_type = self._results.read_report_artifact(identity, report_run_id, name)
            if content_type != declared[name]:
                raise ValidationError("报告资源媒体类型与manifest不一致")
            return content, content_type

        sanitizer = _ReportHtmlSanitizer(html_artifact_name, read_declared)
        sanitizer.feed(html)
        sanitizer.close()
        return sanitizer.result()


class _ReportHtmlSanitizer(HTMLParser):
    def __init__(self, document_name: str, read_declared: Callable[[str], tuple[bytes, str]]) -> None:
        super().__init__(convert_charrefs=True)
        self._document_name = document_name
        self._read_declared = read_declared
        self._parts: list[str] = []
        self._drop_stack: list[str] = []
        self._style_depth = 0

    def result(self) -> str:
        rendered = "".join(self._parts).strip()
        if "<html" not in rendered.casefold():
            rendered = f"<!doctype html><html><head></head><body>{rendered}</body></html>"
        return rendered

    def handle_decl(self, decl: str) -> None:
        if not self._drop_stack and decl.strip().casefold() == "doctype html":
            self._parts.append("<!doctype html>")

    def handle_comment(self, data: str) -> None:
        return

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if self._drop_stack:
            if tag in _DROP_CONTENT_TAGS and tag not in _VOID_TAGS:
                self._drop_stack.append(tag)
            return
        if tag in _DROP_CONTENT_TAGS:
            if tag not in _VOID_TAGS:
                self._drop_stack.append(tag)
            return
        attributes = {str(key).casefold(): value for key, value in attrs}
        if tag == "link":
            rel = str(attributes.get("rel") or "").casefold().split()
            href = attributes.get("href")
            if "stylesheet" in rel and isinstance(href, str):
                css_name = _declared_relative_name(self._document_name, href)
                if css_name:
                    try:
                        css, content_type = self._read_declared(css_name)
                    except KeyError:
                        return
                    if content_type == "text/css":
                        try:
                            decoded = css.decode("utf-8")
                        except UnicodeDecodeError:
                            return
                        self._parts.append(f'<style data-inline-source="{escape(css_name, quote=True)}">')
                        self._parts.append(_clean_css(decoded, css_name, self._read_declared))
                        self._parts.append("</style>")
            return
        if tag in _DROP_TAGS:
            return
        clean_attrs: list[tuple[str, str]] = []
        for key, raw_value in attrs:
            key = str(key).casefold()
            value = "" if raw_value is None else str(raw_value)
            if key.startswith("on") or key in _UNSAFE_ATTRIBUTES:
                continue
            if key in _SVG_PAINT_ATTRIBUTES:
                cleaned = _clean_css(value, self._document_name, self._read_declared)
                if cleaned:
                    clean_attrs.append((key, cleaned))
                continue
            if key == "style":
                cleaned = _clean_css(value, self._document_name, self._read_declared)
                if cleaned:
                    clean_attrs.append((key, cleaned))
                continue
            if key in _URL_ATTRIBUTES:
                cleaned_url = self._safe_url(tag, key, value)
                if cleaned_url:
                    clean_attrs.append((key, cleaned_url))
                continue
            if "javascript:" in value.casefold() or "vbscript:" in value.casefold():
                continue
            clean_attrs.append((key, value))
        if tag == "img" and not any(key == "src" for key, _ in clean_attrs):
            return
        rendered_attrs = "".join(
            f' {escape(key, quote=True)}="{escape(value, quote=True)}"'
            for key, value in clean_attrs
        )
        self._parts.append(f"<{tag}{rendered_attrs}>")
        if tag == "style":
            self._style_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._drop_stack:
            if tag in self._drop_stack:
                del self._drop_stack[self._drop_stack.index(tag):]
            return
        if tag in _DROP_TAGS or tag in _DROP_CONTENT_TAGS or tag in _VOID_TAGS:
            return
        if tag == "style" and self._style_depth:
            self._style_depth -= 1
        self._parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._drop_stack:
            return
        value = _clean_css(data, self._document_name, self._read_declared) if self._style_depth else data
        self._parts.append(value if self._style_depth else escape(value, quote=False))

    def _safe_url(self, tag: str, key: str, value: str) -> str:
        value = value.strip()
        if tag == "a" and key == "href":
            return value if value.startswith("#") and len(value) <= 160 else ""
        if tag in {"use", "image"} and key in {"href", "xlink:href"} and value.startswith("#"):
            return value
        if tag != "img" or key != "src":
            return ""
        data_image = _safe_data_image(value)
        if data_image:
            return data_image
        resource_name = _declared_relative_name(self._document_name, value)
        if not resource_name:
            return ""
        try:
            content, content_type = self._read_declared(resource_name)
        except KeyError:
            return ""
        return _image_data_uri(content, content_type)


def _preferred_html_artifact(record: Mapping[str, Any]) -> str:
    request = record.get("report_request") if isinstance(record.get("report_request"), Mapping) else {}
    audit = request.get("reporter_audit") if isinstance(request.get("reporter_audit"), Mapping) else {}
    delivery = audit.get("public_delivery") if isinstance(audit.get("public_delivery"), Mapping) else {}
    deliveries = delivery.get("deliveries") if isinstance(delivery.get("deliveries"), list) else []
    declared = [
        str(item.get("name")) for item in record.get("artifact_manifest", [])
        if isinstance(item, Mapping) and item.get("content_type") == _HTML_MEDIA_TYPE
    ]
    for item in deliveries:
        name = item.get("artifact_name") if isinstance(item, Mapping) else None
        if isinstance(name, str) and name in declared:
            return name
    output_type = str(request.get("output_type") or "report").casefold()
    mode = request.get("subject_ref") if isinstance(request.get("subject_ref"), Mapping) else {}
    comparison = str(mode.get("delivery_mode") or "").casefold() == "comparison"
    priorities = (
        ["multicard.html", "card.html"] if output_type == "card" and comparison else
        ["multireport.html", "report.html"] if output_type == "report" and comparison else
        [f"{output_type}.html", "report.html", "card.html", "quote.html", "index.html"]
    )
    return next((name for name in priorities if name in declared), declared[0] if declared else "")


def _report_title(request: Mapping[str, Any], html: str) -> str:
    metadata = request.get("metadata") if isinstance(request.get("metadata"), Mapping) else {}
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        return _validated_title(title)
    match = _TITLE.search(html)
    return _validated_title(re.sub(r"<[^>]+>", "", match.group(1)).strip() if match else "报告")


def _validated_title(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("报告标题必须是文本")
    title = " ".join(value.split())
    if not title or len(title) > 120 or any(ord(character) < 32 for character in title):
        raise ValidationError("报告标题必须包含1至120个可见字符")
    return title


def _validated_chart_specs(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise ValidationError("chart_specs必须是图表ID到规格的对象")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValidationError("chart_specs必须是有限JSON数据") from error
    if len(encoded.encode("utf-8")) > 2 * 1024 * 1024 or len(value) > 64:
        raise ValidationError("chart_specs超过报告编辑限制")
    result: dict[str, dict[str, Any]] = {}
    for key, raw in value.items():
        chart_id = str(key)
        if not _CHART_ID.fullmatch(chart_id) or not isinstance(raw, Mapping):
            raise ValidationError("chart_specs包含无效图表ID或规格")
        spec = deepcopy(dict(raw))
        if str(spec.get("id") or "") != chart_id:
            raise ValidationError("chart_specs键必须与规格id一致")
        try:
            validate_chart(spec, f"chart_specs.{chart_id}")
        except ValueError as error:
            raise ValidationError(str(error)) from error
        result[chart_id] = spec
    return result


def _declared_relative_name(base_name: str, raw_reference: str) -> str:
    reference = raw_reference.strip()
    try:
        parsed = urlsplit(reference)
    except ValueError:
        return ""
    if not reference or parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or reference.startswith(("/", "\\")):
        return ""
    try:
        decoded = unquote(parsed.path)
    except UnicodeDecodeError:
        return ""
    relative = PurePosixPath(decoded)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts) or "\\" in decoded:
        return ""
    base = PurePosixPath(base_name).parent
    return (base / relative).as_posix()


def _safe_data_image(value: str) -> str:
    match = re.fullmatch(r"data:(image/(?:png|jpeg|gif|webp|svg\+xml));base64,([A-Za-z0-9+/=\s]+)", value, re.IGNORECASE)
    if not match:
        return ""
    try:
        content = base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)
    except ValueError:
        return ""
    return _image_data_uri(content, match.group(1).casefold())


def _image_data_uri(content: bytes, content_type: str) -> str:
    if content_type not in _IMAGE_MEDIA_TYPES or not content or len(content) > 6 * 1024 * 1024:
        return ""
    if content_type == "image/svg+xml":
        try:
            svg = content.decode("utf-8")
        except UnicodeDecodeError:
            return ""
        sanitizer = _ReportHtmlSanitizer("inline.svg", lambda _name: (_ for _ in ()).throw(KeyError(_name)))
        sanitizer.feed(svg)
        sanitizer.close()
        cleaned = sanitizer.result()
        start, end = cleaned.find("<svg"), cleaned.rfind("</svg>")
        if start < 0 or end < start:
            return ""
        content = cleaned[start:end + len("</svg>")].encode("utf-8")
    return f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}"


def _clean_css(css: str, base_name: str, read_declared: Callable[[str], tuple[bytes, str]]) -> str:
    """Keep passive CSS and resolve URL tokens solely through the report manifest.

    Token scanning keeps strings/comments separate from function names. This
    avoids treating escaped identifiers or newer string-based resource functions
    as harmless declarations, which a literal url(...) regex cannot guarantee.
    Unsupported syntax discards this stylesheet/attribute, never report prose.
    """
    if "\\" in css or "\x00" in css:
        return ""
    css = re.sub(r"</?style\b", "", css, flags=re.IGNORECASE)
    output: list[str] = []
    index = 0

    def token_end(start: int, terminator: str) -> int:
        quote = ""
        depth = 0
        for cursor in range(start, len(css)):
            character = css[cursor]
            if quote:
                if character == quote:
                    quote = ""
            elif character in {"'", '\"'}:
                quote = character
            elif character == terminator and depth == 0:
                return cursor
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
        return len(css)

    def image_url(value: str) -> str:
        value = value.strip()
        if len(value) >= 2 and value[0] in {"'", '\"'} and value[-1] == value[0]:
            value = value[1:-1]
        if re.fullmatch(r"#[A-Za-z_][A-Za-z0-9_.:-]*", value):
            return f'url("{value}")'
        data_image = _safe_data_image(value)
        if data_image:
            return f'url("{data_image}")'
        resource_name = _declared_relative_name(base_name, value)
        if resource_name:
            try:
                content, content_type = read_declared(resource_name)
            except KeyError:
                pass
            else:
                data_image = _image_data_uri(content, content_type)
                if data_image:
                    return f'url("{data_image}")'
        return "none"

    while index < len(css):
        if css.startswith("/*", index):
            end = css.find("*/", index + 2)
            if end < 0:
                return ""
            output.append(" ")
            index = end + 2
            continue
        character = css[index]
        if character in {"'", '\"'}:
            end = css.find(character, index + 1)
            if end < 0:
                return ""
            output.append(css[index:end + 1])
            index = end + 1
            continue
        if character == "@":
            token = _CSS_IDENTIFIER.match(css, index + 1)
            if not token:
                return ""
            name = token.group().casefold()
            if name == "import":
                index = token_end(token.end(), ";") + 1
                continue
            if name not in _CSS_AT_RULES:
                return ""
            output.append(css[index:token.end()])
            index = token.end()
            continue
        token = _CSS_IDENTIFIER.match(css, index)
        if token:
            name = token.group().casefold()
            end = token.end()
            if name in {"expression", "javascript", "vbscript", "-moz-binding", "behavior"}:
                return ""
            if end < len(css) and css[end] == "(":
                if name == "url":
                    close = token_end(end + 1, ")")
                    if close == len(css):
                        return ""
                    output.append(image_url(css[end + 1:close]))
                    index = close + 1
                    continue
                if name not in _CSS_FUNCTIONS:
                    return ""
            output.append(token.group())
            index = end
            continue
        output.append(character)
        index += 1
    return "".join(output)


def _set_document_title(html: str, title: str) -> str:
    html = re.sub(r"(<h1\b[^>]*>).*?(</h1\s*>)",
                  lambda match: match[1] + escape(title) + match[2], html, count=1, flags=re.I | re.S)
    rendered = f"<title>{escape(title)}</title>"
    if _TITLE.search(html):
        return _TITLE.sub(lambda _match: rendered, html, count=1)
    head = re.search(r"<head\b[^>]*>", html, re.IGNORECASE)
    if head:
        return html[:head.end()] + rendered + html[head.end():]
    document = re.search(r"<html\b[^>]*>", html, re.IGNORECASE)
    if document:
        return html[:document.end()] + f"<head>{rendered}</head>" + html[document.end():]
    return f"<!doctype html><html><head>{rendered}</head><body>{html}</body></html>"


def _validated_chart_presentation_source(source: Any) -> str:
    if (
        not isinstance(source, str)
        or not source.strip()
        or len(source.encode("utf-8")) > 256 * 1024
        or "OptionHelperChartPresentation" not in source
        or "renderChart" not in source
    ):
        raise ValidationError("报告图表展示脚本无效")
    return source


def _safe_script_source(source: str) -> str:
    return source.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _attach_trusted_charts(
    html: str,
    chart_specs: Mapping[str, Mapping[str, Any]],
    *,
    echarts_source: str,
    chart_presentation_source: str,
) -> str:
    if not chart_specs:
        return html
    if not isinstance(echarts_source, str) or not echarts_source.strip():
        raise ValidationError("离线ECharts资源不可用")
    presentation_source = _validated_chart_presentation_source(chart_presentation_source)
    chart_json = (
        json.dumps(list(chart_specs.values()), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    echarts_base64 = base64.b64encode(echarts_source.encode("utf-8")).decode("ascii")
    script = """<script>
(()=>{
  const binary=atob("__ECHARTS_BASE64__");
  const bytes=Uint8Array.from(binary,character=>character.charCodeAt(0));
  const element=document.createElement("script");
  element.textContent=new TextDecoder().decode(bytes);
  document.head.appendChild(element);
})();
</script><script>__PRESENTATION__</script><script>
const chartSpecs=__CHARTS__;
let chartMount=null;
function markChartUnavailable(spec){
  const element=document.getElementById(spec.id);
  if(!element)return;
  element.classList.add("chart--unavailable");
  element.textContent="图表初始化失败，请使用下方完整数据表。";
}
function initialiseCharts(){
  const presentation=window.OptionHelperChartPresentation;
  if(!window.echarts||!presentation||typeof presentation.mountCharts!=="function"){
    chartSpecs.forEach(markChartUnavailable);
    return;
  }
  chartMount=presentation.mountCharts({root:document,echarts:window.echarts,specs:chartSpecs,onError:markChartUnavailable});
}
window.addEventListener("DOMContentLoaded",initialiseCharts);
let resizeTimer;
window.addEventListener("resize",()=>{
  window.clearTimeout(resizeTimer);
  resizeTimer=window.setTimeout(()=>chartMount?.resize(),150);
});
</script>"""
    script = (
        script.replace("__ECHARTS_BASE64__", echarts_base64)
        .replace("__PRESENTATION__", _safe_script_source(presentation_source))
        .replace("__CHARTS__", chart_json)
    )
    match = re.search(r"</body\s*>", html, re.IGNORECASE)
    return html[:match.start()] + script + html[match.start():] if match else html + script


def _render_docx_bytes(html: str, chart_specs: Mapping[str, Mapping[str, Any]]) -> bytes:
    try:
        from modules.designer.word_renderer import render_docx
    except ImportError as error:
        raise ValidationError("Word导出组件不可用") from error
    try:
        content = render_docx(html, chart_specs=chart_specs)
    except Exception as error:
        raise ValidationError(f"编辑报告无法导出Word：{error}") from error
    if not isinstance(content, bytes) or not content:
        raise ValidationError("Word导出组件未返回有效文档")
    return content



__all__ = ["ReportEditor"]
