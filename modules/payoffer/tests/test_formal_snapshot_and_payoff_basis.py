"""Payoffer正式快照、毛收益图及公开JSON专项回归。"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "payoffer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.contracts.contract_api import PayoffInput, evaluate_payoff, resolve_contract
from runtime.contracts.contract_types import deep_thaw, semantic_hash
from modules.payoffer.asset_resolver import load_default_figure_payload
from modules.payoffer.impl.engine import (
    build_payoff_input,
    load_registry,
    path_templates,
    preview_payload,
    render_paths,
    render_payoff,
)
from modules.payoffer.impl.path_sampler import candidate_path
from modules.payoffer.service import PayoffEngineError, _formal_payoff_input


def _published_contract(product_id: str = "2.1"):
    current = resolve_contract(product_id)
    return replace(
        current,
        identity={**deep_thaw(current.identity), "product_version": "v1.0"},
        product_version="v1.0",
        contract_fingerprint="",
    )


def _published_snapshot(contract) -> dict[str, object]:
    product_dir = PROJECT_ROOT / "versions" / "v1.0" / "knowledger" / "products" / contract.product_id
    return {
        "product": load_registry()["products"][contract.product_id],
        "product_version": "v1.0",
        "default_json_path": product_dir / "default-payoff.json",
        "default_svg_path": product_dir / "default-payoff.svg",
    }


class FormalSnapshotBindingTests(unittest.TestCase):
    def test_published_contract_uses_attested_product_snapshot_not_current_registry(self) -> None:
        contract = _published_contract()
        snapshot = _published_snapshot(contract)
        with patch("modules.payoffer.asset_resolver.load_current_product_snapshot", return_value=snapshot):
            payoff_input = _formal_payoff_input({"contract": contract.to_protocol_dict()})
            result = render_payoff(payoff_input)

        self.assertEqual(payoff_input.contract.product_version, "v1.0")
        self.assertEqual(result.payload["default_visual_asset"]["asset_version_class"], "published_catalog_snapshot")
        self.assertEqual(result.payload["default_visual_asset"]["registry_product_version"], "v1.0")

    def test_published_contract_rejects_forged_product_or_path_snapshot(self) -> None:
        contract = _published_contract()
        snapshot = _published_snapshot(contract)
        forged_product = replace(contract, product_snapshot_hash="a" * 64)
        altered_paths = list(deep_thaw(contract.paths))
        altered_paths[0]["cases"][0]["pnl"] = "cash(0, 0)"
        forged_paths = replace(
            contract,
            paths=tuple(altered_paths),
            product_paths_hash=semantic_hash(altered_paths),
            contract_fingerprint="",
        )
        with patch("modules.payoffer.asset_resolver.load_current_product_snapshot", return_value=snapshot):
            with self.assertRaisesRegex(PayoffEngineError, "产品快照哈希"):
                _formal_payoff_input({"contract": forged_product.to_protocol_dict()})
            with self.assertRaisesRegex(PayoffEngineError, "路径快照哈希"):
                _formal_payoff_input({"contract": forged_paths.to_protocol_dict()})

    def test_unversioned_contract_keeps_full_registry_hash_verification(self) -> None:
        current = resolve_contract("2.1")
        forged = replace(current, registry_snapshot_hash="a" * 64)
        with self.assertRaisesRegex(PayoffEngineError, "Registry快照哈希"):
            _formal_payoff_input({"contract": forged.to_protocol_dict()})


class GrossPayoffFigureTests(unittest.TestCase):
    def _assert_gross_points_match_shared_cashflows(self, name_zh: str) -> None:
        contract = build_payoff_input(name_zh)
        panels = render_paths(contract, load_default_figure_payload(name_zh)["visual_template"])
        templates = {template.name: template for template in path_templates(contract)}
        premium = float(contract.terms["N"]) * float(contract.terms["p"])
        checked = 0
        for panel in panels:
            self.assertEqual(panel.payoff_basis, "gross_before_premium")
            self.assertIn("扣费前毛收益", panel.status_note or "")
            template = templates[panel.candidate_state["template"]]
            for segment in panel.segments:
                for point in segment:
                    if point.get("endpoint") == "open":
                        continue
                    price_path = candidate_path(contract, panel.axis["unit"], float(point["x"]), template)
                    net_pnl = evaluate_payoff(contract, price_path).pnl
                    self.assertAlmostEqual(float(point["y"]), net_pnl + premium, places=6)
                    checked += 1
        self.assertGreater(checked, 0)
        rendered = render_payoff(PayoffInput(contract=contract))
        self.assertIn("收益（扣费前）", rendered.svg)

    def test_standard_airbag_uses_gross_before_premium_without_changing_cashflows(self) -> None:
        self._assert_gross_points_match_shared_cashflows("标准安全气囊Airbag")

    def test_range_accrual_uses_gross_before_premium_without_changing_cashflows(self) -> None:
        self._assert_gross_points_match_shared_cashflows("区间计息（Range Accrual）")


class PublicPayloadJsonTests(unittest.TestCase):
    def test_runtime_payloads_are_strict_json_and_default_assets_remain_read_only(self) -> None:
        figures = PROJECT_ROOT / "modules" / "payoffer" / "figures"
        before = {str(path.relative_to(figures)): path.read_bytes() for path in figures.rglob("*") if path.is_file()}
        for name_zh in ("看涨期权", "标准安全气囊Airbag", "区间计息（Range Accrual）", "现金二元看涨"):
            with self.subTest(name_zh=name_zh):
                payload = preview_payload(name_zh)
                self.assertIsInstance(json.dumps(payload, ensure_ascii=False, allow_nan=False), str)
                self.assertIsInstance(json.dumps(render_payoff(PayoffInput(contract=build_payoff_input(name_zh))).payload, ensure_ascii=False, allow_nan=False), str)
        after = {str(path.relative_to(figures)): path.read_bytes() for path in figures.rglob("*") if path.is_file()}
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
