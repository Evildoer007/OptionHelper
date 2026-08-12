"""Payoffer的公开输入输出边界。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from runtime.contracts.contract_api import PayoffInput


@dataclass(frozen=True)
class PayoffResult:
    """同一ResolvedContract计算出的参数化收益图结果。"""

    payload: Mapping[str, Any]
    svg: str


__all__ = ("PayoffInput", "PayoffResult")
