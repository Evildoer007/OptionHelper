"""Monte Carlo正式报价的可审计精度门禁。"""

from __future__ import annotations

from dataclasses import dataclass
import math

from .engines.pricing_core.engine.derivatives.results import PricingResult


MINIMUM_QUOTE_PATH_COUNT = 1_000
MAXIMUM_RELATIVE_STANDARD_ERROR = 0.05
_PV_ZERO_TOLERANCE = 1e-12


@dataclass(frozen=True)
class QuotePrecisionDecision:
    status: str
    eligible: bool
    diagnostics: dict[str, object]
    message: str | None = None


def assess_quote_precision(
    result: PricingResult,
    *,
    method: str,
    path_count: int,
    demo_mode: bool,
) -> QuotePrecisionDecision:
    """以路径数和SE相对PV同时判断MC结果能否用于正式报价。"""
    if method != "monte_carlo":
        return QuotePrecisionDecision("quote_eligible", True, {"applies": False})

    pv = result.pv_points_100
    standard_error = result.standard_error_points_100
    relative_error = None
    reasons: list[str] = []
    if pv is None or not math.isfinite(float(pv)) or abs(float(pv)) <= _PV_ZERO_TOLERANCE:
        reasons.append("pv_not_stable_for_relative_error")
    elif standard_error is None or not math.isfinite(float(standard_error)) or float(standard_error) < 0.0:
        reasons.append("standard_error_unavailable")
    else:
        relative_error = abs(float(standard_error) / float(pv))
        if relative_error > MAXIMUM_RELATIVE_STANDARD_ERROR:
            reasons.append("relative_standard_error_above_limit")
    if path_count < MINIMUM_QUOTE_PATH_COUNT:
        reasons.append("path_count_below_minimum")

    diagnostics: dict[str, object] = {
        "applies": True,
        "policy": "mc_relative_se_5pct_min1000",
        "path_count": path_count,
        "minimum_path_count": MINIMUM_QUOTE_PATH_COUNT,
        "pv_points_100": pv,
        "standard_error_points_100": standard_error,
        "relative_standard_error": relative_error,
        "maximum_relative_standard_error": MAXIMUM_RELATIVE_STANDARD_ERROR,
        "reasons": reasons,
    }
    if demo_mode:
        diagnostics["decision"] = "not_quote_demo"
        return QuotePrecisionDecision(
            "demo_only",
            False,
            diagnostics,
            "Monte Carlo演示结果不可用于正式报价",
        )
    if reasons:
        diagnostics["decision"] = "low_precision"
        return QuotePrecisionDecision(
            "low_precision",
            False,
            diagnostics,
            "Monte Carlo结果未同时满足最小路径数和相对标准误差门禁，不可用于正式报价",
        )
    diagnostics["decision"] = "quote_eligible"
    return QuotePrecisionDecision("quote_eligible", True, diagnostics)


__all__ = (
    "MAXIMUM_RELATIVE_STANDARD_ERROR",
    "MINIMUM_QUOTE_PATH_COUNT",
    "QuotePrecisionDecision",
    "assess_quote_precision",
)
