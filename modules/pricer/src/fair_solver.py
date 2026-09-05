"""Pricer公平参数反解的受控数值核心。

本模块只处理数值问题，不读取OptionReg、不解析用户提交的字段路径，也不
创建ModuleRun。产品目标的字段、合法域、收敛和精度规则由``fair_parameter``
能力目录提供；正式服务负责在每次估值前通过Core重新编译候选合同。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import inspect
import math
import re
from typing import Any, Callable, Mapping, Sequence


class FairParameterSolveError(ValueError):
    """公平参数求解的受控失败。

    ``code``是业务协议中的稳定分类；``details``只供同一Pricer Run的私有
    审计使用，不能直接投影到公开结果。
    """

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = dict(details or {})


@dataclass(frozen=True)
class ScalarValuation:
    """一个候选点的标量报价价值。

    ``value``必须已经按目标的``quote_value_basis``表达；内部金额、名义本金
    和100点现金流可以放在``private``，但不会由本模块公开。
    """

    value: float
    standard_error: float | None = None
    private: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class SolvePoint:
    value: float
    quote_value: float
    residual: float
    standard_error: float | None = None


@dataclass(frozen=True)
class FairSolveResult:
    status: str
    converged: bool
    solution: float | None
    residual: float | None
    iterations: int
    slope: float | None
    points: tuple[SolvePoint, ...]
    lower: float | None = None
    upper: float | None = None

    def to_private_dict(self) -> dict[str, Any]:
        """Return a complete numeric trace for the Store-only audit artifact."""
        return {
            "status": self.status,
            "converged": self.converged,
            "solution": self.solution,
            "residual": self.residual,
            "iterations": self.iterations,
            "slope": self.slope,
            "lower": self.lower,
            "upper": self.upper,
            "points": [
                {
                    "value": point.value,
                    "quote_value": point.quote_value,
                    "residual": point.residual,
                    "standard_error": point.standard_error,
                }
                for point in self.points
            ],
        }


def _text_identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FairParameterSolveError("ineligible", f"{label}不能为空")
    return value.strip()


def _interval_pair(value: Any, label: str = "认证区间") -> tuple[float, float]:
    if isinstance(value, Mapping):
        value = (value.get("lower"), value.get("upper"))
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise FairParameterSolveError("ineligible", f"{label}必须包含两个边界")
    lower = _finite(value[0], f"{label}.lower")
    upper = _finite(value[1], f"{label}.upper")
    if not lower < upper:
        raise FairParameterSolveError("ineligible", f"{label}必须为有限且有序的内部参数区间")
    return lower, upper


@dataclass(frozen=True, init=False)
class AnalyticalBoundEvidence:
    """Opaque proof type reserved for a future trusted valuation service.

    The generic numerical solver deliberately has no evidence issuer.  Until a
    formal service registers a real execution result, every instance is
    rejected by ``from_mapping`` and cannot promote a research solve.
    """

    certified_interval: tuple[float, float]
    fair_run_id: str
    solve_run_id: str
    model_id: str
    model_version: str
    product_id: str
    rule_revision: int
    target_id: str
    solve_input: Mapping[str, Any]
    valuation_error_upper_bound: float
    residual_upper_bound: float
    slope_absolute_lower_bound: float
    parameter_representation_error_upper_bound: float
    bound_sources: Mapping[str, str]
    _seal_digest: str = field(init=False, repr=False, compare=False)

    _BOUND_NAMES = (
        "valuation_error_upper_bound",
        "residual_upper_bound",
        "slope_absolute_lower_bound",
        "parameter_representation_error_upper_bound",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise FairParameterSolveError("ineligible", "Analytical认证证据只能由正式内部执行器签发")

    def _validate_payload(self) -> None:
        lower, upper = _interval_pair(self.certified_interval, "Analytical认证区间")
        object.__setattr__(self, "certified_interval", (lower, upper))
        for label in (
            "fair_run_id",
            "solve_run_id",
            "model_id",
            "model_version",
            "product_id",
            "target_id",
        ):
            _text_identity(getattr(self, label), f"Analytical证据{label}")
        if (
            isinstance(self.rule_revision, bool)
            or not isinstance(self.rule_revision, int)
            or self.rule_revision <= 0
        ):
            raise FairParameterSolveError("ineligible", "Analytical证据rule_revision必须为正整数")
        if not isinstance(self.solve_input, Mapping):
            raise FairParameterSolveError("ineligible", "Analytical证据solve_input必须为完整对象")
        for name in self._BOUND_NAMES:
            value = getattr(self, name)
            try:
                numeric = float(value)
            except (TypeError, ValueError) as error:
                raise FairParameterSolveError("ineligible", f"Analytical证据{name}必须为非负有限数") from error
            if isinstance(value, bool) or not math.isfinite(numeric) or numeric < 0.0:
                raise FairParameterSolveError("ineligible", f"Analytical证据{name}必须为非负有限数")
        if float(self.slope_absolute_lower_bound) <= 0.0:
            raise FairParameterSolveError("ineligible", "Analytical证据斜率认证下界必须为正")
        if not isinstance(self.bound_sources, Mapping):
            raise FairParameterSolveError("ineligible", "Analytical证据缺少逐边界来源")
        for name in self._BOUND_NAMES:
            source = self.bound_sources.get(name)
            if not isinstance(source, str) or not source.strip():
                raise FairParameterSolveError("ineligible", f"Analytical证据缺少{name}来源")

    def _assert_sealed(self) -> None:
        raise FairParameterSolveError(
            "evidence_unavailable",
            "当前通用公平求解器未连接受信任Analytical正式估值执行边界",
        )

    @property
    def sealed(self) -> bool:
        self._assert_sealed()
        return True

    @classmethod
    def from_mapping(cls, value: Any) -> "AnalyticalBoundEvidence":
        del cls, value
        raise FairParameterSolveError(
            "evidence_unavailable",
            "当前通用公平求解器未连接受信任Analytical正式估值执行边界",
        )

    def to_private_dict(self) -> dict[str, Any]:
        self._assert_sealed()
        return {
            "certified_interval": {"lower": self.certified_interval[0], "upper": self.certified_interval[1]},
            "fair_run_id": self.fair_run_id,
            "solve_run_id": self.solve_run_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "product_id": self.product_id,
            "rule_revision": self.rule_revision,
            "target_id": self.target_id,
            "solve_input": dict(self.solve_input),
            "valuation_error_upper_bound": self.valuation_error_upper_bound,
            "residual_upper_bound": self.residual_upper_bound,
            "slope_absolute_lower_bound": self.slope_absolute_lower_bound,
            "parameter_representation_error_upper_bound": self.parameter_representation_error_upper_bound,
            "bound_sources": dict(self.bound_sources),
        }


@dataclass(frozen=True, init=False)
class MonteCarloBatchEvidence:
    """Opaque proof type reserved for a future trusted valuation service."""

    fair_run_id: str
    solve_run_id: str
    batch_id: str
    derived_seed: int | str
    random_matrix_fingerprint: str
    product_id: str
    rule_revision: int
    target_id: str
    solve_input: Mapping[str, Any]
    model_id: str
    model_version: str
    path_count: int
    solve_result: FairSolveResult
    _seal_digest: str = field(init=False, repr=False, compare=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise FairParameterSolveError("ineligible", "MC批次证据只能由正式内部执行器签发")

    def _validate_payload(self) -> None:
        for label in (
            "fair_run_id",
            "solve_run_id",
            "batch_id",
            "random_matrix_fingerprint",
            "product_id",
            "target_id",
            "model_id",
            "model_version",
        ):
            _text_identity(getattr(self, label), f"MC批次{label}")
        if (
            isinstance(self.rule_revision, bool)
            or not isinstance(self.rule_revision, int)
            or self.rule_revision <= 0
        ):
            raise FairParameterSolveError("ineligible", "MC批次rule_revision必须为正整数")
        if not isinstance(self.solve_input, Mapping):
            raise FairParameterSolveError("ineligible", "MC批次solve_input必须为完整对象")
        if isinstance(self.derived_seed, bool) or not isinstance(self.derived_seed, (int, str)):
            raise FairParameterSolveError("ineligible", "MC批次derived_seed必须可复现且非布尔值")
        if isinstance(self.derived_seed, str) and not self.derived_seed.strip():
            raise FairParameterSolveError("ineligible", "MC批次derived_seed不能为空")
        if isinstance(self.path_count, bool) or not isinstance(self.path_count, int) or self.path_count <= 0:
            raise FairParameterSolveError("ineligible", "MC批次path_count必须为正整数")
        if not isinstance(self.solve_result, FairSolveResult):
            raise FairParameterSolveError("ineligible", "MC批次必须绑定FairSolveResult")

    def _assert_sealed(self) -> None:
        raise FairParameterSolveError(
            "evidence_unavailable",
            "当前通用公平求解器未连接受信任Monte Carlo正式估值执行边界",
        )

    @property
    def sealed(self) -> bool:
        self._assert_sealed()
        return True

    @classmethod
    def from_mapping(cls, value: Any) -> "MonteCarloBatchEvidence":
        del cls, value
        raise FairParameterSolveError(
            "evidence_unavailable",
            "当前通用公平求解器未连接受信任Monte Carlo正式估值执行边界",
        )

    def to_private_dict(self) -> dict[str, Any]:
        self._assert_sealed()
        return {
            "fair_run_id": self.fair_run_id,
            "solve_run_id": self.solve_run_id,
            "batch_id": self.batch_id,
            "derived_seed": self.derived_seed,
            "random_matrix_fingerprint": self.random_matrix_fingerprint,
            "product_id": self.product_id,
            "rule_revision": self.rule_revision,
            "target_id": self.target_id,
            "solve_input": dict(self.solve_input),
            "model_id": self.model_id,
            "model_version": self.model_version,
            "path_count": self.path_count,
            "solve_result": self.solve_result.to_private_dict(),
        }


def _field(target: Any, name: str, default: Any = None) -> Any:
    if isinstance(target, Mapping):
        return target.get(name, default)
    return getattr(target, name, default)


def _first_mapping(source: Mapping[str, Any], *names: str) -> Mapping[str, Any] | None:
    """Return the first named mapping without assuming a directory shape."""
    for name in names:
        value = source.get(name)
        if isinstance(value, Mapping):
            return value
    return None


def _first_number(source: Mapping[str, Any], *names: str) -> float | None:
    """Read one registered numeric control from a mapping."""
    for name in names:
        value = source.get(name)
        if value is None:
            continue
        return _finite(value, name)
    return None


def _number_from_sections(
    source: Mapping[str, Any],
    section_names: Sequence[str],
    number_names: Sequence[str],
) -> float | None:
    value = _first_number(source, *number_names)
    if value is not None:
        return value
    for section_name in section_names:
        section = source.get(section_name)
        if isinstance(section, Mapping):
            value = _first_number(section, *number_names)
            if value is not None:
                return value
    return None


def _maximum_parameter_absolute_error(target: Any, method: str) -> tuple[float | None, str | None]:
    """Keep formal quote qualification closed until the Core directory binds it.

    The numerical solver accepts dynamic mappings for research experiments.
    They are not an authenticated TargetCapability, so a caller cannot add a
    threshold or source to ``target`` and turn a research result into a quote.
    A future trusted service may pass a threshold obtained from its sealed Core
    binding; that integration is intentionally absent in this module.
    """
    del target, method
    return None, "正式能力目录未登记产品级参数绝对误差门槛"


def _first_sequence_number(value: Any, index: int) -> float | None:
    if not isinstance(value, (list, tuple)) or len(value) <= index:
        return None
    item = value[index]
    return None if item is None else _finite(item, "搜索区间")


def _public_parameter_encoder(
    target: Any,
    *,
    reference_price_basis: float | None = None,
    override: Callable[[float], float] | None = None,
) -> Callable[[float], float]:
    """Build the registered internal-to-public parameter conversion."""
    if override is not None:
        return lambda value: _finite(override(value), "公开参数")
    basis = _field(target, "internal_value_basis", {})
    basis = basis if isinstance(basis, Mapping) else {}
    transform = str(basis.get("public_transform", basis.get("public_conversion", "identity_ratio")))
    if transform == "points_100_to_decimal_ratio":
        return lambda value: _finite(value, "内部参数") / 100.0
    if transform in {"identity_ratio", "not_applicable_non_continuous"}:
        return lambda value: _finite(value, "内部参数")
    if transform == "relative_to_frozen_reference_price_basis":
        raw_reference = reference_price_basis
        if raw_reference is None:
            raw_reference = basis.get("reference_price_basis")
        reference = _finite(raw_reference, "冻结reference_price_basis") if raw_reference is not None else None
        if reference is None or reference <= 0.0:
            raise FairParameterSolveError("ineligible", "price目标缺少正的冻结reference_price_basis")
        return lambda value: _finite(value, "内部参数") / reference
    raise FairParameterSolveError("ineligible", f"目标公开参数转换未登记：{transform}")


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise FairParameterSolveError("ineligible", f"{label}不得为布尔值")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise FairParameterSolveError("ineligible", f"{label}必须为有限数值") from error
    if not math.isfinite(number):
        raise FairParameterSolveError("ineligible", f"{label}必须为有限数值")
    return number


def _numeric_value(value: Any, quote_value_basis: str) -> float:
    if isinstance(value, ScalarValuation):
        return _finite(value.value, "候选报价价值")
    if isinstance(value, Mapping):
        # The registered public value basis is part of the equation.  Never
        # silently substitute another basis: that would introduce a
        # factor-of-100 or product-basis error.
        if quote_value_basis in value and value[quote_value_basis] is not None:
            return _finite(value[quote_value_basis], "候选报价价值")
        raise FairParameterSolveError("data", "候选估值缺少quote_value_basis价值")
    if hasattr(value, quote_value_basis):
        raw = getattr(value, quote_value_basis)
        if raw is not None:
            return _finite(raw, "候选报价价值")
    return _finite(value, "候选报价价值")


def _standard_error(value: Any) -> float | None:
    if isinstance(value, ScalarValuation):
        raw = value.standard_error
    elif isinstance(value, Mapping):
        raw = value.get("standard_error", value.get("standard_error_percent"))
    else:
        raw = getattr(value, "standard_error", None)
        if raw is None:
            raw = getattr(value, "standard_error_percent", None)
    if raw is None:
        return None
    number = _finite(raw, "候选估值标准误")
    return abs(number)


def _is_cancelled(callback: Callable[[], bool] | None) -> bool:
    return bool(callback is not None and callback())


def _emit_progress(callback: Callable[..., Any] | None, payload: Mapping[str, Any]) -> None:
    if callback is None:
        return
    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        parameters = {}
    if len(parameters) == 0:
        callback()
    else:
        callback(dict(payload))


class FairParameterSolver:
    """Resolve one registered scalar target with affine or monotone methods.

    The evaluator receives *internal* candidate values. It must return a scalar
    valuation already expressed on the target's registered quote value basis.
    No candidate callback is allowed to receive a user-defined bracket or
    tolerance; all numerical controls are read from the target specification.
    """

    def __init__(
        self,
        target: Any,
        *,
        evaluator: Callable[[float], Any],
        base_value: float | None = None,
        target_value: float | None = None,
        base_contract: Any | None = None,
        registry: Mapping[str, Any] | None = None,
        method: str | None = None,
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[..., Any] | None = None,
    ) -> None:
        self.target = target
        self.evaluator = evaluator
        self.base_contract = base_contract
        self.registry = registry
        self.method = None if method is None else str(method)
        self.cancelled = cancelled
        self.progress = progress
        raw_quote_basis = _field(target, "quote_value_basis", None)
        self.quote_value_basis = "" if raw_quote_basis is None else str(raw_quote_basis).strip()
        self._target_value_declared = target_value is not None
        if target_value is None:
            descriptor = _field(target, "quote_target_descriptor", {})
            descriptor = descriptor if isinstance(descriptor, Mapping) else {}
            if "target_value" in descriptor:
                target_value = descriptor.get("target_value")
                self._target_value_declared = target_value is not None
        self.target_value = None if target_value is None else _finite(target_value, "报价目标")
        self.base_value = None if base_value is None else _finite(base_value, "基础目标参数")
        self._points: list[SolvePoint] = []
        self._point_cache: dict[float, SolvePoint] = {}
        self._inferred_lower: float | None = None
        self._inferred_upper: float | None = None
        self._inferred_lower_strict = False
        self._inferred_upper_strict = False
        self._domain_lower_strict = False
        self._domain_upper_strict = False

    def solve(self) -> FairSolveResult:
        self._check_cancelled()
        status = str(_field(self.target, "solver_class", ""))
        if status not in {"affine", "bounded_monotone", "coupled_transform"}:
            raise FairParameterSolveError("unsupported", f"目标求解类型不受支持：{status or '未登记'}")
        support_status = str(_field(self.target, "support_status", _field(self.target, "status", "")))
        if support_status != "supported":
            reason = _field(self.target, "unsupported_reason", None) or "目标未获得反解支持"
            error_code = (
                support_status
                if support_status in {"unsupported", "solve_semantics_blocked"}
                else "ineligible"
            )
            raise FairParameterSolveError(error_code, str(reason))
        if self.method is not None:
            methods = tuple(str(item) for item in (_field(self.target, "allowed_methods", ()) or ()))
            if self.method not in methods:
                raise FairParameterSolveError("unsupported", f"目标不支持定价方法：{self.method}")
        if not self.quote_value_basis:
            raise FairParameterSolveError("ineligible", "目标缺少quote_value_basis")
        if not self._target_value_declared or self.target_value is None:
            raise FairParameterSolveError("ineligible", "目标缺少受控报价目标值")
        self._direction_sign()
        bracket = self._find_bracket()
        if status == "affine":
            solved = self._solve_affine(bracket)
        else:
            solved = self._solve_monotone(bracket)
        return solved

    def _check_cancelled(self) -> None:
        if _is_cancelled(self.cancelled):
            raise FairParameterSolveError("cancelled", "用户取消公平参数反解", details={"points": self._point_dicts()})

    def _evaluate(self, value: float) -> SolvePoint:
        self._check_cancelled()
        number = _finite(value, "候选参数")
        cached = self._point_cache.get(number)
        if cached is not None:
            return cached
        raw = self.evaluator(number)
        quote_value = _numeric_value(raw, self.quote_value_basis)
        error = quote_value - self.target_value
        if not math.isfinite(error):
            raise FairParameterSolveError("data", "候选报价方程不是有限值", details={"value": number})
        point = SolvePoint(number, quote_value, error, _standard_error(raw))
        self._points.append(point)
        self._point_cache[number] = point
        _emit_progress(self.progress, {
            "stage": "candidate_valuation",
            "completed": len(self._points),
            "value": number,
            "residual": error,
        })
        return point

    def _domain_and_policy(self) -> tuple[dict[str, float | None], Mapping[str, Any]]:
        domain = _field(self.target, "domain", {})
        policy = _field(self.target, "search_bracket_policy", {})
        domain = dict(domain) if isinstance(domain, Mapping) else {}
        policy = dict(policy) if isinstance(policy, Mapping) else {}
        lower: float | None = None
        upper: float | None = None
        self._domain_lower_strict = False
        self._domain_upper_strict = False
        if domain.get("min") is not None:
            lower = _finite(domain["min"], "目标合法域下限")
        elif domain.get("inclusive_min") is not None:
            lower = _finite(domain["inclusive_min"], "目标合法域下限")
        elif domain.get("exclusive_min") is not None:
            lower = _finite(domain["exclusive_min"], "目标合法域下限")
            self._domain_lower_strict = True
        if domain.get("max") is not None:
            upper = _finite(domain["max"], "目标合法域上限")
        elif domain.get("inclusive_max") is not None:
            upper = _finite(domain["inclusive_max"], "目标合法域上限")
        elif domain.get("exclusive_max") is not None:
            upper = _finite(domain["exclusive_max"], "目标合法域上限")
            self._domain_upper_strict = True

        def policy_number(*names: str) -> float | None:
            for name in names:
                raw = policy.get(name)
                if raw is not None:
                    return _finite(raw, f"search_bracket_policy.{name}")
            return None

        policy_lower = policy_number(
            "lower", "domain_lower", "initial_lower", "bracket_lower",
            "finite_lower", "search_lower", "lower_bound",
        )
        policy_upper = policy_number(
            "upper", "domain_upper", "initial_upper", "max_value", "bracket_upper",
            "finite_upper", "search_upper", "upper_bound",
        )
        nested_interval = _first_mapping(
            policy,
            "initial_bracket", "finite_interval", "finite_search_interval",
            "bracket", "interval", "initial_interval", "search_interval",
        )
        if nested_interval is not None:
            if policy_lower is None:
                policy_lower = _first_number(nested_interval, "lower", "min", "left", "start")
            if policy_upper is None:
                policy_upper = _first_number(nested_interval, "upper", "max", "right", "end")
        if policy_lower is None:
            policy_lower = _first_sequence_number(policy.get("initial_interval"), 0)
        if policy_upper is None:
            policy_upper = _first_sequence_number(policy.get("initial_interval"), 1)
        if policy_lower is None:
            policy_lower = _first_sequence_number(policy.get("finite_interval"), 0)
        if policy_upper is None:
            policy_upper = _first_sequence_number(policy.get("finite_interval"), 1)
        if policy_lower is None:
            policy_lower = _first_sequence_number(policy.get("finite_search_interval"), 0)
        if policy_upper is None:
            policy_upper = _first_sequence_number(policy.get("finite_search_interval"), 1)
        self._infer_constraint_bounds(domain, policy, lower, upper)
        # Intersect explicit economic-domain bounds with the simple fixed-term
        # constraints declared by Core.  Search-seed bounds remain separate;
        # neither source is allowed to widen the legal domain.
        inferred_lower = getattr(self, "_inferred_lower", None)
        inferred_upper = getattr(self, "_inferred_upper", None)
        if inferred_lower is not None and (lower is None or inferred_lower > lower):
            lower = inferred_lower
            self._domain_lower_strict = self._inferred_lower_strict
        elif inferred_lower is not None and lower == inferred_lower:
            self._domain_lower_strict = self._domain_lower_strict or self._inferred_lower_strict
        if inferred_upper is not None and (upper is None or inferred_upper < upper):
            upper = inferred_upper
            self._domain_upper_strict = self._inferred_upper_strict
        elif inferred_upper is not None and upper == inferred_upper:
            self._domain_upper_strict = self._domain_upper_strict or self._inferred_upper_strict
        if lower is not None and upper is not None and not lower < upper:
            raise FairParameterSolveError("ineligible", "目标合法区间为空")
        # Preserve the policy as a private runtime object.  These resolved
        # seed bounds never come from caller input and are not part of the
        # public result.
        policy = dict(policy)
        if policy_lower is not None:
            policy["__resolved_initial_lower"] = policy_lower
        if policy_upper is not None:
            policy["__resolved_initial_upper"] = policy_upper
        return {"lower": lower, "upper": upper}, policy

    def _infer_constraint_bounds(
        self,
        domain: Mapping[str, Any],
        policy: Mapping[str, Any],
        lower: float | None,
        upper: float | None,
    ) -> None:
        """Infer only simple target-vs-fixed-term bounds from Core constraints.

        This is a convenience for a registered policy such as ``K < S_0``;
        arbitrary expressions are never accepted as caller-supplied bounds.
        """
        inferred_lower: float | None = None
        inferred_upper: float | None = None
        inferred_lower_strict = False
        inferred_upper_strict = False
        contract = self.base_contract
        if contract is None or not isinstance(getattr(contract, "terms", None), Mapping):
            self._inferred_lower = None
            self._inferred_upper = None
            self._inferred_lower_strict = False
            self._inferred_upper_strict = False
            return
        target_symbol = str(_field(self.target, "symbol", ""))
        keys = list(_field(self.target, "controlled_term_keys", ()) or ())
        target_keys = set(str(item) for item in keys)
        catalog = self.registry.get("term_catalog", {}) if isinstance(self.registry, Mapping) else {}
        if not isinstance(catalog, Mapping):
            catalog = {}

        def key_for(token: str) -> str | None:
            if token in contract.terms:
                return token
            for key, metadata in catalog.items():
                if isinstance(metadata, Mapping) and str(metadata.get("symbol")) == token:
                    return str(key)
            compact = token.replace("_", "")
            for key in contract.terms:
                if str(key).replace("_", "") == compact:
                    return str(key)
            return None

        constraint_values: list[str] = []
        envelope = policy.get("domain_envelope")
        if isinstance(envelope, Mapping):
            raw = envelope.get("contract_constraints", ())
            if isinstance(raw, (list, tuple)):
                constraint_values.extend(str(item) for item in raw)
        raw_contract_constraints = contract.terms.get("constraints", ())
        if isinstance(raw_contract_constraints, (list, tuple)):
            constraint_values.extend(str(item) for item in raw_contract_constraints)
        token = r"[A-Za-z][A-Za-z0-9_]*"
        pattern = re.compile(rf"^\s*({token})\s*(<=|<|>=|>)\s*({token})\s*$")
        for expression in constraint_values:
            match = pattern.match(expression)
            if match is None:
                continue
            left, relation, right = match.groups()
            left_key, right_key = key_for(left), key_for(right)
            left_is_target = left == target_symbol or left_key in target_keys
            right_is_target = right == target_symbol or right_key in target_keys
            if left_is_target and not right_is_target and right_key in contract.terms:
                fixed = contract.terms[right_key]
                if isinstance(fixed, (int, float)) and not isinstance(fixed, bool) and math.isfinite(float(fixed)):
                    if relation in {"<", "<="}:
                        inferred_upper = float(fixed) if inferred_upper is None else min(inferred_upper, float(fixed))
                        inferred_upper_strict = inferred_upper_strict or relation == "<"
                    elif relation in {">", ">="}:
                        inferred_lower = float(fixed) if inferred_lower is None else max(inferred_lower, float(fixed))
                        inferred_lower_strict = inferred_lower_strict or relation == ">"
            elif right_is_target and not left_is_target and left_key in contract.terms:
                fixed = contract.terms[left_key]
                if isinstance(fixed, (int, float)) and not isinstance(fixed, bool) and math.isfinite(float(fixed)):
                    if relation in {"<", "<="}:
                        inferred_lower = float(fixed) if inferred_lower is None else max(inferred_lower, float(fixed))
                        inferred_lower_strict = inferred_lower_strict or relation == "<"
                    elif relation in {">", ">="}:
                        inferred_upper = float(fixed) if inferred_upper is None else min(inferred_upper, float(fixed))
                        inferred_upper_strict = inferred_upper_strict or relation == ">"
        self._inferred_lower = inferred_lower
        self._inferred_upper = inferred_upper
        self._inferred_lower_strict = inferred_lower_strict
        self._inferred_upper_strict = inferred_upper_strict

    def _bounds(self) -> tuple[float | None, float | None, Mapping[str, Any]]:
        values, policy = self._domain_and_policy()
        legal_lower = values["lower"]
        legal_upper = values["upper"]
        lower = policy.get("__resolved_initial_lower")
        upper = policy.get("__resolved_initial_upper")
        lower = None if lower is None else _finite(lower, "搜索区间下限")
        upper = None if upper is None else _finite(upper, "搜索区间上限")
        if lower is None:
            lower = legal_lower
        if upper is None:
            upper = legal_upper
        base = self.base_value
        # An open or half-infinite economic domain still needs a finite
        # registered search seed.  A base-centred seed is accepted only when
        # the target specification supplies its span; an implementation
        # fallback would make the result depend on solver internals.
        initial_interval = _first_mapping(
            policy,
            "initial_interval", "initial_bracket", "finite_interval", "finite_search_interval",
        ) or {}
        span = _first_number(
            policy,
            "initial_span", "initial_half_width", "base_half_width", "seed_half_width",
        )
        if span is None:
            span = _first_number(
                initial_interval,
                "initial_span", "initial_half_width", "base_half_width", "seed_half_width",
            )
        relative_span = _first_number(
            policy,
            "base_relative_half_width", "relative_half_width", "relative_seed_half_width",
        )
        if relative_span is None:
            relative_span = _first_number(
                initial_interval,
                "base_relative_half_width", "relative_half_width", "relative_seed_half_width",
            )
        zero_base_span = _first_number(policy, "zero_base_half_width", "zero_base_span")
        if zero_base_span is None:
            zero_base_span = _first_number(initial_interval, "zero_base_half_width", "zero_base_span")
        if span is None and relative_span is not None and base is not None:
            if relative_span <= 0.0:
                raise FairParameterSolveError("ineligible", "搜索区间相对初始宽度必须为正")
            span = abs(base) * relative_span
            if span == 0.0:
                span = zero_base_span
        if span is not None and span <= 0.0:
            raise FairParameterSolveError("ineligible", "搜索区间初始宽度必须为正")
        if lower is None or upper is None:
            if base is None or span is None:
                raise FairParameterSolveError("ineligible", "目标缺少登记的有限搜索区间")
            if lower is None:
                lower = base - span
            if upper is None:
                upper = base + span
        # Candidate evaluation must stay strictly inside open boundaries. A
        # A machine-adjacent probe can be numerically meaningless for a
        # contract parameter, so the capability directory supplies a safe
        # interior distance scaled to the frozen base value.
        effective_lower = legal_lower
        effective_upper = legal_upper
        if self._domain_lower_strict and legal_lower is not None:
            effective_lower = self._safe_open_endpoint(float(legal_lower), side="lower", policy=policy)
        if self._domain_upper_strict and legal_upper is not None:
            effective_upper = self._safe_open_endpoint(float(legal_upper), side="upper", policy=policy)
        if effective_lower is not None:
            lower = max(float(lower), float(effective_lower))
        if effective_upper is not None:
            upper = min(float(upper), float(effective_upper))
        if lower is None or upper is None or not lower < upper:
            raise FairParameterSolveError("ineligible", "目标合法区间为空")
        return float(lower), float(upper), policy

    def _safe_open_endpoint(self, endpoint: float, *, side: str, policy: Mapping[str, Any]) -> float:
        rule = _first_mapping(policy, "safe_endpoint", "safe_endpoint_rule", "open_boundary_rule")
        if rule is None:
            raise FairParameterSolveError("ineligible", "开放合法域缺少登记的安全端点规则")
        relative = _first_number(
            rule,
            "relative_to_base", "relative_base_epsilon", "relative_epsilon",
        )
        absolute = _first_number(
            rule,
            "absolute_epsilon", "absolute_endpoint_epsilon", "epsilon",
        )
        zero_base_absolute = _first_number(
            rule,
            "zero_base_absolute_epsilon", "zero_base_epsilon",
        )
        if relative is None or absolute is None or zero_base_absolute is None:
            raise FairParameterSolveError("ineligible", "开放合法域安全端点规则缺少相对与绝对epsilon")
        if relative < 0.0 or absolute <= 0.0 or zero_base_absolute <= 0.0:
            raise FairParameterSolveError("ineligible", "开放合法域安全端点epsilon必须有效")
        if self.base_value is None:
            raise FairParameterSolveError("ineligible", "开放合法域安全端点需要冻结基础参数")
        base_scale = abs(float(self.base_value))
        distance = max(base_scale * relative, zero_base_absolute if base_scale == 0.0 else absolute)
        if not math.isfinite(distance) or distance <= 0.0:
            raise FairParameterSolveError("ineligible", "开放合法域安全端点距离无效")
        safe = endpoint + distance if side == "lower" else endpoint - distance
        if not math.isfinite(safe) or safe == endpoint:
            raise FairParameterSolveError("ineligible", "开放合法域无法生成有限安全端点")
        return safe

    def _find_bracket(self) -> tuple[SolvePoint, SolvePoint, float | None, float | None]:
        lower_raw, upper_raw, policy = self._bounds()
        lower = lower_raw
        upper = upper_raw
        interval_width = abs(upper - lower)
        expansion_rule = _first_mapping(policy, "expansion", "expansion_policy", "expansion_rule") or {}
        expansion_step = _first_number(
            policy, "expansion_step", "minimum_expansion_step", "step",
        )
        if expansion_step is None:
            expansion_step = _first_number(
                expansion_rule, "expansion_step", "minimum_expansion_step", "step",
            )
        if expansion_step is None:
            expansion_step = interval_width
        if expansion_step <= 0.0:
            raise FairParameterSolveError("ineligible", "search_bracket_policy缺少有效扩展步长")
        factor = _first_number(policy, "expansion_factor", "expand_factor", "factor")
        if factor is None:
            factor = _first_number(expansion_rule, "expansion_factor", "expand_factor", "factor")
        if factor is None or factor <= 1.0:
            raise FairParameterSolveError("ineligible", "search_bracket_policy缺少有效扩展因子")
        max_expansions = _first_number(
            policy, "max_expansions", "max_bracket_expansions", "maximum_expansions", "expansion_limit",
        )
        if max_expansions is None:
            max_expansions = _first_number(
                expansion_rule, "max_expansions", "max_bracket_expansions", "maximum_expansions", "expansion_limit",
            )
        if max_expansions is None or max_expansions < 0 or not max_expansions.is_integer():
            raise FairParameterSolveError("ineligible", "search_bracket_policy缺少有效扩展次数")
        max_expansions = int(max_expansions)
        domain_values, _ = self._domain_and_policy()
        domain_lower, domain_upper = domain_values["lower"], domain_values["upper"]
        direction = self._direction_sign()
        low = self._evaluate(lower)
        high = self._evaluate(upper)
        residual_tolerance = self._residual_tolerance()
        endpoint_slope = (high.residual - low.residual) / (high.value - low.value)
        if math.isfinite(endpoint_slope) and abs(endpoint_slope) <= self._slope_floor():
            raise FairParameterSolveError(
                "zero_slope",
                "目标报价方程运行时斜率为0或接近0",
                details={"slope": endpoint_slope, "points": self._point_dicts()},
            )
        if math.isfinite(endpoint_slope) and endpoint_slope * direction < 0.0:
            raise FairParameterSolveError(
                "no_unique_root",
                "运行报价方向与能力目录monotonic_direction不一致",
                details={
                    "slope": endpoint_slope,
                    "monotonic_direction": "increasing" if direction > 0 else "decreasing",
                    "points": self._point_dicts(),
                },
            )
        if abs(low.residual) <= residual_tolerance:
            return low, high, domain_lower, domain_upper
        if abs(high.residual) <= residual_tolerance:
            return low, high, domain_lower, domain_upper
        expansions = 0
        for _ in range(max_expansions):
            self._check_cancelled()
            if low.residual * high.residual < 0.0:
                return low, high, domain_lower, domain_upper
            # Residual sign alone is not enough: the same sign means the
            # missing root is on opposite sides for increasing and
            # decreasing targets.  Direction comes from audited cashflow
            # coefficient evidence, never from these two samples.
            expand_left = (
                (direction > 0 and low.residual > 0.0 and high.residual > 0.0)
                or (direction < 0 and low.residual < 0.0 and high.residual < 0.0)
            )
            expand_right = (
                (direction > 0 and low.residual < 0.0 and high.residual < 0.0)
                or (direction < 0 and low.residual > 0.0 and high.residual > 0.0)
            )
            # The finite-domain side is already authoritative; only a
            # genuinely half-infinite side may be expanded.
            step = max(expansion_step, abs(high.value - low.value))
            if expand_left and domain_lower is None:
                next_lower = low.value - step * factor
                if not math.isfinite(next_lower) or next_lower == low.value:
                    break
                low = self._evaluate(next_lower)
                expansions += 1
                continue
            if expand_right and domain_upper is None:
                next_upper = high.value + step * factor
                if not math.isfinite(next_upper) or next_upper == high.value:
                    break
                high = self._evaluate(next_upper)
                expansions += 1
                continue
            break
        if (
            abs(low.residual) <= residual_tolerance
            or abs(high.residual) <= residual_tolerance
            or low.residual * high.residual < 0.0
        ):
            return low, high, domain_lower, domain_upper
        raise FairParameterSolveError(
            "no_bracket",
            "目标报价价值在登记合法搜索区间内未被包络",
            details={
                "lower": low.value,
                "upper": high.value,
                "expansions": expansions,
                "max_expansions": max_expansions,
                "monotonic_direction": "increasing" if direction > 0 else "decreasing",
                "points": self._point_dicts(),
            },
        )

    def _solve_affine(
        self,
        bracket: tuple[SolvePoint, SolvePoint, float | None, float | None],
    ) -> FairSolveResult:
        low, high, domain_lower, domain_upper = bracket
        if low.value == high.value:
            return FairSolveResult("converged", True, low.value, low.residual, 0, None, tuple(self._points), low.value, high.value)
        slope = (high.residual - low.residual) / (high.value - low.value)
        if not math.isfinite(slope) or abs(slope) <= self._slope_floor():
            raise FairParameterSolveError("zero_slope", "目标报价方程运行时斜率为0或接近0", details={"slope": slope, "points": self._point_dicts()})
        root = low.value - low.residual / slope
        if not self._inside(root, domain_lower, domain_upper) or not min(low.value, high.value) <= root <= max(low.value, high.value):
            raise FairParameterSolveError("no_bracket", "解析根不在登记合法搜索区间内", details={"root": root, "points": self._point_dicts()})
        # A midpoint check catches an incorrectly advertised affine target
        # without exposing a second business entry point.
        midpoint = (low.value + high.value) / 2.0
        if midpoint != low.value and midpoint != high.value:
            mid = self._evaluate(midpoint)
            predicted = low.residual + slope * (midpoint - low.value)
            tolerance = self._residual_tolerance()
            if abs(mid.residual - predicted) > tolerance:
                raise FairParameterSolveError("no_unique_root", "运行时报价方程未满足登记仿射关系", details={"points": self._point_dicts()})
        solved = self._evaluate(root)
        if abs(solved.residual) > self._residual_tolerance():
            raise FairParameterSolveError("non_converged", "仿射反解最终报价残差未达登记阈值", details={"residual": solved.residual, "points": self._point_dicts()})
        return FairSolveResult("converged", True, root, solved.residual, 1, slope, tuple(self._points), low.value, high.value)

    def _solve_monotone(
        self,
        bracket: tuple[SolvePoint, SolvePoint, float | None, float | None],
    ) -> FairSolveResult:
        low, high, domain_lower, domain_upper = bracket
        if low.value == high.value:
            return FairSolveResult("converged", True, low.value, low.residual, 0, None, tuple(self._points), low.value, high.value)
        residual_tolerance = self._residual_tolerance()
        if abs(low.residual) <= residual_tolerance:
            slope = self._slope_between(low, high)
            if slope is None or abs(slope) <= self._slope_floor():
                raise FairParameterSolveError("zero_slope", "目标报价方程运行时斜率为0或接近0", details={"slope": slope, "points": self._point_dicts()})
            self._validate_monotonicity()
            return FairSolveResult("converged", True, low.value, low.residual, 0, slope, tuple(self._points), low.value, high.value)
        if abs(high.residual) <= residual_tolerance:
            slope = self._slope_between(low, high)
            if slope is None or abs(slope) <= self._slope_floor():
                raise FairParameterSolveError("zero_slope", "目标报价方程运行时斜率为0或接近0", details={"slope": slope, "points": self._point_dicts()})
            self._validate_monotonicity()
            return FairSolveResult("converged", True, high.value, high.residual, 0, slope, tuple(self._points), low.value, high.value)
        slope = (high.residual - low.residual) / (high.value - low.value)
        if not math.isfinite(slope) or abs(slope) <= self._slope_floor():
            raise FairParameterSolveError("zero_slope", "目标报价方程运行时斜率为0或接近0", details={"slope": slope, "points": self._point_dicts()})
        max_iterations = self._integer_rule("max_iterations", "maximum_iterations", "iteration_limit")
        x_tolerance = self._x_tolerance()
        iterations = 0
        result = low if abs(low.residual) <= abs(high.residual) else high
        for iterations in range(1, max_iterations + 1):
            self._check_cancelled()
            midpoint = (low.value + high.value) / 2.0
            result = self._evaluate(midpoint)
            if abs(result.residual) <= residual_tolerance or abs(high.value - low.value) <= x_tolerance:
                break
            if low.residual * result.residual <= 0.0:
                high = result
            else:
                low = result
        else:
            raise FairParameterSolveError("non_converged", "有界单调反解超过登记最大迭代次数", details={"points": self._point_dicts(), "slope": slope})
        if abs(result.residual) > residual_tolerance:
            raise FairParameterSolveError("non_converged", "有界单调反解最终报价残差未达登记阈值", details={"points": self._point_dicts(), "slope": slope})
        self._validate_monotonicity()
        local_slope = self._local_slope(result.value, low, high)
        if local_slope is not None and abs(local_slope) <= self._slope_floor():
            raise FairParameterSolveError("zero_slope", "目标报价方程局部斜率为0或接近0", details={"slope": local_slope, "points": self._point_dicts()})
        return FairSolveResult("converged", True, result.value, result.residual, iterations, local_slope or slope, tuple(self._points), low.value, high.value)

    def _slope_between(self, low: SolvePoint, high: SolvePoint) -> float | None:
        if low.value == high.value:
            return None
        slope = (high.residual - low.residual) / (high.value - low.value)
        return slope if math.isfinite(slope) else None

    def _validate_monotonicity(self) -> None:
        points = sorted({point.value: point for point in self._points}.values(), key=lambda point: point.value)
        if len(points) < 3:
            return
        tolerance = self._residual_tolerance()
        direction: int | None = self._direction_sign()
        for left, right in zip(points, points[1:]):
            difference = right.residual - left.residual
            if abs(difference) <= tolerance:
                continue
            sign = 1 if difference > 0.0 else -1
            if sign != direction:
                raise FairParameterSolveError(
                    "no_unique_root",
                    "运行时报价方程未保持登记的严格单调关系",
                    details={"points": self._point_dicts()},
                )

    def _local_slope(self, value: float, low: SolvePoint, high: SolvePoint) -> float | None:
        candidates = [point for point in self._points if point.value != value]
        if not candidates:
            return None
        nearest = min(candidates, key=lambda point: abs(point.value - value))
        difference = value - nearest.value
        if difference == 0.0:
            return None
        return (self._point_residual(value) - nearest.residual) / difference

    def _point_residual(self, value: float) -> float:
        for point in reversed(self._points):
            if point.value == value:
                return point.residual
        return 0.0

    def _inside(self, value: float, lower: float | None, upper: float | None) -> bool:
        if lower is not None and self._domain_lower_strict and value <= lower:
            return False
        if upper is not None and self._domain_upper_strict and value >= upper:
            return False
        return (lower is None or value >= lower) and (upper is None or value <= upper)

    def _rule(self) -> Mapping[str, Any]:
        value = _field(self.target, "convergence_rule", {})
        return value if isinstance(value, Mapping) else {}

    def _direction_sign(self) -> int:
        raw = _field(self.target, "monotonic_direction", None)
        if raw is None:
            policy = _field(self.target, "search_bracket_policy", {})
            if isinstance(policy, Mapping):
                raw = policy.get("monotonic_direction")
        direction = str(raw or "").strip().lower()
        if direction == "increasing":
            return 1
        if direction == "decreasing":
            return -1
        raise FairParameterSolveError("ineligible", "目标缺少受控monotonic_direction")

    def _precision_rule(self) -> Mapping[str, Any]:
        value = _field(self.target, "precision_rule", {})
        return value if isinstance(value, Mapping) else {}

    def _residual_tolerance(self) -> float:
        rule = self._rule()
        value = _number_from_sections(
            rule,
            ("residual", "residual_rule", "value"),
            ("residual_tolerance", "residual_abs_tolerance", "quote_residual_tolerance", "tolerance", "absolute_tolerance"),
        )
        if value is None or value < 0.0:
            raise FairParameterSolveError("ineligible", "convergence_rule缺少有效报价残差容差")
        return value

    def _x_tolerance(self) -> float:
        rule = self._rule()
        value = _number_from_sections(
            rule,
            ("parameter", "parameter_rule", "solver", "x"),
            ("parameter_tolerance", "absolute_tolerance", "x_tolerance", "solver_tolerance"),
        )
        if value is None or value <= 0.0:
            raise FairParameterSolveError("ineligible", "convergence_rule缺少有效参数容差")
        return value

    def _slope_floor(self) -> float:
        sources = (
            (self._rule(), "convergence_rule"),
            (self._precision_rule(), "precision_rule"),
            (_field(self.target, "runtime_identifiability_rule", {}), "runtime_identifiability_rule"),
        )
        for source, label in sources:
            if not isinstance(source, Mapping):
                continue
            value = _number_from_sections(
                source,
                ("slope", "identifiability", "monotonicity"),
                ("slope_absolute_min", "slope_min", "minimum_absolute_slope", "slope_absolute_lower_bound", "slope_lower_bound"),
            )
            if value is not None:
                if value < 0.0:
                    raise FairParameterSolveError("ineligible", f"{label}斜率下界不得为负")
                return value
        raise FairParameterSolveError("ineligible", "目标规范缺少受控斜率下界")

    def _integer_rule(self, *names: str, default: int | None = None) -> int:
        rule = self._rule()
        value = _number_from_sections(rule, ("iterations", "iteration_rule", "solver"), names)
        if value is not None:
            if value <= 0 or not value.is_integer():
                raise FairParameterSolveError("ineligible", "convergence_rule最大迭代次数必须为正整数")
            return int(value)
        if default is None:
            raise FairParameterSolveError("ineligible", "convergence_rule缺少最大迭代次数")
        return default

    def _point_dicts(self) -> list[dict[str, Any]]:
        return [
            {"value": point.value, "quote_value": point.quote_value, "residual": point.residual, "standard_error": point.standard_error}
            for point in self._points
        ]


def deterministic_uncertainty(
    result: FairSolveResult,
    target: Any,
    *,
    bound_evidence: AnalyticalBoundEvidence | Mapping[str, Any] | None = None,
    current_identity: Mapping[str, Any] | None = None,
    fair_run_id: str | None = None,
    product_id: str | None = None,
    rule_revision: int | None = None,
    target_id: str | None = None,
    solve_input: Mapping[str, Any] | None = None,
    model_id: str | None = None,
    model_version: str | None = None,
    certified_interval: Mapping[str, Any] | Sequence[float] | None = None,
    solve_run_id: str | None = None,
    # These legacy keyword names remain accepted for a controlled mismatch
    # response, but they are never a substitute for current_identity and are
    # never used as certification evidence.
    parameter_representation_error: float | None = None,
    valuation_error_upper_bound: float | None = None,
    residual_error_upper_bound: float | None = None,
    slope_absolute_lower_bound: float | None = None,
    parameter_reference_price_basis: float | None = None,
    parameter_public_encoder: Callable[[float], float] | None = None,
) -> dict[str, Any]:
    """Build an Analytical uncertainty contract from structured proof data.

    The target directory can describe what proof is required, but it is not
    itself proof.  In particular, no sampled ``result.slope`` and no bare
    float keyword can promote this result to ``estimated``.
    """
    del parameter_representation_error, valuation_error_upper_bound
    del residual_error_upper_bound, slope_absolute_lower_bound

    def insufficient(reason: str, evidence: AnalyticalBoundEvidence | None = None) -> dict[str, Any]:
        payload = {
            "method": "deterministic_bound",
            "status": "insufficient",
            "precision_status": "insufficient_evidence",
            "quote_eligible": False,
            "quote_delivery_status": "research_only",
            "formal_quote_status": "research_only",
            "precision_rule": {"formal_quote_status": "research_only"},
            "value_encoding": "decimal_ratio",
            "certified_interval": None,
            "valuation_error_upper_bound": None,
            "residual_upper_bound": None,
            "slope_absolute_lower_bound": None,
            "parameter_representation_error_upper_bound": None,
            "absolute_error_upper_bound": None,
            "bound_sources": {},
            "bound_source_details": {},
            "bound_evidence": None,
            "reason": reason,
        }
        if evidence is not None:
            payload.update({
                "valuation_error_upper_bound": evidence.valuation_error_upper_bound,
                "residual_upper_bound": evidence.residual_upper_bound,
                "slope_absolute_lower_bound": evidence.slope_absolute_lower_bound,
                "parameter_representation_error_upper_bound": evidence.parameter_representation_error_upper_bound,
                "bound_sources": dict(evidence.bound_sources),
                "bound_source_details": dict(evidence.bound_sources),
                "bound_evidence": evidence.to_private_dict(),
            })
        return payload

    try:
        evidence = AnalyticalBoundEvidence.from_mapping(bound_evidence)
    except FairParameterSolveError as error:
        return insufficient(str(error))

    # Every identity below is the current execution context, not a value
    # copied from the proof.  Falling back to target/evidence fields would let
    # a caller self-authenticate an otherwise valid-looking bound.
    if current_identity is None:
        return insufficient("当前Analytical运行缺少结构化身份", evidence)
    if not isinstance(current_identity, Mapping):
        return insufficient("当前Analytical运行身份必须为结构化映射", evidence)
    identity = dict(current_identity)

    explicit_identity = {
        "fair_run_id": fair_run_id,
        "solve_run_id": solve_run_id,
        "product_id": product_id,
        "rule_revision": rule_revision,
        "target_id": target_id,
        "solve_input": None if solve_input is None else dict(solve_input),
        "model_id": model_id,
        "model_version": model_version,
    }
    for name, explicit in explicit_identity.items():
        if explicit is not None:
            current = identity.get(name)
            if current is None:
                return insufficient(f"当前Analytical运行身份缺少{name}", evidence)
            if current != explicit:
                return insufficient(f"显式Analytical运行身份{name}与当前身份不一致", evidence)

    expected_values = {
        "fair_run_id": identity.get("fair_run_id"),
        "solve_run_id": identity.get("solve_run_id"),
        "product_id": identity.get("product_id"),
        "rule_revision": identity.get("rule_revision"),
        "target_id": identity.get("target_id"),
        "solve_input": identity.get("solve_input"),
        "model_id": identity.get("model_id"),
        "model_version": identity.get("model_version"),
    }
    for name in ("fair_run_id", "product_id", "target_id", "model_id", "model_version"):
        expected = expected_values[name]
        if not isinstance(expected, str) or not expected.strip():
            return insufficient(f"当前Analytical运行身份缺少{name}", evidence)
    if (
        isinstance(expected_values["rule_revision"], bool)
        or not isinstance(expected_values["rule_revision"], int)
        or expected_values["rule_revision"] <= 0
    ):
        return insufficient("当前Analytical运行身份缺少有效rule_revision", evidence)
    if not isinstance(expected_values["solve_input"], Mapping):
        return insufficient("当前Analytical运行身份缺少完整solve_input", evidence)
    if expected_values["solve_run_id"] is not None and (
        not isinstance(expected_values["solve_run_id"], str)
        or not expected_values["solve_run_id"].strip()
    ):
        return insufficient("当前Analytical运行身份缺少solve_run_id", evidence)
    for name, expected in expected_values.items():
        if expected is not None and getattr(evidence, name) != expected:
            return insufficient(f"Analytical证据{name}与当前运行身份不一致", evidence)

    declared_target_id = _field(target, "target_id", None)
    if declared_target_id != expected_values["target_id"]:
        return insufficient("当前目标target_id与运行身份不一致", evidence)
    try:
        current_interval = _interval_pair(
            certified_interval if certified_interval is not None else identity.get("certified_interval"),
            "当前Analytical认证区间",
        )
    except FairParameterSolveError as error:
        return insufficient(str(error), evidence)
    if current_interval != evidence.certified_interval:
        return insufficient("Analytical证据认证区间与当前运行区间不一致", evidence)
    if not result.converged or result.solution is None:
        return insufficient("求解未收敛，不能生成Analytical认证区间", evidence)

    domain = _field(target, "domain", {})
    if not isinstance(domain, Mapping):
        return insufficient("目标缺少可核对的合法域", evidence)
    legal_lower = None
    legal_upper = None
    lower_strict = upper_strict = False
    if domain.get("min") is not None:
        legal_lower = _finite(domain["min"], "目标合法域下限")
    elif domain.get("inclusive_min") is not None:
        legal_lower = _finite(domain["inclusive_min"], "目标合法域下限")
    elif domain.get("exclusive_min") is not None:
        legal_lower = _finite(domain["exclusive_min"], "目标合法域下限")
        lower_strict = True
    if domain.get("max") is not None:
        legal_upper = _finite(domain["max"], "目标合法域上限")
    elif domain.get("inclusive_max") is not None:
        legal_upper = _finite(domain["inclusive_max"], "目标合法域上限")
    elif domain.get("exclusive_max") is not None:
        legal_upper = _finite(domain["exclusive_max"], "目标合法域上限")
        upper_strict = True
    cert_lower, cert_upper = evidence.certified_interval
    solution = float(result.solution)
    if not cert_lower <= solution <= cert_upper:
        return insufficient("求解值不在Analytical认证区间内", evidence)
    if result.lower is not None and cert_lower < float(result.lower):
        return insufficient("Analytical认证区间超出实际求解下界", evidence)
    if result.upper is not None and cert_upper > float(result.upper):
        return insufficient("Analytical认证区间超出实际求解上界", evidence)
    if legal_lower is not None and (cert_lower < legal_lower or (lower_strict and cert_lower <= legal_lower)):
        return insufficient("Analytical认证区间超出目标合法下界", evidence)
    if legal_upper is not None and (cert_upper > legal_upper or (upper_strict and cert_upper >= legal_upper)):
        return insufficient("Analytical认证区间超出目标合法上界", evidence)

    absolute_error = (
        evidence.valuation_error_upper_bound + evidence.residual_upper_bound
    ) / evidence.slope_absolute_lower_bound + evidence.parameter_representation_error_upper_bound
    internal_lower = solution - absolute_error
    internal_upper = solution + absolute_error
    if internal_lower < cert_lower or internal_upper > cert_upper:
        return insufficient("认证误差区间不能完全落在已证明certified_interval内", evidence)
    if legal_lower is not None and (internal_lower < legal_lower or (lower_strict and internal_lower <= legal_lower)):
        return insufficient("认证误差区间超出目标合法下界", evidence)
    if legal_upper is not None and (internal_upper > legal_upper or (upper_strict and internal_upper >= legal_upper)):
        return insufficient("认证误差区间超出目标合法上界", evidence)
    try:
        encode_parameter = _public_parameter_encoder(
            target,
            reference_price_basis=parameter_reference_price_basis,
            override=parameter_public_encoder,
        )
        lower = encode_parameter(internal_lower)
        upper = encode_parameter(internal_upper)
        public_solution = encode_parameter(solution)
        proof_lower = encode_parameter(cert_lower)
        proof_upper = encode_parameter(cert_upper)
    except FairParameterSolveError as error:
        return insufficient(str(error), evidence)
    if lower > upper or proof_lower > proof_upper:
        return insufficient("目标公开参数转换未保持区间顺序", evidence)
    public_absolute_error = max(abs(float(lower) - public_solution), abs(float(upper) - public_solution))
    maximum_error, threshold_source = _maximum_parameter_absolute_error(target, "analytical")
    parameter_error_gate = (
        "passed"
        if maximum_error is not None and public_absolute_error <= maximum_error
        else "failed"
    )
    formal_quote_status = (
        "eligible_when_precision_passes" if maximum_error is not None else "research_only"
    )
    # Delivery status describes the target's registered gate, not this run's
    # measured result.  The latter is represented by parameter_error_gate.
    quote_delivery_status = formal_quote_status
    return {
        "method": "deterministic_bound",
        "status": "estimated",
        "precision_status": "estimated",
        # This is only the parameter-uncertainty leg.  The formal service must
        # still combine it with final valuation and residual qualification.
        "quote_eligible": parameter_error_gate == "passed",
        "quote_delivery_status": quote_delivery_status,
        "formal_quote_status": formal_quote_status,
        "formal_quote_reason": None if maximum_error is not None else threshold_source,
        "precision_rule": {
            "formal_quote_status": formal_quote_status,
            "maximum_parameter_absolute_error": maximum_error,
            "maximum_parameter_absolute_error_source": threshold_source,
            "formal_quote_reason": None if maximum_error is not None else threshold_source,
        },
        "value_encoding": "decimal_ratio",
        "certified_interval": {"lower": lower, "upper": upper},
        "certified_proof_interval": {"lower": proof_lower, "upper": proof_upper},
        "valuation_error_upper_bound": evidence.valuation_error_upper_bound,
        "residual_upper_bound": evidence.residual_upper_bound,
        "slope_absolute_lower_bound": evidence.slope_absolute_lower_bound,
        "parameter_representation_error_upper_bound": evidence.parameter_representation_error_upper_bound,
        "absolute_error_upper_bound": public_absolute_error,
        "maximum_parameter_absolute_error": maximum_error,
        "parameter_error_gate": parameter_error_gate,
        "threshold_source": threshold_source,
        "bound_sources": dict(evidence.bound_sources),
        "bound_source_details": dict(evidence.bound_sources),
        "bound_evidence": evidence.to_private_dict(),
        "reason": None,
        "quote_reason": None if maximum_error is not None else threshold_source,
    }


def monte_carlo_uncertainty(
    result: FairSolveResult,
    batch_results: Sequence[MonteCarloBatchEvidence],
    target: Any,
    *,
    path_count: int,
    include_result_batch: bool = False,
    current_identity: Mapping[str, Any] | None = None,
    fair_run_id: str | None = None,
    solve_run_id: str | None = None,
    product_id: str | None = None,
    rule_revision: int | None = None,
    target_id: str | None = None,
    solve_input: Mapping[str, Any] | None = None,
    model_id: str | None = None,
    model_version: str | None = None,
    path_group_count: int | None = None,
    parameter_reference_price_basis: float | None = None,
    parameter_public_encoder: Callable[[float], float] | None = None,
) -> dict[str, Any]:
    """Map strict paired-independent MC evidence to a parameter envelope.

    A list of solve results alone carries no evidence that the batches were
    independently generated.  Every item must therefore bind its identity,
    derived seed, random-matrix fingerprint, product rule, complete solve input
    and path count to exactly one scalar solve.
    """
    precision = _field(target, "precision_rule", {})
    precision = dict(precision) if isinstance(precision, Mapping) else {}
    declared = _first_mapping(precision, "monte_carlo", "mc", "stochastic") or precision
    maximum_error, maximum_error_source = _maximum_parameter_absolute_error(target, "monte_carlo")
    if isinstance(path_count, bool) or not isinstance(path_count, int) or path_count <= 0:
        raise FairParameterSolveError("ineligible", "path_count必须为正整数")
    if path_group_count is not None:
        # No product-level grouping proof is currently available.  An
        # integer supplied by a caller is not validation evidence.
        raise FairParameterSolveError("ineligible", "当前没有产品级path_group_count验证证据")

    declared_min = _first_number(
        declared,
        "minimum_independent_batches", "independent_batch_count", "required_independent_batches",
    )
    if declared_min is None:
        raise FairParameterSolveError("ineligible", "precision_rule缺少独立MC批次要求")
    if declared_min <= 0.0 or not declared_min.is_integer():
        raise FairParameterSolveError("ineligible", "独立MC批次要求必须为正整数")
    minimum_batches = int(declared_min)
    confidence = _first_number(declared, "confidence_level", "confidence")
    if confidence is None or not 0.0 < confidence < 1.0:
        raise FairParameterSolveError("ineligible", "precision_rule缺少有效置信水平")
    slope_tolerance = _first_number(
        declared,
        "slope_stability_relative_tolerance", "slope_relative_tolerance", "relative_slope_tolerance",
    )
    if slope_tolerance is None or slope_tolerance < 0.0:
        raise FairParameterSolveError("ineligible", "precision_rule缺少有效斜率稳定性阈值")

    if not isinstance(batch_results, Sequence) or isinstance(batch_results, (str, bytes)):
        raise FairParameterSolveError("ineligible", "MC批次必须为严格MonteCarloBatchEvidence序列")
    batches: list[MonteCarloBatchEvidence] = []
    if current_identity is not None and not isinstance(current_identity, Mapping):
        raise FairParameterSolveError("ineligible", "当前MC运行身份必须为结构化映射")
    identity = dict(current_identity or {})

    def current_text_value(name: str, explicit: Any) -> str | None:
        if explicit is not None and identity.get(name) is not None and identity.get(name) != explicit:
            raise FairParameterSolveError("ineligible", f"显式MC运行身份{name}与当前身份不一致")
        value = explicit if explicit is not None else identity.get(name)
        if value is None:
            return None
        return _text_identity(value, f"当前MC运行身份{name}")

    expected_fair_run_id = current_text_value("fair_run_id", fair_run_id)
    if expected_fair_run_id is None:
        raise FairParameterSolveError("ineligible", "MC当前运行缺少fair_run_id")
    expected_run_id = current_text_value("solve_run_id", solve_run_id)
    expected_product_id = current_text_value("product_id", product_id)
    expected_target_id = current_text_value("target_id", target_id)
    expected_model = current_text_value("model_id", model_id)
    expected_version = current_text_value("model_version", model_version)
    if expected_product_id is None or expected_target_id is None:
        raise FairParameterSolveError("ineligible", "MC当前运行缺少product_id或target_id")
    expected_rule_revision = rule_revision if rule_revision is not None else identity.get("rule_revision")
    if (
        rule_revision is not None
        and identity.get("rule_revision") is not None
        and identity.get("rule_revision") != rule_revision
    ):
        raise FairParameterSolveError("ineligible", "显式MC运行身份rule_revision与当前身份不一致")
    if (
        isinstance(expected_rule_revision, bool)
        or not isinstance(expected_rule_revision, int)
        or expected_rule_revision <= 0
    ):
        raise FairParameterSolveError("ineligible", "MC当前运行缺少有效rule_revision")
    expected_solve_input = dict(solve_input) if solve_input is not None else identity.get("solve_input")
    if (
        solve_input is not None
        and identity.get("solve_input") is not None
        and identity.get("solve_input") != dict(solve_input)
    ):
        raise FairParameterSolveError("ineligible", "显式MC运行身份solve_input与当前身份不一致")
    if not isinstance(expected_solve_input, Mapping):
        raise FairParameterSolveError("ineligible", "MC当前运行缺少完整solve_input")
    declared_target_id = _field(target, "target_id", None)
    if declared_target_id != expected_target_id:
        raise FairParameterSolveError("ineligible", "MC当前目标target_id与运行身份不一致")

    def unavailable_evidence(reason: str) -> dict[str, Any]:
        """Return a restricted result when the formal Service is unavailable."""
        return {
            "method": "paired_independent_batches",
            "status": "insufficient",
            "precision_status": "insufficient_evidence",
            "quote_eligible": False,
            "quote_delivery_status": "research_only",
            "formal_quote_status": "research_only",
            "formal_quote_reason": maximum_error_source,
            "precision_rule": {
                "formal_quote_status": "research_only",
                "maximum_parameter_absolute_error": None,
                "maximum_parameter_absolute_error_source": maximum_error_source,
                "formal_quote_reason": maximum_error_source,
            },
            "value_encoding": "decimal_ratio",
            "independent_batch_count": 0,
            "fair_run_id": expected_fair_run_id,
            "path_group_count": None,
            "primary_batch_included": False,
            "path_count": path_count,
            "random_paths_generated": 0,
            "slope": None,
            "slope_absolute_lower_bound": None,
            "slope_stability_status": "insufficient",
            "threshold_source": declared.get("threshold_sources", declared.get("bound_sources")),
            "maximum_parameter_absolute_error": None,
            "maximum_parameter_absolute_error_source": maximum_error_source,
            "parameter_error_gate": "failed",
            "confidence_level": confidence,
            "lower": None,
            "upper": None,
            "absolute_error_upper_bound": None,
            "reason": reason,
            "quote_reason": reason,
        }

    seen_ids: set[str] = set()
    seen_solve_run_ids: set[str] = set()
    seen_seeds: set[str] = set()
    seen_fingerprints: set[str] = set()
    seen_result_objects: set[int] = set()
    batch_context: tuple[Any, ...] | None = None
    for raw_batch in batch_results:
        try:
            batch = MonteCarloBatchEvidence.from_mapping(raw_batch)
        except FairParameterSolveError as error:
            if error.code == "evidence_unavailable":
                return unavailable_evidence(f"MC正式证据不可用：{error}")
            raise
        if batch.fair_run_id != expected_fair_run_id:
            raise FairParameterSolveError("ineligible", "MC批次fair_run_id与当前公平参数运行不一致")
        if batch.batch_id in seen_ids:
            raise FairParameterSolveError("ineligible", "MC批次batch_id重复")
        if batch.solve_run_id in seen_solve_run_ids:
            raise FairParameterSolveError("ineligible", "MC批次solve_run_id重复")
        seed_key = str(batch.derived_seed)
        if seed_key in seen_seeds:
            raise FairParameterSolveError("ineligible", "MC批次derived_seed重复")
        if batch.random_matrix_fingerprint in seen_fingerprints:
            raise FairParameterSolveError("ineligible", "MC批次random_matrix_fingerprint重复")
        if (
            batch.product_id != expected_product_id
            or batch.rule_revision != expected_rule_revision
            or batch.target_id != expected_target_id
            or dict(batch.solve_input) != dict(expected_solve_input)
        ):
            raise FairParameterSolveError("ineligible", "MC批次产品规则或求解输入与当前运行不一致")
        if batch.path_count != path_count:
            raise FairParameterSolveError("ineligible", "MC批次path_count与请求不一致")
        context = (
            batch.fair_run_id,
            batch.product_id,
            batch.rule_revision,
            batch.target_id,
            dict(batch.solve_input),
            batch.model_id,
            batch.model_version,
        )
        if batch_context is None:
            batch_context = context
        elif context != batch_context:
            raise FairParameterSolveError("ineligible", "MC批次运行或模型身份不一致")
        if expected_model is not None and batch.model_id != expected_model:
            raise FairParameterSolveError("ineligible", "MC批次model_id与当前运行不一致")
        if expected_version is not None and batch.model_version != expected_version:
            raise FairParameterSolveError("ineligible", "MC批次model_version与当前运行不一致")
        result_object_id = id(batch.solve_result)
        if result_object_id in seen_result_objects:
            raise FairParameterSolveError("ineligible", "MC批次求解结果对象重复")
        seen_ids.add(batch.batch_id)
        seen_solve_run_ids.add(batch.solve_run_id)
        seen_seeds.add(seed_key)
        seen_fingerprints.add(batch.random_matrix_fingerprint)
        seen_result_objects.add(result_object_id)
        batches.append(batch)
    # ``solve_run_id`` identifies one independent batch, not the parent fair
    # operation.  Retain the legacy argument only for a one-batch call; never
    # require several independent batches to share it.
    if expected_run_id is not None and len(batches) == 1 and batches[0].solve_run_id != expected_run_id:
        raise FairParameterSolveError("ineligible", "MC批次solve_run_id与当前运行不一致")
    # Identity, rather than dataclass equality, determines whether the main
    # solve is already one of the independent batches.  Two independent
    # batches can legitimately produce identical numeric roots.
    primary_batch_included = any(item.solve_result is result for item in batches)
    if include_result_batch and not primary_batch_included:
        raise FairParameterSolveError("ineligible", "主求解要求计入MC批次时必须绑定到批次证据对象")
    batch_count = len(batches)
    values = [
        item.solve_result.solution
        for item in batches
        if item.solve_result.converged and item.solve_result.solution is not None
    ]
    slopes = [
        abs(float(item.solve_result.slope))
        for item in batches
        if item.solve_result.converged
        and item.solve_result.slope is not None
        and math.isfinite(float(item.solve_result.slope))
    ]
    slope_stability = "stable"
    if len(slopes) < minimum_batches or not slopes:
        slope_stability = "insufficient"
    elif max(slopes) <= 0.0:
        slope_stability = "zero"
    elif (max(slopes) - min(slopes)) / max(slopes) > slope_tolerance:
        slope_stability = "unstable"
    reason: str | None = None
    lower = upper = absolute_error = None
    status = "estimated"
    if (
        batch_count < minimum_batches
        or len(values) < minimum_batches
        or slope_stability != "stable"
        or not result.converged
        or result.solution is None
    ):
        status = "insufficient"
        reason = "独立MC批次不足、求解未完成或局部斜率不稳定"
    else:
        mean = sum(float(value) for value in values) / len(values)
        if len(values) > 1:
            variance = sum((float(value) - mean) ** 2 for value in values) / (len(values) - 1)
            standard_error = math.sqrt(max(0.0, variance) / len(values))
        else:
            standard_error = 0.0
        try:
            from scipy.stats import t as student_t

            quantile = float(student_t.ppf((1.0 + confidence) / 2.0, len(values) - 1))
        except (ImportError, ModuleNotFoundError, ValueError, TypeError) as error:
            status = "insufficient"
            reason = f"无法取得Student-t有限样本分位数：{error}"
            quantile = float("nan")
        if not math.isfinite(quantile):
            status = "insufficient"
            if reason is None:
                reason = "Student-t有限样本分位数不是有限值"
            half_width = None
        else:
            half_width = quantile * standard_error
        if half_width is None:
            lower = upper = absolute_error = None
        else:
            lower, upper = mean - half_width, mean + half_width
        if status == "estimated":
            try:
                encode_parameter = _public_parameter_encoder(
                    target,
                    reference_price_basis=parameter_reference_price_basis,
                    override=parameter_public_encoder,
                )
                public_mean = encode_parameter(mean)
                public_lower = encode_parameter(lower)
                public_upper = encode_parameter(upper)
                lower, upper = public_lower, public_upper
                absolute_error = max(abs(public_lower - public_mean), abs(public_upper - public_mean))
            except FairParameterSolveError as error:
                status = "insufficient"
                reason = str(error)
                lower = upper = absolute_error = None
    method = "paired_independent_batches"
    random_paths_generated = path_count * batch_count
    parameter_error_gate = (
        "passed"
        if status == "estimated" and maximum_error is not None and absolute_error is not None
        and absolute_error <= maximum_error
        else "failed"
    )
    quote_reason = None
    if parameter_error_gate != "passed":
        if maximum_error is None:
            quote_reason = maximum_error_source
        elif absolute_error is None:
            quote_reason = "未形成可比的参数绝对误差上界"
        else:
            quote_reason = "absolute_error_upper_bound超过maximum_parameter_absolute_error"
    return {
        "method": method,
        "status": status,
        "precision_status": "estimated" if status == "estimated" else "insufficient_evidence",
        # This is only the parameter-uncertainty leg.  The formal service must
        # still combine it with final valuation and residual qualification.
        "quote_eligible": parameter_error_gate == "passed",
        "quote_delivery_status": (
            "eligible_when_precision_passes"
            if maximum_error is not None
            else "research_only"
        ),
        "formal_quote_status": (
            "eligible_when_precision_passes" if maximum_error is not None else "research_only"
        ),
        "formal_quote_reason": None if maximum_error is not None else maximum_error_source,
        "precision_rule": {
            "formal_quote_status": (
                "eligible_when_precision_passes" if maximum_error is not None else "research_only"
            ),
            "maximum_parameter_absolute_error": maximum_error,
            "maximum_parameter_absolute_error_source": maximum_error_source,
            "formal_quote_reason": None if maximum_error is not None else maximum_error_source,
        },
        "value_encoding": "decimal_ratio",
        "independent_batch_count": batch_count,
        "fair_run_id": expected_fair_run_id,
        "path_group_count": None,
        "primary_batch_included": primary_batch_included,
        "path_count": path_count,
        "random_paths_generated": random_paths_generated,
        "slope": None if result.slope is None else float(result.slope),
        "slope_absolute_lower_bound": None if not slopes else min(slopes),
        "slope_stability_status": slope_stability,
        "threshold_source": declared.get("threshold_sources", declared.get("bound_sources")),
        "maximum_parameter_absolute_error": maximum_error,
        "maximum_parameter_absolute_error_source": maximum_error_source,
        "parameter_error_gate": parameter_error_gate,
        "confidence_level": confidence,
        "lower": lower,
        "upper": upper,
        "absolute_error_upper_bound": absolute_error,
        "reason": reason,
        "quote_reason": quote_reason,
    }


__all__ = (
    "FairParameterSolveError",
    "FairParameterSolver",
    "AnalyticalBoundEvidence",
    "FairSolveResult",
    "MonteCarloBatchEvidence",
    "ScalarValuation",
    "SolvePoint",
    "deterministic_uncertainty",
    "monte_carlo_uncertainty",
)
