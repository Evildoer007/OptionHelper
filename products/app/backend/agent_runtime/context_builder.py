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
from ..stores.contract_store import ContractStore
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
        {"name": "knowledger.search", "description": "查询受治理期权资料库", "actions": ["search"]},
        {"name": "recommender.run", "description": "运行一次固定结构推荐流程", "actions": ["run"]},
        {"name": "datafetcher.status", "description": "查询数据能力状态", "actions": ["status"]},
        {"name": "datafetcher.fetch", "description": "按受控Provider获取市场数据", "actions": ["fetch"]},
        {"name": "datafetcher.fetch_calendar", "description": "获取中国交易所未来交易日期，不读取未来价格", "actions": ["fetch_calendar"]},
        {"name": "payoffer.run", "description": "基于ResolvedContract生成本次收益图", "actions": ["run"]},
        {"name": "pricer.run", "description": "基于ResolvedContract与市场数据执行估值", "actions": ["run"]},
        {"name": "backtester.run", "description": "基于ResolvedContract与历史数据执行回测", "actions": ["run"]},
        {
            "name": "reporter.run",
            "description": "将当前任务已完成的正式分析整理为交付物；arguments只填kind、format或title。详细报告固定为连续A4七段正文，HTML宽屏提供左侧章节目录。",
            "actions": ["run"],
            "output_types": ["card"],
        },
    ]
    if identity.role.value == "admin":
        tools[-1] = {**tools[-1], "output_types": ["card", "report"]}
    return tools


class ContextBuilder:
    """Build a bounded, task-owned Agent context without a hidden memory store."""

    def __init__(
        self,
        task_service: TaskService,
        contract_store: ContractStore,
        policy: AuthorizationPolicy,
        *,
        catalog_version: str,
        max_messages: int = 12,
        result_store: ResultStore | None = None,
    ) -> None:
        if max_messages < 1:
            raise ValueError("max_messages must be positive")
        self._tasks = task_service
        self._contracts = contract_store
        self._policy = policy
        self._catalog_version = catalog_version
        self._max_messages = max_messages
        self._results = result_store

    def build(self, identity: SessionIdentity, task_id: str, latest_message: str) -> dict[str, Any]:
        task = self._tasks.get(identity, task_id)
        contract = self._contracts.get(identity, task_id)
        messages = task.get("messages", [])
        if not isinstance(messages, list):
            messages = []
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
            "messages": [_message_fact(row, identity) for row in messages[-self._max_messages:] if isinstance(row, Mapping)],
            "latest_message": _redact_text(latest_message, identity),
            "facts": {
                "resolved_contract": _contract_fact(contract),
                "data_asset_refs": _refs(task.get("data_asset_refs")),
                "module_run_refs": _refs(task.get("run_refs")),
                "module_run_facts": self._module_run_facts(identity, task.get("run_refs")),
            },
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


def _message_fact(value: Mapping[str, Any], identity: SessionIdentity) -> dict[str, str]:
    role = str(value.get("role", "user"))
    if role not in {"user", "assistant", "system"}:
        role = "user"
    return {
        "role": role,
        "content": _safe_text(value.get("content", ""), 2_000, identity),
        "status": _safe_text(value.get("status"), 80, identity),
    }


def _contract_fact(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    contract = value.get("resolved_contract")
    if not isinstance(contract, Mapping):
        return None
    identity = contract.get("identity")
    return {
        "contract_ref": _safe_mapping(value.get("contract_ref")),
        "contract_fingerprint": _safe_text(value.get("contract_fingerprint"), 80),
        "catalog_version": _safe_text(value.get("catalog_version"), 80),
        "identity": _safe_mapping(identity),
        "terms": _safe_mapping(contract.get("terms")),
        "path_count": len(contract.get("paths", [])) if isinstance(contract.get("paths"), list) else 0,
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
