"""Recommender只消费的受控端口客户端。

端口的服务端实现归外部Host或App所有。本模块不读取Knowledger文件，也不导入
DataFetcher、Payoffer、Pricer、Backtester、Reporter内部代码。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

from .models import ModelCapability


class PortError(RuntimeError):
    """正式端口不可达、拒绝请求或返回非法协议。"""


class AgentPort(Protocol):
    def capability(self) -> ModelCapability: ...

    def run_step(self, role: str, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


class KnowledgePort(Protocol):
    def search(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


class ToolPort(Protocol):
    def call(self, module: str, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


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
    capability_path: str = "/v1/model/capability"
    step_path: str = "/v1/agent/step"

    def capability(self) -> ModelCapability:
        response = self.endpoint.post(self.capability_path, {"capability": "recommender"})
        if response.get("ok") is False:
            raise PortError(str(response.get("message") or response.get("error") or "模型能力请求失败"))
        payload = response.get("capability", response)
        if not isinstance(payload, Mapping):
            raise PortError("模型能力响应缺少capability对象")
        return ModelCapability.from_mapping(payload)

    def run_step(self, role: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        response = self.endpoint.post(self.step_path, {"role": role, "payload": dict(payload)})
        if response.get("ok") is False:
            raise PortError(str(response.get("message") or response.get("error") or "Agent步骤失败"))
        result = response.get("result", response)
        if not isinstance(result, Mapping):
            raise PortError(f"Agent角色{role}必须返回JSON对象")
        return dict(result)


@dataclass(frozen=True)
class HttpKnowledgePort:
    endpoint: HttpEndpoint
    search_path: str = "/v1/knowledger/search"

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
