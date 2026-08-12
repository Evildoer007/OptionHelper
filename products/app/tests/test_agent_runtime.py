"""Contract tests for the bounded OptChat Agent runtime.

These tests deliberately use deterministic fakes.  They prove the App owns
the conversation loop and authorization boundary without asking a real model
or a financial module to produce a fabricated result.
"""

from __future__ import annotations

import unittest
import tempfile
from typing import Any, Mapping
from pathlib import Path
from unittest.mock import patch

from products.app.backend.authorization.policy import AuthorizationPolicy
from products.app.backend.authorization.roles import Role
from products.app.backend.errors import AuthorizationError, UnavailableCapabilityError
from products.app.backend.identity.session_identity import SessionIdentity


class _ContextBuilder:
    def __init__(self, tools: list[dict[str, Any]] | None = None) -> None:
        self.tools = tools or [
            {"name": "knowledger.search", "description": "read-only product lookup"},
            {"name": "pricer.run", "description": "formal valuation"},
            {"name": "recommender.run", "description": "fixed recommendation workflow"},
        ]
        self.calls: list[tuple[str, str]] = []

    def build(self, identity: SessionIdentity, task_id: str, latest_message: str) -> dict[str, Any]:
        self.calls.append((task_id, latest_message))
        return {
            "task": {"task_id": task_id, "subject": "test"},
            "identity": identity.public(),
            "messages": [{"role": "user", "content": latest_message}],
            "tool_catalog": list(self.tools),
            "facts": {"resolved_contract": {"contract_ref": "contract-safe"}},
        }


class _ScriptedGateway:
    def __init__(self, decisions: list[Mapping[str, Any] | Exception]) -> None:
        self.decisions = list(decisions)
        self.contexts: list[dict[str, Any]] = []

    def decide_for(self, identity: SessionIdentity, task_id: str, context: Mapping[str, Any]) -> Mapping[str, Any]:
        self.contexts.append(dict(context))
        value = self.decisions.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class _ToolExecutor:
    def __init__(self, results: list[Mapping[str, Any] | Exception] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, identity: SessionIdentity, task_id: str, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append((name, dict(arguments)))
        value = self.results.pop(0) if self.results else {"ok": True, "module": name.split(".")[0], "status": "succeeded"}
        if isinstance(value, Exception):
            raise value
        return value


class _Recommender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def run_fixed(self, identity: SessionIdentity, task_id: str, prompt: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append((task_id, prompt))
        return {"route": {"route": "recommendation"}, "recommendation_set": {"status": "pending_approval", "candidates": []}}


def _identity(role: Role = Role.SALES) -> SessionIdentity:
    return SessionIdentity("principal-a", "tenant-a", role, "session-a")


class AgentRuntimeTest(unittest.TestCase):
    def _loop(
        self,
        decisions: list[Mapping[str, Any] | Exception],
        *,
        tool_results: list[Mapping[str, Any] | Exception] | None = None,
        max_rounds: int = 4,
        cancelled: bool = False,
    ) -> tuple[Any, _ScriptedGateway, _ToolExecutor, _Recommender]:
        from products.app.backend.agent_runtime.agent_loop import AgentLoop

        gateway = _ScriptedGateway(decisions)
        executor = _ToolExecutor(tool_results)
        recommender = _Recommender()
        loop = AgentLoop(
            gateway=gateway,
            context_builder=_ContextBuilder(),
            tool_executor=executor,
            recommender=recommender,
            max_rounds=max_rounds,
            timeout_seconds=30,
            is_cancelled=lambda _identity, _task_id: cancelled,
        )
        return loop, gateway, executor, recommender

    def test_knowledge_answer_does_not_invoke_calculation(self) -> None:
        loop, gateway, executor, _ = self._loop([
            {"action": "final", "text": "该结构的条款说明如下。"},
        ])
        result = loop.run(_identity(), "task-1", "解释雪球条款")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(executor.calls, [])
        self.assertEqual(result["text"], "该结构的条款说明如下。")
        self.assertIn("tool_catalog", gateway.contexts[0])

    def test_missing_pricer_inputs_becomes_one_question(self) -> None:
        loop, _, executor, _ = self._loop([
            {"action": "ask_user", "question": "请提供估值日和市场数据引用。"},
        ])
        result = loop.run(_identity(), "task-1", "给我定价")
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(result["action"], "ask_user")
        self.assertEqual(executor.calls, [])

    def test_empty_recommendation_never_offers_an_unresumable_approval(self) -> None:
        loop, _, executor, recommender = self._loop([
            {"action": "call_tool", "tool": "recommender.run", "arguments": {}},
            {"action": "request_approval", "message": "请确认是否执行候选结构。"},
        ])
        result = loop.run(_identity(), "task-1", "根据看涨观点推荐结构")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(recommender.calls), 1)
        self.assertEqual(executor.calls, [])
        self.assertEqual(result["observations"][0]["tool"], "recommender.run")

    def test_tool_failure_is_observed_not_fabricated(self) -> None:
        loop, _, executor, _ = self._loop([
            {"action": "call_tool", "tool": "pricer.run", "arguments": {"product_id": "2.1"}},
            {"action": "final", "text": "定价未完成，需要补充输入后重试。"},
        ], tool_results=[UnavailableCapabilityError("pricer", "market data missing")])
        result = loop.run(_identity(), "task-1", "定价")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(result["observations"][0]["status"], "failed")
        self.assertNotIn("PV", result["text"])

    def test_duplicate_tool_call_stops_before_second_execution(self) -> None:
        loop, _, executor, _ = self._loop([
            {"action": "call_tool", "tool": "pricer.run", "arguments": {"product_id": "2.1"}},
            {"action": "call_tool", "tool": "pricer.run", "arguments": {"product_id": "2.1"}},
        ])
        result = loop.run(_identity(Role.ADMIN), "task-1", "定价")
        self.assertEqual(result["status"], "stopped_duplicate")
        self.assertEqual(len(executor.calls), 1)

    def test_round_limit_cancel_and_permission_stop(self) -> None:
        loop, _, executor, _ = self._loop([
            {"action": "call_tool", "tool": "pricer.run", "arguments": {"product_id": "2.1"}},
            {"action": "call_tool", "tool": "knowledger.search", "arguments": {"query": "call"}},
        ], max_rounds=1)
        self.assertEqual(loop.run(_identity(Role.ADMIN), "task-1", "定价")["status"], "max_rounds")
        self.assertEqual(len(executor.calls), 1)

        cancelled, _, cancelled_executor, _ = self._loop([], cancelled=True)
        self.assertEqual(cancelled.run(_identity(), "task-1", "停止")["status"], "cancelled")
        self.assertEqual(cancelled_executor.calls, [])

        denied, _, denied_executor, _ = self._loop([
            {"action": "call_tool", "tool": "admin.maintenance", "arguments": {}},
        ])
        self.assertEqual(denied.run(_identity(), "task-1", "维护资料")["status"], "blocked")
        self.assertEqual(denied_executor.calls, [])

    def test_sales_proxy_permission_is_separate_from_module_run(self) -> None:
        policy = AuthorizationPolicy()
        self.assertTrue(policy.allows(Role.SALES, "conversation.tool.run"))
        self.assertFalse(policy.allows(Role.SALES, "module.run"))

    def test_decision_must_not_carry_reasoning_or_secret(self) -> None:
        loop, _, executor, _ = self._loop([
            {"action": "final", "text": "ok", "reasoning": "hidden chain"},
        ])
        result = loop.run(_identity(), "task-1", "hello")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(executor.calls, [])

    def test_model_cannot_publish_untraceable_pricing_number(self) -> None:
        loop, _, executor, _ = self._loop([
            {"action": "final", "text": "PV为12.4，建议立即执行。"},
        ])
        result = loop.run(_identity(), "task-1", "给我定价")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(executor.calls, [])
        self.assertNotIn("12.4", result["text"])

    def test_context_exposes_only_role_and_redacts_secret_like_text(self) -> None:
        from products.app.backend.agent_runtime.context_builder import ContextBuilder
        from products.app.backend.stores import _LocalDocumentStore
        from products.app.backend.stores.contract_store import ContractStore
        from products.app.backend.task_runtime.task_service import TaskService

        with tempfile.TemporaryDirectory() as temporary:
            tasks = TaskService(_LocalDocumentStore(Path(temporary)))
            identity = _identity()
            task = tasks.create(identity, "上下文脱敏")
            tasks.append_message(identity, task["task_id"], "user", "不要暴露sk-abcdefghijklmnopqrstuv")
            context = ContextBuilder(tasks, ContractStore(tasks._state), AuthorizationPolicy(), catalog_version="v1.0").build(  # type: ignore[attr-defined]
                identity, task["task_id"], "价格查询",
            )
        self.assertEqual(context["caller"], {"role": "sales", "audience": "option-helper-app"})
        serialized = str(context)
        self.assertNotIn("principal-a", serialized)
        self.assertNotIn("tenant-a", serialized)
        self.assertIn("[REDACTED]", serialized)
        self.assertNotIn("request_approval", context["decision_protocol"]["actions"])

    def test_conversation_service_uses_agent_loop_and_persists_safe_answer(self) -> None:
        from products.app.backend.agent_runtime.conversation_service import ConversationService
        from products.app.backend.stores import _LocalDocumentStore
        from products.app.backend.task_runtime.task_service import TaskService

        class Loop:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def run(self, _identity: SessionIdentity, _task_id: str, message: str) -> dict[str, Any]:
                self.calls.append(message)
                return {"status": "needs_input", "text": "请补充估值日。", "rounds": 1, "observations": []}

        with tempfile.TemporaryDirectory() as temporary:
            tasks = TaskService(_LocalDocumentStore(Path(temporary)))
            task = tasks.create(_identity(), "代理闭环")
            loop = Loop()
            response = ConversationService(tasks, gateway=object(), agent_loop=loop).respond(_identity(), task["task_id"], "定价")  # type: ignore[arg-type]
            stored = tasks.get(_identity(), task["task_id"])
        self.assertEqual(response["status"], "needs_input")
        self.assertEqual(loop.calls, ["定价"])
        self.assertEqual(stored["messages"][-1]["content"], "请补充估值日。")

    def test_cancelled_task_never_reenters_model_loop(self) -> None:
        from products.app.backend.agent_runtime.conversation_service import ConversationService
        from products.app.backend.stores import _LocalDocumentStore
        from products.app.backend.task_runtime.task_service import TaskService

        with tempfile.TemporaryDirectory() as temporary:
            tasks = TaskService(_LocalDocumentStore(Path(temporary)))
            task = tasks.create(_identity(), "取消任务")
            tasks.cancel(_identity(), task["task_id"])
            response = ConversationService(tasks, gateway=object()).respond(_identity(), task["task_id"], "继续")  # type: ignore[arg-type]
        self.assertEqual(response["status"], "cancelled")

    def test_recommender_adapter_uses_fixed_entry_not_generic_recommend(self) -> None:
        from products.app.backend.agent_runtime import recommender_adapter as adapter_module
        from products.app.backend.stores import _LocalDocumentStore
        from products.app.backend.task_runtime.task_service import TaskService

        class Registry:
            capability_root = Path("/does-not-matter")
            manifest = {"catalog_version": "v1.0"}

        class FixedOnlyService:
            def __init__(self, **_ports: object) -> None:
                pass

            def recommend(self, _case: object) -> Mapping[str, Any]:
                raise AssertionError("generic Recommender route must never run in App")

            def recommend_fixed(self, _case: object, *, workflow: str) -> Mapping[str, Any]:
                return {"route": {"fixed_recommendation_workflow": True, "route": workflow}, "recommendation_set": {"status": "pending_approval"}}

        with tempfile.TemporaryDirectory() as temporary:
            tasks = TaskService(_LocalDocumentStore(Path(temporary)))
            task = tasks.create(_identity(), "固定推荐")
            with patch.object(adapter_module, "RecommenderService", FixedOnlyService):
                adapter = adapter_module.RecommenderAdapter(
                    gateway=object(), registry=Registry(), task_service=tasks, tool_executor=object(),  # type: ignore[arg-type]
                )
                result = adapter.run_fixed(_identity(), task["task_id"], "任意自然语言", {"workflow": "recommendation"})
        self.assertEqual(result["recommendation_set"]["status"], "pending_approval")

    def test_optchat_executor_uses_service_context_not_desk_page_context(self) -> None:
        from products.app.backend.agent_runtime.recommender_adapter import AppConversationToolExecutor

        class Registry:
            manifest = {"catalog_version": "v1.0"}

            def __init__(self) -> None:
                self.service_calls = 0

            def service_context(self, _identity: SessionIdentity, module: str, **kwargs: object) -> dict[str, Any]:
                self.service_calls += 1
                return {"module": module, "task_id": kwargs["task_id"], "request_policy": ["conversation.tool.run"]}

            def host_context(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
                raise AssertionError("OptChat must not request a Desk page context")

        class Dispatcher:
            def __init__(self) -> None:
                self.proxy = False

            def dispatch_for_conversation(self, _module: str, _payload: dict[str, Any], _identity: SessionIdentity, **kwargs: object) -> dict[str, Any]:
                self.proxy = True
                self.context = kwargs["module_context"]
                return {"ok": True, "status": "succeeded"}

        registry = Registry()
        dispatcher = Dispatcher()
        result = AppConversationToolExecutor(dispatcher, registry).call(_identity(), "task-1", "pricer.run", {"product_id": "2.1"})  # type: ignore[arg-type]
        self.assertTrue(result["ok"])
        self.assertEqual(registry.service_calls, 1)
        self.assertTrue(dispatcher.proxy)
        self.assertEqual(dispatcher.context["request_policy"], ["conversation.tool.run"])


if __name__ == "__main__":
    unittest.main()
