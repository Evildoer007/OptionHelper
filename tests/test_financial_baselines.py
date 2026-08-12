from __future__ import annotations

import ast
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASELINES = Path(__file__).with_name("baselines")
EXPECTED_RANDOM_SOURCE_SHA256 = "5762ac55cb922bd9dd2cb9ebf7f97857153519bf0405470299b8c4061910b8ed"


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _asset_pair_hash(json_path: Path, svg_path: Path) -> str:
    return sha256(json_path.read_bytes() + b"\0" + svg_path.read_bytes()).hexdigest()


def _expected_asset_pairs() -> dict[str, str]:
    result: dict[str, str] = {}
    source = BASELINES / "payoffer_default_assets.sha256"
    for line_number, raw_line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            digest, name = raw_line.split("  ", 1)
        except ValueError as error:
            raise AssertionError(f"{source}:{line_number}不是'<sha256><两个空格><中文名>'") from error
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise AssertionError(f"{source}:{line_number}包含无效SHA-256")
        if name in result:
            raise AssertionError(f"{source}:{line_number}重复登记{name}")
        result[name] = digest
    return result


def _literal_assignment(path: Path, variable_name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == variable_name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path}未找到可静态读取的{variable_name}赋值")


class ExistingFinancialBaselineTest(unittest.TestCase):
    def test_optionreg_still_contains_exactly_65_products(self) -> None:
        products = _literal_assignment(ROOT / "references" / "optionreg.py", "PRODUCTS")
        self.assertIsInstance(products, dict)
        self.assertEqual(
            len(products),
            65,
            "OptionReg产品数量发生变化。目录迁移不得新增、删除或覆盖产品；产品库变更必须另行审核。",
        )

    def test_all_65_default_json_svg_pairs_match_frozen_hashes(self) -> None:
        expected = _expected_asset_pairs()
        self.assertEqual(len(expected), 65, "Payoffer基线清单本身必须精确包含65个产品")

        candidates = [
            ROOT / "modules" / "payoffer" / "figures",
            ROOT / "assets" / "payoffer" / "figures",
        ]
        roots = [candidate for candidate in candidates if candidate.is_dir()]
        self.assertTrue(
            roots,
            "找不到Payoffer默认资产。迁移中必须保留modules/payoffer/figures或尚未退役的assets/payoffer/figures。",
        )

        for figures_root in roots:
            json_files = {path.stem: path for path in (figures_root / "json").glob("*.json")}
            svg_files = {path.stem: path for path in (figures_root / "svg").glob("*.svg")}
            with self.subTest(figures_root=figures_root):
                self.assertEqual(set(json_files), set(expected), f"{figures_root}/json产品集合漂移")
                self.assertEqual(set(svg_files), set(expected), f"{figures_root}/svg产品集合漂移")
                mismatches = {
                    name: {
                        "expected": expected[name],
                        "actual": _asset_pair_hash(json_files[name], svg_files[name]),
                    }
                    for name in expected
                    if _asset_pair_hash(json_files[name], svg_files[name]) != expected[name]
                }
                self.assertFalse(
                    mismatches,
                    f"{figures_root}存在未经审核的默认JSON/SVG变化：{json.dumps(mismatches, ensure_ascii=False)}",
                )

    def test_pricer_golden_values_have_not_been_rewritten(self) -> None:
        expected = json.loads(
            (BASELINES / "pricer_option_pricing_golden.json").read_text(encoding="utf-8")
        )
        target_fixture = ROOT / "modules" / "pricer" / "tests" / "fixtures" / "option_pricing_golden.json"
        target_embedded = (
            ROOT / "modules" / "pricer" / "src"
            / "engines" / "pricing_core" / "tests" / "golden_standard_results.json"
        )
        sources = [path for path in (target_fixture, target_embedded) if path.is_file()]
        self.assertTrue(
            sources,
            "Pricer Golden文件缺失。旧基座退役前必须把同一基线放入modules/pricer/tests/fixtures。",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(
                    json.loads(source.read_text(encoding="utf-8")),
                    expected,
                    f"{source}与迁移前9结构Golden数值不一致",
                )

    def test_pricer_frozen_random_source_has_not_drifted(self) -> None:
        candidates = [
            ROOT / "modules" / "pricer" / "tests" / "fixtures" / "rand_normal.npy",
            ROOT / "modules" / "pricer" / "src"
            / "engines" / "pricing_core" / "data" / "rand_normal.npy",
        ]
        sources = [path for path in candidates if path.is_file()]
        self.assertTrue(
            sources,
            "Pricer固定随机源缺失。迁移必须保留rand_normal.npy作为逐路径回归证据。",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(
                    _sha256(source),
                    EXPECTED_RANDOM_SOURCE_SHA256,
                    f"{source}随机源发生变化，不能再使用既有路径哈希作回归结论",
                )

    def test_pricer_golden_engine_execution(self) -> None:
        target_test = ROOT / "modules" / "pricer" / "tests" / "test_option_pricing_golden.py"
        target_embedded_root = (
            ROOT / "modules" / "pricer" / "src"
            / "engines" / "pricing_core"
        )

        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["NUMBA_NUM_THREADS"] = "1"
        with tempfile.TemporaryDirectory(prefix="optionhelper-numba-") as numba_cache:
            environment["NUMBA_CACHE_DIR"] = numba_cache
            if (target_embedded_root / "tests" / "test_golden_results.py").is_file():
                # Run through Pricer's public package boundary from the project
                # root.  A top-level ``engines`` import only works when the
                # implementation directory is injected into ``sys.path`` and
                # therefore is not a valid integration check.
                command = [
                    sys.executable,
                    "-m",
                    "unittest",
                    "modules.pricer.engines.pricing_core.tests.test_golden_results",
                    "-v",
                ]
                cwd = ROOT
                python_paths = [ROOT, ROOT / "core" / "src"]
                environment["PYTHONPATH"] = os.pathsep.join(
                    [*(str(path) for path in python_paths), environment.get("PYTHONPATH", "")]
                ).rstrip(os.pathsep)
            else:
                self.assertTrue(
                    target_test.is_file(),
                    "旧Pricer基座已移除，但目标test_option_pricing_golden.py不存在；迁移无法证明数值等价。",
                )
                command = [sys.executable, str(target_test)]
                cwd = ROOT
                python_paths = [
                    ROOT / "core" / "src",
                    *(ROOT / "modules" / module / "src" for module in (
                        "datafetcher", "recommender", "payoffer", "pricer", "backtester", "reporter", "designer"
                    )),
                ]
                environment["PYTHONPATH"] = os.pathsep.join(
                    [*(str(path) for path in python_paths), environment.get("PYTHONPATH", "")]
                ).rstrip(os.pathsep)

            completed = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        self.assertEqual(
            completed.returncode,
            0,
            "Pricer Golden实际执行失败，不能只凭静态JSON声称数值未漂移。\n"
            f"命令：{' '.join(command)}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
