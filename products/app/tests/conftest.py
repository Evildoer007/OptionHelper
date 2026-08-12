from pathlib import Path
import sys


TEST_ROOT = Path(__file__).resolve().parent
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))

from capability_fixture import verified_capability_root


__all__ = ["verified_capability_root"]
