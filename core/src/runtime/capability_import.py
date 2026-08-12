"""One process-wide lock for dynamic verified-Capability imports."""

from __future__ import annotations

import builtins
from threading import RLock


# Builtins is process-global even when the same App source is imported through
# both ``backend.*`` and ``products.app.backend.*``.  ``setdefault`` gives all
# aliases one RLock without retaining any import-path-specific module state.
CAPABILITY_IMPORT_LOCK = builtins.__dict__.setdefault("_optionhelper_capability_import_lock", RLock())
