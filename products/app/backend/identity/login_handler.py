"""Account-login request validation and controlled local-development bypass."""

from __future__ import annotations

import ipaddress
import secrets
from dataclasses import dataclass
from typing import Any, Literal

from ..authorization.roles import Role
from ..errors import AuthorizationError, UnavailableCapabilityError, ValidationError
from .identity_provider import LocalAuthenticationRequest, LocalAuthProvider
from .password_store import PasswordCredentialStore
from .session_identity import SessionIdentity


AuthenticationMode = Literal["local-development", "managed"]
_MANAGED_ISSUER = "optionhelper-managed-password"
_DEVELOPMENT_ISSUER = "optionhelper-development"


@dataclass(frozen=True)
class LoginRequest:
    account: str
    password: str
    remember: bool

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "LoginRequest":
        if set(payload) != {"account", "password", "remember"}:
            raise ValidationError("login accepts only account, password and remember")
        account = payload.get("account")
        password = payload.get("password")
        remember = payload.get("remember")
        if not isinstance(account, str) or not isinstance(password, str) or not isinstance(remember, bool):
            raise ValidationError("login fields have invalid types")
        if len(account) > 128 or len(password.encode("utf-8")) > 4096:
            raise ValidationError("login fields exceed the supported length")
        if not account.strip() or not password:
            raise ValidationError("account and password are required")
        return cls(account=account.strip(), password=password, remember=remember)




@dataclass(frozen=True)
class LoginOutcome:
    identity: SessionIdentity
    mode: AuthenticationMode
    remember: bool
    issuer: str
    account_generation: str


class LoginHandler:
    """Authenticate managed accounts or issue the narrow development identity."""

    def __init__(self, *, mode: AuthenticationMode, password_store: PasswordCredentialStore) -> None:
        if mode not in {"local-development", "managed"}:
            raise ValidationError("Unsupported App authentication mode")
        self.mode = mode
        self._password_store = password_store
        self._local_provider = LocalAuthProvider()
        self._fixed_local_principal_label: str | None = None
        self._fixed_local_session_ids: set[str] = set()

    def bind_fixed_local_identity(self, principal_label: str) -> None:
        """Bind a hidden local verification run to one controlled principal."""

        if self.mode != "local-development":
            raise UnavailableCapabilityError(
                "identity.local.authenticate",
                "固定本机验收身份仅适用于本机开发认证模式。",
            )
        validated = self._local_provider.authenticate(
            LocalAuthenticationRequest(role=Role.ADMIN, principal_label=principal_label),
        )
        canonical_label = validated.principal_id.removeprefix("local:")
        if self._fixed_local_principal_label not in {None, canonical_label}:
            raise ValidationError("本机验收身份已经绑定且不可替换")
        if self._fixed_local_principal_label is None:
            self._fixed_local_principal_label = canonical_label
            self._fixed_local_session_ids.clear()

    def authenticate_local(
        self,
        role: Role,
        principal_label: str | None,
        *,
        client_host: str,
    ) -> SessionIdentity:
        """Issue a loopback-only local identity under the active Host policy."""

        if self.mode != "local-development":
            raise UnavailableCapabilityError(
                "account.login",
                "正式运行不提供本地开发身份；请使用受管账号登录。",
            )
        self.require_local_peer(client_host)
        label = principal_label
        if self._fixed_local_principal_label is not None:
            if role is not Role.ADMIN or label not in {None, self._fixed_local_principal_label}:
                raise AuthorizationError(
                    "identity.local.authenticate",
                    "当前验收运行只接受受控本机验收身份。",
                )
            label = self._fixed_local_principal_label
        identity = self._local_provider.authenticate(
            LocalAuthenticationRequest(role=role, principal_label=label),
        )
        if self._fixed_local_principal_label is not None:
            self._fixed_local_session_ids.add(identity.session_id)
        return identity

    def authenticate(self, request: LoginRequest, *, client_host: str) -> LoginOutcome:
        if self._fixed_local_principal_label is not None and (request.account or request.password):
            raise AuthorizationError(
                "account.login",
                "当前验收运行不接受受管账号覆盖验收身份。",
            )
        if self.mode == "local-development":
            raise UnavailableCapabilityError(
                "account.login",
                "开发与自动化验收仅使用显式本地身份入口。",
            )
        self._password_store.ensure_default_accounts()
        if not request.account or not request.password:
            raise AuthorizationError("account.login", "managed credentials are required")
        account = self._password_store.authenticate(request.account, request.password)
        if account is None:
            raise AuthorizationError("account.login", "account or password is invalid")
        identity = SessionIdentity(
            principal_id=account.principal_id,
            tenant_id=account.tenant_id,
            role=account.role,
            session_id=secrets.token_urlsafe(32),
        )
        return LoginOutcome(
            identity=identity,
            mode="managed",
            remember=request.remember,
            issuer=_MANAGED_ISSUER,
            account_generation=account.account_generation,
        )

    def session_binding(self) -> dict[str, str]:
        if self.mode == "managed":
            self._password_store.ensure_default_accounts()
            return {
                "authentication_mode": "managed",
                "issuer": _MANAGED_ISSUER,
                "account_generation": self._password_store.account_generation(),
            }
        return {
            "authentication_mode": "local-development",
            "issuer": _DEVELOPMENT_ISSUER,
            "account_generation": "development",
        }

    def require_session_identity(self, identity: SessionIdentity) -> SessionIdentity:
        """Reject sessions issued before a hidden verification identity was bound."""

        label = self._fixed_local_principal_label
        if label is None:
            return identity
        if (
            identity.principal_id != f"local:{label}"
            or identity.tenant_id != self._local_provider.tenant_id
            or identity.role is not Role.ADMIN
            or identity.session_id not in self._fixed_local_session_ids
        ):
            raise AuthorizationError(
                "session",
                "当前验收运行只接受受控本机验收会话。",
            )
        return identity


    def prepare_accounts(self) -> None:
        """Prepare local preset accounts without presenting an account wizard."""

        if self.mode == "managed":
            self._password_store.ensure_default_accounts()

    def require_local_peer(self, client_host: str) -> None:
        try:
            loopback = ipaddress.ip_address(client_host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise AuthorizationError("identity.local.authenticate", "local development login requires a loopback peer")

    @property
    def is_local_development(self) -> bool:
        return self.mode == "local-development"
