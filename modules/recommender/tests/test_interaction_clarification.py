"""自然语言约束提取与单问题追问回归。"""

from __future__ import annotations

import unittest

from modules.recommender.interaction import (
    extract_confirmed_constraints,
    merge_confirmed_constraints,
    missing_required_constraints,
    question_for_missing_constraint,
    question_for_missing_constraints,
)


class RecommendationInteractionTests(unittest.TestCase):
    def test_complete_single_turn_extracts_all_confirmed_constraints(self) -> None:
        constraints = extract_confirmed_constraints([
            "我判断未来3个月000905.SH上涨且波动率上升，最大可承受亏损30%，接受本金波动。"
            "请推荐一个适合的期权产品，并生成HTML简报。"
        ])

        self.assertEqual(constraints, {
            "underlying": "000905.SH",
            "horizon": "3个月",
            "market_view": "上涨+波动率上升",
            "max_loss": "30%",
            "principal_fluctuation": True,
            "output_type": "card",
            "format": "html",
        })
        self.assertEqual(missing_required_constraints(constraints), ())

    def test_multiturn_aliases_and_confirmation_merge_without_reasking(self) -> None:
        constraints = merge_confirmed_constraints({}, [
            {"role": "user", "content": "标的是000905.SH，我看好它未来一个季度上行。"},
            {"role": "assistant", "content": "请确认最大可承受亏损。", "status": "needs_input"},
            {"role": "user", "content": "回撤最多三成。"},
            {"role": "assistant", "content": "请确认是否接受本金波动。", "status": "needs_input"},
            {"role": "user", "content": "可以，接受净值波动，做一个网页简报。"},
        ])

        self.assertEqual(constraints["underlying"], "000905.SH")
        self.assertEqual(constraints["horizon"], "3个月")
        self.assertEqual(constraints["market_view"], "上涨")
        self.assertEqual(constraints["max_loss"], "30%")
        self.assertIs(constraints["principal_fluctuation"], True)
        self.assertEqual(constraints["output_type"], "card")
        self.assertEqual(constraints["format"], "html")
        self.assertEqual(missing_required_constraints(constraints), ())

    def test_only_true_missing_field_gets_one_question(self) -> None:
        constraints = extract_confirmed_constraints([
            "000905.SH未来3个月看涨，最大亏损30%，接受本金波动。"
        ])

        self.assertEqual(missing_required_constraints(constraints), ())
        self.assertEqual(question_for_missing_constraint("max_loss"), "请确认最大可承受亏损，例如30%。")

    def test_multiple_material_gaps_are_asked_once(self) -> None:
        fields = ("underlying", "horizon", "market_view", "max_loss", "principal_fluctuation")
        question = question_for_missing_constraints(fields)

        self.assertEqual(question.count("？"), 0)
        self.assertIn("一次补充", question)
        self.assertIn("标的代码", question)
        self.assertIn("研究期限", question)
        self.assertIn("市场方向及波动判断", question)
        self.assertIn("最大可承受亏损", question)
        self.assertIn("是否接受本金波动", question)
        self.assertIn("无需重复", question)

    def test_investment_horizon_is_not_overwritten_by_statistics_window(self) -> None:
        constraints = extract_confirmed_constraints([
            "未来3个月看涨000300.SH；该标的近3年82.2%高分位，波动率预计上升。"
        ])
        self.assertEqual(constraints["horizon"], "3个月")

        statistics_only = extract_confirmed_constraints([
            "000300.SH近3年82.2%高分位，波动率预计上升。"
        ])
        self.assertNotIn("horizon", statistics_only)


if __name__ == "__main__":
    unittest.main()
