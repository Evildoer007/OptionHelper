"""Bind count-based OptionReg terms to the injected contract calendar."""

from __future__ import annotations

from datetime import date, timedelta
import re
from typing import Any, Mapping, Sequence

from runtime.contracts.contract_api import bind_term_symbols, resolve_schedule


def actual_n_obs(
    contract: Any,
    *,
    sessions: Sequence[str],
    as_of: date,
    remaining_years: float,
) -> int | None:
    """Return the actual future observation count for a contract's ``n_obs``.

    The price path always contains every injected trading session.  ``n_obs``
    is therefore derived from the same selector that its monitor expression
    uses, rather than being a proportional-tenor proxy or raw session count.
    """
    if contract.terms.get("n_obs") is None:
        return None
    selector = _n_obs_selector(contract.terms)
    if selector is None:
        raise ValueError("n_obs合同未能从监测公式识别对应观察日程")
    target = as_of + timedelta(days=round(remaining_years * 365.0))
    available = tuple(
        value for value in sessions
        if as_of <= date.fromisoformat(str(value)) <= target
    )
    if not available:
        raise ValueError("注入交易日历未覆盖n_obs合同的剩余期限")
    count = len(resolve_schedule(selector, available))
    if count <= 0:
        raise ValueError("合同观察日程在注入交易日历的剩余期限内没有实际观察日")
    return count


def _n_obs_selector(terms: Mapping[str, Any]) -> object | None:
    monitor = terms.get("monitor")
    if not isinstance(monitor, Mapping):
        return None
    formulas = [
        str(formula) for formula in monitor.values()
        if isinstance(formula, str) and re.search(r"\bn_obs\b", formula)
    ]
    if not formulas:
        return None
    symbols = {
        symbol
        for formula in formulas
        for symbol in re.findall(r"\bS_t\[([A-Za-z_][A-Za-z0-9_]*)\]", formula)
    }
    selectors: list[object] = []
    for key, value in terms.items():
        try:
            bindings = bind_term_symbols({key: value})
        except Exception:
            continue
        if len(bindings) == 1 and next(iter(bindings)) in symbols:
            selectors.append(value)
    if len(selectors) != 1:
        raise ValueError("n_obs合同必须在监测公式中唯一引用一个观察日程")
    return selectors[0]


__all__ = ("actual_n_obs",)
