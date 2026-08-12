"""OptionHelper七个内部能力模块的唯一Tool目录。"""

from __future__ import annotations


MODULES = ("datafetcher", "recommender", "payoffer", "pricer", "backtester", "reporter", "designer")

_OPERATIONS = {
    "datafetcher": (("fetch",), True),
    "recommender": (("recommend", "recommend_fixed"), False),
    "payoffer": (("run",), True),
    "pricer": (("run",), True),
    "backtester": (("run",), True),
    "reporter": (("run",), True),
    "designer": (("render",), False),
}


def tool_catalog() -> dict[str, dict[str, object]]:
    catalog: dict[str, dict[str, object]] = {}
    for module in MODULES:
        operations, has_page = _OPERATIONS[module]
        catalog[module] = {
            "module": module,
            "operation": operations[0],
            "operations": list(operations),
            "entrypoint": f"modules.{module}.service:call_tool",
            "has_page": has_page,
            "independent": True,
            "required_capability": f"{module}.run",
            "input_schema": f"optionhelper://schemas/tool-io#/$defs/{module}_input",
            "output_schema": f"optionhelper://schemas/tool-io#/$defs/{module}_output",
            "protocol_version": "v1.2",
        }
    return catalog


def get_tool(name: str) -> dict[str, object]:
    try:
        return dict(tool_catalog()[name])
    except KeyError as error:
        raise ValueError(f"未知Tool：{name}") from error


__all__ = ("MODULES", "get_tool", "tool_catalog")
