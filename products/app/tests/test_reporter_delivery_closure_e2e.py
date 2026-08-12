"""Controlled Reporter/Designer delivery closure for a concrete user request."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / "products" / "app"
for source in (ROOT, APP_ROOT):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from backend.agent_runtime.agent_loop import AgentLoop
from backend.agent_runtime.recommender_adapter import AppConversationToolExecutor
from backend.authorization.roles import Role
from backend.errors import UnavailableCapabilityError
from backend.identity.session_identity import SessionIdentity
from backend.reporter_adapter import ReporterAdapter
from backend.stores import _LocalDocumentStore
from backend.stores.result_store import ResultStore
from backend.task_runtime.task_service import TaskService


MODULES = ("payoffer", "pricer", "backtester")


def _contract() -> dict[str, object]:
    return {
        "identity": {
            "product_id": "2.1",
            "name_zh": "看涨期权",
            "underlyings": ["000300.SH"],
            "currency": "CNY",
        },
        "terms": {"S0": 4000.0, "K": 4000.0, "T": 0.25, "Pi_0": 120.0},
        "term_sources": {"S0": "fixture", "K": "fixture", "T": "fixture", "Pi_0": "fixture"},
        "paths": [{
            "condition": "True",
            "cases": [
                {"domain": "S_T<=K", "pnl": "-Pi_0"},
                {"domain": "S_T>K", "pnl": "S_T-K-Pi_0"},
            ],
        }],
        "product_version": "product-v1",
        "resolved_schedules": {"observation_dates": ["2026-11-11"]},
        "contract_fingerprint": "d" * 64,
        "analysis_basis_id": "basis-000300-up-vol-up",
        "price_convention": {"spot": "close", "adjustment": "forward"},
    }


def _result(module: str) -> dict[str, object]:
    result: dict[str, object] = {
        "run_id": f"run-{module}",
        "ok": True,
        "status": "completed",
        "analysis_case_id": "case-000300-up-vol-up",
        "resolved_contract": _contract(),
    }
    if module == "payoffer":
        result.update({
            "formula": "max(S_T-K,0)-Pi_0",
            "path_panels": [{"title": "上涨情景", "condition": "S_T>K"}],
        })
    elif module == "pricer":
        result["pricing"] = {
            "method": "fixture-pricer",
            "pv": 121.5,
            "currency": "CNY",
            "greeks": {"Delta": 0.54, "Vega": 1.8},
        }
    else:
        result["backtest"] = {
            "sample_count": 24,
            "win_rate": 0.58,
            "average_return": 0.035,
            "max_loss": -0.12,
            "charts": [{
                "id": "fixture-return",
                "title": "历史情景收益",
                "type": "line",
                "x": ["T0", "T1"],
                "series": [{"name": "收益", "data": [0, 0.035]}],
                "x_axis_name": "日期",
                "y_axis_name": "收益率",
                "source_note": "受控测试数据",
            }],
        }
    return result


class _Registry:
    manifest = {"catalog_version": "v1.0"}

    def service_context(self, _identity: SessionIdentity, module: str, **kwargs: object) -> dict[str, object]:
        return {"module": module, "task_id": kwargs["task_id"]}


class _ReporterDispatcher:
    def __init__(self, results: ResultStore) -> None:
        self._reporter = ReporterAdapter(results)

    def dispatch_for_conversation(
        self,
        module: str,
        payload: dict[str, Any],
        identity: SessionIdentity,
        **_kwargs: object,
    ) -> dict[str, Any]:
        if module != "reporter":
            raise AssertionError("该E2E只允许正式Reporter交付")
        return self._reporter.dispatch(payload, identity)


class _Context:
    def build(self, _identity: SessionIdentity, task_id: str, message: str) -> dict[str, object]:
        return {
            "task": {"task_id": task_id},
            "latest_message": message,
            "tool_catalog": [{"name": "reporter.run"}],
            "facts": {},
        }


class _Gateway:
    def decide_for(self, _identity: SessionIdentity, _task_id: str, _context: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"action": "call_tool", "tool": "reporter.run", "arguments": {"kind": "report", "format": "html"}}


class _NoRecommendation:
    def run_fixed(self, *_args: object, **_kwargs: object) -> Mapping[str, Any]:
        raise AssertionError("已有正式结果时不得重启推荐")


class _UnavailableReporter:
    def call(self, *_args: object, **_kwargs: object) -> Mapping[str, Any]:
        raise UnavailableCapabilityError("report_delivery", "Designer渲染能力未配置，请完成配置后重试。")


class ReporterDeliveryClosureE2E(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = _LocalDocumentStore(Path(self.temporary.name))
        self.results = ResultStore(self.state)
        self.tasks = TaskService(self.state)
        self.identity = SessionIdentity("principal-report", "tenant-report", Role.ADMIN, "session-report")
        self.task = self.tasks.create(self.identity, "000300.SH上涨且波动率上升")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _executor(self) -> AppConversationToolExecutor:
        return AppConversationToolExecutor(_ReporterDispatcher(self.results), _Registry(), self.results)  # type: ignore[arg-type]

    def test_concrete_request_uses_real_reporter_and_designer_with_all_formal_results(self) -> None:
        for module in MODULES:
            self.results.commit_module_run(self.identity, self.task["task_id"], module, _result(module))

        reply = self._executor().call(self.identity, self.task["task_id"], "reporter.run", {
            "kind": "report",
            "format": "html",
            "html_report_layout": "continuous",
            "title": "沪深300上涨与波动率上升期权结构研究",
        })

        self.assertEqual(reply["status"], "completed", reply)
        self.assertEqual(reply["delivery"]["coverage_status"], "complete")
        self.assertEqual(reply["delivery"]["missing_modules"], [])
        self.assertEqual(reply["delivery"]["preview_url"], reply["preview_url"])
        record = self.results.resolve_report_run(self.identity, reply["report_run_ref"])
        html, content_type = self.results.read_report_artifact(
            self.identity,
            reply["report_run_ref"]["report_run_id"],
            reply["output"]["report"],
        )
        rendered = html.decode("utf-8")
        self.assertEqual(content_type, "text/html; charset=utf-8")
        self.assertIn("沪深300上涨与波动率上升期权结构研究", rendered)
        self.assertIn("000300.SH", rendered)
        self.assertIn("看涨期权", rendered)
        # The fixture includes an actual backtest series, so the offline chart
        # runtime must travel with this portable HTML delivery.
        self.assertIn("assets/echarts.min.js", {item["name"] for item in record["artifact_manifest"]})
        self.assertNotIn("<script src=\"http", rendered)

    def test_report_with_only_payoff_is_delivered_as_explicit_partial_coverage(self) -> None:
        self.results.commit_module_run(self.identity, self.task["task_id"], "payoffer", _result("payoffer"))

        reply = self._executor().call(self.identity, self.task["task_id"], "reporter.run", {
            "kind": "report",
            "format": "html",
        })

        self.assertEqual(reply["status"], "completed", reply)
        self.assertEqual(reply["delivery"]["coverage_status"], "partial")
        self.assertEqual(reply["delivery"]["missing_modules"], ["pricing", "backtest"])
        record = self.results.resolve_report_run(self.identity, reply["report_run_ref"])
        html, _ = self.results.read_report_artifact(
            self.identity,
            reply["report_run_ref"]["report_run_id"],
            reply["output"]["report"],
        )
        rendered = html.decode("utf-8")
        self.assertIn("本次未运行该项分析", rendered)
        self.assertEqual(record["report_request"]["output_type"], "report")

    def test_agent_returns_preview_and_does_not_call_partial_coverage_complete(self) -> None:
        self.results.commit_module_run(self.identity, self.task["task_id"], "payoffer", _result("payoffer"))
        result = AgentLoop(
            gateway=_Gateway(),
            context_builder=_Context(),
            tool_executor=self._executor(),
            recommender=_NoRecommendation(),
        ).run(self.identity, self.task["task_id"], "我认为000300.SH未来会上涨、波动变大，请生产报告")

        self.assertEqual(result["status"], "partial", result)
        self.assertIn("/api/reports/", result["text"])
        self.assertIn("估值定价、历史回测未执行", result["text"])
        self.assertNotIn("完整研究报告已生成", result["text"])

    def test_reporter_or_designer_gap_is_returned_as_an_explicit_capability_failure(self) -> None:
        result = AgentLoop(
            gateway=_Gateway(),
            context_builder=_Context(),
            tool_executor=_UnavailableReporter(),
            recommender=_NoRecommendation(),
        ).run(self.identity, self.task["task_id"], "生成完整研究报告")

        self.assertEqual(result["status"], "unavailable", result)
        self.assertIn("Designer渲染能力未配置", result["text"])
        self.assertEqual(result["error"]["capability"], "report_delivery")
        self.assertIn("完成配置后重试", result["error"]["next_step"])


if __name__ == "__main__":
    unittest.main()
