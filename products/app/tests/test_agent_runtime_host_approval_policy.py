"""Regression tests for deterministic Host-owned approval decisions.

The model may suggest content and tools, but it never grants itself a free
approval pause.  Recommendation continuation is decided from the user's
explicit scope and the validated candidate set.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / "products" / "app"
for source in (ROOT, APP_ROOT):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from backend.agent_runtime import recommender_adapter as adapter_module
from backend.agent_runtime.agent_loop import AgentLoop, _recommendation_outcome
from backend.agent_runtime.recommender_adapter import (
    RecommenderAdapter,
    _may_auto_continue_initial_scope,
    _may_resume_pending,
)
from backend.authorization.roles import Role
from backend.identity.session_identity import SessionIdentity
from backend.stores import _LocalDocumentStore
from backend.task_runtime.task_service import TaskService


class _Context:
    def build(self, _identity, _task_id, message):
        return {"latest_message": message, "messages": [], "tool_catalog": []}


class _Gateway:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    def decide_for(self, *_args):
        return self.decisions.pop(0)


class _NoTools:
    def call(self, *_args):
        raise AssertionError("read-only answer must not call a tool")


class _NoRecommender:
    def run_fixed(self, *_args):
        raise AssertionError("read-only answer must not start recommendation")


class HostApprovalPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = SessionIdentity("principal-policy", "tenant-policy", Role.ADMIN, "session-policy")

    def test_model_cannot_force_an_approval_pause_for_read_only_research(self) -> None:
        loop = AgentLoop(
            gateway=_Gateway([
                {"action": "request_approval", "message": "请先批准我查询本地资料。"},
                {"action": "final", "text": "本地资料查询属于只读研究，无需额外确认。"},
            ]),
            context_builder=_Context(),
            tool_executor=_NoTools(),
            recommender=_NoRecommender(),
        )

        result = loop.run(self.identity, "task-readonly", "查看本地产品说明")

        self.assertEqual(result["status"], "completed", result)
        self.assertNotIn("approval", result)
        self.assertNotEqual(result.get("action"), "request_approval")

    def test_confirmation_exposes_candidate_summary_before_choice(self) -> None:
        result = _recommendation_outcome({
            "recommendation_set": {
                "status": "pending_approval",
                "primary_candidate_id": "internal-primary",
                "candidates": [
                    {
                        "candidate_id": "internal-primary",
                        "product_id": "2.1",
                        "product_name": "看涨期权",
                        "library_status": "ready",
                        "reason": "上涨观点与有限损失目标相符。",
                        "main_risks": ["权利金可能全部损失。"],
                        "not_suitable_for": ["不能承受权利金损失。"],
                    },
                    {
                        "candidate_id": "internal-alternative",
                        "product_id": "2.3",
                        "product_name": "牛市价差",
                        "library_status": "ready",
                        "reason": "降低成本，但收益上限受限。",
                        "main_risks": ["大幅上涨时收益封顶。"],
                        "not_suitable_for": ["希望完整保留上涨收益。"],
                    },
                ],
            },
        }, [], 1)

        self.assertEqual(result["status"], "pending_approval", result)
        for text in (
            "主候选：看涨期权", "推荐理由：上涨观点与有限损失目标相符。",
            "主要风险：权利金可能全部损失。", "不适用：不能承受权利金损失。",
            "备选：牛市价差",
        ):
            self.assertIn(text, result["text"])
        for internal in ("candidate_id", "internal-primary", "Tool", "JSON", "/Users/"):
            self.assertNotIn(internal, result["text"])

    def test_initial_recommend_and_report_scope_auto_continues_unique_ready_candidate(self) -> None:
        class Registry:
            capability_root = ROOT / "products" / "app" / "capability" / "option-helper"
            manifest = {"catalog_version": "v1.0"}

        class Service:
            def __init__(self, **_ports):
                pass

            def recommend_fixed(self, _case, *, workflow):
                return {
                    "route": {"fixed_recommendation_workflow": True, "route": workflow},
                    "recommendation_set": {
                        "status": "pending_approval",
                        "primary_candidate_id": "private-candidate",
                        "candidates": [{
                            "candidate_id": "private-candidate",
                            "product_id": "2.1",
                            "product_name": "看涨期权",
                            "underlyings": ["000300.SH"],
                            "library_status": "ready",
                            "missing_inputs": [],
                            "reason": "匹配上涨且波动变大的观点。",
                            "main_risks": ["权利金可能全部损失。"],
                            "not_suitable_for": ["不能承担本金波动。"],
                        }],
                    },
                }

        class Deliveries:
            def __init__(self):
                self.calls = []

            def run_recommendation_delivery(self, _identity, _task_id, **kwargs):
                self.calls.append(dict(kwargs))
                return {"status": "completed", "kind": "report", "format": "html"}

        prompt = (
            "我认为000300.SH未来3个月上涨且波动率上升，最大可承受亏损30%，"
            "接受本金波动。请推荐一个期权结构并生成HTML完整报告。"
        )
        with tempfile.TemporaryDirectory() as temporary:
            tasks = TaskService(_LocalDocumentStore(Path(temporary)))
            task = tasks.create(self.identity, "自动报告")
            tasks.append_message(self.identity, task["task_id"], "user", prompt, status="pending_model")
            deliveries = Deliveries()
            with patch.object(adapter_module, "RecommenderService", Service):
                result = RecommenderAdapter(
                    gateway=object(), registry=Registry(), task_service=tasks, tool_executor=deliveries,
                ).run_fixed(self.identity, task["task_id"], prompt, {})

        self.assertEqual(result["delivery"]["status"], "completed", result)
        self.assertEqual(len(deliveries.calls), 1)
        self.assertEqual(deliveries.calls[0]["candidate"]["product_id"], "2.1")

    def test_delivery_preference_alone_never_approves_a_candidate(self) -> None:
        pending = {
            "catalog_version": "v1.0",
            "confirmed_constraints": {
                "underlying": "000300.SH", "horizon": "3个月", "market_view": "上涨",
                "max_loss": "30%", "principal_fluctuation": True,
            },
        }
        current = {**pending["confirmed_constraints"], "output_type": "report", "format": "html"}

        self.assertFalse(_may_resume_pending(pending, "生成详细HTML报告。", current, "v1.0"))

    def test_multiple_or_incomplete_candidates_never_auto_continue(self) -> None:
        constraints = {"output_type": "report", "format": "html"}
        ready = {
            "candidate_id": "primary", "library_status": "ready", "missing_inputs": [],
        }
        multiple = {
            "status": "pending_approval", "primary_candidate_id": "primary",
            "candidates": [ready, {"candidate_id": "alternative", "library_status": "ready", "missing_inputs": []}],
        }
        incomplete = {
            "status": "pending_approval", "primary_candidate_id": "primary",
            "candidates": [{**ready, "missing_inputs": ["期限"]}],
        }

        self.assertFalse(_may_auto_continue_initial_scope("请推荐并生成报告", multiple, constraints))
        self.assertFalse(_may_auto_continue_initial_scope("请推荐并生成报告", incomplete, constraints))


if __name__ == "__main__":
    unittest.main()
