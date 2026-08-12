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
from runpy import run_path
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

_CORE_SRC = Path(__file__).resolve().parents[4] / "core" / "src"
if str(_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(_CORE_SRC))

from modules.recommender.models import ModelCapability, RecommendationCase
from modules.recommender.ports import AgentPort, KnowledgePort, ToolPort
from modules.recommender.service import RecommenderService
from modules.recommender.interaction import merge_confirmed_constraints

from ..errors import AuthorizationError, UnavailableCapabilityError, ValidationError
from ..identity.session_identity import SessionIdentity
from ..model_gateway.gateway import ModelGateway
from ..page_registry import PageRegistry
from ..stores.result_store import ResultStore
from ..task_runtime.task_service import TaskService
from .tool_dispatcher import ToolDispatcher


_REPORT_MODULES = ("payoff", "pricing", "backtest")


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
        if _may_resume_pending(
            pending,
            prompt,
            confirmed_constraints,
            catalog_version,
            prior_candidate_approval=_prior_candidate_approval(history),
        ):
            requested_delivery = _delivery_from_constraints(confirmed_constraints)
            if pending is not None and requested_delivery is not None and requested_delivery != pending.get("delivery"):
                pending = self._tasks.choose_pending_recommendation_delivery(identity, task_id, requested_delivery)
            if pending is not None and pending.get("delivery") is None:
                return {
                    "route": {"fixed_recommendation_workflow": True, "route": "recommendation"},
                    "recommendation_set": {"status": "pending_approval"},
                    "delivery": {
                        "status": "needs_input",
                        "next_step": "候选已确认。如需形成交付材料，请选择研究简报或完整研究报告；若只需要当前分析结论，也可以直接继续讨论。",
                    },
                }
            approved = self._tasks.approve_pending_recommendation(identity, task_id)
            if approved is not None:
                delivery_requests = _deliveries_from_constraints(approved["confirmed_constraints"])
                if len(delivery_requests) > 1:
                    delivery = self._tools.run_recommendation_deliveries(
                        identity,
                        task_id,
                        analysis_case_id=str(approved["analysis_case_id"]),
                        catalog_version=catalog_version,
                        candidate=approved["candidate"],
                        deliveries=delivery_requests,
                    )
                else:
                    delivery = self._tools.run_recommendation_delivery(
                        identity,
                        task_id,
                        analysis_case_id=str(approved["analysis_case_id"]),
                        catalog_version=catalog_version,
                        candidate=approved["candidate"],
                        delivery=approved["delivery"],
                    )
                if delivery.get("status") == "completed":
                    self._tasks.complete_pending_recommendation(identity, task_id)
                return {
                    "route": {"fixed_recommendation_workflow": True, "route": "recommendation"},
                    "recommendation_set": {"status": "completed" if delivery.get("status") == "completed" else "partial"},
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
        continuation = _pending_recommendation_state(
            recommendation,
            analysis_case_id=case.analysis_case_id,
            catalog_version=catalog_version,
            confirmed_constraints=confirmed_constraints,
        )
        if continuation is not None:
            self._tasks.save_pending_recommendation(identity, task_id, continuation)
            if pending is None and _may_auto_continue_initial_scope(prompt, recommendation, confirmed_constraints):
                approved = self._tasks.approve_pending_recommendation(identity, task_id)
                if approved is not None:
                    delivery_requests = _deliveries_from_constraints(approved["confirmed_constraints"])
                    if len(delivery_requests) > 1:
                        delivery = self._tools.run_recommendation_deliveries(
                            identity,
                            task_id,
                            analysis_case_id=str(approved["analysis_case_id"]),
                            catalog_version=catalog_version,
                            candidate=approved["candidate"],
                            deliveries=delivery_requests,
                        )
                    else:
                        delivery = self._tools.run_recommendation_delivery(
                            identity,
                            task_id,
                            analysis_case_id=str(approved["analysis_case_id"]),
                            catalog_version=catalog_version,
                            candidate=approved["candidate"],
                            delivery=approved["delivery"],
                        )
                    if delivery.get("status") == "completed":
                        self._tasks.complete_pending_recommendation(identity, task_id)
                    return {
                        "route": dict(route),
                        "recommendation_set": {
                            **dict(recommendation),
                            "status": "completed" if delivery.get("status") == "completed" else "partial",
                        },
                        "delivery": delivery,
                    }
        return dict(result)


class AppConversationToolExecutor:
    """App-owned tool adapter used only by AgentLoop and Recommender AppToolPort."""

    def __init__(
        self,
        dispatcher: ToolDispatcher,
        registry: PageRegistry,
        results: ResultStore | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._registry = registry
        self._results = results
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
            prepared = self._prepare_report(identity, task_id, arguments)
            if prepared.get("status") == "needs_input":
                return prepared
            response = self._dispatch(identity, task_id, name, module, prepared)
            return _with_report_delivery(response, prepared)
        else:
            payload = {**dict(arguments), "action": action, "task_id": task_id}
        return self._dispatch(identity, task_id, name, module, payload)

    def run_recommendation_delivery(
        self,
        identity: SessionIdentity,
        task_id: str,
        *,
        analysis_case_id: str,
        catalog_version: str,
        candidate: Mapping[str, Any],
        delivery: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Run one approved delivery while preserving the existing public shape."""

        result = self.run_recommendation_deliveries(
            identity,
            task_id,
            analysis_case_id=analysis_case_id,
            catalog_version=catalog_version,
            candidate=candidate,
            deliveries=(delivery,),
        )
        if result.get("status") == "completed":
            completed = result.get("deliveries")
            if isinstance(completed, list) and len(completed) == 1 and isinstance(completed[0], Mapping):
                return {"status": "completed", **dict(completed[0])}
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
    ) -> dict[str, Any]:
        """Run analysis once, then render every requested delivery from that result."""

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
        analysis = self._dispatch(
            identity,
            task_id,
            "payoffer.run",
            "payoffer",
            {"action": "run", "task_id": task_id, "product_id": product_id, "identity": {"underlyings": ordered_underlyings}, "term_overrides": {}},
            binding=binding,
        )
        if analysis.get("ok") is not True or str(analysis.get("status", "")).lower() not in {"succeeded", "completed", "partial"}:
            return {
                "status": "partial",
                "next_step": "当前未能完成正式收益分析。请稍后重试，或调整标的和市场观点后重新筛选。",
            }
        completed: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str | None]] = set()
        for delivery in deliveries:
            kind = str(delivery.get("kind", "")).strip().lower()
            output_format = str(delivery.get("format", "")).strip().lower()
            layout_value = delivery.get("html_report_layout")
            layout = str(layout_value).strip().lower() if layout_value is not None else None
            if kind not in {"card", "report"} or output_format not in {"html", "pdf"}:
                raise ValidationError("推荐交付类型无效")
            if kind != "report" or output_format != "html":
                if layout is not None:
                    raise ValidationError("Card或PDF不接受HTML Report版式")
            elif layout not in {None, "continuous"}:
                raise ValidationError("HTML Report固定为连续版")
            else:
                layout = "continuous"
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
            if layout is not None:
                report_request["html_report_layout"] = layout
            prepared = self._prepare_report(
                identity,
                task_id,
                report_request,
                report_run_id=report_run_id,
                expected_candidate_id=candidate_id,
            )
            if prepared.get("status") == "needs_input":
                return {
                    "status": "partial",
                    "next_step": "正式收益分析已完成，但暂未形成可交付文件。请稍后重试。",
                    "completed": completed,
                }
            report = _with_report_delivery(
                self._dispatch(identity, task_id, "reporter.run", "reporter", prepared),
                prepared,
            )
            if report.get("ok") is not True or str(report.get("status", "")).lower() not in {"succeeded", "completed"}:
                return {
                    "status": "partial",
                    "next_step": "交付文件暂未全部生成。请稍后重试。",
                    "completed": completed,
                }
            receipt = report.get("delivery")
            completed.append(
                dict(receipt) if isinstance(receipt, Mapping)
                else {"kind": kind, "format": output_format}
            )
        return {"status": "completed", "deliveries": completed}

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
            delivery={"kind": "card", "format": "html", "html_report_layout": None},
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
        expected_candidate_id: str | None = None,
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
        allowed = {"kind", "output_type", "format", "html_report_layout", "title"}
        if set(arguments).difference(allowed):
            raise ValidationError("OptChat报告请求只接受交付类型、格式和标题")
        layout_value = arguments.get("html_report_layout")
        if requested_kind != "report" or requested_format != "html":
            if layout_value is not None:
                raise ValidationError("Card或PDF不接受HTML Report版式")
            layout = None
        else:
            layout = str(layout_value or "continuous").strip().lower()
            if layout not in {None, "continuous"}:
                raise ValidationError("HTML Report固定为连续版")
            layout = "continuous"
        catalog = self._results.list_owned_report_sources(identity, task_id=task_id)
        sources = catalog.get("sources") if isinstance(catalog, Mapping) else None
        if not isinstance(sources, list) or not sources:
            return _report_needs_input()
        source, candidate = _best_report_candidate(sources, expected_candidate_id=expected_candidate_id)
        available = candidate.get("module_run_refs")
        modules = [
            name for name in _REPORT_MODULES
            if isinstance(available, Mapping) and isinstance(available.get(name), Mapping)
        ]
        if not modules:
            return _report_needs_input()
        title = str(arguments.get("title") or candidate.get("product_name") or "期权结构研究").strip()
        selection = {
            "source_id": source["source_id"],
            "candidate_ids": [candidate["candidate_id"]],
            "selected_modules": modules,
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
    missing_modules = [name for name in _REPORT_MODULES if name not in selected_modules] if kind == "report" and selection else []
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


def _best_report_candidate(
    sources: list[object], *, expected_candidate_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    for raw_source in reversed(sources):
        if not isinstance(raw_source, Mapping):
            continue
        source = dict(raw_source)
        candidates: list[dict[str, Any]] = []
        for raw_candidate in source.get("candidates", []):
            if isinstance(raw_candidate, Mapping):
                candidates.append(dict(raw_candidate))
        if candidates:
            if expected_candidate_id is not None:
                matched = next(
                    (candidate for candidate in candidates if str(candidate.get("candidate_id", "")) == expected_candidate_id),
                    None,
                )
                if matched is not None:
                    return source, matched
                continue
            def coverage(candidate: Mapping[str, Any]) -> tuple[int, str]:
                refs = candidate.get("module_run_refs")
                count = sum(
                    isinstance(refs, Mapping) and isinstance(refs.get(name), Mapping)
                    for name in ("payoff", "pricing", "backtest")
                )
                return count, str(candidate.get("candidate_id", ""))

            return source, max(candidates, key=coverage)
    raise ValidationError("当前任务没有可用的报告候选")


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
        and str(value.get("status", "")).lower() == "pending_model"
        and str(value.get("content", "")).strip() == str(prompt or "").strip()
    )


def _pending_recommendation_state(
    recommendation: object,
    *,
    analysis_case_id: str,
    catalog_version: str,
    confirmed_constraints: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Project one approved-later selection; financial facts stay out of TaskService."""

    if not isinstance(recommendation, Mapping) or str(recommendation.get("status", "")).lower() != "pending_approval":
        return None
    candidates = recommendation.get("candidates")
    if not isinstance(candidates, list):
        return None
    primary_id = str(recommendation.get("primary_candidate_id", "")).strip()
    raw_candidate = next(
        (item for item in candidates if isinstance(item, Mapping) and str(item.get("candidate_id", "")).strip() == primary_id),
        next((item for item in candidates if isinstance(item, Mapping)), None),
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
    prompt: str,
    current_constraints: Mapping[str, Any],
    catalog_version: str,
    *,
    prior_candidate_approval: bool = False,
) -> bool:
    if not isinstance(pending, Mapping) or str(pending.get("catalog_version", "")) != catalog_version:
        return False
    # Choosing a document type is not candidate authority.  It can complete a
    # prior explicit candidate confirmation, but can never replace one.
    if not _approval_text(prompt) and not prior_candidate_approval:
        return False
    stored = pending.get("confirmed_constraints")
    if not isinstance(stored, Mapping):
        return False
    # A confirmation may restate the old request, but it must not silently
    # execute a candidate after the user changes market/risk assumptions.
    for field in ("underlying", "horizon", "market_view", "max_loss", "principal_fluctuation"):
        if field in current_constraints and current_constraints.get(field) != stored.get(field):
            return False
    return True


def _may_auto_continue_initial_scope(
    prompt: object,
    recommendation: object,
    constraints: Mapping[str, Any],
) -> bool:
    """Honor a one-request recommendation-and-report scope exactly once."""

    text = str(prompt or "").strip().lower()
    if not any(word in text for word in ("推荐", "适合的期权", "哪种期权", "什么期权")):
        return False
    if _delivery_from_constraints(constraints) is None:
        return False
    if not isinstance(recommendation, Mapping) or str(recommendation.get("status", "")).lower() != "pending_approval":
        return False
    candidates = recommendation.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        return False
    candidate = candidates[0]
    if not isinstance(candidate, Mapping) or str(candidate.get("library_status", "")) != "ready":
        return False
    missing = candidate.get("missing_inputs", ())
    if isinstance(missing, Sequence) and not isinstance(missing, str) and any(str(item).strip() for item in missing):
        return False
    return str(recommendation.get("primary_candidate_id", "")).strip() == str(candidate.get("candidate_id", "")).strip()


def _prior_candidate_approval(history: Sequence[object]) -> bool:
    """Recognize an explicit approval already acknowledged by the App."""

    if len(history) < 2:
        return False
    latest = history[-1]
    previous = history[-2]
    if not isinstance(latest, Mapping) or not isinstance(previous, Mapping):
        return False
    return (
        str(latest.get("role", "")).lower() == "user"
        and str(previous.get("role", "")).lower() == "assistant"
        and str(previous.get("status", "")).lower() == "needs_input"
        and "候选已确认" in str(previous.get("content", ""))
    )


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
        {"kind": "card", "format": output_format, "html_report_layout": None},
        {
            "kind": "report",
            "format": output_format,
            "html_report_layout": "continuous" if output_format == "html" else None,
        },
    )


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
