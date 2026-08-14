"""Module Host与五个正式页面之间的最小上下文协议。

页面只接收不透明引用和短期能力令牌，不能从上下文获得会话原值、凭据或物理
结果目录。浏览器可以做结构和到期时间检查；真正的授权必须由Host在服务端
使用同一令牌和已认证身份重新验证。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import time
from typing import Any

from .models import ModuleRunRef
from .version import (
    MODULE_HOST_PROTOCOL_ID,
    PRICING_CONFIG_SCHEMA_ID,
    RESOLVED_CONTRACT_SCHEMA_ID,
    require_release_id,
)


PAGE_MODULES = ("datafetcher", "payoffer", "pricer", "backtester", "reporter")
CAPABILITY_TOKEN_PREFIX = "oh"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(
    rf"{re.escape(CAPABILITY_TOKEN_PREFIX)}\.(?P<expires_at>[0-9]{{1,12}})\.(?P<signature>[0-9a-f]{{64}})\Z"
)
_CONTEXT_ID = re.compile(r"mhc_[A-Za-z0-9_-]{16,96}\Z")
_HOST_KINDS = frozenset({"app", "local-development"})


class ModuleHostContextError(ValueError):
    """ModuleHostContext或能力令牌不满足公开协议。"""


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModuleHostContextError(f"{field}必须为非空字符串")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ModuleHostContextError(f"{field}不得包含控制字符")
    return value


def _required_hash(value: Any, field: str) -> str:
    text = _required_text(value, field)
    if not _HASH.fullmatch(text):
        raise ModuleHostContextError(f"{field}必须为64位小写SHA-256")
    return text


@dataclass(frozen=True)
class HostObjectRef:
    """页面可见的合同或配置不透明引用，不包含任何存储路径。"""

    reference_id: str
    schema_id: str
    content_hash: str

    def __post_init__(self) -> None:
        _required_text(self.reference_id, "reference_id")
        _required_text(self.schema_id, "schema_id")
        _required_hash(self.content_hash, "content_hash")

    def to_payload(self) -> dict[str, str]:
        return {
            "reference_id": self.reference_id,
            "schema_id": self.schema_id,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_payload(cls, value: Any, field: str) -> "HostObjectRef":
        if not isinstance(value, Mapping):
            raise ModuleHostContextError(f"{field}必须为对象")
        expected = {"reference_id", "schema_id", "content_hash"}
        if set(value) != expected:
            raise ModuleHostContextError(f"{field}字段必须为{','.join(sorted(expected))}")
        return cls(
            reference_id=_required_text(value.get("reference_id"), f"{field}.reference_id"),
            schema_id=_required_text(value.get("schema_id"), f"{field}.schema_id"),
            content_hash=_required_hash(value.get("content_hash"), f"{field}.content_hash"),
        )


@dataclass(frozen=True)
class CapabilityToken:
    """可公开解析的令牌外形；signature只能由Host验证。"""

    expires_at: int
    signature: str


def parse_capability_token(token: Any, *, now: int | None = None) -> CapabilityToken:
    """校验令牌格式和时效，不把此操作视为授权。"""

    if not isinstance(token, str):
        raise ModuleHostContextError("capability_token必须为字符串")
    matched = _TOKEN.fullmatch(token)
    if matched is None:
        raise ModuleHostContextError("capability_token格式无效")
    expires_at = int(matched.group("expires_at"))
    current = int(time.time()) if now is None else now
    if not isinstance(current, int) or isinstance(current, bool):
        raise ModuleHostContextError("now必须为整数Unix时间戳")
    if expires_at <= current:
        raise ModuleHostContextError("capability_token已过期")
    return CapabilityToken(expires_at=expires_at, signature=matched.group("signature"))


def capability_token_payload(
    *,
    session_id: str,
    principal_id: str,
    session_ref: str,
    module: str,
    expires_at: int,
    context_id: str,
    page_hash: str,
    audience: str,
    host_kind: str,
    request_policy: tuple[str, ...],
    capability_version: str,
    protocol_id: str,
    task_id: str | None = None,
    analysis_case_id: str | None = None,
    candidate_id: str | None = None,
    catalog_version: str | None = None,
    contract_fingerprint: str | None = None,
    contract_ref: HostObjectRef | None = None,
    config_ref: HostObjectRef | None = None,
    result_refs: tuple[ModuleRunRef, ...] = (),
) -> bytes:
    """唯一HMAC签名载荷，覆盖页面可见的完整授权上下文。"""

    _required_text(session_id, "session_id")
    _required_text(principal_id, "principal_id")
    _required_text(session_ref, "session_ref")
    _require_page_module(module)
    _require_context_id(context_id)
    _required_hash(page_hash, "page_hash")
    _required_text(audience, "audience")
    if host_kind not in _HOST_KINDS:
        raise ModuleHostContextError("host_kind必须为app或local-development")
    if not isinstance(request_policy, tuple) or not request_policy:
        raise ModuleHostContextError("request_policy必须为非空字符串元组")
    policy = tuple(_required_text(item, "request_policy[]") for item in request_policy)
    require_release_id(capability_version, "capability_version")
    if protocol_id != MODULE_HOST_PROTOCOL_ID:
        raise ModuleHostContextError(f"protocol_id必须为{MODULE_HOST_PROTOCOL_ID}")
    task_id = _optional_scope_id(task_id, "task_id")
    analysis_case_id = _optional_scope_id(analysis_case_id, "analysis_case_id")
    candidate_id = _optional_scope_id(candidate_id, "candidate_id")
    catalog_version = _optional_scope_id(catalog_version, "catalog_version")
    if catalog_version is not None:
        require_release_id(catalog_version, "catalog_version")
    contract_fingerprint = _optional_scope_hash(contract_fingerprint, "contract_fingerprint")
    if contract_ref is not None and not isinstance(contract_ref, HostObjectRef):
        raise ModuleHostContextError("contract_ref必须为HostObjectRef")
    if config_ref is not None and not isinstance(config_ref, HostObjectRef):
        raise ModuleHostContextError("config_ref必须为HostObjectRef")
    if not isinstance(result_refs, tuple) or not all(isinstance(item, ModuleRunRef) for item in result_refs):
        raise ModuleHostContextError("result_refs必须为ModuleRunRef元组")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool) or expires_at <= 0:
        raise ModuleHostContextError("expires_at必须为正整数Unix时间戳")
    binding = {
        "analysis_case_id": analysis_case_id,
        "audience": audience,
        "candidate_id": candidate_id,
        "capability_version": capability_version,
        "catalog_version": catalog_version,
        "config_ref": config_ref.to_payload() if config_ref is not None else None,
        "context_id": context_id,
        "contract_fingerprint": contract_fingerprint,
        "contract_ref": contract_ref.to_payload() if contract_ref is not None else None,
        "expires_at": expires_at,
        "host_kind": host_kind,
        "module": module,
        "page_hash": page_hash,
        "principal_id": principal_id,
        "protocol_id": protocol_id,
        "request_policy": list(policy),
        "result_refs": [_module_run_ref_payload(ref) for ref in result_refs],
        "session_id": session_id,
        "session_ref": session_ref,
        "task_id": task_id,
    }
    return json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def issue_capability_token(
    *,
    token_secret: bytes,
    session_id: str,
    principal_id: str,
    session_ref: str,
    module: str,
    expires_at: int,
    context_id: str,
    page_hash: str,
    audience: str,
    host_kind: str,
    request_policy: tuple[str, ...],
    capability_version: str,
    protocol_id: str,
    task_id: str | None = None,
    analysis_case_id: str | None = None,
    candidate_id: str | None = None,
    catalog_version: str | None = None,
    contract_fingerprint: str | None = None,
    contract_ref: HostObjectRef | None = None,
    config_ref: HostObjectRef | None = None,
    result_refs: tuple[ModuleRunRef, ...] = (),
) -> str:
    """由Host签发短期模块范围令牌。"""

    if not isinstance(token_secret, bytes) or not token_secret:
        raise ModuleHostContextError("token_secret必须为非空bytes")
    payload = capability_token_payload(
        session_id=session_id,
        principal_id=principal_id,
        session_ref=session_ref,
        module=module,
        expires_at=expires_at,
        context_id=context_id,
        page_hash=page_hash,
        audience=audience,
        host_kind=host_kind,
        request_policy=request_policy,
        capability_version=capability_version,
        protocol_id=protocol_id,
        task_id=task_id,
        analysis_case_id=analysis_case_id,
        candidate_id=candidate_id,
        catalog_version=catalog_version,
        contract_fingerprint=contract_fingerprint,
        contract_ref=contract_ref,
        config_ref=config_ref,
        result_refs=result_refs,
    )
    signature = hmac.new(token_secret, payload, hashlib.sha256).hexdigest()
    return f"{CAPABILITY_TOKEN_PREFIX}.{expires_at}.{signature}"


def verify_capability_token(
    token: Any,
    *,
    token_secret: bytes,
    session_id: str,
    principal_id: str,
    session_ref: str,
    module: str,
    context_id: str,
    page_hash: str,
    audience: str,
    host_kind: str,
    request_policy: tuple[str, ...],
    capability_version: str,
    protocol_id: str,
    task_id: str | None = None,
    analysis_case_id: str | None = None,
    candidate_id: str | None = None,
    catalog_version: str | None = None,
    contract_fingerprint: str | None = None,
    contract_ref: HostObjectRef | None = None,
    config_ref: HostObjectRef | None = None,
    result_refs: tuple[ModuleRunRef, ...] = (),
    now: int | None = None,
) -> CapabilityToken:
    """服务端验证令牌签名、时效和模块范围。"""

    parsed = parse_capability_token(token, now=now)
    if not isinstance(token_secret, bytes) or not token_secret:
        raise ModuleHostContextError("token_secret必须为非空bytes")
    expected = hmac.new(
        token_secret,
        capability_token_payload(
            session_id=session_id,
            principal_id=principal_id,
            session_ref=session_ref,
            module=module,
            expires_at=parsed.expires_at,
            context_id=context_id,
            page_hash=page_hash,
            audience=audience,
            host_kind=host_kind,
            request_policy=request_policy,
            capability_version=capability_version,
            protocol_id=protocol_id,
            task_id=task_id,
            analysis_case_id=analysis_case_id,
            candidate_id=candidate_id,
            catalog_version=catalog_version,
            contract_fingerprint=contract_fingerprint,
            contract_ref=contract_ref,
            config_ref=config_ref,
            result_refs=result_refs,
        ),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(parsed.signature, expected):
        raise ModuleHostContextError("capability_token签名或模块范围无效")
    return parsed


@dataclass(frozen=True)
class ModuleHostContext:
    """Host注入页面的唯一上下文，不携带模块业务参数或计算结果。"""

    session_ref: str
    capability_token: str
    analysis_case_id: str | None
    task_id: str | None
    candidate_id: str | None
    catalog_version: str | None
    contract_fingerprint: str | None
    module: str
    page_hash: str
    capability_version: str
    protocol_id: str
    context_id: str
    host_kind: str
    request_policy: tuple[str, ...]
    contract_ref: HostObjectRef | None = None
    config_ref: HostObjectRef | None = None
    result_refs: tuple[ModuleRunRef, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.session_ref, "session_ref")
        if not isinstance(self.capability_token, str) or _TOKEN.fullmatch(self.capability_token) is None:
            raise ModuleHostContextError("capability_token格式无效")
        if self.analysis_case_id is not None:
            _required_text(self.analysis_case_id, "analysis_case_id")
        _optional_scope_id(self.task_id, "task_id")
        _optional_scope_id(self.candidate_id, "candidate_id")
        catalog_version = _optional_scope_id(self.catalog_version, "catalog_version")
        if catalog_version is not None:
            require_release_id(catalog_version, "catalog_version")
        _optional_scope_hash(self.contract_fingerprint, "contract_fingerprint")
        _require_page_module(self.module)
        _required_hash(self.page_hash, "page_hash")
        require_release_id(self.capability_version, "capability_version")
        if self.protocol_id != MODULE_HOST_PROTOCOL_ID:
            raise ModuleHostContextError(f"protocol_id必须为{MODULE_HOST_PROTOCOL_ID}")
        _require_context_id(self.context_id)
        if self.host_kind not in _HOST_KINDS:
            raise ModuleHostContextError("host_kind必须为app或local-development")
        if not isinstance(self.request_policy, tuple) or not self.request_policy or not all(isinstance(item, str) and item for item in self.request_policy):
            raise ModuleHostContextError("request_policy必须为非空字符串元组")
        if self.contract_ref is not None and not isinstance(self.contract_ref, HostObjectRef):
            raise ModuleHostContextError("contract_ref必须为HostObjectRef")
        if self.contract_ref is not None and self.contract_ref.schema_id != RESOLVED_CONTRACT_SCHEMA_ID:
            raise ModuleHostContextError(f"contract_ref.schema_id必须为{RESOLVED_CONTRACT_SCHEMA_ID}")
        if self.config_ref is not None and not isinstance(self.config_ref, HostObjectRef):
            raise ModuleHostContextError("config_ref必须为HostObjectRef")
        if self.config_ref is not None and self.config_ref.schema_id != PRICING_CONFIG_SCHEMA_ID:
            raise ModuleHostContextError(f"config_ref.schema_id必须为{PRICING_CONFIG_SCHEMA_ID}")
        if not isinstance(self.result_refs, tuple) or not all(isinstance(item, ModuleRunRef) for item in self.result_refs):
            raise ModuleHostContextError("result_refs必须为ModuleRunRef元组")

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_ref": self.session_ref,
            "capability_token": self.capability_token,
            "analysis_case_id": self.analysis_case_id,
            "task_id": self.task_id,
            "candidate_id": self.candidate_id,
            "catalog_version": self.catalog_version,
            "contract_fingerprint": self.contract_fingerprint,
            "module": self.module,
            "page_hash": self.page_hash,
            "capability_version": self.capability_version,
            "protocol_id": self.protocol_id,
            "context_id": self.context_id,
            "host_kind": self.host_kind,
            "request_policy": list(self.request_policy),
        }
        if self.contract_ref is not None:
            payload["contract_ref"] = self.contract_ref.to_payload()
        if self.config_ref is not None:
            payload["config_ref"] = self.config_ref.to_payload()
        if self.result_refs:
            payload["result_refs"] = [
                {
                    "module": ref.module,
                    "tenant_id": ref.tenant_id,
                    "task_id": ref.task_id,
                    "run_id": ref.run_id,
                    "expected_semantic_result_hash": ref.expected_semantic_result_hash,
                    "expected_artifact_manifest_hash": ref.expected_artifact_manifest_hash,
                }
                for ref in self.result_refs
            ]
        return payload

    @classmethod
    def from_payload(cls, value: Any, *, now: int | None = None) -> "ModuleHostContext":
        if not isinstance(value, Mapping):
            raise ModuleHostContextError("ModuleHostContext必须为对象")
        required = {
            "session_ref", "capability_token", "analysis_case_id", "task_id", "candidate_id", "catalog_version", "contract_fingerprint", "module", "page_hash",
            "capability_version", "protocol_id", "context_id", "host_kind", "request_policy",
        }
        optional = {"contract_ref", "config_ref", "result_refs"}
        unknown = set(value).difference(required | optional)
        missing = required.difference(value)
        if unknown or missing:
            raise ModuleHostContextError(f"ModuleHostContext字段无效：缺少{sorted(missing)}，未知{sorted(unknown)}")
        token = _required_text(value.get("capability_token"), "capability_token")
        parse_capability_token(token, now=now)
        analysis_case_id = value.get("analysis_case_id")
        if analysis_case_id is not None:
            analysis_case_id = _required_text(analysis_case_id, "analysis_case_id")
        raw_refs = value.get("result_refs", [])
        if not isinstance(raw_refs, list):
            raise ModuleHostContextError("result_refs必须为数组")
        raw_policy = value.get("request_policy")
        if not isinstance(raw_policy, list):
            raise ModuleHostContextError("request_policy必须为数组")
        return cls(
            session_ref=_required_text(value.get("session_ref"), "session_ref"),
            capability_token=token,
            analysis_case_id=analysis_case_id,
            task_id=_optional_scope_id(value.get("task_id"), "task_id"),
            candidate_id=_optional_scope_id(value.get("candidate_id"), "candidate_id"),
            catalog_version=_optional_scope_id(value.get("catalog_version"), "catalog_version"),
            contract_fingerprint=_optional_scope_hash(value.get("contract_fingerprint"), "contract_fingerprint"),
            module=_required_text(value.get("module"), "module"),
            page_hash=_required_hash(value.get("page_hash"), "page_hash"),
            capability_version=_required_text(value.get("capability_version"), "capability_version"),
            protocol_id=_required_text(value.get("protocol_id"), "protocol_id"),
            context_id=_required_text(value.get("context_id"), "context_id"),
            host_kind=_required_text(value.get("host_kind"), "host_kind"),
            request_policy=tuple(_required_text(item, "request_policy[]") for item in raw_policy),
            contract_ref=HostObjectRef.from_payload(value["contract_ref"], "contract_ref") if "contract_ref" in value else None,
            config_ref=HostObjectRef.from_payload(value["config_ref"], "config_ref") if "config_ref" in value else None,
            result_refs=tuple(_module_run_ref(item, f"result_refs[{index}]") for index, item in enumerate(raw_refs)),
        )


def validate_module_host_context(value: Any, *, expected_module: str | None = None, now: int | None = None) -> ModuleHostContext:
    """浏览器共享适配器可调用的结构与时效验证入口，不替代服务端授权。"""

    context = ModuleHostContext.from_payload(value, now=now)
    if expected_module is not None and context.module != expected_module:
        raise ModuleHostContextError(f"ModuleHostContext.module必须为{expected_module}")
    return context


def verify_module_host_context(
    context: ModuleHostContext,
    *,
    token_secret: bytes,
    session_id: str,
    principal_id: str,
    audience: str = "",
    now: int | None = None,
) -> ModuleHostContext:
    """Host服务端在每次页面API调用前验证已解析上下文。"""

    if not isinstance(context, ModuleHostContext):
        raise ModuleHostContextError("context必须为ModuleHostContext")
    verify_capability_token(
        context.capability_token,
        token_secret=token_secret,
        session_id=session_id,
        principal_id=principal_id,
        session_ref=context.session_ref,
        module=context.module,
        context_id=context.context_id,
        page_hash=context.page_hash,
        audience=audience,
        host_kind=context.host_kind,
        request_policy=context.request_policy,
        capability_version=context.capability_version,
        protocol_id=context.protocol_id,
        task_id=context.task_id,
        analysis_case_id=context.analysis_case_id,
        candidate_id=context.candidate_id,
        catalog_version=context.catalog_version,
        contract_fingerprint=context.contract_fingerprint,
        contract_ref=context.contract_ref,
        config_ref=context.config_ref,
        result_refs=context.result_refs,
        now=now,
    )
    return context


def require_host_bound_run_contract(result: Mapping[str, Any], context: ModuleHostContext) -> Mapping[str, Any]:
    """Verify the immutable candidate facts a calculator returns to Recommender.

    The values stay in the module response unchanged.  They must already be
    present in the Host-signed context, so a page cannot substitute another
    candidate or contract after the Host has selected one.
    """
    if not isinstance(result, Mapping):
        raise ModuleHostContextError("ModuleRun结果必须为对象")
    for field in ("candidate_id", "catalog_version", "contract_fingerprint"):
        expected = getattr(context, field)
        value = result.get(field)
        if field == "contract_fingerprint":
            value = _optional_scope_hash(value, field)
        else:
            value = _optional_scope_id(value, field)
        if value is None:
            raise ModuleHostContextError(f"ModuleRun.{field}不能为空")
        if expected is None:
            raise ModuleHostContextError(f"ModuleHostContext.{field}必须由Host预先绑定")
        if value != expected:
            raise ModuleHostContextError(f"ModuleRun.{field}与ModuleHostContext不一致")
    return result


def _module_run_ref(value: Any, field: str) -> ModuleRunRef:
    if not isinstance(value, Mapping):
        raise ModuleHostContextError(f"{field}必须为对象")
    expected = {"module", "tenant_id", "task_id", "run_id", "expected_semantic_result_hash", "expected_artifact_manifest_hash"}
    if set(value) != expected:
        raise ModuleHostContextError(f"{field}字段必须为{','.join(sorted(expected))}")
    return ModuleRunRef(
        module=_required_text(value.get("module"), f"{field}.module"),
        tenant_id=_required_text(value.get("tenant_id"), f"{field}.tenant_id"),
        task_id=_required_text(value.get("task_id"), f"{field}.task_id"),
        run_id=_required_text(value.get("run_id"), f"{field}.run_id"),
        expected_semantic_result_hash=_required_hash(value.get("expected_semantic_result_hash"), f"{field}.expected_semantic_result_hash"),
        expected_artifact_manifest_hash=_required_hash(value.get("expected_artifact_manifest_hash"), f"{field}.expected_artifact_manifest_hash"),
    )


def _module_run_ref_payload(ref: ModuleRunRef) -> dict[str, str]:
    return {
        "module": ref.module,
        "tenant_id": ref.tenant_id,
        "task_id": ref.task_id,
        "run_id": ref.run_id,
        "expected_semantic_result_hash": ref.expected_semantic_result_hash,
        "expected_artifact_manifest_hash": ref.expected_artifact_manifest_hash,
    }


def _require_page_module(module: str) -> str:
    if module not in PAGE_MODULES:
        raise ModuleHostContextError(f"module必须为{','.join(PAGE_MODULES)}之一")
    return module


def _require_context_id(value: str) -> str:
    if not isinstance(value, str) or not _CONTEXT_ID.fullmatch(value):
        raise ModuleHostContextError("context_id必须是Host签发的不透明引用")
    return value


def _optional_scope_id(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _optional_scope_hash(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _required_hash(value, field)


__all__ = (
    "CAPABILITY_TOKEN_PREFIX", "PAGE_MODULES", "CapabilityToken", "HostObjectRef",
    "ModuleHostContext", "ModuleHostContextError", "capability_token_payload",
    "issue_capability_token", "parse_capability_token", "validate_module_host_context",
    "verify_capability_token", "verify_module_host_context", "require_host_bound_run_contract",
)
