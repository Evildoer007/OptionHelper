"""DataFetcher module public-package boundary."""

from __future__ import annotations

from pathlib import Path


_WRAPPER_PACKAGE = Path(__file__).resolve().parent
_SOURCE_PACKAGE = _WRAPPER_PACKAGE / "src"
_SOURCE_INIT = _SOURCE_PACKAGE / "__init__.py"
if not _SOURCE_INIT.is_file():
    raise ImportError(f"DataFetcher源码包不存在：{_SOURCE_INIT}")

__path__[:] = [str(_SOURCE_PACKAGE), str(_WRAPPER_PACKAGE)]
__file__ = str(_SOURCE_INIT)
exec(compile(_SOURCE_INIT.read_text(encoding="utf-8"), str(_SOURCE_INIT), "exec"), globals(), globals())
