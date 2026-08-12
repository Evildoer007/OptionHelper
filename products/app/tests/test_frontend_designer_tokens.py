"""App CSS compatibility aliases must follow Designer's public token contract."""

from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest


PROJECT = Path(__file__).resolve().parents[3]
DESIGNER_SRC = PROJECT / "modules" / "designer" / "src"
if str(DESIGNER_SRC) not in sys.path:
    sys.path.insert(0, str(DESIGNER_SRC))

from modules.designer.design_tokens import DESIGN_SYSTEM_VERSION, TOKENS


class FrontendDesignerTokenTests(unittest.TestCase):
    def test_app_aliases_match_the_authoritative_designer_tokens(self) -> None:
        css = (PROJECT / "products" / "app" / "frontend" / "shared" / "styles.css").read_text(encoding="utf-8")
        root = css.split(":root {", 1)[1].split("}", 1)[0]
        declarations = dict(re.findall(r"--([\w-]+):\s*([^;]+);", root))
        expected = {
            "ink": TOKENS.colors["ink"],
            "muted": TOKENS.colors["muted"],
            "paper": TOKENS.colors["paper"],
            "canvas": TOKENS.colors["ground"],
            "surface": TOKENS.colors["surface"],
            "line": TOKENS.colors["rule"],
            "line-strong": TOKENS.colors["rule_strong"],
            "red": TOKENS.colors["brand_red"],
            "red-deep": TOKENS.colors["brand_red_deep"],
            "red-rail": TOKENS.colors["brand_red_deep"],
            "red-pale": TOKENS.colors["brand_red_soft"],
            "gold": TOKENS.colors["risk_gold"],
            "gold-pale": TOKENS.colors["risk_gold_soft"],
            "font": TOKENS.fonts["sans"],
            "radius-card": TOKENS.radii["lg"],
            "radius-control": TOKENS.radii["md"],
        }
        self.assertEqual({name: declarations.get(name) for name in expected}, expected)
        self.assertIn(f"Designer {DESIGN_SYSTEM_VERSION}", css)
        self.assertNotIn("--color-brand-red:", root, "App must not duplicate Designer's canonical variable namespace")


if __name__ == "__main__":
    unittest.main()
