"""结构推荐的确定性已确认条件提取。

只归一用户已经表达的条件；产品选择、条款和数值计算仍由既有
Interpreter、Selector、Reviewer及后续受控执行流程负责。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


_UNDERLYING = re.compile(r"(?<![A-Za-z0-9])(?P<code>\d{6}\.(?:SH|SZ))(?![A-Za-z0-9])", re.IGNORECASE)
_HORIZON = re.compile(r"(?P<number>\d+|[一二三四五六七八九十两]+)\s*(?P<unit>个?月|月|年|个?季度|季度|季)")
_YEAR_AND_HALF = re.compile(r"(?P<number>\d+|[一二三四五六七八九十两]+)\s*年半")
_LOSS = re.compile(r"(?:最大(?:可承受)?(?:亏损|损失|回撤)?|最大亏损?|亏损(?:不超过|上限为|控制在|改为)?|回撤(?:不超过|上限为|控制在|改为)?|最大(?:可承受)?(?:亏损|损失|回撤)?改为)\s*(?P<value>\d+(?:\.\d+)?)\s*[%％]")
_PATH_COUNT = re.compile(
    r"(?:"
    r"(?:mc|monte\s*carlo|蒙特卡洛)(?:模拟)?(?:路径(?:数)?|样本数)?\s*(?:为|是|=|：|:)?\s*(?P<mc>\d+)"
    r"|"
    r"(?P<plain>\d+)\s*(?:条|个)?(?:模拟)?路径(?:数)?"
    r")",
    re.IGNORECASE,
)
_CANDIDATE_COUNT = re.compile(
    r"(?:"
    r"(?:推荐|筛选|选出|选|列出|提供|给出|给我|返回|想要|需要|要)\s*"
    r"(?P<number>\d+|[一二三四五六七八九十两]+)\s*"
    r"(?:个(?!月)|种|项|款|只)[^，,。；;！？!?]{0,8}?(?:候选|结构|产品|结果)"
    r"|(?:候选|结构|产品)(?:数量|个数)\s*(?:为|是|=|：|:)?\s*"
    r"(?P<explicit>\d+|[一二三四五六七八九十两]+)"
    r"(?!(?:\d|[.%％]|\s*(?:个?月|年|季度|季|周|天)))"
    r")"
)
_CHINESE_NUMBER = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_REQUIRED = ("underlying", "horizon", "market_view", "max_loss", "principal_fluctuation")
_NEGATION = r"(?:不再|不会|没有|并非|并不|难以|未(?!来)|没|别|否|无|不)"
_SEMANTIC_BOUNDARY = re.compile(
    r"[，,。；;！？!?]|(?:改为|改成|调整为|转为|变为|"
    r"改|反而|而是|而会|但是|但|而)"
)
_QUESTIONS = {
    "underlying": "请补充标的代码，例如000905.SH。",
    "horizon": "请确认投资期限，例如3个月。",
    "market_view": "请确认市场观点，例如看涨、看跌或震荡。",
    "max_loss": "请确认最大可承受亏损，例如30%。",
    "principal_fluctuation": "请确认是否接受本金波动。",
}
_TERM_FIELD = (
    r"(?:执行价|执行水平|行权价|票息|票面收益|参与率|权利金|期权费|"
    r"敲出|敲入|气囊障碍|结算|行权方式|(?<![a-z0-9])k(?=\d|[^a-z0-9]|$))"
)
_TERM_ACTION = r"(?:改|调|调整|设|设置|变|替换)"
_TERM_REJECTION = r"(?:不要|不用|无需|不需要|别|拒绝|不接受|不(?:改|调|调整|设|设置|变|替换))"


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
    term_overrides = _normalize_term_overrides(source.get("term_overrides"))
    if term_overrides:
        result["term_overrides"] = term_overrides
    path_count = _normalize_path_count(source.get("path_count"))
    if path_count is not None:
        result["path_count"] = path_count
    window = source.get("backtest_range")
    if isinstance(window, Mapping):
        dates = {key: str(window.get(key, "")).strip() for key in ("start_date", "end_date")}
        if all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in dates.values()):
            result["backtest_range"] = dates
    return result


def confirmed_term_overrides(constraints: Mapping[str, Any]) -> dict[str, Any]:
    """Translate the confirmed investment horizon to the contract year fraction."""
    normalized = normalize_confirmed_constraints(constraints)
    result: dict[str, Any] = {}
    horizon = re.fullmatch(r"(\d+)(年|个月)", normalized.get("horizon", ""))
    if horizon:
        result["T"] = int(horizon[1]) / (12 if horizon[2] == "个月" else 1)
    result.update(normalized.get("term_overrides", {}))
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
        extracted = _extract_text(text)
        _merge_user_constraints(result, extracted, text)
        confirmation = _confirmation(text)
        if pending_field == "principal_fluctuation" and confirmation is not None:
            result["principal_fluctuation"] = confirmation
        if pending_field == "path_count":
            path_count = _normalize_path_count(text)
            if path_count is not None:
                result["path_count"] = path_count
        pending_field = None
    return normalize_confirmed_constraints(result)


def _merge_user_constraints(existing: dict[str, Any], extracted: Mapping[str, Any], text: str) -> None:
    """后续表达只覆盖它真正说明的维度，不能抹掉已确认的波动观点。"""

    value = dict(extracted)
    negated_outputs = set(value.pop("_negated_outputs", ()))
    term_operations = value.pop("_term_override_operations", ())
    previous_view = str(existing.get("market_view", ""))
    current_view = str(value.get("market_view", ""))
    if previous_view:
        previous_direction, previous_volatility = _view_parts(previous_view)
        current_direction, current_volatility = _view_parts(current_view)
        if not current_direction and _negates_market_dimension(text, ("上涨", "看涨", "上行", "走高", "下跌", "看跌", "下行", "走低", "震荡", "横盘")):
            previous_direction = ""
        if not current_volatility and _negates_market_dimension(
            text, ("波动率上升", "波动率下行", "波动率下降", "波动率上行", "隐含波动率上升", "隐含波动率下降", "隐波上升", "隐波下降", "iv上升", "iv下降"),
        ):
            previous_volatility = ""
        direction = current_direction or previous_direction
        volatility = current_volatility or previous_volatility
        merged = "+".join(item for item in (direction, volatility) if item)
        if merged:
            value["market_view"] = merged
        else:
            existing.pop("market_view", None)
    if negated_outputs and "output_type" not in value:
        current_output = str(existing.get("output_type", ""))
        remaining = ({"card", "report"} if current_output == "both" else {current_output}) - negated_outputs
        if remaining == {"card", "report"}:
            existing["output_type"] = "both"
        elif len(remaining) == 1 and next(iter(remaining)) in {"card", "quote", "report"}:
            existing["output_type"] = next(iter(remaining))
        else:
            existing.pop("output_type", None)
    existing.update(value)
    if isinstance(term_operations, Sequence) and not isinstance(term_operations, (str, bytes)):
        merged_overrides = dict(existing.get("term_overrides", {}))
        for operation in term_operations:
            if not isinstance(operation, Sequence) or isinstance(operation, (str, bytes)) or len(operation) < 2:
                continue
            action, key = str(operation[0]), str(operation[1])
            if action == "remove":
                merged_overrides.pop(key, None)
            elif action == "set" and len(operation) == 3:
                merged_overrides[key] = operation[2]
        if merged_overrides:
            existing["term_overrides"] = merged_overrides
        else:
            existing.pop("term_overrides", None)


def _view_parts(value: str) -> tuple[str, str]:
    direction = next((item for item in ("上涨", "看跌", "震荡") if item in value), "")
    volatility = next((item for item in ("波动率上升", "波动率下降") if item in value), "")
    return direction, volatility


def _negates_market_dimension(text: str, terms: Sequence[str]) -> bool:
    lowered = text.lower()
    if any(re.search(rf"{_NEGATION}.{{0,8}}{re.escape(term)}", lowered) for term in terms):
        return True
    return any("波动" in term or "隐波" in term or "iv" in term for term in terms) and bool(
        re.search(rf"{_NEGATION}.{{0,4}}(?:上升|上行|下降|下行)", lowered)
    )


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
    affirmed_outputs, negated_outputs = delivery_preferences_from_text(lowered)
    if {"card", "report"}.issubset(affirmed_outputs):
        result["output_type"] = "both"
    elif len(affirmed_outputs) == 1:
        result["output_type"] = next(iter(affirmed_outputs))
    if negated_outputs:
        result["_negated_outputs"] = tuple(sorted(negated_outputs))
    wants_html = any(word in lowered for word in ("html", "网页", "web"))
    wants_pdf = "pdf" in lowered
    replaces_html = bool(re.search(r"(?:不要|不需要).{0,8}html|(?:改为|改成).{0,8}pdf", lowered))
    if wants_pdf and (replaces_html or not wants_html):
        result["format"] = "pdf"
    elif wants_html:
        result["format"] = "html"
    if "docx" in lowered or "word" in lowered:
        result["format"] = "docx"
    window = re.search(
        r"回测[^。；;\n]{0,20}?(\d{4}-\d{2}-\d{2})\s*(?:至|到|~|～|—|–|to)\s*(\d{4}-\d{2}-\d{2})",
        text, re.IGNORECASE,
    )
    if window:
        result["backtest_range"] = {"start_date": window[1], "end_date": window[2]}
    term_operations = _term_override_operations(text)
    if term_operations:
        result["_term_override_operations"] = term_operations
    path_count = _path_count_from_text(text)
    if path_count is not None:
        result["path_count"] = path_count
    return result


def _normalize_path_count(value: object) -> int | None:
    """Accept only an explicitly supplied positive integer path count."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def requested_candidate_count_from_text(value: object) -> int | None:
    """提取用户明确要求的候选数量，不从期限或产品编号推断。"""

    match = _CANDIDATE_COUNT.search(str(value or ""))
    if match is None:
        return None
    token = match.group("number") or match.group("explicit")
    if token.isdigit() and len(token) > 1 and token.startswith("0"):
        return None
    return _number(token)


def _path_count_from_text(text: str) -> int | None:
    match = _PATH_COUNT.search(str(text or ""))
    if match is None:
        return None
    return _normalize_path_count(match.group("mc") or match.group("plain"))


def _normalize_term_overrides(value: object) -> dict[str, str | float]:
    """Keep only a small, typed vocabulary that the App later intersects with OptionReg.

    Natural-language extraction never assumes that every product owns every
    term.  The App binding layer accepts an item only when that exact key is
    present in the selected product's published term set.
    """

    if not isinstance(value, Mapping):
        return {}
    allowed_numeric = {
        "K", "K1", "K2", "K3", "K4", "Pi_0", "P_net", "c", "c_max", "alpha",
        "H_KO", "H_KI", "B", "T",
    }
    allowed_text = {"O_KO", "O_KI", "Oc", "settlement", "exercise_style"}
    result: dict[str, str | float] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if key in allowed_numeric and isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            result[key] = float(raw_value)
        elif key in allowed_text and isinstance(raw_value, str) and raw_value.strip():
            result[key] = raw_value.strip()
    return result


def _extract_term_overrides_from_clause(text: str) -> dict[str, str | float]:
    compact = re.sub(r"\s+", "", text).lower()
    result: dict[str, str | float] = {}
    changed_number = (
        r"(?:(?:从|由)[0-9]+(?:\.[0-9]+)?[%％]?(?:改为|改成|调整到|调整为|变为)"
        r"|(?:调整|改)?[为是]?)([0-9]+(?:\.[0-9]+)?)"
    )

    numeric_patterns = (
        ("K4", rf"第四执行(?:价|水平){changed_number}"),
        ("K3", rf"第三执行(?:价|水平){changed_number}"),
        ("K2", rf"第二执行(?:价|水平){changed_number}"),
        ("K1", rf"第一执行(?:价|水平){changed_number}"),
        ("K", rf"(?<![一二三四五六七八九十])执行(?:价|水平){changed_number}"),
        ("P_net", rf"净(?:权利金|期权费){changed_number}"),
        ("Pi_0", rf"(?<!净)(?:权利金|期权费){changed_number}"),
    )
    for key, pattern in numeric_patterns:
        match = re.search(pattern, compact)
        if match:
            result[key] = float(match.group(1))

    rate_patterns = (
        ("c", rf"(?:票息|票面收益){changed_number}[%％]"),
        ("alpha", rf"参与率{changed_number}[%％]"),
    )
    for key, pattern in rate_patterns:
        match = re.search(pattern, compact)
        if match:
            result[key] = float(match.group(1)) / 100.0

    barrier_patterns = (
        ("H_KO", rf"敲出障碍(?:水平)?{changed_number}"),
        ("H_KI", rf"敲入障碍(?:水平)?{changed_number}"),
        ("B", rf"(?<!敲出)(?<!敲入)(?:气囊)?障碍(?:水平)?{changed_number}"),
    )
    for key, pattern in barrier_patterns:
        match = re.search(pattern, compact)
        if match:
            result[key] = float(match.group(1))

    schedule = (
        "monthly_last" if re.search(r"(?:每月末|月末).{0,4}观察", compact) else
        "daily" if "每日观察" in compact else None
    )
    if schedule:
        if "敲出" in compact:
            result["O_KO"] = schedule
        if "敲入" in compact:
            result["O_KI"] = schedule
        if re.search(r"票息.{0,6}观察|观察.{0,6}票息", compact):
            result["Oc"] = schedule
    if "现金结算" in compact:
        result["settlement"] = "cash"
    if "欧式行权" in compact:
        result["exercise_style"] = "European"
    return result


def _term_override_operations(text: str) -> tuple[tuple[object, ...], ...]:
    operations: list[tuple[object, ...]] = []
    last_keys: tuple[str, ...] = ()
    for clause in _clauses(text):
        keys = _mentioned_term_keys(clause)
        if keys:
            last_keys = keys
        elif last_keys and _inherits_term_keys(clause):
            keys = last_keys
        removals = keys if keys and re.search(r"(?:恢复|改回|还原|使用|用)?默认|取消|删除|移除|清除|不再使用", clause) else ()
        if removals:
            operations.extend(("remove", key) for key in removals)
            continue
        if keys and _rejects_term_change(clause):
            operations = [operation for operation in operations if str(operation[1]) not in keys]
            continue
        updates = _extract_term_overrides_from_clause(clause)
        if not updates and keys:
            updates = _implied_term_override(clause, keys)
        operations.extend(("set", key, value) for key, value in updates.items())
    return tuple(operations)


def _inherits_term_keys(clause: str) -> bool:
    return bool(
        re.search(r"(?:恢复|改回|还原|使用|用)?默认|(?:取消|删除|移除|清除)|(?:不要|别|无需|不用).{0,6}(?:改|调|调整|设|设置|变)|(?:改为|改成|调整到|调整为|设为|变为)\s*\d", clause)
    )


def _implied_term_override(clause: str, keys: Sequence[str]) -> dict[str, str | float]:
    if len(keys) != 1:
        return {}
    match = re.search(r"(?:改为|改成|调整到|调整为|设为|变为)\s*(\d+(?:\.\d+)?)\s*([%％]?)", clause)
    if match is None:
        return {}
    key = str(keys[0])
    amount = float(match.group(1))
    if key in {"c", "alpha"}:
        return {key: amount / 100.0} if match.group(2) else {}
    if key in {"K", "K1", "K2", "K3", "K4", "Pi_0", "P_net", "H_KO", "H_KI", "B"}:
        return {key: amount}
    return {}


def classify_term_change(value: object) -> str:
    """返回条款修改语义，供Host区分应用、清除、拒绝和未解析修改。"""

    text = str(value or "").strip().lower()
    if not text:
        return "none"
    operations = _term_override_operations(text)
    if operations:
        return "cleared" if operations[-1][0] == "remove" else "applied"
    mentioned = any(_mentioned_term_keys(clause) for clause in _clauses(text))
    if mentioned and any(_rejects_term_change(clause) for clause in _clauses(text)):
        return "rejected"
    if mentioned and re.search(r"(?:改|调|调整|设|设置|变|替换|取消|删除|恢复|默认)", text):
        return "unresolved"
    if re.search(r"(?:恢复|改回|还原).{0,4}默认|(?:取消|删除|移除|清除).{0,4}(?:覆盖|条款|参数)", text):
        return "unresolved"
    if not re.search(r"(?:无需|不用|不要|别).{0,6}(?:调整|修改).{0,6}(?:条款|参数)", text) and re.search(
        r"(?:(?:调整|修改|改动|变更).{0,6}(?:条款|参数)|(?:条款|参数).{0,6}(?:调整|修改|改动|变更))",
        text,
    ):
        return "unresolved"
    return "none"


def _mentioned_term_keys(clause: str) -> tuple[str, ...]:
    lowered = clause.lower()
    keys: list[str] = []
    if re.search(r"(?:执行价|执行水平|行权价|(?<![a-z0-9])k(?=\d|[^a-z0-9]|$))", lowered):
        keys.append("K")
    if re.search(r"(?:票息|票面收益)", lowered):
        keys.append("c")
    if re.search(r"(?:参与率)", lowered):
        keys.append("alpha")
    if re.search(r"(?:净权利金|净期权费)", lowered):
        keys.append("P_net")
    elif re.search(r"(?:权利金|期权费)", lowered):
        keys.append("Pi_0")
    keys.extend(_barrier_feature_keys(lowered, "敲出", "H_KO", "O_KO"))
    keys.extend(_barrier_feature_keys(lowered, "敲入", "H_KI", "O_KI"))
    if "气囊障碍" in lowered:
        keys.append("B")
    if "结算" in lowered:
        keys.append("settlement")
    if "行权方式" in lowered or "行权风格" in lowered:
        keys.append("exercise_style")
    return tuple(dict.fromkeys(keys))


def _barrier_feature_keys(text: str, marker: str, barrier_key: str, observation_key: str) -> tuple[str, ...]:
    keys: list[str] = []
    for match in re.finditer(marker, text):
        following = re.search(r"(?:敲出|敲入)", text[match.end():])
        end = match.end() + following.start() if following else len(text)
        segment = text[match.start():end]
        barrier = bool(re.search(r"障碍|障碍水平", segment))
        observation = bool(re.search(r"观察|频率|日程|方式", segment))
        if not barrier and not observation:
            keys.extend((barrier_key, observation_key))
        else:
            if barrier:
                keys.append(barrier_key)
            if observation:
                keys.append(observation_key)
    return tuple(keys)


def _rejects_term_change(clause: str) -> bool:
    number = r"\d+(?:\.\d+)?[%％]?"
    return bool(
        re.search(rf"{_TERM_REJECTION}.{{0,16}}(?:{_TERM_ACTION}|{_TERM_FIELD})", clause)
        or re.search(rf"{_TERM_ACTION}.{{0,8}}{_TERM_REJECTION}", clause)
        or re.search(rf"{_TERM_FIELD}.{{0,8}}{_TERM_REJECTION}.{{0,8}}{number}", clause)
        or re.search(rf"{_TERM_FIELD}.{{0,6}}{number}.{{0,4}}(?:{_TERM_REJECTION}|取消)", clause)
    )


def _clauses(text: str) -> tuple[str, ...]:
    return tuple(item for item in (part.strip().lower() for part in re.split(r"[，,。；;！？!?]", text)) if item)


def delivery_preferences_from_text(text: str) -> tuple[set[str], set[str]]:
    """返回当前表达中明确要求和明确拒绝的交付类型。"""

    patterns = {
        "card": r"(?:研究简报|简报|卡片|card|简单报告)",
        "quote": r"(?:参考报价|报价表|quote)",
        "report": r"(?:完整研究报告|详细报告|深度报告|完整报告|report|(?<!简)报告)",
    }
    matches: list[tuple[int, int, str, bool]] = []
    for kind, pattern in patterns.items():
        for match in re.finditer(pattern, text):
            matches.append((match.start(), match.end(), kind, _delivery_is_negated(text, match.start(), match.end())))
    active: set[str] = set()
    negated: set[str] = set()
    previous_end = 0
    for start, end, kind, is_negated in sorted(matches):
        bridge = text[previous_end:start]
        if not is_negated and re.search(r"(?:改为|改成|换成|只要|仅要|只需)", bridge):
            active.clear()
        if is_negated:
            active.discard(kind)
            negated.add(kind)
        else:
            active.add(kind)
            negated.discard(kind)
        previous_end = end
    return active, negated


def _normalize_horizon(value: object) -> str | None:
    text = _expand_natural_horizon(str(value or "").strip())
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
        after = text[match.end():min(len(text), match.end() + 12)]
        context = before + match.group(0) + after
        score = 0
        if re.search(r"(?:未来|期限|持有|投资|合同|到期|存续)", context):
            score += 4
        if re.search(r"(?:近|过去|历史|回测|样本|统计)\s*$", before) or re.match(
            r"\s*(?:的)?(?:分位|历史|回测|样本|统计|表现|收益率|波动率)", after,
        ):
            score -= 4
        candidates.append((score, match.start(), normalized))
    eligible = tuple(item for item in candidates if item[0] >= 0)
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item[0], item[1]))[2]


def _expand_natural_horizon(text: str) -> str:
    """把常用自然期限转换为统一月份表达，保留前后语境供窗口判别。"""

    def year_and_half(match: re.Match[str]) -> str:
        number = _number(match.group("number"))
        return f"{number * 12 + 6}个月" if number is not None else match.group(0)

    expanded = _YEAR_AND_HALF.sub(year_and_half, text)
    return expanded.replace("半年", "6个月")


def _normalize_market_view(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    direction_choices: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("上涨", ("上涨", "看涨", "上行", "走高")),
        ("看跌", ("下跌", "看跌", "下行", "走低")),
        ("震荡", ("震荡", "横盘")),
    )
    if not re.search(r"(?:隐含波动率|波动率|隐波|iv)", text):
        direction_choices = (
            ("上涨", ("上涨", "看涨", "上行", "走高", "上升")),
            ("看跌", ("下跌", "看跌", "下行", "走低", "下降")),
            direction_choices[2],
        )
    direction = _latest_market_expression(text, direction_choices) or ""
    volatility_expression = _latest_market_expression(text, (
        ("波动率上升", ("波动率上升", "波动率上行", "隐含波动率上升", "隐波上升", "波动加大", "波动增大", "iv上升")),
        ("波动率下降", ("波动率下降", "波动率下行", "隐含波动率下降", "隐波下降", "iv下降")),
    ))
    volatility = volatility_expression or ""
    volatility_markers = tuple(re.finditer(r"(?:隐含波动率|波动率|隐波|iv)", text))
    if volatility_markers:
        tail = text[volatility_markers[-1].start():]
        stop = re.search(r"(?:但是|但|而)(?:价格|标的|指数)", tail)
        if stop:
            tail = tail[:stop.start()]
        inherited = _latest_market_expression(tail, (
            ("波动率上升", ("上升", "上行", "加大", "增大")),
            ("波动率下降", ("下降", "下行", "减小", "降低")),
        ))
        if inherited is not None:
            volatility = inherited
    return "+".join(part for part in (direction, volatility) if part) or None


def _latest_market_expression(text: str, choices: Sequence[tuple[str, Sequence[str]]]) -> str | None:
    """返回最后一次观点表达；末次否定会清除同维度的旧观点。"""

    matches: list[tuple[int, str, bool]] = []
    for normalized, terms in choices:
        for term in terms:
            for match in re.finditer(re.escape(term), text):
                matches.append((match.end(), normalized, _is_locally_negated(text, match.start())))
    if not matches:
        return None
    _, normalized, negated = max(matches, key=lambda item: item[0])
    return "" if negated else normalized


def _is_locally_negated(text: str, position: int) -> bool:
    local = _local_prefix(text, position)
    return bool(re.search(rf"{_NEGATION}.{{0,8}}$", local))


def _delivery_is_negated(text: str, start: int, end: int) -> bool:
    if _is_locally_negated(text, start) or re.search(r"(?:取消|放弃).{0,6}$", _local_prefix(text, start)):
        return True
    suffix = re.split(r"[，,。；;！？!?]", text[end:], maxsplit=1)[0]
    return bool(re.match(
        r"\s*(?:(?:我|我们|本人)\s*)?(?:不要|不用|无需|不需要|不想要|没(?:有)?必要|取消|放弃|作罢)",
        suffix,
    ))


def _local_prefix(text: str, position: int) -> str:
    prefix = text[:position]
    boundaries = tuple(_SEMANTIC_BOUNDARY.finditer(prefix))
    return prefix[boundaries[-1].end():] if boundaries else prefix


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
    if re.search(r"(?:接受|可接受|可以接受|能接受|承受)?.{0,4}(?:本金|净值).{0,6}(?:不|无|不能)(?:发生)?(?:波动|亏损|损失)", compact):
        return False
    if re.search(r"(?:接受|可接受|可以接受|能接受|承受).{0,8}(?:本金|净值).{0,6}(?:波动|亏损|损失)", compact):
        return True
    return None


def _normalize_output(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"card", "研究简报", "简报", "卡片", "简单报告"}:
        return "card"
    if text in {"quote", "参考报价", "报价表"}:
        return "quote"
    if text in {"both", "两份", "研究简报和完整研究报告"}:
        return "both"
    if text in {"report", "完整研究报告", "详细报告", "深度报告", "完整报告", "报告"}:
        return "report"
    return None


def _normalize_format(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in {"html", "pdf", "docx"} else None


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
    if "路径数" in text or "路径" in text and any(term in text.lower() for term in ("mc", "monte carlo", "蒙特卡洛")):
        return "path_count"
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
    "classify_term_change", "extract_confirmed_constraints", "merge_confirmed_constraints", "missing_required_constraints",
    "normalize_confirmed_constraints", "question_for_missing_constraint", "question_for_missing_constraints",
    "requested_candidate_count_from_text",
)
