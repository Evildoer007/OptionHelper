"""Payoffer对目标OptionReg的运行回归测试。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from collections import Counter
from xml.etree import ElementTree
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]

import sys

for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "payoffer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.contracts.contract_api import PricePath, evaluate_payoff
from modules.payoffer.impl import engine as payoff_engine
from modules.payoffer.impl.engine import build_payoff_input, default_figure_payload, figure_asset_paths, load_default_figure_payload, load_registry, preview_payload, run_runtime, term_overrides_from_default_json
from modules.payoffer.impl.svg_renderer import _visible_thresholds, render_svg
from modules.payoffer.service import _name_from_body, catalog_payload


def _name(product_id: str) -> str:
    return load_registry()["products"][product_id]["identity"]["name_zh"]


class PythonPayoffRuntimeTests(unittest.TestCase):
    def test_page_catalog_and_runtime_request_use_product_id(self) -> None:
        catalog = catalog_payload()
        call = next(row for row in catalog["products"] if row["product_id"] == "2.1")
        self.assertEqual(call["canonical_name"], "看涨期权")
        self.assertEqual(_name_from_body({"product_id": "2.1"}), "看涨期权")

    def test_catalog_reads_target_product_shape(self) -> None:
        registry = load_registry()
        self.assertEqual(len(registry["products"]), 65)
        for product_id, product in registry["products"].items():
            self.assertEqual(set(product), {"identity", "terms", "paths"}, product_id)
            self.assertIs(product["identity"]["entry_status"], True, product_id)
        self.assertEqual(preview_payload("看涨期权")["runtime_status"], "enabled")
        self.assertEqual(preview_payload("助推器")["runtime_status"], "enabled")

    def test_vanilla_call_recalculates_from_resolved_payoff_input(self) -> None:
        contract = build_payoff_input("看涨期权", {"K": 110.0, "Pi_0": 7.0, "n_C": 2.0})
        outcome = evaluate_payoff(
            contract,
            PricePath.from_values([100.0, 130.0], times=[0.0, contract.terms["T"]], asset_ids=contract.underlyings),
        )
        # $P$为每份期权费；两份看涨的期初期权费和到期支付均须按$n_C=2$缩放。
        self.assertEqual(outcome.pnl, 26.0)
        payload = preview_payload("看涨期权", {"K": 110.0, "Pi_0": 7.0, "n_C": 2.0})
        self.assertEqual(len(payload["paths"]), 1)
        self.assertIn("<svg", render_svg(payload))

    def test_runtime_writes_only_parameterized_result_and_preserves_default_svg(self) -> None:
        formal = figure_asset_paths("看涨期权")["svg"]
        before = hashlib.sha256(formal.read_bytes()).hexdigest()
        original_result = payoff_engine.RESULT_ROOT
        with tempfile.TemporaryDirectory() as temporary:
            payoff_engine.RESULT_ROOT = Path(temporary)
            try:
                result = run_runtime("看涨期权", {"K": 105.0}, None, "python_test", "run_001")
                destination = Path(result["destination"])
                self.assertTrue((destination / "artifacts" / "payoff.svg").is_file())
                self.assertTrue((destination / "artifacts" / "payoff.json").is_file())
            finally:
                payoff_engine.RESULT_ROOT = original_result
        self.assertTrue(result["ok"])
        self.assertEqual(before, hashlib.sha256(formal.read_bytes()).hexdigest())

    def test_default_assets_use_unique_chinese_names_and_expose_no_internal_number(self) -> None:
        registry = load_registry()
        for product in registry["products"].values():
            name_zh = product["identity"]["name_zh"]
            asset = default_figure_payload(name_zh)
            stored_asset = load_default_figure_payload(name_zh)
            self.assertEqual(set(asset), {"name_zh", "terms", "paths", "visual_template"})
            self.assertEqual(asset["name_zh"], name_zh)
            self.assertIn("terms", stored_asset)
            self.assertIn("paths", stored_asset)
            self.assertIn("visual_template", stored_asset)
            self.assertNotIn("product_id", json.dumps(asset, ensure_ascii=False))
            paths = figure_asset_paths(name_zh)
            self.assertEqual(paths["json"].name, f"{name_zh}.json")
            self.assertEqual(paths["svg"].name, f"{name_zh}.svg")
            self.assertEqual(paths["json"].parent.name, "json")
            self.assertEqual(paths["svg"].parent.name, "svg")
            self.assertTrue(paths["json"].is_file())
            self.assertTrue(paths["svg"].is_file())

    def test_payoffer_uses_default_json_before_applying_runtime_overrides(self) -> None:
        default = load_default_figure_payload("看涨期权")
        self.assertEqual(default["terms"]["K"], 100.0)
        edited = {**default, "terms": {**default["terms"], "K": 105.0}}
        overrides = term_overrides_from_default_json("看涨期权", edited)
        self.assertEqual(overrides, {"K": 105.0})
        contract = build_payoff_input("看涨期权", overrides)
        self.assertEqual(contract.terms["K"], 105.0)
        self.assertEqual(contract.term_sources["K"], "override")

    def test_payoffer_rejects_derived_rule_edits(self) -> None:
        default = load_default_figure_payload("方差互换")
        edited = {**default, "terms": {**default["terms"], "derived_terms": {"G": "0"}}}
        with self.assertRaisesRegex(Exception, "不能修改固定规则字段derived_terms"):
            term_overrides_from_default_json("方差互换", edited)

    def test_newly_enabled_structured_product_builds_payoff_input(self) -> None:
        contract = build_payoff_input("助推器", {"alpha": 2.5, "Lmax": 0.15})
        self.assertEqual(contract.product_id, "9.29")
        self.assertEqual(contract.terms["alpha"], 2.5)
        self.assertEqual(contract.terms["Lmax"], 0.15)

    def test_runtime_svg_uses_formal_visual_system_without_raw_formula_text(self) -> None:
        svg = render_svg(preview_payload("看涨期权"))
        self.assertIn('fill="url(#everbright-red-gold)"', svg)
        self.assertIn('class="card-accent"', svg)
        self.assertIn('class="axis-arrow"', svg)
        self.assertIn('data-global-legend="true"', svg)
        self.assertIn('data-path-card="0"', svg)
        self.assertIn('width="1200"', svg)
        self.assertIn('width="1168"', svg)
        self.assertNotIn("条件：", svg)
        self.assertNotIn("损益：", svg)
        self.assertNotIn("路径1 收益图", svg)
        ElementTree.fromstring(svg)

    def test_runtime_svg_text_baselines_stay_inside_each_path_card(self) -> None:
        svg = render_svg(preview_payload("凤凰式结构"))
        root = ElementTree.fromstring(svg)
        for group in root.iter():
            if not group.tag.endswith("g") or "data-path-card" not in group.attrib:
                continue
            rectangle = next(node for node in group if node.tag.endswith("rect") and node.attrib.get("class") == "card")
            card_top = float(rectangle.attrib["y"])
            card_bottom = card_top + float(rectangle.attrib["height"])
            for node in group.iter():
                if not node.tag.endswith("text") or "y" not in node.attrib:
                    continue
                self.assertGreaterEqual(float(node.attrib["y"]), card_top, node.text)
                self.assertLessEqual(float(node.attrib["y"]), card_bottom, node.text)

    def test_cash_binary_uses_vertical_jump_and_open_closed_endpoints(self) -> None:
        svg = render_svg(preview_payload("现金二元看涨"))
        self.assertIn('class="payoff-jump"', svg)
        self.assertIn('curve-endpoint open', svg)
        self.assertIn('curve-endpoint closed', svg)

    def test_continuous_call_has_no_spurious_jump_or_endpoints(self) -> None:
        svg = render_svg(preview_payload("看涨期权"))
        self.assertNotIn('class="payoff-jump"', svg)
        self.assertNotIn('<circle class="curve-endpoint', svg)

    def test_non_price_axis_uses_registered_domain_variable(self) -> None:
        self.assertEqual(preview_payload("安全气囊Airbag（上涨收益封顶）")["paths"][1]["axis"]["unit"], "r_T")
        self.assertEqual(preview_payload("方差互换")["paths"][0]["axis"]["unit"], "sigma_realized")
        self.assertEqual(preview_payload("区间计息（Range Accrual）")["paths"][0]["axis"]["unit"], "n_in")

    def test_named_products_cover_required_payoff_categories(self) -> None:
        product_ids = {
            "vanilla_call": "2.1", "vanilla_put": "2.2", "cash_digital": "6.1",
            "asset_digital": "6.3", "straddle": "4.1", "condor": "4.4",
            "range_accrual": "10.8", "variance_swap": "10.4", "worst_of": "9.18",
            "path_snowball": "9.1",
        }
        for label, product_id in product_ids.items():
            with self.subTest(label=label, product_id=product_id):
                payload = preview_payload(_name(product_id))
                self.assertEqual(payload["runtime_status"], "enabled")
                self.assertTrue(payload["paths"])
                ElementTree.fromstring(render_svg(payload))

    def test_parachute_snowball_final_knockout_has_priority_over_daily_knockin(self) -> None:
        contract = build_payoff_input("降落伞雪球")
        self.assertEqual(contract.terms["H_KO_regular"], 103.0)
        self.assertEqual(contract.terms["H_KO_final"], 70.0)
        self.assertIn("tau_out_1", contract.terms["monitor"])
        self.assertIn("tau_out_2", contract.terms["monitor"])
        self.assertNotIn("ko_barrier_schedule", contract.terms)

        times = np.linspace(0.0, contract.terms["T"], 505)
        dates = pd.bdate_range(end="2026-01-30", periods=len(times))
        final_knockout = np.full(505, 100.0)
        final_knockout[-1] = 70.0
        outcome = evaluate_payoff(
            contract,
            PricePath.from_values(final_knockout, times=times, dates=dates, asset_ids=contract.underlyings),
        )
        self.assertEqual(outcome.selected_path, 1)
        self.assertEqual(outcome.selected_case, 0)
        self.assertAlmostEqual(outcome.pnl, 3_000_000.0)

        below_final_barrier = final_knockout.copy()
        below_final_barrier[-1] = 65.0
        outcome = evaluate_payoff(
            contract,
            PricePath.from_values(below_final_barrier, times=times, dates=dates, asset_ids=contract.underlyings),
        )
        self.assertEqual(outcome.selected_path, 3)
        self.assertEqual(outcome.selected_case, 0)
        self.assertAlmostEqual(outcome.pnl, -3_500_000.0)

    def test_all_65_rendered_svg_layouts_keep_annotations_inside_cards(self) -> None:
        registry = load_registry()
        self.assertEqual(len(registry["products"]), 65)
        for product_id in registry["products"]:
            with self.subTest(product_id=product_id):
                root = ElementTree.fromstring(render_svg(preview_payload(_name(product_id))))
                for group in root.iter():
                    if not group.tag.endswith("g") or "data-path-card" not in group.attrib:
                        continue
                    rectangle = next(node for node in group if node.tag.endswith("rect") and node.attrib.get("class") == "card")
                    card_left = float(rectangle.attrib["x"])
                    card_top = float(rectangle.attrib["y"])
                    card_right = card_left + float(rectangle.attrib["width"])
                    card_bottom = card_top + float(rectangle.attrib["height"])
                    axis_lines = [node for node in group.iter() if node.tag.endswith("line") and node.attrib.get("class") == "axis"]
                    plot_top = min(float(node.attrib["y1"]) for node in axis_lines if float(node.attrib["x1"]) == float(node.attrib["x2"]))
                    labels_by_row: dict[float, list[tuple[float, float]]] = {}
                    for node in group.iter():
                        if not node.tag.endswith("text") or "y" not in node.attrib:
                            continue
                        x, y = float(node.attrib.get("x", card_left)), float(node.attrib["y"])
                        self.assertGreaterEqual(x, card_left, node.text)
                        self.assertLessEqual(x, card_right, node.text)
                        self.assertGreaterEqual(y, card_top, node.text)
                        self.assertLessEqual(y, card_bottom, node.text)
                        if node.attrib.get("class") == "cn threshold-value":
                            self.assertLess(y, plot_top, node.text)
                            width = max(30.0, 12.0 + len(node.text or "") * 7.2)
                            labels_by_row.setdefault(y, []).append((x - width / 2.0, x + width / 2.0))
                    for intervals in labels_by_row.values():
                        ordered = sorted(intervals)
                        for (_, left_end), (right_start, _) in zip(ordered, ordered[1:]):
                            self.assertGreaterEqual(right_start - left_end, 7.0)

    def test_threshold_numbers_are_in_annotation_band_and_legend_is_global(self) -> None:
        root = ElementTree.fromstring(render_svg(preview_payload("鹰式")))
        for group in root.iter():
            if not group.tag.endswith("g") or "data-path-card" not in group.attrib:
                continue
            card = next(node for node in group if node.tag.endswith("rect") and node.attrib.get("class") == "card")
            card_top = float(card.attrib["y"])
            threshold_text = [node for node in group.iter() if node.tag.endswith("text") and node.attrib.get("class") == "cn threshold-value"]
            self.assertTrue(threshold_text)
            axis_lines = [node for node in group.iter() if node.tag.endswith("line") and node.attrib.get("class") == "axis"]
            plot_top = min(float(node.attrib["y1"]) for node in axis_lines if float(node.attrib["x1"]) == float(node.attrib["x2"]))
            for node in threshold_text:
                self.assertGreater(float(node.attrib["y"]), card_top)
                self.assertLess(float(node.attrib["y"]), plot_top)
            self.assertNotIn("global-legend", "".join(node.attrib.get("class", "") for node in group.iter()))

    def test_axis_uses_clean_display_boundaries_and_hides_reference_threshold(self) -> None:
        payload = preview_payload("看涨期权")
        path = payload["paths"][0]
        self.assertEqual(path["scale"]["x_max"], 180.0)
        self.assertNotIn(173.6, path["scale"].values())
        self.assertTrue(all(float(item["value"]) != 100.0 for item in _visible_thresholds(path)))
        self.assertNotIn('class="cn range-label"', render_svg(payload))

    def test_horizontal_payoff_levels_and_continuous_turns_use_separate_visual_marks(self) -> None:
        payload = preview_payload("看涨期权")
        path = payload["paths"][0]
        self.assertEqual(path["payoff_levels"], [{"value": -5.0}])
        self.assertEqual(path["turning_points"], [{"x": 100.0, "y": -5.0}])
        svg = render_svg(payload)
        self.assertIn('class="payoff-guide"', svg)
        self.assertIn('class="curve-turn"', svg)
        self.assertNotIn('class="payoff-jump"', svg)

    def test_all_discontinuous_segments_preserve_domain_open_closed_semantics(self) -> None:
        """每个实际跳变都必须以定义域比较符决定端点的空实状态。"""
        jump_count = 0
        for product_id in load_registry()["products"]:
            with self.subTest(product_id=product_id):
                payload = preview_payload(_name(product_id))
                expected_markers = {
                    (str(point["endpoint"]), round(float(point["x"]), 8), round(float(point["y"]), 8))
                    for path in payload["paths"]
                    for segment in path["segments"]
                    for point in (segment[0], segment[-1])
                    if point.get("endpoint") in {"open", "closed"}
                }
                expected = Counter(kind for kind, _, _ in expected_markers)
                root = ElementTree.fromstring(render_svg(payload))
                actual = Counter(
                    "open" if node.attrib.get("class") == "curve-endpoint open" else "closed"
                    for node in root.iter()
                    if node.tag.endswith("circle") and node.attrib.get("class") in {"curve-endpoint open", "curve-endpoint closed"}
                )
                self.assertEqual(actual, expected)
                for path in payload["paths"]:
                    for jump in path["jumps"]:
                        endpoints = [
                            point["endpoint"]
                            for segment in path["segments"]
                            for point in (segment[0], segment[-1])
                            if abs(float(point["x"]) - float(jump["x"])) < 1e-8 and point.get("endpoint") in {"open", "closed"}
                        ]
                        self.assertGreaterEqual(len(endpoints), 2)
                        self.assertTrue(all(kind in {"open", "closed"} for kind in endpoints))
                        jump_count += 1
        self.assertGreater(jump_count, 0)


if __name__ == "__main__":
    unittest.main()
