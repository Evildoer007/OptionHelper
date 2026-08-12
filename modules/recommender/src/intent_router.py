"""自然语言业务路由。

规则只决定进入哪个受控能力，不推测产品条款或推荐结论。
"""

from __future__ import annotations

import re

from .models import RouteDecision


_REPORT = ("报告", "研报", "正式交付", "html", "pdf", "导出")
_RECOMMEND = ("推荐", "适合什么", "选什么", "买什么", "哪种", "什么结构", "结构建议", "配置什么", "怎么做收益", "投资建议", "产品建议", "策略建议", "配置建议")
_MARKET_VIEW = ("看涨", "看跌", "震荡", "波动率", "行情", "期限", "目标收益", "最大亏损", "风险偏好")
_DATA = ("下载数据", "获取数据", "行情数据", "历史数据", "收盘价", "复权", "ifind", "wind")
_RUN = ("定价", "估值", "算一下", "计算", "greeks", "回测", "收益图", "payoffer", "pricer", "backtester", "运行")
_KNOWLEDGE = ("是什么", "解释", "条款", "比较", "区别", "损益", "敲入", "敲出")
_MAINTENANCE = ("新增产品", "修改optionlib", "修改optionreg", "发布产品", "资料维护", "入库")
_EXISTING = ("已有结果", "这次结果", "这些结果", "run_id", "modulerun", "已运行")
_CHAT = ("你好", "您好", "谢谢", "在吗", "早上好", "下午好", "晚上好")
_PRODUCT = ("期权", "雪球", "凤凰", "鲨鱼鳍", "累计", "香草", "价差", "跨式", "宽跨式", "障碍", "结构", "产品", "合同")


def _contains(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def route_intent(prompt: str) -> RouteDecision:
    text = str(prompt).strip()
    if not text:
        raise ValueError("prompt不能为空")

    has_report = _contains(text, _REPORT)
    has_recommend = _contains(text, _RECOMMEND)
    has_market_view = _contains(text, _MARKET_VIEW)
    has_run = _contains(text, _RUN)
    knowledge_request = _contains(text, _KNOWLEDGE)
    explicit_run = _contains(text, ("运行", "算一下", "回测", "生成收益图", "做收益图", "帮我定价", "请定价", "帮我估值", "请估值"))

    if _contains(text, _MAINTENANCE):
        return RouteDecision("maintenance", 0.98, "请求涉及产品资料维护或发布", False, "single")
    if has_report and _contains(text, _EXISTING):
        return RouteDecision("existing_report", 0.96, "请求基于已有运行结果生成报告", False, "single")
    structure_selection = has_recommend or (has_market_view and bool(re.search(r"(结构|产品|策略)", text)))
    if has_report and structure_selection and not _contains(text, _EXISTING):
        return RouteDecision(
            "professional_report", 0.94, "专业报告请求包含结构选择，需要固定推荐流程", True, "capability_based"
        )
    if structure_selection:
        return RouteDecision("recommendation", 0.92, "请求根据观点或约束选择结构", True, "capability_based")
    if _contains(text, _DATA) and not has_run:
        return RouteDecision("data", 0.9, "请求独立获取或整理市场数据", False, "single")
    if knowledge_request and not explicit_run:
        return RouteDecision("knowledge", 0.86, "请求解释产品或定价知识，不执行计算", False, "single")
    if has_run and not _contains(text, ("一起整理", "整理一下", "汇总")) and (
        re.search(r"\b\d+\.\d+(?:\.\d+)?\b", text)
        or _contains(text, ("这个产品", "该合同", "指定结构"))
        or _contains(text, _PRODUCT)
    ):
        return RouteDecision("direct_execution", 0.9, "用户已指定产品或合同并要求运行模块", False, "single")
    if knowledge_request:
        return RouteDecision("knowledge", 0.82, "请求产品知识问答或比较", False, "single")
    if any(text.startswith(term) for term in _CHAT) and len(text) <= 24:
        return RouteDecision("chat", 0.95, "普通闲聊，不启动推荐状态机", False, "single")
    if sum((_contains(text, _DATA), has_run, has_report, _contains(text, _KNOWLEDGE))) >= 2:
        return RouteDecision("freeform", 0.76, "请求自由组合多个能力，由单Agent按需调用", False, "single")
    return RouteDecision("freeform", 0.55, "未命中固定业务流程，交给单Agent按需处理", False, "single")
