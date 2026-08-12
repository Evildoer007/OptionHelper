"""Payoffer运行内核。

Payoffer不维护第二套产品公式。它只将OptionReg解析为ResolvedContract，并以共享
现金流解释器计算每条经济路径的参数化收益图。正式默认SVG是只读资料资产，不参与
本次运行结果的计算，也不会被本模块写入。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from runtime.bootstrap import bootstrap_runtime
from runtime.adapters.local_store import LocalResultStore, StoreError
from runtime.contracts.contract_api import (
    ContractResolutionError,
    PricePath,
    ResolvedContract,
    evaluate_payoff,
    get_product,
    resolve_contract,
)
from runtime.contracts.contract_types import deep_thaw, semantic_hash
from runtime.knowledger import load_registry as load_optionreg
from runtime.ports.result_store import ResultStorePort
from runtime.protocol.module_host import ModuleHostContext

from ..asset_resolver import (
    DefaultAssetError,
    DefaultVisualAsset,
    build_visual_template,
    figure_asset_paths,
    load_default_figure_payload,
    payoff_template_terms_match,
    resolve_default_visual_asset,
)
from ..models import PayoffInput, PayoffResult
from .path_sampler import (
    CandidateTemplate,
    CompiledDomain,
    DomainCompileError,
    axis_scale,
    candidate_path,
    compile_domain,
    inward_value,
    path_templates,
    segment_samples,
    term_variables,
)
from .svg_renderer import render_svg as render_svg_text


MODULE_ROOT = Path(__file__).resolve().parents[4]
RUNTIME_PATHS = bootstrap_runtime(MODULE_ROOT)
RESULT_ROOT = RUNTIME_PATHS.result_root


class PayoffEngineError(ValueError):
    """Payoffer输入或收益图生成请求无效。"""


CandidateEvaluation = tuple[int, int, float, dict[str, Any]]
EvaluationCache = dict[tuple[str, float, CandidateTemplate], CandidateEvaluation]


@dataclass(frozen=True)
class RenderedPath:
    path_id: str
    title: str
    condition_tex: str
    payoff_tex: str
    piece_summaries: list[dict[str, str]]
    chart_kind: str
    axis: dict[str, Any]
    scale: dict[str, float]
    segments: list[list[dict[str, Any]]]
    jumps: list[dict[str, float]]
    turning_points: list[dict[str, float]]
    thresholds: list[dict[str, Any]]
    payoff_levels: list[dict[str, Any]]
    payoff_basis: str = "net_after_premium"
    status_note: str | None = None
    candidate_state: dict[str, Any] | None = None
    time_view: dict[str, Any] | None = None


def load_registry() -> dict[str, Any]:
    """读取唯一OptionReg，不提供旧Registry字段兼容。"""
    return load_optionreg()


def product_by_id(product_id: str, registry: Mapping[str, Any] | None = None) -> dict[str, Any]:
    try:
        return get_product(str(product_id), registry)
    except ContractResolutionError as error:
        raise PayoffEngineError(str(error)) from error


def product_id_by_name(name_zh: str, registry: Mapping[str, Any] | None = None) -> str:
    """以中文结构名定位OptionReg内部编号，不将编号暴露给Payoffer调用方。"""
    name = str(name_zh).strip()
    if not name:
        raise PayoffEngineError("必须提供中文结构名")
    source = registry or load_registry()
    matches = [
        product_id
        for product_id, product in source["products"].items()
        if product["identity"]["name_zh"] == name
    ]
    if not matches:
        raise PayoffEngineError(f"OptionReg不存在中文结构名{name}")
    if len(matches) != 1:
        raise PayoffEngineError(f"中文结构名{name}不唯一，不能作为Payoffer资产名")
    return str(matches[0])


def product_by_name(name_zh: str, registry: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = registry or load_registry()
    return product_by_id(product_id_by_name(name_zh, source), source)


def default_figure_payload(name_zh: str, registry: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """由资料库维护流程重建默认JSON时使用；正常运行不调用本函数。"""
    product = product_by_name(name_zh, registry)
    return {
        "name_zh": product["identity"]["name_zh"],
        "terms": deepcopy(product["terms"]),
        "paths": deepcopy(product["paths"]),
        "visual_template": build_visual_template(product["paths"]),
    }


def term_overrides_from_default_json(name_zh: str, edited_payload: Mapping[str, Any]) -> dict[str, Any]:
    """把大模型或人工修改后的默认JSON转换为本次合法条款覆盖，不允许改写路径经济逻辑。"""
    if not isinstance(edited_payload, Mapping):
        raise PayoffEngineError("本次Payoffer JSON必须是对象")
    default = load_default_figure_payload(name_zh)
    if edited_payload.get("name_zh") != default["name_zh"]:
        raise PayoffEngineError("本次Payoffer JSON的中文结构名与默认JSON不一致")
    edited_terms = edited_payload.get("terms")
    if not isinstance(edited_terms, Mapping):
        raise PayoffEngineError("本次Payoffer JSON必须含terms对象")
    default_terms = default["terms"]
    unknown = set(edited_terms) - set(default_terms)
    if unknown:
        raise PayoffEngineError(f"本次Payoffer JSON含未登记条款：{'、'.join(sorted(unknown))}")
    static = {"monitor", "pricing_methods", "constraints", "derived_terms", "margin_call", "payoff_figure_basis"}
    for key in static & set(edited_terms):
        if edited_terms[key] != default_terms.get(key):
            raise PayoffEngineError(f"本次Payoffer JSON不能修改固定规则字段{key}")
    if "paths" in edited_payload and edited_payload["paths"] != default["paths"]:
        raise PayoffEngineError("本次Payoffer JSON不能修改固定收益路径；路径维护必须走资料库流程")
    if "visual_template" in edited_payload and edited_payload["visual_template"] != default["visual_template"]:
        raise PayoffEngineError("本次Payoffer JSON不能修改固定视觉模板；模板维护必须走资料库流程")
    return {
        key: deepcopy(value)
        for key, value in edited_terms.items()
        if key not in static and value != default_terms.get(key)
    }


def _registry_with_default_figure(name_zh: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """核验默认资产与OptionReg同源，但绝不以JSON替换合同规则。"""
    registry = load_registry()
    product_id = product_id_by_name(name_zh, registry)
    default = load_default_figure_payload(name_zh)
    expected = default_figure_payload(name_zh, registry)
    if (
        not payoff_template_terms_match(default["terms"], expected["terms"])
        or semantic_hash(default["paths"]) != semantic_hash(expected["paths"])
    ):
        raise PayoffEngineError(f"{name_zh}的固定默认JSON与OptionReg不一致；请先完成资料库维护后再发布默认资产")
    # 这份product仍是OptionReg的原始经济规则。默认JSON仅被读取和核验，普通
    # 运行不能让其成为第二套terms/paths来源。
    return product_id, deepcopy(registry["products"][product_id]), registry


def payoff_term_fields(product: Mapping[str, Any], registry: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """输出人工页面所需的条款字段目录；默认值来自固定默认JSON。"""
    source = registry or load_registry()
    catalog = source["term_catalog"]
    terms = product["terms"]
    fields: list[dict[str, Any]] = []
    for key, value in terms.items():
        if key in {"monitor", "pricing_methods", "constraints", "derived_terms"}:
            continue
        definition = catalog[key]
        is_normalized_base = key in {"S0", "S0Vec"}
        fields.append(
            {
                "key": key,
                "chinese_name": definition["name_zh"],
                "english_name": definition.get("name_en", definition["name_zh"]),
                "math_symbol": definition["symbol"],
                "source_policy": "内部标准化基准100，真实合同参考价由identity.contract_reference_spots提供" if is_normalized_base else "OptionReg默认条款，可由本次PayoffInput覆盖",
                "allow_override": not is_normalized_base,
                "default_value": value,
            }
        )
    return fields


def _default_identity(product: Mapping[str, Any]) -> dict[str, Any]:
    terms = product["terms"]
    if "S0Vec" in terms:
        assets = [f"UNDERLYING_{index + 1}" for index in range(len(terms["S0Vec"]))]
        return {"underlyings": assets, "contract_reference_spots": dict(zip(assets, terms["S0Vec"]))}
    return {"underlyings": ["UNDERLYING"], "contract_reference_spots": {"UNDERLYING": float(terms.get("S0", 100.0))}}


def build_payoff_input(
    name_zh: str,
    term_overrides: Mapping[str, Any] | None = None,
    identity: Mapping[str, Any] | None = None,
) -> ResolvedContract:
    """兼容页面：默认条款加本次覆盖解析为唯一ResolvedContract。"""
    try:
        product_id, product, registry = _registry_with_default_figure(name_zh)
        actual_identity = {**_default_identity(product), **dict(identity or {})}
        return resolve_contract(str(product_id), identity=actual_identity, term_overrides=term_overrides, registry=registry)
    except (ContractResolutionError, TypeError, ValueError) as error:
        raise PayoffEngineError(str(error)) from error


_AXIS_META: dict[str, dict[str, Any]] = {
    "S_T": {"label": "到期标的价格", "display_unit": "%", "number_format": "level"},
    "r_T": {"label": "到期收益率", "display_unit": "%", "number_format": "rate"},
    "W_T": {"label": "Worst-Of到期表现", "display_unit": "%", "number_format": "level"},
    "sigma_realized": {"label": "已实现波动率", "display_unit": "%", "number_format": "level"},
    "n_in": {"label": "区间内观察次数", "display_unit": "次", "number_format": "count"},
}


def _compile_paths(contract: ResolvedContract) -> list[list[CompiledDomain]]:
    catalog = load_registry()["term_catalog"]
    variables = term_variables(contract, catalog)
    compiled: list[list[CompiledDomain]] = []
    for path in contract.paths:
        axis: str | None = None
        path_domains: list[CompiledDomain] = []
        for case in path["cases"]:
            domain = compile_domain(case["domain"], variables, expected_axis=axis)
            axis = domain.axis
            path_domains.append(domain)
        compiled.append(path_domains)
    return compiled


def _check_axis_unit(contract: ResolvedContract, compiled: list[list[CompiledDomain]]) -> None:
    """阻止已知的OptionReg量纲矛盾被渲染器静默掩盖。"""
    axes = {domain.axis for path in compiled for domain in path}
    if "W_T" not in axes:
        return
    finite_bounds = [bound for path in compiled for domain in path for interval in domain.intervals for bound in (interval.lower, interval.upper) if np.isfinite(bound)]
    barrier_values = [float(value) for key, value in contract.terms.items() if key.startswith("H_") and isinstance(value, (int, float))]
    if barrier_values and max(barrier_values) > 10.0 and finite_bounds and max(finite_bounds) <= 1.0:
        raise PayoffEngineError(
            f"{contract.product_id}的W_T分段边界使用0至1口径，但共享解释器的W_T为百分比口径；"
            "请先依据OptionLib修正OptionReg后再发布默认图。"
        )


def _path_axis(domains: list[CompiledDomain]) -> str:
    axes = {domain.axis for domain in domains}
    if len(axes) != 1:
        raise PayoffEngineError("同一收益路径只能有一个横轴变量")
    return next(iter(axes))


def _evaluate_candidate(
    contract: ResolvedContract,
    axis: str,
    value: float,
    path_index: int,
    case_index: int,
    templates: tuple[CandidateTemplate, ...],
    preferred: CandidateTemplate | None,
    evaluation_cache: EvaluationCache,
    *,
    fixed: bool = False,
) -> tuple[float, CandidateTemplate, dict[str, Any]] | None:
    if fixed:
        ordered = () if preferred is None else (preferred,)
    else:
        ordered = (preferred, *(template for template in templates if template != preferred)) if preferred is not None else templates
    for template in ordered:
        key = (axis, float(value), template)
        cached = evaluation_cache.get(key)
        if cached is None:
            try:
                outcome = evaluate_payoff(contract, candidate_path(contract, axis, value, template))
            except Exception as error:
                raise PayoffEngineError(f"{contract.product_id}无法由共享解释器结算展示候选路径：{error}") from error
            cached = (
                int(outcome.selected_path),
                int(outcome.selected_case),
                float(outcome.pnl),
                deepcopy(outcome.monitor_values),
            )
            evaluation_cache[key] = cached
        selected_path, selected_case, pnl, monitor_values = cached
        if selected_path == path_index and selected_case == case_index:
            return pnl, template, deepcopy(monitor_values)
    return None


def _probe_value(domain: CompiledDomain, scale: Mapping[str, float]) -> float:
    interval = domain.intervals[0]
    lower = float(scale["x_min"]) if not np.isfinite(interval.lower) else interval.lower
    upper = float(scale["x_max"]) if not np.isfinite(interval.upper) else interval.upper
    if interval.is_point:
        return lower
    return inward_value((lower + upper) / 2.0, interval, scale)


def _safe_axis_trial(axis: str, value: float, scale: Mapping[str, float]) -> float:
    """价格路径要求严格正价格；在0或-100%端点以右侧极限结算。"""
    epsilon = max(1e-8, (float(scale["x_max"]) - float(scale["x_min"])) * 1e-9)
    if axis in {"S_T", "W_T"}:
        return max(epsilon, value)
    if axis == "r_T":
        return max(-1.0 + epsilon, value)
    return value


def _case_segments(
    contract: ResolvedContract,
    axis: str,
    scale: Mapping[str, float],
    path_index: int,
    case_index: int,
    domain: CompiledDomain,
    template: CandidateTemplate,
    evaluation_cache: EvaluationCache,
) -> list[list[dict[str, Any]]]:
    output: list[list[dict[str, Any]]] = []
    found = False
    for interval in domain.intervals:
        active: list[dict[str, Any]] = []
        discrete = axis == "n_in"
        for x_value, endpoint in segment_samples(interval, scale, discrete=discrete):
            trial = inward_value(x_value, interval, scale) if endpoint == "open" else x_value
            trial = _safe_axis_trial(axis, trial, scale)
            evaluation = _evaluate_candidate(
                contract, axis, trial, path_index, case_index, (template,), template,
                evaluation_cache, fixed=True,
            )
            if evaluation is None:
                if active:
                    output.append(active)
                    active = []
                continue
            y_value, _, _ = evaluation
            point: dict[str, Any] = {
                "x": x_value,
                "y": y_value,
                "curve": "step" if discrete else "linear",
                "candidate_template": template.name,
            }
            if endpoint is not None:
                point["endpoint"] = endpoint
            if interval.is_point:
                point["isolated"] = True
            active.append(point)
            found = True
        if active:
            output.append(active)
    if not found:
        raise PayoffEngineError(f"{contract.product_id}的路径{path_index + 1}分段{case_index + 1}无法构造可复核候选路径，拒绝发布收益图。")
    return output


def _case_seed(
    contract: ResolvedContract,
    axis: str,
    scale: Mapping[str, float],
    path_index: int,
    case_index: int,
    domain: CompiledDomain,
    templates: tuple[CandidateTemplate, ...],
    evaluation_cache: EvaluationCache,
) -> tuple[float, CandidateTemplate, dict[str, Any]] | None:
    """选择覆盖当前分段最多终值的固定候选历史。

    候选历史不能仅按定义域中点决定：同一经济路径可能既包括“历史已触发”的
    终值区间，也包括“仅到期日触发”的终值区间。优先选择覆盖最广的固定历史，
    避免把大段可行定义域留成空白。
    """
    samples: list[tuple[float, str | None, Any]] = []
    for interval in domain.intervals:
        samples.extend((value, endpoint, interval) for value, endpoint in segment_samples(interval, scale, discrete=axis == "n_in"))
    probe = _probe_value(domain, scale)
    if not samples:
        return None
    probe_indices = sorted({0, len(samples) - 1, *(int(round(index)) for index in np.linspace(0, len(samples) - 1, min(7, len(samples))) )})
    best: tuple[int, float, tuple[float, CandidateTemplate, dict[str, Any]]] | None = None
    for template in templates:
        hits: list[tuple[float, tuple[float, CandidateTemplate, dict[str, Any]]]] = []
        for index in probe_indices:
            x_value, endpoint, interval = samples[index]
            trial = inward_value(x_value, interval, scale) if endpoint == "open" else x_value
            result = _evaluate_candidate(
                contract, axis, _safe_axis_trial(axis, trial, scale), path_index, case_index,
                (template,), template, evaluation_cache, fixed=True,
            )
            if result is not None:
                hits.append((abs(x_value - probe), result))
        if not hits:
            continue
        hits.sort(key=lambda item: item[0])
        candidate = (len(hits), -hits[0][0], hits[0][1])
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    return None if best is None else best[2]


def _annotate_boundaries(segments: list[list[dict[str, Any]]]) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
    """按定义域保留跳变端点，并识别连续函数的关键转折。"""
    all_y_values = [float(point["y"]) for segment in segments for point in segment]
    # 开区间端点以很小的epsilon取单侧极限。连续函数会留下量级为
    # epsilon×斜率的浮点残差，不能把它误画为垂直跳变。以该图真实收益
    # 范围的十亿分之一作为收敛容差，金额级跳变仍会完整保留。
    payoff_span = max(all_y_values) - min(all_y_values) if all_y_values else 0.0
    continuity_tolerance = max(1e-8, payoff_span * 1e-7)
    by_x: dict[float, list[tuple[int, str, dict[str, Any]]]] = {}
    for segment_index, segment in enumerate(segments):
        if not segment:
            continue
        for position, point in (("start", segment[0]), ("end", segment[-1])):
            if "endpoint" in point:
                by_x.setdefault(round(float(point["x"]), 10), []).append((segment_index, position, point))
    jumps: list[dict[str, float]] = []
    turning_points: list[dict[str, float]] = []
    for value, entries in by_x.items():
        points = [point for _, _, point in entries]
        if len(points) == 1 and not points[0].get("isolated"):
            # 该端点可能是本路径唯一的有限边界，例如未敲出鲨鱼鳍在敲出障碍
            # 前的开端点。即使相邻经济情景画在另一张子图，也须保留空心点，
            # 不能因本子图内没有配对线段而抹去定义域边界。
            # 普通定义域的显示下限（如S_T=0）并非分段转折，不显示单独的实心点；
            # 只有开端点需要明确该路径在该边界不取值。
            if points[0].get("endpoint") != "open":
                points[0].pop("endpoint", None)
            continue
        raw_y_values = sorted(float(point["y"]) for point in points)
        y_values = [raw_y_values[0]]
        for item in raw_y_values[1:]:
            if abs(item - y_values[-1]) > continuity_tolerance:
                y_values.append(item)
        if len(y_values) <= 1:
            closed_points = [float(point["y"]) for point in points if point.get("endpoint") == "closed"]
            for point in points:
                if not point.get("isolated"):
                    point.pop("endpoint", None)
            # 连续转折若存在闭端点，闭端点即为该边界的精确函数值；开端点
            # 只用于单侧极限，不能用其epsilon残差移动紫色转折点。
            turn_y = closed_points[0] if closed_points else float(sum(float(point["y"]) for point in points) / len(points))
            slopes = [
                slope
                for segment_index, position, _ in entries
                if (slope := _edge_slope(segments[segment_index], position)) is not None
            ]
            if len(slopes) >= 2 and max(slopes) - min(slopes) > _slope_tolerance(slopes):
                turning_points.append({"x": float(value), "y": turn_y})
            continue
        jumps.append({"x": value, "y_start": y_values[0], "y_end": y_values[-1]})
    return jumps, turning_points


def _edge_slope(segment: list[dict[str, Any]], position: str) -> float | None:
    """返回线段在有限边界一侧的局部斜率；离散阶梯不作为连续转折。"""
    if len(segment) < 2 or segment[0].get("curve") == "step":
        return None
    first, second = (segment[0], segment[1]) if position == "start" else (segment[-2], segment[-1])
    delta_x = float(second["x"]) - float(first["x"])
    if abs(delta_x) < 1e-12:
        return None
    return (float(second["y"]) - float(first["y"])) / delta_x


def _slope_tolerance(slopes: list[float]) -> float:
    scale = max(1.0, *(abs(slope) for slope in slopes))
    return scale * 1e-6


def _payoff_levels(segments: list[list[dict[str, Any]]]) -> list[dict[str, float]]:
    """提取有实际长度的水平收益段，供纵轴水平辅助线与金额标签使用。"""
    candidates: dict[float, float] = {}
    all_values = [float(point["y"]) for segment in segments for point in segment]
    span = max(all_values) - min(all_values) if all_values else 0.0
    tolerance = max(1e-8, span * 1e-7)
    for segment in segments:
        if len(segment) < 2 or segment[0].get("curve") == "step":
            continue
        values = [float(point["y"]) for point in segment]
        if max(values) - min(values) > tolerance:
            continue
        level = float(sum(values) / len(values))
        if abs(level) <= tolerance:
            continue
        width = abs(float(segment[-1]["x"]) - float(segment[0]["x"]))
        key = round(level, 8)
        candidates[key] = max(candidates.get(key, 0.0), width)
    selected = sorted(candidates.items(), key=lambda item: (-item[1], item[0]))[:3]
    return [{"value": float(value)} for value, _ in sorted(selected)]


def _thresholds(domains: list[CompiledDomain]) -> list[dict[str, Any]]:
    seen: set[tuple[str, float]] = set()
    items: list[dict[str, Any]] = []
    for domain in domains:
        for threshold in domain.thresholds:
            key = (threshold.token, threshold.value)
            if key not in seen:
                seen.add(key)
                items.append({"token": threshold.token, "value": threshold.value})
    return items


def _payoff_figure_basis(contract: ResolvedContract) -> tuple[str, float]:
    """返回收益图展示口径及仅用于图形的期初期权费还原额。"""
    basis = str(contract.terms.get("payoff_figure_basis", "net_after_premium"))
    if basis == "net_after_premium":
        return basis, 0.0
    if basis != "gross_before_premium":
        raise PayoffEngineError(f"{contract.product_id}含未登记的收益图口径{basis}")
    try:
        offset = float(contract.terms["N"]) * float(contract.terms["p"])
    except (KeyError, TypeError, ValueError) as error:
        raise PayoffEngineError(f"{contract.product_id}的扣费前收益图必须定义N与p") from error
    if not np.isfinite(offset) or offset < 0.0:
        raise PayoffEngineError(f"{contract.product_id}的扣费前收益图期权费无效")
    return basis, offset


def _apply_figure_basis(segments: list[list[dict[str, Any]]], offset: float) -> None:
    if offset == 0.0:
        return
    for segment in segments:
        for point in segment:
            point["y"] = float(point["y"]) + offset


def _template_path_views(
    visual_template: Mapping[str, Any],
    compiled: list[list[CompiledDomain]],
) -> list[dict[str, Any]]:
    """读取默认JSON声明的面板顺序与分段绑定，不解释任何经济公式。"""
    views = visual_template.get("path_views")
    if not isinstance(views, list) or len(views) != len(compiled):
        raise PayoffEngineError("默认视觉模板的路径面板与ResolvedContract不一致")
    selected: list[dict[str, Any]] = []
    for view in views:
        if not isinstance(view, Mapping):
            raise PayoffEngineError("默认视觉模板含无效路径面板")
        try:
            path_index = int(view["path_index"]) - 1
            case_indexes = [int(index) - 1 for index in view["case_indexes"]]
            axis = str(view["axis"])
            chart_kind = str(view["chart_kind"])
            title = str(view["title"]).strip()
        except (KeyError, TypeError, ValueError) as error:
            raise PayoffEngineError("默认视觉模板缺少路径面板字段") from error
        if path_index < 0 or path_index >= len(compiled) or not case_indexes:
            raise PayoffEngineError("默认视觉模板的路径或分段索引越界")
        if len(set(case_indexes)) != len(case_indexes) or any(index < 0 or index >= len(compiled[path_index]) for index in case_indexes):
            raise PayoffEngineError("默认视觉模板的分段索引无效")
        expected_axis = _path_axis([compiled[path_index][index] for index in case_indexes])
        expected_kind = "conditional_terminal_metric" if expected_axis in {"S_T", "r_T", "W_T"} else "path_statistic_slice"
        if axis != expected_axis or chart_kind != expected_kind or not title:
            raise PayoffEngineError("默认视觉模板的横轴或图形视图与ResolvedContract不一致")
        selected.append({
            "path_index": path_index,
            "case_indexes": case_indexes,
            "axis": axis,
            "chart_kind": chart_kind,
            "title": title,
        })
    if {view["path_index"] for view in selected} != set(range(len(compiled))):
        raise PayoffEngineError("默认视觉模板必须逐一路径绑定ResolvedContract")
    return selected


def render_paths(contract: ResolvedContract, visual_template: Mapping[str, Any]) -> list[RenderedPath]:
    """按默认JSON声明的视图绑定绘制，并逐点以共享解释器核对路径与现金流。"""
    try:
        compiled = _compile_paths(contract)
    except DomainCompileError as error:
        raise PayoffEngineError(f"{contract.product_id}的分段定义域无法编译：{error}") from error
    _check_axis_unit(contract, compiled)
    templates = path_templates(contract)
    # 同一图的同一横轴点与候选历史在探针、相邻分段端点间会重复出现。缓存完整
    # 共享解释器结果，既不重写经济逻辑，也不跨合同复用任何状态。
    evaluation_cache: EvaluationCache = {}
    payoff_basis, premium_offset = _payoff_figure_basis(contract)
    rendered: list[RenderedPath] = []
    for view in _template_path_views(visual_template, compiled):
        path_index = view["path_index"]
        path = contract.paths[path_index]
        domains = compiled[path_index]
        case_indexes = view["case_indexes"]
        axis = view["axis"]
        x_scale = axis_scale(axis, domains, contract)
        case_seeds: list[tuple[int, CompiledDomain, tuple[float, CandidateTemplate, dict[str, Any]]]] = []
        for case_index in case_indexes:
            domain = domains[case_index]
            seed = _case_seed(contract, axis, x_scale, path_index, case_index, domain, templates, evaluation_cache)
            if seed is not None:
                case_seeds.append((case_index, domain, seed))
                continue
            raise PayoffEngineError(f"{contract.product_id}的路径{path_index + 1}分段{case_index + 1}无法确定候选历史，拒绝发布收益图。")

        # 同一经济路径的不同分段若须依赖不同历史状态，拆成独立子图。这样不会把
        # 例如不同n_coupon、tau_out或Q_acc的候选历史连成一条伪单变量曲线。
        groups: dict[str, list[tuple[int, CompiledDomain, tuple[float, CandidateTemplate, dict[str, Any]]]]] = {}
        for item in case_seeds:
            groups.setdefault(item[2][1].name, []).append(item)
        for state_index, entries in enumerate(groups.values(), start=1):
            fixed_template = entries[0][2][1]
            state_seed = entries[0][2][2]
            segments: list[list[dict[str, Any]]] = []
            selected_domains: list[CompiledDomain] = []
            selected_cases: list[dict[str, str]] = []
            selected_case_indexes: list[int] = []
            for case_index, domain, _ in entries:
                segments.extend(
                    _case_segments(
                        contract, axis, x_scale, path_index, case_index, domain,
                        fixed_template, evaluation_cache,
                    )
                )
                selected_domains.append(domain)
                selected_cases.append({"condition_tex": path["cases"][case_index]["domain"], "payoff_tex": path["cases"][case_index]["pnl"]})
                selected_case_indexes.append(case_index + 1)
            _apply_figure_basis(segments, premium_offset)
            jumps, turning_points = _annotate_boundaries(segments)
            values = [float(point["y"]) for segment in segments for point in segment]
            y_low, y_high = min(0.0, *values), max(0.0, *values)
            padding = max(1.0, (y_high - y_low) * 0.15)
            scale = {**x_scale, "y_min": y_low - padding, "y_max": y_high + padding}
            split_state = len(groups) > 1
            title = view["title"] if not split_state else f"{view['title']}·情景{state_index}"
            path_id = f"path_{path_index + 1}" if not split_state else f"path_{path_index + 1}_state_{state_index}"
            terminal_slice = axis in {"S_T", "r_T", "W_T"}
            chart_kind = view["chart_kind"]
            status_note = "固定非终值历史，仅终值变化" if terminal_slice else "按横轴路径统计量构造确定性候选路径"
            if payoff_basis == "gross_before_premium":
                status_note += "；收益图为扣费前毛收益"
            rendered.append(
                RenderedPath(
                    path_id=path_id, title=title, condition_tex=path["condition"],
                    payoff_tex="；".join(item["payoff_tex"] for item in selected_cases), piece_summaries=selected_cases,
                    chart_kind=chart_kind, axis={"unit": axis, "reference_value": _axis_reference(contract, axis), **_AXIS_META[axis]}, scale=scale,
                    segments=segments, jumps=jumps, turning_points=turning_points, thresholds=_thresholds(selected_domains), payoff_levels=_payoff_levels(segments),
                    payoff_basis=payoff_basis, status_note=status_note,
                    candidate_state={"template": fixed_template.name, "history_rule": "nonterminal_fixed" if terminal_slice else "axis_constructed", "seed_monitor_values": state_seed, "case_indexes": selected_case_indexes},
                )
            )
    return rendered


def _axis_reference(contract: ResolvedContract, axis: str) -> float:
    if axis in {"r_T", "n_in", "sigma_realized"}:
        return 0.0
    if "S0" in contract.terms:
        return float(contract.terms["S0"])
    if "S0Vec" in contract.terms:
        return float(min(contract.terms["S0Vec"]))
    return 100.0


def _protocol_contract(contract: ResolvedContract) -> dict[str, Any]:
    """输出共享核心的完整只读合同快照，而不是旧页面的裁剪副本。"""
    value = contract.to_protocol_dict() if hasattr(contract, "to_protocol_dict") else contract.to_dict()
    return _json_safe(deep_thaw(value))


def _template_term_bindings(asset: DefaultVisualAsset, contract: ResolvedContract) -> dict[str, Any]:
    """把本次合同的可展示条款绑定到固定视觉模板，不读取模板经济公式。"""
    template_terms = asset.template.get("terms", {})
    if not isinstance(template_terms, Mapping):  # 已由AssetResolver校验，保留防御性检查。
        raise PayoffEngineError(f"{contract.name_zh}的默认视觉模板缺少terms")
    return {
        str(key): _json_safe(deep_thaw(contract.terms[key]))
        for key in template_terms
        if key in contract.terms
    }


def _runtime_figure_payload(result: PayoffResult) -> dict[str, Any]:
    """保存本次参数图的JSON，不把默认JSON改造成新的合同事实源。"""
    payload = result.payload
    resolved = payload["resolved_contract"]
    return {
        "name_zh": payload["name_zh"],
        "visual_template": payload["visual_template"],
        "identity": resolved["identity"],
        "terms": resolved["terms"],
        "term_sources": resolved["term_sources"],
        "paths": resolved["paths"],
        "product_version": resolved.get("product_version"),
        "resolved_schedules": resolved.get("resolved_schedules"),
        "contract_fingerprint": resolved.get("contract_fingerprint"),
    }


def _coerce_payoff_contract(payoff_input: Any) -> ResolvedContract:
    """严格接收共享PayoffInput={ResolvedContract}。"""
    if not isinstance(payoff_input, PayoffInput) or not isinstance(payoff_input.contract, ResolvedContract):
        raise PayoffEngineError("render_payoff仅接受PayoffInput={ResolvedContract}")
    return payoff_input.contract


def _renderer_version() -> str:
    """渲染器内容哈希进入运行指纹，避免同合同下静默改变图形事实。"""
    renderer_path = Path(__file__).with_name("svg_renderer.py")
    try:
        return f"svg_renderer:{sha256(renderer_path.read_bytes()).hexdigest()[:16]}"
    except OSError as error:  # pragma: no cover - 部署文件缺失时无法正常运行
        raise PayoffEngineError("Payoffer SVG渲染器文件缺失") from error


def _json_safe(value: Any) -> Any:
    """把路径候选中的numpy值及无穷监测值规范化为严格JSON。"""
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.ndarray):
        return _json_safe(value.tolist())
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float):
        if np.isposinf(value):
            return "inf"
        if np.isneginf(value):
            return "-inf"
        if np.isnan(value):
            return "nan"
    return value


def render_payoff(payoff_input: PayoffInput) -> PayoffResult:
    """ResolvedContract→参数化Payoff SVG/JSON的唯一正常计算入口。"""
    contract = _coerce_payoff_contract(payoff_input)
    try:
        asset = resolve_default_visual_asset(contract)
    except DefaultAssetError as error:
        raise PayoffEngineError(str(error)) from error

    raw_paths = [asdict(path) for path in render_paths(contract, asset.template["visual_template"])]
    path_panels = _json_safe(raw_paths)
    resolved_contract = _protocol_contract(contract)
    renderer_version = _renderer_version()
    result: dict[str, Any] = {
        "module": "payoffer",
        "product_id": contract.product_id,
        "name_zh": contract.name_zh,
        "product_version": contract.product_version,
        "contract_fingerprint": contract.contract_fingerprint,
        "resolved_contract": resolved_contract,
        "default_visual_asset": asset.to_reference(),
        "visual_template": {
            "source": f"figures/json/{asset.name_zh}.json",
            "term_bindings": _template_term_bindings(asset, contract),
        },
        "path_panels": path_panels,
        "cashflow_semantics": {
            "source": "runtime.contracts.evaluate_payoff",
            "holder_side": "cash(t, amount)",
            "discounting": "not_applied",
        },
        "renderer_version": renderer_version,
    }
    result["semantic_result_hash"] = semantic_hash(result)
    svg = render_svg_text({"name_zh": contract.name_zh, "paths": raw_paths})
    if not isinstance(svg, str) or "<svg" not in svg:
        raise PayoffEngineError("SVG渲染器未返回有效SVG文本")
    result["svg_content_hash"] = sha256(svg.encode("utf-8")).hexdigest()
    return PayoffResult(payload=result, svg=svg)


def preview_payload(
    name_zh: str,
    term_overrides: Mapping[str, Any] | None = None,
    identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _, product, _ = _registry_with_default_figure(name_zh)
    if not product["identity"]["entry_status"]:
        return {
            "module": "payoffer", "name_zh": product["identity"]["name_zh"],
            "runtime_status": "blocked", "message": "该产品条款或路径仍待合同确认，不能用Po、P、BT运行；仅可查看既有只读资料图。", "paths": [],
        }
    contract = build_payoff_input(name_zh, term_overrides, identity)
    payoff_result = render_payoff(PayoffInput(contract=contract))
    return {
        **dict(payoff_result.payload),
        # 页面展示使用的历史字段仅在预览适配层生成，正式PayoffResult不复制路径。
        "paths": payoff_result.payload["path_panels"],
        "runtime_status": "enabled",
        "payoff_input": {"contract": _protocol_contract(contract)},
    }


def _store_payoff_result(
    result: PayoffResult,
    contract: ResolvedContract,
    *,
    store: ResultStorePort,
    tenant_id: str,
    task_id: str,
    run_id: str,
    analysis_case_id: str | None,
    candidate_id: str | None,
    catalog_version: str | None,
    expose_destination: bool = False,
) -> dict[str, Any]:
    """唯一结果写入路径：提交到调用方显式提供的ResultStore。"""
    protocol_contract = _protocol_contract(contract)
    default_asset = dict(result.payload["default_visual_asset"])
    execution_fingerprint = semantic_hash({
        "contract_fingerprint": contract.contract_fingerprint,
        "default_visual_asset": default_asset,
        "renderer_version": result.payload["renderer_version"],
    })
    output = {
        "payoff_json": "artifacts/payoff.json",
        "payoff_svg": "artifacts/payoff.svg",
        "artifact_manifest": "artifacts/artifact_manifest.json",
        "result": "result.json",
        "manifest": "manifest.json",
    }
    payoff_payload = dict(result.payload)
    payoff_semantic_hash = str(payoff_payload.pop("semantic_result_hash"))
    stored_result = {
        **payoff_payload,
        "payoff_semantic_hash": payoff_semantic_hash,
        "analysis_case_id": analysis_case_id,
        "candidate_id": candidate_id,
        "catalog_version": catalog_version,
        "contract_fingerprint": contract.contract_fingerprint,
        "output": output,
    }
    input_snapshot = {
        "payoff_input": {"contract": protocol_contract},
        "default_visual_asset": default_asset,
        "requested_output": "parameterized_payoff_svg",
        "host_scope": {
            "analysis_case_id": analysis_case_id,
            "candidate_id": candidate_id,
            "catalog_version": catalog_version,
            "contract_fingerprint": contract.contract_fingerprint,
        },
    }
    manifest = {
        "module": "payoffer",
        "tenant_id": tenant_id,
        "analysis_case_id": analysis_case_id,
        "task_id": task_id,
        "run_id": run_id,
        "candidate_id": candidate_id,
        "catalog_version": catalog_version,
        "status": "succeeded",
        "contract_fingerprint": contract.contract_fingerprint,
        "product_version": contract.product_version,
        "execution_fingerprint": execution_fingerprint,
        "payoff_semantic_hash": payoff_semantic_hash,
        "input_snapshot": "input_snapshot.json",
        "resolved_contract": "resolved_contract.json",
        "data_refs": "data_refs.json",
        "limitations": "limitations.json",
        "result": output["result"],
        "artifacts": [
            {"name": "payoff.json", "storage_ref": output["payoff_json"], "media_type": "application/json"},
            {"name": "payoff.svg", "storage_ref": output["payoff_svg"], "media_type": "image/svg+xml"},
        ],
    }
    files: dict[str, bytes | str | Mapping[str, Any]] = {
        "manifest.json": manifest,
        "input_snapshot.json": input_snapshot,
        "resolved_contract.json": protocol_contract,
        "data_refs.json": "[]\n",
        "limitations.json": "[]\n",
        output["payoff_json"]: _runtime_figure_payload(result),
        output["payoff_svg"]: result.svg,
        "result.json": stored_result,
    }
    try:
        reference = store.commit_module_run(
            module="payoffer", tenant_id=tenant_id, task_id=task_id, run_id=run_id, files=files,
        )
        destination = store.resolve_module_run(reference, tenant_id=tenant_id) if expose_destination else None
    except (StoreError, FileExistsError, FileNotFoundError, PermissionError, ValueError) as error:
        raise PayoffEngineError(str(error)) from error
    response = {
        "ok": True,
        "module": "payoffer",
        "status": "succeeded",
        "run_id": run_id,
        "analysis_case_id": analysis_case_id,
        "candidate_id": candidate_id,
        "catalog_version": catalog_version,
        "contract_fingerprint": contract.contract_fingerprint,
        "module_run_ref": {
            "module": reference.module,
            "tenant_id": reference.tenant_id,
            "task_id": reference.task_id,
            "run_id": reference.run_id,
            "expected_semantic_result_hash": reference.expected_semantic_result_hash,
            "expected_artifact_manifest_hash": reference.expected_artifact_manifest_hash,
        },
        "payoff_semantic_hash": payoff_semantic_hash,
    }
    if destination is not None:
        response["destination"] = str(destination)
    return response


def _host_contract(payoff_input: PayoffInput, host_context: ModuleHostContext) -> ResolvedContract:
    """只接受Host已验证的合同引用，不在Payoffer重做ProductVersion解析。"""
    contract = _coerce_payoff_contract(payoff_input)
    if not isinstance(host_context, ModuleHostContext) or host_context.module != "payoffer":
        raise PayoffEngineError("正式Payoffer运行必须接收payoffer的ModuleHostContext")
    if not {"module.run", "conversation.tool.run"}.intersection(host_context.request_policy):
        raise PayoffEngineError("ModuleHostContext未授权module.run或conversation.tool.run")
    required = {
        "task_id": host_context.task_id,
        "analysis_case_id": host_context.analysis_case_id,
        "candidate_id": host_context.candidate_id,
        "catalog_version": host_context.catalog_version,
        "contract_fingerprint": host_context.contract_fingerprint,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise PayoffEngineError(f"ModuleHostContext缺少正式运行字段：{','.join(missing)}")
    if host_context.contract_fingerprint != contract.contract_fingerprint:
        raise PayoffEngineError("ModuleHostContext.contract_fingerprint与ResolvedContract不一致")
    contract_ref = host_context.contract_ref
    if contract_ref is None:
        raise PayoffEngineError("正式Payoffer运行缺少Core已验证contract_ref")
    if contract_ref.schema_id != "optionhelper.resolved-contract/v1":
        raise PayoffEngineError("Core已验证contract_ref不是ResolvedContract v1引用")
    if contract_ref.content_hash != contract.contract_fingerprint:
        raise PayoffEngineError("Core已验证contract_ref与ResolvedContract fingerprint不一致")
    return contract


def run_payoff(
    payoff_input: PayoffInput,
    *,
    run_id: str,
    host_context: ModuleHostContext,
    result_store: ResultStorePort,
    tenant_id: str,
) -> dict[str, Any]:
    """正式入口：Host上下文与Host Store共同决定不可变运行范围。"""
    if result_store is None:
        raise PayoffEngineError("正式Payoffer运行必须由Host注入ResultStorePort")
    contract = _host_contract(payoff_input, host_context)
    result = render_payoff(payoff_input)
    return _store_payoff_result(
        result,
        contract,
        store=result_store,
        tenant_id=tenant_id,
        task_id=str(host_context.task_id),
        run_id=run_id,
        analysis_case_id=host_context.analysis_case_id,
        candidate_id=host_context.candidate_id,
        catalog_version=host_context.catalog_version,
    )


def run_runtime(
    name_zh: str,
    term_overrides: Mapping[str, Any] | None,
    identity: Mapping[str, Any] | None,
    task_id: str,
    run_id: str,
) -> dict[str, Any]:
    """独立页面开发入口：使用与Hosted结果隔离的本地Store。"""
    contract = build_payoff_input(name_zh, term_overrides, identity)
    payoff_input = PayoffInput(contract=contract)
    result = render_payoff(payoff_input)
    development_store = LocalResultStore(RESULT_ROOT / "payoffer-development")
    return _store_payoff_result(
        result,
        contract,
        store=development_store,
        tenant_id="local-development",
        task_id=task_id,
        run_id=run_id,
        analysis_case_id=task_id,
        candidate_id=None,
        catalog_version="local-development",
        expose_destination=True,
    )
