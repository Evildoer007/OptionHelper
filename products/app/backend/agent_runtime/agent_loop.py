"""Bounded, single-owner OptChat decision loop.

Financial computation never happens here.  The loop asks a model for one
strict action at a time and only exposes opaque, traceable tool observations.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Any, Protocol

from runtime.workflow_policy import decide_workflow, recommendation_execution_mode

from ..errors import AuthorizationError, UnavailableCapabilityError, ValidationError
from ..identity.session_identity import SessionIdentity
from ..settings.settings_models import ModelSelection
from ..report_delivery import delivery_request
from .redaction import has_hidden_reasoning, redact_text
from .durability_checkpoint import DurabilityCheckpointStore, stable_operation_id
from .session_context import SessionId


_ACTIONS = frozenset({"final", "ask_user", "call_tool"})
_FORBIDDEN_DECISION_KEYS = frozenset({"reasoning", "chain_of_thought", "analysis", "secret", "api_key", "token"})
_SECRET_TEXT = ("sk-", "access_token", "refresh_token", "api_key", "password", "bearer ")
_INTERNAL_TEXT = re.compile(
    r"(?i)(reporter\.run|runref|source_id|module_run_ref|report_run_ref|data_asset_ref|"
    r"contract_ref|candidate_id|task_id|artifact_manifest|manifest\.json|文件路径|"
    r"resolved_?contract|caller_?context|host_?context|module_?host_?context|tool_?gateway|"
    r"catalog_version|analysis_case_id|principal_id|request_id|tenant_id|"
    r"capability_manifest|\brecommender\b|\bhost\b)"
)
_REPORT_MODULE_LABELS = {
    "payoff": "收益结构",
    "pricing": "估值定价",
    "backtest": "历史回测",
}


class ContextPort(Protocol):
    def build(self, identity: SessionIdentity, task_id: str, latest_message: str) -> dict[str, Any]: ...


class DecisionPort(Protocol):
    def decide_for(self, identity: SessionIdentity, task_id: str, context: Mapping[str, Any], *, selection: ModelSelection | None = None) -> Mapping[str, Any]: ...


class ConversationToolPort(Protocol):
    def call(self, identity: SessionIdentity, task_id: str, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...


class RecommenderPort(Protocol):
    def run_fixed(
        self, identity: SessionIdentity, task_id: str, prompt: str, arguments: Mapping[str, Any], *,
        selection: ModelSelection | None = None, execution_ids: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]: ...


class ObservationPort(Protocol):
    def facts_for(
        self, identity: SessionIdentity, task_id: str, tool: str, value: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class ConversationAgentPort(Protocol):
    def run_with_execution(
        self,
        identity: SessionIdentity,
        task_id: str,
        message: str,
        *,
        selection: ModelSelection | None = None,
        execution_ids: Mapping[str, str],
        context: Mapping[str, Any] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AgentDecision:
    action: str
    text: str | None = None
    tool: str | None = None
    arguments: Mapping[str, Any] | None = None
    fact_refs: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AgentDecision":
        if not isinstance(value, Mapping):
            raise ValidationError("Agent决策必须为JSON对象")
        forbidden = _FORBIDDEN_DECISION_KEYS.intersection(value)
        if forbidden:
            raise ValidationError("Agent决策不得包含推理或Secret字段")
        action = str(value.get("action", "")).strip()
        if action not in _ACTIONS:
            raise ValidationError("Agent action必须是final、ask_user或call_tool")
        allowed = {
            "final": {"action", "text", "fact_refs"},
            "ask_user": {"action", "question"},
            "call_tool": {"action", "tool", "arguments"},
        }[action]
        unexpected = set(value).difference(allowed)
        if unexpected:
            raise ValidationError(f"Agent决策含未声明字段：{', '.join(sorted(map(str, unexpected)))}")
        if action == "call_tool":
            tool = _text(value.get("tool"), "tool")
            arguments = value.get("arguments", {})
            if not isinstance(arguments, Mapping) or _contains_sensitive_key(arguments):
                raise ValidationError("Agent工具参数必须为不含Secret的对象")
            return cls(action=action, tool=tool, arguments=dict(arguments))
        field = "text" if action == "final" else "question"
        fact_refs = _fact_refs(value.get("fact_refs")) if action == "final" else ()
        return cls(action=action, text=_text(value.get(field), field), fact_refs=fact_refs)


class AgentLoop:
    def __init__(
        self,
        *,
        gateway: DecisionPort,
        context_builder: ContextPort,
        tool_executor: ConversationToolPort,
        recommender: RecommenderPort,
        max_rounds: int = 4,
        timeout_seconds: float = 45.0,
        is_cancelled: Callable[[SessionIdentity, str], bool] | None = None,
        observation_builder: ObservationPort | None = None,
        conversation_agent: ConversationAgentPort | None = None,
        dispatch_checkpoints: DurabilityCheckpointStore | None = None,
        conversation_session_id: Callable[[SessionIdentity, str], SessionId] | None = None,
        visible_event_sink: Callable[[SessionIdentity, str, str | None, str, str, str], None] | None = None,
    ) -> None:
        if not 1 <= max_rounds <= 8 or timeout_seconds <= 0:
            raise ValueError("AgentLoop配置无效")
        self._gateway = gateway
        self._context_builder = context_builder
        self._tool_executor = tool_executor
        self._recommender = recommender
        self._max_rounds = max_rounds
        self._timeout_seconds = timeout_seconds
        self._is_cancelled = is_cancelled or (lambda _identity, _task_id: False)
        self._observation_builder = observation_builder
        self._conversation_agent = conversation_agent
        self._dispatch_checkpoints = dispatch_checkpoints
        self._conversation_session_id = conversation_session_id
        self._visible_event_sink = visible_event_sink

    def run_with_execution(
        self,
        identity: SessionIdentity,
        task_id: str,
        message: str,
        *,
        selection: ModelSelection | None = None,
        execution_ids: Mapping[str, str], attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self.run(
            identity, task_id, message, selection=selection,
            execution_ids=execution_ids, attachments=attachments,
        )

    def run(
        self, identity: SessionIdentity, task_id: str, message: str, *,
        selection: ModelSelection | None = None,
        execution_ids: Mapping[str, str] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        started = monotonic()
        observations: list[dict[str, Any]] = []
        seen_calls: set[str] = set()
        request_id = str((execution_ids or {}).get("request_id") or "").strip() or None
        self._emit_visible_event(identity, task_id, request_id, "routing", "started", "正在判断本次请求的处理路径。")
        for round_number in range(1, self._max_rounds + 1):
            if self._is_cancelled(identity, task_id):
                return _result("cancelled", "本次任务已取消。", observations, round_number - 1)
            if monotonic() - started > self._timeout_seconds:
                return _result("timed_out", "本次任务处理超时，未生成计算结论。", observations, round_number - 1)
            try:
                context = self._context_builder.build(identity, task_id, message)
                workflow_decision = _workflow_decision(self._gateway, identity, message, context)
                if round_number == 1 and workflow_decision.analysis_path == "recommendation" and self._conversation_agent is None:
                    self._emit_visible_event(identity, task_id, request_id, "routing", "completed", "已识别为结构推荐需求，开始候选分析。")
                    arguments = {
                        "workflow": _fixed_recommendation_workflow(message),
                        "main_agent_controlled": True,
                        "require_multi_agent": workflow_decision.execution_semantics == "multi_agent",
                        "recommendation_preset": workflow_decision.preset_id,
                    }
                    self._emit_visible_event(
                        identity, task_id, request_id, "agent_run", "started",
                        ("正在使用多Agent预设分析候选。" if arguments["require_multi_agent"] else "正在使用单Agent分析候选。"),
                    )
                    if execution_ids is None:
                        raw = (
                            self._recommender.run_fixed(identity, task_id, message, arguments)
                            if selection is None
                            else self._recommender.run_fixed(identity, task_id, message, arguments, selection=selection)
                        )
                    else:
                        raw = (
                            self._recommender.run_fixed(
                                identity, task_id, message, arguments, execution_ids=execution_ids,
                            )
                            if selection is None
                            else self._recommender.run_fixed(
                                identity, task_id, message, arguments,
                                selection=selection, execution_ids=execution_ids,
                            )
                        )
                    observations.append(_observation("recommender.run", raw, identity))
                    recommendation = raw.get("recommendation_set", {})
                    recommendation_status = str(recommendation.get("status", "")) if isinstance(recommendation, Mapping) else ""
                    self._emit_visible_event(
                        identity, task_id, request_id, "agent_run",
                        "failed" if recommendation_status in {"failed", "unavailable"} else "completed",
                        "等待补充推荐条件。" if recommendation_status == "pending_question" else
                        "候选分析未完成。" if recommendation_status in {"failed", "unavailable"} else "候选分析已完成。",
                    )
                    if not _recommender_returns_to_main_agent(raw):
                        return _recommendation_outcome(raw, observations, round_number)
                    requested_document = delivery_request(message)
                    if requested_document and _explicit_delivery_kinds(message) != ("card", "report"):
                        # Recommendation and delivery are one requested task. The
                        # Host already owns the confirmed candidate and validated
                        # calculations; do not ask a second model to rediscover or
                        # rerun them after a document has been committed.
                        if self._is_cancelled(identity, task_id):
                            return _result("cancelled", "本次任务已取消。", observations, round_number)
                        delivery = dict(self._tool_executor.call(
                            identity, task_id, "recommendation_delivery.run",
                            {"kind": requested_document["template"], "format": requested_document["format"]},
                        ))
                        observations.append(_observation("recommendation_delivery.run", delivery, identity))
                        return {
                            **delivery,
                            "state": _state(str(delivery.get("status", "unavailable"))),
                            "text": delivery.get("text") or delivery.get("next_step") or delivery.get("message") or "所需计算尚未完成。",
                            "observations": observations,
                            "rounds": round_number,
                        }
                    # Recommender persists the authoritative ordered candidate
                    # state.  The parent Agent must observe that committed state,
                    # never the context captured before recommendation ran.
                    context = self._context_builder.build(identity, task_id, message)
                if round_number == 1 and _explicit_delivery_kinds(message) == ("card", "report"):
                    tool_names = {str(item.get("name")) for item in context.get("tool_catalog", []) if isinstance(item, Mapping)}
                    if "reporter.run" in tool_names:
                        return self._run_dual_delivery(identity, task_id, message, observations, round_number)
                if round_number == 1 and self._conversation_agent is not None:
                    try:
                        return self._conversation_agent.run_with_execution(
                            identity,
                            task_id,
                            message,
                            selection=selection,
                            execution_ids=dict(execution_ids or {}),
                            context=context,
                            attachments=attachments,
                        )
                    except UnavailableCapabilityError as error:
                        # One-release compatibility path: when the dedicated
                        # runtime is explicitly unavailable, keep the existing
                        # strict decision loop instead of changing workflow
                        # semantics or losing the request.
                        if error.capability == "natural_conversation_agent":
                            pass
                        else:
                            return _result(
                                "unavailable",
                                f"当前无法继续：{_safe_error(error)}",
                                observations,
                                round_number,
                            )
                    except (AuthorizationError, ValidationError, ValueError) as error:
                        return _result(
                            "unavailable",
                            f"当前无法继续：{_safe_error(error)}",
                            observations,
                            round_number,
                        )
                    except Exception as error:
                        timed_out = "超时" in str(error) or "timeout" in str(error).lower() or "timed out" in str(error).lower()
                        message = (
                            "模型响应超时，本轮尚未完成。已保留对话，可继续发送需求重试。"
                            if timed_out else "对话执行中断，本轮尚未完成。已保留对话，请查看运行详情后重试。"
                        )
                        return _result("unavailable", message, observations, round_number)
                decision_context = _model_context(
                    context,
                    observations=observations,
                    round_number=round_number,
                    max_rounds=self._max_rounds,
                )
                if round_number == 1:
                    self._emit_visible_event(identity, task_id, request_id, "routing", "completed", "已识别为常规对话。")
                    self._emit_visible_event(identity, task_id, request_id, "agent_run", "started", "正在分析需求。")
                checkpoint = None
                if self._dispatch_checkpoints is not None and self._conversation_session_id is not None:
                    step_namespace = str((execution_ids or {}).get("model_step_id", task_id))
                    checkpoint = self._dispatch_checkpoints.checkpoint(
                        self._conversation_session_id(identity, task_id),
                        operation_id=stable_operation_id(step_namespace, "decision", round_number),
                        operation_kind="main_agent_decision",
                        safe_metadata={"round": round_number},
                    )
                try:
                    if selection is None:
                        raw_decision = self._gateway.decide_for(identity, task_id, decision_context)
                    else:
                        raw_decision = self._gateway.decide_for(identity, task_id, decision_context, selection=selection)
                except Exception:
                    if checkpoint is not None:
                        self._dispatch_checkpoints.failed(checkpoint)
                    raise
                try:
                    decision = AgentDecision.from_mapping(raw_decision)
                except Exception:
                    if checkpoint is not None:
                        self._dispatch_checkpoints.failed(checkpoint)
                    raise
                if checkpoint is not None:
                    self._dispatch_checkpoints.succeeded(checkpoint)
            except (UnavailableCapabilityError, AuthorizationError, ValidationError, ValueError) as error:
                return _result(
                    "unavailable",
                    f"当前无法继续：{_safe_error(error)}",
                    observations,
                    round_number,
                    error_capability=error.capability if isinstance(error, UnavailableCapabilityError) else None,
                    error_next_step=error.next_step if isinstance(error, UnavailableCapabilityError) else None,
                )
            except Exception:  # model adapters must not leak internals to the user
                return _result("unavailable", "当前模型服务未返回可用决策。", observations, round_number)

            if self._is_cancelled(identity, task_id):
                return _result("cancelled", "本次任务已取消。", observations, round_number)
            if monotonic() - started > self._timeout_seconds:
                return _result("timed_out", "本次任务处理超时，未生成计算结论。", observations, round_number)

            if decision.action == "final":
                text = _user_text(decision.text, identity)
                if has_hidden_reasoning(decision.text) or not _has_valid_fact_citations(
                    text, decision.fact_refs, observations, _context_facts(context),
                ):
                    return _result(
                        "partial",
                        "当前结论缺少可核验的正式分析结果。请先运行所需分析，或补充标的、产品结构与期限。",
                        observations,
                        round_number,
                    )
                self._emit_visible_event(identity, task_id, request_id, "agent_run", "completed", "本轮分析已完成。")
                return _result("partial" if _has_failure(observations) else "completed", text, observations, round_number)
            if decision.action == "ask_user":
                text = _user_text(decision.text, identity)
                if has_hidden_reasoning(decision.text) or (_has_financial_number(text) and not _has_valid_fact_citations(text, (), observations, _context_facts(context))):
                    return _result("partial", "模型不能在未引用受控事实时给出金融数字。", observations, round_number)
                self._emit_visible_event(identity, task_id, request_id, "agent_run", "completed", "本轮分析已完成。")
                return _result("needs_input", text, observations, round_number, action="ask_user")
            assert decision.tool is not None and decision.arguments is not None
            tool_names = {str(item.get("name")) for item in context.get("tool_catalog", []) if isinstance(item, Mapping)}
            if decision.tool not in tool_names:
                return _result("blocked", "该操作不在当前身份可调用的工具范围内。", observations, round_number)
            call_key = _call_fingerprint(decision.tool, decision.arguments)
            if call_key in seen_calls:
                return _result(
                    "stopped_duplicate",
                    "当前步骤没有获得新的有效结果，已停止重复执行。请补充标的、期限或需要的分析内容。",
                    observations,
                    round_number,
                )
            seen_calls.add(call_key)
            try:
                module_summary = _host_module_summary(decision.tool)
                if module_summary is not None:
                    self._emit_visible_event(identity, task_id, request_id, "host_module", "started", module_summary[0])
                if decision.tool == "recommender.run":
                    forwarded_arguments = {
                        **dict(decision.arguments),
                        "main_agent_controlled": True,
                        "require_multi_agent": workflow_decision.execution_semantics == "multi_agent",
                        "recommendation_preset": _selected_recommendation_preset(self._gateway, identity),
                    }
                    if execution_ids is None:
                        raw = (
                            self._recommender.run_fixed(identity, task_id, message, forwarded_arguments)
                            if selection is None
                            else self._recommender.run_fixed(
                                identity, task_id, message, forwarded_arguments, selection=selection,
                            )
                        )
                    else:
                        raw = (
                            self._recommender.run_fixed(
                                identity, task_id, message, forwarded_arguments, execution_ids=execution_ids,
                            )
                            if selection is None
                            else self._recommender.run_fixed(
                                identity, task_id, message, forwarded_arguments,
                                selection=selection, execution_ids=execution_ids,
                            )
                        )
                else:
                    raw = self._tool_executor.call(identity, task_id, decision.tool, decision.arguments)
                observation = _observation(decision.tool, raw, identity)
                if module_summary is not None:
                    completed_status = "failed" if observation.get("status") in {"failed", "unsupported"} else "completed"
                    self._emit_visible_event(identity, task_id, request_id, "host_module", completed_status, module_summary[1])
                if self._observation_builder is not None:
                    try:
                        facts = self._observation_builder.facts_for(identity, task_id, decision.tool, raw)
                        if isinstance(facts, Mapping) and isinstance(facts.get("facts"), list):
                            projected_facts = _fact_projection(facts["facts"])
                            if projected_facts:
                                observation["facts"] = projected_facts
                    except Exception:
                        # The tool may already have committed a valid run.  A
                        # failed fact projection must not relabel it failed or
                        # encourage the model to run it again.
                        observation["facts_status"] = "unavailable"
                observations.append(observation)
                if decision.tool == "recommender.run":
                    if _recommender_returns_to_main_agent(raw):
                        continue
                    return _recommendation_outcome(raw, observations, round_number)
                if decision.tool == "recommendation_delivery.run":
                    return _main_agent_delivery_outcome(raw, observations, round_number)
                if decision.tool == "reporter.run":
                    if str(raw.get("status", "")).lower() == "needs_input":
                        return _result(
                            "needs_input",
                            str(raw.get("message") or "当前任务还缺少生成报告所需的正式分析结果。"),
                            observations,
                            round_number,
                            action="ask_user",
                        )
                    if raw.get("ok") is True and str(raw.get("status", "")).lower() in {"completed", "succeeded", "partial"}:
                        requested_kind = str(
                            decision.arguments.get("kind")
                            or decision.arguments.get("output_type")
                            or "report"
                        ).strip().lower()
                        delivery_status, delivery_text = _delivery_completion(raw, requested_kind)
                        return _result(
                            delivery_status,
                            delivery_text,
                            observations,
                            round_number,
                        )
                    return _result(
                        "unavailable",
                        _user_text(
                            str(raw.get("message") or "报告能力未完成本次交付，请检查Reporter和Designer配置后重试。"),
                            identity,
                        ),
                        observations,
                        round_number,
                        error_capability="report_delivery",
                        error_next_step="检查Reporter、Designer及正式分析结果后重试。",
                    )
            except UnavailableCapabilityError as error:
                if _host_module_summary(decision.tool) is not None:
                    self._emit_visible_event(identity, task_id, request_id, "host_module", "failed", "模块运行未完成，正在返回可用结果。")
                observations.append({"tool": decision.tool, "status": "failed", "error": _safe_error(error)})
                if decision.tool == "reporter.run":
                    next_step = _user_text(error.next_step, identity)
                    return _result(
                        "unavailable",
                        f"报告未生成：{next_step}",
                        observations,
                        round_number,
                        error_capability="report_delivery",
                        error_next_step=next_step,
                    )
            except (AuthorizationError, ValidationError) as error:
                if _host_module_summary(decision.tool) is not None:
                    self._emit_visible_event(identity, task_id, request_id, "host_module", "failed", "模块运行未完成，正在返回可用结果。")
                observations.append({"tool": decision.tool, "status": "failed", "error": _safe_error(error)})
                if decision.tool == "reporter.run":
                    return _result(
                        "needs_input",
                        "报告暂未生成。请先确认产品结构，并完成至少一项收益图、估值或回测分析。",
                        observations,
                        round_number,
                        action="ask_user",
                    )
            except Exception:
                if _host_module_summary(decision.tool) is not None:
                    self._emit_visible_event(identity, task_id, request_id, "host_module", "failed", "模块运行未完成，正在返回可用结果。")
                observations.append({"tool": decision.tool, "status": "failed", "error": "工具未完成"})
            if self._is_cancelled(identity, task_id):
                return _result("cancelled", "本次任务已取消。", observations, round_number)
            if monotonic() - started > self._timeout_seconds:
                return _result("timed_out", "本次任务处理超时，未生成计算结论。", observations, round_number)
        return _result("max_rounds", "已达到最大工具决策轮次，未生成最终结论。", observations, self._max_rounds)

    def _run_dual_delivery(
        self,
        identity: SessionIdentity,
        task_id: str,
        message: str,
        observations: list[dict[str, Any]],
        round_number: int,
    ) -> dict[str, Any]:
        """Render both requested documents from the task's existing formal results."""

        output_format = "pdf" if "pdf" in message.lower() else "html"
        deliveries: list[Mapping[str, Any]] = []
        has_partial_delivery = False
        for kind in ("card", "report"):
            arguments: dict[str, Any] = {"kind": kind, "format": output_format}
            try:
                raw = self._tool_executor.call(identity, task_id, "reporter.run", arguments)
                observations.append(_observation("reporter.run", raw, identity))
                deliveries.append(raw)
            except UnavailableCapabilityError as error:
                observations.append({"tool": "reporter.run", "status": "failed", "error": _safe_error(error)})
                return _result(
                    "unavailable",
                    f"交付材料未全部生成：{_user_text(error.next_step, identity)}",
                    observations,
                    round_number,
                    error_capability="report_delivery",
                    error_next_step=_user_text(error.next_step, identity),
                )
            except (AuthorizationError, ValidationError) as error:
                observations.append({"tool": "reporter.run", "status": "failed", "error": _safe_error(error)})
                return _result("partial", "交付材料暂未全部生成。请根据已说明的输入缺口处理后重试。", observations, round_number)
            except Exception:
                observations.append({"tool": "reporter.run", "status": "failed", "error": "工具未完成"})
                return _result("partial", "交付材料暂未全部生成。请稍后重试。", observations, round_number)
            status = str(raw.get("status", "")).lower()
            if status == "needs_input":
                return _result(
                    "needs_input",
                    str(raw.get("message") or "当前任务还缺少生成交付材料所需的正式分析结果。"),
                    observations,
                    round_number,
                    action="ask_user",
                )
            if raw.get("ok") is not True or status not in {"completed", "succeeded", "partial"}:
                return _result("partial", "交付材料暂未全部生成。请稍后重试。", observations, round_number)
            has_partial_delivery = has_partial_delivery or status == "partial"
        detailed = deliveries[-1] if deliveries else {}
        delivery_status, delivery_text = _delivery_completion(detailed, "report")
        if delivery_status == "partial":
            return _result(
                "partial",
                "研究简报已生成；" + delivery_text,
                observations,
                round_number,
            )
        preview_entries = [
            str(item.get("preview_url"))
            for item in deliveries
            if isinstance(item.get("preview_url"), str) and item.get("preview_url")
        ]
        preview_text = "预览入口：" + "；".join(preview_entries) if preview_entries else "可在当前任务中预览或导出。"
        if has_partial_delivery:
            return _result(
                "partial",
                f"研究简报和完整研究报告已形成可预览版本。部分交付内容已标注为不完整。{preview_text}",
                observations,
                round_number,
            )
        return _result(
            "completed",
            f"研究简报和完整研究报告已基于同一组分析结果生成。{preview_text}",
            observations,
            round_number,
        )

    def _emit_visible_event(
        self,
        identity: SessionIdentity,
        task_id: str,
        request_id: str | None,
        event_type: str,
        status: str,
        summary: str,
    ) -> None:
        if self._visible_event_sink is not None:
            self._visible_event_sink(identity, task_id, request_id, event_type, status, summary)


def _host_module_summary(tool_name: str) -> tuple[str, str] | None:
    labels = {
        "payoffer.run": "收益结构模块正在运行。",
        "pricer.run": "估值定价模块正在运行。",
        "backtester.run": "历史回测模块正在运行。",
        "reporter.run": "正在生成交付材料。",
        "recommendation_delivery.run": "正在汇总已验证结果。",
    }
    starts = labels.get(tool_name)
    if starts is None:
        return None
    completed = {
        "payoffer.run": "收益结构模块已返回结果。",
        "pricer.run": "估值定价模块已返回结果。",
        "backtester.run": "历史回测模块已返回结果。",
        "reporter.run": "交付材料已生成或已返回所需输入。",
        "recommendation_delivery.run": "已完成已验证结果汇总。",
    }
    return starts, completed[tool_name]


def _result(
    status: str,
    text: str,
    observations: list[dict[str, Any]],
    rounds: int,
    *,
    action: str | None = None,
    error_capability: str | None = None,
    error_next_step: str | None = None,
    approval: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status, "text": text, "observations": observations, "rounds": rounds,
        "state": _state(status),
    }
    if action:
        result["action"] = action
        if action == "ask_user":
            result["_user_question"] = _user_question(status, text)
    if error_capability:
        result["error"] = {
            "capability": error_capability,
            "next_step": error_next_step or "相关能力当前不可用，请检查配置后重试。",
        }
    if approval:
        result["approval"] = dict(approval)
    return result


def _user_question(status: str, text: str) -> dict[str, Any]:
    """Project one blocking Host question without changing workflow routing.

    The natural-language question remains the authoritative conversation
    content.  This metadata only lets OptChat render safe, replayable answer
    controls for states that already require user input.
    """

    normalized = str(text).strip()
    options: list[dict[str, Any]] = []
    if status == "pending_approval":
        options = [
            {
                "value": "确认采用当前候选和条款。",
                "label": "确认当前候选",
                "description": "继续使用已展示的候选结构与条款。",
                "recommended": True,
            },
            {
                "value": "暂不确认，请调整候选或条款。",
                "label": "调整候选",
                "description": "返回Recommender内部继续修改候选或约束。",
                "recommended": False,
            },
        ]
    lower = normalized.lower()
    delivery_choices = sum((
        any(marker in lower for marker in ("研究简报", "简单报告", "简报", "card")),
        any(marker in lower for marker in ("完整研究报告", "详细报告", "完整报告", "report")),
        any(marker in lower for marker in ("参考报价", "报价表", "quote")),
    ))
    if status == "needs_input" and (delivery_choices >= 2 or "形成交付材料" in normalized):
        options = [
            {
                "value": "请生成简单报告。",
                "label": "研究简报",
                "description": "用于快速查看核心结论、关键条款和主要风险。",
                "recommended": True,
            },
            {
                "value": "请生成详细报告。",
                "label": "研究报告",
                "description": "用于完整保留分析依据、计算结果和风险说明。",
                "recommended": False,
            },
            {
                "value": "请生成参考报价。",
                "label": "参考报价",
                "description": "用于集中展示结构条款和报价字段。",
                "recommended": False,
            },
        ]
    if status == "needs_input" and not options and any(word in normalized for word in ("期限", "方向", "波动", "风险", "损失", "本金", "条款")):
        options = [
            {"label": "采用建议研究参数", "value": "请结合已有资料采用合理研究假设并注明，继续执行；实际行情仍须从已配置数据源获取。", "description": "允许采用明确披露的研究假设，继续完成分析。", "recommended": True},
            {"label": "先说明参数影响", "value": "请简要说明这些缺失参数会怎样影响候选与结果，我再选择。", "description": "先了解取舍，再决定具体条件。", "recommended": False},
        ]
    return {
        "type": "question",
        "question_id": hashlib.sha256(f"{status}\x1f{normalized}".encode("utf-8")).hexdigest()[:24],
        "prompt": normalized,
        "options": options,
        "allow_free_text": True,
    }


def _model_context(
    context: Mapping[str, Any], *, observations: list[dict[str, Any]], round_number: int, max_rounds: int,
) -> dict[str, Any]:
    """Expose business facts to the model without Store reference material."""

    task = context.get("task")
    task_fact = {
        key: str(task[key])[:240]
        for key in ("subject", "status")
        if isinstance(task, Mapping) and task.get(key) is not None
    }
    model_context: dict[str, Any] = {
        "task": task_fact,
        "caller": _public_mapping(context.get("caller"), allowed=("role", "audience")),
        "messages": _public_messages(context.get("messages")),
        "latest_message": str(context.get("latest_message", ""))[:2_000],
        "facts": _public_business_facts(context.get("facts")),
        "tool_catalog": context.get("tool_catalog") if isinstance(context.get("tool_catalog"), list) else [],
        "decision_protocol": context.get("decision_protocol") if isinstance(context.get("decision_protocol"), Mapping) else {},
    }
    return {
        **model_context,
        "observations": observations,
        "round": round_number,
        "max_rounds": max_rounds,
    }


def _public_mapping(value: object, *, allowed: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {key: str(value[key])[:240] for key in allowed if value.get(key) is not None}


def _public_messages(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [
        {
            "role": str(item.get("role", "user"))[:32],
            "content": str(item.get("content", ""))[:2_000],
            "status": str(item.get("status", ""))[:80],
        }
        for item in value[-12:]
        if isinstance(item, Mapping)
    ]


def _public_business_facts(value: object) -> dict[str, Any]:
    """Project only customer-meaningful facts; all Store references stay local."""

    if not isinstance(value, Mapping):
        return {}
    facts: dict[str, Any] = {}
    contract = value.get("resolved_contract")
    if isinstance(contract, Mapping):
        identity = contract.get("identity")
        terms = contract.get("terms")
        projected_contract: dict[str, Any] = {}
        if isinstance(identity, Mapping):
            projected_contract["identity"] = _public_mapping(identity, allowed=("underlyings", "product_name"))
        if isinstance(terms, Mapping):
            projected_contract["terms"] = {
                str(key): item
                for key, item in terms.items()
                if isinstance(item, (str, int, float, bool))
            }
        if projected_contract:
            facts["contract_terms"] = projected_contract
    raw_runs = value.get("module_run_facts")
    if isinstance(raw_runs, list):
        facts["module_run_facts"] = [
            {"facts": projected}
            for row in raw_runs
            if isinstance(row, Mapping)
            for projected in [_fact_projection(row.get("facts") if isinstance(row.get("facts"), list) else [])]
            if projected
        ]
    recommendation = value.get("recommendation_candidate")
    if isinstance(recommendation, Mapping):
        candidates = recommendation.get("candidates")
        facts["recommendation"] = {
            "status": str(recommendation.get("status", ""))[:80],
            "candidate_count": int(recommendation.get("candidate_count", 0) or 0),
            "approved_candidate_ids": [
                str(item)[:160]
                for item in recommendation.get("approved_candidate_ids", [])[:10]
                if isinstance(item, str)
            ],
            "candidates": [
                {
                    key: item[key]
                    for key in (
                        "ordinal", "candidate_id", "product_id", "product_name",
                        "underlyings", "key_terms", "approval_status",
                    )
                    if key in item
                }
                for item in candidates[:10]
                if isinstance(item, Mapping)
            ] if isinstance(candidates, list) else [],
        }
    return facts


def _observation(tool: str, value: Mapping[str, Any], identity: SessionIdentity) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("工具必须返回对象")
    status = str(value.get("status", "succeeded" if value.get("ok") is not False else "failed")).lower()
    result: dict[str, Any] = {"tool": tool, "status": "failed" if value.get("ok") is False else status}
    recommendation = value.get("recommendation_set")
    if not isinstance(recommendation, Mapping) and isinstance(value.get("result"), Mapping):
        recommendation = value["result"].get("recommendation_set")
    if isinstance(recommendation, Mapping):
        candidates = recommendation.get("candidates")
        result["recommendation"] = {
            "status": str(recommendation.get("status", "")),
            "candidate_count": len(candidates) if isinstance(candidates, list) else 0,
        }
    if tool == "knowledger.search" and isinstance(value.get("evidence"), list):
        result["knowledge_evidence"] = [
            {
                "product_id": str(item.get("product_id", "")),
                "source": str(item.get("source", "")),
                "section": str(item.get("section", "")),
                "excerpt": redact_text(item.get("excerpt", ""), identity, limit=1_200),
            }
            for item in value["evidence"][:9]
            if isinstance(item, Mapping)
        ]
    if isinstance(value.get("message"), str):
        result["message"] = redact_text(value["message"], identity, limit=240)
    return result


def _recommendation_follow_up(value: Mapping[str, Any]) -> str | None:
    recommendation = value.get("recommendation_set")
    if not isinstance(recommendation, Mapping) and isinstance(value.get("result"), Mapping):
        recommendation = value["result"].get("recommendation_set")
    if not isinstance(recommendation, Mapping):
        return None
    status = str(recommendation.get("status", "")).lower()
    question = recommendation.get("next_question")
    if status == "pending_question" and isinstance(question, str) and question.strip():
        return question.strip()
    candidates = recommendation.get("candidates")
    if status == "unavailable" and (not isinstance(candidates, list) or not candidates):
        limitations = recommendation.get("limitations")
        detail = " ".join(str(item) for item in limitations if isinstance(item, str)).lower() if isinstance(limitations, list) else ""
        if any(marker in detail for marker in ("模型", "model", "structured", "gateway")):
            return "当前模型服务暂不可用。请检查模型设置后重试；已确认的标的、期限和风险条件不会丢失。"
        return "当前受控产品库暂未形成可验证候选。请调整一项市场观点、期限或风险约束后重新筛选。"
    return None


def _recommender_returns_to_main_agent(value: Mapping[str, Any]) -> bool:
    """Only the App-owned adapter may hand control back to the parent Agent."""

    control = value.get("control")
    return isinstance(control, Mapping) and control.get("resume_main_agent") is True


def _recommendation_outcome(
    raw: Mapping[str, Any],
    observations: list[dict[str, Any]],
    round_number: int,
) -> dict[str, Any]:
    """Translate one fixed RecommendationSet into a user-facing next state."""

    delivery = raw.get("delivery")
    if isinstance(delivery, Mapping):
        delivery_status = str(delivery.get("status", "")).lower()
        completed_deliveries = delivery.get("deliveries")
        if delivery_status == "completed" and isinstance(completed_deliveries, list):
            kinds = {
                str(item.get("kind", "")).lower()
                for item in completed_deliveries
                if isinstance(item, Mapping)
            }
            if {"card", "report"}.issubset(kinds):
                report_delivery = next(
                    (
                        item for item in completed_deliveries
                        if isinstance(item, Mapping) and str(item.get("kind", "")).lower() == "report"
                    ),
                    {},
                )
                coverage_status, report_text = _delivery_completion(
                    {"delivery": report_delivery}, "report",
                )
                if coverage_status == "partial":
                    return _result(
                        "partial",
                        "研究简报已生成；" + report_text,
                        observations,
                        round_number,
                    )
                preview_entries = [
                    str(item.get("preview_url"))
                    for item in completed_deliveries
                    if isinstance(item, Mapping) and isinstance(item.get("preview_url"), str) and item.get("preview_url")
                ]
                preview_text = "预览入口：" + "；".join(preview_entries) if preview_entries else "可在当前任务中预览或导出。"
                return _result(
                    "completed",
                    f"研究简报和完整研究报告已基于同一组分析结果生成。{preview_text}",
                    observations,
                    round_number,
                )
        if delivery_status == "completed" and delivery.get("kind") in {"card", "quote", "report"} and delivery.get("format") == "html":
            status, text = _delivery_completion({"delivery": delivery}, str(delivery.get("kind")))
            return _result(status, text, observations, round_number)
        if delivery_status == "needs_input":
            text = str(delivery.get("next_step") or "如需形成交付材料，可以生成研究简报、完整研究报告或参考报价；多个候选也可以生成多结构对比交付。若只需要当前分析结论，可以直接继续讨论。")
            return _result("needs_input", text, observations, round_number, action="ask_user")
        if delivery_status in {"partial", "unavailable", "failed"}:
            text = str(delivery.get("next_step") or "当前未能完成报告。请稍后重试。")
            return _result("partial", text, observations, round_number)
    recommendation = raw.get("recommendation_set")
    if not isinstance(recommendation, Mapping) and isinstance(raw.get("result"), Mapping):
        recommendation = raw["result"].get("recommendation_set")
    status = str(recommendation.get("status", "")).lower() if isinstance(recommendation, Mapping) else ""
    follow_up = _recommendation_follow_up(raw)
    if follow_up is not None:
        limitations = recommendation.get("limitations", []) if isinstance(recommendation, Mapping) else []
        detail = " ".join(str(item) for item in limitations).lower() if isinstance(limitations, list) else ""
        if status == "unavailable" and any(marker in detail for marker in ("模型", "model", "structured", "gateway")):
            return _result(
                "unavailable", follow_up, observations, round_number,
                error_capability="model", error_next_step=follow_up,
            )
        return _result("needs_input", follow_up, observations, round_number, action="ask_user")
    if status == "pending_approval" and _has_approvable_candidate(recommendation):
        return _result(
            "pending_approval",
            _candidate_confirmation_text(recommendation),
            observations,
            round_number,
            action="ask_user",
        )
    return _result("partial", "当前未形成可交付的推荐结果。请调整一项筛选条件后重新尝试。", observations, round_number)


def _delivery_completion(raw: Mapping[str, Any], requested_kind: str) -> tuple[str, str]:
    """Name the delivered artifact and disclose incomplete report coverage."""

    delivery = raw.get("delivery") if isinstance(raw.get("delivery"), Mapping) else {}
    kind = str(delivery.get("kind") or requested_kind).strip().lower()
    # Reporter returns an App-owned relative route. This is a public preview
    # entry, not a ResultStore path or a filesystem location.
    preview = delivery.get("preview_url") or raw.get("preview_url")
    has_preview = isinstance(preview, str) and preview.startswith("/api/reports/")
    preview_text = f"预览入口：{preview}" if has_preview else "可在当前任务中预览或导出。"
    is_partial = str(raw.get("status", "")).strip().lower() == "partial"
    is_comparison = str(delivery.get("delivery_mode", "")).strip().lower() == "comparison"
    if kind == "card":
        label = "多结构研究简报" if is_comparison else "研究简报"
        if is_partial:
            return "partial", f"{label}已生成。{preview_text}部分内容未能完整交付，已如实标注。"
        return "completed", f"{label}已生成。{preview_text}" if has_preview else f"{label}已生成，{preview_text}"
    if kind == "quote":
        if is_partial:
            return "partial", f"参考报价已生成。{preview_text}部分报价内容未能完整交付，已如实标注。"
        return "completed", f"参考报价已生成。{preview_text}" if has_preview else f"参考报价已生成，{preview_text}"
    missing = delivery.get("missing_modules")
    raw_missing = missing if isinstance(missing, list) else []
    missing_modules = [
        str(item) for item in raw_missing
        if str(item) in _REPORT_MODULE_LABELS
    ]
    if missing_modules:
        labels = "、".join(_REPORT_MODULE_LABELS[item] for item in missing_modules)
        report_label = "多结构完整报告" if is_comparison else "研究报告"
        delivery_text = f"{report_label}已生成。{preview_text}" if has_preview else f"{report_label}已生成，{preview_text}"
        delivery_text = delivery_text.rstrip("。") + "。"
        return (
            "partial",
            f"{delivery_text}本次{labels}未执行，相关章节已明确标注，不作为已完成结论。",
        )
    if is_partial:
        label = "多结构完整报告" if is_comparison else "研究报告"
        return "partial", f"{label}已生成。{preview_text}部分内容未能完整交付，已如实标注。"
    label = "多结构完整报告" if is_comparison else "完整研究报告"
    return (
        "completed",
        f"{label}已生成。{preview_text}" if has_preview else f"{label}已生成，{preview_text}",
    )


def _main_agent_delivery_outcome(
    raw: Mapping[str, Any], observations: list[dict[str, Any]], round_number: int,
) -> dict[str, Any]:
    """Translate the independent delivery state machine without re-entering Recommender."""

    status = str(raw.get("status", "")).strip().lower()
    if status == "completed":
        kind = str(raw.get("kind", "report")).strip().lower()
        completed_status, text = _delivery_completion({"delivery": raw}, kind)
        return _result(completed_status, text, observations, round_number)
    next_step = str(raw.get("next_step") or "当前分析部分完成，但未生成文件；请检查缺少的分析输入后重试。")
    if status == "needs_input":
        return _result("needs_input", next_step, observations, round_number, action="ask_user")
    return _result("partial", next_step, observations, round_number)


def _has_approvable_candidate(recommendation: Mapping[str, Any] | None) -> bool:
    """Never ask for approval unless the persisted workflow can resume it."""

    if not isinstance(recommendation, Mapping):
        return False
    candidates = recommendation.get("candidates")
    if not isinstance(candidates, list):
        return False
    return any(
        isinstance(candidate, Mapping)
        and str(candidate.get("candidate_id", "")).strip()
        and str(candidate.get("product_id", "")).strip()
        and str(candidate.get("library_status", "")) == "ready"
        and not _candidate_has_missing_inputs(candidate)
        for candidate in candidates
    )


def _candidate_has_missing_inputs(candidate: Mapping[str, Any]) -> bool:
    missing = candidate.get("missing_inputs", ())
    return (
        isinstance(missing, list)
        and any(str(item).strip() for item in missing)
    )


def _candidate_confirmation_text(recommendation: Mapping[str, Any]) -> str:
    """Render the choice before asking for confirmation, without internals."""

    candidates = recommendation.get("candidates")
    if not isinstance(candidates, list):
        return "当前没有可确认的候选。"
    visible: list[str] = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping) or str(candidate.get("library_status", "")) != "ready":
            continue
        label = f"候选{index + 1}"
        name = _public_candidate_field(candidate.get("product_name"), "候选结构")
        reason = _public_candidate_field(candidate.get("reason"), "与已确认的市场观点和风险约束匹配")
        risks = _public_candidate_items(candidate.get("main_risks"), "仍需关注结构自身的损失风险")
        exclusions = _public_candidate_items(candidate.get("not_suitable_for"), "不接受该结构主要风险的情形")
        visible.append(
            f"{label}：{name}\n推荐理由：{reason}\n主要风险：{risks}\n不适用：{exclusions}"
        )
    if not visible:
        return "当前没有可确认的候选。"
    instruction = (
        "存在多个实质不同的候选，请明确选择一个或多个候选；多选顺序即后续比较顺序。"
        if len(visible) > 1
        else "如只需查看推荐，可继续讨论；如需正式分析或生成报告，请确认采用该候选。"
    )
    return "\n\n".join((*visible, instruction))


def _public_candidate_items(value: object, fallback: str) -> str:
    if isinstance(value, list):
        rows = [_public_candidate_field(item, "") for item in value[:3]]
        rows = [item for item in rows if item]
        if rows:
            return "；".join(rows)
    return fallback


def _public_candidate_field(value: object, fallback: str) -> str:
    text = redact_text(value, limit=360).strip()
    if not text or _INTERNAL_TEXT.search(text):
        return fallback
    return text


def _should_run_fixed_recommendation(message: str, context: Mapping[str, Any]) -> bool:
    """Compatibility wrapper around the shared business routing policy."""

    facts = context.get("facts")
    messages = context.get("messages")
    decision = decide_workflow(
        message,
        facts=facts if isinstance(facts, Mapping) else {},
        messages=messages if isinstance(messages, list) else (),
    )
    return decision.analysis_path == "recommendation"


def _selected_recommendation_preset(gateway: object, identity: SessionIdentity) -> str:
    resolver = getattr(gateway, "multi_agent_recommendation_preset_for", None)
    value = resolver(identity) if callable(resolver) else "sequential-deliberation"
    preset_id = str(value or "").strip().lower()
    if preset_id not in {
        "sequential-deliberation", "product-trader-loop", "independent-council", "constraint-ranking",
    }:
        raise ValidationError("设置中心选择了未启用的Recommender Mode")
    return preset_id


def _workflow_decision(
    gateway: object,
    identity: SessionIdentity,
    message: str,
    context: Mapping[str, Any],
):
    facts = context.get("facts")
    messages = context.get("messages")
    return decide_workflow(
        message,
        facts=facts if isinstance(facts, Mapping) else {},
        messages=messages if isinstance(messages, list) else (),
        preset_id=_selected_recommendation_preset(gateway, identity),
        execution_semantics="multi_agent" if recommendation_execution_mode(message, getattr(gateway, "recommendation_execution_mode_for", lambda _: "single")(identity)) == "multi" else "single_model",
    )


def _fixed_recommendation_workflow(message: object) -> str:
    """Recommend first; the Host handles document delivery independently."""
    return "recommendation"


def _explicit_delivery_kinds(value: object) -> tuple[str, ...]:
    """Detect only an unambiguous request for both public delivery forms."""

    text = str(value or "").strip().lower()
    wants_card = any(word in text for word in ("研究简报", "简单报告", "简报"))
    wants_report = any(word in text for word in ("完整研究报告", "详细报告", "深度报告", "完整报告"))
    both = any(word in text for word in ("两份", "都要", "都给", "各一份", "各来一份"))
    return ("card", "report") if wants_card and wants_report and both else ()


def _user_text(value: str | None, identity: SessionIdentity) -> str:
    text = redact_text(value, identity, limit=4_000)
    if _INTERNAL_TEXT.search(text):
        return "当前步骤尚未形成可交付结果。请补充标的、产品结构和期限，我会继续完成所需分析。"
    return text


def _call_fingerprint(tool: str, arguments: Mapping[str, Any]) -> str:
    payload = json.dumps({"tool": tool, "arguments": arguments}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 4_000:
        raise ValidationError(f"Agent.{field}无效")
    if any(marker in text.lower() for marker in _SECRET_TEXT):
        raise ValidationError(f"Agent.{field}不得回显Secret")
    return text


def _contains_sensitive_key(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in (
                "secret", "token", "password", "api_key", "access_key", "reasoning", "chain_of_thought", "analysis",
            )) or _contains_sensitive_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _has_failure(observations: list[dict[str, Any]]) -> bool:
    return any(item.get("status") in {"failed", "partial", "unsupported", "cancelled", "timed_out", "unavailable"} for item in observations)


def _safe_error(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "请求缺少必要字段或不符合受控输入规则"
    if isinstance(error, AuthorizationError):
        return "当前身份无权执行该操作"
    if isinstance(error, UnavailableCapabilityError):
        return "相关能力当前不可用"
    return "工具未完成"


_FACT_REF = re.compile(r"fact_[a-f0-9]{6,64}")
_FACT_MARKER = re.compile(r"\[fact:(fact_[a-f0-9]{6,64})\]")
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])[+-]?\d+(?:\.\d+)?%?")


def _fact_refs(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 24:
        raise ValidationError("Agent.fact_refs必须是最多24个事实引用的数组")
    refs = tuple(str(item) for item in value)
    if len(set(refs)) != len(refs) or any(not _FACT_REF.fullmatch(item) for item in refs):
        raise ValidationError("Agent.fact_refs无效")
    return refs


def _fact_projection(value: list[object]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for item in value[:24]:
        if not isinstance(item, Mapping) or not _FACT_REF.fullmatch(str(item.get("fact_ref", ""))):
            continue
        number = item.get("value")
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(float(number)):
            continue
        fact = {"fact_ref": str(item["fact_ref"]), "label": str(item.get("label", "事实")), "value": number}
        if isinstance(item.get("unit"), str):
            fact["unit"] = item["unit"]
        facts.append(fact)
    return facts


def _has_valid_fact_citations(
    text: str,
    declared: tuple[str, ...],
    observations: list[dict[str, Any]],
    context_facts: list[Mapping[str, Any]],
) -> bool:
    available = {
        str(fact.get("fact_ref")): fact.get("value")
        for source in [*observations, *context_facts]
        for fact in source.get("facts", []) if isinstance(fact, Mapping)
        if _FACT_REF.fullmatch(str(fact.get("fact_ref", "")))
        and isinstance(fact.get("value"), (int, float)) and not isinstance(fact.get("value"), bool)
        and math.isfinite(float(fact["value"]))
    }
    markers = tuple(_FACT_MARKER.findall(text))
    if any(reference not in available for reference in (*declared, *markers)):
        return False
    if set(declared) != set(markers):
        return False
    citation_free = _FACT_MARKER.sub(lambda match: " " * len(match.group(0)), text)
    consumed_markers: set[tuple[int, str]] = set()
    for match in _NUMBER.finditer(citation_free):
        end = match.end()
        # Every rendered number must immediately carry one known controlled
        # fact reference.  The marker itself is removed above only to avoid
        # treating hexadecimal ids as financial numbers.
        marker = _FACT_MARKER.match(text, end)
        if marker is None:
            return False
        reference = marker.group(1)
        if not _number_matches_fact(match.group(), available[reference]):
            return False
        consumed_markers.add((marker.start(), reference))
    return len(consumed_markers) == len(markers)


def _context_facts(context: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    facts = context.get("facts")
    rows = facts.get("module_run_facts") if isinstance(facts, Mapping) else None
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _has_financial_number(text: str) -> bool:
    labels = (
        "pv", "greek", "delta", "gamma", "vega", "theta", "rho", "估值", "定价", "回测", "收益率", "胜率", "回撤", "损益",
    )
    # Asset codes, dates and list numbering are context, not calculated facts.
    value = _FACT_MARKER.sub("", text)
    value = re.sub(r"(?<![A-Za-z0-9])\d{6}\s*\.?\s*(?:SH|SZ)(?![A-Za-z0-9])", "标的", value, flags=re.I)
    value = re.sub(r"\b\d{4}[-/]\d{2}[-/]\d{2}\b", "日期", value)
    value = re.sub(r"(?m)^\s*\d+[.、)]\s*", "", value)
    return any(
        _NUMBER.search(clause) and any(label in clause.lower() for label in labels)
        for clause in re.split(r"[。！？；;\n]", value)
    )


def _number_matches_fact(rendered: str, value: object) -> bool:
    try:
        observed = float(rendered.removesuffix("%"))
        expected = float(value)
    except (TypeError, ValueError):
        return False
    if rendered.endswith("%"):
        expected *= 100.0
    return math.isclose(observed, expected, rel_tol=1e-9, abs_tol=1e-12)


def _state(status: str) -> dict[str, Any]:
    rules = {
        "cancelled": (True, False), "timed_out": (True, True), "max_rounds": (True, True),
        "stopped_duplicate": (True, False), "pending_approval": (False, False), "needs_input": (False, False),
        "completed": (True, False), "partial": (True, True), "blocked": (True, False), "unavailable": (True, True),
    }
    terminal, retryable = rules.get(status, (True, False))
    return {"code": status, "terminal": terminal, "retryable": retryable}


__all__ = ("AgentDecision", "AgentLoop")
