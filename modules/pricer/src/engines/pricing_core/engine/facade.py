"""衍生品定价的唯一用户入口。

外部调用固定为：选择结构、填写分组参数、明确方法，然后得到统一
``PricingResult``。本文件只负责严格校验和机械构造领域对象，不包含定价公式。
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from threading import RLock
from typing import Any, Mapping

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
_DERIVATIVES_IMPORT_LOCK = RLock()
_DERIVATIVES_ALIAS = "_pricer_engine_derivatives"


def _load_catalog():
    alias = "_pricer_engine_catalog"
    current = sys.modules.get(alias)
    if current is not None:
        return current
    path = ROOT / "engine" / "catalog.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载产品目录：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if sys.modules.get(alias) is module:
            sys.modules.pop(alias, None)
        raise
    return module


_CATALOG = _load_catalog()
_OUTPUT_MODES = ("TERMINAL_AND_JSON", "TERMINAL", "JSON", "NONE")


class RiskStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNVALIDATED = "UNVALIDATED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


@dataclass(frozen=True)
class RiskMetric:
    status: RiskStatus
    value: float | None
    unit: str | None
    bump: float | None
    difference: str | None
    time_basis: str | None = None
    bump_details: dict[str, Any] | None = None
    pv_amount_value: float | None = None
    pv_amount_unit: str | None = None
    pv_percent_value: float | None = None
    pv_percent_unit: str | None = None
    pv_points_100_value: float | None = None
    pv_points_100_unit: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, RiskStatus):
            object.__setattr__(self, "status", RiskStatus(self.status))


@dataclass(frozen=True)
class RunResult:
    pv_amount: float | None
    pv_percent: float | None
    pv_points_100: float | None
    currency: str | None
    greeks: dict[str, RiskMetric]
    extended_risks: dict[str, RiskMetric]
    method: str
    version: str
    warnings: tuple[str, ...]
    diagnostics: dict[str, Any]
    engine_raw: dict[str, Any]


@dataclass(frozen=True)
class PricingRun:
    run_id: str
    request_fingerprint: str
    family: str
    structure: str
    method: str
    route_id: str
    parameters: dict[str, Any]
    requested_parameters: dict[str, Any]
    effective_parameters: dict[str, Any]
    effective_config: dict[str, Any]
    random_source: dict[str, Any]
    result: RunResult
    json_output_path: str | None = None


@dataclass(frozen=True)
class SolveRunResult:
    value: float
    variable: str
    target_pv_points_100: float
    target_pv_absolute_tolerance: float
    converged: bool
    method: str
    version: str
    contract_patch: dict[str, float]
    warnings: tuple[str, ...]
    diagnostics: dict[str, Any]
    engine_raw: dict[str, Any]


@dataclass(frozen=True)
class SolveRun:
    run_id: str
    request_fingerprint: str
    family: str
    structure: str
    method: str
    route_id: str
    parameters: dict[str, Any]
    requested_parameters: dict[str, Any]
    effective_parameters: dict[str, Any]
    effective_config: dict[str, Any]
    random_source: dict[str, Any]
    requested_target: dict[str, Any]
    target: dict[str, Any]
    result: SolveRunResult
    json_output_path: str | None = None


def list_families() -> tuple[str, ...]:
    """返回稳定、精确且不做别名猜测的产品族标识。"""
    return _CATALOG.list_families()


def list_structures(family: str) -> tuple[str, ...]:
    """返回指定产品族内的精确结构标识。"""
    return _CATALOG.list_structures(family)


def describe_structure(family: str, structure: str) -> dict[str, Any]:
    """返回产品族和结构共同约束的完整目录说明。"""
    return _CATALOG.describe_structure(family, structure)


def price_option(
    family: str,
    structure: str,
    parameters: Mapping[str, Any],
    method: str,
    *,
    output: str = "TERMINAL_AND_JSON",
) -> PricingRun:
    """使用STANDARD引擎执行一次显式产品族、结构和方法定价。"""
    _CATALOG.require_family_structure(family, structure)
    if output not in _OUTPUT_MODES:
        raise ValueError(f"output必须为以下精确值之一：{', '.join(_OUTPUT_MODES)}")
    selected_route_id, route = _pricing_route(family, structure, method)
    if method != route["method"]:
        raise ValueError(
            f"{structure}不支持方法{method!r}；STANDARD路由方法：{route['method']}"
        )

    groups = _require_mapping("parameters", parameters)
    _reject_unknown("parameters", groups, {"contract", "market", "valuation_state", "config"})
    for required_group in ("contract", "market"):
        if required_group not in groups:
            raise ValueError(f"parameters缺少必填分组：{required_group}")
    specs = _parameter_spec_for_method(family, structure, method)
    contract = _validated_group(f"{structure}.contract", groups["contract"], specs["contract"])
    market_values = _validated_group("market", groups["market"], specs["market"])
    state_values = _validated_group(
        "valuation_state", groups.get("valuation_state", {}), specs["valuation_state"]
    )
    config_values = _validated_group("config", groups.get("config", {}), specs["config"])
    parameter_snapshot = _parameter_snapshot(groups)
    with _load_derivatives() as derivatives:
        instrument = _build_instrument(derivatives, structure, contract)
        market = _build_market(derivatives, market_values)
        state = derivatives.ValuationState(**state_values)
        config = _build_config(derivatives, method, config_values)
        pricing_result = derivatives.price(instrument, market, config, valuation_state=state)
        random_metadata = _random_metadata(config)
        effective_parameters = _effective_parameters(
            instrument,
            market,
            state,
            config,
            random_metadata,
            structure,
        )

    request = {
        "family": family,
        "structure": structure,
        "parameters": parameter_snapshot,
        "method": method,
        "route_id": selected_route_id,
    }
    request_fingerprint = hashlib.sha256(
        _canonical_json(request).encode("utf-8")
    ).hexdigest()
    run_id = _new_run_id(request_fingerprint)
    json_output_path = None
    if output in {"TERMINAL_AND_JSON", "JSON"}:
        json_output_path = str(_result_root() / run_id / "pricing_run.json")
    result = _normalize_result(pricing_result, family, structure)
    run = PricingRun(
        run_id=run_id,
        request_fingerprint=request_fingerprint,
        family=family,
        structure=structure,
        method=method,
        route_id=selected_route_id,
        parameters=deepcopy(parameter_snapshot),
        requested_parameters=parameter_snapshot,
        effective_parameters=effective_parameters,
        effective_config=deepcopy(effective_parameters["config"]),
        random_source=random_metadata,
        result=result,
        json_output_path=json_output_path,
    )
    if output in {"TERMINAL_AND_JSON", "JSON"}:
        _write_run_json(run)
    if output in {"TERMINAL_AND_JSON", "TERMINAL"}:
        _print_run(run)
    return run


def solve_option(
    family: str,
    structure: str,
    parameters: Mapping[str, Any],
    method: str,
    target: Mapping[str, Any],
    *,
    output: str = "TERMINAL_AND_JSON",
) -> SolveRun:
    """使用STANDARD引擎反解公平条款，目标PV统一使用每100点口径。"""
    _CATALOG.require_family_structure(family, structure)
    if output not in _OUTPUT_MODES:
        raise ValueError(f"output必须为以下精确值之一：{', '.join(_OUTPUT_MODES)}")
    route_id = _CATALOG.route_id(family, structure)
    route = _CATALOG.route_for(family, structure)
    if route.get("solve_handler") is None:
        raise NotImplementedError(f"{family}/{structure}尚不支持STANDARD反解")
    if method != route["method"]:
        raise ValueError(
            f"{structure}不支持反解方法{method!r}；STANDARD路由方法：{route['method']}"
        )

    target_values = dict(_require_mapping("target", target))
    target_fields = {
        "variable",
        "target_pv_points_100",
        "lower_bound",
        "upper_bound",
        "solver_absolute_tolerance",
        "target_pv_absolute_tolerance",
        "maximum_iterations",
    }
    _reject_unknown("target", target_values, target_fields)
    missing_target = [
        field for field in ("variable", "target_pv_points_100")
        if field not in target_values
    ]
    if missing_target:
        raise ValueError(f"target缺少必填字段：{', '.join(missing_target)}")
    allowed_targets = tuple(
        _CATALOG.describe_structure(family, structure)["solve_targets"]
    )
    if target_values["variable"] not in allowed_targets:
        allowed = ", ".join(allowed_targets) if allowed_targets else "无"
        raise ValueError(
            f"{family}/{structure}不支持反解变量{target_values['variable']!r}；"
            f"允许值：{allowed}"
        )

    groups = _require_mapping("parameters", parameters)
    _reject_unknown("parameters", groups, {"contract", "market", "valuation_state", "config"})
    for required_group in ("contract", "market"):
        if required_group not in groups:
            raise ValueError(f"parameters缺少必填分组：{required_group}")
    specs = _CATALOG.parameter_spec(family, structure)
    contract = _validated_group(f"{structure}.contract", groups["contract"], specs["contract"])
    market_values = _validated_group("market", groups["market"], specs["market"])
    state_values = _validated_group(
        "valuation_state", groups.get("valuation_state", {}), specs["valuation_state"]
    )
    config_values = _validated_group("config", groups.get("config", {}), specs["config"])
    parameter_snapshot = _parameter_snapshot(groups)
    target_snapshot = _safe_snapshot(target_values)
    with _load_derivatives() as derivatives:
        instrument = _build_instrument(derivatives, structure, contract)
        market = _build_market(derivatives, market_values)
        state = derivatives.ValuationState(**state_values)
        config = _build_config(derivatives, method, config_values)
        solve_target = derivatives.SolveTarget(
            variable=target_values["variable"],
            target_pv=target_values["target_pv_points_100"],
            lower_bound=target_values.get("lower_bound"),
            upper_bound=target_values.get("upper_bound"),
            solver_absolute_tolerance=target_values.get(
                "solver_absolute_tolerance", 1e-8
            ),
            target_pv_absolute_tolerance=target_values.get(
                "target_pv_absolute_tolerance", 1e-3
            ),
            maximum_iterations=target_values.get("maximum_iterations", 100),
        )
        solved = derivatives.solve(
            instrument, market, config, solve_target, valuation_state=state
        )
        random_metadata = _random_metadata(config)
        effective_parameters = _effective_parameters(
            instrument, market, state, config, random_metadata, structure
        )

    request = {
        "family": family,
        "structure": structure,
        "parameters": parameter_snapshot,
        "method": method,
        "route_id": route_id,
        "target": target_snapshot,
    }
    request_fingerprint = hashlib.sha256(
        _canonical_json(request).encode("utf-8")
    ).hexdigest()
    run_id = _new_run_id(request_fingerprint)
    json_output_path = None
    if output in {"TERMINAL_AND_JSON", "JSON"}:
        json_output_path = str(_solve_result_root() / run_id / "solve_run.json")
    result = SolveRunResult(
        value=solved.value,
        variable=solved.variable,
        target_pv_points_100=solved.target_pv,
        target_pv_absolute_tolerance=solve_target.target_pv_absolute_tolerance,
        converged=solved.converged,
        method=solved.method.name,
        version=solved.version,
        contract_patch=deepcopy(
            solved.diagnostics.get(
                "contract_patch", {solved.variable: solved.value}
            )
        ),
        warnings=tuple(solved.warnings),
        diagnostics=deepcopy(solved.diagnostics),
        engine_raw=deepcopy(solved.engine_raw),
    )
    run = SolveRun(
        run_id=run_id,
        request_fingerprint=request_fingerprint,
        family=family,
        structure=structure,
        method=method,
        route_id=route_id,
        parameters=deepcopy(parameter_snapshot),
        requested_parameters=parameter_snapshot,
        effective_parameters=effective_parameters,
        effective_config=deepcopy(effective_parameters["config"]),
        random_source=random_metadata,
        requested_target=target_snapshot,
        target={
            "variable": solve_target.variable,
            "target_pv_points_100": solve_target.target_pv,
            "lower_bound": solve_target.lower_bound,
            "upper_bound": solve_target.upper_bound,
            "solver_absolute_tolerance": solve_target.solver_absolute_tolerance,
            "target_pv_absolute_tolerance": solve_target.target_pv_absolute_tolerance,
            "maximum_iterations": solve_target.maximum_iterations,
        },
        result=result,
        json_output_path=json_output_path,
    )
    if output in {"TERMINAL_AND_JSON", "JSON"}:
        _write_solve_json(run)
    if output in {"TERMINAL_AND_JSON", "TERMINAL"}:
        _print_solve_run(run)
    return run


def _pricing_route(family: str, structure: str, method: str) -> tuple[str, dict[str, Any]]:
    """选择基座明确登记的方法；香草BS与冻结MC共享同一结构。"""
    if family == "VANILLA" and structure == "EUROPEAN_VANILLA" and method == "MONTE_CARLO_CPU":
        return "STANDARD_VANILLA_MONTE_CARLO", {
            "instrument_type": "EuropeanVanillaOption", "method": "MONTE_CARLO_CPU",
            "price_handler": "price_vanilla_monte_carlo", "solve_handler": None,
        }
    route_id = _CATALOG.route_id(family, structure)
    return route_id, dict(_CATALOG.ENGINE_ROUTES[route_id])


def _parameter_spec_for_method(family: str, structure: str, method: str) -> dict[str, Any]:
    spec = _CATALOG.parameter_spec(family, structure)
    if family == "VANILLA" and structure == "EUROPEAN_VANILLA" and method == "MONTE_CARLO_CPU":
        spec["config"] = {
            "required": (),
            "optional": ("paths", "seed", "threads", "random_source", "greek_bumps", "diagnostics"),
        }
    return spec


def _require_mapping(label: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label}必须为字典或Mapping")
    return value


def _reject_unknown(label: str, values: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ValueError(f"{label}包含未知字段：{', '.join(unknown)}")


def _validated_group(
    label: str,
    value: Any,
    spec: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    values = _require_mapping(label, value)
    required = set(spec["required"])
    allowed = required | set(spec["optional"])
    _reject_unknown(label, values, allowed)
    missing = [field for field in spec["required"] if field not in values]
    if missing:
        raise ValueError(f"{label}缺少必填字段：{', '.join(missing)}")
    return dict(values)


def _new_run_id(fingerprint: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}_{fingerprint[:12]}"


def _parameter_snapshot(groups: Mapping[str, Any]) -> dict[str, Any]:
    """Detach and recursively secure caller input before it enters PricingRun."""
    snapshot = _safe_snapshot(dict(groups))
    if not isinstance(snapshot, dict):  # pragma: no cover - defensive invariant
        raise TypeError("parameters安全快照必须为字典")
    return snapshot


def _domain_snapshot(value: Any) -> Any:
    info = getattr(value, "info", None)
    if info is not None:
        return _domain_snapshot(info)
    if isinstance(value, np.ndarray):
        return _array_metadata(value)
    if is_dataclass(value):
        return {
            field.name: _domain_snapshot(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, Mapping):
        return {str(key): _domain_snapshot(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_domain_snapshot(item) for item in value)
    if isinstance(value, list):
        return [_domain_snapshot(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return deepcopy(value)


def _effective_parameters(
    instrument: Any,
    market: Any,
    state: Any,
    config: Any,
    random_metadata: dict[str, Any],
    structure: str,
) -> dict[str, Any]:
    state_snapshot = _domain_snapshot(state)
    allowed_state = _CATALOG.parameter_spec(
        _CATALOG.family_for_structure(structure), structure
    )["valuation_state"]["optional"]
    state_snapshot = {
        name: state_snapshot[name]
        for name in allowed_state
    }
    config_snapshot = {
        "method": config.method.name,
        "greek_bumps": dict(config.greek_bumps),
        "diagnostics": _safe_snapshot(dict(config.diagnostics)),
    }
    if random_metadata["used"]:
        config_snapshot.update(
            {
                "paths": config.paths,
                "seed": config.seed,
                "threads": config.threads,
                "random_source": deepcopy(random_metadata),
            }
        )
    return {
        "contract": _domain_snapshot(instrument),
        "market": _domain_snapshot(market),
        "valuation_state": state_snapshot,
        "config": config_snapshot,
    }


def _result_root() -> Path:
    configured = os.environ.get("PRICER_ENGINE_RESULT_ROOT")
    return Path(configured).expanduser() if configured else ROOT / "result" / "pricing"


def _solve_result_root() -> Path:
    configured = os.environ.get("PRICER_ENGINE_RESULT_ROOT")
    return Path(configured).expanduser() if configured else ROOT / "result" / "solving"


def _random_metadata(config: Any) -> dict[str, Any]:
    if config.method.name != "MONTE_CARLO_CPU":
        return {"used": False}
    monte_carlo = config.monte_carlo
    info = monte_carlo.random_source.info
    return {
        "used": True,
        "seed": monte_carlo.seed,
        "paths": monte_carlo.paths,
        "threads": monte_carlo.threads,
        "shape": list(info.shape),
        "dtype": info.dtype,
        "sha256": info.sha256,
        "source_path": info.path,
    }


def _normalize_result(value: Any, family: str, structure: str) -> RunResult:
    risk_specs = _CATALOG.greek_spec(family, structure)
    greeks: dict[str, RiskMetric] = {}
    for display_name in _CATALOG.CORE_GREEKS:
        metadata = risk_specs["core"][display_name]
        status = RiskStatus(metadata["status"])
        raw = value.greeks.get(display_name.lower())
        if status is RiskStatus.AVAILABLE and raw is not None:
            greeks[display_name] = RiskMetric(
                status=RiskStatus.AVAILABLE,
                value=raw.value,
                unit=raw.unit,
                bump=raw.bump,
                difference=raw.difference,
                time_basis=raw.time_basis,
                bump_details=deepcopy(raw.bump_details),
                pv_amount_value=raw.pv_amount_value,
                pv_amount_unit=raw.pv_amount_unit,
                pv_percent_value=raw.pv_percent_value,
                pv_percent_unit=raw.pv_percent_unit,
                pv_points_100_value=raw.pv_points_100_value,
                pv_points_100_unit=raw.pv_points_100_unit,
            )
        else:
            greeks[display_name] = RiskMetric(
                status=(
                    status
                    if status is not RiskStatus.AVAILABLE
                    else RiskStatus.NOT_IMPLEMENTED
                ),
                value=None,
                unit=metadata["unit"],
                bump=metadata["bump"],
                difference=metadata["difference"],
                time_basis=metadata["time_basis"],
            )
    extended_risks: dict[str, RiskMetric] = {}
    for display_name, metadata in risk_specs["extended"].items():
        status = RiskStatus(metadata["status"])
        raw_key = display_name.casefold().replace(" ", "_")
        raw = value.extended_greeks.get(raw_key)
        if status is RiskStatus.AVAILABLE and raw is not None:
            extended_risks[display_name] = RiskMetric(
                status=RiskStatus.AVAILABLE,
                value=raw.value,
                unit=raw.unit,
                bump=raw.bump,
                difference=raw.difference,
                time_basis=raw.time_basis,
                bump_details=deepcopy(raw.bump_details),
                pv_amount_value=raw.pv_amount_value,
                pv_amount_unit=raw.pv_amount_unit,
                pv_percent_value=raw.pv_percent_value,
                pv_percent_unit=raw.pv_percent_unit,
                pv_points_100_value=raw.pv_points_100_value,
                pv_points_100_unit=raw.pv_points_100_unit,
            )
        else:
            extended_risks[display_name] = RiskMetric(
                status=(
                    status
                    if status is not RiskStatus.AVAILABLE
                    else RiskStatus.NOT_IMPLEMENTED
                ),
                value=None,
                unit=metadata["unit"],
                bump=metadata["bump"],
                difference=metadata["difference"],
                time_basis=metadata["time_basis"],
            )
    return RunResult(
        pv_amount=value.pv_amount,
        pv_percent=value.pv_percent,
        pv_points_100=value.pv_points_100,
        currency=value.currency,
        greeks=greeks,
        extended_risks=extended_risks,
        method=value.method.name,
        version=value.version,
        warnings=tuple(value.warnings),
        diagnostics=deepcopy(value.diagnostics),
        engine_raw=deepcopy(value.engine_raw),
    )


def _normalized_key(key: str | None) -> str:
    if key is None:
        return ""
    return "".join(
        character
        for character in key.casefold()
        if character not in {" ", "-", "_"}
    )


def _is_sensitive_key(key: str | None) -> bool:
    normalized = _normalized_key(key)
    return any(
        marker in normalized
        for marker in (
            "account",
            "username",
            "login",
            "password",
            "token",
            "secret",
            "apikey",
            "credential",
        )
    )


def _is_random_container_key(key: str | None) -> bool:
    normalized = _normalized_key(key)
    return any(
        marker in normalized
        for marker in ("matrix", "rng", "randomdraw", "randomvalue", "randomnumber")
    )


def _array_metadata(value: Any) -> dict[str, Any]:
    array = np.ascontiguousarray(np.asarray(value))
    return {
        "shape": [int(size) for size in array.shape],
        "dtype": str(array.dtype),
        "sha256": hashlib.sha256(array.tobytes(order="C")).hexdigest(),
    }


def _numeric_random_container(
    value: Any,
    random_context: bool,
) -> dict[str, Any] | None:
    if not random_context:
        return None
    if isinstance(value, Mapping) or isinstance(value, (str, bytes)):
        return None
    try:
        array = np.asarray(value)
    except (TypeError, ValueError):
        return None
    if array.dtype.kind not in "biufc" or array.ndim == 0:
        return None
    return _array_metadata(array)


def _safe_snapshot(
    value: Any,
    key: str | None = None,
    random_context: bool = False,
) -> Any:
    if _is_sensitive_key(key):
        return "[REDACTED]"
    current_random_context = random_context or _is_random_container_key(key)
    if isinstance(value, np.ndarray):
        return _array_metadata(value)
    random_container = _numeric_random_container(value, current_random_context)
    if random_container is not None:
        return random_container
    info = getattr(value, "info", None)
    if info is not None:
        return _safe_snapshot(info, key, current_random_context)
    if is_dataclass(value):
        return {
            field.name: _safe_snapshot(
                getattr(value, field.name),
                field.name,
                current_random_context,
            )
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, Mapping):
        return {
            str(item_key): _safe_snapshot(
                item_value,
                str(item_key),
                current_random_context,
            )
            for item_key, item_value in value.items()
            if not _is_sensitive_key(str(item_key))
        }
    if isinstance(value, tuple):
        return tuple(
            _safe_snapshot(item, None, current_random_context) for item in value
        )
    if isinstance(value, list):
        return [_safe_snapshot(item, None, current_random_context) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return deepcopy(value)


def _json_value(
    value: Any,
    key: str | None = None,
    random_context: bool = False,
) -> Any:
    if _is_sensitive_key(key):
        return "[REDACTED]"
    current_random_context = random_context or _is_random_container_key(key)
    if isinstance(value, np.ndarray):
        return _array_metadata(value)
    random_container = _numeric_random_container(value, current_random_context)
    if random_container is not None:
        return random_container
    info = getattr(value, "info", None)
    if info is not None:
        return _json_value(info, key, current_random_context)
    if is_dataclass(value):
        return {
            field.name: _json_value(
                getattr(value, field.name), field.name, current_random_context
            )
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, Mapping):
        return {
            str(item_key): _json_value(
                item_value,
                str(item_key),
                current_random_context,
            )
            for item_key, item_value in value.items()
            if not _is_sensitive_key(str(item_key))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item, None, current_random_context) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    return repr(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _run_payload(run: PricingRun) -> dict[str, Any]:
    return _json_value(run)


def _write_run_json(run: PricingRun) -> None:
    if run.json_output_path is None:
        return
    path = Path(run.json_output_path)
    path.parent.mkdir(parents=True, exist_ok=False)
    path.write_text(
        json.dumps(
            _run_payload(run), ensure_ascii=False, sort_keys=False, indent=2, allow_nan=False
        ) + "\n",
        encoding="utf-8",
    )


def _write_solve_json(run: SolveRun) -> None:
    if run.json_output_path is None:
        return
    path = Path(run.json_output_path)
    path.parent.mkdir(parents=True, exist_ok=False)
    path.write_text(
        json.dumps(
            _json_value(run), ensure_ascii=False, sort_keys=False, indent=2,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )


def _print_section(name: str, value: Any) -> None:
    print(f"[{name}]")
    print(json.dumps(_json_value(value), ensure_ascii=False, sort_keys=False, indent=2))


def _print_run(run: PricingRun) -> None:
    _print_section("REQUEST", {
        "run_id": run.run_id,
        "request_fingerprint": run.request_fingerprint,
        "family": run.family,
        "structure": run.structure,
        "method": run.method,
        "route_id": run.route_id,
    })
    _print_section("REQUESTED_PARAMETERS", run.requested_parameters)
    _print_section("EFFECTIVE_PARAMETERS", run.effective_parameters)
    _print_section("CONTRACT", {
        "requested": run.requested_parameters.get("contract", {}),
        "effective": run.effective_parameters["contract"],
    })
    _print_section("MARKET", {
        "requested": run.requested_parameters.get("market", {}),
        "effective": run.effective_parameters["market"],
    })
    _print_section("STATE", {
        "requested": run.requested_parameters.get("valuation_state", {}),
        "effective": run.effective_parameters["valuation_state"],
    })
    _print_section("CONFIG", {
        "requested": run.requested_parameters.get("config", {}),
        "effective": run.effective_parameters["config"],
        "random_source": run.random_source,
    })
    _print_section("RESULT", {
        "method": run.result.method,
        "version": run.result.version,
        "currency": run.result.currency,
    })
    _print_section("PV", {
        "pv_amount": run.result.pv_amount,
        "pv_percent": run.result.pv_percent,
        "pv_points_100": run.result.pv_points_100,
    })
    _print_section("GREEKS", run.result.greeks)
    _print_section("EXTENDED_RISKS", run.result.extended_risks)
    _print_section("WARNINGS", run.result.warnings)
    _print_section("DIAGNOSTICS", {
        "diagnostics": run.result.diagnostics,
        "engine_raw": run.result.engine_raw,
    })
    _print_section("ARTIFACTS", {"pricing_run_json": run.json_output_path})


def _print_solve_run(run: SolveRun) -> None:
    _print_section("REQUEST", {
        "run_id": run.run_id,
        "request_fingerprint": run.request_fingerprint,
        "family": run.family,
        "structure": run.structure,
        "method": run.method,
        "route_id": run.route_id,
    })
    _print_section("TARGET", run.target)
    _print_section("REQUESTED_PARAMETERS", run.requested_parameters)
    _print_section("EFFECTIVE_PARAMETERS", run.effective_parameters)
    _print_section("CONFIG", {
        "effective": run.effective_config,
        "random_source": run.random_source,
    })
    _print_section("SOLUTION", run.result)
    _print_section("ARTIFACTS", {"solve_run_json": run.json_output_path})


def _is_project_derivatives(module: Any) -> bool:
    module_file = getattr(module, "__file__", None)
    if not module_file:
        return False
    try:
        return Path(module_file).resolve() == (
            ROOT / "engine" / "derivatives" / "__init__.py"
        ).resolve()
    except (OSError, RuntimeError, TypeError):
        return False


def _load_aliased_derivatives():
    """以稳定专属包名加载项目内核，完全避开宿主derivatives命名空间。"""
    current = sys.modules.get(_DERIVATIVES_ALIAS)
    if current is not None:
        if not _is_project_derivatives(current):
            raise ImportError(f"专属模块名{_DERIVATIVES_ALIAS}已被非项目模块占用")
        return current

    for name in tuple(sys.modules):
        if name.startswith(_DERIVATIVES_ALIAS + "."):
            sys.modules.pop(name, None)

    package_dir = ROOT / "engine" / "derivatives"
    package_path = package_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        _DERIVATIVES_ALIAS,
        package_path,
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"无法建立项目定价内核别名：{package_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        for name in tuple(sys.modules):
            if name == _DERIVATIVES_ALIAS or name.startswith(_DERIVATIVES_ALIAS + "."):
                sys.modules.pop(name, None)
        raise
    if not _is_project_derivatives(module):
        for name in tuple(sys.modules):
            if name == _DERIVATIVES_ALIAS or name.startswith(_DERIVATIVES_ALIAS + "."):
                sys.modules.pop(name, None)
        raise ImportError("专属别名未加载本项目engine/derivatives定价内核")
    return module


@contextmanager
def _load_derivatives():
    """稳定复用项目内核，并以专属别名避开宿主同名模块。"""
    with _DERIVATIVES_IMPORT_LOCK:
        current = sys.modules.get("derivatives")
        if current is not None and _is_project_derivatives(current):
            yield current
            return

        namespace = {
            name: module
            for name, module in tuple(sys.modules.items())
            if name == "derivatives" or name.startswith("derivatives.")
        }
        if not namespace:
            engine_path = str(ROOT / "engine")
            inserted = engine_path not in sys.path
            if inserted:
                sys.path.insert(0, engine_path)
            imported = False
            try:
                project_module = importlib.import_module("derivatives")
                if not _is_project_derivatives(project_module):
                    raise ImportError("未能加载本项目engine/derivatives定价内核")
                imported = True
                yield project_module
            finally:
                if not imported:
                    for name in tuple(sys.modules):
                        if name == "derivatives" or name.startswith("derivatives."):
                            sys.modules.pop(name, None)
                if inserted:
                    try:
                        sys.path.remove(engine_path)
                    except ValueError:  # pragma: no cover - 防御外部并发修改
                        pass
            return

        yield _load_aliased_derivatives()


def _require_choice(label: str, value: Any, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{label}必须为以下精确值之一：{', '.join(choices)}")
    return value


def _catalog_choices(structure: str, field: str) -> tuple[str, ...]:
    family = _CATALOG.family_for_structure(structure)
    return tuple(_CATALOG.contract_spec(family, structure)["choices"][field])


def _strict_enum(enum_type: Any, label: str, value: Any):
    if not isinstance(value, str) or value not in enum_type.__members__:
        allowed = ", ".join(enum_type.__members__)
        raise ValueError(f"{label}必须使用精确枚举名：{allowed}")
    return enum_type[value]


def _build_basis(derivatives: Any, value: Any):
    values = _require_mapping("contract.basis", value)
    _reject_unknown("contract.basis", values, {"notional", "currency"})
    return derivatives.ResultBasis(
        notional=values.get("notional"),
        currency=values.get("currency"),
    )


def _build_schedule(derivatives: Any, label: str, value: Any) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{label}必须为显式观察日列表")
    points = []
    for index, item in enumerate(value):
        point_label = f"{label}[{index}]"
        values = _require_mapping(point_label, item)
        _reject_unknown(point_label, values, {"trading_day", "calendar_day", "barrier", "amount"})
        missing = [field for field in ("trading_day", "calendar_day") if field not in values]
        if missing:
            raise ValueError(f"{point_label}缺少必填字段：{', '.join(missing)}")
        points.append(
            derivatives.SchedulePoint(
                trading_day=values["trading_day"],
                calendar_day=values["calendar_day"],
                barrier=values.get("barrier"),
                amount=values.get("amount"),
            )
        )
    if not points:
        raise ValueError(f"{label}不得为空")
    return tuple(points)


def _build_instrument(derivatives: Any, structure: str, contract: dict[str, Any]):
    basis = _build_basis(derivatives, contract.pop("basis"))
    if structure == "EUROPEAN_VANILLA":
        return derivatives.EuropeanVanillaOption(
            basis=basis,
            strike=contract["strike"],
            maturity_years=contract["maturity_years"],
            call_put=_strict_enum(derivatives.CallPut, "contract.call_put", contract["call_put"]),
            future=contract.get("future", False),
        )
    if structure == "OPTIONREG_PATH":
        return derivatives.OptionRegPathOption(
            basis=basis,
            resolved_contract=contract["resolved_contract"],
            asset_spots=tuple(contract.get("asset_spots", ())),
            asset_volatilities=tuple(contract.get("asset_volatilities", ())),
            asset_dividend_yields=tuple(contract.get("asset_dividend_yields", ())),
            correlation=(
                None if contract.get("correlation") is None
                else tuple(tuple(row) for row in contract["correlation"])
            ),
            trading_sessions=tuple(contract.get("trading_sessions", ())),
            calendar_id=str(contract.get("calendar_id", "")),
            calendar_version=str(contract.get("calendar_version", "")),
        )
    if structure == "BINARY":
        return derivatives.BinaryOption(
            basis=basis,
            strike=contract["strike"],
            maturity_years=contract["maturity_years"],
            call_put=_strict_enum(derivatives.CallPut, "contract.call_put", contract["call_put"]),
            payout_type=_require_choice(
                "contract.payout_type",
                contract["payout_type"],
                _catalog_choices("BINARY", "payout_type"),
            ),
            payout=contract.get("payout", 0.0),
            future=contract.get("future", False),
        )
    if structure == "BARRIER":
        return derivatives.BarrierOption(
            basis=basis,
            strike=contract["strike"],
            barrier=contract["barrier"],
            maturity_years=contract["maturity_years"],
            call_put=_strict_enum(derivatives.CallPut, "contract.call_put", contract["call_put"]),
            knock=_require_choice(
                "contract.knock",
                contract["knock"],
                _catalog_choices("BARRIER", "knock"),
            ),
            monitoring=_require_choice(
                "contract.monitoring",
                contract["monitoring"],
                _catalog_choices("BARRIER", "monitoring"),
            ),
            rebate=contract.get("rebate", 0.0),
            rebate_at_hit=contract.get("rebate_at_hit", True),
            future=contract.get("future", False),
        )
    if structure == "AIRBAG":
        return _build_airbag(derivatives, basis, contract["legs"])
    if structure == "STATIC_ACCUMULATOR":
        return derivatives.StaticAccumulatorOption(
            basis=basis,
            call_put=_strict_enum(derivatives.CallPut, "contract.call_put", contract["call_put"]),
            initial_spot=contract["initial_spot"],
            strike=contract["strike"],
            barrier=contract["barrier"],
            range_payout=contract["range_payout"],
            knockout_payout=contract["knockout_payout"],
            loss_multiplier=contract["loss_multiplier"],
            first_observation=contract["first_observation"],
            observation_count=contract["observation_count"],
            total_observations=contract["total_observations"],
            accumulator_type=_require_choice(
                "contract.accumulator_type",
                contract["accumulator_type"],
                _catalog_choices("STATIC_ACCUMULATOR", "accumulator_type"),
            ),
            expiry_multiplier=contract.get("expiry_multiplier", 1.0),
            day_adjustment=contract.get("day_adjustment", 0.0),
            quantity_basis=_strict_enum(
                derivatives.AccumulatorQuantityBasis,
                "contract.quantity_basis",
                contract.get("quantity_basis", "WHOLE_CONTRACT"),
            ),
        )
    if structure in {"SNOWBALL", "PHOENIX", "TRIGGER"}:
        return derivatives.AutocallOption(
            basis=basis,
            kind=_strict_enum(derivatives.AutocallKind, "structure", structure),
            call_put=_strict_enum(derivatives.CallPut, "contract.call_put", contract["call_put"]),
            strike=contract["strike"],
            knock_in=contract["knock_in"],
            knock_out=contract["knock_out"],
            floor=contract["floor"],
            coupon=contract["coupon"],
            call_schedule=_build_schedule(derivatives, "contract.call_schedule", contract["call_schedule"]),
            coupon_schedule=_build_schedule(
                derivatives,
                "contract.coupon_schedule",
                contract["coupon_schedule"],
            ),
            final_trading_day=contract["final_trading_day"],
            final_calendar_day=contract["final_calendar_day"],
            final_rebate=contract["final_rebate"],
            margin=contract.get("margin", 0.0),
            knock_out_step_down=contract.get("knock_out_step_down"),
            forward_curve_weight=contract.get("forward_curve_weight", 1.0),
            parachute=contract.get("parachute", False),
            enhanced_strike=contract.get("enhanced_strike"),
            participation=contract.get("participation", 0.0),
        )
    return derivatives.PathAccumulatorOption(
        basis=basis,
        call_put=_strict_enum(derivatives.CallPut, "contract.call_put", contract["call_put"]),
        strike=contract["strike"],
        knock_out=contract["knock_out"],
        multiplier=contract["multiplier"],
        ko_begin_trading_day=contract["ko_begin_trading_day"],
        lock_trading_days=contract["lock_trading_days"],
        ko_terminates=contract["ko_terminates"],
        observation_schedule=_build_schedule(
            derivatives,
            "contract.observation_schedule",
            contract["observation_schedule"],
        ),
        forward_curve_weight=contract.get("forward_curve_weight", 1.0),
        quantity_basis=_strict_enum(
            derivatives.AccumulatorQuantityBasis,
            "contract.quantity_basis",
            contract.get("quantity_basis", "WHOLE_CONTRACT"),
        ),
    )


def _build_airbag(derivatives: Any, basis: Any, value: Any):
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or not value:
        raise ValueError("AIRBAG.contract.legs必须为非空显式腿列表")
    supported = {"EUROPEAN_VANILLA", "BINARY", "BARRIER"}
    legs = []
    for index, item in enumerate(value):
        label = f"AIRBAG.contract.legs[{index}]"
        values = _require_mapping(label, item)
        _reject_unknown(label, values, {"structure", "contract", "method", "weight", "label"})
        missing = [field for field in ("structure", "contract", "method", "weight", "label") if field not in values]
        if missing:
            raise ValueError(f"{label}缺少必填字段：{', '.join(missing)}")
        leg_structure = values["structure"]
        if leg_structure not in supported:
            raise ValueError(f"{label}.structure只支持显式解析腿：{', '.join(sorted(supported))}")
        leg_method = values["method"]
        leg_family = _CATALOG.family_for_structure(leg_structure)
        if leg_method not in _CATALOG.methods(leg_family, leg_structure):
            raise ValueError(f"{label}.method与腿结构不匹配")
        leg_contract = _validated_group(
            f"{label}.contract",
            values["contract"],
            _CATALOG.contract_spec(leg_family, leg_structure),
        )
        instrument = _build_instrument(derivatives, leg_structure, leg_contract)
        legs.append(
            derivatives.OptionLeg(
                weight=values["weight"],
                instrument=instrument,
                method=_strict_enum(derivatives.PricingMethod, f"{label}.method", leg_method),
                label=values["label"],
            )
        )
    return derivatives.CompositeOption(basis=basis, legs=tuple(legs))


def _build_market(derivatives: Any, values: dict[str, Any]):
    as_of = values["as_of"]
    if isinstance(as_of, str):
        try:
            as_of = date.fromisoformat(as_of)
        except ValueError as error:
            raise ValueError("market.as_of必须为ISO日期YYYY-MM-DD") from error
    elif not isinstance(as_of, date):
        raise ValueError("market.as_of必须为date或ISO日期YYYY-MM-DD")
    forward_curve = values.get("forward_curve", ())
    if isinstance(forward_curve, (str, bytes)):
        raise ValueError("market.forward_curve必须为(远期价格,交易日)序列")
    try:
        forward_curve = tuple(tuple(point) for point in forward_curve)
    except TypeError as error:
        raise ValueError("market.forward_curve必须为(远期价格,交易日)序列") from error
    return derivatives.MarketState(
        as_of=as_of,
        spot=values["spot"],
        volatility=values["volatility"],
        risk_free_rate=values["risk_free_rate"],
        dividend_yield=values.get("dividend_yield", 0.0),
        carry=values.get("carry"),
        forward_curve=forward_curve,
        source=values.get("source", "unspecified"),
    )


def _build_config(derivatives: Any, method: str, values: dict[str, Any]):
    greek_bumps = values.get("greek_bumps", ())
    diagnostics = values.get("diagnostics", ())
    if isinstance(greek_bumps, Mapping):
        greek_bumps = tuple(greek_bumps.items())
    else:
        greek_bumps = tuple(greek_bumps)
    if isinstance(diagnostics, Mapping):
        diagnostics = tuple(diagnostics.items())
    else:
        diagnostics = tuple(diagnostics)
    return derivatives.ValuationConfig(
        method=_strict_enum(derivatives.PricingMethod, "method", method),
        paths=values.get("paths", 10),
        seed=values.get("seed", 20240101),
        threads=values.get("threads", 1),
        random_source=values.get("random_source"),
        greek_bumps=greek_bumps,
        diagnostics=diagnostics,
    )
