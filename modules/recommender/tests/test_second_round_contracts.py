"""第二轮推荐约束的先失败回归测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CORE_SRC = PROJECT_ROOT / "core" / "src"
for source in (PROJECT_ROOT, CORE_SRC):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.recommender.intent_router import route_intent
from modules.recommender.candidate_builder import build_candidates
from modules.recommender.executor import execute
from modules.recommender.models import (
    CandidateContract,
    EvidenceRef,
    ModuleRunRef,
    RecommendationCandidate,
    RecommendationValidationError,
)
from modules.recommender.ports import HttpAgentPort, HttpToolPort, PortError
from modules.recommender.service import RecommenderService, call_tool


class StubEndpoint:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def post(self, path: str, payload: dict) -> dict:
        self.calls.append((path, dict(payload)))
        return dict(self.response)


def candidate() -> RecommendationCandidate:
    evidence = EvidenceRef(
        evidence_id="ev_1", product_id="2.1", catalog_version="v1.0",
        source="optionlib", section="2.1", material_status="ready",
        excerpt_hash="a" * 64, excerpt="受控条款",
    )
    return RecommendationCandidate(
        candidate_id="candidate_1", product_id="2.1", underlyings=("000300.SH",), rank=1,
        reason="受控证据支持。", suitable_for=("看涨。",), not_suitable_for=("保本。",),
        main_risks=("权利金损失。",), library_status="ready", evidence_refs=(evidence,),
    )


def contract_without_resolved() -> dict:
    return {
        "candidate_id": "candidate_1",
        "product_id": "2.1",
        "underlyings": ["000300.SH"],
        "catalog_version": "v1.0",
        "evidence_ref_ids": ["ev_1"],
        "contract_fingerprint": "a" * 64,
    }


def resolved_contract() -> dict:
    return {
        "identity": {"product_id": "2.1", "underlyings": ["000300.SH"]},
        "terms": {"strike": 1.0},
        "term_sources": {"strike": "override"},
        "paths": [{"path_id": "up"}],
        "product_version": "v1.0",
        "resolved_schedules": {},
        "contract_fingerprint": "a" * 64,
    }


class RecordingToolPort:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, module: str, payload: dict) -> dict:
        self.calls.append((module, dict(payload)))
        return {
            "status": "succeeded",
            "run_id": f"run_{module}",
            "candidate_id": "candidate_1",
            "catalog_version": "v1.0",
            "contract_fingerprint": "a" * 64,
        }


class FixedExecutorStep:
    class Audit:
        def append(self, *_args, **_kwargs) -> None:
            pass

    def __init__(self, module: str) -> None:
        self.module = module
        self.audit = self.Audit()

    def run(self, _role: str, _payload: dict) -> dict:
        return {"tool_requests": [{"candidate_id": "candidate_1", "module": self.module}]}


def execution_contract(module_inputs: dict | None = None) -> dict:
    return {
        "candidate_id": "candidate_1",
        "product_id": "2.1",
        "underlyings": ["000300.SH"],
        "catalog_version": "v1.0",
        "evidence_ref_ids": ["ev_1"],
        "resolved_contract": resolved_contract(),
        "contract_fingerprint": "a" * 64,
        "module_inputs": module_inputs or {},
    }


class SecondRoundContractTest(unittest.TestCase):
    def test_app_http_tool_adapter_uses_module_route_without_v1_wrapper(self) -> None:
        endpoint = StubEndpoint({"tool": "payoffer", "result": {"status": "unsupported"}})
        result = HttpToolPort(endpoint).call("payoffer", {"contract": resolved_contract()})
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(endpoint.calls, [("/api/tools/payoffer", {"contract": resolved_contract()})])

    def test_executor_uses_shared_payoffer_tool_schema(self) -> None:
        port = RecordingToolPort()
        execute(
            (candidate(),), approved_candidate_ids=("candidate_1",), requested_outputs=("payoff",),
            task_id="task_1", run_id="recommend_1",
            candidate_contracts={"candidate_1": execution_contract()},
            step_runner=FixedExecutorStep("payoffer"), tool_port=port,
        )
        self.assertEqual(port.calls, [("payoffer", {"contract": resolved_contract()})])

    def test_executor_uses_shared_pricer_tool_schema(self) -> None:
        pricing_config = {"model_method": "black_scholes"}
        market_data_refs = [{"data_asset_id": "asset_1", "content_hash": "b" * 64}]
        port = RecordingToolPort()
        execute(
            (candidate(),), approved_candidate_ids=("candidate_1",), requested_outputs=("pricing",),
            task_id="task_1", run_id="recommend_1",
            candidate_contracts={"candidate_1": execution_contract({
                "pricer": {"pricing_config": pricing_config, "market_data_refs": market_data_refs},
            })},
            step_runner=FixedExecutorStep("pricer"), tool_port=port,
        )
        self.assertEqual(port.calls, [("pricer", {
            "contract": resolved_contract(),
            "pricing_config": pricing_config,
            "market_data_refs": market_data_refs,
        })])

    def test_executor_uses_shared_backtester_tool_schema(self) -> None:
        backtest_config = {"entry_frequency": "monthly"}
        historical_data = {"data_asset_id": "asset_2", "content_hash": "c" * 64}
        port = RecordingToolPort()
        execute(
            (candidate(),), approved_candidate_ids=("candidate_1",), requested_outputs=("backtest",),
            task_id="task_1", run_id="recommend_1",
            candidate_contracts={"candidate_1": execution_contract({
                "backtester": {"backtest_config": backtest_config, "historical_data": historical_data},
            })},
            step_runner=FixedExecutorStep("backtester"), tool_port=port,
        )
        self.assertEqual(port.calls, [("backtester", {
            "contract": resolved_contract(),
            "backtest_config": backtest_config,
            "historical_data": historical_data,
        })])

    def test_named_option_execution_and_report_boundary(self) -> None:
        execution = route_intent("运行看涨期权定价")
        self.assertEqual(execution.route, "direct_execution")
        self.assertFalse(execution.fixed_recommendation_workflow)
        report = route_intent("请生成看涨期权的专业报告")
        self.assertEqual(report.route, "freeform")
        self.assertFalse(report.fixed_recommendation_workflow)
        market_report = route_intent("请生成市场行情报告")
        self.assertEqual(market_report.route, "freeform")
        recommendation_report = route_intent("请基于震荡行情生成投资建议报告")
        self.assertEqual(recommendation_report.route, "professional_report")
        self.assertTrue(recommendation_report.fixed_recommendation_workflow)

    def test_candidate_contract_leaves_financial_contract_validation_to_core(self) -> None:
        value = execution_contract()
        value["resolved_contract"] = {"contract_fingerprint": "a" * 64}
        bound = CandidateContract.from_mapping(value, candidate=candidate())
        self.assertEqual(bound.resolved_contract, {"contract_fingerprint": "a" * 64})

    def test_non_recommendation_pdf_is_not_professional_recommendation(self) -> None:
        decision = route_intent("请把2.1产品说明导出PDF")
        self.assertEqual(decision.route, "freeform")
        self.assertFalse(decision.fixed_recommendation_workflow)

    def test_agent_capability_outer_failure_is_rejected(self) -> None:
        port = HttpAgentPort(StubEndpoint({
            "ok": False,
            "message": "gateway拒绝",
            "capability": {"model_id": "stale", "structured_output": True, "tool_calling": True, "multi_agent": True, "max_parallel_agents": 3},
        }))
        with self.assertRaises(PortError):
            port.capability()

    def test_tool_outer_failure_is_not_reinterpreted_as_inner_success(self) -> None:
        port = HttpToolPort(StubEndpoint({
            "ok": False,
            "status": "timed_out",
            "result": {"ok": True, "status": "succeeded", "run_id": "stale-run"},
        }))
        with self.assertRaises(PortError):
            port.call("payoffer", {"action": "run"})

    def test_candidate_contract_requires_resolved_contract(self) -> None:
        with self.assertRaises(RecommendationValidationError):
            CandidateContract.from_mapping(contract_without_resolved(), candidate=candidate())

    def test_module_run_requires_return_binding(self) -> None:
        with self.assertRaises(RecommendationValidationError):
            ModuleRunRef.from_tool_result(
                "payoffer",
                {"status": "succeeded", "run_id": "run_1"},
                candidate_id="candidate_1",
                catalog_version="v1.0",
                contract_fingerprint="a" * 64,
            )

    def test_bound_succeeded_run_is_preserved(self) -> None:
        run = ModuleRunRef.from_tool_result(
            "payoffer",
            {
                "status": "succeeded",
                "run_id": "run_1",
                "candidate_id": "candidate_1",
                "catalog_version": "v1.0",
                "contract_fingerprint": "a" * 64,
            },
            candidate_id="candidate_1",
            catalog_version="v1.0",
            contract_fingerprint="a" * 64,
        )
        self.assertEqual(run.status, "succeeded")

    def test_all_terminal_module_run_statuses_are_preserved(self) -> None:
        for status in ("succeeded", "partial", "failed", "unsupported", "cancelled", "timed_out"):
            with self.subTest(status=status):
                payload = {
                    "status": status,
                    "run_id": f"run_{status}",
                    "candidate_id": "candidate_1",
                    "catalog_version": "v1.0",
                    "contract_fingerprint": "a" * 64,
                }
                run = ModuleRunRef.from_tool_result(
                    "payoffer", payload, candidate_id="candidate_1", catalog_version="v1.0",
                    contract_fingerprint="a" * 64,
                )
                self.assertEqual(run.status, status)

    def test_optionreg_conflicting_status_rejects_candidate(self) -> None:
        def evidence(source: str, *, identity: dict | None = None, entry_status: bool | None = None) -> EvidenceRef:
            return EvidenceRef(
                evidence_id=f"ev_{source}_{entry_status}", product_id="2.1", catalog_version="v1.0",
                source=source, section="2.1", material_status="ready", excerpt_hash="a" * 64,
                excerpt="受控证据", identity=identity or {}, entry_status=entry_status,
            )

        evidence_rows = (
            evidence("optionlist", identity={"product_id": "2.1"}),
            evidence("optionlib"),
            evidence("optionreg_status", entry_status=True),
            evidence("optionreg_status", entry_status=False),
        )
        candidates, rejected = build_candidates(
            {"proposals": [{
                "product_id": "2.1", "underlyings": ["000300.SH"], "reason": "x",
                "suitable_for": ["x"], "not_suitable_for": ["x"], "main_risks": ["x"],
                "library_status": "ready", "evidence_ref_ids": [item.evidence_id for item in evidence_rows],
            }]},
            {"reviews": [{"product_id": "2.1", "hard_reject": False}]},
            evidence=evidence_rows, run_id="run_1",
        )
        self.assertFalse(candidates)
        self.assertIn("entry_status", rejected[0]["reason"])

    def test_freeform_failed_tool_returns_partial_status(self) -> None:
        class Agent:
            def run_step(self, _role: str, _payload: dict) -> dict:
                if not hasattr(self, "called"):
                    self.called = True
                    return {"response": "", "tool_requests": [{"module": "unknown", "request": {}}]}
                return {"response": "工具不可用。", "tool_requests": []}

        service = RecommenderService(agent_port=Agent())
        result = service.recommend({
            "analysis_case_id": "case_1", "task_id": "task_1", "tenant_id": "tenant_1",
            "prompt": "帮我处理这个", "catalog_version": "v1.0",
        })
        self.assertEqual(result["freeform"]["status"], "partial")

    def test_freeform_module_failure_response_returns_partial_status(self) -> None:
        class Agent:
            def run_step(self, _role: str, _payload: dict) -> dict:
                if not hasattr(self, "called"):
                    self.called = True
                    return {"response": "", "tool_requests": [{"module": "payoffer", "request": {"action": "run"}}]}
                return {"response": "收益图未生成。", "tool_requests": []}

        class Tool:
            def call(self, _module: str, _payload: dict) -> dict:
                return {"ok": False, "status": "unavailable", "reason": "模块未接通"}

        service = RecommenderService(agent_port=Agent(), tool_port=Tool())
        result = service.recommend({
            "analysis_case_id": "case_1", "task_id": "task_1", "tenant_id": "tenant_1",
            "prompt": "帮我处理这个", "catalog_version": "v1.0",
        })
        freeform = result["freeform"]
        self.assertEqual(freeform["status"], "partial")
        self.assertEqual(freeform["tool_results"][0]["status"], "failed")
        self.assertEqual(freeform["tool_results"][0]["module_status"], "unavailable")

    def test_freeform_module_partial_response_returns_partial_status(self) -> None:
        class Agent:
            def run_step(self, _role: str, _payload: dict) -> dict:
                if not hasattr(self, "called"):
                    self.called = True
                    return {"response": "", "tool_requests": [{"module": "payoffer", "request": {"action": "run"}}]}
                return {"response": "收益图不完整。", "tool_requests": []}

        class Tool:
            def call(self, _module: str, _payload: dict) -> dict:
                return {"ok": True, "status": "partial", "run_id": "run_partial"}

        service = RecommenderService(agent_port=Agent(), tool_port=Tool())
        result = service.recommend({
            "analysis_case_id": "case_1", "task_id": "task_1", "tenant_id": "tenant_1",
            "prompt": "帮我处理这个", "catalog_version": "v1.0",
        })
        freeform = result["freeform"]
        self.assertEqual(freeform["status"], "partial")
        self.assertEqual(freeform["tool_results"][0]["status"], "partial")
        self.assertEqual(freeform["tool_results"][0]["module_status"], "partial")

    def test_outer_partial_result_is_not_ok(self) -> None:
        class PartialService:
            def recommend(self, _request: dict) -> dict:
                return {"recommendation_set": {"status": "partial"}}

        with patch("modules.recommender.service._service_from_environment", return_value=PartialService()):
            result = call_tool({"action": "recommend", "prompt": "任意请求"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "partial")


if __name__ == "__main__":
    unittest.main()
