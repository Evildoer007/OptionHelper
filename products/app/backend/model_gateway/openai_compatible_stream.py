"""Bounded SSE adapter for the App's generic compatible model endpoint."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ..errors import ValidationError
from ..secrets.secret_ref import SecretRef
from ..settings.settings_models import ModelServiceSettings
from .deepseek_provider import model_http_error, model_network_error
from .https_transport import open_verified_https
from .request_control import ModelRequestCancelled, ModelRequestControl

MAX_STREAM_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SSE_LINE_BYTES = 128 * 1024
MAX_SSE_EVENT_BYTES = 512 * 1024
MAX_TEXT_CHARS = 512 * 1024
MAX_REASONING_DELTA_CHARS = 64 * 1024
MAX_REASONING_CHARS = 512 * 1024
MAX_TOOL_CALL_CHARS = 128 * 1024


class OpenAICompatibleStreamError(ValidationError):
    """Raised when an SSE response cannot be treated as a complete stream."""


def stream_openai_compatible(
    settings: ModelServiceSettings,
    secret_ref: SecretRef,
    messages: Sequence[Mapping[str, str]],
    *,
    resolve_secret: Callable[[SecretRef], str],
    opener: Callable[..., Any] = urlopen,
    request_control: ModelRequestControl | None = None,
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield normalized events from a bounded ``stream=true`` chat request.

    A normal finish is emitted only after ``[DONE]`` or an explicit provider
    ``finish_reason`` followed by a clean end of the response.  Any transport,
    encoding, JSON, size, cancellation, or termination failure is raised and
    therefore cannot be mistaken for a successful partial completion.
    """

    if request_control is not None:
        request_control.raise_if_cancelled()
    endpoint = _completion_endpoint(settings.endpoint)
    model_name = settings.model_name.strip()
    if not model_name:
        raise ValidationError("模型名不能为空，请在设置中心明确配置。")
    try:
        token = resolve_secret(secret_ref)
    except ModelRequestCancelled:
        raise
    except Exception:
        raise ValidationError("模型服务凭据无效") from None
    if not isinstance(token, str) or not token.strip():
        raise ValidationError("模型服务凭据无效")
    try:
        request_body: dict[str, Any] = {"model": model_name, "messages": list(messages), "stream": True}
        if tools:
            request_body["tools"] = list(tools)
            request_body["tool_choice"] = "auto"
        payload = json.dumps(
            request_body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValidationError("模型请求消息格式无效") from error
    request = Request(
        endpoint,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    saw_done = False
    saw_finish_reason = False
    finish_reason: str | None = None
    text_chars = 0
    reasoning_chars = 0
    try:
        timeout = request_control.remaining_seconds() if request_control is not None else 45
        with open_verified_https(request, timeout=timeout, opener=opener) as response:
            remove_cancel_listener: Callable[[], None] = lambda: None
            response_close = getattr(response, "close", None)
            if request_control is not None and callable(response_close):
                remove_cancel_listener = request_control.add_cancel_listener(
                    lambda _reason: response_close()
                )
            try:
                if request_control is not None:
                    request_control.raise_if_cancelled()
                _raise_for_response_status(response)
                for raw_event in _iter_sse_events(response, request_control=request_control):
                    if request_control is not None:
                        request_control.raise_if_cancelled()
                    if raw_event.strip() == "[DONE]":
                        saw_done = True
                        break
                    if not raw_event.strip():
                        continue
                    try:
                        value = json.loads(raw_event)
                    except (json.JSONDecodeError, TypeError) as error:
                        raise OpenAICompatibleStreamError("模型流式响应包含无效JSON") from error
                    if not isinstance(value, Mapping):
                        raise OpenAICompatibleStreamError("模型流式响应必须是JSON对象")
                    if value.get("error") is not None:
                        raise OpenAICompatibleStreamError("模型服务返回了流式错误")
                    events, chunk_finish_reason = _chunk_events(value, secret=token)
                    if chunk_finish_reason is not None:
                        saw_finish_reason = True
                        finish_reason = chunk_finish_reason
                    for event in events:
                        if request_control is not None:
                            request_control.raise_if_cancelled()
                        event_type = event["type"]
                        if event_type == "text_delta":
                            text_chars += len(event["delta"])
                            if text_chars > MAX_TEXT_CHARS:
                                raise OpenAICompatibleStreamError("模型文本流超过允许上限")
                        elif event_type == "reasoning_delta":
                            reasoning_chars += len(event["delta"])
                            if reasoning_chars > MAX_REASONING_CHARS:
                                raise OpenAICompatibleStreamError("模型推理流超过允许上限")
                        yield event
                # A cancellation-triggered close may surface as clean EOF.
                # Cancellation remains authoritative over incomplete-stream
                # validation and can never become a successful finish event.
                if request_control is not None:
                    request_control.raise_if_cancelled()
                if not saw_done and not saw_finish_reason:
                    raise OpenAICompatibleStreamError("模型流式响应未正常结束")
                yield {"type": "finish", "finish_reason": finish_reason}
            finally:
                remove_cancel_listener()
    except ModelRequestCancelled:
        raise
    except OpenAICompatibleStreamError:
        raise
    except HTTPError as error:
        raise model_http_error(error.code) from error
    except (URLError, TimeoutError, OSError) as error:
        if request_control is not None and request_control.cancelled:
            raise ModelRequestCancelled(
                request_control.reason or "model request cancelled"
            ) from error
        raise model_network_error(timed_out=isinstance(error, TimeoutError)) from error


def _iter_sse_events(response: Any, *, request_control: ModelRequestControl | None) -> Iterator[str]:
    """Decode SSE data events while enforcing line, event, and body limits."""

    data_lines: list[str] = []
    event_bytes = 0
    response_bytes = 0
    pending_line = bytearray()

    def consume_line(line_bytes: bytes) -> str | None:
        nonlocal event_bytes
        try:
            text_line = line_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise OpenAICompatibleStreamError("模型流式响应包含无效UTF-8") from error
        if text_line.endswith("\n"):
            text_line = text_line[:-1]
            if text_line.endswith("\r"):
                text_line = text_line[:-1]
        elif text_line.endswith("\r"):
            text_line = text_line[:-1]
        if text_line == "":
            if data_lines:
                value = "\n".join(data_lines)
                data_lines.clear()
                event_bytes = 0
                return value
            return None
        if text_line.startswith(":"):
            return None
        field, separator, value = text_line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field != "data":
            return None
        event_bytes += len(value.encode("utf-8")) + 1
        if event_bytes > MAX_SSE_EVENT_BYTES:
            raise OpenAICompatibleStreamError("模型流式事件超过允许上限")
        data_lines.append(value)
        return None

    while True:
        if request_control is not None:
            request_control.raise_if_cancelled()
        try:
            line = response.readline(MAX_SSE_LINE_BYTES + 1)
        except TypeError:
            line = response.readline()
        if line in (b"", ""):
            if pending_line:
                event = consume_line(bytes(pending_line))
                pending_line.clear()
                if event is not None:
                    yield event
            break
        if isinstance(line, bytes):
            line_bytes = line
        elif isinstance(line, str):
            try:
                line_bytes = line.encode("utf-8")
            except UnicodeEncodeError as error:
                raise OpenAICompatibleStreamError("模型流式响应编码无效") from error
        else:
            raise OpenAICompatibleStreamError("模型流式响应读取格式无效")
        if len(line_bytes) > MAX_SSE_LINE_BYTES:
            raise OpenAICompatibleStreamError("模型流式响应单行超过允许上限")
        response_bytes += len(line_bytes)
        if response_bytes > MAX_STREAM_RESPONSE_BYTES:
            raise OpenAICompatibleStreamError("模型流式响应超过允许上限")
        pending_line.extend(line_bytes)
        if len(pending_line) > MAX_SSE_LINE_BYTES:
            raise OpenAICompatibleStreamError("模型流式响应单行超过允许上限")
        if line_bytes.endswith((b"\n", b"\r")):
            event = consume_line(bytes(pending_line))
            pending_line.clear()
            if event is not None:
                yield event
    if data_lines:
        yield "\n".join(data_lines)


def _chunk_events(value: Mapping[str, Any], *, secret: str) -> tuple[list[dict[str, Any]], str | None]:
    choices = value.get("choices", [])
    if not isinstance(choices, list):
        raise OpenAICompatibleStreamError("模型流式choices格式无效")
    events: list[dict[str, Any]] = []
    finish_reason: str | None = None
    if choices:
        first = choices[0]
        if not isinstance(first, Mapping):
            raise OpenAICompatibleStreamError("模型流式choice格式无效")
        delta = first.get("delta", {})
        if delta is None:
            delta = {}
        if not isinstance(delta, Mapping):
            raise OpenAICompatibleStreamError("模型流式delta格式无效")
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise OpenAICompatibleStreamError("模型文本增量格式无效")
            content = _redact_secret(content, secret)
            if len(content) > MAX_TEXT_CHARS:
                raise OpenAICompatibleStreamError("模型文本增量超过允许上限")
            if content:
                events.append({"type": "text_delta", "delta": content})
        reasoning_key = next(
            (key for key in ("reasoning_content", "reasoning") if isinstance(delta.get(key), str)),
            None,
        )
        if reasoning_key is not None:
            reasoning = _redact_secret(str(delta[reasoning_key]), secret)
            if len(reasoning) > MAX_REASONING_DELTA_CHARS:
                raise OpenAICompatibleStreamError("模型推理增量超过允许上限")
            if reasoning:
                events.append({
                    "type": "reasoning_delta",
                    "delta": reasoning,
                    "metadata": {"available": True},
                })
        tool_calls = delta.get("tool_calls")
        if tool_calls is not None:
            if not isinstance(tool_calls, list):
                raise OpenAICompatibleStreamError("模型工具调用增量格式无效")
            for tool_call in tool_calls:
                safe_call = _safe_tool_call(tool_call, secret=secret)
                serialized = json.dumps(safe_call, ensure_ascii=False, separators=(",", ":"))
                if len(serialized) > MAX_TOOL_CALL_CHARS:
                    raise OpenAICompatibleStreamError("模型工具调用增量超过允许上限")
                events.append({
                    "type": "tool_call_delta",
                    "delta": serialized,
                    "tool_call": safe_call,
                })
        raw_finish_reason = first.get("finish_reason")
        if raw_finish_reason is not None:
            if not isinstance(raw_finish_reason, str):
                raise OpenAICompatibleStreamError("模型finish_reason格式无效")
            finish_reason = raw_finish_reason
    usage = _usage(value.get("usage"))
    if usage is not None:
        events.append({"type": "usage", "usage": usage})
    return events, finish_reason


def _safe_tool_call(value: object, *, secret: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OpenAICompatibleStreamError("模型工具调用格式无效")
    result: dict[str, Any] = {}
    if isinstance(value.get("index"), int) and not isinstance(value.get("index"), bool):
        result["index"] = value["index"]
    for key in ("id", "type"):
        item = value.get(key)
        if isinstance(item, str) and len(item) <= 1024:
            result[key] = _redact_secret(item, secret)
    function = value.get("function")
    if isinstance(function, Mapping):
        safe_function: dict[str, str] = {}
        name = function.get("name")
        arguments = function.get("arguments")
        if isinstance(name, str) and len(name) <= 1024:
            safe_function["name"] = _redact_secret(name, secret)
        if isinstance(arguments, str) and len(arguments) <= MAX_TOOL_CALL_CHARS:
            safe_function["arguments"] = _redact_secret(arguments, secret)
        if safe_function:
            result["function"] = safe_function
    return result


def _usage(value: object) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise OpenAICompatibleStreamError("模型usage格式无效")
    result: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            result[key] = item
    return result or None


def _redact_secret(value: str, secret: str) -> str:
    return value.replace(secret, "[REDACTED]") if secret else value


def _raise_for_response_status(response: Any) -> None:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else None
    if isinstance(status, int) and status >= 400:
        raise model_http_error(status)


def _completion_endpoint(endpoint: str) -> str:
    value = _validate_base_url(endpoint).rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    return f"{value}/chat/completions"


def _validate_base_url(endpoint: str) -> str:
    value = endpoint.strip()
    if not value or any(ord(character) < 33 for character in value):
        raise ValidationError("模型Base URL格式无效")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.params
    ):
        raise ValidationError("模型Base URL必须是无账号、查询参数和片段的完整https地址")
    return value


__all__ = [
    "MAX_REASONING_CHARS",
    "MAX_REASONING_DELTA_CHARS",
    "MAX_SSE_EVENT_BYTES",
    "MAX_SSE_LINE_BYTES",
    "MAX_STREAM_RESPONSE_BYTES",
    "OpenAICompatibleStreamError",
    "stream_openai_compatible",
]
