"""Host-routed provider connectivity tests with no browser credential input."""

from dataclasses import dataclass

from ..datafetcher_adapter import DataFetcherAdapter
from ..errors import UnavailableCapabilityError, ValidationError
from ..identity.session_identity import SessionIdentity
from ..secrets.secret_ref import SecretRef


@dataclass(frozen=True)
class ConnectionTestResult:
    provider_name: str
    status: str
    detail: str
    reason_code: str | None = None
    provider_error_code: str | None = None
    http_status: int | None = None


class ConnectionTester:
    def __init__(self, datafetcher: DataFetcherAdapter) -> None:
        self._datafetcher = datafetcher

    def test(
        self,
        provider_name: str,
        principal: SessionIdentity,
        *,
        request_id: str = "",
        expected_secret_ref: SecretRef | None = None,
    ) -> ConnectionTestResult:
        if provider_name != "ifind":
            raise UnavailableCapabilityError(
                provider_name or "data-provider",
                "该数据接口尚未接入；当前可在设置中心测试iFinD连接。",
            )
        result = self._datafetcher.dispatch(
            {"action": "test_connection"},
            principal,
            request_id=request_id,
            expected_secret_ref=expected_secret_ref,
        )
        connection = result.get("connection") if isinstance(result, dict) else None
        if not isinstance(connection, dict):
            raise ValidationError("DataFetcher未返回连接测试结果")
        provider = "iFind"
        status = str(connection.get("status", "unavailable"))
        detail = str(connection.get("detail", "iFind连接测试未返回说明。"))
        reason_code = connection.get("reason_code")
        provider_error_code = connection.get("provider_error_code")
        http_status = connection.get("http_status")
        return ConnectionTestResult(
            provider,
            status,
            detail,
            str(reason_code) if isinstance(reason_code, str) and reason_code else None,
            str(provider_error_code) if isinstance(provider_error_code, str) and provider_error_code else None,
            http_status if isinstance(http_status, int) and not isinstance(http_status, bool) else None,
        )
