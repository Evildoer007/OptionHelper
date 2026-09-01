#!/usr/bin/env python3
"""Persist the explicitly selected build interpreter as local project state."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile


def persist(project_root: Path, python_path: Path) -> Path:
    root = project_root.expanduser().resolve()
    selected = python_path.expanduser()
    if not selected.is_absolute() or not selected.is_file():
        raise ValueError("Python解释器必须是存在的绝对文件路径")
    destination = root / ".optionhelper" / "runtime" / "build-python-path"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=destination.parent,
        prefix=".build-python-path-",
        delete=False,
    ) as handle:
        handle.write(str(selected.resolve()) + "\n")
        temporary = Path(handle.name)
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    args = parser.parse_args()
    print(persist(args.project_root, args.python))


if __name__ == "__main__":
    main()
