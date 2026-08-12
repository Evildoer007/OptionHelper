"""Regression coverage for App-owned immutable metadata records."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.app_server import AppServer
from backend.errors import ValidationError
from backend.identity.identity_provider import LocalAuthenticationRequest
from backend.authorization.roles import Role
from backend.identity.session_identity import SessionIdentity
from capability_fixture import capability_root


class AppStoreImmutabilityTests(unittest.TestCase):
    def test_data_assets_and_module_runs_reject_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = AppServer(app_data_dir=Path(temporary), capability_root=capability_root())
            identity = app.identity_provider.authenticate(
                LocalAuthenticationRequest(role=Role.ADMIN, principal_label="immutable-record-test")
            )
            asset = {
                "data_asset_id": "data:immutable-test",
                "storage_ref": "snapshot-immutable-test",
                "media_type": "application/json",
                "schema_id": "optionhelper.data-asset/v1",
                "asset_ids": ["000905.SH"],
                "normalized_fields": ["close"],
                "coverage": {"start": "2026/01/01", "end": "2026/01/31"},
                "row_count": 21,
                "price_convention": {"adjustment": "forward"},
                "content_hash": "sha256:test",
                "lineage": {"provider": "ifind-http"},
            }
            first_asset = app.data_assets.register(identity, asset)
            self.assertEqual(app.data_assets.register(identity, asset), first_asset)
            changed_asset = {**asset, "content_hash": "sha256:changed"}
            with self.assertRaises(ValidationError):
                app.data_assets.register(identity, changed_asset)

            task = app.tasks.create(identity, "不可覆盖模块记录")
            result = {"run_id": "run:immutable-test", "ok": True, "status": "complete"}
            app.results.commit_module_run(identity, task["task_id"], "payoffer", result)
            with self.assertRaises(ValidationError):
                app.results.commit_module_run(identity, task["task_id"], "payoffer", result)
            pricer_ref = app.results.commit_module_run(identity, task["task_id"], "pricer", result)
            self.assertEqual(pricer_ref["module"], "pricer")
            self.assertEqual(
                app.results.resolve_owned_module_run(identity, pricer_ref).parts[-3],
                "output_pricing",
            )

    def test_result_selection_lists_only_completed_tenant_runs_without_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = AppServer(app_data_dir=Path(temporary), capability_root=capability_root())
            identity = SessionIdentity("analyst", "tenant-a", Role.ADMIN, "session-a")
            task = app.tasks.create(identity, "Reporter来源")
            succeeded = {
                "run_id": "run-success", "ok": True, "status": "completed", "analysis_case_id": "case-a",
                "resolved_contract": {"contract_fingerprint": "a" * 64, "product_version": "v1", "identity": {"product_id": "2.1", "name_zh": "雪球", "underlyings": ["000905.SH"], "currency": "CNY"}},
            }
            app.results.commit_module_run(identity, task["task_id"], "payoffer", succeeded)
            app.results.commit_module_run(identity, task["task_id"], "pricer", {"run_id": "run-failed", "ok": False, "status": "failed"})
            catalog = app.results.list_report_sources(tenant_id="tenant-a")
            self.assertEqual(len(catalog["sources"]), 1)
            source = catalog["sources"][0]
            self.assertNotIn("path", repr(source).lower())
            self.assertEqual(source["candidates"][0]["module_run_refs"]["payoff"]["run_id"], "run-success")
            self.assertEqual(app.results.list_report_sources(tenant_id="tenant-b")["sources"], [])
            self.assertEqual(app.results.get_report_source(tenant_id="tenant-a", source_id=source["source_id"])["source_id"], source["source_id"])


if __name__ == "__main__":
    unittest.main()
