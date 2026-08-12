"""七模块共用的最小协议对象。

本文件只定义跨模块外形。产品条款、定价参数与回测参数仍分别由
ResolvedContract、Pricer和Backtester负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from runtime.contracts.contract_api import PayoffInput, ResolvedContract


@dataclass(frozen=True)
class DataAssetRef:
    data_asset_id: str
    storage_ref: str
    media_type: str
    schema_id: str
    asset_ids: tuple[str, ...]
    normalized_fields: tuple[str, ...]
    coverage: Mapping[str, Any]
    row_count: int
    price_convention: Mapping[str, Any]
    content_hash: str
    lineage: Mapping[str, Any]
    tenant_id: str = "local"
    created_by: str = "local"
    access_scope: tuple[str, ...] = ("read",)
    partition_spec: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CallerContext:
    tenant_id: str
    principal_id: str
    role: str
    capabilities: tuple[str, ...]
    session_id: str
    audience: str
    request_id: str = ""

    def __post_init__(self) -> None:
        for field in ("tenant_id", "principal_id", "role", "session_id", "audience"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError(f"CallerContext.{field}必须为不含控制字符的非空字符串")
        if (
            not isinstance(self.capabilities, tuple)
            or any(not isinstance(item, str) or not item.strip() or any(ord(char) < 32 or ord(char) == 127 for char in item) for item in self.capabilities)
            or len(set(self.capabilities)) != len(self.capabilities)
        ):
            raise ValueError("CallerContext.capabilities必须为不重复的非空字符串元组")
        if not isinstance(self.request_id, str) or any(ord(char) < 32 or ord(char) == 127 for char in self.request_id):
            raise ValueError("CallerContext.request_id不得包含控制字符")


@dataclass(frozen=True)
class SecretRef:
    """跨Host传递的凭据引用，绝不承载凭据值。"""

    provider: str
    key: str
    version: str | None = None

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.key.strip():
            raise ValueError("SecretRef requires non-empty provider and key")
        if self.version is not None and not self.version.strip():
            raise ValueError("SecretRef version must be non-empty when supplied")

    def redacted(self) -> dict[str, str | None]:
        return {"provider": self.provider, "key": self.key, "version": self.version}


@dataclass(frozen=True)
class DataFetchRunRef:
    tenant_id: str
    task_id: str
    data_fetch_run_id: str
    expected_manifest_hash: str
    expected_data_asset_hash: str


@dataclass(frozen=True)
class ModuleRunRef:
    module: str
    tenant_id: str
    task_id: str
    run_id: str
    expected_semantic_result_hash: str
    expected_artifact_manifest_hash: str

    def __post_init__(self) -> None:
        if self.module not in {"payoffer", "pricer", "backtester"}:
            raise ValueError("ModuleRunRef.module必须为三个计算模块之一")
        for name in ("expected_semantic_result_hash", "expected_artifact_manifest_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"ModuleRunRef.{name}必须为64位小写SHA-256")


@dataclass(frozen=True)
class ReportRunRef:
    tenant_id: str
    report_run_id: str
    expected_semantic_fact_hash: str
    expected_artifact_manifest_hash: str


@dataclass(frozen=True)
class PricingInput:
    contract: ResolvedContract
    pricing_config: Mapping[str, Any]
    market_data_refs: tuple[DataAssetRef, ...] = ()
    trading_calendar_ref: DataAssetRef | None = None


@dataclass(frozen=True)
class BacktestInput:
    contract: ResolvedContract
    backtest_config: Mapping[str, Any]
    historical_data: DataAssetRef


@dataclass(frozen=True)
class ArtifactRef:
    name: str
    storage_ref: str
    media_type: str
    content_hash: str


@dataclass(frozen=True)
class ModuleRun:
    module: str
    analysis_case_id: str
    task_id: str
    run_id: str
    status: str
    contract_fingerprint: str | None
    catalog_version: str | None
    execution_fingerprint: str
    input_snapshot: Mapping[str, Any]
    data_refs: tuple[DataAssetRef, ...]
    result: Mapping[str, Any] | None
    limitations: tuple[str, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    error: Mapping[str, Any] | None = None
    candidate_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    tenant_id: str = "local"
    created_by: str = "local"
    access_scope: tuple[str, ...] = ("read",)
    artifact_manifest: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_module_name(self.module)
        require_run_status(self.status)
        for label, value in (("analysis_case_id", self.analysis_case_id), ("task_id", self.task_id), ("run_id", self.run_id)):
            if not value:
                raise ValueError(f"ModuleRun.{label}不能为空")
        if self.status in {"succeeded", "partial"}:
            if not self.candidate_id:
                raise ValueError("成功或部分成功的ModuleRun.candidate_id不能为空")
            if not self.catalog_version:
                raise ValueError("成功或部分成功的ModuleRun.catalog_version不能为空")
            if not isinstance(self.contract_fingerprint, str) or len(self.contract_fingerprint) != 64 or any(
                char not in "0123456789abcdef" for char in self.contract_fingerprint
            ):
                raise ValueError("成功或部分成功的ModuleRun.contract_fingerprint必须为64位小写SHA-256")


def require_module_name(value: str) -> str:
    allowed = {"payoffer", "pricer", "backtester"}
    if value not in allowed:
        raise ValueError(f"ModuleRun.module必须为{','.join(sorted(allowed))}")
    return value


def require_run_status(value: str) -> str:
    allowed = {
        "created", "validating", "queued", "running", "succeeded", "partial", "failed",
        "unsupported", "cancelled", "timed_out",
    }
    if value not in allowed:
        raise ValueError(f"运行状态无效：{value}")
    return value


_RUN_TRANSITIONS = {
    "created": {"validating", "cancelled"},
    "validating": {"queued", "unsupported", "failed", "cancelled"},
    "queued": {"running", "cancelled", "timed_out"},
    "running": {"succeeded", "partial", "failed", "cancelled", "timed_out"},
}


def require_run_transition(current: str, target: str) -> tuple[str, str]:
    require_run_status(current)
    require_run_status(target)
    if target not in _RUN_TRANSITIONS.get(current, set()):
        raise ValueError(f"非法运行状态转换：{current}->{target}")
    return current, target


__all__ = (
    "ArtifactRef", "BacktestInput", "CallerContext", "DataAssetRef", "DataFetchRunRef",
    "ModuleRun", "ModuleRunRef", "PayoffInput", "PricingInput", "ReportRunRef", "SecretRef",
    "require_module_name", "require_run_status", "require_run_transition",
)
