from __future__ import annotations

from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "evals" / "runner.py"
SPEC = importlib.util.spec_from_file_location("optionhelper_evals_runner", RUNNER_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _tree_hash(root: Path) -> str:
    digest = sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(sha256(path.read_bytes()).digest())
    return digest.hexdigest()


class DevelopmentEvalsTest(unittest.TestCase):
    def test_case_files_are_valid_unique_and_secret_free(self) -> None:
        cases = runner.load_cases()
        self.assertGreaterEqual(len(cases), 30)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        required_regressions = {
            "pricer.mc11.low-precision",
            "pricer.observed-state.required",
            "backtester.path.non-trigger",
            "backtester.entry.non-trading-day",
            "backtester.multi-underlying.missing-history",
            "app.optdesk.module-task-context",
            "app.module-bridge.single-wrapper",
            "reporter.selection.host-projection",
            "reporter.selection.browser-source-refs-rejected",
            "datafetcher.caller-context.force-refresh-denied",
            "datafetcher.caller-context.force-refresh-allowed",
            "compute.payoffer.formal-run",
            "compute.pricer.formal-run",
            "compute.runref-v1.2-required",
            "modulehost.v2.signed-scope",
            "modulehost.v2.permission-intersection",
            "compute.formal-host-rejected",
            "compute.data-ref-tenant-rejected",
            "reporter.selection-negative",
            "recommender.evidence-negative",
            "datafetcher.app-security",
            "agent.security-boundaries",
            "app.agent.knowledge-no-compute",
            "app.agent.pricer-missing-input",
            "app.agent.recommender-fixed-workflow",
            "app.agent.tool-failure-no-fabrication",
            "app.agent.duplicate-call-stops",
            "app.agent.permission-stops",
            "app.agent.round-limit-stops",
            "app.agent.financial-number-requires-runref",
            "app.agent.historical-fact-no-rerun",
            "app.agent.fact-value-mismatch-blocked",
            "app.agent.approval-number-requires-fact",
        }
        self.assertTrue(required_regressions.issubset({case["id"] for case in cases}))
        serialized = json.dumps(cases, ensure_ascii=False).lower()
        for forbidden in ("api_key", "access_token", "password", "ifind_http", '"wind"', "tinyshare", "tushare"):
            self.assertNotIn(forbidden, serialized)

    def test_case_loader_enforces_declared_schema_contract(self) -> None:
        original = runner.CASES
        with tempfile.TemporaryDirectory() as temporary:
            case_dir = Path(temporary)
            invalid = [{
                "id": "Invalid ID", "area": "x", "description": "x", "target": "unknown",
                "input": {}, "expect": {"unknown_assertion": {}},
            }]
            (case_dir / "invalid.json").write_text(json.dumps(invalid), encoding="utf-8")
            runner.CASES = case_dir
            try:
                with self.assertRaises(ValueError):
                    runner.load_cases()
            finally:
                runner.CASES = original

    def test_schema_target_enum_matches_runner(self) -> None:
        schema = json.loads((ROOT / "evals" / "case.schema.json").read_text(encoding="utf-8"))
        declared = set(schema["properties"]["target"]["enum"])
        self.assertEqual(declared, runner.TARGETS)

    def test_new_evals_never_use_legacy_attestation(self) -> None:
        sources = "\n".join(
            (ROOT / "evals" / name).read_text(encoding="utf-8")
            for name in ("runner.py", "support.py")
        )
        self.assertNotIn("attest_legacy_module_run_ref", sources)

    def test_current_delivery_and_pricer_fixtures_use_public_continuous_and_typed_calendar_contracts(self) -> None:
        runtime = runner.EvalRuntime()
        try:
            designer = runtime.execute("tool.call", {
                "module": "designer",
                "request": {
                    "action": "render", "payload": "$DESIGNER_PAYLOAD",
                    "output_type": "report", "format": "html",
                    "html_report_layout": "continuous", "config": {"allow_pdf": False},
                },
            })
            pricer = runtime.execute("pricer.regression", {"scenario": "low_precision"})
        finally:
            runtime.close()
        self.assertEqual(designer["html_report_layouts"], ["continuous"])
        self.assertEqual(designer["artifact"]["layout"], "brief")
        self.assertEqual(pricer["calendar"]["schema_id"], "trading-calendar")
        self.assertTrue(pricer["calendar"]["content_hash_matches"])
        self.assertTrue(pricer["calendar"]["uses_independent_sessions"])

    def test_full_eval_passes_and_is_repeatable(self) -> None:
        first = runner.run()
        second = runner.run()
        self.assertEqual((first["total"], first["passed"], first["failed"]), (second["total"], second["passed"], second["failed"]))
        self.assertEqual(first["failed"], 0, [item for item in first["results"] if not item["passed"]])
        self.assertEqual(second["failed"], 0, [item for item in second["results"] if not item["passed"]])
        self.assertEqual(
            [item["id"] for item in first["results"] if not item["passed"]],
            [item["id"] for item in second["results"] if not item["passed"]],
        )

    def test_frozen_payoff_assets_are_not_mutated(self) -> None:
        roots = [ROOT / "modules" / "payoffer" / "figures" / kind for kind in ("json", "svg")]
        before = [_tree_hash(root) for root in roots]
        outcome = runner.run("payoffer.preview.call")
        self.assertEqual(outcome["total"], 1)
        self.assertEqual(before, [_tree_hash(root) for root in roots])


if __name__ == "__main__":
    unittest.main()
