"""正式PayoffInput只消费Core已验证合同，不重新绑定当前Registry。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "payoffer" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from runtime.contracts.contract_api import ResolvedContract, resolve_contract
from runtime.contracts.contract_types import semantic_hash
from modules.payoffer.service import _formal_payoff_input


class PayofferFormalContractBoundaryTest(unittest.TestCase):
    def test_published_product_version_is_not_rebound_to_current_registry(self) -> None:
        current = resolve_contract("2.1")
        published = ResolvedContract(
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

        payoff_input = _formal_payoff_input({"contract": published.to_protocol_dict()})

        self.assertEqual(payoff_input.contract.product_version, "published-test-v1")
        self.assertEqual(payoff_input.contract.contract_fingerprint, published.contract_fingerprint)


if __name__ == "__main__":
    unittest.main()
