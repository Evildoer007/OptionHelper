"""Recommender只消费的受控端口客户端。

端口的服务端实现归外部Host或App所有。本模块不读取Knowledger文件，也不导入
DataFetcher、Payoffer、Pricer、Backtester、Reporter内部代码。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import json
import hashlib
from collections.abc import Iterator
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

from .models import ModelCapability


class PortError(RuntimeError):
    """正式端口不可达、拒绝请求或返回非法协议。"""


_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class AgentExecutionPolicy:
    """Workflow-owned Agent preference without binding a module or provider."""

    preferred_mode: str = "auto"
    allow_single_agent_fallback: bool = True

    def __post_init__(self) -> None:
        if self.preferred_mode not in {"auto", "single", "multi"}:
            raise ValueError("preferred_mode必须为auto、single或multi")


@dataclass(frozen=True)
class AgentRunReceipt:
    """Host-issued proof for one settled role-scoped Agent run."""

    agent_run_id: str
    child_session_id: str
    role: str
    input_hash: str
    output_hash: str
    status: str = "completed"

    def __post_init__(self) -> None:
        for field_name in ("agent_run_id", "child_session_id", "role"):
            value = str(getattr(self, field_name)).strip()
            if not value:
                raise ValueError(f"AgentRunReceipt.{field_name}不能为空")
            object.__setattr__(self, field_name, value)
        for field_name in ("input_hash", "output_hash"):
            value = str(getattr(self, field_name)).strip()
            if not _SHA256.fullmatch(value):
                raise ValueError(f"AgentRunReceipt.{field_name}必须是SHA-256")
            object.__setattr__(self, field_name, value)
        if self.status != "completed":
            raise ValueError("AgentRunReceipt只接受已完成运行")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AgentRunReceipt":
        if not isinstance(value, Mapping):
            raise ValueError("AgentRunReceipt必须是对象")
        allowed = {
            "agent_run_id", "child_session_id", "role",
            "input_hash", "output_hash", "status",
        }
        unknown = set(value).difference(allowed)
        if unknown:
            raise ValueError(f"AgentRunReceipt含未知字段：{','.join(sorted(unknown))}")
        return cls(
            agent_run_id=value.get("agent_run_id", ""),
            child_session_id=value.get("child_session_id", ""),
            role=value.get("role", ""),
            input_hash=value.get("input_hash", ""),
            output_hash=value.get("output_hash", ""),
            status=value.get("status", "completed"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "agent_run_id": self.agent_run_id,
            "child_session_id": self.child_session_id,
            "role": self.role,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "status": self.status,
        }


@dataclass(frozen=True)
class AgentStepResult(Mapping[str, Any]):
    """Validated result envelope shared by App and external Skill hosts."""

    receipt: AgentRunReceipt
    result: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, AgentRunReceipt):
            raise ValueError("AgentStepResult.receipt必须是AgentRunReceipt")
        if not isinstance(self.result, Mapping):
            raise ValueError("AgentStepResult.result必须是对象")
        object.__setattr__(self, "result", dict(self.result))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AgentStepResult":
        if not isinstance(value, Mapping) or set(value) != {"receipt", "result"}:
            raise ValueError("Agent步骤必须返回receipt和result")
        return cls(
            receipt=AgentRunReceipt.from_mapping(value["receipt"]),
            result=value["result"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {"receipt": self.receipt.to_dict(), "result": dict(self.result)}

    def __getitem__(self, key: str) -> Any:
        return self.result[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.result)

    def __len__(self) -> int:
        return len(self.result)


def completed_agent_step(
    role: str,
    payload: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    agent_run_id: str,
    child_session_id: str,
) -> AgentStepResult:
    """Build the sole successful AgentPort envelope from settled host facts."""

    def digest(value: Mapping[str, Any]) -> str:
        body = json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        ).encode("utf-8")
        return hashlib.sha256(body).hexdigest()

    return AgentStepResult(
        receipt=AgentRunReceipt(
            agent_run_id=agent_run_id,
            child_session_id=child_session_id,
            role=str(role),
            input_hash=digest(payload),
            output_hash=digest(result),
        ),
        result=result,
    )


class AgentPort(Protocol):
    def capability(self) -> ModelCapability: ...

    def run_step(self, role: str, payload: Mapping[str, Any]) -> AgentStepResult: ...

    def run_named_steps(self, requests: Mapping[str, Mapping[str, Any]]) -> Mapping[str, AgentStepResult]: ...


@dataclass(frozen=True)
class HostAgentPort:
    """Provider-neutral adapter for a Skill host's native child sessions."""

    capability_provider: Callable[[], ModelCapability | Mapping[str, Any]]
    step_runner: Callable[[str, Mapping[str, Any]], AgentStepResult | Mapping[str, Any]]
    named_step_runner: Callable[[Mapping[str, Mapping[str, Any]]], Mapping[str, AgentStepResult | Mapping[str, Any]]] | None = None

    def capability(self) -> ModelCapability:
        value = self.capability_provider()
        return value if isinstance(value, ModelCapability) else ModelCapability.from_mapping(value)

    def run_step(self, role: str, payload: Mapping[str, Any]) -> AgentStepResult:
        value = self.step_runner(str(role), dict(payload))
        return value if isinstance(value, AgentStepResult) else AgentStepResult.from_mapping(value)

    def run_named_steps(self, requests: Mapping[str, Mapping[str, Any]]) -> Mapping[str, AgentStepResult]:
        normalized = {str(role): dict(payload) for role, payload in requests.items()}
        if self.named_step_runner is None:
            return {role: self.run_step(role, payload) for role, payload in normalized.items()}
        values = self.named_step_runner(normalized)
        if not isinstance(values, Mapping) or set(values) != set(normalized):
            raise ValueError("宿主具名Agent返回角色集合无效")
        return {
            role: value if isinstance(value, AgentStepResult) else AgentStepResult.from_mapping(value)
            for role, value in values.items()
        }


class KnowledgePort(Protocol):
    def search(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


class ToolPort(Protocol):
    def call(self, module: str, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


class CandidateEvaluationPort(ToolPort, Protocol):
    """Host-only preselection port required by calculation-aware Modes."""

    def evaluate_candidate(
        self,
        *,
        candidate: Mapping[str, Any],
        confirmed_constraints: Mapping[str, Any],
        modules: Sequence[str],
        term_overrides: Mapping[str, Any],
        candidate_version_id: str,
        round_no: int,
        input_fingerprints: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class HttpEndpoint:
    base_url: str
    timeout_seconds: float = 20.0
    max_response_bytes: int = 4_000_000

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PortError("正式端点必须是http或https URL")
        if parsed.username or parsed.password:
            raise PortError("正式端点URL不得内嵌凭据")
        if self.timeout_seconds <= 0:
            raise PortError("端口超时必须为正数")

    def post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = json.dumps(dict(payload), ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = Request(
            urljoin(self.base_url.rstrip("/") + "/", path.lstrip("/")),
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise PortError("正式端点响应超过大小限制")
        except HTTPError as error:
            detail = error.read(4096).decode("utf-8", errors="replace")
            raise PortError(f"正式端点HTTP {error.code}：{detail}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise PortError(f"正式端点连接失败：{error}") from error
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PortError("正式端点必须返回UTF-8 JSON对象") from error
        if not isinstance(value, Mapping):
            raise PortError("正式端点必须返回JSON对象")
        return dict(value)


@dataclass(frozen=True)
class HttpAgentPort:
    endpoint: HttpEndpoint
    capability_path: str = "/model/capability"
    step_path: str = "/agent/step"

    def capability(self) -> ModelCapability:
        response = self.endpoint.post(self.capability_path, {"capability": "recommender"})
        if response.get("ok") is False:
            raise PortError(str(response.get("message") or response.get("error") or "模型能力请求失败"))
        payload = response.get("capability", response)
        if not isinstance(payload, Mapping):
            raise PortError("模型能力响应缺少capability对象")
        return ModelCapability.from_mapping(payload)

    def run_step(self, role: str, payload: Mapping[str, Any]) -> AgentStepResult:
        response = self.endpoint.post(self.step_path, {"role": role, "payload": dict(payload)})
        if response.get("ok") is False:
            raise PortError(str(response.get("message") or response.get("error") or "Agent步骤失败"))
        try:
            return AgentStepResult.from_mapping(response.get("step", response))
        except (TypeError, ValueError) as error:
            raise PortError(f"Agent角色{role}缺少有效独立运行凭证") from error

    def run_named_steps(self, requests: Mapping[str, Mapping[str, Any]]) -> Mapping[str, AgentStepResult]:
        """Portable heterogeneous-role fallback.

        HTTP hosts without an explicit heterogeneous batch endpoint still get
        one request per role.  The App implementation may run independent
        children concurrently; this adapter deliberately preserves role and
        result identity rather than pretending one request is a council.
        """

        return {str(role): self.run_step(str(role), payload) for role, payload in requests.items()}


@dataclass(frozen=True)
class HttpKnowledgePort:
    endpoint: HttpEndpoint
    search_path: str = "/knowledger/search"

    def search(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        response = self.endpoint.post(self.search_path, payload)
        if response.get("ok") is False:
            raise PortError(str(response.get("message") or response.get("error") or "Knowledger检索失败"))
        return response


@dataclass(frozen=True)
class HttpToolPort:
    """App HTTP适配器；Recommender业务流程只依赖ToolPort。"""

    endpoint: HttpEndpoint

    def call(self, module: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        module_name = str(module).strip().lower()
        if not module_name or not all(character.isalnum() or character in {"_", "-"} for character in module_name):
            raise PortError("模块名无效")
        response = self.endpoint.post(f"/api/tools/{quote(module_name, safe='')}", dict(payload))
        if response.get("ok") is False:
            raise PortError(str(response.get("message") or response.get("error") or f"{module_name}调用失败"))
        result = response.get("result", response)
        if not isinstance(result, Mapping):
            raise PortError(f"模块{module_name}必须返回JSON对象")
        return dict(result)
