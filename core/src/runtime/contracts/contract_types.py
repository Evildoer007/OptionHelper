"""共享合同值对象使用的深度不可变容器与确定性序列化。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any, TypeVar


_K = TypeVar("_K")
_V = TypeVar("_V")


class FrozenDict(Mapping[_K, _V]):
    """保持Mapping兼容性的只读字典。

    ``dataclass(frozen=True)``只能阻止字段重新赋值，不能阻止嵌套字典被修改。
    ResolvedContract以本类型递归冻结identity、terms、term_sources和paths。
    """

    __slots__ = ("_data",)

    def __init__(self, values: Mapping[_K, _V] | None = None) -> None:
        self._data = MappingProxyType(dict(values or {}))

    def __getitem__(self, key: _K) -> _V:
        return self._data[key]

    def __iter__(self) -> Iterator[_K]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"FrozenDict({self._data!r})"

    def __deepcopy__(self, memo: dict[int, object]) -> "FrozenDict[_K, _V]":
        del memo
        return self


def deep_freeze(value: Any) -> Any:
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        return FrozenDict({key: deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(deep_freeze(item) for item in value)
    return value


def deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): deep_thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [deep_thaw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((deep_thaw(item) for item in value), key=repr)
    if hasattr(value, "item") and callable(value.item):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        deep_thaw(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def semantic_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


__all__ = ("FrozenDict", "canonical_json", "deep_freeze", "deep_thaw", "semantic_hash")
