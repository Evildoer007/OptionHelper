"""DataFetcher本地领域对象。

``DataAssetRef``、``CallerContext``与``SecretRef``直接复用共享协议；本模块只
拥有当前尚未进入共享层的``DataRequest``与``DataFetchRun``。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from runtime.protocol.models import (
    CallerContext as CallerContext,
    DataAssetRef,
    SecretRef as SecretRef,
)


@dataclass(frozen=True)
class DataRequest:
    """统一数据请求。

    ``asset_ids``支持多个标的；单标的调用可使用``asset_id``输入别名。日期、
    字段、频率、复权、来源与缓存策略均进入请求指纹。
    """

    asset_ids: tuple[str, ...]
    start_date: str
    end_date: str
    fields: tuple[str, ...]
    provider: str | None = None
    frequency: str = "1d"
    adjustment: str = "auto"
    source_priority: tuple[str, ...] = ("ifind_http",)
    cache_policy: str = "force_refresh"
    offline: bool = False
    local_csv: str | None = None
    local_source_fingerprint: str | None = None
    calendar_evidence_identity: str | None = None
    quota_limit: int | None = None

    @property
    def asset_id(self) -> str:
        """兼容单标的调用，不在多标的请求中猜测主标的。"""

        if len(self.asset_ids) != 1:
            raise ValueError("多标的DataRequest不存在唯一asset_id")
        return self.asset_ids[0]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DataRequest":
        payload = dict(value)
        allowed = {
            "asset_id", "asset_ids", "start_date", "end_date", "fields", "provider",
            "frequency", "adjustment", "source_priority", "cache_policy", "offline",
            "local_csv", "quota_limit",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError("DataRequest含未知字段：" + "、".join(sorted(unknown)))
        raw_assets = payload.get("asset_ids", payload.get("asset_id"))
        if isinstance(raw_assets, str):
            asset_ids = (raw_assets,)
        elif isinstance(raw_assets, Sequence) and not isinstance(raw_assets, (bytes, bytearray)):
            asset_ids = tuple(str(item) for item in raw_assets)
        else:
            asset_ids = ()

        raw_fields = payload.get("fields", ())
        if isinstance(raw_fields, str):
            fields = tuple(part.strip() for part in raw_fields.split(",") if part.strip())
        elif isinstance(raw_fields, Sequence) and not isinstance(raw_fields, (bytes, bytearray)):
            fields = tuple(str(item) for item in raw_fields)
        else:
            fields = ()

        provider = payload.get("provider")
        raw_priority = payload.get("source_priority")
        if raw_priority is None:
            source_priority = (str(provider),) if provider else cls.__dataclass_fields__["source_priority"].default
        elif isinstance(raw_priority, str):
            source_priority = tuple(part.strip() for part in raw_priority.split(",") if part.strip())
        elif isinstance(raw_priority, Sequence) and not isinstance(raw_priority, (bytes, bytearray)):
            source_priority = tuple(str(item) for item in raw_priority)
        else:
            source_priority = ()

        quota_value = payload.get("quota_limit")
        try:
            if isinstance(quota_value, bool):
                raise ValueError
            quota_limit = int(quota_value) if quota_value is not None else None
        except (TypeError, ValueError):
            raise ValueError("quota_limit必须为整数") from None

        return cls(
            asset_ids=asset_ids,
            start_date=str(payload.get("start_date", "")),
            end_date=str(payload.get("end_date", "")),
            fields=fields,
            provider=str(provider).strip() if provider is not None else None,
            frequency=str(payload.get("frequency", "1d")),
            adjustment=str(payload.get("adjustment", "auto")),
            source_priority=source_priority,
            cache_policy=str(payload.get("cache_policy", "force_refresh")),
            offline=bool(payload.get("offline", False)),
            local_csv=str(payload["local_csv"]) if payload.get("local_csv") else None,
            quota_limit=quota_limit,
        )

    def public_dict(self, *, include_local_source: bool = False) -> dict[str, Any]:
        """返回可写入结果的非敏感请求快照。"""

        value = asdict(self)
        # 仅用于缓存隔离的内部摘要不属于公开请求协议；实际日历关联写入DataAssetRef谱系。
        value.pop("calendar_evidence_identity", None)
        if not include_local_source and value.get("local_csv"):
            value["local_csv"] = "controlled-local-csv"
        return value


@dataclass(frozen=True)
class CalendarRequest:
    """中国交易所交易日历请求。

    日历请求与行情字段、频率和复权完全解耦。``fields``与``adjustment``只作为
    通用调用器可能误带的兼容输入被丢弃，不进入请求指纹或Provider参数。
    """

    asset_ids: tuple[str, ...]
    start_date: str
    end_date: str
    quota_limit: int | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CalendarRequest":
        payload = dict(value)
        allowed = {"asset_id", "asset_ids", "start_date", "end_date", "fields", "adjustment", "quota_limit"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError("交易日历请求含未知字段：" + "、".join(sorted(unknown)))
        raw_assets = payload.get("asset_ids", payload.get("asset_id"))
        if isinstance(raw_assets, str):
            asset_ids = (raw_assets.strip().upper(),)
        elif isinstance(raw_assets, Sequence) and not isinstance(raw_assets, (bytes, bytearray)):
            asset_ids = tuple(str(item).strip().upper() for item in raw_assets)
        else:
            asset_ids = ()
        quota_value = payload.get("quota_limit")
        try:
            if isinstance(quota_value, bool):
                raise ValueError
            quota_limit = int(quota_value) if quota_value is not None else None
        except (TypeError, ValueError):
            raise ValueError("交易日历quota_limit必须为整数") from None
        return cls(
            asset_ids=asset_ids,
            start_date=str(payload.get("start_date", "")).strip(),
            end_date=str(payload.get("end_date", "")).strip(),
            quota_limit=quota_limit,
        )

    def public_dict(self, *, include_local_source: bool = False) -> dict[str, Any]:
        del include_local_source
        return {
            "asset_ids": list(self.asset_ids),
            "start_date": self.start_date,
            "end_date": self.end_date,
            "quota_limit": self.quota_limit,
        }


@dataclass(frozen=True)
class DataFetchRun:
    data_fetch_run_id: str
    task_id: str
    status: str
    request_hash: str
    cache_decision: str
    provider_calls: tuple[Mapping[str, Any], ...] = ()
    quota_usage: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    error: Mapping[str, Any] | None = None
    data_asset_ref: DataAssetRef | None = None
    tenant_id: str = "local"
    expected_manifest_hash: str | None = None
    expected_data_asset_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if self.expected_manifest_hash and self.expected_data_asset_hash:
            value["data_fetch_run_ref"] = {
                "tenant_id": self.tenant_id,
                "task_id": self.task_id,
                "data_fetch_run_id": self.data_fetch_run_id,
                "expected_manifest_hash": self.expected_manifest_hash,
                "expected_data_asset_hash": self.expected_data_asset_hash,
            }
        return value


@dataclass(frozen=True)
class DataFetchResult:
    ok: bool
    run: DataFetchRun
    quality_report: Mapping[str, Any] | None = None
    data_preview: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = self.run.to_dict()
        result["ok"] = self.ok
        result["module"] = "datafetcher"
        if self.quality_report is not None:
            result["quality_report"] = dict(self.quality_report)
        if self.data_preview:
            result["data_preview"] = [dict(row) for row in self.data_preview]
        return result
