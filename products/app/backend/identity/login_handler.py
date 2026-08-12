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
        if bool(account.strip()) != bool(password):
            raise ValidationError("account and password must be supplied together")
        return cls(account=account.strip(), password=password, remember=remember)


@dataclass(frozen=True)
class LoginOutcome:
    identity: SessionIdentity
    mode: AuthenticationMode
    remember: bool


class LoginHandler:
    """Authenticate managed accounts or issue the narrow development identity."""

    def __init__(self, *, mode: AuthenticationMode, password_store: PasswordCredentialStore) -> None:
        if mode not in {"local-development", "managed"}:
            raise ValidationError("Unsupported App authentication mode")
        self.mode = mode
        self._password_store = password_store
        self._local_provider = LocalAuthProvider()

    def authenticate(self, request: LoginRequest, *, client_host: str) -> LoginOutcome:
        if self.mode == "local-development" and not request.account and not request.password:
            self.require_local_peer(client_host)
            identity = self._local_provider.authenticate(
                LocalAuthenticationRequest(role=Role.ADMIN, principal_label="User1"),
            )
            return LoginOutcome(identity=identity, mode=self.mode, remember=request.remember)

        if not self._password_store.has_accounts():
            raise UnavailableCapabilityError(
                "account.login",
                "账号服务尚未配置；请等待管理员完成账号与口令服务接入。",
            )
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
        return LoginOutcome(identity=identity, mode="managed", remember=request.remember)

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
