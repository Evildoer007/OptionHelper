#!/usr/bin/env python3
"""五个操作页面共用的本机Host入口。"""

from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
import importlib
import ipaddress
import json
from pathlib import Path
import sys


# Page hosting also imports the packaged runtime directly; keep the Skill tree
# immutable on every supported platform.
sys.dont_write_bytecode = True


ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from runtime.adapters.local_host import LocalHostError, LocalProjectLayout
from runtime.bootstrap import bootstrap_runtime, local_runtime_scope


PAGE_MODULES = ("datafetcher", "payoffer", "pricer", "backtester", "reporter")


class ModuleHostError(RuntimeError):
    pass


def page_catalog() -> dict[str, dict[str, str | bool]]:
    paths = bootstrap_runtime()
    result: dict[str, dict[str, str | bool]] = {}
    for module in PAGE_MODULES:
        page = paths.module_page_dir(module) / f"{module}.html"
        result[module] = {"page": str(page), "exists": page.is_file()}
    return result


def _is_loopback_host(host: str) -> bool:
    if host.rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def prepare_local_project_layout(project_root: str | Path | None) -> LocalProjectLayout:
    """Preflight the standalone Host's external writable roots exactly once."""

    if project_root is None:
        raise ModuleHostError("本机模块页面Host必须显式提供项目运行目录。")
    try:
        layout = LocalProjectLayout.create(skill_root=ROOT, project_root=project_root)
        layout.initialize()
    except LocalHostError as error:
        raise ModuleHostError(str(error)) from error
    return layout


def run_module_host(module: str, host: str, port: int, *, project_root: str | Path | None = None) -> None:
    if module not in PAGE_MODULES:
        raise ModuleHostError(f"{module}没有操作页面")
    if not _is_loopback_host(host):
        raise ModuleHostError("本机模块页面Host仅允许回环地址。")
    layout = prepare_local_project_layout(project_root)
    with local_runtime_scope(layout.data_root, layout.result_root):
        bootstrap_runtime()
        try:
            server = importlib.import_module(f"modules.{module}.server")
        except ImportError as error:
            raise ModuleHostError(f"{module}页面服务尚未接入统一Host") from error
        handler = getattr(server, "run_host", None)
        if not callable(handler):
            raise ModuleHostError(f"{module}未提供run_host(host, port)")
        service_module = importlib.import_module(handler.__module__)
        original_server = getattr(service_module, "ThreadingHTTPServer", None)
        if original_server is None:
            raise ModuleHostError(f"{module}的run_host未使用统一HTTP Server")
        setattr(service_module, "ThreadingHTTPServer", _reported_server_class(original_server, module))
        try:
            handler(host=host, port=port)
        finally:
            setattr(service_module, "ThreadingHTTPServer", original_server)


def _reported_server_class(server_type: type[ThreadingHTTPServer], module: str) -> type[ThreadingHTTPServer]:
    """在实际bind后输出地址，端口0永远不会泄露为请求值。"""

    class ReportedServer(server_type):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            bound_host, bound_port = self.server_address[:2]
            print(json.dumps({"module": module, "url": f"http://{bound_host}:{bound_port}"}, ensure_ascii=False), flush=True)

    return ReportedServer


def main() -> None:
    parser = argparse.ArgumentParser(description="OptionHelper模块页面Host")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--module", choices=PAGE_MODULES)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--project-root", help="项目外部运行目录，持有DataStore和ResultStore")
    args = parser.parse_args()
    if args.list:
        print(json.dumps(page_catalog(), ensure_ascii=False, indent=2))
        return
    if not args.module:
        parser.error("必须提供--module或--list")
    run_module_host(args.module, args.host, args.port, project_root=args.project_root)


if __name__ == "__main__":
    main()
