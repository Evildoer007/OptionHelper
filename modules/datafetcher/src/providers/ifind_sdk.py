"""iFinD SDK延迟适配入口；macOS默认仍使用HTTP。"""

from __future__ import annotations

from ..models import DataRequest
from .base import ProviderUnavailable


class IFindSdkProvider:
    name = "ifind_sdk"
    network = True

    @staticmethod
    def estimate_quota(request: DataRequest) -> int:
        return len(request.asset_ids)

    def fetch(self, _request: DataRequest, _config):
        raise ProviderUnavailable("iFinD SDK未在本机验证；请使用ifind_http")
