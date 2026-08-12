"""Verified registry for the five Capability-owned module pages.

The App never copies these bytes into ``frontend``.  The registry verifies the
embedded Capability manifest and serves the original page resources from the
read-only Capability directory.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from threading import RLock
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

_CORE_SRC = Path(__file__).resolve().parents[3] / "core" / "src"
if str(_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(_CORE_SRC))

from runtime.protocol.module_host import (
    HostObjectRef,
    ModuleHostContext,
    ModuleHostContextError,
    issue_capability_token,
    validate_module_host_context,
    verify_module_host_context,
)
from runtime.protocol.models import ModuleRunRef

from .errors import CapabilityIntegrityError, UnavailableCapabilityError, ValidationError
from .identity.session_identity import SessionIdentity


PAGE_MODULES = ("datafetcher", "payoffer", "pricer", "backtester", "reporter")


@dataclass(frozen=True)
class ModulePage:
    module_name: str
    capability_asset: str
    content_hash: str


class PageRegistry:
    def __init__(self, capability_root: Path | None = None, token_secret: bytes | None = None) -> None:
        if capability_root is None:
            raise CapabilityIntegrityError(
                "Capability root must be explicitly supplied; development and tests must use a verified temporary Capability"
            )
        self._root = capability_root.resolve()
        self._token_secret = token_secret or secrets.token_bytes(32)
        self._manifest = self._read_manifest()
        self._pages = self._verify_pages()
        self._integrity = self._verify_content_tree()
        self._contexts: dict[str, dict[str, Any]] = {}
        self._context_lock = RLock()

    @property
    def capability_root(self) -> Path:
        return self._root

    @property
    def manifest(self) -> dict[str, Any]:
        return dict(self._manifest)

    @property
    def integrity(self) -> dict[str, Any]:
        """Integrity result without capability file contents or secret material."""
        return dict(self._integrity)

    def assert_execution_integrity(self) -> None:
        """Recheck the immutable Capability immediately before code execution.

        Startup verification only establishes an initial trust point.  A
        resource directory changed afterwards must never remain executable
        merely because its five page hashes were valid at startup.
        """

        self._verify_pages()
        integrity = self._verify_content_tree()
        if (
            integrity["declared_file_hashes"] != "verified"
            or integrity["content_tree_hash"] != "verified"
            or integrity["untracked_files"]
        ):
            raise CapabilityIntegrityError("Embedded Capability changed after startup verification")

    def get(self, module_name: str) -> ModulePage:
        try:
            return self._pages[module_name]
        except KeyError as error:
            raise KeyError(f"No registered Capability page for module: {module_name}") from error

    def all(self) -> tuple[ModulePage, ...]:
        return tuple(self._pages[name] for name in PAGE_MODULES)

    def read_asset(self, relative_path: str) -> tuple[bytes, str]:
        if not relative_path or relative_path.startswith("/") or ".." in Path(relative_path).parts:
            raise ValidationError("Capability asset path is invalid")
        path = (self._root / relative_path).resolve()
        if self._root not in path.parents or not path.is_file():
            raise KeyError(relative_path)
        expected = self._manifest.get("content_hashes", {}).get(relative_path)
        if not isinstance(expected, str):
            raise CapabilityIntegrityError(f"Capability asset is not declared in manifest: {relative_path}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if not hmac.compare_digest(expected, actual):
            raise CapabilityIntegrityError(f"Capability asset hash mismatch: {relative_path}")
        return path.read_bytes(), self._content_type(path.suffix)

    def host_context(
        self,
        identity: SessionIdentity,
        module_name: str,
        analysis_case_id: str | None = None,
        task_id: str | None = None,
        candidate_id: str | None = None,
        catalog_version: str | None = None,
        contract_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        return self._issue_context(
            identity,
            module_name,
            analysis_case_id=analysis_case_id,
            task_id=task_id,
            candidate_id=candidate_id,
            catalog_version=catalog_version,
            contract_fingerprint=contract_fingerprint,
            require_bridge=True,
            request_policy=("module.catalog",) if task_id is None else ("module.catalog", "module.run", "result.select"),
        )

    def service_context(
        self,
        identity: SessionIdentity,
        module_name: str,
        *,
        task_id: str,
        analysis_case_id: str | None = None,
        candidate_id: str | None = None,
        catalog_version: str | None = None,
        contract_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Issue a server-only OptChat context without mounting a page.

        It retains the same short-lived signature and module hash binding as a
        Desk context, but does not require an iframe bridge or grant
        ``module.run``.  ToolGateway performs the second proxy authorization.
        """

        return self._issue_context(
            identity,
            module_name,
            analysis_case_id=analysis_case_id,
            task_id=task_id,
            candidate_id=candidate_id,
            catalog_version=catalog_version,
            contract_fingerprint=contract_fingerprint,
            require_bridge=False,
            request_policy=("conversation.tool.run",),
        )

    def _issue_context(
        self,
        identity: SessionIdentity,
        module_name: str,
        *,
        analysis_case_id: str | None = None,
        task_id: str | None = None,
        candidate_id: str | None = None,
        catalog_version: str | None = None,
        contract_fingerprint: str | None = None,
        require_bridge: bool,
        request_policy: tuple[str, ...],
        contract_ref: HostObjectRef | None = None,
        config_ref: HostObjectRef | None = None,
        result_refs: tuple[ModuleRunRef, ...] = (),
    ) -> dict[str, Any]:
        if require_bridge and not self.bridge_available(module_name):
            raise UnavailableCapabilityError(
                "module.host_context",
                "the embedded module page does not declare the required ModuleHost bridge; update Capability before enabling App-hosted execution",
            )
        page = self.get(module_name)
        expires_at = int(time.time()) + 300
        context_id = f"mhc_{secrets.token_urlsafe(24)}"
        session_ref = hmac.new(self._token_secret, f"session:{identity.session_id}".encode("utf-8"), hashlib.sha256).hexdigest()[:24]
        session_ref = f"local:{session_ref}"
        capability_version = str(self._manifest["capability_version"])
        protocol_version = str(self._manifest["protocol_version"])
        context = ModuleHostContext(
            session_ref=session_ref,
            capability_token=issue_capability_token(
                token_secret=self._token_secret,
                session_id=identity.session_id,
                principal_id=identity.principal_id,
                session_ref=session_ref,
                module=module_name,
                expires_at=expires_at,
                context_id=context_id,
                page_hash=page.content_hash,
                audience=identity.audience,
                host_kind="app",
                request_policy=request_policy,
                capability_version=capability_version,
                protocol_version=protocol_version,
                task_id=task_id,
                analysis_case_id=analysis_case_id,
                candidate_id=candidate_id,
                catalog_version=catalog_version,
                contract_fingerprint=contract_fingerprint,
                contract_ref=contract_ref,
                config_ref=config_ref,
                result_refs=result_refs,
            ),
            analysis_case_id=analysis_case_id,
            task_id=task_id,
            candidate_id=candidate_id,
            catalog_version=catalog_version,
            contract_fingerprint=contract_fingerprint,
            module=module_name,
            page_hash=page.content_hash,
            capability_version=capability_version,
            protocol_version=protocol_version,
            context_id=context_id,
            host_kind="app",
            request_policy=request_policy,
            contract_ref=contract_ref,
            config_ref=config_ref,
            result_refs=result_refs,
        )
        with self._context_lock:
            self._purge_contexts()
            self._contexts[context_id] = {
                "session_id": identity.session_id,
                "principal_id": identity.principal_id,
                "audience": identity.audience,
                "module": module_name,
                "page_hash": page.content_hash,
                "analysis_case_id": analysis_case_id,
                "task_id": task_id,
                "candidate_id": candidate_id,
                "catalog_version": catalog_version,
                "contract_fingerprint": contract_fingerprint,
                "expires_at": expires_at,
                "request_ids": set(),
            }
        return context.to_payload()

    def bind_contract_context(
        self,
        identity: SessionIdentity,
        context: ModuleHostContext,
        *,
        analysis_case_id: str,
        candidate_id: str,
        catalog_version: str,
        contract_fingerprint: str,
        contract_ref: HostObjectRef,
    ) -> ModuleHostContext:
        """Issue one internal, fully signed derivative of a verified page context."""

        with self._context_lock:
            record = self._contexts.get(context.context_id)
            if record is None or record["expires_at"] <= int(time.time()):
                raise ValidationError("Module Host context is expired or revoked")
            if any(record[key] != value for key, value in {
                "session_id": identity.session_id,
                "principal_id": identity.principal_id,
                "audience": identity.audience,
                "module": context.module,
                "page_hash": context.page_hash,
            }.items()):
                raise ValidationError("Module Host context does not belong to this session")
        try:
            verify_module_host_context(
                context,
                token_secret=self._token_secret,
                session_id=identity.session_id,
                principal_id=identity.principal_id,
                audience=identity.audience,
            )
        except ModuleHostContextError as error:
            raise ValidationError("Module Host context is invalid") from error
        payload = self._issue_context(
            identity,
            context.module,
            analysis_case_id=analysis_case_id,
            task_id=context.task_id,
            candidate_id=candidate_id,
            catalog_version=catalog_version,
            contract_fingerprint=contract_fingerprint,
            require_bridge=False,
            request_policy=context.request_policy,
            contract_ref=contract_ref,
            config_ref=context.config_ref,
            result_refs=context.result_refs,
        )
        return validate_module_host_context(payload, expected_module=context.module)

    def bridge_available(self, module_name: str) -> bool:
        page = self.get(module_name)
        bridge = self._root / "assets" / "pages" / "module-host-bridge.js"
        if not bridge.is_file() or "assets/pages/module-host-bridge.js" not in self._manifest.get("content_hashes", {}):
            return False
        return "module-host-bridge.js" in (self._root / page.capability_asset).read_text(encoding="utf-8")

    def validate_host_request(
        self,
        identity: SessionIdentity,
        module_name: str,
        context_payload: object,
        request_id: str,
    ) -> ModuleHostContext:
        """Re-authorize an iframe request against its session-bound context.

        The browser never chooses tenant, principal, page hash or source paths.
        A request id is single-use per context so an intercepted non-idempotent
        request cannot create another ModuleRun.
        """
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValidationError("Module Host request id is invalid")
        try:
            context = validate_module_host_context(context_payload, expected_module=module_name)
            with self._context_lock:
                record = self._contexts.get(context.context_id)
                if record is None or record["expires_at"] <= int(time.time()):
                    raise ValidationError("Module Host context is expired or revoked")
                if any(record[key] != value for key, value in {
                    "session_id": identity.session_id,
                    "principal_id": identity.principal_id,
                    "audience": identity.audience,
                    "module": module_name,
                    "page_hash": context.page_hash,
                }.items()):
                    raise ValidationError("Module Host context does not belong to this session")
                if any(record[key] != value for key, value in {
                    "analysis_case_id": context.analysis_case_id,
                    "task_id": context.task_id,
                    "candidate_id": context.candidate_id,
                    "catalog_version": context.catalog_version,
                    "contract_fingerprint": context.contract_fingerprint,
                }.items()):
                    raise ValidationError("Module Host context analysis case is invalid")
                verify_module_host_context(
                    context,
                    token_secret=self._token_secret,
                    session_id=identity.session_id,
                    principal_id=identity.principal_id,
                    audience=identity.audience,
                )
                request_ids = record["request_ids"]
                if request_id in request_ids:
                    raise ValidationError("Module Host request was already used")
                request_ids.add(request_id)
        except ModuleHostContextError as error:
            raise ValidationError("Module Host context is invalid") from error
        return context

    def _purge_contexts(self) -> None:
        now = int(time.time())
        for context_id, record in tuple(self._contexts.items()):
            if record["expires_at"] <= now:
                del self._contexts[context_id]

    def _read_manifest(self) -> dict[str, Any]:
        path = self._root / "capability-manifest.json"
        if not path.is_file():
            raise CapabilityIntegrityError("Embedded Capability manifest is missing")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise CapabilityIntegrityError("Embedded Capability manifest is invalid JSON") from error
        required = {"capability_version", "protocol_version", "content_tree_hash", "content_hashes", "modules"}
        missing = required.difference(manifest)
        if missing or not isinstance(manifest.get("content_hashes"), dict):
            raise CapabilityIntegrityError(f"Embedded Capability manifest lacks required fields: {sorted(missing)}")
        if not set(PAGE_MODULES).issubset(set(manifest.get("modules", []))):
            raise CapabilityIntegrityError("Embedded Capability manifest does not declare all five page modules")
        return manifest

    def _verify_pages(self) -> dict[str, ModulePage]:
        pages: dict[str, ModulePage] = {}
        content_hashes: dict[str, str] = self._manifest["content_hashes"]
        for module_name in PAGE_MODULES:
            relative = f"assets/pages/{module_name}/{module_name}.html"
            expected = content_hashes.get(relative)
            path = self._root / relative
            if not isinstance(expected, str) or not path.is_file():
                raise CapabilityIntegrityError(f"Capability page is missing from manifest: {relative}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if not hmac.compare_digest(expected, actual):
                raise CapabilityIntegrityError(f"Capability page hash mismatch: {relative}")
            pages[module_name] = ModulePage(module_name, relative, actual)
        return pages

    def _verify_content_tree(self) -> dict[str, Any]:
        """Diagnose the declared full-tree hash using blueprint hash_spec_version.

        Startup surfaces integrity status for diagnostics.  Code execution
        calls :meth:`assert_execution_integrity` and rejects any mismatch.
        """
        entries: list[dict[str, Any]] = []
        actual_hashes: dict[str, str] = {}
        for path in sorted(self._root.rglob("*")):
            if not path.is_file() or path.name == "capability-manifest.json":
                continue
            relative = path.relative_to(self._root).as_posix()
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            actual_hashes[relative] = digest
            entries.append({
                "path": relative,
                "size": len(content),
                "sha256": digest,
                "executable": bool(os.stat(path).st_mode & 0o111),
            })
        declared_hashes = self._manifest["content_hashes"]
        declared_hashes_match = all(actual_hashes.get(path) == digest for path, digest in declared_hashes.items())
        untracked_files = sorted(set(actual_hashes).difference(declared_hashes))
        # The packaging verifier hashes entries in the explicit manifest
        # order.  Filesystem Path ordering is not equivalent for siblings such
        # as ``dark/`` and ``dark-windows/`` on every platform, so rebuilding
        # the list from ``rglob`` order produced a false release mismatch.
        # Keep the manifest's declared order only when it is a complete,
        # duplicate-free representation of the declared hash map; otherwise
        # fall back to deterministic lexical order and fail the tree hash.
        actual_entries = {str(entry["path"]): entry for entry in entries}
        manifest_entries = self._manifest.get("content_tree_entries")
        manifest_order = [
            entry.get("path")
            for entry in manifest_entries
            if isinstance(entry, Mapping) and isinstance(entry.get("path"), str)
        ] if isinstance(manifest_entries, list) else []
        if (
            len(manifest_order) == len(set(manifest_order))
            and set(manifest_order) == set(declared_hashes)
            and all(path in actual_entries for path in manifest_order)
        ):
            declared_entries = [actual_entries[path] for path in manifest_order]
        else:
            declared_entries = [actual_entries[path] for path in sorted(set(actual_entries).intersection(declared_hashes))]
        canonical = json.dumps(declared_entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        actual_tree_hash = hashlib.sha256(canonical).hexdigest()
        expected_tree_hash = self._manifest["content_tree_hash"]
        tree_match = hmac.compare_digest(actual_tree_hash, expected_tree_hash)
        return {
            "page_hashes": "verified",
            "declared_file_hashes": "verified" if declared_hashes_match else "mismatch",
            "content_tree_hash": "verified" if tree_match else "mismatch",
            "untracked_files": untracked_files,
            "expected_content_tree_hash": expected_tree_hash,
            "actual_content_tree_hash": actual_tree_hash,
            "release_ready": bool(declared_hashes_match and tree_match and not untracked_files),
        }

    @staticmethod
    def _content_type(suffix: str) -> str:
        return {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
        }.get(suffix.lower(), "application/octet-stream")
