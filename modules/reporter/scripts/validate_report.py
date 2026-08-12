#!/usr/bin/env python3
"""校验Reporter v2交付物的冻结事实和文件完整性，不重算金融结果。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "reporter" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.reporter.artifact_validator import validate_report_run_directory
from modules.reporter.models import ReporterError


def validate(output_dir: Path) -> None:
    validation = validate_report_run_directory(output_dir)
    print(json.dumps({
        "ok": True,
        "output": str(validation["root"]),
        "status": validation["manifest"].get("status"),
    }, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate one Reporter v2 output directory.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        validate(args.output.resolve())
    except ReporterError as error:
        raise SystemExit(f"Reporter校验失败：{error}") from error


if __name__ == "__main__":
    main()
