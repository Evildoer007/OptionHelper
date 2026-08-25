#!/usr/bin/env python3
"""Regenerate Designer's checked-in HTML visual baselines.

Samples are derived from the public renderer and the canonical fixture. They
are deliberately not hand-authored presentation files: the asset test verifies
byte equality with a fresh render before any release.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.designer import render
from modules.designer.design_system_builder import build_app_token_stylesheet
from modules.designer.models import DESIGNER_PAYLOAD_SCHEMA, DesignerInput


def _comparison_payload(output_type: str) -> dict:
    candidates = []
    for index, structure in enumerate(("看涨价差", "保护性看跌"), start=1):
        facts = {
            "schema": DESIGNER_PAYLOAD_SCHEMA,
            "meta": {"title": structure, "as_of_date": "2026-08-24"},
            "sections": ["conclusion", "recommendation", "parameters", "payoff", "pricing", "backtest", "risk"],
            "recommendation": {
                "structure_name": structure,
                "underlyings": "000300.SH",
                "reason": "与温和看涨、波动率可能抬升的判断匹配。",
                "reason_points": ["控制初始成本并保留目标区间内的收益弹性。"],
            },
            "contract_highlights": [
                {"label": "期限", "value": "3个月"},
                {"label": "执行价", "value": 3850 + index * 50},
                {"label": "净期权费", "value": 0.018 + index / 1000, "value_format": "percent"},
            ],
            "parameters": {"common_input": [], "payoff_input": [], "pricing_input": [], "backtest_input": []},
            "payoff": {"status": "ready", "scenarios": [{"title": "到期情景", "rule": "收益随S_T变化，并受合同执行价约束。"}]},
            "pricing": {
                "status": "ready",
                "metrics": [{"label": "现值", "value": 0.018 + index / 1000, "value_format": "percent"}],
                "greeks": [
                    {"label": "Delta", "value": 0.22 + index / 100}, {"label": "Gamma", "value": 0.0046},
                    {"label": "Vega", "value": 0.09}, {"label": "Theta", "value": -0.03}, {"label": "Rho", "value": 0.01},
                ],
                "charts": [{
                    "id": f"pricing-{index}", "title": "标的价格敏感度", "type": "line",
                    "x": [-10, 0, 10], "x_axis_name": "标的涨跌幅", "y_axis_name": "估值变动",
                    "value_format": "percent", "source_note": "本次冻结估值结果。",
                    "series": [{"name": "现值变动", "data": [-0.02, 0, 0.02 + index / 1000]}],
                }],
            },
            "backtest": {
                "status": "ready", "window": "2022-08-24至2026-08-24",
                "metrics": [
                    {"label": "样本数", "value": 48},
                    {"label": "胜率", "value": 0.60 + index / 100, "value_format": "percent"},
                    {"label": "平均收益", "value": 0.03 + index / 1000, "value_format": "percent"},
                    {"label": "最大亏损", "value": -0.05 + index / 1000, "value_format": "percent"},
                ],
            },
            "risk": {"items": ["极端行情下可能损失期权费。", "实际成交价格可能偏离估值结果。"]},
        }
        candidates.append({
            "label": f"方案{chr(64 + index)}", "rank": index, "is_primary": index == 1,
            "title": structure, "underlyings": "000300.SH", "facts": facts,
        })
    return {
        "schema": DESIGNER_PAYLOAD_SCHEMA,
        "meta": {
            "title": "MultiCard对比卡片" if output_type == "card" else "MultiReport对比报告",
            "as_of_date": "2026-08-24",
        },
        "sections": (
            ["recommendation", "reason", "contract_highlights", "pricing", "backtest", "risk"]
            if output_type == "card" else
            ["conclusion", "recommendation", "parameters", "payoff", "pricing", "backtest", "risk"]
        ),
        "comparison": {"delivery_mode": "comparison", "candidates": candidates},
    }


def main() -> None:
    fixture = PROJECT_ROOT / "modules" / "designer" / "tests" / "fixtures" / "report-payload.example.json"
    assets = PROJECT_ROOT / "modules" / "designer" / "assets"
    samples = assets / "samples"
    examples = assets / "examples"
    examples.mkdir(exist_ok=True)
    (assets / "themes" / "designer-token-vars.css").write_text(
        build_app_token_stylesheet(), encoding="utf-8"
    )
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    for output_type in ("report", "card", "quote"):
        artifact = render(DesignerInput(payload=payload, output_type=output_type, asset_mode="portable"))
        (samples / f"{output_type}.html").write_text(artifact["html"], encoding="utf-8")
    for output_type, filename in (("card", "multicard.html"), ("report", "multireport.html")):
        artifact = render(DesignerInput(payload=_comparison_payload(output_type), output_type=output_type, asset_mode="portable"))
        (examples / filename).write_text(artifact["html"], encoding="utf-8")


if __name__ == "__main__":
    main()
