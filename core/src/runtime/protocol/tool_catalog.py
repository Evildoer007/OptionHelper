"""OptionHelper七个内部能力模块的唯一Tool目录。"""

from __future__ import annotations

from .version import MODULE_HOST_PROTOCOL_ID


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

_POLICIES = {
    "catalog": "module.catalog",
    "status": "module.catalog",
    "fetch": "module.run",
    "recommend": "conversation.tool.run",
    "recommend_fixed": "conversation.tool.run",
    "run": "module.run",
    "render": "module.run",
}


def tool_catalog() -> dict[str, dict[str, object]]:
    catalog: dict[str, dict[str, object]] = {}
    for module in MODULES:
        operations, has_page = _OPERATIONS[module]
        declared_actions = ("catalog", "status", *operations)
        if module == "datafetcher":
            declared_actions += ("list_assets",)
        if module == "reporter":
            declared_actions += ("list_report_sources",)
        actions = [{"action": action, "required_policy": _POLICIES.get(action, "module.catalog")} for action in declared_actions]
        catalog[module] = {
            "module": module,
            "operation": operations[0],
            "operations": list(operations),
            "entrypoint": f"modules.{module}.service:call_tool",
            "has_page": has_page,
            "independent": True,
            "actions": actions,
            "input_schema": f"optionhelper://schemas/tool-io#/$defs/{module}_input",
            "output_schema": f"optionhelper://schemas/tool-io#/$defs/{module}_output",
            "protocol_id": MODULE_HOST_PROTOCOL_ID,
        }
    return catalog


def get_tool(name: str) -> dict[str, object]:
    try:
        return dict(tool_catalog()[name])
    except KeyError as error:
        raise ValueError(f"未知Tool：{name}") from error


__all__ = ("MODULES", "get_tool", "tool_catalog")
