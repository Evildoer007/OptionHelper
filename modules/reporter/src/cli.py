#!/usr/bin/env python3
"""Reporter命令行入口。

CLI可以读取本地JSON文件作为用户提交载体，但传给服务的仍是纯ReportRequest；
结果只通过受控LocalResultStore按显式ModuleRunRef解析。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from runtime.adapters.local_store import LocalResultStore
from runtime.bootstrap import bootstrap_runtime
from runtime.ports.module import ModulePort

from .config import default_report_output_root
from .models import ReporterError, read_json
from .service import run_report


MODULE_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_PATHS = bootstrap_runtime(MODULE_ROOT)


def run(
    request: dict[str, object],
    *,
    result_store_root: Path,
    designer_port: ModulePort,
    output_root: Path | None = None,
) -> dict[str, object]:
    """供受控脚本调用的显式引用入口，不接受任何Module结果目录。"""

    store = LocalResultStore(result_store_root)
    return run_report(request, result_store=store, designer_port=designer_port, output_root=output_root)


def main() -> None:
    parser = argparse.ArgumentParser(description="使用显式ModuleRunRef生成OptionHelper ReportRun。")
    parser.add_argument("--request", type=Path, required=True, help="ReportRequest v2 JSON文件")
    parser.add_argument("--result-store-root", type=Path, required=True, help="受控Core LocalResultStore根目录")
    parser.add_argument("--output-root", type=Path, default=default_report_output_root(RUNTIME_PATHS), help="报告输出根目录")
    args = parser.parse_args()
    del args
    raise SystemExit(
        "Reporter CLI必须由Module Host注入Designer ModulePort。请通过受控Host调用run(request, result_store_root=..., designer_port=...)；"
        "CLI不会直接导入Designer实现。"
    )


if __name__ == "__main__":
    main()
