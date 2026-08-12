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
            "workflow": "optionhelper.recommender/v1",
            "role_rule": ROLE_RULES[role],
            "required_output": _required_output(role),
            "input": dict(payload),
        }
        try:
            result = self.port.run_step(port_role, request)
        except Exception as error:
            self.audit.append(role.lower(), "failed", agent_role=port_role, input_value=request, output_value=None,
                              detail={"error_type": type(error).__name__, "message": str(error)})
            raise
        self.audit.append(role.lower(), "complete", agent_role=port_role, input_value=request, output_value=result)
        return dict(result)


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
