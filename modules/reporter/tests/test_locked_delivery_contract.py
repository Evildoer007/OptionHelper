"""Locked public-delivery rules for Reporter Card and Report."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import re
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
TEST_ROOT = Path(__file__).resolve().parent
for source in (
    PROJECT_ROOT / "core" / "src",
    PROJECT_ROOT / "modules" / "reporter" / "src",
    PROJECT_ROOT / "modules" / "designer" / "src",
    TEST_ROOT,
):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.adapters.local_store import LocalResultStore
from modules.reporter.reporter_engine import build_report
from test_reporter_run_refs import DESIGNER_PORT, candidate, commit_run, full_contract, report_request


class LockedDeliveryContractTests(unittest.TestCase):
    def _build_outcome(self, *, output_type: str) -> tuple[Path, dict]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        store = LocalResultStore(root / "store")
        contract = full_contract()
        item = candidate(contract=contract)
        refs = {
            item["candidate_id"]: {
                "payoff": asdict(commit_run(store, "payoffer", "pay-card", item, contract)),
                "pricing": asdict(commit_run(store, "pricer", "price-card", item, contract)),
                "backtest": asdict(commit_run(store, "backtester", "back-card", item, contract)),
            }
        }
        request = report_request(
            [item], refs, report_run_id=f"locked-{output_type}", output_type=output_type,
            layout=None if output_type == "card" else "continuous",
        )
        return root, build_report(request, result_store=store, designer_port=DESIGNER_PORT, output_root=root / "reports")

    def test_card_is_a_compact_public_brief_without_payoff_scenarios_or_next_steps(self) -> None:
        _, outcome = self._build_outcome(output_type="card")
        html = (Path(outcome["directory"]) / "report.html").read_text(encoding="utf-8")
        visible = re.sub(r"<(?:style|script)\b[^>]*>.*?</(?:style|script)\s*>", " ", html, flags=re.DOTALL | re.IGNORECASE)

        for required in ("推荐结构", "估值定价", "12.34", "历史回测", "风险提示"):
            self.assertIn(required, html)
        for excluded in ("收益情景", "下一步", "参数", "模块状态"):
            self.assertNotIn(excluded, html)
        # Card keeps pricing and backtest facts in two compact, printable
        # three-line tables.  It remains a concise subset of the Report while
        # preserving complete reader-critical values.
        self.assertEqual(html.count('class="card-data-table"'), 2)
        self.assertNotIn('class="card-metric-grid"', html)
        for greek in ("Delta", "Gamma", "Vega", "Theta", "Rho"):
            self.assertIn(greek, html)
        for forbidden in ("<img", "<svg", "echarts", "审计", "RunRef", "manifest", "hash", "OptionHelper"):
            self.assertNotIn(forbidden.lower(), visible.lower())

    def test_report_has_the_locked_detailed_reader_structure(self) -> None:
        _, outcome = self._build_outcome(output_type="report")
        html = (Path(outcome["directory"]) / "report.html").read_text(encoding="utf-8")

        for heading in ("核心结论", "结构推荐", "合同参数", "收益结构", "估值定价", "历史回测", "风险提示"):
            self.assertIn(heading, html)
        self.assertIn("研究逻辑", html)
        self.assertNotIn("下一步", html)
        self.assertNotIn('class="report-toc"', html)
        self.assertIn("本次参数化收益图", html)
        self.assertNotIn("OptionHelper", html)
        self.assertNotIn("审计", html)


if __name__ == "__main__":
    unittest.main()
