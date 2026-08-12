"""Validated, deterministic random matrices for CPU Monte Carlo pricing."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class RandomMatrixInfo:
    path: str
    seed: int
    shape: tuple[int, int]
    dtype: str
    sha256: str


@dataclass(frozen=True, eq=False, init=False, slots=True)
class NpyRandomSource:
    """Read one fixed float64 ``.npy`` matrix without changing its order."""

    _array: np.ndarray
    info: RandomMatrixInfo

    def __init__(self, path: str | Path, seed: int) -> None:
        source_path = Path(path).expanduser().resolve()
        if not source_path.is_file():
            raise ValueError(f"随机数矩阵不存在：{source_path}")
        try:
            array = np.load(source_path, allow_pickle=False)
        except (OSError, ValueError) as exc:
            raise ValueError(f"无法读取随机数矩阵：{source_path}") from exc
        if array.ndim != 2:
            raise ValueError("随机数矩阵必须是二维数组")
        if array.dtype != np.dtype("float64"):
            raise ValueError("随机数矩阵必须使用float64")
        if not np.isfinite(array).all():
            raise ValueError("随机数矩阵必须全部为有限值")

        contiguous = np.ascontiguousarray(array)
        fixed = np.frombuffer(
            contiguous.tobytes(order="C"),
            dtype=contiguous.dtype,
        ).reshape(contiguous.shape)
        object.__setattr__(self, "_array", fixed)
        object.__setattr__(self, "info", RandomMatrixInfo(
            path=str(source_path),
            seed=int(seed),
            shape=(int(fixed.shape[0]), int(fixed.shape[1])),
            dtype=str(fixed.dtype),
            sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
        ))

    def load(self, paths: int, steps: int) -> np.ndarray:
        if not isinstance(paths, int) or isinstance(paths, bool) or paths <= 0:
            raise ValueError("paths必须为正整数")
        if not isinstance(steps, int) or isinstance(steps, bool) or steps < 0:
            raise ValueError("steps必须为非负整数")
        rows, columns = self.info.shape
        if paths > rows:
            raise ValueError(f"paths={paths}超过随机数矩阵路径上限{rows}")
        if steps > columns:
            raise ValueError(f"steps={steps}超过随机数矩阵期限上限{columns}")
        contiguous = np.ascontiguousarray(self._array[:paths, :steps])
        selected = np.frombuffer(
            contiguous.tobytes(order="C"),
            dtype=contiguous.dtype,
        ).reshape(contiguous.shape)
        return selected


def default_random_matrix_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "data"
        / "rand_normal.npy"
    )


@lru_cache(maxsize=1)
def default_random_source() -> NpyRandomSource:
    return NpyRandomSource(default_random_matrix_path(), seed=20240101)
