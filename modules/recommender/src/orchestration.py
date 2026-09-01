"""Resumable Mode 1 orchestration shared by App and Skill host adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from uuid import uuid4

from .agent_steps import AgentStepRunner, AuditRecorder, MODE_ONE_ROLE_NAMES
from .evidence_retriever import retrieve_evidence
from .interaction import missing_required_constraints, question_for_missing_constraints
from .models import (
    RECOMMENDATION_SET_SCHEMA,
    ModelCapability,
    RecommendationCase,
    RecommendationSet,
    RouteDecision,
)
from .ports import AgentExecutionPolicy, AgentStepResult
from .service import (
    RecommenderService,
    RecommenderUnavailable,
    _prepare_recommendation_case,
    _requested_candidate_count,
)


@dataclass(frozen=True)
class AgentRoleTask:
    workflow_ref: str
    role: str
    port_role: str
    request: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_ref": self.workflow_ref,
            "role": self.role,
            "port_role": self.port_role,
            "request": dict(self.request),
        }


@dataclass
class _ActiveRecommendation:
    workflow_ref: str
    case: RecommendationCase
    route: RouteDecision
    run_id: str
    mode: str
    audit: AuditRecorder
    runner: AgentStepRunner
    next_task: AgentRoleTask
    evidence: tuple[Any, ...] = ()
    intent: Mapping[str, Any] | None = None
    research: Mapping[str, Any] | None = None
    receipts: list[Mapping[str, Any]] = field(default_factory=list)
    status: str = "running"
    result: RecommendationSet | None = None


class RecommendationWorkflowCoordinator:
    """Drive the formal Interpreter→Selector→Reviewer state machine.

    The coordinator stores active payloads only in process memory. Its public
    snapshot exposes hashes and settled receipts, never prompts, model secrets,
    provider credentials, data credentials, or hidden reasoning.
    """

    def __init__(
        self,
        service: RecommenderService,
        *,
        cancel_callback: Callable[[str], object] | None = None,
    ) -> None:
        self._service = service
        self._cancel_callback = cancel_callback
        self._workflows: dict[str, _ActiveRecommendation] = {}

    def begin_recommendation(
        self,
        request: RecommendationCase | Mapping[str, Any],
        *,
        policy: AgentExecutionPolicy | None = None,
        workflow: str = "recommendation",
    ) -> Mapping[str, Any]:
        case = request if isinstance(request, RecommendationCase) else RecommendationCase.from_mapping(request)
        case = _prepare_recommendation_case(case)
        route_name = str(workflow).strip().lower()
        if route_name not in {"recommendation", "professional_report"}:
            raise ValueError("结构推荐状态机仅支持recommendation或professional_report")
        route = RouteDecision(
            route=route_name,
            confidence=1.0,
            reason="App Agent显式请求固定结构推荐Workflow",
            fixed_recommendation_workflow=True,
            agent_policy="capability_based",
        )
        run_id = case.run_id or f"recommend_{uuid4().hex[:12]}"
        missing = missing_required_constraints(case.confirmed_constraints)
        if missing:
            result = RecommendationSet(
                schema=RECOMMENDATION_SET_SCHEMA,
                task_id=case.task_id,
                run_id=run_id,
                analysis_case_id=case.analysis_case_id,
                catalog_version=case.catalog_version,
                route=route.route,
                workflow_mode="single_agent",
                status="pending_question",
                primary_candidate_id=None,
                candidates=(),
                missing_information=missing,
                next_question=question_for_missing_constraints(missing),
                requested_outputs=case.requested_outputs,
                requested_candidate_count=_requested_candidate_count(case),
                returned_candidate_count=0,
            )
            return {"status": "completed", "next_task": None, "recommendation_set": result.to_dict()}

        selected_policy = policy or AgentExecutionPolicy()
        capability = self._service.agent_port.capability()
        mode = self._select_mode(capability, selected_policy)
        workflow_ref = f"recommendation-{uuid4().hex}"
        audit = AuditRecorder(mode)
        audit.append(
            "agent_mode", "selected", agent_role=None,
            input_value={
                "configured": self._service.config.agent_mode,
                "model_id": capability.model_id,
                "structured_output": capability.structured_output,
                "tool_calling": capability.tool_calling,
                "multi_agent": capability.multi_agent,
                "max_parallel_agents": capability.max_parallel_agents,
            },
            output_value={"selected": mode},
            detail={"configured": self._service.config.agent_mode, "selected": mode},
        )
        audit.append(
            "route", "complete", agent_role=None,
            input_value={"prompt": case.prompt},
            output_value=route.to_dict(),
        )
        audit.append(
            "review_policy", "selected", agent_role=None,
            input_value={"policy_id": self._service.review_policy_id},
            output_value={"policy_id": "standard-review", "execution": "mode_builtin_terminal_role"},
            detail={
                "additional_agent_run": False,
                "mode1_terminal_role": "Reviewer",
                "mode2_terminal_role": "Reviewer",
                "mode3_terminal_role": "Moderator",
                "mode4_terminal_role": "Reviewer",
            },
        )
        audit.append(
            "mode1_role_mapping", "complete", agent_role=None,
            input_value={"wire_roles": list(MODE_ONE_ROLE_NAMES)},
            output_value={"display_names": dict(MODE_ONE_ROLE_NAMES)},
            detail={"model_slots_use_wire_roles": True},
        )
        runner = AgentStepRunner(port=self._service.agent_port, workflow_mode=mode, audit=audit)
        task = self._task(
            workflow_ref,
            runner,
            "Interpreter",
            {
                "prompt": case.prompt,
                "confirmed_constraints": dict(case.confirmed_constraints),
                "rule": "仅把用户原文或confirmed_constraints中的事实标为已确认；一次最多生成一个next_question。",
            },
        )
        self._workflows[workflow_ref] = _ActiveRecommendation(
            workflow_ref=workflow_ref,
            case=case,
            route=route,
            run_id=run_id,
            mode=mode,
            audit=audit,
            runner=runner,
            next_task=task,
        )
        return {"status": "running", "workflow_ref": workflow_ref, "next_task": task.to_dict()}

    def submit_agent_result(
        self,
        workflow_ref: str,
        step: AgentStepResult | Mapping[str, Any],
    ) -> Mapping[str, Any]:
        active = self._active(workflow_ref)
        normalized = step if isinstance(step, AgentStepResult) else AgentStepResult.from_mapping(step)
        task = active.next_task
        result = active.runner.accept(task.role, task.port_role, task.request, normalized)
        active.receipts.append(normalized.receipt.to_dict())

        if task.role == "Interpreter":
            active.intent = result
            queries = tuple(str(item).strip() for item in result.get("research_queries", ()) if str(item).strip())
            if self._service.knowledge_port is None:
                raise RecommenderUnavailable("结构推荐需要Knowledger正式端口")
            active.evidence = retrieve_evidence(
                self._service.knowledge_port,
                catalog_version=active.case.catalog_version,
                queries=queries,
            )
            active.audit.append(
                "evidence", "complete", agent_role=None,
                input_value={"queries": queries},
                output_value={
                    "evidence_ids": [item.evidence_id for item in active.evidence],
                    "catalog_version": active.case.catalog_version,
                },
            )
            active.next_task = self._task(
                active.workflow_ref,
                active.runner,
                "Selector",
                {
                    "case": active.case.to_dict(),
                    "intent": dict(result),
                    "evidence": [item.to_dict(include_excerpt=True) for item in active.evidence],
                    "max_candidates": min(
                        _requested_candidate_count(active.case),
                        self._service.config.max_candidates,
                    ),
                },
            )
            return self._running(active)

        if task.role == "Selector":
            active.research = result
            active.next_task = self._task(
                active.workflow_ref,
                active.runner,
                "Reviewer",
                {
                    "case": active.case.to_dict(),
                    "proposals": result.get("proposals", ()),
                    "rule": "只审阅已有product_id；新增product_id将导致整步失败。",
                },
            )
            return self._running(active)

        if task.role != "Reviewer" or active.research is None:
            raise RuntimeError("推荐状态机阶段无效")
        active.result = self._service._complete_mode_one(
            active.case,
            active.route,
            active.run_id,
            active.mode,
            active.audit,
            active.runner,
            evidence=active.evidence,
            research=active.research,
            critic=result,
        )
        active.status = "completed"
        return {
            "status": "completed",
            "workflow_ref": active.workflow_ref,
            "next_task": None,
            "recommendation_set": active.result.to_dict(),
        }

    def cancel_recommendation(self, workflow_ref: str) -> Mapping[str, Any]:
        active = self._active(workflow_ref)
        if self._cancel_callback is not None:
            self._cancel_callback(active.workflow_ref)
        active.status = "cancelled"
        return {"status": "cancelled", "workflow_ref": active.workflow_ref}

    def snapshot(self, workflow_ref: str) -> Mapping[str, Any]:
        active = self._workflows.get(str(workflow_ref))
        if active is None:
            raise KeyError("未知推荐workflow_ref")
        return {
            "workflow_ref": active.workflow_ref,
            "task_id": active.case.task_id,
            "catalog_version": active.case.catalog_version,
            "workflow_mode": active.mode,
            "status": active.status,
            "next_role": active.next_task.role if active.status == "running" else None,
            "receipts": [dict(item) for item in active.receipts],
        }

    @staticmethod
    def _select_mode(capability: ModelCapability, policy: AgentExecutionPolicy) -> str:
        if not capability.structured_output:
            raise RecommenderUnavailable("当前宿主不支持结构化输出")
        if policy.preferred_mode == "single":
            return "single_agent"
        if capability.supports_multi_agent_workflow:
            return "multi_agent"
        if policy.preferred_mode == "multi":
            raise RecommenderUnavailable("当前宿主不能提供独立子Agent，无法执行必须多Agent的推荐。")
        if not policy.allow_single_agent_fallback:
            raise RecommenderUnavailable("当前宿主不能提供独立子Agent，且本工作流不允许单Agent降级。")
        return "single_agent"

    @staticmethod
    def _task(
        workflow_ref: str,
        runner: AgentStepRunner,
        role: str,
        payload: Mapping[str, Any],
    ) -> AgentRoleTask:
        port_role, request = runner.build_request(role, payload)
        return AgentRoleTask(workflow_ref, role, port_role, request)

    @staticmethod
    def _running(active: _ActiveRecommendation) -> Mapping[str, Any]:
        return {
            "status": "running",
            "workflow_ref": active.workflow_ref,
            "next_task": active.next_task.to_dict(),
        }

    def _active(self, workflow_ref: str) -> _ActiveRecommendation:
        active = self._workflows.get(str(workflow_ref))
        if active is None:
            raise KeyError("未知推荐workflow_ref")
        if active.status != "running":
            raise RuntimeError(f"推荐工作流已经{active.status}")
        return active


def begin_recommendation(
    coordinator: RecommendationWorkflowCoordinator,
    case: RecommendationCase | Mapping[str, Any],
    *,
    policy: AgentExecutionPolicy | None = None,
) -> Mapping[str, Any]:
    return coordinator.begin_recommendation(case, policy=policy)


def submit_agent_result(
    coordinator: RecommendationWorkflowCoordinator,
    workflow_ref: str,
    step: AgentStepResult | Mapping[str, Any],
) -> Mapping[str, Any]:
    return coordinator.submit_agent_result(workflow_ref, step)


def cancel_recommendation(
    coordinator: RecommendationWorkflowCoordinator,
    workflow_ref: str,
) -> Mapping[str, Any]:
    return coordinator.cancel_recommendation(workflow_ref)
