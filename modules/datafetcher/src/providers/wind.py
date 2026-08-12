"""Wind延迟适配入口。"""

from __future__ import annotations

from ..models import DataRequest
from .base import ProviderUnavailable


class WindProvider:
    name = "wind"
    network = True

    @classmethod
    def capability(cls, *, enabled: bool) -> dict[str, str | bool]:
        """只报告受控启用状态，不导入SDK、不连接Wind、不消耗额度。"""

        return {
            "provider": cls.name,
            "enabled": enabled,
            "status": "enabled_not_probed" if enabled else "disabled_by_policy",
        }

    @staticmethod
    def estimate_quota(request: DataRequest) -> int:
        return len(request.asset_ids)

    def fetch(self, request: DataRequest, _config):
        try:
            from WindPy import w  # type: ignore[import-not-found]
        except ImportError as error:
            raise ProviderUnavailable("Wind SDK当前不可用") from error
        if not w.isconnected() and getattr(w.start(), "ErrorCode", -1) != 0:
            raise ProviderUnavailable("Wind连接未就绪")
        fields = ",".join(request.fields)
        frames = []
        for asset_id in request.asset_ids:
            result = w.wsd(asset_id, fields, request.start_date, request.end_date, "")
            if getattr(result, "ErrorCode", -1) != 0:
                raise ProviderUnavailable("Wind未返回可用数据")
            try:
                import pandas as pd

                frame = pd.DataFrame(dict(zip(result.Fields, result.Data, strict=True)))
                frame["date"] = [item.strftime("%Y-%m-%d") for item in result.Times]
                frame["asset_id"] = asset_id
                frames.append(frame)
            except Exception as error:
                raise ProviderUnavailable("Wind返回结构无法标准化") from error
        if not frames:
            raise ProviderUnavailable("Wind未返回可用数据")
        return pd.concat(frames, ignore_index=True)
