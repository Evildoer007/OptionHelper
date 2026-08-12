"""Recommender真实HTTP端口、路由、Schema、降级与审计测试。"""

from __future__ import annotations

from collections import defaultdict, deque
import copy
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import unittest
import subprocess


PROJECT_ROOT = Path(__file__).resolve().parents[3]
RECOMMENDER_SRC = PROJECT_ROOT / "modules" / "recommender" / "src"
CORE_SRC = PROJECT_ROOT / "core" / "src"
for source in (RECOMMENDER_SRC, CORE_SRC):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.recommender.candidate_critic import critique_candidates
from modules.recommender.candidate_builder import build_candidates
from modules.recommender.config import RecommenderConfig
from modules.recommender.intent_router import route_intent
from modules.recommender.models import RecommendationValidationError
from modules.recommender.models import EvidenceRef
from modules.recommender.ports import HttpAgentPort, HttpEndpoint, HttpKnowledgePort, HttpToolPort, PortError
from modules.recommender.service import RecommenderService, call_tool, capability


class ContractState:
    def __init__(self) -> None:
        self.capability = {"model_id": "test-model", "structured_output": True, "tool_calling": True, "multi_agent": False, "max_parallel_agents": 1}
        self.steps: dict[str, deque[object]] = defaultdict(deque)
        self.evidence: list[dict] = []
        self.tool_response: dict = {"ok": False, "status": "unavailable", "reason": "测试端口未连接计算模块"}
        self.requests: list[tuple[str, dict]] = []


@contextmanager
def contract_server(state: ContractState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            state.requests.append((self.path, body))
            if self.path == "/v1/model/capability":
                return self.respond(200, {"ok": True, "capability": state.capability})
            if self.path == "/v1/agent/step":
                role = body["role"]
                queue = state.steps[role]
                if not queue:
                    return self.respond(500, {"ok": False, "message": f"没有为{role}配置响应"})
                item = queue.popleft()
                if isinstance(item, Exception):
                    return self.respond(503, {"ok": False, "message": str(item)})
                return self.respond(200, {"ok": True, "result": item})
            if self.path == "/v1/knowledger/search":
                return self.respond(200, {"ok": True, "catalog_version": body["catalog_version"], "evidence": state.evidence})
            if self.path.startswith("/api/tools/"):
                return self.respond(200, {"ok": True, "result": state.tool_response})
            return self.respond(404, {"ok": False, "message": "unknown"})

        def respond(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def evidence() -> dict:
    import hashlib
    excerpt = "适用于看涨并愿意承担权利金损失的情形。"
    return {
        "evidence_id": "ev_21",
        "product_id": "2.1",
        "catalog_version": "catalog_20260807",
        "source": "optionlib",
        "section": "2.1.4适用场景",
        "library_status": "ready",
        "excerpt_hash": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        "excerpt": excerpt,
    }


def library_evidence() -> list[dict]:
    """同一产品、同一CatalogVersion的三库受控证据。"""
    import hashlib

    optionlist_excerpt = "2.1看涨期权，已录入。"
    optionreg_excerpt = "2.1可执行。"
    return [
        {
            "evidence_id": "ev_list_21",
            "product_id": "2.1",
            "catalog_version": "catalog_20260807",
            "source": "optionlist",
            "section": "产品目录",
            "library_status": "ready",
            "excerpt_hash": hashlib.sha256(optionlist_excerpt.encode("utf-8")).hexdigest(),
            "excerpt": optionlist_excerpt,
            "identity": {"product_id": "2.1", "entry_status": True},
        },
        evidence(),
        {
            "evidence_id": "ev_reg_21",
            "product_id": "2.1",
            "catalog_version": "catalog_20260807",
            "source": "optionreg_status",
            "section": "执行状态",
            "library_status": "ready",
            "excerpt_hash": hashlib.sha256(optionreg_excerpt.encode("utf-8")).hexdigest(),
            "excerpt": optionreg_excerpt,
            "entry_status": True,
        },
    ]


def candidate_contract(candidate_id: str = "recommend_001_candidate_01") -> dict:
    return {
        "candidate_id": candidate_id,
        "product_id": "2.1",
        "underlyings": ["000300.SH"],
        "catalog_version": "catalog_20260807",
        "evidence_ref_ids": ["ev_list_21", "ev_21", "ev_reg_21"],
        "resolved_contract": {
            "identity": {"product_id": "2.1", "underlyings": ["000300.SH"]},
            "contract_fingerprint": "a" * 64,
        },
        "contract_fingerprint": "a" * 64,
    }


def intent() -> dict:
    return {
        "confirmed_constraints": {"underlyings": ["000300.SH"], "view": "看涨"},
        "missing_information": [],
        "next_question": None,
        "research_queries": ["看涨且最大损失有限"],
    }


def research() -> dict:
    return {"proposals": [{
        "product_id": "2.1",
        "product_name": "看涨期权",
        "underlyings": ["000300.SH"],
        "reason": "看涨观点与有限损失约束相符。",
        "suitable_for": ["预期标的上涨且可接受权利金损失。"],
        "not_suitable_for": ["要求本金保障或固定收益。"],
        "main_risks": ["到期不涨时可能损失全部权利金。"],
        "library_status": "ready",
        "evidence_ref_ids": ["ev_21"],
        "missing_inputs": ["执行价", "期限"],
    }]}


def critic() -> dict:
    return {"reviews": [{
        "product_id": "2.1",
        "hard_reject": False,
        "rejection_reason": None,
        "additional_not_suitable_for": ["无法承受权利金归零。"],
        "additional_risks": ["时间价值衰减。"],
        "rank_adjustment": 0,
    }]}


def request(**overrides) -> dict:
    data = {
        "analysis_case_id": "case_001",
        "task_id": "task_001",
        "run_id": "recommend_001",
        "tenant_id": "tenant_001",
        "prompt": "沪深300看涨且最大损失有限，请推荐结构",
        "catalog_version": "catalog_20260807",
        "requested_outputs": [],
        "confirmed_constraints": {
            "underlying": "000300.SH", "horizon": "3个月", "market_view": "上涨+波动率上升",
            "max_loss": "30%", "principal_fluctuation": True,
        },
    }
    data.update(overrides)
    return data


def service(url: str, *, mode: str = "auto") -> RecommenderService:
    endpoint = HttpEndpoint(url, timeout_seconds=1)
    return RecommenderService(
        agent_port=HttpAgentPort(endpoint), knowledge_port=HttpKnowledgePort(endpoint), tool_port=HttpToolPort(endpoint),
        config=RecommenderConfig(agent_mode=mode, port_timeout_seconds=1),
    )


class RecommenderServiceTest(unittest.TestCase):
    def test_routes_chat_freeform_and_professional_report(self) -> None:
        self.assertEqual(route_intent("你好").route, "chat")
        self.assertEqual(route_intent("把产品资料和收益图一起整理一下").route, "freeform")
        decision = route_intent("根据当前震荡行情推荐结构并生成正式报告")
        self.assertEqual(decision.route, "professional_report")
        self.assertTrue(decision.fixed_recommendation_workflow)
        self.assertEqual(route_intent("请做一份沪深300专业报告").route, "freeform")

    def test_single_agent_recommendation_matches_strict_v1_contract(self) -> None:
        state = ContractState()
        state.evidence = library_evidence()
        state.steps["SingleAgent.Intent"].append(intent())
        state.steps["SingleAgent.Research"].append(research())
        state.steps["SingleAgent.Critic"].append(critic())
        with contract_server(state) as url:
            result = service(url).recommend(request())["recommendation_set"]
        self.assertEqual(result["schema"], "optionhelper.recommendation-set/v1")
        self.assertEqual(result["status"], "pending_approval")
        row = result["candidates"][0]
        self.assertEqual(row["underlyings"], ["000300.SH"])
        self.assertEqual(row["library_status"], "ready")
        required = {"candidate_id", "product_id", "underlyings", "rank", "reason", "suitable_for", "not_suitable_for", "main_risks", "library_status"}
        self.assertTrue(required <= set(row))
        self.assertTrue(any(path == "/v1/knowledger/search" for path, _ in state.requests))

    def test_multi_agent_failure_degrades_to_single_and_is_audited(self) -> None:
        state = ContractState()
        state.capability.update({"multi_agent": True, "max_parallel_agents": 3})
        state.evidence = library_evidence()
        state.steps["Intent"].append(intent())
        state.steps["Research"].append(RuntimeError("Research模型超时"))
        state.steps["SingleAgent.Intent"].append(intent())
        state.steps["SingleAgent.Research"].append(research())
        state.steps["SingleAgent.Critic"].append(critic())
        with contract_server(state) as url:
            result = service(url).recommend(request())["recommendation_set"]
        self.assertEqual(result["workflow_mode"], "degraded_single_agent")
        self.assertEqual(result["status"], "pending_approval")
        self.assertTrue(any(event["stage"] == "fallback" for event in result["audit_trail"]))

    def test_critic_cannot_add_product(self) -> None:
        with self.assertRaises(RecommendationValidationError):
            critique_candidates(research(), {"reviews": [{"product_id": "9.1", "hard_reject": False}]})

    def test_critic_must_review_every_research_candidate(self) -> None:
        with self.assertRaises(RecommendationValidationError):
            critique_candidates(research(), {"reviews": []})

    def test_evidence_hash_must_match_excerpt(self) -> None:
        row = evidence()
        row["excerpt_hash"] = "0" * 64
        with self.assertRaises(RecommendationValidationError):
            EvidenceRef.from_mapping(row, expected_catalog_version="catalog_20260807")

    def test_research_cannot_forge_status_runs_or_terms(self) -> None:
        controlled_evidence = EvidenceRef.from_mapping(evidence(), expected_catalog_version="catalog_20260807")
        attacks = {
            "candidate_status": "report_ready",
            "module_run_refs": [{"module": "pricer", "run_id": "fake", "status": "complete"}],
            "key_terms": [{"label": "票息", "value": "99%"}],
        }
        for field, malicious_value in attacks.items():
            with self.subTest(field=field):
                malicious_research = copy.deepcopy(research())
                malicious_research["proposals"][0][field] = malicious_value
                with self.assertRaises(RecommendationValidationError):
                    build_candidates(
                        malicious_research,
                        critic(),
                        evidence=(controlled_evidence,),
                        run_id="recommend_attack",
                    )

    def test_freeform_uses_single_agent_and_real_tool_port(self) -> None:
        state = ContractState()
        state.steps["SingleAgent.Freeform"].append({
            "response": "", "tool_requests": [{"module": "payoffer", "request": {"action": "status"}}]
        })
        state.steps["SingleAgent.Freeform"].append({"response": "Payoffer当前不可用，未生成收益图。", "tool_requests": []})
        with contract_server(state) as url:
            result = service(url).recommend(request(prompt="把产品资料和收益图一起整理一下"))["freeform"]
        self.assertIn("不可用", result["response"])
        self.assertEqual(result["tool_results"][0]["result"]["status"], "unavailable")
        self.assertTrue(any(path == "/api/tools/payoffer" for path, _ in state.requests))
        roles = [body["role"] for path, body in state.requests if path == "/v1/agent/step"]
        self.assertEqual(roles, ["SingleAgent.Freeform", "SingleAgent.Freeform"])

    def test_approved_execution_preserves_real_unavailable_status(self) -> None:
        state = ContractState()
        state.evidence = library_evidence()
        for role, value in (("SingleAgent.Intent", intent()), ("SingleAgent.Research", research()), ("SingleAgent.Critic", critic())):
            state.steps[role].append(value)
        state.steps["SingleAgent.Executor"].append({
            "tool_requests": [{
                "candidate_id": "recommend_001_candidate_01", "module": "payoffer"
            }]
        })
        with contract_server(state) as url:
            result = service(url).recommend(request(
                requested_outputs=["payoff"], approved_candidate_ids=["recommend_001_candidate_01"],
                candidate_contracts={"recommend_001_candidate_01": candidate_contract()}
            ))["recommendation_set"]
        self.assertEqual(result["status"], "partial")
        run = result["candidates"][0]["module_run_refs"][0]
        self.assertEqual(run["status"], "unsupported")
        self.assertFalse(run["run_id"])
        tool_bodies = [body for path, body in state.requests if path == "/api/tools/payoffer"]
        self.assertEqual(len(tool_bodies), 1)
        self.assertEqual(tool_bodies[0], {"contract": candidate_contract()["resolved_contract"]})
        self.assertTrue(any(event["stage"] == "tool.payoffer" for event in result["audit_trail"]))

    def test_candidate_requires_optionlist_optionlib_and_executable_optionreg(self) -> None:
        controlled = tuple(
            EvidenceRef.from_mapping(row, expected_catalog_version="catalog_20260807")
            for row in library_evidence()
        )
        for excluded_source in ("optionlist", "optionlib", "optionreg_status"):
            with self.subTest(excluded_source=excluded_source):
                rows = tuple(item for item in controlled if item.source != excluded_source)
                candidates, rejected = build_candidates(
                    research(), critic(), evidence=rows, run_id="recommend_gate"
                )
                self.assertEqual(candidates, ())
                self.assertIn(excluded_source.replace("_status", ""), rejected[0]["reason"].lower())

        unavailable_reg = library_evidence()
        unavailable_reg[-1]["entry_status"] = False
        controlled = tuple(
            EvidenceRef.from_mapping(row, expected_catalog_version="catalog_20260807")
            for row in unavailable_reg
        )
        candidates, rejected = build_candidates(research(), critic(), evidence=controlled, run_id="recommend_gate")
        self.assertEqual(candidates, ())
        self.assertIn("OptionReg", rejected[0]["reason"])

    def test_execution_requires_bound_candidate_contract(self) -> None:
        state = ContractState()
        state.evidence = library_evidence()
        for role, value in (("SingleAgent.Intent", intent()), ("SingleAgent.Research", research()), ("SingleAgent.Critic", critic())):
            state.steps[role].append(value)
        state.steps["SingleAgent.Executor"].append({
            "tool_requests": [{"candidate_id": "recommend_001_candidate_01", "module": "payoffer"}]
        })
        invalid = candidate_contract()
        invalid.pop("evidence_ref_ids")
        with contract_server(state) as url:
            result = service(url).recommend(request(
                requested_outputs=["payoff"], approved_candidate_ids=["recommend_001_candidate_01"],
                candidate_contracts={"recommend_001_candidate_01": invalid},
            ))["recommendation_set"]
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("evidence_ref_ids", result["limitations"][0])

    def test_execution_preserves_terminal_run_status_and_migrates_candidate(self) -> None:
        state = ContractState()
        state.evidence = library_evidence()
        state.tool_response = {
            "ok": False, "status": "timed_out", "run_id": "run_timeout", "message": "计算超时",
            "candidate_id": "recommend_001_candidate_01", "catalog_version": "catalog_20260807",
            "contract_fingerprint": "a" * 64,
        }
        for role, value in (("SingleAgent.Intent", intent()), ("SingleAgent.Research", research()), ("SingleAgent.Critic", critic())):
            state.steps[role].append(value)
        state.steps["SingleAgent.Executor"].append({
            "tool_requests": [{"candidate_id": "recommend_001_candidate_01", "module": "payoffer"}]
        })
        with contract_server(state) as url:
            result = service(url).recommend(request(
                requested_outputs=["payoff"], approved_candidate_ids=["recommend_001_candidate_01"],
                candidate_contracts={"recommend_001_candidate_01": candidate_contract()},
            ))["recommendation_set"]
        row = result["candidates"][0]
        self.assertEqual(row["module_run_refs"][0]["status"], "timed_out")
        self.assertEqual(row["candidate_status"], "pending_data")

    def test_unstructured_model_returns_audited_unavailable_set(self) -> None:
        state = ContractState()
        state.capability["structured_output"] = False
        with contract_server(state) as url:
            result = service(url).recommend(request())["recommendation_set"]
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("结构化输出", result["limitations"][0])
        self.assertTrue(any(event["stage"] == "workflow" for event in result["audit_trail"]))

    def test_missing_confirmed_constraints_are_combined_into_one_question(self) -> None:
        state = ContractState()
        with contract_server(state) as url:
            result = service(url).recommend(request(confirmed_constraints={"underlying": "000300.SH"}))["recommendation_set"]
        self.assertEqual(result["status"], "pending_question")
        self.assertEqual(result["missing_information"], ["horizon", "max_loss", "principal_fluctuation"])
        self.assertIn("期限", result["next_question"])
        self.assertIn("最大可承受亏损", result["next_question"])
        self.assertIn("是否接受本金波动", result["next_question"])
        self.assertIn("一次补充", result["next_question"])

    def test_missing_environment_ports_do_not_claim_success(self) -> None:
        response = call_tool({"action": "recommend", **request()})
        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], "failed")
        self.assertEqual(capability()["status"], "unconfigured")
        self.assertFalse(capability()["ok"])

    def test_unreachable_http_port_raises_port_error(self) -> None:
        port = HttpKnowledgePort(HttpEndpoint("http://127.0.0.1:1", timeout_seconds=0.05))
        with self.assertRaises(PortError):
            port.search({"catalog_version": "x", "query": "y"})

    def test_recommender_imports_without_reporter_package(self) -> None:
        environment = dict(os.environ)
        # 开发态使用 modules/<module>/__init__.py 作为 src 的薄包装层；
        # 因此应注册 modules 的父目录，而不是已扁平化的单个 src 目录。
        environment["PYTHONPATH"] = f"{PROJECT_ROOT}:{CORE_SRC}"
        completed = subprocess.run(
            [sys.executable, "-I", "-c", f"import sys;sys.path[:0]=[{str(PROJECT_ROOT)!r},{str(CORE_SRC)!r}];import modules.recommender.service;assert 'modules.reporter' not in sys.modules;print('ok')"],
            cwd=PROJECT_ROOT, env=environment, capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
