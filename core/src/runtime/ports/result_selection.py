"""Reporter页面的受控结果选择端口。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, cast, runtime_checkable


@runtime_checkable
class ResultSelectionPort(Protocol):
    """Host注入当前租户的脱敏报告来源，不接受目录扫描或物理路径。"""

    def list_report_sources(
        self,
        *,
        tenant_id: str,
        task_id: str | None = None,
        query: str | None = None,
    ) -> Mapping[str, Any]: ...

    def get_report_source(self, *, tenant_id: str, source_id: str) -> Mapping[str, Any]: ...


def require_result_selection_port(value: object) -> ResultSelectionPort:
    """供本机或App Host在注入前检查最小公开端口。"""

    if not isinstance(value, ResultSelectionPort):
        raise TypeError("ResultSelectionPort必须提供list_report_sources和get_report_source")
    return cast(ResultSelectionPort, value)


__all__ = ("ResultSelectionPort", "require_result_selection_port")
