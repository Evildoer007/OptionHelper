"""Card PDF formula layout regression.

The HTML renderer owns MathML.  The PDF projection must keep the equivalent
formula inline and must never expose raw underscore notation or detached
formula-only paragraphs.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "modules" / "designer" / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from modules.designer import render
from modules.designer.models import DesignerInput
from modules.designer.pdf_renderer import _extract_blocks, _mixed_markup


class CardPdfFormulaLayoutTests(unittest.TestCase):
    def test_mathml_stays_inline_as_subscripted_pdf_text(self) -> None:
        payload = {
            "meta": {"title": "公式Card"},
            "recommendation": {
                "structure_name": "熊市看涨价差",
                "underlyings": "000300.SH",
                "reason": "当S_T低于K_1时，净权利金P_net仍是主要风险边界。",
            },
            "pricing": {"status": "not_run"},
            "backtest": {"status": "not_run"},
            "risk": {"items": ["到期价格高于K_2时收益封顶。"]},
        }
        html = render(DesignerInput(payload=payload, output_type="card"))["html"]
        blocks = _extract_blocks(html)
        paragraphs = [str(raw) for kind, _level, raw in blocks if kind == "paragraph"]
        reason = next(value for value in paragraphs if value.startswith("当"))

        self.assertIn("S\ue000T\ue001", reason)
        self.assertIn("K\ue0001\ue001", reason)
        self.assertIn("P\ue000net\ue001", reason)
        self.assertFalse(any(value in {"ST", "K1", "Pnet"} for value in paragraphs))
        self.assertNotRegex(reason, r"(?:S_T|K_1|P_net)")
        markup = _mixed_markup(reason, latin_font="Arial", cjk_font="CJK")
        self.assertIn('<sub><font name="Arial">T</font></sub>', markup)
        self.assertIn('<sub><font name="Arial">net</font></sub>', markup)


if __name__ == "__main__":
    unittest.main()
