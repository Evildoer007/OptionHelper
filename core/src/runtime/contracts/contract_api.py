"""Payoffer、Pricer与Backtester共用的稳定合同入口。"""

from runtime.protocol.version import RESOLVED_CONTRACT_SCHEMA_ID

from .contract_engine import (
    Cashflow,
    ContractResolutionError,
    FormulaError,
    PayoffEvaluation,
    PayoffInput,
    PricePath,
    ResolvedContract,
    bind_term_symbols,
    compute_monitor_values,
    evaluate_contract,
    evaluate_formula,
    get_product,
    load_registry,
    make_payoff_input,
    resolve_contract,
    resolve_schedule,
    resolve_schedules,
    validate_registry,
    verify_product_snapshot_binding,
)

__all__ = (
    "Cashflow",
    "ContractResolutionError",
    "FormulaError",
    "PayoffEvaluation",
    "PayoffInput",
    "PricePath",
    "ResolvedContract",
    "RESOLVED_CONTRACT_SCHEMA_ID",
    "bind_term_symbols",
    "compute_monitor_values",
    "evaluate_formula",
    "evaluate_contract",
    "get_product",
    "load_registry",
    "make_payoff_input",
    "resolve_contract",
    "resolve_schedule",
    "resolve_schedules",
    "validate_registry",
    "verify_product_snapshot_binding",
)
