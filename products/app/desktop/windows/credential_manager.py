"""Windows Credential Manager provider boundary."""

from backend.errors import UnavailableCapabilityError
from backend.secrets.secret_ref import SecretRef


class WindowsCredentialManager:
    def resolve(self, secret_ref: SecretRef) -> str:
        raise UnavailableCapabilityError(
            "Windows Credential Manager",
            f"configure Credential Manager access for SecretRef {secret_ref.provider}:{secret_ref.key}",
        )
