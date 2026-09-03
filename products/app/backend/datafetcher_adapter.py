"""App-owned caller/context adapter for the Capability DataFetcher service."""

from __future__ import annotations

import base64
from hashlib import sha256
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

_CORE_SRC = Path(__file__).resolve().parents[3] / "core" / "src"
if str(_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(_CORE_SRC))

from runtime.protocol.models import CallerContext, DataAssetRef
from runtime.ports.data_store import DataStoreReadPort

from .errors import UnavailableCapabilityError, ValidationError
from .identity.session_identity import SessionIdentity
from .secrets.secret_ref import SecretRef
from .secrets.secret_provider import SecretProvider
from .settings.settings_models import SettingsSnapshot


_RESERVED_CONTEXT_KEYS = {"app_context", "tenant_id", "principal_id", "secret_ref", "credential_ref"}
_SECRET_KEYS = {"password", "token", "access_token", "refresh_token", "api_key", "secret", "secret_value", "private_key"}
_FAILURE_PROJECTIONS = {
    "unauthorized": (
        "iFind凭据未通过验证。",
        "请在设置中心保存当前凭据并完成连接测试后重试。",
    ),
    "quota_exceeded": (
        "iFind数据额度不足。",
        "请等待额度恢复或切换具备可用额度的正式数据连接后重试。",
    ),
    "account_permission_denied": (
        "当前iFind账号已失效或不可用于数据接口。",
        "请在设置中心保存有效凭据并完成连接测试后重试。",
    ),
    "device_limit_exceeded": (
        "iFind设备或IP授权数量已达上限。",
        "请在iFind侧释放旧设备或更新授权后，再到设置中心完成连接测试。",
    ),
    "field_permission_denied": (
        "当前iFind账号无权读取所需行情字段。",
        "请调整为当前iFind账号有权限的行情字段后重试。",
    ),
    "market_permission_denied": (
        "当前iFind账号无权读取所需市场。",
        "请确认当前iFind账号已开通对应市场权限后重试。",
    ),
    "provider_input_error": (
        "数据服务拒绝了当前标的、日期或字段组合。",
        "请检查标的、日期、频率和字段组合后重试。",
    ),
    "no_data": (
        "所选标的和日期区间没有可验证行情。",
        "请确认标的和日期区间；单日无数据时需先取得覆盖此前交易日的正式交易日历。",
    ),
}


class DataFetcherAdapter:
    """Injects the App caller and DataInterfaceSettings into formal service calls."""

    def __init__(
        self,
        settings_for: Callable[[SessionIdentity], SettingsSnapshot],
        services: object,
        data_store: DataStoreReadPort | None = None,
        secret_provider: SecretProvider | None = None,
        verification_for: Callable[[SessionIdentity, SecretRef | None], bool] | None = None,
        revoke_verification: Callable[[SessionIdentity, SecretRef], None] | None = None,
        mark_temporarily_unavailable: Callable[[SessionIdentity, SecretRef], None] | None = None,
    ) -> None:
        self._settings_for = settings_for
        self._app_datafetcher_call = getattr(services, "call_app_datafetcher", None)
        self._app_datafetcher_read = getattr(services, "read_app_datafetcher_asset", None)
        self._data_store = data_store
        self._secret_provider = secret_provider
        self._verification_for = verification_for
        self._revoke_verification = revoke_verification
        self._mark_temporarily_unavailable = mark_temporarily_unavailable
        self._volatile_assets: dict[tuple[str, str, str], tuple[dict[str, Any], bytes]] = {}

    def dispatch(
        self,
        request: dict[str, Any],
        principal: SessionIdentity,
        *,
        request_id: str = "",
        trading_calendar_ref: Mapping[str, Any] | None = None,
        expected_secret_ref: SecretRef | None = None,
    ) -> dict[str, Any]:
        """Dispatch one App-owned request, optionally with Host calendar evidence.

        ``trading_calendar_ref`` is deliberately a keyword-only, in-process
        dependency.  It is never copied into the page request, so a browser
        cannot forge the calendar used to quality-gate historical prices.
        """
        if not isinstance(request, dict):
            raise ValidationError("DataFetcher request must be an object")
        _reject_untrusted_context(request)
        calendar_ref = _trading_calendar_ref(trading_calendar_ref)
        action = _action(request)
        settings = self._settings_for(principal)
        interface = settings.data_interface
        if expected_secret_ref is not None and interface.secret_ref != expected_secret_ref:
            raise ValidationError("数据凭据版本已变化，请重新发起连接测试")
        requires_ifind = _requires_ifind(request, action)
        if requires_ifind and (interface.provider_name != "ifind-http" or interface.secret_ref is None):
            raise UnavailableCapabilityError(
                "datafetcher.configuration",
                "请先在设置中心完成iFind数据服务配置。",
                failure_code="data_interface_configuration",
                stage="data",
            )
        if (
            requires_ifind
            and action != "test_connection"
            and (
                self._verification_for is None
                or not self._verification_for(principal, interface.secret_ref)
            )
        ):
            raise UnavailableCapabilityError(
                "datafetcher.verification",
                "请先在设置中心验证当前iFind数据连接。",
                failure_code="data_interface_unverified",
                stage="data",
            )
        if not callable(self._app_datafetcher_call):
            raise UnavailableCapabilityError(
                "datafetcher.app_secret_port",
                "the verified DataFetcher service cannot receive the App caller and SecretRef",
            )
        expose_configuration = action in {"status", "catalog", "list_assets"}
        secret_ref = (
            interface.secret_ref
            if self._secret_provider is not None and (requires_ifind or expose_configuration)
            else None
        )
        call_kwargs: dict[str, Any] = {}
        if calendar_ref is not None:
            call_kwargs["trading_calendar_ref"] = calendar_ref
        if calendar_ref is None:
            result = self._app_datafetcher_call(
                dict(request),
                _caller(principal, request_id),
                secret_ref,
                self._secret_port(secret_ref),
            )
        else:
            result = self._app_datafetcher_call(
                dict(request),
                _caller(principal, request_id),
                secret_ref,
                self._secret_port(secret_ref),
                **call_kwargs,
            )
        if not isinstance(result, dict):
            raise ValidationError("DataFetcher App port must return an object")
        volatile_payload = result.pop("_volatile_payload_b64", None)
        reference = result.get("data_asset_ref")
        if volatile_payload is not None:
            if not isinstance(volatile_payload, str) or not isinstance(reference, dict):
                raise ValidationError("DataFetcher返回的内存数据协议无效")
            try:
                content = base64.b64decode(volatile_payload, validate=True)
            except (ValueError, TypeError) as error:
                raise ValidationError("DataFetcher返回的内存数据无法解码") from error
            if sha256(content).hexdigest() != reference.get("content_hash"):
                raise ValidationError("DataFetcher内存数据与内容哈希不一致")
            key = (principal.tenant_id, principal.principal_id, str(reference.get("data_asset_id", "")))
            self._volatile_assets[key] = (dict(reference), content)
        failure_code = _failure_code(result)
        if requires_ifind and interface.secret_ref is not None:
            if (
                failure_code in {"unauthorized", "account_permission_denied"}
                and self._revoke_verification is not None
            ):
                self._revoke_verification(principal, interface.secret_ref)
            elif failure_code == "device_limit_exceeded" and self._mark_temporarily_unavailable is not None:
                self._mark_temporarily_unavailable(principal, interface.secret_ref)
        result = _project_datafetcher_failure(result)
        if not expose_configuration:
            return result
        verified = bool(
            interface.secret_ref is not None
            and self._verification_for is not None
            and self._verification_for(principal, interface.secret_ref)
        )
        public_status = {
            "configured": interface.secret_ref is not None,
            "state": "verified" if verified else "configured_unverified" if interface.secret_ref else "not_configured",
            "source": "app_host_secret_ref" if interface.secret_ref else "none",
            "remote_provider": "available" if verified else "not_checked" if interface.secret_ref else "not_configured",
        }
        return {**result, "credential_status": public_status}

    def requires_ifind(self, request: Mapping[str, Any]) -> bool:
        """Expose the adapter's single provider decision to its App Host owner."""

        return _requires_ifind(request, _action(dict(request)))
    def _secret_port(self, expected_ref: SecretRef | None) -> Callable[[SecretRef], str] | None:
        if expected_ref is None or self._secret_provider is None:
            return None

        def resolve(reference: SecretRef) -> str:
            if reference != expected_ref:
                raise ValidationError("DataFetcher只能解析当前设置中心批准的SecretRef")
            return self._secret_provider.resolve(reference, "iFind数据凭据")

        return resolve

    def read_asset(
        self,
        data_asset_id: str,
        principal: SessionIdentity,
        *,
        request_id: str = "",
    ) -> tuple[dict[str, Any], bytes]:
        if not isinstance(data_asset_id, str) or not data_asset_id:
            raise ValidationError("DataAsset id is required")
        volatile = self._volatile_assets.get((principal.tenant_id, principal.principal_id, data_asset_id))
        if volatile is not None:
            reference, content = volatile
            return dict(reference), bytes(content)
        if not callable(self._app_datafetcher_read):
            raise UnavailableCapabilityError(
                "datafetcher.download_port",
                "the verified DataFetcher service cannot read an indexed asset",
            )
        reference, content = self._app_datafetcher_read(data_asset_id, _caller(principal, request_id))
        if not isinstance(reference, dict) or not isinstance(content, bytes):
            raise ValidationError("DataFetcher download must return DataAssetRef and bytes")
        try:
            frozen_ref = DataAssetRef(**reference)
        except (TypeError, ValueError) as error:
            raise ValidationError("DataFetcher download returned an invalid DataAssetRef") from error
        if sha256(content).hexdigest() != frozen_ref.content_hash:
            raise ValidationError("DataFetcher download bytes do not match DataAssetRef.content_hash")
        return reference, content

    def bind_data_store(
        self,
        principal: SessionIdentity,
        *,
        data_refs: tuple[Mapping[str, Any], ...],
    ) -> DataStoreReadPort:
        """Bind an owner-scoped port to the exact refs selected for one run."""
        if self._data_store is None:
            raise UnavailableCapabilityError(
                "pricing.data_store", "App Host has no configured DataStorePort",
            )
        return _BoundDataStore(principal, data_refs, self._data_store, self._read_volatile_bytes)

    def _read_volatile_bytes(self, principal: SessionIdentity, ref: DataAssetRef) -> bytes | None:
        value = self._volatile_assets.get((principal.tenant_id, principal.principal_id, ref.data_asset_id))
        if value is None:
            return None
        reference, content = value
        if reference.get("content_hash") != ref.content_hash:
            raise ValidationError("DataFetcher内存数据引用与内容哈希不一致")
        return bytes(content)


def _project_datafetcher_failure(result: Mapping[str, Any]) -> dict[str, Any]:
    """Attach the App's fixed actionable fields to reviewed DataFetcher codes."""

    if result.get("ok") is not False:
        return dict(result)
    nested = result.get("error")
    nested = nested if isinstance(nested, Mapping) else {}
    code = str(nested.get("code", result.get("status", ""))).strip()
    projection = _FAILURE_PROJECTIONS.get(code)
    if projection is None:
        return {
            "ok": False,
            "module": "datafetcher",
            "status": "failed",
            "error": {"code": "unclassified_data_failure"},
        }
    message, next_step = projection
    error: dict[str, Any] = {
        "code": code,
        "failure_code": code,
        "stage": "data",
        "message": message,
        "next_step": next_step,
        "retryable": False,
    }
    for field in ("reason_code", "provider_error_code", "http_status"):
        candidate = nested.get(field)
        if isinstance(candidate, str) and candidate and field != "http_status":
            error[field] = candidate
        elif field == "http_status" and isinstance(candidate, int) and not isinstance(candidate, bool):
            error[field] = candidate
    return {"ok": False, "module": "datafetcher", "status": "failed", "error": error}


def _failure_code(result: Mapping[str, Any]) -> str:
    nested = result.get("error")
    nested = nested if isinstance(nested, Mapping) else {}
    return str(nested.get("code", result.get("status", ""))).strip()


def _datafetcher_failure_fields(result: Mapping[str, Any]) -> dict[str, Any] | None:
    projected = _project_datafetcher_failure(result)
    error = projected.get("error")
    return dict(error) if isinstance(error, Mapping) and "failure_code" in error else None


class _BoundDataStore:
    """Host-private adapter; browser input can never construct this object."""

    def __init__(
        self,
        principal: SessionIdentity,
        data_refs: tuple[Mapping[str, Any], ...],
        data_store: DataStoreReadPort,
        volatile_reader: Callable[[SessionIdentity, DataAssetRef], bytes | None],
    ) -> None:
        self._principal = principal
        self._allowed_refs = {
            (str(ref.get("data_asset_id")), str(ref.get("content_hash")))
            for ref in data_refs
            if isinstance(ref, Mapping)
        }
        if not self._allowed_refs:
            raise ValidationError("计算DataStore必须绑定本次运行已验证的DataAssetRef")
        self._data_store = data_store
        self._volatile_reader = volatile_reader

    def read_bytes(self, ref: DataAssetRef, *, tenant_id: str) -> bytes:
        if not isinstance(ref, DataAssetRef):
            raise ValidationError("Pricer/Backtester DataStore requires a Core DataAssetRef")
        if tenant_id != self._principal.tenant_id or ref.tenant_id != tenant_id:
            raise PermissionError("DataAssetRef tenant does not match the App caller")
        if ref.created_by != self._principal.principal_id:
            raise PermissionError("DataAssetRef owner does not match the App caller")
        if (ref.data_asset_id, ref.content_hash) not in self._allowed_refs:
            raise PermissionError("DataAssetRef is not selected for this run")
        if "read" not in ref.access_scope:
            raise PermissionError("DataAssetRef does not grant read access")
        content = self._volatile_reader(self._principal, ref)
        if content is None:
            content = self._data_store.read_bytes(ref, tenant_id=tenant_id)
        actual_hash = sha256(content).hexdigest()
        if actual_hash != ref.content_hash:
            raise ValidationError("DataAssetRef.content_hash does not match the actual bytes")
        return content
def _caller(principal: SessionIdentity, request_id: str) -> CallerContext:
    return CallerContext(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        role=principal.role.value,
        capabilities=("data:read", "data:force_refresh"),
        session_id=principal.session_id,
        audience=principal.audience,
        request_id=request_id,
    )


def _trading_calendar_ref(value: Mapping[str, Any] | None) -> DataAssetRef | None:
    """Validate the Host-only calendar evidence passed to DataFetcher."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError("交易日历必须由App Host以DataAssetRef注入")
    try:
        reference = DataAssetRef(**dict(value))
    except (TypeError, ValueError) as error:
        raise ValidationError("交易日历DataAssetRef无效") from error
    if reference.schema_id != "trading-calendar":
        raise ValidationError("交易日历DataAssetRef.schema_id必须为trading-calendar")
    return reference


def _action(request: dict[str, Any]) -> str:
    value = request.get("action", "fetch")
    if not isinstance(value, str):
        raise ValidationError("DataFetcher action must be a string")
    return value.strip().lower()


def _requires_ifind(request: Mapping[str, Any], action: str) -> bool:
    """Apply the Host credential gate only when the effective request can call iFind."""

    if action in {"fetch_calendar", "test_connection"}:
        return True
    if action != "fetch":
        return False
    source = request.get("request", request.get("data_request", request))
    if not isinstance(source, Mapping):
        return True

    raw_priority = source.get("source_priority")
    if isinstance(raw_priority, str):
        providers = [item.strip().lower() for item in raw_priority.split(",") if item.strip()]
    elif isinstance(raw_priority, (list, tuple)):
        providers = [str(item).strip().lower() for item in raw_priority if str(item).strip()]
    elif raw_priority is None:
        providers = []
    else:
        return True

    provider = source.get("provider")
    if isinstance(provider, str) and provider.strip():
        normalized = provider.strip().lower()
        if normalized not in providers:
            providers.insert(0, normalized)
    elif provider is not None:
        return True
    if not providers:
        return True
    return any(provider in {"ifind_http", "ifind_sdk"} for provider in providers)


def _reject_untrusted_context(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.lower()
            if normalized in _RESERVED_CONTEXT_KEYS:
                raise ValidationError(f"{key} is App-owned and cannot be supplied by a module request")
            if normalized in _SECRET_KEYS:
                raise ValidationError(f"{key} plaintext is forbidden; DataFetcher accepts SecretRef only")
            _reject_untrusted_context(item)
    elif isinstance(value, list):
        for item in value:
            _reject_untrusted_context(item)
