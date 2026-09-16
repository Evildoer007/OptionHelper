"""Static 3D projection of frozen grids for PDF, Word and print charts."""
from __future__ import annotations
import html
import math


def surface_svg(spec):
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
        a,b = ((52,120,184),(243,238,238)) if t <= .5 else ((243,238,238),(200,16,46))
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
        out.append('<polygon points="'+' '.join(f'{a:.3f},{b:.3f}' for a,b in points)+f'" fill="{colour(value)}" stroke="#aaa" stroke-width="0.4"/>')
    if not faces:
        for (i,j),v in cells.items():
            if 0<=i<len(x) and 0<=j<len(y):
                a,b=project(position(x,i),position(y,j),z(v))
                out.append(f'<circle cx="{a:.3f}" cy="{b:.3f}" r="3" fill="{colour(v)}"/>')
    def label(a,b,value,anchor='middle'):
        out.append(f'<text x="{a}" y="{b}" text-anchor="{anchor}" font-size="12" fill="#555">{html.escape(str(value))}</text>')
    origin=project(0,0,0)
    for target in [project(1,0,0),project(0,1,0),project(0,0,1)]:
        out.append(f'<line x1="{origin[0]}" y1="{origin[1]}" x2="{target[0]}" y2="{target[1]}" stroke="#888"/>')
    label(460,267,spec.get('x_axis_name','横轴'));label(175,250,spec.get('y_axis_name','纵轴'));label(280,110,spec.get('z_axis_name','数值'))
    if x: label(310,287,x[0]);label(538,230,x[-1])
    if y: label(280,285,y[0]);label(140,187,y[-1])
    def number(v):
        return f'{v*100:.4g}%' if spec.get('value_format')=='percent' else f'{v:.4g}'
    label(282,265,number(lo),'end');label(285,145,number(hi),'end')
    out.append('</svg>')
    return ''.join(out)
