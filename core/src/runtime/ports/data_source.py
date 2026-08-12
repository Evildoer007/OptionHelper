"""外部数据Provider端口。"""
from typing import Any, Mapping, Protocol

class DataSourceProvider(Protocol):
    def fetch(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...

__all__ = ("DataSourceProvider",)
