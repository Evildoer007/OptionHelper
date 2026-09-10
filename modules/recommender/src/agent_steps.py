"""单Agent与多Agent共用的固定步骤和审计记录。"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping

from .models import AuditEvent
from .ports import AgentPort, AgentStepResult


# One source for the model-facing array schema and the Host allowlist.
EVALUATION_MODULES = ("payoffer", "pricer", "backtester")
STRUCTURER_PLAN_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["product_id", "modules", "term_overrides"],
        "additionalProperties": False,
        "properties": {
            "product_id": {"type": "string", "minLength": 1},
            "modules": {"type": "array", "minItems": 1, "uniqueItems": True,
                        "items": {"type": "string", "enum": list(EVALUATION_MODULES)}},
            "term_overrides": {"type": "object"},
        },
    },
}

ROLE_RULES = {
    "Interpreter": "只提取用户已确认事实、缺失信息和产品资料检索查询；查询应使用结构类型、收益特征或风险方向，不查询行情、期权链或波动率数据；不得推荐产品。",
    "Selector": "只能从输入evidence选择产品；每个候选必须引用evidence_id。",
    "Framer": "只框定用户已确认事实、目标、约束和产品资料检索查询；查询应使用结构类型、收益特征或风险方向，不查询行情或波动率数据；不得推荐产品、不得生成金融指标。",
    "Matcher": "只能从输入evidence提出匹配目标与约束的产品候选；每个候选必须引用evidence_id；不得生成金融指标。",
    "Hedger": "只能从输入evidence独立审视候选的适配边界和风险，并以产品候选形式提出意见；每个候选必须引用evidence_id；不得生成金融指标。",
    "Moderator": "只能合并Framer、Matcher、Hedger和输入evidence已有内容，输出可审计候选及复核；不得新增无证据产品、不得生成金融指标。",
    "Structurer": "只能基于输入证据提出产品候选、受控条款调整和需要验证的模块；不得生成收益、估值、Greeks或回测数值。",
    "Trader": "按当前候选计划调用获授权的业务工具，只依据工具返回的FactRef接受候选或要求受控条款调整；不得新增产品、改写模块结果或生成金融数值。",
    "Specifier": "只把用户原文中的硬约束和排序要求转换为受控RankingSpec；不得生成候选、计算指标或补写用户未表达的阈值。",
    "Generator": "只能从输入evidence提出候选产品；每个候选必须引用evidence_id，不得生成金融指标或改变RankingSpec。",
    "Reviewer": "Mode1只复核Selector已有候选；其他Mode只对当前候选或确定性排序作最终批准或整体拒绝；不得新增候选、重排、改写当前输入或模块事实。",
    "Executor": "只能为已批准候选规划允许的Tool调用；不得生成计算数值。",
    "Freeform": "按当前路由选择最少必要工具；不得把未执行工具写成成功。",
}


MODE_ONE_ROLE_NAMES = {
    "Interpreter": "Interpreter", "Selector": "Selector", "Reviewer": "Reviewer",
}


def canonical_hash(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


@dataclass
class AuditRecorder:
    mode: str
    events: list[AuditEvent] = field(default_factory=list)

    def append(
        self,
        stage: str,
        status: str,
        *,
        agent_role: str | None,
        input_value: Any,
        output_value: Any | None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        self.events.append(AuditEvent(
            sequence=len(self.events) + 1,
            stage=stage,
            status=status,
            agent_role=agent_role,
            mode=self.mode,
            input_hash=canonical_hash(input_value),
            output_hash=canonical_hash(output_value) if output_value is not None else None,
            detail=dict(detail or {}),
        ))


@dataclass
class AgentReceiptLedger:
    workflow_mode: str
    _agent_run_roles: dict[str, str] = field(default_factory=dict, init=False)
    _child_session_roles: dict[str, str] = field(default_factory=dict, init=False)

    def validate(
        self,
        role: str,
        port_role: str,
        request: Mapping[str, Any],
        step: AgentStepResult,
    ) -> Mapping[str, Any]:
        if not isinstance(step, AgentStepResult):
            raise ValueError(f"{role}必须返回AgentStepResult")
        receipt = step.receipt
        if receipt.role != port_role:
            raise ValueError(f"{role}运行凭证角色不匹配")
        if receipt.input_hash != canonical_hash(request):
            raise ValueError(f"{role}运行凭证输入哈希不匹配")
        if receipt.output_hash != canonical_hash(step.result):
            raise ValueError(f"{role}运行凭证输出哈希不匹配")
        if self.workflow_mode == "multi_agent":
            run_owner = self._agent_run_roles.get(receipt.agent_run_id)
            session_owner = self._child_session_roles.get(receipt.child_session_id)
            if run_owner is not None and run_owner != role:
                raise ValueError("多Agent工作流跨角色重复使用agent_run_id")
            if session_owner is not None and session_owner != role:
                raise ValueError("多Agent工作流跨角色重复使用child_session_id")
        self._agent_run_roles[receipt.agent_run_id] = role
        self._child_session_roles[receipt.child_session_id] = role
        _validate_role_result(role, step.result)
        return dict(step.result)


@dataclass
class AgentStepRunner:
    port: AgentPort
    workflow_mode: str
    audit: AuditRecorder
    receipt_ledger: AgentReceiptLedger = field(init=False)

    def __post_init__(self) -> None:
        self.receipt_ledger = AgentReceiptLedger(self.workflow_mode)

    def build_request(self, role: str, payload: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
        if role not in ROLE_RULES:
            raise ValueError(f"未知Agent角色：{role}")
        port_role = role if self.workflow_mode == "multi_agent" else f"SingleAgent.{role}"
        request = {
            "workflow": "optionhelper.recommender",
            "role_rule": ROLE_RULES[role],
            "required_output": _required_output(role, payload),
            "input": dict(payload),
        }
        if role == "Structurer":
            request["output_schema"] = {
                "type": "object", "required": ["research_queries", "proposals", "evaluation_plan"],
                "additionalProperties": False,
                "properties": {
                    "research_queries": {"type": "array", "items": {"type": "string"}},
                    "proposals": {"type": "array", "items": {"type": "object"}},
                    "evaluation_plan": STRUCTURER_PLAN_SCHEMA,
                },
            }
        if role == "Structurer" and payload.get("comparison_only") is True:
            # No financial plan is needed to compare product-library evidence.
            request["required_output"]["evaluation_plan"] = []
            request["output_schema"]["properties"]["evaluation_plan"] = {"type": "array", "maxItems": 0}
        return port_role, request

    def accept(
        self,
        role: str,
        port_role: str,
        request: Mapping[str, Any],
        step: AgentStepResult,
    ) -> Mapping[str, Any]:
        try:
            _validate_request_result(role, request, step.result)
            result = self.receipt_ledger.validate(role, port_role, request, step)
        except Exception as error:
            self.audit.append(role.lower(), "failed", agent_role=port_role, input_value=request, output_value=None,
                              detail={"error_type": type(error).__name__, "message": str(error)})
            raise
        self.audit.append(role.lower(), "complete", agent_role=port_role, input_value=request, output_value=result)
        return dict(result)

    def run(self, role: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        port_role, request = self.build_request(role, payload)
        step = self.port.run_step(port_role, request)
        if role == "Structurer":
            try:
                _validate_request_result(role, request, step.result)
            except ValueError as error:
                # Repair structure once, before accepting a receipt or executing
                # a plan. Never coerce strings into modules or retry tool effects.
                self.audit.append("structurer", "invalid_output", agent_role=port_role,
                                  input_value=request, output_value=None,
                                  detail={"message": str(error), "repair_attempt": 1})
                request = {**request, "validation_feedback": {
                    "error": str(error), "attempt": 1,
                    "instruction": "按output_schema重新返回完整result；modules必须为JSON数组，不得新增工具或计算事实。",
                }}
                step = self.port.run_step(port_role, request)
        return self.accept(role, port_role, request, step)

    def run_named(self, payloads: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Mapping[str, Any]]:
        """Run distinct named roles as separate AgentRuns.

        An App port may schedule these concurrently; the common Recommender
        contract deliberately requires only independent runs.  This keeps the
        domain workflow portable to hosts without a safe heterogeneous batch
        primitive, while never collapsing separate roles into one model call.
        """

        if not payloads:
            return {}
        unknown = sorted(set(payloads).difference(ROLE_RULES))
        if unknown:
            raise ValueError(f"未知Agent角色：{','.join(unknown)}")
        batch = getattr(self.port, "run_named_steps", None)
        if not callable(batch):
            return {role: self.run(role, payload) for role, payload in payloads.items()}
        requests: dict[str, Mapping[str, Any]] = {}
        port_roles: dict[str, str] = {}
        for role, payload in payloads.items():
            port_role = role if self.workflow_mode == "multi_agent" else f"SingleAgent.{role}"
            port_roles[role] = port_role
            requests[role] = {
                "workflow": "optionhelper.recommender",
                "role_rule": ROLE_RULES[role],
                "required_output": _required_output(role, payload),
                "input": dict(payload),
            }
        try:
            results = batch({port_roles[role]: request for role, request in requests.items()})
            if not isinstance(results, Mapping) or set(results) != set(requests):
                raise ValueError("具名Agent步骤返回角色集合无效")
            normalized: dict[str, Mapping[str, Any]] = {}
            for role, request in requests.items():
                step = results[port_roles[role]]
                result = self.receipt_ledger.validate(role, port_roles[role], request, step)
                self.audit.append(
                    role.lower(), "complete", agent_role=port_roles[role], input_value=request,
                    output_value=result, detail={"independent_agent_run": True},
                )
                normalized[role] = dict(result)
            return normalized
        except Exception as error:
            self.audit.append(
                "named_roles", "failed", agent_role=None, input_value=requests, output_value=None,
                detail={"error_type": type(error).__name__, "message": str(error)},
            )
            raise


def _required_output(role: str, payload: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    if role in {"Interpreter", "Framer"}:
        return {
            "confirmed_constraints": "object",
            "missing_information": "string[]",
            "next_question": "string|null",
            "research_queries": "string[]",
        }
    if role in {"Matcher", "Hedger"} and isinstance(payload, Mapping) and "candidates" in payload:
        return {
            "branch_results": [{
                "candidate_id": "string", "recommendation": "string",
                "risk_conclusion": "string", "used_fact_refs": "string[]",
            }]
        }
    if role in {"Selector", "Matcher", "Hedger"}:
        return {
            "proposals": [{
                "product_id": "string", "product_name": "string|null", "underlyings": "string[]",
                "reason": "string", "suitable_for": "string[]", "not_suitable_for": "string[]",
                "main_risks": "string[]", "library_status": "ready|partial|conflict|unavailable",
                "evidence_ref_ids": "string[]", "missing_inputs": "string[]",
            }]
        }
    if role == "Structurer":
        return {
            "research_queries": "string[]",
            "proposals": [{
                "product_id": "string", "product_name": "string|null", "underlyings": "string[]",
                "reason": "string", "suitable_for": "string[]", "not_suitable_for": "string[]",
                "main_risks": "string[]", "library_status": "ready|partial|conflict|unavailable",
                "evidence_ref_ids": "string[]", "missing_inputs": "string[]",
            }],
            "evaluation_plan": [{
                "product_id": "string", "modules": ["payoffer"], "term_overrides": "object",
            }],
        }
    if role == "Trader" and isinstance(payload, Mapping) and payload.get("comparison_only") is True:
        return _required_output("Reviewer", {"proposals": []})
    if role == "Trader":
        return {
            "evaluations": [{
                "product_id": "string", "candidate_id": "string",
                "decision": "accept|rework", "reason": "string",
                "used_fact_refs": "string[]", "term_adjustments": "object",
            }]
        }
    if role == "Specifier":
        return {
            "research_queries": "string[]",
            "ranking_spec": {
                "ranking_spec_id": "string", "hard_constraints": "object", "sort_keys": "object[]",
                "tie_break_policy": "candidate_id",
            },
        }
    if role == "Generator":
        return _required_output("Selector")
    if role == "Reviewer" and isinstance(payload, Mapping) and "proposals" in payload:
        return {
            "reviews": [{
                "product_id": "string", "hard_reject": "boolean", "rejection_reason": "string|null",
                "additional_not_suitable_for": "string[]", "additional_risks": "string[]", "rank_adjustment": "integer",
            }]
        }
    if role == "Reviewer":
        return {"decision": "approve|reject", "reason": "string"}
    if role == "Moderator" and isinstance(payload, Mapping) and "branch_candidates" in payload:
        return {"selected_candidate_ids": "string[]", "conflicts": "string[]"}
    if role == "Moderator":
        return {
            "proposals": [{
                "product_id": "string", "product_name": "string|null", "underlyings": "string[]",
                "reason": "string", "suitable_for": "string[]", "not_suitable_for": "string[]",
                "main_risks": "string[]", "library_status": "ready|partial|conflict|unavailable",
                "evidence_ref_ids": "string[]", "missing_inputs": "string[]",
            }],
            "reviews": [{
                "product_id": "string", "hard_reject": "boolean", "rejection_reason": "string|null",
                "additional_not_suitable_for": "string[]", "additional_risks": "string[]", "rank_adjustment": "integer",
            }],
        }
    if role == "Executor":
        return {"tool_requests": [{"candidate_id": "string", "module": "string"}]}
    return {"response": "string", "tool_requests": [{"module": "string", "request": "object"}]}


def _validate_request_result(role: str, request: Mapping[str, Any], value: object) -> None:
    _validate_role_result(role, value)
    domain_input = request.get("input", {})
    if role == "Structurer" and domain_input.get("comparison_only") is True and value.get("evaluation_plan"):
        raise ValueError("只比较模式的evaluation_plan必须为[]，不得安排定价、回测或收益计算")


def _validate_role_result(role: str, value: object) -> None:
    """在角色边界拒绝格式漂移，不把不完整模型输出交给下一步。"""

    if not isinstance(value, Mapping):
        raise ValueError(f"{role}必须返回对象")
    if role in {"Interpreter", "Framer"}:
        _closed_fields(value, {"confirmed_constraints", "missing_information", "next_question", "research_queries"}, role)
        _mapping_field(value, "confirmed_constraints")
        _strings_field(value, "missing_information")
        _strings_field(value, "research_queries")
        question = value.get("next_question")
        if question is not None and not isinstance(question, str):
            raise ValueError("Interpreter.next_question必须为字符串或null")
        return
    if role in {"Matcher", "Hedger"} and "branch_results" in value:
        _closed_fields(value, {"branch_results"}, role)
        rows = _array_field(value, "branch_results")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError(f"{role}.branch_results必须为对象数组")
            _closed_fields(
                row,
                {"candidate_id", "recommendation", "risk_conclusion", "used_fact_refs"},
                f"{role}.branch_result",
            )
            _nonempty_text(row.get("candidate_id"), f"{role}.candidate_id")
            _nonempty_text(row.get("recommendation"), f"{role}.recommendation")
            _nonempty_text(row.get("risk_conclusion"), f"{role}.risk_conclusion")
            _strings_value(row.get("used_fact_refs"), f"{role}.used_fact_refs")
        return
    if role in {"Selector", "Matcher", "Hedger"}:
        _closed_fields(value, {"proposals"}, role)
        proposals = _array_field(value, "proposals")
        for proposal in proposals:
            if not isinstance(proposal, Mapping):
                raise ValueError("Selector.proposals必须为对象数组")
            _closed_fields(
                proposal,
                {"product_id", "product_name", "underlyings", "reason", "suitable_for", "not_suitable_for", "main_risks", "library_status", "evidence_ref_ids", "missing_inputs"},
                "Selector.proposal",
            )
            _nonempty_text(proposal.get("product_id"), "Selector.proposal.product_id")
            product_name = proposal.get("product_name")
            if product_name is not None and not isinstance(product_name, str):
                raise ValueError("Selector.proposal.product_name必须为字符串或null")
            _nonempty_strings(proposal.get("underlyings"), "Selector.proposal.underlyings")
            _nonempty_text(proposal.get("reason"), "Selector.proposal.reason")
            for key in ("suitable_for", "not_suitable_for", "main_risks", "evidence_ref_ids"):
                _strings_value(proposal.get(key), f"Selector.proposal.{key}")
            if "missing_inputs" in proposal:
                _strings_value(proposal.get("missing_inputs"), "Selector.proposal.missing_inputs")
            if "library_status" in proposal and proposal["library_status"] not in {"ready", "partial", "conflict", "unavailable"}:
                raise ValueError("Selector.proposal.library_status无效")
        return
    if role == "Structurer":
        _closed_fields(value, {"research_queries", "proposals", "evaluation_plan"}, role)
        _strings_field(value, "research_queries")
        _validate_role_result("Selector", {"proposals": value.get("proposals")})
        plans = _array_field(value, "evaluation_plan")
        for index, plan in enumerate(plans):
            if not isinstance(plan, Mapping):
                raise ValueError("Structurer.evaluation_plan必须为对象数组")
            _closed_fields(plan, {"product_id", "modules", "term_overrides"}, "Structurer.evaluation_plan")
            _nonempty_text(plan.get("product_id"), "Structurer.evaluation_plan.product_id")
            modules = plan.get("modules")
            if not isinstance(modules, list):
                raise ValueError(f'Structurer.evaluation_plan[{index}].modules必须为数组，例如["payoffer"]，不能为字符串或null')
            if not modules or any(not isinstance(module, str) or module not in EVALUATION_MODULES for module in modules):
                raise ValueError("Structurer.evaluation_plan.modules包含未授权模块")
            if len(modules) != len(set(modules)):
                raise ValueError("Structurer.evaluation_plan.modules不得重复")
            if not isinstance(plan.get("term_overrides", {}), Mapping):
                raise ValueError("Structurer.evaluation_plan.term_overrides必须为对象")
        return
    if role == "Trader" and "reviews" in value:
        _validate_role_result("Reviewer", value)
        return
    if role == "Trader":
        _closed_fields(value, {"evaluations"}, role)
        rows = _array_field(value, "evaluations")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("Trader.evaluations必须为对象数组")
            _closed_fields(
                row,
                {"product_id", "candidate_id", "decision", "reason", "used_fact_refs", "term_adjustments"},
                "Trader.evaluation",
            )
            _nonempty_text(row.get("product_id"), "Trader.evaluation.product_id")
            _nonempty_text(row.get("candidate_id"), "Trader.evaluation.candidate_id")
            if row.get("decision") not in {"accept", "rework"}:
                raise ValueError("Trader.evaluation.decision无效")
            _nonempty_text(row.get("reason"), "Trader.evaluation.reason")
            _strings_value(row.get("used_fact_refs"), "Trader.evaluation.used_fact_refs")
            if not isinstance(row.get("term_adjustments", {}), Mapping):
                raise ValueError("Trader.evaluation.term_adjustments必须为对象")
        return
    if role == "Specifier":
        _closed_fields(value, {"research_queries", "ranking_spec"}, role)
        _strings_field(value, "research_queries")
        if not isinstance(value.get("ranking_spec"), Mapping):
            raise ValueError("Specifier.ranking_spec必须为对象")
        return
    if role == "Generator":
        _validate_role_result("Selector", {"proposals": value.get("proposals")})
        _closed_fields(value, {"proposals"}, role)
        return
    if role == "Reviewer" and "reviews" in value:
        _closed_fields(value, {"reviews"}, role)
        reviews = _array_field(value, "reviews")
        for review in reviews:
            if not isinstance(review, Mapping) or "product_id" not in review or "hard_reject" not in review:
                raise ValueError("Reviewer.reviews必须包含product_id和hard_reject")
            _closed_fields(
                review,
                {"product_id", "hard_reject", "rejection_reason", "additional_not_suitable_for", "additional_risks", "rank_adjustment"},
                "Reviewer.review",
            )
            _nonempty_text(review.get("product_id"), "Reviewer.review.product_id")
            if not isinstance(review["hard_reject"], bool):
                raise ValueError("Reviewer.hard_reject必须为布尔值")
            if review.get("rejection_reason") is not None and not isinstance(review.get("rejection_reason"), str):
                raise ValueError("Reviewer.rejection_reason必须为字符串或null")
            for key in ("additional_not_suitable_for", "additional_risks"):
                if key in review:
                    _strings_value(review[key], f"Reviewer.review.{key}")
            if "rank_adjustment" in review and (isinstance(review["rank_adjustment"], bool) or not isinstance(review["rank_adjustment"], int)):
                raise ValueError("Reviewer.rank_adjustment必须为整数")
        return
    if role == "Reviewer":
        _closed_fields(value, {"decision", "reason"}, role)
        if value.get("decision") not in {"approve", "reject"}:
            raise ValueError("Reviewer.decision无效")
        if not isinstance(value.get("reason", ""), str):
            raise ValueError("Reviewer.reason必须为字符串")
        if value.get("decision") == "reject" and not str(value.get("reason", "")).strip():
            raise ValueError("Reviewer拒绝时必须说明reason")
        return
    if role == "Moderator" and "selected_candidate_ids" in value:
        _closed_fields(value, {"selected_candidate_ids", "conflicts"}, role)
        _nonempty_strings(value.get("selected_candidate_ids"), "Moderator.selected_candidate_ids")
        _strings_value(value.get("conflicts"), "Moderator.conflicts")
        return
    if role == "Moderator":
        _closed_fields(value, {"proposals", "reviews"}, role)
        _validate_role_result("Selector", {"proposals": value.get("proposals")})
        _validate_role_result("Reviewer", {"reviews": value.get("reviews")})
        return
    if role == "Executor":
        _closed_fields(value, {"tool_requests"}, role)
        requests = _array_field(value, "tool_requests")
        for request in requests:
            if not isinstance(request, Mapping):
                raise ValueError("Executor.tool_requests必须包含candidate_id和module")
            _closed_fields(request, {"candidate_id", "module"}, "Executor.tool_request")
            _nonempty_text(request.get("candidate_id"), "Executor.tool_request.candidate_id")
            _nonempty_text(request.get("module"), "Executor.tool_request.module")
        return
    _closed_fields(value, {"response", "tool_requests"}, role)
    requests = _array_field(value, "tool_requests", default=())
    if "response" in value and not isinstance(value["response"], str):
        raise ValueError("Freeform.response必须为字符串")
    for request in requests:
        if not isinstance(request, Mapping):
            raise ValueError("Freeform.tool_requests必须包含module和request")
        _closed_fields(request, {"module", "request"}, "Freeform.tool_request")
        _nonempty_text(request.get("module"), "Freeform.tool_request.module")
        if not isinstance(request.get("request", {}), Mapping):
            raise ValueError("Freeform.tool_requests必须包含module和request")


def _closed_fields(value: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise ValueError(f"{name}包含未知字段：{','.join(unknown)}")


def _nonempty_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}必须为非空字符串")


def _strings_value(value: object, name: str) -> None:
    if isinstance(value, (str, bytes)) or not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name}必须为字符串数组")


def _nonempty_strings(value: object, name: str) -> None:
    _strings_value(value, name)
    if not value or any(not item.strip() for item in value):
        raise ValueError(f"{name}必须为非空字符串数组")


def _array_field(value: Mapping[str, Any], key: str, *, default: object = None) -> list[Any]:
    raw = value.get(key, default)
    if isinstance(raw, (str, bytes)) or not isinstance(raw, list):
        raise ValueError(f"{key}必须为数组")
    return raw


def _mapping_field(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    raw = value.get(key)
    if not isinstance(raw, Mapping):
        raise ValueError(f"{key}必须为对象")
    return raw


def _strings_field(value: Mapping[str, Any], key: str) -> list[str]:
    rows = _array_field(value, key)
    if any(not isinstance(item, str) for item in rows):
        raise ValueError(f"{key}必须为字符串数组")
    return rows
