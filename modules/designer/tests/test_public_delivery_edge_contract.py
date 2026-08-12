"""Regression tests for public delivery edge cases.

They keep public copy truthful when an analysis is incomplete and when Reporter
builds a multi-candidate landing page.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT / "core" / "src", ROOT / "modules" / "designer" / "src", ROOT / "modules" / "reporter" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.designer import render
from modules.designer.models import DesignerInput
from modules.reporter.designer_handoff import build_collection_payload


class PublicDeliveryEdgeContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = json.loads(
            (ROOT / "modules/designer/tests/fixtures/report-payload.example.json").read_text(encoding="utf-8")
        )

    def test_card_never_expands_into_a_payoff_scenario_surface(self) -> None:
        payload = deepcopy(self.payload)
        payload["payoff"] = {"status": "ready", "scenarios": []}

        html = render(DesignerInput(payload=payload, output_type="card"))["html"]

        self.assertNotIn("收益情景", html)
        self.assertNotIn("尚未形成可展示的收益情景", html)
        self.assertNotIn("<img", html)

    def test_collection_projects_internal_candidate_fields_before_rendering(self) -> None:
        request = SimpleNamespace(
            delivery_mode="batch",
            metadata={},
            format="html",
            output_type="report",
            html_report_layout="continuous",
        )
        units = [
            {
                "candidate": {
                    "candidate_id": "candidate-internal-a",
                    "product_name": "看涨期权",
                    "reason": "适用于已确认的市场判断。",
                    "rank": 1,
                },
                "content": {
                    "payoff": {"status": "not_run"},
                    "pricing": {"status": "not_run"},
                    "backtest": {"status": "not_run"},
                    "risk": {"items": ["标的价格波动可能造成损失。"]},
                },
                "subject": {"product_name": "看涨期权"},
                "evidence_status": "verified",
                "semantic_fact_hash": "frozen-hash",
            }
        ]

        payload = build_collection_payload(units, request)
        artifact = render(DesignerInput(payload=payload))
        self.assertNotIn("candidate-internal-a", artifact["html"])
        self.assertNotIn("frozen-hash", artifact["html"])


if __name__ == "__main__":
    unittest.main()
