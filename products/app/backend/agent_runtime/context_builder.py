"""Small, redacted task context for the OptChat Agent loop.

The model receives task facts and opaque references, never Store paths,
credential material, raw Provider responses or hidden reasoning.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..authorization.policy import AuthorizationPolicy
from ..errors import AuthorizationError, ValidationError
from ..identity.session_identity import SessionIdentity
from ..stores.result_store import ResultStore
from ..task_runtime.task_service import TaskService
from .redaction import redact_text


_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|private[_-]?key)")
def conversation_tool_catalog(policy: AuthorizationPolicy, identity: SessionIdentity) -> list[dict[str, Any]]:
    """Return only tool names that the current server-side role may request.

    This is a model visibility boundary.  ToolGateway independently performs
    the same proxy-capability check before a tool can execute.
    """

    if not policy.allows(identity.role, "conversation.tool.run"):
        return []
    tools = [
        {"name": "attachment.search", "description": "检索当前Task已附加业务文档的文本片段", "actions": ["search"]},
        {"name": "attachment.read", "description": "读取当前Task已附加业务文档的指定片段", "actions": ["read"]},
        {"name": "knowledger.search", "description": "查询受治理期权资料库", "actions": ["search"]},
        {"name": "recommender.run", "description": "运行一次固定结构推荐流程", "actions": ["run"]},
        {
            "name": "recommendation_delivery.run",
            "description": "将当前推荐整理为报告；用户明确要求的计算先执行，已有结果复用，缺项不得改交草稿；arguments只填kind或format",
            "actions": ["run"],
        },
        {"name": "datafetcher.status", "description": "查询数据能力状态", "actions": ["status"]},
        {"name": "datafetcher.fetch", "description": "按受控Provider获取市场数据", "actions": ["fetch"]},
        {"name": "datafetcher.fetch_calendar", "description": "获取中国交易所未来交易日期，不读取未来价格", "actions": ["fetch_calendar"]},
        {"name": "payoffer.run", "description": "基于ResolvedContract生成本次收益图", "actions": ["run"]},
        {"name": "pricer.run", "description": "基于ResolvedContract与市场数据执行估值", "actions": ["run"]},
        {"name": "backtester.run", "description": "基于ResolvedContract与历史数据执行回测", "actions": ["run"]},
        {
            "name": "reporter.run",
            "description": "将当前任务已完成的正式分析整理为交付物；arguments可填kind、format、title或delivery_mode。delivery_mode=comparison用于多结构研究简报或多结构完整报告。",
            "actions": ["run"],
            "output_types": ["card"],
            "delivery_modes": ["single", "comparison"],
        },
    ]
    output_types = ["card"]
    if policy.allows(identity.role, "report.quote.request"):
        output_types.append("quote")
    if policy.allows(identity.role, "report.full.request"):
        output_types.append("report")
    tools[-1] = {**tools[-1], "output_types": output_types}
    tools.append({"name":"reporter.create_document", "description":"根据当前对话生成可编辑HTML、PDF或Word报告草稿，不要求先完成计算。", "actions":["create_document"]})
    return tools


class ContextBuilder:
    """Build a bounded, task-owned Agent context without a hidden memory store."""

    def __init__(
        self,
        task_service: TaskService,
        policy: AuthorizationPolicy,
        *,
        catalog_version: str,
        max_messages: int = 12,
        result_store: ResultStore | None = None,
    ) -> None:
        if max_messages < 1:
            raise ValueError("max_messages must be positive")
        self._tasks = task_service
        self._policy = policy
        self._catalog_version = catalog_version
        self._max_messages = max_messages
        self._results = result_store

    def build(self, identity: SessionIdentity, task_id: str, latest_message: str) -> dict[str, Any]:
        task = self._tasks.get(identity, task_id)
        recommendation = self._tasks.pending_recommendation(identity, task_id)
        facts = {
            "recommendation_candidate": _recommendation_candidate_fact(recommendation),
            "module_run_refs": _refs(task.get("run_refs")),
            "module_run_facts": self._module_run_facts(identity, task.get("run_refs")),
        }
        surface = self._tasks.model_surface(identity, task_id, controlled_facts=facts)
        return {
            "task": {
                "task_id": task_id,
                "subject": _safe_text(task.get("subject"), 240, identity),
                "status": _safe_text(task.get("status"), 80, identity),
            },
            # The remote model needs the user's role and declared audience for
            # response scope.  Principal, tenant and session identifiers stay
            # inside the App boundary.
            "caller": {"role": identity.role.value, "audience": identity.audience},
            "catalog_version": self._catalog_version,
            "messages": [
                _surface_message_fact(row, identity)
                for row in surface.messages[-self._max_messages:]
                if isinstance(row, Mapping)
            ],
            "latest_message": _redact_text(latest_message, identity),
            "facts": dict(surface.controlled_facts),
            "tool_catalog": conversation_tool_catalog(self._policy, identity),
            "decision_protocol": {
                "actions": ["final", "ask_user", "call_tool"],
                "rule": "不得计算或编造金融数字；正式计算只能通过受控能力完成；许可与候选确认由Host确定性策略处理，模型不得自行申请许可；面向用户的文字不得出现内部工具名、内部引用名或内部字段名。",
            },
        }

    def _module_run_facts(self, identity: SessionIdentity, references: object) -> list[dict[str, Any]]:
        if self._results is None or not isinstance(references, list):
            return []
        facts: list[dict[str, Any]] = []
        for reference in references[-12:]:
            if not isinstance(reference, dict):
                continue
            try:
                facts.append(self._results.verified_fact_summary(identity, reference))
            except (AuthorizationError, ValidationError, KeyError, OSError):
                # A stale or tampered ref is deliberately invisible to the
                # model.  The user can rerun the controlled module instead.
                continue
        return facts


class ResultStoreObservationBuilder:
    """Attach only independently verified ModuleRun facts to a tool observation."""

    def __init__(self, results: ResultStore) -> None:
        self._results = results

    def facts_for(
        self, identity: SessionIdentity, task_id: str, _tool: str, value: Mapping[str, Any],
    ) -> dict[str, Any]:
        reference = value.get("module_run_ref")
        if not isinstance(reference, dict) or reference.get("task_id") != task_id:
            return {}
        return self._results.verified_fact_summary(identity, reference)


def _surface_message_fact(value: Mapping[str, Any], identity: SessionIdentity) -> dict[str, Any]:
    role = str(value.get("role", "user"))
    if role not in {"user", "assistant", "system", "tool"}:
        role = "user"
    if role == "assistant" and isinstance(value.get("tool_call"), Mapping):
        call = value["tool_call"]
        return {
            "role": "assistant",
            "tool_call": {
                "call_id": _safe_text(call.get("call_id"), 160, identity),
                "name": _safe_text(call.get("name"), 160, identity),
                "arguments": _safe_mapping(call.get("arguments")),
            },
        }
    result: dict[str, Any] = {
        "role": role,
        "content": _safe_text(value.get("content", ""), 2_000, identity),
    }
    attachments = value.get("attachments")
    if role == "user" and isinstance(attachments, list):
        result["attachments"] = [
            {
                key: item[key]
                for key in ("attachment_id", "kind", "media_type", "name", "extraction_status")
                if key in item
            }
            for item in attachments[:20] if isinstance(item, Mapping)
        ]
    if role == "tool":
        result.update({
            "call_id": _safe_text(value.get("call_id"), 160, identity),
            "name": _safe_text(value.get("name"), 160, identity),
            "is_error": bool(value.get("is_error", False)),
        })
    if value.get("kind") == "context_summary":
        result["kind"] = "context_summary"
    return result


def _recommendation_candidate_fact(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Expose only the control state needed by the parent Agent."""

    if not isinstance(value, Mapping):
        return None
    status = str(value.get("status", "")).strip().lower()
    if status not in {"pending_approval", "approval_prepared", "approved"}:
        return None
    candidate_ids = value.get("candidate_ids")
    contracts = value.get("candidate_contracts")
    approved = value.get("approved_candidate_ids")
    if (
        not isinstance(candidate_ids, list)
        or not candidate_ids
        or not isinstance(contracts, Mapping)
        or not isinstance(approved, list)
    ):
        return None
    candidates: list[dict[str, Any]] = []
    for index, candidate_id in enumerate(candidate_ids[:10], start=1):
        if not isinstance(candidate_id, str):
            return None
        contract = contracts.get(candidate_id)
        candidate = contract.get("candidate") if isinstance(contract, Mapping) else None
        public_projection = contract.get("public_projection") if isinstance(contract, Mapping) else None
        if not isinstance(candidate, Mapping):
            return None
        overrides = contract.get("term_overrides") if isinstance(contract, Mapping) else None
        candidates.append({
            "ordinal": index,
            "candidate_id": _safe_text(candidate_id, 160),
            "product_id": _safe_text(candidate.get("product_id"), 80),
            "rule_revision": contract.get("rule_revision"),
            "product_name": _safe_text(candidate.get("product_name"), 160),
            "underlyings": [
                _safe_text(item, 80)
                for item in candidate.get("underlyings", [])[:8]
            ] if isinstance(candidate.get("underlyings"), list) else [],
            "key_terms": _safe_mapping(overrides) if isinstance(overrides, Mapping) else {},
            "approval_status": _safe_text(contract.get("approval_status"), 80),
            "reason": _safe_text(public_projection.get("reason"), 800) if isinstance(public_projection, Mapping) else "",
            "suitable_for": [
                _safe_text(item, 800) for item in public_projection.get("suitable_for", [])[:16]
            ] if isinstance(public_projection, Mapping) and isinstance(public_projection.get("suitable_for"), list) else [],
            "not_suitable_for": [
                _safe_text(item, 800) for item in public_projection.get("not_suitable_for", [])[:16]
            ] if isinstance(public_projection, Mapping) and isinstance(public_projection.get("not_suitable_for"), list) else [],
            "main_risks": [
                _safe_text(item, 800) for item in public_projection.get("main_risks", [])[:16]
            ] if isinstance(public_projection, Mapping) and isinstance(public_projection.get("main_risks"), list) else [],
            "proposed_terms": list(public_projection.get("key_terms", [])) if isinstance(public_projection, Mapping) else [],
        })
    return {
        "status": status,
        "candidate_count": len(candidates),
        "candidate_ids": [item["candidate_id"] for item in candidates],
        "approved_candidate_ids": [
            _safe_text(item, 160) for item in approved[:10] if isinstance(item, str)
        ],
        "candidates": candidates,
        "delivery_selected": value.get("delivery") is not None,
    }


def _refs(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_safe_mapping(item) for item in value[-12:] if isinstance(item, Mapping)]


def _safe_mapping(value: object, *, _depth: int = 0) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    if _depth >= 4:
        return {}
    result: dict[str, Any] = {}
    for raw_key, raw_value in list(value.items())[:48]:
        key = str(raw_key)
        if _SECRET_KEY.search(key) or key.lower() in {
            "storage_ref", "path", "file_path", "content", "excerpt",
            "tenant_id", "principal_id", "session_id", "created_by", "access_scope", "lineage",
        }:
            continue
        if isinstance(raw_value, Mapping):
            result[key] = _safe_mapping(raw_value, _depth=_depth + 1)
        elif isinstance(raw_value, list):
            result[key] = [_safe_mapping(item, _depth=_depth + 1) if isinstance(item, Mapping) else _safe_text(item, 160) for item in raw_value[:16]]
        else:
            result[key] = _safe_text(raw_value, 240)
    return result


def _safe_text(value: object, limit: int, identity: SessionIdentity | None = None) -> str:
    return _redact_text(str(value or ""), identity)[:limit]


def _redact_text(value: object, identity: SessionIdentity | None = None) -> str:
    return redact_text(value, identity)


__all__ = ("ContextBuilder", "ResultStoreObservationBuilder", "conversation_tool_catalog")
