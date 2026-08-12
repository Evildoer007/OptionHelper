"""Backtester的ModuleHost v2授权与外置DataStore边界回归。"""

# ruff: noqa: E402

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT, PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "backtester" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from core import tool_entry
from modules.backtester.service import BacktesterRuntime, BacktesterWebInputError, _formal_backtest_input, call_tool
from modules.backtester.tests.fixtures import modulehost_v2_scope, two_asset_history
from modules.backtester import HistoricalData
from runtime.adapters.local_store import LocalResultStore
from runtime.contracts.contract_api import resolve_contract
from runtime.protocol.models import DataAssetRef
from runtime.protocol.module_host import ModuleHostContextError, verify_module_host_context


PAYLOAD = b"date,asset_id,close\n2024-01-02,000905.SH,100\n2024-01-03,000905.SH,101\n2024-01-04,000905.SH,102\n"


class _ExternalStore:
    def read_bytes(self, _reference: object, *, tenant_id: str) -> bytes:
        if tenant_id != "tenant-a":
            raise PermissionError("tenant mismatch")
        return PAYLOAD


def _data_ref(content_hash: str, *, access_scope: tuple[str, ...] = ("read",)) -> DataAssetRef:
    return DataAssetRef(
        data_asset_id="modulehost-v2-market",
        storage_ref="app-data:tenant-a:modulehost-v2-market",
        media_type="text/csv",
        schema_id="market-history-v1",
        asset_ids=("000905.SH",),
        normalized_fields=("date", "asset_id", "close"),
        coverage={"by_asset": {"000905.SH": {
            "start_date": "2024-01-02", "end_date": "2024-01-04", "row_count": 3,
        }}},
        row_count=3,
        price_convention={
            "contract_close_field": "close",
            "contract_adjustment": "unadjusted",
            "hv_close_field": None,
            "hv_adjustment": None,
            "close_equals_adj_close": False,
        },
        content_hash=content_hash,
        lineage={"fixture": "modulehost-v2"},
        tenant_id="tenant-a",
        created_by="host-test",
        access_scope=access_scope,
    )


class ModuleHostV2BoundaryTest(unittest.TestCase):
    def test_public_catalog_uses_verified_caller_and_matching_host_context(self) -> None:
        caller, context, token_secret = modulehost_v2_scope(request_policy=("module.catalog",))
        verify_module_host_context(
            context,
            token_secret=token_secret,
            session_id=caller.session_id,
            principal_id=caller.principal_id,
            audience=caller.audience,
            now=1_800_000_000,
        )
        output = tool_entry.call_tool(
            "backtester",
            {"action": "catalog"},
            caller_context=caller,
            host_context=context,
        )
        self.assertTrue(output["ok"])
        self.assertEqual(output["module"], "backtester")
        self.assertEqual(len(output["products"]), 65)

        with self.assertRaisesRegex(ModuleHostContextError, "签名或模块范围"):
            verify_module_host_context(
                context,
                token_secret=token_secret,
                session_id=caller.session_id,
                principal_id=replace(caller, principal_id="other-principal").principal_id,
                audience=caller.audience,
                now=1_800_000_000,
            )

    def test_module_run_permission_cannot_authorize_catalog(self) -> None:
        caller, context, _ = modulehost_v2_scope(request_policy=("module.run",))
        with self.assertRaisesRegex(tool_entry.ToolDispatchError, "module.catalog"):
            tool_entry.call_tool(
                "backtester",
                {"action": "catalog"},
                caller_context=caller,
                host_context=context,
            )

    def test_formal_input_cannot_smuggle_a_request_owned_datastore(self) -> None:
        contract = resolve_contract("2.1", identity={"underlyings": ["000905.SH"]})
        reference = DataAssetRef(**HistoricalData.from_frame(two_asset_history()).data_asset_ref)
        with self.assertRaisesRegex(BacktesterWebInputError, "字段必须为"):
            _formal_backtest_input({
                "contract": contract,
                "backtest_config": {},
                "historical_data": reference,
                "data_store": object(),
            })
        caller, context, _ = modulehost_v2_scope(request_policy=("module.catalog",))
        with self.assertRaisesRegex(BacktesterWebInputError, "Host私有依赖"):
            call_tool(
                {"action": "catalog", "data_store": object()},
                host_context=context,
                tenant_id=caller.tenant_id,
            )

    def test_host_datastore_is_rejected_for_catalog_and_legacy_run(self) -> None:
        data_store = _ExternalStore()
        catalog_caller, catalog_context, _ = modulehost_v2_scope(request_policy=("module.catalog",))
        with self.assertRaisesRegex(BacktesterWebInputError, "正式BacktestInput"):
            call_tool(
                {"action": "catalog"},
                host_context=catalog_context,
                tenant_id=catalog_caller.tenant_id,
                data_store=data_store,
            )

        contract = resolve_contract("2.1", identity={"underlyings": ["000905.SH"]})
        run_caller, run_context, _ = modulehost_v2_scope(contract)
        with self.assertRaisesRegex(BacktesterWebInputError, "正式BacktestInput"):
            call_tool(
                {
                    "action": "run",
                    "product_id": "2.1",
                    "identity": {"underlyings": ["000905.SH"]},
                    "backtest_config": {},
                },
                host_context=run_context,
                tenant_id=run_caller.tenant_id,
                data_store=data_store,
            )

    def test_host_datastore_requires_a_real_module_host_context(self) -> None:
        contract = resolve_contract("2.1", identity={"underlyings": ["000905.SH"]})
        reference = _data_ref(sha256(PAYLOAD).hexdigest())
        with self.assertRaisesRegex(BacktesterWebInputError, "ModuleHostContext"):
            call_tool(
                {
                    "action": "run",
                    "contract": contract,
                    "backtest_config": {},
                    "historical_data": reference,
                },
                data_store=_ExternalStore(),
                tenant_id="tenant-a",
            )

    def test_formal_runtime_rejects_payload_hash_mismatch(self) -> None:
        contract = resolve_contract(
            "2.1", identity={"underlyings": ["000905.SH"]}, term_overrides={"T": 2 / 365},
        )
        caller, context, _ = modulehost_v2_scope(contract)
        with tempfile.TemporaryDirectory(prefix="backtester-v2-tamper-") as temporary:
            runtime = BacktesterRuntime(
                data_store=_ExternalStore(),
                result_store=LocalResultStore(temporary),
                tenant_id=caller.tenant_id,
            )
            with self.assertRaisesRegex(BacktesterWebInputError, "content_hash"):
                runtime.run_formal({
                    "contract": contract,
                    "backtest_config": {"entry_rule": "explicit", "entry_dates": ["2024-01-02"]},
                    "historical_data": _data_ref("0" * 64),
                }, host_context=context)

            with self.assertRaisesRegex(BacktesterWebInputError, "access_scope"):
                runtime.run_formal({
                    "contract": contract,
                    "backtest_config": {"entry_rule": "explicit", "entry_dates": ["2024-01-02"]},
                    "historical_data": _data_ref(sha256(PAYLOAD).hexdigest(), access_scope=()),
                }, host_context=context)

    def test_core_public_call_tool_injects_host_owned_datastore_keyword(self) -> None:
        contract = resolve_contract(
            "2.1", identity={"underlyings": ["000905.SH"]}, term_overrides={"T": 2 / 365},
        )
        caller, context, _ = modulehost_v2_scope(contract)
        with tempfile.TemporaryDirectory(prefix="backtester-v2-store-") as temporary:
            output = tool_entry.call_tool(
                "backtester",
                {
                    "contract": contract,
                    "backtest_config": {"entry_rule": "explicit", "entry_dates": ["2024-01-02"]},
                    "historical_data": _data_ref(sha256(PAYLOAD).hexdigest()),
                },
                caller_context=caller,
                host_context=context,
                result_store=LocalResultStore(temporary),
                data_store=_ExternalStore(),
            )
        self.assertTrue(output["ok"])
        self.assertEqual(output["data_refs"][0]["content_hash"], sha256(PAYLOAD).hexdigest())

    def test_core_public_call_tool_accepts_conversation_tool_run(self) -> None:
        contract = resolve_contract(
            "2.1", identity={"underlyings": ["000905.SH"]}, term_overrides={"T": 2 / 365},
        )
        caller, context, _ = modulehost_v2_scope(contract, request_policy=("conversation.tool.run",))
        request = {
            "contract": contract,
            "backtest_config": {"entry_rule": "explicit", "entry_dates": ["2024-01-02"]},
            "historical_data": _data_ref(sha256(PAYLOAD).hexdigest()),
        }
        with tempfile.TemporaryDirectory(prefix="backtester-optchat-store-") as temporary:
            output = tool_entry.call_tool(
                "backtester",
                request,
                caller_context=caller,
                host_context=context,
                result_store=LocalResultStore(temporary),
                data_store=_ExternalStore(),
            )
        self.assertTrue(output["ok"])
        self.assertEqual(output["status"], "succeeded")

    def test_module_catalog_permission_cannot_authorize_formal_run(self) -> None:
        contract = resolve_contract(
            "2.1", identity={"underlyings": ["000905.SH"]}, term_overrides={"T": 2 / 365},
        )
        caller, context, _ = modulehost_v2_scope(contract, request_policy=("module.catalog",))
        with tempfile.TemporaryDirectory(prefix="backtester-catalog-reject-") as temporary:
            with self.assertRaisesRegex(tool_entry.ToolDispatchError, "module.run或conversation.tool.run"):
                tool_entry.call_tool(
                    "backtester",
                    {
                        "contract": contract,
                        "backtest_config": {"entry_rule": "explicit", "entry_dates": ["2024-01-02"]},
                        "historical_data": _data_ref(sha256(PAYLOAD).hexdigest()),
                    },
                    caller_context=caller,
                    host_context=context,
                    result_store=LocalResultStore(temporary),
                    data_store=_ExternalStore(),
                )

    def test_formal_service_call_rejects_missing_host_datastore(self) -> None:
        contract = resolve_contract(
            "2.1", identity={"underlyings": ["000905.SH"]}, term_overrides={"T": 2 / 365},
        )
        caller, context, _ = modulehost_v2_scope(contract)
        with tempfile.TemporaryDirectory(prefix="backtester-missing-store-") as temporary:
            with self.assertRaisesRegex(BacktesterWebInputError, "Host注入data_store"):
                call_tool(
                    {
                        "contract": contract,
                        "backtest_config": {"entry_rule": "explicit", "entry_dates": ["2024-01-02"]},
                        "historical_data": _data_ref(sha256(PAYLOAD).hexdigest()),
                    },
                    host_context=context,
                    result_store=LocalResultStore(temporary),
                    tenant_id=caller.tenant_id,
                )


if __name__ == "__main__":
    unittest.main()
