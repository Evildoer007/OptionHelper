"""Host注入模块的统一内部工具调用端口。"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class ToolGatewayPort(Protocol):
    def call(self, module: str, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


def require_tool_gateway_port(value: object) -> ToolGatewayPort:
    if not isinstance(value, ToolGatewayPort):
        raise TypeError("ToolGatewayPort必须实现call(module, request)")
    return value


__all__ = ("ToolGatewayPort", "require_tool_gateway_port")
