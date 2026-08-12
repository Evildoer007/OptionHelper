"""Pricer module public-package boundary.

The importable implementation lives directly under ``src/``.  Test discovery
and runtime callers continue to use the stable public name ``modules.pricer``.
"""

from __future__ import annotations

from pathlib import Path


_WRAPPER_PACKAGE = Path(__file__).resolve().parent
_SOURCE_PACKAGE = _WRAPPER_PACKAGE / "src"
_SOURCE_INIT = _SOURCE_PACKAGE / "__init__.py"
if not _SOURCE_INIT.is_file():  # pragma: no cover - protects incomplete distributions
    raise ImportError(f"Pricer源码包不存在：{_SOURCE_INIT}")

# ``__path__`` must point at the sole formal package before executing its
# public surface; otherwise ``from .config`` would resolve against this
# repository wrapper and fail during unittest discovery.
__path__[:] = [str(_SOURCE_PACKAGE), str(_WRAPPER_PACKAGE)]
__file__ = str(_SOURCE_INIT)
exec(compile(_SOURCE_INIT.read_text(encoding="utf-8"), str(_SOURCE_INIT), "exec"), globals(), globals())
