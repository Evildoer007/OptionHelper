"""Authenticated business-tool bridge for OptionHelper Agent runs.

The bridge exposes only business-level operations. It delegates calculation
execution to the existing ToolDispatcher and reads verified facts from the
existing ResultStore; it never lets an Agent choose identity, storage paths or
credentials.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import hmac
import inspect
import json
import secrets
from threading import RLock
from typing import Any
from uuid import uuid4

from ..errors import AuthorizationError, UnavailableCapabilityError, ValidationError
from ..identity.session_identity import SessionIdentity


TOOL_NAMES = ("search_option_structures", "evaluate_research_candidate", "read_research_evidence")
_HOST_FIELDS = frozenset({"tenant_id", "principal_id", "session_id", "task_id", "workflow_id", "agent_run_id", "connection_id"})
_FORBIDDEN_KEYS = frozenset({
    "secret", "secrets", "password", "credential", "credentials", "apikey", "api_key",
    "access_token", "authorization", "private_key", "system_prompt", "hidden_reasoning",
    "reasoning_content", "path", "filepath", "directory", "result_dir", "storage_ref",
})


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_fields: tuple[str, ...]
    mutating: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_fields": list(self.input_fields),
            "mutating": self.mutating,
        }


@dataclass(frozen=True)
class ToolConnection:
    connection_id: str
    task_id: str
    workflow_id: str
    agent_run_id: str
    role_id: str
    allowed_tools: tuple[str, ...]
    max_tools: int = 0
    _access_token: str = field(repr=False, compare=False, default="")

    def __post_init__(self) -> None:
        for name in ("connection_id", "task_id", "workflow_id", "agent_run_id", "role_id"):
            value = str(getattr(self, name) or "").strip()
            if not value or len(value) > 160 or any(ord(char) < 33 for char in value):
                raise ValidationError(f"ToolConnection.{name}无效")
            object.__setattr__(self, name, value)
        if not self._access_token or len(self._access_token) < 24:
            raise ValidationError("ToolConnection缺少访问凭据")
        allowed = tuple(str(item).strip() for item in self.allowed_tools)
        if any(item not in TOOL_NAMES for item in allowed) or len(set(allowed)) != len(allowed):
            raise ValidationError("ToolConnection.allowed_tools无效")
        object.__setattr__(self, "allowed_tools", allowed)
        if isinstance(self.max_tools, bool) or not isinstance(self.max_tools, int) or self.max_tools < 0:
            raise ValidationError("ToolConnection.max_tools必须为非负整数，0表示不限")

    @property
    def access_token(self) -> str:
        """Return the ephemeral capability token to the in-process caller."""

        return self._access_token

    def public(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "task_id": self.task_id,
            "workflow_id": self.workflow_id,
            "agent_run_id": self.agent_run_id,
            "role_id": self.role_id,
            "allowed_tools": list(self.allowed_tools),
            "max_tools": self.max_tools,
        }


@dataclass(frozen=True)
class ToolCallResult:
    request_id: str
    tool_name: str
    status: str
    result: Mapping[str, Any]
    idempotent_replay: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "result": dict(self.result),
            "idempotent_replay": self.idempotent_replay,
        }


@dataclass
class _ConnectionState:
    connection: ToolConnection
    identity: SessionIdentity
    request_cache: dict[str, tuple[str, ToolCallResult]] = field(default_factory=dict)
    tool_calls: int = 0


ToolHandler = Callable[..., Mapping[str, Any]]


def _safe_input(value: object, *, field_name: str = "tool_input") -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _HOST_FIELDS:
                raise ValidationError(f"{field_name}包含Host管理字段")
            if normalized in _FORBIDDEN_KEYS or any(marker in normalized for marker in ("secret", "password", "credential", "api_key", "token")):
                raise ValidationError(f"{field_name}包含不允许的敏感字段")
            result[str(key)] = _safe_input(item, field_name=field_name)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_input(item, field_name=field_name) for item in value]
    if isinstance(value, str):
        if len(value) > 128_000:
            raise ValidationError(f"{field_name}文本超过上限")
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValidationError(f"{field_name}必须是JSON值")


def _safe_output(value: object, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS or any(marker in normalized for marker in ("secret", "password", "credential", "api_key", "token")):
                continue
            if normalized in {"path", "filepath", "directory", "result_dir", "storage_ref"}:
                continue
            result[str(key)] = _safe_output(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_output(item, depth=depth + 1) for item in value[:256]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:64_000]
    return str(value)[:1_000]


def _request_hash(tool_name: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps({"tool": tool_name, "payload": payload}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class RuntimeToolBridge:
    """Expose authenticated, task-bound business operations to Agent runs."""

    def __init__(
        self,
        *,
        task_service: object | None = None,
        search_handler: ToolHandler | None = None,
        handlers: Mapping[str, ToolHandler] | None = None,
    ) -> None:
        self._tasks = task_service
        self._handlers: dict[str, ToolHandler] = {}
        if search_handler is not None:
            self._handlers["search_option_structures"] = search_handler
        for name, handler in dict(handlers or {}).items():
            self.register_handler(name, handler)
        self._connections: dict[str, _ConnectionState] = {}
        self._lock = RLock()
        self._definitions = _tool_definitions()
        self._research = None

    def bind_research_session(self, research):
        with self._lock:
            self._research = research
            for state in self._connections.values():
                state.request_cache.clear()
                state.tool_calls = 0
        self.register_handler("evaluate_research_candidate", self._evaluate_research)
        self.register_handler("read_research_evidence", self._read_research)

    def _evaluate_research(self, identity, task_id, payload, connection):
        research = self._research
        if research is None or research.case.task_id != task_id or research.case.tenant_id != identity.tenant_id:
            raise ValidationError("当前任务没有可用研究会话")
        return research.evaluate({key:value for key,value in payload.items() if key != "task_id"}, role=connection.role_id)

    def _read_research(self, identity, task_id, payload, connection):
        research = self._research
        if research is None or research.case.task_id != task_id or research.case.tenant_id != identity.tenant_id:
            raise ValidationError("当前任务没有可用研究会话")
        if set(payload)-{"task_id", "candidate_id", "module", "result_path", "offset", "limit", "expected_run_ref", "catalog_ref", "stage_ref"}:
            raise ValidationError("读取研究证据含未声明字段")
        if "stage_ref" in payload:
            if set(payload) - {"task_id", "stage_ref", "result_path"}:
                raise ValidationError("冻结阶段读取不能混用其他参数")
            return research.read_stage_input(payload["stage_ref"], connection.role_id, payload.get("result_path", []))
        if "catalog_ref" in payload:
            if set(payload) - {"task_id", "catalog_ref"}:
                raise ValidationError("冻结目录读取不能混用结果读取参数")
            return research.read_catalog(payload["catalog_ref"])
        if "module" in payload:
            return research.read_result(payload.get("candidate_id"),payload["module"],payload.get("result_path",[]),
                role=connection.role_id,offset=payload.get("offset",0),limit=payload.get("limit",64),expected_run_ref=payload.get("expected_run_ref"))
        if set(payload)-{"task_id", "candidate_id"}:
            raise ValidationError("读取结果字段时必须指定module")
        return {"status":"completed", "evidence":research.read(payload.get("candidate_id"), role=connection.role_id)}

    def register_handler(self, tool_name: str, handler: ToolHandler) -> None:
        name = str(tool_name).strip()
        if name not in TOOL_NAMES or not callable(handler):
            raise ValidationError("只能注册已声明的OptionHelper业务工具")
        self._handlers[name] = handler

    def open_connection(
        self,
        identity: SessionIdentity,
        *,
        task_id: str,
        workflow_id: str,
        agent_run_id: str,
        role_id: str,
        allowed_tools: tuple[str, ...] | list[str] | None = None,
        max_tools: int = 0,
    ) -> ToolConnection:
        if not isinstance(identity, SessionIdentity):
            raise ValidationError("ToolConnection需要已认证SessionIdentity")
        if self._tasks is not None:
            getter = getattr(self._tasks, "get", None)
            if callable(getter):
                getter(identity, task_id)
        allowed = tuple(TOOL_NAMES if allowed_tools is None else allowed_tools)
        if any(item not in TOOL_NAMES for item in allowed):
            raise ValidationError("allowed_tools包含未声明工具")
        if isinstance(max_tools, bool) or not isinstance(max_tools, int) or max_tools < 0:
            raise ValidationError("max_tools必须为非负整数，0表示不限")
        token = secrets.token_urlsafe(32)
        connection = ToolConnection(
            connection_id=f"tool-connection-{uuid4().hex}",
            task_id=str(task_id).strip(),
            workflow_id=str(workflow_id).strip(),
            agent_run_id=str(agent_run_id).strip(),
            role_id=str(role_id).strip(),
            allowed_tools=allowed,
            max_tools=max_tools,
            _access_token=token,
        )
        with self._lock:
            self._connections[connection.connection_id] = _ConnectionState(connection, identity)
        return connection

    def close_connection(self, connection: ToolConnection | Mapping[str, Any] | str) -> None:
        connection_id = self._connection_id(connection)
        with self._lock:
            self._connections.pop(connection_id, None)

    def list_tools(self, connection: ToolConnection | Mapping[str, Any]) -> list[dict[str, Any]]:
        state = self._authenticate(connection)
        return [self._definitions[name].to_dict() for name in state.connection.allowed_tools]

    def model_tool_schemas(self, allowed_tools: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
        """Project non-secret business definitions into model function schemas."""

        names = tuple(str(name) for name in allowed_tools)
        if any(name not in self._definitions for name in names):
            raise ValidationError("模型工具Schema包含未注册业务工具")
        schemas: list[dict[str, Any]] = []
        for name in names:
            definition = self._definitions[name]
            schemas.append({
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            field: {"description": f"{field}业务参数"}
                            for field in definition.input_fields
                        },
                        "additionalProperties": True,
                    },
                },
            })
        for schema in schemas:
            function = schema["function"]
            if function["name"] == "evaluate_research_candidate":
                function["parameters"] = {
                    "type":"object", "additionalProperties":False,
                    "required":["candidate_id","modules","question"],
                    "properties":{
                        "candidate_id":{"type":"string","minLength":1},
                        "modules":{"type":"array","minItems":1,"uniqueItems":True,
                                   "items":{"type":"string","enum":["payoffer","pricer","backtester"]}},
                        "question":{"type":"string","minLength":1},
                        "round_no":{"type":"integer","minimum":1},
                    },
                }
            elif function["name"] == "read_research_evidence":
                function["parameters"]={"type":"object","additionalProperties":False,
                    "properties":{"stage_ref":{"type":"object","additionalProperties":False,"required":["research_id","role","content_hash"],"properties":{key:{"type":"string","minLength":1} for key in ("research_id","role","content_hash")}},"catalog_ref":{"type":"object","additionalProperties":False,"required":["evidence_id","catalog_version","excerpt_hash","research_id"],"properties":{key:{"type":"string","minLength":1} for key in ("evidence_id","catalog_version","excerpt_hash","research_id")}}, "candidate_id":{"type":"string"},
                        "module":{"type":"string","enum":["payoffer","pricer","backtester"]},
                        "result_path":{"type":"array","maxItems":12,"items":{"anyOf":[{"type":"string"},{"type":"integer","minimum":0}]}},
                        "expected_run_ref":{"type":"object","additionalProperties":False,"required":["module","tenant_id","task_id","run_id","expected_result_file_hash","expected_artifact_manifest_hash"],"properties":{key:{"type":"string","minLength":1} for key in ("module","tenant_id","task_id","run_id","expected_result_file_hash","expected_artifact_manifest_hash")}},
                        "offset":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":128}}}
        return schemas

    def call(
        self,
        connection: ToolConnection | Mapping[str, Any],
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        request_id: str,
    ) -> ToolCallResult:
        state = self._authenticate(connection)
        name = str(tool_name or "").strip()
        if name not in TOOL_NAMES:
            raise ValidationError("业务工具未注册")
        if name not in state.connection.allowed_tools:
            raise AuthorizationError("agent_runtime.tool", "当前角色没有该业务工具权限")
        request = _safe_input(dict(arguments or {}))
        if not isinstance(request, dict):
            raise ValidationError("工具arguments必须是对象")
        # A cached stage read must still belong to the live role and current contracts.
        # Revalidate before the idempotent cache can return an old successful response.
        if name == "read_research_evidence" and "stage_ref" in request:
            self._read_research(state.identity, state.connection.task_id, request, state.connection)
        normalized_request_id = self._request_id(request_id)
        request_digest = _request_hash(name, request)
        with self._lock:
            cached = state.request_cache.get(normalized_request_id)
            if cached is not None:
                previous_hash, previous_result = cached
                if previous_hash != request_digest:
                    raise ValidationError("同一request_id不能对应不同工具输入")
                return ToolCallResult(
                    previous_result.request_id,
                    previous_result.tool_name,
                    previous_result.status,
                    previous_result.result,
                    idempotent_replay=True,
                )
            if state.connection.max_tools > 0 and state.tool_calls >= state.connection.max_tools:
                raise ValidationError("工具调用次数达到显式配置的上限")
            state.tool_calls += 1
        payload = {"task_id": state.connection.task_id, **request}
        research = self._research
        result = self._dispatch(name, state, payload, normalized_request_id)
        if name == "search_option_structures" and research is not None and result.get("ok") is True:
            with self._lock:
                if self._research is not research or research.case.task_id != state.connection.task_id or research.case.tenant_id != state.identity.tenant_id:
                    raise ValidationError("检索结果不属于当前研究会话")
                research.record_catalog_search(result, role=state.connection.role_id)
        projected = ToolCallResult(
            request_id=normalized_request_id,
            tool_name=name,
            status=str(result.get("status", "completed")),
            result=_safe_output(result),
        )
        with self._lock:
            state.request_cache[normalized_request_id] = (request_digest, projected)
        return projected

    def _dispatch(self, name: str, state: _ConnectionState, payload: Mapping[str, Any], request_id: str) -> dict[str, Any]:
        custom = self._handlers.get(name)
        if custom is not None:
            return self._call_handler(custom, state, payload, request_id)
        raise UnavailableCapabilityError("search_option_structures", "当前App未绑定期权结构搜索适配器。")

    def _call_handler(self, handler: ToolHandler, state: _ConnectionState, payload: Mapping[str, Any], request_id: str) -> dict[str, Any]:
        signature = inspect.signature(handler)
        count = len(signature.parameters)
        if count >= 4:
            raw = handler(state.identity, state.connection.task_id, dict(payload), state.connection)
        elif count == 3:
            raw = handler(state.identity, state.connection.task_id, dict(payload))
        elif count == 2:
            raw = handler(state.connection, dict(payload))
        else:
            raw = handler(dict(payload))
        if not isinstance(raw, Mapping):
            raise ValidationError("业务工具适配器必须返回对象")
        return {**dict(_safe_output(raw)), "request_id": request_id}

    def _authenticate(self, value: ToolConnection | Mapping[str, Any]) -> _ConnectionState:
        if isinstance(value, ToolConnection):
            connection_id, token = value.connection_id, value.access_token
        elif isinstance(value, Mapping):
            connection_id = str(value.get("connection_id", ""))
            token = str(value.get("access_token", ""))
        else:
            raise AuthorizationError("agent_runtime.tool", "工具连接凭据无效")
        with self._lock:
            state = self._connections.get(connection_id)
        if state is None or not hmac.compare_digest(state.connection._access_token, token):
            raise AuthorizationError("agent_runtime.tool", "工具连接已失效或不属于当前运行")
        return state

    @staticmethod
    def _connection_id(value: ToolConnection | Mapping[str, Any] | str) -> str:
        if isinstance(value, ToolConnection):
            return value.connection_id
        if isinstance(value, Mapping):
            return str(value.get("connection_id", ""))
        return str(value)

    @staticmethod
    def _request_id(value: object) -> str:
        result = str(value or "").strip()
        if not result or len(result) > 160 or any(ord(char) < 33 for char in result):
            raise ValidationError("request_id必须是有限的可打印标识")
        return result


def _tool_definitions() -> dict[str, ToolDefinition]:
    return {
        "evaluate_research_candidate": ToolDefinition("evaluate_research_candidate", "为已登记候选调用真实计算；必须说明待验证问题。", ("candidate_id", "modules", "question", "round_no"), True),
        "read_research_evidence": ToolDefinition("read_research_evidence", "stage_history中的stage_ref可原样传回以读取本轮本角色冻结阶段原始输入，可用result_path定向读取字段，不与candidate_id/module等参数混用；候选改变后旧引用失效。读取已有证据。指定candidate_id和module后，可用result_path逐层读取完整结果的字段、风险曲线或情景，并按offset和limit分页；不会重新计算。较大的子项会在deferred_fields中给出result_path，按需进入读取；始终使用返回的next_offset翻页，数组的value_indices表示原始位置。概览不等于已经读完所有细项。catalog_references中read_request可原样传回，读取本轮冻结目录原文；引用不可修改，且不与其他读取参数混用。取回归档页时传入其expected_run_ref，严格读取指定冻结版本；候选或来源失效时返回错误，不替换运行。", ("candidate_id", "module", "result_path", "offset", "limit", "expected_run_ref", "catalog_ref", "stage_ref")),
        "search_option_structures": ToolDefinition("search_option_structures", "检索受控期权结构候选。", ("query", "constraints")),
    }


__all__ = [
    "RuntimeToolBridge",
    "ToolCallResult",
    "ToolConnection",
    "ToolDefinition",
]
