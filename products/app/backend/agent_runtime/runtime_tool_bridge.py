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
from typing import Any, Protocol
from uuid import uuid4

from ..errors import AuthorizationError, UnavailableCapabilityError, ValidationError
from ..identity.session_identity import SessionIdentity


TOOL_NAMES = (
    "search_option_structures",
    "create_candidate_version",
    "evaluate_candidate_payoff",
    "evaluate_candidate_pricing",
    "evaluate_candidate_backtest",
    "compare_candidate_versions",
    "get_verified_fact",
)
_EVALUATION_TO_MODULE = {
    "evaluate_candidate_payoff": "payoffer",
    "evaluate_candidate_pricing": "pricer",
    "evaluate_candidate_backtest": "backtester",
}
_HOST_FIELDS = frozenset({"tenant_id", "principal_id", "session_id", "task_id", "workflow_id", "agent_run_id", "connection_id"})
_FORBIDDEN_KEYS = frozenset({
    "secret", "secrets", "password", "credential", "credentials", "apikey", "api_key",
    "access_token", "authorization", "private_key", "system_prompt", "hidden_reasoning",
    "reasoning_content", "path", "filepath", "directory", "result_dir", "storage_ref",
})


class ToolDispatcherPort(Protocol):
    def dispatch_for_conversation(self, tool_name: str, payload: dict[str, Any], identity: SessionIdentity, *, module_context: object, request_id: str, candidate_variant: Mapping[str, Any] | None = None) -> dict[str, Any]: ...


class CandidateStorePort(Protocol):
    def get_candidate_variant(self, identity: SessionIdentity, task_id: str, candidate_key: str, candidate_version_id: str) -> Mapping[str, Any] | None: ...

    def put_candidate_variant(self, identity: SessionIdentity, task_id: str, candidate_key: str, candidate_version_id: str, prepared: Mapping[str, Any], *, catalog_version: str, parent_version_id: str | None = None, revision: int = 0) -> Mapping[str, Any]: ...


class VerifiedFactStorePort(Protocol):
    def verified_fact_summary(self, identity: SessionIdentity, reference: Mapping[str, str], *, expected_binding: Mapping[str, str] | None = None) -> Mapping[str, Any]: ...


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
    max_tools: int = 12
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
        if isinstance(self.max_tools, bool) or not isinstance(self.max_tools, int) or not 0 <= self.max_tools <= 12:
            raise ValidationError("ToolConnection.max_tools必须位于0至12")

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
        return [_safe_input(item, field_name=field_name) for item in value[:256]]
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
        dispatcher: ToolDispatcherPort | object | None = None,
        result_store: VerifiedFactStorePort | None = None,
        candidate_store: CandidateStorePort | None = None,
        task_service: object | None = None,
        module_context_for: Callable[..., object] | None = None,
        search_handler: ToolHandler | None = None,
        compare_handler: ToolHandler | None = None,
        handlers: Mapping[str, ToolHandler] | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._results = result_store
        self._candidates = candidate_store
        self._tasks = task_service
        self._module_context_for = module_context_for
        self._handlers: dict[str, ToolHandler] = {}
        if search_handler is not None:
            self._handlers["search_option_structures"] = search_handler
        if compare_handler is not None:
            self._handlers["compare_candidate_versions"] = compare_handler
        for name, handler in dict(handlers or {}).items():
            self.register_handler(name, handler)
        self._connections: dict[str, _ConnectionState] = {}
        self._lock = RLock()
        self._definitions = _tool_definitions()

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
        max_tools: int = 12,
    ) -> ToolConnection:
        if not isinstance(identity, SessionIdentity):
            raise ValidationError("ToolConnection需要已认证SessionIdentity")
        if self._tasks is not None:
            getter = getattr(self._tasks, "get", None)
            if callable(getter):
                getter(identity, task_id)
        allowed = tuple(allowed_tools or TOOL_NAMES)
        if any(item not in TOOL_NAMES for item in allowed):
            raise ValidationError("allowed_tools包含未声明工具")
        if isinstance(max_tools, bool) or not isinstance(max_tools, int) or not 0 <= max_tools <= 12:
            raise ValidationError("max_tools必须位于0至12")
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
            if state.tool_calls >= state.connection.max_tools:
                raise ValidationError("当前Child Session工具调用次数超过12次上限")
            state.tool_calls += 1
        payload = {"task_id": state.connection.task_id, **request}
        result = self._dispatch(name, state, payload, normalized_request_id)
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
        if name == "create_candidate_version":
            return self._create_candidate(state, payload)
        if name == "get_verified_fact":
            return self._get_fact(state, payload)
        if name == "compare_candidate_versions":
            raise UnavailableCapabilityError("compare_candidate_versions", "当前App未绑定候选比较适配器。")
        module = _EVALUATION_TO_MODULE.get(name)
        if module is not None:
            return self._evaluate(module, state, payload, request_id)
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

    def _create_candidate(self, state: _ConnectionState, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._candidates is None:
            raise UnavailableCapabilityError("create_candidate_version", "当前App未绑定CandidateVersion存储适配器。")
        candidate_key, version_id = _candidate_ids(payload)
        prepared = payload.get("prepared", payload.get("candidate"))
        if not isinstance(prepared, Mapping):
            raise ValidationError("create_candidate_version需要prepared对象")
        catalog_version = payload.get("catalog_version")
        if not isinstance(catalog_version, str) or not catalog_version.strip():
            raise ValidationError("create_candidate_version需要catalog_version")
        parent = payload.get("parent_version_id")
        revision = payload.get("revision", 0)
        if parent is not None and not isinstance(parent, str):
            raise ValidationError("parent_version_id无效")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValidationError("revision无效")
        stored = self._candidates.put_candidate_variant(
            state.identity,
            state.connection.task_id,
            candidate_key,
            version_id,
            dict(prepared),
            catalog_version=catalog_version,
            parent_version_id=parent,
            revision=revision,
        )
        if not isinstance(stored, Mapping):
            raise ValidationError("CandidateVersion存储适配器返回无效对象")
        return {
            "status": "completed",
            "candidate_ref": {
                "candidate_key": candidate_key,
                "candidate_version_id": version_id,
                "revision": revision,
            },
            "contract_fingerprint": stored.get("contract_fingerprint"),
        }

    def _evaluate(self, module: str, state: _ConnectionState, payload: Mapping[str, Any], request_id: str) -> dict[str, Any]:
        candidate = self._candidate_variant(state, payload)
        dispatcher = self._dispatcher
        if dispatcher is None:
            raise UnavailableCapabilityError(module, "当前App未绑定ToolDispatcher适配器。")
        module_context = self._module_context(module, payload, state.identity)
        call = getattr(dispatcher, "dispatch_for_conversation", None)
        if callable(call):
            if module_context is None:
                raise UnavailableCapabilityError(module, "当前App未绑定受控ModuleHostContext适配器。")
            raw = call(
                module,
                dict(payload),
                state.identity,
                module_context=module_context,
                request_id=request_id,
                candidate_variant=candidate,
            )
        else:
            call = getattr(dispatcher, "dispatch", None)
            if not callable(call):
                raise ValidationError("ToolDispatcher适配器缺少dispatch")
            raw = call(
                module,
                dict(payload),
                state.identity,
                module_context=module_context,
                request_id=request_id,
                agent_proxy=True,
                candidate_variant=candidate,
            )
        if not isinstance(raw, Mapping):
            raise ValidationError("ToolDispatcher必须返回对象")
        return self._project_module_result(module, state, raw, payload)

    def _module_context(self, module: str, payload: Mapping[str, Any], identity: SessionIdentity) -> object | None:
        if self._module_context_for is None:
            return None
        signature = inspect.signature(self._module_context_for)
        positional = [
            item for item in signature.parameters.values()
            if item.kind in {item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD}
        ]
        if len(positional) >= 3:
            return self._module_context_for(module, payload, identity)
        return self._module_context_for(module, payload)

    def _candidate_variant(self, state: _ConnectionState, payload: Mapping[str, Any]) -> dict[str, Any]:
        candidate_key, version_id = _candidate_ids(payload)
        stored: Mapping[str, Any] | None = None
        if self._candidates is not None:
            stored = self._candidates.get_candidate_variant(state.identity, state.connection.task_id, candidate_key, version_id)
        if not isinstance(stored, Mapping):
            raise ValidationError("CandidateVersion不存在或不属于当前Task")
        if stored.get("candidate_key") != candidate_key or stored.get("candidate_version_id") != version_id:
            raise ValidationError("CandidateVersion身份与存储记录不一致")
        requested_revision = payload.get("revision")
        actual_revision = stored.get("revision", 0)
        if requested_revision is not None and requested_revision != actual_revision:
            raise ValidationError("CandidateVersion revision已变化")
        return {
            "candidate_key": candidate_key,
            "candidate_version_id": version_id,
            "parent_version_id": stored.get("parent_version_id"),
            "revision": actual_revision,
        }

    def _project_module_result(self, module: str, state: _ConnectionState, raw: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        reference = raw.get("module_run_ref")
        if isinstance(reference, Mapping):
            clean_reference = {
                key: str(reference.get(key, ""))
                for key in (
                    "module", "tenant_id", "task_id", "run_id",
                    "expected_semantic_result_hash", "expected_artifact_manifest_hash",
                )
                if reference.get(key) is not None
            }
            if clean_reference.get("module") != module or clean_reference.get("tenant_id") != state.identity.tenant_id or clean_reference.get("task_id") != state.connection.task_id:
                raise AuthorizationError("agent_runtime.result", "ModuleRunRef不属于当前Task")
            fact = None
            if self._results is not None:
                fact = self._results.verified_fact_summary(
                    state.identity,
                    clean_reference,
                    expected_binding={
                        "candidate_key": str(payload["candidate_key"]),
                        "candidate_version_id": str(payload["candidate_version_id"]),
                    },
                )
            if isinstance(fact, Mapping):
                run_ref = fact.get("run_ref")
                return {
                    "status": str(raw.get("status", "completed")),
                    "module": module,
                    "fact_ref": _safe_output(run_ref if isinstance(run_ref, Mapping) else clean_reference),
                    "facts": _safe_output(fact.get("facts", [])),
                }
            return {
                "status": str(raw.get("status", "completed")),
                "module": module,
                "fact_ref": _safe_output(clean_reference),
            }
        return {
            "status": str(raw.get("status", "failed")),
            "module": module,
            "message": str(raw.get("message", raw.get("reason", "模块未返回可验证结果。")))[:500],
        }

    def _get_fact(self, state: _ConnectionState, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._results is None:
            raise UnavailableCapabilityError("get_verified_fact", "当前App未绑定ResultStore适配器。")
        raw_ref = payload.get("module_run_ref", payload.get("fact_ref"))
        if not isinstance(raw_ref, Mapping):
            raise ValidationError("get_verified_fact需要ModuleRunRef")
        required = {
            "module", "tenant_id", "task_id", "run_id",
            "expected_semantic_result_hash", "expected_artifact_manifest_hash",
        }
        if set(raw_ref) != required:
            raise ValidationError("ModuleRunRef字段不完整")
        if raw_ref.get("tenant_id") != state.identity.tenant_id or raw_ref.get("task_id") != state.connection.task_id:
            raise AuthorizationError("agent_runtime.result", "ModuleRunRef不属于当前Task")
        expected = {
            key: str(payload[key])
            for key in ("candidate_key", "candidate_version_id", "contract_fingerprint")
            if key in payload
        }
        fact = self._results.verified_fact_summary(state.identity, dict(raw_ref), expected_binding=expected or None)
        if not isinstance(fact, Mapping):
            raise ValidationError("ResultStore事实投影无效")
        return {"status": "completed", "fact_ref": _safe_output(fact.get("run_ref", {})), "facts": _safe_output(fact.get("facts", []))}

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


def _candidate_ids(payload: Mapping[str, Any]) -> tuple[str, str]:
    candidate_key = payload.get("candidate_key")
    version_id = payload.get("candidate_version_id")
    if not isinstance(candidate_key, str) or not candidate_key.strip() or not isinstance(version_id, str) or not version_id.strip():
        raise ValidationError("工具调用需要candidate_key和candidate_version_id")
    for value, field_name in ((candidate_key, "candidate_key"), (version_id, "candidate_version_id")):
        if len(value) > 160 or any(ord(char) < 33 for char in value):
            raise ValidationError(f"{field_name}无效")
    return candidate_key.strip(), version_id.strip()


def _tool_definitions() -> dict[str, ToolDefinition]:
    return {
        "search_option_structures": ToolDefinition("search_option_structures", "检索受控期权结构候选。", ("query", "constraints")),
        "create_candidate_version": ToolDefinition("create_candidate_version", "保存不可激活的CandidateVersion。", ("candidate_key", "candidate_version_id", "prepared", "catalog_version"), True),
        "evaluate_candidate_payoff": ToolDefinition("evaluate_candidate_payoff", "对指定CandidateVersion执行收益评估。", ("candidate_key", "candidate_version_id"), True),
        "evaluate_candidate_pricing": ToolDefinition("evaluate_candidate_pricing", "对指定CandidateVersion执行估值评估。", ("candidate_key", "candidate_version_id"), True),
        "evaluate_candidate_backtest": ToolDefinition("evaluate_candidate_backtest", "对指定CandidateVersion执行历史回放。", ("candidate_key", "candidate_version_id"), True),
        "compare_candidate_versions": ToolDefinition("compare_candidate_versions", "比较已验证候选事实。", ("candidate_version_ids",)),
        "get_verified_fact": ToolDefinition("get_verified_fact", "读取已验证ModuleRun的受控事实。", ("module_run_ref",)),
    }


__all__ = [
    "CandidateStorePort",
    "RuntimeToolBridge",
    "ToolCallResult",
    "ToolConnection",
    "ToolDefinition",
    "ToolDispatcherPort",
    "VerifiedFactStorePort",
]
