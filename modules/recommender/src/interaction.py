"""结构推荐的确定性已确认条件提取。

只归一用户已经表达的条件；产品选择、条款和数值计算仍由既有
Intent、Research、Critic、Executor流程负责。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


_UNDERLYING = re.compile(r"(?<![A-Za-z0-9])(?P<code>\d{6}\.(?:SH|SZ))(?![A-Za-z0-9])", re.IGNORECASE)
_HORIZON = re.compile(r"(?P<number>\d+|[一二三四五六七八九十两]+)\s*(?P<unit>个?月|月|年|个?季度|季度|季)")
_LOSS = re.compile(r"(?:最大(?:可承受)?(?:亏损|损失|回撤)?|最大亏损?|亏损(?:不超过|上限为|控制在)?|回撤(?:不超过|上限为|控制在)?)\s*(?P<value>\d+(?:\.\d+)?)\s*[%％]")
_CHINESE_NUMBER = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_REQUIRED = ("underlying", "horizon", "market_view", "max_loss", "principal_fluctuation")
_QUESTIONS = {
    "underlying": "请补充标的代码，例如000905.SH。",
    "horizon": "请确认投资期限，例如3个月。",
    "market_view": "请确认市场观点，例如看涨、看跌或震荡。",
    "max_loss": "请确认最大可承受亏损，例如30%。",
    "principal_fluctuation": "请确认是否接受本金波动。",
}


def normalize_confirmed_constraints(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """返回唯一的用户确认条件表示，忽略未知及无效字段。"""

    source = dict(value or {})
    result: dict[str, Any] = {}
    underlying = source.get("underlying")
    if not underlying:
        rows = source.get("underlyings")
        if isinstance(rows, Sequence) and not isinstance(rows, str) and rows:
            underlying = rows[0]
    if isinstance(underlying, str):
        match = _UNDERLYING.search(underlying.upper())
        if match:
            result["underlying"] = match.group("code").upper()
    horizon = _normalize_horizon(source.get("horizon") or source.get("tenor"))
    if horizon:
        result["horizon"] = horizon
    view = _normalize_market_view(source.get("market_view") or source.get("view"))
    if view:
        result["market_view"] = view
    loss = _normalize_loss(source.get("max_loss") or source.get("max_drawdown"))
    if loss:
        result["max_loss"] = loss
    principal = _normalize_principal(source.get("principal_fluctuation"))
    if principal is not None:
        result["principal_fluctuation"] = principal
    output = _normalize_output(source.get("output_type") or source.get("kind"))
    if output:
        result["output_type"] = output
    output_format = _normalize_format(source.get("format"))
    if output_format:
        result["format"] = output_format
    return result


def extract_confirmed_constraints(messages: Sequence[object]) -> dict[str, Any]:
    """从一组用户文本提取最后一次明确给出的条件。"""

    return merge_confirmed_constraints({}, messages)


def merge_confirmed_constraints(existing: Mapping[str, Any] | None, messages: Sequence[object]) -> dict[str, Any]:
    """合并持久化对话中的用户确认，后续明确表达覆盖前值。"""

    result = normalize_confirmed_constraints(existing)
    pending_field: str | None = None
    for item in messages:
        role, text, status = _message(item)
        if role == "assistant":
            pending_field = _pending_field(text) if status == "needs_input" else None
            continue
        if role != "user" or not text:
            continue
        result.update(_extract_text(text))
        confirmation = _confirmation(text)
        if pending_field == "principal_fluctuation" and confirmation is not None:
            result["principal_fluctuation"] = confirmation
        pending_field = None
    return normalize_confirmed_constraints(result)


def missing_required_constraints(constraints: Mapping[str, Any]) -> tuple[str, ...]:
    normalized = normalize_confirmed_constraints(constraints)
    return tuple(field for field in _REQUIRED if field not in normalized)


def question_for_missing_constraint(field: str) -> str:
    return _QUESTIONS.get(str(field), "请补充一项会影响产品筛选的条件。")


def question_for_missing_constraints(fields: Sequence[str]) -> str:
    """Combine every material gap into one research question.

    The caller must never turn five missing fields into five conversational
    rounds.  A single-field request keeps its concise wording; multiple fields
    are presented once in a stable research order.
    """

    ordered = tuple(field for field in _REQUIRED if field in {str(item) for item in fields})
    if len(ordered) == 1:
        return question_for_missing_constraint(ordered[0])
    labels = {
        "underlying": "标的代码",
        "horizon": "研究期限",
        "market_view": "市场方向及波动判断",
        "max_loss": "最大可承受亏损",
        "principal_fluctuation": "是否接受本金波动",
    }
    items = "、".join(labels[field] for field in ordered)
    return f"为完成结构筛选，请一次补充：{items}。已在对话中确认的内容无需重复。"


def _message(value: object) -> tuple[str, str, str]:
    if isinstance(value, Mapping):
        return (
            str(value.get("role", "user")).strip().lower(),
            str(value.get("content", "")).strip(),
            str(value.get("status", "")).strip().lower(),
        )
    return "user", str(value or "").strip(), ""


def _extract_text(text: str) -> dict[str, Any]:
    lowered = text.lower()
    result: dict[str, Any] = {}
    match = _UNDERLYING.search(text.upper())
    if match:
        result["underlying"] = match.group("code").upper()
    horizon = _normalize_horizon(text)
    if horizon:
        result["horizon"] = horizon
    market_view = _normalize_market_view(text)
    if market_view:
        result["market_view"] = market_view
    loss = _normalize_loss(text)
    if loss:
        result["max_loss"] = loss
    principal = _principal_from_text(text)
    if principal is not None:
        result["principal_fluctuation"] = principal
    wants_card = any(word in lowered for word in ("研究简报", "简报", "卡片", "card", "简单报告"))
    wants_report = any(word in lowered for word in ("完整研究报告", "详细报告", "深度报告", "完整报告", "report"))
    card_terms = r"(?:研究简报|简报|卡片|card|简单报告)"
    report_terms = r"(?:完整研究报告|详细报告|深度报告|完整报告|report)"
    negative = r"(?:不要|不需要|不用)"
    clauses = re.split(r"[，,。；;]", lowered)
    card_negated = any(re.search(rf"{negative}.{{0,12}}{card_terms}", clause) for clause in clauses)
    report_negated = any(re.search(rf"{negative}.{{0,12}}{report_terms}", clause) for clause in clauses)
    only_report = wants_report and (card_negated or bool(re.search(rf"(?:只要|仅要|只需).{{0,12}}{report_terms}", lowered)))
    only_card = wants_card and (report_negated or bool(re.search(rf"(?:只要|仅要|只需).{{0,12}}{card_terms}", lowered)))
    wants_both = wants_card and wants_report and not card_negated and not report_negated and not only_report and not only_card
    if only_report and not report_negated:
        result["output_type"] = "report"
    elif only_card and not card_negated:
        result["output_type"] = "card"
    elif wants_both:
        result["output_type"] = "both"
    elif wants_card:
        result["output_type"] = "card"
    elif wants_report or "报告" in lowered:
        result["output_type"] = "report"
    wants_html = any(word in lowered for word in ("html", "网页", "web"))
    wants_pdf = "pdf" in lowered
    replaces_html = bool(re.search(r"(?:不要|不需要).{0,8}html|(?:改为|改成).{0,8}pdf", lowered))
    if wants_pdf and (replaces_html or not wants_html):
        result["format"] = "pdf"
    elif wants_html:
        result["format"] = "html"
    return result


def _normalize_horizon(value: object) -> str | None:
    text = str(value or "").strip()
    matches = tuple(_HORIZON.finditer(text))
    if not matches:
        return "3个月" if "一季" in text else None
    candidates: list[tuple[int, int, str]] = []
    for match in matches:
        number = _number(match.group("number"))
        if number is None or not 1 <= number <= 120:
            continue
        unit = match.group("unit")
        normalized = (
            f"{number}年" if "年" in unit else
            f"{number * 3}个月" if "季" in unit else
            f"{number}个月"
        )
        before = text[max(0, match.start() - 10):match.start()]
        after = text[match.end():min(len(text), match.end() + 16)]
        context = before + match.group(0) + after
        score = 0
        if re.search(r"(?:未来|期限|持有|投资|合同|到期|存续)", context):
            score += 4
        if re.search(r"(?:近|过去|历史|回测|样本|统计)$", before) or re.search(
            r"(?:%|％|分位|历史|回测|样本|统计|表现|收益率|波动率)", after,
        ):
            score -= 4
        candidates.append((score, match.start(), normalized))
    eligible = tuple(item for item in candidates if item[0] >= 0)
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item[0], item[1]))[2]


def _normalize_market_view(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    direction = "上涨" if any(word in text for word in ("上涨", "看涨", "上行", "走高")) else "看跌" if any(word in text for word in ("下跌", "看跌", "下行", "走低")) else "震荡" if any(word in text for word in ("震荡", "横盘")) else ""
    volatility = "波动率上升" if any(word in text for word in ("波动率上升", "波动率上行", "隐含波动率上升", "隐波上升", "波动加大", "波动增大", "iv上升")) else "波动率下降" if any(word in text for word in ("波动率下降", "波动率下行", "隐含波动率下降", "隐波下降", "iv下降")) else ""
    return "+".join(part for part in (direction, volatility) if part) or None


def _normalize_loss(value: object) -> str | None:
    text = str(value or "").strip()
    direct = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[%％]", text)
    if direct:
        amount = float(direct.group(1))
        return f"{amount:g}%" if 0 < amount <= 100 else None
    match = _LOSS.search(text)
    if match:
        amount = float(match.group("value"))
        if 0 < amount <= 100:
            return f"{amount:g}%"
    revised = re.search(
        r"(?:最大(?:可承受)?(?:亏损|损失|回撤)?|最大亏损?|亏损|回撤).{0,12}?(?:改为|改成|调整为|设为)\s*(\d+(?:\.\d+)?)\s*[%％]",
        text,
    )
    if revised:
        amount = float(revised.group(1))
        if 0 < amount <= 100:
            return f"{amount:g}%"
    bare = re.search(r"(?:最多|至多|不超过)\s*(\d+(?:\.\d+)?)\s*[%％]", text)
    if bare:
        amount = float(bare.group(1))
        if 0 < amount <= 100:
            return f"{amount:g}%"
    if any(word in text for word in ("亏损", "回撤", "损失")):
        chinese = re.search(r"([一二三四五六七八九十两])成", text)
        if chinese:
            return f"{_CHINESE_NUMBER[chinese.group(1)] * 10}%"
    return None


def _normalize_principal(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return _principal_from_text(str(value or ""))


def _principal_from_text(text: str) -> bool | None:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return None
    if re.search(r"(?:不接受|不能接受|不愿接受|拒绝).{0,8}(?:本金|净值).{0,6}(?:波动|亏损|损失)", compact):
        return False
    if re.search(r"(?:接受|可接受|可以接受|能接受|承受).{0,8}(?:本金|净值).{0,6}(?:波动|亏损|损失)", compact):
        return True
    return None


def _normalize_output(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"card", "研究简报", "简报", "卡片", "简单报告"}:
        return "card"
    if text in {"both", "两份", "研究简报和完整研究报告"}:
        return "both"
    if text in {"report", "完整研究报告", "详细报告", "深度报告", "完整报告", "报告"}:
        return "report"
    return None


def _normalize_format(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in {"html", "pdf"} else None


def _number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    if value in _CHINESE_NUMBER:
        return _CHINESE_NUMBER[value]
    if value.startswith("十") and len(value) == 2 and value[1] in _CHINESE_NUMBER:
        return 10 + _CHINESE_NUMBER[value[1]]
    if value.endswith("十") and len(value) == 2 and value[0] in _CHINESE_NUMBER:
        return _CHINESE_NUMBER[value[0]] * 10
    return None


def _pending_field(text: str) -> str | None:
    if "本金" in text or "净值" in text:
        return "principal_fluctuation"
    if "最大" in text and any(word in text for word in ("亏损", "回撤", "损失")):
        return "max_loss"
    if "期限" in text:
        return "horizon"
    if "标的" in text:
        return "underlying"
    if "观点" in text:
        return "market_view"
    return None


def _confirmation(text: str) -> bool | None:
    compact = re.sub(r"[\s，。！？、,.!？]", "", text).lower()
    if compact in {"是", "对", "可以", "确认", "接受", "好的", "好"}:
        return True
    if compact in {"否", "不", "不接受", "不可以", "不能"}:
        return False
    return None


__all__ = (
    "extract_confirmed_constraints", "merge_confirmed_constraints", "missing_required_constraints",
    "normalize_confirmed_constraints", "question_for_missing_constraint", "question_for_missing_constraints",
)
