#!/usr/bin/env python3
"""Designer渲染器脚本入口，保持模块源代码不依赖旧assets路径。"""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT, PROJECT_ROOT / "core" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.designer.renderer import main


if __name__ == "__main__":
    main()
