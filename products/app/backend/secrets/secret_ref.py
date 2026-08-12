"""App compatibility import for the Core-owned opaque SecretRef."""

from pathlib import Path
import sys

_CORE_SRC = Path(__file__).resolve().parents[4] / "core" / "src"
if str(_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(_CORE_SRC))

from runtime.protocol.models import SecretRef

__all__ = ("SecretRef",)
