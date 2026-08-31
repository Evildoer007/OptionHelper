"""DataFetcher Provider共用错误与最小约定。"""

from __future__ import annotations


class ProviderError(RuntimeError):
    code = "provider_error"

    def __init__(
        self,
        message: str,
        *,
        reason_code: str | None = None,
        provider_code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code or self.code
        self.provider_code = provider_code
        self.http_status = http_status


class ProviderUnavailable(ProviderError):
    code = "provider_unavailable"


class ProviderUnauthorized(ProviderError):
    code = "unauthorized"


class ProviderAccountPermissionDenied(ProviderUnauthorized):
    code = "account_permission_denied"


class ProviderDeviceLimitExceeded(ProviderUnauthorized):
    code = "device_limit_exceeded"


class ProviderQuotaExceeded(ProviderError):
    code = "quota_exceeded"


class ProviderNoData(ProviderError):
    code = "no_data"


class ProviderFieldPermissionDenied(ProviderError):
    code = "field_permission_denied"


class ProviderMarketPermissionDenied(ProviderError):
    code = "market_permission_denied"


class ProviderInputError(ProviderError):
    code = "provider_input_error"
