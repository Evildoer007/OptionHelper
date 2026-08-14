"""DataFetcher非敏感配置。"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
from typing import Callable, Mapping

from runtime.bootstrap import bootstrap_runtime

from .models import DataAssetRef, SecretRef


DEFAULT_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "adj_open",
    "adj_high",
    "adj_low",
    "adj_close",
    "volume",
)


class SecretPortUnavailable(RuntimeError):
    """Host受控凭据端口暂不可用，异常细节不得离开Provider边界。"""


def resolve_secret(
    ref: SecretRef | None,
    fallback_env: str,
    *,
    host_secret_port: Callable[[SecretRef], str] | None = None,
) -> str | None:
    """在Provider调用边界解析Core SecretRef，调用者不得记录返回值。"""

    if host_secret_port is not None:
        if ref is None:
            return None
        try:
            value = host_secret_port(ref)
        except Exception as error:
            # Host凭据端口的异常可能含敏感上下文；只保留可重试的非敏感状态。
            raise SecretPortUnavailable("Host凭据端口暂不可用") from error
        return value.strip() if isinstance(value, str) and value.strip() else None
    if ref is None:
        return os.environ.get(fallback_env) or None
    provider = ref.provider.strip().lower()
    if provider in {"env", "environment"}:
        return os.environ.get(ref.key) or None
    if provider == "keychain":
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-a", ref.key, "-s", "OptionHelper", "-w"],
                text=True,
                capture_output=True,
                check=True,
            )
            return result.stdout.rstrip("\r\n") or None
        except (FileNotFoundError, subprocess.CalledProcessError):
            return None
    return None


@dataclass(frozen=True)
class DataFetcherConfig:
    """本机DataStore与Provider策略。

    Token只能由环境变量或Core ``SecretRef``解析，绝不写进本配置、日志或
    结果。``cache_root``和``result_root``可在测试中替换为临时目录。
    """

    data_root: Path | None = None
    result_root: Path | None = None
    cache_root: Path | None = None
    local_csv_root: Path | None = None
    provider_priority: tuple[str, ...] = ("ifind_http",)
    wind_enabled: bool = False
    allowed_fields: tuple[str, ...] = DEFAULT_FIELDS
    max_span_days: int = 3_700
    max_provider_units: int = 20
    offline: bool = False
    ifind_secret_ref: SecretRef | None = None
    ifind_secret_port: Callable[[SecretRef], str] | None = field(default=None, repr=False, compare=False)
    timeout_seconds: int = 30
    # Host确认的最新可观测行情日。未注入时以Asia/Shanghai当前自然日为上限；
    # 未来日期只能通过独立交易日历入口取得，不能形成历史OHLC资产。
    market_data_as_of_date: str | None = None
    # 已登记的受控trading-calendar资产。历史质量仅在该资产完整覆盖请求时标记complete。
    trading_calendar_ref: DataAssetRef | None = None
    # 只有含calendar_id、calendar_revision和完整覆盖声明的映射才可作为已验证日历。
    # 裸sessions仍可保留为辅助信息，但行情质量必须标记为unverified。
    trading_calendar_sessions: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_runtime(cls) -> "DataFetcherConfig":
        paths = bootstrap_runtime()
        data_root = Path(os.environ.get("OPTIONHELPER_DATA_ROOT", paths.data_root)).expanduser().resolve()
        result_root = Path(os.environ.get("OPTIONHELPER_RESULT_ROOT", paths.result_root)).expanduser().resolve()
        offline = os.environ.get("OPTIONHELPER_DATAFETCHER_OFFLINE", "").strip().lower() in {"1", "true", "yes"}
        return cls(
            data_root=data_root,
            result_root=result_root,
            cache_root=data_root / "datafetcher-cache",
            local_csv_root=data_root,
            offline=offline,
        )

    def resolved(self) -> "DataFetcherConfig":
        paths = bootstrap_runtime()
        data_root = Path(self.data_root or paths.data_root).expanduser().resolve()
        result_root = Path(self.result_root or paths.result_root).expanduser().resolve()
        return DataFetcherConfig(
            data_root=data_root,
            result_root=result_root,
            cache_root=Path(self.cache_root or (data_root / "datafetcher-cache")).expanduser().resolve(),
            local_csv_root=Path(self.local_csv_root or data_root).expanduser().resolve(),
            provider_priority=tuple(self.provider_priority),
            wind_enabled=bool(self.wind_enabled),
            allowed_fields=tuple(self.allowed_fields),
            max_span_days=self.max_span_days,
            max_provider_units=self.max_provider_units,
            offline=self.offline,
            ifind_secret_ref=self.ifind_secret_ref,
            ifind_secret_port=self.ifind_secret_port,
            timeout_seconds=self.timeout_seconds,
            market_data_as_of_date=self.market_data_as_of_date,
            trading_calendar_ref=self.trading_calendar_ref,
            trading_calendar_sessions={
                key: dict(value) if isinstance(value, Mapping) else tuple(value)
                for key, value in self.trading_calendar_sessions.items()
            },
        )
