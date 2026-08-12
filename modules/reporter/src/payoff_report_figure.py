"""Report-only Payoffer SVG derivation.

Default Payoffer figures are immutable product examples.  A ReportRun may carry
one verified copy of that source, but its report layout uses a separately
derived SVG without the product-name banner.  This module deliberately does
not mutate, rewrite in place, or relax the source ArtifactRef hash check.
"""

from __future__ import annotations

from hashlib import sha256
import re
from typing import Iterable
from xml.etree import ElementTree as ET

from .models import ReporterError


PROFILE = "payoffer-report-figure/v1"
_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_NS = "{" + _SVG_NS + "}"
_FORBIDDEN_ELEMENTS = {
    "script", "foreignObject", "iframe", "object", "embed", "image", "audio", "video",
    "a", "animate", "animateMotion", "animateTransform", "set",
}
_UNSAFE_SCHEME = re.compile(r"(?i)(?:javascript|data|https?|file):|//|\\\\|(?:^|[(/])\.\.(?:/|$)")
_SAFE_FRAGMENT_URL = re.compile(r"^url\(\s*#[A-Za-z_][A-Za-z0-9_.:-]*\s*\)$")
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _number(value: str | None, *, default: float | None = None) -> float | None:
    if value is None:
        return default
    match = _NUMBER.fullmatch(value.strip())
    return float(match.group(0)) if match else default


def _reject_unsafe_svg(root: ET.Element) -> None:
    """Accept only static, self-contained SVG constructs needed by Payoffer."""

    for node in root.iter():
        name = _local_name(node.tag)
        if name in _FORBIDDEN_ELEMENTS:
            raise ReporterError(f"Payoffer SVG包含不允许的{name}元素")
        for raw_name, raw_value in node.attrib.items():
            attr = _local_name(raw_name).lower()
            value = str(raw_value).strip()
            if attr.startswith("on"):
                raise ReporterError("Payoffer SVG不允许事件处理属性")
            if attr == "href" or raw_name == f"{{{_XLINK_NS}}}href":
                if not value.startswith("#"):
                    raise ReporterError("Payoffer SVG不允许外链或路径引用")
                continue
            if _UNSAFE_SCHEME.search(value):
                raise ReporterError("Payoffer SVG不允许外链或路径引用")
            if "url(" in value.lower() and not _SAFE_FRAGMENT_URL.fullmatch(value):
                raise ReporterError("Payoffer SVG只允许本地defs片段引用")
        if name == "style":
            style = node.text or ""
            if re.search(r"(?i)@import|expression\s*\(|(?:javascript|data|https?|file):|//", style):
                raise ReporterError("Payoffer SVG样式不允许外部资源或表达式")
            for value in re.findall(r"(?i)url\(([^)]*)\)", style):
                if not re.fullmatch(r"\s*#[A-Za-z_][A-Za-z0-9_.:-]*\s*", value):
                    raise ReporterError("Payoffer SVG样式只允许本地defs片段引用")


def _header_nodes(children: Iterable[ET.Element]) -> tuple[set[ET.Element], float | None]:
    """Locate the standard Payoffer title banner without touching path panels."""

    remove: set[ET.Element] = set()
    header_bottom: float | None = None
    for node in children:
        name = _local_name(node.tag)
        if name in {"title", "desc"}:
            remove.add(node)
            continue
        y = _number(node.get("y"))
        height = _number(node.get("height"))
        if name == "rect" and y is not None and height is not None and y >= 0 and y + height <= 80:
            # Do not remove the full-SVG background rectangle, only the banner
            # rectangles that start below the canvas origin.
            if y > 0:
                remove.add(node)
                header_bottom = max(header_bottom or 0.0, y + height)
            continue
        if name == "text" and y is not None and 0 < y <= 80:
            # Payoffer's visual product name is a root text node in its top
            # banner.  Scenario/axis labels start below this band.
            remove.add(node)
            header_bottom = max(header_bottom or 0.0, y)
    return remove, header_bottom


def _crop_top_banner(root: ET.Element, header_bottom: float | None) -> None:
    if header_bottom is None:
        return
    view_box = root.get("viewBox")
    if not view_box:
        return
    values = [_number(value) for value in view_box.replace(",", " ").split()]
    if len(values) != 4 or any(value is None for value in values):
        return
    min_x, min_y, width, height = (float(value) for value in values)
    crop_y = min_y + max(0.0, header_bottom + 4.0 - min_y)
    if crop_y >= min_y + height:
        return
    root.set("viewBox", f"{min_x:g} {crop_y:g} {width:g} {min_y + height - crop_y:g}")
    if _number(root.get("height")) is not None:
        root.set("height", f"{min_y + height - crop_y:g}")


def derive_report_payoff_svg(source: bytes, *, expected_source_hash: str) -> bytes:
    """Return a static report figure cryptographically bound to its source.

    ``expected_source_hash`` must originate from the already verified ModuleRun
    manifest.  The source bytes themselves remain untouched by this function.
    """

    actual_hash = sha256(source).hexdigest()
    if actual_hash != expected_source_hash:
        raise ReporterError("Payoffer SVG源文件哈希与ArtifactRef不一致")
    if b"<!DOCTYPE" in source.upper() or b"<!ENTITY" in source.upper():
        raise ReporterError("Payoffer SVG不允许DOCTYPE或实体声明")
    try:
        root = ET.fromstring(source)
    except (ET.ParseError, UnicodeDecodeError) as error:
        raise ReporterError(f"Payoffer SVG不是有效XML：{error}") from error
    if root.tag != f"{_NS}svg":
        raise ReporterError("Payoffer SVG根元素必须为svg")
    _reject_unsafe_svg(root)
    children = list(root)
    remove, header_bottom = _header_nodes(children)
    for node in remove:
        root.remove(node)
    _crop_top_banner(root, header_bottom)
    root.attrib.pop("aria-labelledby", None)
    root.set("role", "img")
    root.set("aria-label", "本次参数化收益图")
    root.set("data-report-figure-profile", PROFILE)
    root.set("data-source-sha256", actual_hash)
    ET.register_namespace("", _SVG_NS)
    return ET.tostring(root, encoding="utf-8", xml_declaration=False)


def validate_report_payoff_svg(payload: bytes, *, expected_source_hash: str) -> None:
    """Validate a persisted report-derived figure without trusting its name."""

    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ReporterError("报告Payoffer SVG不允许DOCTYPE或实体声明")
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, UnicodeDecodeError) as error:
        raise ReporterError(f"报告Payoffer SVG不是有效XML：{error}") from error
    if root.tag != f"{_NS}svg":
        raise ReporterError("报告Payoffer SVG根元素必须为svg")
    _reject_unsafe_svg(root)
    if root.get("data-report-figure-profile") != PROFILE:
        raise ReporterError("报告Payoffer SVG缺少派生配置标识")
    if root.get("data-source-sha256") != expected_source_hash:
        raise ReporterError("报告Payoffer SVG未绑定源ArtifactRef哈希")
    for node in root:
        if _local_name(node.tag) in {"title", "desc"}:
            raise ReporterError("报告Payoffer SVG不得保留图内标题")


__all__ = ["PROFILE", "derive_report_payoff_svg", "validate_report_payoff_svg"]
