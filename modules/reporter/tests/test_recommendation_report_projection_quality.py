"""Reporter projection contract for the seven-section recommendation Report."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "core" / "src", ROOT / "modules" / "reporter" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.reporter.designer_handoff import build_designer_payload
from modules.reporter.models import ReportRequest, stable_hash
from modules.reporter.report_unit_builder import _parameter_rows, normalize_parameter_groups


def request() -> ReportRequest:
    return ReportRequest(
        tenant_id="tenant-a",
        task_id="task-a",
        report_run_id="report-a",
        analysis_case_id="case-a",
        subject_type="contract",
        subject_ref={
            "delivery_mode": "single",
            "candidate_ids": ["candidate-a"],
            "selected_modules": ["recommender", "payoff", "pricing", "backtest"],
        },
        source_refs={},
        output_type="report",
        format="html",
        html_report_layout="continuous",
        audience="professional",
        metadata={},
    )


def report_unit() -> dict:
    unit = {
        "unit_type": "ContractReportUnit",
        "subject": {"product_name": "熊市看涨价差", "underlyings": ["000300.SH"]},
        "modules": {},
        "content": {
            "recommendation": {
                "candidate_id": "candidate-a",
                "product_name": "熊市看涨价差",
                "underlyings": ["000300.SH"],
                "reason": "匹配温和上涨、上行空间有限的市场判断。",
                "suitable_for": ["可接受净权利金损失。", "预期标的温和上涨。"],
                "not_suitable_for": ["预期标的大幅上涨。"],
                "main_risks": ["最大收益受限。", "净权利金可能损失。", "不应进入摘要。"],
                "key_terms": [{"label": "执行价1", "value": 4050.12}],
            },
            "payoff": {"status": "ready", "scenarios": []},
            "pricing": {
                "status": "ready",
                "metrics": [
                    {"label": "现值", "value": -228.456, "note": "人民币"},
                    {"label": "理论价值占比", "value": -0.0223, "value_format": "percent"},
                    {"label": "标准误", "value": 1.2345},
                    {"label": "不应进入摘要", "value": 99},
                ],
                "greeks": [
                    {"label": "Delta", "value": 0.22},
                    {"label": "Gamma", "value": 0.001},
                    {"label": "Vega", "value": 3.4},
                ],
            },
            "backtest": {
                "status": "ready",
                "metrics": [
                    {"label": "样本数", "value": 48},
                    {"label": "胜率", "value": 0.58, "value_format": "percent"},
                    {"label": "平均损益", "value": 123.45},
                    {"label": "最大亏损", "value": -456.78},
                    {"label": "额外指标", "value": 100},
                ],
                "detail_tables": [{
                    "title": "公共回测统计",
                    "rows": [{"label": "平均损益", "value": 123.45}],
                }],
            },
            "parameters": {},
            "audit": {"limitations": []},
            "risk": {"items": ["最大收益受限。", "净权利金可能损失。", "不应进入摘要。"], "disclaimer": "仅供研究。"},
        },
    }
    unit["semantic_fact_hash"] = stable_hash(unit)
    return unit


class RecommendationReportProjectionQualityTests(unittest.TestCase):
    def test_report_projection_is_structured_without_research_or_next_steps(self) -> None:
        unit = report_unit()
        original = deepcopy(unit)
        payload = build_designer_payload(unit, request())

        self.assertEqual(payload["sections"], ["conclusion", "recommendation", "parameters", "payoff", "pricing", "backtest", "risk"])
        self.assertNotIn("research", payload)
        conclusion = payload["conclusion"]
        self.assertEqual(conclusion["structure_name"], "熊市看涨价差")
        self.assertEqual(conclusion["underlyings"], "000300.SH")
        self.assertEqual(len(conclusion["reasons"]), 3)
        self.assertEqual(len(conclusion["valuation_summary"]), 3)
        self.assertEqual([row["label"] for row in conclusion["greeks_summary"]], ["Delta", "Vega"])
        self.assertEqual(
            [row["label"] for row in conclusion["backtest_summary"]],
            ["样本数", "胜率", "平均损益", "最大亏损"],
        )
        self.assertEqual(conclusion["risk_summary"], ["最大收益受限。", "净权利金可能损失。"])
        self.assertNotIn("summary", conclusion)
        self.assertNotIn("next_steps", conclusion)
        self.assertNotIn("terms", payload["recommendation"])
        self.assertNotIn("key_terms", payload["recommendation"])
        self.assertEqual(unit, original)
        self.assertNotIn("report_unit_semantic_fact_hash", payload)

    def test_parameter_projection_maps_chinese_terms_and_symbols(self) -> None:
        rows = _parameter_rows({
            "terms": {"K1": 4050.12, "K2": 4250.34, "P_net": 32.1, "constraints": "K_1 < K_2", "monitor": "到期观察"},
            "term_sources": {},
        })
        by_name = {row["cn"]: row for row in rows}

        self.assertEqual(by_name["执行价1"]["symbol"], "K_1")
        self.assertEqual(by_name["执行价2"]["symbol"], "K_2")
        self.assertEqual(by_name["净权利金"]["symbol"], "P_net")
        self.assertEqual(by_name["合同约束"]["symbol"], "")
        self.assertEqual(by_name["观察设置"]["symbol"], "")

        legacy = normalize_parameter_groups({
            "common_input": [
                {"cn": "K1", "en": "K1", "symbol": "", "value": 4050.12},
                {"cn": "P_net", "en": "P_net", "symbol": "", "value": 32.1},
            ]
        })
        self.assertEqual(legacy["common_input"][0]["cn"], "执行价1")
        self.assertEqual(legacy["common_input"][0]["symbol"], "K_1")
        self.assertEqual(legacy["common_input"][1]["cn"], "净权利金")
        self.assertEqual(legacy["common_input"][1]["symbol"], "P_net")


if __name__ == "__main__":
    unittest.main()
