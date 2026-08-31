"""Public enumerations for the unified derivatives domain."""

from __future__ import annotations

from enum import Enum


class PricingMethod(Enum):
    DISCOUNTED_CASHFLOW = "discounted_cashflow"
    BLACK_SCHOLES = "black_scholes"
    BINARY_ANALYTIC = "binary_analytic"
    REINER_RUBINSTEIN = "reiner_rubinstein"
    STATIC_REPLICATION = "static_replication"
    WORST_OF_ANALYTIC = "worst_of_analytic"
    VARIANCE_EXPECTATION = "variance_expectation"
    RANGE_ACCRUAL_ANALYTIC = "range_accrual_analytic"
    MONTE_CARLO_CPU = "monte_carlo_cpu"


class CallPut(Enum):
    CALL = "Call"
    PUT = "Put"

    @property
    def lower_name(self) -> str:
        return self.value.lower()


class AutocallKind(Enum):
    SNOWBALL = "snowball"
    PHOENIX = "phoenix"
    TRIGGER = "trigger"


class ObservationFrequency(Enum):
    DAILY = "d"
    WEEKLY = "w"
    MONTHLY = "m"


class AccumulatorQuantityBasis(Enum):
    WHOLE_CONTRACT = "whole_contract"
    PER_OBSERVATION = "per_observation"
