from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.knowledger.audit import audit_repository


ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = ROOT / "core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from runtime.contracts.contract_engine import (  # noqa: E402
    ContractResolutionError,
    PricePath,
    evaluate_payoff,
    resolve_contract,
)


class RepositoryIntegrityTests(unittest.TestCase):
    def test_current_repository_has_no_high_confidence_error(self) -> None:
        report = audit_repository(ROOT)
        self.assertEqual(report.counts, {"optionlist": 65, "optionlib": 65, "optionreg": 65})
        self.assertEqual(report.exit_code(), 0)
        self.assertFalse([issue for issue in report.issues if issue.severity == "error"])
        self.assertEqual(report.coverage["default_assignments_checked"], 327)
        self.assertEqual(report.coverage["not_fully_proven_cross_source"]["condition_domain_pnl_equivalence"], 65)

    def test_release_gate_has_no_machine_blocker_and_preserves_manual_reviews(self) -> None:
        report = audit_repository(ROOT)
        blockers = [issue for issue in report.issues if issue.severity == "release_blocker"]
        self.assertEqual(report.exit_code(strict_release=True), 0)
        self.assertEqual(blockers, [])
        self.assertFalse(any(issue.code.startswith("schedule.weekly") for issue in blockers))
        segment_products = {
            issue.product_id for issue in report.issues if issue.code == "cross.segment_granularity"
        }
        self.assertEqual(segment_products, {"9.5", "9.8", "9.9", "9.12", "9.14", "10.4"})
        self.assertFalse([
            issue for issue in report.issues
            if issue.product_id == "9.27" and issue.code in {"cross.segment_granularity", "lib.example_coverage"}
        ])
        self.assertTrue(any(
            issue.code == "cross.symbol_manual_review" and issue.product_id == "9.12"
            for issue in report.issues
        ))

    def test_cli_strict_gate_writes_readable_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "release-audit.md"
            command = [
                sys.executable, "-m", "tests.knowledger.audit", "audit", "--root", str(ROOT),
                "--format", "markdown", "--strict-release", "--output", str(output),
            ]
            completed = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0)
            rendered = output.read_text(encoding="utf-8")
            self.assertIn("严格发布阻断", rendered)
            self.assertIn("严格发布阻断：0项", rendered)
            self.assertIn("不代表65个产品", rendered)

    def test_formal_runtime_sources_have_no_weekly_schedule_semantics(self) -> None:
        reg = (ROOT / "references" / "optionreg.py").read_text(encoding="utf-8")
        lib = (ROOT / "references" / "optionlib.md").read_text(encoding="utf-8")
        trigger = (ROOT / "modules" / "payoffer" / "figures" / "json" / "触发器.json").read_text(encoding="utf-8")
        engine = (ROOT / "core" / "src" / "runtime" / "contracts" / "contract_engine.py").read_text(encoding="utf-8")
        for text in (reg, lib, trigger, engine):
            self.assertNotIn("weekly_", text)
            self.assertNotIn("周度观察", text)
            self.assertNotIn("周度敲出", text)
        rejection = (ROOT / "modules" / "backtester" / "src" / "path_replay.py").read_text(encoding="utf-8")
        self.assertIn("weekly_observation_schedule_not_accepted", rejection)

    def test_confirmed_contract_regressions(self) -> None:
        with self.assertRaisesRegex(ContractResolutionError, r"alpha > 0"):
            resolve_contract("7.3", term_overrides={"alpha": 0})
        self.assertAlmostEqual(float(resolve_contract("9.14").terms["c_reset"]), 0.10)

        contract = resolve_contract(
            "9.25",
            identity={"underlyings": ["A"], "contract_reference_spots": {"A": 100.0}},
        )
        path = PricePath.from_values(
            [100.0, 90.0, 90.0],
            times=[0.0, 30 / 365, 1.0],
            dates=["2024-01-02", "2024-01-31", "2024-02-15"],
            asset_ids=["A"],
        )
        result = evaluate_payoff(contract, path)
        self.assertEqual(result.selected_path, 2)
        self.assertEqual(result.monitor_values["n_coupon"], 1)
        self.assertAlmostEqual(result.pnl, -940_000.0, places=6)


if __name__ == "__main__":
    unittest.main()
