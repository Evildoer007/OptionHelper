"""Recommender唯一领域入口与自然语言路由。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .agent_steps import AuditRecorder, AgentStepRunner, canonical_hash
from .candidate_builder import build_candidates
from .config import RecommenderConfig
from .evidence_retriever import retrieve_evidence
from .executor import ALLOWED_MODULES, execute
from .interaction import merge_confirmed_constraints, missing_required_constraints, question_for_missing_constraints
from .intent_router import route_intent
from .models import CandidateContract, ModelCapability, RECOMMENDATION_SET_SCHEMA, RecommendationCase, RecommendationSet, RouteDecision, RecommendationValidationError, public_text
from .ports import AgentPort, HttpAgentPort, HttpEndpoint, HttpKnowledgePort, HttpToolPort, KnowledgePort, ToolPort


class RecommenderUnavailable(RuntimeError):
    """必要正式端口未注入或不可达。"""


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
        "agent_roles": ["Intent", "Research", "Critic", "Executor"],
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
    ) -> None:
        self.agent_port = agent_port
        self.knowledge_port = knowledge_port
        self.tool_port = tool_port
        self.config = config or RecommenderConfig()

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
        if workflow is not None:
            route_name = str(workflow).strip().lower()
            if route_name not in {"recommendation", "professional_report"}:
                raise ValueError("固定Recommender workflow仅支持recommendation或professional_report")
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
        case = replace(
            case,
            confirmed_constraints=merge_confirmed_constraints(case.confirmed_constraints, (case.prompt,)),
        )
        case = replace(case, requested_outputs=_requested_outputs(case))
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
        initial_mode = self._workflow_mode(model_capability)
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
        try:
            return self._run_fixed(case, route, run_id, initial_mode, audit)
        except Exception as first_error:
            tool_started = any(event.stage.startswith("tool.") for event in audit.events)
            if initial_mode == "multi_agent" and not tool_started:
                audit.append("fallback", "degraded", agent_role=None, input_value={"from": "multi_agent"},
                             output_value={"to": "degraded_single_agent"},
                             detail={"error_type": type(first_error).__name__, "message": str(first_error)})
                audit.mode = "degraded_single_agent"
                try:
                    return self._run_fixed(case, route, run_id, "degraded_single_agent", audit)
                except Exception as second_error:
                    return self._unavailable_set(case, route, run_id, "degraded_single_agent", audit, second_error)
            return self._unavailable_set(case, route, run_id, initial_mode, audit, first_error)

    def _workflow_mode(self, capability: ModelCapability) -> str:
        if self.config.agent_mode == "single":
            return "single_agent"
        if self.config.agent_mode == "multi":
            return "multi_agent" if capability.supports_multi_agent_workflow else "single_agent"
        return "multi_agent" if capability.supports_multi_agent_workflow else "single_agent"

    def _run_fixed(
        self,
        case: RecommendationCase,
        route: RouteDecision,
        run_id: str,
        mode: str,
        audit: AuditRecorder,
    ) -> RecommendationSet:
        runner = AgentStepRunner(
            port=self.agent_port,
            workflow_mode="multi_agent" if mode == "multi_agent" else "single_agent",
            audit=audit,
        )
        intent = runner.run("Intent", {
            "prompt": case.prompt,
            "confirmed_constraints": dict(case.confirmed_constraints),
            "rule": "仅把用户原文或confirmed_constraints中的事实标为已确认；一次最多生成一个next_question。",
        })
        queries = _strings(intent.get("research_queries", ()))[: self.config.max_research_queries]
        if self.knowledge_port is None:
            raise RecommenderUnavailable("结构推荐需要Knowledger正式端口")
        evidence = retrieve_evidence(
            self.knowledge_port,
            catalog_version=case.catalog_version,
            queries=queries or _research_queries(case),
        )
        audit.append("evidence", "complete", agent_role=None, input_value={"queries": queries},
                     output_value={"evidence_ids": [item.evidence_id for item in evidence], "catalog_version": case.catalog_version})
        research = runner.run("Research", {
            "case": case.to_dict(),
            "intent": dict(intent),
            "evidence": [item.to_dict(include_excerpt=True) for item in evidence],
            "max_candidates": self.config.max_candidates,
        })
        critic = runner.run("Critic", {
            "case": case.to_dict(),
            "proposals": research.get("proposals", ()),
            "rule": "只审阅已有product_id；新增product_id将导致整步失败。",
        })
        candidates, rejected = build_candidates(
            research, critic, evidence=evidence, run_id=run_id, max_candidates=self.config.max_candidates,
            confirmed_constraints=case.confirmed_constraints,
        )
        audit.append("aggregation", "complete", agent_role=None,
                     input_value={"research": research, "critic": critic},
                     output_value={"candidate_ids": [item.candidate_id for item in candidates], "rejected": rejected})
        if not candidates:
            return RecommendationSet(
                schema=RECOMMENDATION_SET_SCHEMA, task_id=case.task_id, run_id=run_id,
                analysis_case_id=case.analysis_case_id, catalog_version=case.catalog_version, route=route.route,
                workflow_mode=mode, status="unavailable", primary_candidate_id=None, candidates=(), rejected=rejected,
                requested_outputs=case.requested_outputs,
                limitations=("受控证据与反证门禁后没有可输出候选。",), audit_trail=tuple(audit.events),
            )
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
            analysis_status = "not_started"
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
            analysis_status=analysis_status,
            delivery_status="pending" if report_requested else "not_requested",
            next_question=_CONTRACT_CONFIRMATION_QUESTION if not approved and needs_confirmation else None,
            audit_trail=tuple(audit.events),
            limitations=tuple(limitations),
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


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


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


def _requested_outputs(case: RecommendationCase) -> tuple[str, ...]:
    """合并显式调用参数与客户已确认的交付偏好，不推断计算模块。"""

    requested = [str(item).strip().lower() for item in case.requested_outputs if str(item).strip()]
    output_type = str(case.confirmed_constraints.get("output_type", "")).strip().lower()
    deliveries = {
        "card": ("card",),
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
