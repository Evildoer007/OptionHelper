"""Standalone project Host contract and real Reporter/Designer closure."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
for source in (ROOT, ROOT / "core" / "src"):
    if str(source) not in os.sys.path:
        os.sys.path.insert(0, str(source))

from core import tool_entry
from modules.recommender.models import ModelCapability
from runtime.adapters.local_host import LocalHostError, LocalProjectLayout


class _DeterministicModel:
    def capability(self) -> ModelCapability:
        return ModelCapability(model_id="fixture-model", structured_output=True, tool_calling=False)

    def run_step(self, role: str, payload: dict) -> dict:
        data = payload["input"]
        if role.endswith("Intent"):
            return {
                "confirmed_constraints": dict(data["confirmed_constraints"]),
                "missing_information": [],
                "next_question": None,
                "research_queries": ["看涨期权 上涨 波动率上升"],
            }
        if role.endswith("Research"):
            evidence = [item for item in data["evidence"] if item["product_id"] == "2.1"]
            return {"proposals": [{
                "product_id": "2.1",
                "product_name": "看涨期权",
                "underlyings": ["000300.SH"],
                "reason": "方向与波动判断均受益于多头看涨结构，最大损失限定为期权费。",
                "suitable_for": ["看涨且预期波动率上升"],
                "not_suitable_for": ["预期波动率显著下降"],
                "main_risks": ["到期未上涨时可能损失全部期权费"],
                "library_status": "ready",
                "evidence_ref_ids": [item["evidence_id"] for item in evidence],
                "missing_inputs": [],
            }]}
        if role.endswith("Critic"):
            return {"reviews": [{
                "product_id": "2.1", "hard_reject": False,
                "rejection_reason": None,
                "additional_not_suitable_for": [],
                "additional_risks": ["时间价值衰减"],
                "rank_adjustment": 0,
            }]}
        raise AssertionError(role)


class _FakeIFindProvider:
    name = "ifind_http"
    network = True

    @staticmethod
    def estimate_quota(request) -> int:
        return len(request.asset_ids)

    def fetch(self, request, config):
        del config
        dates = pd.date_range(request.start_date, request.end_date, freq="B")
        rows = []
        for asset in request.asset_ids:
            for index, timestamp in enumerate(dates):
                close = 100.0 + index * 0.02
                rows.append({
                    "date": timestamp.strftime("%Y-%m-%d"), "asset_id": asset,
                    "open": close - 0.2, "high": close + 0.5,
                    "low": close - 0.5, "close": close,
                    "adj_open": close - 0.2, "adj_high": close + 0.5,
                    "adj_low": close - 0.5, "adj_close": close,
                    "volume": 1_000_000.0,
                })
        return pd.DataFrame(rows)


class LocalProjectHostTest(unittest.TestCase):
    def test_project_layout_rejects_skill_installation_as_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill = Path(temporary) / "option-helper"
            skill.mkdir()
            with self.assertRaisesRegex(LocalHostError, "研究项目目录"):
                LocalProjectLayout.create(skill_root=skill, project_root=skill / "nested")

    def test_missing_secrets_are_reported_as_one_human_configuration_gap(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(tool_entry.ProjectRequestError) as raised:
                tool_entry.run_project_request("给我推荐结构并生成报告")
        self.assertEqual(raised.exception.stage, "configuration")
        self.assertEqual(
            raised.exception.missing,
            ("模型服务地址", "模型名称", "模型API Key", "iFind Refresh Token"),
        )
        self.assertNotIn("Traceback", str(raised.exception))

    def test_fake_model_and_ifind_use_real_payoffer_reporter_and_designer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="optionhelper-project-") as temporary:
            project = Path(temporary)
            events: list[dict] = []
            with (
                patch.dict(os.environ, {"IFIND_REFRESH_TOKEN": "fixture-refresh"}),
                patch("modules.datafetcher.service._providers", return_value={"ifind_http": _FakeIFindProvider()}),
            ):
                result = tool_entry.run_project_request(
                    "我认为000300.SH未来会上涨，波动变大，给我推荐一个期权结构，并且生产报告",
                    project_root=project,
                    agent_port=_DeterministicModel(),
                    ifind_probe=lambda token: self.assertEqual(token, "fixture-refresh"),
                    progress=lambda event: events.append(dict(event)),
                )

            report = Path(result["report"]["path"])
            html = report.read_text(encoding="utf-8")
            project_root_names = [path.name for path in project.iterdir()]

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["request"]["html_report_layout"], "continuous")
        self.assertEqual(result["recommendation"]["product_id"], "2.1")
        self.assertIn("module_run_ref", {"module_run_ref": result["module_runs"]["payoff"]})
        self.assertTrue(report.name.endswith(".html"))
        self.assertIn("<!doctype html>", html.lower())
        for section in ("payoff", "pricing", "backtest"):
            self.assertIn(f'id="section-{section}" class="report-section" data-status="ready"', html)
            self.assertIn("Designer", json.dumps(events, ensure_ascii=False))
            self.assertFalse(any(name.endswith(".py") for name in project_root_names))
            self.assertFalse(any(name.endswith(".html") for name in project_root_names))

    def test_fake_model_request_for_brief_generates_a_real_compact_card(self) -> None:
        with tempfile.TemporaryDirectory(prefix="optionhelper-project-card-") as temporary:
            project = Path(temporary)
            with (
                patch.dict(os.environ, {"IFIND_REFRESH_TOKEN": "fixture-refresh"}),
                patch("modules.datafetcher.service._providers", return_value={"ifind_http": _FakeIFindProvider()}),
            ):
                result = tool_entry.run_project_request(
                    "我认为000300.SH未来会上涨，波动变大，给我推荐一个期权结构并生成简报",
                    project_root=project,
                    agent_port=_DeterministicModel(),
                    ifind_probe=lambda token: self.assertEqual(token, "fixture-refresh"),
                )

            html = Path(result["report"]["path"]).read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["request"]["output_type"], "card")
        self.assertIsNone(result["request"]["html_report_layout"])
        for required in ("推荐结构", "估值定价", "历史回测", "风险提示"):
            self.assertIn(required, html)
        for excluded in ("收益情景", "下一步", "<img", "echarts"):
            self.assertNotIn(excluded, html)

    def test_public_cli_projection_hides_internal_run_identifiers(self) -> None:
        public = tool_entry.public_project_result({
            "ok": True,
            "status": "completed",
            "message": "完成",
            "recommendation": {"product_name": "看涨期权", "reason": "看涨且波动率上升"},
            "report": {"format": "html", "coverage_status": "complete", "path": "/project/result/report.html"},
            "module_runs": {"payoff": {"run_id": "hidden"}},
        })
        encoded = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("run_id", encoded)
        self.assertNotIn("candidate_id", encoded)
        self.assertIn("report.html", encoded)


if __name__ == "__main__":
    unittest.main()
