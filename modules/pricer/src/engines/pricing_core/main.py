"""统一衍生品定价的精简公共入口。"""

from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path
import sys
from threading import RLock
from types import ModuleType
from typing import Any, Mapping


__all__ = (
    "list_families",
    "list_structures",
    "describe_structure",
    "price_option",
    "solve_option",
    "fetch_ifind_market_snapshot",
    "load_market_snapshot",
)

_ROOT = Path(__file__).resolve().parent
_FACADE_MODULE_NAME = "_pricer_engine_facade"
_FACADE: ModuleType | None = None
_FACADE_LOCK = RLock()
_MARKET_DATA_MODULE_NAME = "_pricer_engine_market_data"
_MARKET_DATA: ModuleType | None = None
_MARKET_DATA_LOCK = RLock()


def _ensure_contract_runtime() -> None:
    """Expose the shared OptionReg interpreter when this base is run alone.

    The internal pricing core deliberately remains usable from its own directory, as
    documented in its README.  The shared contract interpreter lives in
    ``core/src`` in the enclosing checkout, so it is discovered here at the
    public-entry boundary only when an OptionReg path is requested.  No
    absolute checkout path or business-level import is required.
    """
    try:
        importlib.import_module("runtime.contracts.contract_engine")
        return
    except ModuleNotFoundError as error:
        if error.name != "runtime":
            raise
    for parent in (_ROOT, *_ROOT.parents):
        candidate = parent / "core" / "src"
        if (candidate / "runtime" / "contracts" / "contract_engine.py").is_file():
            value = str(candidate)
            if value not in sys.path:
                sys.path.insert(0, value)
            importlib.invalidate_caches()
            importlib.import_module("runtime.contracts.contract_engine")
            return
    raise ModuleNotFoundError("Pricer离散路径模型缺少共享合同解释器；未找到项目core/src")


def _facade() -> ModuleType:
    global _FACADE
    if _FACADE is None:
        with _FACADE_LOCK:
            if _FACADE is None:
                existing = sys.modules.get(_FACADE_MODULE_NAME)
                if existing is not None:
                    _FACADE = existing
                else:
                    path = _ROOT / "engine" / "facade.py"
                    spec = importlib.util.spec_from_file_location(
                        _FACADE_MODULE_NAME,
                        path,
                    )
                    if spec is None or spec.loader is None:
                        raise ImportError(f"无法加载定价入口：{path}")
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = module
                    try:
                        spec.loader.exec_module(module)
                    except BaseException:
                        if sys.modules.get(spec.name) is module:
                            sys.modules.pop(spec.name, None)
                        raise
                    _FACADE = module
    return _FACADE


def _market_data() -> ModuleType:
    """以项目专属包名加载市场数据层，避免宿主engine同名包冲突。"""
    global _MARKET_DATA
    if _MARKET_DATA is None:
        with _MARKET_DATA_LOCK:
            if _MARKET_DATA is None:
                existing = sys.modules.get(_MARKET_DATA_MODULE_NAME)
                if existing is not None:
                    _MARKET_DATA = existing
                else:
                    package_dir = _ROOT / "engine" / "market_data"
                    path = package_dir / "__init__.py"
                    spec = importlib.util.spec_from_file_location(
                        _MARKET_DATA_MODULE_NAME,
                        path,
                        submodule_search_locations=[str(package_dir)],
                    )
                    if spec is None or spec.loader is None:
                        raise ImportError(f"无法加载市场数据层：{path}")
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = module
                    try:
                        spec.loader.exec_module(module)
                    except BaseException:
                        for name in tuple(sys.modules):
                            if name == spec.name or name.startswith(spec.name + "."):
                                sys.modules.pop(name, None)
                        raise
                    _MARKET_DATA = module
    return _MARKET_DATA


def list_families() -> tuple[str, ...]:
    """列出全部精确产品族标识。"""
    return _facade().list_families()


def list_structures(family: str) -> tuple[str, ...]:
    """列出指定产品族内的精确结构标识。"""
    return _facade().list_structures(family)


def describe_structure(family: str, structure: str) -> dict[str, Any]:
    """查看产品族和结构对应的参数、方法与风险指标。"""
    return _facade().describe_structure(family, structure)


def price_option(
    family: str,
    structure: str,
    parameters: Mapping[str, Any],
    method: str,
    *,
    output: str = "TERMINAL_AND_JSON",
):
    """按“产品族+结构+方法”使用STANDARD引擎定价。"""
    if family == "OPTIONREG" or structure == "OPTIONREG_PATH":
        _ensure_contract_runtime()
    return _facade().price_option(
        family,
        structure,
        parameters,
        method,
        output=output,
    )


def solve_option(
    family: str,
    structure: str,
    parameters: Mapping[str, Any],
    method: str,
    target: Mapping[str, Any],
    *,
    output: str = "TERMINAL_AND_JSON",
):
    """使用STANDARD引擎反解公平条款，目标PV采用每100点口径。"""
    return _facade().solve_option(
        family,
        structure,
        parameters,
        method,
        target,
        output=output,
    )


def fetch_ifind_market_snapshot(
    code: str,
    as_of: date | str,
    *,
    asset_type: str,
    volatility_window: int,
    risk_free_rate: float,
    dividend_yield: float = 0.0,
    carry: float | None = None,
    output_path: str | Path | None = None,
):
    """通过iFinD HTTP历史行情生成可复现的收盘市场快照。"""
    market_data = _market_data()
    if isinstance(as_of, str):
        try:
            as_of = date.fromisoformat(as_of)
        except ValueError as error:
            raise ValueError("as_of必须为ISO日期YYYY-MM-DD") from error
    if not isinstance(as_of, date):
        raise ValueError("as_of必须为date或ISO日期YYYY-MM-DD")
    provider = market_data.IFindHTTPProvider.from_environment()
    request = market_data.MarketSnapshotRequest(
        code=code,
        as_of=as_of,
        asset_type=asset_type,
        volatility_window=volatility_window,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
        carry=carry,
    )
    snapshot = market_data.build_market_snapshot(provider, request)
    if output_path is not None:
        market_data.save_market_snapshot(snapshot, Path(output_path))
    return snapshot


def load_market_snapshot(path: str | Path):
    """读取离线市场快照；定价阶段不触发网络请求。"""
    market_data = _market_data()
    return market_data.load_market_snapshot(Path(path))


def main() -> None:
    """运行离线Vanilla快速示例。"""
    run = price_option(
        "VANILLA",
        "EUROPEAN_VANILLA",
        {
            "contract": {
                "strike": 100.0,
                "maturity_years": 0.25,
                "call_put": "CALL",
                "basis": {
                    "cashflow_scale": 1_000_000.0,
                    "cashflow_scale_kind": "contract_cashflow",
                    "currency": "CNY",
                },
            },
            "market": {
                "as_of": date.today(),
                "spot": 100.0,
                "volatility": 0.20,
                "risk_free_rate": 0.02,
                "source": "offline_example",
            },
        },
        "BLACK_SCHOLES",
    )
    print(f"pv_percent={run.result.pv_percent:.12f}")


if __name__ == "__main__":
    main()
