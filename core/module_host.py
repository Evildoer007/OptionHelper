#!/usr/bin/env python3
"""五个操作页面共用的本机Host入口。"""

from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
import importlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from runtime.bootstrap import bootstrap_runtime


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


def run_module_host(module: str, host: str, port: int) -> None:
    if module not in PAGE_MODULES:
        raise ModuleHostError(f"{module}没有操作页面")
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
    args = parser.parse_args()
    if args.list:
        print(json.dumps(page_catalog(), ensure_ascii=False, indent=2))
        return
    if not args.module:
        parser.error("必须提供--module或--list")
    run_module_host(args.module, args.host, args.port)


if __name__ == "__main__":
    main()
