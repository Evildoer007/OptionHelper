"""In-process App adapters for the fixed Recommender workflow.

This module deliberately does not expose Recommender freeform to OptChat.  It
adapts the existing typed ports to App-owned identity, task, context and Tool
Gateway boundaries without a localhost HTTP hop.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date, timedelta
from runpy import run_path
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

_CORE_SRC = Path(__file__).resolve().parents[4] / "core" / "src"
if str(_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(_CORE_SRC))

from modules.recommender.models import (
    RECOMMENDATION_SET_SCHEMA,
    ModelCapability,
    RecommendationCandidate,
    RecommendationCase,
    RecommendationSet,
)
from modules.recommender.ports import AgentPort, KnowledgePort, ToolPort
from modules.recommender.service import RecommenderService
from modules.recommender.interaction import constraints_fingerprint, merge_confirmed_constraints
from runtime.protocol.models import ModuleRunRef

from ..errors import AuthorizationError, UnavailableCapabilityError, ValidationError
from ..identity.session_identity import SessionIdentity
from ..model_gateway.gateway import ModelGateway
from ..page_registry import PageRegistry
from ..stores.contract_store import ContractStore
from ..stores.result_store import ResultStore
from ..task_runtime.task_service import TaskService
from .tool_dispatcher import ToolDispatcher


_REPORT_MODULES = ("payoff", "pricing", "backtest")
_REPORT_TO_COMPUTE_MODULE = {"payoff": "payoffer", "pricing": "pricer", "backtest": "backtester"}
_MODULE_RUN_REF_FIELDS = (
    "module", "tenant_id", "task_id", "run_id",
    "expected_semantic_result_hash", "expected_artifact_manifest_hash",
)


class AppAgentPort(AgentPort):
    """Maps Recommender's typed step requests to the provider-neutral gateway."""

    def __init__(self, gateway: ModelGateway, identity: SessionIdentity, task_id: str) -> None:
        self._gateway = gateway
        self._identity = identity
        self._task_id = task_id

    def capability(self) -> ModelCapability:
        capability = self._gateway.capability_for(self._identity)
        return ModelCapability.from_mapping(capability)

    def run_step(self, role: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        response = self._gateway.decide_for(self._identity, self._task_id, {
            "operation": "recommender_fixed_step",
            "role": str(role),
            "input": _safe_model_payload(payload),
            "rule": "只返回该步骤的结构化result；不得调用工具、不得产生金融数值。",
        })
        if not isinstance(response, Mapping) or set(response) != {"action", "result"} or response.get("action") != "final":
            raise ValidationError("Recommender步骤必须返回{action:'final',result:{...}}")
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise ValidationError("Recommender步骤result必须为对象")
        if _contains_hidden_model_field(result):
            raise ValidationError("Recommender步骤不得返回推理或Secret字段")
        return dict(result)


class AppKnowledgePort(KnowledgePort):
    """Read-only Knowledger projection with bounded OptionList/OptionLib evidence."""

    def __init__(self, capability_root: Path, catalog_version: str) -> None:
        self._root = capability_root
        self._catalog_version = catalog_version

    def search(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(payload.get("catalog_version", "")) != self._catalog_version:
            raise ValidationError("Knowledger查询版本与当前任务不一致")
        queries = payload.get("queries", ())
        if isinstance(queries, str):
            queries = (queries,)
        if not isinstance(queries, Sequence):
            raise ValidationError("Knowledger查询必须是字符串数组")
        query_text = " ".join(str(item).strip().lower() for item in queries if str(item).strip())
        registry = _load_capability_registry(self._root)
        rows = [
            (product_id, product)
            for product_id, product in registry["products"].items()
            if _matches(query_text, product_id, product)
        ][:3]
        evidence: list[dict[str, Any]] = []
        for product_id, product in rows:
            identity = dict(product.get("identity", {}))
            name = str(identity.get("name_zh", product_id))
            list_excerpt = _optionlist_excerpt(self._root / "references" / "optionlist.md", product_id, name)
            lib_excerpt = _optionlib_excerpt(self._root / "references" / "optionlib.md", product_id, name)
            status_excerpt = f"{product_id} {name} entry_status={bool(identity.get('entry_status'))}"
            evidence.extend((
                _evidence(product_id, "optionlist", list_excerpt or f"OptionList未找到{product_id}身份记录", identity,
                          bool(identity.get("entry_status")), self._catalog_version, "ready" if list_excerpt else "unavailable"),
                _evidence(product_id, "optionlib", lib_excerpt or f"OptionLib未找到{product_id}章节", identity,
                          bool(identity.get("entry_status")), self._catalog_version, "ready" if lib_excerpt else "unavailable"),
                _evidence(product_id, "optionreg_status", status_excerpt, identity, bool(identity.get("entry_status")), self._catalog_version),
            ))
        return {"ok": True, "catalog_version": self._catalog_version, "evidence": evidence}


class AppToolPort(ToolPort):
    """Recommender executor port bound to one App identity and task."""

    def __init__(self, executor: "AppConversationToolExecutor", identity: SessionIdentity, task_id: str) -> None:
        self._executor = executor
        self._identity = identity
        self._task_id = task_id

    def call(self, module: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._executor.call(self._identity, self._task_id, f"{module}.run", payload)


class RecommenderAdapter:
    """Run exactly one fixed Recommendation Workflow for an OptChat task."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        registry: PageRegistry,
        task_service: TaskService,
        tool_executor: "AppConversationToolExecutor",
    ) -> None:
        self._gateway = gateway
        self._registry = registry
        self._tasks = task_service
        self._tools = tool_executor

    def run_fixed(self, identity: SessionIdentity, task_id: str, prompt: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        task = self._tasks.get(identity, task_id)
        catalog_version = str(self._registry.manifest["catalog_version"])
        messages = task.get("messages", [])
        history = list(messages) if isinstance(messages, list) else []
        if not history or not _same_pending_user_turn(history[-1], prompt):
            history.append({"role": "user", "content": str(prompt), "status": "pending_model"})
        confirmed_constraints = merge_confirmed_constraints({}, history)
        pending = self._tasks.pending_recommendation(identity, task_id)
        # TaskService intentionally keeps a compact, non-financial continuation
        # record. Product-owned term overrides remain in the authenticated task
        # conversation and are deterministically recovered from the turns that
        # preceded the current reply. This keeps one source of truth without
        # weakening TaskService's persisted-state allow-list.
        stored_constraints = _pending_constraints_from_history(pending, history)
        if _may_resume_pending(
            pending,
            confirmed_constraints,
            catalog_version,
            explicit_candidate_confirmation=_approval_text(prompt),
            stored_constraints=stored_constraints,
        ):
            assert pending is not None
            expected = _current_candidate_binding(
                pending["candidate"],
                stored_constraints,
                self._registry.capability_root,
            )
            if expected is None:
                return _confirmation_unavailable(task_id, catalog_version, pending)
            if not _binding_matches_pending(expected, pending, constraints=stored_constraints):
                return _confirmation_invalidated(task_id, catalog_version, pending)
            requested_delivery = _delivery_from_constraints(confirmed_constraints)
            if pending is not None and requested_delivery is not None and requested_delivery != pending.get("delivery"):
                pending = self._tasks.choose_pending_recommendation_delivery(
                    identity,
                    task_id,
                    requested_delivery,
                    confirmed_constraints=_persisted_constraints(confirmed_constraints),
                )
                if pending is None:
                    return _confirmation_invalidated(task_id, catalog_version)
                expected = _current_candidate_binding(
                    pending["candidate"],
                    _pending_constraints_from_history(pending, history),
                    self._registry.capability_root,
                )
                if expected is None or not _binding_matches_pending(
                    expected, pending, constraints=_pending_constraints_from_history(pending, history),
                ):
                    return _confirmation_invalidated(task_id, catalog_version, pending)
            if pending is not None and pending.get("delivery") is None:
                approved = self._tasks.approve_pending_recommendation(identity, task_id)
                if approved is None or not _binding_matches_pending(expected, approved, constraints=stored_constraints):
                    return _confirmation_invalidated(task_id, catalog_version, pending)
                return {
                    "route": {"fixed_recommendation_workflow": True, "route": "recommendation"},
                    "recommendation_set": _continuation_recommendation_set(
                        task_id, catalog_version, approved, status="pending_approval", candidate_status="approved",
                    ),
                    "delivery": {
                        "status": "needs_input",
                        "next_step": "候选已确认。如需形成交付材料，请选择研究简报或完整研究报告；若只需要当前分析结论，也可以直接继续讨论。",
                    },
                }
            approved = self._tasks.approve_pending_recommendation(identity, task_id)
            if approved is not None:
                if not _binding_matches_pending(
                    expected, approved, constraints=_pending_constraints_from_history(approved, history),
                ):
                    return _confirmation_invalidated(task_id, catalog_version, approved)
                delivery_requests = _deliveries_from_constraints(approved["confirmed_constraints"])
                if len(delivery_requests) > 1:
                    delivery = self._tools.run_recommendation_deliveries(
                        identity,
                        task_id,
                        analysis_case_id=str(approved["analysis_case_id"]),
                        catalog_version=catalog_version,
                        candidate=approved["candidate"],
                        deliveries=delivery_requests,
                        confirmed_constraints=_pending_constraints_from_history(approved, history),
                    )
                else:
                    delivery = self._tools.run_recommendation_delivery(
                        identity,
                        task_id,
                        analysis_case_id=str(approved["analysis_case_id"]),
                        catalog_version=catalog_version,
                        candidate=approved["candidate"],
                        delivery=approved["delivery"],
                        confirmed_constraints=_pending_constraints_from_history(approved, history),
                    )
                if delivery.get("status") == "completed":
                    self._tasks.complete_pending_recommendation(identity, task_id)
                analysis_status = str(delivery.get("analysis_status", "not_started")).strip().lower()
                delivery_status = str(
                    delivery.get("delivery_status", "completed" if delivery.get("status") == "completed" else "partial")
                ).strip().lower()
                return {
                    "route": {"fixed_recommendation_workflow": True, "route": "recommendation"},
                    "recommendation_set": _continuation_recommendation_set(
                        task_id,
                        catalog_version,
                        approved,
                        status="completed" if analysis_status == "completed" and delivery_status == "completed" else "partial",
                        candidate_status="approved",
                        analysis_status=analysis_status,
                        delivery_status=delivery_status,
                    ),
                    "delivery": delivery,
                }
        requested_outputs = _requested_outputs(arguments, confirmed_constraints)
        case = RecommendationCase(
            analysis_case_id=f"chat-{task_id}",
            task_id=task_id,
            tenant_id=identity.tenant_id,
            prompt=str(prompt).strip(),
            catalog_version=catalog_version,
            run_id=f"recommend-{task_id}",
            requested_outputs=requested_outputs,
            audience=identity.audience,
            conversation_ref=f"task:{task_id}",
            confirmed_constraints=confirmed_constraints,
            approved_candidate_ids=(),
            candidate_contracts={},
        )
        service = RecommenderService(
            agent_port=AppAgentPort(self._gateway, identity, task_id),
            knowledge_port=AppKnowledgePort(self._registry.capability_root, catalog_version),
            tool_port=AppToolPort(self._tools, identity, task_id),
        )
        workflow = str(arguments.get("workflow", "recommendation")).strip().lower()
        result = service.recommend_fixed(case, workflow=workflow)
        route = result.get("route", {})
        if not isinstance(route, Mapping) or route.get("fixed_recommendation_workflow") is not True:
            raise ValidationError("App内Recommender只允许固定推荐Workflow，不允许freeform")
        if "freeform" in result:
            raise ValidationError("App内Recommender不得返回freeform")
        recommendation = result.get("recommendation_set")
        selected_candidate_id = _selected_candidate_id(prompt, recommendation)
        continuation = _pending_recommendation_state(
            recommendation,
            analysis_case_id=case.analysis_case_id,
            catalog_version=catalog_version,
            confirmed_constraints=confirmed_constraints,
            selected_candidate_id=selected_candidate_id,
        )
        if continuation is not None:
            binding = _current_candidate_binding(
                continuation["candidate"],
                continuation["confirmed_constraints"],
                self._registry.capability_root,
            )
            if binding is None:
                return _confirmation_unavailable(task_id, catalog_version)
            continuation["analysis_case_id"] = _confirmation_analysis_case_id(binding["contract_fingerprint"])
            continuation["workflow_mode"] = str(recommendation.get("workflow_mode", "single_agent"))
            continuation["confirmed_constraints"] = _persisted_constraints(continuation["confirmed_constraints"])
            self._tasks.save_pending_recommendation(identity, task_id, continuation)
            return {
                "route": dict(route),
                "recommendation_set": _with_confirmation_terms(recommendation, continuation["candidate"], binding),
                "delivery": {
                    "status": "needs_input",
                    "next_step": _confirmation_question(continuation["candidate"], binding),
                },
            }
        return dict(result)


class AppConversationToolExecutor:
    """App-owned tool adapter used only by AgentLoop and Recommender AppToolPort."""

    def __init__(
        self,
        dispatcher: ToolDispatcher,
        registry: PageRegistry,
        results: ResultStore | None = None,
        contracts: ContractStore | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._registry = registry
        self._results = results
        self._contracts = contracts
        self._recommender: RecommenderAdapter | None = None

    def bind_recommender(self, recommender: RecommenderAdapter) -> None:
        self._recommender = recommender

    def call(self, identity: SessionIdentity, task_id: str, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        module, action = _tool_name(name)
        if any(key in arguments for key in ("action", "task_id", "tenant_id", "principal_id", "session_id", "host_context", "capability_token")):
            raise ValidationError("OptChat工具参数不得覆盖App路由或身份字段")
        if module == "knowledger" and action == "search":
            return AppKnowledgePort(self._registry.capability_root, str(self._registry.manifest["catalog_version"])).search({
                "catalog_version": str(self._registry.manifest["catalog_version"]),
                "queries": arguments.get("queries", [arguments.get("query", "")]),
            })
        if module == "recommender" and action == "run":
            if self._recommender is None:
                raise ValidationError("RecommenderAdapter未绑定")
            return self._recommender.run_fixed(identity, task_id, str(arguments.get("prompt", "")), arguments)
        if module == "reporter" and action == "run":
            prepared = self._prepare_report(
                identity,
                task_id,
                arguments,
                expected_binding=self._report_binding(identity, task_id),
            )
            if prepared.get("status") == "needs_input":
                return prepared
            response = self._dispatch(identity, task_id, name, module, prepared)
            return _with_report_delivery(response, prepared)
        else:
            payload = {**dict(arguments), "action": action, "task_id": task_id}
        return self._dispatch(identity, task_id, name, module, payload)

    def _report_binding(self, identity: SessionIdentity, task_id: str) -> dict[str, str] | None:
        """Derive Reporter matching fields from the task's immutable contract binding."""

        if self._contracts is None:
            return None
        binding = self._contracts.get(identity, task_id)
        if not isinstance(binding, Mapping):
            return None
        fingerprint = binding.get("contract_fingerprint")
        catalog_version = binding.get("catalog_version")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or not isinstance(catalog_version, str) or not catalog_version:
            return None
        return {
            "analysis_case_id": f"case-{fingerprint[:24]}",
            "candidate_id": f"candidate-{fingerprint[:24]}",
            "catalog_version": catalog_version,
            "contract_fingerprint": fingerprint,
        }

    def run_recommendation_delivery(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        analysis_case_id: str,
        catalog_version: str,
        candidate: Mapping[str, Any],
        delivery: Mapping[str, Any],
        confirmed_constraints: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one approved delivery while preserving the existing public shape."""

        result = self.run_recommendation_deliveries(
            identity,
            task_id,
            analysis_case_id=analysis_case_id,
            catalog_version=catalog_version,
            candidate=candidate,
            deliveries=(delivery,),
            confirmed_constraints=confirmed_constraints,
        )
        if result.get("status") == "completed":
            completed = result.get("deliveries")
            if isinstance(completed, list) and len(completed) == 1 and isinstance(completed[0], Mapping):
                return {
                    "status": "completed",
                    "analysis_status": str(result.get("analysis_status", "completed")),
                    "delivery_status": str(result.get("delivery_status", "completed")),
                    **dict(completed[0]),
                }
        return result

    def run_recommendation_deliveries(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        analysis_case_id: str,
        catalog_version: str,
        candidate: Mapping[str, Any],
        deliveries: Sequence[Mapping[str, Any]],
        confirmed_constraints: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run the exact analysis coverage required by the requested delivery."""

        candidate_id = _identifier(candidate.get("candidate_id"), "candidate_id")
        product_id = _identifier(candidate.get("product_id"), "product_id")
        underlyings = candidate.get("underlyings")
        if isinstance(underlyings, str) or not isinstance(underlyings, Sequence):
            raise ValidationError("推荐候选缺少有序标的")
        ordered_underlyings = [str(item).strip() for item in underlyings if str(item).strip()]
        if not ordered_underlyings:
            raise ValidationError("推荐候选缺少有序标的")
        binding = {
            "analysis_case_id": _identifier(analysis_case_id, "analysis_case_id"),
            "candidate_id": candidate_id,
            "catalog_version": _identifier(catalog_version, "catalog_version"),
        }
        expected = _current_candidate_binding(candidate, confirmed_constraints or {}, self._registry.capability_root)
        if expected is None:
            return {
                "status": "partial",
                "next_step": "当前候选条款无法完成受控校验。请重新确认候选和条款后再试。",
            }
        binding["contract_fingerprint"] = expected["contract_fingerprint"]
        requested_kinds = {
            str(delivery.get("kind", "")).strip().lower()
            for delivery in deliveries
            if isinstance(delivery, Mapping)
        }
        if not requested_kinds or not requested_kinds.issubset({"card", "report"}):
            raise ValidationError("推荐交付类型无效")
        term_overrides = _term_overrides_from_constraints(
            confirmed_constraints or {}, self._registry.capability_root, product_id,
        )
        compute_base = {
            "action": "run", "task_id": task_id, "product_id": product_id,
            "identity": {"underlyings": ordered_underlyings}, "term_overrides": term_overrides,
        }
        try:
            analysis = self._dispatch(identity, task_id, "payoffer.run", "payoffer", compute_base, binding=binding)
        except (AuthorizationError, UnavailableCapabilityError, ValidationError):
            analysis = {"ok": False, "status": "failed"}
        analysis_status = _analysis_status(analysis)
        if analysis.get("ok") is not True or str(analysis.get("status", "")).lower() not in {"succeeded", "completed", "partial"}:
            return {
                "status": "partial",
                "analysis_status": analysis_status,
                "delivery_status": "not_requested",
                "next_step": "当前未能完成正式收益分析。请稍后重试，或调整标的和市场观点后重新筛选。",
            }
        if not _analysis_binding_matches(analysis, binding, tenant_id=identity.tenant_id, task_id=task_id):
            return {
                "status": "partial",
                "analysis_status": "failed",
                "delivery_status": "not_requested",
                "next_step": "当前分析结果未能与已确认候选条款一致绑定，已停止生成交付材料。请重新确认后再试。",
            }
        analysis_runs: dict[str, Mapping[str, Any]] = {"payoff": analysis}
        missing_analysis_modules: list[str] = []
        if "report" in requested_kinds:
            data_ref = self._fetch_report_market_data(identity, task_id, ordered_underlyings)
            if data_ref is None:
                missing_analysis_modules.extend(("pricing", "backtest"))
            else:
                detail_payloads = {
                    "pricing": {
                        **compute_base,
                        "pricing_config": {"model_method": "auto"},
                        "market_data_refs": [data_ref],
                    },
                    "backtest": {
                        **compute_base,
                        "backtest_config": {},
                        "historical_data": data_ref,
                    },
                }
                for display_module, compute_module in _REPORT_TO_COMPUTE_MODULE.items():
                    if display_module == "payoff":
                        continue
                    try:
                        response = self._dispatch(
                            identity,
                            task_id,
                            f"{compute_module}.run",
                            compute_module,
                            detail_payloads[display_module],
                            binding=binding,
                        )
                    except (AuthorizationError, UnavailableCapabilityError, ValidationError):
                        response = {"ok": False, "status": "failed"}
                    analysis_runs[display_module] = response
                    if (
                        response.get("ok") is not True
                        or _analysis_status(response) != "completed"
                        or not _analysis_binding_matches(
                            response, binding, tenant_id=identity.tenant_id, task_id=task_id,
                            module=compute_module,
                        )
                    ):
                        missing_analysis_modules.append(display_module)
        aggregate_analysis_status = "partial" if missing_analysis_modules else analysis_status
        # A full report is only meaningful when its requested analysis coverage
        # is complete. Do not pass an incomplete selection to Reporter: that
        # would make an apparently formal report out of partial evidence.
        if "report" in requested_kinds and missing_analysis_modules:
            return {
                "status": "partial",
                "analysis_status": aggregate_analysis_status,
                "delivery_status": "not_requested",
                "missing_modules": list(missing_analysis_modules),
                "next_step": _incomplete_report_next_step(missing_analysis_modules),
                "deliveries": [],
            }
        completed: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str | None]] = set()
        reporter_partial = False
        for delivery in deliveries:
            kind = str(delivery.get("kind", "")).strip().lower()
            output_format = str(delivery.get("format", "")).strip().lower()
            if kind not in {"card", "report"} or output_format not in {"html", "pdf"}:
                raise ValidationError("推荐交付类型无效")
            # HTML完整报告的连续版式由App内部固定，Card与PDF没有可选版式。
            layout = "continuous" if kind == "report" and output_format == "html" else None
            signature = (kind, output_format, layout)
            if signature in seen:
                continue
            seen.add(signature)
            delivery_key = f"{task_id}:{candidate_id}:{kind}:{output_format}:{layout or ''}"
            report_run_id = f"chat-{kind}-{hashlib.sha256(delivery_key.encode()).hexdigest()[:16]}"
            report_request: dict[str, Any] = {
                "kind": kind,
                "format": output_format,
                "title": str(candidate.get("product_name") or "期权结构研究"),
            }
            prepared = self._prepare_report(
                identity,
                task_id,
                report_request,
                report_run_id=report_run_id,
                expected_binding=binding,
            )
            if prepared.get("status") == "needs_input":
                return {
                    "status": "partial",
                    "analysis_status": aggregate_analysis_status,
                    "delivery_status": "partial",
                    "next_step": _incomplete_report_next_step(missing_analysis_modules),
                    "completed": completed,
                }
            try:
                report = _with_report_delivery(
                    self._dispatch(identity, task_id, "reporter.run", "reporter", prepared),
                    prepared,
                )
            except (AuthorizationError, UnavailableCapabilityError, ValidationError):
                return {
                    "status": "partial",
                    "analysis_status": aggregate_analysis_status,
                    "delivery_status": "not_requested",
                    "next_step": _undelivered_card_next_step(kind),
                    "completed": completed,
                }
            report_status = str(report.get("status", "")).lower()
            if report.get("ok") is not True or report_status not in {"succeeded", "completed", "partial"}:
                return {
                    "status": "partial",
                    "analysis_status": aggregate_analysis_status,
                    "delivery_status": "partial",
                    "next_step": _incomplete_report_next_step(missing_analysis_modules),
                    "completed": completed,
                }
            reporter_partial = reporter_partial or report_status == "partial"
            receipt = report.get("delivery")
            completed_receipt = dict(receipt) if isinstance(receipt, Mapping) else {"kind": kind, "format": output_format}
            if kind == "report" and missing_analysis_modules:
                completed_receipt.update({
                    "coverage_status": "partial",
                    "missing_modules": list(missing_analysis_modules),
                })
            completed.append(completed_receipt)
        coverage_partial = any(
            isinstance(receipt, Mapping) and receipt.get("coverage_status") == "partial"
            for receipt in completed
        )
        delivery_status = "partial" if coverage_partial or reporter_partial or missing_analysis_modules else "completed"
        return {
            "status": "completed" if aggregate_analysis_status == "completed" and delivery_status == "completed" else "partial",
            "analysis_status": aggregate_analysis_status,
            "delivery_status": delivery_status,
            "deliveries": completed,
        }

    def _fetch_report_market_data(
        self, identity: SessionIdentity, task_id: str, underlyings: Sequence[str],
    ) -> dict[str, str] | None:
        """Fetch one task-owned daily asset before detailed pricing and backtest."""

        today = date.today()
        request = {
            "action": "fetch", "task_id": task_id, "asset_ids": list(underlyings),
            "start_date": (today - timedelta(days=730)).isoformat(), "end_date": today.isoformat(),
            "fields": ["close", "adj_close"], "provider": "ifind_http", "frequency": "1d",
            "adjustment": "forward", "cache_policy": "force_refresh", "offline": False,
        }
        try:
            response = self._dispatch(identity, task_id, "datafetcher.fetch", "datafetcher", request)
        except (AuthorizationError, UnavailableCapabilityError, ValidationError):
            return None
        raw_ref = response.get("data_asset_ref") if isinstance(response, Mapping) else None
        if response.get("ok") is not True or not isinstance(raw_ref, Mapping):
            return None
        asset_id = raw_ref.get("data_asset_id")
        content_hash = raw_ref.get("content_hash")
        if not isinstance(asset_id, str) or not asset_id or not isinstance(content_hash, str) or len(content_hash) != 64:
            return None
        return {"data_asset_id": asset_id, "content_hash": content_hash}

    def run_recommendation_card(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        analysis_case_id: str,
        catalog_version: str,
        candidate: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Compatibility wrapper for the explicit Card-only internal entrypoint."""

        return self.run_recommendation_delivery(
            identity,
            task_id,
            analysis_case_id=analysis_case_id,
            catalog_version=catalog_version,
            candidate=candidate,
            delivery={"kind": "card", "format": "html"},
            confirmed_constraints=None,
        )

    def _dispatch(
        self,
        identity: SessionIdentity,
        task_id: str,
        name: str,
        module: str,
        payload: Mapping[str, Any],
        *,
        binding: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        # OptChat is server-side orchestration. It uses the same signed module
        # scope as Desk, but never grants the model direct module authority.
        binding = dict(binding or {})
        context = self._registry.service_context(
            identity,
            module,
            task_id=task_id,
            analysis_case_id=binding.get("analysis_case_id"),
            candidate_id=binding.get("candidate_id"),
            catalog_version=binding.get("catalog_version") or str(self._registry.manifest["catalog_version"]),
            contract_fingerprint=binding.get("contract_fingerprint"),
        )
        request_material = json.dumps(
            {"task_id": task_id, "tool": name, "payload": payload, "binding": binding},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        )
        return dict(self._dispatcher.dispatch_for_conversation(
            module,
            dict(payload),
            identity,
            module_context=context,
            request_id=f"conversation-{hashlib.sha256(request_material.encode()).hexdigest()[:24]}",
        ))

    def _prepare_report(
        self,
        identity: SessionIdentity,
        task_id: str,
        arguments: Mapping[str, Any],
        *,
        report_run_id: str | None = None,
        expected_binding: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Turn a semantic chat request into one server-owned Reporter selection."""

        if self._results is None:
            return _report_needs_input()
        default_kind = "report" if identity.role.value == "admin" else "card"
        requested_kind = str(arguments.get("kind") or arguments.get("output_type") or default_kind).strip().lower()
        if requested_kind not in {"card", "report"}:
            raise ValidationError("交付类型只能是card或report")
        requested_format = str(arguments.get("format") or "html").strip().lower()
        if requested_format not in {"html", "pdf"}:
            raise ValidationError("交付格式只能是html或pdf")
        allowed = {"kind", "output_type", "format", "title"}
        if set(arguments).difference(allowed):
            raise ValidationError("OptChat报告请求只接受交付类型、格式和标题")
        layout = "continuous" if requested_kind == "report" and requested_format == "html" else None
        catalog = self._results.list_owned_report_sources(identity, task_id=task_id)
        sources = catalog.get("sources") if isinstance(catalog, Mapping) else None
        if not isinstance(sources, list) or not sources:
            return _report_needs_input()
        try:
            source, candidate = _best_report_candidate(sources, expected_binding=expected_binding)
        except ValidationError:
            return _report_needs_input()
        available = candidate.get("module_run_refs")
        available_modules = [
            name for name in _REPORT_MODULES
            if isinstance(available, Mapping) and isinstance(available.get(name), Mapping)
        ]
        if not available_modules:
            return _report_needs_input()
        modules = list(_REPORT_MODULES) if requested_kind == "report" else available_modules
        module_run_refs = _current_verified_report_refs(
            available,
            modules,
            identity=identity,
            task_id=task_id,
        )
        if module_run_refs is None:
            return _report_needs_input()
        title = str(arguments.get("title") or candidate.get("product_name") or "期权结构研究").strip()
        selection = {
            "source_id": source["source_id"],
            "candidate_ids": [candidate["candidate_id"]],
            "selected_modules": modules,
            "module_run_refs": {candidate["candidate_id"]: module_run_refs},
            "delivery_mode": "single",
            "output_type": requested_kind,
            "format": requested_format,
            "html_report_layout": layout,
            "audience": identity.audience,
            "report_run_id": report_run_id or f"chat-report-{uuid4().hex[:16]}",
            "metadata": {"title": title},
        }
        return {"action": "run", "task_id": task_id, "kind": requested_kind, "selection": selection}


def _with_report_delivery(response: Mapping[str, Any], prepared: Mapping[str, Any]) -> dict[str, Any]:
    """Attach a public delivery receipt without exposing Reporter internals."""

    result = dict(response)
    selection = prepared.get("selection") if isinstance(prepared.get("selection"), Mapping) else {}
    kind = str(selection.get("output_type") or prepared.get("kind") or "").strip().lower()
    output_format = str(selection.get("format") or "").strip().lower()
    selected = selection.get("selected_modules")
    selected_modules = [
        name for name in _REPORT_MODULES
        if isinstance(selected, Sequence) and not isinstance(selected, (str, bytes)) and name in selected
    ]
    selection_refs = selection.get("module_run_refs")
    candidate_refs = {}
    if isinstance(selection_refs, Mapping):
        candidate_ids = selection.get("candidate_ids")
        if isinstance(candidate_ids, Sequence) and not isinstance(candidate_ids, (str, bytes)) and len(candidate_ids) == 1:
            raw = selection_refs.get(str(candidate_ids[0]))
            if isinstance(raw, Mapping):
                candidate_refs = raw
    missing_modules = [
        name for name in _REPORT_MODULES
        if name not in selected_modules or not isinstance(candidate_refs.get(name), Mapping)
    ] if kind == "report" and selection else []
    receipt: dict[str, Any] = {
        "kind": kind,
        "format": output_format,
    }
    if selection:
        receipt.update({
            "coverage_status": "partial" if missing_modules else "complete",
            "included_modules": selected_modules,
            "missing_modules": missing_modules,
        })
    for field in ("preview_url", "download_url"):
        if isinstance(result.get(field), str) and result[field]:
            receipt[field] = result[field]
    output = result.get("output")
    if isinstance(output, Mapping) and isinstance(output.get("report"), str):
        receipt["artifact_name"] = output["report"]
    result["delivery"] = receipt
    return result


def _analysis_status(response: Mapping[str, Any]) -> str:
    """保留正式计算的终态，不以交付层partial覆盖它。"""

    status = str(response.get("status", "")).strip().lower()
    aliases = {"succeeded": "completed", "complete": "completed", "timeout": "timed_out", "unavailable": "unsupported"}
    normalized = aliases.get(status, status)
    return normalized if normalized in {"completed", "partial", "failed", "unsupported", "cancelled", "timed_out"} else "failed"


def _best_report_candidate(
    sources: list[object], *, expected_binding: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if expected_binding is None:
        raise ValidationError("生成报告前必须确认候选和合同条款")
    for raw_source in reversed(sources):
        if not isinstance(raw_source, Mapping):
            continue
        source = dict(raw_source)
        candidates: list[dict[str, Any]] = []
        for raw_candidate in source.get("candidates", []):
            if isinstance(raw_candidate, Mapping):
                candidates.append(dict(raw_candidate))
        matched = next(
            (
                candidate for candidate in candidates
                if _report_candidate_matches_binding(source, candidate, expected_binding)
            ),
            None,
        )
        if matched is not None:
            return source, matched
    raise ValidationError("当前任务没有可用的报告候选")


def _report_candidate_matches_binding(
    source: Mapping[str, Any], candidate: Mapping[str, Any], expected: Mapping[str, str],
) -> bool:
    """Reporter只能消费本次候选、版本与合同共同绑定的正式结果。"""

    return (
        str(source.get("analysis_case_id", "")) == expected.get("analysis_case_id")
        and str(source.get("catalog_version", "")) == expected.get("catalog_version")
        and str(candidate.get("candidate_id", "")) == expected.get("candidate_id")
        and str(candidate.get("contract_fingerprint", "")) == expected.get("contract_fingerprint")
    )


def _current_verified_report_refs(
    available: Mapping[str, Any],
    modules: Sequence[str],
    *,
    identity: SessionIdentity,
    task_id: str,
) -> dict[str, dict[str, str] | None] | None:
    """仅把当前任务Store已验证的完整Core ModuleRunRef交给Reporter。"""

    selected: dict[str, dict[str, str] | None] = {}
    for display_module in modules:
        raw = available.get(display_module)
        if not isinstance(raw, Mapping):
            selected[display_module] = None
            continue
        try:
            reference = ModuleRunRef(**{field: raw.get(field) for field in _MODULE_RUN_REF_FIELDS})
        except (TypeError, ValueError):
            return None
        if (
            reference.module != _REPORT_TO_COMPUTE_MODULE[display_module]
            or reference.tenant_id != identity.tenant_id
            or reference.task_id != task_id
            or str(raw.get("status", "succeeded")).lower() != "succeeded"
        ):
            return None
        selected[display_module] = {field: str(raw[field]) for field in _MODULE_RUN_REF_FIELDS}
    return selected


def _report_needs_input() -> dict[str, Any]:
    return {
        "ok": True,
        "status": "needs_input",
        "message": "当前任务还没有可用于生成报告的正式分析结果。请先确定产品结构，并选择需要的收益图、估值或回测内容。",
    }


def _tool_name(name: str) -> tuple[str, str]:
    module, separator, action = str(name).strip().lower().partition(".")
    if not separator or not module or not action:
        raise ValidationError("Agent工具名称必须使用module.action")
    if module not in {"knowledger", "recommender", "datafetcher", "payoffer", "pricer", "backtester", "reporter"}:
        raise AuthorizationError("conversation.tool.run", "工具不在受限Agent目录中")
    permitted = {
        ("knowledger", "search"), ("recommender", "run"), ("datafetcher", "status"),
        ("datafetcher", "fetch"), ("datafetcher", "fetch_calendar"),
        ("payoffer", "run"), ("pricer", "run"), ("backtester", "run"), ("reporter", "run"),
    }
    if (module, action) not in permitted:
        raise AuthorizationError("conversation.tool.run", "工具动作不在受限Agent目录中")
    return module, action


def _matches(query: str, product_id: str, product: Mapping[str, Any]) -> bool:
    if not query:
        return False
    identity = product.get("identity", {})
    name = str(identity.get("name_zh", "")).lower() if isinstance(identity, Mapping) else ""
    if product_id.lower() in query or name in query:
        return True
    directional_hints = (
        (("上涨", "看涨", "上行", "走高"), "看涨"),
        (("下跌", "看跌", "下行", "走低"), "看跌"),
    )
    if any(any(term in query for term in terms) and target in name for terms, target in directional_hints):
        return True
    return any(token and token in name for token in re.findall(r"[A-Za-z0-9.]+|[\u4e00-\u9fff]{2,}", query))


def _optionlist_excerpt(path: Path, product_id: str, fallback: str) -> str:
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if f"| {product_id} |" in line or f"|{product_id}|" in line.replace(" ", ""):
                return line[:180]
    return f"{product_id} {fallback}"


def _optionlib_excerpt(path: Path, product_id: str, fallback: str) -> str | None:
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        match = re.search(rf"^###\s+{re.escape(product_id)}\s+.*$", text, re.MULTILINE)
        if match:
            end = text.find("\n### ", match.end())
            return text[match.start(): len(text) if end < 0 else end][:1800]
    return None


def _evidence(
    product_id: str,
    source: str,
    excerpt: str,
    identity: Mapping[str, Any],
    entry_status: bool,
    catalog_version: str,
    library_status: str = "ready",
) -> dict[str, Any]:
    normalized = str(excerpt).strip() or product_id
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return {
        "evidence_id": hashlib.sha256(f"{source}:{product_id}:{digest}".encode("utf-8")).hexdigest()[:24],
        "product_id": product_id,
        "catalog_version": catalog_version,
        "source": source,
        "section": product_id,
        "library_status": library_status,
        "excerpt_hash": digest,
        "excerpt": normalized,
        "identity": dict(identity),
        "entry_status": entry_status,
    }


def _load_capability_registry(capability_root: Path) -> Mapping[str, Any]:
    """Load OptionReg from the same verified Capability as OptionList/Lib.

    ``runtime.knowledger.load_registry`` intentionally resolves a bootstrap
    root.  Using it here would mix development OptionReg with a temporary or
    embedded Capability during App tests.  This bounded loader only accepts
    the Capability's declared read-only reference file.
    """

    path = capability_root / "scripts" / "knowledger" / "optionreg.py"
    try:
        registry = run_path(str(path)).get("REGISTRY")
    except (OSError, SyntaxError) as error:
        raise ValidationError("已验证Capability中缺少可读取的OptionReg") from error
    if not isinstance(registry, Mapping) or set(registry) != {"term_catalog", "products"}:
        raise ValidationError("Capability OptionReg结构无效")
    if not isinstance(registry.get("products"), Mapping):
        raise ValidationError("Capability OptionReg.products无效")
    return registry


def _text_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValidationError("requested_outputs必须为字符串数组")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _requested_outputs(arguments: Mapping[str, Any], constraints: Mapping[str, Any]) -> tuple[str, ...]:
    """只保留用户显式要求的交付类型，避免把Card误当成管理员默认Report。"""

    output_type = str(constraints.get("output_type", "")).strip().lower()
    if output_type == "both":
        return ("card", "report")
    if output_type in {"card", "report"}:
        return (output_type,)
    return _text_tuple(arguments.get("requested_outputs", ()))


def _same_pending_user_turn(value: object, prompt: object) -> bool:
    return (
        isinstance(value, Mapping)
        and str(value.get("role", "")).lower() == "user"
        and str(value.get("content", "")).strip() == str(prompt or "").strip()
    )


def _pending_recommendation_state(
    recommendation: object,
    *,
    analysis_case_id: str,
    catalog_version: str,
    confirmed_constraints: Mapping[str, Any],
    selected_candidate_id: str | None = None,
) -> dict[str, Any] | None:
    """Project one approved-later selection; financial facts stay out of TaskService."""

    if not isinstance(recommendation, Mapping) or str(recommendation.get("status", "")).lower() != "pending_approval":
        return None
    candidates = recommendation.get("candidates")
    if not isinstance(candidates, list):
        return None
    if len(candidates) != 1 and not selected_candidate_id:
        # 多候选必须由客户明确选择，不能把“确认”默认为第一名。
        return None
    primary_id = selected_candidate_id or str(recommendation.get("primary_candidate_id", "")).strip()
    raw_candidate = next(
        (item for item in candidates if isinstance(item, Mapping) and str(item.get("candidate_id", "")).strip() == primary_id),
        None,
    )
    if not isinstance(raw_candidate, Mapping) or str(raw_candidate.get("library_status", "")) != "ready":
        return None
    missing_inputs = raw_candidate.get("missing_inputs", ())
    if (
        isinstance(missing_inputs, Sequence)
        and not isinstance(missing_inputs, str)
        and any(str(item).strip() for item in missing_inputs)
    ):
        return None
    underlyings = raw_candidate.get("underlyings")
    if isinstance(underlyings, str) or not isinstance(underlyings, Sequence):
        return None
    ordered_underlyings = [str(item).strip() for item in underlyings if str(item).strip()]
    candidate_id = str(raw_candidate.get("candidate_id", "")).strip()
    product_id = str(raw_candidate.get("product_id", "")).strip()
    if not candidate_id or not product_id or not ordered_underlyings:
        return None
    delivery = _delivery_from_constraints(confirmed_constraints)
    return {
        "status": "pending_approval",
        "analysis_case_id": analysis_case_id,
        "catalog_version": catalog_version,
        "workflow_mode": str(recommendation.get("workflow_mode", "single_agent")),
        "confirmed_constraints": dict(confirmed_constraints),
        "delivery": delivery,
        "candidate": {
            "candidate_id": candidate_id,
            "product_id": product_id,
            "product_name": str(raw_candidate.get("product_name", "")).strip(),
            "underlyings": ordered_underlyings,
            "library_status": "ready",
        },
    }


def _may_resume_pending(
    pending: Mapping[str, Any] | None,
    current_constraints: Mapping[str, Any],
    catalog_version: str,
    *,
    explicit_candidate_confirmation: bool = False,
    stored_constraints: Mapping[str, Any] | None = None,
) -> bool:
    if not isinstance(pending, Mapping) or str(pending.get("catalog_version", "")) != catalog_version:
        return False
    stored = stored_constraints if isinstance(stored_constraints, Mapping) else pending.get("confirmed_constraints")
    if not isinstance(stored, Mapping):
        return False
    # 只有会改变候选或合同解释的条件发生变化时才重建候选；交付种类
    # 与格式是已确认结构之后的独立选择。
    if _candidate_constraints_fingerprint(current_constraints) != _candidate_constraints_fingerprint(stored):
        return False
    status = str(pending.get("status", "")).strip().lower()
    if status == "pending_approval":
        return explicit_candidate_confirmation
    if status == "approved":
        return _delivery_from_constraints(current_constraints) is not None
    return False


def _persisted_constraints(constraints: Mapping[str, Any]) -> dict[str, Any]:
    """Project user constraints onto the TaskService continuation schema.

    Product-owned term overrides are deliberately not duplicated into the task
    state. They are recovered from the task's authenticated message history by
    :func:`_pending_constraints_from_history` before any binding or execution.
    """

    return {key: value for key, value in constraints.items() if key != "term_overrides"}


def _pending_constraints_from_history(
    pending: Mapping[str, Any] | None,
    history: Sequence[object],
) -> dict[str, Any]:
    """Recover the frozen pre-reply constraints for a persisted candidate."""

    if not isinstance(pending, Mapping):
        return {}
    stored = pending.get("confirmed_constraints")
    if not isinstance(stored, Mapping):
        return {}
    # A pending candidate is created after the final user turn. On a later
    # confirmation or adjustment, the final turn is new and must not silently
    # become part of the old contract. Excluding it makes a changed override
    # invalidate the candidate rather than execute it under altered terms.
    earlier_turns: Sequence[object] = history[:-1] if history else ()
    return merge_confirmed_constraints(dict(stored), earlier_turns)


def _candidate_constraints_fingerprint(constraints: Mapping[str, Any]) -> str:
    """Fingerprint only the confirmed fields that select or interpret a contract."""

    candidate_fields = {
        key: value
        for key, value in constraints.items()
        if key in {"underlying", "horizon", "market_view", "max_loss", "principal_fluctuation", "term_overrides"}
    }
    return constraints_fingerprint(candidate_fields)


def _delivery_from_constraints(constraints: Mapping[str, Any]) -> dict[str, Any] | None:
    """Translate an explicit user delivery choice without inventing one."""

    kind = str(constraints.get("output_type", "")).strip().lower()
    if kind not in {"card", "report", "both"}:
        return None
    output_format = str(constraints.get("format", "html")).strip().lower()
    if output_format not in {"html", "pdf"}:
        return None
    return {
        # TaskService stores one approval marker.  For a dual request the
        # original output_type remains "both" in confirmed_constraints and
        # this report-shaped marker authorizes the combined delivery once.
        "kind": "report" if kind == "both" else kind,
        "format": output_format,
        # 这是TaskService私有状态，为既有严格状态校验保留；客户端请求和
        # 面向用户的交付回执均不接受或暴露该字段。
        "html_report_layout": "continuous" if kind in {"report", "both"} and output_format == "html" else None,
    }


def _deliveries_from_constraints(constraints: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return the exact user-requested delivery set in stable render order."""

    marker = _delivery_from_constraints(constraints)
    if marker is None:
        return ()
    if str(constraints.get("output_type", "")).strip().lower() != "both":
        return (marker,)
    output_format = str(constraints.get("format", "html")).strip().lower()
    return (
        {"kind": "card", "format": output_format},
        {
            "kind": "report",
            "format": output_format,
        },
    )


def _selected_candidate_id(prompt: object, recommendation: object) -> str | None:
    """仅接受明确编号、名称或排序选择，不把泛确认解释为主候选选择。"""

    if not isinstance(recommendation, Mapping):
        return None
    rows = recommendation.get("candidates")
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        return None
    candidates = [dict(item) for item in rows if isinstance(item, Mapping)]
    if len(candidates) <= 1:
        return None
    text = str(prompt or "").strip().lower()
    rank_match = re.search(r"(?:第|选择|选)([123])(?:个|号|名|项|候选)?", text)
    if rank_match:
        rank = int(rank_match.group(1))
        selected = next((item for item in candidates if item.get("rank") == rank), None)
        if selected is not None:
            return str(selected.get("candidate_id", "")).strip() or None
    for candidate in candidates:
        product_id = str(candidate.get("product_id", "")).strip().lower()
        product_name = str(candidate.get("product_name", "")).strip().lower()
        if (product_id and product_id in text) or (product_name and product_name in text):
            return str(candidate.get("candidate_id", "")).strip() or None
    return None


def _current_candidate_binding(
    candidate: Mapping[str, Any],
    constraints: Mapping[str, Any],
    capability_root: Path,
) -> dict[str, Any] | None:
    """用Core合同引擎重建当前候选的唯一条款快照。

    不在任务状态保存ResolvedContract；该对象仅在本次受控执行前由Core解析，
    指纹随客户条件变化而失效。
    """

    try:
        from runtime.contracts.contract_engine import resolve_contract

        product_id = _identifier(candidate.get("product_id"), "product_id")
        underlyings = candidate.get("underlyings")
        if isinstance(underlyings, (str, bytes)) or not isinstance(underlyings, Sequence):
            return None
        ordered = tuple(str(item).strip() for item in underlyings if str(item).strip())
        if not ordered:
            return None
        registry = _load_capability_registry(capability_root)
        contract = resolve_contract(
            product_id,
            identity={"underlyings": ordered},
            term_overrides=_term_overrides_from_constraints(constraints, capability_root, product_id),
            registry=registry,
        ).to_protocol_dict()
        fingerprint = str(contract.get("contract_fingerprint", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            return None
        return {
            "candidate_id": _identifier(candidate.get("candidate_id"), "candidate_id"),
            "constraints_fingerprint": constraints_fingerprint(constraints),
            "contract_fingerprint": fingerprint,
            "display_terms": _display_terms(contract, registry, constraints),
        }
    except Exception:
        return None


def _term_overrides_from_constraints(
    constraints: Mapping[str, Any], capability_root: Path, product_id: str,
) -> dict[str, Any]:
    """Map only typed customer terms that the selected OptionReg product owns."""

    registry = _load_capability_registry(capability_root)
    product = registry.get("products", {}).get(product_id)
    product_terms = product.get("terms") if isinstance(product, Mapping) else None
    if not isinstance(product_terms, Mapping):
        return {}
    result: dict[str, Any] = {}
    supplied = constraints.get("term_overrides")
    if isinstance(supplied, Mapping):
        for raw_key, value in supplied.items():
            key = str(raw_key)
            if key not in product_terms or isinstance(value, (Mapping, list, tuple, bool)):
                continue
            if not isinstance(value, (str, int, float)):
                continue
            result[key] = value
    horizon = str(constraints.get("horizon", "")).strip()
    if "T" not in product_terms or not horizon:
        return result
    months = re.fullmatch(r"(\d+)个月", horizon)
    years = re.fullmatch(r"(\d+)年", horizon)
    if months:
        result["T"] = int(months.group(1)) / 12
    if years:
        result["T"] = int(years.group(1))
    return result


def _display_terms(
    contract: Mapping[str, Any], registry: Mapping[str, Any], constraints: Mapping[str, Any],
) -> list[dict[str, str]]:
    """将Core合同的受控字段投影为确认前可见的专业条款摘要。"""

    identity = contract.get("identity") if isinstance(contract.get("identity"), Mapping) else {}
    terms = contract.get("terms") if isinstance(contract.get("terms"), Mapping) else {}
    sources = contract.get("term_sources") if isinstance(contract.get("term_sources"), Mapping) else {}
    catalog = registry.get("term_catalog") if isinstance(registry.get("term_catalog"), Mapping) else {}
    result = [{
        "label": "标的",
        "value": "、".join(str(item) for item in identity.get("underlyings", ())),
        "source": "用户输入",
    }]
    if constraints.get("horizon"):
        result.append({"label": "研究期限", "value": str(constraints["horizon"]), "source": "用户输入"})
    labels = {
        "T": "合同期限",
        "K": "执行水平",
        "K1": "低执行水平",
        "K2": "高执行水平",
        "K3": "第三执行水平",
        "K4": "第四执行水平",
        "Pi_0": "权利金",
        "P_net": "净权利金",
        "c": "票息",
        "c_max": "最高票息",
        "alpha": "参与率",
        "H_KO": "敲出障碍",
        "H_KI": "敲入障碍",
        "B": "气囊障碍",
        "observation_price": "观察价格",
        "O_KO": "敲出观察日程",
        "O_KI": "敲入观察日程",
        "Oc": "票息观察日程",
        "exercise_style": "行权方式",
        "settlement": "结算方式",
    }
    preferred = (
        "T", "K", "K1", "K2", "K3", "K4", "Pi_0", "P_net", "c", "c_max", "alpha",
        "H_KO", "H_KI", "B", "observation_price", "O_KO", "O_KI", "Oc", "exercise_style", "settlement",
    )
    ignored = {"S0", "S0Vec", "N", "Nvar", "Nvega", "G", "pricing_methods", "constraints"}
    ordered_keys = (*preferred, *(key for key in terms if key not in preferred and key not in ignored))
    for key in ordered_keys:
        value = terms.get(key)
        if key not in terms or isinstance(value, (Mapping, list, tuple)):
            continue
        definition = catalog.get(key)
        label = str(definition.get("name_zh", labels.get(key, key))) if isinstance(definition, Mapping) else labels.get(key, key)
        source = "用户输入" if sources.get(key) == "override" else "拟采用参数"
        result.append({"label": label, "value": str(value), "source": source})
    return result


def _with_confirmation_terms(
    recommendation: object, candidate: Mapping[str, Any], binding: Mapping[str, Any],
) -> dict[str, Any]:
    result = dict(recommendation) if isinstance(recommendation, Mapping) else {"status": "pending_approval"}
    rows = result.get("candidates")
    if isinstance(rows, list):
        prepared = []
        for item in rows:
            current = dict(item) if isinstance(item, Mapping) else item
            if isinstance(current, dict) and current.get("candidate_id") == candidate.get("candidate_id"):
                current["candidate_status"] = "pending_confirmation"
                current["key_terms"] = list(binding["display_terms"])
            prepared.append(current)
        result["candidates"] = prepared
    result["analysis_status"] = "not_started"
    result["delivery_status"] = "pending" if result.get("requested_outputs") else "not_requested"
    return result


def _confirmation_question(candidate: Mapping[str, Any], binding: Mapping[str, Any]) -> str:
    terms = binding.get("display_terms") if isinstance(binding.get("display_terms"), Sequence) else ()
    summary = "；".join(f"{item.get('label')}：{item.get('value')}" for item in terms if isinstance(item, Mapping))
    title = str(candidate.get("product_name") or candidate.get("product_id") or "该候选")
    return f"拟采用{title}。{summary}。请明确回复“确认按此候选和条款继续”，或一次说明需要调整的条件。"


def _binding_matches_pending(
    binding: Mapping[str, Any], pending: Mapping[str, Any], *, constraints: Mapping[str, Any] | None = None,
) -> bool:
    candidate = pending.get("candidate")
    frozen_constraints = constraints if isinstance(constraints, Mapping) else pending.get("confirmed_constraints")
    return (
        isinstance(candidate, Mapping)
        and isinstance(frozen_constraints, Mapping)
        and binding.get("candidate_id") == candidate.get("candidate_id")
        and binding.get("constraints_fingerprint") == constraints_fingerprint(frozen_constraints)
        and binding.get("contract_fingerprint") == _confirmation_contract_fingerprint(pending.get("analysis_case_id"))
    )


def _confirmation_analysis_case_id(contract_fingerprint: object) -> str:
    fingerprint = str(contract_fingerprint or "")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValidationError("候选合同指纹无效")
    return f"rc.{fingerprint}"


def _confirmation_contract_fingerprint(analysis_case_id: object) -> str | None:
    matched = re.fullmatch(r"rc\.([0-9a-f]{64})", str(analysis_case_id or ""))
    return matched.group(1) if matched else None


def _analysis_binding_matches(
    analysis: Mapping[str, Any], expected: Mapping[str, Any], *, tenant_id: str, task_id: str, module: str = "payoffer",
) -> bool:
    if not all(
        analysis.get(field) == expected.get(field)
        for field in ("analysis_case_id", "candidate_id", "catalog_version", "contract_fingerprint")
    ):
        return False
    raw_ref = analysis.get("module_run_ref")
    if not isinstance(raw_ref, Mapping):
        return False
    try:
        reference = ModuleRunRef(**dict(raw_ref))
    except (TypeError, ValueError):
        return False
    return reference.module == module and reference.tenant_id == tenant_id and reference.task_id == task_id


def _incomplete_report_next_step(missing_modules: Sequence[str]) -> str:
    if not missing_modules:
        return "正式收益分析已完成，但暂未形成可交付文件。请稍后重试。"
    labels = {
        "pricing": "估值定价",
        "backtest": "历史回测",
        "payoff": "收益结构",
    }
    missing = "、".join(labels.get(item, item) for item in missing_modules)
    return f"收益结构已完成，但{missing}未能取得受控正式结果。本次交付仅可标记为部分完成，请检查行情数据后重试。"


def _undelivered_card_next_step(kind: str) -> str:
    """Do not turn a rejected delivery request into a fictitious artifact."""

    label = "研究简报" if kind == "card" else "研究报告"
    return f"正式收益分析已完成，但{label}未满足受控交付条件，当前未生成文件。请检查正式结果后重试。"


def _continuation_recommendation_set(
    task_id: str,
    catalog_version: str,
    pending: Mapping[str, Any] | None,
    *,
    status: str,
    candidate_status: str = "candidate",
    analysis_status: str = "not_started",
    delivery_status: str = "not_requested",
) -> dict[str, Any]:
    """Return the one public RecommendationSet schema on every App continuation.

    The task index retains only the chosen candidate and customer constraints,
    never the original evidence or financial result.  This projection restores
    the typed envelope from that state without re-running Research or exposing
    private identifiers to the customer-facing AgentLoop.
    """

    state = dict(pending or {})
    constraints = state.get("confirmed_constraints")
    candidate_data = state.get("candidate")
    candidate: RecommendationCandidate | None = None
    if isinstance(constraints, Mapping) and isinstance(candidate_data, Mapping):
        try:
            candidate = RecommendationCandidate(
                candidate_id=_identifier(candidate_data.get("candidate_id"), "candidate_id"),
                product_id=_identifier(candidate_data.get("product_id"), "product_id"),
                underlyings=tuple(str(item).strip() for item in candidate_data.get("underlyings", ()) if str(item).strip()),
                rank=1,
                reason="已按客户确认条件保留该候选。",
                suitable_for=(),
                not_suitable_for=(),
                main_risks=(),
                library_status="ready",
                product_name=str(candidate_data.get("product_name", "")).strip() or None,
                candidate_status=candidate_status,
                constraints_fingerprint=constraints_fingerprint(constraints),
            )
        except (TypeError, ValueError, ValidationError):
            candidate = None
    output_type = str(constraints.get("output_type", "")).strip().lower() if isinstance(constraints, Mapping) else ""
    requested_outputs = (
        ("card", "report") if output_type == "both" else
        (output_type,) if output_type in {"card", "report"} else ()
    )
    workflow_mode = str(state.get("workflow_mode", "single_agent"))
    if workflow_mode not in {"single_agent", "multi_agent", "degraded_single_agent"}:
        workflow_mode = "single_agent"
    result = RecommendationSet(
        schema=RECOMMENDATION_SET_SCHEMA,
        task_id=task_id,
        run_id=f"recommend-{task_id}",
        analysis_case_id=str(state.get("analysis_case_id") or f"chat-{task_id}"),
        catalog_version=catalog_version,
        route="recommendation",
        workflow_mode=workflow_mode,
        status=status,
        primary_candidate_id=candidate.candidate_id if candidate is not None else None,
        candidates=(candidate,) if candidate is not None else (),
        analysis_status=analysis_status,
        delivery_status=delivery_status,
        requested_outputs=requested_outputs,
    )
    return result.to_dict()


def _confirmation_unavailable(
    task_id: str,
    catalog_version: str,
    pending: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "route": {"fixed_recommendation_workflow": True, "route": "recommendation"},
        "recommendation_set": _continuation_recommendation_set(
            task_id, catalog_version, pending, status="unavailable", analysis_status="not_started", delivery_status="unavailable",
        ),
        "delivery": {"status": "needs_input", "next_step": "当前候选条款暂无法受控校验。请稍后重试推荐。"},
    }


def _confirmation_invalidated(
    task_id: str,
    catalog_version: str,
    pending: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "route": {"fixed_recommendation_workflow": True, "route": "recommendation"},
        "recommendation_set": _continuation_recommendation_set(
            task_id, catalog_version, pending, status="pending_approval", analysis_status="not_started", delivery_status="pending",
        ),
        "delivery": {"status": "needs_input", "next_step": "检测到条件已变化，原候选条款已失效。请重新确认新的候选和条款。"},
    }


def _approval_text(value: object) -> bool:
    text = str(value or "").strip().lower()
    if not text or any(word in text for word in ("不确认", "不同意", "取消", "不要执行", "先不要")):
        return False
    return any(word in text for word in ("确认", "同意", "按此", "按这个", "继续执行", "继续生成", "继续分析", "可以执行", "好的", "好，"))


def _identifier(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", text):
        raise ValidationError(f"{field}无效")
    return text


def _safe_model_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """Bound long evidence while preserving the typed Recommender contract."""
    text = str(value)
    if len(text) > 80_000:
        raise ValidationError("Recommender步骤上下文超过安全上限")
    return dict(value)


def _contains_hidden_model_field(value: object) -> bool:
    forbidden = ("reasoning", "chain_of_thought", "analysis", "secret", "token", "password", "api_key")
    if isinstance(value, Mapping):
        return any(any(token in str(key).lower() for token in forbidden) or _contains_hidden_model_field(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_hidden_model_field(item) for item in value)
    return False


__all__ = ("AppAgentPort", "AppConversationToolExecutor", "AppKnowledgePort", "AppToolPort", "RecommenderAdapter")
