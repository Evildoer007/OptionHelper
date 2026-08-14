"""运行时协议的稳定标识。

发布版本由打包层写入Manifest。Core只定义协议和Schema身份，不绑定某次发行。
"""

from __future__ import annotations


RESOLVED_CONTRACT_SCHEMA_ID = "optionhelper.resolved-contract"
PRICING_CONFIG_SCHEMA_ID = "optionhelper.pricing-config"
MODULE_HOST_PROTOCOL_ID = "optionhelper.module-host"
DEVELOPMENT_RELEASE_ID = "development"


def require_release_id(value: object, field: str) -> str:
    """校验Host注入的发行身份，不对具体发行号作运行时硬编码。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}必须为非空字符串")
    return value


__all__ = (
    "DEVELOPMENT_RELEASE_ID", "MODULE_HOST_PROTOCOL_ID", "PRICING_CONFIG_SCHEMA_ID",
    "RESOLVED_CONTRACT_SCHEMA_ID", "require_release_id",
)
