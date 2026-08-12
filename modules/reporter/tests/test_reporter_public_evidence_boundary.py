"""Reporter public projection must remain a reader-facing fact projection."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT / "core" / "src", ROOT / "modules" / "reporter" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.reporter.designer_handoff import _report_title, _sections, build_collection_payload, build_designer_payload
from modules.reporter.report_unit_builder import _backtest_content, _contract_report_unit, _product_unit


class ReporterPublicEvidenceBoundaryTests(unittest.TestCase):
    def test_designer_payload_excludes_internal_audit_records_but_keeps_public_limitations(self) -> None:
        request = SimpleNamespace(
            output_type="report",
            selected_modules=("recommender", "backtest"),
            metadata={"as_of_date": "2026-08-12"},
        )
        unit = {
            "unit_type": "ContractReportUnit",
            "semantic_fact_hash": "frozen-fact-hash",
            "subject": {"product_name": "看涨期权", "underlyings": ["000905.SH"]},
            "modules": {
                "backtest": {
                    "status": "ready",
                    "run": {"run_id": "backtest-internal-run", "semantic_result_hash": "a" * 64},
                    "limitations": ["no_nav_curve_is_generated"],
                },
            },
            "content": {
                "recommendation": {
                    "product_name": "看涨期权", "underlyings": ["000905.SH"],
                    "reason": "适用于温和看涨判断。", "main_risks": ["权利金可能全部损失。"],
                    "candidate_id": "candidate-internal", "contract_fingerprint": "contract-internal",
                    "analysis_basis_id": "basis-internal", "evidence_refs": ["source-internal"],
                },
                "contract_highlights": [],
                "payoff": {"status": "not_requested"},
                "pricing": {"status": "not_requested"},
                "backtest": {"status": "ready", "metrics": []},
                "parameters": {},
                "audit": {
                    "recommender": {"run_id": "recommend-internal-run"},
                    "source_refs": {"catalog_version_ref": {"content_hash": "b" * 64}},
                    "limitations": [],
                },
                "risk": {"items": ["权利金可能全部损失。"]},
            },
        }

        payload = build_designer_payload(unit, request)

        self.assertNotIn("audit", payload)
        public_json = json.dumps(payload, ensure_ascii=False)
        for internal in ("run_id", "semantic_result_hash", "recommend-internal-run", "backtest-internal-run", "content_hash", "candidate_id", "contract_fingerprint", "analysis_basis_id", "evidence_refs", "candidate-internal", "contract-internal", "basis-internal", "source-internal"):
            self.assertNotIn(internal, public_json)
        self.assertIn("未生成持有期净值曲线", " ".join(payload["risk"]["limitations"]))

    def test_backtest_public_notes_state_gross_basis_net_unavailable_and_win_denominator(self) -> None:
        content = _backtest_content({
            "status": "ready",
            "result": {"backtest": {
                "common_metrics": {"sample_count": 8, "win_rate": 0.5, "average_pnl": 10.0, "minimum_pnl": -5.0},
                "backtest_config": {"return_denominator": "notional"},
                "economic_convention": {
                    "pnl_basis": "contract_cashflow_before_external_costs",
                    "external_costs_modelled": False,
                    "client_net_pnl_status": "not_modelled",
                    "win_rate_numerator": "contract_cashflow_pnl_gt_zero",
                    "win_rate_denominator": "valid_trade_count",
                },
                "branch_coverage": {
                    "status": "partial", "declared_pair_count": 5,
                    "observed_pair_count": 3, "uncovered_pair_count": 2,
                    "limitations": ["coverage_does_not_claim_unobserved_scenarios_were_economically_exercised"],
                },
            }},
        })
        overview = next(table for table in content["detail_tables"] if table["title"] == "公共回测统计")
        rows = {row["label"]: row for row in overview["rows"]}
        self.assertIn("合约毛损益", rows["平均损益"]["note"])
        self.assertIn("客户净损益不可得", rows["平均损益"]["note"])
        self.assertIn("分母为8个有效入场样本", rows["胜率"]["note"])
        coverage = next(table for table in content["detail_tables"] if table["title"] == "分支覆盖")
        self.assertIn("共5个合同声明分支", coverage["rows"][0]["note"])
        self.assertIn("情景覆盖仅表示样本中观察到的路径", " ".join(content["limitations"]))

    def test_inconsistent_branch_counts_are_not_presented_as_coverage(self) -> None:
        content = _backtest_content({
            "status": "ready",
            "result": {"backtest": {
                "common_metrics": {"sample_count": 3, "win_rate": 1 / 3},
                "branch_coverage": {
                    "status": "partial", "declared_pair_count": 3,
                    "observed_pair_count": 3, "uncovered_pair_count": 1,
                },
            }},
        })
        self.assertNotIn("分支覆盖", [table["title"] for table in content["detail_tables"]])
        self.assertIn("分支覆盖统计不一致", " ".join(content["limitations"]))

    def test_product_knowledge_report_has_no_contract_calculation_chapters(self) -> None:
        request = SimpleNamespace(
            output_type="report", selected_modules=("recommender",), metadata={},
            tenant_id="tenant-a", task_id="task-a", analysis_case_id="case-a",
            source_refs={"catalog_version_ref": {}, "product_version_refs": {}},
        )
        unit = _product_unit(
            request,
            {"candidate": {
                "candidate_id": "product-2.1", "product_id": "2.1", "product_name": "看涨期权",
                "product_version": "product-v1", "underlyings": [], "reason": "产品知识说明。",
                "main_risks": ["权利金可能全部损失。"],
            }},
        )
        self.assertEqual(unit["evidence_status"], "partial")
        self.assertEqual(_sections(unit, request), ["recommendation", "risk"])
        self.assertEqual(_report_title(unit, request), "看涨期权产品资料报告")
        self.assertIn("未包含本次收益、估值或历史回测", " ".join(unit["content"]["audit"]["limitations"]))

    def test_multi_candidate_card_collection_is_a_summary_without_calculation_sections(self) -> None:
        request = SimpleNamespace(output_type="card", delivery_mode="combined", metadata={})
        units = [
            {"semantic_fact_hash": "unit-a", "subject": {"product_name": "看涨期权"}, "candidate": {"product_name": "看涨期权", "reason": "适用于温和看涨判断。"}, "content": {"risk": {"items": ["权利金风险。"]}}},
            {"semantic_fact_hash": "unit-b", "subject": {"product_name": "看跌期权"}, "candidate": {"product_name": "看跌期权", "reason": "适用于温和看跌判断。"}, "content": {"risk": {"items": ["标的上涨风险。"]}}},
        ]
        payload = build_collection_payload(units, request)
        self.assertEqual(payload["sections"], ["recommendation", "risk"])
        self.assertEqual(payload["card_modules"], [])
        self.assertEqual(payload["payoff"]["status"], "not_run")

    def test_recommendation_only_contract_delivery_is_partial_not_complete(self) -> None:
        request = SimpleNamespace(
            tenant_id="tenant-a", task_id="task-a", analysis_case_id="case-a",
            selected_modules=("recommender",),
            source_refs={"catalog_version_ref": {}, "product_version_refs": {}},
        )
        candidate = {
            "candidate_id": "candidate-a", "product_id": "2.1", "product_name": "看涨期权",
            "product_version": "product-v1", "underlyings": ["000905.SH"], "reason": "适用于温和看涨判断。",
            "main_risks": ["权利金可能全部损失。"], "key_terms": [],
        }
        unit = _contract_report_unit(
            request,
            {"candidate": candidate, "contract": None, "modules": {
                "payoff": {"status": "not_requested", "note": ""},
                "pricing": {"status": "not_requested", "note": ""},
                "backtest": {"status": "not_requested", "note": ""},
            }},
            {"status": "ready", "source": "controlled-selection", "run_id": "selection-a"},
        )
        self.assertEqual(unit["evidence_status"], "partial")
        self.assertIn("未包含收益、估值或历史回测", " ".join(unit["content"]["audit"]["limitations"]))


if __name__ == "__main__":
    unittest.main()
