"""Regression tests for the OptChat runtime hardening boundaries.

They deliberately exercise only App-owned adapters and deterministic fakes;
no provider, Capability source tree, or financial calculation is invoked.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import tempfile
import unittest
from typing import Any, Mapping
from unittest.mock import patch

from products.app.backend.authorization.policy import AuthorizationPolicy
from products.app.backend.authorization.roles import Role
from products.app.backend.errors import AuthorizationError, ValidationError
from products.app.backend.identity.session_identity import SessionIdentity


def _identity() -> SessionIdentity:
    return SessionIdentity("principal-a", "tenant-a", Role.SALES, "session-a")


class _Context:
    def build(self, _identity: SessionIdentity, _task_id: str, _message: str) -> dict[str, Any]:
        return {"tool_catalog": [{"name": "pricer.run"}]}


class _Gateway:
    def __init__(self, decisions: list[Mapping[str, Any]]) -> None:
        self._decisions = list(decisions)

    def decide_for(self, _identity: SessionIdentity, _task_id: str, _context: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._decisions.pop(0)


class _Tools:
    def __init__(self, result: Mapping[str, Any] | None = None) -> None:
        self.calls = 0
        self._result = result or {"ok": True, "status": "succeeded"}

    def call(self, _identity: SessionIdentity, _task_id: str, _name: str, _arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls += 1
        return self._result


class _Recommender:
    def run_fixed(self, _identity: SessionIdentity, _task_id: str, _prompt: str, _arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        raise AssertionError("this regression does not call Recommender")


class AgentRuntimeHardeningTests(unittest.TestCase):
    def test_compute_idempotency_replays_persisted_run_ref_after_restart(self) -> None:
        from products.app.backend.agent_runtime.tool_dispatcher import ToolDispatcher
        from products.app.backend.stores import _LocalDocumentStore
        from products.app.backend.stores.data_store import DataStore
        from products.app.backend.stores.result_store import ResultStore
        from products.app.backend.task_runtime.job_runner import JobRunner
        from products.app.backend.task_runtime.task_service import TaskService

        class Gateway:
            def __init__(self, fail: bool = False) -> None:
                self.calls = 0
                self.fail = fail

            def dispatch(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
                self.calls += 1
                if self.fail:
                    raise AssertionError("persistent idempotency did not replay")
                return {"run_id": "pricing-idem", "ok": True, "status": "completed", "pricing": {"pv": 12.5}}

        with tempfile.TemporaryDirectory() as temporary:
            state = _LocalDocumentStore(Path(temporary))
            tasks = TaskService(state)
            results = ResultStore(state)
            task = tasks.create(_identity(), "持久计算幂等")
            first_gateway = Gateway()
            first = ToolDispatcher(first_gateway, JobRunner(), tasks, results, DataStore(state)).dispatch(
                "pricer",
                {"action": "run", "task_id": task["task_id"], "idempotency_key": "idem-1", "pricing_config": {"method": "bs"}},
                _identity(),
                module_context=object(),
                request_id="request-1",
            )
            restarted_state = _LocalDocumentStore(Path(temporary))
            restarted_tasks = TaskService(restarted_state)
            restarted_results = ResultStore(restarted_state)
            restarted_gateway = Gateway(fail=True)
            replay = ToolDispatcher(restarted_gateway, JobRunner(), restarted_tasks, restarted_results, DataStore(restarted_state)).dispatch(
                "pricer",
                {"action": "run", "task_id": task["task_id"], "idempotency_key": "idem-1", "pricing_config": {"method": "bs"}},
                _identity(),
                module_context=object(),
                request_id="request-2",
            )
            with self.assertRaises(ValidationError):
                ToolDispatcher(restarted_gateway, JobRunner(), restarted_tasks, restarted_results, DataStore(restarted_state)).dispatch(
                    "pricer",
                    {"action": "run", "task_id": task["task_id"], "idempotency_key": "idem-1", "pricing_config": {"method": "mc"}},
                    _identity(),
                    module_context=object(),
                    request_id="request-3",
                )

        self.assertEqual(first_gateway.calls, 1)
        self.assertEqual(restarted_gateway.calls, 0)
        self.assertEqual(replay["module_run_ref"], first["module_run_ref"])
        self.assertTrue(replay["idempotent_replay"])

    def test_two_dispatchers_atomically_claim_one_compute_run(self) -> None:
        from products.app.backend.agent_runtime.tool_dispatcher import ToolDispatcher
        from products.app.backend.stores import _LocalDocumentStore
        from products.app.backend.stores.data_store import DataStore
        from products.app.backend.stores.result_store import ResultStore
        from products.app.backend.task_runtime.job_runner import JobRunner
        from products.app.backend.task_runtime.task_service import TaskService

        class BlockingGateway:
            def __init__(self) -> None:
                self.calls = 0
                self.started = Event()
                self.release = Event()

            def dispatch(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
                self.calls += 1
                self.started.set()
                self.release.wait(timeout=2)
                return {"ok": True, "status": "completed", "pricing": {"pv": 12.5}}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_state = _LocalDocumentStore(root)
            first_tasks = TaskService(first_state)
            task = first_tasks.create(_identity(), "跨实例计算幂等")
            gateway = BlockingGateway()
            first = ToolDispatcher(gateway, JobRunner(), first_tasks, ResultStore(first_state), DataStore(first_state))
            second_state = _LocalDocumentStore(root)
            second = ToolDispatcher(
                gateway, JobRunner(), TaskService(second_state), ResultStore(second_state), DataStore(second_state),
            )
            request = {
                "action": "run", "task_id": task["task_id"], "idempotency_key": "idem-concurrent",
                "pricing_config": {"method": "bs"},
            }
            with ThreadPoolExecutor(max_workers=2) as pool:
                owner = pool.submit(
                    first.dispatch, "pricer", dict(request), _identity(),
                    module_context=object(), request_id="request-owner",
                )
                self.assertTrue(gateway.started.wait(timeout=1))
                duplicate = pool.submit(
                    second.dispatch, "pricer", dict(request), _identity(),
                    module_context=object(), request_id="request-duplicate",
                )
                with self.assertRaisesRegex(ValidationError, "正在执行"):
                    duplicate.result(timeout=2)
                gateway.release.set()
                first_result = owner.result(timeout=2)
            replay = second.dispatch(
                "pricer", dict(request), _identity(), module_context=object(), request_id="request-replay",
            )

        self.assertEqual(gateway.calls, 1)
        self.assertEqual(replay["module_run_ref"], first_result["module_run_ref"])
        self.assertTrue(replay["idempotent_replay"])

    def test_failed_compute_releases_its_persistent_claim(self) -> None:
        from products.app.backend.agent_runtime.tool_dispatcher import ToolDispatcher
        from products.app.backend.stores import _LocalDocumentStore
        from products.app.backend.stores.data_store import DataStore
        from products.app.backend.stores.result_store import ResultStore
        from products.app.backend.task_runtime.job_runner import JobRunner
        from products.app.backend.task_runtime.task_service import TaskService

        class Gateway:
            def __init__(self) -> None:
                self.calls = 0

            def dispatch(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("transient module failure")
                return {"run_id": "retry-run", "ok": True, "status": "completed", "pricing": {"pv": 1.0}}

        with tempfile.TemporaryDirectory() as temporary:
            state = _LocalDocumentStore(Path(temporary))
            tasks = TaskService(state)
            results = ResultStore(state)
            task = tasks.create(_identity(), "失败后明确重试")
            gateway = Gateway()
            dispatcher = ToolDispatcher(gateway, JobRunner(), tasks, results, DataStore(state))
            request = {
                "action": "run", "task_id": task["task_id"], "idempotency_key": "idem-retry",
                "pricing_config": {"method": "bs"},
            }
            with self.assertRaisesRegex(RuntimeError, "transient"):
                dispatcher.dispatch(
                    "pricer", dict(request), _identity(), module_context=object(), request_id="request-first",
                )
            result = dispatcher.dispatch(
                "pricer", dict(request), _identity(), module_context=object(), request_id="request-retry",
            )

        self.assertEqual(gateway.calls, 2)
        self.assertEqual(result["module_run_ref"]["run_id"], "retry-run")

    def test_same_request_id_replays_one_persisted_response_under_concurrency(self) -> None:
        from products.app.backend.agent_runtime.conversation_service import ConversationService
        from products.app.backend.stores import _LocalDocumentStore
        from products.app.backend.task_runtime.task_service import TaskService

        class BlockingLoop:
            def __init__(self) -> None:
                self.calls = 0
                self.started = Event()
                self.release = Event()

            def run(self, _identity: SessionIdentity, _task_id: str, _message: str) -> dict[str, Any]:
                self.calls += 1
                self.started.set()
                self.release.wait(timeout=2)
                return {"status": "completed", "text": "已完成。", "rounds": 1, "observations": []}

        with tempfile.TemporaryDirectory() as temporary:
            tasks = TaskService(_LocalDocumentStore(Path(temporary)))
            task = tasks.create(_identity(), "同任务幂等")
            loop = BlockingLoop()
            service = ConversationService(tasks, gateway=object(), agent_loop=loop)  # type: ignore[arg-type]
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(service.respond, _identity(), task["task_id"], "请定价", request_id="retry-1")
                self.assertTrue(loop.started.wait(timeout=1))
                second = pool.submit(service.respond, _identity(), task["task_id"], "请定价", request_id="retry-1")
                loop.release.set()
                first_result = first.result(timeout=2)
                second_result = second.result(timeout=2)
            stored = tasks.get(_identity(), task["task_id"])

        self.assertEqual(loop.calls, 1)
        self.assertEqual(first_result, second_result)
        self.assertEqual([item["role"] for item in stored["messages"]], ["user", "assistant"])

    def test_reused_request_id_with_different_content_is_rejected(self) -> None:
        from products.app.backend.agent_runtime.conversation_service import ConversationService
        from products.app.backend.stores import _LocalDocumentStore
        from products.app.backend.task_runtime.task_service import TaskService

        class Loop:
            def run(self, _identity: SessionIdentity, _task_id: str, _message: str) -> dict[str, Any]:
                return {"status": "completed", "text": "已完成。", "rounds": 1, "observations": []}

        with tempfile.TemporaryDirectory() as temporary:
            tasks = TaskService(_LocalDocumentStore(Path(temporary)))
            task = tasks.create(_identity(), "请求冲突")
            service = ConversationService(tasks, gateway=object(), agent_loop=Loop())  # type: ignore[arg-type]
            service.respond(_identity(), task["task_id"], "第一条", request_id="retry-conflict")
            with self.assertRaises(ValidationError):
                service.respond(_identity(), task["task_id"], "第二条", request_id="retry-conflict")

    def test_verified_result_facts_are_available_without_tenant_or_principal(self) -> None:
        from products.app.backend.agent_runtime.context_builder import ContextBuilder
        from products.app.backend.stores import _LocalDocumentStore
        from products.app.backend.stores.contract_store import ContractStore
        from products.app.backend.stores.result_store import ResultStore
        from products.app.backend.task_runtime.task_service import TaskService

        with tempfile.TemporaryDirectory() as temporary:
            state = _LocalDocumentStore(Path(temporary))
            tasks = TaskService(state)
            results = ResultStore(state)
            task = tasks.create(_identity(), "受控估值事实 tenant-a")
            reference = results.commit_module_run(_identity(), task["task_id"], "pricer", {
                "run_id": "pricing-facts", "ok": True, "status": "completed",
                "pricing": {"pv": 12.34, "currency": "CNY", "greeks": {"Delta": 0.51}},
            })
            tasks.append_run_ref(_identity(), task["task_id"], reference)
            tasks.append_message(_identity(), task["task_id"], "user", "principal-a 的路径是/Users/haoranxu/private")
            context = ContextBuilder(
                tasks, ContractStore(state), AuthorizationPolicy(), catalog_version="v1.0", result_store=results,
            ).build(_identity(), task["task_id"], "解释 tenant-a principal-a")

        facts = context["facts"]["module_run_facts"]
        self.assertEqual(facts[0]["run_ref"]["module"], "pricer")
        self.assertTrue(any(item["label"] == "PV" and item["value"] == 12.34 for item in facts[0]["facts"]))
        self.assertTrue(all(item["fact_ref"].startswith("fact_") for item in facts[0]["facts"]))
        serialized = str(context)
        self.assertNotIn("tenant-a", serialized)
        self.assertNotIn("principal-a", serialized)
        self.assertNotIn("/Users/haoranxu", serialized)

    def test_fact_summary_reads_core_result_not_mutable_app_index(self) -> None:
        from products.app.backend.stores import _LocalDocumentStore
        from products.app.backend.stores.result_store import ResultStore
        from products.app.backend.task_runtime.task_service import TaskService

        with tempfile.TemporaryDirectory() as temporary:
            state = _LocalDocumentStore(Path(temporary))
            tasks = TaskService(state)
            results = ResultStore(state)
            task = tasks.create(_identity(), "核心事实来源")
            reference = results.commit_module_run(_identity(), task["task_id"], "pricer", {
                "run_id": "core-source", "ok": True, "status": "completed", "pricing": {"pv": 12.34, "currency": "CNY"},
            })

            def tamper(rows: dict[str, Any]) -> dict[str, Any]:
                rows["pricer:core-source"]["result"]["pricing"]["pv"] = 99.0
                return rows

            state.update("results", tamper)
            summary = results.verified_fact_summary(_identity(), reference)

        self.assertEqual(summary["facts"][0]["value"], 12.34)

    def test_numeric_final_requires_known_fact_citation(self) -> None:
        from products.app.backend.agent_runtime.agent_loop import AgentLoop

        class Facts:
            def facts_for(self, _identity: SessionIdentity, _task_id: str, _tool: str, _value: Mapping[str, Any]) -> Mapping[str, Any]:
                return {
                    "run_ref": {"module": "pricer", "run_id": "pricing-facts", "result_hash": "a" * 64},
                    "facts": [{"fact_ref": "fact_a1b2c3d4", "label": "PV", "value": 12.34, "unit": "CNY"}],
                }

        tools = _Tools({"ok": True, "status": "succeeded"})
        loop = AgentLoop(
            gateway=_Gateway([
                {"action": "call_tool", "tool": "pricer.run", "arguments": {}},
                {"action": "final", "text": "PV为12.34[fact:fact_a1b2c3d4]。", "fact_refs": ["fact_a1b2c3d4"]},
            ]),
            context_builder=_Context(), tool_executor=tools, recommender=_Recommender(), observation_builder=Facts(),
        )
        result = loop.run(_identity(), "task-facts", "请解释PV")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["observations"][0]["facts"][0]["fact_ref"], "fact_a1b2c3d4")

        mismatch = AgentLoop(
            gateway=_Gateway([
                {"action": "call_tool", "tool": "pricer.run", "arguments": {}},
                {"action": "final", "text": "PV为99[fact:fact_a1b2c3d4]。", "fact_refs": ["fact_a1b2c3d4"]},
            ]),
            context_builder=_Context(), tool_executor=_Tools({"ok": True, "status": "succeeded"}),
            recommender=_Recommender(), observation_builder=Facts(),
        ).run(_identity(), "task-facts", "请解释PV")
        self.assertEqual(mismatch["status"], "partial")

    def test_historical_verified_fact_can_be_cited_without_rerunning_module(self) -> None:
        from products.app.backend.agent_runtime.agent_loop import AgentLoop

        class HistoricalContext(_Context):
            def build(self, _identity: SessionIdentity, _task_id: str, _message: str) -> dict[str, Any]:
                return {
                    "tool_catalog": [{"name": "pricer.run"}],
                    "facts": {"module_run_facts": [{
                        "run_ref": {"module": "pricer", "run_id": "prior", "result_hash": "a" * 64},
                        "facts": [{"fact_ref": "fact_a1b2c3d4", "label": "PV", "value": 12.34, "unit": "CNY"}],
                    }]},
                }

        tools = _Tools()
        result = AgentLoop(
            gateway=_Gateway([{"action": "final", "text": "PV为12.34[fact:fact_a1b2c3d4]。", "fact_refs": ["fact_a1b2c3d4"]}]),
            context_builder=HistoricalContext(), tool_executor=tools, recommender=_Recommender(),
        ).run(_identity(), "task-history", "解释已有PV")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(tools.calls, 0)

    def test_final_and_tool_observation_redact_paths_and_identity(self) -> None:
        from products.app.backend.agent_runtime.agent_loop import AgentLoop

        loop = AgentLoop(
            gateway=_Gateway([
                {"action": "call_tool", "tool": "pricer.run", "arguments": {}},
                {"action": "final", "text": "结果位于/Users/haoranxu/private.json，租户tenant-a，用户principal-a。"},
            ]),
            context_builder=_Context(),
            tool_executor=_Tools({"ok": True, "status": "failed", "message": "Bearer abcdefghijklmnopqrstuvwxyz /private/tmp/key tenant-a"}),
            recommender=_Recommender(),
        )
        result = loop.run(_identity(), "task-redaction", "解释结果")
        serialized = str(result)
        self.assertNotIn("/Users/haoranxu", serialized)
        self.assertNotIn("/private/tmp/key", serialized)
        self.assertNotIn("tenant-a", serialized)
        self.assertNotIn("principal-a", serialized)

    def test_conversation_scope_rejects_uncontrolled_data_and_full_report_for_sales(self) -> None:
        from products.app.backend.agent_runtime.tool_dispatcher import _require_task_data_scope
        from products.app.backend.stores import _LocalDocumentStore
        from products.app.backend.task_runtime.task_service import TaskService
        from products.app.backend.tool_gateway import _require_conversation_tool_scope

        with self.assertRaises(AuthorizationError):
            _require_conversation_tool_scope(_identity(), "datafetcher", {
                "action": "fetch", "asset_id": "000905.SH", "start_date": "2024-01-01", "end_date": "2024-12-31",
                "fields": ["close"], "local_csv": "/private/tmp/input.csv",
            })
        with self.assertRaises(AuthorizationError):
            _require_conversation_tool_scope(_identity(), "reporter", {
                "action": "run", "kind": "report", "selection": {"output_type": "report"},
            })
        with tempfile.TemporaryDirectory() as temporary:
            tasks = TaskService(_LocalDocumentStore(Path(temporary)))
            task = tasks.create(_identity(), "任务数据范围")
            with self.assertRaises(ValidationError):
                _require_task_data_scope(tasks, _identity(), "pricer", {"task_id": task["task_id"], "market_data_refs": ["data-missing"]})
            tasks.append_data_asset_ref(_identity(), task["task_id"], {"data_asset_id": "data-owned", "content_hash": "a" * 64})
            with self.assertRaises(AuthorizationError):
                _require_task_data_scope(tasks, _identity(), "pricer", {"task_id": task["task_id"], "market_data_refs": ["data-other"]})

    def test_conversation_data_fetch_is_ifind_realtime_only(self) -> None:
        from products.app.backend.tool_gateway import _require_conversation_tool_scope

        base = {
            "action": "fetch", "asset_id": "000905.SH",
            "start_date": "2024-01-01", "end_date": "2024-12-31",
            "fields": ["close", "adj_close"],
        }
        _require_conversation_tool_scope(_identity(), "datafetcher", base)
        _require_conversation_tool_scope(
            _identity(), "datafetcher", {**base, "provider": "ifind_http", "cache_policy": "force_refresh"},
        )
        for override in (
            {"provider": "wind"}, {"provider": "local"},
            {"cache_policy": "reuse"}, {"cache_policy": "extend_only"}, {"offline": True},
        ):
            with self.subTest(override=override), self.assertRaises(AuthorizationError):
                _require_conversation_tool_scope(_identity(), "datafetcher", {**base, **override})

    def test_tool_name_and_arguments_cannot_override_conversation_scope(self) -> None:
        from products.app.backend.agent_runtime.agent_loop import AgentLoop
        from products.app.backend.agent_runtime.recommender_adapter import AppConversationToolExecutor

        executor = AppConversationToolExecutor(object(), object())  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            executor.call(_identity(), "task-1", "datafetcher.status", {"action": "fetch"})
        with self.assertRaises(ValidationError):
            executor.call(_identity(), "task-1", "pricer.run", {"task_id": "other-task"})
        loop = AgentLoop(
            gateway=_Gateway([{"action": "call_tool", "tool": "pricer.run", "arguments": {"analysis": "hidden"}}]),
            context_builder=_Context(), tool_executor=_Tools(), recommender=_Recommender(),
        )
        self.assertEqual(loop.run(_identity(), "task-1", "运行")["status"], "unavailable")

    def test_post_tool_cancel_and_timeout_keep_terminal_machine_state(self) -> None:
        from products.app.backend.agent_runtime.agent_loop import AgentLoop

        cancelled = {"value": False}

        class CancellingTools(_Tools):
            def call(self, identity: SessionIdentity, task_id: str, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
                result = super().call(identity, task_id, name, arguments)
                cancelled["value"] = True
                return result

        stopped = AgentLoop(
            gateway=_Gateway([{"action": "call_tool", "tool": "pricer.run", "arguments": {}}]),
            context_builder=_Context(), tool_executor=CancellingTools(), recommender=_Recommender(),
            is_cancelled=lambda _identity, _task_id: cancelled["value"],
        ).run(_identity(), "task-cancel-after-tool", "运行")
        self.assertEqual(stopped["state"]["code"], "cancelled")
        self.assertEqual(stopped["observations"][0]["status"], "succeeded")

        timed = AgentLoop(
            gateway=_Gateway([{"action": "call_tool", "tool": "pricer.run", "arguments": {}}]),
            context_builder=_Context(), tool_executor=_Tools(), recommender=_Recommender(), timeout_seconds=1,
        )
        with patch("products.app.backend.agent_runtime.agent_loop.monotonic", side_effect=[0.0, 0.0, 0.0, 2.0]):
            timed_result = timed.run(_identity(), "task-timeout-after-tool", "运行")
        self.assertEqual(timed_result["state"]["code"], "timed_out")
        self.assertEqual(timed_result["observations"][0]["status"], "succeeded")

    def test_provider_failure_does_not_echo_secret_or_local_path(self) -> None:
        from products.app.backend.agent_runtime.conversation_service import ConversationService
        from products.app.backend.stores import _LocalDocumentStore
        from products.app.backend.task_runtime.task_service import TaskService

        class FailingGateway:
            def complete_for(self, _identity: SessionIdentity, _task_id: str, _message: str) -> str:
                raise RuntimeError("Bearer abcdefghijklmnopqrstuvwxyz /Users/haoranxu/config tenant-a")

        with tempfile.TemporaryDirectory() as temporary:
            tasks = TaskService(_LocalDocumentStore(Path(temporary)))
            task = tasks.create(_identity(), "Provider脱敏")
            result = ConversationService(tasks, FailingGateway()).respond(_identity(), task["task_id"], "运行", request_id="provider-redaction")  # type: ignore[arg-type]
        serialized = str(result)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("Bearer", serialized)
        self.assertNotIn("/Users/haoranxu", serialized)
        self.assertNotIn("tenant-a", serialized)

    def test_terminal_stops_and_host_rejects_model_authored_approval(self) -> None:
        from products.app.backend.agent_runtime.agent_loop import AgentLoop

        cancelled = AgentLoop(
            gateway=_Gateway([]), context_builder=_Context(), tool_executor=_Tools(), recommender=_Recommender(),
            is_cancelled=lambda _identity, _task_id: True,
        ).run(_identity(), "task-stop", "停止")
        self.assertEqual(cancelled["state"], {"code": "cancelled", "terminal": True, "retryable": False})

        approval = AgentLoop(
            gateway=_Gateway([
                {"action": "request_approval", "message": "请确认后重新提交。"},
                {"action": "final", "text": "当前只读操作无需额外确认。"},
            ]),
            context_builder=_Context(), tool_executor=_Tools(), recommender=_Recommender(),
        ).run(_identity(), "task-approval", "执行")
        self.assertEqual(approval["state"], {"code": "completed", "terminal": True, "retryable": False})
        self.assertNotIn("approval", approval)

        unsafe = AgentLoop(
            gateway=_Gateway([{"action": "ask_user", "question": "PV为99，请确认。"}]),
            context_builder=_Context(), tool_executor=_Tools(), recommender=_Recommender(),
        ).run(_identity(), "task-ask-user", "继续")
        self.assertEqual(unsafe["status"], "partial")


if __name__ == "__main__":
    unittest.main()
