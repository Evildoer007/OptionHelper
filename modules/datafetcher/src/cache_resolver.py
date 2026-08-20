"""本机DataStore缓存索引与覆盖判定。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Mapping, Sequence

import pandas as pd

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .data_normalizer import DataNormalizationError, daily_content_hash
from .models import DataRequest
from .quality_validator import observed_edge_intervals
from .request_validator import cache_identity


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class CacheIndexError(RuntimeError):
    code = "cache_corrupt"


def _lock_for(root: Path) -> threading.RLock:
    key = str(root.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _process_lock(path: Path):
    """用标准库文件锁覆盖同机多Host进程，不引入第二套缓存服务。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
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
            yield
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
            with _process_lock(self.root / ".index.lock"):
                yield

    def _index(self) -> dict[str, Any]:
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

        lock = _lock_for(self.root / ".request-locks" / identity)
        with lock:
            with _process_lock(self.root / ".request-locks" / f"{identity}.lock"):
                yield

    def lookup(
        self,
        request: DataRequest,
        provider: str,
        *,
        tenant_id: str = "local",
        expected_trading_dates: Mapping[str, Sequence[str]] | None = None,
        latest_completed_date: str | None = None,
        data_asset_ref_key: str | None = None,
    ) -> CacheMatch:
        identity = cache_identity(request, provider, tenant_id=tenant_id)
        with self._index_guard():
            record = self._index()["entries"].get(identity)
            if not isinstance(record, dict):
                return self._miss(provider, identity, request)
            relative_path = record.get("data_path")
            if not isinstance(relative_path, str):
                return self._miss(provider, identity, request)
            path = (self.root / relative_path).resolve()
            if self.root.resolve() not in path.parents or not path.is_file():
                return self._miss(provider, identity, request)
            try:
                frame = pd.read_csv(path, dtype={"date": str, "asset_id": str})
                actual_hash = daily_content_hash(frame)
                expected_hash = record.get("content_hash")
                reference = record.get("data_asset_ref")
                references = record.get("data_asset_refs")
                if data_asset_ref_key is not None and isinstance(references, Mapping):
                    candidate = references.get(data_asset_ref_key)
                    reference = candidate if isinstance(candidate, Mapping) else None
            except (DataNormalizationError, KeyError, OSError, pd.errors.ParserError):
                return self._miss(provider, identity, request)
            if (
                not isinstance(expected_hash, str)
                or actual_hash != expected_hash
                or (reference is not None and (not isinstance(reference, Mapping) or reference.get("content_hash") != expected_hash))
            ):
                return self._miss(provider, identity, request)
            missing: dict[str, tuple[tuple[str, str], ...]] = {}
            for asset_id in request.asset_ids:
                if expected_trading_dates is None:
                    intervals = observed_edge_intervals(
                        frame,
                        asset_id,
                        request.start_date,
                        request.end_date,
                        latest_completed_date=latest_completed_date,
                    )
                else:
                    sessions = tuple(str(item) for item in expected_trading_dates.get(asset_id, ()))
                    observed = set(frame.loc[frame["asset_id"].astype(str).str.upper() == asset_id, "date"].astype(str))
                    missing_indexes = [index for index, session in enumerate(sessions) if session not in observed]
                    groups: list[tuple[str, str]] = []
                    for index in missing_indexes:
                        if not groups or index == 0 or sessions[index - 1] != groups[-1][1]:
                            groups.append((sessions[index], sessions[index]))
                        else:
                            groups[-1] = (groups[-1][0], sessions[index])
                    intervals = tuple(groups)
                if intervals:
                    missing[asset_id] = intervals
            complete = not missing
            metadata = dict(record)
            metadata["data_asset_ref"] = dict(reference) if isinstance(reference, Mapping) else None
            return CacheMatch(provider, identity, frame, metadata, complete, missing)

    def save(
        self,
        identity: str,
        frame: pd.DataFrame,
        metadata: Mapping[str, Any],
        *,
        data_asset_ref_key: str | None = None,
    ) -> None:
        content_hash = str(metadata["content_hash"])
        relative_path = f"assets/{content_hash}.csv"
        with self._index_guard():
            _atomic_write_csv(self.root / relative_path, frame)
            _atomic_write_json(self.root / "assets" / f"{content_hash}.json", dict(metadata))
            index = self._index()
            existing = index["entries"].get(identity)
            references: dict[str, Any] = {}
            if isinstance(existing, Mapping) and isinstance(existing.get("data_asset_refs"), Mapping):
                references.update(existing["data_asset_refs"])
            reference = metadata.get("data_asset_ref")
            if isinstance(reference, Mapping) and isinstance(reference.get("created_by"), str):
                reference_key = data_asset_ref_key or reference["created_by"]
                references[reference_key] = dict(reference)
            index["entries"][identity] = {
                "data_path": relative_path,
                **dict(metadata),
                "data_asset_refs": references,
            }
            _atomic_write_json(self.index_path, index)

    def list_assets(self, *, tenant_id: str, limit: int = 100) -> list[Mapping[str, Any]]:
        """列出本缓存索引登记的受控资产，不扫描数据目录。"""

        safe_limit = max(1, min(int(limit), 100))
        with self._index_guard():
            records = list(self._index()["entries"].values())
        assets: dict[str, Mapping[str, Any]] = {}
        for record in records:
            if not isinstance(record, Mapping) or record.get("tenant_id") != tenant_id:
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

    def find_asset(self, *, tenant_id: str, data_asset_id: str) -> Mapping[str, Any] | None:
        """由受控索引精确定位一项资产，不受页面列表展示上限影响。"""

        with self._index_guard():
            records = list(self._index()["entries"].values())
        for record in records:
            if not isinstance(record, Mapping) or record.get("tenant_id") != tenant_id:
                continue
            raw_references = record.get("data_asset_refs")
            references = raw_references.values() if isinstance(raw_references, Mapping) else (record.get("data_asset_ref"),)
            for reference in references:
                if isinstance(reference, Mapping) and reference.get("data_asset_id") == data_asset_id:
                    return {"data_asset_ref": dict(reference), "provider": record.get("provider"), "updated_at": record.get("updated_at")}
        return None
