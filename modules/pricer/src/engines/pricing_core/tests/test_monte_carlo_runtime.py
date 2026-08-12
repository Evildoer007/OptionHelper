from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import unittest

from .. import main

from .support import autocall_parameters, path_accumulator_parameters


def _price_autocall(structure: str, threads: int):
    return main.price_option(
        "AUTOCALL",
        structure,
        autocall_parameters(structure, threads=threads),
        "MONTE_CARLO_CPU",
        output="NONE",
    )


def _price_path(threads: int):
    return main.price_option(
        "ACCUMULATOR",
        "PATH_ACCUMULATOR",
        path_accumulator_parameters(threads=threads),
        "MONTE_CARLO_CPU",
        output="NONE",
    )


class MonteCarloRuntimeTest(unittest.TestCase):
    def test_threads_1_2_4_preserve_path_vector_and_all_risks(self):
        for label, price in (
            ("SNOWBALL", lambda threads: _price_autocall("SNOWBALL", threads)),
            ("PATH_ACCUMULATOR", _price_path),
        ):
            runs = [price(threads) for threads in (1, 2, 4)]
            reference = runs[0].result
            with self.subTest(label=label):
                for run in runs[1:]:
                    self.assertEqual(run.result.pv_points_100, reference.pv_points_100)
                    self.assertEqual(run.result.greeks, reference.greeks)
                    self.assertEqual(run.result.extended_risks, reference.extended_risks)
                    self.assertEqual(
                        run.result.diagnostics["path_pv_sha256_float32"],
                        reference.diagnostics["path_pv_sha256_float32"],
                    )

    def test_call_order_does_not_change_results(self):
        snowball_first = _price_autocall("SNOWBALL", 1).result
        _price_path(4)
        _price_autocall("PHOENIX", 2)
        snowball_last = _price_autocall("SNOWBALL", 1).result
        self.assertEqual(snowball_first, snowball_last)

    def test_cross_engine_concurrency_is_deterministic(self):
        expected_snowball = _price_autocall("SNOWBALL", 1).result
        expected_path = _price_path(1).result
        jobs = [
            ("snowball", 4),
            ("path", 2),
            ("snowball", 2),
            ("path", 4),
        ] * 2

        def run(job):
            kind, threads = job
            return (
                _price_autocall("SNOWBALL", threads).result
                if kind == "snowball"
                else _price_path(threads).result
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(run, jobs))
        for (kind, _), result in zip(jobs, results, strict=True):
            expected = expected_snowball if kind == "snowball" else expected_path
            self.assertEqual(result.pv_points_100, expected.pv_points_100)
            self.assertEqual(result.greeks, expected.greeks)
            self.assertEqual(result.extended_risks, expected.extended_risks)
            self.assertEqual(
                result.diagnostics["path_pv_sha256_float32"],
                expected.diagnostics["path_pv_sha256_float32"],
            )


if __name__ == "__main__":
    unittest.main()
