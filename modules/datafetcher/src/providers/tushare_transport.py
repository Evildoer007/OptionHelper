"""Auditable HTTP transport. No SDK import, implicit fallback or CDN execution."""
from __future__ import annotations

from typing import Any
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
import json
import os
import platform
import re
import subprocess
import tempfile
import uuid
import pandas as pd
import requests

from .base import (ProviderAccountPermissionDenied, ProviderDeviceLimitExceeded,
                   ProviderError, ProviderInputError, ProviderQuotaExceeded,
                   ProviderUnauthorized, ProviderUnavailable)

ALLOWED_APIS = frozenset({"daily", "fund_daily", "index_daily", "adj_factor", "fund_adj", "trade_cal"})


def provider_failure(detail: str, http_status: int | None = None) -> ProviderError:
    """Classify reviewed upstream causes without echoing credentials or free text.

    Tinyshare also uses HTTP401 for a temporary cross-device lock. Its precise
    device, quota and permission messages take precedence over generic auth.
    """
    value = detail.lower()
    context = {"http_status": http_status}
    if http_status is not None and http_status >= 500:
        return ProviderUnavailable("数据服务暂不可用，请稍后重试", **context)
    if any(marker in value for marker in (
        "其他设备使用", "其它设备使用", "另一台设备", "another device", "other device",
    )):
        return ProviderDeviceLimitExceeded(
            "当前Token正在被其他设备使用，请暂停其他设备调用，等待服务端限制解除后重试",
            reason_code="tinyshare_device_busy", **context,
        )
    if http_status == 429 or any(marker in value for marker in (
        "频次", "频率", "每分钟", "quota", "rate limit", "额度",
    )):
        return ProviderQuotaExceeded("数据服务调用额度或频次受限", **context)
    if http_status == 403 or any(marker in value for marker in ("积分", "权限", "permission", "forbidden")):
        return ProviderAccountPermissionDenied("当前数据账号没有接口权限", **context)
    token_subject = any(marker in value for marker in ("token", "授权码", "认证", "鉴权", "authorization code"))
    invalid_state = any(marker in value for marker in (
        "无效", "失效", "过期", "错误", "invalid", "expired", "revoked", "incorrect",
    ))
    # Legacy client exceptions can identify the credential field without an
    # HTTP status; retain that reviewed classification after specific causes.
    legacy_token_diagnostic = http_status is None and re.search(r"\btoken\s*=", value) is not None
    if http_status == 401 or (token_subject and invalid_state) or legacy_token_diagnostic:
        return ProviderUnauthorized("Token验证失败，请检查所选服务与Token是否匹配", **context)
    return ProviderUnavailable("数据服务请求未完成，请检查网络与服务状态", **context)


class TushareHttpClient:
    def __init__(self, provider: str, token: str, timeout: int):
        self.provider = provider
        self._token = token
        self.timeout = timeout

    def _request(self, api: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if self.provider == "tushare":
            fields = params.pop("fields", "")
            return "https://api.tushare.pro", {"api_name": api, "token": self._token, "params": params, "fields": fields}
        root, prefix = tinyshare_route(self._token)
        return f"{root}/{prefix}/{api}", {
            "auth_code": self._token, "params": params, "fields": params.get("fields", ""),
            "version": 1030, "deviceId": tinyshare_device_id(),
        }

    def query(self, api: str, **params: Any) -> pd.DataFrame:
        if api not in ALLOWED_APIS:
            raise ProviderInputError("该数据接口尚未接入")
        url, payload = self._request(api, dict(params))
        try:
            response = requests.post(url, json=payload, timeout=self.timeout, allow_redirects=False)
        except requests.RequestException:
            raise ProviderUnavailable("数据服务连接失败") from None
        if 300 <= response.status_code < 400:
            raise ProviderUnavailable("数据服务返回重定向，已阻止转发凭据")
        try:
            result = response.json()
        except (ValueError, TypeError):
            raise ProviderUnavailable("数据服务返回非JSON内容") from None
        if not isinstance(result, dict):
            raise ProviderUnavailable("数据服务返回格式无效")
        ok = result.get("code") == 0 if self.provider == "tushare" else result.get("success") is True
        if response.status_code != 200 or not ok:
            raise provider_failure(str(result.get("msg", result.get("error", ""))), response.status_code)
        if result.get("cdn_url"):
            raise ProviderUnavailable("服务返回外部数据下载链接，当前连接不支持该返回格式", reason_code="unsupported_cdn_response")
        data = result.get("data")
        if isinstance(data, dict) and {"fields", "items"}.issubset(data):
            fields, items = data["fields"], data["items"]
            if not isinstance(fields, list) or not all(isinstance(item, str) for item in fields) or len(set(fields)) != len(fields) or not isinstance(items, list) or not all(isinstance(row, list) and len(row) == len(fields) for row in items):
                raise ProviderInputError("数据表字段与行结构不一致")
            return pd.DataFrame(items, columns=fields)
        if self.provider == "tinyshare" and isinstance(data, dict) and data and all(isinstance(column, list) for column in data.values()):
            if len({len(column) for column in data.values()}) != 1:
                raise ProviderInputError("数据表列长度不一致")
            return pd.DataFrame(data)
        if self.provider == "tinyshare" and isinstance(data, list) and all(isinstance(item, dict) for item in data):
            return pd.DataFrame(data)
        raise ProviderInputError("数据服务缺少可识别的数据表")


def make_client(provider: str, token: str, timeout: int) -> TushareHttpClient:
    if provider not in {"tinyshare", "tushare"}:
        raise ProviderInputError("未知数据服务")
    return TushareHttpClient(provider, token, timeout)


def tinyshare_route(token: str) -> tuple[str, str]:
    """Match the reviewed 0.1030.0 protocol; hosts cannot come from request input."""
    seed = ""
    if len(token) == 64:
        try:
            candidate = token[7 + int(token[6])]
            seed = candidate.lower() if candidate.isalpha() else ""
        except (ValueError, IndexError):
            pass
    root = {
        "t": "http://124.221.49.16:8080",
        "g": "http://124.221.23.4:8090",
    }.get(seed, "http://115.159.100.200:8080")
    return root, "api/tspure" if seed == "g" else "api/tushare"


def _windows_hardware_identity() -> list[str]:
    """Read the SDK's optional WMI fields using the OS, without Python WMI.

    Tinyshare0.1030.0 uses the first processor's ProcessorId followed by the
    first disk with a nonempty SerialNumber. CIM reads those same WMI classes;
    no hardware values are invented or copied from another device. The SDK
    permits either optional read to fail and hashes the remaining real facts.
    """
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = root / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
        "$cpu = $null; $disk = $null;"
        "try { $cpu = Get-CimInstance Win32_Processor -ErrorAction Stop | "
        "Select-Object -First 1 -ExpandProperty ProcessorId } catch {};"
        "try { $disk = Get-CimInstance Win32_DiskDrive -ErrorAction Stop | "
        "Where-Object { $_.SerialNumber } | Select-Object -First 1 -ExpandProperty SerialNumber } catch {};"
        "@{ processor_id = $cpu; disk_serial = $disk } | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8-sig", timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            return []
        hardware = json.loads(result.stdout)
        if not isinstance(hardware, dict):
            return []
        processor = hardware.get("processor_id")
        disk = hardware.get("disk_serial")
        return [value for value in (
            processor if isinstance(processor, str) else "",
            disk.strip() if isinstance(disk, str) else "",
        ) if value]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def _remember_device_identity(cache: Path, identity: str) -> None:
    """Persist only the derived SDK-compatible hash, never raw machine data."""
    temporary: Path | None = None
    try:
        cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache.parent,
                                         prefix=".device-id-", suffix=".tmp", delete=False) as target:
            temporary = Path(target.name)
            json.dump({"device_id": identity}, target)
        os.replace(temporary, cache)
    except OSError:
        # Like the SDK, a read-only user directory does not prevent this request.
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


@lru_cache(maxsize=1)
def tinyshare_device_id() -> str:
    """Reuse the existing SDK device identity without importing its bytecode.

    With no existing cache, reproduce its OS identity derivation and retain
    only the derived hash in the SDK's existing per-user cache location.
    """
    cache = Path.home() / ".tinyshare" / "device_id.json"
    try:
        identity = json.loads(cache.read_text(encoding="utf-8")).get("device_id")
        if isinstance(identity, str) and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", identity):
            return identity
    except (OSError, ValueError, AttributeError):
        pass
    values = [platform.machine(), platform.processor(), platform.system(), str(uuid.getnode()), platform.node()]
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(["/usr/sbin/system_profiler", "SPHardwareDataType"], capture_output=True, text=True, timeout=5, check=False)
            hardware = next((line.split(":")[-1].strip() for line in result.stdout.splitlines() if "Hardware UUID" in line), "")
            values.append(hardware)
        except (OSError, subprocess.TimeoutExpired):
            raise ProviderUnavailable("无法确认Tinyshare设备标识，请检查设备配置") from None
    elif platform.system() == "Linux":
        for filename in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                values.append(Path(filename).read_text().strip())
                break
            except OSError:
                continue
    elif platform.system() == "Windows":
        values.extend(_windows_hardware_identity())
    values.append(str(Path.home()))
    identity = sha256("|".join(filter(None, values)).encode("utf-8")).hexdigest()[:32]
    _remember_device_identity(cache, identity)
    return identity
