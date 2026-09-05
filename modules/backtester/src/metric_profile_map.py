"""65个OptionReg产品到Backtester专属指标的显式映射。

映射按稳定product_id维护，不依赖中文产品名称、monitor字段猜测或页面文案。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class MetricProfileSpec:
    profile_id: str
    display_name: str
    supported: bool = True
    unsupported_reason: str | None = None


# 每类报告必须存在的最小顶层事实。这里只声明输出契约，不计算指标。
REQUIRED_PROFILE_OUTPUT_KEYS: dict[str, tuple[str, ...]] = {
    "terminal_payoff": (
        "terminal_performance", "terminal_performance_sign", "terminal_segments",
        "selected_path_case_contract_settlement_return",
    ),
    "single_knock_out": ("events", "trigger_vs_untriggered"),
    "single_knock_in": ("events", "knock_in_outcomes"),
    "touch_binary": ("events", "touch_vs_untouched"),
    "airbag": ("events", "buffer_outcomes", "knock_in_outcomes", "selected_path_case_contract_settlement_return"),
    "accumulator": (
        "events", "accumulated_quantity", "contract_settlement_return_per_accumulated_unit",
        "knock_out_vs_full_term", "contract_purchase_price", "quantity_multiplier",
    ),
    "dual_knock_autocall": ("events", "three_outcome_summary", "conditional_summary"),
    "coupon_autocall": ("events", "coupon_observations", "coupon_payment"),
    "single_knock_out_autocall": ("events", "trigger_vs_untriggered"),
    "shark_fin": (
        "events", "trigger_vs_untriggered", "terminal_performance",
        "selected_path_case_contract_settlement_return",
    ),
    "variance_swap": (
        "realized_volatility", "realized_variance", "realized_volatility_vs_strike",
        "volatility_buckets", "variance_contract_settlement_return",
    ),
    "range_accrual": ("range_observations", "in_range_observation_ratio", "range_accrual_contract_settlement_return"),
}


def _assign(profile_id: str, display_name: str, product_ids: Iterable[str]) -> dict[str, MetricProfileSpec]:
    return {product_id: MetricProfileSpec(profile_id, display_name) for product_id in product_ids}


METRIC_PROFILE_MAP: dict[str, MetricProfileSpec] = {}
METRIC_PROFILE_MAP.update(_assign("terminal_payoff", "到期损益型", (
    "1.1", "1.2", "2.1", "2.2", "2.3", "2.4", "3.1", "3.2", "3.3", "3.4",
    "5.1", "5.2", "5.3", "5.4", "9.1", "9.2", "9.3",
)))
METRIC_PROFILE_MAP.update(_assign("single_knock_out", "单敲出事件型", ("4.1", "4.2", "4.5", "4.6")))
METRIC_PROFILE_MAP.update(_assign("single_knock_in", "单敲入事件型", ("4.3", "4.4", "4.7", "4.8")))
METRIC_PROFILE_MAP.update(_assign("touch_binary", "触碰二元型", ("5.5", "5.6")))
METRIC_PROFILE_MAP.update(_assign("airbag", "安全气囊型", ("6.1", "6.2", "6.3")))
METRIC_PROFILE_MAP.update(_assign("accumulator", "累购型", ("7.1",)))
METRIC_PROFILE_MAP.update(_assign("dual_knock_autocall", "双障碍自动赎回型", (
    "8.1", "8.2", "8.3", "8.4", "8.5", "8.6", "8.7", "8.8", "8.9", "8.10",
    "8.11", "8.12", "8.13", "8.14", "8.15", "8.18", "8.22", "8.23", "8.26",
)))
METRIC_PROFILE_MAP.update(_assign("coupon_autocall", "票息观察型", ("8.19", "8.20", "8.24", "8.25")))
METRIC_PROFILE_MAP.update(_assign("single_knock_out_autocall", "单敲出自动赎回型", (
    "8.16", "8.17", "8.21", "8.27", "8.28", "8.29",
)))
METRIC_PROFILE_MAP.update(_assign("shark_fin", "鲨鱼鳍型", ("9.5", "9.6", "9.7")))
METRIC_PROFILE_MAP.update(_assign("variance_swap", "方差收益型", ("9.4",)))
METRIC_PROFILE_MAP.update(_assign("range_accrual", "区间计息型", ("9.8",)))
def metric_profile_for(product_id: str) -> MetricProfileSpec:
    try:
        return METRIC_PROFILE_MAP[str(product_id)]
    except KeyError as error:
        raise KeyError(f"产品{product_id}未绑定Backtester MetricProfile") from error


def required_profile_output_keys(profile_id: str) -> tuple[str, ...]:
    try:
        return REQUIRED_PROFILE_OUTPUT_KEYS[profile_id]
    except KeyError as error:
        raise KeyError(f"MetricProfile {profile_id}未定义必需输出") from error


def validate_metric_profile_coverage(product_ids: Iterable[str]) -> tuple[str, ...]:
    product_id_set = {str(item) for item in product_ids}
    mapped = set(METRIC_PROFILE_MAP)
    issues: list[str] = []
    missing = sorted(product_id_set - mapped)
    extra = sorted(mapped - product_id_set)
    if missing:
        issues.append(f"未映射产品：{','.join(missing)}")
    if extra:
        issues.append(f"无对应OptionReg产品的映射：{','.join(extra)}")
    missing_output_contracts = sorted({
        spec.profile_id for spec in METRIC_PROFILE_MAP.values()
        if spec.profile_id not in REQUIRED_PROFILE_OUTPUT_KEYS
    })
    if missing_output_contracts:
        issues.append(f"未定义必需输出的Profile：{','.join(missing_output_contracts)}")
    return tuple(issues)
