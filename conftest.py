"""项目根pytest的唯一受控导入定位与收集规则。"""

from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parent
CORE_SRC = PROJECT_ROOT / "core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))


def pytest_ignore_collect(collection_path, config):
    return bool(re.search(r"\s\d+(?:\.[^.]+)+$", collection_path.name))
