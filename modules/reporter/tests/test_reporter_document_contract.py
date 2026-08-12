"""Reporter公开文档与当前固定交付契约一致。"""

from __future__ import annotations

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONTRACT = PROJECT_ROOT / "modules" / "reporter" / "references" / "report-request-contract.md"
GUIDE = PROJECT_ROOT / "modules" / "reporter" / "module-guide.md"


class ReporterDocumentContractTests(unittest.TestCase):
    def test_reporter_documents_match_the_fixed_seven_section_delivery_shape(self) -> None:
        documents = (CONTRACT.read_text(encoding="utf-8"), GUIDE.read_text(encoding="utf-8"))

        expected_report_sections = (
            "核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示"
        )
        for document in documents:
            self.assertIn(expected_report_sections, document)
            self.assertNotIn("研究逻辑", document)
            self.assertNotIn("带窄幅阅读目录", document)
        self.assertIn("Report HTML仅支持连续版", documents[0])
        self.assertIn("Card无目录、无损益图", documents[0])


if __name__ == "__main__":
    unittest.main()
