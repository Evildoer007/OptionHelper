"""Small, deterministic HTML components.

Components consume already-structured values. They perform escaping and
layout only; no component may calculate a financial number or change a fact.
"""

from __future__ import annotations

import html
import re
from typing import Any, Iterable, Mapping

from ..design_tokens import TOKENS


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _esc(value: Any) -> str:
    return html.escape(_text(value), quote=True)


def render_module_header(module: str, title: str, description: str = "") -> str:
    return (
        '<header class="module-header" data-module="'
        + _esc(module)
        + '" data-design-system="'
        + TOKENS.system_id
        + '"><p class="module-header__eyebrow">'
        + _esc(module)
        + '</p><h1>'
        + _esc(title)
        + "</h1>"
        + ("<p>" + _esc(description) + "</p>" if description else "")
        + "</header>"
    )


def render_status(status: str, note: str = "") -> str:
    value = _text(status).lower() or "pending"
    state = TOKENS.states.get(value, TOKENS.states["pending"])
    label = state.get("label", value)
    detail = note or "该模块没有可展示的本次运行结果。"
    return (
        '<div class="module-state" data-status="'
        + _esc(value)
        + '" role="status"><strong>'
        + _esc(label)
        + "</strong><span>"
        + _esc(detail)
        + "</span></div>"
    )


def render_formula(formula: str = "", formula_mathml: str = "") -> str:
    """Render a formula as MathML rather than a code-style string.

    Reporter may pass trusted MathML generated from its structured formula
    fields. For plain text fallback we put the text in ``mtext`` so it remains
    mathematical content without executing markup.
    """

    raw_mathml = formula_mathml
    if formula_mathml:
        # MathML is generated upstream, but the renderer still treats it as
        # untrusted text.  Allow only the structural tags needed by the
        # report formulas and reject arbitrary attributes such as onclick.
        outer = re.fullmatch(r"\s*<math\b[^>]*>(?P<body>.*)</math>\s*", formula_mathml, re.DOTALL | re.IGNORECASE)
        if outer:
            formula_mathml = outer.group("body")
        allowed = {
            "mrow", "mi", "mn", "mo", "msub", "msup", "mfrac", "mtext",
            "mfenced", "mover", "munder", "munderover", "annotation",
            "msqrt", "mroot", "mtable", "mtr", "mtd", "semantics",
            "msubsup", "mspace", "mstyle", "mpadded", "mphantom",
            "menclose", "merror", "mmultiscripts", "mprescripts", "none",
        }
        tags = re.findall(r"</?([A-Za-z][A-Za-z0-9]*)\b[^>]*>", formula_mathml)
        if tags and all(tag in allowed for tag in tags):
            def clean_tag(match: re.Match[str]) -> str:
                source = match.group(0)
                tag = match.group(1)
                if source.startswith("</"):
                    return f"</{tag}>"
                if source.rstrip().endswith("/>"):
                    return f"<{tag}/>"
                return f"<{tag}>"

            formula_mathml = re.sub(
                r"</?([A-Za-z][A-Za-z0-9]*)\b[^>]*>",
                clean_tag,
                formula_mathml,
            )
        else:
            formula_mathml = ""
    body = formula_mathml or _fallback_mathml(formula or raw_mathml)
    return f'<div class="formula" aria-label="数学公式"><math display="block">{body}</math></div>'


def render_inline_formula(formula: str) -> str:
    """Render compact public notation as safe inline MathML."""

    return f'<math class="math-inline" aria-label="数学表达式">{_fallback_mathml(formula)}</math>'


_MATH_TOKENS = re.compile(r"[A-Za-zΠπ]+(?:_[A-Za-z0-9]+)?|\d+(?:\.\d+)?|<=|>=|!=|[+\-×*/=(),<>]")


def _fallback_mathml(formula: str) -> str:
    """Turn the compact public formula notation into readable MathML.

    Formal module outputs occasionally contain a plain-text fallback such as
    ``S_T``.  Leaving that text in ``mtext`` makes an otherwise mathematical
    report look like source code.  This narrow token renderer deliberately
    supports public payoff notation only; anything outside that notation is
    escaped as ordinary mathematical text rather than interpreted as markup.
    """

    source = str(formula or "").strip()
    if re.search(r"</?[A-Za-z]", source):
        return f"<mtext>{_esc(source)}</mtext>"
    tokens = _MATH_TOKENS.findall(source)
    if not tokens or "".join(tokens).replace("×", "*") != re.sub(r"\s+", "", source).replace("×", "*"):
        return f"<mtext>{_esc(source)}</mtext>"

    rendered: list[str] = []
    for token in tokens:
        if "_" in token:
            base, suffix = token.split("_", 1)
            base = "Π" if base in {"Pi", "pi"} else base
            base_tag = "mn" if base.isdigit() else "mi"
            suffix_tag = "mn" if suffix.isdigit() else "mi"
            rendered.append(f"<msub><{base_tag}>{_esc(base)}</{base_tag}><{suffix_tag}>{_esc(suffix)}</{suffix_tag}></msub>")
        elif re.fullmatch(r"\d+(?:\.\d+)?", token):
            rendered.append(f"<mn>{_esc(token)}</mn>")
        elif token in {"+", "-", "×", "*", "/", "=", "(", ")", ",", "<", ">", "<=", ">=", "!="}:
            rendered.append(f"<mo>{_esc('×' if token == '*' else token)}</mo>")
        else:
            rendered.append(f"<mi>{_esc('Π' if token in {'Pi', 'pi'} else token)}</mi>")
    return "<mrow>" + "".join(rendered) + "</mrow>"


def render_source(source: str) -> str:
    if not source:
        return ""
    return f'<p class="source-note">{_esc(source)}</p>'


def render_table(rows: Iterable[Mapping[str, Any]], columns: Iterable[tuple[str, str]], caption: str = "") -> str:
    columns = list(columns)
    body = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        body.append("<tr>" + "".join(f'<td>{_esc(raw.get(key))}</td>' for key, _ in columns) + "</tr>")
    if not body:
        return ""
    head = "".join(f'<th scope="col">{_esc(title)}</th>' for _, title in columns)
    caption_html = f"<caption>{_esc(caption)}</caption>" if caption else ""
    return (
        '<div class="table-wrap">'
        f'<table>{caption_html}<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'
    )


def render_card_shell(title: str, body: str, *, brand: str = "结构化产品研究") -> str:
    return (
        '<main><article class="designer-card" data-output-type="card" data-design-system="'
        + TOKENS.system_id
        + '"><p class="designer-card__brand">'
        + _esc(brand)
        + "</p><h1>"
        + _esc(title)
        + "</h1>"
        + body
        + "</article></main>"
    )


__all__ = ["render_card_shell", "render_formula", "render_inline_formula", "render_module_header", "render_source", "render_status", "render_table"]
