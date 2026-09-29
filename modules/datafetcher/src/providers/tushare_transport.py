"""Auditable HTTP transport. No SDK import, implicit fallback or CDN execution."""
from __future__ import annotations

from typing import Any
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
import json
import platform
import re
import subprocess
import uuid
import pandas as pd
import requests

from .base import ProviderInputError, ProviderUnavailable, ProviderUnauthorized, ProviderAccountPermissionDenied, ProviderQuotaExceeded

ALLOWED_APIS = frozenset({"daily", "fund_daily", "index_daily", "adj_factor", "fund_adj", "trade_cal"})


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
            status = response.status_code
            detail = str(result.get("msg", result.get("error", ""))).lower()
            if status == 401 or any(word in detail for word in ("token", "授权码", "认证", "鉴权")):
                raise ProviderUnauthorized("Token验证失败，请检查所选服务与Token是否匹配")
            if status == 429 or any(word in detail for word in ("频次", "频率", "每分钟", "quota")):
                raise ProviderQuotaExceeded("数据服务调用额度或频次受限")
            if status == 403 or any(word in detail for word in ("积分", "权限", "permission")):
                raise ProviderAccountPermissionDenied("当前数据账号没有接口权限")
            raise ProviderUnavailable("数据服务拒绝了请求")
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


@lru_cache(maxsize=1)
def tinyshare_device_id() -> str:
    """Reuse the existing SDK device identity without importing its bytecode.

    With no existing cache, reproduce its OS identity derivation in memory;
    never persist hostname or hardware information into App records.
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
            result = subprocess.run(["system_profiler", "SPHardwareDataType"], capture_output=True, text=True, timeout=5, check=False)
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
        # Do not invent a different SDK identity on Windows without a cache.
        raise ProviderUnavailable("请先配置Tinyshare本机设备标识，再测试连接", reason_code="device_identity_missing")
    values.append(str(Path.home()))
    return sha256("|".join(filter(None, values)).encode("utf-8")).hexdigest()[:32]
