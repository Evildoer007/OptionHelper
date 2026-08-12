#!/usr/bin/env python3
"""OptionHelper开发仓库专用、离线且可重复的Skill评测运行器。"""

from __future__ import annotations

import argparse
import builtins
import copy
import csv
from contextlib import contextmanager
from dataclasses import asdict, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from threading import RLock
from typing import Any, Iterator, Mapping
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CASES = Path(__file__).resolve().parent / "cases"
TARGETS = {
    "tool.call", "knowledger.lookup", "authorization.check", "recommender.controlled",
    "reporter.real_delivery", "store.cross_tenant", "workflow.card_report_equivalence",
    "workflow.catalog_alignment", "workflow.knowledge_payoff",
    "workflow.delivery_contract", "workflow.pdf_boundary", "workflow.formal_compute_protocol",
    "pricer.regression", "backtester.regression", "app.bridge_contract",
    "reporter.selection_protocol", "datafetcher.caller_context",
    "app.agent_runtime", "compute.formal_run", "compute.security_matrix",
    "compute.runref_v12",
    "modulehost.v2",
    "reporter.public_protocol", "recommender.evidence_matrix",
    "datafetcher.app_security", "app.agent_security_matrix",
}


def load_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted(CASES.glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError(f"{path.name}必须是案例数组")
        cases.extend(value)
    ids = [str(case.get("id", "")) for case in cases]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("案例ID缺失或重复")
    required = {"id", "area", "description", "target", "input", "expect"}
    for case in cases:
        if set(case) != required or not isinstance(case["input"], dict) or not isinstance(case["expect"], dict):
            raise ValueError(f"案例格式无效：{case.get('id', '<unknown>')}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]+", case["id"]):
            raise ValueError(f"案例ID无效：{case['id']}")
        if not isinstance(case["area"], str) or not case["area"] or not isinstance(case["description"], str) or not case["description"]:
            raise ValueError(f"案例说明无效：{case['id']}")
        if case["target"] not in TARGETS:
            raise ValueError(f"案例target无效：{case['id']}")
        if not case["expect"] or set(case["expect"]) - {"equals", "contains", "not_contains", "length", "exists"}:
            raise ValueError(f"案例expect无效：{case['id']}")
        if any(not isinstance(case["expect"].get(key, {}), dict) for key in ("equals", "contains", "not_contains", "length")):
            raise ValueError(f"案例断言必须为对象：{case['id']}")
        if "exists" in case["expect"] and (
            not isinstance(case["expect"]["exists"], list)
            or not all(isinstance(item, str) for item in case["expect"]["exists"])
        ):
            raise ValueError(f"案例exists必须为字符串数组：{case['id']}")
    return cases


def _path(value: Any, dotted: str) -> Any:
    current = value
    for part in dotted.split(".") if dotted else ():
        if isinstance(current, Mapping):
            current = current[part]
        elif isinstance(current, (list, tuple)):
            current = current[int(part)]
        else:
            raise KeyError(dotted)
    return current


def _assertions(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    for dotted, wanted in expected.get("equals", {}).items():
        try:
            observed = _path(actual, dotted)
        except (KeyError, IndexError, TypeError, ValueError):
            failures.append(f"{dotted}:字段不存在")
            continue
        if observed != wanted:
            failures.append(f"{dotted}:期望{wanted!r}，实际{observed!r}")
    for dotted, wanted in expected.get("contains", {}).items():
        try:
            observed = _path(actual, dotted)
        except (KeyError, IndexError, TypeError, ValueError):
            failures.append(f"{dotted}:字段不存在")
            continue
        if wanted not in observed:
            failures.append(f"{dotted}:不包含{wanted!r}")
    for dotted, unwanted in expected.get("not_contains", {}).items():
        try:
            observed = _path(actual, dotted)
        except (KeyError, IndexError, TypeError, ValueError):
            failures.append(f"{dotted}:字段不存在")
            continue
        if unwanted in observed:
            failures.append(f"{dotted}:不应包含{unwanted!r}")
    for dotted, wanted in expected.get("length", {}).items():
        try:
            observed = len(_path(actual, dotted))
        except (KeyError, IndexError, TypeError, ValueError):
            failures.append(f"{dotted}:无法读取长度")
            continue
        if observed != wanted:
            failures.append(f"{dotted}:期望长度{wanted}，实际{observed}")
    for dotted in expected.get("exists", []):
        try:
            _path(actual, dotted)
        except (KeyError, IndexError, TypeError, ValueError):
            failures.append(f"{dotted}:字段不存在")
    return failures


class EvalRuntime:
    def __init__(self) -> None:
        self._environment_names = (
            "OPTIONHELPER_PROJECT_ROOT", "OPTIONHELPER_DATA_ROOT", "OPTIONHELPER_RESULT_ROOT",
            "OPTIONHELPER_DATAFETCHER_OFFLINE", "OPTIONHELPER_MODEL_GATEWAY_URL",
            "OPTIONHELPER_KNOWLEDGER_URL", "OPTIONHELPER_TOOL_GATEWAY_URL",
        )
        self._temporary = tempfile.TemporaryDirectory(prefix="optionhelper-evals-")
        self.root = Path(self._temporary.name)
        self.data_root = self.root / "data"
        self.result_root = self.root / "result"
        self.data_root.mkdir(parents=True)
        self.market_history = ROOT / "evals" / "fixtures" / "market_history.csv"
        self.data_fixture = self.data_root / "eval-market.csv"
        with self.data_fixture.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("date", "asset_id", "close", "adj_close"))
            writer.writerows((
                ("2024-01-02", "000905.SH", 5000.0, 5000.0),
                ("2024-01-03", "000905.SH", 5020.0, 5020.0),
                ("2024-01-04", "000905.SH", 5010.0, 5010.0),
                ("2024-01-05", "000905.SH", 5050.0, 5050.0),
            ))
        self.designer_payload = json.loads(
            (ROOT / "modules" / "designer" / "tests" / "fixtures" / "report-payload.example.json").read_text(encoding="utf-8")
        )
    def close(self) -> None:
        # Each ``execute`` restores its own environment, path and module
        # snapshot under the shared lock.  Closing therefore owns only the
        # temporary directory and cannot overwrite concurrent App state.
        self._temporary.cleanup()

    @staticmethod
    def _shared_import_lock() -> object:
        """Read the same process-global lock as ``runtime.capability_import``."""

        return builtins.__dict__.setdefault("_optionhelper_capability_import_lock", RLock())

    @contextmanager
    def _execution_scope(self) -> Iterator[None]:
        """Make Eval's development roots visible only while one case executes."""

        previous_environment = {name: os.environ.get(name) for name in self._environment_names}
        previous_path = list(sys.path)
        module_snapshot = {
            name: module
            for name, module in sys.modules.items()
            if name == "modules" or name.startswith("modules.")
        }
        previous_dont_write_bytecode = sys.dont_write_bytecode
        os.environ["OPTIONHELPER_PROJECT_ROOT"] = str(ROOT)
        os.environ["OPTIONHELPER_DATA_ROOT"] = str(self.data_root)
        os.environ["OPTIONHELPER_RESULT_ROOT"] = str(self.result_root)
        os.environ["OPTIONHELPER_DATAFETCHER_OFFLINE"] = "1"
        for name in ("OPTIONHELPER_MODEL_GATEWAY_URL", "OPTIONHELPER_KNOWLEDGER_URL", "OPTIONHELPER_TOOL_GATEWAY_URL"):
            os.environ.pop(name, None)
        for path in (ROOT, ROOT / "core" / "src"):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
        sys.dont_write_bytecode = True
        # Module services capture their RuntimePaths at import time.  A test
        # executed earlier in this interpreter may therefore point a service
        # at its own temporary Store even after the Eval environment is set.
        # Eval must import a clean module namespace, then restore the caller's
        # namespace unchanged in ``finally`` below.
        for name in tuple(sys.modules):
            if name == "modules" or name.startswith("modules."):
                sys.modules.pop(name, None)
        try:
            yield
        finally:
            for name, value in previous_environment.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            for name in tuple(sys.modules):
                if (name == "modules" or name.startswith("modules.")) and name not in module_snapshot:
                    sys.modules.pop(name, None)
            sys.modules.update(module_snapshot)
            sys.path[:] = previous_path
            sys.dont_write_bytecode = previous_dont_write_bytecode

    def _resolve(self, value: Any) -> Any:
        if value == "$MARKET_HISTORY":
            return str(self.market_history)
        if value == "$DATA_FIXTURE":
            return self.data_fixture.name
        if value == "$DESIGNER_PAYLOAD":
            return copy.deepcopy(self.designer_payload)
        if value == "$DESIGNER_PAYLOAD_WITH_SCRIPT":
            payload = copy.deepcopy(self.designer_payload)
            payload["meta"]["title"] = "<script>alert(1)</script>"
            return payload
        if isinstance(value, list):
            return [self._resolve(item) for item in value]
        if isinstance(value, dict):
            return {key: self._resolve(item) for key, item in value.items()}
        return value

    def execute(self, target: str, supplied: Mapping[str, Any]) -> Mapping[str, Any]:
        # Serialize each Eval action with the App Capability import scope.
        # This prevents an Eval import from being observed half-built by an App
        # request in the same interpreter.
        with self._shared_import_lock():
            with self._execution_scope():
                return self._execute_locked(target, supplied)

    def _execute_locked(self, target: str, supplied: Mapping[str, Any]) -> Mapping[str, Any]:
        value = self._resolve(dict(supplied))
        if target == "tool.call":
            module = str(value["module"])
            request = value["request"]
            if module == "backtester" and request.get("action") == "run":
                service = __import__(f"modules.{module}.service", fromlist=["service"])
                evaluation_paths = replace(service.RUNTIME_PATHS, data_root=self.data_root, result_root=self.result_root)
                with patch.object(service, "RUNTIME_PATHS", evaluation_paths):
                    return self._formal_backtester_run(request)
            return self._public_tool_call(module, request)
        if target == "knowledger.lookup":
            from runtime.knowledger import load_registry
            registry = load_registry()
            product_id = str(value["product_id"])
            if product_id not in registry["products"]:
                raise KeyError(f"未知产品：{product_id}")
            product = registry["products"][product_id]
            return {
                "product_id": product_id,
                "name_zh": product["identity"]["name_zh"],
                "term_keys": sorted(product["terms"]),
                "path_count": len(product["paths"]),
                "product_count": len(registry["products"]),
            }
        if target == "authorization.check":
            from products.app.backend.authorization.policy import AuthorizationPolicy
            from products.app.backend.authorization.roles import Role
            role = Role(str(value["role"]))
            capability = str(value["capability"])
            return {"role": role.value, "capability": capability, "allowed": AuthorizationPolicy().allows(role, capability)}
        if target == "recommender.controlled":
            from evals.support import controlled_recommendation
            return dict(controlled_recommendation())
        if target == "recommender.evidence_matrix":
            from evals.support import recommender_evidence_matrix
            return dict(recommender_evidence_matrix())
        if target == "pricer.regression":
            from evals.support import pricer_regression
            return dict(pricer_regression(str(value["scenario"])))
        if target == "backtester.regression":
            from evals.support import backtester_regression
            return dict(backtester_regression(str(value["scenario"])))
        if target == "app.bridge_contract":
            from evals.support import app_bridge_contract
            return dict(app_bridge_contract())
        if target == "reporter.selection_protocol":
            from evals.support import reporter_selection_protocol
            return dict(reporter_selection_protocol(str(value["scenario"])))
        if target == "reporter.public_protocol":
            from evals.support import reporter_public_protocol
            return dict(reporter_public_protocol(self.root / "reporter-public"))
        if target == "datafetcher.caller_context":
            from evals.support import datafetcher_caller_context
            return dict(datafetcher_caller_context(bool(value["allowed"])))
        if target == "datafetcher.app_security":
            from evals.support import datafetcher_app_security
            return dict(datafetcher_app_security())
        if target == "app.agent_runtime":
            from evals.support import agent_runtime_contract
            return dict(agent_runtime_contract(str(value["scenario"])))
        if target == "app.agent_security_matrix":
            from evals.support import agent_security_matrix
            return dict(agent_security_matrix())
        if target == "compute.formal_run":
            return self._formal_compute_run(str(value["module"]))
        if target == "compute.security_matrix":
            return self._formal_compute_security(str(value["scenario"]))
        if target == "compute.runref_v12":
            return self._runref_v12()
        if target == "modulehost.v2":
            return self._modulehost_v2(str(value["scenario"]))
        if target == "reporter.real_delivery":
            from evals.support import real_report_delivery
            return dict(real_report_delivery(self.root / f"delivery-{value['output_type']}", str(value["output_type"])))
        if target == "store.cross_tenant":
            from runtime.adapters.local_store import LocalDataStore
            store = LocalDataStore(self.root / "tenant-store")
            reference = store.put_bytes(
                tenant_id="tenant-a", data_asset_id="asset-1", payload=b"price\n100\n",
                media_type="text/csv", schema_id="eval-prices",
            )
            store.read_bytes(reference, tenant_id="tenant-b")
            return {"unexpected": "cross_tenant_read_succeeded"}
        if target == "workflow.card_report_equivalence":
            from evals.support import real_report_delivery
            report = real_report_delivery(self.root / "equivalence-report", "report")
            card = real_report_delivery(self.root / "equivalence-card", "card")
            return {
                "equal": report["semantic_fact_hash"] == card["semantic_fact_hash"],
                "report_hash": report["semantic_fact_hash"], "card_hash": card["semantic_fact_hash"],
            }
        if target == "workflow.catalog_alignment":
            return self._catalog_alignment()
        if target == "workflow.knowledge_payoff":
            return self._knowledge_payoff(str(value["product_id"]))
        if target == "workflow.delivery_contract":
            return self._delivery_contract(str(value["output_type"]))
        if target == "workflow.pdf_boundary":
            return self._pdf_boundary()
        if target == "workflow.formal_compute_protocol":
            return self._formal_compute_protocol()
        raise ValueError(f"未知评测target：{target}")

    def _public_tool_call(self, module: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Use each module's current public entry without inventing a legacy Core route."""
        if module not in {"payoffer", "pricer", "backtester"}:
            service = __import__(f"modules.{module}.service", fromlist=["service"])
            return dict(service.call_tool(dict(request)))
        from core.tool_entry import call_tool

        action = str(request.get("action", "catalog")).strip().lower()
        policy = "module.catalog" if action in {"catalog", "status"} else "module.run"
        caller, context = self._host_context(module, policy=policy)
        return dict(call_tool(module, request, caller_context=caller, host_context=context))

    def _formal_backtester_run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """经Core编译与Host签名上下文运行Backtester，禁止回退到页面本地输入。"""
        from core.tool_entry import call_tool, prepare_compute_request
        from runtime.adapters.local_store import LocalDataStore, LocalResultStore

        tenant_id = "eval-tenant"
        task_id = "eval-backtest"
        analysis_case_id = "backtester-run-local"
        candidate_id = "eval-backtest-candidate"

        payload = self.market_history.read_bytes()
        with self.market_history.open(encoding="utf-8", newline="") as handle:
            rows = tuple(csv.DictReader(handle))
        if not rows or not rows[0]:
            raise ValueError("Backtester评测行情夹具不能为空")
        try:
            sessions = tuple(dict.fromkeys(str(row["date"]) for row in rows))
            asset_ids = tuple(sorted({str(row["asset_id"]) for row in rows}))
        except KeyError as error:
            raise ValueError(f"Backtester评测行情夹具缺少字段：{error.args[0]}") from error
        if not sessions or not asset_ids:
            raise ValueError("Backtester评测行情夹具缺少交易日或标的")
        data_ref = LocalDataStore(self.data_root).put_bytes(
            tenant_id=tenant_id,
            data_asset_id="eval-backtester-market-history",
            payload=payload,
            media_type="text/csv",
            schema_id="market-history-v1",
            asset_ids=asset_ids,
            normalized_fields=tuple(rows[0]),
            coverage={
                "start": sessions[0],
                "end": sessions[-1],
                "sessions": sessions,
                "calendar_id": "CN-SSE",
                "calendar_version": "eval-market-history-v1",
            },
            row_count=len(rows),
            price_convention={
                "contract_close_field": "close",
                "contract_adjustment": "unadjusted",
                "hv_close_field": "adj_close",
                "hv_adjustment": "forward",
                "close_equals_adj_close": True,
                "calendar": "trading_days",
            },
            lineage={"fixture": "evals/fixtures/market_history.csv"},
            created_by="eval-runner",
        )
        prepared = prepare_compute_request("backtester", request, data_refs=(asdict(data_ref),))
        caller, context = self._host_context(
            "backtester", prepared, policy="module.run", task_id=task_id,
            analysis_case_id=analysis_case_id, candidate_id=candidate_id,
        )
        return dict(call_tool(
            "backtester",
            prepared["request"],
            caller_context=caller,
            host_context=context,
            result_store=LocalResultStore(self.result_root),
            data_store=LocalDataStore(self.data_root),
        ))

    def _market_data_ref(self, tenant_id: str, asset_id: str) -> Any:
        """Store the deterministic Eval fixture and return its authenticated DataAssetRef."""
        from runtime.adapters.local_store import LocalDataStore

        with self.market_history.open(encoding="utf-8", newline="") as handle:
            rows = tuple(csv.DictReader(handle))
        sessions = tuple(dict.fromkeys(str(row["date"]) for row in rows))
        payload = self.market_history.read_bytes()
        return LocalDataStore(self.data_root).put_bytes(
            tenant_id=tenant_id,
            data_asset_id=asset_id,
            payload=payload,
            media_type="text/csv",
            schema_id="market-history-v1",
            asset_ids=("000905.SH",),
            normalized_fields=tuple(rows[0]),
            coverage={
                "start": sessions[0], "end": sessions[-1], "sessions": sessions,
                "calendar_id": "CN-SSE", "calendar_version": "eval-market-history-v1",
                "by_asset": {"000905.SH": {"start": sessions[0], "end": sessions[-1]}},
            },
            row_count=len(rows),
            price_convention={
                "adjustment": "close_and_adj_close", "close": "unadjusted", "adj_close": "adjusted",
            },
            lineage={"fixture": "evals/fixtures/market_history.csv"},
            created_by="eval-runner",
        )

    @staticmethod
    def _host_context(
        module: str,
        prepared: Mapping[str, Any] | None = None,
        *,
        fingerprint: str | None = None,
        policy: str = "module.run",
        task_id: str | None = None,
        analysis_case_id: str | None = None,
        candidate_id: str | None = None,
        tenant_id: str = "eval-tenant",
    ) -> tuple[Any, Any]:
        """Create and cryptographically verify one deterministic-scope Host context."""
        from runtime.protocol.module_host import (
            HostObjectRef,
            ModuleHostContext,
            issue_capability_token,
            verify_module_host_context,
        )

        from runtime.protocol.models import CallerContext

        task_id = task_id or (f"eval-{module}-task" if prepared is not None else None)
        case_id = analysis_case_id or (f"eval-{module}-case" if prepared is not None else None)
        candidate_id = candidate_id or (f"eval-{module}-candidate" if prepared is not None else None)
        session_id = f"eval-{module}-session"
        principal_id = "eval-principal"
        context_id = f"mhc_eval_{module}_formal_0001"
        page_hash = sha256(f"eval-{module}-page".encode()).hexdigest()
        audience = "eval-host"
        token_secret = f"eval-{module}-host-secret".encode()
        contract_fingerprint = fingerprint or (str(prepared["contract_fingerprint"]) if prepared is not None else None)
        catalog_version = str(prepared["registry_snapshot_hash"]) if prepared is not None else None
        session_ref = f"local:{session_id}"
        request_policy = (policy,)
        capability_version = "12.1"
        protocol_version = "v1.2"
        contract_ref = HostObjectRef(
            reference_id=f"resolved-contract:eval-{module}",
            schema_id="optionhelper.resolved-contract/v1",
            content_hash=contract_fingerprint,
        ) if contract_fingerprint is not None else None
        expires_at = int(time.time()) + 300
        token = issue_capability_token(
            token_secret=token_secret,
            session_id=session_id,
            principal_id=principal_id,
            session_ref=session_ref,
            module=module,
            expires_at=expires_at,
            context_id=context_id,
            page_hash=page_hash,
            audience=audience,
            host_kind="app",
            request_policy=request_policy,
            capability_version=capability_version,
            protocol_version=protocol_version,
            task_id=task_id,
            analysis_case_id=case_id,
            candidate_id=candidate_id,
            catalog_version=catalog_version,
            contract_fingerprint=contract_fingerprint,
            contract_ref=contract_ref,
            config_ref=None,
            result_refs=(),
        )
        context = ModuleHostContext(
            session_ref=session_ref, capability_token=token,
            analysis_case_id=case_id, task_id=task_id, candidate_id=candidate_id,
            catalog_version=catalog_version,
            contract_fingerprint=contract_fingerprint, module=module, page_hash=page_hash,
            capability_version=capability_version, protocol_version=protocol_version,
            context_id=context_id, host_kind="app", request_policy=request_policy,
            contract_ref=contract_ref, result_refs=(),
        )
        verify_module_host_context(
            context, token_secret=token_secret, session_id=session_id,
            principal_id=principal_id, audience=audience,
        )
        caller = CallerContext(
            tenant_id=tenant_id, principal_id=principal_id, role="admin",
            capabilities=request_policy, session_id=session_id, audience=audience,
            request_id=f"eval-{module}-request",
        )
        return caller, context

    def _prepared_compute(self, module: str, *, data_tenant: str = "eval-tenant") -> tuple[dict[str, Any], Any | None]:
        from core.tool_entry import prepare_compute_request

        common = {
            "product_id": "2.1",
            "identity": {
                "underlyings": ["000905.SH"],
                "reference_prices": {"000905.SH": 5000.0},
                "contract_start_date": "2024-01-02",
            },
            "term_overrides": {"K": 5100.0, "T": 0.5, "Pi_0": 1.0},
        }
        if module == "payoffer":
            return prepare_compute_request(module, common), None
        data_ref = self._market_data_ref(data_tenant, f"eval-{module}-{data_tenant}-market")
        if module == "pricer":
            request = {
                **common,
                "pricing_config": {
                    "valuation_date": "2024-02-05", "spot": 5390.0,
                    "historical_volatility": 0.20, "dividend_yield": 0.0,
                    "risk_free_rate": 0.02, "model_method": "black_scholes",
                },
            }
        elif module == "backtester":
            request = {
                **common,
                "backtest_config": {"entry_rule": "explicit", "entry_dates": ["2024-01-02"]},
            }
        else:
            raise ValueError(f"未知计算模块：{module}")
        from runtime.adapters.local_store import LocalDataStore

        return prepare_compute_request(
            module,
            request,
            data_refs=(asdict(data_ref),),
            data_store=LocalDataStore(self.data_root),
        ), data_ref

    def _formal_compute_run(self, module: str) -> Mapping[str, Any]:
        """Exercise the public Core Tool route with HostContext and the formal ResultStore."""
        from core.tool_entry import call_tool
        from runtime.adapters.local_store import LocalDataStore, LocalResultStore
        from runtime.protocol.models import ModuleRunRef

        prepared, _ = self._prepared_compute(module)
        caller, context = self._host_context(module, prepared)
        store = LocalResultStore(self.result_root)
        kwargs: dict[str, Any] = {
            "caller_context": caller,
            "host_context": context,
            "result_store": store,
        }
        if module in {"pricer", "backtester"}:
            kwargs["data_store"] = LocalDataStore(self.data_root)
        output = dict(call_tool(module, prepared["request"], **kwargs))
        reference = output.get("module_run_ref") if isinstance(output.get("module_run_ref"), Mapping) else {}
        run_directory = store.resolve_module_run(
            ModuleRunRef(**reference), tenant_id="eval-tenant",
        ) if reference else None
        run_manifest = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8")) if run_directory else {}
        artifact_manifest_hash = sha256(
            (run_directory / "artifacts" / "artifact_manifest.json").read_bytes()
        ).hexdigest() if run_directory else None
        return {
            "module": output.get("module"), "status": output.get("status"),
            "contract_fingerprint": output.get("contract_fingerprint"),
            "expected_contract_fingerprint": prepared["contract_fingerprint"],
            "contract_matches": output.get("contract_fingerprint") == prepared["contract_fingerprint"],
            "run_ref": dict(reference),
            "run_ref_resolves": bool(reference) and run_manifest.get("contract_fingerprint") == prepared["contract_fingerprint"],
            "artifact_anchor_matches": bool(reference) and artifact_manifest_hash == reference.get("expected_artifact_manifest_hash"),
        }

    def _formal_compute_security(self, scenario: str) -> Mapping[str, Any]:
        """Probe formal Host and tenant boundaries without weakening the production protocol."""
        from core.tool_entry import call_tool
        from runtime.adapters.local_store import LocalDataStore, LocalResultStore

        if scenario == "host_rejected":
            outcomes: dict[str, str] = {}
            prepared, _ = self._prepared_compute("payoffer")
            caller, context = self._host_context("payoffer", prepared)
            wrong_caller, wrong_context = self._host_context("payoffer", prepared, fingerprint="f" * 64)
            for label, kwargs in {
                "missing_context": {"caller_context": caller, "result_store": LocalResultStore(self.result_root)},
                "missing_store": {"caller_context": caller, "host_context": context},
                "wrong_fingerprint": {
                    "caller_context": wrong_caller, "host_context": wrong_context,
                    "result_store": LocalResultStore(self.result_root),
                },
            }.items():
                try:
                    call_tool("payoffer", prepared["request"], **kwargs)
                    outcomes[label] = "accepted"
                except Exception as error:
                    outcomes[label] = type(error).__name__
            return {"scenario": scenario, "outcomes": outcomes, "all_rejected": all(value != "accepted" for value in outcomes.values())}
        if scenario == "cross_tenant_data_ref":
            outcomes = {}
            for module in ("pricer", "backtester"):
                prepared, _ = self._prepared_compute(module, data_tenant="other-tenant")
                caller, context = self._host_context(module, prepared)
                kwargs: dict[str, Any] = {
                    "caller_context": caller,
                    "host_context": context,
                    "result_store": LocalResultStore(self.result_root),
                }
                kwargs["data_store"] = LocalDataStore(self.data_root)
                try:
                    call_tool(module, prepared["request"], **kwargs)
                    outcomes[module] = "accepted"
                except Exception as error:
                    outcomes[module] = str(error)
            return {
                "scenario": scenario, "outcomes": outcomes,
                "all_rejected": all(value != "accepted" for value in outcomes.values()),
            }
        raise ValueError(f"未知计算安全场景：{scenario}")

    def _runref_v12(self) -> Mapping[str, Any]:
        """Prove v1.2 refs use the external artifact anchor and reject five-field refs."""
        from runtime.adapters.local_store import LocalResultStore
        from runtime.protocol.models import ModuleRunRef

        formal = self._formal_compute_run("payoffer")
        reference = dict(formal["run_ref"])
        missing = dict(reference)
        missing.pop("expected_artifact_manifest_hash", None)
        missing_rejected = False
        try:
            ModuleRunRef(**missing)
        except (TypeError, ValueError):
            missing_rejected = True

        tampered = ModuleRunRef(**{**reference, "expected_artifact_manifest_hash": "f" * 64})
        tampered_rejected = False
        try:
            LocalResultStore(self.result_root).resolve_module_run(tampered, tenant_id="eval-tenant")
        except Exception:
            tampered_rejected = True
        return {
            "schema": "optionhelper.module-run-ref/v1.2",
            "field_count": len(reference),
            "artifact_anchor_matches": formal["artifact_anchor_matches"],
            "missing_artifact_anchor_rejected": missing_rejected,
            "tampered_artifact_anchor_rejected": tampered_rejected,
            "legacy_attestation_used": False,
        }

    def _modulehost_v2(self, scenario: str) -> Mapping[str, Any]:
        """Exercise v2 signed scope and the Caller/Host permission intersection."""
        from core.tool_entry import call_tool
        from runtime.protocol.module_host import verify_module_host_context

        if scenario == "signed_scope":
            prepared, _ = self._prepared_compute("payoffer")
            _, context = self._host_context("payoffer", prepared)
            replacements = {
                "session_ref": "local:tampered-session",
                "host_kind": "local-development",
                "request_policy": ("module.catalog",),
                "capability_version": "tampered-capability",
                "protocol_version": "v1.1",
                "contract_ref": replace(context.contract_ref, reference_id="resolved-contract:tampered"),
            }
            outcomes: dict[str, str] = {}
            for field, value in replacements.items():
                try:
                    verify_module_host_context(
                        replace(context, **{field: value}),
                        token_secret=b"eval-payoffer-host-secret",
                        session_id="eval-payoffer-session",
                        principal_id="eval-principal",
                        audience="eval-host",
                    )
                    outcomes[field] = "accepted"
                except Exception as error:
                    outcomes[field] = type(error).__name__
            return {
                "scenario": scenario,
                "all_tampering_rejected": all(value != "accepted" for value in outcomes.values()),
                "outcomes": outcomes,
            }
        if scenario == "permission_intersection":
            caller_catalog, host_catalog = self._host_context("payoffer", policy="module.catalog")
            catalog = call_tool(
                "payoffer", {"action": "catalog"},
                caller_context=caller_catalog, host_context=host_catalog,
            )
            catalog_mismatch = "accepted"
            try:
                call_tool(
                    "payoffer", {"action": "catalog"},
                    caller_context=replace(caller_catalog, capabilities=("module.run",)),
                    host_context=host_catalog,
                )
            except Exception as error:
                catalog_mismatch = type(error).__name__

            caller_run, host_run = self._host_context("payoffer", policy="module.run")
            preview = call_tool(
                "payoffer", {"action": "preview", "product_id": "2.1"},
                caller_context=caller_run, host_context=host_run,
            )
            run_mismatch = "accepted"
            try:
                call_tool(
                    "payoffer", {"action": "preview", "product_id": "2.1"},
                    caller_context=replace(caller_run, capabilities=("module.catalog",)),
                    host_context=host_run,
                )
            except Exception as error:
                run_mismatch = type(error).__name__
            local_host = "accepted"
            try:
                call_tool(
                    "payoffer", {"action": "preview", "product_id": "2.1"},
                    caller_context=caller_run,
                    host_context=replace(host_run, host_kind="local-development"),
                )
            except Exception as error:
                local_host = type(error).__name__
            return {
                "scenario": scenario,
                "catalog_allowed": bool(catalog.get("ok")),
                "run_allowed": bool(preview.get("ok")),
                "catalog_mismatch_rejected": catalog_mismatch != "accepted",
                "run_mismatch_rejected": run_mismatch != "accepted",
                "local_host_rejected": local_host != "accepted",
            }
        raise ValueError(f"未知ModuleHost v2场景：{scenario}")

    def _catalog_alignment(self) -> Mapping[str, Any]:
        from runtime.knowledger import load_registry
        registry_ids = set(load_registry()["products"])
        catalogs = {name: self._public_tool_call(name, {"action": "catalog"}) for name in ("payoffer", "pricer", "backtester")}
        ids = {name: {item["product_id"] for item in catalog["products"]} for name, catalog in catalogs.items()}
        return {
            "aligned": all(value == registry_ids for value in ids.values()),
            "registry_count": len(registry_ids),
            "payoffer_count": len(ids["payoffer"]),
            "pricer_count": len(ids["pricer"]),
            "backtester_count": len(ids["backtester"]),
        }

    def _knowledge_payoff(self, product_id: str) -> Mapping[str, Any]:
        from runtime.knowledger import load_registry
        product = load_registry()["products"][product_id]
        payoff = self._public_tool_call("payoffer", {"action": "preview", "product_id": product_id})
        return {"product_id": product_id, "name_zh": product["identity"]["name_zh"], "payoff_ok": payoff["ok"], "svg": payoff["svg"]}

    def _delivery_contract(self, output_type: str) -> Mapping[str, Any]:
        reporter_service = __import__("modules.reporter.service", fromlist=["service"])
        designer_service = __import__("modules.designer.service", fromlist=["service"])
        reporter = reporter_service.call_tool({"action": "status"})
        designer = designer_service.call_tool({
            "action": "render", "payload": copy.deepcopy(self.designer_payload),
            "output_type": output_type, "format": "html", "config": {"allow_pdf": False},
        })
        artifact = designer.get("artifact", {})
        return {
            "reporter_supports_output": output_type in reporter["output_types"],
            "designer_ok": bool(designer.get("ok")),
            "artifact_output_type": artifact.get("output_type"),
            "artifact_format": artifact.get("format"),
        }

    @staticmethod
    def _pdf_boundary() -> Mapping[str, Any]:
        reporter_service = __import__("modules.reporter.service", fromlist=["service"])
        designer_service = __import__("modules.designer.service", fromlist=["service"])
        reporter = reporter_service.call_tool({"action": "status"})
        designer = designer_service.call_tool({"action": "status", "config": {"allow_pdf": False}})
        available = "pdf" in designer["formats"]
        return {"reporter_formats": reporter["formats"], "designer_pdf_available": available, "status": "available" if available else "unavailable"}

    @staticmethod
    def _formal_compute_protocol() -> Mapping[str, Any]:
        """Assert the three public calculator inputs share one Core contract."""
        import inspect

        from core.tool_entry import call_tool, prepare_compute_request

        asset = "000905.SH"
        market_payload = (
            "date,asset_id,close,adj_close\n"
            "2024-01-02,000905.SH,5000,5000\n"
            "2024-01-03,000905.SH,5000,5000\n"
            "2024-01-04,000905.SH,5000,5000\n"
            "2024-01-05,000905.SH,5000,5000\n"
        ).encode("utf-8")
        market_hash = sha256(market_payload).hexdigest()

        class ProtocolDataStore:
            def read_bytes(self, reference: Any, *, tenant_id: str) -> bytes:
                if tenant_id != "eval-tenant" or reference.content_hash != market_hash:
                    raise PermissionError("Eval DataAssetRef范围不匹配")
                return market_payload

        data_ref = {
            "data_asset_id": "eval-formal-market",
            "storage_ref": "data:eval-tenant:eval-formal-market:" + market_hash + ":" + "b" * 64,
            "media_type": "text/csv",
            "schema_id": "market-history-v1",
            "asset_ids": [asset],
            "normalized_fields": ["date", "asset_id", "close", "adj_close"],
            "coverage": {
                "start": "2024-01-02", "end": "2024-01-05",
                "sessions": ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
                "calendar_id": "CN-SSE", "calendar_version": "eval-v1",
            },
            "row_count": 4,
            "price_convention": {"adjustment": "close_and_adj_close"},
            "content_hash": market_hash,
            "lineage": {"fixture": "formal-compute-protocol"},
            "tenant_id": "eval-tenant",
            "created_by": "eval-principal",
            "access_scope": ["read"],
            "partition_spec": {},
        }
        common = {
            "product_id": "2.1",
            "identity": {
                "underlyings": [asset],
                "reference_prices": {asset: 5000.0},
                "contract_start_date": "2024-01-02",
            },
            "term_overrides": {"K": 5100.0},
        }
        prepared = {
            "payoffer": prepare_compute_request("payoffer", common),
            "pricer": prepare_compute_request("pricer", {
                **common,
                "pricing_config": {"valuation_date": "2024-01-05", "model_method": "black_scholes"},
            }, data_refs=(data_ref,), data_store=ProtocolDataStore()),
            "backtester": prepare_compute_request("backtester", {
                **common,
                "backtest_config": {"entry_rule": "explicit", "entry_dates": ["2024-01-02"]},
            }, data_refs=(data_ref,)),
        }
        return {
            "tool_parameters": list(inspect.signature(call_tool).parameters),
            "same_contract": len({value["contract_fingerprint"] for value in prepared.values()}) == 1,
            "payoffer_keys": sorted(prepared["payoffer"]["request"]),
            "pricer_keys": sorted(prepared["pricer"]["request"]),
            "backtester_keys": sorted(prepared["backtester"]["request"]),
        }


def run(case_id: str | None = None) -> dict[str, Any]:
    selected = [case for case in load_cases() if case_id is None or case["id"] == case_id]
    if case_id and not selected:
        raise ValueError(f"没有案例：{case_id}")
    runtime = EvalRuntime()
    results: list[dict[str, Any]] = []
    try:
        for case in selected:
            started = time.perf_counter()
            try:
                actual = dict(runtime.execute(case["target"], case["input"]))
            except Exception as error:
                actual = {"exception": {"type": type(error).__name__, "message": str(error)}}
            failures = _assertions(actual, case["expect"])
            results.append({
                "id": case["id"], "area": case["area"], "passed": not failures,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "failures": failures,
            })
    finally:
        runtime.close()
    passed = sum(result["passed"] for result in results)
    return {"schema": "optionhelper.eval-run.v1", "total": len(results), "passed": passed, "failed": len(results) - passed, "results": results}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="只运行一个案例ID")
    parser.add_argument("--list", action="store_true", help="列出案例")
    parser.add_argument("--json", action="store_true", help="输出JSON")
    args = parser.parse_args()
    if args.list:
        for case in load_cases():
            print(f"{case['id']}\t{case['description']}")
        return
    outcome = run(args.case)
    if args.json:
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
    else:
        for result in outcome["results"]:
            label = "PASS" if result["passed"] else "FAIL"
            print(f"{label} {result['id']} ({result['duration_ms']:.2f}ms)")
            for failure in result["failures"]:
                print(f"  {failure}")
        print(f"SUMMARY {outcome['passed']}/{outcome['total']} passed, {outcome['failed']} failed")
    raise SystemExit(0 if outcome["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
