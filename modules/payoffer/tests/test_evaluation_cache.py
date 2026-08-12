"""单张Payoff图的共享解释器结果缓存回归。"""

from __future__ import annotations

from unittest.mock import patch
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "payoffer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.payoffer.impl import engine
from modules.payoffer.impl.engine import build_payoff_input
from modules.payoffer.impl.path_sampler import CandidateTemplate


class EvaluationCacheTests(unittest.TestCase):
    def test_identical_candidate_path_is_evaluated_once_per_render(self) -> None:
        contract = build_payoff_input("看涨期权")
        template = CandidateTemplate("linear", ())
        cache = {}
        with patch.object(engine, "evaluate_payoff", wraps=engine.evaluate_payoff) as evaluate:
            first = engine._evaluate_candidate(contract, "S_T", 110.0, 0, 1, (template,), template, cache, fixed=True)
            second = engine._evaluate_candidate(contract, "S_T", 110.0, 0, 1, (template,), template, cache, fixed=True)
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertEqual(evaluate.call_count, 1)


if __name__ == "__main__":
    unittest.main()
