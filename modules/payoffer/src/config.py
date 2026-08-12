"""Payoffer运行配置。默认示例资产不属于本配置，也不能由运行时覆盖。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PayofferConfig:
    task_id: str = ""
    run_id: str = ""

