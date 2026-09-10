"""Shared, provider-neutral business routing for OptionHelper conversations."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal, Mapping, Sequence


AnalysisPath = Literal["consultation", "direct_module", "recommendation"]
DeliveryKind = Literal["none", "card", "report", "quote"]
DeliveryFormat = Literal["html", "pdf"]
ExecutionSemantics = Literal["multi_agent", "single_model"]


_DELIVERY_MARKERS = {
    "card": ("研究简报", "简单报告", "简报", "card"),
    "report": ("完整研究报告", "详细报告", "深度报告", "完整报告", "report", "html", "pdf", "报告"),
    "quote": ("参考报价", "报价表", "quote"),
}
_RECOMMENDATION_MARKERS = (
    "推荐",
    "适合的期权",
    "哪种期权",
    "什么期权",
    "选择结构",
    "筛选结构",
    "适合什么结构",
    "什么结构适合",
)
_MODULE_MARKERS = (
    "收益图", "收益情景", "损益", "估值", "定价", "greek", "delta", "gamma", "vega", "theta",
    "rho", "回测", "历史胜率", "取数", "行情", "波动率", "重新计算", "重新估值", "重新回测", "历史数据", "交易日历", "获取数据", "下载数据",
)
_TERM_CHANGE_ACTIONS = ("修改", "调整", "改成", "改为", "重新定价", "重新回测", "重算")
_TERM_FIELDS = (
    "行权价",
    "执行价",
    "执行水平",
    "期权费",
    "票息",
    "障碍",
    "敲入水平",
    "敲出水平",
    "气囊水平",
    "期限",
    "参与率",
    "观察频率",
    "观察方式",
    "行权方式",
    "结算方式",
)
_SPECIFIED_STRUCTURE_MARKERS = (
    "看涨期权", "看跌期权", "看涨价差", "看跌价差", "跨式", "宽跨式", "蝶式", "鹰式", "风险逆转",
    "障碍期权", "敲入", "敲出", "二元期权", "触碰期权", "雪球", "安全气囊", "累购", "累沽", "fcn",
    "dcn", "凤凰", "鲨鱼鳍", "方差互换", "多标的",
)


@dataclass(frozen=True)
class WorkflowDecision:
    analysis_path: AnalysisPath
    delivery_kind: DeliveryKind = "none"
    delivery_format: DeliveryFormat = "html"
    preset_id: str = "sequential-deliberation"
    execution_semantics: ExecutionSemantics = "multi_agent"
    reason_code: str = "consultation"

    def __post_init__(self) -> None:
        if not str(self.preset_id).strip():
            raise ValueError("WorkflowDecision.preset_id不能为空")


def decide_workflow(
    message: object,
    *,
    facts: Mapping[str, object] | None = None,
    messages: Sequence[Mapping[str, object]] | None = None,
    preset_id: str = "sequential-deliberation",
    execution_semantics: ExecutionSemantics = "multi_agent",
) -> WorkflowDecision:
    """Choose the business path without deciding Agent lifecycle or Provider."""

    text = str(message or "").strip()
    lowered = text.casefold()
    controlled_facts = facts or {}
    action_text = _affirmative_actions(lowered)
    delivery_kind = _delivery_kind(action_text)
    delivery_format: DeliveryFormat = "pdf" if "pdf" in lowered else "html"
    has_contract = isinstance(controlled_facts.get("resolved_contract"), Mapping)
    module_run_facts = controlled_facts.get("module_run_facts")
    has_verified_results = isinstance(module_run_facts, Sequence) and not isinstance(
        module_run_facts, (str, bytes)
    ) and bool(module_run_facts)
    pending_candidate = controlled_facts.get("recommendation_candidate")
    has_pending_candidate = isinstance(pending_candidate, Mapping)
    recommendation_requested = any(marker in action_text for marker in _RECOMMENDATION_MARKERS) or bool(
        re.search(r"(?:筛选|挑选|选出|选择)[^，。；;!?！？]{0,18}(?:期权|结构|产品)", action_text)
    )
    module_requested = any(marker in action_text for marker in _MODULE_MARKERS)
    changing_terms = has_term_change_intent(lowered)
    specified_structure = _has_specified_structure(lowered) and (
        module_requested
        or delivery_kind != "none"
        or changing_terms
        or recommendation_requested
        or any(marker in lowered for marker in ("这个", "这只", "该结构", "上述结构", "已有结构", "当前结构"))
    )

    # Current instructions take priority over artifacts left by earlier turns.
    if is_consultation_request(lowered):
        return WorkflowDecision("consultation", preset_id=preset_id,
                                execution_semantics=execution_semantics, reason_code="consultation")
    if recommendation_requested:
        return WorkflowDecision("recommendation", delivery_kind, delivery_format, preset_id,
                                execution_semantics, "structure_selection_required")
    if has_pending_candidate and re.search(r"(?:确认|就用|按)(?:这个|刚才|上述|第[0-9一二三四五六七八九十]+个)", lowered):
        return WorkflowDecision("recommendation", delivery_kind, delivery_format, preset_id,
                                execution_semantics, "recommendation_continuation")
    if module_requested and not changing_terms and re.search(r"(?:只|仅|先|获取|下载|取数|拉取|计算|估值|定价|回测|画)", lowered):
        return WorkflowDecision("direct_module", delivery_kind, delivery_format, preset_id,
                                execution_semantics, "specified_structure" if specified_structure else "direct_capability_request")
    if specified_structure and (module_requested or delivery_kind != "none") and not recommendation_requested:
        return WorkflowDecision(
            "direct_module", delivery_kind, delivery_format, preset_id,
            execution_semantics, "specified_structure",
        )
    if has_pending_candidate and _continues_recommendation(text, messages or ()):
        return WorkflowDecision(
            "recommendation", delivery_kind, delivery_format, preset_id,
            execution_semantics, "recommendation_continuation",
        )
    if has_contract and (module_requested or changing_terms or delivery_kind != "none"):
        reason = "existing_contract_term_change" if changing_terms else "existing_contract"
        return WorkflowDecision(
            "direct_module", delivery_kind, delivery_format, preset_id,
            execution_semantics, reason,
        )
    if has_verified_results and delivery_kind != "none":
        return WorkflowDecision(
            "direct_module", delivery_kind, delivery_format, preset_id,
            execution_semantics, "verified_results_delivery",
        )
    if specified_structure and not recommendation_requested:
        return WorkflowDecision(
            "direct_module", delivery_kind, delivery_format, preset_id,
            execution_semantics, "specified_structure",
        )
    if recommendation_requested or delivery_kind == "quote":
        return WorkflowDecision(
            "recommendation", delivery_kind, delivery_format, preset_id,
            execution_semantics, "structure_selection_required",
        )
    if module_requested or delivery_kind != "none" or changing_terms:
        return WorkflowDecision(
            "direct_module", delivery_kind, delivery_format, preset_id,
            execution_semantics, "direct_capability_request",
        )
    return WorkflowDecision(
        "consultation", delivery_kind, delivery_format, preset_id,
        execution_semantics, "consultation",
    )


def _affirmative_actions(text: str) -> str:
    """Exclude negated action lists without swallowing the next affirmative action."""
    action = r"(?:(?:生成|执行|进行|做)\s*)?(?:推荐|筛选|定价|估值|回测|报告|html|pdf|计算|文件)"
    return re.sub(
        rf"(?:不做|不要|不用|无需|不需要|先别|别)\s*(?:再|重新|走)?\s*{action}(?:\s*(?:和|或|与|、|及)\s*{action})*",
        "", text,
    )


def is_consultation_request(text: str) -> bool:
    if re.fullmatch(r"(?:你好|您好|谢谢|多谢|辛苦了|hello|hi)[，。！!\s]*", text):
        return True
    explanation = re.search(r"解释|说明一下|什么是|是什么意思|有什么区别|为什么|怎么理解|如何理解|原理", text)
    action = re.search(r"生成|导出|出一份|重新计算|重新估值|重新定价|重新回测|重算|计算一下|回测一下|帮我.*(?:计算|估值|定价|回测)", text)
    return bool(explanation and not action)


def _delivery_kind(text: str) -> DeliveryKind:
    for kind in ("quote", "card", "report"):
        if any(marker in text for marker in _DELIVERY_MARKERS[kind]):
            return kind  # type: ignore[return-value]
    return "none"


def _has_specified_structure(text: str) -> bool:
    if any(marker in text for marker in _SPECIFIED_STRUCTURE_MARKERS):
        return True
    return bool(re.search(r"(?:产品|结构)\s*[0-9]+(?:\.[0-9]+)+", text, re.IGNORECASE))


def _continues_recommendation(
    text: str, messages: Sequence[Mapping[str, object]],
) -> bool:
    lowered = text.casefold()
    # A rejection still belongs to the pending recommendation state machine;
    # it must not fall through to an unrelated direct module or free chat.
    if re.search(r"(?:不|别|无需|不用|不要|禁止)[^，。！？,.!?]*(?:确认|同意|执行|批准)", lowered) or any(
        marker in lowered for marker in ("不同意", "取消", "先不要")
    ):
        return True
    if has_term_change_intent(lowered) or re.search(
        r"(?:选择|选|候选|第)[^，。！？,.!?]{0,12}(?:[0-9一二三四五六七八九十]|个|两个|全部|都)",
        lowered,
    ):
        return True
    if any(marker in lowered for marker in (
        "市场观点", "看涨", "看跌", "震荡", "最大亏损", "风险承受", "本金波动",
        "标的", "期限", "路径数", "多选", "前两个", "都选",
    )):
        return True
    if any(marker in lowered for marker in (
        "确认", "同意", "按此", "按这个", "继续", "好的", "研究简报", "完整研究报告", "参考报价",
    )):
        return True
    if not messages:
        return False
    latest = next((row for row in reversed(messages) if row.get("role") == "assistant"), {})
    # Only a short answer can implicitly complete a pending question. Explicit
    # new actions and explanations have already been routed above.
    answer = re.fullmatch(
        r"(?:[0-9][0-9.,%％/\-至到\s]*(?:万|千|[kKwW]|条|个?月|年|天|\.(?:SH|SZ))?|"
        r"是|否|可以|接受|不接受|默认|都行|都可以|按默认|用默认)", text.strip(), re.IGNORECASE,
    )
    return bool(answer) and str(latest.get("status", "")).casefold() in {"pending_approval", "needs_input"}


def has_term_change_intent(message: object) -> bool:
    text = str(message or "").strip().casefold()
    return (
        any(marker in text for marker in _TERM_CHANGE_ACTIONS)
        and any(marker in text for marker in _TERM_FIELDS)
    )


__all__ = ("WorkflowDecision", "decide_workflow", "has_term_change_intent")


def recommendation_execution_mode(message: str, default: str = "single") -> str:
    """Resolve an explicit per-request choice without mutating saved preferences."""
    mode = default if default in {"single", "multi"} else "single"
    pattern = r"(?P<negative>不要|不用|别用|禁止|不使用)?\s*(?P<mode>单\s*(?:agent|智能体)|single[ -]?agent|多\s*(?:agent|智能体)|multi[ -]?agent)"
    for match in re.finditer(pattern, str(message), re.IGNORECASE):
        selected = "single" if re.match(r"单|single", match.group("mode"), re.IGNORECASE) else "multi"
        mode = ({"single": "multi", "multi": "single"}[selected] if match.group("negative") else selected)
    return mode
