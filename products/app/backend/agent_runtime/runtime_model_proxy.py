"""Model adapter for the OptionHelper Agent runtime.

Only the existing ModelGateway is allowed to resolve credentials. This module
accepts a host-issued ``ModelRouteRef`` and never receives or stores secret
material.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import re
from typing import Any, Protocol

from ..errors import ValidationError
from ..identity.session_identity import SessionIdentity
from ..model_gateway.request_control import ModelRequestControl
from ..settings.settings_models import ModelSelection
from .runtime_protocol import ModelRouteIssuer, ModelRouteRef


def _safe_reasoning_text(value: object) -> str:
    text = str(value or "")[:64_000]
    text = re.sub(
        r"(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|secret|password|authorization|private[_ -]?key|base[_ -]?url)\s*[:=]\s*[^\s,;]+",
        r"\1: [已过滤]",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer [已过滤]", text, flags=re.IGNORECASE)


class ModelGatewayPort(Protocol):
    def resolve_agent_model_route(self, identity: SessionIdentity, *, explicit_selection: ModelSelection | None, preset_role_selection: ModelSelection | None) -> Any: ...

    def complete_for(self, identity: SessionIdentity, task_id: str, message: str, *, selection: ModelSelection | None = None, request_control: ModelRequestControl | None = None) -> str: ...

    def decide_for(self, identity: SessionIdentity, task_id: str, context: Mapping[str, Any], *, selection: ModelSelection | None = None, request_control: ModelRequestControl | None = None) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ModelRouteBinding:
    """Host-only binding between an opaque Runtime route and the real dispatch selection."""

    route_id: str
    audit_provider_name: str
    audit_model_name: str
    dispatch_selection: ModelSelection | None
    generation: str
    source: str


@dataclass(frozen=True)
class ModelCompletion:
    text: str
    usage: Mapping[str, int] | None = None
    finish_reason: str | None = None
    streaming: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text:
            raise ValidationError("模型返回文本不能为空")
        if len(self.text) > 512_000:
            raise ValidationError("模型返回文本超过运行时上限")
        if self.usage is not None:
            object.__setattr__(self, "usage", _usage(self.usage))
        if self.finish_reason is not None and not isinstance(self.finish_reason, str):
            raise ValidationError("模型finish_reason类型无效")

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "usage": dict(self.usage) if self.usage else None,
            "finish_reason": self.finish_reason,
            "streaming": self.streaming,
        }


@dataclass(frozen=True)
class ModelStreamDelta:
    type: str
    delta: str = ""
    usage: Mapping[str, int] | None = None
    finish_reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in {"text_delta", "reasoning_delta", "tool_call_delta", "usage", "finish"}:
            raise ValidationError("模型流事件类型未注册")
        if not isinstance(self.delta, str) or len(self.delta) > 64_000:
            raise ValidationError("模型流增量文本超过运行时上限")
        if self.usage is not None:
            object.__setattr__(self, "usage", _usage(self.usage))
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata))
        if self.type == "reasoning_delta":
            safe_text = _safe_reasoning_text(self.delta)
            metadata = dict(self.metadata)
            metadata["available"] = bool(safe_text) or metadata.get("available") is True
            metadata["chars"] = len(safe_text)
            object.__setattr__(self, "delta", safe_text)
            object.__setattr__(self, "metadata", metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "delta": self.delta,
            "usage": dict(self.usage) if self.usage else None,
            "finish_reason": self.finish_reason,
            "metadata": dict(self.metadata),
        }


def _usage(value: Mapping[str, Any]) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValidationError("模型usage必须是对象")
    result: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            result[key] = item
    return result


def _safe_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("模型结果必须是对象")
    forbidden = {
        "secret", "secrets", "password", "credential", "credentials", "apikey",
        "api_key", "access_token", "authorization", "private_key", "system_prompt",
        "hidden_reasoning", "reasoning_content",
    }
    result: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).casefold().replace("-", "_")
        if normalized in forbidden or any(marker in normalized for marker in ("secret", "password", "credential", "api_key")):
            continue
        if isinstance(item, Mapping):
            result[str(key)] = _safe_mapping(item)
        elif isinstance(item, list):
            result[str(key)] = [_safe_mapping(child) if isinstance(child, Mapping) else child for child in item[:256]]
        elif isinstance(item, (str, int, float, bool)) or item is None:
            result[str(key)] = item if not isinstance(item, str) else item[:64_000]
    return result


def _messages(value: object) -> tuple[dict[str, Any], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise ValidationError("模型messages必须是非空数组")
    allowed_roles = {"system", "user", "assistant", "tool"}
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item).difference({
            "role", "content", "tool_calls", "tool_call_id", "reasoning_content",
        }):
            raise ValidationError("模型message字段无效")
        role = item.get("role")
        content = item.get("content")
        if role not in allowed_roles or not isinstance(content, (str, list)):
            raise ValidationError("模型message必须包含有效role和content")
        if isinstance(content, str):
            if len(content) > 128_000:
                raise ValidationError("模型message超过运行时上限")
            safe_content: str | list[dict[str, Any]] = content
        else:
            safe_content = _multimodal_content(content)
            if role != "user":
                raise ValidationError("仅用户消息可包含多模态内容")
        message: dict[str, Any] = {"role": role, "content": safe_content}
        if role == "assistant" and item.get("reasoning_content") is not None:
            reasoning_content = item.get("reasoning_content")
            if not isinstance(reasoning_content, str) or len(reasoning_content) > 512_000:
                raise ValidationError("assistant.reasoning_content格式无效")
            if reasoning_content:
                message["reasoning_content"] = reasoning_content
        if role == "assistant" and item.get("tool_calls") is not None:
            calls = item.get("tool_calls")
            if not isinstance(calls, list) or any(not isinstance(call, Mapping) for call in calls):
                raise ValidationError("assistant.tool_calls格式无效")
            message["tool_calls"] = [dict(call) for call in calls]
        if role == "tool":
            call_id = item.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id.strip():
                raise ValidationError("tool message缺少tool_call_id")
            message["tool_call_id"] = call_id.strip()
        result.append(message)
    return tuple(result)


def _message_text(messages: Sequence[Mapping[str, Any]]) -> str:
    if len(messages) == 1:
        return _content_text(messages[0]["content"])
    return "\n\n".join(f"[{item['role']}]\n{_content_text(item['content'])}" for item in messages)


def _multimodal_content(value: list[Any]) -> list[dict[str, Any]]:
    if not value or len(value) > 24:
        raise ValidationError("多模态消息内容无效")
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValidationError("多模态消息块无效")
        block_type = str(item.get("type", ""))
        if block_type == "text":
            text = item.get("text")
            if not isinstance(text, str) or len(text) > 128_000:
                raise ValidationError("多模态文本块无效")
            result.append({"type": "text", "text": text})
        elif block_type == "image_url":
            image_url = item.get("image_url")
            url = image_url.get("url") if isinstance(image_url, Mapping) else None
            if not isinstance(url, str) or not re.fullmatch(r"data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+", url):
                raise ValidationError("图片消息块无效")
            if len(url) > 6 * 1024 * 1024:
                raise ValidationError("图片消息块超过模型输入上限")
            result.append({"type": "image_url", "image_url": {"url": url}})
        else:
            raise ValidationError("多模态消息块类型无效")
    return result


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, Mapping) and item.get("type") == "text" else "[图片]"
            for item in value
        )
    return ""


class RuntimeModelProxy:
    """Adapt the current ModelGateway to the neutral runtime contract."""

    def __init__(
        self,
        gateway: ModelGatewayPort,
        identity: SessionIdentity,
        task_id: str,
        *,
        issuer: ModelRouteIssuer | None = None,
    ) -> None:
        if not isinstance(identity, SessionIdentity) or not str(task_id).strip():
            raise ValidationError("RuntimeModelProxy需要有效的身份和Task")
        self._gateway = gateway
        self._identity = identity
        self._task_id = str(task_id).strip()
        self._issuer = issuer or ModelRouteIssuer()
        self._bindings: dict[str, ModelRouteBinding] = {}

    @property
    def task_id(self) -> str:
        return self._task_id

    def issue_route(
        self,
        *,
        explicit_selection: ModelSelection | None = None,
        preset_role_selection: ModelSelection | None = None,
        generation: str = "current",
    ) -> ModelRouteRef:
        resolver = getattr(self._gateway, "resolve_agent_model_route", None)
        if callable(resolver):
            resolved = resolver(
                self._identity,
                explicit_selection=explicit_selection,
                preset_role_selection=preset_role_selection,
            )
            selection = getattr(resolved, "model_selection", None)
            dispatch_marker = object()
            dispatch_selection = getattr(resolved, "dispatch_selection", dispatch_marker)
            source = str(getattr(resolved, "source", "host"))
            if selection is None and isinstance(resolved, Mapping):
                selection = ModelSelection(str(resolved.get("provider_id", "")), str(resolved.get("model_id", "")))
                dispatch_selection = resolved.get("dispatch_selection", dispatch_marker)
                source = str(resolved.get("source", source))
        else:
            selection = explicit_selection or preset_role_selection
            dispatch_marker = object()
            dispatch_selection = selection
            source = "explicit"
        if not isinstance(selection, ModelSelection):
            raise ValidationError("Host未解析出可用ModelSelection")
        if dispatch_selection is dispatch_marker:
            dispatch_selection = selection
        if dispatch_selection is not None and not isinstance(dispatch_selection, ModelSelection):
            raise ValidationError("Host返回了无效的模型调用选择")
        route = self._issuer.issue(
            selection.provider_id,
            selection.model_id,
            generation=generation,
            source=source or "host",
        )
        self._bindings[route.route_id] = ModelRouteBinding(
            route_id=route.route_id,
            audit_provider_name=selection.provider_id,
            audit_model_name=selection.model_id,
            dispatch_selection=dispatch_selection,
            generation=route.generation,
            source=route.source,
        )
        return route

    def _selection(self, route: ModelRouteRef) -> ModelSelection | None:
        if not isinstance(route, ModelRouteRef) or not self._issuer.owns(route):
            raise ValidationError("ModelRouteRef必须由当前Host实例签发")
        binding = self._bindings.get(route.route_id)
        if binding is None:
            raise ValidationError("ModelRouteRef缺少Host模型调用映射")
        return binding.dispatch_selection

    def complete(
        self,
        route: ModelRouteRef,
        messages: Sequence[Mapping[str, Any]],
        *,
        request_control: ModelRequestControl | None = None,
    ) -> ModelCompletion:
        selection = self._selection(route)
        normalized = _messages(messages)
        metadata_call = getattr(self._gateway, "complete_messages_with_metadata_for", None)
        if callable(metadata_call):
            raw = metadata_call(
                self._identity,
                self._task_id,
                list(normalized),
                selection=selection,
                request_control=request_control,
            )
        else:
            message_call = getattr(self._gateway, "complete_messages_for", None)
            if callable(message_call):
                raw = message_call(
                    self._identity, self._task_id, list(normalized),
                    selection=selection, request_control=request_control,
                )
            else:
                text = _message_text(normalized)
                metadata_text_call = getattr(self._gateway, "complete_with_metadata_for", None)
                if callable(metadata_text_call):
                    raw = metadata_text_call(
                        self._identity, self._task_id, text,
                        selection=selection, request_control=request_control,
                    )
                else:
                    raw = self._gateway.complete_for(
                        self._identity, self._task_id, text,
                        selection=selection, request_control=request_control,
                    )
        if isinstance(raw, Mapping):
            text = raw.get("text")
            usage = raw.get("usage")
            finish_reason = raw.get("finish_reason")
        else:
            text, usage, finish_reason = raw, None, None
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("ModelGateway未返回有效文本")
        return ModelCompletion(
            text=text.strip(),
            usage=_usage(usage) if isinstance(usage, Mapping) else None,
            finish_reason=str(finish_reason) if finish_reason is not None else None,
            streaming=False,
        )

    def decide(
        self,
        route: ModelRouteRef,
        context: Mapping[str, Any],
        *,
        request_control: ModelRequestControl | None = None,
    ) -> Mapping[str, Any]:
        selection = self._selection(route)
        if not isinstance(context, Mapping):
            raise ValidationError("模型决策context必须是对象")
        result = self._gateway.decide_for(
            self._identity,
            self._task_id,
            _safe_mapping(context),
            selection=selection,
            request_control=request_control,
        )
        return _safe_mapping(result)

    def stream(
        self,
        route: ModelRouteRef,
        messages: Sequence[Mapping[str, Any]],
        *,
        request_control: ModelRequestControl | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> Iterable[ModelStreamDelta]:
        selection = self._selection(route)
        normalized = _messages(messages)
        stream_call = getattr(self._gateway, "stream_messages_for", None)
        message_stream = callable(stream_call)
        if not message_stream:
            stream_call = getattr(self._gateway, "stream_for", None)
        if not callable(stream_call):
            completed = self.complete(route, normalized, request_control=request_control)
            yield ModelStreamDelta("text_delta", completed.text, completed.usage)
            yield ModelStreamDelta("finish", "", completed.usage, completed.finish_reason)
            return
        if message_stream:
            raw_stream = stream_call(
                self._identity,
                self._task_id,
                list(normalized),
                selection=selection,
                request_control=request_control,
                tools=list(tools or ()),
            )
        else:
            raw_stream = stream_call(
                self._identity,
                self._task_id,
                _message_text(normalized),
                selection=selection,
                request_control=request_control,
            )
        if raw_stream is None:
            raise ValidationError("ModelGateway.stream_for未返回流")
        for raw in raw_stream:
            yield self._normalize_delta(raw)

    def _normalize_delta(self, raw: object) -> ModelStreamDelta:
        if isinstance(raw, str):
            return ModelStreamDelta("text_delta", raw)
        if not isinstance(raw, Mapping):
            raise ValidationError("模型流事件格式无效")
        kind = str(raw.get("type", raw.get("event", "text_delta"))).replace("-", "_")
        if kind in {"text", "text_delta", "content_delta"}:
            return ModelStreamDelta("text_delta", str(raw.get("delta", raw.get("content", ""))))
        if kind in {"reasoning", "reasoning_delta"}:
            reasoning = raw.get("delta", raw.get("content", raw.get("reasoning_content", "")))
            return ModelStreamDelta("reasoning_delta", str(reasoning or ""), metadata={"available": bool(reasoning)})
        if kind in {"tool_call", "tool_call_delta"}:
            tool_call = raw.get("tool_call") if isinstance(raw.get("tool_call"), Mapping) else raw
            function = tool_call.get("function") if isinstance(tool_call.get("function"), Mapping) else {}
            metadata: dict[str, Any] = {}
            call_id = tool_call.get("id", tool_call.get("call_id"))
            if isinstance(call_id, str) and call_id:
                metadata["id"] = call_id
            index = tool_call.get("index")
            if isinstance(index, int) and not isinstance(index, bool):
                metadata["index"] = index
            name = function.get("name", tool_call.get("name"))
            if isinstance(name, str) and name:
                metadata["name"] = name
            arguments = function.get("arguments", tool_call.get("arguments"))
            if isinstance(arguments, str):
                metadata["arguments"] = arguments
            return ModelStreamDelta("tool_call_delta", "", metadata=metadata)
        if kind in {"usage", "usage_delta"}:
            return ModelStreamDelta("usage", "", usage=raw.get("usage") if isinstance(raw.get("usage"), Mapping) else {})
        if kind in {"finish", "done"}:
            return ModelStreamDelta("finish", "", raw.get("usage") if isinstance(raw.get("usage"), Mapping) else None, str(raw.get("finish_reason")) if raw.get("finish_reason") is not None else None)
        raise ValidationError("模型流事件类型未注册")

    def capabilities(self, route: ModelRouteRef) -> dict[str, Any]:
        selection = self._selection(route)
        provider = getattr(self._gateway, "capability_for", None)
        declared = provider(self._identity, selection=selection) if callable(provider) else {}
        return {
            "streaming": callable(getattr(self._gateway, "stream_for", None)),
            "structured_output": callable(getattr(self._gateway, "decide_for", None)),
            "reasoning_content": callable(getattr(self._gateway, "stream_for", None)),
            "secret_access": False,
            "input_modalities": list(declared.get("input_modalities", ["text"])) if isinstance(declared, Mapping) else ["text"],
            "tool_calling": bool(declared.get("tool_calling", False)) if isinstance(declared, Mapping) else False,
        }


__all__ = ["ModelCompletion", "ModelGatewayPort", "ModelStreamDelta", "RuntimeModelProxy"]
