"""Monte Carlo运行统计。"""

from __future__ import annotations

from dataclasses import dataclass
import math

from .engines.pricing_core.engine.derivatives.results import PricingResult


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
    """记录本次MC配置和标准误，不因路径数改变已完成结果的状态。"""
    if method != "monte_carlo":
        return QuotePrecisionDecision("quote_eligible", True, {"applies": False})

    pv = result.pv_points_100
    standard_error = result.standard_error_points_100
    relative_error = None
    if pv is None or not math.isfinite(float(pv)) or abs(float(pv)) <= _PV_ZERO_TOLERANCE:
        relative_error = None
    elif standard_error is None or not math.isfinite(float(standard_error)) or float(standard_error) < 0.0:
        relative_error = None
    else:
        relative_error = abs(float(standard_error) / float(pv))

    diagnostics: dict[str, object] = {
        "applies": True,
        "policy": "configured_path_count",
        "path_count": path_count,
        "pv_points_100": pv,
        "standard_error_points_100": standard_error,
        "relative_standard_error": relative_error,
    }
    if demo_mode:
        diagnostics["decision"] = "not_quote_demo"
        return QuotePrecisionDecision(
            "demo_only",
            False,
            diagnostics,
            "Monte Carlo演示结果不可用于正式报价",
        )
    diagnostics["decision"] = "configured_paths"
    return QuotePrecisionDecision("configured_paths", True, diagnostics)


__all__ = (
    "QuotePrecisionDecision",
    "assess_quote_precision",
)
