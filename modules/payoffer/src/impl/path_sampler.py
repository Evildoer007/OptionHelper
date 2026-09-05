"""Payoffer的路径域编译与确定性候选路径构造。

本模块只解释OptionReg已登记的分段定义域，并为共享现金流解释器提供可复核的
展示候选路径。它不包含任何产品专属损益公式，也不使用``eval``。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from math import floor, inf, isfinite, log10
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from runtime.contracts.contract_api import (
    PricePath,
    ResolvedContract,
)
from runtime.contracts.contract_api import bind_term_symbols, evaluate_formula


AXIS_VARIABLES = frozenset({"S_T", "r_T", "W_T", "sigma_realized", "n_in"})


class DomainCompileError(ValueError):
    """已登记的分段定义域无法安全编译为单变量图形区间。"""


@dataclass(frozen=True)
class Interval:
    """一维分段区间；端点可为无穷。"""

    lower: float = -inf
    upper: float = inf
    lower_closed: bool = False
    upper_closed: bool = False

    @property
    def is_point(self) -> bool:
        return self.lower == self.upper and self.lower_closed and self.upper_closed

    def intersection(self, other: "Interval") -> "Interval | None":
        lower = max(self.lower, other.lower)
        upper = min(self.upper, other.upper)
        lower_closed = self.lower_closed if self.lower > other.lower else other.lower_closed if other.lower > self.lower else self.lower_closed and other.lower_closed
        upper_closed = self.upper_closed if self.upper < other.upper else other.upper_closed if other.upper < self.upper else self.upper_closed and other.upper_closed
        if lower < upper:
            return Interval(lower, upper, lower_closed, upper_closed)
        if lower == upper and lower_closed and upper_closed:
            return Interval(lower, upper, True, True)
        return None


@dataclass(frozen=True)
class DomainThreshold:
    """来自分段定义域的真实边界。"""

    token: str
    value: float


@dataclass(frozen=True)
class CompiledDomain:
    axis: str
    intervals: tuple[Interval, ...]
    thresholds: tuple[DomainThreshold, ...]


@dataclass(frozen=True)
class CandidateTemplate:
    """展示路径的形状与内部极值，不承担现金流计算。"""

    name: str
    waypoints: tuple[tuple[float, float], ...]
    terminal_period_end: bool = False


def compile_domain(
    expression: str,
    term_variables: Mapping[str, Any],
    *,
    expected_axis: str | None = None,
) -> CompiledDomain:
    """将已登记的比较域编译成可画图的一维区间并保留边界来源。"""
    try:
        root = ast.parse(expression, mode="eval").body
    except SyntaxError as error:
        raise DomainCompileError(f"分段定义域语法无效：{expression}") from error
    names = {node.id for node in ast.walk(root) if isinstance(node, ast.Name)}
    axes = names & AXIS_VARIABLES
    if len(axes) != 1:
        raise DomainCompileError(f"分段定义域必须且只能包含一个横轴变量：{expression}")
    axis = next(iter(axes))
    if expected_axis is not None and axis != expected_axis:
        raise DomainCompileError(f"同一路径的分段横轴不一致：{expected_axis}与{axis}")
    intervals, thresholds = _compile_boolean(root, axis, term_variables)
    normalized = _merge_intervals(intervals)
    if not normalized:
        raise DomainCompileError(f"分段定义域没有有效区间：{expression}")
    return CompiledDomain(axis, tuple(normalized), tuple(_unique_thresholds(thresholds)))


def axis_scale(axis: str, domains: Iterable[CompiledDomain], contract: ResolvedContract) -> dict[str, float]:
    """由分段域和合同参考水平确定横轴显示范围。"""
    finite_bounds = [bound for domain in domains for interval in domain.intervals for bound in (interval.lower, interval.upper) if isfinite(bound)]
    reference = _reference_level(contract)
    if axis == "S_T":
        upper = max([reference * 1.55, *(finite_bounds or [reference])])
        return {"x_min": 0.0, "x_max": max(1.0, _nice_ceiling(upper * 1.12))}
    if axis == "r_T":
        lower = min([-0.65, *(finite_bounds or [0.0])])
        upper = max([0.65, *(finite_bounds or [0.0])])
        padding = max(0.08, (upper - lower) * 0.12)
        return {
            "x_min": max(-0.98, -_nice_ceiling(abs(lower - padding))),
            "x_max": _nice_ceiling(upper + padding),
        }
    if axis == "W_T":
        upper = max([reference * 1.55, *(finite_bounds or [reference])])
        return {"x_min": 0.0, "x_max": max(1.0, _nice_ceiling(upper * 1.12))}
    if axis == "sigma_realized":
        upper = max([60.0, *(finite_bounds or [0.0])])
        return {"x_min": 0.0, "x_max": _nice_ceiling(upper * 1.18)}
    if axis == "n_in":
        count = int(contract.terms.get("n_obs", max(finite_bounds or [1.0])))
        return {"x_min": 0.0, "x_max": float(max(1, count))}
    raise DomainCompileError(f"不支持的横轴变量：{axis}")


def _nice_ceiling(value: float) -> float:
    """将展示边界向上收敛为可读刻度，避免173.6%这类内部比例出现在图上。"""
    if not isfinite(value) or value <= 0.0:
        return 1.0
    step = 10.0 ** (floor(log10(value)) - 1)
    return float(np.ceil(value / step) * step)


def path_templates(contract: ResolvedContract) -> tuple[CandidateTemplate, ...]:
    """生成有限、确定性的路径形状库，覆盖无事件、早晚触发及先后触发。"""
    reference = _reference_level(contract)
    levels = sorted({
        float(value)
        for key, value in contract.terms.items()
        if isinstance(value, (int, float)) and key.startswith(("S0", "K", "H", "B")) and value > 0.0
    })
    lower_levels = sorted({max(reference * 0.18, min(reference * 0.98, level * 0.92)) for level in levels if level < reference})
    upper_levels = sorted({max(reference * 1.02, level * 1.08) for level in levels if level >= reference})
    # 相邻障碍之间还需要一个稳定的中间水平。否则只有“低于敲入线”或“高于
    # 敲出线”的模板，无法构造未敲入且未敲出的合法候选历史。
    middle_levels = sorted({(lower + upper) / 2.0 for lower, upper in zip(levels, levels[1:]) if lower < upper})
    if not lower_levels:
        lower_levels = [reference * 0.55]
    if not upper_levels:
        upper_levels = [reference * 1.45]
    # 保持路径库有界：足以区分障碍先后关系，但不会让每个图的候选搜索失控。
    lower_levels = _representative_levels(lower_levels, limit=3)
    upper_levels = _representative_levels(upper_levels, limit=3)
    middle_levels = _representative_levels(middle_levels, limit=3)
    templates: list[CandidateTemplate] = [CandidateTemplate("linear", ())]
    for level in middle_levels:
        templates.extend((
            CandidateTemplate(f"mid-early-{level:g}", ((0.10, level),)),
            CandidateTemplate(f"mid-late-{level:g}", ((0.68, level),)),
        ))
    for level in lower_levels:
        templates.extend((
            CandidateTemplate(f"low-early-{level:g}", ((0.10, level),)),
            CandidateTemplate(f"low-late-{level:g}", ((0.68, level),)),
        ))
    for level in upper_levels:
        templates.extend((
            CandidateTemplate(f"high-early-{level:g}", ((0.10, level),)),
            CandidateTemplate(f"high-late-{level:g}", ((0.68, level),)),
        ))
    for low in lower_levels:
        for high in upper_levels:
            templates.extend((
                CandidateTemplate(f"low-high-early-{low:g}-{high:g}", ((0.10, low), (0.28, high))),
                CandidateTemplate(f"high-low-early-{high:g}-{low:g}", ((0.10, high), (0.28, low))),
                CandidateTemplate(f"low-high-late-{low:g}-{high:g}", ((0.42, low), (0.76, high))),
                CandidateTemplate(f"high-low-late-{high:g}-{low:g}", ((0.42, high), (0.76, low))),
            ))
    # 月度观察只取实际月末；“到期日恰为月末”仅作为独立候选状态加入，不能据此把
    # 所有月度路径的到期日伪装成观察日。
    monthly_schedule = any(
        isinstance(value, str) and value.startswith("monthly_")
        for key, value in contract.terms.items()
        if key.startswith("O")
    )
    if monthly_schedule:
        for level in middle_levels:
            templates.append(CandidateTemplate(f"month-end-mid-{level:g}", ((0.10, level),), terminal_period_end=True))
    return tuple(templates)


def candidate_path(
    contract: ResolvedContract,
    axis: str,
    value: float,
    template: CandidateTemplate,
) -> PricePath:
    """将横轴取值和固定形状转为共享解释器可结算的价格路径。"""
    if axis == "sigma_realized":
        return _variance_path(contract, value)
    if axis == "n_in":
        return _range_count_path(contract, int(round(value)))
    frozen_grid = _candidate_frozen_grid(contract, terminal_period_end=template.terminal_period_end)
    points = len(frozen_grid[0]) if frozen_grid is not None else _observation_count(contract)
    normalized_reference = _normalized_reference(contract)
    target = _terminal_normalized(contract, axis, value, normalized_reference)
    times = (
        frozen_grid[1]
        if frozen_grid is not None
        else np.linspace(0.0, float(contract.terms["T"]), points)
    )
    fractions = times / float(contract.terms["T"])
    # 收益图横轴只改变终值。候选历史的非终值观察点必须固定，否则改变S_T时会
    # 连带改变tau、累计量和派息次数，把不同路径状态伪装成一条单变量损益曲线。
    # 各模板在其最后一个路标后保持该水平，最后一个观察点才设为横轴终值。
    anchors_x = np.asarray([0.0, *(fraction for fraction, _ in template.waypoints)], dtype=float)
    anchors_y = np.asarray([normalized_reference[0], *(level for _, level in template.waypoints)], dtype=float)
    if len(anchors_x) == 1:
        base = np.full(points, anchors_y[0], dtype=float)
    else:
        base = np.interp(fractions, anchors_x, anchors_y, right=anchors_y[-1])
    base[-1] = target
    values = np.column_stack([base for _ in contract.underlyings])
    if axis == "W_T":
        # Worst-Of横轴把所有标的终值设为同一表现；路径中保持同形状，从而明确W_T。
        values = np.column_stack([base for _ in contract.underlyings])
    raw_reference = _raw_reference(contract)
    normalized = _normalized_reference(contract)
    raw_values = values / normalized[None, :] * raw_reference[None, :]
    dates = (
        frozen_grid[0]
        if frozen_grid is not None
        else _display_dates(contract, points, terminal_period_end=template.terminal_period_end)
    )
    return PricePath.from_values(
        raw_values,
        times=times,
        asset_ids=contract.underlyings,
        dates=dates,
    )


def segment_samples(interval: Interval, scale: Mapping[str, float], *, discrete: bool) -> list[tuple[float, str | None]]:
    """返回精确端点与内部采样点；端点状态供SVG空实点使用。"""
    lower = float(scale["x_min"]) if not isfinite(interval.lower) else interval.lower
    upper = float(scale["x_max"]) if not isfinite(interval.upper) else interval.upper
    lower_marker = None if not isfinite(interval.lower) else "closed" if interval.lower_closed else "open"
    upper_marker = None if not isfinite(interval.upper) else "closed" if interval.upper_closed else "open"
    if lower > upper:
        return []
    if lower == upper:
        return [(lower, "closed")] if interval.is_point else []
    if discrete:
        start = int(np.ceil(lower if interval.lower_closed else lower + 1.0))
        end = int(np.floor(upper if interval.upper_closed else upper - 1.0))
        values = [float(item) for item in range(start, end + 1)]
        return [(value, "closed") for value in values]
    span_px = (upper - lower) / (float(scale["x_max"]) - float(scale["x_min"])) * 642.0
    count = max(48, int(np.ceil(span_px / 14.0)) + 1)
    inner = np.linspace(lower, upper, count)
    samples: list[tuple[float, str | None]] = []
    for index, value in enumerate(inner):
        marker = lower_marker if index == 0 else upper_marker if index == len(inner) - 1 else None
        samples.append((float(value), marker))
    return samples


def inward_value(value: float, interval: Interval, scale: Mapping[str, float]) -> float:
    """开端点仅用于求单侧极限，图上仍落在精确边界。"""
    epsilon = max(1e-10, (float(scale["x_max"]) - float(scale["x_min"])) * 1e-9)
    if value == interval.lower and not interval.lower_closed:
        return value + epsilon
    if value == interval.upper and not interval.upper_closed:
        return value - epsilon
    return value


def _compile_boolean(node: ast.AST, axis: str, variables: Mapping[str, Any]) -> tuple[list[Interval], list[DomainThreshold]]:
    if isinstance(node, ast.BoolOp):
        pieces = [_compile_boolean(item, axis, variables) for item in node.values]
        if isinstance(node.op, ast.Or):
            return [interval for intervals, _ in pieces for interval in intervals], [threshold for _, thresholds in pieces for threshold in thresholds]
        if isinstance(node.op, ast.And):
            intervals = [Interval()]
            thresholds: list[DomainThreshold] = []
            for next_intervals, next_thresholds in pieces:
                intervals = [merged for current in intervals for following in next_intervals if (merged := current.intersection(following)) is not None]
                thresholds.extend(next_thresholds)
            return intervals, thresholds
    if isinstance(node, ast.Compare):
        intervals = [Interval()]
        thresholds: list[DomainThreshold] = []
        left = node.left
        for operator, right in zip(node.ops, node.comparators):
            part, found = _comparison_interval(left, operator, right, axis, variables)
            intervals = [merged for current in intervals if (merged := current.intersection(part)) is not None]
            thresholds.extend(found)
            left = right
        return intervals, thresholds
    raise DomainCompileError("分段定义域只支持比较及and/or组合")


def _comparison_interval(left: ast.AST, operator: ast.cmpop, right: ast.AST, axis: str, variables: Mapping[str, Any]) -> tuple[Interval, list[DomainThreshold]]:
    left_is_axis = isinstance(left, ast.Name) and left.id == axis
    right_is_axis = isinstance(right, ast.Name) and right.id == axis
    if left_is_axis == right_is_axis:
        raise DomainCompileError("每个比较只能以横轴变量与数值边界组成")
    bound_node = right if left_is_axis else left
    try:
        bound = float(evaluate_formula(ast.unparse(bound_node), variables))
    except Exception as error:
        raise DomainCompileError(f"无法计算分段边界：{ast.unparse(bound_node)}") from error
    if not isfinite(bound):
        raise DomainCompileError("分段边界必须为有限数值")
    op = _flip_operator(operator) if right_is_axis else operator
    threshold = _domain_threshold(bound_node, bound)
    if isinstance(op, ast.Gt):
        return Interval(lower=bound, lower_closed=False), [threshold]
    if isinstance(op, ast.GtE):
        return Interval(lower=bound, lower_closed=True), [threshold]
    if isinstance(op, ast.Lt):
        return Interval(upper=bound, upper_closed=False), [threshold]
    if isinstance(op, ast.LtE):
        return Interval(upper=bound, upper_closed=True), [threshold]
    if isinstance(op, ast.Eq):
        return Interval(bound, bound, True, True), [threshold]
    raise DomainCompileError("分段定义域不支持!=")


def _flip_operator(operator: ast.cmpop) -> ast.cmpop:
    if isinstance(operator, ast.Gt): return ast.Lt()
    if isinstance(operator, ast.GtE): return ast.LtE()
    if isinstance(operator, ast.Lt): return ast.Gt()
    if isinstance(operator, ast.LtE): return ast.GtE()
    return operator


def _domain_threshold(node: ast.AST, value: float) -> DomainThreshold:
    source = ast.unparse(node).replace(" ", "")
    return DomainThreshold(source, value)


def _merge_intervals(intervals: Sequence[Interval]) -> list[Interval]:
    ordered = sorted(intervals, key=lambda item: (item.lower, not item.lower_closed, item.upper))
    result: list[Interval] = []
    for current in ordered:
        if not result:
            result.append(current)
            continue
        prior = result[-1]
        overlaps = current.lower < prior.upper or (current.lower == prior.upper and (current.lower_closed or prior.upper_closed))
        if not overlaps:
            result.append(current)
            continue
        if current.upper > prior.upper or (current.upper == prior.upper and current.upper_closed):
            result[-1] = Interval(prior.lower, current.upper, prior.lower_closed, current.upper_closed)
    return result


def _unique_thresholds(thresholds: Sequence[DomainThreshold]) -> list[DomainThreshold]:
    unique: list[DomainThreshold] = []
    seen: set[tuple[str, float]] = set()
    for threshold in thresholds:
        key = (threshold.token, threshold.value)
        if key not in seen:
            seen.add(key)
            unique.append(threshold)
    return unique


def _representative_levels(values: Sequence[float], limit: int) -> list[float]:
    if len(values) <= limit:
        return list(values)
    positions = np.linspace(0, len(values) - 1, limit).round().astype(int)
    return [float(values[index]) for index in sorted(set(positions))]


def _reference_level(contract: ResolvedContract) -> float:
    if "S0" in contract.terms:
        return float(contract.terms["S0"])
    if "S0Vec" in contract.terms:
        return float(min(contract.terms["S0Vec"]))
    return 100.0


def _raw_reference(contract: ResolvedContract) -> np.ndarray:
    values = contract.identity.get("reference_prices")
    if values is None and "S0" not in contract.terms and "S0Vec" not in contract.terms:
        # 方差互换的经济条款只依赖收益率/已实现波动率，不定义合同初始价格。
        # 候选路径仍需要一个正的原始价格起点来构造数值序列；任意统一正基准
        # 在共享解释器中都会被同一路径首点归一化，因而不改变该类合同的现金流。
        return np.full(len(contract.underlyings), 100.0, dtype=float)
    if not isinstance(values, Mapping) or set(values) != set(contract.underlyings):
        raise DomainCompileError("ResolvedContract必须提供逐标的reference_prices")
    return np.asarray([values[asset] for asset in contract.underlyings], dtype=float)


def _normalized_reference(contract: ResolvedContract) -> np.ndarray:
    if "S0Vec" in contract.terms:
        return np.asarray(contract.terms["S0Vec"], dtype=float)
    return np.full(len(contract.underlyings), float(contract.terms.get("S0", 100.0)), dtype=float)


def _terminal_normalized(contract: ResolvedContract, axis: str, value: float, reference: np.ndarray) -> float:
    if axis == "S_T":
        return value
    if axis == "r_T":
        return float(reference[0] * (1.0 + value))
    if axis == "W_T":
        return value
    raise DomainCompileError(f"横轴{axis}不能由普通终值构造")


def _observation_count(contract: ResolvedContract) -> int:
    monitor = contract.terms.get("monitor", {})
    if any("accumulated_quantity(" in str(expression) for expression in monitor.values()):
        return int(contract.terms["n_obs"])
    return max(25, int(contract.terms.get("n_obs", 0)), int(np.ceil(float(contract.terms.get("T", 1.0)) * 252)) + 1)


def _candidate_frozen_grid(
    contract: ResolvedContract,
    *,
    terminal_period_end: bool = False,
) -> tuple[pd.DatetimeIndex, np.ndarray] | None:
    """返回以真实日期锚定的候选路径日期与合同时间网格。

    冻结观察日只描述可观察的期点，不能本身冒充完整价格路径。候选路径须额外
    保留合同起点和结算终点，再由真实日历间隔映射到合同期限。这样月度第4期不会
    被错误压缩成11个观察点中的第4个，日度累购也不会把超过``n_obs``的本地预览
    日历误认为合同观察期。
    """
    snapshots = tuple(contract.resolved_schedules.values())
    if not snapshots:
        return None
    try:
        schedule_dates = tuple(
            pd.Timestamp(item).normalize()
            for snapshot in snapshots
            for item in snapshot["dates"]
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DomainCompileError("ResolvedContract冻结观察日无效") from error
    if not schedule_dates:
        raise DomainCompileError("ResolvedContract冻结观察日为空")
    observed_dates = pd.DatetimeIndex(sorted(set(schedule_dates)))
    first_date = observed_dates[0]
    contract_start = contract.identity.get("contract_start_date")
    contract_end = contract.identity.get("contract_end_date")
    try:
        daily_snapshots = tuple(snapshot for snapshot in snapshots if snapshot.get("selector") == "daily")
        monthly_snapshots = tuple(
            snapshot
            for snapshot in snapshots
            if isinstance(snapshot.get("selector"), str)
            and (
                str(snapshot["selector"]) == "monthly_last"
                or str(snapshot["selector"]).startswith("monthly_")
            )
        )
        lower = pd.Timestamp(contract_start).normalize() if contract_start else _derived_contract_start(first_date, monthly_snapshots)
        if contract_end:
            upper = pd.Timestamp(contract_end).normalize()
        elif _has_host_frozen_calendar(contract):
            # Host正式合同必须冻结实际结算日。月度观察日并不必然等于到期日；
            # 不能为了画图在最后观察日后自行推迟结算日并伪造合同路径。
            raise DomainCompileError("正式ResolvedContract缺少identity.contract_end_date，不能推定收益图结算日")
        else:
            upper = _derived_contract_end(
                contract, lower, daily_snapshots, monthly_snapshots,
                terminal_period_end=terminal_period_end,
            )
        if daily_snapshots and "n_obs" in contract.terms:
            expected_observations = int(contract.terms["n_obs"])
            daily_dates = sorted({
                pd.Timestamp(item).normalize()
                for snapshot in daily_snapshots
                for item in snapshot["dates"]
                if lower <= pd.Timestamp(item).normalize() <= upper
            })
            if len(daily_dates) < expected_observations:
                raise DomainCompileError("ResolvedContract日度冻结观察日数量与n_obs不一致，无法生成候选收益路径")
            # n_obs是日度观察合同的受控期数，不是自然年内所有交易日的
            # 上限校验值。Host可能冻结到理论ACT/365到期日的完整日历，
            # 候选路径仍必须与Pricer和Backtester一样在第n_obs个真实观察日收口。
            upper = daily_dates[expected_observations - 1]
    except DomainCompileError:
        raise
    except (TypeError, ValueError, KeyError) as error:
        raise DomainCompileError("ResolvedContract合同期限无法确定候选观察日") from error
    if upper <= lower:
        raise DomainCompileError("ResolvedContract合同起止日必须严格递增")
    if _requires_maturity_observation(contract):
        maturity_observations = observed_dates[(observed_dates > lower) & (observed_dates <= upper)]
        if not len(maturity_observations):
            raise DomainCompileError("ResolvedContract到期日前缺少冻结观察日")
        # ``require_maturity_observation``要求终值本身属于冻结观察集合。Host日历
        # 可能把理论ACT/365到期日落在周末或节假日之后；候选路径必须收口在
        # 到期日前最后一个真实观察日，不能把非观察日终值附加到其后。
        upper = maturity_observations[-1]
    dates = pd.DatetimeIndex(sorted({lower, *(item for item in observed_dates if lower <= item <= upper), upper}))
    if len(dates) < 2:
        raise DomainCompileError("ResolvedContract合同期限内冻结观察日不足两个")
    elapsed_days = float((dates[-1] - dates[0]).days)
    if elapsed_days <= 0.0:
        raise DomainCompileError("ResolvedContract候选路径日期间隔必须为正")
    identity_end = pd.Timestamp(contract_end).normalize() if contract_end else None
    if contract_start and identity_end is not None and dates[-1] == identity_end:
        # 完整覆盖Host冻结起止日的正式路径必须使用真实ACT/365时钟。
        # 将367天跨闰日合同压回登记T=1会把真实到期观察误判为提前观察。
        times = (dates - dates[0]).days.to_numpy(dtype=float) / 365.0
    else:
        # 日度n_obs等候选会按登记观察期提前收口；该路径的经济期限仍由条款T定义。
        times = (dates - dates[0]).days.to_numpy(dtype=float) / elapsed_days * float(contract.terms["T"])
    return dates, times


def _requires_maturity_observation(contract: ResolvedContract) -> bool:
    monitor = contract.terms.get("monitor", {})
    return isinstance(monitor, Mapping) and any(
        "require_maturity_observation" in str(expression)
        for expression in monitor.values()
    )


def _has_host_frozen_calendar(contract: ResolvedContract) -> bool:
    """Host日历存在时，候选路径只能使用合同中冻结的实际期限。"""
    identity = contract.identity
    calendar_id = str(identity.get("calendar_id") or "")
    revision = str(identity.get("calendar_revision") or "")
    return bool(
        calendar_id
        and revision
        and not (
            calendar_id == "payoffer-structural-candidate"
            and revision == "structural-candidate"
        )
    )


def _derived_contract_start(first_observation: pd.Timestamp, monthly_snapshots: Sequence[Mapping[str, Any]]) -> pd.Timestamp:
    """结构候选缺少Host日期时，从冻结日程得到不与首期观察重合的起点。"""
    if monthly_snapshots:
        return first_observation.to_period("M").start_time.normalize()
    return first_observation - pd.Timedelta(days=1)


def _derived_contract_end(
    contract: ResolvedContract,
    lower: pd.Timestamp,
    daily_snapshots: Sequence[Mapping[str, Any]],
    monthly_snapshots: Sequence[Mapping[str, Any]],
    *,
    terminal_period_end: bool,
) -> pd.Timestamp:
    """开发预览用合同登记的观察数量确定必要结算终点。"""
    if daily_snapshots and "n_obs" in contract.terms:
        observation_count = int(contract.terms["n_obs"])
        try:
            return pd.Timestamp(daily_snapshots[0]["dates"][observation_count - 1]).normalize()
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise ValueError("冻结日度观察日不足n_obs") from error
    tenure = float(contract.terms["T"])
    if monthly_snapshots:
        monthly_count = int(round(tenure * 12.0))
        if monthly_count <= 0 or not np.isclose(tenure * 12.0, monthly_count, rtol=0.0, atol=1e-12):
            raise ValueError("月度观察合同T必须对应完整月数")
        try:
            final_observation = max(
                pd.Timestamp(snapshot["dates"][monthly_count - 1]).normalize()
                for snapshot in monthly_snapshots
            )
            # 独立开发预览没有Host冻结的实际到期日。普通候选必须把结算终点
            # 放在最后一个月度观察日之后，否则所有终值都会被误当作当月敲出；
            # 只有明确要求“到期日恰为月末”的候选，才让该观察日同时作为终点。
            # 该分支只服务独立开发预览，并在本地日历中保留与最后观察日
            # 不同的结算点；正式Host合同始终使用冻结的contract_end_date。
            return final_observation if terminal_period_end else final_observation + pd.offsets.BDay(11)
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise ValueError("冻结月度观察日不足合同期限") from error
    tenor_days = int(round(tenure * 365.25))
    if tenor_days <= 0:
        raise ValueError("T必须为正")
    return lower + pd.Timedelta(days=tenor_days)


def _display_dates(contract: ResolvedContract, points: int, *, terminal_period_end: bool = False) -> pd.DatetimeIndex | None:
    """仅为收益图候选路径构造可复核的交易日序列。

    实盘、定价和回测必须传入真实日期；这里不把无日期的终点伪造为周期末。
    只有条款明确要求月度观察包含到期日，或候选状态明确为实际月末到期时，才使用
    一个以实际工作月末收尾的展示日历；其他候选不把终点伪装为月末。
    """
    monitor = contract.terms.get("monitor", {})
    terminal_observation = any(
        "require_maturity_observation" in str(expression)
        for expression in dict(monitor).values()
    )
    if not terminal_observation and not terminal_period_end:
        return None
    end = pd.Timestamp("2028-12-01") + pd.offsets.BMonthEnd(0)
    return pd.bdate_range(end=end, periods=points)


def _variance_path(contract: ResolvedContract, sigma: float) -> PricePath:
    frozen_grid = _candidate_frozen_grid(contract)
    if frozen_grid is None:
        points = max(25, int(contract.terms.get("n_obs", 0)) + 1)
        dates = None
        times = np.linspace(0.0, float(contract.terms["T"]), points)
    else:
        dates, times = frozen_grid
        points = len(dates)
    annual_days = float(contract.terms.get("annualization_days", 244))
    target_log = max(0.0, sigma) / 100.0 / max(annual_days, 1.0) ** 0.5
    returns = np.resize(np.asarray([target_log, -target_log], dtype=float), points - 1)
    raw_reference = _raw_reference(contract)[0]
    values = raw_reference * np.exp(np.concatenate(([0.0], np.cumsum(returns))))
    return PricePath.from_values(values, times=times, asset_ids=contract.underlyings, dates=dates)


def _range_count_path(contract: ResolvedContract, target_count: int) -> PricePath:
    observations = int(contract.terms["n_obs"])
    count = min(max(target_count, 0), observations)
    lower, upper = float(contract.terms["Hlow"]), float(contract.terms["Hup"])
    inside = (lower + upper) / 2.0
    outside = upper * 1.08
    normalized_reference = _normalized_reference(contract)[0]
    raw_reference = _raw_reference(contract)[0]
    frozen_grid = _candidate_frozen_grid(contract)
    if frozen_grid is None:
        dates = None
        times = np.linspace(0.0, float(contract.terms["T"]), observations)
        observation_indices = np.arange(observations)
    else:
        dates, times = frozen_grid
        frozen_observations = {
            pd.Timestamp(item).normalize()
            for snapshot in contract.resolved_schedules.values()
            for item in snapshot["dates"]
        }
        observation_indices = np.asarray(
            [index for index, date in enumerate(dates) if date in frozen_observations],
            dtype=int,
        )
        if len(observation_indices) != observations:
            raise DomainCompileError(
                "区间计息冻结观察日数量与n_obs不一致，不能构造收益图候选路径"
            )
    values = np.full(len(times), outside / normalized_reference * raw_reference, dtype=float)
    if count:
        values[observation_indices[:count]] = inside / normalized_reference * raw_reference
    return PricePath.from_values(values, times=times, asset_ids=contract.underlyings, dates=dates)


def term_variables(contract: ResolvedContract, catalog: Mapping[str, Any]) -> dict[str, Any]:
    """仅绑定已解析条款符号，供分段域边界计算使用。"""
    return bind_term_symbols(contract.terms, catalog)
