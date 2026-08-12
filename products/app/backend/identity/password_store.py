"""App-private password verifier storage for the future managed account service.

The store is deliberately not exposed through HTTP.  It persists only salted
PBKDF2 verifiers and account claims; account provisioning remains an external
administrative responsibility until the managed account service is available.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from ..authorization.roles import Role
from ..errors import ValidationError


_SCHEME = "pbkdf2-sha256"
_ITERATIONS = 310_000
_DUMMY_SALT = b"OptionHelper-password-store-dummy"


@dataclass(frozen=True)
class PasswordAccount:
    account: str
    principal_id: str
    tenant_id: str
    role: Role


class PasswordCredentialStore:
    """Atomic file store containing no plaintext passwords or session tokens."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().resolve()
        self._lock = RLock()

    def has_accounts(self) -> bool:
        with self._lock:
            return bool(self._read().get("accounts"))

    def provision(
        self,
        account: str,
        password: str,
        role: Role,
        *,
        tenant_id: str,
        principal_id: str | None = None,
    ) -> PasswordAccount:
        """Provision one verifier for a future trusted administrative caller.

        No App route invokes this method.  Keeping provisioning outside the
        browser prevents the unfinished account system from becoming a hidden
        self-registration endpoint.
        """

        canonical = _canonical_account(account)
        secret = _validated_password(password)
        tenant = tenant_id.strip()
        if not tenant or len(tenant) > 128:
            raise ValidationError("tenant_id must contain 1 to 128 visible characters")
        principal = (principal_id or f"account:{canonical}").strip()
        if not principal or len(principal) > 160:
            raise ValidationError("principal_id must contain 1 to 160 visible characters")
        salt = secrets.token_bytes(32)
        verifier = _derive(secret, salt, _ITERATIONS)
        record = {
            "account": canonical,
            "principal_id": principal,
            "tenant_id": tenant,
            "role": role.value,
            "scheme": _SCHEME,
            "iterations": _ITERATIONS,
            "salt": base64.b64encode(salt).decode("ascii"),
            "verifier": base64.b64encode(verifier).decode("ascii"),
        }
        with self._lock:
            value = self._read()
            accounts = value.setdefault("accounts", {})
            if not isinstance(accounts, dict):
                raise ValidationError("Password account store is invalid")
            accounts[canonical] = record
            self._write(value)
        return PasswordAccount(canonical, principal, tenant, role)

    def authenticate(self, account: str, password: str) -> PasswordAccount | None:
        canonical = _canonical_account(account)
        secret = _validated_password(password)
        with self._lock:
            raw = self._read().get("accounts", {}).get(canonical)
        if not isinstance(raw, dict):
            _derive(secret, _DUMMY_SALT, _ITERATIONS)
            return None
        try:
            if raw.get("scheme") != _SCHEME:
                raise ValueError("unsupported password verifier")
            iterations = int(raw["iterations"])
            if iterations < _ITERATIONS:
                raise ValueError("weak password verifier")
            salt = base64.b64decode(str(raw["salt"]), validate=True)
            expected = base64.b64decode(str(raw["verifier"]), validate=True)
            role = Role(str(raw["role"]))
            principal_id = str(raw["principal_id"])
            tenant_id = str(raw["tenant_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValidationError("Password account store is invalid") from error
        actual = _derive(secret, salt, iterations)
        if not hmac.compare_digest(actual, expected):
            return None
        return PasswordAccount(canonical, principal_id, tenant_id, role)

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"version": 1, "accounts": {}}
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValidationError("Password account store is invalid") from error
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("accounts"), dict):
            raise ValidationError("Password account store is invalid")
        return value

    def _write(self, value: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".passwords-", suffix=".tmp", dir=self._path.parent)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self._path)
            if os.name != "nt":
                os.chmod(self._path, 0o600)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)


def _canonical_account(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("account must be a string")
    account = value.strip().casefold()
    if not account or len(account) > 128 or any(character.isspace() for character in account):
        raise ValidationError("account must contain 1 to 128 non-space characters")
    return account


def _validated_password(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValidationError("password must be a string")
    encoded = value.encode("utf-8")
    if not encoded or len(encoded) > 4096:
        raise ValidationError("password is outside the supported length")
    return encoded


def _derive(password: bytes, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password, salt, iterations, dklen=32)
