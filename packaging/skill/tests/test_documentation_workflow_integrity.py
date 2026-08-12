"""Keep the public workflow documentation aligned across the source tree."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]
HEADINGS = "核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示"


class DocumentationWorkflowIntegrityTests(unittest.TestCase):
    def _read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_authoritative_documents_share_one_delivery_contract(self) -> None:
        documents = (
            "SKILL.md",
            "CONTEXT.md",
            "modules/recommender/module-guide.md",
            "modules/reporter/module-guide.md",
            "modules/designer/module-guide.md",
            "modules/designer/references/output-formats.md",
        )
        for relative in documents:
            with self.subTest(document=relative):
                text = self._read(relative)
                self.assertIn(HEADINGS, text)
                self.assertIn("不提供目录版", text)

    def test_reporter_designer_boundary_is_not_optional(self) -> None:
        skill = self._read("SKILL.md")
        readme = self._read("packaging/skill/SKILL_README.md")
        reporter = self._read("modules/reporter/module-guide.md")
        for text in (skill, readme, reporter):
            self.assertIn("Reporter→Designer", text)
        self.assertIn("不得以“快速版本”或“先做示例”为由创建临时Python、HTML、SVG或PDF", skill)
        self.assertIn("不需要临时`.py`或手工HTML", readme)

    def test_card_geometry_and_scope_are_described_consistently(self) -> None:
        documents = (
            "SKILL.md",
            "CONTEXT.md",
            "modules/reporter/module-guide.md",
            "modules/designer/module-guide.md",
            "modules/designer/references/output-formats.md",
            "modules/designer/references/report-data-contract.md",
        )
        for relative in documents:
            with self.subTest(document=relative):
                text = self._read(relative)
                self.assertIn("210mm宽度、高度随完整内容自然延展", text)
                self.assertIn("无损益图", text)
                self.assertIn("最多2项风险提示", text)

    def test_public_documents_do_not_restore_removed_report_layouts(self) -> None:
        documents = (
            "SKILL.md",
            "CONTEXT.md",
            "modules/recommender/module-guide.md",
            "modules/reporter/module-guide.md",
            "modules/designer/module-guide.md",
            "modules/designer/references/output-formats.md",
            "modules/designer/references/report-data-contract.md",
            "modules/reporter/references/report-request-contract.md",
            "modules/reporter/references/designer-handoff.md",
        )
        for relative in documents:
            with self.subTest(document=relative):
                text = self._read(relative)
                self.assertNotIn("with_toc", text)
                self.assertNotIn("with_toc", text)
        self.assertIn("内部兼容标记", self._read("modules/designer/references/output-formats.md"))

    def test_skill_packaging_documents_describe_the_current_build_boundary(self) -> None:
        repository_readme = self._read("README.md")
        skill_readme = self._read("packaging/skill/SKILL_README.md")
        self.assertIn("可替换的当前候选交付目录", repository_readme)
        self.assertIn("不可覆盖的正式签发归档", repository_readme)
        self.assertIn("不需要临时`.py`或手工HTML", skill_readme)


if __name__ == "__main__":
    unittest.main()
