"""Recommender唯一领域入口与当前候选工作流。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .agent_steps import AuditRecorder, AgentStepRunner, MODE_ONE_ROLE_NAMES, canonical_hash
from .candidate_builder import build_candidates
from .config import RecommenderConfig
from .constraint_ranking import (
    METRIC_SOURCES,
    apply_reviewer_verdict,
    parse_reviewer_ranking_verdict,
    parse_specifier_ranking_spec,
    rank_candidates,
    required_metric_sources,
    required_modules,
)
from .evidence_retriever import retrieve_evidence
from .evaluation_evidence import read_evaluation_records
from .executor import ALLOWED_MODULES, execute
from .interaction import (
    delivery_preferences_from_text,
    merge_confirmed_constraints,
    requested_candidate_count_from_text,
)
from .intent_router import route_intent
from .models import (
    EvaluationRecord,
    ModelCapability,
    RECOMMENDATION_SET_SCHEMA,
    RecommendationCase,
    RecommendationCandidate,
    RecommendationSet,
    RecommendationValidationError,
    RouteDecision,
    public_text,
)
from .ports import (
    AgentPort,
    HttpAgentPort,
    HttpEndpoint,
    HttpKnowledgePort,
    HttpToolPort,
    KnowledgePort,
    ToolPort,
)
from .product_trader_loop import ProductTraderLoop, ProductTraderRound, attach_host_evaluations


DEFAULT_REQUESTED_CANDIDATE_COUNT = 3
MAX_REQUESTED_CANDIDATE_COUNT = 10


class RecommenderUnavailable(RuntimeError):
    """必要正式端口未注入或不可达。"""


class RecommendationInputRequired(RuntimeError):
    """正式运行前仍缺少一个当前输入。"""

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
        "actions": ["recommend", "recommend_fixed", "confirmation_inputs", "execute_confirmed"],
        "routes": [
            "chat", "knowledge", "data", "direct_execution", "recommendation",
            "professional_report", "existing_report", "maintenance", "freeform",
        ],
        "agent_roles": list(preset_roles[config.multi_agent_preset]),
        "agent_role_display_names": dict(MODE_ONE_ROLE_NAMES),
        "external_ports": configured,
        "fully_configured": all(configured.values()),
    }


def confirmation_inputs(
    candidates: Sequence[RecommendationCandidate],
    candidate_ids: Sequence[str] | None = None,
) -> tuple[Mapping[str, Any], ...]:
    """投影用户确认的当前候选输入，不生成或保存已编译产品对象。"""

    rows = tuple(candidates)
    if any(not isinstance(item, RecommendationCandidate) for item in rows):
        raise RecommendationValidationError("candidates必须为RecommendationCandidate数组")
    index = {item.candidate_id: item for item in rows}
    if len(index) != len(rows):
        raise RecommendationValidationError("candidate_id必须唯一")
    selected = tuple(index) if candidate_ids is None else tuple(str(item).strip() for item in candidate_ids)
    if len(selected) != len(set(selected)):
        raise RecommendationValidationError("candidate_ids不得重复")
    unknown = sorted(set(selected) - set(index))
    if unknown:
        raise RecommendationValidationError(f"确认了不存在的候选：{','.join(unknown)}")
    return tuple(index[item].to_confirmation_dict() for item in selected)


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

    def recommend_fixed(
        self,
        request: RecommendationCase | Mapping[str, Any],
        *,
        workflow: str = "recommendation",
    ) -> Mapping[str, Any]:
        result, _ = self._recommend_with_domain(request, workflow=workflow)
        return result

    def confirmation_inputs(
        self,
        candidates: Sequence[RecommendationCandidate],
        candidate_ids: Sequence[str] | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        return confirmation_inputs(candidates, candidate_ids)

    def execute_confirmed(
        self,
        candidates: Sequence[RecommendationCandidate],
        *,
        candidate_ids: Sequence[str],
        requested_outputs: Sequence[str],
        task_id: str,
        run_id: str,
        tenant_id: str = "local",
    ) -> tuple[RecommendationCandidate, ...]:
        """让Hosted层按最新OptionReg编译已确认当前输入并运行明确请求的模块。"""

        confirmation_inputs(candidates, candidate_ids)
        audit = AuditRecorder("single_agent")
        runner = AgentStepRunner(self.agent_port, "single_agent", audit)
        return execute(
            candidates,
            approved_candidate_ids=candidate_ids,
            requested_outputs=requested_outputs,
            tenant_id=tenant_id,
            task_id=task_id,
            run_id=run_id,
            step_runner=runner,
            tool_port=self.tool_port,
        )

    def _recommend_with_domain(
        self,
        request: RecommendationCase | Mapping[str, Any],
        *,
        workflow: str | None = None,
    ) -> tuple[Mapping[str, Any], RecommendationSet | None]:
        count = _requested_candidate_count_from_request(request)
        case = _case_from_request(request)
        case = _bind_requested_candidate_count(case, count)
        if workflow is None:
            route = route_intent(case.prompt)
        else:
            route_name = str(workflow).strip().lower()
            if route_name not in {"recommendation", "professional_report"}:
                raise ValueError("固定Recommender workflow仅支持recommendation或professional_report")
            requested, declined = delivery_preferences_from_text(case.prompt.lower())
            if route_name == "professional_report" and declined and not requested:
                route_name = "recommendation"
            route = RouteDecision(
                route=route_name,
                confidence=1.0,
                reason="App Agent显式请求固定结构推荐Workflow",
                fixed_recommendation_workflow=True,
                agent_policy="capability_based",
            )
        if route.fixed_recommendation_workflow:
            recommendation = self._run_recommendation(case, route=route)
            return {"route": route.to_dict(), "recommendation_set": recommendation.to_dict()}, recommendation
        return {"route": route.to_dict(), "freeform": self._run_freeform(case, route)}, None

    def _run_recommendation(self, case: RecommendationCase, *, route: RouteDecision) -> RecommendationSet:
        run_id = case.run_id or f"recommend_{uuid4().hex[:12]}"
        case = _prepare_recommendation_case(case)
        # Let the selected agent assess genuine information gaps. A research
        # recommendation does not require every executable contract parameter.
        try:
            model_capability = self.agent_port.capability()
            mode = self._workflow_mode(model_capability)
        except Exception as error:
            audit = AuditRecorder("single_agent")
            return self._unavailable_set(case, route, run_id, "single_agent", audit, error)
        audit = AuditRecorder(mode)
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
            output_value={"selected": mode},
            detail={"configured": self.config.agent_mode, "selected": mode},
        )
        audit.append("route", "complete", agent_role=None, input_value={"prompt": case.prompt}, output_value=route.to_dict())
        if self.review_policy_id != "standard-review":
            return self._unavailable_set(
                case, route, run_id, mode, audit,
                RecommenderUnavailable(f"ReviewPolicy未启用：{self.review_policy_id or '<empty>'}"),
            )
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
        audit.append(
            "mode1_role_mapping", "complete", agent_role=None,
            input_value={"wire_roles": list(MODE_ONE_ROLE_NAMES)},
            output_value={"display_names": dict(MODE_ONE_ROLE_NAMES)},
            detail={"model_slots_use_wire_roles": True},
        )
        runner = AgentStepRunner(self.agent_port, mode, audit)
        preset = self.config.multi_agent_preset if mode == "multi_agent" else "sequential-deliberation"
        try:
            if preset == "independent-council":
                return self._run_independent_council(case, route, run_id, mode, audit, runner)
            if preset == "product-trader-loop":
                return self._run_product_trader_loop(case, route, run_id, mode, audit, runner)
            if preset == "constraint-ranking":
                return self._run_constraint_ranking(case, route, run_id, mode, audit, runner)
            return self._run_fixed(case, route, run_id, mode, audit, runner)
        except RecommendationInputRequired as error:
            audit.append(
                "workflow", "pending", agent_role=None,
                input_value={"case": case.analysis_case_id},
                output_value={"missing_information": [error.field], "next_question": error.question},
            )
            return RecommendationSet(
                schema=RECOMMENDATION_SET_SCHEMA, task_id=case.task_id, run_id=run_id,
                analysis_case_id=case.analysis_case_id, catalog_version=case.catalog_version,
                route=route.route, workflow_mode=mode, status="pending_question",
                primary_candidate_id=None, candidates=(), requested_outputs=case.requested_outputs,
                missing_information=(error.field,), next_question=error.question,
                requested_candidate_count=_requested_candidate_count(case), returned_candidate_count=0,
                audit_trail=tuple(audit.events),
            )
        except Exception as error:
            return self._unavailable_set(case, route, run_id, mode, audit, error)

    def _workflow_mode(self, capability: ModelCapability) -> str:
        if not capability.structured_output:
            raise RecommenderUnavailable("当前模型不支持结构化输出，不能生成严格RecommendationSet")
        if self.config.agent_mode == "single":
            return "single_agent"
        if self.config.agent_mode == "multi":
            if not capability.supports_multi_agent_workflow:
                raise RecommenderUnavailable("当前宿主不能提供独立子Agent，无法执行必须多Agent的推荐")
            return "multi_agent"
        return "multi_agent" if capability.supports_multi_agent_workflow else "single_agent"

    def _run_fixed(
        self,
        case: RecommendationCase,
        route: RouteDecision,
        run_id: str,
        mode: str,
        audit: AuditRecorder,
        runner: AgentStepRunner,
    ) -> RecommendationSet:
        intent = runner.run("Interpreter", {
            "prompt": case.prompt,
            "research_context": case.research_context,
            "confirmed_constraints": dict(case.confirmed_constraints),
            "rule": "仅把用户原文或confirmed_constraints中的事实标为已确认；research_context是待核实研究资料，不能作为用户确认。用户允许默认时，明确列出研究假设并继续推荐，不把缺少完整定价条款变成固定问卷；只有确实阻塞本次操作的缺口才追问，一次最多一个next_question。",
        })
        evidence = self._retrieve(case, intent.get("research_queries", ()), audit)
        research = runner.run("Selector", {
            "case": case.to_dict(),
            "intent": dict(intent),
            "evidence": [item.to_dict(include_excerpt=True) for item in evidence],
            "max_candidates": min(_requested_candidate_count(case), self.config.max_candidates),
        })
        critic = runner.run("Reviewer", {
            "case": case.to_dict(),
            "proposals": research.get("proposals", ()),
            "rule": "只审阅已有product_id；新增product_id将导致整步失败。",
        })
        return self._complete_mode_one(
            case, route, run_id, mode, audit, runner,
            evidence=evidence, research=research, critic=critic,
        )

    def _complete_mode_one(
        self,
        case: RecommendationCase,
        route: RouteDecision,
        run_id: str,
        mode: str,
        audit: AuditRecorder,
        step_runner: AgentStepRunner,
        *,
        evidence: Sequence[Any],
        research: Mapping[str, Any],
        critic: Mapping[str, Any],
    ) -> RecommendationSet:
        del step_runner
        candidates, rejected = build_candidates(
            research,
            critic,
            evidence=evidence,
            run_id=run_id,
            max_candidates=min(_requested_candidate_count(case), self.config.max_candidates),
            confirmed_constraints=case.confirmed_constraints,
        )
        return self._candidate_set(case, route, run_id, mode, audit, candidates, rejected)

    def _run_independent_council(
        self,
        case: RecommendationCase,
        route: RouteDecision,
        run_id: str,
        mode: str,
        audit: AuditRecorder,
        runner: AgentStepRunner,
    ) -> RecommendationSet:
        framing = runner.run("Framer", {
            "prompt": case.prompt,
            "research_context": case.research_context,
            "confirmed_constraints": dict(case.confirmed_constraints),
        })
        evidence = self._retrieve(case, framing.get("research_queries", ()), audit)
        shared = {
            "case": case.to_dict(),
            "framing": dict(framing),
            "evidence": [item.to_dict(include_excerpt=True) for item in evidence],
            "max_candidates": min(_requested_candidate_count(case), self.config.max_candidates),
        }
        branches = runner.run_named({"Matcher": shared, "Hedger": shared})
        moderated = runner.run("Moderator", {
            **shared,
            "matcher": dict(branches["Matcher"]),
            "hedger": dict(branches["Hedger"]),
        })
        audit.append(
            "council", "complete", agent_role=None,
            input_value={"roles": ["Framer", "Matcher", "Hedger", "Moderator"]},
            output_value={"proposal_count": len(moderated.get("proposals", ()))},
            detail={"roles": ["Framer", "Matcher", "Hedger", "Moderator"]},
        )
        return self._complete_mode_one(
            case, route, run_id, mode, audit, runner,
            evidence=evidence,
            research={"proposals": moderated.get("proposals", ())},
            critic={"reviews": moderated.get("reviews", ())},
        )

    def _run_product_trader_loop(
        self,
        case: RecommendationCase,
        route: RouteDecision,
        run_id: str,
        mode: str,
        audit: AuditRecorder,
        runner: AgentStepRunner,
    ) -> RecommendationSet:
        evidence = self._retrieve(case, _research_queries(case), audit)
        structurer = runner.run("Structurer", {
            "case": case.to_dict(),
            "evidence": [item.to_dict(include_excerpt=True) for item in evidence],
            "max_candidates": min(_requested_candidate_count(case), self.config.max_candidates),
        })
        candidates, rejected = build_candidates(
            {"proposals": structurer.get("proposals", ())},
            {"reviews": _accepting_reviews(structurer.get("proposals", ()))},
            evidence=evidence,
            run_id=run_id,
            max_candidates=min(_requested_candidate_count(case), self.config.max_candidates),
            confirmed_constraints=case.confirmed_constraints,
        )
        if not candidates:
            return self._candidate_set(case, route, run_id, mode, audit, (), rejected)
        ready = tuple(item for item in candidates if item.library_status == "ready")
        pending = {item.candidate_id: item for item in candidates if item.library_status != "ready"}
        if not ready:
            return self._candidate_set(case, route, run_id, mode, audit, candidates, rejected)
        loop = ProductTraderLoop.start(ready)
        ready_products = {item.product_id for item in ready}
        current_plan = loop.plan_round(_structurer_loop_plan(loop, {
            **dict(structurer), "evaluation_plan": [
                item for item in structurer.get("evaluation_plan", ()) if item.get("product_id") in ready_products
            ],
        }))
        accepted: dict[str, RecommendationCandidate] = {}
        planned_modules = {
            item.candidate.candidate.candidate_id: item.modules for item in current_plan.plans
        }
        while True:
            attached, host_results = self._evaluate_round(case, current_plan, audit)
            trader = runner.run("Trader", {
                "case": case.to_dict(),
                "round": attached.to_dict(),
                "host_results": host_results,
            })
            transition = loop.apply_trader_output(attached, _trader_loop_decisions(attached, trader))
            accepted.update({item.candidate_id: item for item in transition.accepted_candidates})
            audit.append(
                "product_trader_round", "complete", agent_role=None,
                input_value={"round_no": attached.round_no},
                output_value={
                    "accepted_candidate_ids": sorted(accepted),
                    "reworked_candidate_ids": list(transition.reworked_candidate_ids),
                },
            )
            if transition.next_loop is None:
                break
            loop = transition.next_loop
            current_plan = loop.plan_round({
                "proposals": [
                    {
                        "candidate_id": item.candidate.candidate_id,
                        "evaluation_plan": list(planned_modules[item.candidate.candidate_id]),
                        "term_overrides": {},
                    }
                    for item in loop.candidates if item.status == "open"
                ]
            })
        selected = tuple(
            accepted.get(item.candidate_id, pending.get(item.candidate_id))
            for item in candidates if item.candidate_id in accepted or item.candidate_id in pending
        )
        reviewer = runner.run("Reviewer", {
            "candidates": [item.to_dict() for item in selected],
            "rule": "只能整体批准或拒绝当前候选，不得改写当前输入。",
        })
        verdict = parse_reviewer_ranking_verdict(reviewer)
        if not verdict.approved:
            return self._candidate_set(
                case, route, run_id, mode, audit, (),
                (*rejected, {"reason": verdict.reason or "Reviewer拒绝", "source": "Reviewer"}),
            )
        ranked = tuple(replace(item, rank=index) for index, item in enumerate(selected, start=1))
        return self._candidate_set(case, route, run_id, mode, audit, ranked, rejected)

    def _evaluate_round(
        self,
        case: RecommendationCase,
        round_plan: ProductTraderRound,
        audit: AuditRecorder,
    ) -> tuple[ProductTraderRound, Mapping[str, Mapping[str, Any]]]:
        evaluator = getattr(self.tool_port, "evaluate_candidate", None)
        if not callable(evaluator):
            raise RecommenderUnavailable("product-trader-loop需要Hosted候选评估端口")
        host_results: dict[str, Mapping[str, Any]] = {}
        attachable: dict[str, Mapping[str, Any]] = {}
        for plan in round_plan.plans:
            current = plan.candidate.candidate
            response = evaluator(
                candidate=current.to_confirmation_dict(),
                confirmed_constraints=dict(case.confirmed_constraints),
                modules=plan.modules,
                term_overrides=dict(plan.candidate.term_overrides),
                candidate_id=current.candidate_id,
                round_no=round_plan.round_no,
            )
            if not isinstance(response, Mapping):
                raise RecommendationValidationError("Hosted候选评估结果必须为对象")
            row = dict(response)
            _validate_generated_identity(row, current)
            host_results[current.candidate_id] = row
            attachable[current.candidate_id] = {
                key: value for key, value in row.items()
                if key not in {"candidate_id", "product_id", "rule_revision"}
            }
            audit.append(
                "candidate_evaluation", "complete", agent_role=None,
                input_value=current.to_confirmation_dict(),
                output_value={
                    "candidate_id": current.candidate_id,
                    "product_id": current.product_id,
                    "rule_revision": current.rule_revision,
                    "modules": list(plan.modules),
                },
            )
        return attach_host_evaluations(
            round_plan, attachable, tenant_id=case.tenant_id, task_id=case.task_id,
        ), host_results

    def _run_constraint_ranking(
        self,
        case: RecommendationCase,
        route: RouteDecision,
        run_id: str,
        mode: str,
        audit: AuditRecorder,
        runner: AgentStepRunner,
    ) -> RecommendationSet:
        specified = runner.run("Specifier", {
            "prompt": case.prompt,
            "research_context": case.research_context,
            "confirmed_constraints": dict(case.confirmed_constraints),
        })
        spec = parse_specifier_ranking_spec(specified.get("ranking_spec", {}))
        evidence = self._retrieve(case, specified.get("research_queries", ()), audit)
        generated = runner.run("Generator", {
            "case": case.to_dict(),
            "ranking_spec": spec.to_dict(),
            "evidence": [item.to_dict(include_excerpt=True) for item in evidence],
            "max_candidates": min(_requested_candidate_count(case), self.config.max_candidates),
        })
        candidates, rejected = build_candidates(
            generated,
            {"reviews": _accepting_reviews(generated.get("proposals", ()))},
            evidence=evidence,
            run_id=run_id,
            max_candidates=min(_requested_candidate_count(case), self.config.max_candidates),
            confirmed_constraints=case.confirmed_constraints,
        )
        if not candidates:
            return self._candidate_set(case, route, run_id, mode, audit, (), rejected)
        pending = tuple(item for item in candidates if item.library_status != "ready")
        candidates = tuple(item for item in candidates if item.library_status == "ready")
        if not candidates:
            return self._candidate_set(case, route, run_id, mode, audit, pending, rejected)
        rejected = (*rejected, *({
            "candidate_id": item.candidate_id, "product_id": item.product_id,
            "reason": "资料尚未就绪，未参与金融排序", "source": "evidence_gate",
        } for item in pending))
        evaluator = getattr(self.tool_port, "evaluate_candidate", None)
        if not callable(evaluator):
            raise RecommenderUnavailable("constraint-ranking需要Hosted候选评估端口")
        modules = required_modules(spec)
        metric_sources = required_metric_sources(spec)
        metrics_by_candidate: dict[str, Mapping[str, Any]] = {}
        evaluated: list[RecommendationCandidate] = []
        for candidate in candidates:
            response = evaluator(
                candidate=candidate.to_confirmation_dict(),
                confirmed_constraints=dict(case.confirmed_constraints),
                modules=modules,
                term_overrides=dict(candidate.current_inputs.get("term_overrides", {})),
                candidate_id=candidate.candidate_id,
                round_no=1,
            )
            if not isinstance(response, Mapping):
                raise RecommendationValidationError("Hosted候选评估结果必须为对象")
            row = dict(response)
            _validate_generated_identity(row, candidate)
            records = read_evaluation_records(
                row, candidate_id=candidate.candidate_id, modules=modules, round_no=1,
                tenant_id=case.tenant_id, task_id=case.task_id,
            )
            evaluated.append(replace(
                candidate, evaluation_records=records,
                module_run_refs=tuple(item.module_run_ref for item in records if item.module_run_ref is not None),
                module_statuses={item.module: item.status for item in records},
            ))
            metrics_by_candidate[candidate.candidate_id] = _ranking_metrics(row, metric_sources, records)
            audit.append(
                "candidate_evaluation", "complete", agent_role=None,
                input_value=candidate.to_confirmation_dict(),
                output_value={
                    "candidate_id": candidate.candidate_id,
                    "product_id": candidate.product_id,
                    "rule_revision": candidate.rule_revision,
                    "metric_sources": sorted(set(metric_sources.values())),
                },
            )
        ranking = rank_candidates(spec, evaluated, metrics_by_candidate)
        reviewer = runner.run("Reviewer", {
            "ranking_spec": spec.to_dict(),
            "ranking_decisions": [item.to_dict() for item in ranking.decisions],
            "candidates": [item.to_dict() for item in ranking.ranked_candidates],
        })
        reviewed = apply_reviewer_verdict(ranking, parse_reviewer_ranking_verdict(reviewer))
        ranking_rejected = tuple(
            {"candidate_id": item.candidate_id, "reason": ";".join(item.reasons), "source": "constraint_ranking"}
            for item in ranking.exclusions
        )
        if not reviewed.approved:
            return self._candidate_set(
                case, route, run_id, mode, audit, (),
                (*rejected, *ranking_rejected, {"reason": reviewed.rejection_reason or "Reviewer拒绝", "source": "Reviewer"}),
                ranking_spec=spec.to_dict(),
            )
        return self._candidate_set(
            case, route, run_id, mode, audit,
            reviewed.ranking.ranked_candidates,
            (*rejected, *ranking_rejected),
            ranking_spec=spec.to_dict(),
        )

    def _retrieve(
        self,
        case: RecommendationCase,
        raw_queries: object,
        audit: AuditRecorder,
    ) -> tuple[Any, ...]:
        if self.knowledge_port is None:
            raise RecommenderUnavailable("结构推荐需要Knowledger正式端口")
        queries = _strings(raw_queries) or _research_queries(case)
        evidence = retrieve_evidence(
            self.knowledge_port,
            catalog_version=case.catalog_version,
            queries=queries[: self.config.max_research_queries],
        )
        if not evidence:
            fallback = _research_queries(case)[: self.config.max_research_queries]
            if tuple(queries) != fallback:
                evidence = retrieve_evidence(self.knowledge_port, catalog_version=case.catalog_version, queries=fallback)
                queries = tuple(dict.fromkeys((*queries, *fallback)))
        audit.append(
            "evidence", "complete" if evidence else "empty", agent_role=None,
            input_value={"queries": queries},
            output_value={
                "evidence_ids": [item.evidence_id for item in evidence],
                "catalog_version": case.catalog_version,
            },
        )
        return evidence

    def _candidate_set(
        self,
        case: RecommendationCase,
        route: RouteDecision,
        run_id: str,
        mode: str,
        audit: AuditRecorder,
        candidates: Sequence[RecommendationCandidate],
        rejected: Sequence[Mapping[str, Any]],
        *,
        ranking_spec: Mapping[str, Any] | None = None,
    ) -> RecommendationSet:
        current = tuple(
            replace(item, rank=index, candidate_status=(
                "pending_confirmation" if item.library_status == "ready" else "pending_terms"
            ))
            for index, item in enumerate(candidates, start=1)
        )
        audit.append(
            "aggregation", "complete" if current else "failed", agent_role=None,
            input_value={"requested_candidate_count": _requested_candidate_count(case)},
            output_value={
                "candidate_ids": [item.candidate_id for item in current],
                "current_inputs": [item.to_confirmation_dict() for item in current],
            },
        )
        limitations: tuple[str, ...] = ()
        if not current:
            limitations = ("当前证据和约束未形成可确认候选。",)
        elif len(current) < _requested_candidate_count(case):
            limitations = (f"仅有{len(current)}个候选通过当前证据与约束校验。",)
        if any(item.library_status != "ready" for item in current):
            limitations += ("部分候选资料尚未就绪，仅保留研究说明，未执行金融计算。",)
        confirmable = any(item.library_status == "ready" for item in current)
        spec_id = str(ranking_spec.get("ranking_spec_id")) if ranking_spec is not None else None
        return RecommendationSet(
            schema=RECOMMENDATION_SET_SCHEMA,
            task_id=case.task_id,
            run_id=run_id,
            analysis_case_id=case.analysis_case_id,
            catalog_version=case.catalog_version,
            route=route.route,
            workflow_mode=mode,
            status="pending_approval" if confirmable else "partial" if current else "unavailable",
            primary_candidate_id=current[0].candidate_id if current else None,
            candidates=current,
            requested_outputs=case.requested_outputs,
            rejected=tuple(dict(item) for item in rejected),
            limitations=limitations,
            audit_trail=tuple(audit.events),
            requested_candidate_count=_requested_candidate_count(case),
            returned_candidate_count=len(current),
            ranking_spec_id=spec_id,
            ranking_spec=ranking_spec,
            delivery_status=(
                "pending" if confirmable else "unavailable"
            ) if case.requested_outputs else "not_requested",
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
        audit.append(
            "workflow", "failed", agent_role=None,
            input_value={"case": case.analysis_case_id},
            output_value=None,
            detail={"error_type": type(error).__name__, "message": str(error)},
        )
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
            limitations=(str(error),),
            audit_trail=tuple(audit.events),
            requested_candidate_count=_requested_candidate_count(case),
            returned_candidate_count=0,
        )

    def _run_freeform(self, case: RecommendationCase, route: RouteDecision) -> Mapping[str, Any]:
        audit = AuditRecorder("single_agent")
        audit.append("route", "complete", agent_role=None, input_value={"prompt": case.prompt}, output_value=route.to_dict())
        try:
            return self._run_freeform_loop(case, route, audit)
        except Exception as error:
            audit.append(
                "workflow", "failed", agent_role=None,
                input_value={"case": case.analysis_case_id}, output_value=None,
                detail={"error_type": type(error).__name__, "message": str(error)},
            )
            return {
                "response": "", "status": "unavailable", "tool_results": [],
                "limitations": [str(error)],
                "audit_trail": [item.to_dict() for item in audit.events],
            }

    def _run_freeform_loop(
        self,
        case: RecommendationCase,
        route: RouteDecision,
        audit: AuditRecorder,
    ) -> Mapping[str, Any]:
        runner = AgentStepRunner(self.agent_port, "single_agent", audit)
        tool_results: list[Mapping[str, Any]] = []
        seen_results: dict[str, Mapping[str, Any]] = {}
        allowed_modules = _ROUTE_TOOLS.get(route.route, frozenset())
        for _ in range(self.config.max_agent_rounds):
            result = runner.run("Freeform", {
                "prompt": case.prompt,
                "research_context": case.research_context,
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
                return {
                    "response": response_text,
                    "status": status,
                    "tool_results": tool_results,
                    "audit_trail": [item.to_dict() for item in audit.events],
                }
            if isinstance(requests, (str, bytes)) or not isinstance(requests, Sequence):
                raise ValueError("Freeform.tool_requests必须为数组")
            for raw in requests:
                if not isinstance(raw, Mapping):
                    raise ValueError("Freeform.tool_request必须为对象")
                module = str(raw.get("module", "")).strip().lower()
                tool_request = raw.get("request", {})
                if not isinstance(tool_request, Mapping):
                    raise ValueError("Freeform工具请求必须为对象")
                call_id = canonical_hash({"module": module, "request": dict(tool_request)})
                if call_id in seen_results:
                    reused = {**dict(seen_results[call_id]), "reused": True}
                    tool_results.append(reused)
                    audit.append(f"tool.{module or 'unknown'}", "reused", agent_role="SingleAgent.Freeform", input_value=tool_request, output_value=reused)
                    continue
                try:
                    if module not in allowed_modules:
                        raise ValueError(f"路由{route.route}无权调用工具：{module or '<empty>'}")
                    if module == "knowledger":
                        if self.knowledge_port is None:
                            raise RecommenderUnavailable("未配置Knowledger正式端口")
                        payload = dict(tool_request)
                        payload["catalog_version"] = case.catalog_version
                        response = self.knowledge_port.search(payload)
                    else:
                        if self.tool_port is None:
                            raise RecommenderUnavailable("未配置Tool Gateway正式端口")
                        response = self.tool_port.call(module, tool_request)
                    module_status = str(response.get("status", "")).strip().lower()
                    failed = response.get("ok") is False or module_status in {
                        "partial", "failed", "unsupported", "cancelled", "timed_out", "unavailable",
                    }
                    outcome = "partial" if module_status == "partial" else "failed" if failed else "returned"
                    tool_result = {
                        "module": module,
                        "status": outcome,
                        "module_status": module_status or None,
                        "result": dict(response),
                    }
                    tool_results.append(tool_result)
                    seen_results[call_id] = tool_result
                    audit.append(f"tool.{module}", outcome, agent_role="SingleAgent.Freeform", input_value=tool_request, output_value=response)
                except Exception as error:
                    tool_result = {"module": module, "status": "failed", "error": str(error)}
                    tool_results.append(tool_result)
                    seen_results[call_id] = tool_result
                    audit.append(
                        f"tool.{module or 'unknown'}", "failed", agent_role="SingleAgent.Freeform",
                        input_value=tool_request, output_value=None,
                        detail={"error_type": type(error).__name__, "message": str(error)},
                    )
        return {
            "response": "单Agent达到最大工具轮次，未生成最终答复。",
            "tool_results": tool_results,
            "audit_trail": [item.to_dict() for item in audit.events],
            "status": "partial",
        }


def _case_from_request(request: RecommendationCase | Mapping[str, Any]) -> RecommendationCase:
    if isinstance(request, RecommendationCase):
        return request
    data = dict(request)
    for key in ("requested_candidate_count", "candidate_count", "selection_spec", "candidate_selection"):
        data.pop(key, None)
    return RecommendationCase.from_mapping(data)


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


def _structurer_loop_plan(loop: ProductTraderLoop, output: Mapping[str, Any]) -> Mapping[str, Any]:
    raw_plans = output.get("evaluation_plan", ())
    if isinstance(raw_plans, (str, bytes)) or not isinstance(raw_plans, Sequence):
        raise RecommendationValidationError("Structurer.evaluation_plan必须为数组")
    states = {item.candidate.product_id: item for item in loop.candidates if item.status == "open"}
    plans: dict[str, Mapping[str, Any]] = {}
    for raw in raw_plans:
        if not isinstance(raw, Mapping):
            raise RecommendationValidationError("Structurer.evaluation_plan必须为对象数组")
        product_id = str(raw.get("product_id", "")).strip()
        if product_id not in states or product_id in plans:
            raise RecommendationValidationError("Structurer.evaluation_plan产品无效或重复")
        plans[product_id] = raw
    if set(plans) != set(states):
        raise RecommendationValidationError("Structurer.evaluation_plan必须覆盖全部当前候选")
    return {
        "proposals": [
            {
                "candidate_id": state.candidate.candidate_id,
                "evaluation_plan": list(plans[product_id].get("modules", ())),
                "term_overrides": dict(plans[product_id].get("term_overrides", {})),
            }
            for product_id, state in states.items()
        ]
    }


def _trader_loop_decisions(
    round_plan: ProductTraderRound,
    output: Mapping[str, Any],
) -> Mapping[str, Any]:
    raw_rows = output.get("evaluations", ())
    if isinstance(raw_rows, (str, bytes)) or not isinstance(raw_rows, Sequence):
        raise RecommendationValidationError("Trader.evaluations必须为数组")
    expected = {
        item.candidate.candidate.candidate_id: item.candidate.candidate.product_id
        for item in round_plan.plans
    }
    decisions: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise RecommendationValidationError("Trader.evaluations必须为对象数组")
        candidate_id = str(raw.get("candidate_id", "")).strip()
        product_id = str(raw.get("product_id", "")).strip()
        if candidate_id not in expected or expected[candidate_id] != product_id or candidate_id in seen:
            raise RecommendationValidationError("Trader.evaluations未绑定当前candidate_id与product_id")
        seen.add(candidate_id)
        decisions.append({
            "candidate_id": candidate_id,
            "action": raw.get("decision"),
            "term_adjustments": raw.get("term_adjustments", {}),
        })
    if seen != set(expected):
        raise RecommendationValidationError("Trader.evaluations必须覆盖本轮全部候选")
    return {"decisions": decisions}


def _validate_generated_identity(value: Mapping[str, Any], candidate: RecommendationCandidate) -> None:
    if type(value.get("rule_revision")) is not int:
        raise RecommendationValidationError("Hosted结果的rule_revision必须为正整数")
    expected = {
        "candidate_id": candidate.candidate_id,
        "product_id": candidate.product_id,
        "rule_revision": candidate.rule_revision,
    }
    for field_name, expected_value in expected.items():
        if value.get(field_name) != expected_value:
            raise RecommendationValidationError(f"Hosted结果未绑定当前{field_name}")


def _ranking_metrics(
    value: Mapping[str, Any],
    metric_sources: Mapping[str, str],
    records: Sequence[EvaluationRecord],
) -> Mapping[str, Mapping[str, Any]]:
    raw = value.get("verified_metrics")
    if not isinstance(raw, Mapping):
        raise RecommendationValidationError("Hosted候选评估缺少verified_metrics")
    source_records = {item.module: item for item in records}
    nested_shape = set(raw) <= set(METRIC_SOURCES.values())
    result: dict[str, dict[str, Any]] = {}
    for metric, source in metric_sources.items():
        if nested_shape:
            for supplied_source, metrics in raw.items():
                if not isinstance(metrics, Mapping):
                    raise RecommendationValidationError("Hosted候选评估指标来源必须为对象")
                if metric in metrics and supplied_source != source:
                    raise RecommendationValidationError(f"Hosted候选评估含来源不一致指标：{metric}")
            entry = raw.get(source, {}).get(metric)
        else:
            fact_path = f"greeks.{metric}" if metric in {"delta", "gamma", "vega", "theta", "rho"} else metric
            if fact_path != metric and metric in raw and fact_path in raw:
                raise RecommendationValidationError(f"Hosted指标{metric}包含重复事实路径")
            entry = raw.get(metric, raw.get(fact_path))
            if isinstance(entry, Mapping):
                if entry.get("source") != source or not isinstance(entry.get("fact_ref"), str) or not entry["fact_ref"].strip():
                    raise RecommendationValidationError(f"Hosted指标{metric}缺少匹配来源和FactRef")
                entry = entry.get("value")
        if entry is None:
            continue
        if source != "contract_terms":
            record = source_records.get(source)
            if record is None or record.module_run_ref is None or record.status not in {"succeeded", "partial"}:
                raise RecommendationValidationError(f"Hosted指标{metric}缺少可消费的{source} ModuleRunRef")
        result.setdefault(source, {})[metric] = entry
    return result


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _research_queries(case: RecommendationCase) -> tuple[str, ...]:
    constraints = case.confirmed_constraints
    view = str(constraints.get("market_view", ""))
    terms = [str(constraints.get("underlying", "")).strip()]
    if "上涨" in view:
        terms.append("看涨期权")
    elif "看跌" in view:
        terms.append("看跌期权")
    terms.append(case.prompt)
    return tuple(dict.fromkeys(item for item in terms if item))


def _requested_candidate_count(case: RecommendationCase) -> int:
    value = case.confirmed_constraints.get("_requested_candidate_count")
    if value is None:
        value = requested_candidate_count_from_text(case.prompt) or DEFAULT_REQUESTED_CANDIDATE_COUNT
    return _normalize_requested_candidate_count((value,))


def _requested_candidate_count_from_request(request: RecommendationCase | Mapping[str, Any]) -> int:
    values: list[object] = []
    if isinstance(request, Mapping):
        for key in ("requested_candidate_count", "candidate_count", "selection_spec", "candidate_selection"):
            if key in request:
                values.append(request[key])
        constraints = request.get("confirmed_constraints")
        if isinstance(constraints, Mapping):
            for key in ("requested_candidate_count", "candidate_count", "selection_spec", "candidate_selection"):
                if key in constraints:
                    values.append(constraints[key])
        prompt = str(request.get("prompt", ""))
    else:
        prompt = request.prompt
        if "_requested_candidate_count" in request.confirmed_constraints:
            values.append(request.confirmed_constraints["_requested_candidate_count"])
    if not values:
        natural = requested_candidate_count_from_text(prompt)
        if natural is not None:
            values.append(natural)
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
    count = _requested_candidate_count(case)
    constraints = merge_confirmed_constraints(case.confirmed_constraints, (case.prompt,))
    constraints["_requested_candidate_count"] = count
    prepared = replace(case, confirmed_constraints=constraints)
    return replace(prepared, requested_outputs=_requested_outputs(prepared))


def _requested_outputs(case: RecommendationCase) -> tuple[str, ...]:
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
            if isinstance(service, RecommenderService):
                result, recommendation = service._recommend_with_domain(
                    body,
                    workflow=workflow if action == "recommend_fixed" else None,
                )
            else:
                recommendation = None
                result = (
                    service.recommend(body)
                    if action == "recommend"
                    else service.recommend_fixed(body, workflow=workflow)
                )
            recommendation_value = result.get("recommendation_set")
            if isinstance(recommendation_value, Mapping):
                status = str(recommendation_value.get("status", "failed"))
            else:
                freeform = result.get("freeform")
                status = str(freeform.get("status", "completed")) if isinstance(freeform, Mapping) else "failed"
            return {
                "ok": status not in {"partial", "failed", "unavailable"},
                "module": "recommender",
                "status": status,
                "result": _public_result(result, recommendation=recommendation),
            }
    except Exception as error:
        return {
            "ok": False,
            "module": "recommender",
            "status": "failed",
            "error": {"type": type(error).__name__, "message": "推荐服务当前不可用，请稍后重试。"},
        }
    return {
        "ok": False,
        "module": "recommender",
        "status": "unsupported",
        "message": f"Recommender不支持action={action}；支持status、recommend、recommend_fixed。",
    }


def _public_result(
    result: Mapping[str, Any],
    *,
    recommendation: RecommendationSet | None = None,
) -> Mapping[str, Any]:
    if isinstance(recommendation, RecommendationSet):
        return recommendation.to_public_dict()
    freeform = result.get("freeform")
    if isinstance(freeform, Mapping):
        response = public_text(str(freeform.get("response", "")))
        return {"说明": response or "当前未形成可展示结果。", "状态": str(freeform.get("status", "unavailable"))}
    return {"说明": "当前未形成可展示结果。"}
