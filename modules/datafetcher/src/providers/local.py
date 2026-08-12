"""受控本地CSV Provider。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..config import DataFetcherConfig
from ..models import DataRequest
from ..request_validator import controlled_local_csv_path
from .base import ProviderUnavailable


class LocalCsvProvider:
    name = "local"
    network = False

    @staticmethod
    def estimate_quota(_request: DataRequest) -> int:
        return 0

    def fetch(self, request: DataRequest, config: DataFetcherConfig) -> pd.DataFrame:
        root = Path(config.local_csv_root or config.data_root or Path.cwd()).resolve()
        if request.local_csv:
            source = controlled_local_csv_path(request, config)
            if source is None:
                raise ProviderUnavailable("受控本地CSV不存在")
            paths = [(request.asset_id, source)]
        else:
            paths = []
            for asset_id in request.asset_ids:
                candidates = (root / "market" / f"{asset_id}_daily.csv", root / "market" / f"{asset_id}.csv")
                match = next((path for path in candidates if path.is_file()), None)
                if match is None:
                    raise ProviderUnavailable(f"本地DataStore未找到{asset_id}日线CSV")
                paths.append((asset_id, match))
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
