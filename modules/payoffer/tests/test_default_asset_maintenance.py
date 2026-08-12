"""已批准默认资产维护流程回归。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from dataclasses import asdict
from xml.etree import ElementTree


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "payoffer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.contracts.contract_api import load_registry, resolve_contract
from modules.payoffer.asset_resolver import build_visual_template
from modules.payoffer.impl.engine import render_paths
from modules.payoffer.impl.svg_renderer import render_svg


def _load_manager():
    path = PROJECT_ROOT / "modules" / "payoffer" / "maintenance" / "default_asset_manager.py"
    spec = importlib.util.spec_from_file_location("payoffer_default_asset_manager_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


class DefaultAssetMaintenanceTests(unittest.TestCase):
    def test_confirmed_refresh_updates_only_914_and_925_and_archives_previous_assets(self) -> None:
        manager = _load_manager()
        source = PROJECT_ROOT / "modules" / "payoffer" / "figures"
        source_before = _snapshot(source)
        published_history = (
            PROJECT_ROOT / "modules" / "payoffer" / "maintenance" / "history"
            / "approved_optionreg_914_925_20260807"
        )
        self.assertTrue((published_history / "manifest.json").is_file())

        with tempfile.TemporaryDirectory() as temporary:
            figures = Path(temporary) / "figures"
            shutil.copytree(source, figures)
            for name in ("救生艇雪球", "保底DCN"):
                for suffix in ("json", "svg"):
                    shutil.copyfile(
                        published_history / "before" / f"{name}.{suffix}",
                        figures / suffix / f"{name}.{suffix}",
                    )
            planned = manager.plan_default_asset_refresh(figures)
            self.assertEqual({item["product_id"] for item in planned}, {"9.14", "9.25"})
            record = manager.publish_default_assets("test_approved_refresh", figures)
            self.assertEqual({item["product_id"] for item in record["updates"]}, {"9.14", "9.25"})
            history = figures.parent / "maintenance" / "history" / "test_approved_refresh"
            self.assertTrue((history / "manifest.json").is_file())
            for name in ("救生艇雪球", "保底DCN"):
                self.assertTrue((history / "before" / f"{name}.json").is_file())
                self.assertTrue((history / "before" / f"{name}.svg").is_file())
                payload = json.loads((figures / "json" / f"{name}.json").read_text(encoding="utf-8"))
                self.assertEqual(payload["visual_template"]["schema_version"], 1)
                ElementTree.fromstring((figures / "svg" / f"{name}.svg").read_text(encoding="utf-8"))

        self.assertEqual(source_before, _snapshot(source), "测试不得修改正式默认资产")

    def test_published_914_and_925_assets_match_optionreg_and_deterministic_svg(self) -> None:
        figures = PROJECT_ROOT / "modules" / "payoffer" / "figures"
        registry = load_registry()
        expected_fields = {"9.14": "terms", "9.25": "paths"}
        for product_id, field in expected_fields.items():
            product = registry["products"][product_id]
            name_zh = product["identity"]["name_zh"]
            payload = json.loads((figures / "json" / f"{name_zh}.json").read_text(encoding="utf-8"))
            self.assertEqual(payload[field], product[field])
            self.assertEqual(payload["visual_template"], build_visual_template(product["paths"]))
            contract = resolve_contract(product_id)
            rendered = render_svg({
                "name_zh": name_zh,
                "paths": [asdict(path) for path in render_paths(contract, payload["visual_template"])],
            })
            self.assertEqual(rendered, (figures / "svg" / f"{name_zh}.svg").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
