"""Validated, deterministic random matrices for CPU Monte Carlo pricing."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path

import numpy as np


DEFAULT_RANDOM_SEED = 20240101
DEFAULT_RANDOM_SHAPE = (2000, 800)
DEFAULT_RANDOM_DTYPE = np.dtype("float64")


class FrozenRandomSourceError(ValueError):
    """The immutable default Monte Carlo random source cannot be verified."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RandomMatrixInfo:
    path: str
    seed: int | None
    origin: str
    shape: tuple[int, int]
    dtype: str
    sha256: str


@dataclass(frozen=True)
class FrozenRandomSourceSpec:
    path: Path
    sha256: str
    seed: int
    shape: tuple[int, int]
    dtype: str


_DEFAULT_SOURCE_CACHE: tuple[
    FrozenRandomSourceSpec,
    tuple[int, int, int, int, int],
    "NpyRandomSource",
] | None = None


@dataclass(frozen=True, eq=False, init=False, slots=True)
class NpyRandomSource:
    """Read one fixed float64 ``.npy`` matrix without changing its order."""

    _array: np.ndarray
    info: RandomMatrixInfo

    def __init__(
        self,
        path: str | Path,
        seed: int | None = None,
        *,
        origin: str = "external",
    ) -> None:
        source_path = Path(path).expanduser().resolve()
        if not source_path.is_file():
            raise ValueError(f"随机数矩阵不存在：{source_path}")
        try:
            array = np.load(source_path, allow_pickle=False)
        except (OSError, ValueError) as exc:
            raise ValueError(f"无法读取随机数矩阵：{source_path}") from exc
        if array.ndim != 2:
            raise ValueError("随机数矩阵必须是二维数组")
        if array.dtype != DEFAULT_RANDOM_DTYPE:
            raise ValueError("随机数矩阵必须使用float64")
        if not np.isfinite(array).all():
            raise ValueError("随机数矩阵必须全部为有限值")

        contiguous = np.ascontiguousarray(array)
        fixed = np.frombuffer(
            contiguous.tobytes(order="C"),
            dtype=contiguous.dtype,
        ).reshape(contiguous.shape)
        digest = _sha256(source_path)
        object.__setattr__(self, "_array", fixed)
        object.__setattr__(self, "info", RandomMatrixInfo(
            path=str(source_path),
            seed=seed,
            origin=origin,
            shape=(int(fixed.shape[0]), int(fixed.shape[1])),
            dtype=str(fixed.dtype),
            sha256=digest,
        ))

    def load(self, paths: int, steps: int) -> np.ndarray:
        if not isinstance(paths, int) or isinstance(paths, bool) or paths <= 0:
            raise ValueError("paths必须为正整数")
        if not isinstance(steps, int) or isinstance(steps, bool) or steps < 0:
            raise ValueError("steps必须为非负整数")
        rows, columns = self.info.shape
        if steps > columns:
            raise ValueError(f"steps={steps}超过随机数矩阵期限上限{columns}")
        if paths > rows:
            if self.info.seed is None:
                raise ValueError("随机数矩阵路径不足，且该随机源没有可复现seed，无法按请求路径数扩展")
            # Keep the frozen matrix as the prefix and extend only in memory.
            # RandomState fills C-order rows, so regenerating the declared
            # column width preserves every existing baseline draw exactly.
            extended = np.random.RandomState(self.info.seed).standard_normal((paths, columns))
            return np.ascontiguousarray(extended[:, :steps], dtype=DEFAULT_RANDOM_DTYPE)
        contiguous = np.ascontiguousarray(self._array[:paths, :steps])
        return np.frombuffer(
            contiguous.tobytes(order="C"),
            dtype=contiguous.dtype,
        ).reshape(contiguous.shape)

    def iter_batches(
        self,
        paths: int,
        steps: int,
        *,
        batch_size: int = 2048,
    ):
        """Yield deterministic row batches without materialising an expanded matrix.

        The frozen source is laid out as ``(path, 800 draws)``.  Expansion must
        therefore advance ``RandomState`` by complete 800-column rows even when
        a product consumes fewer draws.  This preserves the exact legacy prefix
        and seed semantics while bounding peak memory for large path counts.
        """
        if not isinstance(paths, int) or isinstance(paths, bool) or paths <= 0:
            raise ValueError("paths必须为正整数")
        if not isinstance(steps, int) or isinstance(steps, bool) or steps < 0:
            raise ValueError("steps必须为非负整数")
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
            raise ValueError("batch_size必须为正整数")
        rows, columns = self.info.shape
        if steps > columns:
            raise ValueError(f"steps={steps}超过随机数矩阵期限上限{columns}")
        if paths <= rows:
            for start in range(0, paths, batch_size):
                stop = min(paths, start + batch_size)
                yield self._array[start:stop, :steps]
            return
        if self.info.seed is None:
            raise ValueError("随机数矩阵路径不足，且该随机源没有可复现seed，无法按请求路径数扩展")
        generator = np.random.RandomState(self.info.seed)
        for start in range(0, paths, batch_size):
            count = min(batch_size, paths - start)
            complete_rows = generator.standard_normal((count, columns))
            yield np.ascontiguousarray(complete_rows[:, :steps], dtype=DEFAULT_RANDOM_DTYPE)

    @classmethod
    def generated(cls, seed: int) -> "NpyRandomSource":
        """Build the matrix for ``seed`` in memory without touching source assets."""
        if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= 0xFFFFFFFF:
            raise ValueError("seed必须是0至4294967295之间的整数")
        matrix = np.random.RandomState(seed).standard_normal(DEFAULT_RANDOM_SHAPE)
        if matrix.dtype != DEFAULT_RANDOM_DTYPE:
            matrix = matrix.astype(DEFAULT_RANDOM_DTYPE, copy=False)
        buffer = io.BytesIO()
        np.save(buffer, matrix, allow_pickle=False)
        digest = hashlib.sha256(buffer.getvalue()).hexdigest()
        source = object.__new__(cls)
        contiguous = np.ascontiguousarray(matrix)
        fixed = np.frombuffer(
            contiguous.tobytes(order="C"),
            dtype=contiguous.dtype,
        ).reshape(contiguous.shape)
        object.__setattr__(source, "_array", fixed)
        object.__setattr__(source, "info", RandomMatrixInfo(
            path=f"generated:seed:{seed}",
            seed=seed,
            origin="generated",
            shape=DEFAULT_RANDOM_SHAPE,
            dtype=str(DEFAULT_RANDOM_DTYPE),
            sha256=digest,
        ))
        return source


def default_random_matrix_path() -> Path:
    """Return the path declared by the frozen pricing-core baseline."""
    return _default_random_spec().path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pricing_core_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_random_spec() -> FrozenRandomSourceSpec:
    """Load the only permitted default random source from baseline_manifest."""
    manifest_path = _pricing_core_root() / "baseline_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise FrozenRandomSourceError(
            "baseline_manifest_invalid",
            f"固定随机源基线清单无法读取：{manifest_path}",
        ) from exc
    raw = manifest.get("random_source") if isinstance(manifest, dict) else None
    if not isinstance(raw, dict):
        raise FrozenRandomSourceError("baseline_manifest_invalid", "基线清单缺少random_source")

    relative_path = raw.get("path")
    sha256 = raw.get("sha256")
    seed = raw.get("seed")
    shape = raw.get("shape")
    dtype = raw.get("dtype")
    if (
        relative_path != "data/rand_normal.npy"
        or not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
        or not isinstance(seed, int)
        or isinstance(seed, bool)
        or not isinstance(shape, list)
        or len(shape) != 2
        or any(not isinstance(size, int) or isinstance(size, bool) or size <= 0 for size in shape)
        or not isinstance(dtype, str)
    ):
        raise FrozenRandomSourceError("baseline_manifest_invalid", "基线清单中的随机源规格无效")
    if seed != DEFAULT_RANDOM_SEED:
        raise FrozenRandomSourceError(
            "baseline_seed_invalid",
            f"基线随机源seed必须为{DEFAULT_RANDOM_SEED}",
        )
    if tuple(shape) != DEFAULT_RANDOM_SHAPE or dtype != str(DEFAULT_RANDOM_DTYPE):
        raise FrozenRandomSourceError("baseline_shape_invalid", "基线随机源规格与定价基线不一致")
    return FrozenRandomSourceSpec(
        path=_pricing_core_root() / relative_path,
        sha256=sha256,
        seed=seed,
        shape=(shape[0], shape[1]),
        dtype=dtype,
    )


def _default_matrix_signature(path: Path) -> tuple[int, int, int, int, int]:
    """Return the file identity that makes a verified in-process cache safe."""
    try:
        metadata = path.stat()
    except OSError as exc:
        raise FrozenRandomSourceError("default_matrix_missing", f"固定随机源不存在：{path}") from exc
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def clear_default_random_source_cache() -> None:
    """Clear in-memory sources; never changes the checked-in baseline asset."""
    global _DEFAULT_SOURCE_CACHE
    _DEFAULT_SOURCE_CACHE = None
    _generated_random_source.cache_clear()


@lru_cache(maxsize=2)
def _generated_random_source(seed: int) -> NpyRandomSource:
    return NpyRandomSource.generated(seed)


def default_random_source(seed: int = DEFAULT_RANDOM_SEED) -> NpyRandomSource:
    """Load the frozen baseline or generate the requested seed in memory."""
    if seed != DEFAULT_RANDOM_SEED:
        return _generated_random_source(seed)
    spec = _default_random_spec()
    signature = _default_matrix_signature(spec.path)
    global _DEFAULT_SOURCE_CACHE
    cached = _DEFAULT_SOURCE_CACHE
    if cached is not None and cached[0] == spec and cached[1] == signature:
        return cached[2]
    try:
        source = NpyRandomSource(spec.path, seed=spec.seed, origin="baseline")
    except ValueError as exc:
        raise FrozenRandomSourceError("default_matrix_invalid", str(exc)) from exc
    if (
        source.info.sha256 != spec.sha256
        or source.info.shape != spec.shape
        or source.info.dtype != spec.dtype
        or source.info.seed != spec.seed
    ):
        raise FrozenRandomSourceError(
            "default_matrix_mismatch",
            "固定随机源与baseline_manifest不一致，已拒绝运行",
        )
    _DEFAULT_SOURCE_CACHE = (spec, signature, source)
    return source
