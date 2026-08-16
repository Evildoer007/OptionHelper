"""App-owned caller/context adapter for the Capability DataFetcher service."""

from __future__ import annotations

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


class DataFetcherAdapter:
    """Injects the App caller and DataInterfaceSettings into formal service calls."""

    def __init__(
        self,
        settings_for: Callable[[SessionIdentity], SettingsSnapshot],
        services: object,
        data_store: DataStoreReadPort | None = None,
        secret_provider: SecretProvider | None = None,
    ) -> None:
        self._settings_for = settings_for
        self._app_datafetcher_call = getattr(services, "call_app_datafetcher", None)
        self._app_datafetcher_read = getattr(services, "read_app_datafetcher_asset", None)
        self._data_store = data_store
        self._secret_provider = secret_provider

    def dispatch(
        self,
        request: dict[str, Any],
        principal: SessionIdentity,
        *,
        request_id: str = "",
        trading_calendar_ref: Mapping[str, Any] | None = None,
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
        requires_ifind = action in {"fetch", "fetch_calendar", "test_connection"}
        if requires_ifind and (interface.provider_name != "ifind-http" or interface.secret_ref is None):
            raise UnavailableCapabilityError("datafetcher.configuration", "请先在设置中心完成iFind数据服务配置。")
        if not callable(self._app_datafetcher_call):
            raise UnavailableCapabilityError(
                "datafetcher.app_secret_port",
                "the verified DataFetcher service cannot receive the App caller and SecretRef",
            )
        if self._secret_provider is None:
            if calendar_ref is None:
                return self._app_datafetcher_call(dict(request), _caller(principal, request_id), None)
            return self._app_datafetcher_call(
                dict(request), _caller(principal, request_id), None,
                trading_calendar_ref=calendar_ref,
            )
        secret_ref = interface.secret_ref
        if calendar_ref is None:
            return self._app_datafetcher_call(
                dict(request),
                _caller(principal, request_id),
                secret_ref,
                self._secret_port(secret_ref),
            )
        return self._app_datafetcher_call(
            dict(request),
            _caller(principal, request_id),
            secret_ref,
            self._secret_port(secret_ref),
            trading_calendar_ref=calendar_ref,
        )

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
        task_id: str,
        data_refs: tuple[Mapping[str, Any], ...],
    ) -> DataStoreReadPort:
        """Bind a tenant-scoped, read-only DataStore port for pricing modules."""
        if self._data_store is None:
            raise UnavailableCapabilityError(
                "pricing.data_store", "App Host has no configured DataStorePort",
            )
        return _BoundDataStore(principal, task_id, data_refs, self._data_store)


class _BoundDataStore:
    """Host-private adapter; browser input can never construct this object."""

    def __init__(
        self,
        principal: SessionIdentity,
        task_id: str,
        data_refs: tuple[Mapping[str, Any], ...],
        data_store: DataStoreReadPort,
    ) -> None:
        self._principal = principal
        if not isinstance(task_id, str) or not task_id:
            raise ValidationError("计算DataStore必须绑定当前任务")
        self._task_id = task_id
        self._allowed_refs = {
            (str(ref.get("data_asset_id")), str(ref.get("content_hash")))
            for ref in data_refs
            if isinstance(ref, Mapping)
        }
        if not self._allowed_refs:
            raise ValidationError("计算DataStore必须绑定当前任务已验证DataAssetRef")
        self._data_store = data_store

    def read_bytes(self, ref: DataAssetRef, *, tenant_id: str) -> bytes:
        if not isinstance(ref, DataAssetRef):
            raise ValidationError("Pricer/Backtester DataStore requires a Core DataAssetRef")
        if tenant_id != self._principal.tenant_id or ref.tenant_id != tenant_id:
            raise PermissionError("DataAssetRef tenant does not match the App caller")
        if ref.created_by != self._principal.principal_id:
            raise PermissionError("DataAssetRef owner does not match the App caller")
        if (ref.data_asset_id, ref.content_hash) not in self._allowed_refs:
            raise PermissionError("DataAssetRef is not attached to the current App task")
        if "read" not in ref.access_scope:
            raise PermissionError("DataAssetRef does not grant read access")
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
