"""Core Runtime安全回归：仅覆盖可复现的边界漏洞。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = ROOT / "core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from core import tool_entry
from runtime.adapters.local_host import JsonHttpEndpoint, LocalHostAuthority, LocalHostError, LocalProjectLayout
from runtime.adapters import local_store
from runtime.adapters.local_store import LocalDataStore, LocalResultStore, StoreError
from runtime.contracts.contract_api import resolve_contract
from runtime.contracts.input_adapter import ContractResolutionError, prepare_compute_request
from runtime.knowledger import registry_loader
from runtime.protocol.models import CallerContext


def _registry_source(product_id: str) -> str:
    return (
        "REGISTRY = {\n"
        "  'term_catalog': {'K': {'symbol': 'K'}},\n"
        f"  'products': {{'{product_id}': {{}}}},\n"
        "}\n"
    )


class RuntimeSecurityRegressionTest(unittest.TestCase):
    def test_formal_inputs_classify_history_and_calendar_by_schema(self) -> None:
        """行情与交易日历必须由Schema分类，不能由调用方数组顺序决定。"""

        contract = resolve_contract("2.1", identity={
            "underlyings": ["000300.SH"],
            "contract_start_date": "2026-08-07",
            "reference_prices": {"000300.SH": 4000.0},
        })
        def data_ref(identifier: str, schema_id: str, media_type: str) -> dict[str, object]:
            return {
                "data_asset_id": identifier,
                "storage_ref": f"assets/{identifier}",
                "media_type": media_type,
                "schema_id": schema_id,
                "asset_ids": ["000300.SH"],
                "normalized_fields": ["date", "close"],
                "coverage": {"start": "2026-08-01", "end": "2026-08-07"},
                "row_count": 5,
                "price_convention": {"adjustment": "close"},
                "content_hash": "a" * 64,
                "lineage": {"provider": "test"},
                "tenant_id": "local",
                "created_by": "local-project-user",
                "access_scope": ["read"],
                "partition_spec": {},
            }

        request = {
            "action": "run",
            "product_id": contract.product_id,
            "identity": dict(contract.identity),
            "term_overrides": {},
            "pricing_config": {"valuation_date": "2026-08-07", "model_method": "black_scholes"},
        }
        prepared = prepare_compute_request(
            "pricer", request,
            data_refs=(
                data_ref("calendar", "trading-calendar", "application/json"),
                data_ref("history", "market-history-v1", "text/csv"),
            ),
            resolved_contract=contract.to_protocol_dict(),
        )["request"]
        self.assertEqual(prepared["market_data_refs"][0]["schema_id"], "market-history-v1")
        self.assertEqual(prepared["trading_calendar_ref"]["schema_id"], "trading-calendar")
        with self.assertRaisesRegex(ContractResolutionError, "DataAssetRef.schema_id"):
            prepare_compute_request(
                "pricer", request,
                data_refs=(data_ref("unknown", "market-history-v2", "text/csv"),),
                resolved_contract=contract.to_protocol_dict(),
            )

    def test_release_registry_must_match_the_capability_manifest(self) -> None:
        """发行模式先校验已安装清单，不把首次读取当作发布认证。"""

        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "option-helper"
            source = release / "scripts" / "knowledger" / "optionreg.py"
            source.parent.mkdir(parents=True)
            safe = _registry_source("safe").encode("utf-8")
            source.write_bytes(safe)
            (release / "capability-manifest.json").write_text(
                '{"content_hashes":{"scripts/knowledger/optionreg.py":"'
                + sha256(safe).hexdigest() + '"}}',
                encoding="utf-8",
            )
            paths = SimpleNamespace(mode="release", project_root=release)
            with (
                patch.object(registry_loader, "get_default_registry_path", return_value=source),
                patch.object(registry_loader, "bootstrap_runtime", return_value=paths),
                patch.dict(registry_loader._REGISTRY_PINNED_HASHES, clear=True),
            ):
                registry_loader._load_registry_cached.cache_clear()
                self.assertIn("safe", registry_loader.load_registry()["products"])
                source.write_text(_registry_source("replaced"), encoding="utf-8")
                registry_loader._load_registry_cached.cache_clear()
                with self.assertRaisesRegex(registry_loader.RegistryLoadError, "Capability清单哈希"):
                    registry_loader.load_registry()

    def test_caller_context_enforces_the_protocol_identity_shape(self) -> None:
        """内部调用不能绕过CallerContext Schema的关键身份约束。"""

        with self.assertRaisesRegex(ValueError, "CallerContext.role"):
            CallerContext("tenant-a", "principal-a", "", ("module.run",), "session-a", "option-helper-app")
        with self.assertRaisesRegex(ValueError, "capabilities"):
            CallerContext(
                "tenant-a", "principal-a", "admin", ("module.run", "module.run"), "session-a", "option-helper-app",
            )

    def test_registry_rejects_a_post_start_replacement(self) -> None:
        """启动后替换OptionReg不得触发新的Python执行。"""

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "optionreg.py"
            source.write_text(_registry_source("safe"), encoding="utf-8")
            registry_loader._load_registry_cached.cache_clear()
            try:
                with patch.object(registry_loader, "get_default_registry_path", return_value=source), patch.dict(
                    registry_loader._REGISTRY_PINNED_HASHES, clear=True,
                ):
                    registry = registry_loader.load_registry()
                    source.write_text(_registry_source("replaced"), encoding="utf-8")
                    with self.assertRaisesRegex(registry_loader.RegistryLoadError, "启动后发生变化"):
                        registry_loader.load_registry()
            finally:
                registry_loader._load_registry_cached.cache_clear()
            self.assertEqual(set(registry["products"]), {"safe"})

    def test_data_store_returns_verified_bytes_when_payload_changes_after_validation(self) -> None:
        """read_bytes不得在验证后对同一路径进行第二次不受保护读取。"""

        with tempfile.TemporaryDirectory() as temporary:
            store = LocalDataStore(Path(temporary) / "data")
            ref = store.put_bytes(
                tenant_id="tenant-a", data_asset_id="asset-a", payload=b"verified", media_type="text/plain", schema_id="test",
            )
            original_read = local_store._read_regular_bytes
            hook_called = False

            def read_then_replace(path: Path, label: str) -> bytes:
                nonlocal hook_called
                payload = original_read(path, label)
                if path.name == "payload.bin":
                    hook_called = True
                    path.write_bytes(b"replaced")
                return payload

            with patch.object(local_store, "_read_regular_bytes", side_effect=read_then_replace):
                self.assertEqual(store.read_bytes(ref, tenant_id="tenant-a"), b"verified")
            self.assertTrue(hook_called)

    def test_local_host_creates_caller_context_inside_the_authority_boundary(self) -> None:
        """local-development调用者不能再自带可伪造CallerContext。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "skill").mkdir()
            (root / "project").mkdir()
            authority = LocalHostAuthority(LocalProjectLayout.create(skill_root=root / "skill", project_root=root / "project"))
            context = authority.context(
                "pricer", task_id="task-a", analysis_case_id="case-a", candidate_id="candidate-a",
                catalog_version="catalog-a", contract_fingerprint="a" * 64,
            )
            with patch.object(tool_entry, "_call_validated_tool", return_value={"ok": True}) as invoke:
                result = tool_entry.call_local_tool(
                    "pricer", {"action": "catalog"}, authority=authority,
                    request_id="request-a", host_context=context,
                )
            self.assertTrue(result["ok"])
            self.assertEqual(invoke.call_args.kwargs["caller_context"], authority.caller(request_id="request-a"))
            with self.assertRaises(TypeError):
                tool_entry.call_local_tool(  # type: ignore[call-arg]
                    "pricer", {"action": "catalog"}, authority=authority,
                    request_id="request-a", host_context=context, caller_context=CallerContext(
                        "local", "forged", "local", ("module.catalog",), "forged", "option-helper-local",
                    ),
                )

    def test_project_store_rejects_a_symlink_inserted_after_layout_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill, project, escaped = root / "skill", root / "project", root / "escaped"
            skill.mkdir()
            project.mkdir()
            escaped.mkdir()
            layout = LocalProjectLayout.create(skill_root=skill, project_root=project)
            (project / "data").symlink_to(escaped, target_is_directory=True)
            with self.assertRaisesRegex(LocalHostError, "DataStore.*符号链接"):
                layout.initialize()

    def test_store_rejects_a_root_replaced_by_symlink_after_opening(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, escaped = root / "data", root / "escaped"
            store = LocalDataStore(data)
            escaped.mkdir()
            data.rename(root / "data-old")
            data.symlink_to(escaped, target_is_directory=True)
            with self.assertRaisesRegex(StoreError, "符号链接|发生变化"):
                store.put_bytes(
                    tenant_id="tenant-a", data_asset_id="asset-a", payload=b"blocked", media_type="text/plain", schema_id="test",
                )

    def test_remote_plaintext_http_and_redirects_are_rejected_for_bearer_transport(self) -> None:
        with self.assertRaisesRegex(LocalHostError, "HTTPS"):
            JsonHttpEndpoint("http://model.example.test")
        self.assertEqual(JsonHttpEndpoint("http://127.0.0.1:8080").base_url, "http://127.0.0.1:8080")

    def test_public_project_projection_redacts_module_exception_text(self) -> None:
        public = tool_entry.public_project_result({
            "ok": True, "status": "partial", "message": "完成",
            "module_failures": {"pricing": "/private/secret/api-key=leak"},
        })
        encoded = str(public)
        self.assertNotIn("/private/secret", encoded)
        self.assertNotIn("api-key", encoded)

    def test_result_store_can_read_one_runref_bound_file_without_a_bare_path(self) -> None:
        files = {
            "manifest.json": {"status": "succeeded"}, "input_snapshot.json": {}, "resolved_contract.json": {},
            "data_refs.json": {}, "limitations.json": {}, "result.json": {"value": 1}, "artifacts/a.txt": "verified",
        }
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalResultStore(Path(temporary) / "result")
            ref = store.commit_module_run(module="pricer", tenant_id="tenant-a", task_id="task-a", run_id="run-a", files=files)
            self.assertEqual(store.read_module_run_file(ref, "artifacts/a.txt", tenant_id="tenant-a"), b"verified")


if __name__ == "__main__":
    unittest.main()
