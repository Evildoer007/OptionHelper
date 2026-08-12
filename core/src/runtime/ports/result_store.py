"""ResultStore业务端口。"""
from pathlib import Path
from typing import Any, Mapping, Protocol
from runtime.protocol.models import ModuleRunRef

class ResultStorePort(Protocol):
    def commit_module_run(self, *, module: str, tenant_id: str, task_id: str, run_id: str, files: Mapping[str, bytes | str | Mapping[str, Any]]) -> ModuleRunRef: ...
    def resolve_module_run(self, ref: ModuleRunRef, *, tenant_id: str) -> Path: ...
    def read_module_run_file(self, ref: ModuleRunRef, name: str, *, tenant_id: str) -> bytes: ...

__all__ = ("ResultStorePort",)
