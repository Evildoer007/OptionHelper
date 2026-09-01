"""Reusable active capability probe for a configured streaming model endpoint."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
import json
from typing import Any

from .request_control import ModelRequestCancelled, ModelRequestControl


StreamInvoker = Callable[
    [Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]], ModelRequestControl],
    Iterable[Mapping[str, Any]],
]


def probe_provider_capabilities(
    stream: StreamInvoker,
    *,
    timeout_seconds: float = 8.0,
    bounded_request_control: bool,
) -> dict[str, Any]:
    """Actively verify SSE, tool continuation and cooperative cancellation.

    Reasoning is observational: a provider that emits no reasoning remains a
    valid streaming tool model. No credential, endpoint or raw model response
    is returned to callers.
    """

    if not callable(stream):
        raise TypeError("stream probe requires a callable")
    if timeout_seconds <= 0:
        raise ValueError("probe timeout must be positive")

    tool = {
        "type": "function",
        "function": {
            "name": "optionhelper_capability_probe",
            "description": "Return the supplied probe value without external side effects.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": "Call optionhelper_capability_probe exactly once with value OH_PROBE. Do not answer before the tool result.",
        },
        {"role": "user", "content": "Run the capability probe."},
    ]
    result: dict[str, Any] = {
        "streaming": False,
        "tool_calling": False,
        "tool_result_continuation": False,
        "reasoning": False,
        "usage": False,
        "cancellation": False,
        "bounded_timeout": bool(bounded_request_control),
        "verified": False,
        "errors": [],
    }

    try:
        first = list(stream(messages, [tool], ModelRequestControl(timeout_seconds)))
        result["streaming"] = _has_finish(first)
        result["reasoning"] = any(_event_type(item) == "reasoning_delta" for item in first)
        result["usage"] = any(_event_type(item) == "usage" for item in first)
        reasoning_content = "".join(
            str(item.get("delta", ""))
            for item in first
            if _event_type(item) == "reasoning_delta" and isinstance(item.get("delta"), str)
        )
        call = _tool_call(first)
        result["tool_calling"] = call is not None
        if call is not None:
            messages.extend([
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": call["arguments"]},
                    }],
                    **({"reasoning_content": reasoning_content} if reasoning_content else {}),
                },
                {"role": "tool", "tool_call_id": call["id"], "content": '{"value":"OH_PROBE"}'},
            ])
            # Keep the same tool contract available during continuation. Some
            # compatible providers require the assistant reasoning payload to
            # be replayed whenever tools remain in the request.
            second = list(stream(messages, [tool], ModelRequestControl(timeout_seconds)))
            result["tool_result_continuation"] = _has_finish(second) and any(
                _event_type(item) == "text_delta" and bool(item.get("delta")) for item in second
            )
    except Exception as error:
        result["errors"].append(_error_code(error))

    cancellation = ModelRequestControl(timeout_seconds)
    cancellation.cancel("capability_probe_cancelled")
    try:
        list(stream([{"role": "user", "content": "Cancel this probe."}], [], cancellation))
    except ModelRequestCancelled:
        result["cancellation"] = True
    except Exception as error:
        result["errors"].append(_error_code(error))
    else:
        result["errors"].append("CANCELLATION_NOT_ENFORCED")

    result["errors"] = list(dict.fromkeys(result["errors"]))
    result["verified"] = all(
        result[key]
        for key in ("streaming", "tool_calling", "tool_result_continuation", "cancellation", "bounded_timeout")
    )
    return result


def _event_type(event: Mapping[str, Any]) -> str:
    return str(event.get("type", event.get("event", ""))).replace("-", "_")


def _has_finish(events: Sequence[Mapping[str, Any]]) -> bool:
    return any(_event_type(item) in {"finish", "done"} for item in events)


def _tool_call(events: Sequence[Mapping[str, Any]]) -> dict[str, str] | None:
    calls: dict[int, dict[str, str]] = {}
    for event in events:
        if _event_type(event) != "tool_call_delta":
            continue
        raw = event.get("tool_call") if isinstance(event.get("tool_call"), Mapping) else event
        index = raw.get("index", 0)
        if isinstance(index, bool) or not isinstance(index, int):
            index = 0
        function = raw.get("function") if isinstance(raw.get("function"), Mapping) else {}
        current = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if isinstance(raw.get("id"), str):
            current["id"] = str(raw["id"])
        if isinstance(function.get("name"), str):
            current["name"] = str(function["name"])
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            current["arguments"] += arguments
        elif isinstance(event.get("delta"), str) and event.get("delta"):
            try:
                decoded = json.loads(str(event["delta"]))
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, Mapping):
                nested = decoded.get("function") if isinstance(decoded.get("function"), Mapping) else {}
                if isinstance(decoded.get("id"), str):
                    current["id"] = str(decoded["id"])
                if isinstance(nested.get("name"), str):
                    current["name"] = str(nested["name"])
                if isinstance(nested.get("arguments"), str):
                    current["arguments"] += str(nested["arguments"])
    for call in calls.values():
        if call["id"] and call["name"] == "optionhelper_capability_probe":
            return {**call, "arguments": call["arguments"] or "{}"}
    return None


def _error_code(error: Exception) -> str:
    name = type(error).__name__.upper()
    return name[:80] if name else "PROBE_ERROR"


__all__ = ["probe_provider_capabilities"]
