"""Keep the public seven-chapter Report contract aligned across documentation."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]
HEADINGS = "核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示"
REPORT_DOCUMENTS = (
    "SKILL.md",
    "README.md",
    "CONTEXT.md",
    "packaging/skill/SKILL_README.md",
    "modules/recommender/module-guide.md",
    "modules/reporter/module-guide.md",
    "modules/designer/module-guide.md",
    "modules/reporter/references/report-request-contract.md",
    "modules/designer/references/design-system.md",
    "modules/designer/references/output-formats.md",
    "modules/designer/references/report-data-contract.md",
)


class ReportSevenChapterContractTests(unittest.TestCase):
    def test_report_documents_use_the_fixed_seven_chapter_contract(self) -> None:
        for relative in REPORT_DOCUMENTS:
            with self.subTest(document=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn(HEADINGS, text)
                self.assertNotIn("研究逻辑", text)
                self.assertIn("不提供目录版", text)

    def test_all_module_guides_exclude_the_removed_research_logic_chapter(self) -> None:
        for guide in (ROOT / "modules").glob("*/module-guide.md"):
            with self.subTest(guide=guide.parent.name):
                self.assertNotIn("研究逻辑", guide.read_text(encoding="utf-8"))

    def test_card_contract_is_unchanged(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("1.推荐结构与标的；2.推荐依据；3.估值定价；4.历史回测摘要；5.最多2项风险提示", skill)

        card_content = "推荐结构与标的、推荐依据、估值定价、历史回测"
        for relative in (
            "modules/recommender/module-guide.md",
            "modules/reporter/module-guide.md",
            "modules/reporter/references/report-request-contract.md",
            "modules/designer/module-guide.md",
            "modules/designer/references/output-formats.md",
        ):
            with self.subTest(card_document=relative):
                text = (ROOT / relative).read_text(encoding="utf-8").replace("已冻结的", "")
                self.assertIn(card_content, text)
                self.assertIn("最多2项风险提示", text)


if __name__ == "__main__":
    unittest.main()
