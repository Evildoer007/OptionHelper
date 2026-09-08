"""Runnable local HTTP platform for OptionHelper App.

The server is intentionally limited to App concerns: local development
identity, server-side authorization, App-owned state, settings, audit and
Capability page mounting.  Financial execution remains in the verified
Capability behind :class:`ToolGateway`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import threading
from dataclasses import asdict, replace
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from .agent_runtime.conversation_service import ConversationService
from .agent_runtime.context_builder import ContextBuilder, ResultStoreObservationBuilder
from .agent_runtime.agent_loop import AgentLoop
from .agent_runtime.conversation_agent import OptionConversationAgent
from .agent_runtime.recommender_adapter import AppConversationToolExecutor, RecommenderAdapter
from .agent_runtime.multi_agent import (
    AgentRunConcurrencyGate,
    AgentRunSettlementRegistry,
    default_agent_instructions,
    effective_agent_instructions,
    effective_preset_revision,
    recommendation_presets,
)
from .agent_runtime import multi_agent as multi_agent_runtime
from .agent_runtime.tool_dispatcher import ToolDispatcher
from .agent_runtime.durability_checkpoint import DurabilityCheckpointStore, stable_operation_id
from .agent_runtime.runtime_telemetry import RuntimeTelemetry
from .attachments import AttachmentStore
from .audit.audit_models import AuditEvent
from .audit.audit_service import LocalAuditService
from .authorization.policy import AuthorizationPolicy
from .authorization.roles import Role
from .capability_service import CapabilityServiceCaller
from .datafetcher_adapter import DataFetcherAdapter
from .errors import AuthorizationError, CapabilityIntegrityError, UnavailableCapabilityError, UserActionError, ValidationError
from .identity.identity_provider import IdentityProvider, LocalAuthProvider
from .identity.login_handler import AuthenticationMode, LoginHandler, LoginOutcome, LoginRequest
from .identity.password_store import PasswordCredentialStore
from .identity.session_identity import SessionIdentity
from .model_gateway.gateway import ModelGateway
from .model_gateway.deepseek_provider import complete_openai_compatible, complete_openai_compatible_with_metadata, decide_openai_compatible, discover_openai_models, validate_openai_base_url
from .model_gateway.openai_compatible_stream import stream_openai_compatible
from .model_gateway.provider_registry import ProviderRegistry
from .page_registry import PAGE_MODULES, PageRegistry
from .settings.connection_tester import ConnectionTester
from .settings.settings_models import (
    DataInterfaceSettings,
    MULTI_AGENT_DEFAULT_REVIEW_POLICY_ID,
    MULTI_AGENT_RECOMMENDATION_ROLES,
    ModelCatalogEntry,
    ModelConnectionSettings,
    ModelProviderProfile,
    ModelSelection,
    ModelServiceSettings,
    PreferenceSettings,
    SettingsSnapshot,
    StorageExportSettings,
    serialize_settings,
    serialize_settings_public,
)
from .settings.model_catalog import built_in_provider_catalog
from .settings.settings_service import SettingsService
from .secrets.credential_migration import migrate_configured_credentials
from .secrets.secret_ref import SecretRef
from .secrets.secret_provider import SecretProvider
from .stores.data_store import DataStore
from .stores import _LocalDocumentStore
from .stores.result_store import ResultStore
from .stores.session_store import LocalSessionStore
from .stores.settings_store import LocalSettingsStore
from .task_runtime.job_runner import JobRunner
from .task_runtime.job_registry import AppJobWorker, JobRegistry
from .task_runtime.operation_service import OperationCancelled, TaskOperationService
from .task_runtime.compute_process import ComputeProcessSupervisor
from .task_runtime.task_service import TaskService
from .tool_gateway import ToolGateway
from .reporter_adapter import ReporterAdapter
from .report_editor import ReportEditor
from .report_delivery import ReportDelivery
from desktop.common.paths import AppPaths
from runtime.adapters.local_store import LocalDataStore
from runtime.protocol.models import ModuleRunRef


FRONTEND_ASSETS = frozenset({
    "login/index.html", "login/login-startup.js", "login/login.js",
    "optchat/index.html", "optchat/optchat.js", "optchat/attachment-utils.js",
    "optdesk/index.html", "optdesk/optdesk.js",
    "settings/index.html", "settings/settings.js", "settings/model-providers.css", "settings/general-settings.css", "settings/settings-shell.css",
    "shared/styles.css", "shared/refinement.css", "shared/theme-overrides.css", "shared/app.js", "shared/transition-scope.js", "shared/theme-bootstrap.js", "shared/theme.js", "shared/ui-scale.js", "shared/scrollbar-activity.js", "shared/vol-surface.js", "shared/thinking-orb.js", "shared/thinking-orbs-engine.js", "shared/bloub-engine.js", "shared/bloub-avatar.js", "shared/activity-motion.js",
})
_CAPABILITY_CSP = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'self'; form-action 'self'"
_AGENT_RUNTIME_MODES = frozenset({"disabled", "shadow", "active"})
_RECOMMENDATION_MODE_IDS = (
    "sequential-deliberation", "product-trader-loop", "independent-council", "constraint-ranking",
)


def _agent_runtime_source_entrypoint() -> Path:
    return Path(__file__).resolve().parents[1] / "runtime" / "optionhelper_agent_runtime" / "dist" / "runtime.cjs"


def _resolve_agent_runtime_mode(explicit: str | None) -> str:
    configured = str(explicit).strip().lower() if explicit is not None else os.environ.get(
        "OPTIONHELPER_AGENT_RUNTIME_MODE", "",
    ).strip().lower()
    if configured:
        if configured not in _AGENT_RUNTIME_MODES:
            raise ValidationError("Agent运行时模式必须是disabled、shadow或active")
        return configured
    runtime_path = os.environ.get("OPTIONHELPER_AGENT_RUNTIME_PATH", "").strip()
    if runtime_path and Path(runtime_path).expanduser().is_file():
        return "active"
    if not bool(getattr(sys, "frozen", False)) and _agent_runtime_source_entrypoint().is_file() and shutil.which("node"):
        return "active"
    return "disabled"


def _agent_runtime_public_status(mode: str) -> dict[str, Any]:
    runtime_path = os.environ.get("OPTIONHELPER_AGENT_RUNTIME_PATH", "").strip()
    bundled_ready = bool(runtime_path and Path(runtime_path).expanduser().is_file())
    source_ready = bool(
        not getattr(sys, "frozen", False)
        and _agent_runtime_source_entrypoint().is_file()
        and shutil.which("node")
    )
    available = bundled_ready or source_ready
    if mode == "disabled":
        reason = "Agent运行时已显式关闭。"
    elif available:
        reason = "Agent运行时已通过本机入口探测。"
    else:
        reason = "未找到可用的Agent运行时。"
    return {
        "runtime_status": "available" if available and mode in {"active", "shadow"} else "unavailable",
        "runtime_mode": mode,
        "runtime_version": os.environ.get(
            "OPTIONHELPER_AGENT_RUNTIME_VERSION", "source" if source_ready else "unknown",
        ),
        "runtime_reason": reason,
    }
_RECOMMENDATION_MODE_ROLE_FALLBACKS = {
    "sequential-deliberation": ("Interpreter", "Selector", "Reviewer"),
    "product-trader-loop": ("Structurer", "Trader", "Reviewer"),
    "independent-council": ("Framer", "Matcher", "Hedger", "Moderator"),
    "constraint-ranking": ("Specifier", "Generator", "Evaluator", "Reviewer"),
}
_REVIEW_POLICY_ROLE_FALLBACKS = {"standard-review": ()}


def _catalog_value(item: object, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, Mapping) else getattr(item, name, default)


def _catalog_roles(item: object, fallback: tuple[str, ...]) -> list[str]:
    roles = _catalog_value(item, "roles")
    if isinstance(roles, (list, tuple)) and all(isinstance(role, str) and role for role in roles):
        return list(roles)
    defaults = _catalog_value(item, "role_model_defaults", {})
    if isinstance(defaults, Mapping) and defaults:
        return [str(role) for role in defaults]
    return list(fallback)


def _review_policies() -> Mapping[str, object]:
    """Read the runtime registry once available; keep settings safe during rollout."""

    factory = getattr(multi_agent_runtime, "review_policies", None)
    if callable(factory):
        return factory()
    return {
        MULTI_AGENT_DEFAULT_REVIEW_POLICY_ID: {
            "policy_id": MULTI_AGENT_DEFAULT_REVIEW_POLICY_ID,
            "version": "initial",
            "display_name": "标准复核",
            "enabled": True,
            "disabled_reason": None,
        },
        "strict-review": {
            "policy_id": "strict-review",
            "version": "initial",
            "display_name": "严格复核",
            "enabled": False,
            "disabled_reason": "严格复核协议尚未发布。",
        },
    }


def _review_policy_roles(policy_id: str, policy: object) -> list[str]:
    """Expose only additional policy-owned runs, not a Mode's terminal role."""

    if policy_id == MULTI_AGENT_DEFAULT_REVIEW_POLICY_ID:
        return []
    return _catalog_roles(policy, _REVIEW_POLICY_ROLE_FALLBACKS.get(policy_id, ()))


class AppServer:
    """Local App runtime with the same HTTP boundaries expected by a server mode."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        app_data_dir: Path | None = None,
        capability_root: Path | None = None,
        frontend_root: Path | None = None,
        brand_assets_root: Path | None = None,
        secret_provider: SecretProvider | None = None,
        *,
        allow_unverified_capability: bool = False,
        authentication_mode: AuthenticationMode = "local-development",
        agent_runtime_mode: str | None = None,
    ) -> None:
        if host != "127.0.0.1":
            raise ValidationError("Local App mode must bind to a loopback address")
        self.host = host
        self.port = port
        self.frontend_root = (frontend_root or Path(__file__).resolve().parents[1] / "frontend").resolve()
        if not self.frontend_root.is_dir():
            raise ValidationError("App frontend resources are missing")
        self.brand_assets_root = (brand_assets_root or Path(__file__).resolve().parents[3] / "assets" / "icons").resolve()
        if not self.brand_assets_root.is_dir():
            raise ValidationError("App brand resources are missing")
        default_data_dir = AppPaths().current_user_data_dir() / "local-state"
        self._documents = _LocalDocumentStore(app_data_dir or default_data_dir)
        self._capability_runtime_root = self._documents._root / "runtime"
        for directory in (
            self._capability_runtime_root,
            self._capability_runtime_root / "data",
            self._capability_runtime_root / "result",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.policy = AuthorizationPolicy()
        self.identity_provider: IdentityProvider = LocalAuthProvider()
        self.sessions = LocalSessionStore(self._documents)
        self.login_handler = LoginHandler(
            mode=authentication_mode,
            password_store=PasswordCredentialStore(self._documents._root / "authentication" / "passwords.json"),
        )
        self.audit = LocalAuditService(self._documents)
        self._settings_store = LocalSettingsStore(self._documents)
        self.settings = SettingsService(self._settings_store)
        self.tasks = TaskService(self._documents)
        self.attachments = AttachmentStore(self._documents._root / "attachments")
        self.data_assets = DataStore(self._documents)
        self.results = ResultStore(self._documents)
        self.registry = PageRegistry(capability_root)
        if not self.registry.integrity["release_ready"] and not allow_unverified_capability:
            raise CapabilityIntegrityError("Embedded Capability is not release-ready; App launch is blocked")
        chart_presentation_bytes, chart_presentation_type = self.registry.read_asset(
            "assets/pages/reporter/editor/chart-presentation.js"
        )
        if "javascript" not in chart_presentation_type:
            raise CapabilityIntegrityError("Report chart presentation asset has an invalid media type")
        try:
            chart_presentation_source = chart_presentation_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CapabilityIntegrityError("Report chart presentation asset is not UTF-8") from error
        self.report_editor = ReportEditor(
            self.results,
            self.tasks,
            chart_presentation_source=chart_presentation_source,
        )
        self.capability_services = CapabilityServiceCaller(
            self.registry,
            runtime_root=self._capability_runtime_root,
        )
        self.secret_provider = secret_provider or _platform_secret_provider(self._documents._root)
        self.credential_migration = migrate_configured_credentials(
            self._settings_store, self.secret_provider,
        )
        self._secret_reference_provider = next(
            (
                provider_name
                for provider_name in ("local-secret", "managed-secret", "keychain", "credential-manager")
                if self.secret_provider.supports(provider_name)
            ),
            "local-secret",
        )
        self.datafetcher = DataFetcherAdapter(
            self.settings_for,
            self.capability_services,
            LocalDataStore(self._capability_runtime_root / "data"),
            self.secret_provider,
            verification_for=self.data_connection_verified,
            revoke_verification=self.revoke_data_connection_verification,
            mark_temporarily_unavailable=self.mark_data_connection_temporarily_unavailable,
        )
        self.compute_supervisor = ComputeProcessSupervisor(
            minimum_slots=1,
            diagnostics_root=self._documents._root / "diagnostics" / "compute",
        )
        self.gateway = ToolGateway(
            self.registry,
            self.policy,
            ReporterAdapter(self.results, self.tasks),
            self.datafetcher,
            self.results,
            self.data_assets,
            self.tasks,
            capability_runtime_root=self._capability_runtime_root,
            compute_supervisor=self.compute_supervisor,
        )
        self.provider_registry = ProviderRegistry()
        self.provider_registry.register(
            "openai-compatible",
            lambda settings, reference, messages, request_control=None: complete_openai_compatible(
                settings,
                reference,
                messages,
                resolve_secret=lambda ref: self.secret_provider.resolve(ref, "模型服务凭据"),
                request_control=request_control,
            ),
            decision=lambda settings, reference, context, request_control=None: decide_openai_compatible(
                settings,
                reference,
                context,
                resolve_secret=lambda ref: self.secret_provider.resolve(ref, "模型服务凭据"),
                request_control=request_control,
            ),
            completion_with_metadata=lambda settings, reference, messages, request_control=None: complete_openai_compatible_with_metadata(
                settings,
                reference,
                messages,
                resolve_secret=lambda ref: self.secret_provider.resolve(ref, "模型服务凭据"),
                request_control=request_control,
            ),
            stream=lambda settings, reference, messages, request_control=None, tools=None: stream_openai_compatible(
                settings,
                reference,
                messages,
                resolve_secret=lambda ref: self.secret_provider.resolve(ref, "模型服务凭据"),
                request_control=request_control,
                tools=tools,
            ),
            bounded_request_control=True,
        )
        self.job_registry = JobRegistry(self._documents._root / "calculation-jobs.sqlite3")
        self.dispatch_checkpoints = DurabilityCheckpointStore(self.tasks.session_event_log)
        def verified_job_proof(record):
            if record.module_run_ref is None:
                return None
            recovery_identity = SessionIdentity(
                record.owner_id, record.tenant_id, Role.ADMIN, f"job-recovery:{record.job_id}",
            )
            self.results.verify_owned_module_run(recovery_identity, record.module_run_ref)
            return record.module_run_ref

        self.job_registry.recover_interrupted(verified_job_proof)
        def verified_job_proofs(records):
            requests = []
            references = []
            for record in records:
                if record.module_run_ref is None:
                    raise ValidationError("Succeeded calculation job has no verified Core proof")
                requests.append((
                    SessionIdentity(
                        record.owner_id, record.tenant_id, Role.ADMIN,
                        f"job-recovery:{record.job_id}",
                    ),
                    record.module_run_ref,
                ))
                references.append(record.module_run_ref)
            self.results.verify_owned_module_runs(requests)
            return tuple(references)

        self.job_registry.validate_succeeded_proofs(verified_job_proofs)
        self.dispatch_checkpoints.reconcile_succeeded(
            set(self.job_registry.succeeded_operation_ids())
        )
        self.job_worker = AppJobWorker(4)
        self.operations = TaskOperationService(self._documents._root / "task-operations.sqlite3")
        def recovered_operation_result(operation, request_id: str, input_hash: str):
            dispatch_operation_id = stable_operation_id(
                operation.tenant_id,
                operation.task_id,
                operation.module,
                input_hash,
                request_id,
            )
            job = self.job_registry.get_by_operation(dispatch_operation_id)
            if (
                job is None
                or job.state != "succeeded"
                or job.module_run_ref is None
                or job.tenant_id != operation.tenant_id
                or job.owner_id != operation.owner_id
                or job.task_id != operation.task_id
                or job.module != operation.module
            ):
                return None
            try:
                reference = ModuleRunRef(**dict(job.module_run_ref))
            except (TypeError, ValueError):
                return None
            canonical_reference = asdict(reference)
            if canonical_reference != job.module_run_ref or (
                reference.tenant_id != operation.tenant_id
                or reference.task_id != operation.task_id
                or reference.module != operation.module
            ):
                return None
            identity = SessionIdentity(
                operation.owner_id,
                operation.tenant_id,
                Role.ADMIN,
                f"operation-recovery:{operation.operation_id}",
            )
            try:
                self.tasks.get(identity, operation.task_id)
                stored = self.results.resolve_module_run(identity, canonical_reference)
            except (KeyError, AuthorizationError, ValidationError):
                return None
            result = stored.get("result")
            return {**result, "module_run_ref": canonical_reference} if isinstance(result, dict) else None

        self.operations.recover_succeeded(recovered_operation_result)
        self.runtime_telemetry = RuntimeTelemetry(self.tasks.session_event_log, self.job_registry)
        self.tool_dispatcher = ToolDispatcher(
            self.gateway, JobRunner(self.job_registry, self.job_worker), self.tasks,
            self.results, self.data_assets,
        )
        self.model_gateway = ModelGateway(self.settings_for, self.provider_registry, self.secret_provider)
        self.agent_run_concurrency_gate = AgentRunConcurrencyGate(6)
        self.agent_run_settlement_registry = AgentRunSettlementRegistry(self.tasks.session_event_log)
        def settled_operation_state(operation_id: str) -> str | None:
            record = self.job_registry.get_by_operation(operation_id)
            return record.state if record is not None else None

        self.dispatch_checkpoints.recover_incomplete(settled_operation_state)
        self.conversation_tools = AppConversationToolExecutor(
            self.tool_dispatcher,
            self.registry,
            self.results,
            self.tasks,
        )
        self.agent_runtime_mode = _resolve_agent_runtime_mode(agent_runtime_mode)
        self.agent_runtime_status = _agent_runtime_public_status(self.agent_runtime_mode)
        self.recommender = RecommenderAdapter(
            gateway=self.model_gateway,
            registry=self.registry,
            task_service=self.tasks,
            tool_executor=self.conversation_tools,
            concurrency_gate=self.agent_run_concurrency_gate,
            settlement_registry=self.agent_run_settlement_registry,
            agent_runtime_mode=self.agent_runtime_mode,
            agent_runtime_session_root=self._documents._root / "agent-runtime-sessions",
        )
        self.conversation_tools.bind_recommender(self.recommender)
        conversation_context = ContextBuilder(
            self.tasks,
            self.policy,
            catalog_version=str(self.registry.manifest["catalog_version"]),
            result_store=self.results,
        )
        conversation_observations = ResultStoreObservationBuilder(self.results)
        self.conversation_agent = OptionConversationAgent(
            self.model_gateway,
            self.tasks,
            conversation_context,
            self.conversation_tools,
            conversation_observations,
            self.attachments,
            runtime_mode=self.agent_runtime_mode,
            session_root=self._documents._root / "agent-runtime-sessions",
            is_cancelled=self.tasks.is_cancelled,
        )
        self.report_delivery = ReportDelivery(self.report_editor, self.results, self.tasks, self.conversation_tools)
        self.conversation_tools.report_delivery = self.report_delivery
        self.conversations = ConversationService(
            self.tasks,
            self.model_gateway,
            report_delivery=self.report_delivery,
            agent_loop=AgentLoop(
                gateway=self.model_gateway,
                context_builder=conversation_context,
                tool_executor=self.conversation_tools,
                recommender=self.recommender,
                is_cancelled=self.tasks.is_cancelled,
                observation_builder=conversation_observations,
                conversation_agent=self.conversation_agent,
                dispatch_checkpoints=self.dispatch_checkpoints,
                conversation_session_id=self.tasks.conversation_session_id,
                visible_event_sink=self.tasks.append_visible_process_event,
            ),
        )
        self.connection_tester = ConnectionTester(self.datafetcher)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("App server is not running")
        return f"http://{self.host}:{self._httpd.server_port}"

    def start(self) -> None:
        """Run until shutdown.  Use ``start_background`` for tests or desktop startup."""
        self._ensure_httpd()
        self._httpd.serve_forever(poll_interval=0.1)

    def start_background(self) -> str:
        self._ensure_httpd()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            self._thread.start()
        return self.url

    def shutdown(self) -> None:
        close_conversations = getattr(self.conversation_agent, "close_all", None)
        if callable(close_conversations):
            close_conversations()
        close_runtimes = getattr(self.recommender, "close_all_runtimes", None)
        if callable(close_runtimes):
            close_runtimes()
        self.operations.shutdown()
        self.compute_supervisor.shutdown()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _ensure_httpd(self) -> None:
        if self._httpd is not None:
            return
        platform = self

        class Handler(_AppRequestHandler):
            app = platform

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)

    def identity_from_cookie(self, cookie_header: str | None) -> SessionIdentity:
        if not cookie_header:
            raise AuthorizationError("session", "an authenticated App session is required")
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get("optionhelper_session")
        if morsel is None:
            raise AuthorizationError("session", "an authenticated App session is required")
        try:
            binding = self.login_handler.session_binding()
            record = self.sessions.load(morsel.value, **binding)
            identity = SessionIdentity(
                principal_id=str(record["principal_id"]),
                tenant_id=str(record["tenant_id"]),
                role=Role(str(record["role"])),
                session_id=str(record["session_id"]),
                audience=str(record.get("audience", "option-helper-app")),
            )
            return self.login_handler.require_session_identity(identity)
        except (KeyError, ValueError) as error:
            raise AuthorizationError("session", "session is invalid or expired") from error

    def create_local_session(
        self,
        role: Role,
        principal_label: str | None,
        *,
        remember: bool = False,
        client_host: str = "127.0.0.1",
    ) -> SessionIdentity:
        identity = self.login_handler.authenticate_local(
            role,
            principal_label,
            client_host=client_host,
        )
        binding = self.login_handler.session_binding()
        self.sessions.save(identity, remember=remember, **binding)
        self.audit.record(AuditEvent(action="identity.local.authenticate", principal_id=identity.principal_id, outcome="succeeded", tenant_id=identity.tenant_id, decision="allow"))
        return identity

    def create_login_session(self, request: LoginRequest, *, client_host: str) -> LoginOutcome:
        outcome = self.login_handler.authenticate(request, client_host=client_host)
        self.sessions.save(
            outcome.identity,
            remember=outcome.remember,
            authentication_mode=outcome.mode,
            issuer=outcome.issuer,
            account_generation=outcome.account_generation,
        )
        self.audit.record(AuditEvent(
            action="identity.login.authenticate",
            principal_id=outcome.identity.principal_id,
            outcome="succeeded",
            tenant_id=outcome.identity.tenant_id,
            decision="allow",
        ))
        return outcome

    def close_session(self, identity: SessionIdentity) -> None:
        self.sessions.delete(identity.session_id)
        self.audit.record(AuditEvent(
            action="identity.logout",
            principal_id=identity.principal_id,
            outcome="succeeded",
            tenant_id=identity.tenant_id,
            decision="allow",
        ))


    def settings_for(self, identity: SessionIdentity) -> SettingsSnapshot:
        principal = self._principal_settings(identity)
        tenant_key = self._tenant_settings_key(identity.tenant_id)
        try:
            tenant = self.settings.load(tenant_key)
        except KeyError:
            tenant = _default_settings("admin")
            self.settings.save(tenant_key, tenant)
        tenant = self._normalized_tenant_settings(identity.tenant_id, tenant)
        principal = self._normalized_local_model_settings(identity, principal)
        local_model = principal.model_service
        active_connection_id = principal.active_model_connection_id
        local_selection = principal.default_model_selection
        if principal.model_providers and local_selection is not None:
            selected_provider = next(
                (item for item in principal.model_providers if item.provider_id == local_selection.provider_id),
                None,
            )
            selected_model = next(
                (item for item in selected_provider.models if item.model_id == local_selection.model_id and item.enabled),
                None,
            ) if selected_provider is not None else None
            if selected_provider is not None and selected_model is not None:
                local_model = ModelServiceSettings(
                    "openai-compatible", selected_provider.endpoint, selected_provider.secret_ref, selected_model.model_id,
                )
        # Legacy connections remain readable during migration, but a migrated
        # or newly configured provider catalog is the authoritative source of
        # the active model.  Otherwise an old connection can silently override
        # a model the user selected in the new UI.
        if principal.model_connections and not (principal.model_providers and local_selection is not None):
            active = next(
                (connection for connection in principal.model_connections if connection.connection_id == active_connection_id),
                principal.model_connections[0],
            )
            local_model = active.model_service
            active_connection_id = active.connection_id
        local_data = principal.data_interface
        if local_data.secret_ref is not None:
            reference = self.secret_reference(
                identity.tenant_id, "ifind", owner_id=identity.principal_id,
                revision=local_data.secret_ref.revision,
            )
            self.secret_provider.allow_reference(reference, "iFind数据凭据")
            local_data = replace(local_data, secret_ref=self._configured_reference(
                reference, local_data.secret_ref, "iFind数据凭据",
            ))
        effective_model = local_model if local_model.provider_name != "unconfigured" else tenant.model_service
        return SettingsSnapshot(
            role=identity.role.value,
            model_service=effective_model,
            data_interface=local_data if local_data.provider_name != "unconfigured" else tenant.data_interface,
            storage_export=principal.storage_export,
            preferences=principal.preferences,
            model_connections=principal.model_connections,
            active_model_connection_id=active_connection_id,
            model_providers=principal.model_providers,
            default_model_selection=local_selection,
            recommendation_execution_mode=principal.recommendation_execution_mode,
            multi_agent_recommendation_preset_id=principal.multi_agent_recommendation_preset_id,
            multi_agent_preset_role_models=principal.multi_agent_preset_role_models,
            multi_agent_preset_agent_instructions=principal.multi_agent_preset_agent_instructions,
            multi_agent_review_policy_id=principal.multi_agent_review_policy_id,
            multi_agent_review_policy_role_models=principal.multi_agent_review_policy_role_models,
        )

    def model_providers_for(self, identity: SessionIdentity) -> dict[str, Any]:
        """Expose provider-owned catalogs without credential references."""

        settings = self.settings_for(identity)
        providers = [
            {
                "provider_id": item.provider_id,
                "display_name": item.display_name,
                "endpoint": item.endpoint,
                "protocol": item.protocol,
                "scope": "device",
                "credential_configured": item.secret_ref is not None,
                "models": [
                    {
                        **_public_model_catalog_entry(model),
                        **self._settings_store.model_verification_status(
                            tenant_id=identity.tenant_id,
                            principal_id=identity.principal_id,
                            provider_id=item.provider_id,
                            model_id=model.model_id,
                            endpoint=item.endpoint,
                            secret_ref=item.secret_ref,
                        ),
                    }
                    for model in item.models
                ],
            }
            for item in settings.model_providers
        ]
        if not providers and settings.model_service.provider_name != "unconfigured" and settings.model_service.model_name:
            providers.append({
                "provider_id": "provider-default",
                "display_name": "默认Provider",
                "endpoint": settings.model_service.endpoint,
                "protocol": "openai-chat-completions",
                "scope": "provider",
                "credential_configured": settings.model_service.secret_ref is not None,
                "models": [_public_model_catalog_entry(
                    ModelCatalogEntry(settings.model_service.model_name, settings.model_service.model_name, True),
                )],
            })
        builtin = []
        for provider_id, item in built_in_provider_catalog().items():
            builtin.append({
                "provider_id": provider_id,
                "display_name": str(item["display_name"]),
                "endpoint": str(item["endpoint"]),
                "protocol": str(item["protocol"]),
                "models": [_public_model_catalog_entry(model) for model in item["models"]],
            })
        return {
            "schema": "optionhelper.model-providers.v1",
            "providers": providers,
            "builtins": builtin,
            "default_model_selection": (
                {"provider_id": settings.default_model_selection.provider_id, "model_id": settings.default_model_selection.model_id}
                if settings.default_model_selection else None
            ),
            "multi_agent_preset_role_models": {
                preset_id: {
                    role: {"provider_id": selection.provider_id, "model_id": selection.model_id}
                    for role, selection in role_models.items()
                    if role in MULTI_AGENT_RECOMMENDATION_ROLES
                }
                for preset_id, role_models in settings.multi_agent_preset_role_models.items()
            },
            "multi_agent_review_policy_role_models": {
                policy_id: {
                    role: {"provider_id": selection.provider_id, "model_id": selection.model_id}
                    for role, selection in role_models.items()
                }
                for policy_id, role_models in settings.multi_agent_review_policy_role_models.items()
            },
        }

    def model_service_for_selection(self, identity: SessionIdentity, selection: ModelSelection | None) -> ModelServiceSettings:
        """Resolve one enabled provider/model pair to a runnable settings value."""

        settings = self.settings_for(identity)
        selected = selection or settings.default_model_selection
        if selected is not None:
            provider = next((item for item in settings.model_providers if item.provider_id == selected.provider_id), None)
            model = next((item for item in provider.models if item.model_id == selected.model_id and item.enabled), None) if provider else None
            if provider is None or model is None or provider.secret_ref is None:
                raise UnavailableCapabilityError("模型服务", "所选模型未配置、已停用或已更新，请在设置中心选择可用模型后重试。")
            return ModelServiceSettings("openai-compatible", provider.endpoint, provider.secret_ref, model.model_id)
        return settings.model_service

    def record_model_connection_verification(
        self,
        identity: SessionIdentity,
        *,
        selection: ModelSelection,
        expected_reference: SecretRef,
        endpoint: str,
        probe: Mapping[str, Any],
    ) -> None:
        matched = self._settings_store.save_model_verification_for_current_reference(
            settings_key=identity.principal_id,
            tenant_id=identity.tenant_id,
            principal_id=identity.principal_id,
            provider_id=selection.provider_id,
            model_id=selection.model_id,
            endpoint=endpoint,
            secret_ref=expected_reference,
            verified=bool(probe.get("verified")),
            connected=bool(probe.get("connection_available", probe.get("initialized", False))),
            capabilities=dict(probe.get("effective", {})) if isinstance(probe.get("effective"), Mapping) else {},
        )
        if not matched:
            raise ValidationError("模型配置或API Key已变化，请重新发起连接测试")

    def model_connections_for(self, identity: SessionIdentity) -> dict[str, Any]:
        """Expose the browser contract without exposing credential refs.

        Device-owned profiles are returned first-class. If a user has not
        created one, the legacy default configuration is exposed as a
        read-compatible provider connection.
        """
        settings = self.settings_for(identity)
        if settings.model_connections:
            connections = [
                {
                    "connection_id": connection.connection_id,
                    "display_name": connection.display_name,
                    "provider_name": connection.model_service.provider_name,
                    "endpoint": connection.model_service.endpoint,
                    "model_name": connection.model_service.model_name,
                    "credential_configured": connection.model_service.secret_ref is not None,
                    "scope": "device",
                }
                for connection in settings.model_connections
            ]
            active_id = settings.active_model_connection_id
        else:
            model = settings.model_service
            connections = [] if model.provider_name == "unconfigured" or not model.model_name else [{
                "connection_id": "provider-default",
                "display_name": "默认Provider",
                "provider_name": model.provider_name,
                "endpoint": model.endpoint,
                "model_name": model.model_name,
                "credential_configured": model.secret_ref is not None,
                "scope": "provider",
            }]
            active_id = "provider-default" if connections else None
        return {
            "schema": "optionhelper.model-connections",
            "active_connection_id": active_id,
            "connections": connections,
        }

    def save_settings(self, identity: SessionIdentity, snapshot: SettingsSnapshot, capability: str) -> SettingsSnapshot:
        self.policy.require(identity.role, capability)
        before = serialize_settings(self.settings_for(identity))
        if capability in {"settings.model.write", "settings.data.write"}:
            tenant_key = self._tenant_settings_key(identity.tenant_id)
            try:
                stored = self.settings.load(tenant_key)
            except KeyError:
                stored = _default_settings("admin")
            if capability == "settings.model.write":
                reference = self.secret_reference(
                    identity.tenant_id,
                    "model",
                    revision=snapshot.model_service.secret_ref.revision if snapshot.model_service.secret_ref else None,
                ) if snapshot.model_service.secret_ref else None
                model = replace(snapshot.model_service, secret_ref=reference)
                stored = replace(stored, model_service=model)
            else:
                reference = self.secret_reference(
                    identity.tenant_id,
                    "ifind",
                    revision=snapshot.data_interface.secret_ref.revision if snapshot.data_interface.secret_ref else None,
                ) if snapshot.data_interface.secret_ref else None
                data = replace(snapshot.data_interface, secret_ref=reference)
                stored = replace(stored, data_interface=data)
            self.settings.save(tenant_key, replace(stored, role="admin"))
        elif capability == "settings.model.local.write":
            principal = self._principal_settings(identity)
            connections = tuple(
                replace(
                    connection,
                    model_service=replace(
                        connection.model_service,
                        secret_ref=self.secret_reference(
                            identity.tenant_id,
                            "model",
                            owner_id=identity.principal_id,
                            connection_id=connection.connection_id,
                            revision=connection.model_service.secret_ref.revision,
                        ) if connection.model_service.secret_ref else None,
                    ),
                )
                for connection in snapshot.model_connections
            )
            active_id = snapshot.active_model_connection_id
            active = next((connection for connection in connections if connection.connection_id == active_id), None)
            providers = tuple(
                replace(
                    provider,
                    secret_ref=self.secret_reference(
                        identity.tenant_id,
                        "model",
                        owner_id=identity.principal_id,
                        connection_id=provider.provider_id,
                        revision=provider.secret_ref.revision,
                    ) if provider.secret_ref else None,
                )
                for provider in snapshot.model_providers
            )
            selection = snapshot.default_model_selection
            principal = replace(
                principal,
                model_service=active.model_service if active is not None else ModelServiceSettings("unconfigured", ""),
                model_connections=connections,
                active_model_connection_id=active.connection_id if active is not None else None,
                model_providers=providers,
                default_model_selection=selection,
                recommendation_execution_mode=snapshot.recommendation_execution_mode,
                multi_agent_recommendation_preset_id=snapshot.multi_agent_recommendation_preset_id,
                multi_agent_preset_role_models=snapshot.multi_agent_preset_role_models,
                multi_agent_preset_agent_instructions=snapshot.multi_agent_preset_agent_instructions,
                multi_agent_review_policy_id=snapshot.multi_agent_review_policy_id,
                multi_agent_review_policy_role_models=snapshot.multi_agent_review_policy_role_models,
            )
            self.settings.save(identity.principal_id, replace(principal, role=identity.role.value))
        else:
            principal = self._principal_settings(identity)
            if capability == "settings.preferences.write":
                principal = replace(principal, preferences=snapshot.preferences)
            elif capability == "settings.storage.write":
                principal = replace(principal, storage_export=snapshot.storage_export)
            elif capability == "settings.data.local.write":
                reference = self.secret_reference(
                    identity.tenant_id, "ifind", owner_id=identity.principal_id,
                    revision=snapshot.data_interface.secret_ref.revision,
                ) if snapshot.data_interface.secret_ref else None
                principal = replace(principal, data_interface=replace(snapshot.data_interface, secret_ref=reference))
            self.settings.save(identity.principal_id, replace(principal, role=identity.role.value))
        saved = self.settings_for(identity)
        self.audit.record(
            AuditEvent(
                action="settings.save",
                principal_id=identity.principal_id,
                outcome="succeeded",
                tenant_id=identity.tenant_id,
                decision="allow",
                reference=capability,
                before_hash=_json_hash(before),
                after_hash=_json_hash(serialize_settings(saved)),
            )
        )
        return saved

    @staticmethod
    def _tenant_settings_key(tenant_id: str) -> str:
        digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:24]
        return f"tenant:{digest}"

    def secret_reference(
        self,
        tenant_id: str,
        purpose: str,
        *,
        owner_id: str | None = None,
        connection_id: str | None = None,
        revision: str | None = None,
    ) -> SecretRef:
        if purpose not in {"model", "ifind"}:
            raise ValidationError("Credential purpose is not supported")
        owner = owner_id or tenant_id
        digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:24]
        suffix = "" if connection_id in {None, "primary"} and owner_id is None else f"/{connection_id or 'primary'}"
        return SecretRef(self._secret_reference_provider, f"optionhelper/{purpose}/{digest}{suffix}", revision)

    def data_settings_capability(self, identity: SessionIdentity) -> str:
        capability = "settings.data.write" if self.policy.allows(identity.role, "settings.data.write") else "settings.data.local.write"
        self.policy.require(identity.role, capability)
        return capability

    def data_connection_verified(self, identity: SessionIdentity, reference: SecretRef | None) -> bool:
        return self._settings_store.data_verification_available(
            tenant_id=identity.tenant_id,
            principal_id=identity.principal_id,
            secret_ref=reference,
        )

    def public_settings(self, identity: SessionIdentity, snapshot: SettingsSnapshot | None = None) -> dict[str, Any]:
        """Project settings plus revision-bound, non-secret data connection state."""

        current = snapshot or self.settings_for(identity)
        public = serialize_settings_public(current)
        data = public.get("data_interface")
        if not isinstance(data, dict):
            raise ValidationError("Settings公开投影缺少数据接口")
        data.update(self._settings_store.data_verification_status(
            tenant_id=identity.tenant_id,
            principal_id=identity.principal_id,
            secret_ref=current.data_interface.secret_ref,
        ))
        return public

    def record_data_connection_verification(
        self,
        identity: SessionIdentity,
        *,
        available: bool,
        expected_reference: SecretRef | None = None,
    ) -> None:
        reference = expected_reference or self.settings_for(identity).data_interface.secret_ref
        if reference is None:
            return
        matched = self._settings_store.save_data_verification_for_current_reference(
            settings_key=(identity.principal_id if reference.key == self.secret_reference(identity.tenant_id, "ifind", owner_id=identity.principal_id).key else self._tenant_settings_key(identity.tenant_id)),
            tenant_id=identity.tenant_id,
            principal_id=identity.principal_id,
            secret_ref=reference,
            available=available,
        )
        if not matched:
            raise ValidationError("数据凭据版本已变化，请重新发起连接测试")

    def revoke_data_connection_verification(self, identity: SessionIdentity, reference: SecretRef) -> None:
        self._settings_store.revoke_data_verification(
            tenant_id=identity.tenant_id,
            principal_id=identity.principal_id,
            secret_ref=reference,
        )

    def mark_data_connection_temporarily_unavailable(
        self, identity: SessionIdentity, reference: SecretRef,
    ) -> None:
        self._settings_store.mark_data_verification_temporarily_unavailable(
            tenant_id=identity.tenant_id,
            principal_id=identity.principal_id,
            secret_ref=reference,
        )

    def _principal_settings(self, identity: SessionIdentity) -> SettingsSnapshot:
        try:
            snapshot = self.settings.load(identity.principal_id)
        except KeyError:
            snapshot = _default_settings(identity.role.value)
            self.settings.save(identity.principal_id, snapshot)
        if snapshot.role != identity.role.value:
            snapshot = replace(snapshot, role=identity.role.value)
            self.settings.save(identity.principal_id, snapshot)
        return snapshot

    def _normalized_local_model_settings(self, identity: SessionIdentity, snapshot: SettingsSnapshot) -> SettingsSnapshot:
        """Reattach valid references and safely migrate legacy model connections."""

        normalized_connections: list[ModelConnectionSettings] = []
        for connection in snapshot.model_connections:
            try:
                connection_id = _model_connection_id(connection.connection_id)
                display_name = _model_display_name(connection.display_name, connection_id)
                model = _model_from({
                    "provider_name": connection.model_service.provider_name,
                    "endpoint": connection.model_service.endpoint,
                    "model_name": connection.model_service.model_name,
                })
                if (
                    urlparse(model.endpoint).hostname == "api.deepseek.com"
                    and model.model_name in {"deepseek-chat", "deepseek-reasoner"}
                ):
                    model = replace(model, model_name="deepseek-v4-flash")
            except (ValidationError, ValueError):
                continue
            reference = self.secret_reference(
                identity.tenant_id,
                "model",
                owner_id=identity.principal_id,
                connection_id=connection_id,
                revision=connection.model_service.secret_ref.revision if connection.model_service.secret_ref else None,
            )
            self.secret_provider.allow_reference(reference, "模型服务凭据")
            model = replace(
                model,
                secret_ref=self._configured_reference(
                    reference, connection.model_service.secret_ref, "模型服务凭据",
                ),
            )
            normalized_connections.append(ModelConnectionSettings(connection_id, display_name, model))
        active_id = snapshot.active_model_connection_id
        if active_id not in {connection.connection_id for connection in normalized_connections}:
            active_id = normalized_connections[0].connection_id if normalized_connections else None
        normalized_providers: list[ModelProviderProfile] = []
        raw_providers = snapshot.model_providers
        if not raw_providers:
            # Never inspect or merge existing secrets.  Each old connection is
            # preserved as one single-model provider with the same opaque key.
            raw_providers = tuple(
                ModelProviderProfile(
                    provider_id=connection.connection_id,
                    display_name=connection.display_name,
                    endpoint=connection.model_service.endpoint,
                    secret_ref=connection.model_service.secret_ref,
                    models=(ModelCatalogEntry(connection.model_service.model_name, connection.model_service.model_name, True),),
                )
                for connection in normalized_connections
            )
        for provider in raw_providers:
            try:
                normalized_provider = _model_provider_from({
                    "provider_id": provider.provider_id,
                    "display_name": provider.display_name,
                    "endpoint": provider.endpoint,
                    "protocol": provider.protocol,
                    "models": [
                        {
                            "model_id": model.model_id,
                            "display_name": model.display_name,
                            "enabled": model.enabled,
                            "context_window": model.context_window,
                            "max_output_tokens": model.max_output_tokens,
                            "input_modalities": list(model.input_modalities),
                            "reasoning_support": model.reasoning_support,
                            "tool_calling": model.tool_calling,
                        }
                        for model in provider.models
                    ],
                })
            except (ValidationError, ValueError):
                continue
            reference = self.secret_reference(
                identity.tenant_id, "model", owner_id=identity.principal_id, connection_id=normalized_provider.provider_id,
                revision=provider.secret_ref.revision if provider.secret_ref else None,
            )
            self.secret_provider.allow_reference(reference, "模型服务凭据")
            normalized_providers.append(replace(
                normalized_provider,
                secret_ref=self._configured_reference(
                    reference, provider.secret_ref, "模型服务凭据",
                ),
            ))
        selection = snapshot.default_model_selection
        valid_selections = {
            (provider.provider_id, model.model_id)
            for provider in normalized_providers for model in provider.models if model.enabled
        }
        if selection is None and active_id:
            active_connection = next((item for item in normalized_connections if item.connection_id == active_id), None)
            if active_connection is not None:
                candidate = (active_connection.connection_id, active_connection.model_service.model_name)
                selection = ModelSelection(*candidate) if candidate in valid_selections else None
        if selection is not None and (selection.provider_id, selection.model_id) not in valid_selections:
            selection = next(
                (ModelSelection(provider.provider_id, model.model_id)
                 for provider in normalized_providers for model in provider.models if model.enabled),
                None,
            )
        normalized = replace(
            snapshot,
            model_connections=tuple(normalized_connections),
            active_model_connection_id=active_id,
            model_providers=tuple(normalized_providers),
            default_model_selection=selection,
            multi_agent_preset_role_models={
                preset_id: {
                    role: route
                    for role, route in role_models.items()
                    if role in MULTI_AGENT_RECOMMENDATION_ROLES
                    and (route.provider_id, route.model_id) in valid_selections
                }
                for preset_id, role_models in snapshot.multi_agent_preset_role_models.items()
                if preset_id in set(_RECOMMENDATION_MODE_IDS)
            },
            multi_agent_review_policy_role_models={
                policy_id: {
                    role: route
                    for role, route in role_models.items()
                    if (route.provider_id, route.model_id) in valid_selections
                }
                for policy_id, role_models in snapshot.multi_agent_review_policy_role_models.items()
                if policy_id == MULTI_AGENT_DEFAULT_REVIEW_POLICY_ID
            },
        )
        if normalized != snapshot:
            self.settings.save(identity.principal_id, normalized)
        return normalized

    def _normalized_tenant_settings(self, tenant_id: str, snapshot: SettingsSnapshot) -> SettingsSnapshot:
        model_reference = self.secret_reference(
            tenant_id,
            "model",
            revision=snapshot.model_service.secret_ref.revision if snapshot.model_service.secret_ref else None,
        )
        data_reference = self.secret_reference(
            tenant_id,
            "ifind",
            revision=snapshot.data_interface.secret_ref.revision if snapshot.data_interface.secret_ref else None,
        )
        self.secret_provider.allow_reference(model_reference, "模型服务凭据")
        self.secret_provider.allow_reference(data_reference, "iFind数据凭据")
        model = snapshot.model_service
        if model.provider_name == "deepseek-compatible":
            model = replace(model, provider_name="openai-compatible", model_name=model.model_name or "deepseek-v4-flash")
        elif model.provider_name == "kimi-compatible":
            model = (
                replace(model, provider_name="openai-compatible")
                if model.model_name.strip()
                else ModelServiceSettings("unconfigured", "")
            )
        if model.provider_name != "unconfigured":
            try:
                endpoint = validate_openai_base_url(model.endpoint)
            except ValidationError:
                model = ModelServiceSettings("unconfigured", "")
            else:
                if model.provider_name != "openai-compatible" or not model.model_name.strip():
                    model = ModelServiceSettings("unconfigured", "")
                else:
                    model = replace(model, endpoint=endpoint)
        # DeepSeek retired the legacy chat/reasoner aliases after the V4
        # rollout.  Migrate only the official DeepSeek endpoint; arbitrary
        # OpenAI-compatible gateways may still intentionally expose their own
        # model ids and must remain untouched.
        if (
            model.provider_name == "openai-compatible"
            and urlparse(model.endpoint).hostname == "api.deepseek.com"
            and model.model_name in {"deepseek-chat", "deepseek-reasoner"}
        ):
            model = replace(model, model_name="deepseek-v4-flash")
        if model.provider_name != "unconfigured" and model.secret_ref != model_reference:
            model = replace(
                model,
                secret_ref=self._configured_reference(
                    model_reference, snapshot.model_service.secret_ref, "模型服务凭据",
                ),
            )
        data = snapshot.data_interface
        if data.provider_name not in {"unconfigured", "ifind-http"}:
            data = DataInterfaceSettings("unconfigured")
        if data.provider_name != "unconfigured" and data.secret_ref != data_reference:
            data = replace(
                data,
                secret_ref=self._configured_reference(
                    data_reference, snapshot.data_interface.secret_ref, "iFind数据凭据",
                ),
            )
        normalized = replace(snapshot, role="admin", model_service=model, data_interface=data)
        if normalized != snapshot:
            self.settings.save(self._tenant_settings_key(tenant_id), normalized)
        return normalized

    def _configured_reference(
        self,
        target: SecretRef,
        existing: SecretRef | None,
        purpose: str,
    ) -> SecretRef | None:
        """Prefer verified local storage without discarding a pending legacy ref."""

        self.secret_provider.allow_reference(target, purpose)
        if self._credential_is_saved(target, purpose):
            return target
        if existing is not None and existing.provider in {"keychain", "credential-manager"}:
            self.secret_provider.allow_reference(existing, purpose)
            return existing
        return None

    def _credential_is_saved(self, reference: SecretRef, purpose: str) -> bool:
        """Reattach one exact Host-owned reference after a development rebuild."""

        try:
            self.secret_provider.resolve(reference, purpose)
        except UnavailableCapabilityError:
            return False
        return True

    def save_credential(
        self,
        identity: SessionIdentity,
        reference: SecretRef,
        value: str,
        purpose: str,
        snapshot: SettingsSnapshot,
        capability: str,
    ) -> SettingsSnapshot:
        """Store one Host-owned credential and restore it if settings persistence fails."""

        self.policy.require(identity.role, capability)
        self.secret_provider.allow_reference(reference, purpose)
        previous: str | None = None
        try:
            previous = self.secret_provider.resolve(reference, purpose)
        except UnavailableCapabilityError as error:
            if not _is_missing_credential(error):
                raise
        self.secret_provider.store(reference, value, purpose)
        try:
            return self.save_settings(identity, snapshot, capability)
        except Exception:
            if previous is None:
                self.secret_provider.delete(reference, purpose)
            else:
                self.secret_provider.store(reference, previous, purpose)
            raise

    def local_model_snapshot(
        self,
        identity: SessionIdentity,
        connection_id: str,
        display_name: str,
        model: ModelServiceSettings,
        *,
        secret_ref: SecretRef | None = None,
    ) -> SettingsSnapshot:
        """Build a device profile update without accepting a plaintext key."""

        current = self.settings_for(identity)
        existing = next((item for item in current.model_connections if item.connection_id == connection_id), None)
        if secret_ref is None and existing is not None:
            secret_ref = existing.model_service.secret_ref
        profile = ModelConnectionSettings(
            connection_id,
            display_name,
            replace(model, secret_ref=secret_ref),
        )
        connections = [item for item in current.model_connections if item.connection_id != connection_id]
        connections.append(profile)
        # Keep the deprecated one-model endpoint behavior coherent with the
        # provider catalog migration.  Otherwise adding a second legacy
        # connection leaves the first migrated provider selected forever.
        provider = ModelProviderProfile(
            provider_id=connection_id,
            display_name=display_name,
            endpoint=model.endpoint,
            secret_ref=secret_ref,
            models=(ModelCatalogEntry(model.model_name, model.model_name, True),),
        )
        providers = [item for item in current.model_providers if item.provider_id != connection_id]
        providers.append(provider)
        return replace(
            current,
            model_service=profile.model_service,
            model_connections=tuple(connections),
            active_model_connection_id=connection_id,
            model_providers=tuple(providers),
            default_model_selection=ModelSelection(connection_id, model.model_name),
        )

    def local_provider_snapshot(
        self,
        identity: SessionIdentity,
        profile: ModelProviderProfile,
        *,
        secret_ref: SecretRef | None = None,
        default_model_id: str | None = None,
    ) -> SettingsSnapshot:
        """Upsert one provider while preserving its credential unless replaced."""

        current = self.settings_for(identity)
        existing = next((item for item in current.model_providers if item.provider_id == profile.provider_id), None)
        if secret_ref is None and existing is not None:
            secret_ref = existing.secret_ref
        updated = replace(profile, secret_ref=secret_ref)
        providers = [item for item in current.model_providers if item.provider_id != updated.provider_id]
        providers.append(updated)
        eligible = {model.model_id for model in updated.models if model.enabled}
        first_enabled = next((model.model_id for model in updated.models if model.enabled), "")
        selected_id = default_model_id or (
            current.default_model_selection.model_id
            if current.default_model_selection and current.default_model_selection.provider_id == updated.provider_id
            else first_enabled
        )
        if selected_id not in eligible:
            raise ValidationError("默认模型必须是当前提供方中已启用的模型")
        valid_selections = {
            (provider.provider_id, model.model_id)
            for provider in providers for model in provider.models if model.enabled
        }
        role_models = {
            preset_id: {
                role: selection for role, selection in mappings.items()
                if role in MULTI_AGENT_RECOMMENDATION_ROLES
                and (selection.provider_id, selection.model_id) in valid_selections
            }
            for preset_id, mappings in current.multi_agent_preset_role_models.items()
        }
        review_role_models = {
            policy_id: {
                role: selection for role, selection in mappings.items()
                if (selection.provider_id, selection.model_id) in valid_selections
            }
            for policy_id, mappings in current.multi_agent_review_policy_role_models.items()
        }
        return replace(
            current,
            model_providers=tuple(providers),
            default_model_selection=ModelSelection(updated.provider_id, selected_id),
            multi_agent_preset_role_models=role_models,
            multi_agent_review_policy_role_models=review_role_models,
        )


class _AppRequestHandler(BaseHTTPRequestHandler):
    app: AppServer
    server_version = "OptionHelperApp"

    def log_message(self, format: str, *args: object) -> None:
        # Access logs can contain query text.  Audit only explicit, redacted events.
        return

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        request_id = self.headers.get("X-Request-Id") or "local-request"
        parsed = urlparse(self.path)
        try:
            if method == "GET":
                self._get(parsed, request_id)
            else:
                self._post(parsed, request_id)
        except AuthorizationError as error:
            self._audit_denial(error, request_id)
            # Authentication failures must remain distinguishable from an
            # authenticated caller being denied a capability.  The login page
            # can then give the user a precise, non-sensitive credential hint
            # without weakening authorization failures elsewhere.
            if error.capability == "account.login":
                self._json(HTTPStatus.UNAUTHORIZED, {
                    "error": "unauthorized",
                    "capability": error.capability,
                    "reason": "账号或密码不正确",
                })
            else:
                self._json(HTTPStatus.FORBIDDEN, {
                    "error": "forbidden",
                    "capability": error.capability,
                    "reason": "请求未获授权",
                })
        except UnavailableCapabilityError as error:
            public_message = _unavailable_public_message(error)
            diagnostic_id = _diagnostic_id(
                self.headers.get("X-OptionHelper-Request-Id") or request_id,
                error.failure_code,
                error.stage,
            )
            self._audit_operational_failure(
                request_id,
                failure_code=error.failure_code,
                stage=error.stage,
                reference=error.capability,
            )
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {
                "error": "unavailable",
                "capability": error.capability,
                "failure_code": error.failure_code,
                "stage": error.stage,
                "message": public_message,
                "next_step": error.next_step,
                "diagnostic_id": diagnostic_id,
            })
        except UserActionError as error:
            diagnostic_id = _diagnostic_id(
                self.headers.get("X-OptionHelper-Request-Id") or request_id,
                error.code,
                error.stage,
            )
            self._audit_operational_failure(
                request_id,
                failure_code=error.code,
                stage=error.stage,
            )
            self._json(HTTPStatus.CONFLICT, {
                "ok": False,
                "error": error.code,
                "failure_code": error.code,
                "stage": error.stage,
                "message": error.message,
                "next_step": error.next_step,
                "retryable": error.retryable,
                "diagnostic_id": diagnostic_id,
                **error.details,
            })
        except (ValidationError, ValueError) as error:
            authentication_input = parsed.path == "/api/auth/login"
            validation_detail = _safe_public_validation_message(error)
            diagnostic_id = _diagnostic_id(
                self.headers.get("X-OptionHelper-Request-Id") or request_id,
                "authentication_input_invalid" if authentication_input else "input_invalid",
                "authentication" if authentication_input else "input",
            )
            failure_code = "authentication_input_invalid" if authentication_input else "input_invalid"
            failure_stage = "authentication" if authentication_input else "input"
            self._audit_operational_failure(request_id, failure_code=failure_code, stage=failure_stage)
            self._json(HTTPStatus.BAD_REQUEST, {
                "error": "invalid_request",
                "failure_code": failure_code,
                "stage": failure_stage,
                "message": (
                    "请检查账号和密码后重试。"
                    if authentication_input else
                    "本次输入不完整或格式不正确。请检查日期、条款和定价参数后重试。"
                ),
                "detail": "认证输入不符合要求" if authentication_input else validation_detail,
                "next_step": (
                    "请完整填写账号和密码。"
                    if authentication_input else
                    "请检查日期、标的、条款与定价或回测参数后重试。"
                ),
                "diagnostic_id": diagnostic_id,
            })
        except KeyError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found", "detail": "请求的受控对象不存在"})
        except CapabilityIntegrityError:
            diagnostic_id = _diagnostic_id(request_id, "capability_integrity_error", "capability")
            self._audit_operational_failure(request_id, failure_code="capability_integrity_error", stage="capability")
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {
                "error": "capability_integrity_error",
                "failure_code": "capability_integrity_error",
                "stage": "capability",
                "detail": "内置能力完整性校验未通过",
                "next_step": "请重新安装完整的OptionHelper应用后重试。",
                "diagnostic_id": diagnostic_id,
            })
        except Exception as error:  # pragma: no cover - safety boundary
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error", "detail": type(error).__name__})

    def _get(self, parsed: Any, request_id: str) -> None:
        path = parsed.path
        if path in {"/health", "/api/health"}:
            self._json(HTTPStatus.OK, {"status": "ok", "mode": "local", "capability": _capability_summary(self.app.registry)})
            return
        if path == "/":
            self._serve_frontend_asset("login/index.html")
            return
        if path == "/app/designer-token-vars.css":
            content, content_type = self.app.registry.read_asset("assets/designer/themes/designer-token-vars.css")
            self._bytes(HTTPStatus.OK, content, content_type, extra_headers={
                "Content-Security-Policy": "default-src 'none'; style-src 'self'; base-uri 'none'; object-src 'none'",
            })
            return
        if path.startswith("/app/frontend/"):
            self._serve_frontend_asset(path.removeprefix("/app/frontend/"))
            return
        if path == "/api/me":
            self.app.login_handler.prepare_accounts()
            identity = self._identity()
            self._json(HTTPStatus.OK, {"identity": identity.public(), "capabilities": _capabilities_for(self.app.policy, identity.role)})
            return
        if path == "/api/capability/status":
            identity = self._identity()
            self._json(HTTPStatus.OK, {"status": "verified", "caller": identity.public(), "capability": _capability_summary(self.app.registry)})
            return
        if path == "/api/tasks":
            identity = self._identity()
            self.app.policy.require(identity.role, "task.read")
            tasks = self.app.tasks.list(identity)
            active = self.app.operations.active_summaries(identity, [str(task["task_id"]) for task in tasks])
            self._json(HTTPStatus.OK, {"tasks": [{**task, "active_operations": active.get(str(task["task_id"]), [])} for task in tasks]})
            return
        if path == "/api/runtime/active-operations":
            identity = self._identity()
            self.app.policy.require(identity.role, "task.read")
            tasks = self.app.tasks.list(identity)
            active = self.app.operations.active_summaries(identity, [str(task["task_id"]) for task in tasks])
            self._json(HTTPStatus.OK, {"active_count": sum(len(value) for value in active.values())})
            return
        if path.startswith("/api/tasks/") and path.endswith("/operations"):
            identity = self._identity()
            self.app.policy.require(identity.role, "task.read")
            task_id = path.removeprefix("/api/tasks/").removesuffix("/operations").rstrip("/")
            self.app.tasks.get(identity, task_id)
            include_result = parse_qs(parsed.query).get("include_result", ["false"])[0].strip().casefold() in {"1", "true", "yes"}
            self._json(HTTPStatus.OK, {
                "operations": [
                    item.public(include_result=include_result)
                    for item in self.app.operations.list(identity, task_id)
                ],
            })
            return
        if path.startswith("/api/tasks/") and path.endswith("/result") and "/operations/" in path:
            identity = self._identity()
            self.app.policy.require(identity.role, "task.read")
            task_id, operation_id = path.removeprefix("/api/tasks/").removesuffix("/result").split("/operations/", 1)
            if not task_id or not operation_id or "/" in operation_id:
                raise KeyError(path)
            self.app.tasks.get(identity, task_id)
            operation = self.app.operations.get(identity, operation_id)
            if operation.task_id != task_id:
                raise KeyError(path)
            if operation.state == "succeeded":
                self._json(HTTPStatus.OK, operation.result or {})
                return
            if operation.state in {"failed", "cancelled", "interrupted"}:
                status = HTTPStatus.INTERNAL_SERVER_ERROR if operation.state == "failed" else HTTPStatus.CONFLICT
                self._json(status, {
                    **(operation.result or {}),
                    "ok": False,
                    "error": operation.state,
                    "message": operation.message or "后台运行未完成。",
                })
                return
            self._json(HTTPStatus.ACCEPTED, {"operation": operation.public(include_result=False)})
            return
        if path.startswith("/api/tasks/") and "/operations/" in path:
            identity = self._identity()
            self.app.policy.require(identity.role, "task.read")
            task_id, operation_id = path.removeprefix("/api/tasks/").split("/operations/", 1)
            if not task_id or not operation_id or "/" in operation_id:
                raise KeyError(path)
            self.app.tasks.get(identity, task_id)
            operation = self.app.operations.get(identity, operation_id)
            if operation.task_id != task_id:
                raise KeyError(path)
            include_result = parse_qs(parsed.query).get("include_result", ["true"])[0].strip().casefold() not in {"0", "false", "no"}
            self._json(HTTPStatus.OK, {"operation": operation.public(include_result=include_result)})
            return
        if path.startswith("/api/tasks/") and path.endswith("/reports"):
            identity = self._identity()
            self.app.policy.require(identity.role, "task.read")
            task_id = path.removeprefix("/api/tasks/").removesuffix("/reports").rstrip("/")
            self.app.tasks.get(identity, task_id)
            self._json(HTTPStatus.OK, {"reports": self.app.results.list_report_runs(identity, task_id)})
            return
        if path.startswith("/api/tasks/") and path.endswith("/attachment-drafts"):
            identity = self._identity()
            self.app.policy.require(identity.role, "task.read")
            task_id = path.removeprefix("/api/tasks/").removesuffix("/attachment-drafts").rstrip("/")
            self.app.tasks.get(identity, task_id)
            self._json(HTTPStatus.OK, {"attachments": self.app.tasks.pending_attachment_uploads(identity, task_id)})
            return
        if path.startswith("/api/tasks/") and "/attachments/" in path:
            identity = self._identity()
            self.app.policy.require(identity.role, "task.read")
            task_id, encoded_attachment_id = path.removeprefix("/api/tasks/").split("/attachments/", 1)
            attachment_id = unquote(encoded_attachment_id)
            if not task_id or not attachment_id or "/" in attachment_id:
                raise KeyError(path)
            # Drafts are already persisted before the composer renders them.
            # Resolve only references registered to this owned task, never an
            # arbitrary content hash from the attachment store.
            reference = next((
                item for item in self.app.tasks.pending_attachment_uploads(identity, task_id)
                if item.get("attachment_id") == attachment_id
            ), None)
            if reference is None:
                reference = self.app.tasks.attachment_reference(identity, task_id, attachment_id)
            content = self.app.attachments.read(reference)
            media_type = str(reference.get("media_type", "application/octet-stream"))
            disposition = "inline" if reference.get("kind") == "image" else "attachment"
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(reference.get("name", "attachment")))[:120]
            self._bytes(HTTPStatus.OK, content, media_type, extra_headers={
                "Content-Disposition": f'{disposition}; filename="{safe_name or "attachment"}"',
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "X-Frame-Options": "DENY",
            })
            return
        if path.startswith("/api/tasks/") and "/conversation-requests/" in path and path.endswith("/events"):
            identity = self._identity()
            self.app.policy.require(identity.role, "conversation.write")
            task_id, request_id = path.removeprefix("/api/tasks/").split("/conversation-requests/", 1)
            request_id = request_id.removesuffix("/events").rstrip("/")
            if not task_id or not request_id or "/" in request_id:
                raise KeyError(path)
            raw_after_seq = parse_qs(parsed.query).get("after_seq", ["0"])[0]
            try:
                after_seq = int(str(raw_after_seq))
            except (TypeError, ValueError) as error:
                raise ValidationError("Conversation process event cursor is invalid") from error
            self._json(HTTPStatus.OK, self.app.tasks.visible_process_events(
                identity, task_id, request_id, after_seq=after_seq,
            ))
            return
        if path.startswith("/api/tasks/") and "/conversation-requests/" in path:
            identity = self._identity()
            self.app.policy.require(identity.role, "conversation.write")
            task_id, request_id = path.removeprefix("/api/tasks/").split("/conversation-requests/", 1)
            if not task_id or not request_id or "/" in request_id:
                raise KeyError(path)
            response = self.app.tasks.get_conversation_response_by_id(identity, task_id, request_id)
            if response is None:
                raise KeyError(path)
            self._json(HTTPStatus.OK, {"response": response})
            return
        if path.startswith("/api/reports/") and "/document-artifacts/" in path:
            from urllib.parse import quote
            identity = self._identity()
            report_id, name = path.removeprefix("/api/reports/").split("/document-artifacts/", 1)
            content, content_type = self.app.results.read_report_document_artifact(identity, report_id, name)
            document = self.app.results.get_report_document(identity, report_id)
            title = re.sub(r'[\\/:*?"<>|]', "_", document["title"])
            filename = quote(title + Path(name).suffix, safe="")
            download = parse_qs(parsed.query).get("download", [""])[0] == "1"
            self._bytes(HTTPStatus.OK, content, content_type, extra_headers={
                "Content-Disposition": f"{'attachment' if download else 'inline'}; filename=report{Path(name).suffix}; filename*=UTF-8''{filename}",
                "Content-Security-Policy": "sandbox allow-scripts; default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'none'; frame-ancestors 'self'",
                "X-Frame-Options": "SAMEORIGIN",
            })
            return
        if path.startswith("/api/reports/") and "/artifacts/" in path:
            identity = self._identity()
            report_run_id, artifact_name = path.removeprefix("/api/reports/").split("/artifacts/", 1)
            content, content_type = self.app.results.read_report_artifact(identity, report_run_id, artifact_name)
            download = parse_qs(parsed.query).get("download", [""])[0] == "1"
            self._bytes(HTTPStatus.OK, content, content_type, extra_headers={
                "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{Path(artifact_name).name}"',
                "Content-Security-Policy": "sandbox allow-scripts; default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'none'; form-action 'none'; frame-ancestors 'self'",
                "X-Frame-Options": "SAMEORIGIN",
            })
            return
        if path.startswith("/api/reports/") and path.endswith("/editor"):
            identity = self._identity()
            self.app.policy.require(identity.role, "task.read")
            report_run_id = path.removeprefix("/api/reports/").removesuffix("/editor").rstrip("/")
            if not report_run_id or "/" in report_run_id:
                raise KeyError(path)
            self._json(HTTPStatus.OK, self.app.report_editor.editor_payload(identity, report_run_id))
            return
        if path.startswith("/api/tasks/"):
            identity = self._identity()
            self.app.policy.require(identity.role, "task.read")
            task_id = path.removeprefix("/api/tasks/")
            self._json(HTTPStatus.OK, {"task": self.app.tasks.get(identity, task_id)})
            return
        if path == "/api/settings":
            identity = self._identity()
            self.app.policy.require(identity.role, "settings.read")
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity)})
            return
        if path == "/api/settings/model-connections":
            identity = self._identity()
            self.app.policy.require(identity.role, "settings.read")
            self._json(HTTPStatus.OK, self.app.model_connections_for(identity))
            return
        if path == "/api/settings/model-providers":
            identity = self._identity()
            self.app.policy.require(identity.role, "settings.read")
            self._json(HTTPStatus.OK, self.app.model_providers_for(identity))
            return
        if path == "/api/settings/multi-agent-presets":
            identity = self._identity()
            self.app.policy.require(identity.role, "settings.read")
            settings = self.app.settings_for(identity)
            presets = recommendation_presets()
            review_policies = _review_policies()
            agent_files = {
                preset_id: dict(effective_agent_instructions(
                    preset,
                    settings.multi_agent_preset_agent_instructions.get(preset_id, {}),
                ))
                for preset_id, preset in presets.items()
                if preset_id in _RECOMMENDATION_MODE_IDS
            }
            default_agent_files = {
                preset_id: default_agent_instructions(preset_id)
                for preset_id in _RECOMMENDATION_MODE_IDS
            }
            self._json(HTTPStatus.OK, {
                **self.app.agent_runtime_status,
                "execution_mode": settings.recommendation_execution_mode,
                "selected_preset_id": settings.multi_agent_recommendation_preset_id,
                "presets": [
                    {
                        "preset_id": preset.preset_id,
                        "revision": effective_preset_revision(preset, agent_files[preset_id]),
                        "display_name": preset.display_name,
                        "execution_strategy": preset.execution_strategy,
                        "enabled": preset.enabled,
                        "disabled_reason": preset.disabled_reason,
                        "roles": _catalog_roles(
                            preset,
                            _RECOMMENDATION_MODE_ROLE_FALLBACKS.get(preset.preset_id, ()),
                        ),
                    }
                    for preset_id, preset in presets.items()
                    if preset_id in _RECOMMENDATION_MODE_IDS
                ],
                "role_models": serialize_settings_public(settings)["multi_agent_preset_role_models"],
                "agent_files": agent_files,
                "default_agent_files": default_agent_files,
                "selected_review_policy_id": settings.multi_agent_review_policy_id,
                "review_policies": [
                    {
                        "policy_id": policy_id,
                        "version": _catalog_value(policy, "version", "initial"),
                        "display_name": _catalog_value(policy, "display_name", policy_id),
                        "enabled": bool(_catalog_value(policy, "enabled", False)),
                        "disabled_reason": _catalog_value(policy, "disabled_reason"),
                        "roles": _review_policy_roles(policy_id, policy),
                    }
                    for policy_id, policy in review_policies.items()
                ],
                "review_policy_role_models": serialize_settings_public(settings)["multi_agent_review_policy_role_models"],
                "available_models": [
                    {
                        "provider_id": provider.provider_id,
                        "provider_name": provider.display_name,
                        "model_id": model.model_id,
                        "model_name": model.display_name,
                    }
                    for provider in settings.model_providers
                    for model in provider.models
                    if model.enabled and provider.secret_ref is not None
                ],
            })
            return
        if path == "/api/internal/runtime-telemetry":
            identity = self._identity()
            if identity.role is not Role.ADMIN:
                raise AuthorizationError("runtime.telemetry.read", "administrator access is required")
            self._json(HTTPStatus.OK, self.app.runtime_telemetry.snapshot())
            return
        if path.startswith("/api/module-host/"):
            identity = self._identity()
            self.app.policy.require(identity.role, "module.host_context")
            module_name = path.removeprefix("/api/module-host/")
            query = parse_qs(parsed.query)
            task_id = query.get("task_id", [None])[0]
            analysis_case_id = query.get("analysis_case_id", [None])[0]
            if task_id is not None:
                self.app.tasks.get(identity, task_id)
            self._json(HTTPStatus.OK, {
                "context": self.app.registry.host_context(
                    identity,
                    module_name,
                    analysis_case_id,
                    task_id=task_id,
                    candidate_id=None,
                    catalog_version=None,
                    product_id=None,
                    rule_revision=None,
                ),
            })
            return
        if path.startswith("/capability/"):
            relative = path.removeprefix("/capability/")
            if relative.startswith("assets/icons/"):
                self._serve_brand_asset(self._optional_identity(), relative.removeprefix("assets/icons/"))
                return
            identity = self._identity()
            self._serve_capability_asset(identity, relative)
            return
        if path.startswith("/app/assets/icons/"):
            self._serve_brand_asset(self._optional_identity(), path.removeprefix("/app/assets/icons/"))
            return
        editor_match = re.fullmatch(r"/reports/([A-Za-z0-9._:-]+)/edit", path)
        if editor_match:
            identity = self._identity()
            self.app.policy.require(identity.role, "conversation.write")
            report_id = editor_match.group(1)
            self.app.report_editor.editor_payload(identity, report_id)
            content, _ = self.app.registry.read_asset("assets/pages/reporter/reporter.html")
            html = content.decode("utf-8")
            html = html.replace('<head>', '<head><base href="/capability/assets/pages/reporter/"><style>body > .app-shell{display:none}</style>')
            html = re.sub(r'<script src="(?:\.\./module-host-(?:presentation|bridge)\.js|\./reporter\.js)"></script>', '', html)
            bootstrap = "<script>const editor=window.OptionHelperReportEditor.createReportEditor({root:document});editor.open(" + json.dumps(report_id) + ");editor.close=()=>location.assign('/optchat');</script>"
            html = html.replace('</body>', bootstrap + '</body>')
            self._bytes(HTTPStatus.OK, html.encode('utf-8'), 'text/html; charset=utf-8', extra_headers={"Content-Security-Policy":_CAPABILITY_CSP})
            return
        if path == "/optchat":
            identity = self._identity()
            self.app.policy.require(identity.role, "optchat")
            self._serve_frontend_asset("optchat/index.html")
            return
        if path == "/optdesk":
            identity = self._identity()
            self.app.policy.require(identity.role, "optdesk")
            self._serve_frontend_asset("optdesk/index.html")
            return
        if path == "/settings":
            identity = self._identity()
            self.app.policy.require(identity.role, "settings.read")
            self._serve_frontend_asset("settings/index.html")
            return
        raise KeyError(path)

    def _post(self, parsed: Any, request_id: str) -> None:
        path = parsed.path
        attachment_upload = path.startswith("/api/tasks/") and path.endswith("/attachments")
        preauthenticated_identity: SessionIdentity | None = None
        if attachment_upload:
            preauthenticated_identity = self._identity()
            self.app.policy.require(preauthenticated_identity.role, "conversation.write")
            upload_task_id = path.removeprefix("/api/tasks/").removesuffix("/attachments").rstrip("/")
            self.app.tasks.get(preauthenticated_identity, upload_task_id)
        report_edit = path.startswith("/api/reports/") and path.endswith("/edits")
        body = self._body_json(
            allowed_plaintext_fields=_credential_fields_for(path),
            max_bytes=280 * 1024 * 1024 if attachment_upload else 16 * 1024 * 1024 if report_edit or path == "/api/report-documents" else 200_000,
        )
        if path == "/api/auth/login":
            request = LoginRequest.from_payload(body)
            outcome = self.app.create_login_session(request, client_host=str(self.client_address[0]))
            self._json(
                HTTPStatus.OK,
                {"identity": outcome.identity.public(), "mode": outcome.mode},
                cookie=outcome.identity.session_id,
                persistent_cookie=outcome.remember,
            )
            return
        if path == "/api/auth/logout":
            _only_fields(body, set())
            identity = self._identity()
            close_runtime = getattr(self.app.recommender, "close_task_runtime", None)
            for task in self.app.tasks.list(identity):
                task_id = str(task.get("task_id", ""))
                if not task_id:
                    continue
                self.app.operations.cancel_task(identity, task_id)
                if callable(close_runtime):
                    close_runtime(identity, task_id, reason="signed_out")
            self.app.close_session(identity)
            self._json(HTTPStatus.OK, {"status": "signed_out"}, clear_cookie=True)
            return
        if path == "/api/auth/local":
            if not self.app.login_handler.is_local_development:
                raise UnavailableCapabilityError(
                    "account.login",
                    "正式运行不提供本地开发身份；请等待管理员完成账号服务接入。",
                )
            self.app.login_handler.require_local_peer(str(self.client_address[0]))
            role_raw = body.get("role", "sales")
            try:
                role = Role(str(role_raw))
            except ValueError as error:
                raise ValidationError("使用身份必须为用户或管理员") from error
            label = body.get("principal_label")
            if label is not None and not isinstance(label, str):
                raise ValidationError("principal_label must be a string")
            identity = self.app.create_local_session(
                role,
                label,
                client_host=str(self.client_address[0]),
            )
            self._json(HTTPStatus.OK, {"identity": identity.public(), "mode": "local-development"}, cookie=identity.session_id)
            return
        if path == "/api/report-documents":
            identity = self._identity()
            self.app.policy.require(identity.role, "conversation.write")
            self._json(HTTPStatus.CREATED, self.app.report_editor.import_document(identity, body))
            return
        if report_edit:
            identity = self._identity()
            self.app.policy.require(identity.role, "conversation.write")
            source_report_run_id = path.removeprefix("/api/reports/").removesuffix("/edits").rstrip("/")
            if not source_report_run_id or "/" in source_report_run_id:
                raise KeyError(path)
            self._json(
                HTTPStatus.CREATED,
                self.app.report_editor.save_edit(identity, source_report_run_id, body),
            )
            return
        identity = preauthenticated_identity or self._identity()
        data_asset_id = _data_asset_download_id(path)
        if data_asset_id is not None:
            _only_fields(body, set())
            self.app.policy.require(identity.role, "module.run")
            module_context = self._module_host_context("datafetcher")
            verified = self.app.registry.validate_host_request(
                identity,
                "datafetcher",
                module_context,
                self.headers.get("X-OptionHelper-Request-Id", ""),
            )
            if "module.run" not in verified.request_policy:
                raise AuthorizationError("module.run", "Module Host context is not issued for downloads")
            if not verified.task_id:
                raise ValidationError("DataAsset download must be bound to an App task")
            self.app.tasks.get(identity, verified.task_id)
            registered = self.app.data_assets.get(identity, data_asset_id)
            if registered.get("created_by") != identity.principal_id or "read" not in registered.get("access_scope", []):
                raise AuthorizationError("data.read", "data asset is not owned by current caller")
            reference, content = self.app.datafetcher.read_asset(
                data_asset_id,
                identity,
                request_id=self.headers.get("X-OptionHelper-Request-Id", ""),
            )
            for field in ("data_asset_id", "tenant_id", "created_by", "content_hash", "media_type", "schema_id"):
                if reference.get(field) != registered.get(field):
                    raise ValidationError(f"DataAsset download {field} does not match its App registration")
            media_type = str(reference["media_type"]).split(";", 1)[0].strip().lower()
            schema_id = str(reference.get("schema_id", "")).strip()
            if media_type == "text/csv":
                extension, response_type = "csv", "text/csv; charset=utf-8"
            elif media_type == "application/json" and schema_id == "trading-calendar":
                extension, response_type = "json", "application/json; charset=utf-8"
            else:
                raise ValidationError("DataFetcher page downloads only registered CSV or trading-calendar JSON assets")
            self.app.audit.record(AuditEvent(
                action="data_asset.download",
                principal_id=identity.principal_id,
                tenant_id=identity.tenant_id,
                outcome="succeeded",
                decision="allow",
                reference=data_asset_id,
                request_id=request_id,
            ))
            self._bytes(HTTPStatus.OK, content, response_type, extra_headers={
                "Content-Disposition": f'attachment; filename="{data_asset_id}.{extension}"',
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "X-Frame-Options": "DENY",
            })
            return
        if path == "/api/tasks":
            self.app.policy.require(identity.role, "task.create")
            task = self.app.tasks.create(identity, str(body.get("subject", "")))
            self.app.audit.record(AuditEvent(action="task.create", principal_id=identity.principal_id, tenant_id=identity.tenant_id, outcome="succeeded", decision="allow", reference=task["task_id"], request_id=request_id))
            self._json(HTTPStatus.CREATED, {"task": task})
            return
        if path == "/api/runtime/shutdown":
            self.app.policy.require(identity.role, "task.read")
            _only_fields(body, set())
            count = self.app.operations.interrupt_active("用户确认退出OptionHelper，未完成运行已中断。")
            self._json(HTTPStatus.OK, {"interrupted": count})
            return
        if path.startswith("/api/tasks/") and path.endswith("/operations"):
            self.app.policy.require(identity.role, "module.run")
            task_id = path.removeprefix("/api/tasks/").removesuffix("/operations").rstrip("/")
            self.app.tasks.get(identity, task_id)
            _only_fields(body, {"module", "action", "payload"})
            module = str(body.get("module", "")).strip()
            action = str(body.get("action", "")).strip().lower()
            payload = body.get("payload")
            allowed_actions = {"fetch", "run"}
            if module == "reporter":
                allowed_actions.add("rerender")
            if module not in PAGE_MODULES or action not in allowed_actions or not isinstance(payload, dict):
                raise ValidationError("Task operation request is invalid")
            module_request_id = self.headers.get("X-OptionHelper-Request-Id", "")
            module_context = self._module_host_context(module)
            verified_operation_context = self.app.registry.validate_host_request(
                identity,
                module,
                module_context,
                module_request_id,
                consume_request_id=False,
            )
            if verified_operation_context.task_id != task_id:
                raise ValidationError("Module Host context does not match task operation")
            controlled_payload = {**payload, "action": action, "task_id": task_id}
            if module == "reporter" and action == "rerender":
                source_run_id = payload.get("source_report_run_id")
                if not isinstance(source_run_id, str) or not source_run_id:
                    raise ValidationError("Reporter PDF交付缺少source_report_run_id")
                try:
                    source_run = self.app.results.get_owned_report_run(identity, source_run_id)
                except (KeyError, AuthorizationError) as error:
                    raise ValidationError("源报告不存在或不可访问") from error
                if source_run.get("task_id") != task_id:
                    raise ValidationError("源报告不属于当前任务")
                source_request = source_run.get("report_request")
                kind = str(source_request.get("output_type", "")).strip().lower() if isinstance(source_request, dict) else ""
                capability = {"card": "report.card.request", "quote": "report.quote.request", "report": "report.full.request"}.get(kind)
                if capability is None:
                    raise ValidationError("源交付类型不支持另存")
                self.app.policy.require(identity.role, capability)
                controlled_payload = {
                    "action": "rerender",
                    "task_id": task_id,
                    "source_report_run_id": source_run_id,
                    "output_type": kind,
                    "format": payload.get("format"),
                    "report_run_id": payload.get("report_run_id"),
                }
            elif module == "reporter":
                selection = controlled_payload.get("selection")
                if not isinstance(selection, dict):
                    raise ValidationError("Reporter page requires one controlled selection")
                source_id = selection.get("source_id")
                if not isinstance(source_id, str) or not source_id:
                    raise ValidationError("selection.source_id is required")
                source = self.app.results.get_owned_report_source(identity, source_id)
                kind = str(selection.get("output_type", ""))
                capability = {"card": "report.card.request", "quote": "report.quote.request", "report": "report.full.request"}.get(kind)
                if capability is None:
                    raise ValidationError("selection.output_type must be card, quote or report")
                self.app.policy.require(identity.role, capability)
                controlled_payload = {"action": "run", "task_id": source["task_id"], "kind": kind, "selection": selection}
            operation = self.app.operations.submit(
                identity,
                task_id=task_id,
                module=module,
                kind=action,
                operation=lambda cancelled: _dispatch_background_tool(
                    self.app.tool_dispatcher, module, controlled_payload, identity,
                    module_context=self.app.registry.renew_task_operation_context(
                        identity, verified_operation_context,
                    ),
                    request_id=module_request_id,
                    cancelled=cancelled,
                ),
                request_id=module_request_id or None,
                input_hash=_json_hash(controlled_payload) if module_request_id else None,
                lane="compute" if module in {"payoffer", "pricer", "backtester"} else "control",
            )
            self.app.audit.record(AuditEvent(action="task.operation.submit", principal_id=identity.principal_id, tenant_id=identity.tenant_id, outcome="queued", decision="allow", reference=operation.operation_id, request_id=request_id))
            self._json(HTTPStatus.ACCEPTED, {"operation": operation.public()})
            return
        if path.startswith("/api/tasks/") and path.endswith("/cancel") and "/operations/" in path:
            self.app.policy.require(identity.role, "conversation.write")
            task_id, operation_id = path.removeprefix("/api/tasks/").removesuffix("/cancel").split("/operations/", 1)
            if not task_id or not operation_id or "/" in operation_id:
                raise KeyError(path)
            self.app.tasks.get(identity, task_id)
            _only_fields(body, set())
            operation = self.app.operations.cancel_for_task(identity, task_id, operation_id)
            self._json(HTTPStatus.OK, {"operation": operation.public()})
            return
        if path.startswith("/api/tasks/") and path.endswith("/rename"):
            self.app.policy.require(identity.role, "conversation.write")
            task_id = path.removeprefix("/api/tasks/").removesuffix("/rename").rstrip("/")
            _only_fields(body, {"subject"})
            task = self.app.tasks.rename(identity, task_id, str(body.get("subject", "")))
            self.app.audit.record(AuditEvent(action="task.rename", principal_id=identity.principal_id, tenant_id=identity.tenant_id, outcome="succeeded", decision="allow", reference=task_id, request_id=request_id))
            self._json(HTTPStatus.OK, {"task": task})
            return
        if path.startswith("/api/tasks/") and path.endswith("/delete"):
            self.app.policy.require(identity.role, "conversation.write")
            task_id = path.removeprefix("/api/tasks/").removesuffix("/delete").rstrip("/")
            _only_fields(body, set())
            stopping = self.app.operations.cancel_task(identity, task_id)
            if any(operation.state in {"queued", "running", "recovering", "cancel_requested"} for operation in stopping):
                raise UserActionError(
                    "task_operations_stopping",
                    "当前任务仍有正在停止的后台运行，暂不能删除。",
                    stage="task",
                    next_step="请等待后台运行停止后，再删除当前任务。",
                )
            close_runtime = getattr(self.app.recommender, "close_task_runtime", None)
            if callable(close_runtime):
                close_runtime(identity, task_id, reason="task_deleted")
            task = self.app.tasks.delete(identity, task_id)
            self.app.audit.record(AuditEvent(action="task.delete", principal_id=identity.principal_id, tenant_id=identity.tenant_id, outcome="succeeded", decision="allow", reference=task_id, request_id=request_id))
            self._json(HTTPStatus.OK, {"task": task})
            return
        if path.startswith("/api/tasks/") and path.endswith("/attachments"):
            task_id = path.removeprefix("/api/tasks/").removesuffix("/attachments").rstrip("/")
            _only_fields(body, {"files"})
            files = body.get("files")
            if not isinstance(files, list):
                raise ValidationError("附件上传必须包含files数组")
            self.app.tasks.reclaim_orphaned_attachments()
            references = self.app.attachments.save_encoded(files)
            references = self.app.tasks.register_attachment_uploads(identity, task_id, references)
            self._json(HTTPStatus.CREATED, {"attachments": references})
            return
        if path.startswith("/api/tasks/") and path.endswith("/attachment-drafts/discard"):
            self.app.policy.require(identity.role, "conversation.write")
            task_id = path.removeprefix("/api/tasks/").removesuffix("/attachment-drafts/discard").rstrip("/")
            _only_fields(body, {"attachment_ids"})
            attachment_ids = body.get("attachment_ids")
            if not isinstance(attachment_ids, list):
                raise ValidationError("Attachment draft identifiers must be an array")
            removed = self.app.tasks.discard_attachment_uploads(identity, task_id, attachment_ids)
            self._json(HTTPStatus.OK, {"removed": removed})
            return
        if path.startswith("/api/tasks/") and path.endswith("/messages"):
            self.app.policy.require(identity.role, "conversation.write")
            task_id = path.removeprefix("/api/tasks/").removesuffix("/messages").rstrip("/")
            _only_fields(body, {"content", "attachments", "model_selection", "background"})
            attachments = body.get("attachments", [])
            if not isinstance(attachments, list):
                raise ValidationError("消息附件必须是数组")
            for reference in attachments:
                if not isinstance(reference, Mapping):
                    raise ValidationError("消息附件引用无效")
                self.app.attachments.read(reference)
            model_selection = _conversation_model_selection(body.get("model_selection"))
            if body.get("background") is True:
                content = str(body.get("content", ""))
                conversation_request_id = self.headers.get("X-Request-Id")
                operation = self.app.operations.submit(
                    identity,
                    task_id=task_id,
                    module="optchat",
                    kind="conversation",
                    operation=lambda cancelled: _dispatch_background_conversation(
                        self.app.conversations, identity, task_id, content,
                        request_id=conversation_request_id, selection=model_selection,
                        attachments=attachments, cancelled=cancelled,
                    ),
                    lane="conversation",
                )
                self._json(HTTPStatus.ACCEPTED, {"operation": operation.public()})
                return
            response = self.app.conversations.respond(
                identity,
                task_id,
                str(body.get("content", "")),
                request_id=self.headers.get("X-Request-Id"),
                selection=model_selection,
                attachments=attachments,
            )
            selection_note = (
                f"{model_selection.provider_id}/{model_selection.model_id}"
                if model_selection is not None else response.get("error", {}).get("capability")
            )
            self.app.audit.record(AuditEvent(action="conversation.model_request", principal_id=identity.principal_id, tenant_id=identity.tenant_id, outcome=response["status"], decision="allow", reference=task_id, request_id=request_id, reason=selection_note))
            status = HTTPStatus.SERVICE_UNAVAILABLE if response["status"] == "unavailable" else HTTPStatus.OK
            self._json(status, response)
            return
        if path.startswith("/api/tasks/") and path.endswith("/cancel"):
            self.app.policy.require(identity.role, "conversation.write")
            task_id = path.removeprefix("/api/tasks/").removesuffix("/cancel").rstrip("/")
            self.app.operations.cancel_task(identity, task_id)
            cancel_runtime = getattr(self.app.recommender, "cancel_task_runtime", None)
            if callable(cancel_runtime):
                cancel_runtime(identity, task_id, reason="parent_task_cancelled")
            task = self.app.tasks.cancel(identity, task_id)
            self.app.audit.record(AuditEvent(action="task.cancel", principal_id=identity.principal_id, tenant_id=identity.tenant_id, outcome="succeeded", decision="allow", reference=task_id, request_id=request_id))
            self._json(HTTPStatus.OK, {"task": task})
            return
        if path.startswith("/api/tasks/") and path.endswith("/reports"):
            task_id = path.removeprefix("/api/tasks/").removesuffix("/reports").rstrip("/")
            self.app.policy.require(identity.role, "task.read")
            self.app.tasks.get(identity, task_id)
            selection = body.get("selection")
            if selection is None:
                # OptChat declares only the public delivery type.  The
                # App-owned executor selects current, verified task evidence;
                # no client path or ModuleRunRef is accepted on this route.
                _only_fields(body, {"kind", "format", "background"})
                kind = str(body.get("kind", "")).strip().lower()
                if kind not in {"card", "quote", "report"}:
                    raise ValidationError("report kind must be card, quote or report")
                output_format = str(body.get("format", "html")).strip().lower()
                if output_format not in {"html", "pdf"}:
                    raise ValidationError("report format must be html or pdf")
                arguments = {"kind": kind, "format": output_format}
                if body.get("background") is True:
                    report_request_id = self.headers.get("X-Request-Id", "")
                    operation = self.app.operations.submit(
                        identity,
                        task_id=task_id,
                        module="reporter",
                        kind="report",
                        operation=lambda cancelled: _dispatch_background_report(
                            self.app.conversation_tools, identity, task_id, arguments, cancelled,
                        ),
                        request_id=report_request_id or None,
                        input_hash=_json_hash(arguments) if report_request_id else None,
                        lane="control",
                    )
                    self._json(HTTPStatus.ACCEPTED, {"operation": operation.public()})
                    return
                result = self.app.conversation_tools.call(identity, task_id, "reporter.run", arguments)
                self._json(HTTPStatus.OK, {"result": result})
                return
            if not isinstance(selection, dict):
                raise ValidationError("report request requires one controlled selection")
            kind = str(selection.get("output_type", ""))
            capability = {"card": "report.card.request", "quote": "report.quote.request", "report": "report.full.request"}.get(kind)
            if capability is None:
                raise ValidationError("selection.output_type must be card, quote or report")
            self.app.policy.require(identity.role, capability)
            source_id = selection.get("source_id")
            if not isinstance(source_id, str) or not source_id:
                raise ValidationError("selection.source_id is required")
            source = self.app.results.get_owned_report_source(identity, source_id)
            if source.get("task_id") != task_id:
                raise ValidationError("selection.source_id is not part of task_id")
            # This is an App REST workflow, not an iframe request.  The Host
            # creates the same short-lived, task-bound Reporter context
            # internally so callers cannot fabricate browser headers.
            module_context = self.app.registry.host_context(identity, "reporter", task_id=task_id)
            result = self.app.tool_dispatcher.dispatch(
                "reporter", {"action": "run", "task_id": task_id, "kind": kind, "selection": selection}, identity,
                module_context=module_context, request_id=f"server-report-{task_id}",
            )
            self._json(HTTPStatus.OK, {"result": result})
            return
        if path == "/api/settings/preferences":
            current = self.app.settings_for(identity)
            preferences = _preference_from(body, current.preferences)
            snapshot = self.app.save_settings(identity, replace(current, preferences=preferences), "settings.preferences.write")
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, snapshot)})
            return
        if path == "/api/settings/model-provider/credential":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"provider_id", "original_provider_id", "display_name", "endpoint", "protocol", "models", "default_model_id", "api_key"})
            profile = _model_provider_from(body)
            original_provider_id = _model_connection_id(body.get("original_provider_id", profile.provider_id))
            current = self.app.settings_for(identity)
            original_exists = any(item.provider_id == original_provider_id for item in current.model_providers)
            target_exists = any(item.provider_id == profile.provider_id for item in current.model_providers)
            if original_exists and original_provider_id != profile.provider_id:
                raise UserActionError(
                    "provider_identity_locked",
                    "已保存的Provider ID不可修改。",
                    stage="settings",
                    next_step="请新建Provider并确认可用后，再删除旧配置。",
                )
            if target_exists and original_provider_id != profile.provider_id:
                raise UserActionError(
                    "provider_identity_conflict",
                    "该Provider ID已存在，不能覆盖另一项配置。",
                    stage="settings",
                    next_step="请为新Provider填写不同的Provider ID。",
                )
            api_key = _credential_value(body, "api_key")
            reference = self.app.secret_reference(
                identity.tenant_id, "model", owner_id=identity.principal_id, connection_id=profile.provider_id,
                revision=f"rev-{uuid4().hex}",
            )
            snapshot = self.app.local_provider_snapshot(
                identity, profile, secret_ref=reference, default_model_id=_optional_model_id(body.get("default_model_id")),
            )
            saved = self.app.save_credential(
                identity, reference, api_key, "模型服务凭据", snapshot, "settings.model.local.write",
            )
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, saved), "credential_status": "stored"})
            return
        if path == "/api/settings/model-provider":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"provider_id", "original_provider_id", "display_name", "endpoint", "protocol", "models", "default_model_id"})
            profile = _model_provider_from(body)
            current = self.app.settings_for(identity)
            original_provider_id = _model_connection_id(body.get("original_provider_id", profile.provider_id))
            if original_provider_id != profile.provider_id:
                raise UserActionError(
                    "provider_identity_locked",
                    "已保存的Provider ID不可修改。",
                    stage="settings",
                    next_step="请新建Provider并确认可用后，再删除旧配置。",
                )
            existing = next((item for item in current.model_providers if item.provider_id == profile.provider_id), None)
            if existing is None:
                raise UserActionError(
                    "provider_credential_required",
                    "新增Provider时必须同时填写并保存API Key。",
                    stage="settings",
                    next_step="请粘贴该Provider的API Key后重新保存。",
                )
            if existing and existing.secret_ref and not _same_credential_origin(existing.endpoint, profile.endpoint):
                raise ValidationError("更换模型服务地址时，请同时重新粘贴该服务的API Key并保存。")
            snapshot = self.app.local_provider_snapshot(
                identity, profile, default_model_id=_optional_model_id(body.get("default_model_id")),
            )
            saved = self.app.save_settings(identity, snapshot, "settings.model.local.write")
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, saved)})
            return
        if path == "/api/settings/model-provider/default":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"provider_id", "model_id"})
            selection = ModelSelection(_model_connection_id(body.get("provider_id")), _required_model_id(body.get("model_id")))
            current = self.app.settings_for(identity)
            self.app.model_service_for_selection(identity, selection)
            saved = self.app.save_settings(
                identity, replace(current, default_model_selection=selection), "settings.model.local.write",
            )
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, saved)})
            return
        if path == "/api/settings/recommendation-execution":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"execution_mode"})
            mode = str(body.get("execution_mode", ""))
            if mode not in {"single", "multi"}:
                raise ValidationError("推荐执行方式必须为single或multi")
            saved = self.app.save_settings(identity, replace(self.app.settings_for(identity), recommendation_execution_mode=mode), "settings.model.local.write")
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, saved)})
            return
        if path == "/api/settings/multi-agent-preset/default":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"preset_id"})
            preset_id = str(body.get("preset_id", "")).strip()
            preset = recommendation_presets().get(preset_id)
            if preset is None or not preset.enabled:
                raise ValidationError("MultiAgent预设未启用")
            current = self.app.settings_for(identity)
            saved = self.app.save_settings(
                identity,
                replace(current, multi_agent_recommendation_preset_id=preset_id),
                "settings.model.local.write",
            )
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, saved)})
            return
        if path == "/api/settings/multi-agent-role-models":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"preset_id", "role_models"})
            preset_id = str(body.get("preset_id", "")).strip()
            preset = recommendation_presets().get(preset_id)
            if preset is None or not preset.enabled:
                raise ValidationError("MultiAgent预设未启用")
            raw_role_models = body.get("role_models")
            if not isinstance(raw_role_models, dict):
                raise ValidationError("role_models必须为对象")
            allowed_roles = set(_catalog_roles(
                preset, _RECOMMENDATION_MODE_ROLE_FALLBACKS.get(preset_id, ()),
            ))
            selected_roles: dict[str, ModelSelection] = {}
            for role, raw_selection in raw_role_models.items():
                if role not in allowed_roles or not isinstance(raw_selection, dict):
                    raise ValidationError("MultiAgent角色模型映射无效")
                _only_fields(raw_selection, {"provider_id", "model_id"})
                selection = ModelSelection(
                    _model_connection_id(raw_selection.get("provider_id")),
                    _required_model_id(raw_selection.get("model_id")),
                )
                self.app.model_service_for_selection(identity, selection)
                selected_roles[role] = selection
            current = self.app.settings_for(identity)
            mappings = {
                key: dict(value) for key, value in current.multi_agent_preset_role_models.items()
            }
            if selected_roles:
                mappings[preset_id] = selected_roles
            else:
                mappings.pop(preset_id, None)
            saved = self.app.save_settings(
                identity,
                replace(current, multi_agent_preset_role_models=mappings),
                "settings.model.local.write",
            )
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, saved)})
            return
        if path == "/api/settings/multi-agent-role-config":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"preset_id", "role_models", "agent_files"})
            preset_id = str(body.get("preset_id", "")).strip()
            preset = recommendation_presets().get(preset_id)
            if preset is None or not preset.enabled:
                raise ValidationError("MultiAgent预设未启用")
            allowed_roles = set(_catalog_roles(
                preset, _RECOMMENDATION_MODE_ROLE_FALLBACKS.get(preset_id, ()),
            ))
            raw_role_models = body.get("role_models")
            if not isinstance(raw_role_models, dict):
                raise ValidationError("role_models必须为对象")
            selected_roles: dict[str, ModelSelection] = {}
            for role, raw_selection in raw_role_models.items():
                if role not in allowed_roles or not isinstance(raw_selection, dict):
                    raise ValidationError("MultiAgent角色模型映射无效")
                _only_fields(raw_selection, {"provider_id", "model_id"})
                selection = ModelSelection(
                    _model_connection_id(raw_selection.get("provider_id")),
                    _required_model_id(raw_selection.get("model_id")),
                )
                self.app.model_service_for_selection(identity, selection)
                selected_roles[role] = selection
            raw_agent_files = body.get("agent_files")
            if not isinstance(raw_agent_files, dict) or set(raw_agent_files) != allowed_roles:
                raise ValidationError("必须提交当前预设全部Agent的AGENT.md")
            selected_agent_files = {
                role: _agent_instruction_content(raw_agent_files[role])
                for role in sorted(allowed_roles)
            }
            defaults = default_agent_instructions(preset_id)
            overrides = {
                role: content
                for role, content in selected_agent_files.items()
                if content != defaults.get(role)
            }
            current = self.app.settings_for(identity)
            role_mappings = {
                key: dict(value) for key, value in current.multi_agent_preset_role_models.items()
            }
            instruction_mappings = {
                key: dict(value) for key, value in current.multi_agent_preset_agent_instructions.items()
            }
            if selected_roles:
                role_mappings[preset_id] = selected_roles
            else:
                role_mappings.pop(preset_id, None)
            if overrides:
                instruction_mappings[preset_id] = overrides
            else:
                instruction_mappings.pop(preset_id, None)
            saved = self.app.save_settings(
                identity,
                replace(
                    current,
                    multi_agent_preset_role_models=role_mappings,
                    multi_agent_preset_agent_instructions=instruction_mappings,
                ),
                "settings.model.local.write",
            )
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, saved)})
            return
        if path == "/api/settings/multi-agent-review-policy/default":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"review_policy_id"})
            policy_id = str(body.get("review_policy_id", "")).strip()
            policy = _review_policies().get(policy_id)
            if policy is None or not bool(_catalog_value(policy, "enabled", False)):
                raise ValidationError("MultiAgent复核策略未启用")
            current = self.app.settings_for(identity)
            saved = self.app.save_settings(
                identity,
                replace(current, multi_agent_review_policy_id=policy_id),
                "settings.model.local.write",
            )
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, saved)})
            return
        if path == "/api/settings/multi-agent-review-policy-role-models":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"review_policy_id", "role_models"})
            policy_id = str(body.get("review_policy_id", "")).strip()
            policy = _review_policies().get(policy_id)
            if policy is None or not bool(_catalog_value(policy, "enabled", False)):
                raise ValidationError("MultiAgent复核策略未启用")
            raw_role_models = body.get("role_models")
            if not isinstance(raw_role_models, dict):
                raise ValidationError("role_models必须为对象")
            allowed_roles = set(_review_policy_roles(policy_id, policy))
            selected_roles: dict[str, ModelSelection] = {}
            for role, raw_selection in raw_role_models.items():
                if role not in allowed_roles or not isinstance(raw_selection, dict):
                    raise ValidationError("MultiAgent复核角色模型映射无效")
                _only_fields(raw_selection, {"provider_id", "model_id"})
                selection = ModelSelection(
                    _model_connection_id(raw_selection.get("provider_id")),
                    _required_model_id(raw_selection.get("model_id")),
                )
                self.app.model_service_for_selection(identity, selection)
                selected_roles[role] = selection
            current = self.app.settings_for(identity)
            mappings = {
                key: dict(value) for key, value in current.multi_agent_review_policy_role_models.items()
            }
            if selected_roles:
                mappings[policy_id] = selected_roles
            else:
                mappings.pop(policy_id, None)
            saved = self.app.save_settings(
                identity,
                replace(current, multi_agent_review_policy_role_models=mappings),
                "settings.model.local.write",
            )
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, saved)})
            return
        if path == "/api/settings/model-provider/delete":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"provider_id"})
            provider_id = _model_connection_id(body.get("provider_id"))
            current = self.app.settings_for(identity)
            providers = tuple(item for item in current.model_providers if item.provider_id != provider_id)
            if len(providers) == len(current.model_providers):
                raise ValidationError("模型提供方不存在，请刷新设置中心后重试。")
            next_selection = next(
                (ModelSelection(item.provider_id, model.model_id)
                 for item in providers for model in item.models if model.enabled),
                None,
            )
            valid_selections = {
                (item.provider_id, model.model_id)
                for item in providers for model in item.models if model.enabled
            }
            role_models = {
                preset_id: {
                role: selection for role, selection in mappings.items()
                if role in MULTI_AGENT_RECOMMENDATION_ROLES
                and (selection.provider_id, selection.model_id) in valid_selections
                }
                for preset_id, mappings in current.multi_agent_preset_role_models.items()
            }
            review_role_models = {
                policy_id: {
                    role: selection for role, selection in mappings.items()
                    if (selection.provider_id, selection.model_id) in valid_selections
                }
                for policy_id, mappings in current.multi_agent_review_policy_role_models.items()
            }
            saved = self.app.save_settings(
                identity, replace(
                    current,
                    model_providers=providers,
                    default_model_selection=next_selection,
                    multi_agent_preset_role_models=role_models,
                    multi_agent_review_policy_role_models=review_role_models,
                ),
                "settings.model.local.write",
            )
            reference = self.app.secret_reference(
                identity.tenant_id, "model", owner_id=identity.principal_id, connection_id=provider_id,
            )
            self.app.secret_provider.allow_reference(reference, "模型服务凭据")
            try:
                self.app.secret_provider.delete(reference, "模型服务凭据")
            except UnavailableCapabilityError:
                pass
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, saved)})
            return
        if path == "/api/settings/model-provider/discover":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"provider_id", "endpoint", "api_key"})
            provider_id = _model_connection_id(body.get("provider_id"))
            endpoint = validate_openai_base_url(str(body.get("endpoint", "")))
            api_key = str(body.get("api_key", "")).strip()
            if not api_key:
                current = self.app.settings_for(identity)
                provider = next((item for item in current.model_providers if item.provider_id == provider_id), None)
                if provider is not None and provider.secret_ref is not None:
                    api_key = self.app.secret_provider.resolve(provider.secret_ref, "模型服务凭据")
            models = discover_openai_models(endpoint, api_key or None)
            self._json(HTTPStatus.OK, {"models": models})
            return
        if path == "/api/settings/model-provider/test":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"provider_id", "endpoint", "model_id", "api_key"})
            provider_id = _model_connection_id(body.get("provider_id"))
            endpoint = validate_openai_base_url(str(body.get("endpoint", "")))
            model_id = _required_model_id(body.get("model_id"))
            api_key = str(body.get("api_key", "")).strip()
            if not api_key:
                current = self.app.settings_for(identity)
                provider = next((item for item in current.model_providers if item.provider_id == provider_id), None)
                if provider is None or provider.secret_ref is None:
                    raise UnavailableCapabilityError("模型服务", "请先粘贴API Key，或保存提供方后再测试连接。")
                api_key = self.app.secret_provider.resolve(provider.secret_ref, "模型服务凭据")
            probe = ModelServiceSettings("openai-compatible", endpoint, SecretRef("ephemeral", "probe"), model_id)
            complete_openai_compatible(probe, probe.secret_ref, [{"role": "user", "content": "仅返回OK。"}], resolve_secret=lambda _ref: api_key)
            self._json(HTTPStatus.OK, {"connection": {"provider_name": provider_id, "status": "available", "detail": "模型服务连接可用。"}})
            return
        if path == "/api/settings/local-model/credential":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"connection_id", "display_name", "provider_name", "endpoint", "model_name", "api_key"})
            connection_id = _model_connection_id(body.get("connection_id"))
            display_name = _model_display_name(body.get("display_name"), connection_id)
            api_key = _credential_value(body, "api_key")
            model = _model_from(body)
            reference = self.app.secret_reference(
                identity.tenant_id,
                "model",
                owner_id=identity.principal_id,
                connection_id=connection_id,
                revision=f"rev-{uuid4().hex}",
            )
            current = self.app.settings_for(identity)
            existing = next((item for item in current.model_connections if item.connection_id == connection_id), None)
            snapshot = self.app.local_model_snapshot(
                identity,
                connection_id,
                display_name,
                model,
                secret_ref=reference,
            )
            if existing is not None and not _same_credential_origin(existing.model_service.endpoint, model.endpoint):
                # A supplied key is present on this route, so changing the
                # provider origin is explicitly allowed and gets a new value.
                pass
            saved = self.app.save_credential(
                identity,
                reference,
                api_key,
                "模型服务凭据",
                snapshot,
                "settings.model.local.write",
            )
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, saved), "credential_status": "stored"})
            return
        if path == "/api/settings/local-model":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"connection_id", "display_name", "provider_name", "endpoint", "model_name"})
            connection_id = _model_connection_id(body.get("connection_id"))
            display_name = _model_display_name(body.get("display_name"), connection_id)
            model = _model_from(body)
            current = self.app.settings_for(identity)
            existing = next((item for item in current.model_connections if item.connection_id == connection_id), None)
            if existing is not None and existing.model_service.secret_ref is not None and not _same_credential_origin(
                existing.model_service.endpoint,
                model.endpoint,
            ):
                raise ValidationError("更换模型服务地址时，请同时重新粘贴该服务的API Key并保存。")
            snapshot = self.app.local_model_snapshot(identity, connection_id, display_name, model)
            saved = self.app.save_settings(identity, snapshot, "settings.model.local.write")
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, saved)})
            return
        if path == "/api/settings/local-model/active":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"connection_id"})
            connection_id = _model_connection_id(body.get("connection_id"))
            current = self.app.settings_for(identity)
            selected = next((item for item in current.model_connections if item.connection_id == connection_id), None)
            if selected is None:
                raise ValidationError("模型连接不存在，请先保存该连接。")
            saved = self.app.save_settings(
                identity,
                replace(
                    current,
                    model_service=selected.model_service,
                    active_model_connection_id=connection_id,
                    default_model_selection=ModelSelection(connection_id, selected.model_service.model_name),
                ),
                "settings.model.local.write",
            )
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, saved)})
            return
        if path == "/api/settings/local-model/delete":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"connection_id"})
            connection_id = _model_connection_id(body.get("connection_id"))
            current = self.app.settings_for(identity)
            remaining = tuple(item for item in current.model_connections if item.connection_id != connection_id)
            if len(remaining) == len(current.model_connections):
                raise ValidationError("模型连接不存在，请刷新设置中心后重试。")
            next_active = remaining[0] if remaining else None
            providers = tuple(item for item in current.model_providers if item.provider_id != connection_id)
            next_selection = (
                ModelSelection(next_active.connection_id, next_active.model_service.model_name)
                if next_active is not None else None
            )
            valid_selections = {
                (provider.provider_id, model.model_id)
                for provider in providers for model in provider.models if model.enabled
            }
            role_models = {
                preset_id: {
                    role: selection for role, selection in mappings.items()
                    if role in MULTI_AGENT_RECOMMENDATION_ROLES
                    and (selection.provider_id, selection.model_id) in valid_selections
                }
                for preset_id, mappings in current.multi_agent_preset_role_models.items()
            }
            review_role_models = {
                policy_id: {
                    role: selection for role, selection in mappings.items()
                    if (selection.provider_id, selection.model_id) in valid_selections
                }
                for policy_id, mappings in current.multi_agent_review_policy_role_models.items()
            }
            saved = self.app.save_settings(
                identity,
                replace(
                    current,
                    model_service=next_active.model_service if next_active else ModelServiceSettings("unconfigured", ""),
                    model_connections=remaining,
                    active_model_connection_id=next_active.connection_id if next_active else None,
                    model_providers=providers,
                    default_model_selection=next_selection,
                    multi_agent_preset_role_models=role_models,
                    multi_agent_review_policy_role_models=review_role_models,
                ),
                "settings.model.local.write",
            )
            reference = self.app.secret_reference(
                identity.tenant_id,
                "model",
                owner_id=identity.principal_id,
                connection_id=connection_id,
            )
            self.app.secret_provider.allow_reference(reference, "模型服务凭据")
            try:
                self.app.secret_provider.delete(reference, "模型服务凭据")
            except UnavailableCapabilityError:
                pass
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, saved)})
            return
        if path == "/api/settings/model/credential":
            self.app.policy.require(identity.role, "settings.model.write")
            _only_fields(body, {"provider_name", "endpoint", "model_name", "api_key"})
            api_key = _credential_value(body, "api_key")
            settings = _model_from(body)
            reference = self.app.secret_reference(
                identity.tenant_id, "model", revision=f"rev-{uuid4().hex}",
            )
            current = self.app.settings_for(identity)
            snapshot = self.app.save_credential(
                identity,
                reference,
                api_key,
                "模型服务凭据",
                replace(current, model_service=replace(settings, secret_ref=reference)),
                "settings.model.write",
            )
            self.app.audit.record(AuditEvent(
                action="settings.model.credential_store", principal_id=identity.principal_id,
                tenant_id=identity.tenant_id, outcome="succeeded", decision="allow",
                reference=reference.key, request_id=request_id,
            ))
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, snapshot), "credential_status": "stored"})
            return
        if path == "/api/settings/data/credential":
            capability = self.app.data_settings_capability(identity)
            _only_fields(body, {"provider_name", "refresh_token"})
            refresh_token = _credential_value(body, "refresh_token")
            reference = self.app.secret_reference(
                identity.tenant_id,
                "ifind",
                owner_id=identity.principal_id if capability == "settings.data.local.write" else None,
                revision=f"rev-{uuid4().hex}",
            )
            credential = {"refresh_token": refresh_token}
            credential_record = json.dumps(credential, separators=(",", ":"))
            current = self.app.settings_for(identity)
            snapshot = self.app.save_credential(
                identity,
                reference,
                credential_record,
                "iFind数据凭据",
                replace(current, data_interface=DataInterfaceSettings(str(body["provider_name"]).strip(), reference)),
                capability,
            )
            self.app.audit.record(AuditEvent(
                action="settings.data.credential_store", principal_id=identity.principal_id,
                tenant_id=identity.tenant_id, outcome="succeeded", decision="allow",
                reference=reference.key, request_id=request_id,
            ))
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, snapshot), "credential_status": "stored"})
            return
        if path in {"/api/settings/model", "/api/settings/data", "/api/settings/storage"}:
            current = self.app.settings_for(identity)
            if path.endswith("/model"):
                _only_fields(body, {"provider_name", "endpoint", "model_name"})
                model = _model_from(body)
                if current.model_service.secret_ref is not None and not _same_credential_origin(
                    current.model_service.endpoint, model.endpoint,
                ):
                    raise ValidationError("更换模型服务地址时，请同时重新粘贴该服务的API Key并保存。")
                snapshot = replace(current, model_service=replace(model, secret_ref=current.model_service.secret_ref))
                capability = "settings.model.write"
            elif path.endswith("/data"):
                _only_fields(body, {"provider_name"})
                snapshot = replace(current, data_interface=replace(_data_from(body), secret_ref=current.data_interface.secret_ref))
                capability = self.app.data_settings_capability(identity)
            else:
                snapshot = replace(current, storage_export=_storage_from(body))
                capability = "settings.storage.write"
            snapshot = self.app.save_settings(identity, snapshot, capability)
            self._json(HTTPStatus.OK, {"settings": self.app.public_settings(identity, snapshot)})
            return
        if path == "/api/settings/test/model":
            if self.app.policy.allows(identity.role, "settings.model.write"):
                capability = "settings.model.write"
            else:
                self.app.policy.require(identity.role, "settings.model.local.write")
                capability = "settings.model.local.write"
            _only_fields(body, {"provider_id", "model_id"})
            raw_provider_id = body.get("provider_id")
            raw_model_id = body.get("model_id")
            if (raw_provider_id is None) != (raw_model_id is None):
                raise ValidationError("测试模型时必须同时提供Provider ID和模型ID")
            selection = (
                ModelSelection(
                    _model_connection_id(raw_provider_id),
                    _required_model_id(raw_model_id),
                )
                if raw_provider_id is not None else None
            )
            configured = self.app.model_service_for_selection(identity, selection)
            if configured.secret_ref is None:
                raise UnavailableCapabilityError("模型服务", "请先保存Provider和API Key后再测试连接。")
            probe = (
                self.app.model_gateway.probe_settings_capabilities_for(
                    identity,
                    "settings-capability-probe",
                    selection=selection,
                )
                if selection is not None else
                self.app.model_gateway.probe_settings_capabilities_for(
                    identity,
                    "settings-capability-probe",
                )
            )
            verified = bool(probe.get("verified"))
            model = probe.get("model") if isinstance(probe.get("model"), dict) else {}
            resolved_selection = ModelSelection(
                str(model.get("provider_id", "")),
                str(model.get("model_id", "")),
            )
            current_settings = self.app.settings_for(identity)
            current_provider = next((
                item for item in current_settings.model_providers
                if item.provider_id == resolved_selection.provider_id
            ), None)
            if current_provider is not None and configured.secret_ref.revision is not None:
                self.app.record_model_connection_verification(
                    identity,
                    selection=resolved_selection,
                    expected_reference=configured.secret_ref,
                    endpoint=configured.endpoint,
                    probe=probe,
                )
            connection = {
                "provider_name": str(model.get("provider_id", "model")),
                "model_id": str(model.get("model_id", "")),
                "status": "available" if verified or probe.get("connection_available", probe.get("initialized", False)) else "unavailable",
                "detail": (
                    "连接成功，模型与工具调用可用。"
                    if verified else
                    "连接成功，可用于对话；部分工具能力未确认。"
                    if probe.get("connection_available", probe.get("initialized", False)) else
                    "连接失败，请检查API地址、密钥、模型名称或网络后重试。"
                ),
                "capability_probe": probe,
            }
            self.app.audit.record(AuditEvent(
                action="settings.connection_test", principal_id=identity.principal_id,
                tenant_id=identity.tenant_id, outcome=str(connection["status"]), decision="allow",
                reference=capability, request_id=request_id,
            ))
            self._json(HTTPStatus.OK, {"connection": connection})
            return
        if path.startswith("/api/settings/test/"):
            capability = self.app.data_settings_capability(identity)
            provider = path.removeprefix("/api/settings/test/")
            tested_ref = (
                self.app.settings_for(identity).data_interface.secret_ref
                if provider == "ifind" else None
            )
            try:
                result = self.app.connection_tester.test(
                    provider,
                    identity,
                    request_id=request_id,
                    expected_secret_ref=tested_ref,
                )
            except Exception:
                raise
            if provider == "ifind":
                if result.status == "available":
                    self.app.record_data_connection_verification(
                        identity, available=True, expected_reference=tested_ref,
                    )
                elif tested_ref is not None and result.status in {
                    "unauthorized", "account_permission_denied",
                }:
                    self.app.revoke_data_connection_verification(identity, tested_ref)
                elif tested_ref is not None and result.status == "device_limit_exceeded":
                    self.app.mark_data_connection_temporarily_unavailable(identity, tested_ref)
            self.app.audit.record(AuditEvent(
                action="settings.connection_test", principal_id=identity.principal_id,
                tenant_id=identity.tenant_id, outcome=result.status, decision="allow",
                reference=provider, request_id=request_id,
            ))
            self._json(HTTPStatus.OK, {"connection": result.__dict__})
            return
        if path.startswith("/api/tools/"):
            tool = path.removeprefix("/api/tools/")
            action = str(body.get("action", "run")).strip().lower()
            datafetcher_run = tool == "datafetcher" and action == "fetch"
            reporter_source_list = tool == "reporter" and action == "list_report_sources"
            if (
                action not in {"catalog", "default", "preview", "status", "list_assets"}
                and not datafetcher_run
                and not reporter_source_list
            ):
                raise ValidationError("正式run、fetch与rerender必须通过当前任务的Task Operation提交")
            self.app.policy.require(identity.role, "module.run" if datafetcher_run else "module.catalog")
            module_context = self._module_host_context(tool)
            result = self.app.tool_dispatcher.dispatch(
                tool, body, identity,
                module_context=module_context,
                request_id=self.headers.get("X-OptionHelper-Request-Id", ""),
            )
            self.app.audit.record(AuditEvent(action="tool.dispatch", principal_id=identity.principal_id, tenant_id=identity.tenant_id, outcome="succeeded", decision="allow", reference=tool, request_id=request_id))
            self._json(HTTPStatus.OK, {"tool": tool, "result": result})
            return
        raise KeyError(path)

    def _serve_capability_asset(self, identity: SessionIdentity, relative: str) -> None:
        page_prefix = "assets/pages/"
        shared_designer_assets = {
            "assets/designer/themes/designer-token-vars.css",
            "assets/designer/vendor/echarts.min.js",
        }
        if not relative.startswith(page_prefix) and relative not in shared_designer_assets:
            raise AuthorizationError("module.page", "only registered module page assets are mountable")
        parts = Path(relative).parts
        shared_page_assets = {
            "assets/pages/date-input-control.js",
            "assets/pages/date-input-control.css",
            "assets/pages/module-host-bridge.js",
            "assets/pages/module-host-presentation.css",
            "assets/pages/module-host-presentation.js",
            "assets/pages/plotly-chart-system.js",
            "assets/pages/vendor/plotly-optionhelper.min.js",
        }
        if relative not in shared_page_assets | shared_designer_assets and (
            len(parts) < 4 or parts[0:2] != ("assets", "pages") or parts[2] not in PAGE_MODULES
        ):
            raise ValidationError("Capability page asset path is invalid")
        editor_asset = relative.startswith("assets/pages/reporter/editor/") or relative in shared_designer_assets | {"assets/pages/reporter/reporter.css", "assets/pages/module-host-presentation.css"}
        self.app.policy.require(identity.role, "conversation.write" if editor_asset else "module.page")
        content, content_type = self.app.registry.read_asset(relative)
        self._bytes(HTTPStatus.OK, content, content_type, extra_headers={"Content-Security-Policy": _CAPABILITY_CSP})

    def _serve_brand_asset(self, identity: SessionIdentity | None, filename: str) -> None:
        capability_asset = {"optionhelper-logo.svg", "optionhelper-mark.svg"}
        themed_icon_asset = {"optionhelper-app-icon-tile-light.svg", "optionhelper-app-icon-tile-dark.svg"}
        if filename not in capability_asset | themed_icon_asset:
            raise KeyError(filename)
        if identity is not None:
            self.app.policy.require(identity.role, "optchat")
        if filename in themed_icon_asset:
            path = (self.app.brand_assets_root / filename).resolve()
            if path.parent != self.app.brand_assets_root or not path.is_file():
                raise KeyError(filename)
            content, content_type = path.read_bytes(), "image/svg+xml"
        else:
            content, content_type = self.app.registry.read_asset(f"assets/icons/{filename}")
        self._bytes(HTTPStatus.OK, content, content_type)

    def _serve_frontend_asset(self, relative: str) -> None:
        if not relative or relative.startswith("/") or ".." in Path(relative).parts:
            raise ValidationError("App frontend asset path is invalid")
        if relative not in FRONTEND_ASSETS:
            raise KeyError(relative)
        path = (self.app.frontend_root / relative).resolve()
        if self.app.frontend_root not in path.parents or not path.is_file():
            raise KeyError(relative)
        parts = Path(relative).parts
        if path.suffix.lower() not in {".html", ".css", ".js"}:
            raise KeyError(relative)
        if parts[0] == "optchat":
            self.app.policy.require(self._identity().role, "optchat")
        elif parts[0] == "optdesk":
            self.app.policy.require(self._identity().role, "optdesk")
        elif parts[0] == "settings":
            self.app.policy.require(self._identity().role, "settings.read")
        content_type = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8"}[path.suffix.lower()]
        self._bytes(
            HTTPStatus.OK,
            path.read_bytes(),
            content_type,
            extra_headers={
                # Local CSS icons use data images. Uploaded attachments use
                # authenticated same-origin endpoints; blob images, remote
                # resources and inline scripts remain disallowed.
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'self'; object-src 'none'; frame-src 'self'; frame-ancestors 'self'; form-action 'self'",
            },
        )

    def _identity(self) -> SessionIdentity:
        return self.app.identity_from_cookie(self.headers.get("Cookie"))

    def _module_host_context(self, tool: str) -> dict[str, Any]:
        if self.headers.get("Origin") != self.app.url:
            raise AuthorizationError("module.host_context", "Module Host Origin is invalid")
        raw = self.headers.get("X-OptionHelper-Module-Context")
        if raw is None or len(raw) > 8_000:
            raise ValidationError("Module Host context header is required")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValidationError("Module Host context header is invalid") from error
        if not isinstance(value, dict) or value.get("module") != tool:
            raise ValidationError("Module Host context does not match tool")
        return value

    def _optional_identity(self) -> SessionIdentity | None:
        try:
            return self._identity()
        except AuthorizationError:
            return None

    def _body_json(
        self, *, allowed_plaintext_fields: frozenset[str] = frozenset(), max_bytes: int = 200_000,
    ) -> dict[str, Any]:
        length_raw = self.headers.get("Content-Length", "0")
        try:
            length = int(length_raw)
        except ValueError as error:
            raise ValidationError("Content-Length is invalid") from error
        if length < 0 or length > max_bytes:
            raise ValidationError("Request body is too large")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as error:
            raise ValidationError("Request body must be a JSON object") from error
        if not isinstance(value, dict):
            raise ValidationError("Request body must be a JSON object")
        _reject_plain_secrets(value, allowed_plaintext_fields=allowed_plaintext_fields)
        return value


    def _json(
        self,
        status: HTTPStatus,
        value: dict[str, Any],
        cookie: str | None = None,
        *,
        persistent_cookie: bool = False,
        clear_cookie: bool = False,
    ) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            max_age = "; Max-Age=2592000" if persistent_cookie else ""
            self.send_header("Set-Cookie", f"optionhelper_session={cookie}; HttpOnly; SameSite=Strict; Path=/{max_age}")
        elif clear_cookie:
            self.send_header("Set-Cookie", "optionhelper_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, status: HTTPStatus, value: bytes, content_type: str, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, header_value in (extra_headers or {}).items():
            self.send_header(name, header_value)
        self.send_header("Content-Length", str(len(value)))
        self.end_headers()
        self.wfile.write(value)

    def _audit_denial(self, error: AuthorizationError, request_id: str) -> None:
        identity = self._optional_identity()
        self.app.audit.record(
            AuditEvent(
                action="authorization.reject",
                principal_id=identity.principal_id if identity else "anonymous",
                tenant_id=identity.tenant_id if identity else "unknown",
                outcome="rejected",
                decision="deny",
                reference=error.capability,
                reason=error.reason,
                request_id=request_id,
            )
        )

    def _audit_operational_failure(
        self,
        request_id: str,
        *,
        failure_code: str,
        stage: str,
        reference: str | None = None,
    ) -> None:
        """Record a diagnosable failure without persisting request payloads or causes.

        The public response carries the same deterministic diagnostic id.  The
        local audit deliberately stores only the reviewed code and stage: raw
        provider exceptions can contain paths or credential material.
        """

        identity = self._optional_identity()
        if identity is None:
            return
        self.app.audit.record(
            AuditEvent(
                action="tool.dispatch",
                principal_id=identity.principal_id,
                tenant_id=identity.tenant_id,
                outcome="failed",
                decision="allow",
                reference=reference,
                reason=f"{stage}:{failure_code}",
                request_id=request_id,
            )
        )


def _diagnostic_id(request_id: str, failure_code: str, stage: str) -> str:
    """Return a stable public handle without reflecting implementation detail."""

    material = f"{request_id}|{stage}|{failure_code}".encode("utf-8")
    return f"diag-{hashlib.sha256(material).hexdigest()[:12]}"


def _safe_public_validation_message(error: Exception) -> str:
    """Keep reviewed field guidance while suppressing paths and stack detail."""

    message = str(error).strip().replace("\r", " ").replace("\n", " ")[:240]
    if not message or "/" in message or "\\" in message:
        return "请求不符合受控输入规则"
    return message


def _unavailable_public_message(error: UnavailableCapabilityError) -> str:
    """Map reviewed App failures to a user-actionable, non-sensitive message."""

    if isinstance(error.message, str) and error.message:
        return error.message
    if error.capability == "datafetcher.configuration":
        return "请先在设置中心填写并保存iFind数据凭据，然后重试。"
    if error.capability in {"market_data", "backtester.trading_calendar"}:
        return "行情或交易日历暂不可用。请检查数据获取配置后重新获取所需区间。"
    if error.failure_code == "capability_execution_failed":
        return "本次计算未完成。请检查产品条款、行情数据和交易日历后重试。"
    return "相关功能暂不可用。请确认本机服务、当前任务和数据配置后重试。"


def _reject_plain_secrets(value: Any, *, allowed_plaintext_fields: frozenset = frozenset(), depth: int = 0) -> None:
    forbidden = {"password", "token", "api_key", "access_token", "refresh_token", "secret", "secret_value", "private_key"}
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = key.casefold()
            if normalized_key in forbidden and not (depth == 0 and key in allowed_plaintext_fields and isinstance(item, str)):
                raise ValidationError(f"{key} plaintext is forbidden outside the dedicated credential endpoint")
            if normalized_key == "secret_ref":
                raise ValidationError("secret_ref is Host-owned and cannot be submitted by a client")
            _reject_plain_secrets(item, allowed_plaintext_fields=frozenset(), depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _reject_plain_secrets(item, allowed_plaintext_fields=frozenset(), depth=depth + 1)


def _data_asset_download_id(path: str) -> str | None:
    match = re.fullmatch(r"/api/assets/([^/]+)/download", path)
    if match is None:
        return None
    value = unquote(match.group(1))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValidationError("DataAsset标识非法")
    return value


def _credential_fields_for(path: str) -> frozenset[str]:
    return {
        "/api/auth/login": frozenset({"password"}),
        "/api/settings/model/credential": frozenset({"api_key"}),
        "/api/settings/local-model/credential": frozenset({"api_key"}),
        "/api/settings/model-provider/credential": frozenset({"api_key"}),
        "/api/settings/model-provider/discover": frozenset({"api_key"}),
        "/api/settings/model-provider/test": frozenset({"api_key"}),
        "/api/settings/data/credential": frozenset({"refresh_token"}),
    }.get(path, frozenset())




def _only_fields(value: dict[str, Any], allowed: set[str]) -> None:
    unexpected = set(value).difference(allowed)
    if unexpected:
        raise ValidationError(f"请求包含不允许的字段：{', '.join(sorted(unexpected))}")


def _agent_instruction_content(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("AGENT.md必须为文本")
    content = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not content or len(content) > 16_000:
        raise ValidationError("每份AGENT.md必须为1至16000个字符")
    if "\x00" in content or any(ord(character) < 32 and character not in "\n\t" for character in content):
        raise ValidationError("AGENT.md包含不支持的控制字符")
    return content


def _credential_value(value: dict[str, Any], field: str) -> str:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError(f"{field}不能为空")
    return raw.strip()


def _platform_secret_provider(app_data_root: Path) -> SecretProvider:
    from .secrets.platform_provider import platform_secret_provider

    return platform_secret_provider(app_data_root)


def _same_credential_origin(current_endpoint: str, next_endpoint: str) -> bool:
    try:
        current = urlparse(validate_openai_base_url(current_endpoint))
        next_value = urlparse(validate_openai_base_url(next_endpoint))
    except ValidationError:
        return False
    return (current.scheme, current.hostname, current.port) == (next_value.scheme, next_value.hostname, next_value.port)


def _model_connection_id(value: object) -> str:
    connection_id = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", connection_id):
        raise ValidationError("模型连接ID只能包含小写字母、数字、下划线和短横线")
    return connection_id


def _model_display_name(value: object, connection_id: str) -> str:
    display_name = str(value or "").strip()
    if not display_name:
        return connection_id
    if len(display_name) > 80 or any(ord(character) < 32 for character in display_name):
        raise ValidationError("模型连接名称长度或字符不合法")
    return display_name


def _model_provider_from(value: dict[str, Any]) -> ModelProviderProfile:
    provider_id = _model_connection_id(value.get("provider_id"))
    display_name = _model_display_name(value.get("display_name"), provider_id)
    endpoint = validate_openai_base_url(str(value.get("endpoint", "")))
    protocol = str(value.get("protocol", "openai-chat-completions")).strip()
    if protocol != "openai-chat-completions":
        raise ValidationError("当前仅支持OpenAI兼容Chat Completions协议")
    raw_models = value.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ValidationError("至少保留一个模型")
    models: list[ModelCatalogEntry] = []
    model_ids: set[str] = set()
    for raw in raw_models:
        if not isinstance(raw, dict):
            raise ValidationError("模型目录条目必须是对象")
        model_id = str(raw.get("model_id", "")).strip()
        if not model_id or len(model_id) > 120 or any(ord(character) < 33 for character in model_id):
            raise ValidationError("模型ID格式不合法")
        if model_id in model_ids:
            raise ValidationError("同一提供方内不能重复添加模型")
        model_ids.add(model_id)
        model_name = str(raw.get("display_name", "")).strip() or model_id
        if len(model_name) > 120 or any(ord(character) < 32 for character in model_name):
            raise ValidationError("模型显示名称格式不合法")
        enabled = bool(raw.get("enabled", True))
        context_window = _model_capacity(raw.get("context_window"), "上下文长度")
        max_output_tokens = _model_capacity(raw.get("max_output_tokens"), "最大输出Token")
        models.append(ModelCatalogEntry(
            model_id=model_id,
            display_name=model_name,
            enabled=enabled,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            input_modalities=_model_input_modalities(raw.get("input_modalities")),
            reasoning_support=_model_capability_flag(raw.get("reasoning_support"), default=False, label="推理能力"),
            tool_calling=_model_capability_flag(raw.get("tool_calling"), default=True, label="工具调用能力"),
        ))
    if not any(item.enabled for item in models):
        raise ValidationError("请至少启用一个模型")
    return ModelProviderProfile(provider_id, display_name, endpoint, protocol, None, tuple(models))


def _model_capacity(value: object, label: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{label}必须是正整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValidationError(f"{label}必须是正整数") from error
    if parsed <= 0:
        raise ValidationError(f"{label}必须是正整数")
    return parsed


def _model_input_modalities(value: object) -> tuple[str, ...]:
    if value is None:
        return ("text",)
    if not isinstance(value, list):
        raise ValidationError("输入模态必须是数组")
    modalities = tuple(dict.fromkeys(str(item).strip().lower() for item in value))
    if not modalities or modalities[0] != "text" or any(item not in {"text", "image"} for item in modalities):
        raise ValidationError("输入模态必须以text开始，且仅支持text和image")
    return modalities


def _model_capability_flag(value: object, *, default: bool, label: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValidationError(f"{label}必须是布尔值")
    return value


def _public_model_catalog_entry(model: ModelCatalogEntry) -> dict[str, Any]:
    """Serialize one catalog entry without exposing any credential material."""

    return {
        "model_id": model.model_id,
        "display_name": model.display_name or model.model_id,
        "enabled": model.enabled,
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
        "input_modalities": list(model.input_modalities),
        "reasoning_support": model.reasoning_support,
        "tool_calling": model.tool_calling,
    }


def _required_model_id(value: object) -> str:
    model_id = str(value or "").strip()
    if not model_id or len(model_id) > 120 or any(ord(character) < 33 for character in model_id):
        raise ValidationError("模型ID格式不合法")
    return model_id


def _optional_model_id(value: object) -> str | None:
    if value is None or value == "":
        return None
    return _required_model_id(value)


def _conversation_model_selection(value: object) -> ModelSelection | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value).difference({"provider_id", "model_id"}):
        raise ValidationError("model_selection格式不合法")
    return ModelSelection(
        _model_connection_id(value.get("provider_id")),
        _required_model_id(value.get("model_id")),
    )


def _model_from(value: dict[str, Any]) -> ModelServiceSettings:
    provider_name = str(value.get("provider_name", "")).strip()
    endpoint = validate_openai_base_url(str(value.get("endpoint", "")))
    model_name = str(value.get("model_name", "")).strip()
    if provider_name != "openai-compatible":
        raise ValidationError("模型服务必须使用已批准的OpenAI兼容适配器")
    if not model_name:
        raise ValidationError("model_name is required for an OpenAI-compatible provider")
    return ModelServiceSettings(provider_name, endpoint, None, model_name)


def _dispatch_background_tool(
    dispatcher: ToolDispatcher,
    module: str,
    payload: dict[str, Any],
    identity: SessionIdentity,
    *,
    module_context: dict[str, Any],
    request_id: str,
    cancelled: Callable[[], bool],
) -> dict[str, Any]:
    """Run the existing module gateway outside the request lifetime."""
    if cancelled():
        raise OperationCancelled()
    try:
        result = dispatcher.dispatch(
            module,
            payload,
            identity,
            module_context=module_context,
            request_id=request_id,
            cancellation_check=cancelled,
        )
    except UserActionError as error:
        if cancelled() and error.code == "compute_cancelled":
            raise OperationCancelled() from error
        raise
    if cancelled() and isinstance(result, dict) and result.get("cancelled") is True:
        raise OperationCancelled()
    return result


def _dispatch_background_conversation(
    conversations: ConversationService,
    identity: SessionIdentity,
    task_id: str,
    content: str,
    *,
    request_id: str | None,
    selection: ModelSelection | None,
    attachments: list[dict[str, Any]],
    cancelled: Callable[[], bool],
) -> dict[str, Any]:
    """Persist the normal conversation response while its view is absent."""
    if cancelled():
        raise OperationCancelled()
    response = conversations.respond(
        identity, task_id, content, request_id=request_id, selection=selection, attachments=attachments,
    )
    if response.get("status") == "cancelled":
        raise OperationCancelled()
    return response


def _dispatch_background_report(
    conversation_tools: Any,
    identity: SessionIdentity,
    task_id: str,
    arguments: dict[str, str],
    cancelled: Callable[[], bool],
) -> dict[str, Any]:
    if cancelled():
        raise OperationCancelled()
    result = conversation_tools.call(identity, task_id, "reporter.run", arguments)
    if cancelled() and result.get("cancelled") is True:
        raise OperationCancelled()
    return result


def _data_from(value: dict[str, Any]) -> DataInterfaceSettings:
    provider_name = str(value.get("provider_name", "")).strip()
    if provider_name != "ifind-http":
        raise ValidationError("当前仅支持iFind数据服务")
    return DataInterfaceSettings(provider_name, None)


def _storage_from(value: dict[str, Any]) -> StorageExportSettings:
    reference = value.get("export_location_ref")
    if reference is not None and not isinstance(reference, str):
        raise ValidationError("export_location_ref must be a string")
    return StorageExportSettings(reference, bool(value.get("allow_user_selected_directory", True)))


def _preference_from(value: dict[str, Any], current: PreferenceSettings) -> PreferenceSettings:
    if set(value).difference({"language", "font_scale", "theme"}):
        raise ValidationError("preferences contains unsupported fields")
    language = value.get("language", current.language)
    font_scale = value.get("font_scale", current.font_scale)
    theme = value.get("theme", current.theme)
    if language not in {"zh-CN", "en-US"}:
        raise ValidationError("language must be zh-CN or en-US")
    try:
        scale = float(font_scale)
    except (TypeError, ValueError) as error:
        raise ValidationError("font_scale must be numeric") from error
    if not 0.8 <= scale <= 1.4 or theme not in {"light", "dark", "auto"}:
        raise ValidationError("preferences are outside allowed values")
    return PreferenceSettings(language=language, font_scale=scale, theme=theme)


def _json_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _default_settings(role: str) -> SettingsSnapshot:
    return SettingsSnapshot(
        role="admin" if role == "admin" else "sales",
        model_service=ModelServiceSettings("unconfigured", ""),
        data_interface=DataInterfaceSettings("unconfigured"),
        storage_export=StorageExportSettings(),
        preferences=PreferenceSettings(),
    )


def _is_missing_credential(error: UnavailableCapabilityError) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, UnavailableCapabilityError) and current.capability == "本机凭据缺失":
            return True
        current = current.__cause__
    return False


def _capabilities_for(policy: AuthorizationPolicy, role: Role) -> list[str]:
    candidates = ("optchat", "optdesk", "settings.read", "settings.preferences.write", "settings.model.write", "settings.model.local.write", "settings.storage.write", "settings.data.write", "settings.data.local.write", "task.create", "task.read", "conversation.tool.run", "module.page", "module.catalog", "module.run", "report.card.request", "report.quote.request", "report.full.request")
    return [capability for capability in candidates if policy.allows(role, capability)]


def _capability_summary(registry: PageRegistry) -> dict[str, Any]:
    manifest = registry.manifest
    return {
        "capability_version": manifest["capability_version"],
        "protocol_id": manifest["protocol_id"],
        "content_tree_hash": manifest["content_tree_hash"],
        "integrity": registry.integrity,
        "pages": [{"module": page.module_name, "path": page.capability_asset, "sha256": page.content_hash} for page in registry.all()],
    }




def main() -> None:
    parser = argparse.ArgumentParser(description="OptionHelper本地App平台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--capability-root", type=Path, help="开发/测试时显式指定已验证的Capability根目录")
    parser.add_argument("--brand-assets-root", type=Path, help="开发/测试时显式指定主题图标根目录")
    args = parser.parse_args()
    app = AppServer(
        host=args.host,
        port=args.port,
        app_data_dir=args.data_dir,
        capability_root=args.capability_root,
        brand_assets_root=args.brand_assets_root,
        authentication_mode="managed",
    )
    try:
        app._ensure_httpd()
        print(f"OptionHelper App listening on {app.url}", flush=True)
        app.start()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()


if __name__ == "__main__":
    main()
