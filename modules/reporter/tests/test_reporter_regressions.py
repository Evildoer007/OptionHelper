from __future__ import annotations

from dataclasses import asdict
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
TEST_ROOT = Path(__file__).resolve().parent
for source in (
    PROJECT_ROOT / "core" / "src",
    PROJECT_ROOT / "modules" / "reporter" / "src",
    PROJECT_ROOT / "modules" / "designer" / "src",
    PROJECT_ROOT / "modules" / "reporter",
    TEST_ROOT,
):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.adapters.local_store import LocalResultStore
from modules.reporter.models import ReporterError, ReportRequest
from modules.reporter.reporter_engine import build_report
from test_reporter_run_refs import DESIGNER_PORT, TENANT, candidate, commit_run, full_contract, report_request


class ReporterRegressionTest(unittest.TestCase):
    def test_delivery_defaults_and_rendered_content_follow_card_report_contract(self) -> None:
        """Public delivery defaults must not expose implementation/audit content."""

        item = candidate()
        base = report_request([item], {item["candidate_id"]: {}}, selected=["recommender"])
        base.pop("format")
        base.pop("html_report_layout")
        parsed = ReportRequest.from_mapping(base)
        self.assertEqual(parsed.format, "html")
        self.assertEqual(parsed.html_report_layout, "continuous")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            outcome = build_report(
                base,
                result_store=store,
                designer_port=DESIGNER_PORT,
                output_root=root / "reports",
            )
            report = (Path(outcome["directory"]) / "report.html").read_text(encoding="utf-8")
            self.assertNotIn("OptionHelper", report)
            self.assertNotIn("审计", report)
            self.assertNotIn("运行记录", report)
            for internal in ("ModuleRunRef", "RunRef", "source_id", "manifest"):
                self.assertNotIn(internal, report)

    def test_card_report_content_and_html_pdf_format_are_orthogonal(self) -> None:
        item = candidate()
        base = report_request([item], {item["candidate_id"]: {}}, selected=["recommender"])
        valid = (
            ("card", "html", None),
            ("card", "pdf", None),
            ("report", "html", "continuous"),
            ("report", "pdf", None),
        )
        for output_type, output_format, layout in valid:
            request = deepcopy(base)
            request.update({"output_type": output_type, "format": output_format, "html_report_layout": layout})
            parsed = ReportRequest.from_mapping(request)
            self.assertEqual((parsed.output_type, parsed.format, parsed.html_report_layout), (output_type, output_format, layout))

        for output_type, output_format, layout in (
            ("card", "html", "with_toc"),
            ("report", "html", "with_toc"),
            ("report", "pdf", "continuous"),
        ):
            request = deepcopy(base)
            request.update({"output_type": output_type, "format": output_format, "html_report_layout": layout})
            with self.assertRaises(ReporterError):
                ReportRequest.from_mapping(request)

    def test_frozen_module_run_ref_keeps_runtime_module_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            resolved = full_contract()
            item = candidate(contract=resolved)
            payoff = commit_run(store, "payoffer", "pay-module", item, resolved)
            refs = {item["candidate_id"]: {"payoff": asdict(payoff)}}

            outcome = build_report(
                report_request([item], refs, report_run_id="report-module", selected=["payoff"]),
                result_store=store,
                designer_port=DESIGNER_PORT,
                output_root=root / "reports",
            )

            unit = json.loads((Path(outcome["directory"]) / "report-unit.json").read_text(encoding="utf-8"))
            manifest = json.loads((Path(outcome["directory"]) / "run_manifest.json").read_text(encoding="utf-8"))
            marker = json.loads((store.resolve_module_run(payoff, tenant_id=TENANT) / "commit_marker.json").read_text(encoding="utf-8"))
            self.assertEqual(unit["modules"]["payoff"]["run_ref"]["module"], "payoffer")
            self.assertEqual(manifest["source_runs"][0]["module_run_ref"]["module"], "payoffer")
            self.assertEqual(unit["modules"]["payoff"]["run"]["artifact_manifest_hash"], marker["artifact_manifest_hash"])

    def test_card_contains_selected_pricing_result_not_only_run_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalResultStore(root / "store")
            resolved = full_contract()
            item = candidate(contract=resolved)
            pricing = commit_run(store, "pricer", "pricing-card", item, resolved)
            refs = {item["candidate_id"]: {"pricing": asdict(pricing)}}

            outcome = build_report(
                report_request(
                    [item], refs, report_run_id="report-pricing-card",
                    output_type="card", layout=None, selected=["pricing"],
                ),
                result_store=store,
                designer_port=DESIGNER_PORT,
                output_root=root / "reports",
            )

            html = (Path(outcome["directory"]) / "report.html").read_text(encoding="utf-8")
            self.assertIn("估值", html)
            self.assertIn("12.34", html)

    def test_source_refresh_failure_clears_stale_selection_and_preview(self) -> None:
        script = (PROJECT_ROOT / "modules" / "reporter" / "page" / "reporter.js").read_text(encoding="utf-8")
        failure_branch = script.split("if (!response.ok)", 1)[1].split("state.ready = true", 1)[0]
        self.assertIn("clearSourceState()", failure_branch)
        self.assertIn("previewPanel", script.split("function clearSourceState()", 1)[1].split("function", 1)[0])

    def test_page_allows_recommender_to_be_selected_independently(self) -> None:
        html = (PROJECT_ROOT / "modules" / "reporter" / "page" / "reporter.html").read_text(encoding="utf-8")
        script = (PROJECT_ROOT / "modules" / "reporter" / "page" / "reporter.js").read_text(encoding="utf-8")
        self.assertIn('data-module="recommender"', html)
        self.assertNotIn("['recommender', ...", script)


if __name__ == "__main__":
    unittest.main()
