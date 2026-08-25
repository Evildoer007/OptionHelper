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

ROOT = Path(__file__).resolve().parents[3]
AGENT_RUNTIME_PACKAGING = ROOT / "packaging" / "app" / "agent_runtime"
import sys
if str(AGENT_RUNTIME_PACKAGING) not in sys.path:
    sys.path.insert(0, str(AGENT_RUNTIME_PACKAGING))
from build_runtime import (  # noqa: E402
    RuntimeBuildError,
    probe_runtime_process,
    verify_staged_runtime,
)


class AppVerificationError(RuntimeError):
    pass


# The frozen numerical runtime may compile its first Numba kernel during the
# 6.2 path-dependent pricing probe.  Keep this artifact-level timeout longer
# than ordinary UI requests so a cold machine is not reported as a broken DMG.
ARTIFACT_REQUEST_TIMEOUT_SECONDS = 120


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
    """Verify the optional Resources runtime without claiming platform support."""

    manifest_path = resources / "app-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AppVerificationError("App Manifest不可解析，无法验证Agent运行时") from error
    summary = manifest.get("agent_runtime", {"status": "disabled"})
    if not isinstance(summary, dict):
        raise AppVerificationError("App Manifest中的Agent运行时字段无效")
    try:
        verified = verify_staged_runtime(resources, "macos-arm64", summary)
        if verified.get("status") == "local_verified":
            probe_runtime_process(resources / str(verified["resource_path"]))
    except RuntimeBuildError as error:
        raise AppVerificationError(str(error)) from error
    return dict(verified)


def verify(bundle: Path) -> dict[str, object]:
    bundle = bundle.expanduser().resolve()
    resources = bundle / "Contents" / "Resources"
    backend = resources / "backend" / "OptionHelperBackend" / "OptionHelperBackend"
    require(backend.is_file(), "App缺少内置后端")
    runtime_summary = verify_agent_runtime(resources)
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
                {"role": "admin", "principal_label": "artifact-verifier"},
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
            base_input = {
                "product_id": "1.1",
                "identity": {
                    "underlyings": ["000905.SH"],
                    "reference_prices": {"000905.SH": 100.0},
                },
                "term_overrides": {"K": 100.0, "T": 1.0, "Pi_0": 0.0},
                "task_id": task_id,
            }
            status, context_body, _ = request(
                connection,
                "GET",
                f"/api/module-host/payoffer?task_id={task_id}",
                headers={"Cookie": admin},
            )
            context = context_body.get("context")
            require(status == 200 and isinstance(context, dict), "payoffer预览缺少Module Host Context")
            headers = {
                "Cookie": admin,
                "Origin": url,
                "X-OptionHelper-Module-Context": json.dumps(context),
                "X-OptionHelper-Request-Id": "artifact-payoffer-preview",
            }
            status, tool_body, _ = request(
                connection,
                "POST",
                "/api/tools/payoffer",
                {"action": "preview", **base_input},
                headers,
            )
            preview = tool_body.get("result")
            require(
                status == 200 and isinstance(preview, dict)
                and isinstance(preview.get("preview"), dict)
                and isinstance(preview.get("svg"), str)
                and preview["svg"].lstrip().startswith("<svg"),
                f"payoffer成品预览失败：{tool_body}",
            )
            compute_requests = {
                "payoffer": base_input,
                "backtester": {
                    **base_input,
                    # 回测合同的起始参考价必须由已绑定历史的实际收盘价冻结；
                    # 不沿用Payoffer预览用的展示参考价100。
                    "identity": {"underlyings": ["000905.SH"]},
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
                        "model_method": "black_scholes",
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
                status, tool_body, _ = request(
                    connection,
                    "POST",
                    f"/api/tools/{module}",
                    compute_requests[module],
                    headers,
                )
                require(status == 200, f"{module}成品实际计算失败：{tool_body}")
                result = tool_body.get("result")
                require(isinstance(result, dict), f"{module}成品实际计算没有结果对象")
                compute_status[module] = summarize_compute_result(module, result, task_id=task_id)

            status, context_body, _ = request(
                connection,
                "GET",
                f"/api/module-host/payoffer?task_id={task_id}",
                headers={"Cookie": admin},
            )
            context = context_body.get("context")
            require(status == 200 and isinstance(context, dict), "已绑定任务的payoffer预览缺少Module Host Context")
            scoped_preview = {"action": "preview", **base_input}
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
            status, tool_body, _ = request(
                connection,
                "POST",
                "/api/tools/pricer",
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
                        "path_count": 100,
                    },
                    "task_id": path_task_id,
                },
                headers,
            )
            require(status == 200, f"6.2成品实际定价失败：{tool_body}")
            path_pricer_result = tool_body.get("result")
            require(isinstance(path_pricer_result, dict), "6.2成品实际定价没有结果对象")
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

            # Payoffer uses the same first-run contract compiler. Verify it
            # independently so an observation calendar cannot regress to a
            # Pricer-only App preparation path.
            status, payoff_task_body, _ = request(
                connection,
                "POST",
                "/api/tasks",
                {"subject": "成品App 6.2 Airbag收益结构验收"},
                {"Cookie": admin},
            )
            payoff_task = payoff_task_body.get("task")
            require(
                status == 201 and isinstance(payoff_task, dict)
                and isinstance(payoff_task.get("task_id"), str),
                "6.2收益结构验收任务创建失败",
            )
            payoff_task_id = payoff_task["task_id"]
            status, context_body, _ = request(
                connection,
                "GET",
                f"/api/module-host/payoffer?task_id={payoff_task_id}",
                headers={"Cookie": admin},
            )
            context = context_body.get("context")
            require(status == 200 and isinstance(context, dict), "6.2收益结构缺少Module Host Context")
            headers = {
                "Cookie": admin,
                "Origin": url,
                "X-OptionHelper-Module-Context": json.dumps(context),
                "X-OptionHelper-Request-Id": "artifact-payoffer-6-2",
            }
            status, tool_body, _ = request(
                connection,
                "POST",
                "/api/tools/payoffer",
                {
                    "product_id": "6.2",
                    "identity": {"underlyings": ["000905.SH"]},
                    "term_overrides": {},
                    "task_id": payoff_task_id,
                },
                headers,
            )
            require(status == 200, f"6.2成品收益结构失败：{tool_body}")
            path_payoffer_result = tool_body.get("result")
            require(isinstance(path_payoffer_result, dict), "6.2成品收益结构没有结果对象")
            compute_status["payoffer_6_2"] = summarize_compute_result(
                "payoffer",
                path_payoffer_result,
                task_id=payoff_task_id,
            )

            status, _body, _ = request(connection, "GET", "/optdesk", headers={"Cookie": admin})
            require(status == 200, "本地管理员身份无法访问OptDesk")
            return {
                "status": "verified",
                "agent_runtime": runtime_summary,
                "capability_version": capability.get("capability_version"),
                "modules": tool_status,
                "compute_runs": compute_status,
                "local_admin_optdesk_status": status,
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
