"""65产品分段定义域与共享解释器的边界探针回归。"""

from __future__ import annotations

from math import isfinite
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "payoffer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.contracts.contract_api import evaluate_formula
from modules.payoffer.impl.engine import _compile_paths, build_payoff_input, load_registry
from modules.payoffer.impl.path_sampler import Interval, term_variables


def _contains(interval: Interval, value: float) -> bool:
    lower = value > interval.lower or interval.lower_closed and value == interval.lower
    upper = value < interval.upper or interval.upper_closed and value == interval.upper
    return lower and upper


class PayofferDomainBoundaryProbeTest(unittest.TestCase):
    def test_926_boundary_and_neighbour_probes_match_shared_interpreter(self) -> None:
        registry = load_registry()
        probes = 0
        mismatches: list[tuple[str, str, float, bool, bool]] = []
        for product_id, product in registry["products"].items():
            contract = build_payoff_input(product["identity"]["name_zh"])
            variables = term_variables(contract, registry["term_catalog"])
            for path, domains in zip(contract.paths, _compile_paths(contract)):
                for case, domain in zip(path["cases"], domains):
                    values: list[float] = []
                    for interval in domain.intervals:
                        for boundary in (interval.lower, interval.upper):
                            if isfinite(boundary):
                                epsilon = max(1e-7, abs(boundary) * 1e-7)
                                values.extend((boundary - epsilon, boundary, boundary + epsilon))
                        if isfinite(interval.lower) and isfinite(interval.upper):
                            values.append((interval.lower + interval.upper) / 2.0)
                    for value in values:
                        expected = any(_contains(interval, value) for interval in domain.intervals)
                        actual = bool(evaluate_formula(case["domain"], {**variables, domain.axis: value}))
                        probes += 1
                        if actual != expected:
                            mismatches.append((str(product_id), case["domain"], value, expected, actual))

        self.assertEqual(probes, 926)
        self.assertEqual(mismatches, [])


if __name__ == "__main__":
    unittest.main()
