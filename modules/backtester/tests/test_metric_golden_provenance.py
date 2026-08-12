"""冻结v1.0指标快照与当前65产品指标Golden的谱系回归。"""

# ruff: noqa: E402

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from zipfile import ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT, PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "backtester" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.backtester import BacktestConfig, BacktestInput, HistoricalData, backtest
from modules.backtester.tests.fixtures import two_asset_history
from runtime.contracts.contract_api import load_registry, resolve_contract


V10_METRIC_DIGEST = "08b30d20b726cc17555902b778c3dbeb621ff402e5c9a334bf28d980ca038086"
CURRENT_METRIC_DIGEST = "9114834b9dba9b401954854d759429dbfeb2e60adc5284487a5c298683c6e540"
_PACKAGE = "_frozen_backtester_v10_metrics"
_ZIP_PREFIX = "option-helper/scripts/modules/backtester/"


def metric_snapshot_diff() -> dict[str, object]:
    """以冻结v1.0指标函数重算同一账本，生成65产品字段级差异证据。"""

    history = HistoricalData.from_frame(two_asset_history())
    with _frozen_v10_metric_modules() as (legacy_common, legacy_map, legacy_profiles):
        legacy_rows: dict[str, object] = {}
        current_rows: dict[str, object] = {}
        products: list[dict[str, object]] = []
        for product_id in load_registry()["products"]:
            contract = resolve_contract(product_id, identity={"underlyings": ["000905.SH", "000300.SH"]})
            result = backtest(BacktestInput(
                contract,
                BacktestConfig(entry_rule="explicit", entry_dates=("2021-01-04",)),
                history,
            ))
            payload = result.to_dict()
            legacy_summary = legacy_common.summarize_common_metrics(result.trades, contract, include_annual=False)
            legacy_common_metrics = dict(legacy_summary["common_metrics"])
            legacy_common_metrics["skipped_count"] = len(result.skipped_entries)
            legacy_profile = legacy_map.metric_profile_for(product_id)
            legacy_specialized = legacy_profiles.specialized_metrics(
                legacy_profile,
                result.trades,
                terms=contract.terms,
                event_summary=legacy_summary["event_summary"],
                monitor_summary=legacy_summary["monitor_summary"],
                outcome_summary=legacy_summary["outcome_summary"],
                three_outcome_summary=legacy_summary["three_outcome_summary"],
                conditional_summary=legacy_summary["conditional_summary"],
            )
            legacy_row = {
                "ledger_hash": payload["ledger_hash"],
                "common_metrics": legacy_common_metrics,
                "specialized_metrics": legacy_specialized,
            }
            current_row = {
                "ledger_hash": payload["ledger_hash"],
                "common_metrics": payload["common_metrics"],
                "specialized_metrics": payload["specialized_metrics"],
            }
            legacy_rows[product_id] = legacy_row
            current_rows[product_id] = current_row
            trace = _canonical_json([
                {
                    "entry_date": trade.entry_date,
                    "exit_date": trade.exit_date,
                    "path_id": trade.path_id,
                    "case_id": trade.case_id,
                    "cashflows": list(trade.cashflows),
                    "pnl": trade.pnl,
                    "return_value": trade.return_value,
                }
                for trade in result.trades
            ])
            products.append({
                "product_id": product_id,
                "ledger_hash": payload["ledger_hash"],
                "ledger_trace_sha256": sha256(trace.encode()).hexdigest(),
                "ledger_matches_frozen_metric_replay": legacy_row["ledger_hash"] == current_row["ledger_hash"],
                "legacy_profile_id": legacy_profile.profile_id,
                "current_profile_id": payload["metric_profile"]["profile_id"],
                "common_metric_differences": _field_differences(legacy_common_metrics, payload["common_metrics"]),
                "specialized_metric_differences": _field_differences(legacy_specialized, payload["specialized_metrics"]),
            })
    return {
        "schema_version": "backtester.metric-golden-provenance.v1",
        "frozen_source": "versions/v1.0/option-helper.zip",
        "legacy_metric_digest": _digest(legacy_rows),
        "current_metric_digest": _digest(current_rows),
        "products": products,
    }


@contextmanager
def _frozen_v10_metric_modules():
    """仅加载v1.0压缩包中的指标代码；不导入或改写当前模块。"""

    members = ("common_metrics.py", "metric_profile_map.py", "metric_profiles.py")
    archive = PROJECT_ROOT / "versions" / "v1.0" / "option-helper.zip"
    with tempfile.TemporaryDirectory(prefix="backtester-v10-metrics-") as temporary:
        target = Path(temporary)
        with ZipFile(archive) as bundle:
            for member in members:
                bundle.extract(_ZIP_PREFIX + member, target)
        base = target / _ZIP_PREFIX
        original = {name: sys.modules.get(name) for name in (_PACKAGE, *(_PACKAGE + "." + member[:-3] for member in members))}
        package = ModuleType(_PACKAGE)
        package.__path__ = [str(base)]
        sys.modules[_PACKAGE] = package
        try:
            common = _load_module(f"{_PACKAGE}.common_metrics", base / "common_metrics.py")
            profile_map = _load_module(f"{_PACKAGE}.metric_profile_map", base / "metric_profile_map.py")
            profiles = _load_module(f"{_PACKAGE}.metric_profiles", base / "metric_profiles.py")
            yield common, profile_map, profiles
        finally:
            for name, value in original.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载冻结指标代码：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode()).hexdigest()


def _field_differences(legacy: object, current: object, path: str = "") -> list[dict[str, object]]:
    if isinstance(legacy, dict) and isinstance(current, dict):
        rows: list[dict[str, object]] = []
        for key in sorted(set(legacy) | set(current)):
            child = f"{path}.{key}" if path else str(key)
            rows.extend(_field_differences(legacy.get(key), current.get(key), child))
        return rows
    if legacy == current:
        return []
    return [{"field": path, "legacy": legacy, "current": current}]


class MetricGoldenProvenanceTest(unittest.TestCase):
    def test_frozen_v10_metrics_reproduce_old_golden_and_current_ledger_is_unchanged(self) -> None:
        evidence = metric_snapshot_diff()

        self.assertEqual(evidence["legacy_metric_digest"], V10_METRIC_DIGEST)
        self.assertEqual(evidence["current_metric_digest"], CURRENT_METRIC_DIGEST)
        products = evidence["products"]
        self.assertEqual(len(products), 65)
        self.assertTrue(all(row["ledger_matches_frozen_metric_replay"] for row in products))
        self.assertTrue(all(row["ledger_trace_sha256"] for row in products))
        self.assertTrue(any(row["legacy_profile_id"] != row["current_profile_id"] for row in products))
        self.assertTrue(any(row["common_metric_differences"] for row in products))
        self.assertTrue(any(row["specialized_metric_differences"] for row in products))


if __name__ == "__main__":
    unittest.main()
