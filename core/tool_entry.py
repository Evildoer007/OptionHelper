#!/usr/bin/env python3
"""OptionHelper结构化Tool及项目级Skill Host入口。

``call_tool``保持App授权边界；``--project-request``仅供无对话模型的批处理
模式使用。已有对话模型的Host直接调用``run_project_request``提交已验证选择，
再由local-development Host完成正式计算、Reporter和Designer闭环。
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, timedelta
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Mapping, Sequence


# A Skill installation is an immutable, hashed artifact.  Its entrypoint must
# never let Python create ``__pycache__`` inside the installation directory.
sys.dont_write_bytecode = True


ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from runtime.bootstrap import BootstrapError, bootstrap_runtime, local_runtime_scope
from runtime.adapters.local_host import (
    JsonHttpEndpoint,
    LocalHostAuthority,
    LocalHostError,
    LocalProjectLayout,
)
from runtime.adapters.local_store import LocalResultStore
from runtime.contracts.contract_api import ResolvedContract
from runtime.contracts.input_adapter import (
    is_explicit_demo_pricing_config,
    prepare_compute_request as _prepare_compute_request,
)
from runtime.protocol.models import CallerContext, DataAssetRef, ModuleRunRef
from runtime.protocol.module_host import ModuleHostContext
from runtime.protocol.tool_catalog import MODULES, tool_catalog
from runtime.protocol.version import DEVELOPMENT_RELEASE_ID, require_release_id


class ToolDispatchError(ValueError):
    pass


_GATEWAY_AUTHORITY = object()


class _GatewayAuthorization:
    """One in-process handoff after the App has verified the Host HMAC.

    Core deliberately does not duplicate App's session-key verifier.  The
    public calculator entry therefore accepts this sealed handoff only; raw
    CallerContext and ModuleHostContext values are not an authorization API.
    """

    __slots__ = ("caller_context", "host_context", "_authority")

    def __init__(
        self,
        caller_context: CallerContext,
        host_context: ModuleHostContext,
        authority: object,
    ) -> None:
        object.__setattr__(self, "caller_context", caller_context)
        object.__setattr__(self, "host_context", host_context)
        object.__setattr__(self, "_authority", authority)

    def __setattr__(self, name: str, value: object) -> None:
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError("授权交接对象不可修改")
        object.__setattr__(self, name, value)


def _authorize_verified_app_call(
    caller_context: CallerContext,
    host_context: ModuleHostContext,
) -> _GatewayAuthorization:
    """Private ToolGateway handoff for an already HMAC-verified App request."""

    if not isinstance(caller_context, CallerContext):
        raise ToolDispatchError("ToolGateway必须提供已验证的CallerContext")
    if not isinstance(host_context, ModuleHostContext) or host_context.host_kind != "app":
        raise ToolDispatchError("ToolGateway必须提供App Host验证后的ModuleHostContext")
    return _GatewayAuthorization(caller_context, host_context, _GATEWAY_AUTHORITY)


class ProjectRequestError(RuntimeError):
    """Human-readable standalone Host failure without a traceback contract."""

    def __init__(self, stage: str, message: str, *, missing: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.stage = stage
        self.missing = tuple(missing)


@contextmanager
def _cli_project_store_scope() -> Any:
    """Initialize the local project Store before a released CLI resolves runtime.

    The App supplies its own scoped Store.  A project-level Skill CLI has no
    App Host, so it owns one explicit current-project scope instead of asking
    the user to export three implementation variables before every command.
    Development source already has a valid runtime and remains unchanged.
    """

    try:
        bootstrap_runtime(mutate_sys_path=False)
    except BootstrapError:
        layout = LocalProjectLayout.create(skill_root=ROOT, project_root=Path.cwd())
        layout.initialize()
        with local_runtime_scope(layout.data_root, layout.result_root):
            yield
    else:
        yield


_RECOMMENDATION_REQUEST_FIELDS = frozenset({"prompt", "constraints"})
_REPORT_REQUEST_FIELDS = frozenset({
    "prompt", "constraints", "term_overrides", "pricing_config", "backtest_config",
    "output_type", "format", "title", "selection", "selections", "delivery_mode", "quote_variants",
})
_MODULE_REQUEST_FIELDS = frozenset({
    "module", "product_id", "identity", "term_overrides", "pricing_config",
    "backtest_config", "data_window", "horizon",
})
_AGENT_SELECTION_FIELDS = frozenset({
    "product_id", "underlyings", "reason", "suitable_for", "not_suitable_for", "main_risks",
})


_HOST_PRIVATE_FIELDS = {"tenantid", "principalid", "sessionid", "callercontext", "hostcontext", "capabilitytoken", "datastore", "datastoreport"}
_RAW_PATH_FIELDS = {
    "requestpath", "outputroot", "historyreference", "resultdir",
    "directory", "path", "filepath", "localcsv", "csvpath",
}


def _normalized_request_key(key: object) -> str:
    """Compare browser keys without accepting spelling variations as a bypass."""
    return "".join(character for character in str(key).casefold() if character.isalnum())


def _reject_untrusted_request_fields(value: object) -> None:
    """Keep Host dependencies and filesystem controls out of Tool request roots.

    The App invokes this function after compiling a friendly browser payload.
    Compiled ``DataAssetRef`` objects legitimately contain tenant metadata, so
    only root keys are untrusted at this point; App ingress validates nested
    browser payloads before that compilation happens.
    """
    if not isinstance(value, Mapping):
        raise ToolDispatchError("正式Tool请求必须为对象")
    for key in value:
        normalized = _normalized_request_key(key)
        if normalized in _HOST_PRIVATE_FIELDS:
            raise ToolDispatchError(f"{key}由App Host管理，正式请求不得提交")
        if normalized in _RAW_PATH_FIELDS:
            raise ToolDispatchError(f"{key}不是正式Tool输入；请使用受控的DataAssetRef")


def _mapping_field(body: Mapping[str, Any], name: str) -> dict[str, Any]:
    """Read an optional public request object without accepting loose values."""

    value = body.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProjectRequestError("request", f"{name}必须为对象。")
    _reject_untrusted_request_fields(value)
    return dict(value)


def _horizon_years(value: object) -> float | None:
    """Convert the Recommender's canonical horizon to an explicit contract tenor."""

    match = re.fullmatch(r"(\d+)(个月|年)", str(value or "").strip())
    if match is None:
        return None
    amount = int(match.group(1))
    return amount / 12.0 if match.group(2) == "个月" else float(amount)


def _horizon_label(value: object) -> str | None:
    """Render the frozen contract tenor instead of repeating model assumptions."""

    try:
        years = float(value)
    except (TypeError, ValueError):
        return None
    if years <= 0:
        return None
    months = round(years * 12)
    if abs(years * 12 - months) <= 1e-8:
        return f"{months // 12}年" if months % 12 == 0 else f"{months}个月"
    return f"{years:g}年"


def _request_requires_trading_calendar(
    product_id: object,
    *,
    identity: Mapping[str, Any],
    term_overrides: Mapping[str, Any],
) -> bool:
    """Inspect immutable product metadata before any calendar is available.

    The actual contract is resolved only after the Host has authenticated the
    calendar bytes.  This preflight must therefore never fabricate a calendar
    merely to ask whether the product needs one.
    """

    del identity, term_overrides
    from runtime.contracts.contract_api import get_product

    product = get_product(str(product_id).strip())
    terms = product.get("terms") if isinstance(product, Mapping) else None
    return bool(isinstance(terms, Mapping) and terms.get("monitor"))


def _request_hash(project_root: Path, body: Mapping[str, Any]) -> str:
    """Keep changed terms and inputs from colliding with a prior immutable run."""

    payload = json.dumps(dict(body), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{project_root}:{payload}".encode()).hexdigest()[:20]


def prepare_compute_request(
    module: str,
    request: Mapping[str, Any],
    *,
    data_refs: tuple[Mapping[str, Any], ...] = (),
    resolved_contract: Mapping[str, Any] | None = None,
    data_store: Any | None = None,
    product_snapshot_provider: Any | None = None,
) -> dict[str, Any]:
    """Compile a calculator request through Core's sole contract resolver."""
    if module not in {"payoffer", "pricer", "backtester"}:
        raise ToolDispatchError(f"模块{module}不是计算模块")
    bootstrap_runtime()
    friendly_request = request
    if module == "pricer" and resolved_contract is None:
        # Pricer owns its market-date and contract-reference compilation.  Core
        # remains the only contract resolver and receives an already data-backed
        # identity, never a fabricated S0Raw.
        from modules.pricer.input_defaults import compile_pricer_input_defaults

        friendly_request = compile_pricer_input_defaults(
            request, data_refs=data_refs, data_store=data_store,
        )
    return _prepare_compute_request(
        module,
        friendly_request,
        data_refs=data_refs,
        resolved_contract=resolved_contract,
        data_store=data_store,
        product_snapshot_provider=product_snapshot_provider,
    )


def call_tool(
    module: str,
    request: Mapping[str, Any],
    *,
    authorization: _GatewayAuthorization | None = None,
    result_store: Any | None = None,
    data_store: Any | None = None,
) -> Mapping[str, Any]:
    """Formal App-only entry; raw Context values cannot authorize a call."""

    if module not in MODULES:
        raise ToolDispatchError(f"未知模块：{module}")
    _reject_untrusted_request_fields(request)
    if not isinstance(authorization, _GatewayAuthorization) or authorization._authority is not _GATEWAY_AUTHORITY:
        raise ToolDispatchError("正式Tool调用只能由已验证的App ToolGateway发起")
    caller_context = authorization.caller_context
    host_context = authorization.host_context
    if host_context.module != module:
        raise ToolDispatchError("正式Tool调用必须携带模块一致的ModuleHostContext")
    if host_context.host_kind != "app":
        raise ToolDispatchError("正式Tool调用只接受App Host授权上下文")
    return _call_validated_tool(
        module, request, caller_context=caller_context, host_context=host_context,
        result_store=result_store, data_store=data_store,
    )


def call_local_tool(
    module: str,
    request: Mapping[str, Any],
    *,
    authority: LocalHostAuthority,
    request_id: str,
    host_context: ModuleHostContext,
    result_store: Any | None = None,
    data_store: Any | None = None,
) -> Mapping[str, Any]:
    """Execute only an in-process, Host-issued local-development context."""

    if not isinstance(authority, LocalHostAuthority):
        raise ToolDispatchError("项目级Tool调用缺少本机Host授权器")
    authority.verify(host_context)
    if host_context.host_kind != "local-development" or host_context.module != module:
        raise ToolDispatchError("项目级Tool调用必须使用模块一致的local-development上下文")
    try:
        caller_context = authority.caller(request_id=request_id)
    except Exception as error:
        raise ToolDispatchError("项目级Tool调用无法生成本机CallerContext") from error
    return _call_validated_tool(
        module, request, caller_context=caller_context, host_context=host_context,
        result_store=result_store, data_store=data_store,
    )


def _call_validated_tool(
    module: str,
    request: Mapping[str, Any],
    *,
    caller_context: CallerContext,
    host_context: ModuleHostContext,
    result_store: Any | None,
    data_store: Any | None,
) -> Mapping[str, Any]:
    if module not in MODULES:
        raise ToolDispatchError(f"未知模块：{module}")
    _reject_untrusted_request_fields(request)
    action = str(request.get("action", "run" if {"contract", "payoff_input"}.intersection(request) else "catalog")).strip().lower()
    if action in {"catalog", "default", "status"}:
        if "module.catalog" not in caller_context.capabilities or "module.catalog" not in host_context.request_policy:
            raise ToolDispatchError("只读目录调用必须同时由CallerContext和ModuleHostContext授权module.catalog")
    elif not set(caller_context.capabilities).intersection(host_context.request_policy).intersection(
        {"module.run", "conversation.tool.run"}
    ):
        raise ToolDispatchError("正式执行必须具有匹配的module.run或conversation.tool.run授权")
    if not caller_context.tenant_id or not caller_context.principal_id or not caller_context.session_id:
        raise ToolDispatchError("CallerContext身份范围不完整")
    for reference in host_context.result_refs:
        if reference.tenant_id != caller_context.tenant_id or (
            host_context.task_id is not None and reference.task_id != host_context.task_id
        ):
            raise ToolDispatchError("ModuleHostContext结果引用超出CallerContext租户或任务范围")
    needs_data_store = module == "backtester" or (
        module == "pricer"
        and action == "run"
        and not is_explicit_demo_pricing_config(request.get("pricing_config"))
    )
    if data_store is not None:
        if module not in {"pricer", "backtester"} or action != "run":
            raise ToolDispatchError("外置DataStorePort仅允许Host注入正式Pricer或Backtester运行")
        if not callable(getattr(data_store, "read_bytes", None)):
            raise ToolDispatchError("正式计算模块必须由Host注入DataStorePort")
    elif needs_data_store and action == "run":
        raise ToolDispatchError("正式Pricer或Backtester运行必须由Host注入DataStorePort")
    bootstrap_runtime()
    try:
        service = importlib.import_module(f"modules.{module}.service")
    except ModuleNotFoundError as error:
        if error.name in {f"modules.{module}", f"modules.{module}.service"}:
            raise ToolDispatchError(f"模块{module}尚未完成结构化Tool接入") from error
        raise
    handler = getattr(service, "call_tool", None)
    if not callable(handler):
        raise ToolDispatchError(f"模块{module}未提供call_tool(request)")
    kwargs = {"host_context": host_context}
    if result_store is not None:
        kwargs["result_store"] = result_store
    if data_store is not None and module in {"pricer", "backtester"}:
        kwargs["data_store"] = data_store
    kwargs["tenant_id"] = caller_context.tenant_id
    result = handler(dict(request), **kwargs)
    if not isinstance(result, Mapping):
        raise ToolDispatchError(f"模块{module}返回值必须为JSON对象")
    return dict(result)


class _LocalKnowledgePort:
    """Read-only projection of the verified Skill Knowledger snapshot."""

    def __init__(self, paths: Any, catalog_version: str) -> None:
        self._paths = paths
        self._catalog_version = catalog_version

    def search(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(payload.get("catalog_version", "")) != self._catalog_version:
            raise ProjectRequestError("knowledger", "推荐资料版本与当前Skill不一致。")
        from runtime.knowledger import load_registry

        registry = load_registry()
        queries = payload.get("queries", ())
        query = " ".join(str(item).strip().lower() for item in queries if str(item).strip()) if isinstance(queries, Sequence) and not isinstance(queries, str) else str(queries).lower()
        products = registry.get("products", {})
        matched = [
            (product_id, product)
            for product_id, product in products.items()
            if isinstance(product, Mapping) and _knowledge_match(query, product_id, product)
        ]
        if not matched:
            matched = [
                (product_id, product)
                for product_id, product in products.items()
                if isinstance(product, Mapping) and bool((product.get("identity") or {}).get("entry_status"))
            ]
        evidence: list[dict[str, Any]] = []
        for product_id, product in matched[:8]:
            identity = dict(product.get("identity", {}))
            name = str(identity.get("name_zh") or product_id)
            optionlist = _optionlist_excerpt(self._paths.project_root / "references" / "optionlist.md", product_id, name)
            optionlib = _optionlib_excerpt(self._paths.project_root / "references" / "optionlib.md", product_id, name)
            ready = bool(identity.get("entry_status"))
            evidence.extend((
                _evidence(product_id, "optionlist", optionlist, identity, ready, self._catalog_version, "ready" if optionlist else "unavailable"),
                _evidence(product_id, "optionlib", optionlib or f"OptionLib未找到{product_id}章节", identity, ready, self._catalog_version, "ready" if optionlib else "unavailable"),
                _evidence(product_id, "optionreg_status", f"{product_id} {name} entry_status={ready}", identity, ready, self._catalog_version),
            ))
        return {"ok": True, "catalog_version": self._catalog_version, "evidence": evidence}


class _DesignerPort:
    def call_tool(self, request: dict[str, Any]) -> dict[str, Any]:
        from modules.designer.service import call_tool

        return dict(call_tool(request))


class _SelectionPort:
    def __init__(self, source: Mapping[str, Any]) -> None:
        self._source = dict(source)

    def list_report_sources(self, *, tenant_id: str, task_id: str | None = None, query: str | None = None) -> Mapping[str, Any]:
        del query
        rows = [self._source] if tenant_id == self._source["tenant_id"] and (task_id is None or task_id == self._source["task_id"]) else []
        return {"tenant_id": tenant_id, "sources": rows}

    def get_report_source(self, *, tenant_id: str, source_id: str) -> Mapping[str, Any]:
        if tenant_id != self._source["tenant_id"] or source_id != self._source["source_id"]:
            raise KeyError(source_id)
        return dict(self._source)


def _knowledge_match(query: str, product_id: str, product: Mapping[str, Any]) -> bool:
    identity = product.get("identity", {})
    name = str(identity.get("name_zh", "")).lower() if isinstance(identity, Mapping) else ""
    if product_id.lower() in query or (name and name in query):
        return True
    direction = "看涨" if any(word in query for word in ("上涨", "看涨", "上行", "走高")) else "看跌" if any(word in query for word in ("下跌", "看跌", "下行")) else ""
    return bool(direction and direction in name)


def _optionlist_excerpt(path: Path, product_id: str, fallback: str) -> str:
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if f"| {product_id} |" in line or f"|{product_id}|" in line.replace(" ", ""):
                return line[:240]
    return f"{product_id} {fallback}"


def _optionlib_excerpt(path: Path, product_id: str, fallback: str) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"^###\s+{re.escape(product_id)}\s+.*$", text, re.MULTILINE)
    if match is None:
        return None
    end = text.find("\n### ", match.end())
    return text[match.start(): len(text) if end < 0 else end][:1800] or fallback


def _evidence(product_id: str, source: str, excerpt: str, identity: Mapping[str, Any], entry_status: bool, catalog_version: str, library_status: str = "ready") -> dict[str, Any]:
    normalized = str(excerpt).strip() or product_id
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return {
        "evidence_id": hashlib.sha256(f"{source}:{product_id}:{digest}".encode()).hexdigest()[:24],
        "product_id": product_id,
        "catalog_version": catalog_version,
        "source": source,
        "section": product_id,
        "library_status": library_status,
        "excerpt_hash": digest,
        "excerpt": normalized,
        "identity": dict(identity),
        "entry_status": entry_status,
    }


def _catalog_version(paths: Any) -> str:
    """Return the sole public catalog version for the installed protocol.

    An optional catalog declaration may attest this value, but it cannot
    introduce a second release vocabulary or revive an old storage layout.
    """

    path = paths.knowledger_root / "catalog-version.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        value = None
    declared = value.get("catalog_version") if isinstance(value, Mapping) else None
    if declared is not None:
        try:
            require_release_id(declared, "catalog_version")
        except ValueError as error:
            raise ProjectRequestError("knowledger", "资料目录版本与当前正式协议不一致。") from error
        return declared
    return DEVELOPMENT_RELEASE_ID


def _local_ifind_refresh_token(project_root: str | Path | None = None) -> str:
    """Resolve the optional project-local Skill token without changing App/Core auth."""

    # Project memory belongs to the standalone Skill only. App/Capability
    # calls must continue through their Host SecretProvider boundary. The
    # packaged Skill keeps this helper beside the copied entrypoint, while
    # repository Core and App launches do not.
    if os.environ.get("OPTIONHELPER_CAPABILITY_ROOT", "").strip():
        return ""
    if not (Path(__file__).resolve().with_name("environment_check.py")).is_file():
        return ""
    try:
        from environment_check import load_ifind_refresh_token
    except ImportError:
        return ""
    try:
        return str(load_ifind_refresh_token(
            project_root=project_root,
            skill_root=Path(__file__).resolve().parents[1],
        ) or "").strip()
    except (OSError, ValueError):
        return ""


def _activate_ifind_refresh_token(token: str) -> None:
    """Expose a resolved local token only to the current Skill process."""

    if token:
        os.environ["IFIND_REFRESH_TOKEN"] = token


def _configuration(
    *,
    require_ifind: bool = True,
    project_root: str | Path | None = None,
) -> dict[str, str]:
    host_url = os.environ.get("OPTIONHELPER_HOST_URL", "").strip()
    if host_url:
        return {"mode": "host", "host_url": host_url}
    ifind = _local_ifind_refresh_token(project_root)
    if require_ifind and not ifind:
        missing = ["iFind Refresh Token"]
    else:
        missing = []
    if missing:
        raise ProjectRequestError(
            "configuration",
            "OptionHelper尚未完成运行配置，请在Secret Store中补充：" + "、".join(missing) + "。",
            missing=missing,
        )
    public_values: dict[str, str] = {"mode": "direct"}
    if require_ifind:
        public_values["ifind"] = ifind
    return public_values


def _progress(callback: Callable[[Mapping[str, Any]], None] | None, stage: str, message: str, status: str = "running") -> None:
    if callback is not None:
        callback({"type": "progress", "stage": stage, "status": status, "message": message})


def _confirmed_constraints(
    body: Mapping[str, Any],
    prompt: str,
    *,
    output_type: str | None = None,
) -> dict[str, Any]:
    """Apply structured user revisions before extracting natural-language facts.

    A structured value is an explicit user instruction.  The prompt is then
    applied last so a later sentence such as "改成3个月" wins over an earlier
    value from the same turn.
    """

    from modules.recommender.interaction import merge_confirmed_constraints

    defaults: dict[str, Any] = {
        "horizon": "3个月",
        "max_loss": "100%",
        "principal_fluctuation": True,
    }
    if output_type:
        defaults["output_type"] = output_type
        defaults["format"] = "html"
    defaults.update(_mapping_field(body, "constraints"))
    return merge_confirmed_constraints(defaults, (prompt,))


def _recommendation_case(
    *,
    prompt: str,
    workflow: str,
    requested_outputs: tuple[str, ...],
    body: Mapping[str, Any],
    paths: Any,
    layout: LocalProjectLayout,
    authority: LocalHostAuthority,
) -> Any:
    from modules.recommender.models import RecommendationCase

    catalog_version = _catalog_version(paths)
    task_hash = _request_hash(layout.project_root, body)
    return RecommendationCase(
        analysis_case_id=f"case-{task_hash}",
        task_id=f"project-{task_hash}",
        tenant_id=authority.tenant_id,
        prompt=prompt.replace("波动变大", "波动增大"),
        catalog_version=catalog_version,
        run_id=f"recommend-{task_hash}",
        requested_outputs=requested_outputs,
        audience="professional",
        confirmed_constraints=_confirmed_constraints(
            body, prompt, output_type=requested_outputs[0] if requested_outputs else None,
        ),
    )


def _primary_ready_candidate(recommendation: Mapping[str, Any]) -> dict[str, Any]:
    """Bind a report to the ranked primary without discarding useful alternatives.

    Exploration may contain up to three candidates.  A formal calculation and
    ReportUnit must nevertheless bind exactly one contract, so an explicit
    report request authorizes the ranked primary when it is executable.
    """

    values = recommendation.get("candidates")
    if not isinstance(values, list) or not values:
        raise ProjectRequestError("recommender", "当前需求没有形成可执行候选，请补充会影响结构选择的风险约束。")
    primary_id = str(recommendation.get("primary_candidate_id") or "")
    candidates = [dict(item) for item in values if isinstance(item, Mapping)]
    candidate = next((item for item in candidates if str(item.get("candidate_id")) == primary_id), candidates[0])
    if candidate.get("library_status") != "ready" or candidate.get("missing_inputs"):
        raise ProjectRequestError("recommender", "主候选的资料或必要条款不完整，暂不能执行正式计算或生成报告。")
    return candidate


def _agent_native_candidate(
    selection: Mapping[str, Any],
    *,
    paths: Any,
    task_hash: str,
) -> dict[str, Any]:
    """Turn a Host model's public recommendation into one verified candidate.

    The current conversation host is already the model.  It must never be
    asked to configure a second model gateway merely to pass its researched
    choice into the formal calculation/report pipeline.
    The Host may choose a product and explain it, but Core still validates the
    product against OptionReg and creates all identities, hashes and run IDs.
    """

    unknown = set(selection).difference(_AGENT_SELECTION_FIELDS)
    if unknown:
        raise ProjectRequestError("request", "正式报告选择只接受产品、标的和面向读者的研究理由。")
    product_id = str(selection.get("product_id") or "").strip()
    raw_underlyings = selection.get("underlyings")
    if isinstance(raw_underlyings, str):
        underlyings = [item.strip() for item in raw_underlyings.split(",") if item.strip()]
    elif isinstance(raw_underlyings, Sequence) and not isinstance(raw_underlyings, (bytes, bytearray)):
        underlyings = [str(item).strip() for item in raw_underlyings if str(item).strip()]
    else:
        underlyings = []
    if not product_id or not underlyings:
        raise ProjectRequestError("request", "正式报告需要已确认的产品编号和标的代码。")

    from runtime.contracts.contract_engine import load_registry

    registry = load_registry()
    product = registry.get("products", {}).get(product_id)
    if not isinstance(product, Mapping) or not bool(dict(product.get("identity") or {}).get("entry_status")):
        raise ProjectRequestError("knowledger", "所选产品不在当前已发布产品目录中，不能用于正式计算或报告。")
    identity = dict(product.get("identity") or {})
    return {
        "candidate_id": f"host-{task_hash}",
        "product_id": product_id,
        "product_name": str(identity.get("name_zh") or product_id),
        "underlyings": underlyings,
        "rank": 1,
        "reason": str(selection.get("reason") or "该结构与已确认的市场观点和期限相匹配。"),
        "suitable_for": list(selection.get("suitable_for") or []),
        "not_suitable_for": list(selection.get("not_suitable_for") or []),
        "main_risks": list(selection.get("main_risks") or []),
        "library_status": "ready",
        "missing_inputs": [],
    }


def _project_delivery_mode(body: Mapping[str, Any], prompt: str, *, output_type: str) -> str:
    """Resolve single versus comparison delivery without creating new output types."""

    explicit = str(body.get("delivery_mode") or "").strip().lower()
    request_lower = prompt.casefold()
    inferred = "comparison" if any(token in request_lower for token in (
        "multicard", "multireport", "横向对比", "对比卡片", "对比报告",
    )) else "single"
    delivery_mode = explicit or inferred
    if delivery_mode not in {"single", "comparison"}:
        raise ProjectRequestError("request", "交付方式只能选择单结构或横向对比。")
    if output_type == "quote" and delivery_mode == "comparison":
        raise ProjectRequestError("request", "参考报价已经按多结构汇总，不使用横向对比模式。")
    return delivery_mode


def _explicit_project_candidates(
    body: Mapping[str, Any],
    *,
    paths: Any,
    task_hash: str,
) -> list[dict[str, Any]]:
    """Validate one or more conversation-selected products in user order."""

    has_single = "selection" in body and body.get("selection") is not None
    has_multiple = "selections" in body and body.get("selections") is not None
    if has_single and has_multiple:
        raise ProjectRequestError("request", "单结构选择和多候选选择不能同时提交。")
    if has_multiple:
        raw = body.get("selections")
        if not isinstance(raw, list) or not raw or not all(isinstance(item, Mapping) for item in raw):
            raise ProjectRequestError("request", "多候选选择必须是非空的结构选择数组。")
        candidates = [
            _agent_native_candidate(item, paths=paths, task_hash=f"{task_hash}-{index:02d}")
            for index, item in enumerate(raw, start=1)
        ]
    elif has_single:
        raw = body.get("selection")
        if not isinstance(raw, Mapping):
            raise ProjectRequestError("request", "结构选择必须是对象。")
        candidates = [_agent_native_candidate(raw, paths=paths, task_hash=task_hash)]
    else:
        return []
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank
    return candidates


def _ordered_ready_candidates(values: object) -> list[dict[str, Any]]:
    """Return executable recommender candidates in stable frozen-rank order."""

    candidates = [
        dict(item)
        for item in values if isinstance(item, Mapping)
        and item.get("library_status") == "ready" and not item.get("missing_inputs")
    ] if isinstance(values, list) else []

    def rank(item: tuple[int, Mapping[str, Any]]) -> tuple[int, int]:
        try:
            value = int(item[1].get("rank") or 1_000_000)
        except (TypeError, ValueError):
            value = 1_000_000
        return value, item[0]

    return [item for _, item in sorted(enumerate(candidates), key=rank)]


def run_recommendation_request(
    request: str | Mapping[str, Any],
    *,
    project_root: str | Path | None = None,
    agent_port: Any | None = None,
    progress: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run recommendation only.  This path never fetches data or creates a report."""

    body = {"prompt": request} if isinstance(request, str) else dict(request)
    unknown = set(body).difference(_RECOMMENDATION_REQUEST_FIELDS)
    if unknown:
        raise ProjectRequestError("request", "结构推荐只接受需求文本和已确认条件。")
    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        raise ProjectRequestError("request", "请提供需要筛选的期权结构需求。")
    if agent_port is None:
        config = _configuration(require_ifind=False, project_root=project_root)
        if config["mode"] == "host":
            _progress(progress, "recommendation", "正在分析需求并整理候选结构")
            response = JsonHttpEndpoint(config["host_url"]).post("project/recommend", body)
            if response.get("ok") is not True:
                raise ProjectRequestError("host", str(response.get("message") or "研究服务未能完成结构推荐。"))
            return response
        raise ProjectRequestError(
            "request",
            "当前对话尚未完成候选确认，暂不能生成正式交付；请在已集成OptionHelper的对话入口中重试。",
        )
    paths = bootstrap_runtime()
    layout = LocalProjectLayout.create(skill_root=paths.project_root, project_root=project_root or Path.cwd())
    layout.initialize()
    authority = LocalHostAuthority(layout)
    from modules.recommender.service import RecommenderService

    case = _recommendation_case(
        prompt=prompt, workflow="recommendation", requested_outputs=(), body=body,
        paths=paths, layout=layout, authority=authority,
    )
    _progress(progress, "recommendation", "正在分析需求并整理候选结构")
    result = RecommenderService(
        agent_port=agent_port, knowledge_port=_LocalKnowledgePort(paths, case.catalog_version),
    ).recommend_fixed(case, workflow="recommendation")
    recommendation = result.get("recommendation_set")
    if not isinstance(recommendation, Mapping):
        raise ProjectRequestError("recommender", "未能形成可验证的候选结构。")
    status = str(recommendation.get("status") or "unavailable")
    if status == "pending_question":
        return {
            "ok": False,
            "status": "needs_input",
            "message": str(recommendation.get("next_question") or "研究条件仍不完整。"),
            "constraints": dict(case.confirmed_constraints),
            "recommendation": {"candidates": []},
        }
    if status == "unavailable":
        raise ProjectRequestError("recommender", "当前条件不足以形成可验证推荐。")
    candidates = [
        {
            key: value for key, value in dict(item).items()
            if key in {"product_id", "product_name", "rank", "reason", "suitable_for", "not_suitable_for", "main_risks"}
        }
        for item in recommendation.get("candidates", ()) if isinstance(item, Mapping)
    ]
    _progress(progress, "recommendation", "候选结构已整理完成", "completed")
    return {
        "ok": True,
        "status": "completed",
        "message": "结构推荐已完成。",
        "constraints": dict(case.confirmed_constraints),
        "recommendation": {"candidates": candidates},
    }


def _data_window_dates(data_window: Mapping[str, Any]) -> tuple[date, date]:
    """Validate one Host-owned market-data interval before any provider call."""

    try:
        end_date = date.fromisoformat(str(data_window.get("end_date") or date.today().isoformat()))
        start_date = date.fromisoformat(str(data_window.get("start_date") or (end_date - timedelta(days=1_460)).isoformat()))
    except ValueError as error:
        raise ProjectRequestError("datafetcher", "数据区间必须使用YYYY-MM-DD日期。") from error
    if start_date > end_date:
        raise ProjectRequestError("datafetcher", "数据起始日不得晚于结束日。")
    return start_date, end_date


def _fetch_local_data_asset(
    *,
    layout: LocalProjectLayout,
    authority: LocalHostAuthority,
    task_id: str,
    underlyings: Sequence[str],
    data_window: Mapping[str, Any],
    request_id: str,
    progress: Callable[[Mapping[str, Any]], None] | None,
) -> tuple[Any, str]:
    """Fetch raw history without claiming that the requested end is observed.

    The request end remains the user's valuation request.  During a trading
    day the last close can legitimately precede it.  A verified calendar is
    deliberately *not* passed to DataFetcher here: its strict completeness
    check rightly rejects every declared session without an observed close.
    Once the provider returns its actual per-asset coverage, the Host binds a
    historical-calendar slice ending at the common observed date and keeps the
    future path calendar as a separate authenticated reference.
    """

    from modules.datafetcher.config import DataFetcherConfig
    from modules.datafetcher.service import fetch_data

    start_date, end_date = _data_window_dates(data_window)
    _progress(progress, "data", "正在准备市场数据")
    result = fetch_data({
        "asset_ids": [str(item) for item in underlyings],
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "fields": ["open", "high", "low", "close", "adj_open", "adj_high", "adj_low", "adj_close", "volume"],
        "provider": "ifind_http",
        "source_priority": ["ifind_http"],
        "cache_policy": "extend_only",
    }, authority.caller(request_id=request_id), config=DataFetcherConfig(
        data_root=layout.data_root,
        result_root=layout.result_root,
        cache_root=layout.data_root / "datafetcher-cache",
    ), task_id=task_id)
    if not result.ok or result.run.data_asset_ref is None:
        error = result.run.error or {}
        raise ProjectRequestError(
            "datafetcher",
            str(error.get("message") or "iFind行情未能形成可验证DataAssetRef，请检查凭据、网络或数据权限。"),
        )
    data_ref = result.run.data_asset_ref
    valuation_date = _latest_available_data_date(data_ref, requested_date=end_date)
    _progress(progress, "data", "市场数据已准备完成", "completed")
    return data_ref, valuation_date


def _persist_calendar_bound_history_asset(
    *,
    data_ref: DataAssetRef,
    calendar_ref: DataAssetRef,
    data_store: Any,
    principal_id: str,
) -> DataAssetRef:
    """Persist the Host-verified calendar binding as a new immutable asset ref.

    A LocalDataStore authenticates all reference metadata.  Mutating the
    DataFetcher reference in memory would therefore fail closed at read time.
    The Host instead reuses the verified CSV bytes and commits a second,
    metadata-bound ref.  Its calendar identity and sessions are derived only
    from the verified calendar asset, never from browser or provider metadata.
    """

    if data_ref.created_by != principal_id:
        raise ProjectRequestError("datafetcher", "历史行情DataAssetRef所有者与当前Host主体不一致。")
    from runtime.contracts.input_adapter import bind_verified_calendar_to_history

    bound = bind_verified_calendar_to_history(asdict(data_ref), asdict(calendar_ref), data_store)
    payload = data_store.read_bytes(data_ref, tenant_id=data_ref.tenant_id)
    if not isinstance(payload, bytes):
        raise ProjectRequestError("datafetcher", "DataStore未返回历史行情原始字节。")
    lineage = dict(bound["lineage"])
    lineage["host_verified_calendar_ref"] = {
        "data_asset_id": calendar_ref.data_asset_id,
        "content_hash": calendar_ref.content_hash,
        "schema_id": calendar_ref.schema_id,
    }
    binding_metadata = {
        "tenant_id": data_ref.tenant_id,
        "content_hash": data_ref.content_hash,
        "media_type": str(bound["media_type"]),
        "schema_id": str(bound["schema_id"]),
        "asset_ids": tuple(str(item) for item in bound["asset_ids"]),
        "normalized_fields": tuple(str(item) for item in bound["normalized_fields"]),
        "coverage": dict(bound["coverage"]),
        "row_count": int(bound["row_count"]),
        "partition_spec": dict(bound["partition_spec"]),
        "price_convention": dict(bound["price_convention"]),
        "lineage": lineage,
        "created_by": data_ref.created_by,
        "access_scope": tuple(str(item) for item in bound["access_scope"]),
    }
    # The original ``raw-data-id + calendar-hash`` name made different
    # bindings collide whenever a later request reused the same calendar.
    # LocalDataStore signs coverage, conventions and lineage into storage_ref,
    # so every one of those fields must participate in the immutable asset
    # identity.  Identical bindings still reuse their existing artifact;
    # differing six-month windows or run provenance receive another safe ref.
    binding_identity = hashlib.sha256(json.dumps(
        {
            "source_storage_ref": data_ref.storage_ref,
            "calendar_storage_ref": calendar_ref.storage_ref,
            "metadata": binding_metadata,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()
    expected = {
        **binding_metadata,
        "data_asset_id": f"calendar-bound-{binding_identity}",
    }
    try:
        return data_store.put_bytes(
            tenant_id=expected["tenant_id"],
            data_asset_id=expected["data_asset_id"],
            payload=payload,
            media_type=expected["media_type"],
            schema_id=expected["schema_id"],
            asset_ids=expected["asset_ids"],
            normalized_fields=expected["normalized_fields"],
            coverage=expected["coverage"],
            row_count=expected["row_count"],
            partition_spec=expected["partition_spec"],
            price_convention=expected["price_convention"],
            lineage=expected["lineage"],
            created_by=expected["created_by"],
            access_scope=expected["access_scope"],
        )
    except FileExistsError:
        try:
            existing = data_store.get_ref(
                tenant_id=expected["tenant_id"], data_asset_id=expected["data_asset_id"],
            )
        except (AttributeError, FileNotFoundError, OSError, PermissionError, TypeError, ValueError) as error:
            raise ProjectRequestError("datafetcher", "已有交易日历绑定行情未通过完整性校验。") from error
        from runtime.contracts.contract_types import canonical_json

        if any(
            canonical_json(getattr(existing, key)) != canonical_json(value)
            for key, value in expected.items()
        ):
            raise ProjectRequestError("datafetcher", "已有交易日历绑定行情与本次请求不一致，不能复用。")
        return existing
    except (OSError, PermissionError, TypeError, ValueError) as error:
        raise ProjectRequestError("datafetcher", "Host无法提交经验证交易日历绑定的历史行情。") from error


def _latest_available_data_date(data_ref: Any, *, requested_date: date) -> str:
    """Resolve the last date actually present instead of the requested range end.

    DataFetcher keeps the requested interval in ``coverage.end_date`` while
    ``coverage.by_asset[*].end_date`` records the last observed row.  During
    the trading day those dates legitimately differ.  New contracts must be
    frozen to the observed data date, otherwise the formal Pricer later sees a
    contract start after its effective valuation session.
    """

    coverage = getattr(data_ref, "coverage", None)
    asset_ids = tuple(str(value) for value in getattr(data_ref, "asset_ids", ()))
    if not isinstance(coverage, Mapping) or not asset_ids:
        raise ProjectRequestError("datafetcher", "行情DataAssetRef缺少实际数据日期。")
    by_asset = coverage.get("by_asset")
    if not isinstance(by_asset, Mapping):
        raise ProjectRequestError("datafetcher", "行情DataAssetRef未声明逐标的实际覆盖日期。")
    actual_dates: list[date] = []
    for asset_id in asset_ids:
        item = by_asset.get(asset_id)
        raw_end = item.get("end_date") if isinstance(item, Mapping) else None
        try:
            actual = date.fromisoformat(str(raw_end))
        except (TypeError, ValueError) as error:
            raise ProjectRequestError(
                "datafetcher", f"行情DataAssetRef缺少{asset_id}的实际数据截止日。",
            ) from error
        if actual > requested_date:
            raise ProjectRequestError("datafetcher", "行情实际数据日期不得晚于请求日期。")
        actual_dates.append(actual)
    # Multi-underlying valuation can only start from a date covered by every
    # underlying.  Data quality owns finer row completeness validation.
    return min(actual_dates).isoformat()


def _fetch_local_calendar_asset(
    *,
    layout: LocalProjectLayout,
    authority: LocalHostAuthority,
    task_id: str,
    underlyings: Sequence[str],
    valuation_date: str,
    horizon_years: float,
    request_id: str,
    progress: Callable[[Mapping[str, Any]], None] | None,
    history_start_date: str | None = None,
) -> Any:
    """获取估值日前回看和合同期限内的完整中国交易日，不读取未来价格。"""

    from modules.datafetcher.config import DataFetcherConfig
    from modules.datafetcher.service import fetch_calendar_data

    requested = date.fromisoformat(valuation_date)
    start = requested - timedelta(days=14)
    if history_start_date is not None:
        try:
            start = min(start, date.fromisoformat(history_start_date))
        except ValueError as error:
            raise ProjectRequestError("datafetcher", "交易日历历史起始日必须为YYYY-MM-DD。") from error
    end = requested + timedelta(days=round(max(float(horizon_years), 1.0 / 365.0) * 365.0) + 14)
    _progress(progress, "calendar", "正在准备估值所需交易日历")
    result = fetch_calendar_data({
        "asset_ids": [str(item) for item in underlyings],
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }, authority.caller(request_id=request_id), config=DataFetcherConfig(
        data_root=layout.data_root,
        result_root=layout.result_root,
        cache_root=layout.data_root / "datafetcher-cache",
    ), task_id=task_id)
    if not result.ok or result.run.data_asset_ref is None:
        error = result.run.error or {}
        raise ProjectRequestError(
            "datafetcher",
            str(error.get("message") or "交易日历暂不可用，且没有完整覆盖本次区间的已验证缓存。"),
        )
    _progress(progress, "calendar", "交易日历已准备完成", "completed")
    return result.run.data_asset_ref


def run_module_request(
    request: Mapping[str, Any],
    *,
    project_root: str | Path | None = None,
    ifind_probe: Callable[[str], object] | None = None,
    progress: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run exactly one formal calculator without invoking Reporter or Designer.

    ``term_overrides`` are a new calculation instruction, never a mutation of
    a prior contract.  Core recompiles a fresh ResolvedContract and writes a
    separate immutable ModuleRun for the new parameter set.
    """

    body = dict(request)
    unknown = set(body).difference(_MODULE_REQUEST_FIELDS)
    if unknown:
        raise ProjectRequestError("request", "单模块请求含不支持字段；请只提交模块、产品、身份、条款、计算参数和数据区间。")
    module = str(body.get("module") or "").strip().lower()
    if module not in {"payoffer", "pricer", "backtester"}:
        raise ProjectRequestError("request", "单模块运行仅支持Payoffer、Pricer或Backtester；报告必须使用项目报告入口。")
    product_id = str(body.get("product_id") or "").strip()
    identity = _mapping_field(body, "identity")
    underlyings_value = identity.get("underlyings")
    if isinstance(underlyings_value, str):
        underlyings = tuple(item.strip() for item in underlyings_value.split(",") if item.strip())
    elif isinstance(underlyings_value, Sequence):
        underlyings = tuple(str(item).strip() for item in underlyings_value if str(item).strip())
    else:
        underlyings = ()
    if not product_id or not underlyings:
        raise ProjectRequestError("request", "单模块运行必须提供产品编号和标的代码。")

    paths = bootstrap_runtime()
    layout = LocalProjectLayout.create(skill_root=paths.project_root, project_root=project_root or Path.cwd())
    layout.initialize()
    authority = LocalHostAuthority(layout)
    task_hash = _request_hash(layout.project_root, body)
    task_id = f"module-{task_hash}"
    analysis_case_id = f"module-case-{task_hash}"
    term_overrides = _mapping_field(body, "term_overrides")
    horizon_years = _horizon_years(body.get("horizon"))
    if horizon_years is not None and "T" not in term_overrides:
        term_overrides["T"] = horizon_years

    data_ref = None
    calendar_ref = None
    data_store = None
    valuation_date = None
    market_as_of_date = None
    requested_pricing = _mapping_field(body, "pricing_config") if module == "pricer" else {}
    data_window = _mapping_field(body, "data_window")
    requires_market_history = module in {"pricer", "backtester"}
    requires_calendar = module == "backtester" or _request_requires_trading_calendar(
        product_id, identity=identity, term_overrides=term_overrides,
    )
    if requires_market_history or requires_calendar:
        token = _local_ifind_refresh_token(project_root)
        if not token:
            raise ProjectRequestError(
                "configuration", "含观察条款的单模块运行或行情估值需要iFind Refresh Token。",
                missing=("iFind Refresh Token",),
            )
        _activate_ifind_refresh_token(token)
        if ifind_probe is not None:
            try:
                ifind_probe(token)
            except Exception as error:
                raise ProjectRequestError("ifind", "iFind凭据验证失败，请检查Refresh Token或网络后重试。") from error
        valuation_date = str(
            requested_pricing.get("valuation_date")
            or data_window.get("end_date")
            or identity.get("contract_start_date")
            or date.today().isoformat()
        )
        history_start = _data_window_dates(data_window)[0] if requires_market_history else None
        if requires_calendar:
            effective_horizon = horizon_years or float(term_overrides.get("T", 1.0))
            calendar_ref = _fetch_local_calendar_asset(
                layout=layout,
                authority=authority,
                task_id=task_id,
                underlyings=underlyings,
                valuation_date=valuation_date,
                horizon_years=effective_horizon,
                request_id=f"calendar-{task_hash}",
                progress=progress,
                history_start_date=None if history_start is None else history_start.isoformat(),
            )
        data_store = importlib.import_module("runtime.adapters.local_store").LocalDataStore(layout.data_root)
        if requires_market_history:
            data_ref, market_as_of_date = _fetch_local_data_asset(
                layout=layout, authority=authority, task_id=task_id, underlyings=underlyings,
                data_window=data_window, request_id=f"data-{task_hash}", progress=progress,
            )
            if calendar_ref is not None:
                data_ref = _persist_calendar_bound_history_asset(
                    data_ref=data_ref, calendar_ref=calendar_ref, data_store=data_store,
                    principal_id=authority.principal_id,
                )
            elif module == "backtester":
                raise ProjectRequestError("datafetcher", "正式Backtester运行缺少经验证交易日历。")
            identity.setdefault("contract_start_date", market_as_of_date)
        elif calendar_ref is None:
            raise ProjectRequestError("datafetcher", "正式Backtester运行缺少经验证交易日历。")
        else:
            # Payoffer has no history asset to determine an observed close.
            # Its explicit requested contract start is still frozen with the
            # verified future schedule, never inferred from a price bar.
            identity.setdefault("contract_start_date", valuation_date)

    if module == "payoffer":
        friendly: dict[str, Any] = {
            "action": "run", "product_id": product_id, "identity": identity, "term_overrides": term_overrides,
        }
    elif module == "pricer":
        friendly = {
            "action": "run", "product_id": product_id, "identity": identity, "term_overrides": term_overrides,
            "pricing_config": {"valuation_date": valuation_date, "path_count": 10, **requested_pricing},
        }
    else:
        friendly = {
            "action": "run", "product_id": product_id, "identity": identity, "term_overrides": term_overrides,
            "backtest_config": {
                "start_date": str(data_ref.coverage.get("start") or data_ref.coverage.get("start_date")),
                "end_date": market_as_of_date, "entry_rule": "monthly", "complete_tenor": True,
                **_mapping_field(body, "backtest_config"),
            },
        }
    data_refs = tuple(
        asdict(reference)
        for reference in (data_ref, calendar_ref)
        if reference is not None
    )
    prepared = prepare_compute_request(module, friendly, data_refs=data_refs, data_store=data_store)
    catalog_version = _catalog_version(paths)
    context = authority.context(
        module, task_id=task_id, analysis_case_id=analysis_case_id,
        candidate_id=f"direct-{task_hash}", catalog_version=catalog_version,
        contract_fingerprint=str(prepared["contract_fingerprint"]),
    )
    module_label = {"payoff": "收益结构", "pricing": "估值", "backtest": "历史回测"}.get(module, "计算")
    _progress(progress, module, f"正在完成{module_label}计算")
    result = call_local_tool(
        module, dict(prepared["request"]), authority=authority,
        request_id=f"{module}-{task_hash}", host_context=context,
        result_store=LocalResultStore(layout.result_root),
        # Core consumes the calendar bytes while compiling an observation
        # contract.  Payoffer receives only that frozen contract, never a
        # DataStore capability it cannot lawfully use.
        data_store=data_store if module in {"pricer", "backtester"} else None,
    )
    if result.get("ok") is not True:
        raise ProjectRequestError(module, str(result.get("message") or f"{module}未能完成。"))
    _progress(progress, module, f"{module_label}计算已完成", "completed")
    return {
        "ok": True,
        "status": str(result.get("status") or "completed"),
        "module": module,
        "contract_fingerprint": prepared["contract_fingerprint"],
        "result": dict(result),
    }


def _quote_variant_plan(
    body: Mapping[str, Any],
    *,
    candidates: Sequence[Mapping[str, Any]],
    paths: Any,
    task_hash: str,
) -> list[dict[str, Any]]:
    """Turn one conversational quote decision into independently frozen contracts."""

    base_terms = _mapping_field(body, "term_overrides")
    base_pricing = _mapping_field(body, "pricing_config")
    supplied = body.get("quote_variants")
    if supplied is None:
        rows: Sequence[Mapping[str, Any]] = tuple(candidates)
    elif isinstance(supplied, list) and supplied:
        rows = tuple(item for item in supplied if isinstance(item, Mapping))
        if len(rows) != len(supplied):
            raise ProjectRequestError("request", "Quote参数版本必须是对象数组。")
    else:
        raise ProjectRequestError("request", "组合Quote至少需要一个参数版本。")

    if not candidates:
        raise ProjectRequestError("recommender", "当前需求没有可用于报价的推荐结构。")
    default = dict(candidates[0])
    variants: list[dict[str, Any]] = []
    for index, raw in enumerate(rows, start=1):
        selection = {
            key: raw[key]
            for key in _AGENT_SELECTION_FIELDS
            if key in raw
        }
        if selection:
            baseline = {
                key: default.get(key)
                for key in _AGENT_SELECTION_FIELDS
                if key in default
            }
            candidate = _agent_native_candidate(
                {**baseline, **selection}, paths=paths, task_hash=f"{task_hash}-quote-{index:02d}",
            )
        else:
            candidate = dict(raw)
            if not candidate.get("product_id") or not candidate.get("underlyings"):
                candidate = dict(default)
        candidate["candidate_id"] = f"quote-{task_hash}-{index:02d}"

        raw_terms = raw.get("term_overrides", {})
        raw_pricing = raw.get("pricing_config", {})
        if not isinstance(raw_terms, Mapping) or not isinstance(raw_pricing, Mapping):
            raise ProjectRequestError("request", "Quote参数版本中的term_overrides和pricing_config必须是对象。")
        label = str(raw.get("label") or _horizon_label(raw_terms.get("T")) or f"参数版本{index}").strip()
        variants.append({
            "candidate": candidate,
            "term_overrides": {**base_terms, **dict(raw_terms)},
            "pricing_config": {**base_pricing, **dict(raw_pricing)},
            "label": label or f"参数版本{index}",
        })
    return variants


def _quote_variant_error(error: Exception) -> str:
    """Return a concise public reason without exposing a traceback or local paths."""

    if isinstance(error, ProjectRequestError):
        message = str(error).replace("\n", " ").strip()
        return message[:240] if message else "定价未形成可验证运行结果。"
    from runtime.errors import error_info

    return error_info(error, stage="pricer").message


def _run_quote_delivery(
    *,
    body: Mapping[str, Any],
    prompt: str,
    case: Any,
    candidates: Sequence[Mapping[str, Any]],
    paths: Any,
    layout: LocalProjectLayout,
    authority: LocalHostAuthority,
    result_store: LocalResultStore,
    catalog_version: str,
    progress: Callable[[Mapping[str, Any]], None] | None,
) -> dict[str, Any]:
    """Create one Quote from all recommended structures and parameter variants.

    Quote rows are generated from verified Pricer runs.  They are not a manual
    picker over old results, although those saved runs remain reusable later.
    """

    task_hash = case.task_id.removeprefix("project-")
    task_id = case.task_id
    analysis_case_id = case.analysis_case_id
    variants = _quote_variant_plan(body, candidates=candidates, paths=paths, task_hash=task_hash)
    default_horizon = _horizon_years(case.confirmed_constraints.get("horizon"))
    data_store = importlib.import_module("runtime.adapters.local_store").LocalDataStore(layout.data_root)
    history_cache: dict[tuple[str, ...], tuple[DataAssetRef, str]] = {}
    calendar_cache: dict[tuple[tuple[str, ...], str, float], DataAssetRef] = {}
    bound_history_cache: dict[tuple[str, str], DataAssetRef] = {}
    report_candidates: list[dict[str, Any]] = []
    quote_items: list[dict[str, Any]] = []
    completed_runs: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    history_start, _ = _data_window_dates({})

    for index, variant in enumerate(variants, start=1):
        candidate = dict(variant["candidate"])
        product_id = str(candidate["product_id"])
        underlyings = tuple(str(item) for item in candidate["underlyings"])
        terms = dict(variant["term_overrides"])
        if default_horizon is not None and "T" not in terms:
            terms["T"] = default_horizon
        pricing_config = {
            "valuation_date": str(variant["pricing_config"].get("valuation_date") or date.today().isoformat()),
            "path_count": 10,
            **dict(variant["pricing_config"]),
        }
        valuation_date = str(pricing_config["valuation_date"])
        label = str(variant["label"])
        try:
            calendar_ref: DataAssetRef | None = None
            requires_calendar = _request_requires_trading_calendar(
                product_id, identity={"underlyings": list(underlyings)}, term_overrides=terms,
            )
            if requires_calendar:
                try:
                    horizon = float(terms.get("T", default_horizon or 1.0))
                except (TypeError, ValueError) as error:
                    raise ProjectRequestError("request", "Quote期限T必须为有效数字。") from error
                calendar_key = (underlyings, valuation_date, horizon)
                calendar_ref = calendar_cache.get(calendar_key)
                if calendar_ref is None:
                    calendar_ref = _fetch_local_calendar_asset(
                        layout=layout, authority=authority, task_id=task_id, underlyings=underlyings,
                        valuation_date=valuation_date, horizon_years=horizon,
                        request_id=f"calendar-{task_hash}-{index:02d}", progress=progress,
                        history_start_date=history_start.isoformat(),
                    )
                    calendar_cache[calendar_key] = calendar_ref

            history = history_cache.get(underlyings)
            if history is None:
                history = _fetch_local_data_asset(
                    layout=layout, authority=authority, task_id=task_id, underlyings=underlyings,
                    data_window={}, request_id=f"data-{task_hash}-{index:02d}", progress=progress,
                )
                history_cache[underlyings] = history
            data_ref, market_as_of_date = history
            if calendar_ref is not None:
                bound_key = (data_ref.storage_ref, calendar_ref.storage_ref)
                bound_ref = bound_history_cache.get(bound_key)
                if bound_ref is None:
                    bound_ref = _persist_calendar_bound_history_asset(
                        data_ref=data_ref, calendar_ref=calendar_ref, data_store=data_store,
                        principal_id=authority.principal_id,
                    )
                    bound_history_cache[bound_key] = bound_ref
                data_ref = bound_ref

            prepared = prepare_compute_request(
                "pricer",
                {
                    "product_id": product_id,
                    "identity": {"underlyings": list(underlyings), "contract_start_date": market_as_of_date},
                    "term_overrides": terms,
                    "pricing_config": pricing_config,
                },
                data_refs=tuple(
                    asdict(reference)
                    for reference in (data_ref, calendar_ref)
                    if reference is not None
                ),
                data_store=data_store,
            )
            contract = ResolvedContract(**dict(prepared["resolved_contract"]))
            _progress(progress, "quote", f"正在完成{label}的参考报价")
            context = authority.context(
                "pricer", task_id=task_id, analysis_case_id=analysis_case_id,
                candidate_id=str(candidate["candidate_id"]), catalog_version=catalog_version,
                contract_fingerprint=contract.contract_fingerprint,
            )
            pricing = call_local_tool(
                "pricer", dict(prepared["request"]), authority=authority,
                request_id=f"pricer-{task_hash}-{index:02d}", host_context=context,
                result_store=result_store, data_store=data_store,
            )
            raw_ref = pricing.get("module_run_ref")
            if pricing.get("ok") is not True or not isinstance(raw_ref, Mapping):
                raise ProjectRequestError("pricer", str(pricing.get("message") or "定价未形成可验证运行结果。"))
            reference = ModuleRunRef(**dict(raw_ref))
            result_store.verify_module_run(reference, tenant_id=authority.tenant_id)
            identity = dict(contract.identity)
            report_candidate = {
                **candidate,
                "product_name": str(identity["name_zh"]),
                "product_version": contract.product_version,
                "product_version_content_hash": contract.product_snapshot_hash,
                "catalog_content_hash": contract.registry_snapshot_hash,
                "contract_fingerprint": contract.contract_fingerprint,
                "analysis_basis_id": f"basis-{contract.contract_fingerprint[:20]}",
                "underlyings": list(contract.underlyings),
                "currency": contract.currency,
                "price_convention": {"spot": "close", "contract_basis": identity["price_convention"]},
                "module_run_refs": {"pricing": dict(raw_ref)},
                "module_run_options": {"pricing": [dict(raw_ref)]},
            }
            report_candidates.append(report_candidate)
            quote_items.append({
                "candidate_id": report_candidate["candidate_id"],
                "module": "pricing",
                "module_run_ref": dict(raw_ref),
            })
            completed_runs[report_candidate["candidate_id"]] = {"pricing": dict(raw_ref)}
            _progress(progress, "quote", f"{label}的参考报价已完成", "completed")
        except Exception as error:
            failures[label] = _quote_variant_error(error)
            _progress(progress, "quote", f"{label}未完成参考报价：{failures[label]}", "failed")

    if failures:
        details = "；".join(f"{label}：{message}" for label, message in failures.items())
        raise ProjectRequestError("pricer", f"以下报价参数未能完成；未生成不完整参考报价：{details}")
    source_id = f"source-{task_hash}"
    source = {
        "source_id": source_id,
        "tenant_id": authority.tenant_id,
        "task_id": task_id,
        "analysis_case_id": analysis_case_id,
        "catalog_version": catalog_version,
        "catalog_content_hash": report_candidates[0]["catalog_content_hash"],
        "candidates": report_candidates,
    }
    selection = {
        "source_id": source_id,
        "quote_items": [
            {"source_id": source_id, **item}
            for item in quote_items
        ],
        "output_type": "quote",
        "format": "html",
        "audience": "professional",
        "report_run_id": f"quote-{task_hash}",
        "metadata": {
            "title": str(body.get("title") or "组合参考报价"),
            "as_of_date": str(date.today().isoformat()),
        },
    }
    from modules.reporter.artifact_validator import validate_report_run_directory
    from modules.reporter.service import build_selected_request, call_tool as reporter_call_tool

    selection_port = _SelectionPort(source)
    expected_request = build_selected_request(
        selection, tenant_id=authority.tenant_id, selection_port=selection_port,
    )
    _progress(progress, "delivery", "正在生成组合参考报价")
    report = dict(reporter_call_tool(
        {"action": "run", "selection": selection},
        result_store=result_store, designer_port=_DesignerPort(), selection_port=selection_port,
        tenant_id=authority.tenant_id, output_root=layout.result_root,
    ))
    if report.get("ok") is not True or report.get("status") not in {"completed", "partial"}:
        raise ProjectRequestError("reporter", str(report.get("message") or "正式交付服务未能生成组合参考报价。"))
    report_name = str((report.get("output") or {}).get("report") or "")
    report_path = (layout.result_root / task_id / selection["report_run_id"] / report_name).resolve()
    if not report_name or not report_path.is_file() or layout.result_root not in report_path.parents:
        raise ProjectRequestError("reporter", "组合参考报价未返回可打开的HTML交付文件。")
    try:
        validate_report_run_directory(report_path.parent, expected_request=expected_request)
    except Exception as error:
        raise ProjectRequestError("reporter", "组合参考报价未通过完整性校验，请检查运行日志。") from error
    _progress(progress, "delivery", "组合参考报价已生成，正在打开", "completed")
    primary = report_candidates[0]
    return {
        "ok": True,
        "status": "completed",
        "message": "组合参考报价已完成。",
        "request": {"prompt": prompt, "output_type": "quote", "format": "html"},
        "constraints": dict(case.confirmed_constraints),
        "recommendation": {
            "candidate_id": primary["candidate_id"],
            "product_id": primary["product_id"],
            "product_name": primary["product_name"],
            "reason": primary.get("reason"),
            "main_risks": primary.get("main_risks", []),
            "quote_variants": [
                {"product_id": item["product_id"], "product_name": item["product_name"], "contract_fingerprint": item["contract_fingerprint"]}
                for item in report_candidates
            ],
        },
        "module_runs": completed_runs,
        "module_failures": failures,
        "report": {"output_type": "quote", "format": "html", "coverage_status": "complete", "path": str(report_path)},
    }


def _run_project_candidate(
    *,
    body: Mapping[str, Any],
    candidate: Mapping[str, Any],
    candidate_index: int,
    case: Any,
    layout: LocalProjectLayout,
    authority: LocalHostAuthority,
    result_store: LocalResultStore,
    catalog_version: str,
    caches: dict[str, dict[Any, Any]],
    progress: Callable[[Mapping[str, Any]], None] | None,
) -> dict[str, Any]:
    """Freeze and run one candidate for either single or comparison delivery."""

    task_hash = case.task_id.removeprefix("project-")
    request_suffix = f"{task_hash}-{candidate_index:02d}"
    task_id = case.task_id
    analysis_case_id = case.analysis_case_id
    candidate_id = str(candidate["candidate_id"])
    candidate_label = str(candidate.get("product_name") or candidate.get("product_id") or candidate_id)
    product_id = str(candidate["product_id"])
    underlyings = tuple(str(item) for item in candidate.get("underlyings", ()))
    requested_pricing = _mapping_field(body, "pricing_config")
    term_overrides = _mapping_field(body, "term_overrides")
    horizon_years = _horizon_years(case.confirmed_constraints.get("horizon"))
    if horizon_years is not None and "T" not in term_overrides:
        term_overrides["T"] = horizon_years
    effective_horizon = float(term_overrides.get("T", horizon_years or 1.0))
    valuation_date = str(requested_pricing.get("valuation_date") or date.today().isoformat())
    history_start, _ = _data_window_dates({})

    calendar_key = (underlyings, valuation_date, effective_horizon, history_start.isoformat())
    calendar_ref = caches["calendars"].get(calendar_key)
    if calendar_ref is None:
        calendar_ref = _fetch_local_calendar_asset(
            layout=layout,
            authority=authority,
            task_id=task_id,
            underlyings=underlyings,
            valuation_date=valuation_date,
            horizon_years=effective_horizon,
            request_id=f"calendar-{request_suffix}",
            progress=progress,
            history_start_date=history_start.isoformat(),
        )
        caches["calendars"][calendar_key] = calendar_ref

    history = caches["history"].get(underlyings)
    if history is None:
        history = _fetch_local_data_asset(
            layout=layout,
            authority=authority,
            task_id=task_id,
            underlyings=underlyings,
            data_window={},
            request_id=f"data-{request_suffix}",
            progress=progress,
        )
        caches["history"][underlyings] = history
    data_ref, market_as_of_date = history
    data_store = importlib.import_module("runtime.adapters.local_store").LocalDataStore(layout.data_root)
    binding_key = (data_ref.storage_ref, calendar_ref.storage_ref)
    bound_ref = caches["bound_history"].get(binding_key)
    if bound_ref is None:
        bound_ref = _persist_calendar_bound_history_asset(
            data_ref=data_ref,
            calendar_ref=calendar_ref,
            data_store=data_store,
            principal_id=authority.principal_id,
        )
        caches["bound_history"][binding_key] = bound_ref
    data_ref = bound_ref

    pricing_config = {"valuation_date": valuation_date, "path_count": 10, **requested_pricing}
    pricing_request = prepare_compute_request(
        "pricer",
        {
            "product_id": product_id,
            "identity": {"underlyings": list(underlyings), "contract_start_date": market_as_of_date},
            "term_overrides": term_overrides,
            "pricing_config": pricing_config,
        },
        data_refs=(asdict(data_ref), asdict(calendar_ref)),
        data_store=data_store,
    )
    contract_payload = dict(pricing_request["resolved_contract"])
    contract = ResolvedContract(**contract_payload)
    _progress(progress, "data", f"{candidate_label}的市场数据已准备完成", "completed")

    payoff_context = authority.context(
        "payoffer",
        task_id=task_id,
        analysis_case_id=analysis_case_id,
        candidate_id=candidate_id,
        catalog_version=catalog_version,
        contract_fingerprint=contract.contract_fingerprint,
    )
    _progress(progress, "calculation", f"正在完成{candidate_label}的收益结构计算")
    payoff = call_local_tool(
        "payoffer",
        {"action": "run", "payoff_input": {"contract": contract_payload}},
        authority=authority,
        request_id=f"payoff-{request_suffix}",
        host_context=payoff_context,
        result_store=result_store,
    )
    if payoff.get("ok") is not True or str(payoff.get("status", "")).lower() not in {"succeeded", "completed", "partial"}:
        raise ProjectRequestError("payoffer", str(payoff.get("message") or f"{candidate_label}的收益结构模块未能完成。"))
    payoff_raw_ref = payoff.get("module_run_ref")
    if not isinstance(payoff_raw_ref, Mapping):
        raise ProjectRequestError("payoffer", f"{candidate_label}的收益结构模块缺少可验证运行引用。")
    payoff_ref = ModuleRunRef(**dict(payoff_raw_ref))
    result_store.verify_module_run(payoff_ref, tenant_id=authority.tenant_id)
    _progress(progress, "calculation", f"{candidate_label}的收益结构计算已完成", "completed")

    completed_refs: dict[str, dict[str, Any]] = {"payoff": dict(payoff_raw_ref)}
    module_failures: dict[str, str] = {}
    backtest_request = prepare_compute_request(
        "backtester",
        {
            "action": "run",
            "product_id": product_id,
            "identity": {"underlyings": list(underlyings), "contract_start_date": market_as_of_date},
            "term_overrides": term_overrides,
            "backtest_config": {
                "start_date": str(data_ref.coverage.get("start") or data_ref.coverage.get("start_date")),
                "end_date": market_as_of_date,
                "entry_rule": "monthly",
                "complete_tenor": True,
                **_mapping_field(body, "backtest_config"),
            },
        },
        data_refs=(asdict(data_ref), asdict(calendar_ref)),
        resolved_contract=contract_payload,
        data_store=data_store,
    )
    for module, public_name, module_request in (
        ("pricer", "pricing", dict(pricing_request["request"])),
        ("backtester", "backtest", dict(backtest_request["request"])),
    ):
        module_label = {"pricer": "估值", "backtester": "历史回测"}[module]
        _progress(progress, "calculation", f"正在完成{candidate_label}的{module_label}计算")
        module_context = authority.context(
            module,
            task_id=task_id,
            analysis_case_id=analysis_case_id,
            candidate_id=candidate_id,
            catalog_version=catalog_version,
            contract_fingerprint=contract.contract_fingerprint,
            result_refs=(payoff_ref,),
        )
        try:
            module_result = call_local_tool(
                module,
                module_request,
                authority=authority,
                request_id=f"{module}-{request_suffix}",
                host_context=module_context,
                result_store=result_store,
                data_store=data_store,
            )
            module_raw_ref = module_result.get("module_run_ref")
            if module_result.get("ok") is not True or not isinstance(module_raw_ref, Mapping):
                raise ProjectRequestError(module, str(module_result.get("message") or f"{module}未形成可验证运行引用。"))
            verified_ref = ModuleRunRef(**dict(module_raw_ref))
            result_store.verify_module_run(verified_ref, tenant_id=authority.tenant_id)
            completed_refs[public_name] = dict(module_raw_ref)
            _progress(progress, "calculation", f"{candidate_label}的{module_label}计算已完成", "completed")
        except Exception:
            module_failures[public_name] = "正式模块未完成，报告已标注覆盖缺口。"
            _progress(progress, "calculation", f"{candidate_label}的{module_label}未完成，报告将标注覆盖缺口", "partial")

    identity = dict(contract.identity)
    report_candidate = {
        **dict(candidate),
        "candidate_id": candidate_id,
        "product_name": str(identity["name_zh"]),
        "product_version": contract.product_version,
        "product_version_content_hash": contract.product_snapshot_hash,
        "contract_fingerprint": contract.contract_fingerprint,
        "analysis_basis_id": f"basis-{contract.contract_fingerprint[:20]}",
        "underlyings": list(contract.underlyings),
        "currency": contract.currency,
        "price_convention": {"spot": "close", "contract_basis": identity["price_convention"]},
        "module_run_refs": completed_refs,
        "module_run_options": {name: [reference] for name, reference in completed_refs.items()},
    }
    return {
        "candidate": report_candidate,
        "module_refs": completed_refs,
        "module_failures": module_failures,
        "catalog_content_hash": contract.registry_snapshot_hash,
        "valuation_date": valuation_date,
        "frozen_horizon": _horizon_label(contract.terms.get("T")),
    }


def run_project_request(
    request: str | Mapping[str, Any],
    *,
    project_root: str | Path | None = None,
    agent_port: Any | None = None,
    ifind_probe: Callable[[str], object] | None = None,
    progress: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run one natural-language recommendation and a formal public delivery.

    This is deliberately the *only* standalone path that invokes Reporter and
    Designer.  Recommendation-only and direct-module requests use their own
    entry points so a model never has to invent a substitute HTML file.
    """

    body = {"prompt": request} if isinstance(request, str) else dict(request)
    unknown = set(body).difference(_REPORT_REQUEST_FIELDS)
    if unknown:
        raise ProjectRequestError("request", "项目报告请求只接受需求文本、已确认条件、合同条款、计算参数和交付选项。")
    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        raise ProjectRequestError("request", "请提供需要研究的期权结构需求。")
    explicit_output = str(body.get("output_type") or "").strip().lower()
    request_lower = prompt.casefold()
    requested_card = any(token in request_lower for token in ("简报", "简单报告", "卡片", "card"))
    requested_report = any(token in request_lower for token in ("详细报告", "深度报告", "完整报告", "report"))
    requested_quote = any(token in request_lower for token in ("参考报价", "quote", "报价表"))
    output_type = explicit_output or ("quote" if requested_quote else "card" if requested_card and not requested_report else "report")
    if output_type not in {"card", "quote", "report"}:
        raise ProjectRequestError("request", "交付类型只能选择研究简报、参考报价或详细报告。")
    delivery_mode = _project_delivery_mode(body, prompt, output_type=output_type)
    output_format = str(body.get("format") or "html").strip().lower()
    if output_format != "html":
        raise ProjectRequestError("request", "项目级自然语言入口当前仅交付HTML格式。")
    has_explicit_selection = any(key in body and body.get(key) is not None for key in ("selection", "selections"))
    if has_explicit_selection:
        config = _configuration(require_ifind=True, project_root=project_root)
    elif agent_port is None:
        config = _configuration(require_ifind=False, project_root=project_root)
        if config["mode"] == "host":
            _progress(progress, "workflow", "正在分析需求并整理候选结构")
            response = JsonHttpEndpoint(config["host_url"]).post("project/run", body)
            if response.get("ok") is not True:
                raise ProjectRequestError("host", str(response.get("message") or "研究服务未能完成研究请求。"))
            return response
        raise ProjectRequestError(
            "request",
            "当前对话尚未完成候选确认，暂不能生成正式交付；请在已集成OptionHelper的对话入口中重试。",
        )
    else:
        ifind = _local_ifind_refresh_token(project_root)
        if not ifind:
            raise ProjectRequestError(
                "configuration",
                "iFind尚未在项目memory.md中配置；请先确认保存Refresh Token后重试。",
                missing=("iFind Refresh Token",),
            )
        config = {"mode": "direct", "ifind": ifind}
    if config["mode"] == "host":
        _progress(progress, "workflow", "正在分析需求并整理候选结构")
        response = JsonHttpEndpoint(config["host_url"]).post("project/run", body)
        if response.get("ok") is not True:
            raise ProjectRequestError("host", str(response.get("message") or "研究服务未能完成研究请求。"))
        return response
    _activate_ifind_refresh_token(config.get("ifind", ""))

    paths = bootstrap_runtime()
    layout = LocalProjectLayout.create(skill_root=paths.project_root, project_root=project_root or Path.cwd())
    layout.initialize()
    authority = LocalHostAuthority(layout)
    result_store = LocalResultStore(layout.result_root)
    _progress(progress, "preflight", "运行条件已就绪", "completed")

    if ifind_probe is not None:
        _progress(progress, "data", "正在检查数据服务配置")
        try:
            ifind_probe(config["ifind"])
        except Exception as error:
            raise ProjectRequestError("ifind", "iFind凭据验证失败，请检查Refresh Token或网络后重试。") from error
        _progress(progress, "data", "数据服务配置已就绪", "completed")
    else:
        # DataFetcher的正式iFind Provider在真实取数前完成唯一一次
        # Refresh Token→Access Token交换；Host不重复换取或持久化Access Token。
        _progress(progress, "data", "数据服务配置已就绪", "completed")

    catalog_version = _catalog_version(paths)
    case = _recommendation_case(
        prompt=prompt, workflow="professional_report", requested_outputs=(output_type,), body=body,
        paths=paths, layout=layout, authority=authority,
    )
    task_hash = case.task_id.removeprefix("project-")
    task_id = case.task_id
    analysis_case_id = case.analysis_case_id
    explicit_candidates = _explicit_project_candidates(body, paths=paths, task_hash=task_hash)
    if explicit_candidates:
        if delivery_mode == "single" and output_type != "quote" and len(explicit_candidates) != 1:
            raise ProjectRequestError("request", "单结构交付只能提交一个候选；多个候选请使用横向对比。")
        _progress(progress, "recommendation", "正在核对当前对话中的结构选择")
        candidate = explicit_candidates[0]
        comparison_candidates = explicit_candidates
        quote_candidates = explicit_candidates
        _progress(progress, "recommendation", "结构选择已核对完成", "completed")
    else:
        if agent_port is None:
            raise ProjectRequestError(
                "request",
                "当前对话尚未完成候选确认，暂不能生成正式交付；请在已集成OptionHelper的对话入口中重试。",
            )
        from modules.recommender.service import RecommenderService

        _progress(progress, "recommendation", "正在分析需求并整理候选结构")
        recommendation_result = RecommenderService(
            agent_port=agent_port,
            knowledge_port=_LocalKnowledgePort(paths, catalog_version),
        ).recommend_fixed(case, workflow="professional_report")
        recommendation = recommendation_result.get("recommendation_set")
        if not isinstance(recommendation, Mapping):
            raise ProjectRequestError("recommender", "未能形成可验证的候选结构。")
        if recommendation.get("status") == "pending_question":
            raise ProjectRequestError("recommender", str(recommendation.get("next_question") or "研究条件仍不完整。"))
        candidate = _primary_ready_candidate(recommendation)
        ready_candidates = _ordered_ready_candidates(recommendation.get("candidates")) or [candidate]
        comparison_candidates = ready_candidates
        quote_candidates = ready_candidates
        _progress(progress, "recommendation", "候选结构已整理完成", "completed")

    if output_type == "quote":
        return _run_quote_delivery(
            body=body, prompt=prompt, case=case, candidates=quote_candidates,
            paths=paths, layout=layout, authority=authority, result_store=result_store,
            catalog_version=catalog_version, progress=progress,
        )

    if delivery_mode == "comparison" and len(comparison_candidates) < 2:
        raise ProjectRequestError("recommender", "横向对比至少需要两个可执行候选，请补充另一个结构或调整推荐条件。")

    source_id = f"source-{task_hash}"
    report_run_id = f"{'multi' if delivery_mode == 'comparison' else 'report'}-{task_hash}"
    selected_candidates = comparison_candidates if delivery_mode == "comparison" else [candidate]
    caches: dict[str, dict[Any, Any]] = {"history": {}, "calendars": {}, "bound_history": {}}
    executions = [
        _run_project_candidate(
            body=body,
            candidate=item,
            candidate_index=index,
            case=case,
            layout=layout,
            authority=authority,
            result_store=result_store,
            catalog_version=catalog_version,
            caches=caches,
            progress=progress,
        )
        for index, item in enumerate(selected_candidates, start=1)
    ]
    report_candidates = [dict(item["candidate"]) for item in executions]
    all_module_refs = {
        item["candidate"]["candidate_id"]: dict(item["module_refs"])
        for item in executions
    }
    module_failures = {
        f"{item['candidate']['candidate_id']}:{module}": message
        for item in executions
        for module, message in item["module_failures"].items()
    }
    catalog_hashes = {str(item["catalog_content_hash"]) for item in executions}
    if len(catalog_hashes) != 1:
        raise ProjectRequestError("reporter", "对比候选不是由同一产品目录快照生成，不能混合交付。")
    report_candidate = report_candidates[0]
    completed_refs = all_module_refs[report_candidate["candidate_id"]]
    frozen_horizon = str(executions[0]["frozen_horizon"] or "")
    valuation_date = str(executions[0]["valuation_date"])
    source = {
        "source_id": source_id,
        "tenant_id": authority.tenant_id,
        "task_id": task_id,
        "analysis_case_id": analysis_case_id,
        "catalog_version": catalog_version,
        "catalog_content_hash": catalog_hashes.pop(),
        "candidates": report_candidates,
    }
    selected_modules = [
        name for name in ("payoff", "pricing", "backtest")
        if any(name in refs for refs in all_module_refs.values())
    ]
    selected_module_refs = {
        candidate_id: {name: refs.get(name) for name in selected_modules}
        for candidate_id, refs in all_module_refs.items()
    }
    default_title = (
        "多结构研究简报" if delivery_mode == "comparison" and output_type == "card"
        else "多结构完整报告" if delivery_mode == "comparison"
        else f"{report_candidate['product_name']}结构{'简报' if output_type == 'card' else '研究'}"
    )
    selection = {
        "source_id": source_id,
        "candidate_ids": [item["candidate_id"] for item in report_candidates],
        "selected_modules": selected_modules,
        "module_run_refs": selected_module_refs,
        "delivery_mode": delivery_mode,
        "output_type": output_type,
        "format": "html",
        "audience": "professional",
        "report_run_id": report_run_id,
        "metadata": {
            "title": str(body.get("title") or default_title),
            "as_of_date": valuation_date,
        }
    }
    from modules.reporter.artifact_validator import validate_report_run_directory
    from modules.reporter.service import build_selected_request, call_tool as reporter_call_tool

    delivery_name = {"card": "研究简报", "quote": "参考报价", "report": "完整研究报告"}[output_type]
    if delivery_mode == "comparison":
        delivery_name = "多结构研究简报" if output_type == "card" else "多结构完整报告"
    _progress(progress, "delivery", f"正在生成{delivery_name}")
    selection_port = _SelectionPort(source)
    expected_report_request = build_selected_request(
        selection, tenant_id=authority.tenant_id, selection_port=selection_port,
    )
    report = dict(reporter_call_tool(
        {"action": "run", "selection": selection},
        result_store=result_store,
        designer_port=_DesignerPort(),
        selection_port=selection_port,
        tenant_id=authority.tenant_id,
        output_root=layout.result_root,
    ))
    # Reporter may deliberately return ``partial`` after producing a verified
    # HTML artifact whose evidence section identifies unavailable modules.  That
    # is a valid delivery state; the project result below keeps it partial rather
    # than throwing away the artifact or claiming complete coverage.
    if report.get("ok") is not True or report.get("status") not in {"completed", "partial"}:
        raise ProjectRequestError("reporter", str(report.get("message") or "正式交付服务未能生成报告。"))
    report_name = str((report.get("output") or {}).get("report") or "")
    report_path = (layout.result_root / task_id / report_run_id / report_name).resolve()
    if not report_name or not report_path.is_file() or layout.result_root not in report_path.parents:
        raise ProjectRequestError("reporter", "报告完成状态未返回可打开的HTML交付文件。")
    try:
        report_validation = validate_report_run_directory(
            report_path.parent, expected_request=expected_report_request,
        )
    except Exception as error:
        raise ProjectRequestError("reporter", "HTML交付未通过完整性校验，请检查运行日志。") from error
    frozen_statuses = report_validation.get("frozen_evidence_statuses")
    evidence_verified = isinstance(frozen_statuses, list) and bool(frozen_statuses) and all(
        status == "verified" for status in frozen_statuses
    )
    _progress(progress, "delivery", "HTML交付已生成，正在打开", "completed")
    full_coverage = (
        not module_failures
        and all(set(refs) == {"payoff", "pricing", "backtest"} for refs in all_module_refs.values())
        and evidence_verified
    )
    return {
        "ok": True,
        "status": "completed" if full_coverage else "partial",
        "message": f"期权结构推荐和{delivery_name}已完成。" if full_coverage else f"期权结构推荐和{delivery_name}已完成，但部分计算模块未完成，交付物已明确标注缺口。",
        "request": {
            "prompt": prompt,
            "output_type": output_type,
            **({"delivery_mode": "comparison"} if delivery_mode == "comparison" else {}),
            "format": "html",
        },
        "constraints": {
            **dict(case.confirmed_constraints),
            **({"horizon": frozen_horizon} if frozen_horizon else {}),
        },
        "assumptions": [
            f"本次合同期限为{frozen_horizon or case.confirmed_constraints.get('horizon', '3个月')}，已按冻结合同重新计算",
            "正式收益结构按当前已确认条款计算",
            "最大损失约束按期权费完全损失进行压力边界说明",
        ],
        "recommendation": {
            "candidate_id": report_candidate["candidate_id"],
            "product_id": report_candidate["product_id"],
            "product_name": report_candidate["product_name"],
            "reason": report_candidate.get("reason"),
            "main_risks": report_candidate.get("main_risks", []),
        },
        "module_runs": all_module_refs if delivery_mode == "comparison" else completed_refs,
        "module_failures": module_failures,
        "report": {
            "output_type": output_type,
            "format": "html",
            "coverage_status": "complete" if full_coverage else "partial",
            "path": str(report_path),
        },
    }


def public_project_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Project CLI projection for a human-facing Agent response.

    Internal immutable references remain available to in-process Hosts through
    ``run_project_request`` but are not printed by the one-line public command.
    """

    recommendation = result.get("recommendation")
    report = result.get("report")
    return {
        "ok": bool(result.get("ok")),
        "status": str(result.get("status") or "failed"),
        "message": str(result.get("message") or ""),
        "user_summary": {
            "recommendation": {
                key: value
                for key, value in dict(recommendation).items()
                if key in {"product_id", "product_name", "reason", "main_risks"}
            } if isinstance(recommendation, Mapping) else {},
            "report": {
                key: value
                for key, value in dict(report).items()
                if key in {"format", "coverage_status", "path"}
            } if isinstance(report, Mapping) else {},
            "assumptions": list(result.get("assumptions", [])) if isinstance(result.get("assumptions"), list) else [],
            "module_failures": {
                str(name): "正式模块未完成，报告已标注覆盖缺口。"
                for name in result.get("module_failures", {})
            } if isinstance(result.get("module_failures"), Mapping) else {},
        },
    }


def public_recommendation_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Human-facing projection for recommendation-only requests."""

    recommendation = result.get("recommendation")
    return {
        "ok": bool(result.get("ok")),
        "status": str(result.get("status") or "failed"),
        "message": str(result.get("message") or ""),
        "constraints": dict(result.get("constraints") or {}),
        "recommendation": dict(recommendation) if isinstance(recommendation, Mapping) else {"candidates": []},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="OptionHelper结构化Tool入口")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="输出Tool目录")
    group.add_argument("--recommend-request", help="仅执行结构推荐，不取数、不计算、不生成报告")
    group.add_argument("--recommend-json", help="执行单行JSON结构推荐请求")
    group.add_argument("--module-json", help="执行单行JSON单模块计算请求")
    group.add_argument("--project-request", help="批处理模式：调用独立模型完成推荐、计算和HTML正式交付")
    args = parser.parse_args()
    if args.list:
        print(json.dumps(tool_catalog(), ensure_ascii=False, indent=2))
        return
    try:
        with _cli_project_store_scope():
            def emit(event: Mapping[str, Any]) -> None:
                print(json.dumps(dict(event), ensure_ascii=False), file=sys.stderr, flush=True)

            if args.recommend_request is not None:
                result = run_recommendation_request(str(args.recommend_request), progress=emit)
                output = public_recommendation_result(result)
            elif args.recommend_json is not None:
                value = json.loads(args.recommend_json)
                if not isinstance(value, Mapping):
                    raise ProjectRequestError("request", "结构推荐请求格式无效。")
                result = run_recommendation_request(value, progress=emit)
                output = public_recommendation_result(result)
            elif args.module_json is not None:
                value = json.loads(args.module_json)
                if not isinstance(value, Mapping):
                    raise ProjectRequestError("request", "单模块请求格式无效。")
                output = run_module_request(value, progress=emit)
            else:
                result = run_project_request(str(args.project_request), progress=emit)
                output = public_project_result(result)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except (ProjectRequestError, LocalHostError, ToolDispatchError, json.JSONDecodeError) as error:
        stage = error.stage if isinstance(error, ProjectRequestError) else "host"
        missing = list(error.missing) if isinstance(error, ProjectRequestError) else []
        print(json.dumps({
            "ok": False,
            "status": "needs_configuration" if stage == "configuration" else "failed",
            "stage": stage,
            "message": str(error),
            "missing": missing,
        }, ensure_ascii=False, indent=2))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
