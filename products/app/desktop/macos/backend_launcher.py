"""Frozen-resource launcher for the macOS App Host.

It starts the same AppServer used during development on a loopback ephemeral
port, then prints a machine-readable URL for the native WKWebView shell.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path


def resource_root() -> Path:
    """Return PyInstaller's immutable resource directory or the source root."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled)
    return Path(__file__).resolve().parents[2]


def configure_resource_imports(resources: Path) -> None:
    """Load shared runtime and module services only from the staged Capability."""
    for root in (resources, resources / "runtime", resources / "capability" / "option-helper" / "scripts"):
        value = str(root.resolve())
        if root.is_dir() and value not in sys.path:
            sys.path.insert(0, value)


def main() -> int:
    parser = argparse.ArgumentParser(description="OptionHelper macOS App Host")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--resource-dir", type=Path)
    parser.add_argument("--probe-pdf-runtime", action="store_true")
    args = parser.parse_args()

    resources = (args.resource_dir or resource_root()).resolve()
    configure_resource_imports(resources)
    if args.probe_pdf_runtime:
        from modules.designer.pdf_renderer import runtime_status

        status = runtime_status()
        if status.get("available") is not True:
            raise RuntimeError(f"PDF运行组件不可用：{status.get('message', '未知原因')}")
        print("OPTIONHELPER_PDF_RUNTIME=" + json.dumps(status, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    if args.data_dir is None:
        parser.error("运行App必须提供--data-dir")
    runtime_root = args.data_dir.expanduser().resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    os.environ["OPTIONHELPER_RUNTIME_ROOT"] = str(runtime_root)
    from backend.app_server import AppServer
    from backend.secrets.local_secret_store import LocalSecretStore
    from backend.secrets.secret_provider import SecretProvider

    stopped = threading.Event()

    def stop(*_unused: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    app = AppServer(
        host=args.host,
        port=args.port,
        app_data_dir=args.data_dir,
        capability_root=resources / "capability" / "option-helper",
        frontend_root=resources / "frontend",
        brand_assets_root=resources / "assets" / "icons",
        # Development builds intentionally use the loopback-only direct-entry
        # identity. Account provisioning is not part of the current workflow.
        authentication_mode="local-development",
        # This launcher is the loopback-only development App. Keep credential
        # values in its owner-only local store so repeated ad-hoc rebuilds do
        # not trigger Keychain ACL prompts. Settings, tasks and reports still
        # receive only opaque SecretRef metadata.
        secret_provider=SecretProvider({
            "local-secret": LocalSecretStore(runtime_root / "credentials"),
        }),
    )
    try:
        url = app.start_background()
        print(f"OPTIONHELPER_URL={url}", flush=True)
        while not stopped.wait(0.2):
            pass
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
