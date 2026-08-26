"""Structured failures returned by the App platform.

The platform deliberately distinguishes an unavailable external dependency from
an authorization failure.  A caller must never receive a fabricated successful
model, data, or financial-module response.
"""


class UnavailableCapabilityError(RuntimeError):
    """Raised when a source-stage App integration has no approved implementation."""

    def __init__(
        self,
        capability: str,
        next_step: str,
        *,
        failure_code: str = "capability_unavailable",
        stage: str = "capability",
        message: str | None = None,
    ) -> None:
        self.capability = capability
        self.next_step = next_step
        self.failure_code = failure_code
        self.stage = stage
        self.message = message
        super().__init__(f"{capability} is unavailable: {next_step}")


class AuthorizationError(PermissionError):
    """Raised when a server-side capability decision rejects a request."""

    def __init__(self, capability: str, reason: str) -> None:
        self.capability = capability
        self.reason = reason
        super().__init__(f"Access denied for {capability}: {reason}")


class ValidationError(ValueError):
    """Raised when an App boundary receives an invalid non-financial payload."""


class UserActionError(ValidationError):
    """A preflight failure whose public explanation has been approved by the App Host.

    Most validation failures intentionally stay generic at the HTTP boundary so
    paths, credentials and internal implementation details cannot be reflected
    into a module page.  This exception is reserved for fixed, user-actionable
    preconditions constructed by the Host itself.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str = "input",
        next_step: str | None = None,
    ) -> None:
        if not code or not message:
            raise ValueError("UserActionError requires a public code and message")
        self.code = code
        self.message = message
        self.stage = stage
        self.next_step = next_step or "请按提示调整当前任务的输入或数据后重试。"
        super().__init__(message)


class CapabilityIntegrityError(RuntimeError):
    """Raised when the embedded Capability manifest or a page hash is invalid."""
