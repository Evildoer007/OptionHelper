"""Continuous HTML contract for exportable Designer deliveries.

The reader may scroll the document itself in a browser, but no report fact may
be hidden in an internal scroll region, a chart zoom control, or an expander.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "modules" / "designer" / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from modules.designer import render
from modules.designer.config import load_designer_config
from modules.designer.models import DesignerInput


def _payload() -> dict:
    return {
        "meta": {"title": "连续交付验证", "as_of_date": "2026-08-11"},
        "pricing": {
            "status": "ready",
            "charts": [{
                "id": "continuous-series",
                "title": "连续数据验证",
                "type": "line",
                "x": ["T0", "T1"],
                "series": [{"name": "数值", "data": [0, 0.01]}],
                "x_axis_name": "观察点",
                "y_axis_name": "数值",
                "source_note": "本次冻结结果",
            }],
        },
        "backtest": {"status": "not_run", "note": "未提供历史回测结果。"},
        "risk": {"items": ["仅用于验证连续文档交付。"]},
    }


class ContinuousDocumentContractTests(unittest.TestCase):
    def test_chart_data_is_complete_and_never_hidden_behind_a_control(self) -> None:
        payload = deepcopy(_payload())
        chart = payload["pricing"]["charts"][0]
        chart["x"] = [f"T{i}" for i in range(72)]
        chart["series"][0]["data"] = [round(index / 100, 2) for index in range(72)]

        html = render(DesignerInput(payload=payload, output_type="report"))["html"]
        for value in ("T0", "T36", "T71", "0.36", "0.71"):
            self.assertIn(value, html)
        self.assertIn('class="chart-data"', html)
        self.assertNotIn("<details", html)
        self.assertNotIn("<summary", html)
        self.assertNotIn("dataZoom", html)
        self.assertNotIn("可横向滚动", html)

    def test_theme_has_no_internal_scroll_or_collapsed_chart_data(self) -> None:
        theme = load_designer_config().read_report_theme()
        self.assertNotIn("overflow-x: auto", theme)
        self.assertNotIn("overflow: auto", theme)
        self.assertNotIn(".chart-data summary", theme)
        self.assertIn(".table-wrap {\n  margin: 12px 0;\n}", theme)


if __name__ == "__main__":
    unittest.main()
