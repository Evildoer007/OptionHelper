"""Frozen-resource launcher for the macOS App Host.

It starts the same AppServer used during development on a loopback ephemeral
port, then prints a machine-readable URL for the native WKWebView shell.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import stat
import sys
import tempfile
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


def configure_agent_runtime(resources: Path) -> Path | None:
    """Expose only a validated, bundled runtime executable to the App Host."""

    environment_key = "OPTIONHELPER_AGENT_RUNTIME_PATH"
    os.environ.pop(environment_key, None)
    executable_name = "optionhelper-agent-runtime.exe" if os.name == "nt" else "optionhelper-agent-runtime"
    candidate = resources / "agent-runtime" / executable_name
    manifest_path = resources / "app-manifest.json"
    runtime_manifest: dict[str, object] = {}
    if manifest_path.is_file():
        try:
            app_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            runtime_manifest = dict(app_manifest.get("agent_runtime", {}))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise RuntimeError("成品Agent运行时Manifest无效") from error
    executable = candidate.is_file() and (
        os.name == "nt" or bool(candidate.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    )
    if executable:
        expected_hash = str(runtime_manifest.get("sha256", "")).strip()
        actual_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if runtime_manifest.get("status") != "local_verified" or expected_hash != actual_hash:
            raise RuntimeError("成品Agent运行时未通过Manifest和哈希校验")
        os.environ[environment_key] = str(candidate.resolve())
        os.environ["OPTIONHELPER_AGENT_RUNTIME_MODE"] = "active"
        os.environ["OPTIONHELPER_AGENT_RUNTIME_VERSION"] = expected_hash[:12]
        return candidate.resolve()
    mode = os.environ.get("OPTIONHELPER_AGENT_RUNTIME_MODE", "disabled").strip().lower()
    if bool(getattr(sys, "frozen", False)) and mode in {"active", "shadow"}:
        raise RuntimeError("成品Agent运行时缺少Resources内置可执行文件")
    return None


def require_isolated_fixture_directory(runtime_root: Path) -> None:
    """Reject verification data outside an empty operating-system temp tree."""

    temporary_roots = {Path(tempfile.gettempdir()).resolve()}
    if os.name != "nt":
        temporary_roots.update({Path("/tmp").resolve(), Path("/private/tmp").resolve()})
    candidate = runtime_root.resolve()
    if not any(candidate == root or root in candidate.parents for root in temporary_roots):
        raise RuntimeError("验收Fixture必须使用系统临时目录")
    if candidate.exists() and any(candidate.iterdir()):
        raise RuntimeError("验收Fixture必须使用全新空目录")


def document_runtime_status() -> dict[str, object]:
    """Import and exercise the document runtime used by the frozen Capability."""

    import bs4
    import soupsieve
    from io import BytesIO
    from zipfile import ZipFile
    from lxml import etree
    from PIL import Image
    from pypdf import PdfReader

    from modules.designer.pdf_renderer import render_pdf, runtime_status
    from modules.designer.word_renderer import render_docx

    status: dict[str, object] = dict(runtime_status())
    if status.get("available") is not True:
        raise RuntimeError(f"PDF运行组件不可用：{status.get('message', '未知原因')}")
    markup = '<main><h1>运行组件验证</h1><p>OptionHelper document probe</p><div class="chart" id="runtime-chart"></div></main>'
    charts = {"runtime-chart": {
        "type": "line", "title": "Runtime chart", "x": ["A", "B", "C"],
        "series": [{"name": "Probe", "data": [1, 3, 2]}],
    }}
    word_document = render_docx(markup, chart_specs=charts)
    with ZipFile(BytesIO(word_document)) as archive:
        images = [name for name in archive.namelist() if name.startswith("word/media/")]
        if not images or "word/document.xml" not in archive.namelist():
            raise RuntimeError("Word运行组件未生成有效文档和图表。")
        for name in images:
            with Image.open(BytesIO(archive.read(name))) as image:
                image.verify()
    pdf_document = render_pdf(markup, chart_specs=charts)
    if not PdfReader(BytesIO(pdf_document)).pages:
        raise RuntimeError("PDF运行组件未生成有效页面。")
    status["word_runtime"] = {
        "available": True,
        "beautifulsoup4": bs4.__version__,
        "soupsieve": soupsieve.__version__,
        "lxml": ".".join(str(part) for part in etree.LXML_VERSION),
    }
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description="OptionHelper macOS App Host")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--resource-dir", type=Path)
    parser.add_argument("--compute-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--probe-compute-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--probe-pdf-runtime", action="store_true")
    parser.add_argument(
        "--verification-fixture",
        action="store_true",
        help="仅为成品App计算验收登记受控测试行情，不用于正常启动",
    )
    args = parser.parse_args()

    resources = (args.resource_dir or resource_root()).resolve()
    os.environ["OPTIONHELPER_RESOURCE_ROOT"] = str(resources)
    configure_resource_imports(resources)
    capability_root = resources / "capability" / "option-helper"
    # PyInstaller modules have no repository-relative __file__ at runtime.
    # Bind the staged immutable Capability explicitly so Runtime Bootstrap
    # remains in release mode when the App is launched from a mounted DMG.
    os.environ["OPTIONHELPER_CAPABILITY_ROOT"] = str(capability_root)
    if args.compute_worker:
        from backend.task_runtime.compute_worker import main as compute_worker_main

        return compute_worker_main()
    if args.probe_compute_worker:
        # Exercise schema dependencies in the frozen host, not only the worker.
        from jsonschema import FormatChecker, validate

        validate("https://example.org/研究", {"type": "string", "format": "iri"}, format_checker=FormatChecker())
        from backend.task_runtime.compute_process import probe_compute_worker

        print("OPTIONHELPER_COMPUTE_WORKER=" + json.dumps(probe_compute_worker(), ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    if args.probe_pdf_runtime:
        status = document_runtime_status()
        print("OPTIONHELPER_PDF_RUNTIME=" + json.dumps(status, ensure_ascii=False, sort_keys=True), flush=True)
        return 0
    if args.data_dir is None:
        parser.error("运行App必须提供--data-dir")
    runtime_root = args.data_dir.expanduser().resolve()
    if args.verification_fixture:
        require_isolated_fixture_directory(runtime_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    os.environ["OPTIONHELPER_RUNTIME_ROOT"] = str(runtime_root)
    configure_agent_runtime(resources)
    from backend.app_server import AppServer
    from backend.secrets.platform_provider import platform_secret_provider

    stopped = threading.Event()
    def stop(*_unused: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    app = AppServer(
        host=args.host,
        port=args.port,
        app_data_dir=args.data_dir,
        capability_root=capability_root,
        frontend_root=resources / "frontend",
        brand_assets_root=resources / "assets" / "icons",
        authentication_mode="local-development" if args.verification_fixture else "managed",
        # Model and iFind credentials live under the OptionHelper user-data
        # directory.  The native vault remains available for one-release
        # migration.
        secret_provider=platform_secret_provider(
            runtime_root.parent,
            legacy_app_data_root=runtime_root,
            enable_system_migration=not args.verification_fixture,
        ),
    )
    if args.verification_fixture:
        from backend.verification_fixture import install_compute_verification_fixture

        install_compute_verification_fixture(app)
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
