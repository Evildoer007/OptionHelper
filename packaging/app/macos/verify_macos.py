#!/usr/bin/env python3
"""Run an end-to-end smoke test against a built OptionHelper.app bundle."""

from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time
from urllib.parse import urlparse


class AppVerificationError(RuntimeError):
    pass


def request(
    connection: http.client.HTTPConnection,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object], str | None]:
    actual_headers = dict(headers or {})
    payload = None
    if body is not None:
        payload = json.dumps(body)
        actual_headers["Content-Type"] = "application/json"
    connection.request(method, path, body=payload, headers=actual_headers)
    response = connection.getresponse()
    raw = response.read().decode("utf-8")
    value = json.loads(raw) if raw else {}
    return response.status, value, response.getheader("Set-Cookie")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AppVerificationError(message)


def summarize_tool_result(result: dict[str, object]) -> dict[str, object]:
    """Keep artifact-verification evidence concise without weakening checks."""
    summary: dict[str, object] = {
        "ok": result.get("ok", True),
        "status": result.get("status", "ok"),
    }
    for key in ("products", "catalog", "items", "sources"):
        value = result.get(key)
        if isinstance(value, (list, dict)):
            summary[f"{key}_count"] = len(value)
    return summary


def wait_for_url(process: subprocess.Popen[str], timeout: float = 45.0) -> str:
    if process.stdout is None:
        raise AppVerificationError("App Host没有可读输出")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read()
            raise AppVerificationError(f"App Host提前退出：{output.strip()}")
        for key, _mask in selector.select(timeout=0.5):
            line = key.fileobj.readline()
            if line.startswith("OPTIONHELPER_URL="):
                return line.strip().split("=", 1)[1]
    raise AppVerificationError("App Host启动超时")


def verify(bundle: Path) -> dict[str, object]:
    bundle = bundle.expanduser().resolve()
    resources = bundle / "Contents" / "Resources"
    backend = resources / "backend" / "OptionHelperBackend" / "OptionHelperBackend"
    require(backend.is_file(), "App缺少内置后端")
    subprocess.run(
        ["codesign", "--verify", "--deep", "--verbose=2", str(bundle)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    with tempfile.TemporaryDirectory(prefix="optionhelper-app-verify-") as temporary:
        environment = dict(os.environ)
        for key in ("OPTIONHELPER_RUNTIME_ROOT", "OPTIONHELPER_DATA_ROOT", "OPTIONHELPER_RESULT_ROOT"):
            environment.pop(key, None)
        process = subprocess.Popen(
            [
                str(backend), "--host", "127.0.0.1", "--port", "0",
                "--data-dir", str(Path(temporary) / "state"), "--resource-dir", str(resources),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
            start_new_session=True,
        )
        connection: http.client.HTTPConnection | None = None
        try:
            url = wait_for_url(process)
            parsed = urlparse(url)
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=20)
            status, health, _ = request(connection, "GET", "/api/health")
            require(status == 200 and health.get("status") == "ok", "App健康检查失败")
            capability = health.get("capability", {})
            require(isinstance(capability, dict), "App健康检查缺少Capability")
            integrity = capability.get("integrity", {})
            require(isinstance(integrity, dict) and integrity.get("release_ready") is True, "内置Capability未通过完整性门禁")

            status, admin_body, admin_cookie = request(
                connection, "POST", "/api/auth/local", {"role": "admin", "principal_label": "artifact-verifier"}
            )
            require(status == 200 and admin_cookie is not None, "管理员本地登录失败")
            admin = admin_cookie.split(";", 1)[0]
            modules = ("datafetcher", "payoffer", "pricer", "backtester", "reporter")
            actions = {
                "datafetcher": "status",
                "payoffer": "catalog",
                "pricer": "catalog",
                "backtester": "catalog",
                "reporter": "status",
            }
            tool_status: dict[str, object] = {}
            for module in modules:
                status, context_body, _ = request(
                    connection, "GET", f"/api/module-host/{module}", headers={"Cookie": admin}
                )
                context = context_body.get("context")
                require(status == 200 and isinstance(context, dict), f"{module}缺少Module Host Context")
                headers = {
                    "Cookie": admin,
                    "Origin": url,
                    "X-OptionHelper-Module-Context": json.dumps(context),
                    "X-OptionHelper-Request-Id": f"artifact-{module}",
                }
                status, tool_body, _ = request(
                    connection, "POST", f"/api/tools/{module}", {"action": actions[module]}, headers
                )
                require(status == 200, f"{module}成品调用失败：{tool_body}")
                result = tool_body.get("result", {})
                require(isinstance(result, dict) and result.get("ok") is not False, f"{module}拒绝成品调用：{result}")
                tool_status[module] = summarize_tool_result(result)

            status, _sales_body, sales_cookie = request(
                connection, "POST", "/api/auth/local", {"role": "sales", "principal_label": "artifact-sales"}
            )
            require(status == 200 and sales_cookie is not None, "销售本地登录失败")
            sales = sales_cookie.split(";", 1)[0]
            status, _body, _ = request(connection, "GET", "/optdesk", headers={"Cookie": sales})
            require(status == 403, "销售权限错误：不应访问OptDesk")
            return {
                "status": "verified",
                "capability_version": capability.get("capability_version"),
                "modules": tool_status,
                "sales_optdesk_status": status,
            }
        finally:
            if connection is not None:
                connection.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description="验证已构建OptionHelper macOS App")
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.bundle), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
