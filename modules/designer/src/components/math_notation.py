"""Render a bounded mathematical notation vocabulary without evaluating values."""
from __future__ import annotations
import html
import re
from xml.etree import ElementTree as ET

_SYMBOLS = dict(Delta='Δ', Gamma='Γ', Theta='Θ', Pi='Π', Sigma='Σ', Omega='Ω',
    delta='δ', gamma='γ', theta='θ', rho='ρ', sigma='σ', mu='μ', pi='π',
    alpha='α', beta='β', lambda_='λ', tau='τ', epsilon='ε', le='≤', leq='≤',
    ge='≥', geq='≥', ne='≠', neq='≠', cdot='⋅', times='×', pm='±', infty='∞',
    partial='∂', approx='≈', to='→', sum='∑', prod='∏', int='∫', in_='∈',
    notin='∉', sim='∼', cup='∪', cap='∩', ldots='…', dots='…')
_SYMBOLS.update({'lambda': 'λ', 'in': '∈', 'gt': '>', 'lt': '<'})
_MATH_DELIMITERS = re.compile(r'\$\$([\s\S]+?)\$\$|\$([^$\n]+)\$|\\\(([\s\S]+?)\\\)|\\\[([\s\S]+?)\\\]')


def _node(tag, *items):
    node=ET.Element(tag)
    for item in items:
        if isinstance(item,ET.Element): node.append(item)
        else: node.text=(node.text or '')+str(item)
    return node


class _Notation:
    def __init__(self,source):
        self.tokens=re.findall(r'\\[A-Za-z]+|\\.|\d+(?:\.\d+)?|[^\s]',source)
        self.index=0
        self.depth=0

    def peek(self):
        return self.tokens[self.index] if self.index<len(self.tokens) else None

    def pop(self):
        value=self.peek()
        if value is None: raise ValueError('Incomplete mathematical expression')
        self.index+=1
        return value

    def group_text(self):
        if self.pop()!='{': raise ValueError('Expected mathematical group')
        parts=[]; depth=1
        while depth:
            token=self.pop()
            if token=='{': depth+=1
            if token=='}': depth-=1
            if depth: parts.append(token)
        return ''.join(parts)

    def atom(self):
        self.depth+=1
        if self.depth>32: raise ValueError('Mathematical nesting limit')
        token=self.pop()
        if token=='{':
            result=self.row({'}'}); self.pop()
        elif token in (r'\frac',r'\dfrac',r'\tfrac'):
            result=_node('mfrac',self.atom(),self.atom())
        elif token==r'\sqrt': result=_node('msqrt',self.atom())
        elif token in (r'\text', r'\operatorname'):
            result = _node('mtext', self.group_text())
        elif token in (r'\mathrm', r'\mathbf', r'\mathcal', r'\mathbb'):
            # TeX font commands accept either a group or one following atom.
            # Keep that atom's mathematical structure, including later scripts.
            result = _node('mstyle', self.atom())
            result.set('mathvariant', {
                r'\mathrm': 'normal', r'\mathbf': 'bold', r'\mathcal': 'script', r'\mathbb': 'double-struck',
            }[token])
        elif token==r'\begin':
            kind=self.group_text()
            if kind not in ('cases','aligned','matrix'): raise ValueError('Unsupported mathematical environment')
            rows=[]
            while self.peek()!=r'\end':
                cells=[_node('mtd',self.row({'&',r'\\',r'\end'}))]
                while self.peek()=='&':
                    self.pop();cells.append(_node('mtd',self.row({'&',r'\\',r'\end'})))
                rows.append(_node('mtr',*cells))
                if self.peek()==r'\\':
                    self.pop()
                    if self.peek()=='[':
                        while self.pop()!=']': pass
                elif self.peek()!=r'\end': raise ValueError('Unclosed mathematical environment')
            self.pop()
            if self.group_text()!=kind: raise ValueError('Mismatched mathematical environment')
            table=_node('mtable',*rows)
            result=_node('mrow',_node('mo','{'),table) if kind=='cases' else table
        elif token.startswith('\\'):
            command=token[1:]
            if command in _SYMBOLS: result=_node('mo',_SYMBOLS[command])
            elif command in ('max','min','log','ln','exp','sin','cos','Pr'): result=_node('mi',command)
            elif command in ('left','right','big','Big','bigg','Bigg','bigl','bigr','Bigl','Bigr'): result=self.atom()
            elif command in (',',';','!',' ','quad','qquad'): result=_node('mspace')
            elif command in ('{','}','|','%','_'): result=_node('mo',command)
            else: raise ValueError('Unsupported mathematical command')
        elif token in ('}','&'): raise ValueError('Unexpected mathematical delimiter')
        else:
            result=_node('mn' if token[0].isdigit() else 'mi' if token.isalpha() else 'mo',token)
        self.depth-=1
        return result

    def row(self,closing=()):
        result=_node('mrow')
        while self.peek() is not None and self.peek() not in closing:
            base=self.atom();scripts={}
            while self.peek() in ('_','^'):
                key=self.pop()
                if key in scripts: raise ValueError('Repeated mathematical script')
                scripts[key]=self.atom()
            if len(scripts)==2:base=_node('msubsup',base,scripts['_'],scripts['^'])
            elif scripts:
                key=next(iter(scripts));base=_node('msub' if key=='_' else 'msup',base,scripts[key])
            result.append(base)
        if closing and self.peek() is None: raise ValueError('Unclosed mathematical group')
        return result


def render_math_notation(source,display=False):
    """Return MathML, or None to preserve unsupported input as escaped text."""
    if len(source)>12000:return None
    try:
        notation=_Notation(source)
        math=_node('math',notation.row())
        math.set('display','block' if display else 'inline')
        math.set('class','math-inline' if not display else 'math-display')
        return ET.tostring(math,encoding='unicode')
    except (ValueError,RecursionError):return None


def render_delimited_math(source,render_prose):
    """Use the existing prose renderer outside explicitly delimited formulas."""
    parts=[];cursor=0
    for match in _MATH_DELIMITERS.finditer(source):
        parts.append(render_prose(source[cursor:match.start()]))
        formula=next(group for group in match.groups() if group is not None)
        rendered=render_math_notation(formula,match.group().startswith(('$$',r'\[')))
        parts.append(rendered if rendered is not None else html.escape(match.group(),quote=True))
        cursor=match.end()
    parts.append(render_prose(source[cursor:]))
    return ''.join(parts)
