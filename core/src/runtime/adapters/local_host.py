"""Standalone Skill Host security and OpenAI-compatible transport helpers.

This module contains no option selection, pricing, or reporting logic.  It
only owns the local-development identity boundary and bounded HTTP transport
used by ``core/tool_entry.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import secrets
import stat
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from runtime.protocol.models import CallerContext, ModuleRunRef
from runtime.protocol.version import (
    DEVELOPMENT_RELEASE_ID,
    MODULE_HOST_PROTOCOL_ID,
    RESOLVED_CONTRACT_SCHEMA_ID,
)
from runtime.protocol.module_host import (
    HostObjectRef,
    ModuleHostContext,
    issue_capability_token,
    verify_module_host_context,
)


class LocalHostError(RuntimeError):
    """The standalone Host configuration or response is unsafe."""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _path_identity(path: Path, label: str) -> tuple[int, int]:
    try:
        entry = os.lstat(path)
    except OSError as error:
        raise LocalHostError(f"{label}不可用。") from error
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        raise LocalHostError(f"{label}必须是非符号链接目录。")
    return entry.st_dev, entry.st_ino


@dataclass(frozen=True)
class LocalProjectLayout:
    """One project-owned writable layout outside the installed Skill."""

    skill_root: Path
    project_root: Path
    data_root: Path
    result_root: Path
    project_identity: tuple[int, int]

    @classmethod
    def create(cls, *, skill_root: str | Path, project_root: str | Path) -> "LocalProjectLayout":
        skill = Path(skill_root).expanduser().resolve()
        requested_project = Path(os.path.abspath(Path(project_root).expanduser()))
        if requested_project.exists() and stat.S_ISLNK(os.lstat(requested_project).st_mode):
            raise LocalHostError("研究项目目录必须是非符号链接目录。")
        # Keep the pinned root in the kernel's canonical path namespace.  On
        # macOS this normalises the harmless /var -> /private/var alias while
        # the direct supplied project directory remains explicitly checked.
        project = requested_project.resolve(strict=False)
        identity = _path_identity(project, "研究项目目录")
        if project == skill or _inside(project, skill):
            raise LocalHostError("请从研究项目目录运行OptionHelper，不能把Skill安装目录当作项目目录。")
        data = project / "data"
        result = project / "result"
        if not _inside(data, project) or not _inside(result, project) or data == result:
            raise LocalHostError("项目DataStore与ResultStore配置无效。")
        return cls(skill_root=skill, project_root=project, data_root=data, result_root=result, project_identity=identity)

    def initialize(self) -> None:
        # ``dir_fd`` and ``O_DIRECTORY`` are POSIX-only.  Windows must use
        # pathname operations, but keep the same invariant: the project root
        # and each store directory are checked as real directories before and
        # after creation, and a symlink is never accepted.
        supports_dir_fd = getattr(os, "supports_dir_fd", set())
        posix_directory_api = (
            os.name != "nt"
            and bool(getattr(os, "O_DIRECTORY", 0))
            and os.mkdir in supports_dir_fd
            and os.stat in supports_dir_fd
        )
        if not posix_directory_api:
            self._initialize_portable()
            return

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.project_root, flags)
        except OSError as error:
            raise LocalHostError("研究项目目录在Host初始化前发生变化。") from error
        try:
            if (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino) != self.project_identity:
                raise LocalHostError("研究项目目录在Host初始化前发生变化。")
            for name, label in (("data", "项目DataStore"), ("result", "项目ResultStore")):
                try:
                    os.mkdir(name, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                entry = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
                    raise LocalHostError(f"{label}必须是非符号链接目录。")
        finally:
            os.close(descriptor)

    def _initialize_portable(self) -> None:
        """Initialize stores on platforms without POSIX directory handles.

        Windows has no ``openat``/``dir_fd`` equivalent in Python.  The
        root identity is therefore rechecked around every mutation and the
        resulting entries are inspected with ``lstat``.  This preserves the
        fail-closed symlink policy without making the Skill POSIX-only.
        """

        def assert_root() -> None:
            if _path_identity(self.project_root, "研究项目目录") != self.project_identity:
                raise LocalHostError("研究项目目录在Host初始化前发生变化。")

        assert_root()
        for name, label in (("data", "项目DataStore"), ("result", "项目ResultStore")):
            assert_root()
            target = self.project_root / name
            try:
                os.mkdir(target, mode=0o700)
            except FileExistsError:
                pass
            assert_root()
            try:
                entry = os.lstat(target)
            except OSError as error:
                raise LocalHostError(f"{label}不可用。") from error
            if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
                raise LocalHostError(f"{label}必须是非符号链接目录。")
        assert_root()


class LocalHostAuthority:
    """Issue and verify exact local-development module contexts in-process."""

    def __init__(self, layout: LocalProjectLayout) -> None:
        self.layout = layout
        self._secret = secrets.token_bytes(32)
        self._session_id = f"local-{secrets.token_hex(12)}"
        self._principal_id = "local-project-user"
        self._tenant_id = "local"
        self._audience = "option-helper-local"

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    @property
    def principal_id(self) -> str:
        """The fixed local-development principal used for Host-owned assets."""

        return self._principal_id

    def caller(self, *, request_id: str) -> CallerContext:
        return CallerContext(
            tenant_id=self._tenant_id,
            principal_id=self._principal_id,
            role="local",
            capabilities=("module.run", "module.catalog", "data:read", "data:force_refresh"),
            session_id=self._session_id,
            audience=self._audience,
            request_id=request_id,
        )

    def context(
        self,
        module: str,
        *,
        task_id: str,
        analysis_case_id: str,
        candidate_id: str,
        catalog_version: str,
        contract_fingerprint: str,
        result_refs: tuple[ModuleRunRef, ...] = (),
    ) -> ModuleHostContext:
        issued_at = int(time.time())
        values = {
            "session_ref": f"local:{self._session_id}",
            "analysis_case_id": analysis_case_id,
            "task_id": task_id,
            "candidate_id": candidate_id,
            "catalog_version": catalog_version,
            "contract_fingerprint": contract_fingerprint,
            "module": module,
            "page_hash": "0" * 64,
            "capability_version": DEVELOPMENT_RELEASE_ID,
            "protocol_id": MODULE_HOST_PROTOCOL_ID,
            "context_id": f"mhc_{secrets.token_urlsafe(18)}",
            "host_kind": "local-development",
            "request_policy": ("module.catalog", "module.run"),
            "result_refs": result_refs,
            "contract_ref": HostObjectRef(
                reference_id=f"contract-{contract_fingerprint[:24]}",
                schema_id=RESOLVED_CONTRACT_SCHEMA_ID,
                content_hash=contract_fingerprint,
            ),
        }
        token = issue_capability_token(
            token_secret=self._secret,
            session_id=self._session_id,
            principal_id=self._principal_id,
            audience=self._audience,
            expires_at=issued_at + 600,
            **values,
        )
        context = ModuleHostContext(capability_token=token, **values)
        return self.verify(context)

    def verify(self, context: ModuleHostContext) -> ModuleHostContext:
        if context.host_kind != "local-development":
            raise LocalHostError("项目级Host只接受local-development上下文。")
        return verify_module_host_context(
            context,
            token_secret=self._secret,
            session_id=self._session_id,
            principal_id=self._principal_id,
            audience=self._audience,
        )


@dataclass(frozen=True)
class JsonHttpEndpoint:
    """Bounded JSON POST transport that never includes credentials in errors."""

    base_url: str
    timeout_seconds: float = 60.0
    max_response_bytes: int = 4_000_000

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise LocalHostError("Host或模型服务地址必须是不含凭据的HTTP(S)地址。")
        host = parsed.hostname
        if not host:
            raise LocalHostError("Host或模型服务地址必须包含主机名。")
        if parsed.scheme == "http" and not _is_loopback_host(host):
            raise LocalHostError("携带API Key的远端模型服务必须使用HTTPS；HTTP仅允许本机回环地址。")

    def post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        bearer_token: str | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(dict(payload), ensure_ascii=False, allow_nan=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        request = Request(
            urljoin(self.base_url.rstrip("/") + "/", path.lstrip("/")),
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with build_opener(_NoRedirect()).open(request, timeout=self.timeout_seconds) as response:
                raw = response.read(self.max_response_bytes + 1)
        except HTTPError as error:
            raise LocalHostError(f"服务请求失败，HTTP状态码为{error.code}。") from error
        except (URLError, TimeoutError, OSError) as error:
            raise LocalHostError("服务暂时无法连接，请检查网络或Host地址。") from error
        if len(raw) > self.max_response_bytes:
            raise LocalHostError("服务响应超过安全大小限制。")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LocalHostError("服务没有返回有效JSON结果。") from error
        if not isinstance(value, Mapping):
            raise LocalHostError("服务必须返回JSON对象。")
        return dict(value)


def _is_loopback_host(host: str) -> bool:
    if host.rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirect(HTTPRedirectHandler):
    """Never forward an Authorization header to a redirect destination."""

    def redirect_request(self, request: Request, fp: Any, code: int, message: str, headers: Any, newurl: str) -> Request | None:
        raise LocalHostError("模型服务重定向被拒绝，未转发API Key。")


__all__ = (
    "JsonHttpEndpoint",
    "LocalHostAuthority",
    "LocalHostError",
    "LocalProjectLayout",
)
