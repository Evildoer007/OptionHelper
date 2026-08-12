from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = ROOT / "core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from runtime.ports.result_selection import ResultSelectionPort, require_result_selection_port
from runtime.protocol.module_host import (
    HostObjectRef,
    ModuleHostContextError,
    issue_capability_token,
    require_host_bound_run_contract,
    validate_module_host_context,
    verify_module_host_context,
)


HASH = "a" * 64
SECRET = b"module-host-test-secret"
SESSION_ID = "session-1"
PRINCIPAL_ID = "principal-1"
NOW = 1_800_000_000


class _SelectionPort:
    def list_report_sources(self, *, tenant_id: str, task_id: str | None = None, query: str | None = None) -> dict[str, object]:
        return {"tenant_id": tenant_id, "sources": []}

    def get_report_source(self, *, tenant_id: str, source_id: str) -> dict[str, object]:
        return {"tenant_id": tenant_id, "source_id": source_id}


class ModuleHostContextTest(unittest.TestCase):
    def payload(self) -> dict[str, object]:
        token = issue_capability_token(
            token_secret=SECRET,
            session_id=SESSION_ID,
            principal_id=PRINCIPAL_ID,
            session_ref="local:opaque-session",
            module="pricer",
            expires_at=NOW + 300,
            context_id="mhc_test_context_0001",
            page_hash=HASH,
            audience="option-helper-app",
            host_kind="app",
            request_policy=("module.run",),
            capability_version="12.1",
            protocol_version="v1.1",
            task_id="task-1",
            analysis_case_id="case-1",
            candidate_id="candidate-1",
            catalog_version="catalog-1",
            contract_fingerprint=HASH,
        )
        return {
            "session_ref": "local:opaque-session",
            "capability_token": token,
            "analysis_case_id": "case-1",
            "task_id": "task-1",
            "candidate_id": "candidate-1",
            "catalog_version": "catalog-1",
            "contract_fingerprint": HASH,
            "module": "pricer",
            "page_hash": HASH,
            "capability_version": "12.1",
            "protocol_version": "v1.1",
            "context_id": "mhc_test_context_0001",
            "host_kind": "app",
            "request_policy": ["module.run"],
        }

    def test_current_app_host_payload_is_adapter_compatible_and_verifiable(self) -> None:
        context = validate_module_host_context(self.payload(), expected_module="pricer", now=NOW)
        self.assertEqual(context.module, "pricer")
        self.assertEqual(context.to_payload(), self.payload())
        self.assertIs(
            verify_module_host_context(
                context,
                token_secret=SECRET,
                session_id=SESSION_ID,
                principal_id=PRINCIPAL_ID,
                audience="option-helper-app",
                now=NOW,
            ),
            context,
        )

    def test_task_case_and_candidate_scope_are_hmac_bound(self) -> None:
        payload = self.payload()
        payload["task_id"] = "task-other"
        context = validate_module_host_context(payload, now=NOW)
        with self.assertRaisesRegex(ModuleHostContextError, "签名或模块范围"):
            verify_module_host_context(
                context,
                token_secret=SECRET,
                session_id=SESSION_ID,
                principal_id=PRINCIPAL_ID,
                audience="option-helper-app",
                now=NOW,
            )

    def test_complete_authorization_context_is_hmac_bound(self) -> None:
        mutations = (
            ("host_kind", "local-development"),
            ("request_policy", ["conversation.tool.run"]),
            ("capability_version", "forged-capability"),
            ("protocol_version", "forged-protocol"),
            ("contract_ref", HostObjectRef("contract-1", "resolved-contract/v1", HASH).to_payload()),
            ("config_ref", HostObjectRef("config-1", "pricing-config/v1", HASH).to_payload()),
            ("result_refs", [{
                "module": "pricer", "tenant_id": "local", "task_id": "task-1", "run_id": "run-1",
                "expected_semantic_result_hash": HASH,
                "expected_artifact_manifest_hash": HASH,
            }]),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                payload = self.payload()
                payload[field] = value
                context = validate_module_host_context(payload, now=NOW)
                with self.assertRaisesRegex(ModuleHostContextError, "签名|范围"):
                    verify_module_host_context(
                        context,
                        token_secret=SECRET,
                        session_id=SESSION_ID,
                        principal_id=PRINCIPAL_ID,
                        audience="option-helper-app",
                        now=NOW,
                    )

    def test_formal_payload_cannot_fall_back_to_legacy_host_defaults(self) -> None:
        for field in ("context_id", "host_kind", "request_policy"):
            with self.subTest(field=field):
                payload = self.payload()
                payload.pop(field)
                with self.assertRaisesRegex(ModuleHostContextError, "字段无效"):
                    validate_module_host_context(payload, now=NOW)

    def test_recommender_module_run_contract_must_match_host_context(self) -> None:
        context = validate_module_host_context(self.payload(), now=NOW)
        run = {"candidate_id": "candidate-1", "catalog_version": "catalog-1", "contract_fingerprint": HASH}
        self.assertIs(require_host_bound_run_contract(run, context), run)
        run["candidate_id"] = "candidate-other"
        with self.assertRaisesRegex(ModuleHostContextError, "candidate_id"):
            require_host_bound_run_contract(run, context)

        unbound = validate_module_host_context({**self.payload(), "candidate_id": None}, now=NOW)
        with self.assertRaisesRegex(ModuleHostContextError, "candidate_id"):
            require_host_bound_run_contract(
                {"candidate_id": "module-chosen", "catalog_version": "catalog-1", "contract_fingerprint": HASH},
                unbound,
            )

    def test_context_allows_only_opaque_contract_config_and_result_references(self) -> None:
        payload = self.payload()
        payload.update({
            "contract_ref": HostObjectRef("contract-1", "optionhelper.resolved-contract/v1", HASH).to_payload(),
            "config_ref": HostObjectRef("config-1", "optionhelper.pricing-config/v1", HASH).to_payload(),
            "result_refs": [{
                "module": "pricer", "tenant_id": "local", "task_id": "task-1", "run_id": "run-1",
                "expected_semantic_result_hash": HASH,
                "expected_artifact_manifest_hash": HASH,
            }],
        })
        context = validate_module_host_context(payload, now=NOW)
        self.assertEqual(context.contract_ref.reference_id if context.contract_ref else None, "contract-1")
        self.assertEqual(context.result_refs[0].run_id, "run-1")
        payload["result_refs"] = [{"path": "/private/result"}]
        with self.assertRaisesRegex(ModuleHostContextError, "result_refs"):
            validate_module_host_context(payload, now=NOW)

    def test_token_is_scoped_to_module_and_cannot_be_reused_after_expiry(self) -> None:
        wrong_scope = self.payload()
        wrong_scope["module"] = "backtester"
        context = validate_module_host_context(wrong_scope, now=NOW)
        with self.assertRaisesRegex(ModuleHostContextError, "签名或模块范围"):
            verify_module_host_context(
                context,
                token_secret=SECRET,
                session_id=SESSION_ID,
                principal_id=PRINCIPAL_ID,
                audience="option-helper-app",
                now=NOW,
            )
        expired = self.payload()
        expired["capability_token"] = issue_capability_token(
            token_secret=SECRET,
            session_id=SESSION_ID,
            principal_id=PRINCIPAL_ID,
            session_ref="local:opaque-session",
            module="pricer",
            expires_at=NOW,
            context_id="mhc_test_context_0001",
            page_hash=HASH,
            audience="option-helper-app",
            host_kind="app",
            request_policy=("module.run",),
            capability_version="12.1",
            protocol_version="v1.1",
            task_id="task-1",
            analysis_case_id="case-1",
            candidate_id="candidate-1",
            catalog_version="catalog-1",
            contract_fingerprint=HASH,
        )
        with self.assertRaisesRegex(ModuleHostContextError, "已过期"):
            validate_module_host_context(expired, now=NOW)

        with self.assertRaisesRegex(ModuleHostContextError, "控制字符"):
            issue_capability_token(
                token_secret=SECRET,
                session_id="session\x1fforged",
                principal_id=PRINCIPAL_ID,
                session_ref="local:opaque-session",
                module="pricer",
                expires_at=NOW + 300,
                context_id="mhc_test_context_0001",
                page_hash=HASH,
                audience="option-helper-app",
                host_kind="app",
                request_policy=("module.run",),
                capability_version="12.1",
                protocol_version="v1.1",
            )

    def test_result_selection_port_is_a_core_injection_boundary(self) -> None:
        port = _SelectionPort()
        self.assertIsInstance(port, ResultSelectionPort)
        self.assertIs(require_result_selection_port(port), port)
        with self.assertRaises(TypeError):
            require_result_selection_port(object())


if __name__ == "__main__":
    unittest.main()
