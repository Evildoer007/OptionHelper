from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import shutil
import tempfile
import unittest

from tests.knowledger.audit import audit_candidate


ROOT = Path(__file__).resolve().parents[2]
SOURCES = ("optionlist.md", "optionlib.md", "optionreg.py")


def _copy_sources(target: Path) -> Path:
    references = target / "references"
    references.mkdir(parents=True)
    for name in SOURCES:
        shutil.copy2(ROOT / "references" / name, references / name)
    return target


def _hashes(root: Path) -> dict[str, str]:
    return {name: sha256((root / "references" / name).read_bytes()).hexdigest() for name in SOURCES}


class CandidateFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_candidate_validation_is_read_only(self) -> None:
        baseline = _copy_sources(self.temp_path / "baseline")
        candidate = _copy_sources(self.temp_path / "candidate")
        before = {"baseline": _hashes(baseline), "candidate": _hashes(candidate)}
        report = audit_candidate(baseline, candidate)
        after = {"baseline": _hashes(baseline), "candidate": _hashes(candidate)}
        self.assertEqual(before, after)
        self.assertTrue(report.changes["read_only_hashes_unchanged"])
        self.assertFalse([issue for issue in report.issues if issue.severity == "error"])

    def test_new_product_must_be_added_to_all_three_sources(self) -> None:
        baseline = _copy_sources(self.temp_path / "baseline")
        candidate = _copy_sources(self.temp_path / "candidate")
        path = candidate / "references" / "optionlist.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n| 66 | 临时候选 | 11.1 | 其他 | 已录入 |\n", encoding="utf-8")
        report = audit_candidate(baseline, candidate)
        self.assertTrue(any(issue.code == "candidate.addition_incomplete" for issue in report.issues))

    def test_existing_product_may_have_single_source_runtime_correction(self) -> None:
        baseline = _copy_sources(self.temp_path / "baseline")
        candidate = _copy_sources(self.temp_path / "candidate")
        path = candidate / "references" / "optionreg.py"
        text = path.read_text(encoding="utf-8")
        old = "cash(T, N * (c * n_coupon + max(S_T / S_0 - 1, F - 1)))"
        new = "cash(T,N*(c*n_coupon+max(S_T/S_0-1,F-1)))"
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        report = audit_candidate(baseline, candidate)
        self.assertEqual(report.changes["changed"]["optionreg"], ["9.25"])
        self.assertFalse([issue for issue in report.issues if issue.code == "candidate.change_scope_mismatch"])
        self.assertFalse([issue for issue in report.issues if issue.severity == "error"])

    def test_ast_gate_detects_duplicate_literal_key(self) -> None:
        baseline = _copy_sources(self.temp_path / "baseline")
        candidate = _copy_sources(self.temp_path / "candidate")
        path = candidate / "references" / "optionreg.py"
        text = path.read_text(encoding="utf-8")
        old = 'REGISTRY = {\n    "term_catalog": TERM_CATALOG,\n    "products": PRODUCTS,\n}'
        new = 'REGISTRY = {\n    "term_catalog": TERM_CATALOG,\n    "term_catalog": TERM_CATALOG,\n    "products": PRODUCTS,\n}'
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        report = audit_candidate(baseline, candidate)
        self.assertTrue(any(issue.code == "reg.duplicate_literal_key" for issue in report.issues))

    def test_category_and_sequence_injections_are_rejected(self) -> None:
        baseline = _copy_sources(self.temp_path / "baseline")
        category_candidate = _copy_sources(self.temp_path / "category")
        path = category_candidate / "references" / "optionlist.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("| 1 | 看涨期权 | 2.1 | 方向性期权 |", "| 1 | 看涨期权 | 2.1 | 其他 |", 1), encoding="utf-8")
        report = audit_candidate(baseline, category_candidate)
        self.assertTrue(any(issue.code == "cross.category_mismatch" and issue.product_id == "2.1" for issue in report.issues))

        sequence_candidate = _copy_sources(self.temp_path / "sequence")
        path = sequence_candidate / "references" / "optionlist.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("| 2 | 看跌期权 |", "| 3 | 看跌期权 |", 1), encoding="utf-8")
        report = audit_candidate(baseline, sequence_candidate)
        self.assertTrue(any(issue.code == "list.sequence_invalid" for issue in report.issues))

    def test_module_config_fields_are_rejected(self) -> None:
        baseline = _copy_sources(self.temp_path / "baseline")
        candidate = _copy_sources(self.temp_path / "candidate")
        path = candidate / "references" / "optionreg.py"
        text = path.read_text(encoding="utf-8")
        marker = "'2.1': {'identity':"
        self.assertIn(marker, text)
        product_start = text.index(marker)
        terms_start = text.index("'terms': {", product_start) + len("'terms': {")
        text = text[:terms_start] + "'pricing_config': {'method': 'mock'}, " + text[terms_start:]
        path.write_text(text, encoding="utf-8")
        report = audit_candidate(baseline, candidate)
        self.assertTrue(any(
            issue.code == "reg.module_config_forbidden" and issue.product_id == "2.1"
            for issue in report.issues
        ))


if __name__ == "__main__":
    unittest.main()
