"""七个内部能力模块的稳定调用端口。"""
from typing import Any, Mapping, Protocol

class ModulePort(Protocol):
    def call_tool(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...

__all__ = ("ModulePort",)
