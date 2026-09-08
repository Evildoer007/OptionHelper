#!/usr/bin/env python3
"""Run the local App against one verified, process-lifetime Capability."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading


ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "packaging" / "skill", ROOT / "packaging" / "app", ROOT / "products" / "app"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from build_app import AppBuildError, validated_capability
from build_capability import build_app_capability
from build_skill import build_skill, verify_source_snapshot
from verify_skill import probe_runtime, verify_skill
from verify_app import verify_app


class DevelopmentLaunchError(RuntimeError):
    pass


def build_development_capability(workspace: Path, catalog_version: str) -> Path:
    """Build a verified working-tree Capability without signing a release.

    ``catalog_version`` is retained in the launcher interface so callers do not
    need a second development command, but an unsigned development run must not
    depend on or create ``versions/<version>``.  The final release pipeline is
    the only workflow allowed to consume a signed knowledge source manifest.
    """

    del catalog_version
    skill = build_skill(workspace / "skill", candidate=True, repo_root=ROOT)
    errors = [*verify_skill(skill), *verify_source_snapshot(skill, repo_root=ROOT), *probe_runtime(skill)]
    if errors:
        raise DevelopmentLaunchError("开发Capability未通过验收：\n" + "\n".join(errors))
    try:
        capability = validated_capability(build_app_capability(workspace / "app-capability", skill, repo_root=ROOT))
    except AppBuildError as error:
        raise DevelopmentLaunchError(str(error)) from error
    app_errors = verify_app(ROOT / "products" / "app", capability_root=capability)
    if app_errors:
        raise DevelopmentLaunchError("App开发源未通过当次Capability验收：\n" + "\n".join(app_errors))
    return capability


def main() -> int:
    parser = argparse.ArgumentParser(description="以临时已验证Capability启动OptionHelper本机App")
    parser.add_argument("--catalog-version", default="development")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4181)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    if args.data_dir is None:
        from desktop.common.paths import AppPaths
        app_data_dir = AppPaths().current_user_data_dir() / "local-state"
    else:
        app_data_dir = args.data_dir.expanduser().resolve()
    app_data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["OPTIONHELPER_RUNTIME_ROOT"] = str(app_data_dir)

    with tempfile.TemporaryDirectory(prefix="optionhelper-dev-capability-") as temporary_name:
        capability = build_development_capability(Path(temporary_name), args.catalog_version)
        scripts_root = str((capability / "scripts").resolve())
        if scripts_root not in sys.path:
            sys.path.insert(0, scripts_root)
        sys.dont_write_bytecode = True
        from backend.app_server import AppServer

        app = AppServer(
            host=args.host,
            port=args.port,
            app_data_dir=app_data_dir,
            capability_root=capability,
            authentication_mode="local-development",
        )
        stopped = threading.Event()

        def stop(*_unused: object) -> None:
            stopped.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            print(f"OPTIONHELPER_CAPABILITY_ROOT={capability}", flush=True)
            print(f"OPTIONHELPER_URL={app.start_background()}", flush=True)
            while not stopped.wait(0.2):
                pass
        finally:
            app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
