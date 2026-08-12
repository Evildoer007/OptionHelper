"""评测专用内存端口与真实Reporter交付夹具。"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping


class ControlledAgentPort:
    def capability(self):
        from modules.recommender.models import ModelCapability
        return ModelCapability.from_mapping({
            "model_id": "eval-model", "structured_output": True, "tool_calling": True,
            "multi_agent": False, "max_parallel_agents": 1,
        })

    def run_step(self, role: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        responses = {
            "SingleAgent.Intent": {
                "confirmed_constraints": {"underlyings": ["000300.SH"], "view": "看涨"},
                "missing_information": [], "next_question": None,
                "research_queries": ["看涨且最大损失有限"],
            },
            "SingleAgent.Research": {"proposals": [{
                "product_id": "2.1", "product_name": "看涨期权", "underlyings": ["000300.SH"],
                "reason": "看涨观点与有限损失约束相符。",
                "suitable_for": ["预期标的上涨且可接受权利金损失。"],
                "not_suitable_for": ["要求本金保障或固定收益。"],
                "main_risks": ["到期不涨时可能损失全部权利金。"],
                "library_status": "ready", "evidence_ref_ids": ["eval-optionlib-21"],
                "missing_inputs": ["执行价", "期限"],
            }]},
            "SingleAgent.Critic": {"reviews": [{
                "product_id": "2.1", "hard_reject": False, "rejection_reason": None,
                "additional_not_suitable_for": ["无法承受权利金归零。"],
                "additional_risks": ["时间价值衰减。"], "rank_adjustment": 0,
            }]},
        }
        if role not in responses:
            raise ValueError(f"未配置评测角色：{role}")
        return responses[role]


class ControlledKnowledgePort:
    def __init__(self, scenario: str = "valid") -> None:
        self.scenario = scenario

    def search(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        catalog_version = payload["catalog_version"]
        rows = [
            (
                "eval-optionlist-21", "optionlist", "2.1看涨期权",
                "2.1产品身份", {"product_id": "2.1", "name_zh": "看涨期权"}, None,
            ),
            (
                "eval-optionlib-21", "optionlib", "适用于看涨并愿意承担权利金损失的情形。",
                "2.1.4适用场景", {}, None,
            ),
            (
                "eval-optionreg-21", "optionreg_status", "2.1当前登记为可执行产品。",
                "OptionReg.entry_status", {}, True,
            ),
        ]
        if self.scenario == "missing_optionlib":
            rows = [row for row in rows if row[1] != "optionlib"]
        if self.scenario == "entry_blocked":
            rows = [
                (*row[:-1], False) if row[1] == "optionreg_status" else row
                for row in rows
            ]
        returned_catalog = "other-catalog" if self.scenario == "catalog_mismatch" else catalog_version
        return {
            "ok": True,
            "catalog_version": returned_catalog,
            "evidence": [
                {
                    "evidence_id": evidence_id,
                    "product_id": "2.1",
                    "catalog_version": catalog_version,
                    "source": source,
                    "section": section,
                    "library_status": "ready",
                    "excerpt_hash": sha256(excerpt.encode("utf-8")).hexdigest(),
                    "excerpt": excerpt,
                    **({"identity": identity} if identity else {}),
                    **({"entry_status": entry_status} if entry_status is not None else {}),
                }
                for evidence_id, source, excerpt, section, identity, entry_status in rows
            ],
        }


def controlled_recommendation(scenario: str = "valid") -> Mapping[str, Any]:
    from modules.recommender.config import RecommenderConfig
    from modules.recommender.service import RecommenderService
    service = RecommenderService(
        agent_port=ControlledAgentPort(), knowledge_port=ControlledKnowledgePort(scenario),
        config=RecommenderConfig(agent_mode="single"),
    )
    return service.recommend({
        "analysis_case_id": "eval-case", "task_id": "eval-task", "run_id": "eval-recommend",
        "tenant_id": "eval-tenant", "prompt": "沪深300看涨且最大损失有限，请推荐结构",
        "catalog_version": "eval-catalog", "requested_outputs": [],
        "confirmed_constraints": {"underlyings": ["000300.SH"]},
    })


def recommender_evidence_matrix() -> Mapping[str, Any]:
    """Require aligned OptionList, OptionLib and executable OptionReg evidence."""
    outcomes: dict[str, dict[str, Any]] = {}
    for scenario in ("missing_optionlib", "catalog_mismatch", "entry_blocked"):
        try:
            result = dict(controlled_recommendation(scenario))
            recommendation = result.get("recommendation_set") if isinstance(result.get("recommendation_set"), Mapping) else result
            outcomes[scenario] = {
                "status": recommendation.get("status"),
                "candidate_count": len(recommendation.get("candidates", [])) if isinstance(recommendation.get("candidates"), list) else 0,
            }
        except Exception as error:
            outcomes[scenario] = {"status": "rejected", "error_type": type(error).__name__, "candidate_count": 0}
    return {
        "all_rejected": all(item["status"] not in {"pending_approval", "completed", "succeeded"} for item in outcomes.values()),
        "no_candidates": all(item["candidate_count"] == 0 for item in outcomes.values()),
        "outcomes": outcomes,
    }


def pricer_regression(scenario: str) -> Mapping[str, Any]:
    """Run the frozen MC precision and ObservedState boundaries offline."""
    import csv

    from runtime.contracts.contract_api import resolve_contract
    from runtime.protocol.models import DataAssetRef
    from modules.pricer import HistoricalData, PricingConfig, PricingInput, price
    from modules.pricer.models import TradingCalendarData

    source = Path(__file__).resolve().parent / "fixtures" / "market_history.csv"
    with source.open(encoding="utf-8", newline="") as handle:
        rows = tuple(dict(row) for row in csv.DictReader(handle))
    sessions = tuple(dict.fromkeys(str(row["date"]) for row in rows))
    coverage = {
        "start_date": sessions[0],
        "end_date": sessions[-1],
        "sessions": sessions,
        "calendar_id": "CN-SSE",
        "calendar_version": "eval-market-history-v1",
        "by_asset": {"000905.SH": {"start": sessions[0], "end": sessions[-1]}},
    }
    historical = HistoricalData(
        "memory://eval-market-history",
        rows,
        asset_ids=("000905.SH",),
        coverage=coverage,
    )
    data_ref = DataAssetRef(
        data_asset_id="eval-market-history",
        storage_ref=historical.source_ref,
        media_type="text/csv",
        schema_id="market-history-v1",
        asset_ids=("000905.SH",),
        normalized_fields=("date", "asset_id", "close", "adj_close"),
        coverage=coverage,
        row_count=len(rows),
        price_convention={},
        content_hash=historical.content_hash,
        lineage={"fixture": "explicit-cn-sessions"},
    )
    calendar_sessions = tuple(value for value in sessions if value >= "2024-01-31")
    calendar_hash = sha256(b"eval-pricer-trading-calendar-v1").hexdigest()
    calendar_data = TradingCalendarData(
        source_ref="memory://eval-pricer-trading-calendar",
        asset_ids=("000905.SH",),
        sessions=calendar_sessions,
        sessions_by_exchange={"SSE": calendar_sessions},
        asset_exchange={"000905.SH": "SSE"},
        requested_start_date=calendar_sessions[0],
        requested_end_date=calendar_sessions[-1],
        content_hash=calendar_hash,
    )
    calendar_ref = DataAssetRef(
        data_asset_id="eval-pricer-trading-calendar",
        storage_ref=calendar_data.source_ref,
        media_type="application/json",
        schema_id="trading-calendar",
        asset_ids=("000905.SH",),
        normalized_fields=("session",),
        coverage={
            "start_date": calendar_sessions[0],
            "end_date": calendar_sessions[-1],
            "sessions": calendar_sessions,
            "calendar_id": "CN-SSE",
            "calendar_version": "eval-pricer-calendar-v1",
        },
        row_count=len(calendar_sessions),
        price_convention={"contains_market_prices": False},
        content_hash=calendar_hash,
        lineage={"fixture": "explicit-trading-calendar"},
    )
    identity: dict[str, Any] = {
        "underlyings": ("000905.SH",),
        "contract_reference_spots": {"000905.SH": 5000.0},
    }
    if scenario == "observed_state_required":
        identity["contract_start_date"] = "2024-01-30"
    contract = resolve_contract(
        "5.1",
        identity=identity,
        term_overrides={"T": 0.01, "H_KO": 110.0, "Pi_0": 1.0},
        trading_dates=sessions,
    )
    result = price(PricingInput(
        contract=contract,
        pricing_config=PricingConfig(
            valuation_date="2024-01-31",
            spot=5390.0,
            volatility_override=0.2,
            model_method="monte_carlo",
            path_count=11,
            risk_free_rate=0.01,
        ),
        historical_data=historical,
        market_data_refs=(data_ref,),
        trading_calendar_data=calendar_data,
        trading_calendar_ref=calendar_ref,
    ))
    return {
        "scenario": scenario,
        "status": result.status,
        "precision_status": result.precision_status,
        "quote_eligible": result.quote_eligible,
        "path_count": result.path_count,
        "messages": list(result.messages),
        "has_precision_gate": "quote_precision_gate" in result.diagnostics,
        "calendar": {
            "schema_id": calendar_ref.schema_id,
            "content_hash_matches": calendar_data.content_hash == calendar_ref.content_hash,
            "uses_independent_sessions": tuple(calendar_ref.coverage["sessions"]) == calendar_data.sessions,
        },
    }


def backtester_regression(scenario: str) -> Mapping[str, Any]:
    """Probe path non-triggering, exchange-date entry and multi-asset coverage."""
    import pandas as pd

    from runtime.contracts.contract_api import resolve_contract
    from modules.backtester import BacktestConfig, BacktestInput, HistoricalData, backtest

    dates = pd.date_range("2024-01-02", periods=12, freq="B")
    frame = pd.DataFrame({
        "date": dates,
        "asset_id": "000905.SH",
        "close": [100.0] * len(dates),
        "adj_close": [100.0 + index * 0.01 for index in range(len(dates))],
    })
    if scenario == "missing_multi_underlying":
        contract = resolve_contract("10.1", identity={"underlyings": ["000905.SH", "000300.SH"]})
        config = BacktestConfig(entry_rule="explicit", entry_dates=("2024-01-02",))
    elif scenario == "non_trading_entry":
        contract = resolve_contract(
            "2.1", identity={"underlyings": ["000905.SH"]}, term_overrides={"T": 2 / 365, "Pi_0": 1.0},
        )
        config = BacktestConfig(entry_rule="explicit", entry_dates=("2024-01-06",))
    else:
        contract = resolve_contract(
            "5.1", identity={"underlyings": ["000905.SH"]},
            term_overrides={"T": 3 / 365, "H_KO": 120.0, "Pi_0": 1.0},
        )
        config = BacktestConfig(entry_rule="explicit", entry_dates=("2024-01-02",))
    try:
        result = backtest(BacktestInput(contract, config, HistoricalData.from_frame(frame)))
    except Exception as error:
        return {"scenario": scenario, "exception_type": type(error).__name__, "message": str(error)}
    triggered_count = sum(trade.events.get("tau_out") is not None for trade in result.trades)
    return {
        "scenario": scenario,
        "trade_count": len(result.trades),
        "triggered_count": triggered_count,
        "untriggered_count": len(result.trades) - triggered_count,
    }


def app_bridge_contract() -> Mapping[str, Any]:
    """Compare the canonical bridge, embedded App copy and OptDesk context post."""
    root = Path(__file__).resolve().parents[1]
    canonical = (root / "core" / "src" / "runtime" / "browser" / "module_host_bridge.js").read_text(encoding="utf-8")
    embedded = (root / "products" / "app" / "capability" / "option-helper" / "assets" / "pages" / "module-host-bridge.js").read_text(encoding="utf-8")
    workspace = (root / "products" / "app" / "frontend" / "optchat" / "optchat.js").read_text(encoding="utf-8")

    def single_wrapper(source: str) -> bool:
        return (
            'body: JSON.stringify(body)' in source
            and 'envelope.result || envelope' in source
            and 'JSON.stringify({ request: body })' not in source
        )

    return {
        "canonical_reads_task_id": "context.task_id" in canonical and 'body[field] = bound' in canonical,
        "embedded_reads_task_id": "context.task_id" in embedded and 'body[field] = bound' in embedded,
        "workspace_posts_host_context": 'postMessage({ type: "optionhelper.module-host-context", context }' in workspace,
        "canonical_single_wrapper": single_wrapper(canonical),
        "embedded_single_wrapper": single_wrapper(embedded),
    }


class ControlledSelectionPort:
    def __init__(self, scenario: str = "valid", module_run_ref: Mapping[str, Any] | None = None) -> None:
        run_ref = dict(module_run_ref or {
            "module": "pricer", "tenant_id": "eval-tenant", "task_id": "eval-task",
            "run_id": "eval-pricing", "expected_semantic_result_hash": "b" * 64,
            "expected_artifact_manifest_hash": "c" * 64,
        })
        self.source = {
            "source_id": "eval-source",
            "label": "评测来源",
            "tenant_id": "eval-tenant",
            "task_id": "eval-task",
            "analysis_case_id": "eval-case",
            "catalog_version": "eval-catalog",
            "source_refs": {
                "catalog_version_ref": {"catalog_version": "eval-catalog", "content_hash": "a" * 64},
                "product_version_refs": {},
                "evidence_refs": {},
            },
            "candidates": [{
                "candidate_id": "eval-candidate",
                "product_id": "2.1",
                "product_version": "v1.0",
                "product_name": "看涨期权",
                "contract_fingerprint": "d" * 64,
                "analysis_basis_id": "eval-basis",
                "underlyings": ["000905.SH"],
                "currency": "CNY",
                "price_convention": {"spot": "close", "adjustment": "forward"},
                "rank": 1,
                "reason": "受控评测候选，仅用于验证选择协议。",
                "suitable_for": ["可承受权利金损失的专业客户。"],
                "not_suitable_for": ["要求本金保障的客户。"],
                "main_risks": ["期权权利金可能全部损失。"],
                "library_status": "ready",
                "key_terms": [],
                "evidence_refs": [],
                "module_run_refs": {
                    "pricing": run_ref,
                },
            }],
        }
        if scenario == "cross_tenant":
            self.source["tenant_id"] = "other-tenant"
        if scenario == "tampered_hash":
            self.source["candidates"][0]["module_run_refs"]["pricing"]["expected_semantic_result_hash"] = "c" * 64

    def list_report_sources(self, *, tenant_id: str, task_id: str | None = None, query: str | None = None) -> Mapping[str, Any]:
        return {"tenant_id": tenant_id, "sources": [self.source]}

    def get_report_source(self, *, tenant_id: str, source_id: str) -> Mapping[str, Any]:
        if source_id != "eval-source":
            raise KeyError(source_id)
        return self.source


def reporter_selection_protocol(scenario: str) -> Mapping[str, Any]:
    from modules.reporter.service import build_selected_request

    selection = {
        "source_id": "eval-source",
        "candidate_ids": ["eval-candidate"],
        "selected_modules": ["pricing"],
        "delivery_mode": "single",
        "output_type": "report",
        "format": "html",
        "html_report_layout": "continuous",
        "audience": "professional",
        "report_run_id": "eval-report",
        "metadata": {"title": "评测报告"},
    }
    if scenario == "browser_source_refs_rejected":
        selection["source_refs"] = {"result_dir": "/tmp/untrusted"}
    try:
        request = build_selected_request(selection, tenant_id="eval-tenant", selection_port=ControlledSelectionPort())
    except Exception as error:
        return {"scenario": scenario, "exception_type": type(error).__name__, "message": str(error)}
    serialized = json.dumps(request, ensure_ascii=False)
    return {
        "scenario": scenario,
        "task_id": request["task_id"],
        "selected_modules": request["subject_ref"]["selected_modules"],
        "pricing_run_id": request["source_refs"]["module_run_refs"]["eval-candidate"]["pricing"]["run_id"],
        "html_report_layout": request["html_report_layout"],
        "contains_physical_path": "/tmp/" in serialized or "result_dir" in serialized,
    }


def reporter_public_protocol(output_root: Path) -> Mapping[str, Any]:
    """Probe Reporter only through its public Tool adapter and injected ports."""
    from runtime.adapters.local_store import LocalResultStore
    from modules.reporter.models import stable_hash
    from modules.reporter.service import call_tool

    selection = {
        "source_id": "eval-source", "candidate_ids": ["eval-candidate"],
        "selected_modules": ["pricing"], "delivery_mode": "single",
        "output_type": "report", "format": "html", "html_report_layout": "continuous",
        "audience": "professional", "report_run_id": "eval-report-public", "metadata": {},
    }
    store = LocalResultStore(output_root / "store")
    result = {"present_value": 1.25, "execution_fingerprint": "e" * 64}
    semantic_hash = stable_hash(result)
    run_ref = store.commit_module_run(
        module="pricer", tenant_id="eval-tenant", task_id="eval-task", run_id="eval-pricing",
        files={
            "manifest.json": {
                "module": "pricer", "tenant_id": "eval-tenant", "task_id": "eval-task",
                "run_id": "eval-pricing", "analysis_case_id": "eval-case",
                "candidate_id": "eval-candidate", "status": "succeeded", "lifecycle_status": "succeeded",
                "semantic_result_hash": semantic_hash, "execution_fingerprint": "e" * 64,
                "contract_fingerprint": "d" * 64, "product_version": "v1.0",
                "resolved_contract": "resolved_contract.json", "result": "result.json",
                "limitations": "limitations.json", "artifacts": [],
            },
            "input_snapshot.json": {},
            "resolved_contract.json": {
                "identity": {
                    "product_id": "2.1", "name_zh": "看涨期权",
                    "underlyings": ["000905.SH"], "currency": "CNY",
                },
                "terms": {"S0": 100.0, "K": 100.0, "T": 1.0, "Pi_0": 5.0},
                "term_sources": {}, "paths": [], "product_version": "v1.0",
                "resolved_schedules": {}, "contract_fingerprint": "d" * 64,
                "analysis_basis_id": "eval-basis",
                "price_convention": {"spot": "close", "adjustment": "forward"},
            },
            "data_refs.json": {"data_refs": []}, "limitations.json": {"limitations": []},
            "result.json": result,
        },
    )
    tampered_selection = {**selection, "report_run_id": "eval-report-public-tampered"}
    outcomes = {
        "missing_store": call_tool({"action": "run"}, designer_port=PublicDesignerPort()),
        "missing_designer": call_tool({"action": "run"}, result_store=store),
        "missing_selection_port": call_tool(
            {"action": "run", "selection": selection}, result_store=store,
            designer_port=PublicDesignerPort(), tenant_id="eval-tenant",
        ),
        "unknown_source": call_tool(
            {"action": "run", "selection": {**selection, "source_id": "unknown-source"}},
            result_store=store, designer_port=PublicDesignerPort(),
            selection_port=ControlledSelectionPort(module_run_ref=run_ref.__dict__), tenant_id="eval-tenant",
        ),
        "cross_tenant": call_tool(
            {"action": "run", "selection": selection}, result_store=store,
            designer_port=PublicDesignerPort(), selection_port=ControlledSelectionPort("cross_tenant", run_ref.__dict__),
            tenant_id="eval-tenant",
        ),
        "tampered_hash": call_tool(
            {"action": "run", "selection": tampered_selection}, result_store=store,
            designer_port=PublicDesignerPort(), selection_port=ControlledSelectionPort("tampered_hash", run_ref.__dict__),
            tenant_id="eval-tenant", output_root=output_root / "reports",
        ),
    }
    errors = {name: str(result.get("error")) for name, result in outcomes.items()}
    return {
        "errors": errors,
        "all_rejected": all(result.get("ok") is False for result in outcomes.values()),
        "selection_never_auto_latest": errors["missing_selection_port"] == "selection_validation_failed",
        "tampered_hash_rejected": outcomes["tampered_hash"].get("ok") is False,
    }


def datafetcher_caller_context(allowed: bool) -> Mapping[str, Any]:
    from runtime.protocol.models import CallerContext
    from modules.datafetcher.config import DataFetcherConfig
    from modules.datafetcher.models import DataRequest
    from modules.datafetcher.request_validator import validate_request

    caller = CallerContext(
        tenant_id="eval-tenant",
        principal_id="eval-principal",
        role="admin" if allowed else "desk",
        capabilities=("data:read", "data:force_refresh") if allowed else ("data:read",),
        session_id="eval-session",
        audience="internal",
    )
    request = DataRequest.from_mapping({
        "asset_id": "000905.SH",
        "start_date": "2024-01-02",
        "end_date": "2024-01-05",
        "fields": ["close"],
        "source_priority": ["local"],
        "cache_policy": "force_refresh",
    })
    try:
        validated = validate_request(request, DataFetcherConfig(), caller)
    except Exception as error:
        return {"allowed": allowed, "exception_type": type(error).__name__, "message": str(error)}
    return {"allowed": allowed, "cache_policy": validated.cache_policy, "tenant_id": caller.tenant_id}


def datafetcher_app_security() -> Mapping[str, Any]:
    """Exercise the App-only DataFetcher boundary without network or credentials."""
    from runtime.protocol.models import CallerContext
    from modules.datafetcher.service import call_tool_from_app

    allowed = CallerContext(
        tenant_id="eval-tenant", principal_id="eval-principal", role="admin",
        capabilities=("data:read",), session_id="eval-session", audience="internal",
    )
    denied = CallerContext(
        tenant_id="eval-tenant", principal_id="eval-principal", role="desk",
        capabilities=(), session_id="eval-session", audience="internal",
    )
    requests = {
        "plaintext_secret": ({"action": "status", "token": "redacted-eval-value"}, allowed),
        "forged_context": ({"action": "status", "caller_context": {"role": "admin"}}, allowed),
        "missing_read_capability": ({"action": "status"}, denied),
        "fetch_without_secret_ref": ({
            "action": "fetch", "request": {
                "asset_id": "000905.SH", "start_date": "2024-01-02", "end_date": "2024-01-05",
                "fields": ["close"], "source_priority": ["local"],
            },
        }, allowed),
    }
    outcomes: dict[str, str] = {}
    for name, (request, caller) in requests.items():
        try:
            call_tool_from_app(request, caller_context=caller)
            outcomes[name] = "accepted"
        except Exception as error:
            outcomes[name] = type(error).__name__
    return {
        "outcomes": outcomes,
        "all_rejected": all(value != "accepted" for value in outcomes.values()),
    }


class PublicDesignerPort:
    def call_tool(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        from modules.designer.service import call_tool
        value = dict(request)
        value["config"] = {"allow_pdf": False}
        return call_tool(value)


def real_report_delivery(output_root: Path, output_type: str) -> Mapping[str, Any]:
    from runtime.adapters.local_store import LocalResultStore
    from modules.reporter.models import stable_hash
    from modules.reporter.reporter_engine import build_report

    candidate = {
        "candidate_id": "candidate-call", "product_id": "2.1", "product_name": "看涨期权",
        "product_version": "eval-v1", "contract_fingerprint": "eval-contract-fp",
        "analysis_basis_id": "eval-basis", "underlyings": ["000300.SH"], "currency": "CNY",
        "price_convention": {"spot": "close", "adjustment": "forward"}, "rank": 1,
        "reason": "受控评测候选。", "suitable_for": ["可承受权利金损失。"],
        "not_suitable_for": ["要求保本。"], "main_risks": ["权利金可能全部损失。"],
        "library_status": "ready", "key_terms": [], "evidence_refs": ["eval-evidence-21"],
    }
    recommendation = {
        "schema": "optionhelper.recommendation-set/v2", "tenant_id": "eval-tenant",
        "task_id": "eval-task", "analysis_case_id": "eval-case", "run_id": "eval-recommend",
        "catalog_version": "eval-catalog", "catalog_content_hash": "a" * 64,
        "candidates": [candidate],
    }
    request = {
        "schema": "optionhelper.report-request/v2", "tenant_id": "eval-tenant",
        "task_id": "eval-task", "report_run_id": f"eval-{output_type}",
        "analysis_case_id": "eval-case", "subject_type": "contract",
        "subject_ref": {"delivery_mode": "single", "candidate_ids": ["candidate-call"], "selected_modules": ["recommender"]},
        "source_refs": {
            "product_version_refs": {"candidate-call": {"product_id": "2.1", "product_version": "eval-v1", "content_hash": "1" * 64}},
            "catalog_version_ref": {"catalog_version": "eval-catalog", "content_hash": "a" * 64},
            "evidence_refs": {"recommendation_set": {"source_id": "eval/recommendation", "run_id": "eval-recommend", "payload": recommendation, "expected_semantic_result_hash": stable_hash(recommendation)}},
            "module_run_refs": {"candidate-call": {}},
        },
        "output_type": output_type, "format": "html",
        "html_report_layout": "continuous" if output_type == "report" else None,
        "audience": "professional", "metadata": {"title": "评测结构报告", "as_of_date": "2026-08-09"},
    }
    outcome = build_report(
        request, result_store=LocalResultStore(output_root / "store"),
        designer_port=PublicDesignerPort(), output_root=output_root / "reports",
    )
    directory = Path(outcome["directory"])
    unit = json.loads((directory / "report-unit.json").read_text(encoding="utf-8"))
    html = (directory / "report.html").read_text(encoding="utf-8")
    return {
        "output_type": output_type, "html": html, "semantic_fact_hash": unit["semantic_fact_hash"],
        "evidence_status": unit["evidence_status"], "report_exists": True,
    }


def agent_runtime_contract(scenario: str) -> Mapping[str, Any]:
    """Offline regression gate for the bounded App Agent decision protocol."""

    from products.app.backend.agent_runtime.agent_loop import AgentLoop
    from products.app.backend.authorization.roles import Role
    from products.app.backend.errors import UnavailableCapabilityError
    from products.app.backend.identity.session_identity import SessionIdentity

    identity = SessionIdentity("eval-principal", "eval-tenant", Role.SALES, "eval-session")

    class Context:
        def build(self, _identity: object, _task_id: str, message: str) -> Mapping[str, Any]:
            context: dict[str, Any] = {
                "caller": {"role": "sales"}, "messages": [{"role": "user", "content": message}],
                "tool_catalog": [{"name": "knowledger.search"}, {"name": "pricer.run"}, {"name": "recommender.run"}],
            }
            if scenario == "historical_fact":
                context["facts"] = {"module_run_facts": [{
                    "run_ref": {"module": "pricer", "run_id": "prior-run", "result_hash": "a" * 64},
                    "facts": [{"fact_ref": "fact_a1b2c3d4", "label": "PV", "value": 12.34, "unit": "CNY"}],
                }]}
            return context

    class Gateway:
        def __init__(self, decisions: list[Mapping[str, Any]]) -> None:
            self.decisions = decisions

        def decide_for(self, _identity: object, _task_id: str, _context: Mapping[str, Any]) -> Mapping[str, Any]:
            return self.decisions.pop(0)

    class Tools:
        def __init__(self, failure: bool = False) -> None:
            self.calls: list[str] = []
            self.failure = failure

        def call(self, _identity: object, _task_id: str, name: str, _arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            self.calls.append(name)
            if self.failure:
                raise UnavailableCapabilityError("pricer", "offline")
            return {"ok": True, "status": "succeeded", "module_run_ref": {
                "module": "pricer", "run_id": "eval-run", "task_id": "eval-task", "tenant_id": "eval-tenant",
                "expected_semantic_result_hash": "a" * 64,
                "expected_artifact_manifest_hash": "b" * 64,
            }}

    class Recommender:
        def __init__(self) -> None:
            self.calls = 0

        def run_fixed(self, _identity: object, _task_id: str, _prompt: str, _arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            self.calls += 1
            return {"route": {"fixed_recommendation_workflow": True}, "recommendation_set": {"status": "pending_approval", "candidates": []}}

    class Facts:
        def facts_for(self, _identity: object, _task_id: str, _tool: str, _value: Mapping[str, Any]) -> Mapping[str, Any]:
            return {
                "run_ref": {"module": "pricer", "run_id": "eval-run", "result_hash": "a" * 64},
                "facts": [{"fact_ref": "fact_a1b2c3d4", "label": "PV", "value": 12.34, "unit": "CNY"}],
            }

    plans: dict[str, list[Mapping[str, Any]]] = {
        "knowledge": [{"action": "final", "text": "该结构的条款和主要风险如下。"}],
        "missing_pricer": [{"action": "ask_user", "question": "请提供估值日和市场数据引用。"}],
        "recommendation": [
            {"action": "call_tool", "tool": "recommender.run", "arguments": {}},
            {"action": "request_approval", "message": "请确认候选结构后再运行。"},
        ],
        "tool_failure": [
            {"action": "call_tool", "tool": "pricer.run", "arguments": {"product_id": "2.1"}},
            {"action": "final", "text": "定价未完成，需要补充输入后重试。"},
        ],
        "duplicate": [
            {"action": "call_tool", "tool": "pricer.run", "arguments": {"product_id": "2.1"}},
            {"action": "call_tool", "tool": "pricer.run", "arguments": {"product_id": "2.1"}},
        ],
        "permission": [{"action": "call_tool", "tool": "admin.maintenance", "arguments": {}}],
        "round_limit": [
            {"action": "call_tool", "tool": "pricer.run", "arguments": {"product_id": "2.1"}},
            {"action": "final", "text": "不应到达。"},
        ],
        "untraceable_number": [{"action": "final", "text": "PV为12.4。"}],
        "historical_fact": [{"action": "final", "text": "PV为12.34[fact:fact_a1b2c3d4]。", "fact_refs": ["fact_a1b2c3d4"]}],
        "mismatched_fact": [
            {"action": "call_tool", "tool": "pricer.run", "arguments": {"product_id": "2.1"}},
            {"action": "final", "text": "PV为99[fact:fact_a1b2c3d4]。", "fact_refs": ["fact_a1b2c3d4"]},
        ],
        "approval_number": [{"action": "request_approval", "message": "PV为99，请确认。"}],
        "sensitive_argument": [{
            "action": "call_tool", "tool": "pricer.run",
            "arguments": {"secret_value": "redacted-eval-value"},
        }],
    }
    if scenario not in plans:
        raise ValueError(f"未知Agent评测场景：{scenario}")
    tools = Tools(failure=scenario == "tool_failure")
    recommender = Recommender()
    result = AgentLoop(
        gateway=Gateway(plans[scenario]), context_builder=Context(), tool_executor=tools, recommender=recommender,
        max_rounds=1 if scenario == "round_limit" else 3,
        observation_builder=Facts() if scenario == "mismatched_fact" else None,
    ).run(identity, "eval-task", "评测请求")
    return {
        "scenario": scenario,
        "status": result["status"],
        "tool_calls": list(tools.calls),
        "recommender_calls": recommender.calls,
        "text": result["text"],
        "observation_statuses": [item.get("status") for item in result["observations"]],
    }


def agent_security_matrix() -> Mapping[str, Any]:
    """Keep authorization, secret, and traceable-number gates on the real AgentLoop."""
    outcomes = {
        scenario: dict(agent_runtime_contract(scenario))
        for scenario in ("permission", "sensitive_argument", "untraceable_number", "mismatched_fact")
    }
    expected = {
        "permission": "blocked",
        "sensitive_argument": "unavailable",
        "untraceable_number": "partial",
        "mismatched_fact": "partial",
    }
    return {
        "statuses": {name: result["status"] for name, result in outcomes.items()},
        "all_blocked": all(outcomes[name]["status"] == status for name, status in expected.items()),
        "unauthorized_tool_not_called": outcomes["permission"]["tool_calls"] == [],
        "sensitive_tool_not_called": outcomes["sensitive_argument"]["tool_calls"] == [],
    }
