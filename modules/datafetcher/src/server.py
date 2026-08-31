"""DataFetcher页面的薄HTTP适配器。"""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from runtime.adapters.local_store import StoreError

from .models import DataAssetRef
from .service import HOST, PAGE, PAGE_DIR, PORT, RUNTIME_PATHS, DataFetcherError, call_tool, read_data_asset


def _download_media(ref: DataAssetRef) -> tuple[str, str]:
    """由受控DataAssetRef决定下载媒体类型，页面不能指定扩展名。"""

    if ref.schema_id == "market-history" and ref.media_type == "text/csv":
        return "text/csv; charset=utf-8", ".csv"
    if ref.schema_id == "trading-calendar" and ref.media_type == "application/json":
        return "application/json; charset=utf-8", ".json"
    raise DataFetcherError("DataAsset媒体类型不支持下载")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: Any) -> None:
        # 默认HTTP日志可能含查询参数或用户输入，不写入本机日志。
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/status":
            return self._json(HTTPStatus.OK, call_tool({"action": "status"}))
        if self.path == "/api/assets":
            return self._json(HTTPStatus.OK, call_tool({"action": "list_assets"}))
        if self.path.startswith("/api/assets/") and self.path.endswith("/download"):
            return self._download(self.path.removeprefix("/api/assets/").removesuffix("/download").strip("/"))
        if self.path in {"/", "/datafetcher.html"}:
            return self._file(PAGE, "text/html; charset=utf-8")
        if self.path == "/icons/optionhelper-logo.svg":
            return self._file(RUNTIME_PATHS.project_root / "assets" / "icons" / "optionhelper-logo.svg", "image/svg+xml")
        if self.path == "/icons/optionhelper-app-icon-tile-light.svg":
            return self._file(RUNTIME_PATHS.project_root / "assets" / "icons" / "optionhelper-app-icon-tile-light.svg", "image/svg+xml")
        if self.path == "/datafetcher.css":
            return self._file(PAGE_DIR / "datafetcher.css", "text/css; charset=utf-8")
        if self.path == "/datafetcher.js":
            return self._file(PAGE_DIR / "datafetcher.js", "application/javascript; charset=utf-8")
        shared_browser_root = RUNTIME_PATHS.project_root / "core" / "src" / "runtime" / "browser"
        shared_resources = {
            "/module-host-presentation.css": (shared_browser_root / "module_host_presentation.css", "text/css; charset=utf-8"),
            "/module-host-presentation.js": (shared_browser_root / "module_host_presentation.js", "application/javascript; charset=utf-8"),
            "/module-host-bridge.js": (shared_browser_root / "module_host_bridge.js", "application/javascript; charset=utf-8"),
            "/date-input-control.js": (shared_browser_root / "date_input_control.js", "application/javascript; charset=utf-8"),
            "/date-input-control.css": (shared_browser_root / "date_input_control.css", "text/css; charset=utf-8"),
            "/designer/themes/designer-token-vars.css": (
                RUNTIME_PATHS.project_root / "modules" / "designer" / "assets" / "themes" / "designer-token-vars.css",
                "text/css; charset=utf-8",
            ),
        }
        if self.path in shared_resources:
            path, content_type = shared_resources[self.path]
            return self._file(path, content_type)
        return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "未找到资源"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/fetch":
            return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "未找到接口"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65_536:
                raise ValueError
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": "请求必须是小于64KB的JSON对象"})
        result = dict(call_tool({"action": "fetch", **payload}))
        return self._json(HTTPStatus.OK if result.get("ok") else HTTPStatus.UNPROCESSABLE_ENTITY, result)

    def _file(self, path, content_type: str) -> None:
        if not path.is_file():
            return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面文件不存在"})
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _download(self, asset_id: str) -> None:
        try:
            ref, body = read_data_asset(asset_id)
            content_type, extension = _download_media(ref)
        except (DataFetcherError, FileNotFoundError, PermissionError, StoreError):
            return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "DataAsset不存在或无访问权限"})
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{ref.data_asset_id}{extension}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_host(*, host: str = HOST, port: int = PORT) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
