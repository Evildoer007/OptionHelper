#!/usr/bin/env python3
"""OptionHelper结构化Tool及项目级Skill Host入口。

``call_tool``保持App授权边界；``--project-request``供无对话模型的批处理
模式使用。已有对话模型的Host通过``--project-json``提交已验证选择，再由
local-development Host完成正式计算、Reporter和Designer闭环。
"""

from __future__ import annotations

import argparse
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


ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from runtime.bootstrap import bootstrap_runtime
from runtime.adapters.local_host import (
    JsonHttpEndpoint,
    LocalHostAuthority,
    LocalHostError,
    LocalProjectLayout,
)
from runtime.adapters.local_store import LocalResultStore
from runtime.contracts.contract_api import ResolvedContract, resolve_contract
from runtime.contracts.input_adapter import (
    is_explicit_demo_pricing_config,
    prepare_compute_request as _prepare_compute_request,
)
from runtime.protocol.models import CallerContext, DataAssetRef, ModuleRunRef
from runtime.protocol.module_host import ModuleHostContext
from runtime.protocol.tool_catalog import MODULES, tool_catalog
from runtime.protocol.version import PUBLIC_VERSION, require_public_version


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


_RECOMMENDATION_REQUEST_FIELDS = frozenset({"prompt", "constraints"})
_REPORT_REQUEST_FIELDS = frozenset({
    "prompt", "constraints", "term_overrides", "pricing_config", "backtest_config",
    "output_type", "format", "html_report_layout", "title", "selection",
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
    if action in {"catalog", "status"}:
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
            raise ToolDispatchError("外置DataStorePort仅允许Host注入Pricer或Backtester正式运行")
        if not callable(getattr(data_store, "read_bytes", None)):
            raise ToolDispatchError("正式Pricer或Backtester运行必须由Host注入DataStorePort")
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
    if data_store is not None:
        kwargs["data_store"] = data_store
    kwargs["tenant_id"] = caller_context.tenant_id
    result = handler(dict(request), **kwargs)
    if not isinstance(result, Mapping):
        raise ToolDispatchError(f"模块{module}返回值必须为JSON对象")
    return dict(result)


class _OpenAICompatibleAgentPort:
    """Provider-neutral structured AgentPort; the API key never enters payloads."""

    def __init__(self, endpoint: JsonHttpEndpoint, model: str, api_key: str) -> None:
        self._endpoint = endpoint
        self._model = model
        self._api_key = api_key

    def capability(self) -> Any:
        from modules.recommender.models import ModelCapability

        return ModelCapability(
            model_id=self._model,
            structured_output=True,
            tool_calling=False,
            multi_agent=False,
            max_parallel_agents=1,
        )

    def run_step(self, role: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        response = self._endpoint.post("chat/completions", {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是OptionHelper固定推荐流程中的受限步骤。严格遵守role_rule，"
                        "只输出required_output要求的JSON对象，不输出Markdown，不调用工具，不编造金融数值。"
                    ),
                },
                {"role": "user", "content": json.dumps({"role": role, **dict(payload)}, ensure_ascii=False)},
            ],
        }, bearer_token=self._api_key)
        try:
            content = response["choices"][0]["message"]["content"]
            value = json.loads(content) if isinstance(content, str) else content
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise ProjectRequestError("recommender", "模型没有返回可验证的结构化推荐结果。") from error
        if not isinstance(value, Mapping):
            raise ProjectRequestError("recommender", "模型没有返回可验证的结构化推荐结果。")
        return dict(value)


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
            require_public_version(declared, "catalog_version")
        except ValueError as error:
            raise ProjectRequestError("knowledger", "资料目录版本与当前正式协议不一致。") from error
    return PUBLIC_VERSION


def _configuration(*, require_model: bool = True, require_ifind: bool = True) -> dict[str, str]:
    host_url = os.environ.get("OPTIONHELPER_HOST_URL", "").strip()
    if host_url:
        return {"mode": "host", "host_url": host_url}
    values = {
        "base_url": os.environ.get("OPTIONHELPER_MODEL_BASE_URL", "").strip(),
        "model": os.environ.get("OPTIONHELPER_MODEL", "").strip(),
        "api_key": os.environ.get("OPTIONHELPER_MODEL_API_KEY", "").strip(),
        "ifind": os.environ.get("IFIND_REFRESH_TOKEN", "").strip(),
    }
    required: list[tuple[str, str]] = []
    if require_model:
        required.extend((
            ("base_url", "模型服务地址"), ("model", "模型名称"),
            ("api_key", "模型API Key"),
        ))
    if require_ifind:
        required.append(("ifind", "iFind Refresh Token"))
    missing = [label for key, label in required if not values[key]]
    if missing:
        raise ProjectRequestError(
            "configuration",
            "OptionHelper尚未完成运行配置，请在Host的环境或Secret Store中补充：" + "、".join(missing) + "。",
            missing=missing,
        )
    public_values: dict[str, str] = {"mode": "direct"}
    if require_model:
        public_values.update({key: values[key] for key in ("base_url", "model", "api_key")})
    if require_ifind:
        public_values["ifind"] = values["ifind"]
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

    A conversational Host such as Claude, DeepSeek or Codex is already the
    model.  It must never be asked to configure a second model gateway merely
    to pass its researched choice into the formal calculation/report pipeline.
    The Host may choose a product and explain it, but Core still validates the
    product against OptionReg and creates all identities, hashes and run IDs.
    """

    unknown = set(selection).difference(_AGENT_SELECTION_FIELDS)
    if unknown:
        raise ProjectRequestError("request", "正式报告选择只接受产品、标的和面向读者的研究理由，不接受内部引用或运行字段。")
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
        config = _configuration(require_model=True, require_ifind=False)
        if config["mode"] == "host":
            _progress(progress, "host", "正在由受控Host执行结构推荐")
            response = JsonHttpEndpoint(config["host_url"]).post("v1/project/recommend", body)
            if response.get("ok") is not True:
                raise ProjectRequestError("host", str(response.get("message") or "受控Host未能完成结构推荐。"))
            return response
    paths = bootstrap_runtime()
    layout = LocalProjectLayout.create(skill_root=paths.project_root, project_root=project_root or Path.cwd())
    layout.initialize()
    authority = LocalHostAuthority(layout)
    if agent_port is None:
        agent_port = _OpenAICompatibleAgentPort(
            JsonHttpEndpoint(config["base_url"]), config["model"], config["api_key"],
        )
    from modules.recommender.service import RecommenderService

    case = _recommendation_case(
        prompt=prompt, workflow="recommendation", requested_outputs=(), body=body,
        paths=paths, layout=layout, authority=authority,
    )
    _progress(progress, "recommender", "正在整理市场观点并审阅候选结构")
    result = RecommenderService(
        agent_port=agent_port, knowledge_port=_LocalKnowledgePort(paths, case.catalog_version),
    ).recommend_fixed(case, workflow="recommendation")
    recommendation = result.get("recommendation_set")
    if not isinstance(recommendation, Mapping):
        raise ProjectRequestError("recommender", "Recommender没有返回可验证的推荐结果。")
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
    _progress(progress, "recommender", "结构筛选完成，未调用计算或报告模块", "completed")
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
    _progress(progress, "datafetcher", "正在通过iFind获取并校验标的历史行情")
    result = fetch_data({
        "asset_ids": [str(item) for item in underlyings],
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "fields": ["open", "high", "low", "close", "adj_open", "adj_high", "adj_low", "adj_close", "volume"],
        "provider": "ifind_http",
        "source_priority": ["ifind_http"],
        "cache_policy": "force_refresh",
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
    _progress(progress, "datafetcher", "iFind行情已冻结为本次计算数据", "completed")
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
    try:
        return data_store.put_bytes(
            tenant_id=data_ref.tenant_id,
            data_asset_id=f"{data_ref.data_asset_id}-calendar-{calendar_ref.content_hash[:16]}",
            payload=payload,
            media_type=str(bound["media_type"]),
            schema_id=str(bound["schema_id"]),
            asset_ids=tuple(str(item) for item in bound["asset_ids"]),
            normalized_fields=tuple(str(item) for item in bound["normalized_fields"]),
            coverage=dict(bound["coverage"]),
            row_count=int(bound["row_count"]),
            partition_spec=dict(bound["partition_spec"]),
            price_convention=dict(bound["price_convention"]),
            lineage=lineage,
            created_by=data_ref.created_by,
            access_scope=tuple(str(item) for item in bound["access_scope"]),
        )
    except (FileExistsError, OSError, PermissionError, TypeError, ValueError) as error:
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
    _progress(progress, "datafetcher", "正在通过iFind获取估值日至合同到期日的中国交易日历")
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
    _progress(progress, "datafetcher", "中国交易日历已冻结，未来价格仍由Pricer模拟", "completed")
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
        token = os.environ.get("IFIND_REFRESH_TOKEN", "").strip()
        if not token:
            raise ProjectRequestError(
                "configuration", "含观察条款的单模块运行或行情估值需要iFind Refresh Token。",
                missing=("iFind Refresh Token",),
            )
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
            "pricing_config": {"valuation_date": valuation_date, "path_count": 2_000, **requested_pricing},
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
    _progress(progress, module, f"正在按本次确认参数运行{module}")
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
    _progress(progress, module, f"{module}已按本次确认参数完成", "completed")
    return {
        "ok": True,
        "status": str(result.get("status") or "completed"),
        "module": module,
        "contract_fingerprint": prepared["contract_fingerprint"],
        "result": dict(result),
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
    requested_card = any(token in request_lower for token in ("简报", "简单报告", "card"))
    requested_report = any(token in request_lower for token in ("详细报告", "深度报告", "完整报告"))
    output_type = explicit_output or ("card" if requested_card and not requested_report else "report")
    if output_type not in {"card", "report"}:
        raise ProjectRequestError("request", "交付类型只能选择简单报告或详细报告。")
    output_format = str(body.get("format") or "html").strip().lower()
    if output_format != "html":
        raise ProjectRequestError("request", "项目级自然语言入口当前仅交付HTML格式。")
    layout_name = None if output_type == "card" else str(body.get("html_report_layout") or "continuous").strip().lower()
    if layout_name is not None and layout_name != "continuous":
        raise ProjectRequestError("request", "HTML详细报告固定为连续正文，不提供目录版。")

    model_selection = _mapping_field(body, "selection") if "selection" in body else {}
    config = (
        _configuration(require_model=False, require_ifind=True)
        if model_selection
        else _configuration()
        if agent_port is None
        else {"mode": "direct", "ifind": os.environ.get("IFIND_REFRESH_TOKEN", "test-refresh-token")}
    )
    if config["mode"] == "host":
        _progress(progress, "host", "正在由受控Host执行推荐与报告流程")
        response = JsonHttpEndpoint(config["host_url"]).post("v1/project/run", body)
        if response.get("ok") is not True:
            raise ProjectRequestError("host", str(response.get("message") or "受控Host未能完成研究请求。"))
        return response

    paths = bootstrap_runtime()
    layout = LocalProjectLayout.create(skill_root=paths.project_root, project_root=project_root or Path.cwd())
    layout.initialize()
    authority = LocalHostAuthority(layout)
    result_store = LocalResultStore(layout.result_root)
    _progress(progress, "preflight", "已建立项目级受控Store和本机身份", "completed")

    if ifind_probe is not None:
        _progress(progress, "ifind", "正在验证iFind Refresh Token")
        try:
            ifind_probe(config["ifind"])
        except Exception as error:
            raise ProjectRequestError("ifind", "iFind凭据验证失败，请检查Refresh Token或网络后重试。") from error
        _progress(progress, "ifind", "iFind凭据验证通过", "completed")
    else:
        # DataFetcher的正式iFind Provider在真实取数前完成唯一一次
        # Refresh Token→Access Token交换；Host不重复换取或持久化Access Token。
        _progress(progress, "ifind", "iFind Refresh Token已配置，将在正式取数时自动鉴权", "completed")

    catalog_version = _catalog_version(paths)
    case = _recommendation_case(
        prompt=prompt, workflow="professional_report", requested_outputs=(output_type,), body=body,
        paths=paths, layout=layout, authority=authority,
    )
    task_hash = case.task_id.removeprefix("project-")
    task_id = case.task_id
    analysis_case_id = case.analysis_case_id
    if model_selection:
        _progress(progress, "recommender", "正在验证当前对话已选择的产品结构")
        candidate = _agent_native_candidate(model_selection, paths=paths, task_hash=task_hash)
        _progress(progress, "recommender", "已验证当前对话选择，未调用第二个模型服务", "completed")
    else:
        if agent_port is None:
            agent_port = _OpenAICompatibleAgentPort(
                JsonHttpEndpoint(config["base_url"]), config["model"], config["api_key"]
            )
        from modules.recommender.service import RecommenderService

        _progress(progress, "recommender", "正在检索产品资料并审阅候选结构")
        recommendation_result = RecommenderService(
            agent_port=agent_port,
            knowledge_port=_LocalKnowledgePort(paths, catalog_version),
        ).recommend_fixed(case, workflow="professional_report")
        recommendation = recommendation_result.get("recommendation_set")
        if not isinstance(recommendation, Mapping):
            raise ProjectRequestError("recommender", "Recommender没有返回可验证的推荐结果。")
        if recommendation.get("status") == "pending_question":
            raise ProjectRequestError("recommender", str(recommendation.get("next_question") or "研究条件仍不完整。"))
        candidate = _primary_ready_candidate(recommendation)
        _progress(progress, "recommender", "已完成候选结构审阅", "completed")

    product_id = str(candidate["product_id"])
    underlyings = tuple(str(item) for item in candidate.get("underlyings", ()))
    requested_pricing = _mapping_field(body, "pricing_config")
    term_overrides = _mapping_field(body, "term_overrides")
    horizon_years = _horizon_years(case.confirmed_constraints.get("horizon"))
    if horizon_years is not None and "T" not in term_overrides:
        term_overrides["T"] = horizon_years
    effective_horizon = float(term_overrides.get("T", horizon_years or 1.0))
    valuation_date = str(requested_pricing.get("valuation_date") or date.today().isoformat())
    data_window: dict[str, Any] = {}
    history_start, _ = _data_window_dates(data_window)
    # The project flow always invokes Backtester.  It therefore obtains one
    # verified calendar before history, then gives the same ref to both the
    # DataFetcher quality gate and the contract compiler.
    calendar_ref = _fetch_local_calendar_asset(
        layout=layout,
        authority=authority,
        task_id=task_id,
        underlyings=underlyings,
        valuation_date=valuation_date,
        horizon_years=effective_horizon,
        request_id=f"calendar-{task_hash}",
        progress=progress,
        history_start_date=history_start.isoformat(),
    )
    data_ref, market_as_of_date = _fetch_local_data_asset(
        layout=layout, authority=authority, task_id=task_id, underlyings=underlyings,
        data_window=data_window, request_id=f"data-{task_hash}", progress=progress,
    )
    data_store = importlib.import_module("runtime.adapters.local_store").LocalDataStore(layout.data_root)
    data_ref = _persist_calendar_bound_history_asset(
        data_ref=data_ref, calendar_ref=calendar_ref, data_store=data_store,
        principal_id=authority.principal_id,
    )
    pricing_config = {
        "valuation_date": valuation_date,
        "path_count": 2_000,
        **requested_pricing,
    }
    pricing_request = prepare_compute_request(
        "pricer",
        {
            "product_id": product_id,
            "identity": {"underlyings": list(underlyings), "contract_start_date": market_as_of_date},
            "term_overrides": term_overrides,
            "pricing_config": pricing_config,
        },
        data_refs=(
            (asdict(data_ref), asdict(calendar_ref))
            if calendar_ref is not None else
            (asdict(data_ref),)
        ),
        data_store=data_store,
    )
    contract_payload = dict(pricing_request["resolved_contract"])
    contract = ResolvedContract(**contract_payload)
    frozen_horizon = _horizon_label(contract.terms.get("T"))
    _progress(progress, "datafetcher", "iFind行情已冻结为DataAssetRef", "completed")

    context = authority.context(
        "payoffer", task_id=task_id, analysis_case_id=analysis_case_id,
        candidate_id=str(candidate["candidate_id"]), catalog_version=catalog_version,
        contract_fingerprint=contract.contract_fingerprint,
    )
    _progress(progress, "payoffer", "正在运行正式收益结构模块")
    payoff = call_local_tool(
        "payoffer", {"action": "run", "payoff_input": {"contract": contract_payload}},
        authority=authority, request_id=f"payoff-{task_hash}",
        host_context=context, result_store=result_store,
    )
    if payoff.get("ok") is not True or str(payoff.get("status", "")).lower() not in {"succeeded", "completed", "partial"}:
        raise ProjectRequestError("payoffer", str(payoff.get("message") or "正式收益结构模块未能完成。"))
    raw_ref = payoff.get("module_run_ref")
    if not isinstance(raw_ref, Mapping):
        raise ProjectRequestError("payoffer", "正式收益结构模块缺少可验证运行引用。")
    module_ref = ModuleRunRef(**dict(raw_ref))
    result_store.verify_module_run(module_ref, tenant_id=authority.tenant_id)
    _progress(progress, "payoffer", "正式收益结构结果已验证", "completed")

    backtest_request = prepare_compute_request(
        "backtester",
        {
            "action": "run",
            "product_id": product_id,
            "identity": {
                "underlyings": list(underlyings),
                "contract_start_date": market_as_of_date,
            },
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
    completed_refs: dict[str, dict[str, Any]] = {"payoff": dict(raw_ref)}
    module_failures: dict[str, str] = {}
    for module, display_name, module_request in (
        ("pricer", "pricing", dict(pricing_request["request"])),
            ("backtester", "backtest", dict(backtest_request["request"])),
    ):
        _progress(progress, module, f"正在运行正式{module}模块")
        module_context = authority.context(
            module, task_id=task_id, analysis_case_id=analysis_case_id,
            candidate_id=str(candidate["candidate_id"]), catalog_version=catalog_version,
            contract_fingerprint=contract.contract_fingerprint,
            result_refs=(module_ref,),
        )
        try:
            module_result = call_local_tool(
                module, module_request,
                authority=authority,
                request_id=f"{module}-{task_hash}",
                host_context=module_context,
                result_store=result_store,
                data_store=data_store,
            )
            module_raw_ref = module_result.get("module_run_ref")
            if module_result.get("ok") is not True or not isinstance(module_raw_ref, Mapping):
                raise ProjectRequestError(module, str(module_result.get("message") or f"{module}未形成可验证运行引用。"))
            verified_ref = ModuleRunRef(**dict(module_raw_ref))
            result_store.verify_module_run(verified_ref, tenant_id=authority.tenant_id)
            completed_refs[display_name] = dict(module_raw_ref)
            _progress(progress, module, f"正式{module}结果已验证", "completed")
        except Exception:
            module_failures[display_name] = "正式模块未完成，报告已标注覆盖缺口。"
            _progress(progress, module, f"正式{module}暂未完成，报告将明确标注缺口", "partial")

    identity = dict(contract.identity)
    source_id = f"source-{task_hash}"
    report_run_id = f"report-{task_hash}"
    report_candidate = {
        **candidate,
        "candidate_id": str(candidate["candidate_id"]),
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
    source = {
        "source_id": source_id,
        "tenant_id": authority.tenant_id,
        "task_id": task_id,
        "analysis_case_id": analysis_case_id,
        "catalog_version": catalog_version,
        # A report source is bound to the registry snapshot that compiled the
        # exact ResolvedContract, never to a mutable catalog label alone.
        "catalog_content_hash": contract.registry_snapshot_hash,
        "candidates": [report_candidate],
    }
    selection = {
        "source_id": source_id,
        "candidate_ids": [report_candidate["candidate_id"]],
        "selected_modules": [name for name in ("payoff", "pricing", "backtest") if name in completed_refs],
        "module_run_refs": {report_candidate["candidate_id"]: completed_refs},
        "delivery_mode": "single",
        "output_type": output_type,
        "format": "html",
        "audience": "professional",
        "report_run_id": report_run_id,
        "metadata": {
            "title": str(body.get("title") or f"{identity['name_zh']}结构{'简报' if output_type == 'card' else '研究'}"),
            "as_of_date": valuation_date,
        },
    }
    from modules.reporter.artifact_validator import validate_report_run_directory
    from modules.reporter.service import build_selected_request, call_tool as reporter_call_tool

    delivery_name = "HTML简报" if output_type == "card" else "HTML详细报告"
    _progress(progress, "reporter", f"Reporter正在冻结事实并交由Designer生成{delivery_name}")
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
        raise ProjectRequestError("reporter", str(report.get("message") or "Reporter或Designer未能生成正式报告。"))
    report_name = str((report.get("output") or {}).get("report") or "")
    report_path = (layout.result_root / task_id / report_run_id / report_name).resolve()
    if not report_name or not report_path.is_file() or layout.result_root not in report_path.parents:
        raise ProjectRequestError("reporter", "报告完成状态缺少受控交付文件。")
    try:
        report_validation = validate_report_run_directory(
            report_path.parent, expected_request=expected_report_request,
        )
    except Exception as error:
        raise ProjectRequestError("reporter", "Reporter或Designer交付物未通过完整性校验，请检查运行日志。") from error
    report_unit = report_validation["unit"]
    report_modules = report_unit.get("modules") if isinstance(report_unit, Mapping) else None
    verified_report_modules = {
        name
        for name in ("payoff", "pricing", "backtest")
        if isinstance(report_modules, Mapping)
        and isinstance(report_modules.get(name), Mapping)
        and report_modules[name].get("status") == "ready"
    }
    _progress(progress, "reporter", f"Reporter和Designer已完成正式{delivery_name}", "completed")
    full_coverage = (
        not module_failures
        and set(completed_refs) == {"payoff", "pricing", "backtest"}
        and verified_report_modules == {"payoff", "pricing", "backtest"}
        and report_unit.get("evidence_status") == "verified"
    )
    return {
        "ok": True,
        "status": "completed" if full_coverage else "partial",
        "message": f"期权结构推荐和{delivery_name}已完成。" if full_coverage else f"期权结构推荐和{delivery_name}已完成，但部分计算模块未完成，交付物已明确标注缺口。",
        "request": {"prompt": prompt, "output_type": output_type, "format": "html", "html_report_layout": layout_name},
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
            "product_id": product_id,
            "product_name": report_candidate["product_name"],
            "reason": report_candidate.get("reason"),
            "main_risks": report_candidate.get("main_risks", []),
        },
        "module_runs": completed_refs,
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
    group.add_argument("--project-json", help="对话Agent模式：提交已选择产品并执行正式报告链路")
    args = parser.parse_args()
    if args.list:
        print(json.dumps(tool_catalog(), ensure_ascii=False, indent=2))
        return
    try:
        def emit(event: Mapping[str, Any]) -> None:
            print(json.dumps(dict(event), ensure_ascii=False), file=sys.stderr, flush=True)

        if args.recommend_request is not None:
            result = run_recommendation_request(str(args.recommend_request), progress=emit)
            output = public_recommendation_result(result)
        elif args.recommend_json is not None:
            value = json.loads(args.recommend_json)
            if not isinstance(value, Mapping):
                raise ProjectRequestError("request", "结构推荐JSON必须是对象。")
            result = run_recommendation_request(value, progress=emit)
            output = public_recommendation_result(result)
        elif args.module_json is not None:
            value = json.loads(args.module_json)
            if not isinstance(value, Mapping):
                raise ProjectRequestError("request", "单模块JSON必须是对象。")
            output = run_module_request(value, progress=emit)
        else:
            request: str | Mapping[str, Any]
            if args.project_json is not None:
                value = json.loads(args.project_json)
                if not isinstance(value, Mapping):
                    raise ProjectRequestError("request", "项目报告JSON必须是对象。")
                request = value
            else:
                request = str(args.project_request)
            result = run_project_request(request, progress=emit)
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
