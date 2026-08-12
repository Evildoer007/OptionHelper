from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT / "core" / "src", ROOT / "modules" / "reporter" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.reporter.designer_handoff import _report_title
from modules.reporter.models import ReporterError
from modules.reporter.report_unit_builder import _backtest_content, _contract_highlights


def contract(terms: dict | None = None, *, underlyings: list[str] | None = None) -> dict:
    return {
        "identity": {
            "product_id": "9.11",
            "name_zh": "经典型雪球",
            "underlyings": underlyings or ["159928.SZ"],
            "currency": "CNY",
        },
        "terms": terms or {
            "S0": 100.0,
            "T": 2.0,
            "N": 10_000_000.0,
            "g": 0.2,
            "K": 100.0,
            "H_KO": 103.0,
            "H_KI": 70.0,
            "c": 0.2,
            "O_KO": "monthly_last",
            "O_KI": "daily",
            "settlement": "cash",
            "observation_price": "close",
        },
        "term_sources": {},
        "price_convention": {"spot": "close", "contract_basis": "normalized_100"},
    }


class CardTitlesTermsAndUnitsTests(unittest.TestCase):
    def test_single_delivery_title_uses_frozen_subject_and_ignores_generic_metadata(self) -> None:
        unit = {"subject": {"underlyings": ["159928.SZ"], "product_name": "经典型雪球"}}

        card = _report_title(unit, SimpleNamespace(output_type="card", metadata={"title": "通用标题"}))
        report = _report_title(unit, SimpleNamespace(output_type="report", metadata={"title": "通用标题"}))

        self.assertEqual(card, "159928.SZ经典型雪球推荐卡片")
        self.assertEqual(report, "159928.SZ经典型雪球推荐报告")

    def test_multi_underlying_title_preserves_contract_order_and_missing_identity_fails(self) -> None:
        request = SimpleNamespace(output_type="card", metadata={})
        self.assertEqual(
            _report_title(
                {"subject": {"underlyings": ["000300.SH", "000905.SH"], "product_name": "Worst-Of看涨期权"}},
                request,
            ),
            "000300.SH、000905.SHWorst-Of看涨期权推荐卡片",
        )
        for subject in ({"underlyings": [], "product_name": "看涨期权"}, {"underlyings": ["000300.SH"], "product_name": ""}):
            with self.subTest(subject=subject), self.assertRaises(ReporterError):
                _report_title({"subject": subject}, request)

    def test_key_terms_are_product_aware_limited_and_tenor_first(self) -> None:
        snowball = _contract_highlights(contract())
        self.assertEqual([row["label"] for row in snowball], ["期限", "名义本金", "敲出障碍", "敲入障碍", "年化票息", "观察频率"])
        self.assertEqual(snowball[0], {"label": "期限", "value": "2", "note": "年"})
        self.assertEqual(snowball[1], {"label": "名义本金", "value": "10,000,000", "note": "人民币"})
        self.assertEqual(snowball[2]["note"], "S₀=100标准化水平")
        self.assertEqual(snowball[4], {"label": "年化票息", "value": "20%", "note": "年化，ACT/365"})
        self.assertEqual(snowball[5], {"label": "观察频率", "value": "敲出：每月最后一个交易日；敲入：每个交易日", "note": "仅交易日"})

        vanilla = _contract_highlights(contract({"T": 1.0, "K": 100.0, "Pi_0": 5.0, "n_C": 1.0, "exercise_style": "European", "settlement": "cash", "observation_price": "close"}))
        spread = _contract_highlights(contract({"T": 1.0, "K1": 95.0, "K2": 105.0, "P_net": 4.0, "exercise_style": "European", "settlement": "cash"}))
        self.assertEqual(vanilla[0]["label"], "期限")
        self.assertEqual(spread[0]["label"], "期限")
        self.assertLessEqual(len(vanilla), 6)
        self.assertLessEqual(len(spread), 6)

    def test_current_snowball_backtest_card_rows_have_explicit_units_and_denominators(self) -> None:
        module = {
            "status": "ready",
            "result": {
                "backtest": {
                    "backtest_config": {"return_denominator": "notional"},
                    "common_metrics": {
                        "sample_count": 13,
                        "win_rate": 6 / 13,
                        "average_pnl": -1_291_991.75,
                        "minimum_pnl": -2_941_712.2,
                    },
                    "metric_profile": {"profile_id": "dual_knock_autocall"},
                    "specialized_metrics": {
                        "profile_id": "dual_knock_autocall",
                        "events": {
                            "tau_out": {"trigger_rate": 6 / 13},
                            "tau_in": {"trigger_rate": 7 / 13},
                        },
                        "conditional_summary": {
                            "knock_in_then_no_knock_out_rate": 1.0,
                            "knock_in_then_knock_out_rate": 0.0,
                        },
                    },
                }
            },
        }

        public = _backtest_content(module, contract())
        overview = next(table for table in public["detail_tables"] if table["title"] == "公共回测统计")
        rows = {row["label"]: row for row in [*public["metrics"], *overview["rows"], *public["card_metrics"]]}

        expected = {
            "样本数": "个有效入场样本",
            "胜率": "合同条款现金流损益大于0的历史样本占比，分母为13个有效入场样本；不等同客户净收益，不代表未来获利概率",
            "平均损益": "人民币/份合同；合同条款现金流口径，未扣除交易费、资金成本及税费",
            "最差损益": "人民币/份合同；合同条款现金流口径，未扣除交易费、资金成本及税费",
            "敲出比例": "占有效入场样本",
            "敲入比例": "占有效入场样本",
            "敲入未敲出比例": "占已敲入样本",
            "敲入后敲出比例": "占已敲入样本",
        }
        self.assertEqual({label: rows[label]["note"] for label in expected}, expected)


if __name__ == "__main__":
    unittest.main()
