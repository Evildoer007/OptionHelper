"""Backtester的唯一历史数据边界：合同结算close与入场HV adj_close。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping

import numpy as np
import pandas as pd

from runtime.protocol.models import DataAssetRef


CONTRACT_PRICE_FIELDS = {"close", "open", "high", "low"}
_LOCAL_STORAGE_REF = re.compile(r"local-data:sha256:([0-9a-f]{64})")
DATA_ASSET_REQUIRED_FIELDS = frozenset({
    "data_asset_id", "storage_ref", "media_type", "schema_id", "asset_ids", "normalized_fields", "coverage",
    "row_count", "price_convention", "content_hash", "lineage", "tenant_id", "created_by", "access_scope",
    "partition_spec",
})


class HistoricalDataError(ValueError):
    """历史DataAssetRef、价格口径或本地文件不满足回测要求。"""


class DataFetcherPortUnavailable(HistoricalDataError):
    """DataFetcher端口或其DataStore读取端口不可用。"""


@dataclass(frozen=True)
class HistoricalData:
    """已标准化的交易日历史数据及其不可替代的数据谱系。"""

    frame: pd.DataFrame
    data_asset_ref: dict[str, Any]
    normalized_content_hash: str
    data_asset_ref_fingerprint: str
    contract_price_field: str = "close"
    contract_adjustment: str = "unadjusted"
    hv_price_field: str | None = None
    hv_adjustment: str | None = None
    trading_sessions: pd.DatetimeIndex = field(default_factory=pd.DatetimeIndex)
    calendar_id: str = ""
    calendar_revision: str = ""
    calendar_coverage_end: pd.Timestamp | None = None
    calendar_source_declared: bool = False
    limitations: tuple[str, ...] = ()

    @classmethod
    def from_frame(cls, frame: pd.DataFrame, *, data_asset_ref: Mapping[str, Any] | None = None) -> "HistoricalData":
        data = normalize_history_frame(frame).copy(deep=True)
        normalized_hash = _frame_hash(data)
        reference = _build_or_validate_asset_ref(data, data_asset_ref, normalized_hash)
        contract_field, contract_adjustment, hv_field, hv_adjustment = _price_convention(data, reference)
        sessions = _trading_sessions(reference["coverage"])
        calendar_id = str(reference["coverage"]["calendar_id"])
        calendar_revision = str(reference["coverage"]["calendar_revision"])
        calendar_coverage_end = _calendar_coverage_end(reference["coverage"])
        source_declared = _calendar_is_source_declared(data_asset_ref)
        limitations: list[str] = []
        if not source_declared:
            limitations.append("calendar_sessions_derived_from_frame_not_formal_evidence")
        if data_asset_ref is None:
            limitations.append("in_memory_historical_data_without_persisted_data_asset")
        return cls(
            data, reference, normalized_hash, _reference_fingerprint(reference),
            contract_price_field=contract_field, contract_adjustment=contract_adjustment,
            hv_price_field=hv_field, hv_adjustment=hv_adjustment, trading_sessions=sessions,
            calendar_id=calendar_id, calendar_revision=calendar_revision, calendar_coverage_end=calendar_coverage_end,
            calendar_source_declared=source_declared,
            limitations=tuple(limitations),
        )

    def validate_integrity(self) -> None:
        """冻结对象内的DataFrame仍可能被外部改写，正式入口必须重验。"""
        if self.contract_price_field != "close" or self.contract_adjustment != "unadjusted":
            raise HistoricalDataError("Backtester合同回放价格必须为不复权close")
        data = normalize_history_frame(self.frame)
        if _frame_hash(data) != self.normalized_content_hash:
            raise HistoricalDataError("HistoricalData内容已变化，拒绝使用与DataAssetRef不一致的价格路径")
        if _reference_fingerprint(self.data_asset_ref) != self.data_asset_ref_fingerprint:
            raise HistoricalDataError("HistoricalData.data_asset_ref已变化，拒绝使用失真的数据谱系")
        _build_or_validate_asset_ref(data, self.data_asset_ref, self.normalized_content_hash)
        sessions = _trading_sessions(self.data_asset_ref["coverage"])
        if not sessions.equals(self.trading_sessions):
            raise HistoricalDataError("HistoricalData交易日历已变化，拒绝使用失真的期限路径")
        if _calendar_coverage_end(self.data_asset_ref["coverage"]) != self.calendar_coverage_end:
            raise HistoricalDataError("HistoricalData交易日历覆盖终点已变化，拒绝使用失真的期限路径")


def normalize_history_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """校验合同不复权OHLC及可选前复权adj_close。"""
    if not isinstance(frame, pd.DataFrame):
        raise HistoricalDataError("HistoricalData.frame必须为pandas.DataFrame")
    missing = {"date", "asset_id"} - set(frame.columns)
    if missing:
        raise HistoricalDataError(f"历史行情缺少字段：{','.join(sorted(missing))}")
    if "close" not in frame.columns:
        raise HistoricalDataError("Backtester必须提供合同结算不复权收盘价close")
    selected = ["date", "asset_id", "close", *(column for column in ("open", "high", "low", "adj_close") if column in frame.columns)]
    data = frame.loc[:, selected].copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    data["asset_id"] = data["asset_id"].astype(str).str.strip()
    for column in selected[2:]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    invalid = data["date"].isna() | data["asset_id"].eq("")
    contract_fields = [column for column in ("close", "open", "high", "low") if column in data.columns]
    for column in contract_fields:
        invalid = invalid | ~np.isfinite(data[column]) | (data[column] < 0)
    if invalid.any():
        raise HistoricalDataError("历史行情含无效date、asset_id或价格字段")
    first_rows = data.sort_values(["asset_id", "date"]).groupby("asset_id", sort=False).head(1)
    if (first_rows[contract_fields] <= 0).any().any():
        raise HistoricalDataError("每个标的首个合同价格观察必须严格为正")
    if data.duplicated(["date", "asset_id"]).any():
        raise HistoricalDataError("历史行情含重复date、asset_id")
    if (data["date"].dt.dayofweek >= 5).any():
        raise HistoricalDataError("历史数据含周末日期，不是交易日序列")
    return data.sort_values(["date", "asset_id"]).reset_index(drop=True)


def load_local_historical_data(reference: str | Path | Mapping[str, Any], *, project_root: Path, data_root: Path) -> HistoricalData:
    """从受控``data/``内读取CSV或本地DataAssetRef，拒绝任意路径。"""
    supplied = _reference_mapping(reference) if isinstance(reference, Mapping) or is_dataclass(reference) else None
    raw_reference = str(supplied["storage_ref"]) if supplied is not None else reference
    raw_text = str(raw_reference)
    if raw_text.startswith("data:"):
        raise DataFetcherPortUnavailable("DataAssetRef需要经注入的DataStore读取，Backtester不会猜测DataFetcher存储路径")
    local_match = _LOCAL_STORAGE_REF.fullmatch(raw_text)
    path = (
        _controlled_asset_by_hash(local_match.group(1), data_root=data_root)
        if local_match
        else _controlled_local_path(raw_reference, project_root=project_root, data_root=data_root)
    )
    if path.suffix.lower() != ".csv":
        raise HistoricalDataError("本地历史数据当前仅支持CSV DataAssetRef")
    try:
        payload = path.read_bytes()
        frame = pd.read_csv(BytesIO(payload))
    except (OSError, UnicodeDecodeError, pd.errors.ParserError) as error:
        raise HistoricalDataError(f"无法读取本地历史数据：{path}") from error
    reference_payload = dict(supplied or {})
    content_hash = sha256(payload).hexdigest()
    if reference_payload.get("content_hash") not in {None, content_hash}:
        raise HistoricalDataError("DataAssetRef.content_hash与本地CSV字节不一致")
    reference_payload.update({
        "storage_ref": f"local-data:sha256:{content_hash}",
        "content_hash": content_hash,
    })
    asset_ids = tuple(sorted(str(item).strip() for item in frame["asset_id"].unique()))
    equal_close = frame["close"].astype("float64").equals(frame["adj_close"].astype("float64")) if "adj_close" in frame.columns else False
    if equal_close and all(_is_china_index_asset(asset_id) for asset_id in asset_ids):
        reference_payload.setdefault("price_convention", {
            "contract_close_field": "close", "contract_adjustment": "unadjusted",
            "hv_close_field": "adj_close", "hv_adjustment": "forward",
            "close_equals_adj_close": True, "calendar": "trading_days",
        })
    reference_payload.setdefault("lineage", {"provider": "local", "load_mode": "controlled_local_input"})
    return HistoricalData.from_frame(frame, data_asset_ref=reference_payload)


def load_port_historical_data(
    request: Mapping[str, Any],
    *,
    project_root: Path,
    data_root: Path,
    call_port: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    data_store: Any | None = None,
) -> HistoricalData:
    """经注入ModulePort与DataStorePort消费真实DataAssetRef，不导入DataFetcher。"""
    if call_port is None:
        raise DataFetcherPortUnavailable("未注入历史数据端口，Backtester不会直接导入DataFetcher")
    response = call_port({"action": "fetch", "request": dict(request)})
    if not isinstance(response, Mapping) or not response.get("ok"):
        reason = response.get("reason") if isinstance(response, Mapping) else None
        raise DataFetcherPortUnavailable(f"DataFetcher未提供可消费的历史DataAssetRef：{reason or 'unavailable'}")
    raw_reference = response.get("data_asset_ref")
    if raw_reference is None:
        raise DataFetcherPortUnavailable("DataFetcher响应缺少data_asset_ref，禁止用替代行情继续回测")
    reference = _reference_mapping(raw_reference)
    if data_store is None or not callable(getattr(data_store, "read_bytes", None)):
        if not str(reference["storage_ref"]).startswith("data:"):
            return load_local_historical_data(reference, project_root=project_root, data_root=data_root)
        raise DataFetcherPortUnavailable("DataFetcher DataAssetRef需要注入DataStorePort.read_bytes，禁止直接解析其内部路径")
    asset_ref = _protocol_data_asset_ref(reference)
    try:
        payload = data_store.read_bytes(asset_ref, tenant_id=asset_ref.tenant_id)
        frame = pd.read_csv(BytesIO(payload))
    except (OSError, UnicodeDecodeError, pd.errors.ParserError, PermissionError, ValueError) as error:
        raise DataFetcherPortUnavailable("无法通过注入DataStore读取DataFetcher历史资产") from error
    if sha256(payload).hexdigest() != asset_ref.content_hash:
        raise HistoricalDataError("DataStore返回内容与DataAssetRef.content_hash不一致")
    return HistoricalData.from_frame(frame, data_asset_ref=asdict(asset_ref))


def required_contract_fields(observation_price: str) -> tuple[str, ...]:
    if observation_price not in CONTRACT_PRICE_FIELDS:
        raise HistoricalDataError(f"不支持的合同观察价格：{observation_price}")
    return ("close", observation_price) if observation_price != "close" else ("close",)


def _controlled_local_path(value: str | Path, *, project_root: Path, data_root: Path) -> Path:
    raw = Path(value).expanduser()
    path = raw if raw.is_absolute() else project_root / raw
    path, root = path.resolve(), data_root.resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise HistoricalDataError("本地历史数据必须位于受控data目录，禁止任意路径读取") from error
    if not path.is_file():
        raise HistoricalDataError(f"历史DataAssetRef不存在：{path}")
    return path


def _controlled_asset_by_hash(content_hash: str, *, data_root: Path) -> Path:
    """只在受控data根内按内容哈希解析本地opaque引用。"""
    root = data_root.resolve()
    if not root.is_dir():
        raise HistoricalDataError("受控data目录不存在")
    matches: list[Path] = []
    for candidate in root.rglob("*.csv"):
        try:
            path = _controlled_local_path(candidate, project_root=root, data_root=root)
            if sha256(path.read_bytes()).hexdigest() == content_hash:
                matches.append(path)
        except (HistoricalDataError, OSError):
            continue
    if not matches:
        raise HistoricalDataError("本地DataAssetRef在受控data目录中不存在或内容哈希不匹配")
    return sorted(matches)[0]


def _reference_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    raise HistoricalDataError("data_asset_ref必须为DataAssetRef或对象")


def _protocol_data_asset_ref(reference: Mapping[str, Any]) -> DataAssetRef:
    missing = DATA_ASSET_REQUIRED_FIELDS - set(reference)
    if missing:
        raise HistoricalDataError(f"DataAssetRef缺少字段：{','.join(sorted(missing))}")
    unknown = set(reference) - DATA_ASSET_REQUIRED_FIELDS
    if unknown:
        raise HistoricalDataError(f"DataAssetRef含未知字段：{','.join(sorted(unknown))}")
    string_fields = ("data_asset_id", "storage_ref", "media_type", "schema_id", "content_hash", "tenant_id", "created_by")
    if any(not isinstance(reference[name], str) or not reference[name].strip() for name in string_fields):
        raise HistoricalDataError("DataAssetRef必填字符串字段不能为空")
    if not isinstance(reference["asset_ids"], (list, tuple)) or not reference["asset_ids"]:
        raise HistoricalDataError("DataAssetRef.asset_ids必须为非空数组")
    if any(not isinstance(item, str) for item in reference["asset_ids"]):
        raise HistoricalDataError("DataAssetRef.asset_ids元素必须为字符串")
    for name in ("normalized_fields", "access_scope"):
        if not isinstance(reference[name], (list, tuple)) or any(not isinstance(item, str) for item in reference[name]):
            raise HistoricalDataError(f"DataAssetRef.{name}必须为字符串数组")
    for name in ("coverage", "price_convention", "lineage", "partition_spec"):
        if not isinstance(reference[name], Mapping):
            raise HistoricalDataError(f"DataAssetRef.{name}必须为对象")
    if isinstance(reference["row_count"], bool) or not isinstance(reference["row_count"], int) or reference["row_count"] < 0:
        raise HistoricalDataError("DataAssetRef.row_count必须为非负整数")
    try:
        return DataAssetRef(
            data_asset_id=str(reference["data_asset_id"]), storage_ref=str(reference["storage_ref"]), media_type=str(reference["media_type"]),
            schema_id=str(reference["schema_id"]), asset_ids=tuple(str(item) for item in reference["asset_ids"]),
            normalized_fields=tuple(str(item) for item in reference["normalized_fields"]), coverage=dict(reference["coverage"]),
            row_count=int(reference["row_count"]), price_convention=dict(reference["price_convention"]), content_hash=str(reference["content_hash"]),
            lineage=dict(reference["lineage"]), tenant_id=str(reference["tenant_id"]), created_by=str(reference["created_by"]),
            access_scope=tuple(str(item) for item in reference["access_scope"]), partition_spec=dict(reference["partition_spec"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        # Core会先对storage_ref执行不透明引用校验。保留这一边界的语义，
        # 使调用者不会把“物理路径被拒绝”误判为普通字段格式问题。
        if "物理路径" in str(error):
            raise HistoricalDataError("DataAssetRef.storage_ref必须为受控opaque引用，禁止裸物理路径") from error
        raise HistoricalDataError("DataAssetRef格式无效") from error


def validate_data_asset_ref(reference: Mapping[str, Any]) -> dict[str, Any]:
    """以Core DataAssetRef作为唯一协议模型并返回可序列化规范值。"""
    canonical = json.loads(json.dumps(asdict(_protocol_data_asset_ref(reference)), ensure_ascii=False, allow_nan=False))
    storage_ref = str(canonical["storage_ref"])
    if not _is_opaque_storage_ref(storage_ref):
        raise HistoricalDataError("DataAssetRef.storage_ref必须为受控opaque引用，禁止裸物理路径")
    if not re.fullmatch(r"[0-9a-f]{64}", str(canonical["content_hash"])):
        raise HistoricalDataError("DataAssetRef.content_hash必须为SHA-256")
    return canonical


def _build_or_validate_asset_ref(data: pd.DataFrame, supplied: Mapping[str, Any] | None, normalized_hash: str) -> dict[str, Any]:
    unknown = set(supplied or {}) - DATA_ASSET_REQUIRED_FIELDS
    if unknown:
        raise HistoricalDataError(f"DataAssetRef含未知字段：{','.join(sorted(unknown))}")
    coverage = _coverage(data)
    default = {
        "data_asset_id": f"local-{normalized_hash[:16]}", "storage_ref": "in_memory", "media_type": "text/csv",
        "schema_id": "market-history", "asset_ids": sorted(data["asset_id"].unique().tolist()),
        "normalized_fields": data.columns.tolist(), "coverage": coverage, "row_count": int(len(data)),
        "price_convention": {
            "contract_close_field": "close", "contract_adjustment": "unadjusted",
            "hv_close_field": "adj_close" if "adj_close" in data.columns else None,
            "hv_adjustment": "forward" if "adj_close" in data.columns else None,
            "close_equals_adj_close": False, "calendar": "trading_days",
        },
        "content_hash": normalized_hash,
        "lineage": {"provider": "local", "normalizer": "backtester.historical-data"},
        "tenant_id": "local", "created_by": "local", "access_scope": ["read"], "partition_spec": {},
    }
    supplied_mapping = dict(supplied or {})
    supplied_coverage = supplied_mapping.get("coverage")
    if supplied_coverage is not None and not isinstance(supplied_coverage, Mapping):
        raise HistoricalDataError("DataAssetRef.coverage必须为对象")
    reference = {
        **default,
        **supplied_mapping,
        "coverage": {**coverage, **dict(supplied_coverage or {})},
    }
    missing = DATA_ASSET_REQUIRED_FIELDS - set(reference)
    if missing:
        raise HistoricalDataError(f"DataAssetRef缺少字段：{','.join(sorted(missing))}")
    if not isinstance(reference["content_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", reference["content_hash"]):
        raise HistoricalDataError("DataAssetRef.content_hash必须为SHA-256")
    if not _is_opaque_storage_ref(str(reference["storage_ref"])):
        raise HistoricalDataError("DataAssetRef.storage_ref必须为受控opaque引用，禁止裸物理路径")
    _price_convention(data, reference)
    fields = reference.get("normalized_fields")
    if not isinstance(fields, (list, tuple)) or "close" not in fields:
        raise HistoricalDataError("DataAssetRef.normalized_fields必须声明close")
    try:
        asset_ids, row_count = sorted(str(item) for item in reference["asset_ids"]), int(reference["row_count"])
    except (TypeError, ValueError) as error:
        raise HistoricalDataError("DataAssetRef.asset_ids或row_count格式无效") from error
    if asset_ids != default["asset_ids"]:
        raise HistoricalDataError("DataAssetRef.asset_ids与历史数据不一致")
    if row_count != default["row_count"]:
        raise HistoricalDataError("DataAssetRef.row_count与历史数据不一致")
    if not _coverage_matches(reference["coverage"], coverage):
        raise HistoricalDataError("DataAssetRef.coverage与历史数据不一致")
    if not _all_assets_cover_sessions(data, _trading_sessions(reference["coverage"])):
        raise HistoricalDataError("历史数据未覆盖DataAssetRef声明的全部交易日sessions")
    return reference


def _coverage(data: pd.DataFrame) -> dict[str, Any]:
    sessions = _session_texts(data["date"].drop_duplicates().sort_values())
    by_asset = {
        asset_id: {"start_date": group["date"].min().strftime("%Y-%m-%d"), "end_date": group["date"].max().strftime("%Y-%m-%d"), "row_count": int(len(group))}
        for asset_id, group in data.groupby("asset_id", sort=True)
    }
    return {
        "date_start": data["date"].min().strftime("%Y-%m-%d"), "date_end": data["date"].max().strftime("%Y-%m-%d"),
        "trading_day_rows": int(data["date"].nunique()), "by_asset": by_asset,
        "sessions": sessions,
        "calendar_id": "local-development",
        "calendar_revision": "frame-sessions",
    }


def _coverage_matches(declared: Any, expected: Mapping[str, Any]) -> bool:
    if not isinstance(declared, Mapping):
        return False
    if "by_asset" in declared and dict(declared["by_asset"]) != expected["by_asset"]:
        return False
    if "trading_day_rows" in declared and declared["trading_day_rows"] != expected["trading_day_rows"]:
        return False
    declared_sessions = _trading_sessions(declared)
    expected_sessions = _trading_sessions(expected)
    if not declared_sessions.equals(expected_sessions):
        return False
    if not _metadata_boundaries_enclose_sessions(declared, declared_sessions):
        return False
    return _calendar_coverage_end(declared) <= declared_sessions[-1]


def _metadata_boundaries_enclose_sessions(coverage: Mapping[str, Any], sessions: pd.DatetimeIndex) -> bool:
    """允许请求落在非交易日，但不允许元数据排除已观测交易日。

    ``sessions`` 是唯一用于合同期限与每日观察的覆盖事实；``date_*`` 与
    ``start/end_date`` 可能是数据请求边界，周末或节假日不应被误当成缺失
    行情。它们仍须是有效日期，并且必须包住实际观测的首末交易日。
    ``calendar_coverage_end`` 则仍是合同日历覆盖证据，必须由
    :func:`_calendar_coverage_end` 约束在最后一个实际 session 之内。
    """
    boundaries = (
        ("date_start", "start_date", "start"),
        ("date_end", "end_date", "end"),
    )
    for names, actual in zip(boundaries, (sessions[0], sessions[-1]), strict=True):
        for name in names:
            if name not in coverage:
                continue
            value = coverage[name]
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return False
            try:
                boundary = pd.Timestamp(pd.to_datetime(value, format="%Y-%m-%d", errors="raise"))
            except (TypeError, ValueError):
                return False
            if (name.endswith("start") or name in {"date_start", "start_date"}) and boundary > actual:
                return False
            if (name.endswith("end") or name in {"date_end", "end_date"}) and boundary < actual:
                return False
    return True


def _all_assets_cover_sessions(data: pd.DataFrame, sessions: pd.DatetimeIndex) -> bool:
    """每个标的日期必须来自受控日历；跨标的缺口由intersection显式处理。"""
    expected = set(sessions)
    return all(bool(observed := set(group["date"])) and observed <= expected for _, group in data.groupby("asset_id", sort=False))


def _calendar_is_source_declared(supplied: Mapping[str, Any] | None) -> bool:
    """只有资产引用原始声明的日历才能支撑正式完整期限判断。"""
    if not isinstance(supplied, Mapping):
        return False
    coverage = supplied.get("coverage")
    if not isinstance(coverage, Mapping):
        return False
    required = ("sessions", "calendar_id", "calendar_revision")
    if not all(name in coverage and coverage[name] is not None and coverage[name] != "" for name in required):
        return False
    calendar_id, calendar_revision = str(coverage["calendar_id"]).strip(), str(coverage["calendar_revision"]).strip()
    if (
        calendar_id.upper().startswith("UNVERIFIED")
        or calendar_revision.casefold() in {"unverified", "unknown", "derived", "frame-sessions"}
        or calendar_id.casefold() in {"local-development", "unknown"}
    ):
        return False
    try:
        _trading_sessions(coverage)
        _calendar_coverage_end(coverage)
    except HistoricalDataError:
        return False
    return True


def _session_texts(values: Any) -> list[str]:
    return [pd.Timestamp(value).strftime("%Y-%m-%d") for value in values]


def _trading_sessions(coverage: Mapping[str, Any]) -> pd.DatetimeIndex:
    values = coverage.get("sessions")
    if not isinstance(values, (list, tuple)) or not values:
        raise HistoricalDataError("DataAssetRef.coverage必须提供非空交易日sessions")
    if any(not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in values):
        raise HistoricalDataError("DataAssetRef.coverage.sessions必须为YYYY-MM-DD交易日")
    try:
        sessions = pd.DatetimeIndex(pd.to_datetime(list(values), format="%Y-%m-%d", errors="raise"))
    except (TypeError, ValueError) as error:
        raise HistoricalDataError("DataAssetRef.coverage.sessions必须为有效交易日") from error
    if sessions.has_duplicates or not sessions.is_monotonic_increasing:
        raise HistoricalDataError("DataAssetRef.coverage.sessions必须严格递增且不重复")
    if (sessions.dayofweek >= 5).any():
        raise HistoricalDataError("DataAssetRef.coverage.sessions含非交易日周末")
    for calendar_field in ("calendar_id", "calendar_revision"):
        value = coverage.get(calendar_field)
        if not isinstance(value, str) or not value.strip():
            raise HistoricalDataError(f"DataAssetRef.coverage.{calendar_field}必须为非空字符串")
    return sessions


def _calendar_coverage_end(coverage: Mapping[str, Any]) -> pd.Timestamp:
    """日历覆盖终点不得越过CSV实际声明的最后交易session。"""
    value = coverage.get("calendar_coverage_end", coverage.get("date_end", coverage.get("end")))
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HistoricalDataError("DataAssetRef.coverage必须声明YYYY-MM-DD日历覆盖终点")
    try:
        end = pd.Timestamp(pd.to_datetime(value, format="%Y-%m-%d", errors="raise"))
    except (TypeError, ValueError) as error:
        raise HistoricalDataError("DataAssetRef.coverage日历覆盖终点无效") from error
    sessions = _trading_sessions(coverage)
    if end > sessions[-1]:
        raise HistoricalDataError("DataAssetRef.coverage日历覆盖终点不得晚于已声明的最后交易session")
    return end


def _frame_hash(data: pd.DataFrame) -> str:
    canonical = data.copy()
    canonical["date"] = canonical["date"].dt.strftime("%Y-%m-%d")
    return sha256(canonical.to_csv(index=False, lineterminator="\n", float_format="%.12g").encode("utf-8")).hexdigest()


def _reference_fingerprint(reference: Mapping[str, Any]) -> str:
    return sha256(json.dumps(reference, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _is_opaque_storage_ref(value: str) -> bool:
    if value == "in_memory":
        return True
    return re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", value) is not None and not value.lower().startswith("file:")


def _is_china_index_asset(asset_id: str) -> bool:
    """本地裸CSV仅对可由标准代码确定的沪深指数声明close别名。"""
    return re.fullmatch(r"(?:000\d{3}\.SH|399\d{3}\.SZ)", asset_id) is not None


def _price_convention(data: pd.DataFrame, reference: Mapping[str, Any]) -> tuple[str, str, str | None, str | None]:
    """读取模块内HistoricalData或DataFetcher逐资产价格口径，不改写DataAssetRef。"""
    convention = reference.get("price_convention")
    if not isinstance(convention, Mapping):
        raise HistoricalDataError("DataAssetRef.price_convention必须为对象")
    has_adj_close = "adj_close" in data.columns
    if "contract_close_field" in convention:
        if convention.get("contract_close_field") != "close" or convention.get("contract_adjustment") != "unadjusted":
            raise HistoricalDataError("DataAssetRef必须声明合同结算为不复权close")
        hv_field, hv_adjustment = convention.get("hv_close_field"), convention.get("hv_adjustment")
        if has_adj_close and (hv_field != "adj_close" or hv_adjustment not in {"forward", "none"}):
            raise HistoricalDataError("DataAssetRef必须声明adj_close的HV复权口径")
        if not has_adj_close and (hv_field is not None or hv_adjustment is not None):
            raise HistoricalDataError("无adj_close时DataAssetRef不得声明HV价格字段")
        equal_prices = has_adj_close and data["close"].astype("float64").equals(data["adj_close"].astype("float64"))
        if equal_prices and convention.get("close_equals_adj_close") is not True:
            raise HistoricalDataError("close与adj_close相同仅限明确声明的指数等资产")
        return "close", "unadjusted", str(hv_field) if hv_field is not None else None, str(hv_adjustment) if hv_adjustment is not None else None

    by_asset = convention.get("field_adjustment_by_asset")
    market = convention.get("asset_market_conventions")
    assets = tuple(sorted(data["asset_id"].unique()))
    if not isinstance(by_asset, Mapping) or not isinstance(market, Mapping) or any(asset not in by_asset or asset not in market for asset in assets):
        raise HistoricalDataError("DataFetcher price_convention未逐资产声明价格与复权口径")
    if any(not isinstance(by_asset[asset], Mapping) or by_asset[asset].get("close") != "unadjusted" for asset in assets):
        raise HistoricalDataError("DataAssetRef必须声明合同结算为不复权close")
    if has_adj_close:
        adjustments = [by_asset[asset].get("adj_close") for asset in assets]
        allowed = {"forward_adjusted", "not_applicable_alias_of_raw"}
        if any(value not in allowed for value in adjustments):
            return "close", "unadjusted", None, None
        hv_adjustment = "forward" if set(adjustments) == {"forward_adjusted"} else "none" if set(adjustments) == {"not_applicable_alias_of_raw"} else "by_asset"
        return "close", "unadjusted", "adj_close", hv_adjustment
    if all(
        isinstance(market[asset], Mapping)
        and market[asset].get("asset_class") == "index"
        and market[asset].get("historical_return_field") == "close"
        for asset in assets
    ):
        return "close", "unadjusted", "close", "none"
    return "close", "unadjusted", None, None
