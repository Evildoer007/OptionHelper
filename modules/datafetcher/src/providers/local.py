"""受控本地CSV Provider。"""

from __future__ import annotations

import pandas as pd

from ..config import DataFetcherConfig
from ..models import DataRequest
from ..request_validator import controlled_local_csv_sources
from .base import ProviderUnavailable


class LocalCsvProvider:
    name = "local"
    network = False

    @staticmethod
    def estimate_quota(_request: DataRequest) -> int:
        return 0

    def fetch(self, request: DataRequest, config: DataFetcherConfig) -> pd.DataFrame:
        paths = controlled_local_csv_sources(request, config)
        available = {asset_id for asset_id, _path in paths}
        for asset_id in request.asset_ids:
            if asset_id not in available:
                raise ProviderUnavailable(f"本地DataStore未找到{asset_id}日线CSV")
        frames: list[pd.DataFrame] = []
        for asset_id, path in paths:
            try:
                frame = pd.read_csv(path)
            except (OSError, pd.errors.ParserError) as error:
                raise ProviderUnavailable("受控本地CSV无法读取") from error
            if "asset_id" not in frame.columns and "ts_code" not in frame.columns and "thscode" not in frame.columns:
                frame["asset_id"] = asset_id
            frames.append(frame)
        return pd.concat(frames, ignore_index=True)
