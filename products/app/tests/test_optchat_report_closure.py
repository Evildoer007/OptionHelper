"""Focused regressions for natural-language Card and Report orchestration."""

from __future__ import annotations

import unittest
from typing import Any, Mapping

from products.app.backend.authorization.roles import Role
from products.app.backend.identity.session_identity import SessionIdentity


def _admin() -> SessionIdentity:
    return SessionIdentity("User1", "local", Role.ADMIN, "session-1")


class _Registry:
    manifest = {"catalog_version": "v1.0"}

    def service_context(self, _identity: SessionIdentity, module: str, **kwargs: object) -> dict[str, Any]:
        return {"module": module, "task_id": kwargs["task_id"]}


class _Dispatcher:
    def __init__(self, result: Mapping[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result = dict(result or {"ok": True, "status": "completed", "report_run_ref": {"run_id": "report-1"}})

    def dispatch_for_conversation(
        self,
        module: str,
        payload: dict[str, Any],
        _identity: SessionIdentity,
        **_kwargs: object,
    ) -> dict[str, Any]:
        self.calls.append((module, payload))
        return dict(self.result)


class _Results:
    def __init__(self, sources: list[dict[str, Any]]) -> None:
        self.sources = sources

    def list_owned_report_sources(
        self,
        _identity: SessionIdentity,
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        return {"tenant_id": "local", "sources": self.sources if task_id == "task-1" else []}


def _source() -> dict[str, Any]:
    return {
        "source_id": "source-owned",
        "task_id": "task-1",
        "candidates": [{
            "candidate_id": "candidate-current",
            "product_name": "看涨期权",
            "module_run_refs": {
                "payoff": {"run_id": "payoff-1"},
                "pricing": {"run_id": "pricing-1"},
            },
        }],
    }


class _Context:
    def __init__(self, tools: list[str]) -> None:
        self.tools = tools

    def build(self, _identity: SessionIdentity, task_id: str, message: str) -> dict[str, Any]:
        return {
            "task": {"task_id": task_id},
            "latest_message": message,
            "tool_catalog": [{"name": name} for name in self.tools],
            "facts": {},
        }


class _Gateway:
    def __init__(self, decisions: list[Mapping[str, Any]]) -> None:
        self.decisions = list(decisions)
        self.calls = 0

    def decide_for(self, _identity: SessionIdentity, _task_id: str, _context: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls += 1
        return self.decisions.pop(0)


class _Tools:
    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = result
        self.calls = 0

    def call(self, _identity: SessionIdentity, _task_id: str, _name: str, _arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls += 1
        return self.result


class _Recommender:
    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = result
        self.calls = 0

    def run_fixed(self, _identity: SessionIdentity, _task_id: str, _prompt: str, _arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls += 1
        return self.result


class OptChatReportClosureTests(unittest.TestCase):
    def test_reporter_arguments_are_completed_from_owned_formal_results(self) -> None:
        from products.app.backend.agent_runtime.recommender_adapter import AppConversationToolExecutor

        dispatcher = _Dispatcher()
        executor = AppConversationToolExecutor(dispatcher, _Registry(), _Results([_source()]))  # type: ignore[arg-type]
        result = executor.call(_admin(), "task-1", "reporter.run", {"kind": "report", "format": "html"})

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(dispatcher.calls), 1)
        module, payload = dispatcher.calls[0]
        self.assertEqual(module, "reporter")
        selection = payload["selection"]
        self.assertEqual(selection["source_id"], "source-owned")
        self.assertEqual(selection["candidate_ids"], ["candidate-current"])
        self.assertEqual(selection["selected_modules"], ["payoff", "pricing"])
        self.assertEqual(selection["output_type"], "report")
        self.assertEqual(selection["html_report_layout"], "continuous")

    def test_reporter_without_formal_results_returns_actionable_next_step(self) -> None:
        from products.app.backend.agent_runtime.recommender_adapter import AppConversationToolExecutor

        dispatcher = _Dispatcher()
        executor = AppConversationToolExecutor(dispatcher, _Registry(), _Results([]))  # type: ignore[arg-type]
        result = executor.call(_admin(), "task-1", "reporter.run", {"kind": "report"})

        self.assertEqual(result["status"], "needs_input")
        self.assertIn("正式分析结果", result["message"])
        self.assertEqual(dispatcher.calls, [])

    def test_reporter_uses_latest_task_source_instead_of_stale_contract(self) -> None:
        from products.app.backend.agent_runtime.recommender_adapter import AppConversationToolExecutor

        old = _source()
        old["source_id"] = "source-old"
        old["candidates"][0]["candidate_id"] = "2.1"
        current = _source()
        current["source_id"] = "source-current"
        current["candidates"][0]["candidate_id"] = "candidate-new"
        dispatcher = _Dispatcher()
        executor = AppConversationToolExecutor(dispatcher, _Registry(), _Results([old, current]))  # type: ignore[arg-type]

        executor.call(_admin(), "task-1", "reporter.run", {"kind": "report"})

        selection = dispatcher.calls[0][1]["selection"]
        self.assertEqual(selection["source_id"], "source-current")
        self.assertEqual(selection["candidate_ids"], ["candidate-new"])

    def test_empty_recommendation_stops_before_stale_product_or_report_call(self) -> None:
        from products.app.backend.agent_runtime.agent_loop import AgentLoop

        gateway = _Gateway([
            {"action": "call_tool", "tool": "recommender.run", "arguments": {"workflow": "professional_report"}},
            {"action": "call_tool", "tool": "reporter.run", "arguments": {"kind": "report", "product_id": "2.1"}},
        ])
        tools = _Tools({"ok": True, "status": "completed"})
        recommender = _Recommender({
            "route": {"fixed_recommendation_workflow": True},
            "recommendation_set": {
                "status": "unavailable",
                "primary_candidate_id": None,
                "candidates": [],
            },
        })
        result = AgentLoop(
            gateway=gateway,
            context_builder=_Context(["recommender.run", "reporter.run"]),
            tool_executor=tools,
            recommender=recommender,
        ).run(_admin(), "task-1", "推荐一个结构并生成全面报告")

        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(gateway.calls, 1)
        self.assertEqual(tools.calls, 0)
        self.assertNotIn("2.1", str(result))
        self.assertNotIn("reporter.run", result["text"].lower())

    def test_successful_detailed_report_returns_delivery_message_without_second_model_round(self) -> None:
        from products.app.backend.agent_runtime.agent_loop import AgentLoop

        gateway = _Gateway([
            {"action": "call_tool", "tool": "reporter.run", "arguments": {"kind": "report"}},
        ])
        tools = _Tools({"ok": True, "status": "completed", "report_run_ref": {"run_id": "report-1"}})
        result = AgentLoop(
            gateway=gateway,
            context_builder=_Context(["reporter.run"]),
            tool_executor=tools,
            recommender=_Recommender({}),
        ).run(_admin(), "task-1", "生成报告")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(gateway.calls, 1)
        self.assertEqual(tools.calls, 1)
        self.assertEqual(result["text"], "完整研究报告已生成，可在当前任务中预览或导出。")

    def test_successful_brief_is_named_for_users_without_internal_type(self) -> None:
        from products.app.backend.agent_runtime.agent_loop import AgentLoop

        gateway = _Gateway([{
            "action": "call_tool", "tool": "reporter.run", "arguments": {"kind": "card"},
        }])
        result = AgentLoop(
            gateway=gateway,
            context_builder=_Context(["reporter.run"]),
            tool_executor=_Tools({"ok": True, "status": "completed", "report_run_ref": {"run_id": "card-1"}}),
            recommender=_Recommender({}),
        ).run(_admin(), "task-1", "生成Card")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["text"], "研究简报已生成，可在当前任务中预览或导出。")

    def test_internal_runtime_terms_are_not_returned_to_user(self) -> None:
        from products.app.backend.agent_runtime.agent_loop import AgentLoop

        result = AgentLoop(
            gateway=_Gateway([{
                "action": "final",
                "text": "reporter.run被拒绝，请提供RunRef和source_id。",
                "fact_refs": [],
            }]),
            context_builder=_Context([]),
            tool_executor=_Tools({}),
            recommender=_Recommender({}),
        ).run(_admin(), "task-1", "为什么没生成")

        self.assertEqual(result["status"], "completed")
        lowered = result["text"].lower()
        self.assertNotIn("reporter.run", lowered)
        self.assertNotIn("runref", lowered)
        self.assertNotIn("source_id", lowered)

    def test_internal_file_or_contract_requests_are_replaced_by_business_questions(self) -> None:
        from products.app.backend.agent_runtime.agent_loop import AgentLoop

        result = AgentLoop(
            gateway=_Gateway([{
                "action": "final",
                "text": "请提供contract_ref、task_id和内部文件路径。",
                "fact_refs": [],
            }]),
            context_builder=_Context([]),
            tool_executor=_Tools({}),
            recommender=_Recommender({}),
        ).run(_admin(), "task-1", "生成全面报告")

        self.assertEqual(result["status"], "completed")
        self.assertNotIn("contract_ref", result["text"].lower())
        self.assertNotIn("task_id", result["text"].lower())
        self.assertIn("标的", result["text"])

    def test_additional_runtime_vocabulary_is_not_returned(self) -> None:
        from products.app.backend.agent_runtime.agent_loop import AgentLoop

        for term in (
            "ResolvedContract", "CallerContext", "HostContext", "ToolGateway",
            "catalog_version", "contract_fingerprint", "analysis_case_id", "principal_id", "request_id",
        ):
            with self.subTest(term=term):
                result = AgentLoop(
                    gateway=_Gateway([{"action": "final", "text": f"请提供{term}。", "fact_refs": []}]),
                    context_builder=_Context([]),
                    tool_executor=_Tools({}),
                    recommender=_Recommender({}),
                ).run(_admin(), "task-1", "继续生成报告")
                self.assertNotIn(term.lower(), result["text"].lower())


if __name__ == "__main__":
    unittest.main()
