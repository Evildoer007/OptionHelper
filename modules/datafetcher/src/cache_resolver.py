"""本机DataStore缓存索引与覆盖判定。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Mapping, Sequence
from uuid import uuid4

import pandas as pd

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .data_normalizer import DataNormalizationError, daily_content_hash, merge_daily_history
from .models import DataRequest
from .quality_validator import observed_edge_intervals
from .request_validator import cache_identity


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_VOLATILE_ENTRIES: dict[tuple[str, str], dict[str, Any]] = {}
_VOLATILE_ENTRIES_LOCK = threading.RLock()
_PROVIDER_COVERAGE_DECISIONS = {"cache_miss_fetched", "cache_extended"}


class CacheIndexError(RuntimeError):
    code = "cache_corrupt"


def _natural_dates(start_date: str, end_date: str) -> tuple[str, ...]:
    current = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    values: list[str] = []
    while current <= end:
        values.append(current.isoformat())
        current += timedelta(days=1)
    return tuple(values)


def _provider_confirmed_dates(
    references: Sequence[Any],
    provider: str,
) -> Mapping[str, frozenset[str]]:
    """从真实Provider调用形成的资产引用恢复已请求自然日覆盖。"""

    confirmed: dict[str, set[str]] = {}
    for reference in references:
        if not isinstance(reference, Mapping):
            continue
        lineage = reference.get("lineage")
        coverage = reference.get("coverage")
        asset_ids = reference.get("asset_ids")
        if (
            not isinstance(lineage, Mapping)
            or lineage.get("provider") != provider
            or lineage.get("cache_decision") not in _PROVIDER_COVERAGE_DECISIONS
            or not isinstance(coverage, Mapping)
            or isinstance(asset_ids, (str, bytes))
            or not isinstance(asset_ids, Sequence)
        ):
            continue
        start_date = coverage.get("requested_start_date")
        end_date = coverage.get("provider_confirmed_end_date", coverage.get("end_date"))
        if not isinstance(start_date, str) or not isinstance(end_date, str):
            continue
        try:
            dates = _natural_dates(start_date, end_date)
        except ValueError:
            continue
        for asset_id in asset_ids:
            if isinstance(asset_id, str) and asset_id:
                confirmed.setdefault(asset_id.upper(), set()).update(dates)
    return {asset_id: frozenset(dates) for asset_id, dates in confirmed.items()}


def _lock_for(root: Path) -> threading.RLock:
    key = str(root.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _temporary_lock_path(namespace: str, identity: str) -> Path:
    """Return a deterministic lock path outside every configured data root."""

    digest = hashlib.sha256(f"{namespace}:{identity}".encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / "optionhelper-datafetcher-locks" / f"{digest}.lock"


@contextmanager
def _process_lock(path: Path):
    """等待者核验锁文件身份，避免旧inode与新路径同时进入临界区。"""

    while True:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = path.open("a+b")
        except FileNotFoundError:
            # 另一个持锁者刚清理空锁目录，重新创建后再打开。
            continue
        with handle:
            if os.name == "nt":
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    current = path.stat()
                except FileNotFoundError:
                    continue
                opened = os.fstat(handle.fileno())
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    continue
                try:
                    yield
                finally:
                    # POSIX在解锁前移除名字。旧等待者取得锁后必须重新验证inode。
                    # Windows不能移除仍打开的锁文件，保留系统临时目录内的空锁。
                    if os.name != "nt":
                        path.unlink(missing_ok=True)
                        try:
                            path.parent.rmdir()
                        except OSError:
                            pass
                return
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        temporary = Path(handle.name)
    temporary.replace(path)


def _atomic_write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
        frame.to_csv(handle, index=False, lineterminator="\n")
        temporary = Path(handle.name)
    temporary.replace(path)


@dataclass(frozen=True)
class CacheMatch:
    provider: str
    identity: str
    frame: pd.DataFrame | None
    metadata: Mapping[str, Any] | None
    complete: bool
    missing_by_asset: Mapping[str, tuple[tuple[str, str], ...]]


class LocalCache:
    """一个本机缓存索引；数据版本以content_hash为文件名，旧资产不覆盖。"""

    def __init__(self, root: Path):
        self.root = root
        self.index_path = root / "index.json"
        self.assets_dir = root / "assets"
        self._lock = _lock_for(root)

    @contextmanager
    def _index_guard(self):
        with self._lock:
            with _process_lock(_temporary_lock_path("history-index", str(self.root.resolve()))):
                yield

    def _index(self, *, migrate: bool = True) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"schema": "optionhelper.data-cache-index", "entries": {}}
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CacheIndexError("DataFetcher缓存索引损坏，拒绝静默覆盖") from error
        if self._is_v1_index(value):
            value = {
                "schema": "optionhelper.data-cache-index",
                "entries": value["entries"],
            }
            if migrate:
                _atomic_write_json(self.index_path, value)
        if (
            not isinstance(value, dict)
            or value.get("schema") != "optionhelper.data-cache-index"
            or not isinstance(value.get("entries"), dict)
        ):
            raise CacheIndexError("DataFetcher缓存索引结构无效，拒绝静默覆盖")
        return value

    @staticmethod
    def _is_v1_index(value: Any) -> bool:
        """仅迁移本模块已发布过的v1索引，其他格式继续失败关闭。"""

        if not isinstance(value, dict) or set(value) != {"version", "entries"}:
            return False
        entries = value.get("entries")
        if value.get("version") != 1 or not isinstance(entries, dict):
            return False
        required = {"data_path", "content_hash", "provider", "tenant_id", "updated_at"}
        return all(
            isinstance(identity, str)
            and isinstance(record, dict)
            and required.issubset(record)
            and all(isinstance(record[field], str) for field in required)
            for identity, record in entries.items()
        )

    @staticmethod
    def _miss(provider: str, identity: str, request: DataRequest) -> CacheMatch:
        return CacheMatch(provider, identity, None, None, False, {asset: ((request.start_date, request.end_date),) for asset in request.asset_ids})

    @contextmanager
    def fetch_lock(self, identity: str):
        """按缓存身份串行同机请求，覆盖查找、Provider与写缓存整个周期。"""

        lock_key = f"{self.root.resolve()}:{identity}"
        process_path = _temporary_lock_path("history-request", lock_key)
        lock = _lock_for(process_path)
        with lock:
            with _process_lock(process_path):
                yield

    @staticmethod
    def _missing(
        frame: pd.DataFrame,
        request: DataRequest,
        *,
        expected_trading_dates: Mapping[str, Sequence[str]] | None,
        latest_completed_date: str | None,
        provider_confirmed_dates: Mapping[str, Sequence[str]] | None,
    ) -> dict[str, tuple[tuple[str, str], ...]]:
        missing: dict[str, tuple[tuple[str, str], ...]] = {}
        for asset_id in request.asset_ids:
            if expected_trading_dates is None and provider_confirmed_dates is not None:
                completed_end = min(request.end_date, latest_completed_date or request.end_date)
                intervals = missing_intervals(
                    _natural_dates(request.start_date, completed_end),
                    provider_confirmed_dates.get(asset_id, ()),
                )
            elif expected_trading_dates is None:
                intervals = observed_edge_intervals(
                    frame,
                    asset_id,
                    request.start_date,
                    request.end_date,
                    latest_completed_date=latest_completed_date,
                )
            else:
                sessions = tuple(str(item) for item in expected_trading_dates.get(asset_id, ()))
                observed = set(
                    frame.loc[frame["asset_id"].astype(str).str.upper() == asset_id, "date"].astype(str)
                )
                intervals = missing_intervals(sessions, observed)
            if intervals:
                missing[asset_id] = intervals
        return missing

    def _disk_record(self, identity: str) -> tuple[pd.DataFrame, dict[str, Any]] | None:
        if not self.index_path.exists():
            return None
        with self._index_guard():
            record = self._index(migrate=False)["entries"].get(identity)
            if not isinstance(record, dict):
                return None
            relative_path = record.get("data_path")
            if not isinstance(relative_path, str):
                return None
            path = (self.root / relative_path).resolve()
            if self.root.resolve() not in path.parents or not path.is_file():
                return None
            try:
                frame = pd.read_csv(path, dtype={"date": str, "asset_id": str})
                actual_hash = daily_content_hash(frame)
            except (DataNormalizationError, KeyError, OSError, pd.errors.ParserError):
                return None
            if actual_hash != record.get("content_hash"):
                return None
            return frame, dict(record)

    def _volatile_record(self, identity: str) -> tuple[pd.DataFrame, dict[str, Any]] | None:
        key = (str(self.root.resolve()), identity)
        with _VOLATILE_ENTRIES_LOCK:
            record = _VOLATILE_ENTRIES.get(key)
            if not isinstance(record, Mapping) or not isinstance(record.get("frame"), pd.DataFrame):
                return None
            return record["frame"].copy(deep=True), dict(record["metadata"])

    def lookup(
        self,
        request: DataRequest,
        provider: str,
        *,
        tenant_id: str = "local",
        principal_id: str = "local-user",
        expected_trading_dates: Mapping[str, Sequence[str]] | None = None,
        latest_completed_date: str | None = None,
        data_asset_ref_key: str | None = None,
    ) -> CacheMatch:
        identity = cache_identity(
            request,
            provider,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        disk = self._disk_record(identity)
        volatile = self._volatile_record(identity)
        if volatile is not None and "base_disk_revision" in volatile[1]:
            disk_revision = self._record_revision(disk)
            # volatile存放本次完整缓存快照；资料库在别的进程更新后，旧快照失效。
            records = [volatile] if disk is None or volatile[1]["base_disk_revision"] == disk_revision else [disk]
        else:
            records = [item for item in (disk, volatile) if item]
        if not records:
            return self._miss(provider, identity, request)
        try:
            frame = merge_daily_history(*(item[0] for item in records))
        except DataNormalizationError as error:
            raise CacheIndexError("进程内与资料库行情覆盖冲突，拒绝静默远程覆盖") from error
        references: dict[str, Any] = {}
        metadata: dict[str, Any] = {}
        for _frame, record in records:
            metadata.update(record)
            raw_references = record.get("data_asset_refs")
            if isinstance(raw_references, Mapping):
                references.update(raw_references)
            reference = record.get("data_asset_ref")
            if isinstance(reference, Mapping) and isinstance(reference.get("created_by"), str):
                references.setdefault(str(reference["created_by"]), dict(reference))
        selected = references.get(data_asset_ref_key) if data_asset_ref_key is not None else metadata.get("data_asset_ref")
        metadata["data_asset_refs"] = references
        metadata["data_asset_ref"] = dict(selected) if isinstance(selected, Mapping) else None
        confirmed_dates = None
        if provider != "local":
            confirmed_dates = self._confirmed_dates(metadata, references, provider)
            metadata["provider_confirmed_dates"] = {key: sorted(value) for key, value in confirmed_dates.items()}
        missing = self._missing(
            frame,
            request,
            expected_trading_dates=expected_trading_dates,
            latest_completed_date=latest_completed_date,
            provider_confirmed_dates=confirmed_dates,
        )
        return CacheMatch(provider, identity, frame, metadata, not missing, missing)

    @staticmethod
    def _record_revision(record: tuple[pd.DataFrame, Mapping[str, Any]] | None) -> str | None:
        if record is None:
            return None
        return str(record[1].get("cache_revision", record[1].get("content_hash", "")))

    @staticmethod
    def _confirmed_dates(metadata: Mapping[str, Any], references: Mapping[str, Any], provider: str) -> dict[str, set[str]]:
        confirmed = {key: set(value) for key, value in _provider_confirmed_dates(tuple(references.values()), provider).items()}
        stored = metadata.get("provider_confirmed_dates", {})
        if isinstance(stored, Mapping):
            for asset_id, values in stored.items():
                if isinstance(asset_id, str) and isinstance(values, (list, tuple)):
                    confirmed.setdefault(asset_id, set()).update(value for value in values if isinstance(value, str))
        return confirmed

    @classmethod
    def _saved_metadata(
        cls,
        metadata: Mapping[str, Any],
        existing: Mapping[str, Any],
        data_asset_ref_key: str | None,
        reset_coverage: bool,
    ) -> dict[str, Any]:
        references = {} if reset_coverage else dict(existing.get("data_asset_refs", {}))
        reference = metadata.get("data_asset_ref")
        if isinstance(reference, Mapping) and isinstance(reference.get("created_by"), str):
            references[data_asset_ref_key or str(reference["created_by"])] = dict(reference)
        confirmed = cls._confirmed_dates(metadata, references, str(metadata.get("provider", "")))
        return {
            **dict(metadata), "data_asset_refs": references,
            "provider_confirmed_dates": {key: sorted(value) for key, value in confirmed.items()},
            "cache_revision": uuid4().hex,
        }

    def save_volatile(
        self,
        identity: str,
        frame: pd.DataFrame,
        metadata: Mapping[str, Any],
        *,
        data_asset_ref_key: str | None = None,
        reset_coverage: bool = False,
    ) -> None:
        """Keep local-first coverage in memory without touching configured roots."""

        key = (str(self.root.resolve()), identity)
        disk_revision = self._record_revision(self._disk_record(identity))
        with _VOLATILE_ENTRIES_LOCK:
            existing = _VOLATILE_ENTRIES.get(key, {})
            existing_metadata = existing.get("metadata", {}) if isinstance(existing, Mapping) else {}
            if existing_metadata.get("base_disk_revision") != disk_revision:
                existing_metadata = {}
            _VOLATILE_ENTRIES[key] = {
                "frame": frame.copy(deep=True),
                "metadata": {
                    **self._saved_metadata(metadata, existing_metadata, data_asset_ref_key, reset_coverage),
                    "base_disk_revision": disk_revision,
                },
            }

    def save(
        self,
        identity: str,
        frame: pd.DataFrame,
        metadata: Mapping[str, Any],
        *,
        data_asset_ref_key: str | None = None,
        reset_coverage: bool = False,
    ) -> None:
        content_hash = str(metadata["content_hash"])
        relative_path = f"assets/{content_hash}.csv"
        with self._index_guard():
            _atomic_write_csv(self.root / relative_path, frame)
            _atomic_write_json(self.root / "assets" / f"{content_hash}.json", dict(metadata))
            index = self._index()
            existing = index["entries"].get(identity)
            index["entries"][identity] = {
                "data_path": relative_path,
                **self._saved_metadata(metadata, existing or {}, data_asset_ref_key, reset_coverage),
            }
            _atomic_write_json(self.index_path, index)
        with _VOLATILE_ENTRIES_LOCK:
            _VOLATILE_ENTRIES.pop((str(self.root.resolve()), identity), None)

    def list_assets(
        self,
        *,
        tenant_id: str,
        principal_id: str | None = None,
        limit: int = 100,
    ) -> list[Mapping[str, Any]]:
        """列出本缓存索引登记的受控资产，不扫描数据目录。"""

        safe_limit = max(1, min(int(limit), 100))
        with self._index_guard():
            records = list(self._index(migrate=False)["entries"].values())
        assets: dict[str, Mapping[str, Any]] = {}
        for record in records:
            if (
                not isinstance(record, Mapping)
                or record.get("tenant_id") != tenant_id
                or (principal_id is not None and record.get("principal_id") not in {None, principal_id})
            ):
                continue
            raw_references = record.get("data_asset_refs")
            references = raw_references.values() if isinstance(raw_references, Mapping) else (record.get("data_asset_ref"),)
            for reference in references:
                if not isinstance(reference, Mapping):
                    continue
                asset_id = reference.get("data_asset_id")
                if not isinstance(asset_id, str):
                    continue
                assets[asset_id] = {
                    "data_asset_ref": dict(reference),
                    "provider": record.get("provider"),
                    "updated_at": record.get("updated_at"),
                }
        return sorted(assets.values(), key=lambda item: str(item.get("updated_at", "")), reverse=True)[:safe_limit]

    def find_asset(
        self,
        *,
        tenant_id: str,
        data_asset_id: str,
        principal_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        """由受控索引精确定位一项资产，不受页面列表展示上限影响。"""

        with self._index_guard():
            records = list(self._index(migrate=False)["entries"].values())
        for record in records:
            if (
                not isinstance(record, Mapping)
                or record.get("tenant_id") != tenant_id
            ):
                continue
            raw_references = record.get("data_asset_refs")
            references = raw_references.values() if isinstance(raw_references, Mapping) else (record.get("data_asset_ref"),)
            for reference in references:
                if isinstance(reference, Mapping) and reference.get("data_asset_id") == data_asset_id:
                    return {"data_asset_ref": dict(reference), "provider": record.get("provider"), "updated_at": record.get("updated_at")}
        return None


def missing_intervals(
    expected_dates: Sequence[str],
    observed_dates: Sequence[str] | set[str],
) -> tuple[tuple[str, str], ...]:
    """Group exact missing dates using the ordering of the authoritative sequence."""

    observed = {str(item) for item in observed_dates}
    groups: list[tuple[str, str]] = []
    previous_missing_index: int | None = None
    for index, raw_date in enumerate(expected_dates):
        current = str(raw_date)
        if current in observed:
            previous_missing_index = None
            continue
        if previous_missing_index is not None and index == previous_missing_index + 1:
            groups[-1] = (groups[-1][0], current)
        else:
            groups.append((current, current))
        previous_missing_index = index
    return tuple(groups)
