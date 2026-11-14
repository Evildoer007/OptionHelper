"""Print-safe projection of the frozen Plotly scene for PDF and Word."""
from __future__ import annotations

import html
import math
from ..design_system_builder import build_design_system


def surface_svg(spec):
    from ..renderer import chart_axis_precision, display_text

    theme = build_design_system().echarts_theme
    x, y = list(spec.get('x') or []), list(spec.get('y') or [])
    finite = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    cells = {(row[0], row[1]): row[2] for row in spec.get('data', [])
             if len(row) == 3 and finite(row[2]) and isinstance(row[0], int) and isinstance(row[1], int)
             and 0 <= row[0] < len(x) and 0 <= row[1] < len(y)}
    values = list(cells.values())
    if not values:
        title = html.escape(str(spec.get('title', '3D曲面')))
        return ('<svg xmlns="http://www.w3.org/2000/svg" width="720" height="400" viewBox="0 0 720 400">'
                f'<title>{title}</title><text x="360" y="200" text-anchor="middle" font-size="14" '
                f'fill="{theme["axis"]["label"]}">当前曲面没有有效数值</text></svg>')
    lo, hi = (min(values), max(values)) if values else (0, 1)
    padding = max(abs(lo) * .02, 1e-8) if lo == hi else 0
    minimum, maximum = lo - padding, hi + padding
    # The same signed-value stops as OptionHelperPlotlyCharts.valueColorscale.
    if minimum < 0 < maximum:
        zero = -minimum / (maximum - minimum)
        stops = [(0, '#ad2940'), (zero * .56, '#d07a89'), (zero, '#f1f2f1'),
                 (zero + (1 - zero) * .5, '#7892a5'), (1, '#3f6078')]
    elif maximum <= 0:
        stops = [(0, '#ad2940'), (.52, '#d07a89'), (.82, '#f0dadd'), (1, '#f1f2f1')]
    else:
        stops = [(0, '#f1f2f1'), (.34, '#dbe4e9'), (.68, '#7892a5'), (1, '#3f6078')]

    def colour(value):
        position = (value - minimum) / (maximum - minimum)
        for (a, left), (b, right) in zip(stops, stops[1:]):
            if position <= b:
                ratio = max(0, min(1, (position - a) / max(b - a, 1e-20)))
                parts = [round(int(left[i:i+2], 16) + (int(right[i:i+2], 16) - int(left[i:i+2], 16)) * ratio) for i in (1, 3, 5)]
                return '#' + ''.join(f'{part:02x}' for part in parts)
        return stops[-1][1]

    def normalize(vector):
        length = math.sqrt(sum(value * value for value in vector))
        return tuple(value / length for value in vector)

    def cross(a, b):
        return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])

    def dot(a, b):
        return sum(v * w for v, w in zip(a, b))

    # Orthographic camera eye=(1.45,-1.55,1.08), center=(0,0,-.08), up=(0,0,1).
    forward = normalize((-1.45, 1.55, -1.16))
    right = normalize(cross(forward, (0, 0, 1)))
    up = cross(right, forward)
    aspect = (1.5, 1, .68)

    def world(a, b, c):
        return tuple((value - .5) * scale for value, scale in zip((a, b, c), aspect))

    def project(a, b, c):
        point = world(a, b, c)
        return (300 + 165 * dot(point, right), 190 - 165 * dot(point, up))

    def position(items, index):
        if items and all(finite(value) for value in items) and max(items) != min(items):
            return (items[index] - min(items)) / (max(items) - min(items))
        return index / max(1, len(items) - 1)

    def z(value):
        return (value - minimum) / (maximum - minimum)

    def number(value, value_format=None):
        scale = 100 if value_format == 'percent' else 1
        absolute = abs(value * scale)
        digits = 2 if absolute == 0 or absolute >= 1 else min(10, math.ceil(-math.log10(absolute)) + 2)
        return display_text(value, value_format, digits=max(digits, chart_axis_precision([lo, hi], value_format)))

    def label(a, b, value, anchor='middle'):
        out.append(f'<text x="{a:.2f}" y="{b:.2f}" text-anchor="{anchor}" font-size="12" fill="{theme["axis"]["label"]}">{html.escape(str(value))}</text>')

    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="720" height="400" viewBox="0 0 720 400">',
           '<title>' + html.escape(str(spec.get('title', '3D曲面'))) + '</title>']
    faces = []
    for j in range(len(y) - 1):
        for i in range(len(x) - 1):
            keys = [(i,j), (i+1,j), (i+1,j+1), (i,j+1)]
            if not all(key in cells for key in keys):
                continue
            positions = [(position(x,a), position(y,b), z(cells[a,b])) for a,b in keys]
            depth = sum(dot(world(*point), forward) for point in positions) / 4
            faces.append((depth, [project(*point) for point in positions], sum(cells[key] for key in keys) / 4))
    for _, points, value in sorted(faces, key=lambda face: -face[0]):
        out.append('<polygon points="' + ' '.join(f'{a:.3f},{b:.3f}' for a,b in points) + f'" fill="{colour(value)}"/>')
    if not faces:
        for (i,j), value in cells.items():
            a,b = project(position(x,i), position(y,j), z(value))
            out.append(f'<circle cx="{a:.3f}" cy="{b:.3f}" r="3" fill="{colour(value)}"/>')
    origin = project(0, 0, 0)
    for start, end in [(origin, project(1, 0, 0)), (origin, project(0, 1, 0)), (origin, project(0,0,1))]:
        out.append(f'<line x1="{start[0]:.2f}" y1="{start[1]:.2f}" x2="{end[0]:.2f}" y2="{end[1]:.2f}" stroke="#d7dce0"/>')
    for items, axis, unit in [(x, 'x', spec.get('x_axis_unit')), (y, 'y', spec.get('y_axis_unit'))]:
        value_format = 'percent' if unit in {'decimal', 'decimal_rate'} else 'number'
        whole_prices = value_format == 'number' and chart_axis_precision(items, axis_name=spec.get(f'{axis}_axis_name')) == 0
        for index in sorted(set((0, len(items) - 1))):
            if index < 0:
                continue
            point = project(position(x,index), 0, 0) if axis == 'x' else project(0, position(y,index), 0)
            value = items[index]
            if finite(value):
                value = display_text(value, digits=0) if whole_prices else number(value, value_format)
            if axis == 'y' and index > 0:
                label(point[0] - 12, point[1] - 7, value, 'end')
            else:
                offset = -9 if axis == 'x' else 9
                label(point[0] + offset, point[1] + 21, value, 'end' if axis == 'x' else 'start')
    label(28, 25, spec.get('title', '3D曲面'), 'start')
    label(28, 46, spec.get('z_axis_name', '数值'), 'start')
    label(300, 365, spec.get('x_axis_name', ''))
    a,b = project(0, .65, 0)
    label(a - 25, b - 5, spec.get('y_axis_name', ''), 'end')
    for (start, _), (end, _) in zip(stops, stops[1:]):
        for step in range(20):
            a = start + (end - start) * step / 20
            b = start + (end - start) * (step + 1) / 20
            value = minimum + a * (maximum - minimum)
            out.append(f'<rect x="612" y="{85+(1-b)*181.7:.2f}" width="9" height="{(b-a)*181.7+.15:.2f}" fill="{colour(value)}"/>')
    ticks = [lo] if lo == hi else [minimum, *([0] if minimum < 0 < maximum else []), maximum]
    for value in ticks:
        label(632, 89 + (1 - z(value)) * 181.7, number(value, spec.get('value_format')), 'start')
    for value in ticks:
        a,b = project(0, 0, z(value))
        label(a - 9, b + 4, number(value, spec.get('value_format')), 'end')
    if finite(spec.get('reference_x')) and x and y and all(finite(value) for value in x+y):
        i = min(range(len(x)), key=lambda index: abs(x[index] - spec['reference_x']))
        j = max(range(len(y)), key=lambda index: y[index])
        if (i,j) in cells:
            a,b = project(position(x,i), position(y,j), z(cells[i,j]))
            out.append(f'<circle cx="{a:.3f}" cy="{b:.3f}" r="4" fill="#252525" stroke="#ffffff" stroke-width="1.5"/>')
    out.append('</svg>')
    return ''.join(out)
