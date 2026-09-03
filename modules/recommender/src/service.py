"""Recommender唯一领域入口与自然语言路由。"""

from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import math
from threading import Lock
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .agent_steps import AuditRecorder, AgentStepRunner, MODE_ONE_ROLE_NAMES, canonical_hash
from .candidate_builder import build_candidates
from .constraint_ranking import (
    apply_reviewer_verdict,
    parse_reviewer_ranking_verdict,
    parse_specifier_ranking_spec,
    rank_candidates,
    required_modules,
)
from .config import RecommenderConfig
from .evidence_retriever import retrieve_evidence
from .executor import ALLOWED_MODULES, execute
from .interaction import (
    delivery_preferences_from_text,
    merge_confirmed_constraints,
    missing_required_constraints,
    question_for_missing_constraints,
    requested_candidate_count_from_text,
)
from .intent_router import route_intent
from .models import CandidateContract, EvaluationRecord, ModelCapability, RECOMMENDATION_SET_SCHEMA, RecommendationCase, RecommendationCandidate, RecommendationSet, RouteDecision, RecommendationValidationError, public_text
from .ports import AgentPort, AgentStepResult, HttpAgentPort, HttpEndpoint, HttpKnowledgePort, HttpToolPort, KnowledgePort, ToolPort
from .product_trader_loop import ProductTraderLoop, attach_host_evaluations


MAX_EVALUATORS = 4
DEFAULT_REQUESTED_CANDIDATE_COUNT = 3
MAX_REQUESTED_CANDIDATE_COUNT = 10

class RecommenderUnavailable(RuntimeError):
    """必要正式端口未注入或不可达。"""


class RecommendationInputRequired(RuntimeError):
    """A contract-specific input is required before any pricing dispatch."""

    def __init__(self, field: str, question: str) -> None:
        super().__init__(question)
        self.field = str(field)
        self.question = str(question)


_ROUTE_TOOLS = {
    "chat": frozenset(),
    "knowledge": frozenset({"knowledger"}),
    "data": frozenset({"knowledger", "datafetcher"}),
    "direct_execution": frozenset({"datafetcher", "payoffer", "pricer", "backtester"}),
    "existing_report": frozenset({"reporter"}),
    "maintenance": frozenset(),
    "freeform": frozenset({"knowledger", *ALLOWED_MODULES}),
}

_CONTRACT_CONFIRMATION_QUESTION = (
    "以上为拟采用合同条款，是否按此继续？如需调整，请一次说明需要修改的期限、执行水平、"
    "票息或参与率、障碍、观察方式、结算方式或其他适用条款。"
)


def capability(config: RecommenderConfig | None = None) -> dict[str, Any]:
    config = config or RecommenderConfig.from_environment()
    preset_roles = {
        "sequential-deliberation": ("Interpreter", "Selector", "Reviewer"),
        "product-trader-loop": ("Structurer", "Trader", "Reviewer"),
        "independent-council": ("Framer", "Matcher", "Hedger", "Moderator"),
        "constraint-ranking": ("Specifier", "Generator", "Evaluator", "Reviewer"),
    }
    configured = {
        "model_gateway": bool(config.model_gateway_url),
        "knowledger": bool(config.knowledger_url),
        "tool_gateway": bool(config.tool_gateway_url),
    }
    return {
        "ok": all(configured.values()),
        "module": "recommender",
        "status": "ready" if all(configured.values()) else "unconfigured",
        "schema": RECOMMENDATION_SET_SCHEMA,
        "actions": ["recommend", "recommend_fixed"],
        "routes": ["chat", "knowledge", "data", "direct_execution", "recommendation", "professional_report", "existing_report", "maintenance", "freeform"],
        "agent_roles": list(preset_roles[config.multi_agent_preset]),
        "agent_role_display_names": {
            "Interpreter": "Interpreter",
            "Selector": "Selector",
            "Reviewer": "Reviewer",
            "Evaluator": "Evaluator",
        },
        "external_ports": configured,
        "fully_configured": all(configured.values()),
    }


class RecommenderService:
    def __init__(
        self,
        *,
        agent_port: AgentPort,
        knowledge_port: KnowledgePort | None = None,
        tool_port: ToolPort | None = None,
        config: RecommenderConfig | None = None,
        review_policy_id: str = "standard-review",
    ) -> None:
        self.agent_port = agent_port
        self.knowledge_port = knowledge_port
        self.tool_port = tool_port
        self.config = config or RecommenderConfig()
        self.review_policy_id = str(review_policy_id).strip().lower()

    def recommend(self, request: RecommendationCase | Mapping[str, Any]) -> Mapping[str, Any]:
        result, _ = self._recommend_with_domain(request)
        return result

    def _recommend_with_domain(
        self,
        request: RecommendationCase | Mapping[str, Any],
        *,
        workflow: str | None = None,
    ) -> tuple[Mapping[str, Any], RecommendationSet | None]:
        """执行推荐，并仅在正式Tool调用链内保留领域对象供安全投影。"""

        case = request if isinstance(request, RecommendationCase) else RecommendationCase.from_mapping(request)
        case = _bind_requested_candidate_count(
            case,
            _requested_candidate_count_from_request(request, case),
        )
        if workflow is not None:
            route_name = str(workflow).strip().lower()
            if route_name not in {"recommendation", "professional_report"}:
                raise ValueError("固定Recommender workflow仅支持recommendation或professional_report")
            requested_deliveries, declined_deliveries = delivery_preferences_from_text(case.prompt.lower())
            if route_name == "professional_report" and declined_deliveries and not requested_deliveries:
                route_name = "recommendation"
            route = RouteDecision(
                route=route_name,
                confidence=1.0,
                reason="App Agent显式请求固定结构推荐Workflow",
                fixed_recommendation_workflow=True,
                agent_policy="capability_based",
            )
        else:
            route = route_intent(case.prompt)
        if route.fixed_recommendation_workflow:
            result = self._run_recommendation(case, route=route)
            return {"route": route.to_dict(), "recommendation_set": result.to_dict()}, result
        return {"route": route.to_dict(), "freeform": self._run_freeform(case, route)}, None

    def recommend_fixed(
        self,
        request: RecommendationCase | Mapping[str, Any],
        *,
        workflow: str = "recommendation",
    ) -> Mapping[str, Any]:
        """Run only the fixed recommendation workflow.

        App callers use this explicit entry point rather than ``recommend``:
        their natural-language prompt must never be re-routed to Recommender's
        standalone freeform agent.
        """

        result, _ = self._recommend_with_domain(request, workflow=workflow)
        return result

    def _run_recommendation(self, case: RecommendationCase, *, route: RouteDecision) -> RecommendationSet:
        run_id = case.run_id or f"recommend_{uuid4().hex[:12]}"
        case = _prepare_recommendation_case(case)
        requested_candidate_count = _requested_candidate_count(case)
        missing = missing_required_constraints(case.confirmed_constraints)
        if missing:
            audit = AuditRecorder("single_agent")
            audit.append("route", "complete", agent_role=None, input_value={"prompt": case.prompt}, output_value=route.to_dict())
            audit.append(
                "clarification", "pending", agent_role=None,
                input_value={"confirmed_constraints": dict(case.confirmed_constraints)},
                output_value={"missing": missing}, detail={"rule": "全部关键缺口一次合并追问"},
            )
            return RecommendationSet(
                schema=RECOMMENDATION_SET_SCHEMA, task_id=case.task_id, run_id=run_id,
                analysis_case_id=case.analysis_case_id, catalog_version=case.catalog_version, route=route.route,
                workflow_mode="single_agent", status="pending_question", primary_candidate_id=None, candidates=(),
                missing_information=missing, next_question=question_for_missing_constraints(missing),
                requested_outputs=case.requested_outputs,
                requested_candidate_count=requested_candidate_count,
                returned_candidate_count=0,
                audit_trail=tuple(audit.events),
            )
        try:
            model_capability = self.agent_port.capability()
        except Exception as error:
            audit = AuditRecorder("single_agent")
            return self._unavailable_set(case, route, run_id, "single_agent", audit, error)
        if not model_capability.structured_output:
            audit = AuditRecorder("single_agent")
            return self._unavailable_set(
                case, route, run_id, "single_agent", audit,
                RecommenderUnavailable("当前模型不支持结构化输出，不能生成严格RecommendationSet"),
            )
        try:
            initial_mode = self._workflow_mode(model_capability)
        except RecommenderUnavailable as error:
            audit = AuditRecorder("single_agent")
            return self._unavailable_set(case, route, run_id, "single_agent", audit, error)
        audit = AuditRecorder(initial_mode)
        audit.append(
            "agent_mode", "selected", agent_role=None,
            input_value={
                "configured": self.config.agent_mode,
                "model_id": model_capability.model_id,
                "structured_output": model_capability.structured_output,
                "tool_calling": model_capability.tool_calling,
                "multi_agent": model_capability.multi_agent,
                "max_parallel_agents": model_capability.max_parallel_agents,
            },
            output_value={"selected": initial_mode},
            detail={"configured": self.config.agent_mode, "selected": initial_mode},
        )
        audit.append("route", "complete", agent_role=None, input_value={"prompt": case.prompt}, output_value=route.to_dict())
        review_error = self._review_policy_error()
        if review_error is not None:
            return self._unavailable_set(case, route, run_id, initial_mode, audit, review_error)
        audit.append(
            "review_policy", "selected", agent_role=None,
            input_value={"policy_id": self.review_policy_id},
            output_value={"policy_id": "standard-review", "execution": "mode_builtin_terminal_role"},
            detail={
                "additional_agent_run": False,
                "mode1_terminal_role": "Reviewer",
                "mode2_terminal_role": "Reviewer",
                "mode3_terminal_role": "Moderator",
                "mode4_terminal_role": "Reviewer",
            },
        )
        if self.config.multi_agent_preset == "sequential-deliberation":
            audit.append(
                "mode1_role_mapping", "complete", agent_role=None,
                input_value={"wire_roles": list(MODE_ONE_ROLE_NAMES)},
                output_value={"display_names": dict(MODE_ONE_ROLE_NAMES)},
                detail={"model_slots_use_wire_roles": True},
            )
        try:
            if self.config.multi_agent_preset == "product-trader-loop":
                return self._run_product_trader_loop(case, route, run_id, initial_mode, audit)
            if self.config.multi_agent_preset == "constraint-ranking":
                return self._run_constraint_ranking(case, route, run_id, initial_mode, audit)
            return self._run_fixed(case, route, run_id, initial_mode, audit)
        except RecommendationInputRequired as required:
            audit.append(
                "clarification", "pending", agent_role=None,
                input_value={"confirmed_constraints": dict(case.confirmed_constraints)},
                output_value={"missing": [required.field]},
                detail={"reason": required.question},
            )
            return RecommendationSet(
                schema=RECOMMENDATION_SET_SCHEMA, task_id=case.task_id, run_id=run_id,
                analysis_case_id=case.analysis_case_id, catalog_version=case.catalog_version, route=route.route,
                workflow_mode=initial_mode, status="pending_question", primary_candidate_id=None, candidates=(),
                missing_information=(required.field,), next_question=required.question,
                requested_outputs=case.requested_outputs,
                requested_candidate_count=_requested_candidate_count(case),
                returned_candidate_count=0,
                audit_trail=tuple(audit.events),
            )
        except Exception as error:
            # Capability selection happens before the first role starts. Once
            # a host declares independent child sessions, a failed multi-Agent
            # run remains failed; silently replaying it as one model changes
            # the selected topology and can duplicate future tool effects.
            return self._unavailable_set(case, route, run_id, initial_mode, audit, error)

    def _review_policy_error(self) -> RecommenderUnavailable | None:
        """Keep review-policy selection fail-closed without importing App runtime.

        The only published policy is standard review, whose meaning is to use
        the selected Mode's terminal role.  It never creates a separate
        Reviewer call.  Other identifiers, including adversarial review, are
        intentionally rejected before any child AgentRun is started.
        """

        if self.review_policy_id == "standard-review":
            return None
        return RecommenderUnavailable("当前ReviewPolicy未启用，不能启动Recommender工作流。")

    def _workflow_mode(self, capability: ModelCapability) -> str:
        if self.config.agent_mode == "single":
            return "single_agent"
        if self.config.agent_mode == "multi":
            if not capability.supports_multi_agent_workflow:
                raise RecommenderUnavailable("当前宿主不能提供独立子Agent，无法执行必须多Agent的推荐。")
            return "multi_agent"
        return "multi_agent" if capability.supports_multi_agent_workflow else "single_agent"

    def _run_fixed(
        self,
        case: RecommendationCase,
        route: RouteDecision,
        run_id: str,
        mode: str,
        audit: AuditRecorder,
        *,
        agent_port: AgentPort | None = None,
    ) -> RecommendationSet:
        requested_candidate_count = _requested_candidate_count(case)
        candidate_limit = min(requested_candidate_count, self.config.max_candidates)
        runner = AgentStepRunner(
            port=agent_port or self.agent_port,
            workflow_mode="multi_agent" if mode == "multi_agent" else "single_agent",
            audit=audit,
        )
        council = mode == "multi_agent" and self.config.multi_agent_preset == "independent-council"
        framing_role = "Framer" if council else "Interpreter"
        framing = runner.run(framing_role, {
            "prompt": case.prompt,
            "confirmed_constraints": dict(case.confirmed_constraints),
            "rule": "仅把用户原文或confirmed_constraints中的事实标为已确认；一次最多生成一个next_question。",
        })
        queries = _strings(framing.get("research_queries", ()))[: self.config.max_research_queries]
        if self.knowledge_port is None:
            raise RecommenderUnavailable("结构推荐需要Knowledger正式端口")
        evidence = retrieve_evidence(
            self.knowledge_port,
            catalog_version=case.catalog_version,
            queries=queries or _research_queries(case),
        )
        audit.append("evidence", "complete", agent_role=None, input_value={"queries": queries},
                     output_value={"evidence_ids": [item.evidence_id for item in evidence], "catalog_version": case.catalog_version})
        research_payload = {
            "case": case.to_dict(),
            "intent": dict(framing),
            "evidence": [item.to_dict(include_excerpt=True) for item in evidence],
            "max_candidates": candidate_limit,
        }
        if council:
            role_outputs = runner.run_named({
                "Matcher": {
                    **research_payload,
                    "framing": dict(framing),
                    "role_focus": "匹配用户目标、方向与条款约束。",
                },
                "Hedger": {
                    **research_payload,
                    "framing": dict(framing),
                    "role_focus": "独立识别适配边界、风险与不适合情形。",
                },
            })
            if bool(getattr(self.tool_port, "child_managed_evaluation", False)):
                return self._complete_child_managed_council(
                    case,
                    route,
                    run_id,
                    mode,
                    audit,
                    runner,
                    evidence=evidence,
                    framing=framing,
                    role_outputs=role_outputs,
                )
            moderated = runner.run("Moderator", {
                "case": case.to_dict(),
                "framing": dict(framing),
                "matcher": dict(role_outputs["Matcher"]),
                "hedger": dict(role_outputs["Hedger"]),
                "evidence": [item.to_dict(include_excerpt=True) for item in evidence],
                "max_candidates": candidate_limit,
                "rule": "只能汇总已有候选及受控证据；每个候选均须给出对应reviews，不得计算或编造财务指标。",
            })
            research = {"proposals": moderated["proposals"]}
            critic = {"reviews": moderated["reviews"]}
            audit.append(
                "council", "complete", agent_role=None, input_value=research_payload,
                output_value={"proposals": research["proposals"], "reviews": critic["reviews"]},
                detail={"agent_runs": 4, "roles": ["Framer", "Matcher", "Hedger", "Moderator"]},
            )
        else:
            research = runner.run("Selector", research_payload)
            critic = runner.run("Reviewer", {
                "case": case.to_dict(),
                "proposals": research.get("proposals", ()),
                "rule": "只审阅已有product_id；新增product_id将导致整步失败。",
            })
        return self._complete_mode_one(
            case, route, run_id, mode, audit, runner,
            evidence=evidence, research=research, critic=critic,
        )

    def _complete_child_managed_council(
        self,
        case: RecommendationCase,
        route: RouteDecision,
        run_id: str,
        mode: str,
        audit: AuditRecorder,
        runner: AgentStepRunner,
        *,
        evidence: Sequence[Any],
        framing: Mapping[str, Any],
        role_outputs: Mapping[str, Mapping[str, Any]],
    ) -> RecommendationSet:
        """Keep Mode3 branches independent through candidate and FactRef settlement."""

        candidate_limit = min(_requested_candidate_count(case), self.config.max_candidates)
        register_plan = getattr(self.tool_port, "register_candidate_plan", None)
        resolve_evaluation = getattr(self.tool_port, "resolve_candidate_evaluation", None)
        if not callable(register_plan) or not callable(resolve_evaluation):
            raise RecommenderUnavailable("Mode3缺少独立分支候选计划与FactRef解析端口")
        modules = _mode3_required_modules(case)
        term_overrides = case.confirmed_constraints.get("term_overrides", {})
        term_overrides = dict(term_overrides) if isinstance(term_overrides, Mapping) else {}
        branch_candidates: dict[str, tuple[RecommendationCandidate, ...]] = {}
        rejected: list[Mapping[str, Any]] = []
        for role in ("Matcher", "Hedger"):
            output = role_outputs[role]
            candidates, branch_rejected = build_candidates(
                {"proposals": output.get("proposals", ())},
                {"reviews": _accepting_reviews(output.get("proposals", ()))},
                evidence=evidence,
                run_id=f"{run_id}.{role.lower()}",
                max_candidates=candidate_limit,
                confirmed_constraints=case.confirmed_constraints,
            )
            if not candidates:
                raise RecommendationValidationError(f"Mode3{role}分支未形成通过证据门禁的候选")
            branch_candidates[role] = candidates
            rejected.extend(branch_rejected)

        settled_by_version: dict[str, RecommendationCandidate] = {}
        branch_results: Mapping[str, Mapping[str, Any]] = {}
        if modules:
            followups: dict[str, Mapping[str, Any]] = {}
            for role, candidates in branch_candidates.items():
                candidate_rows = []
                for candidate in candidates:
                    version_id = str(candidate.candidate_version_id)
                    fingerprints = {
                        module: canonical_hash({
                            "mode": "independent-council",
                            "role": role,
                            "candidate_version_id": version_id,
                            "module": module,
                        })
                        for module in modules
                    }
                    register_plan(
                        candidate={**candidate.to_dict(), "catalog_version": case.catalog_version},
                        confirmed_constraints=case.confirmed_constraints,
                        modules=modules,
                        term_overrides=term_overrides,
                        candidate_version_id=version_id,
                        round_no=1,
                        input_fingerprints=fingerprints,
                    )
                    candidate_rows.append({
                        "product_id": candidate.product_id,
                        "candidate_key": candidate.candidate_key,
                        "candidate_version_id": version_id,
                        "required_modules": list(modules),
                    })
                followups[role] = {
                    "candidate_versions": candidate_rows,
                    "rule": (
                        "逐一调用每个CandidateVersion的required_modules。工具结果回到本分支Session后，"
                        "分别返回建议、风险结论及全部used_fact_refs。"
                    ),
                }
            branch_results = runner.run_named(followups)

        moderator_rows: list[Mapping[str, Any]] = []
        for role, candidates in branch_candidates.items():
            output_rows = branch_results.get(role, {}).get("branch_results", ()) if modules else ()
            by_version = {
                str(row.get("candidate_version_id", "")): row
                for row in output_rows
                if isinstance(row, Mapping)
            }
            if modules and set(by_version) != {str(item.candidate_version_id) for item in candidates}:
                raise RecommendationValidationError(f"Mode3{role}未覆盖本分支全部CandidateVersion")
            for candidate in candidates:
                version_id = str(candidate.candidate_version_id)
                fact_refs: tuple[str, ...] = ()
                settled = candidate
                branch_row: Mapping[str, Any] = by_version.get(version_id, {})
                if modules:
                    host_evaluation = resolve_evaluation(version_id)
                    if not isinstance(host_evaluation, Mapping):
                        raise RecommendationValidationError("Mode3分支工具结果必须解析为Host事实对象")
                    settled = _attach_candidate_evaluation(candidate, host_evaluation)
                    fact_refs = _fact_refs_from_verified_metrics(host_evaluation)
                    used = tuple(_fact_ref(item, f"{role}.used_fact_refs") for item in branch_row.get("used_fact_refs", ()))
                    if set(used) != set(fact_refs):
                        raise RecommendationValidationError(f"Mode3{role}必须逐一引用本候选FactRef")
                settled_by_version[version_id] = settled
                moderator_rows.append({
                    **_review_candidate_projection(settled, fact_refs=fact_refs),
                    "branch": role,
                    "branch_recommendation": str(branch_row.get("recommendation", settled.reason)),
                    "risk_conclusion": str(branch_row.get("risk_conclusion", "；".join(settled.main_risks))),
                })

        moderated = runner.run("Moderator", {
            "case": case.to_dict(),
            "framing": dict(framing),
            "branch_candidates": moderator_rows,
            "max_candidates": candidate_limit,
            "rule": (
                "只能从branch_candidates选择CandidateVersion并说明冲突；不得重新计算、改写FactRef或"
                "读取任一分支的临时Reasoning。"
            ),
        })
        selected_ids = tuple(str(item) for item in moderated.get("selected_candidate_version_ids", ()))
        if len(set(selected_ids)) != len(selected_ids) or any(item not in settled_by_version for item in selected_ids):
            raise RecommendationValidationError("Mode3 Moderator选择了未知或重复CandidateVersion")
        selected = tuple(
            replace(settled_by_version[version_id], rank=rank)
            for rank, version_id in enumerate(selected_ids[:candidate_limit], start=1)
        )
        if not selected:
            raise RecommendationValidationError("Mode3 Moderator未选择可交付候选")
        audit.append(
            "council", "complete", agent_role=None,
            input_value={"branches": moderator_rows},
            output_value={"selected_candidate_version_ids": list(selected_ids)},
            detail={
                "roles": ["Framer", "Matcher", "Hedger", "Moderator"],
                "branch_sessions_independent": True,
                "financial_modules_owned_by_branches": bool(modules),
            },
        )
        return self._finalize_candidates(
            case, route, run_id, mode, audit, runner, selected, tuple(rejected),
        )

    def _complete_mode_one(
        self,
        case: RecommendationCase,
        route: RouteDecision,
        run_id: str,
        mode: str,
        audit: AuditRecorder,
        runner: AgentStepRunner,
        *,
        evidence: Sequence[Any],
        research: Mapping[str, Any],
        critic: Mapping[str, Any],
    ) -> RecommendationSet:
        """Apply the sole deterministic Mode 1 aggregation and approval path."""

        candidate_limit = min(_requested_candidate_count(case), self.config.max_candidates)
        candidates, rejected = build_candidates(
            research, critic, evidence=evidence, run_id=run_id, max_candidates=candidate_limit,
            confirmed_constraints=case.confirmed_constraints,
        )
        audit.append(
            "aggregation", "complete", agent_role=None,
            input_value={"research": research, "critic": critic},
            output_value={"candidate_ids": [item.candidate_id for item in candidates], "rejected": rejected},
        )
        if not candidates:
            return RecommendationSet(
                schema=RECOMMENDATION_SET_SCHEMA, task_id=case.task_id, run_id=run_id,
                analysis_case_id=case.analysis_case_id, catalog_version=case.catalog_version, route=route.route,
                workflow_mode=mode, status="unavailable", primary_candidate_id=None, candidates=(), rejected=rejected,
                requested_outputs=case.requested_outputs,
                requested_candidate_count=_requested_candidate_count(case),
                returned_candidate_count=0,
                limitations=("受控证据与反证门禁后没有可输出候选。",), audit_trail=tuple(audit.events),
            )
        return self._finalize_candidates(case, route, run_id, mode, audit, runner, candidates, rejected)

    def _run_product_trader_loop(
        self,
        case: RecommendationCase,
        route: RouteDecision,
        run_id: str,
        mode: str,
        audit: AuditRecorder,
    ) -> RecommendationSet:
        """Run Mode2 as a bounded Structurer↔Trader calculation loop."""

        if mode != "multi_agent":
            raise RecommenderUnavailable("Mode2需要App多Agent运行时，禁止降级为单Agent替代拓扑")
        if self.knowledge_port is None:
            raise RecommenderUnavailable("Mode2需要Knowledger正式端口")
        child_managed = bool(getattr(self.tool_port, "child_managed_evaluation", False))
        evaluator = getattr(self.tool_port, "evaluate_candidate", None)
        register_plan = getattr(self.tool_port, "register_candidate_plan", None)
        resolve_evaluation = getattr(self.tool_port, "resolve_candidate_evaluation", None)
        if child_managed:
            if not callable(register_plan) or not callable(resolve_evaluation):
                raise RecommenderUnavailable("Mode2缺少Child Agent候选计划与FactRef解析端口")
        elif not callable(evaluator):
            raise RecommenderUnavailable("Mode2需要Host受控候选评估端口")
        runner = AgentStepRunner(port=self.agent_port, workflow_mode="multi_agent", audit=audit)
        candidate_limit = min(_requested_candidate_count(case), self.config.max_candidates)
        evidence = retrieve_evidence(
            self.knowledge_port,
            catalog_version=case.catalog_version,
            queries=_research_queries(case)[: self.config.max_research_queries],
        )
        evidence_payload = [item.to_dict(include_excerpt=True) for item in evidence]
        structurer = runner.run("Structurer", {
            "case": case.to_dict(),
            "evidence": evidence_payload,
            "max_candidates": candidate_limit,
            "round_no": 1,
            "rule": "提出候选及每个候选需要验证的模块；计算结果只能由Host返回。",
        })
        candidates, rejected = build_candidates(
            {"proposals": structurer.get("proposals", ())},
            {"reviews": _accepting_reviews(structurer.get("proposals", ()))},
            evidence=evidence,
            run_id=run_id,
            max_candidates=candidate_limit,
            confirmed_constraints=case.confirmed_constraints,
        )
        if not candidates:
            return self._empty_recommendation(case, route, run_id, mode, audit, rejected, "Mode2没有通过证据门禁的候选。")
        loop = ProductTraderLoop.start(candidates)
        accepted: tuple[RecommendationCandidate, ...] = ()
        current_structurer = structurer
        completed_rounds = 0
        maximum_rounds = min(self.config.max_loop_rounds, 2)
        while completed_rounds < maximum_rounds:
            plan = loop.plan_round(_mode2_structurer_plan(loop, current_structurer))
            host_rows: dict[str, Mapping[str, Any]] = {}
            host_fact_rows: dict[str, Mapping[str, Any]] = {}
            trader_rows: list[dict[str, Any]] = []
            evaluation_requests: list[dict[str, Any]] = []
            for item in plan.plans:
                current = item.candidate.candidate
                requests = item.requests()
                evaluation_requests.append({
                    "candidate": {**current.to_dict(), "catalog_version": case.catalog_version},
                    "confirmed_constraints": case.confirmed_constraints,
                    "modules": item.modules,
                    "term_overrides": item.candidate.term_overrides,
                    "candidate_version_id": str(current.candidate_version_id),
                    "round_no": plan.round_no,
                    "input_fingerprints": {
                        request.module: request.input_fingerprint for request in requests
                    },
                })
            if child_managed:
                for request in evaluation_requests:
                    register_plan(**request)
                for item, request in zip(plan.plans, evaluation_requests, strict=True):
                    current = item.candidate.candidate
                    trader_rows.append({
                        "product_id": current.product_id,
                        "candidate_key": current.candidate_key,
                        "candidate_version_id": current.candidate_version_id,
                        "required_modules": list(request["modules"]),
                    })
            else:
                evaluations = _bounded_evaluations(evaluator, evaluation_requests)
                for item, evaluation in zip(plan.plans, evaluations, strict=True):
                    current = item.candidate.candidate
                    if not isinstance(evaluation, Mapping):
                        raise RecommendationValidationError("Mode2 Host候选计算必须返回对象")
                    host_rows[str(current.candidate_key)] = _host_attachment(evaluation)
                    host_fact_rows[str(current.candidate_key)] = dict(evaluation)
                    trader_rows.append({
                        "product_id": current.product_id,
                        "candidate_version_id": current.candidate_version_id,
                        "module_statuses": dict(evaluation.get("module_statuses", {})),
                        "verified_metrics": _verified_metric_projection(evaluation),
                        "display_terms": list(evaluation.get("display_terms", ())),
                    })
            trader = runner.run("Trader", {
                "round_no": plan.round_no,
                "candidates": trader_rows,
                "rule": (
                    "逐一调用每个候选required_modules对应的业务工具；工具结果回到当前Trader Session后，"
                    "只依据已验证FactRef决定accept或rework，不得新增产品或改写计算事实。"
                    if child_managed else
                    "只基于Host已验证指标决定accept或rework；不得新增产品或改写计算事实。"
                ),
            })
            if child_managed:
                evaluations = [
                    resolve_evaluation(str(item.candidate.candidate.candidate_version_id))
                    for item in plan.plans
                ]
                for item, evaluation in zip(plan.plans, evaluations, strict=True):
                    current = item.candidate.candidate
                    if not isinstance(evaluation, Mapping):
                        raise RecommendationValidationError("Mode2 Child工具结果必须解析为Host事实对象")
                    host_rows[str(current.candidate_key)] = _host_attachment(evaluation)
                    host_fact_rows[str(current.candidate_key)] = dict(evaluation)
            attached = attach_host_evaluations(plan, host_rows)
            transition = loop.apply_trader_output(
                attached,
                _mode2_trader_decisions(attached, trader, host_fact_rows),
            )
            completed_rounds = plan.round_no
            accepted = transition.accepted_candidates
            audit.append(
                "product_trader_round", "complete", agent_role=None,
                input_value={"round_no": plan.round_no, "candidate_count": len(plan.plans)},
                output_value={
                    "accepted": [item.candidate_id for item in accepted],
                    "reworked_candidate_keys": list(transition.reworked_candidate_keys),
                },
                detail={"bounded_rounds": maximum_rounds},
            )
            if transition.next_loop is None:
                break
            loop = transition.next_loop
            current_structurer = runner.run("Structurer", {
                "case": case.to_dict(),
                "evidence": evidence_payload,
                "round_no": plan.round_no + 1,
                "current_candidates": [item.to_dict() for item in loop.candidates if item.status == "open"],
                "trader_feedback": dict(trader),
                "rule": "仅重构Trader退回的现有候选，并采用受控term_adjustments；不得新增产品。",
            })
        if not accepted or transition.next_loop is not None:
            raise RecommendationValidationError("Mode2在两轮内未形成全部可接受候选")
        reviewer = runner.run("Reviewer", {
            "mode": "product-trader-loop",
            "candidates": [_review_candidate_projection(item) for item in accepted],
            "rounds": completed_rounds,
            "rule": "只能整体批准或拒绝，不得改写候选版本或模块事实。",
        })
        verdict = parse_reviewer_ranking_verdict(reviewer)
        if not verdict.approved:
            return self._empty_recommendation(
                case, route, run_id, mode, audit, rejected, verdict.reason or "Mode2终审未通过。",
            )
        audit.append(
            "mode2_review", "approved", agent_role="Reviewer",
            input_value={"candidate_ids": [item.candidate_id for item in accepted]},
            output_value={"decision": "approve"},
        )
        return self._finalize_candidates(case, route, run_id, mode, audit, runner, accepted, rejected)

    def _run_constraint_ranking(
        self,
        case: RecommendationCase,
        route: RouteDecision,
        run_id: str,
        mode: str,
        audit: AuditRecorder,
    ) -> RecommendationSet:
        """Run Mode4 with model-specified rules and deterministic Host ranking."""

        if mode != "multi_agent":
            raise RecommenderUnavailable("Mode4需要App多Agent运行时，禁止降级为单Agent替代拓扑")
        if self.knowledge_port is None:
            raise RecommenderUnavailable("Mode4需要Knowledger正式端口")
        child_managed = bool(getattr(self.tool_port, "child_managed_evaluation", False))
        evaluator = getattr(self.tool_port, "evaluate_candidate", None)
        register_plan = getattr(self.tool_port, "register_candidate_plan", None)
        resolve_evaluation = getattr(self.tool_port, "resolve_candidate_evaluation", None)
        if child_managed:
            if not callable(register_plan) or not callable(resolve_evaluation):
                raise RecommenderUnavailable("Mode4缺少Evaluator候选计划与FactRef解析端口")
        elif not callable(evaluator):
            raise RecommenderUnavailable("Mode4需要Host受控候选评估端口")
        runner = AgentStepRunner(port=self.agent_port, workflow_mode="multi_agent", audit=audit)
        requested_candidate_count = _requested_candidate_count(case)
        candidate_limit = min(requested_candidate_count, self.config.max_candidates)
        specifier = runner.run("Specifier", {
            "prompt": case.prompt,
            "confirmed_constraints": dict(case.confirmed_constraints),
            "allowed_metrics": [
                "premium", "pv_percent", "delta", "gamma", "vega", "theta", "rho",
                "positive_return_rate", "average_contract_settlement_return", "max_loss_contract_settlement_return",
            ],
            "rule": "只转换用户明确表达的硬约束和排序顺序，不得补写阈值。",
        })
        spec = parse_specifier_ranking_spec(specifier.get("ranking_spec", {}))
        queries = _strings(specifier.get("research_queries", ()))[: self.config.max_research_queries]
        evidence = retrieve_evidence(
            self.knowledge_port,
            catalog_version=case.catalog_version,
            queries=queries or _research_queries(case),
        )
        modules = required_modules(spec)
        term_overrides = case.confirmed_constraints.get("term_overrides", {})
        term_overrides = dict(term_overrides) if isinstance(term_overrides, Mapping) else {}
        rejected_rows: list[Mapping[str, Any]] = []
        ranking = None
        evaluated: list[RecommendationCandidate] = []
        metrics_by_version: dict[str, Mapping[str, Mapping[str, float]]] = {}
        evaluator_reviews: dict[str, Mapping[str, Any]] = {}
        fact_refs_by_version: dict[str, tuple[str, ...]] = {}
        generator_feedback: Mapping[str, Any] = {}
        for generation_round in range(1, 3):
            generator = runner.run("Generator", {
                "case": case.to_dict(),
                "ranking_spec": spec.to_dict(),
                "evidence": [item.to_dict(include_excerpt=True) for item in evidence],
                "max_candidates": candidate_limit,
                "generation_round": generation_round,
                "previous_rejections": list(rejected_rows),
                "feedback": dict(generator_feedback),
                "rule": "只从受控证据生成候选，不得输出金融指标或名次；第二轮只修复上一轮被拒原因。",
            })
            candidates, generated_rejected = build_candidates(
                {"proposals": generator.get("proposals", ())},
                {"reviews": _accepting_reviews(generator.get("proposals", ()))},
                evidence=evidence,
                run_id=f"{run_id}.mode4g{generation_round}",
                max_candidates=candidate_limit,
                confirmed_constraints=case.confirmed_constraints,
            )
            rejected_rows.extend(generated_rejected)
            if len(candidates) < requested_candidate_count:
                generator_feedback = {
                    "reason": (
                        "数量不足或候选未通过证据门禁"
                        f"（需要{requested_candidate_count}个，当前{len(candidates)}个）"
                    ),
                    "rejected": list(generated_rejected),
                }
                if generation_round == 2 and not candidates:
                    return self._empty_recommendation(
                        case, route, run_id, mode, audit, tuple(rejected_rows),
                        f"Mode4两轮Generator后仍未形成{requested_candidate_count}个通过证据门禁的候选。",
                        ranking_spec_id=spec.ranking_spec_id,
                        ranking_spec_fingerprint=spec.ranking_spec_fingerprint,
                        ranking_spec=spec.to_dict(),
                    )
                if generation_round == 1:
                    continue

            evaluation_requests: list[dict[str, Any]] = []
            for candidate in candidates:
                version_id = str(candidate.candidate_version_id)
                fingerprints = {
                    module: canonical_hash({
                        "mode": "constraint-ranking", "ranking_spec_id": spec.ranking_spec_id,
                        "candidate_version_id": version_id, "module": module,
                    })
                    for module in modules
                }
                evaluation_requests.append({
                    "candidate": {**candidate.to_dict(), "catalog_version": case.catalog_version},
                    "confirmed_constraints": case.confirmed_constraints,
                    "modules": modules,
                    "term_overrides": term_overrides,
                    "candidate_version_id": version_id,
                    "round_no": 1,
                    "input_fingerprints": fingerprints,
                })
            if child_managed:
                for request in evaluation_requests:
                    register_plan(**request)
                evaluator_payloads = [
                    {
                        "mode": "constraint-ranking",
                        "candidate": {
                            "product_id": candidate.product_id,
                            "candidate_key": candidate.candidate_key,
                            "candidate_version_id": str(candidate.candidate_version_id),
                        },
                        "required_modules": list(modules),
                        "rule": (
                            "逐一调用required_modules对应的业务工具；工具结果回到当前Evaluator Session后，"
                            "返回本CandidateVersion使用的全部FactRef。"
                        ),
                    }
                    for candidate in candidates
                ]
                evaluator_outputs = _bounded_evaluator_agents(
                    runner, evaluator_payloads, child_managed=True,
                )
                host_evaluations = [
                    resolve_evaluation(str(candidate.candidate_version_id))
                    for candidate in candidates
                ]
            else:
                host_evaluations = _bounded_evaluations(evaluator, evaluation_requests)
            evaluated_by_version: dict[str, RecommendationCandidate] = {}
            metrics_by_version = {}
            host_by_version: dict[str, Mapping[str, Any]] = {}
            for candidate, host_evaluation in zip(candidates, host_evaluations, strict=True):
                if not isinstance(host_evaluation, Mapping):
                    raise RecommendationValidationError("Mode4 Host候选计算必须返回对象")
                version_id = str(candidate.candidate_version_id)
                evaluated_candidate = _attach_candidate_evaluation(candidate, host_evaluation)
                evaluated_by_version[version_id] = evaluated_candidate
                host_by_version[version_id] = host_evaluation
                # Always bind metrics to the version from this candidate, not
                # the last version used while constructing the request list.
                metrics_by_version[version_id] = _mode4_metrics(host_evaluation)

            if not child_managed:
                evaluator_payloads = [
                    {
                        "mode": "constraint-ranking",
                        "candidate": {
                            "product_id": candidate.product_id,
                            "candidate_key": candidate.candidate_key,
                            "candidate_version_id": version_id,
                            "module_statuses": dict(host_by_version[version_id].get("module_statuses", {})),
                        },
                        "fact_refs": list(_fact_refs_from_verified_metrics(host_by_version[version_id])),
                        "rule": "只核对当前CandidateVersion对应的Host FactRef及模块状态；不得读取或生成金融数值，不得引用其他CandidateVersion。",
                    }
                    for version_id, candidate in evaluated_by_version.items()
                ]
                evaluator_outputs = _bounded_evaluator_agents(runner, evaluator_payloads)
            approved_versions: set[str] = set()
            for payload, evaluator_output in zip(evaluator_payloads, evaluator_outputs, strict=True):
                version_id = str(payload["candidate"]["candidate_version_id"])
                fact_refs = _fact_refs_from_verified_metrics(host_by_version[version_id])
                approved, projection = _validate_mode4_evaluator_result(
                    evaluator_output, version_id, fact_refs,
                )
                evaluator_reviews[version_id] = projection
                if approved:
                    approved_versions.add(version_id)
                else:
                    rejected_rows.append({
                        "candidate_version_id": version_id,
                        "reason": str(projection.get("reason") or "Evaluator未确认该CandidateVersion的FactRef"),
                        "source": "evaluator",
                    })
            evaluated = [
                evaluated_by_version[version_id]
                for version_id in evaluated_by_version
                if version_id in approved_versions
            ]
            fact_refs_by_version = {
                version_id: tuple(str(item) for item in _fact_refs_from_verified_metrics(host_by_version[version_id]))
                for version_id in approved_versions
            }
            if len(evaluated) < requested_candidate_count:
                generator_feedback = {
                    "reason": (
                        "Evaluator未确认足够的CandidateVersion FactRef"
                        f"（需要{requested_candidate_count}个，当前{len(evaluated)}个）"
                    ),
                    "rejected": list(rejected_rows),
                }
                if generation_round == 2 and not evaluated:
                    return self._empty_recommendation(
                        case, route, run_id, mode, audit, tuple(rejected_rows),
                        f"Mode4两轮Generator后仍未形成{requested_candidate_count}个通过Evaluator FactRef核对的候选。",
                        ranking_spec_id=spec.ranking_spec_id,
                        ranking_spec_fingerprint=spec.ranking_spec_fingerprint,
                        ranking_spec=spec.to_dict(),
                    )
                if generation_round == 1:
                    continue
            approved_metrics = {
                version_id: metrics_by_version[version_id]
                for version_id in approved_versions
            }
            ranking = rank_candidates(spec, evaluated, approved_metrics)
            rejected_rows.extend({
                "candidate_key": item.candidate_key,
                "candidate_version_id": item.version_id,
                "reason": "；".join(item.reasons),
                "source": "constraint_ranking",
            } for item in ranking.exclusions)
            if len(ranking.ranked_candidates) >= requested_candidate_count:
                break
            generator_feedback = {
                "reason": (
                    "硬约束或必要指标筛选后数量不足"
                    f"（需要{requested_candidate_count}个，当前{len(ranking.ranked_candidates)}个）"
                ),
                "rejected": list(rejected_rows),
            }
            if generation_round == 2 and not ranking.ranked_candidates:
                return self._empty_recommendation(
                    case, route, run_id, mode, audit, tuple(rejected_rows),
                    f"Mode4两轮Generator后仍未形成{requested_candidate_count}个满足硬约束的候选。",
                    ranking_spec_id=spec.ranking_spec_id,
                    ranking_spec_fingerprint=spec.ranking_spec_fingerprint,
                    ranking_spec=spec.to_dict(),
                )
            if generation_round == 2:
                break
        if ranking is None or not ranking.ranked_candidates:
            raise RecommendationValidationError("Mode4未形成确定性排序")
        reviewer = runner.run("Reviewer", {
            "mode": "constraint-ranking",
            "ranking_spec": spec.to_dict(),
            "decisions": [item.to_dict() for item in ranking.decisions],
            "candidates": [
                _review_candidate_projection(item, fact_refs=fact_refs_by_version.get(str(item.candidate_version_id), ()))
                for item in ranking.ranked_candidates
            ],
            "evaluator_reviews": dict(evaluator_reviews),
            "rule": "只能整体批准或拒绝确定性Ranker排序，不得改写名次、CandidateVersion或FactRef。",
        })
        reviewed = apply_reviewer_verdict(ranking, parse_reviewer_ranking_verdict(reviewer))
        if not reviewed.approved:
            return self._empty_recommendation(
                case, route, run_id, mode, audit, tuple(rejected_rows),
                reviewed.rejection_reason or "Mode4终审未通过。",
                ranking_spec_id=spec.ranking_spec_id, ranking_spec_fingerprint=spec.ranking_spec_fingerprint,
                ranking_spec=spec.to_dict(),
            )
        audit.append(
            "constraint_ranking", "complete", agent_role=None,
            input_value={"ranking_spec_id": spec.ranking_spec_id},
            output_value={
                "candidate_ids": [item.candidate_id for item in reviewed.ranking.ranked_candidates],
                "decisions": [item.to_dict() for item in reviewed.ranking.decisions],
            },
            detail={"ranking_owner": "deterministic_host", "reviewer_can_reorder": False},
        )
        return self._finalize_candidates(
            case, route, run_id, mode, audit, runner, reviewed.ranking.ranked_candidates, tuple(rejected_rows),
            ranking_spec_id=spec.ranking_spec_id, ranking_spec_fingerprint=spec.ranking_spec_fingerprint,
            ranking_spec=spec.to_dict(),
        )

    def _empty_recommendation(
        self,
        case: RecommendationCase,
        route: RouteDecision,
        run_id: str,
        mode: str,
        audit: AuditRecorder,
        rejected: Sequence[Mapping[str, Any]],
        limitation: str,
        *,
        ranking_spec_id: str | None = None,
        ranking_spec_fingerprint: str | None = None,
        ranking_spec: Mapping[str, Any] | None = None,
    ) -> RecommendationSet:
        requested_candidate_count = _requested_candidate_count(case)
        return RecommendationSet(
            schema=RECOMMENDATION_SET_SCHEMA,
            task_id=case.task_id,
            run_id=run_id,
            analysis_case_id=case.analysis_case_id,
            catalog_version=case.catalog_version,
            route=route.route,
            workflow_mode=mode,
            status="unavailable",
            primary_candidate_id=None,
            candidates=(),
            requested_outputs=case.requested_outputs,
            rejected=tuple(rejected),
            requested_candidate_count=requested_candidate_count,
            returned_candidate_count=0,
            limitations=(str(limitation),),
            audit_trail=tuple(audit.events),
            ranking_spec_id=ranking_spec_id,
            ranking_spec_fingerprint=ranking_spec_fingerprint,
            ranking_spec=ranking_spec,
        )

    def _finalize_candidates(
        self,
        case: RecommendationCase,
        route: RouteDecision,
        run_id: str,
        mode: str,
        audit: AuditRecorder,
        runner: AgentStepRunner,
        candidates: Sequence[RecommendationCandidate],
        rejected: Sequence[Mapping[str, Any]],
        *,
        ranking_spec_id: str | None = None,
        ranking_spec_fingerprint: str | None = None,
        ranking_spec: Mapping[str, Any] | None = None,
    ) -> RecommendationSet:
        requested_candidate_count = _requested_candidate_count(case)
        candidates = tuple(candidates)
        rejected = tuple(rejected)
        candidate_index = {item.candidate_id: item for item in candidates}
        stale_contracts = sorted(set(case.candidate_contracts) - set(candidate_index))
        if stale_contracts:
            raise RecommendationValidationError(f"CandidateContract不属于当前候选：{','.join(stale_contracts)}")
        approved = set(case.approved_candidate_ids)
        if approved:
            candidates = execute(
                candidates,
                approved_candidate_ids=case.approved_candidate_ids,
                requested_outputs=case.requested_outputs,
                tenant_id=case.tenant_id,
                task_id=case.task_id,
                run_id=run_id,
                candidate_contracts=case.candidate_contracts,
                step_runner=runner,
                tool_port=self.tool_port,
            )
        else:
            candidates, needs_confirmation = _prepare_contract_confirmation(candidates, case.candidate_contracts)
            audit.append("approval_gate", "pending", agent_role=None,
                         input_value={"candidate_ids": [item.candidate_id for item in candidates]}, output_value=None,
                         detail={"reason": "未提供用户确认或受控自动批准"})
        module_statuses = {
            status for item in candidates for status in item.module_statuses.values()
        }
        if not approved:
            status = "pending_approval"
            if not module_statuses:
                analysis_status = "not_started"
            elif module_statuses <= {"succeeded"}:
                analysis_status = "completed"
            elif "timed_out" in module_statuses:
                analysis_status = "timed_out"
            elif "cancelled" in module_statuses:
                analysis_status = "cancelled"
            elif "failed" in module_statuses:
                analysis_status = "failed"
            elif module_statuses <= {"unsupported", "succeeded"} and "unsupported" in module_statuses:
                analysis_status = "unsupported"
            else:
                analysis_status = "partial"
        elif any(item.candidate_status == "pending_terms" for item in candidates):
            status = "partial"
            analysis_status = "not_started"
        elif not module_statuses:
            status = "candidate_ready"
            analysis_status = "not_started"
        elif module_statuses <= {"succeeded"}:
            status = "completed"
            analysis_status = "completed"
        elif module_statuses <= {"unsupported", "succeeded"} and "unsupported" in module_statuses:
            status = "partial"
            analysis_status = "unsupported"
        elif "timed_out" in module_statuses:
            status = "partial"
            analysis_status = "timed_out"
        elif "cancelled" in module_statuses:
            status = "partial"
            analysis_status = "cancelled"
        elif "failed" in module_statuses:
            status = "partial"
            analysis_status = "failed"
        else:
            status = "partial"
            analysis_status = "partial"
        limitations: list[str] = []
        if len(candidates) < requested_candidate_count:
            limitations.append(
                f"请求{requested_candidate_count}个候选，证据、约束与复核门禁后仅"
                f"{len(candidates)}个合格；已保留全部合格候选。"
            )
        limitations.extend(
            f"候选{item.candidate_id}资料状态为{item.library_status}，未调用计算模块。"
            for item in candidates if item.candidate_status == "pending_terms"
        )
        report_requested = route.route == "professional_report" or bool(
            {str(item).strip().lower() for item in case.requested_outputs} & {"card", "report", "reporting", "reporter"}
        )
        if report_requested:
            limitations.append("所选交付将在同一合同下的验证结果完成后生成。")
        return RecommendationSet(
            schema=RECOMMENDATION_SET_SCHEMA, task_id=case.task_id, run_id=run_id,
            analysis_case_id=case.analysis_case_id, catalog_version=case.catalog_version, route=route.route,
            workflow_mode=mode, status=status, primary_candidate_id=candidates[0].candidate_id,
            candidates=candidates, requested_outputs=case.requested_outputs, rejected=rejected,
            requested_candidate_count=requested_candidate_count,
            returned_candidate_count=len(candidates),
            analysis_status=analysis_status,
            delivery_status="pending" if report_requested else "not_requested",
            next_question=_CONTRACT_CONFIRMATION_QUESTION if not approved and needs_confirmation else None,
            audit_trail=tuple(audit.events),
            limitations=tuple(limitations),
            ranking_spec_id=ranking_spec_id,
            ranking_spec_fingerprint=ranking_spec_fingerprint,
            ranking_spec=ranking_spec,
        )

    def _unavailable_set(
        self,
        case: RecommendationCase,
        route: RouteDecision,
        run_id: str,
        mode: str,
        audit: AuditRecorder,
        error: Exception,
    ) -> RecommendationSet:
        audit.append("workflow", "failed", agent_role=None, input_value={"case": case.analysis_case_id}, output_value=None,
                     detail={"error_type": type(error).__name__, "message": str(error)})
        return RecommendationSet(
            schema=RECOMMENDATION_SET_SCHEMA, task_id=case.task_id, run_id=run_id,
            analysis_case_id=case.analysis_case_id, catalog_version=case.catalog_version, route=route.route,
            workflow_mode=mode, status="unavailable", primary_candidate_id=None, candidates=(),
            requested_outputs=case.requested_outputs,
            requested_candidate_count=_requested_candidate_count(case),
            returned_candidate_count=0,
            limitations=(str(error),), audit_trail=tuple(audit.events),
        )

    def _run_freeform(self, case: RecommendationCase, route: RouteDecision) -> Mapping[str, Any]:
        audit = AuditRecorder("single_agent")
        audit.append("route", "complete", agent_role=None, input_value={"prompt": case.prompt}, output_value=route.to_dict())
        try:
            return self._run_freeform_loop(case, route, audit)
        except Exception as error:
            audit.append(
                "workflow", "failed", agent_role=None, input_value={"case": case.analysis_case_id}, output_value=None,
                detail={"error_type": type(error).__name__, "message": str(error)},
            )
            return {
                "response": "", "status": "unavailable", "tool_results": [],
                "limitations": [str(error)], "audit_trail": [item.to_dict() for item in audit.events],
            }

    def _run_freeform_loop(
        self,
        case: RecommendationCase,
        route: RouteDecision,
        audit: AuditRecorder,
    ) -> Mapping[str, Any]:
        runner = AgentStepRunner(port=self.agent_port, workflow_mode="single_agent", audit=audit)
        tool_results: list[Mapping[str, Any]] = []
        seen_results: dict[str, Mapping[str, Any]] = {}
        allowed_modules = _ROUTE_TOOLS.get(route.route, frozenset())
        for _ in range(self.config.max_agent_rounds):
            result = runner.run("Freeform", {
                "prompt": case.prompt,
                "route": route.to_dict(),
                "tool_results": tool_results,
                "allowed_modules": sorted(allowed_modules),
            })
            requests = result.get("tool_requests", ())
            if not requests:
                response_text = str(result.get("response", "")).strip()
                if not response_text:
                    raise ValueError("Freeform未调用工具且未返回答复")
                status = "partial" if any(item.get("status") in {"failed", "partial"} for item in tool_results) else "completed"
                return {"response": response_text, "status": status, "tool_results": tool_results,
                        "audit_trail": [item.to_dict() for item in audit.events]}
            if isinstance(requests, (str, bytes)) or not isinstance(requests, Sequence):
                raise ValueError("Freeform.tool_requests必须为数组")
            for raw in requests:
                if not isinstance(raw, Mapping):
                    raise ValueError("Freeform.tool_request必须为对象")
                module = str(raw.get("module", "")).strip().lower()
                request = raw.get("request", {})
                if not isinstance(request, Mapping):
                    raise ValueError("Freeform工具请求必须为对象")
                call_key = canonical_hash({"module": module, "request": dict(request)})
                if call_key in seen_results:
                    reused = {**dict(seen_results[call_key]), "reused": True}
                    tool_results.append(reused)
                    audit.append(f"tool.{module or 'unknown'}", "reused", agent_role="SingleAgent.Freeform",
                                 input_value=request, output_value=reused)
                    continue
                try:
                    if module not in allowed_modules:
                        raise ValueError(f"路由{route.route}无权调用工具：{module or '<empty>'}")
                    if module == "knowledger":
                        if self.knowledge_port is None:
                            raise RecommenderUnavailable("未配置Knowledger正式端口")
                        payload = dict(request)
                        payload["catalog_version"] = case.catalog_version
                        response = self.knowledge_port.search(payload)
                    else:
                        if self.tool_port is None:
                            raise RecommenderUnavailable("未配置Tool Gateway正式端口")
                        response = self.tool_port.call(module, request)
                    module_status = str(response.get("status", "")).strip().lower()
                    failed_result = response.get("ok") is False or module_status in {
                        "partial", "failed", "unsupported", "cancelled", "timed_out", "unavailable",
                    }
                    outcome = "partial" if module_status == "partial" else "failed" if failed_result else "returned"
                    tool_result = {
                        "module": module,
                        "status": outcome,
                        "module_status": module_status or None,
                        "result": dict(response),
                    }
                    tool_results.append(tool_result)
                    seen_results[call_key] = tool_result
                    audit.append(f"tool.{module}", outcome, agent_role="SingleAgent.Freeform",
                                 input_value=request, output_value=response)
                except Exception as error:
                    tool_result = {"module": module, "status": "failed", "error": str(error)}
                    tool_results.append(tool_result)
                    seen_results[call_key] = tool_result
                    audit.append(f"tool.{module or 'unknown'}", "failed", agent_role="SingleAgent.Freeform",
                                 input_value=request, output_value=None,
                                 detail={"error_type": type(error).__name__, "message": str(error)})
        return {"response": "单Agent达到最大工具轮次，未生成最终答复。", "tool_results": tool_results,
                "audit_trail": [item.to_dict() for item in audit.events], "status": "partial"}


def _accepting_reviews(proposals: object) -> list[dict[str, Any]]:
    if isinstance(proposals, (str, bytes)) or not isinstance(proposals, Sequence):
        raise RecommendationValidationError("候选生成结果必须为数组")
    reviews: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in proposals:
        if not isinstance(raw, Mapping):
            raise RecommendationValidationError("候选生成结果必须为对象数组")
        product_id = str(raw.get("product_id", "")).strip()
        if not product_id or product_id in seen:
            raise RecommendationValidationError("候选生成product_id不能为空或重复")
        seen.add(product_id)
        reviews.append({
            "product_id": product_id,
            "hard_reject": False,
            "rejection_reason": None,
            "additional_not_suitable_for": [],
            "additional_risks": [],
            "rank_adjustment": 0,
        })
    return reviews


def _mode2_structurer_plan(loop: ProductTraderLoop, output: Mapping[str, Any]) -> Mapping[str, Any]:
    raw_plans = output.get("evaluation_plan", ())
    raw_proposals = output.get("proposals", ())
    if isinstance(raw_plans, (str, bytes)) or not isinstance(raw_plans, Sequence):
        raise RecommendationValidationError("Structurer.evaluation_plan必须为数组")
    if isinstance(raw_proposals, (str, bytes)) or not isinstance(raw_proposals, Sequence):
        raise RecommendationValidationError("Structurer.proposals必须为数组")
    open_states = [item for item in loop.candidates if item.status == "open"]
    product_to_state = {item.candidate.product_id: item for item in open_states}
    if len(product_to_state) != len(open_states):
        raise RecommendationValidationError("Mode2同轮候选product_id不得重复")
    proposal_ids = {
        str(item.get("product_id", "")).strip()
        for item in raw_proposals if isinstance(item, Mapping)
    }
    if proposal_ids != set(product_to_state):
        raise RecommendationValidationError("Structurer第二轮不得新增、删除或替换候选产品")
    by_product: dict[str, Mapping[str, Any]] = {}
    for raw in raw_plans:
        if not isinstance(raw, Mapping):
            raise RecommendationValidationError("Structurer.evaluation_plan必须为对象数组")
        product_id = str(raw.get("product_id", "")).strip()
        if not product_id or product_id in by_product:
            raise RecommendationValidationError("Structurer.evaluation_plan产品不能为空或重复")
        by_product[product_id] = raw
    if set(by_product) != set(product_to_state):
        raise RecommendationValidationError("Structurer必须为本轮每个候选提供一个evaluation_plan")
    return {"proposals": [{
        "candidate_key": str(product_to_state[product_id].candidate.candidate_key),
        "evaluation_plan": list(by_product[product_id].get("modules", ())),
        "term_overrides": dict(by_product[product_id].get("term_overrides", {})),
    } for product_id in product_to_state]}


def _mode2_trader_decisions(
    plan: object,
    output: Mapping[str, Any],
    host_rows: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    plans = getattr(plan, "plans", ())
    product_to_key = {
        item.candidate.candidate.product_id: str(item.candidate.candidate.candidate_key)
        for item in plans
    }
    product_to_version = {
        item.candidate.candidate.product_id: str(item.candidate.candidate.candidate_version_id)
        for item in plans
    }
    product_to_statuses = {
        item.candidate.candidate.product_id: dict(item.candidate.candidate.module_statuses)
        for item in plans
    }
    product_to_fact_refs: dict[str, set[str]] = {}
    for product_id, candidate_key in product_to_key.items():
        metrics = host_rows.get(candidate_key, {}).get("verified_metrics", {})
        if not isinstance(metrics, Mapping):
            raise RecommendationValidationError("Mode2 Host verified_metrics必须为对象")
        product_to_fact_refs[product_id] = {
            str(detail.get("fact_ref"))
            for detail in metrics.values()
            if isinstance(detail, Mapping) and isinstance(detail.get("fact_ref"), str)
        }
    rows = output.get("evaluations", ())
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise RecommendationValidationError("Trader.evaluations必须为数组")
    decisions = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise RecommendationValidationError("Trader.evaluations必须为对象数组")
        product_id = str(raw.get("product_id", "")).strip()
        if product_id not in product_to_key or product_id in seen:
            raise RecommendationValidationError("Trader只能逐一复核本轮已有候选")
        if str(raw.get("candidate_version_id", "")).strip() != product_to_version[product_id]:
            raise RecommendationValidationError("Trader决定未绑定当前candidate_version_id")
        used_refs = raw.get("used_fact_refs", ())
        if isinstance(used_refs, (str, bytes)) or not isinstance(used_refs, Sequence):
            raise RecommendationValidationError("Trader.used_fact_refs必须为数组")
        if any(not isinstance(item, str) or not item.strip() for item in used_refs):
            raise RecommendationValidationError("Trader.used_fact_refs必须为非空字符串数组")
        used_refs = tuple(
            _fact_ref(item, "Trader.used_fact_refs") for item in used_refs
        )
        decision = str(raw.get("decision", "")).strip().lower()
        if not set(used_refs).issubset(product_to_fact_refs[product_id]):
            raise RecommendationValidationError("Trader引用了非本候选Host事实")
        if decision == "accept" and product_to_fact_refs[product_id] and not used_refs:
            raise RecommendationValidationError("Trader接受含数值事实的候选时必须声明used_fact_refs")
        if decision == "accept" and any(status != "succeeded" for status in product_to_statuses[product_id].values()):
            raise RecommendationValidationError("Trader不得接受存在失败、超时或不支持模块的候选")
        seen.add(product_id)
        decisions.append({
            "candidate_key": product_to_key[product_id],
            "action": decision,
            "term_adjustments": dict(raw.get("term_adjustments", {})),
        })
    if seen != set(product_to_key):
        raise RecommendationValidationError("Trader必须覆盖本轮全部候选")
    return {"decisions": decisions}


def _host_attachment(value: Mapping[str, Any]) -> Mapping[str, Any]:
    required = {"evaluation_records", "module_run_refs", "module_statuses", "display_terms", "contract_fingerprint"}
    missing = required - set(value)
    if missing:
        raise RecommendationValidationError(f"Host候选计算缺少字段：{','.join(sorted(missing))}")
    return {key: value[key] for key in required}


def _verified_metric_projection(value: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = value.get("verified_metrics", {})
    if not isinstance(raw, Mapping):
        raise RecommendationValidationError("Host verified_metrics必须为对象")
    result: dict[str, Any] = {}
    for metric, detail in raw.items():
        if not isinstance(detail, Mapping):
            raise RecommendationValidationError("Host verified_metrics明细必须为对象")
        numeric = detail.get("value")
        if (
            isinstance(numeric, bool)
            or not isinstance(numeric, (int, float))
            or not math.isfinite(float(numeric))
        ):
            raise RecommendationValidationError("Host verified_metrics只允许有限数值")
        fact_ref = _fact_ref(detail.get("fact_ref"), f"Host verified_metrics.{metric}.fact_ref")
        result[str(metric)] = {
            "value": numeric,
            "unit": detail.get("unit"),
            "fact_ref": fact_ref,
            "source": detail.get("source"),
        }
    return result


def _attach_candidate_evaluation(
    candidate: RecommendationCandidate,
    value: Mapping[str, Any],
) -> RecommendationCandidate:
    version_id = str(candidate.candidate_version_id or "")
    if str(value.get("candidate_version_id", "")) != version_id:
        raise RecommendationValidationError("Host计算结果与Mode4候选版本不一致")
    raw_records = value.get("evaluation_records", ())
    if isinstance(raw_records, (str, bytes)) or not isinstance(raw_records, Sequence):
        raise RecommendationValidationError("Host evaluation_records必须为数组")
    records = tuple(
        EvaluationRecord.from_mapping(item) if isinstance(item, Mapping) else item
        for item in raw_records
    )
    if any(not isinstance(item, EvaluationRecord) or item.version_id != version_id for item in records):
        raise RecommendationValidationError("Host EvaluationRecord未绑定Mode4候选版本")
    statuses = value.get("module_statuses", {})
    if not isinstance(statuses, Mapping) or dict(statuses) != {item.module: item.status for item in records}:
        raise RecommendationValidationError("Host module_statuses与EvaluationRecord不一致")
    terms = value.get("display_terms", ())
    if isinstance(terms, (str, bytes)) or not isinstance(terms, Sequence):
        raise RecommendationValidationError("Host display_terms必须为数组")
    term_overrides = value.get("term_overrides", {})
    if not isinstance(term_overrides, Mapping):
        raise RecommendationValidationError("Host term_overrides必须为对象")
    return replace(
        candidate,
        key_terms=tuple(dict(item) for item in terms if isinstance(item, Mapping)),
        module_run_refs=tuple(item.module_run_ref for item in records if item.module_run_ref is not None),
        module_statuses=dict(statuses),
        evaluation_records=records,
        contract_fingerprint=_contract_fingerprint(value),
        term_overrides=dict(term_overrides),
    )


def _mode4_metrics(value: Mapping[str, Any]) -> Mapping[str, Mapping[str, float]]:
    raw = value.get("verified_metrics", {})
    if not isinstance(raw, Mapping):
        raise RecommendationValidationError("Host verified_metrics必须为对象")
    result: dict[str, dict[str, float]] = {}
    source_alias = {"resolved_contract": "contract_terms"}
    metric_alias = {f"greeks.{name}": name for name in ("delta", "gamma", "vega", "theta", "rho")}
    for raw_metric, detail in raw.items():
        if not isinstance(detail, Mapping):
            raise RecommendationValidationError("Host verified_metrics明细必须为对象")
        metric = metric_alias.get(str(raw_metric), str(raw_metric))
        source = source_alias.get(str(detail.get("source", "")), str(detail.get("source", "")))
        value_number = detail.get("value")
        if (
            isinstance(value_number, bool)
            or not isinstance(value_number, (int, float))
            or not math.isfinite(float(value_number))
        ):
            raise RecommendationValidationError("Host verified_metrics只允许有限数值")
        _fact_ref(detail.get("fact_ref"), f"Host verified_metrics.{raw_metric}.fact_ref")
        result.setdefault(source, {})[metric] = value_number
    return result


def _review_candidate_projection(
    candidate: RecommendationCandidate,
    *,
    fact_refs: Sequence[str] = (),
) -> Mapping[str, Any]:
    return {
        "product_id": candidate.product_id,
        "product_name": candidate.product_name,
        "candidate_key": candidate.candidate_key,
        "candidate_version_id": candidate.candidate_version_id,
        "contract_fingerprint": candidate.contract_fingerprint,
        "rank": candidate.rank,
        "reason": candidate.reason,
        "module_statuses": dict(candidate.module_statuses),
        "display_terms": [dict(item) for item in candidate.key_terms],
        # ModuleRunRef is an execution identity, not a financial fact.  Facts
        # must be projected separately with an opaque FactRef by the Host.
        "fact_refs": [str(item) for item in fact_refs],
    }


def _fact_ref(value: object, field_name: str) -> str:
    result = str(value or "").strip()
    if (
        not result.startswith("fact")
        or len(result) > 160
        or any(ord(character) < 33 for character in result)
    ):
        raise RecommendationValidationError(f"{field_name}必须是FactRef")
    return result


def _bounded_evaluations(
    evaluator: Any,
    requests: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    if not requests:
        return []
    workers = min(MAX_EVALUATORS, len(requests))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="recommender-evaluator") as executor:
        futures = [executor.submit(evaluator, **dict(request)) for request in requests]
        return [future.result() for future in futures]


def _fact_refs_from_verified_metrics(value: Mapping[str, Any]) -> tuple[str, ...]:
    """Project only opaque FactRefs from Host metrics for an Evaluator child."""

    raw = value.get("verified_metrics", {})
    if not isinstance(raw, Mapping):
        raise RecommendationValidationError("Host verified_metrics必须为对象")
    refs: list[str] = []
    for raw_metric, detail in raw.items():
        if not isinstance(detail, Mapping):
            raise RecommendationValidationError(f"Host verified_metrics.{raw_metric}明细必须为对象")
        refs.append(_fact_ref(detail.get("fact_ref"), f"Host verified_metrics.{raw_metric}.fact_ref"))
    return tuple(dict.fromkeys(refs))


def _bounded_evaluator_agents(
    runner: AgentStepRunner,
    payloads: Sequence[Mapping[str, Any]],
    *,
    child_managed: bool = False,
) -> list[Mapping[str, Any]]:
    """Run one independent Evaluator AgentRun for every CandidateVersion.

    The Host has already produced the numeric projection used by the
    deterministic Ranker.  Evaluator children receive only their own opaque
    FactRefs and module statuses, so they can confirm provenance without
    becoming a second source of financial facts or ranking behavior.
    """

    if not payloads:
        return []
    audit_lock = Lock()
    receipt_lock = Lock()
    run_ids: set[str] = set()
    session_ids: set[str] = set()

    def evaluate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        request = {
            "workflow": "optionhelper.recommender",
            "role_rule": (
                "Evaluator必须自行调用当前CandidateVersion获授权的业务工具，并只使用工具返回的FactRef；"
                "不得引用其他CandidateVersion或改写金融事实。"
                if child_managed else
                "Evaluator只能核对当前CandidateVersion提供的FactRef及模块状态；"
                "不得读取、生成或改写金融数值，不得引用其他CandidateVersion。"
            ),
            "required_output": {
                "candidate_version_id": "string",
                "decision": "approve|reject",
                "reason": "string",
                "used_fact_refs": "string[]",
            },
            "input": dict(payload),
        }
        try:
            step = runner.port.run_step("Evaluator", request)
            if not isinstance(step, AgentStepResult):
                raise RecommendationValidationError("Evaluator必须返回AgentStepResult")
            receipt = step.receipt
            if receipt.role != "Evaluator":
                raise RecommendationValidationError("Evaluator运行凭证角色不匹配")
            if receipt.input_hash != canonical_hash(request):
                raise RecommendationValidationError("Evaluator运行凭证输入哈希不匹配")
            if receipt.output_hash != canonical_hash(step.result):
                raise RecommendationValidationError("Evaluator运行凭证输出哈希不匹配")
            with receipt_lock:
                if receipt.agent_run_id in run_ids:
                    raise RecommendationValidationError("Evaluator重复使用agent_run_id")
                if receipt.child_session_id in session_ids:
                    raise RecommendationValidationError("Evaluator重复使用child_session_id")
                run_ids.add(receipt.agent_run_id)
                session_ids.add(receipt.child_session_id)
            if not isinstance(step.result, Mapping):
                raise RecommendationValidationError("Evaluator结果必须为对象")
            result = dict(step.result)
            with audit_lock:
                runner.audit.append(
                    "evaluator", "complete", agent_role="Evaluator",
                    input_value=request, output_value=result,
                    detail={
                        "independent_agent_run": True,
                        "candidate_version_id": str(payload.get("candidate", {}).get("candidate_version_id", "")),
                        "child_session_id": receipt.child_session_id,
                        "agent_run_id": receipt.agent_run_id,
                    },
                )
            return result
        except Exception as error:
            with audit_lock:
                runner.audit.append(
                    "evaluator", "failed", agent_role="Evaluator",
                    input_value=request, output_value=None,
                    detail={"error_type": type(error).__name__, "message": str(error)},
                )
            raise

    workers = min(MAX_EVALUATORS, len(payloads))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="recommender-evaluator-agent") as executor:
        futures = [executor.submit(evaluate, payload) for payload in payloads]
        return [future.result() for future in futures]


def _validate_mode4_evaluator_result(
    value: Mapping[str, Any],
    version_id: str,
    expected_fact_refs: Sequence[str],
) -> tuple[bool, Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        raise RecommendationValidationError("Evaluator结果必须为对象")
    unknown = set(value).difference({"candidate_version_id", "decision", "reason", "used_fact_refs"})
    if unknown:
        raise RecommendationValidationError(f"Evaluator结果包含未知字段：{','.join(sorted(unknown))}")
    if str(value.get("candidate_version_id", "")).strip() != version_id:
        raise RecommendationValidationError("Evaluator结果未绑定当前CandidateVersion")
    decision = str(value.get("decision", "")).strip().lower()
    if decision in {"approve", "approved", "accept", "accepted"}:
        decision = "approve"
    elif decision in {"reject", "rejected"}:
        decision = "reject"
    else:
        raise RecommendationValidationError("Evaluator.decision必须为approve或reject")
    reason = value.get("reason", "")
    if not isinstance(reason, str):
        raise RecommendationValidationError("Evaluator.reason必须为字符串")
    raw_refs = value.get("used_fact_refs", ())
    if isinstance(raw_refs, (str, bytes)) or not isinstance(raw_refs, Sequence):
        raise RecommendationValidationError("Evaluator.used_fact_refs必须为数组")
    used_refs = tuple(_fact_ref(item, "Evaluator.used_fact_refs") for item in raw_refs)
    if len(set(used_refs)) != len(used_refs):
        raise RecommendationValidationError("Evaluator.used_fact_refs不得重复")
    expected = tuple(dict.fromkeys(_fact_ref(item, "Evaluator.expected_fact_refs") for item in expected_fact_refs))
    if set(used_refs) != set(expected):
        raise RecommendationValidationError("Evaluator必须逐一核对当前CandidateVersion的FactRef")
    if decision == "reject" and not reason.strip():
        raise RecommendationValidationError("Evaluator拒绝时必须说明reason")
    return decision == "approve", {
        "candidate_version_id": version_id,
        "decision": decision,
        "reason": reason.strip(),
        "used_fact_refs": list(used_refs),
    }


def _contract_fingerprint(value: Mapping[str, Any]) -> str:
    fingerprint = str(value.get("contract_fingerprint", "")).strip()
    if len(fingerprint) != 64 or any(character not in "0123456789abcdef" for character in fingerprint):
        raise RecommendationValidationError("Host contract_fingerprint无效")
    return fingerprint


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _merge_independent_research(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Deterministically aggregate fresh council outputs before Reviewer sees them."""

    proposals: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        values = row.get("proposals", ())
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise RecommendationValidationError("独立评审团Selector.proposals必须为数组")
        for proposal in values:
            if not isinstance(proposal, Mapping):
                raise RecommendationValidationError("独立评审团Selector.proposals必须为对象数组")
            product_id = str(proposal.get("product_id", "")).strip()
            if not product_id:
                raise RecommendationValidationError("独立评审团候选缺少product_id")
            proposals.setdefault(product_id, dict(proposal))
    return {"proposals": list(proposals.values())}


def _research_queries(case: RecommendationCase) -> tuple[str, ...]:
    """在模型未给检索词时，用已确认观点提供受控召回提示。"""

    constraints = case.confirmed_constraints
    view = str(constraints.get("market_view", ""))
    terms = [str(constraints.get("underlying", "")).strip()]
    if "上涨" in view:
        terms.append("看涨期权")
    elif "看跌" in view:
        terms.append("看跌期权")
    terms.append(case.prompt)
    return tuple(dict.fromkeys(item for item in terms if item))


def _mode3_required_modules(case: RecommendationCase) -> tuple[str, ...]:
    """Select only calculations explicitly needed by the council request."""

    text = f"{case.prompt} {dict(case.confirmed_constraints)}".casefold()
    modules: list[str] = []
    if any(marker in text for marker in ("收益", "损益", "payoff")):
        modules.append("payoffer")
    if any(marker in text for marker in (
        "估值", "定价", "期权费", "premium", "greek", "delta", "gamma", "vega", "theta", "rho",
    )):
        modules.append("pricer")
    if any(marker in text for marker in ("回测", "历史", "胜率", "win rate", "backtest")):
        modules.append("backtester")
    return tuple(modules)


def _requested_candidate_count(case: RecommendationCase) -> int:
    """Read the explicit selection quantity without coupling to a model version.

    ``CandidateSelectionSpec`` is being migrated by the App settings layer. The
    Recommender accepts its current object form, the legacy scalar aliases and
    the private normalized value carried by this service, so persisted settings
    remain readable during that migration.
    """

    values: list[object] = []
    constraints = case.confirmed_constraints
    if isinstance(constraints, Mapping):
        for key in ("_requested_candidate_count", "requested_candidate_count", "candidate_count"):
            if key in constraints:
                values.append(constraints[key])
        for key in ("selection_spec", "candidate_selection"):
            if key in constraints:
                values.append(constraints[key])
    for attribute in ("requested_candidate_count", "candidate_count", "selection_spec", "candidate_selection"):
        if hasattr(case, attribute):
            values.append(getattr(case, attribute))
    if not values:
        natural_count = requested_candidate_count_from_text(case.prompt)
        if natural_count is not None:
            values.append(natural_count)
    return _normalize_requested_candidate_count(values)


def _requested_candidate_count_from_request(
    request: RecommendationCase | Mapping[str, Any],
    case: RecommendationCase,
) -> int:
    values: list[object] = []
    if isinstance(request, Mapping):
        for key in ("requested_candidate_count", "candidate_count", "selection_spec", "candidate_selection"):
            if key in request:
                values.append(request[key])
        raw_constraints = request.get("confirmed_constraints")
        if isinstance(raw_constraints, Mapping):
            for key in ("requested_candidate_count", "candidate_count", "selection_spec", "candidate_selection"):
                if key in raw_constraints:
                    values.append(raw_constraints[key])
    if not values:
        return _requested_candidate_count(case)
    return _normalize_requested_candidate_count(values)


def _normalize_requested_candidate_count(values: Sequence[object]) -> int:
    scalars: list[int] = []
    for raw in values:
        value = raw
        if isinstance(raw, Mapping):
            nested = [raw[key] for key in ("requested_candidate_count", "candidate_count") if key in raw]
            if not nested:
                continue
            if len(nested) > 1 and nested[0] != nested[1]:
                raise RecommendationValidationError("候选数量字段冲突")
            value = nested[0]
        else:
            nested_value = getattr(raw, "requested_candidate_count", None)
            if nested_value is not None:
                value = nested_value
            elif hasattr(raw, "candidate_count") and not isinstance(raw, (int, float, str, bytes)):
                value = getattr(raw, "candidate_count")
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise RecommendationValidationError("requested_candidate_count必须为1至10的整数")
        scalars.append(value)
    if scalars and any(item != scalars[0] for item in scalars[1:]):
        raise RecommendationValidationError("候选数量字段冲突")
    result = scalars[0] if scalars else DEFAULT_REQUESTED_CANDIDATE_COUNT
    if not 1 <= result <= MAX_REQUESTED_CANDIDATE_COUNT:
        raise RecommendationValidationError("requested_candidate_count必须位于1至10")
    return result


def _bind_requested_candidate_count(case: RecommendationCase, count: int) -> RecommendationCase:
    constraints = dict(case.confirmed_constraints)
    constraints["_requested_candidate_count"] = count
    return replace(case, confirmed_constraints=constraints)


def _prepare_recommendation_case(case: RecommendationCase) -> RecommendationCase:
    requested_candidate_count = _requested_candidate_count(case)
    merged_constraints = merge_confirmed_constraints(case.confirmed_constraints, (case.prompt,))
    # Candidate quantity belongs to workflow selection rather than the financial
    # constraint vocabulary, so preserve it after normalization.
    merged_constraints["_requested_candidate_count"] = requested_candidate_count
    prepared = replace(case, confirmed_constraints=merged_constraints)
    return replace(prepared, requested_outputs=_requested_outputs(prepared))


def _requested_outputs(case: RecommendationCase) -> tuple[str, ...]:
    """合并显式调用参数与客户已确认的交付偏好，不推断计算模块。"""

    requested = [str(item).strip().lower() for item in case.requested_outputs if str(item).strip()]
    _, declined = delivery_preferences_from_text(case.prompt.lower())
    aliases = {
        "card": {"card"},
        "quote": {"quote"},
        "report": {"report", "reporting", "reporter"},
    }
    declined_aliases = set().union(*(aliases[item] for item in declined)) if declined else set()
    requested = [item for item in requested if item not in declined_aliases]
    output_type = str(case.confirmed_constraints.get("output_type", "")).strip().lower()
    deliveries = {
        "card": ("card",),
        "quote": ("quote",),
        "report": ("report",),
        "both": ("card", "report"),
    }
    if output_type in deliveries:
        requested = [item for item in requested if item not in {"card", "report", "reporting", "reporter"}]
        requested.extend(deliveries[output_type])
    return tuple(dict.fromkeys(requested))


def _prepare_contract_confirmation(
    candidates: Sequence[Any],
    candidate_contracts: Mapping[str, Mapping[str, Any]],
) -> tuple[tuple[Any, ...], bool]:
    """将Host经Core解析的客户条款摘要附到候选，且不触发任何模块调用。"""

    prepared = []
    needs_confirmation = False
    for candidate in candidates:
        if candidate.library_status != "ready":
            prepared.append(replace(candidate, candidate_status="pending_terms"))
            continue
        raw_contract = candidate_contracts.get(candidate.candidate_id)
        if raw_contract is None:
            prepared.append(replace(candidate, candidate_status="candidate"))
            continue
        contract = CandidateContract.from_mapping(raw_contract, candidate=candidate)
        if not contract.display_terms:
            raise RecommendationValidationError(f"候选{candidate.candidate_id}缺少客户条款摘要，不能请求确认")
        prepared.append(replace(
            candidate,
            candidate_status="pending_confirmation",
            key_terms=contract.display_terms,
        ))
        needs_confirmation = True
    return tuple(prepared), needs_confirmation


def _service_from_environment(config: RecommenderConfig | None = None) -> RecommenderService:
    config = config or RecommenderConfig.from_environment()
    if not config.model_gateway_url:
        raise RecommenderUnavailable("缺少正式端口配置：OPTIONHELPER_MODEL_GATEWAY_URL")
    return RecommenderService(
        agent_port=HttpAgentPort(HttpEndpoint(config.model_gateway_url, config.port_timeout_seconds)),
        knowledge_port=HttpKnowledgePort(HttpEndpoint(config.knowledger_url, config.port_timeout_seconds)) if config.knowledger_url else None,
        tool_port=HttpToolPort(HttpEndpoint(config.tool_gateway_url, config.port_timeout_seconds)) if config.tool_gateway_url else None,
        config=config,
    )


def recommend(request: Mapping[str, Any], *, service: RecommenderService | None = None) -> Mapping[str, Any]:
    return (service or _service_from_environment()).recommend(request)


def recommend_fixed(
    request: Mapping[str, Any],
    *,
    workflow: str = "recommendation",
    service: RecommenderService | None = None,
) -> Mapping[str, Any]:
    return (service or _service_from_environment()).recommend_fixed(request, workflow=workflow)


def call_tool(request: Mapping[str, Any]) -> Mapping[str, Any]:
    body = dict(request)
    action = str(body.pop("action", "status")).strip().lower()
    if action == "status":
        return capability()
    try:
        service = _service_from_environment()
        if action in {"recommend", "recommend_fixed"}:
            workflow = str(body.pop("workflow", "recommendation"))
            recommendation: RecommendationSet | None = None
            if isinstance(service, RecommenderService):
                result, recommendation = service._recommend_with_domain(
                    body,
                    workflow=workflow if action == "recommend_fixed" else None,
                )
            else:
                result = service.recommend(body) if action == "recommend" else service.recommend_fixed(body, workflow=workflow)
            recommendation_set = result.get("recommendation_set")
            if isinstance(recommendation_set, Mapping):
                status = str(recommendation_set.get("status", "failed"))
            else:
                freeform = result.get("freeform")
                status = str(freeform.get("status", "completed")) if isinstance(freeform, Mapping) else "failed"
            public = _public_result(result, recommendation=recommendation)
            return {
                "ok": status not in {"partial", "failed", "unavailable"},
                "module": "recommender",
                "status": status,
                "result": public,
            }
    except Exception as error:
        return {"ok": False, "module": "recommender", "status": "failed",
                "error": {"type": type(error).__name__, "message": "推荐服务当前不可用，请稍后重试。"}}
    return {"ok": False, "module": "recommender", "status": "unsupported",
            "message": f"Recommender不支持action={action}；支持status、recommend、recommend_fixed。"}


def _public_result(
    result: Mapping[str, Any], *, recommendation: RecommendationSet | None = None,
) -> Mapping[str, Any]:
    """正式Tool入口默认仅返回客户可见投影。"""

    if isinstance(recommendation, RecommendationSet):
        return recommendation.to_public_dict()
    freeform = result.get("freeform")
    if isinstance(freeform, Mapping):
        response = public_text(str(freeform.get("response", "")))
        return {
            "说明": response or "当前未形成可展示结果。",
            "状态": str(freeform.get("status", "unavailable")),
        }
    return {"说明": "当前未形成可展示结果。"}
