"""Real App loopback for one frozen contract and all three calculators."""

from __future__ import annotations

from dataclasses import asdict
import csv
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urlencode, urlparse
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / "products" / "app"
for source in (ROOT / "core" / "src", APP_ROOT):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from backend.app_server import AppServer  # noqa: E402
from runtime.adapters.local_store import LocalDataStore  # noqa: E402
from modules.pricer.tests.pricer_test_fixtures import market_csv_payload  # noqa: E402
from capability_fixture import capability_root  # noqa: E402


class ComputeHttpLoopbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="optionhelper-compute-loopback-")
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {
            "OPTIONHELPER_RUNTIME_ROOT": str(self.root / "runtime"),
            "OPTIONHELPER_DATA_ROOT": str(self.root / "data"),
            "OPTIONHELPER_RESULT_ROOT": str(self.root / "result"),
        })
        self.environment.start()
        self.app = AppServer(app_data_dir=self.root / "app-state", capability_root=capability_root())
        parsed = urlparse(self.app.start_background())
        self.connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=30)

    def tearDown(self) -> None:
        self.connection.close()
        self.app.shutdown()
        self.environment.stop()
        self.temporary.cleanup()

    def request(self, method: str, path: str, body=None, cookie=None, headers=None):
        values = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body)
            values["Content-Type"] = "application/json"
        if cookie:
            values["Cookie"] = cookie
        self.connection.request(method, path, body=payload, headers=values)
        response = self.connection.getresponse()
        decoded = json.loads(response.read().decode("utf-8"))
        self.assertLess(response.status, 300, decoded)
        return response.status, decoded, response.getheader("Set-Cookie")

    def host_headers(self, module: str, task_id: str | None, cookie: str, sequence: int) -> dict[str, str]:
        query = f"?{urlencode({'task_id': task_id})}" if task_id else ""
        _, body, _ = self.request("GET", f"/api/module-host/{module}{query}", cookie=cookie)
        return {
            "Origin": self.app.url,
            "X-OptionHelper-Module-Context": json.dumps(body["context"]),
            "X-OptionHelper-Request-Id": f"compute-loopback-{module}-{sequence}",
        }

    def request_status(self, method: str, path: str, body=None, cookie=None, headers=None):
        values = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body)
            values["Content-Type"] = "application/json"
        if cookie:
            values["Cookie"] = cookie
        self.connection.request(method, path, body=payload, headers=values)
        response = self.connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))

    def test_compute_catalogs_are_read_only_but_still_require_a_real_host_context(self) -> None:
        """Catalogs do not create a task, contract binding, or ModuleRun."""
        _, _login, set_cookie = self.request("POST", "/api/auth/local", {"role": "admin", "principal_label": "compute-catalog"})
        cookie = str(set_cookie).split(";", 1)[0]

        for sequence, module in enumerate(("payoffer", "pricer", "backtester"), start=1):
            _, envelope, _ = self.request(
                "POST", f"/api/tools/{module}", {"action": "catalog"}, cookie,
                self.host_headers(module, None, cookie, sequence),
            )
            result = envelope["result"]
            self.assertTrue(result["ok"])
            self.assertEqual(result["module"], module)
            self.assertIn("products", result)

        status, rejection = self.request_status(
            "POST", "/api/tools/payoffer", {"action": "run"}, cookie,
            self.host_headers("payoffer", None, cookie, 4),
        )
        self.assertEqual(status, 400, rejection)
        self.assertEqual(rejection["error"], "invalid_request")
        self.assertEqual(self.app._documents.read("results"), {})
        self.assertEqual(self.app._documents.read("contracts"), {})
        self.assertEqual(list((self.root / "app-state" / "module-runs").rglob("commit_marker.json")), [])

        for sequence, module in enumerate(("payoffer", "pricer", "backtester"), start=5):
            status, rejection = self.request_status(
                "POST", f"/api/tools/{module}", {"action": "unsupported_probe"}, cookie,
                self.host_headers(module, None, cookie, sequence),
            )
            self.assertEqual(status, 403, rejection)
            self.assertEqual(rejection["error"], "forbidden")
            self.assertEqual(rejection["capability"], "module.run")
        self.assertEqual(self.app._documents.read("results"), {})
        self.assertEqual(self.app._documents.read("contracts"), {})

    def test_pricer_without_data_explains_market_data_gate(self) -> None:
        """A non-demo Pricer request must expose a safe, actionable data gate."""
        _, _login, set_cookie = self.request(
            "POST", "/api/auth/local", {"role": "admin", "principal_label": "pricer-page-demo"},
        )
        cookie = str(set_cookie).split(";", 1)[0]
        _, created, _ = self.request("POST", "/api/tasks", {"subject": "中证500看涨期权MC10演示"}, cookie)
        task_id = created["task"]["task_id"]

        # This is the exact friendly request assembled by the prefilled Pricer page.
        page_request = {
            "action": "run",
            "product_id": "2.1",
            "identity": {
                "underlyings": ["000905.SH"],
                "contract_start_date": "2026-07-28",
                "contract_reference_spots": {"000905.SH": 7443.4332},
            },
            "term_overrides": {},
            "pricing_config": {
                "valuation_date": "2026-07-28",
                "risk_free_rate": 0.02,
                "dividend_yield": 0.0,
                "hv_window": 20,
                "model_method": "monte_carlo",
                "path_count": 1000,
                "demo_mode": False,
            },
            "task_id": task_id,
        }
        status, rejection = self.request_status(
            "POST", "/api/tools/pricer", page_request, cookie,
            self.host_headers("pricer", task_id, cookie, 11),
        )

        self.assertEqual(status, 409, rejection)
        self.assertEqual(rejection["error"], "market_data_required")
        self.assertEqual(
            rejection["message"],
            "当前任务尚无可用于000905.SH的行情数据；请先在数据获取中获取000905.SH日频行情，再运行定价。",
        )
        self.assertNotIn("path", str(rejection).lower())
        self.assertNotIn("token", str(rejection).lower())

    def test_pricer_page_csi500_demo_runs_from_explicit_snapshot_only(self) -> None:
        """The page MC10 sample cannot silently read, infer or fabricate market data."""
        _, _login, set_cookie = self.request(
            "POST", "/api/auth/local", {"role": "admin", "principal_label": "pricer-page-demo-success"},
        )
        cookie = str(set_cookie).split(";", 1)[0]
        _, created, _ = self.request("POST", "/api/tasks", {"subject": "中证500看涨期权MC10演示"}, cookie)
        task_id = created["task"]["task_id"]
        page_request = {
            "action": "run",
            "product_id": "2.1",
            "identity": {
                "underlyings": ["000905.SH"],
                "contract_start_date": "2026-07-28",
                "contract_reference_spots": {"000905.SH": 7443.4332},
            },
            "term_overrides": {},
            "pricing_config": {
                "valuation_date": "2026-07-28",
                "risk_free_rate": 0.02,
                "dividend_yield": 0.0,
                "hv_window": 20,
                "model_method": "monte_carlo",
                "path_count": 10,
                "demo_mode": True,
                "spot": 7443.4332,
                "volatility_override": 0.20,
                "time_to_maturity": 1.0,
                "demo_calendar": {
                    "calendar_id": "demo-european-vanilla",
                    "calendar_version": "v1",
                    "sessions": ["2026-07-28"],
                    "discrete_path": False,
                },
            },
            "task_id": task_id,
        }
        status, envelope, _ = self.request(
            "POST", "/api/tools/pricer", page_request, cookie,
            self.host_headers("pricer", task_id, cookie, 12),
        )
        self.assertEqual(status, 200, envelope)
        result = envelope["result"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pricing"]["precision_status"], "demo_only")
        self.assertFalse(result["pricing"]["quote_eligible"])
        self.assertEqual(result["data_refs"], [])
        self.assertEqual(result["market_snapshot"]["source"], "explicit_demo_market_snapshot")
        self.assertEqual(result["market_snapshot"]["data_lineage"]["mode"], "explicit_demo_only")
        self.assertEqual(result["market_snapshot"]["trading_calendar"]["sessions"], ["2026-07-28"])
        self.assertEqual(result["pricing"]["input_snapshot"]["contract"]["identity"]["underlyings"], ["000905.SH"])
        scenario_spots = {
            row["spot_shift"]: row["spot"]
            for row in result["pricing"]["risk_scenarios"]
            if row["remaining_days"] == 365.0
        }
        self.assertEqual(scenario_spots, {
            -.10: 7_443.4332 * .90,
            0.0: 7_443.4332,
            .10: 7_443.4332 * 1.10,
        })
        self.assertNotIn("storage_ref", str(result))
        self.assertIn("module_run_ref", result)

    def test_pricer_zero_data_mc10_demo_rejects_non_call_before_capability(self) -> None:
        _, _login, set_cookie = self.request(
            "POST", "/api/auth/local", {"role": "admin", "principal_label": "pricer-demo-product-gate"},
        )
        cookie = str(set_cookie).split(";", 1)[0]
        _, created, _ = self.request("POST", "/api/tasks", {"subject": "非看涨期权MC10演示"}, cookie)
        task_id = created["task"]["task_id"]
        request = {
            "product_id": "2.2",
            "identity": {
                "underlyings": ["000905.SH"], "contract_start_date": "2026-07-28",
                "contract_reference_spots": {"000905.SH": 7443.4332},
            },
            "term_overrides": {},
            "pricing_config": {
                "valuation_date": "2026-07-28", "spot": 7443.4332,
                "volatility_override": 0.20, "time_to_maturity": 1.0,
                "risk_free_rate": 0.02, "dividend_yield": 0.0,
                "model_method": "monte_carlo", "path_count": 10, "demo_mode": True,
                "demo_calendar": {
                    "calendar_id": "demo-european-vanilla", "calendar_version": "v1",
                    "sessions": ["2026-07-28"], "discrete_path": False,
                },
            },
            "task_id": task_id,
        }
        status, rejection = self.request_status(
            "POST", "/api/tools/pricer", request, cookie,
            self.host_headers("pricer", task_id, cookie, 13),
        )
        self.assertEqual(status, 409, rejection)
        self.assertEqual(rejection["error"], "invalid_demo_market_snapshot")
        self.assertIn("仅支持2.1看涨期权", rejection["message"])

    def test_payoffer_pricer_backtester_share_one_host_contract_and_commit_once(self) -> None:
        _, login, set_cookie = self.request("POST", "/api/auth/local", {"role": "admin", "principal_label": "compute-loopback"})
        cookie = str(set_cookie).split(";", 1)[0]
        identity = self.app.identity_from_cookie(cookie)
        _, created, _ = self.request("POST", "/api/tasks", {"subject": "三模块正式输入"}, cookie)
        task_id = created["task"]["task_id"]

        payload = market_csv_payload()
        rows = list(csv.DictReader(payload.decode("utf-8").splitlines()))
        sessions = tuple(row["date"] for row in rows)
        contract_start_close = float(rows[0]["close"])
        # Seed the exact App-owned DataStore used by the verified Capability.
        # Process environment roots are deliberately irrelevant to App calls.
        ref = LocalDataStore(self.app._capability_runtime_root / "data").put_bytes(
            tenant_id=identity.tenant_id,
            data_asset_id="compute-loopback-market",
            payload=payload,
            media_type="text/csv",
            schema_id="market-history-v1",
            asset_ids=("000905.SH",),
            normalized_fields=("date", "asset_id", "open", "high", "low", "close", "adj_close"),
            coverage={
                "start": sessions[0], "end": sessions[-1], "sessions": sessions,
                "calendar_id": "CN-SSE", "calendar_version": "compute-loopback-v1",
                "by_asset": {
                    "000905.SH": {
                        "start_date": sessions[0],
                        "end_date": sessions[-1],
                        "row_count": len(rows),
                    },
                },
            },
            row_count=len(rows),
            price_convention={
                "contract_close_field": "close", "contract_adjustment": "unadjusted",
                "hv_close_field": "adj_close", "hv_adjustment": "forward",
                "close_equals_adj_close": True, "calendar": "trading_days",
            },
            lineage={"fixture": "compute-http-loopback"},
            created_by=identity.principal_id,
        )
        self.app.data_assets.register(identity, asdict(ref))

        first = {
            "product_id": "2.1",
            "identity": {
                "underlyings": ["000905.SH"],
                "reference_prices": {"000905.SH": contract_start_close},
                "contract_start_date": sessions[0],
            },
            "term_overrides": {"K": 100.0, "T": 1.0, "Pi_0": 0.0},
        }
        requests = {
            "payoffer": first,
            "pricer": {
                "pricing_config": {
                    "valuation_date": sessions[20], "spot": 100.0, "historical_volatility": 0.20,
                    "dividend_yield": 0.0, "risk_free_rate": 0.02, "model_method": "black_scholes",
                },
                "market_data_refs": [{"data_asset_id": ref.data_asset_id, "content_hash": ref.content_hash}],
            },
            "backtester": {
                "backtest_config": {"entry_rule": "explicit", "entry_dates": [sessions[0]]},
                "historical_data": {"data_asset_id": ref.data_asset_id, "content_hash": ref.content_hash},
            },
        }
        run_refs = []
        for sequence, module in enumerate(("payoffer", "pricer", "backtester"), start=1):
            request = {**requests[module], "task_id": task_id}
            _, envelope, _ = self.request(
                "POST", f"/api/tools/{module}", request, cookie,
                self.host_headers(module, task_id, cookie, sequence),
            )
            result = envelope["result"]
            self.assertIn(result["status"], {"succeeded", "partial"}, result)
            run_refs.append(result["module_run_ref"])

        binding = self.app.contracts.get(identity, task_id)
        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual({ref["task_id"] for ref in run_refs}, {task_id})
        self.assertEqual(len(self.app.tasks.get(identity, task_id)["run_refs"]), 3)
        self.assertEqual(len(list((self.root / "app-state" / "module-runs").rglob("commit_marker.json"))), 3)
        self.assertEqual(self.app._documents.read("result_recovery"), {})
        self.assertEqual(binding["contract_ref"]["content_hash"], binding["contract_fingerprint"])


if __name__ == "__main__":
    unittest.main()
