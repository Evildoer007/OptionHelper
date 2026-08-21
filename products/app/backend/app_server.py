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
import re
import threading
from dataclasses import replace
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .agent_runtime.conversation_service import ConversationService
from .agent_runtime.context_builder import ContextBuilder, ResultStoreObservationBuilder
from .agent_runtime.agent_loop import AgentLoop
from .agent_runtime.recommender_adapter import AppConversationToolExecutor, RecommenderAdapter
from .agent_runtime.tool_dispatcher import ToolDispatcher
from .audit.audit_models import AuditEvent
from .audit.audit_service import LocalAuditService
from .authorization.policy import AuthorizationPolicy
from .authorization.roles import Role
from .capability_service import CapabilityServiceCaller
from .datafetcher_adapter import DataFetcherAdapter
from .errors import AuthorizationError, CapabilityIntegrityError, UnavailableCapabilityError, UserActionError, ValidationError
from .identity.identity_provider import IdentityProvider, LocalAuthenticationRequest, LocalAuthProvider
from .identity.login_handler import AuthenticationMode, InitialProvisionRequest, LoginHandler, LoginOutcome, LoginRequest
from .identity.password_store import PasswordCredentialStore
from .identity.session_identity import SessionIdentity
from .model_gateway.gateway import ModelGateway
from .model_gateway.deepseek_provider import complete_openai_compatible, decide_openai_compatible, discover_openai_models, validate_openai_base_url
from .model_gateway.provider_registry import ProviderRegistry
from .page_registry import PAGE_MODULES, PageRegistry
from .settings.connection_tester import ConnectionTester
from .settings.settings_models import (
    DataInterfaceSettings,
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
from .settings.model_catalog import built_in_provider, built_in_provider_catalog
from .settings.settings_service import SettingsService
from .secrets.secret_ref import SecretRef
from .secrets.secret_provider import SecretProvider
from .stores.data_store import DataStore
from .stores.contract_store import ContractStore
from .stores import _LocalDocumentStore
from .stores.result_store import ResultStore
from .stores.session_store import LocalSessionStore
from .stores.settings_store import LocalSettingsStore
from .task_runtime.job_runner import JobRunner
from .task_runtime.idempotency_store import ToolIdempotencyStore
from .task_runtime.task_service import TaskService
from .tool_gateway import ToolGateway
from .reporter_adapter import ReporterAdapter
from desktop.common.paths import AppPaths
from runtime.adapters.local_store import LocalDataStore


FRONTEND_ASSETS = frozenset({
    "login/index.html", "login/login-startup.js", "login/login.js",
    "optchat/index.html", "optchat/optchat.js",
    "optdesk/index.html", "optdesk/optdesk.js",
    "settings/index.html", "settings/settings.js", "settings/model-providers.css", "settings/general-settings.css", "settings/settings-shell.css",
    "shared/styles.css", "shared/refinement.css", "shared/theme-overrides.css", "shared/app.js", "shared/transition-scope.js", "shared/theme-bootstrap.js", "shared/theme.js", "shared/vol-surface.js",
})
_CAPABILITY_CSP = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'self'; form-action 'self'"


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
        self.settings = SettingsService(LocalSettingsStore(self._documents))
        self.tasks = TaskService(self._documents)
        self.data_assets = DataStore(self._documents)
        self.contracts = ContractStore(self._documents)
        self.results = ResultStore(self._documents)
        self.registry = PageRegistry(capability_root)
        if not self.registry.integrity["release_ready"] and not allow_unverified_capability:
            raise CapabilityIntegrityError("Embedded Capability is not release-ready; App launch is blocked")
        self.capability_services = CapabilityServiceCaller(
            self.registry,
            runtime_root=self._capability_runtime_root,
        )
        self.secret_provider = secret_provider or _platform_secret_provider(self._documents._root)
        self._secret_reference_provider = (
            "local-secret" if self.secret_provider.supports("local-secret") else "keychain"
        )
        self.datafetcher = DataFetcherAdapter(
            self.settings_for,
            self.capability_services,
            LocalDataStore(self._capability_runtime_root / "data"),
            self.secret_provider,
        )
        self.gateway = ToolGateway(
            self.registry,
            self.policy,
            ReporterAdapter(self.results),
            self.datafetcher,
            self.results,
            self.data_assets,
            self.contracts,
            self.tasks,
            capability_runtime_root=self._capability_runtime_root,
        )
        self.provider_registry = ProviderRegistry()
        self.provider_registry.register(
            "openai-compatible",
            lambda settings, reference, messages: complete_openai_compatible(
                settings,
                reference,
                messages,
                resolve_secret=lambda ref: self.secret_provider.resolve(ref, "模型服务凭据"),
            ),
            decision=lambda settings, reference, context: decide_openai_compatible(
                settings,
                reference,
                context,
                resolve_secret=lambda ref: self.secret_provider.resolve(ref, "模型服务凭据"),
            ),
        )
        self.tool_idempotency = ToolIdempotencyStore(self._documents._root / "tool-idempotency.sqlite3")
        self.tool_dispatcher = ToolDispatcher(
            self.gateway, JobRunner(), self.tasks, self.results, self.data_assets, self.tool_idempotency,
        )
        self.model_gateway = ModelGateway(self.settings_for, self.provider_registry, self.secret_provider)
        self.conversation_tools = AppConversationToolExecutor(
            self.tool_dispatcher,
            self.registry,
            self.results,
            self.contracts,
        )
        self.recommender = RecommenderAdapter(
            gateway=self.model_gateway,
            registry=self.registry,
            task_service=self.tasks,
            tool_executor=self.conversation_tools,
        )
        self.conversation_tools.bind_recommender(self.recommender)
        self.conversations = ConversationService(
            self.tasks,
            self.model_gateway,
            agent_loop=AgentLoop(
                gateway=self.model_gateway,
                context_builder=ContextBuilder(
                    self.tasks,
                    self.contracts,
                    self.policy,
                    catalog_version=str(self.registry.manifest["catalog_version"]),
                    result_store=self.results,
                ),
                tool_executor=self.conversation_tools,
                recommender=self.recommender,
                is_cancelled=self.tasks.is_cancelled,
                observation_builder=ResultStoreObservationBuilder(self.results),
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
            record = self.sessions.load(morsel.value)
            return SessionIdentity(
                principal_id=str(record["principal_id"]),
                tenant_id=str(record["tenant_id"]),
                role=Role(str(record["role"])),
                session_id=str(record["session_id"]),
                audience=str(record.get("audience", "option-helper-app")),
            )
        except (KeyError, ValueError) as error:
            raise AuthorizationError("session", "session is invalid or expired") from error

    def create_local_session(self, role: Role, principal_label: str | None, *, remember: bool = False) -> SessionIdentity:
        identity = self.identity_provider.authenticate(LocalAuthenticationRequest(role=role, principal_label=principal_label))
        self.sessions.save(identity, remember=remember)
        self.audit.record(AuditEvent(action="identity.local.authenticate", principal_id=identity.principal_id, outcome="succeeded", tenant_id=identity.tenant_id, decision="allow"))
        return identity

    def create_login_session(self, request: LoginRequest, *, client_host: str) -> LoginOutcome:
        outcome = self.login_handler.authenticate(request, client_host=client_host)
        self.sessions.save(outcome.identity, remember=outcome.remember)
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

    def provision_initial_administrator(
        self,
        request: InitialProvisionRequest,
        *,
        client_host: str,
    ) -> None:
        """Persist one managed local admin verifier without logging its password."""

        account = self.login_handler.provision_initial_administrator(request, client_host=client_host)
        self.audit.record(AuditEvent(
            action="identity.account.initialize",
            principal_id=account.principal_id,
            outcome="succeeded",
            tenant_id=account.tenant_id,
            decision="allow",
            reference="managed-local-initial-administrator",
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
        effective_model = local_model if local_model.provider_name != "unconfigured" else tenant.model_service
        return SettingsSnapshot(
            role=identity.role.value,
            model_service=effective_model,
            data_interface=tenant.data_interface,
            storage_export=principal.storage_export,
            preferences=principal.preferences,
            model_connections=principal.model_connections,
            active_model_connection_id=active_connection_id,
            model_providers=principal.model_providers,
            default_model_selection=local_selection,
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
                        "model_id": model.model_id,
                        "display_name": model.display_name or model.model_id,
                        "enabled": model.enabled,
                        "context_window": model.context_window,
                        "max_output_tokens": model.max_output_tokens,
                    }
                    for model in item.models
                ],
            }
            for item in settings.model_providers
        ]
        if not providers and settings.model_service.provider_name != "unconfigured" and settings.model_service.model_name:
            providers.append({
                "provider_id": "provider-default",
                "display_name": "默认模型提供方",
                "endpoint": settings.model_service.endpoint,
                "protocol": "openai-chat-completions",
                "scope": "provider",
                "credential_configured": settings.model_service.secret_ref is not None,
                "models": [{"model_id": settings.model_service.model_name, "display_name": settings.model_service.model_name, "enabled": True, "context_window": None, "max_output_tokens": None}],
            })
        builtin = []
        for provider_id, item in built_in_provider_catalog().items():
            builtin.append({
                "provider_id": provider_id,
                "display_name": str(item["display_name"]),
                "endpoint": str(item["endpoint"]),
                "protocol": str(item["protocol"]),
                "models": [
                    {"model_id": model.model_id, "display_name": model.display_name, "enabled": False}
                    for model in item["models"]
                ],
            })
        return {
            "schema": "optionhelper.model-providers.v1",
            "providers": providers,
            "builtins": builtin,
            "default_model_selection": (
                {"provider_id": settings.default_model_selection.provider_id, "model_id": settings.default_model_selection.model_id}
                if settings.default_model_selection else None
            ),
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
                "display_name": "默认模型提供方",
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
                reference = self.secret_reference(identity.tenant_id, "model") if snapshot.model_service.secret_ref else None
                model = replace(snapshot.model_service, secret_ref=reference)
                stored = replace(stored, model_service=model)
            else:
                reference = self.secret_reference(identity.tenant_id, "ifind") if snapshot.data_interface.secret_ref else None
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
            )
            self.settings.save(identity.principal_id, replace(principal, role=identity.role.value))
        else:
            principal = self._principal_settings(identity)
            if capability == "settings.preferences.write":
                principal = replace(principal, preferences=snapshot.preferences)
            elif capability == "settings.storage.write":
                principal = replace(principal, storage_export=snapshot.storage_export)
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
    ) -> SecretRef:
        if purpose not in {"model", "ifind"}:
            raise ValidationError("Credential purpose is not supported")
        owner = owner_id or tenant_id
        digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:24]
        suffix = "" if connection_id in {None, "primary"} and owner_id is None else f"/{connection_id or 'primary'}"
        return SecretRef(self._secret_reference_provider, f"optionhelper/{purpose}/{digest}{suffix}")

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
            )
            self.secret_provider.allow_reference(reference, "模型服务凭据")
            model = replace(
                model,
                secret_ref=reference if self._credential_is_saved(reference, "模型服务凭据") else None,
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
                        }
                        for model in provider.models
                    ],
                })
            except (ValidationError, ValueError):
                continue
            reference = self.secret_reference(
                identity.tenant_id, "model", owner_id=identity.principal_id, connection_id=normalized_provider.provider_id,
            )
            self.secret_provider.allow_reference(reference, "模型服务凭据")
            normalized_providers.append(replace(
                normalized_provider,
                secret_ref=reference if self._credential_is_saved(reference, "模型服务凭据") else None,
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
        )
        if normalized != snapshot:
            self.settings.save(identity.principal_id, normalized)
        return normalized

    def _normalized_tenant_settings(self, tenant_id: str, snapshot: SettingsSnapshot) -> SettingsSnapshot:
        model_reference = self.secret_reference(tenant_id, "model")
        data_reference = self.secret_reference(tenant_id, "ifind")
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
                secret_ref=model_reference
                if self._credential_is_saved(model_reference, "模型服务凭据")
                else None,
            )
        data = snapshot.data_interface
        if data.provider_name not in {"unconfigured", "ifind-http"}:
            data = DataInterfaceSettings("unconfigured")
        if data.provider_name != "unconfigured" and data.secret_ref != data_reference:
            data = replace(
                data,
                secret_ref=data_reference
                if self._credential_is_saved(data_reference, "iFind数据凭据")
                else None,
            )
        normalized = replace(snapshot, role="admin", model_service=model, data_interface=data)
        if normalized != snapshot:
            self.settings.save(self._tenant_settings_key(tenant_id), normalized)
        return normalized

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
        return replace(
            current,
            model_providers=tuple(providers),
            default_model_selection=ModelSelection(updated.provider_id, selected_id),
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
                "diagnostic_id": diagnostic_id,
            })
        except (ValidationError, ValueError):
            diagnostic_id = _diagnostic_id(
                self.headers.get("X-OptionHelper-Request-Id") or request_id,
                "input_invalid",
                "input",
            )
            self._audit_operational_failure(request_id, failure_code="input_invalid", stage="input")
            self._json(HTTPStatus.BAD_REQUEST, {
                "error": "invalid_request",
                "failure_code": "input_invalid",
                "stage": "input",
                "message": "本次输入不完整或格式不正确。请检查日期、条款和定价参数后重试。",
                "detail": "请求不符合受控输入规则",
                "next_step": "请检查日期、标的、条款与定价或回测参数后重试。",
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
        if path == "/api/auth/initialization":
            self.app.login_handler.require_local_peer(str(self.client_address[0]))
            self._json(HTTPStatus.OK, {
                "initialization_required": self.app.login_handler.requires_initialization(),
            })
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
            self._json(HTTPStatus.OK, {"tasks": self.app.tasks.list(identity)})
            return
        if path.startswith("/api/tasks/") and path.endswith("/reports"):
            identity = self._identity()
            self.app.policy.require(identity.role, "task.read")
            task_id = path.removeprefix("/api/tasks/").removesuffix("/reports").rstrip("/")
            self.app.tasks.get(identity, task_id)
            self._json(HTTPStatus.OK, {"reports": self.app.results.list_report_runs(identity, task_id)})
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
        if path.startswith("/api/tasks/"):
            identity = self._identity()
            self.app.policy.require(identity.role, "task.read")
            task_id = path.removeprefix("/api/tasks/")
            self._json(HTTPStatus.OK, {"task": self.app.tasks.get(identity, task_id)})
            return
        if path == "/api/settings":
            identity = self._identity()
            self.app.policy.require(identity.role, "settings.read")
            self._json(HTTPStatus.OK, {"settings": serialize_settings_public(self.app.settings_for(identity))})
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
        if path.startswith("/api/module-host/"):
            identity = self._identity()
            self.app.policy.require(identity.role, "module.host_context")
            module_name = path.removeprefix("/api/module-host/")
            query = parse_qs(parsed.query)
            task_id = query.get("task_id", [None])[0]
            analysis_case_id = query.get("analysis_case_id", [None])[0]
            if task_id is not None:
                self.app.tasks.get(identity, task_id)
            binding = self.app.contracts.get_current(
                identity,
                task_id,
                catalog_version=str(self.app.registry.manifest["catalog_version"]),
            ) if task_id is not None else None
            if task_id is not None and binding is None and module_name in {"pricer", "backtester"}:
                stale = self.app.contracts.get(identity, task_id)
                if stale is not None:
                    raise UserActionError(
                        "stale_task_contract",
                        "当前任务的合同来自旧产品目录。请新建研究任务后继续估值或历史回测。",
                    )
            if binding is not None:
                fingerprint = str(binding["contract_fingerprint"])
                analysis_case_id = f"case-{fingerprint[:24]}"
            self._json(HTTPStatus.OK, {
                "context": self.app.registry.host_context(
                    identity,
                    module_name,
                    analysis_case_id,
                    task_id=task_id,
                    candidate_id=f"candidate-{fingerprint[:24]}" if binding is not None else None,
                    catalog_version=str(binding["catalog_version"]) if binding is not None else None,
                    contract_fingerprint=fingerprint if binding is not None else None,
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
        body = self._body_json(allowed_plaintext_fields=_credential_fields_for(path))
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
        if path == "/api/auth/initialize":
            request = InitialProvisionRequest.from_payload(body)
            self.app.provision_initial_administrator(request, client_host=str(self.client_address[0]))
            self._json(
                HTTPStatus.CREATED,
                {"status": "initialized", "message": "首个本机管理员账号已创建。请使用该账号登录。"},
            )
            return
        if path == "/api/auth/logout":
            _only_fields(body, set())
            identity = self._identity()
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
            identity = self.app.create_local_session(role, label)
            self._json(HTTPStatus.OK, {"identity": identity.public(), "mode": "local-development"}, cookie=identity.session_id)
            return
        identity = self._identity()
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
            for field in ("data_asset_id", "tenant_id", "created_by", "content_hash", "media_type"):
                if reference.get(field) != registered.get(field):
                    raise ValidationError(f"DataAsset download {field} does not match its App registration")
            if not str(reference["media_type"]).lower().startswith("text/csv"):
                raise ValidationError("DataFetcher page downloads only registered CSV assets")
            self.app.audit.record(AuditEvent(
                action="data_asset.download",
                principal_id=identity.principal_id,
                tenant_id=identity.tenant_id,
                outcome="succeeded",
                decision="allow",
                reference=data_asset_id,
                request_id=request_id,
            ))
            self._bytes(HTTPStatus.OK, content, "text/csv; charset=utf-8", extra_headers={
                "Content-Disposition": f'attachment; filename="{data_asset_id}.csv"',
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
            task = self.app.tasks.delete(identity, task_id)
            self.app.audit.record(AuditEvent(action="task.delete", principal_id=identity.principal_id, tenant_id=identity.tenant_id, outcome="succeeded", decision="allow", reference=task_id, request_id=request_id))
            self._json(HTTPStatus.OK, {"task": task})
            return
        if path.startswith("/api/tasks/") and path.endswith("/messages"):
            self.app.policy.require(identity.role, "conversation.write")
            task_id = path.removeprefix("/api/tasks/").removesuffix("/messages").rstrip("/")
            _only_fields(body, {"content", "model_selection"})
            model_selection = _conversation_model_selection(body.get("model_selection"))
            response = self.app.conversations.respond(
                identity,
                task_id,
                str(body.get("content", "")),
                request_id=self.headers.get("X-Request-Id"),
                selection=model_selection,
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
                _only_fields(body, {"kind", "format"})
                kind = str(body.get("kind", "")).strip().lower()
                if kind not in {"card", "quote", "report"}:
                    raise ValidationError("report kind must be card, quote or report")
                output_format = str(body.get("format", "html")).strip().lower()
                if output_format not in {"html", "pdf"}:
                    raise ValidationError("report format must be html or pdf")
                arguments = {"kind": kind, "format": output_format}
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
            self._json(HTTPStatus.OK, {"settings": serialize_settings_public(snapshot)})
            return
        if path == "/api/settings/model-provider/credential":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"provider_id", "display_name", "endpoint", "protocol", "models", "default_model_id", "api_key"})
            profile = _model_provider_from(body)
            api_key = _credential_value(body, "api_key")
            reference = self.app.secret_reference(
                identity.tenant_id, "model", owner_id=identity.principal_id, connection_id=profile.provider_id,
            )
            snapshot = self.app.local_provider_snapshot(
                identity, profile, secret_ref=reference, default_model_id=_optional_model_id(body.get("default_model_id")),
            )
            saved = self.app.save_credential(
                identity, reference, api_key, "模型服务凭据", snapshot, "settings.model.local.write",
            )
            self._json(HTTPStatus.OK, {"settings": serialize_settings_public(saved), "credential_status": "stored"})
            return
        if path == "/api/settings/model-provider":
            self.app.policy.require(identity.role, "settings.model.local.write")
            _only_fields(body, {"provider_id", "display_name", "endpoint", "protocol", "models", "default_model_id"})
            profile = _model_provider_from(body)
            current = self.app.settings_for(identity)
            existing = next((item for item in current.model_providers if item.provider_id == profile.provider_id), None)
            if existing and existing.secret_ref and not _same_credential_origin(existing.endpoint, profile.endpoint):
                raise ValidationError("更换模型服务地址时，请同时重新粘贴该服务的API Key并保存。")
            snapshot = self.app.local_provider_snapshot(
                identity, profile, default_model_id=_optional_model_id(body.get("default_model_id")),
            )
            saved = self.app.save_settings(identity, snapshot, "settings.model.local.write")
            self._json(HTTPStatus.OK, {"settings": serialize_settings_public(saved)})
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
            self._json(HTTPStatus.OK, {"settings": serialize_settings_public(saved)})
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
            saved = self.app.save_settings(
                identity, replace(current, model_providers=providers, default_model_selection=next_selection),
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
            self._json(HTTPStatus.OK, {"settings": serialize_settings_public(saved)})
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
            self._json(HTTPStatus.OK, {"settings": serialize_settings_public(saved), "credential_status": "stored"})
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
            self._json(HTTPStatus.OK, {"settings": serialize_settings_public(saved)})
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
            self._json(HTTPStatus.OK, {"settings": serialize_settings_public(saved)})
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
            saved = self.app.save_settings(
                identity,
                replace(
                    current,
                    model_service=next_active.model_service if next_active else ModelServiceSettings("unconfigured", ""),
                    model_connections=remaining,
                    active_model_connection_id=next_active.connection_id if next_active else None,
                    model_providers=providers,
                    default_model_selection=next_selection,
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
            self._json(HTTPStatus.OK, {"settings": serialize_settings_public(saved)})
            return
        if path == "/api/settings/model/credential":
            self.app.policy.require(identity.role, "settings.model.write")
            _only_fields(body, {"provider_name", "endpoint", "model_name", "api_key"})
            api_key = _credential_value(body, "api_key")
            settings = _model_from(body)
            reference = self.app.secret_reference(identity.tenant_id, "model")
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
            self._json(HTTPStatus.OK, {"settings": serialize_settings_public(snapshot), "credential_status": "stored"})
            return
        if path == "/api/settings/data/credential":
            self.app.policy.require(identity.role, "settings.data.write")
            _only_fields(body, {"provider_name", "refresh_token"})
            refresh_token = _credential_value(body, "refresh_token")
            reference = self.app.secret_reference(identity.tenant_id, "ifind")
            credential = {"refresh_token": refresh_token}
            credential_record = json.dumps(credential, separators=(",", ":"))
            current = self.app.settings_for(identity)
            snapshot = self.app.save_credential(
                identity,
                reference,
                credential_record,
                "iFind数据凭据",
                replace(current, data_interface=DataInterfaceSettings(str(body["provider_name"]).strip(), reference)),
                "settings.data.write",
            )
            self.app.audit.record(AuditEvent(
                action="settings.data.credential_store", principal_id=identity.principal_id,
                tenant_id=identity.tenant_id, outcome="succeeded", decision="allow",
                reference=reference.key, request_id=request_id,
            ))
            self._json(HTTPStatus.OK, {"settings": serialize_settings_public(snapshot), "credential_status": "stored"})
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
                capability = "settings.data.write"
            else:
                snapshot = replace(current, storage_export=_storage_from(body))
                capability = "settings.storage.write"
            snapshot = self.app.save_settings(identity, snapshot, capability)
            self._json(HTTPStatus.OK, {"settings": serialize_settings_public(snapshot)})
            return
        if path == "/api/settings/test/model":
            if self.app.policy.allows(identity.role, "settings.model.write"):
                capability = "settings.model.write"
            else:
                self.app.policy.require(identity.role, "settings.model.local.write")
                capability = "settings.model.local.write"
            self.app.model_gateway.complete_for(identity, "settings-connection-test", "仅返回OK。")
            self.app.audit.record(AuditEvent(
                action="settings.connection_test", principal_id=identity.principal_id,
                tenant_id=identity.tenant_id, outcome="succeeded", decision="allow",
                reference=capability, request_id=request_id,
            ))
            self._json(HTTPStatus.OK, {"connection": {"provider_name": "model", "status": "available", "detail": "模型服务连接可用。"}})
            return
        if path.startswith("/api/settings/test/"):
            self.app.policy.require(identity.role, "settings.data.write")
            provider = path.removeprefix("/api/settings/test/")
            result = self.app.connection_tester.test(provider, identity, request_id=request_id)
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
            self.app.policy.require(
                identity.role,
                "module.catalog" if action in {"catalog", "status", "list_assets"} else "module.run",
            )
            module_context = self._module_host_context(tool)
            if tool == "reporter" and body.get("action") == "run":
                selection = body.get("selection")
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
                body = {"action": "run", "task_id": source["task_id"], "kind": kind, "selection": selection}
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
        }
        if not relative.startswith(page_prefix) and relative not in shared_designer_assets:
            raise AuthorizationError("module.page", "only registered module page assets are mountable")
        parts = Path(relative).parts
        shared_page_assets = {
            "assets/pages/module-host-bridge.js",
            "assets/pages/module-host-presentation.css",
            "assets/pages/module-host-presentation.js",
        }
        if relative not in shared_page_assets | shared_designer_assets and (
            len(parts) < 4 or parts[0:2] != ("assets", "pages") or parts[2] not in PAGE_MODULES
        ):
            raise ValidationError("Capability page asset path is invalid")
        self.app.policy.require(identity.role, "module.page")
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
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'self'; object-src 'none'; frame-src 'self'; frame-ancestors 'self'; form-action 'self'",
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

    def _body_json(self, *, allowed_plaintext_fields: frozenset[str] = frozenset()) -> dict[str, Any]:
        length_raw = self.headers.get("Content-Length", "0")
        try:
            length = int(length_raw)
        except ValueError as error:
            raise ValidationError("Content-Length is invalid") from error
        if length < 0 or length > 200_000:
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


def _unavailable_public_message(error: UnavailableCapabilityError) -> str:
    """Map reviewed App failures to a user-actionable, non-sensitive message."""

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
        "/api/auth/initialize": frozenset({"password"}),
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


def _credential_value(value: dict[str, Any], field: str) -> str:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError(f"{field}不能为空")
    return raw.strip()


def _platform_secret_provider(app_data_root: Path) -> SecretProvider:
    from .secrets.local_secret_store import LocalSecretStore

    return SecretProvider({"local-secret": LocalSecretStore(app_data_root / "credentials")})


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
        models.append(ModelCatalogEntry(model_id, model_name, enabled, context_window, max_output_tokens))
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
    candidates = ("optchat", "optdesk", "settings.read", "settings.preferences.write", "settings.model.write", "settings.model.local.write", "settings.storage.write", "settings.data.write", "task.create", "task.read", "conversation.tool.run", "module.page", "module.catalog", "module.run", "report.card.request", "report.quote.request", "report.full.request")
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
