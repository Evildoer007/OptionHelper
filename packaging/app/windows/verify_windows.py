#!/usr/bin/env python3
"""Run the packaged Windows App acceptance matrix on a real Windows host."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
from queue import Empty, Queue
import subprocess
import tempfile
from threading import Thread
import time
from urllib.parse import urlencode, urlparse
import sys


HELPERS = Path(__file__).resolve().parent / "verification"
if not HELPERS.is_dir():
    HELPERS = Path(__file__).resolve().parents[1]
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))
from platform_payload import (  # noqa: E402
    WINDOWS_ALLOWED_RESOURCE_ENTRIES,
    WINDOWS_BUILD_INPUTS,
    WINDOWS_PAYLOAD_ROOTS,
    WINDOWS_SOURCE_MAPPINGS,
    WINDOWS_STRICT_DIRECTORY_PAYLOADS,
    PlatformPayloadError,
    assert_outer_resource_layout,
    read_packaged_verification_fixture_identity,
    verify_outer_payload_manifest,
)
from python_runtime_licenses import (  # noqa: E402
    PythonRuntimeLicenseError,
    verify_python_runtime_licenses,
)


REQUEST_TIMEOUT_SECONDS = 180
PROCESS_TIMEOUT_SECONDS = 900
PAGE_MODULES = ("datafetcher", "payoffer", "pricer", "backtester", "reporter")
REQUIRED_CAPABILITY_LICENSES = (
    "ReportLab-LICENSE.txt",
    "Pillow-LICENSE.txt",
    "pypdf-LICENSE.txt",
    "python-docx-LICENSE.txt",
    "openpyxl-LICENSE.txt",
)


class WindowsVerificationError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise WindowsVerificationError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def request(
    connection: http.client.HTTPConnection,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object], str | None]:
    values = dict(headers or {})
    payload = None
    if body is not None:
        payload = json.dumps(body)
        values["Content-Type"] = "application/json"
    connection.request(method, path, payload, values)
    response = connection.getresponse()
    raw = response.read().decode("utf-8")
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError as error:
        raise WindowsVerificationError(f"App返回非JSON响应：{path}") from error
    require(isinstance(parsed, dict), f"App响应不是对象：{path}")
    return response.status, parsed, response.getheader("Set-Cookie")


def wait_for_url(process: subprocess.Popen[str], timeout: float = 60.0) -> str:
    require(process.stdout is not None, "后端没有可读输出")
    output: Queue[str | None] = Queue()

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            output.put(line)
        output.put(None)

    Thread(target=read_output, daemon=True).start()
    deadline = time.monotonic() + timeout
    recent: list[str] = []
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            line = output.get(timeout=min(0.2, remaining))
        except Empty:
            if process.poll() is not None:
                raise WindowsVerificationError("后端提前退出：" + "".join(recent)[-1000:])
            continue
        if line is None:
            raise WindowsVerificationError("后端提前退出：" + "".join(recent)[-1000:])
        recent.append(line)
        recent = recent[-30:]
        if line.startswith("OPTIONHELPER_URL="):
            return line.strip().split("=", 1)[1]
    raise WindowsVerificationError("后端启动超时")


def hosted_headers(
    connection: http.client.HTTPConnection,
    base_url: str,
    module: str,
    cookie: str,
    sequence: int,
    task_id: str | None = None,
) -> dict[str, str]:
    suffix = "" if task_id is None else "?" + urlencode({"task_id": task_id})
    status, body, _ = request(
        connection,
        "GET",
        f"/api/module-host/{module}{suffix}",
        headers={"Cookie": cookie},
    )
    require(status == 200 and isinstance(body.get("context"), dict), f"{module}缺少Module Host Context")
    return {
        "Cookie": cookie,
        "Origin": base_url,
        "X-OptionHelper-Module-Context": json.dumps(body["context"]),
        "X-OptionHelper-Request-Id": f"windows-acceptance-{module}-{sequence}",
    }


def create_task(connection: http.client.HTTPConnection, cookie: str, subject: str) -> str:
    status, body, _ = request(
        connection,
        "POST",
        "/api/tasks",
        {"subject": subject},
        {"Cookie": cookie},
    )
    task = body.get("task")
    require(status == 201 and isinstance(task, dict) and isinstance(task.get("task_id"), str), "创建验收任务失败")
    return str(task["task_id"])


def run_operation(
    connection: http.client.HTTPConnection,
    module: str,
    task_id: str,
    payload: dict[str, object],
    headers: dict[str, str],
) -> dict[str, object]:
    """Submit a task-owned formal run and return its persisted result."""

    status, body, _ = request(
        connection,
        "POST",
        f"/api/tasks/{task_id}/operations",
        {"module": module, "action": "run", "payload": payload},
        headers,
    )
    operation = body.get("operation") if isinstance(body, dict) else None
    require(
        status == 202 and isinstance(operation, dict)
        and isinstance(operation.get("operation_id"), str),
        f"{module} Windows Operation提交失败：{body}",
    )
    operation_id = str(operation["operation_id"])
    deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status, body, _ = request(
            connection,
            "GET",
            f"/api/tasks/{task_id}/operations/{operation_id}",
            headers={"Cookie": headers["Cookie"]},
        )
        operation = body.get("operation") if isinstance(body, dict) else None
        require(status == 200 and isinstance(operation, dict), f"{module} Windows Operation状态无效：{body}")
        state = operation.get("state")
        if state == "succeeded":
            result = operation.get("result")
            require(isinstance(result, dict), f"{module} Windows Operation没有结果对象")
            return result
        if state in {"failed", "cancelled", "interrupted"}:
            raise WindowsVerificationError(f"{module} Windows Operation未完成：{operation}")
        time.sleep(0.05)
    raise WindowsVerificationError(f"{module} Windows Operation在受控时限内未完成")


def verify_layout(app: Path, *, source_root: Path | None = None) -> dict[str, object]:
    resources = app / "Resources"
    try:
        read_packaged_verification_fixture_identity(app, resources_relative="Resources")
    except PlatformPayloadError as error:
        raise WindowsVerificationError(str(error)) from error
    required = (
        app / "OptionHelper.exe",
        resources / "backend" / "OptionHelperBackend" / "OptionHelperBackend.exe",
        resources / "app-manifest.json",
        *(resources / "capability" / "option-helper" / "assets" / "pages" / module / f"{module}.html" for module in PAGE_MODULES),
    )
    missing = [str(path.relative_to(app)) for path in required if not path.is_file()]
    require(not missing, "Windows App缺少资源：" + "、".join(missing))
    require(
        not (resources / "app" / "backend").exists()
        and not (resources / "app" / "config").exists(),
        "Windows App不得携带冻结后端之外的原始backend/config源码",
    )
    require(not (resources / "runtime").exists(), "Windows App不得重复携带Capability外层runtime源码树")
    for name in REQUIRED_CAPABILITY_LICENSES:
        license_path = resources / "LICENSES" / "capability" / name
        require(license_path.is_file() and license_path.stat().st_size > 0, f"Windows App缺少必需第三方许可证：{name}")
    third_party = resources / "LICENSES" / "THIRD_PARTY.md"
    require(third_party.is_file(), "Windows App缺少THIRD_PARTY.md")
    require("`python-runtime/`" in third_party.read_text(encoding="utf-8"), "Windows THIRD_PARTY.md未索引冻结依赖许可证")
    for name in (
        "OptionHelper-Agent-Runtime-LICENSE.txt",
        "OptionHelper-Agent-Runtime-NOTICES.txt",
        "OptionHelper-Agent-Runtime-SOURCE-MAPPING.md",
    ):
        matches = list(resources.rglob(name))
        expected = resources / "LICENSES" / "agent-runtime" / name
        require(matches == [expected], f"Windows Agent Runtime发行声明必须唯一：{name}")
    manifest = json.loads((resources / "app-manifest.json").read_text(encoding="utf-8"))
    try:
        verify_outer_payload_manifest(
            source_root or app,
            app,
            manifest.get("outer_payload"),
            source_mappings=WINDOWS_SOURCE_MAPPINGS,
            payload_roots=WINDOWS_PAYLOAD_ROOTS,
            build_inputs=WINDOWS_BUILD_INPUTS,
            strict_directories=WINDOWS_STRICT_DIRECTORY_PAYLOADS,
            verify_sources=source_root is not None,
        )
        assert_outer_resource_layout(
            app,
            resources_relative="Resources",
            allowed_entries=WINDOWS_ALLOWED_RESOURCE_ENTRIES,
        )
        runtime_licenses = resources / "LICENSES" / "python-runtime"
        inventory = json.loads(
            (runtime_licenses / "python-runtime-license-manifest.json").read_text(encoding="utf-8")
        )
        verify_python_runtime_licenses(runtime_licenses, inventory)
    except (OSError, json.JSONDecodeError, PlatformPayloadError, PythonRuntimeLicenseError) as error:
        raise WindowsVerificationError(str(error)) from error
    formal = manifest.get("formal_release") is True
    expected_status = "formal_release" if formal else "local_candidate"
    require(manifest.get("release_status") == expected_status, "Windows App发布状态无效")
    signing = manifest.get("signing")
    require(isinstance(signing, dict), "Windows App Manifest缺少签名记录")
    if formal:
        thumbprint = str(signing.get("certificate_thumbprint", "")).strip().upper()
        require(
            signing.get("method") == "authenticode"
            and signing.get("status") == "valid"
            and len(thumbprint) == 40
            and all(character in "0123456789ABCDEF" for character in thumbprint),
            "Windows正式App缺少有效Authenticode记录",
        )
        script = (
            "$signature=Get-AuthenticodeSignature -LiteralPath $args[0];"
            "if($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate){exit 2};"
            "if($signature.SignerCertificate.Thumbprint -ne $args[1]){exit 3}"
        )
        for executable in (
            app / "OptionHelper.exe",
            resources / "backend" / "OptionHelperBackend" / "OptionHelperBackend.exe",
        ):
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script, str(executable), thumbprint],
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
            )
            require(completed.returncode == 0, f"Windows正式EXE Authenticode验证失败：{executable.name}")
    else:
        require(signing.get("method") == "unsigned", "Windows本地候选必须明确记录unsigned")
    runtime = manifest.get("agent_runtime")
    require(isinstance(runtime, dict) and runtime.get("status") == "local_verified", "Windows App缺少已验证Agent Runtime")
    runtime_path = resources / str(runtime.get("resource_path", ""))
    require(runtime_path.is_file() and sha256(runtime_path) == runtime.get("sha256"), "Windows Agent Runtime哈希不一致")
    return manifest


def verify_credential_acl(root: Path) -> dict[str, object]:
    files = tuple(root.glob("*.secret"))
    require(root.is_dir() and len(files) >= 2, "Windows凭据目录或凭据文件缺失")
    command = [
        "powershell", "-NoProfile", "-Command",
        "$items=@($args[0])+(Get-ChildItem -LiteralPath $args[0] -Filter *.secret).FullName;"
        "$items|ForEach-Object{(Get-Acl -LiteralPath $_).Access|"
        "Select-Object IdentityReference,FileSystemRights,AccessControlType}|ConvertTo-Json -Depth 4",
        str(root),
    ]
    try:
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
    except subprocess.TimeoutExpired as error:
        raise WindowsVerificationError("Windows凭据ACL检查超时") from error
    require(completed.returncode == 0, f"Windows凭据ACL检查失败：{completed.stdout}")
    lowered = completed.stdout.casefold()
    forbidden = ("everyone", "authenticated users", "builtin\\users", "所有人", "经过身份验证的用户")
    require(not any(name in lowered for name in forbidden), "Windows凭据ACL包含宽泛读取主体")
    return {"files": len(files), "acl_checked": True}


def verify_backend_api_on_windows(app: Path, *, source_root: Path | None = None) -> dict[str, object]:
    require(platform.system() == "Windows", "Windows后端/API验收只能在Windows运行")
    manifest = verify_layout(app, source_root=source_root)
    resources = app / "Resources"
    try:
        fixture_principal_label = read_packaged_verification_fixture_identity(
            app,
            resources_relative="Resources",
        )
    except PlatformPayloadError as error:
        raise WindowsVerificationError(str(error)) from error
    backend = resources / "backend" / "OptionHelperBackend" / "OptionHelperBackend.exe"
    subprocess.run(
        [str(app / "OptionHelper.exe"), "--check-webview2"],
        cwd=app,
        check=True,
        timeout=60,
    )
    with tempfile.TemporaryDirectory(prefix="optionhelper-windows-acceptance-") as temporary_name:
        temporary = Path(temporary_name)
        environment = dict(os.environ)
        for name in ("OPTIONHELPER_RUNTIME_ROOT", "OPTIONHELPER_DATA_ROOT", "OPTIONHELPER_RESULT_ROOT"):
            environment.pop(name, None)
        process = subprocess.Popen(
            [
                str(backend), "--host", "127.0.0.1", "--port", "0",
                "--data-dir", str(temporary / "state"), "--resource-dir", str(resources),
                "--verification-fixture",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
        connection: http.client.HTTPConnection | None = None
        try:
            base_url = wait_for_url(process)
            parsed = urlparse(base_url)
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=REQUEST_TIMEOUT_SECONDS)
            status, health, _ = request(connection, "GET", "/api/health")
            require(status == 200 and health.get("status") == "ok", "Windows后端健康检查失败")
            status, login, set_cookie = request(
                connection,
                "POST",
                "/api/auth/local",
                {"role": "admin", "principal_label": fixture_principal_label},
            )
            require(status == 200 and set_cookie is not None, "Windows本地管理员登录失败")
            cookie = set_cookie.split(";", 1)[0]

            page_status: dict[str, int] = {}
            actions = {"datafetcher": "status", "payoffer": "catalog", "pricer": "catalog", "backtester": "catalog", "reporter": "status"}
            catalogs: dict[str, list[dict[str, object]]] = {}
            for sequence, module in enumerate(PAGE_MODULES, start=1):
                headers = hosted_headers(connection, base_url, module, cookie, sequence)
                status, body, _ = request(connection, "POST", f"/api/tools/{module}", {"action": actions[module]}, headers)
                require(status == 200 and isinstance(body.get("result"), dict), f"{module}页面Host调用失败")
                page_status[module] = status
                products = body["result"].get("products")
                if isinstance(products, list):
                    catalogs[module] = products
            products = catalogs.get("pricer", [])
            require(len(products) == 65 and len({str(item.get("product_id")) for item in products}) == 65, "Pricer目录不是65个唯一产品")

            pricing_runs = 0
            for sequence, product in enumerate(products, start=100):
                product_id = str(product["product_id"])
                assets = ["000905.SH", "000300.SH"] if product.get("underlying_scope") == "multi_underlying" else ["000905.SH"]
                task_id = create_task(connection, cookie, f"Windows MC10 {product_id}")
                dividend: object = {asset: 0.0 for asset in assets} if len(assets) > 1 else 0.0
                payload = {
                    "action": "run",
                    "task_id": task_id,
                    "product_id": product_id,
                    "identity": {"underlyings": assets},
                    "term_overrides": {},
                    "pricing_config": {
                        "valuation_date": "2023-07-28",
                        "risk_free_rate": 0.02,
                        "dividend_yield": dividend,
                        "model_method": "monte_carlo",
                        "path_count": 10,
                    },
                }
                headers = hosted_headers(connection, base_url, "pricer", cookie, sequence, task_id)
                result = run_operation(connection, "pricer", task_id, payload, headers)
                require(
                    result.get("status") in {"succeeded", "partial"},
                    f"产品{product_id}的Windows MC10验收失败：{result}",
                )
                pricing = result.get("pricing")
                require(isinstance(pricing, dict) and pricing.get("path_count") == 10, f"产品{product_id}未按MC10运行")
                pricing_runs += 1

            report_task = create_task(connection, cookie, "Windows完整报告验收")
            backtest = run_operation(
                connection,
                "backtester",
                report_task,
                {
                    "product_id": "1.1",
                    "identity": {"underlyings": ["000905.SH"]},
                    "term_overrides": {"K": 100.0, "T": 1.0, "Pi_0": 0.0},
                    "backtest_config": {"entry_rule": "explicit", "entry_dates": ["2022-01-04"]},
                },
                hosted_headers(connection, base_url, "backtester", cookie, 899, report_task),
            )
            require(backtest.get("status") in {"succeeded", "partial"}, f"报告前置历史合同失败：{backtest}")
            payoffer_payload = {
                "product_id": "1.1",
                "term_overrides": {"K": 100.0, "Pi_0": 0.0},
            }
            payoffer = run_operation(
                connection, "payoffer", report_task, payoffer_payload,
                hosted_headers(connection, base_url, "payoffer", cookie, 900, report_task),
            )
            require(payoffer.get("status") in {"succeeded", "partial"}, f"报告前置收益结构失败：{payoffer}")
            status, report, _ = request(
                connection,
                "POST",
                f"/api/tasks/{report_task}/reports",
                {"kind": "report", "format": "html"},
                {"Cookie": cookie},
            )
            delivery = report.get("result") if isinstance(report, dict) else None
            require(status == 200 and isinstance(delivery, dict), f"Windows Reporter报告请求失败：{report}")
            require(delivery.get("status") in {"completed", "partial"}, "Windows Reporter报告生成失败")

            for path, payload in (
                ("/api/settings/model/credential", {"provider_name": "openai-compatible", "endpoint": "https://invalid.local/v1", "model_name": "acceptance", "api_key": "test-only"}),
                ("/api/settings/data/credential", {"provider_name": "ifind-http", "refresh_token": "test-only"}),
            ):
                status, _body, _ = request(connection, "POST", path, payload, {"Cookie": cookie})
                require(status == 200, "Windows本地凭据写入验收失败")
            acl = verify_credential_acl(temporary / "credentials")
            return {
                "status": "backend_api_verified",
                "native_interaction_acceptance": "not_run",
                "app_version": manifest.get("app_version"),
                "pages": page_status,
                "mc10_products": pricing_runs,
                "reporter": "verified",
                "credential_acl": acl,
            }
        finally:
            if connection is not None:
                connection.close()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description="Windows后端/API与静态包验收")
    parser.add_argument("app", type=Path)
    parser.add_argument("--source-root", type=Path)
    args = parser.parse_args()
    result = verify_backend_api_on_windows(args.app.resolve(), source_root=args.source_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
