#!/usr/bin/env python3
"""Run an end-to-end smoke test against a built OptionHelper.app bundle."""

from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import plistlib
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[3]
AGENT_RUNTIME_PACKAGING = ROOT / "packaging" / "app" / "agent_runtime"
import sys
if str(AGENT_RUNTIME_PACKAGING) not in sys.path:
    sys.path.insert(0, str(AGENT_RUNTIME_PACKAGING))
from build_runtime import (  # noqa: E402
    LOCAL_VERIFIED,
    RuntimeBuildError,
    probe_runtime_process,
    verify_staged_runtime,
)
from name_boundary import assert_agent_runtime_delivery_clean
APP_PACKAGING = ROOT / "packaging" / "app"
if str(APP_PACKAGING) not in sys.path:
    sys.path.insert(0, str(APP_PACKAGING))
from platform_payload import (  # noqa: E402
    MACOS_ALLOWED_RESOURCE_ENTRIES,
    MACOS_BUILD_INPUTS,
    MACOS_PAYLOAD_ROOTS,
    MACOS_SOURCE_MAPPINGS,
    MACOS_STRICT_DIRECTORY_PAYLOADS,
    PlatformPayloadError,
    assert_outer_resource_layout,
    read_packaged_verification_fixture_identity,
    verify_outer_payload_manifest,
)
from python_runtime_licenses import (  # noqa: E402
    PythonRuntimeLicenseError,
    verify_python_runtime_licenses,
)


class AppVerificationError(RuntimeError):
    pass


# The frozen numerical runtime may compile its first Numba kernel during the
# 6.2 path-dependent pricing probe.  Keep this artifact-level timeout longer
# than ordinary UI requests so a cold machine is not reported as a broken DMG.
ARTIFACT_REQUEST_TIMEOUT_SECONDS = 120
REQUIRED_CAPABILITY_LICENSES = (
    "ReportLab-LICENSE.txt",
    "Pillow-LICENSE.txt",
    "pypdf-LICENSE.txt",
    "python-docx-LICENSE.txt",
    "openpyxl-LICENSE.txt",
)


def request(
    connection: http.client.HTTPConnection,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object] | str, str | None]:
    actual_headers = dict(headers or {})
    payload = None
    if body is not None:
        payload = json.dumps(body)
        actual_headers["Content-Type"] = "application/json"
    connection.request(method, path, body=payload, headers=actual_headers)
    response = connection.getresponse()
    raw = response.read().decode("utf-8")
    try:
        value: dict[str, object] | str = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        value = raw
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


def summarize_compute_result(
    module: str,
    result: dict[str, object],
    *,
    task_id: str,
) -> dict[str, object]:
    """Return only non-sensitive proof that one persisted calculator run exists."""
    status = result.get("status")
    require(status in {"succeeded", "partial"}, f"{module}未返回可验收状态：{status}")
    reference = result.get("module_run_ref")
    require(isinstance(reference, dict), f"{module}缺少ModuleRunRef")
    require(
        reference.get("module") == module and reference.get("task_id") == task_id,
        f"{module}的ModuleRunRef未绑定当前任务",
    )
    contract_fingerprint = result.get("contract_fingerprint")
    require(
        isinstance(contract_fingerprint, str)
        and len(contract_fingerprint) == 64,
        f"{module}缺少有效合同指纹",
    )
    if module == "payoffer":
        require(isinstance(result.get("payoff_semantic_hash"), str) and result["payoff_semantic_hash"], "payoffer缺少收益结构语义结果哈希")
        expected_result = "payoff_semantic_hash"
    else:
        expected_result = {"pricer": "pricing", "backtester": "backtest"}[module]
        readable = result.get(expected_result)
        require(isinstance(readable, dict) and bool(readable), f"{module}没有非空可读结果")
    return {
        "status": status,
        "module_run_id": reference.get("run_id"),
        "result_section": expected_result,
    }


def run_operation(
    connection: http.client.HTTPConnection,
    module: str,
    task_id: str,
    payload: dict[str, object],
    headers: dict[str, str],
) -> dict[str, object]:
    """Submit one task-owned module run and wait for its persisted result."""

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
        f"{module}成品Operation提交失败：{body}",
    )
    operation_id = str(operation["operation_id"])
    deadline = time.monotonic() + ARTIFACT_REQUEST_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status, body, _ = request(
            connection,
            "GET",
            f"/api/tasks/{task_id}/operations/{operation_id}",
            headers={"Cookie": headers["Cookie"]},
        )
        operation = body.get("operation") if isinstance(body, dict) else None
        require(status == 200 and isinstance(operation, dict), f"{module}成品Operation状态无效：{body}")
        state = operation.get("state")
        if state == "succeeded":
            result = operation.get("result")
            require(isinstance(result, dict), f"{module}成品Operation没有结果对象")
            return result
        if state in {"failed", "cancelled", "interrupted"}:
            raise AppVerificationError(f"{module}成品Operation未完成：{operation}")
        time.sleep(0.05)
    raise AppVerificationError(f"{module}成品Operation在受控时限内未完成")


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


def verify_agent_runtime(resources: Path) -> dict[str, object]:
    """Verify the required Resources runtime and its local proof."""

    manifest_path = resources / "app-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AppVerificationError("App Manifest不可解析，无法验证Agent运行时") from error
    summary = manifest.get("agent_runtime")
    if not isinstance(summary, dict):
        raise AppVerificationError("App Manifest缺少已验证Agent运行时")
    if summary.get("status") != LOCAL_VERIFIED or summary.get("support_status") != LOCAL_VERIFIED:
        raise AppVerificationError("App Manifest中的Agent运行时不是local_verified")
    try:
        verified = verify_staged_runtime(resources, "macos-arm64", summary)
        probe = probe_runtime_process(resources / str(verified["resource_path"]))
        if probe.get("status") != LOCAL_VERIFIED or probe.get("support_status") != LOCAL_VERIFIED:
            raise RuntimeBuildError("macOS Agent运行时探测未记录local_verified")
    except RuntimeBuildError as error:
        raise AppVerificationError(str(error)) from error
    return dict(verified)


def verify_delivery_metadata(
    bundle: Path,
    *,
    verify_sources: bool = True,
    repository_root: Path = ROOT,
) -> dict[str, object]:
    """Verify version, release state, legal payload, and development-file hygiene."""

    resources = bundle / "Contents" / "Resources"
    try:
        manifest = json.loads((resources / "app-manifest.json").read_text(encoding="utf-8"))
        with (bundle / "Contents" / "Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise AppVerificationError("App版本或发布记录不可解析") from error
    require(isinstance(manifest, dict), "App Manifest必须是对象")
    build_version = str(manifest.get("build_version", "")).removeprefix("v")
    require(bool(build_version), "App Manifest缺少构建版本")
    require(info.get("CFBundleShortVersionString") == build_version, "Info.plist版本与App构建版本不一致")
    formal = manifest.get("formal_release") is True
    expected_status = "formal_release" if formal else "local_candidate"
    require(manifest.get("release_status") == expected_status, "App Manifest发布状态无效")
    require(info.get("OptionHelperReleaseStatus") == expected_status, "Info.plist发布状态与App Manifest不一致")
    require(not (resources / "runtime").exists(), "App不得重复携带Capability外层runtime源码树")
    third_party = resources / "LICENSES" / "THIRD_PARTY.md"
    require(third_party.is_file(), "App缺少THIRD_PARTY.md")
    index = third_party.read_text(encoding="utf-8")
    for name in REQUIRED_CAPABILITY_LICENSES:
        source = repository_root / "LICENSES" / name
        packaged = resources / "LICENSES" / "capability" / name
        require(packaged.is_file(), f"App缺少必需第三方许可证：{name}")
        if verify_sources:
            require(source.is_file(), f"受控源码缺少必需第三方许可证：{name}")
            require(packaged.read_bytes() == source.read_bytes(), f"App第三方许可证哈希不一致：{name}")
        require(f"`capability/{name}`" in index, f"THIRD_PARTY.md未索引必需许可证：{name}")
    try:
        verify_outer_payload_manifest(
            repository_root,
            bundle,
            manifest.get("outer_payload"),
            source_mappings=MACOS_SOURCE_MAPPINGS,
            payload_roots=MACOS_PAYLOAD_ROOTS,
            build_inputs=MACOS_BUILD_INPUTS,
            strict_directories=MACOS_STRICT_DIRECTORY_PAYLOADS,
            verify_sources=verify_sources,
        )
        assert_outer_resource_layout(
            bundle,
            resources_relative="Contents/Resources",
            allowed_entries=MACOS_ALLOWED_RESOURCE_ENTRIES,
        )
        runtime_licenses = resources / "LICENSES" / "python-runtime"
        inventory = json.loads(
            (runtime_licenses / "python-runtime-license-manifest.json").read_text(encoding="utf-8")
        )
        verify_python_runtime_licenses(runtime_licenses, inventory)
    except (OSError, json.JSONDecodeError, PlatformPayloadError, PythonRuntimeLicenseError) as error:
        raise AppVerificationError(str(error)) from error
    signing = manifest.get("signing")
    require(isinstance(signing, dict), "App Manifest缺少签名记录")
    if formal:
        identity = str(signing.get("identity", "")).strip()
        require(
            signing.get("method") == "developer-id"
            and identity.startswith("Developer ID Application:"),
            "正式App缺少Developer ID签名身份",
        )
        require(
            signing.get("notarized") is True
            and signing.get("app_stapled") is False
            and signing.get("installer_stapled") is True
            and signing.get("gatekeeper") == "accepted",
            "正式App缺少公证、DMG staple或Gatekeeper成功记录",
        )
        try:
            details = subprocess.run(
                ["codesign", "--display", "--verbose=4", str(bundle)],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
            ).stdout
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            output = getattr(error, "stdout", "") or ""
            raise AppVerificationError(f"正式App签名检查失败：{output[-2000:]}") from error
        require("Signature=adhoc" not in details, "正式App不得使用ad-hoc签名")
        require(f"Authority={identity}" in details, "正式App Developer ID身份不一致")
        try:
            subprocess.run(
                ["spctl", "--assess", "--type", "execute", "--verbose=4", str(bundle)],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            output = getattr(error, "stdout", "") or ""
            raise AppVerificationError(f"正式App Gatekeeper检查失败：{output[-2000:]}") from error
    else:
        require(signing.get("method") == "ad-hoc", "本地候选必须明确记录ad-hoc签名")
    return manifest


def verify(
    bundle: Path,
    *,
    verify_sources: bool = True,
    repository_root: Path = ROOT,
) -> dict[str, object]:
    bundle = bundle.expanduser().resolve()
    resources = bundle / "Contents" / "Resources"
    try:
        fixture_principal_label = read_packaged_verification_fixture_identity(
            bundle,
            resources_relative="Contents/Resources",
        )
    except PlatformPayloadError as error:
        raise AppVerificationError(str(error)) from error
    backend = resources / "backend" / "OptionHelperBackend" / "OptionHelperBackend"
    require(backend.is_file(), "App缺少内置后端")
    try:
        assert_agent_runtime_delivery_clean(resources)
    except AssertionError as error:
        raise AppVerificationError(str(error)) from error
    manifest = verify_delivery_metadata(
        bundle,
        verify_sources=verify_sources,
        repository_root=repository_root,
    )
    runtime_summary = verify_agent_runtime(resources)
    subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(bundle)],
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
                "--verification-fixture",
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
            connection = http.client.HTTPConnection(
                parsed.hostname,
                parsed.port,
                timeout=ARTIFACT_REQUEST_TIMEOUT_SECONDS,
            )
            status, health, _ = request(connection, "GET", "/api/health")
            require(status == 200 and health.get("status") == "ok", "App健康检查失败")
            capability = health.get("capability", {})
            require(isinstance(capability, dict), "App健康检查缺少Capability")
            integrity = capability.get("integrity", {})
            require(isinstance(integrity, dict) and integrity.get("release_ready") is True, "内置Capability未通过完整性门禁")

            status, local_login, local_cookie = request(
                connection,
                "POST",
                "/api/auth/local",
                {"role": "admin", "principal_label": fixture_principal_label},
            )
            require(
                status == 200 and local_cookie is not None
                and isinstance(local_login.get("identity"), dict)
                and local_login["identity"].get("role") == "admin",
                "成品App本地管理员身份登录失败",
            )
            admin = local_cookie.split(";", 1)[0]
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

            status, task_body, _ = request(
                connection,
                "POST",
                "/api/tasks",
                {"subject": "成品App Pricer与Backtester受控计算验收"},
                {"Cookie": admin},
            )
            task = task_body.get("task")
            require(status == 201 and isinstance(task, dict) and isinstance(task.get("task_id"), str), "计算验收任务创建失败")
            task_id = task["task_id"]
            underlyings = ["000905.SH"]
            base_input = {
                "product_id": "1.1",
                "identity": {"underlyings": underlyings},
                "term_overrides": {"K": 100.0, "T": 1.0, "Pi_0": 0.0},
                "task_id": task_id,
            }
            compute_requests = {
                "payoffer": {
                    "product_id": base_input["product_id"],
                    "underlyings": list(underlyings),
                    "term_overrides": dict(base_input["term_overrides"]),
                    "task_id": task_id,
                },
                "backtester": {
                    **base_input,
                    # 回测合同的起始参考价必须由已绑定历史的实际收盘价冻结；
                    # 不沿用Payoffer预览用的展示参考价100。
                    "identity": {"underlyings": underlyings},
                    "backtest_config": {"entry_rule": "explicit", "entry_dates": ["2022-01-04"]},
                },
                "pricer": {
                    **base_input,
                    "pricing_config": {
                        "valuation_date": "2022-02-01",
                        "spot": 100.0,
                        "historical_volatility": 0.20,
                        "dividend_yield": 0.0,
                        "risk_free_rate": 0.02,
                        "model_method": "analytical",
                    },
                },
            }
            compute_status: dict[str, object] = {}
            # Backtester establishes the first data-backed contract.  Payoffer
            # is then also run formally against that same contract, and its
            # subsequent scoped preview verifies the Desk display path.
            for sequence, module in enumerate(("backtester", "pricer", "payoffer"), start=20):
                status, context_body, _ = request(
                    connection,
                    "GET",
                    f"/api/module-host/{module}?task_id={task_id}",
                    headers={"Cookie": admin},
                )
                context = context_body.get("context")
                require(status == 200 and isinstance(context, dict), f"{module}计算缺少Module Host Context")
                headers = {
                    "Cookie": admin,
                    "Origin": url,
                    "X-OptionHelper-Module-Context": json.dumps(context),
                    "X-OptionHelper-Request-Id": f"artifact-compute-{module}-{sequence}",
                }
                result = run_operation(connection, module, task_id, compute_requests[module], headers)
                compute_status[module] = summarize_compute_result(module, result, task_id=task_id)

            status, context_body, _ = request(
                connection,
                "GET",
                f"/api/module-host/payoffer?task_id={task_id}",
                headers={"Cookie": admin},
            )
            context = context_body.get("context")
            require(status == 200 and isinstance(context, dict), "已绑定任务的payoffer预览缺少Module Host Context")
            require(context.get("task_id") == task_id, "已绑定payoffer预览上下文未绑定当前任务")
            contract_ref = context.get("contract_ref")
            contract_fingerprint = context.get("contract_fingerprint")
            require(isinstance(contract_ref, dict), "已绑定payoffer预览缺少合同引用")
            require(
                isinstance(contract_fingerprint, str)
                and len(contract_fingerprint) == 64
                and contract_ref.get("content_hash") == contract_fingerprint,
                "已绑定payoffer预览合同引用与指纹不一致",
            )
            headers = {
                "Cookie": admin,
                "Origin": url,
                "X-OptionHelper-Module-Context": json.dumps(context),
                "X-OptionHelper-Request-Id": "artifact-payoffer-bound-catalog",
            }
            status, tool_body, _ = request(
                connection, "POST", "/api/tools/payoffer", {"action": "catalog"}, headers,
            )
            catalog = tool_body.get("result") if isinstance(tool_body, dict) else None
            task_contract = catalog.get("task_contract") if isinstance(catalog, dict) else None
            require(
                status == 200 and isinstance(task_contract, dict)
                and task_contract.get("product_id") == base_input["product_id"]
                and isinstance(task_contract.get("identity"), dict)
                and task_contract.get("contract_fingerprint") == contract_fingerprint,
                f"payoffer目录未返回当前任务合同：{tool_body}",
            )
            task_identity = task_contract["identity"]
            task_underlyings = task_identity.get("underlyings")
            require(
                isinstance(task_underlyings, list)
                and bool(task_underlyings)
                and all(isinstance(item, str) and item.strip() for item in task_underlyings),
                "已绑定payoffer预览的任务合同缺少有效标的",
            )

            # Module Host contexts are single-use. Fetch a fresh context after
            # reading the task contract. The page submits only editable fields;
            # the Host reuses and validates the task-bound contract identity.
            status, context_body, _ = request(
                connection,
                "GET",
                f"/api/module-host/payoffer?task_id={task_id}",
                headers={"Cookie": admin},
            )
            context = context_body.get("context")
            require(
                status == 200 and isinstance(context, dict)
                and context.get("task_id") == task_id
                and context.get("contract_fingerprint") == contract_fingerprint
                and isinstance(context.get("contract_ref"), dict)
                and context["contract_ref"].get("content_hash") == contract_fingerprint,
                "payoffer预览未取得最新任务合同上下文",
            )
            scoped_preview = {
                "action": "preview",
                "product_id": str(task_contract["product_id"]),
                "underlyings": list(task_underlyings),
                "term_overrides": dict(base_input["term_overrides"]),
                "task_id": task_id,
            }
            for field in ("analysis_case_id", "candidate_id", "catalog_version", "contract_fingerprint"):
                if context.get(field) is not None:
                    scoped_preview[field] = context[field]
            headers = {
                "Cookie": admin,
                "Origin": url,
                "X-OptionHelper-Module-Context": json.dumps(context),
                "X-OptionHelper-Request-Id": "artifact-payoffer-bound-preview",
            }
            status, tool_body, _ = request(connection, "POST", "/api/tools/payoffer", scoped_preview, headers)
            preview = tool_body.get("result")
            require(
                status == 200 and isinstance(preview, dict)
                and isinstance(preview.get("preview"), dict)
                and isinstance(preview.get("svg"), str)
                and preview["svg"].lstrip().startswith("<svg"),
                f"已绑定任务的payoffer预览失败：{tool_body}",
            )

            # Airbag 6.2 has a daily observation schedule.  This is the
            # regression case for the first-run calendar preparation path:
            # its formal contract must be compiled only after the Host has
            # provided a verified trading calendar.
            status, path_task_body, _ = request(
                connection,
                "POST",
                "/api/tasks",
                {"subject": "成品App 6.2 Airbag路径定价验收"},
                {"Cookie": admin},
            )
            path_task = path_task_body.get("task")
            require(
                status == 201 and isinstance(path_task, dict)
                and isinstance(path_task.get("task_id"), str),
                "6.2路径定价验收任务创建失败",
            )
            path_task_id = path_task["task_id"]
            status, context_body, _ = request(
                connection,
                "GET",
                f"/api/module-host/pricer?task_id={path_task_id}",
                headers={"Cookie": admin},
            )
            context = context_body.get("context")
            require(status == 200 and isinstance(context, dict), "6.2定价缺少Module Host Context")
            headers = {
                "Cookie": admin,
                "Origin": url,
                "X-OptionHelper-Module-Context": json.dumps(context),
                "X-OptionHelper-Request-Id": "artifact-pricer-6-2",
            }
            path_pricer_result = run_operation(
                connection,
                "pricer",
                path_task_id,
                {
                    "product_id": "6.2",
                    "identity": {"underlyings": ["000905.SH"]},
                    "term_overrides": {},
                    "pricing_config": {
                        "valuation_date": "2022-02-01",
                        "spot": 100.0,
                        "historical_volatility": 0.20,
                        "dividend_yield": 0.0,
                        "risk_free_rate": 0.02,
                        "model_method": "monte_carlo",
                        # The release probe verifies the complete isolated
                        # process path, not pricing precision. Ten seeded
                        # paths keep the fixed smoke run bounded while still
                        # exercising the path-dependent engine and surfaces.
                        "path_count": 10,
                    },
                    "task_id": path_task_id,
                },
                headers,
            )
            path_data_refs = path_pricer_result.get("data_refs")
            require(
                isinstance(path_data_refs, list)
                and {ref.get("schema_id") for ref in path_data_refs if isinstance(ref, dict)}
                == {"market-history", "trading-calendar"},
                "6.2定价没有绑定市场历史与交易日历",
            )
            compute_status["pricer_6_2"] = summarize_compute_result(
                "pricer",
                path_pricer_result,
                task_id=path_task_id,
            )
            path_contract_fingerprint = path_pricer_result.get("contract_fingerprint")
            require(
                isinstance(path_contract_fingerprint, str)
                and len(path_contract_fingerprint) == 64,
                "6.2定价没有返回合同指纹",
            )

            # Reuse the data-backed 6.2 contract established by the formal
            # Pricer Operation. Payoffer must never compile this
            # path-dependent product from an unrelated empty task.
            status, context_body, _ = request(
                connection,
                "GET",
                f"/api/module-host/payoffer?task_id={path_task_id}",
                headers={"Cookie": admin},
            )
            context = context_body.get("context")
            require(
                status == 200 and isinstance(context, dict)
                and context.get("task_id") == path_task_id
                and isinstance(context.get("contract_ref"), dict)
                and isinstance(context.get("contract_fingerprint"), str)
                and context.get("contract_fingerprint") == path_contract_fingerprint
                and context["contract_ref"].get("content_hash") == context.get("contract_fingerprint"),
                "6.2收益结构未复用已冻结的定价任务合同",
            )
            headers = {
                "Cookie": admin,
                "Origin": url,
                "X-OptionHelper-Module-Context": json.dumps(context),
                "X-OptionHelper-Request-Id": "artifact-payoffer-6-2",
            }
            path_payoffer_result = run_operation(
                connection,
                "payoffer",
                path_task_id,
                {
                    "product_id": "6.2",
                    "underlyings": ["000905.SH"],
                    "term_overrides": {},
                    "task_id": path_task_id,
                },
                headers,
            )
            compute_status["payoffer_6_2"] = summarize_compute_result(
                "payoffer",
                path_payoffer_result,
                task_id=path_task_id,
            )
            require(
                path_payoffer_result.get("contract_fingerprint") == path_contract_fingerprint,
                "6.2收益结构结果未复用定价任务合同",
            )

            status, _body, _ = request(connection, "GET", "/optdesk", headers={"Cookie": admin})
            require(status == 200, "本地管理员身份无法访问OptDesk")

            status, _body, _ = request(
                connection,
                "POST",
                "/api/settings/model/credential",
                {
                    "provider_name": "openai-compatible",
                    "endpoint": "https://models.example.invalid/v1",
                    "model_name": "artifact-model",
                    "api_key": "artifact-model-key",
                },
                {"Cookie": admin},
            )
            require(status == 200, "成品App未能保存OptionHelper本地模型凭据")
            status, _body, _ = request(
                connection,
                "POST",
                "/api/settings/data/credential",
                {"provider_name": "ifind-http", "refresh_token": "artifact-ifind-refresh"},
                {"Cookie": admin},
            )
            require(status == 200, "成品App未能保存OptionHelper本地iFind凭据")
            credential_root = Path(temporary) / "credentials"
            credential_files = tuple(credential_root.glob("*.secret"))
            require(credential_root.is_dir(), "成品App未创建OptionHelper本地凭据目录")
            require(stat.S_IMODE(credential_root.stat().st_mode) == 0o700, "成品App凭据目录权限不是700")
            require(len(credential_files) >= 2, "成品App未生成模型和iFind本地凭据文件")
            require(
                all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in credential_files),
                "成品App凭据文件权限不是600",
            )
            settings_document = json.loads(
                (Path(temporary) / "state" / "settings.json").read_text(encoding="utf-8"),
            )
            settings_text = json.dumps(settings_document, ensure_ascii=False)
            require('"provider": "local-secret"' in settings_text, "成品App设置未保存local-secret引用")
            require('"provider": "keychain"' not in settings_text, "成品App设置仍写入钥匙串引用")
            require('"provider": "credential-manager"' not in settings_text, "成品App设置仍写入系统凭据引用")
            return {
                "status": "backend_api_verified",
                "native_interaction_acceptance": "not_run",
                "agent_runtime": runtime_summary,
                "capability_version": capability.get("capability_version"),
                "build_version": manifest.get("build_version"),
                "release_status": manifest.get("release_status"),
                "modules": tool_status,
                "compute_runs": compute_status,
                "local_admin_optdesk_status": status,
                "credential_storage": {
                    "provider": "local-secret",
                    "directory_mode": "700",
                    "file_mode": "600",
                    "credential_files": len(credential_files),
                },
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
    assert_outer_resource_layout,
