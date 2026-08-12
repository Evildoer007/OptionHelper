"""从任意工作目录直接运行的离线Vanilla示例。"""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pricer_engine_main", ROOT / "main.py")
if spec is None or spec.loader is None:
    raise ImportError("无法加载Pricer内部数值入口")
pricing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pricing)

print(f"families={pricing.list_families()}")
print(f"vanilla_structures={pricing.list_structures('VANILLA')}")
print(pricing.describe_structure("VANILLA", "EUROPEAN_VANILLA"))

parameters = {
    "contract": {
        "strike": 100.0,
        "maturity_years": 0.25,
        "call_put": "CALL",
        "basis": {
            "notional": 1_000_000.0,
            "currency": "CNY",
        },
    },
    "market": {
        "as_of": "2026-08-06",
        "spot": 100.0,
        "volatility": 0.20,
        "risk_free_rate": 0.02,
        "source": "offline_quick_start",
    },
}

run = pricing.price_option(
    "VANILLA",
    "EUROPEAN_VANILLA",
    parameters,
    "BLACK_SCHOLES",
    output="TERMINAL_AND_JSON",
)
print(f"pv_points_100={run.result.pv_points_100:.12f}")
print(f"json_output_path={run.json_output_path}")
