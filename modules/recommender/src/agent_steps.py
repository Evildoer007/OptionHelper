"""单Agent与多Agent共用的固定步骤和审计记录。"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping

from .models import AuditEvent
from .ports import AgentPort


ROLE_RULES = {
    "Intent": "只提取用户已确认事实、缺失信息和检索查询；不得推荐产品。",
    "Research": "只能从输入evidence选择产品；每个候选必须引用evidence_id。",
    "Critic": "只能审阅Research已有product_id；不得新增产品或删除不利证据。",
    "Executor": "只能为已批准候选规划允许的Tool调用；不得生成计算数值。",
    "Freeform": "按当前路由选择最少必要工具；不得把未执行工具写成成功。",
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
class AgentStepRunner:
    port: AgentPort
    workflow_mode: str
    audit: AuditRecorder

    def run(self, role: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if role not in ROLE_RULES:
            raise ValueError(f"未知Agent角色：{role}")
        port_role = role if self.workflow_mode == "multi_agent" else f"SingleAgent.{role}"
        request = {
            "workflow": "optionhelper.recommender",
            "role_rule": ROLE_RULES[role],
            "required_output": _required_output(role),
            "input": dict(payload),
        }
        try:
            result = self.port.run_step(port_role, request)
            _validate_role_result(role, result)
        except Exception as error:
            self.audit.append(role.lower(), "failed", agent_role=port_role, input_value=request, output_value=None,
                              detail={"error_type": type(error).__name__, "message": str(error)})
            raise
        self.audit.append(role.lower(), "complete", agent_role=port_role, input_value=request, output_value=result)
        return dict(result)

    def run_many(self, role: str, payloads: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Run independent fresh children when the App port provides them."""
        if not payloads:
            return []
        batch = getattr(self.port, "run_steps", None)
        if not callable(batch):
            return [self.run(role, payload) for payload in payloads]
        port_role = role if self.workflow_mode == "multi_agent" else f"SingleAgent.{role}"
        requests = [
            {
                "workflow": "optionhelper.recommender",
                "role_rule": ROLE_RULES[role],
                "required_output": _required_output(role),
                "input": dict(payload),
            }
            for payload in payloads
        ]
        try:
            results = batch(port_role, requests)
            if not isinstance(results, list) or len(results) != len(requests):
                raise ValueError("Agent批量步骤返回数量无效")
            for request, result in zip(requests, results, strict=True):
                _validate_role_result(role, result)
                self.audit.append(role.lower(), "complete", agent_role=port_role, input_value=request, output_value=result,
                                  detail={"independent_agent_run": True})
            return [dict(result) for result in results]
        except Exception as error:
            self.audit.append(role.lower(), "failed", agent_role=port_role, input_value=requests, output_value=None,
                              detail={"error_type": type(error).__name__, "message": str(error)})
            raise


def _required_output(role: str) -> Mapping[str, Any]:
    if role == "Intent":
        return {
            "confirmed_constraints": "object",
            "missing_information": "string[]",
            "next_question": "string|null",
            "research_queries": "string[]",
        }
    if role == "Research":
        return {
            "proposals": [{
                "product_id": "string", "product_name": "string|null", "underlyings": "string[]",
                "reason": "string", "suitable_for": "string[]", "not_suitable_for": "string[]",
                "main_risks": "string[]", "library_status": "ready|partial|conflict|unavailable",
                "evidence_ref_ids": "string[]", "missing_inputs": "string[]",
            }]
        }
    if role == "Critic":
        return {
            "reviews": [{
                "product_id": "string", "hard_reject": "boolean", "rejection_reason": "string|null",
                "additional_not_suitable_for": "string[]", "additional_risks": "string[]", "rank_adjustment": "integer",
            }]
        }
    if role == "Executor":
        return {"tool_requests": [{"candidate_id": "string", "module": "string"}]}
    return {"response": "string", "tool_requests": [{"module": "string", "request": "object"}]}


def _validate_role_result(role: str, value: object) -> None:
    """在角色边界拒绝格式漂移，不把不完整模型输出交给下一步。"""

    if not isinstance(value, Mapping):
        raise ValueError(f"{role}必须返回对象")
    if role == "Intent":
        _closed_fields(value, {"confirmed_constraints", "missing_information", "next_question", "research_queries"}, role)
        _mapping_field(value, "confirmed_constraints")
        _strings_field(value, "missing_information")
        _strings_field(value, "research_queries")
        question = value.get("next_question")
        if question is not None and not isinstance(question, str):
            raise ValueError("Intent.next_question必须为字符串或null")
        return
    if role == "Research":
        _closed_fields(value, {"proposals"}, role)
        proposals = _array_field(value, "proposals")
        for proposal in proposals:
            if not isinstance(proposal, Mapping):
                raise ValueError("Research.proposals必须为对象数组")
            _closed_fields(
                proposal,
                {"product_id", "product_name", "underlyings", "reason", "suitable_for", "not_suitable_for", "main_risks", "library_status", "evidence_ref_ids", "missing_inputs"},
                "Research.proposal",
            )
            _nonempty_text(proposal.get("product_id"), "Research.proposal.product_id")
            product_name = proposal.get("product_name")
            if product_name is not None and not isinstance(product_name, str):
                raise ValueError("Research.proposal.product_name必须为字符串或null")
            _nonempty_strings(proposal.get("underlyings"), "Research.proposal.underlyings")
            _nonempty_text(proposal.get("reason"), "Research.proposal.reason")
            for key in ("suitable_for", "not_suitable_for", "main_risks", "evidence_ref_ids"):
                _strings_value(proposal.get(key), f"Research.proposal.{key}")
            if "missing_inputs" in proposal:
                _strings_value(proposal.get("missing_inputs"), "Research.proposal.missing_inputs")
            if "library_status" in proposal and proposal["library_status"] not in {"ready", "partial", "conflict", "unavailable"}:
                raise ValueError("Research.proposal.library_status无效")
        return
    if role == "Critic":
        _closed_fields(value, {"reviews"}, role)
        reviews = _array_field(value, "reviews")
        for review in reviews:
            if not isinstance(review, Mapping) or "product_id" not in review or "hard_reject" not in review:
                raise ValueError("Critic.reviews必须包含product_id和hard_reject")
            _closed_fields(
                review,
                {"product_id", "hard_reject", "rejection_reason", "additional_not_suitable_for", "additional_risks", "rank_adjustment"},
                "Critic.review",
            )
            _nonempty_text(review.get("product_id"), "Critic.review.product_id")
            if not isinstance(review["hard_reject"], bool):
                raise ValueError("Critic.hard_reject必须为布尔值")
            if review.get("rejection_reason") is not None and not isinstance(review.get("rejection_reason"), str):
                raise ValueError("Critic.rejection_reason必须为字符串或null")
            for key in ("additional_not_suitable_for", "additional_risks"):
                if key in review:
                    _strings_value(review[key], f"Critic.review.{key}")
            if "rank_adjustment" in review and (isinstance(review["rank_adjustment"], bool) or not isinstance(review["rank_adjustment"], int)):
                raise ValueError("Critic.rank_adjustment必须为整数")
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
