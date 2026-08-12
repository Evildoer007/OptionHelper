"""DataFetcher Provider共用错误与最小约定。"""

from __future__ import annotations


class ProviderError(RuntimeError):
    code = "provider_error"


class ProviderUnavailable(ProviderError):
    code = "provider_unavailable"


class ProviderUnauthorized(ProviderError):
    code = "unauthorized"


class ProviderQuotaExceeded(ProviderError):
    code = "quota_exceeded"
