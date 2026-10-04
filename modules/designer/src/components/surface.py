"""Static 3D projection of frozen grids for PDF, Word and print charts."""
from __future__ import annotations
import html
import math
from ..design_system_builder import build_design_system


def surface_svg(spec):
    from ..renderer import chart_axis_precision, chart_category, display_text
    theme = build_design_system().echarts_theme
    stops = [theme['heatmap']['low'], *theme['color'][:2]]
    palette = [tuple(int(stop[i:i+2], 16) for i in (1, 3, 5)) for stop in stops]
    x, y = list(spec.get('x') or []), list(spec.get('y') or [])
    def finite(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    cells = {(row[0], row[1]): row[2] for row in spec.get('data', []) if len(row) == 3 and finite(row[2])}
    values = list(cells.values())
    lo, hi = (min(values), max(values)) if values else (0, 1)
    def position(items, i):
        if all(finite(v) for v in items) and max(items) != min(items):
            return (items[i] - min(items)) / (max(items) - min(items))
        return i / max(1, len(items)-1)
    def project(a, b, c):
        return (300 + 230*a - 150*b, 270 - 50*a - 80*b - 130*c)
    def z(value):
        return (value-lo)/(hi-lo) if hi != lo else .5
    def colour(value):
        t = z(value)
        a,b = palette[:2] if t <= .5 else palette[1:]
        u = 2*t if t <= .5 else 2*t-1
        return '#'+''.join(f'{round(v+(w-v)*u):02x}' for v,w in zip(a,b))
    out=['<svg xmlns="http://www.w3.org/2000/svg" width="720" height="350" viewBox="0 0 720 350">', '<title>'+html.escape(str(spec.get('title','3D曲面')))+'</title>']
    faces=[]
    for j in range(len(y)-1):
        for i in range(len(x)-1):
            keys=[(i,j),(i+1,j),(i+1,j+1),(i,j+1)]
            if not all(key in cells for key in keys): continue
            points=[project(position(x,a),position(y,b),z(cells[a,b])) for a,b in keys]
            faces.append((i+j, points, sum(cells[key] for key in keys)/4))
    for _,points,value in sorted(faces, key=lambda face: -face[0]):
        out.append('<polygon points="'+' '.join(f'{a:.3f},{b:.3f}' for a,b in points)+f'" fill="{colour(value)}" stroke="{theme["axis"]["split"]}" stroke-width="0.4"/>')
    if not faces:
        for (i,j),v in cells.items():
            if 0<=i<len(x) and 0<=j<len(y):
                a,b=project(position(x,i),position(y,j),z(v))
                out.append(f'<circle cx="{a:.3f}" cy="{b:.3f}" r="3" fill="{colour(v)}"/>')
    def label(a,b,value,anchor='middle'):
        out.append(f'<text x="{a}" y="{b}" text-anchor="{anchor}" font-size="12" fill="{theme["axis"]["label"]}">{html.escape(str(value))}</text>')
    # Put the value axis on the rear left edge, outside the data surface.
    origin = project(0, 0, 0)
    value_origin = project(0, 1, 0)
    axes = [(origin, project(1, 0, 0)), (origin, value_origin),
            (value_origin, project(0, 1, 1))]
    for start, end in axes:
        out.append(f'<line x1="{start[0]}" y1="{start[1]}" x2="{end[0]}" y2="{end[1]}" stroke="{theme["axis"]["line"]}"/>')
    label(28, 25, spec.get('title', '3D曲面'), 'start')
    label(28, 46, spec.get('z_axis_name', '数值'), 'start')
    label(430, 315, spec.get('x_axis_name', '横轴'))
    label(170, 235, spec.get('y_axis_name', '纵轴'))
    if x:
        digits = chart_axis_precision(x)
        label(330, 290, chart_category(x[0], spec.get('x_axis_name'), digits=digits))
        label(548, 230, chart_category(x[-1], spec.get('x_axis_name'), digits=digits), 'start')
    if y:
        digits = chart_axis_precision(y)
        label(275, 310, chart_category(y[0], spec.get('y_axis_name'), digits=digits), 'end')
        label(137, 202, chart_category(y[-1], spec.get('y_axis_name'), digits=digits), 'end')
    def number(value):
        return display_text(value, spec.get('value_format'), digits=chart_axis_precision([lo, hi], spec.get('value_format')))
    label(137, 182, number(lo), 'end')
    label(137, 69, number(hi), 'end')
    out.append('</svg>')
    return ''.join(out)
