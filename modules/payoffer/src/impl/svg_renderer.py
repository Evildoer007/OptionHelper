"""Python Payoffer的确定性SVG渲染器。

渲染器只消费共享解释器已经核对的路径点。每张子图只显示路径标识；曲线与
阈值说明统一集中在整图底部，避免把图例和数字堆叠在收益曲线附近。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape
from typing import Any, Mapping, Sequence


CANVAS_WIDTH = 1200
OUTER_MARGIN = 16
CARD_WIDTH = 578
CARD_GAP = 12
CARD_BASE_HEIGHT = 400
PLOT_LEFT = 38
PLOT_RIGHT = 540
_TOKEN_PATTERN = re.compile(r"^([A-Za-z])_(?:\{(.+)\}|(.+))$")

_THRESHOLD_META: dict[str, tuple[str, str, str]] = {
    "K": ("执行价", "#1F5FA8", "9 4"), "K_1": ("执行价1", "#1F5FA8", "9 4"),
    "K_2": ("执行价2", "#1F5FA8", "7 4"), "K_3": ("执行价3", "#1F5FA8", "5 4"),
    "K_4": ("执行价4", "#1F5FA8", "3 4"), "K_p": ("看跌执行价", "#1F5FA8", "9 4"),
    "K_c": ("看涨执行价", "#1F5FA8", "7 4"), "K_u": ("上执行价", "#1F5FA8", "5 4"),
    "K_d": ("下执行价", "#1F5FA8", "3 4"), "K_buf": ("缓冲价", "#1F5FA8", "4 3"),
    "H": ("触发价", "#D89B27", "8 5"), "H_out": ("敲出价", "#D89B27", "12 5"),
    "H_out_1": ("常规敲出价", "#D89B27", "12 5"), "H_out_2": ("最后敲出价", "#D89B27", "8 3"),
    "H_in": ("敲入价", "#D89B27", "12 5"), "H_c": ("计息价", "#D89B27", "6 4"),
    "H_touch": ("触碰障碍", "#D89B27", "8 4"), "H_buffer": ("缓冲价", "#D89B27", "8 3"),
    "H_floor": ("保底价", "#D89B27", "7 3"), "H_reset": ("重设价", "#D89B27", "4 3"),
    "H_u": ("上障碍价", "#D89B27", "10 4"), "H_d": ("下障碍价", "#D89B27", "6 3"),
    "B": ("气囊敲入价", "#D89B27", "8 3"), "F": ("保底比例", "#D89B27", "7 3"),
    "ell": ("限损比例", "#D89B27", "7 3"), "r_cap": ("收益上限", "#1F5FA8", "6 3"),
}


@dataclass(frozen=True)
class _Annotation:
    x: float
    row: int
    text: str
    color: str
    width: float


@dataclass(frozen=True)
class _CardLayout:
    x: float
    y: float
    width: float
    height: float
    annotations: tuple[_Annotation, ...]


def _number(value: float) -> str:
    return f"{float(value):.4f}".rstrip("0").rstrip(".")


def _coord_x(value: float, scale: Mapping[str, float], left: float, width: float) -> float:
    return left + (value - float(scale["x_min"])) / (float(scale["x_max"]) - float(scale["x_min"])) * width


def _coord_y(value: float, scale: Mapping[str, float], top: float, height: float) -> float:
    return top + (float(scale["y_max"]) - value) / (float(scale["y_max"]) - float(scale["y_min"])) * height


def _token_key(token: str) -> str:
    return str(token).replace("₀", "_0").replace("₁", "_1").replace("₂", "_2").replace("₃", "_3").replace("₄", "_4")


def _svg_token(token: str) -> str:
    source = str(token).strip()
    match = _TOKEN_PATTERN.match(source)
    if not match:
        return escape(source)
    suffix = match.group(2) or match.group(3)
    return f'{escape(match.group(1))}<tspan class="token-sub" baseline-shift="sub">{escape(suffix)}</tspan>'


def _threshold_meta(threshold: Mapping[str, Any] | str) -> tuple[str, str, str]:
    """Use the TermCatalog label when a controlled semantic threshold has one."""
    if isinstance(threshold, Mapping):
        token = str(threshold.get("token", ""))
        catalog_label = str(threshold.get("label", "")).strip()
    else:
        token = str(threshold)
        catalog_label = ""
    default_label, color, dash = _THRESHOLD_META.get(_token_key(token), ("关键阈值", "#6F7780", "4 4"))
    return catalog_label or default_label, color, dash


def _format_value(value: float, axis: Mapping[str, Any]) -> str:
    kind = str(axis.get("number_format", "level"))
    if kind == "rate":
        return _number(value * 100.0)
    if kind == "count":
        return _number(round(value))
    return _number(value)


def _axis_spec(path: Mapping[str, Any]) -> tuple[str, str, Mapping[str, Any]]:
    axis = path.get("axis", {})
    return str(axis.get("label") or axis.get("unit") or "横轴"), str(axis.get("display_unit", "")), axis


def _line_path(segments: Sequence[Sequence[Mapping[str, Any]]], scale: Mapping[str, float], left: float, top: float, width: float, height: float) -> str:
    commands: list[str] = []
    for segment in segments:
        if not segment:
            continue
        first = segment[0]
        commands.append(f"M{_number(_coord_x(float(first['x']), scale, left, width))} {_number(_coord_y(float(first['y']), scale, top, height))}")
        for point in segment[1:]:
            x = _coord_x(float(point["x"]), scale, left, width)
            y = _coord_y(float(point["y"]), scale, top, height)
            if point.get("curve") == "step":
                commands.append(f"H{_number(x)} V{_number(y)}")
            else:
                commands.append(f"L{_number(x)} {_number(y)}")
    return " ".join(commands)


def _endpoint_markers(segments: Sequence[Sequence[Mapping[str, Any]]], scale: Mapping[str, float], left: float, top: float, width: float, height: float) -> str:
    """仅在真正不连续或孤立相等点保留空实端点。"""
    markers: dict[tuple[str, str], str] = {}
    for segment in segments:
        if not segment:
            continue
        for point in (segment[0], segment[-1]):
            endpoint = point.get("endpoint")
            if endpoint not in {"open", "closed"}:
                continue
            x = _number(_coord_x(float(point["x"]), scale, left, width))
            y = _number(_coord_y(float(point["y"]), scale, top, height))
            if endpoint == "closed" or (x, y) not in markers:
                markers[(x, y)] = str(endpoint)
    return "".join(f'<circle class="curve-endpoint {endpoint}" cx="{x}" cy="{y}" r="4.4"/>' for (x, y), endpoint in markers.items())


def _jump_svg(jumps: Sequence[Mapping[str, Any]], scale: Mapping[str, float], left: float, top: float, width: float, height: float) -> str:
    parts: list[str] = []
    for jump in jumps:
        x = _coord_x(float(jump["x"]), scale, left, width)
        y1 = _coord_y(float(jump["y_start"]), scale, top, height)
        y2 = _coord_y(float(jump["y_end"]), scale, top, height)
        if abs(y1 - y2) > 0.5:
            parts.append(f'<line class="payoff-jump" x1="{_number(x)}" y1="{_number(y1)}" x2="{_number(x)}" y2="{_number(y2)}"/>')
    return "".join(parts)


def _format_pnl(value: float) -> str:
    sign = "+" if value > 0.0 else "-"
    amount = abs(float(value))
    return f"{sign}{_number(amount)}%"


def _payoff_level_svg(path: Mapping[str, Any], scale: Mapping[str, float], *, left: float, right: float, top: float, bottom: float, axis_x: float) -> str:
    """水平收益段的纵轴辅助线与金额标注，置于收益线之下。"""
    parts: list[str] = []
    for level in path.get("payoff_levels", []):
        value = float(level["value"])
        y = _coord_y(value, scale, top, bottom - top)
        if y <= top + 9.0 or y >= bottom - 7.0:
            continue
        label_x = max(left + 34.0, axis_x - 10.0)
        parts.append(
            f'<line class="payoff-guide" x1="{_number(left)}" y1="{_number(y)}" x2="{_number(right)}" y2="{_number(y)}"/>'
            f'<text class="cn payoff-level-label" x="{_number(label_x)}" y="{_number(y - 7.0)}" text-anchor="end">{_format_pnl(value)}</text>'
        )
    return "".join(parts)


def _turn_svg(turning_points: Sequence[Mapping[str, Any]], scale: Mapping[str, float], left: float, top: float, width: float, height: float) -> str:
    parts: list[str] = []
    for point in turning_points:
        x = _coord_x(float(point["x"]), scale, left, width)
        y = _coord_y(float(point["y"]), scale, top, height)
        parts.append(f'<circle class="curve-turn" cx="{_number(x)}" cy="{_number(y)}" r="3.6"/>')
    return "".join(parts)


def _visible_thresholds(path: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    scale = path["scale"]
    reference = _reference_value(path)
    return [
        item
        for item in path.get("thresholds", [])
        if abs(float(item["value"])) > 1e-12
        # A TermCatalog-bound price term remains a visible semantic threshold
        # at the reference level.  Only an unlabeled domain boundary at that
        # same coordinate is redundant with the axis reference line.
        and (
            str(item.get("semantic_role", "")) == "contract_price_term"
            or abs(float(item["value"]) - reference) > 1e-9
        )
        and float(scale["x_min"]) <= float(item["value"]) <= float(scale["x_max"])
    ]


def _annotation_layout(path: Mapping[str, Any], plot_left: float, plot_right: float) -> tuple[_Annotation, ...]:
    _, unit, axis = _axis_spec(path)
    scale = path["scale"]
    candidates: list[tuple[float, str, str, float]] = []
    for item in _visible_thresholds(path):
        x = _coord_x(float(item["value"]), scale, plot_left, plot_right - plot_left)
        label, color, _ = _threshold_meta(item)
        text = f"{label} {_token_key(str(item['token']))}={_format_value(float(item['value']), axis)}{unit}"
        candidates.append((x, text, color, max(30.0, 12.0 + len(text) * 7.2)))
    candidates.sort(key=lambda item: item[0])
    row_ends: list[float] = []
    annotations: list[_Annotation] = []
    for x, text, color, width in candidates:
        left = max(plot_left, min(plot_right - width, x - width / 2.0))
        row = next((index for index, right_edge in enumerate(row_ends) if left >= right_edge + 7.0), len(row_ends))
        if row == len(row_ends):
            row_ends.append(left + width)
        else:
            row_ends[row] = left + width
        annotations.append(_Annotation(x=left + width / 2.0, row=row, text=text, color=color, width=width))
    return tuple(annotations)


def _threshold_svg(path: Mapping[str, Any], annotations: Sequence[_Annotation], *, left: float, right: float, top: float, bottom: float, band_y: float) -> str:
    scale = path["scale"]
    parts: list[str] = []
    for item in _visible_thresholds(path):
        value = float(item["value"])
        x = _coord_x(value, scale, left, right - left)
        _, color, dash = _threshold_meta(item)
        parts.append(f'<line class="threshold" style="stroke:{color};stroke-dasharray:{dash}" x1="{_number(x)}" y1="{_number(top)}" x2="{_number(x)}" y2="{_number(bottom)}"/>')
    for annotation in annotations:
        y = band_y + annotation.row * 20.0
        parts.append(f'<text class="cn threshold-value" x="{_number(annotation.x)}" y="{_number(y)}" text-anchor="middle" style="fill:{annotation.color}">{escape(annotation.text)}</text>')
    return "".join(parts)


def _reference_value(path: Mapping[str, Any]) -> float:
    return float(path.get("axis", {}).get("reference_value", 0.0))


def _layout_cards(paths: Sequence[Mapping[str, Any]], columns: int) -> tuple[list[_CardLayout], float]:
    sketches: list[tuple[float, float, tuple[_Annotation, ...], float]] = []
    for index, path in enumerate(paths):
        card_width = CANVAS_WIDTH - 2 * OUTER_MARGIN if columns == 1 else CARD_WIDTH
        card_x = OUTER_MARGIN + (index % columns) * (card_width + CARD_GAP)
        plot_left, plot_right = card_x + PLOT_LEFT, card_x + card_width - (CARD_WIDTH - PLOT_RIGHT)
        annotations = _annotation_layout(path, plot_left, plot_right)
        rows = 1 + max((annotation.row for annotation in annotations), default=-1)
        card_height = CARD_BASE_HEIGHT + max(0, rows - 2) * 22.0
        sketches.append((card_x, card_width, annotations, card_height))
    layouts: list[_CardLayout] = []
    y = 92.0
    for start in range(0, len(sketches), columns):
        row = sketches[start:start + columns]
        row_height = max(item[3] for item in row)
        for offset, (x, width, annotations, height) in enumerate(row):
            layouts.append(_CardLayout(x=x, y=y, width=width, height=height, annotations=annotations))
        y += row_height + 12.0
    return layouts, y


def _card(path: Mapping[str, Any], index: int, layout: _CardLayout) -> str:
    card_x, card_y, card_width, card_height = layout.x, layout.y, layout.width, layout.height
    annotation_rows = 1 + max((annotation.row for annotation in layout.annotations), default=-1)
    left, right = card_x + PLOT_LEFT, card_x + card_width - (CARD_WIDTH - PLOT_RIGHT)
    top = card_y + 76 + annotation_rows * 20.0
    bottom = card_y + card_height - 46
    width, height = right - left, bottom - top
    scale = path["scale"]
    reference = _reference_value(path)
    y_axis_x = _coord_x(reference, scale, left, width)
    x_axis_y = _coord_y(0.0, scale, top, height)
    axis_label, axis_unit, axis = _axis_spec(path)
    curve = _line_path(path.get("segments", []), scale, left, top, width, height)
    endpoints = _endpoint_markers(path.get("segments", []), scale, left, top, width, height)
    jumps = _jump_svg(path.get("jumps", []), scale, left, top, width, height)
    turns = _turn_svg(path.get("turning_points", []), scale, left, top, width, height)
    payoff_levels = _payoff_level_svg(path, scale, left=left, right=right, top=top, bottom=bottom, axis_x=y_axis_x)
    thresholds = _threshold_svg(path, layout.annotations, left=left, right=right, top=top, bottom=bottom, band_y=card_y + 70)
    x_axis_label_y = min(card_y + card_height - 22, x_axis_y + 24)
    reference_anchor = "end" if y_axis_x >= right - 74 else "start"
    reference_x = y_axis_x - 10 if reference_anchor == "end" else y_axis_x + 10
    title = escape(str(path.get("title", f"路径{index + 1}")))
    state_note = escape(str(path.get("status_note") or ""))
    payoff_label = "收益率（扣费前）" if path.get("payoff_basis") == "gross_before_premium" else "净收益率"
    return f'''<g data-path-card="{index}">
  <defs><clipPath id="payoffer-card-clip-{index}"><rect x="{_number(card_x + 4)}" y="{_number(card_y + 4)}" width="{_number(card_width - 8)}" height="{_number(card_height - 8)}"/></clipPath></defs>
  <rect class="card" x="{_number(card_x)}" y="{_number(card_y)}" width="{_number(card_width)}" height="{_number(card_height)}"/><line class="card-accent" x1="{_number(card_x)}" y1="{_number(card_y + 2)}" x2="{_number(card_x + card_width)}" y2="{_number(card_y + 2)}"/>
  <g clip-path="url(#payoffer-card-clip-{index})">
  <text class="cn scenario-no" x="{_number(card_x + 24)}" y="{_number(card_y + 39)}">{title}</text><text class="cn scenario-note" x="{_number(card_x + 24)}" y="{_number(card_y + 58)}">{state_note}</text>
  {payoff_levels}
  <line class="axis" x1="{_number(left)}" y1="{_number(x_axis_y)}" x2="{_number(right)}" y2="{_number(x_axis_y)}"/><line class="axis" x1="{_number(y_axis_x)}" y1="{_number(top)}" x2="{_number(y_axis_x)}" y2="{_number(bottom)}"/>
  <path class="axis-arrow" d="M{_number(y_axis_x)} {_number(top - 7)} L{_number(y_axis_x - 4.5)} {_number(top + 2)} L{_number(y_axis_x + 4.5)} {_number(top + 2)} Z"/><path class="axis-arrow" d="M{_number(right + 7)} {_number(x_axis_y)} L{_number(right - 2)} {_number(x_axis_y - 4.5)} L{_number(right - 2)} {_number(x_axis_y + 4.5)} Z"/>
  {thresholds}{jumps}<path class="payoff-line" d="{curve}"/>{turns}{endpoints}
  <text class="cn axis-title" x="{_number(y_axis_x)}" y="{_number(top - 12)}" text-anchor="middle">{payoff_label}</text><text class="cn reference-label" x="{_number(reference_x)}" y="{_number(x_axis_label_y)}" text-anchor="{reference_anchor}">{_format_value(reference, axis)}{escape(axis_unit)}</text><text class="cn axis-title" x="{_number(right)}" y="{_number(x_axis_label_y)}" text-anchor="end">{escape(axis_label)}</text>
  </g>
</g>'''


def _legend_items(paths: Sequence[Mapping[str, Any]]) -> list[dict[str, str | float]]:
    items: list[dict[str, str | float]] = [{"kind": "payoff", "width": 154.0}]
    seen: set[tuple[str, float, str]] = set()
    for path in paths:
        _, unit, axis = _axis_spec(path)
        for threshold in _visible_thresholds(path):
            token, value = str(threshold["token"]), float(threshold["value"])
            key = (token, value, str(axis.get("unit", "")))
            if key in seen:
                continue
            seen.add(key)
            label, color, dash = _threshold_meta(threshold)
            full_label = f"{label} {token} = {_format_value(value, axis)}{unit}"
            items.append({"kind": "threshold", "token": token, "value": value, "label": label, "color": color, "dash": dash, "unit": unit, "value_text": _format_value(value, axis), "width": max(184.0, 98.0 + len(full_label) * 8.0)})
    return items


def _legend_rows(items: Sequence[Mapping[str, str | float]]) -> list[list[dict[str, str | float]]]:
    rows: list[list[dict[str, str | float]]] = [[]]
    used = 0.0
    for source in items:
        item = dict(source)
        width = float(item["width"])
        if used and used + width > CANVAS_WIDTH - 2 * OUTER_MARGIN - 10:
            rows.append([])
            used = 0.0
        item["x"] = OUTER_MARGIN + used
        rows[-1].append(item)
        used += width
    return rows


def _legend_svg(rows: Sequence[Sequence[Mapping[str, str | float]]], height: float) -> str:
    parts: list[str] = []
    for row_index, row in enumerate(rows):
        y = height - 20 - (len(rows) - row_index - 1) * 28
        for item in row:
            x = float(item["x"])
            if item["kind"] == "payoff":
                parts.append(f'<line x1="{_number(x)}" y1="{_number(y)}" x2="{_number(x + 42)}" y2="{_number(y)}" stroke="#C8102E" stroke-width="2.6"/><text class="global-legend" x="{_number(x + 55)}" y="{_number(y + 5)}">收益函数</text>')
                continue
            parts.append(f'<line x1="{_number(x)}" y1="{_number(y)}" x2="{_number(x + 42)}" y2="{_number(y)}" stroke="{item["color"]}" stroke-width="1" stroke-dasharray="{item["dash"]}"/><text class="global-legend" x="{_number(x + 55)}" y="{_number(y + 5)}">{escape(str(item["label"]))} <tspan class="legend-token">{_svg_token(str(item["token"]))}</tspan> = {escape(str(item["value_text"]))}{escape(str(item["unit"]))}</text>')
    return "".join(parts)


def render_svg(payload: Mapping[str, Any]) -> str:
    """根据Python运行时路径渲染，不读取默认SVG或历史Editor文件。"""
    paths = list(payload.get("paths", []))
    columns = 1 if len(paths) == 1 else 2
    layouts, cards_bottom = _layout_cards(paths, columns)
    legend_rows = _legend_rows(_legend_items(paths))
    height = max(566.0, cards_bottom + 62.0 + (len(legend_rows) - 1) * 28.0)
    cards = "".join(_card(path, index, layouts[index]) for index, path in enumerate(paths))
    legend = _legend_svg(legend_rows, height)
    name = escape(str(payload.get("name_zh", "")))
    footer_y = height - 50 - (len(legend_rows) - 1) * 28
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS_WIDTH}" height="{_number(height)}" viewBox="0 0 {CANVAS_WIDTH} {_number(height)}" role="img" aria-labelledby="title desc">
  <title id="title">{name}路径Payoff图</title><desc id="desc">由Python Payoffer根据本次ResolvedContract计算并由共享现金流解释器逐点核对的参数化路径Payoff图。</desc>
  <defs><linearGradient id="everbright-red-gold" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#C8102E"/><stop offset="1" stop-color="#D89B27"/></linearGradient><style>
  .cn{{font-family:"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",Arial,sans-serif}}.card{{fill:#fff;stroke:#CFCFCF;stroke-width:1.1}}.card-accent{{stroke:#C8102E;stroke-width:4.5}}.scenario-no{{font-size:17px;font-weight:700;fill:#252525}}.scenario-note{{font-size:10.5px;fill:#6F7780}}.axis-title{{font-size:12px;font-weight:600;fill:#252525}}.reference-label,.range-label{{font-size:12px;fill:#4F4F4F}}.axis{{stroke:#252525;stroke-width:1.3}}.axis-arrow{{fill:#252525}}.threshold{{stroke-width:1;opacity:.82}}.threshold-value{{font-size:11px;font-weight:700}}.payoff-guide{{stroke:#B9BEC7;stroke-width:1;stroke-dasharray:3 4}}.payoff-level-label{{font-size:11px;font-weight:600;fill:#6F7780}}.payoff-line{{fill:none;stroke:#C8102E;stroke-width:2.6;stroke-linecap:round;stroke-linejoin:round}}.payoff-jump{{stroke:#C8102E;stroke-width:1.4;stroke-dasharray:4 3;fill:none}}.curve-turn{{fill:#6B3FA0;stroke:#fff;stroke-width:1.2}}.curve-endpoint{{stroke:#C8102E;stroke-width:1.7}}.curve-endpoint.open{{fill:#fff}}.curve-endpoint.closed{{fill:#C8102E}}.global-legend{{font-size:12px;fill:#555}}.global-legend .token-sub{{font-size:9px}}
  </style></defs>
  <rect width="{CANVAS_WIDTH}" height="{_number(height)}" fill="#fff"/><rect x="{OUTER_MARGIN}" y="16" width="{CANVAS_WIDTH - 2 * OUTER_MARGIN}" height="56" fill="url(#everbright-red-gold)"/><rect x="{OUTER_MARGIN}" y="16" width="8" height="56" fill="#A80F28"/><text class="cn" x="38" y="52" fill="#fff" font-size="26" font-weight="700">{name}</text>
  {cards}
  <line x1="{OUTER_MARGIN}" y1="{_number(footer_y)}" x2="{CANVAS_WIDTH - OUTER_MARGIN}" y2="{_number(footer_y)}" stroke="#C8102E" stroke-width="1.4"/><g class="cn" data-global-legend="true">{legend}</g>
</svg>'''
