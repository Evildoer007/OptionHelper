"""App-private password verifier storage for the managed local App.

Only salted PBKDF2 verifiers and account claims are persisted.  The narrow
first-account flow may create exactly one local administrator; subsequent
account administration remains outside the browser-facing App API.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from ..authorization.roles import Role
from ..errors import UserActionError, ValidationError


_SCHEME = "pbkdf2-sha256"
_ITERATIONS = 310_000
_DUMMY_SALT = b"OptionHelper-password-store-dummy"
_STORE_SCHEMA = "optionhelper.password-account-store"
_LEGACY_STORE_VERSION = 1
_MAX_STORE_BYTES = 4 * 1024 * 1024
_ACCOUNT_FIELDS = frozenset({
    "account",
    "principal_id",
    "tenant_id",
    "role",
    "scheme",
    "iterations",
    "salt",
    "verifier",
})


class PasswordStoreRecoveryError(UserActionError):
    """Fixed public failure for an unreadable or invalid local account store."""

    def __init__(self) -> None:
        super().__init__(
            "account_store_unreadable",
            "本机账号存储无法读取，需要恢复后才能登录。",
            stage="authentication",
            next_step="请从可信备份恢复本机账号存储，或联系管理员处理。",
            retryable=False,
        )


@dataclass(frozen=True)
class PasswordAccount:
    account: str
    principal_id: str
    tenant_id: str
    role: Role
    account_generation: str


class PasswordCredentialStore:
    """Atomic file store containing no plaintext passwords or session tokens."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().absolute()
        self._lock = RLock()

    def has_accounts(self) -> bool:
        with self._lock, _exclusive_store_lock(self._path):
            return bool(self._read().get("accounts"))

    def account_generation(self) -> str:
        """Return the verifier-bound generation used to invalidate old sessions."""

        with self._lock, _exclusive_store_lock(self._path):
            value = self._read()
            return self._generation_for(value)

    def provision(
        self,
        account: str,
        password: str,
        role: Role,
        *,
        tenant_id: str,
        principal_id: str | None = None,
    ) -> PasswordAccount:
        """Provision one verifier for a trusted account-management caller."""

        canonical = _canonical_account(account)
        secret = _validated_password(password)
        tenant = _validated_claim(tenant_id, "tenant_id", 128)
        principal = _validated_claim(principal_id or f"account:{canonical}", "principal_id", 160)
        record = _account_record(canonical, secret, role, tenant, principal)
        with self._lock, _exclusive_store_lock(self._path):
            value = self._read()
            accounts = value.setdefault("accounts", {})
            accounts[canonical] = record
            self._write(value)
            generation = self._generation_for(value)
        return PasswordAccount(canonical, principal, tenant, role, generation)

    def provision_initial_administrator(self, account: str, password: str) -> PasswordAccount:
        """Create the single first local administrator without accepting claims.

        This method is deliberately separate from :meth:`provision`: it is the
        only account-writing path exposed by the App Host and it refuses every
        request once any account exists.  Role, tenant and principal claims are
        fixed by the Host rather than supplied by a browser client.
        """

        canonical = _canonical_account(account)
        secret = _validated_initial_administrator_password(password)
        tenant = "managed-local"
        principal = f"managed-local:{canonical}"
        record = _account_record(canonical, secret, Role.ADMIN, tenant, principal)
        with self._lock, _exclusive_store_lock(self._path):
            value = self._read()
            accounts = value.setdefault("accounts", {})
            if accounts:
                raise UserActionError(
                    "account_already_initialized",
                    "本机账号已初始化。如需新增或重置账号，请联系管理员。",
                )
            accounts[canonical] = record
            self._write(value)
            generation = self._generation_for(value)
        return PasswordAccount(canonical, principal, tenant, Role.ADMIN, generation)

    def authenticate(self, account: str, password: str) -> PasswordAccount | None:
        canonical = _canonical_account(account)
        secret = _validated_password(password)
        with self._lock, _exclusive_store_lock(self._path):
            value = self._read()
            raw = value.get("accounts", {}).get(canonical)
            if not isinstance(raw, dict):
                _derive(secret, _DUMMY_SALT, _ITERATIONS)
                return None
            iterations = raw["iterations"]
            salt = _strict_base64(raw["salt"], expected_bytes=32)
            expected = _strict_base64(raw["verifier"], expected_bytes=32)
            role = Role(raw["role"])
            principal_id = raw["principal_id"]
            tenant_id = raw["tenant_id"]
            actual = _derive(secret, salt, iterations)
            if not hmac.compare_digest(actual, expected):
                return None
            generation = self._generation_for(value)
            return PasswordAccount(canonical, principal_id, tenant_id, role, generation)

    def _read(self) -> dict[str, Any]:
        try:
            metadata = self._path.lstat()
        except FileNotFoundError:
            value = {"schema": _STORE_SCHEMA, "accounts": {}}
            return value
        try:
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise PasswordStoreRecoveryError()
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise PasswordStoreRecoveryError()
            if metadata.st_size > _MAX_STORE_BYTES:
                raise PasswordStoreRecoveryError()
            value = json.loads(self._path.read_text(encoding="utf-8"))
            if _is_current_store(value):
                if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
                    raise PasswordStoreRecoveryError()
                _validate_accounts(value["accounts"], allow_empty=True)
                return value
            if _is_legacy_store(value):
                _validate_accounts(value["accounts"], allow_empty=False)
                migrated = {"schema": _STORE_SCHEMA, "accounts": value["accounts"]}
                self._write(migrated)
                return migrated
        except PasswordStoreRecoveryError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PasswordStoreRecoveryError() from error
        raise PasswordStoreRecoveryError()

    def _write(self, value: dict[str, Any]) -> None:
        temporary_name: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".passwords-", suffix=".tmp", dir=self._path.parent,
            )
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self._path)
            if os.name != "nt":
                os.chmod(self._path, 0o600)
                _sync_directory(self._path.parent)
        except OSError as error:
            raise PasswordStoreRecoveryError() from error
        finally:
            if temporary_name is not None and os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _generation_for(self, value: dict[str, Any]) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"account-{hashlib.sha256(encoded).hexdigest()}"


@contextmanager
def _exclusive_store_lock(store_path: Path) -> Iterator[None]:
    """Serialize account reads, migrations and initialization across App Hosts."""

    store_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = store_path.with_name(f".{store_path.name}.lock")
    try:
        existing = lock_path.lstat()
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
            raise PasswordStoreRecoveryError()
        flags = os.O_RDWR
    except FileNotFoundError:
        flags = os.O_RDWR | os.O_CREAT
    try:
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise PasswordStoreRecoveryError() from error
    with os.fdopen(descriptor, "r+b") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise PasswordStoreRecoveryError()
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PasswordStoreRecoveryError()
        if hasattr(os, "fchmod"):
            os.fchmod(stream.fileno(), 0o600)
        if os.name != "nt":
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            return

        import msvcrt

        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_current_store(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"schema", "accounts"}
        and value.get("schema") == _STORE_SCHEMA
        and isinstance(value.get("accounts"), dict)
    )


def _is_legacy_store(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"version", "accounts"}
        and type(value.get("version")) is int
        and value.get("version") == _LEGACY_STORE_VERSION
        and isinstance(value.get("accounts"), dict)
    )


def _validate_accounts(accounts: dict[object, object], *, allow_empty: bool) -> None:
    if not allow_empty and not accounts:
        raise PasswordStoreRecoveryError()
    for account, raw in accounts.items():
        if not isinstance(account, str):
            raise PasswordStoreRecoveryError()
        if not isinstance(raw, dict) or set(raw) != _ACCOUNT_FIELDS:
            raise PasswordStoreRecoveryError()
        if raw["account"] != account:
            raise PasswordStoreRecoveryError()
        try:
            if _canonical_account(account) != account:
                raise ValueError("account key is not canonical")
            _validated_claim(raw["principal_id"], "principal_id", 160)
            _validated_claim(raw["tenant_id"], "tenant_id", 128)
            if not isinstance(raw["role"], str):
                raise ValueError("role must be a string")
            Role(raw["role"])
            if raw["scheme"] != _SCHEME:
                raise ValueError("unsupported password verifier")
            if type(raw["iterations"]) is not int or raw["iterations"] != _ITERATIONS:
                raise ValueError("unsupported password verifier parameters")
            _strict_base64(raw["salt"], expected_bytes=32)
            _strict_base64(raw["verifier"], expected_bytes=32)
        except (TypeError, ValueError, ValidationError) as error:
            raise PasswordStoreRecoveryError() from error


def _strict_base64(value: object, *, expected_bytes: int) -> bytes:
    if not isinstance(value, str):
        raise ValueError("password verifier field must be a string")
    decoded = base64.b64decode(value, validate=True)
    if len(decoded) != expected_bytes or base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("password verifier field is invalid")
    return decoded


def _canonical_account(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("account must be a string")
    account = value.strip().casefold()
    if not account or len(account) > 128 or any(character.isspace() for character in account):
        raise ValidationError("account must contain 1 to 128 non-space characters")
    return account


def _validated_claim(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    if (
        not value
        or value != value.strip()
        or len(value) > maximum
        or any(not character.isprintable() for character in value)
    ):
        raise ValidationError(f"{field} must contain 1 to {maximum} visible characters")
    return value


def _validated_password(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValidationError("password must be a string")
    encoded = value.encode("utf-8")
    if not encoded or len(encoded) > 4096:
        raise ValidationError("password is outside the supported length")
    return encoded


def _validated_initial_administrator_password(value: str) -> bytes:
    """Apply the one additional guard needed by the unrecoverable first setup."""

    encoded = _validated_password(value)
    if len(value) < 12:
        raise ValidationError("initial administrator password must contain at least 12 characters")
    return encoded


def _account_record(account: str, password: bytes, role: Role, tenant_id: str, principal_id: str) -> dict[str, object]:
    salt = secrets.token_bytes(32)
    verifier = _derive(password, salt, _ITERATIONS)
    return {
        "account": account,
        "principal_id": principal_id,
        "tenant_id": tenant_id,
        "role": role.value,
        "scheme": _SCHEME,
        "iterations": _ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "verifier": base64.b64encode(verifier).decode("ascii"),
    }


def _derive(password: bytes, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password, salt, iterations, dklen=32)
