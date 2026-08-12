"""默认JSON视觉模板与运行时绑定回归。"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "payoffer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.payoffer.asset_resolver import figure_asset_paths, load_default_figure_payload
from modules.payoffer.impl.engine import build_payoff_input, load_registry, render_paths


class DefaultTemplateTests(unittest.TestCase):
    def test_all_65_default_templates_parse_and_bind_every_registered_path(self) -> None:
        registry = load_registry()
        self.assertEqual(len(registry["products"]), 65)
        for product in registry["products"].values():
            name_zh = product["identity"]["name_zh"]
            with self.subTest(name_zh=name_zh):
                template = load_default_figure_payload(name_zh)["visual_template"]
                self.assertEqual(template["schema_version"], 1)
                self.assertEqual(len(template["path_views"]), len(product["paths"]))
                self.assertEqual(
                    [view["path_index"] for view in template["path_views"]],
                    list(range(1, len(product["paths"]) + 1)),
                )

    def test_runtime_render_uses_template_panel_title_without_mutating_default_json(self) -> None:
        name_zh = "看涨期权"
        json_path = figure_asset_paths(name_zh)["json"]
        before = hashlib.sha256(json_path.read_bytes()).hexdigest()
        template = deepcopy(load_default_figure_payload(name_zh)["visual_template"])
        template["path_views"][0]["title"] = "模板面板"

        paths = render_paths(build_payoff_input(name_zh), template)

        self.assertEqual(paths[0].title, "模板面板")
        self.assertEqual(before, hashlib.sha256(json_path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
