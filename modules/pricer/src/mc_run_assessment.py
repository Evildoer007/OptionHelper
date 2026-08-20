"""Monte Carlo运行配置与标准误披露。"""

from __future__ import annotations

from dataclasses import dataclass
import math

from .engines.pricing_core.engine.derivatives.results import PricingResult


_PV_ZERO_TOLERANCE = 1e-12


@dataclass(frozen=True)
class MonteCarloRunAssessment:
    status: str
    eligible: bool
    diagnostics: dict[str, object]
    message: str | None = None


def assess_monte_carlo_run(
    result: PricingResult,
    *,
    method: str,
    path_count: int,
    demo_mode: bool,
) -> MonteCarloRunAssessment:
    """披露实际路径数和标准误，不按路径数或相对误差拒绝结果。"""
    if method != "monte_carlo":
        return MonteCarloRunAssessment("quote_eligible", True, {"applies": False})

    pv = result.pv_points_100
    standard_error = result.standard_error_points_100
    relative_error = None
    if (
        pv is not None
        and math.isfinite(float(pv))
        and abs(float(pv)) > _PV_ZERO_TOLERANCE
        and standard_error is not None
        and math.isfinite(float(standard_error))
        and float(standard_error) >= 0.0
    ):
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
        diagnostics["decision"] = "not_quote_test_run"
        return MonteCarloRunAssessment(
            "demo_only",
            False,
            diagnostics,
            "Monte Carlo测试运行不可用于正式报价",
        )
    diagnostics["decision"] = "configured_paths"
    return MonteCarloRunAssessment("configured_paths", True, diagnostics)


__all__ = ("MonteCarloRunAssessment", "assess_monte_carlo_run")
