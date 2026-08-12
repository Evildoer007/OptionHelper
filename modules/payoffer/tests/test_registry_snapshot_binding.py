"""Payoffer正式协议必须验证冻结合同仍绑定当前产品快照。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "payoffer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.payoffer.service import PayoffEngineError, _formal_payoff_input
from runtime.contracts.contract_api import ResolvedContract, resolve_contract
from runtime.contracts.contract_types import semantic_hash


class PayofferRegistrySnapshotBindingTests(unittest.TestCase):
    def test_formal_input_rejects_contract_with_unbound_product_snapshot(self) -> None:
        current = resolve_contract("2.1")
        stale = ResolvedContract(
            identity={**current.identity, "product_version": "published-test-v1"},
            terms=current.terms,
            term_sources=current.term_sources,
            paths=current.paths,
            product_version="published-test-v1",
            resolved_schedules=current.resolved_schedules,
            registry_snapshot_hash="a" * 64,
            product_snapshot_hash="b" * 64,
            product_paths_hash=semantic_hash(current.paths),
        )

        with self.assertRaisesRegex(PayoffEngineError, "产品快照无效"):
            _formal_payoff_input({"contract": stale.to_protocol_dict()})


if __name__ == "__main__":
    unittest.main()
