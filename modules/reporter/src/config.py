"""Reporter专属输出与产物校验默认值。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class RuntimePathsLike(Protocol):
    result_root: Path
    mode: str


@dataclass(frozen=True)
class ReporterConfig:
    """服务入口和交付物校验共享的最小配置。"""

    output_directory_name: str = "output_report"
    max_request_bytes: int = 2_000_000
    require_pdf_header: bool = True

    def __post_init__(self) -> None:
        if not self.output_directory_name or "/" in self.output_directory_name or "\\" in self.output_directory_name:
            raise ValueError("output_directory_name必须是单个目录名")
        if self.max_request_bytes <= 0:
            raise ValueError("max_request_bytes必须为正数")


DEFAULT_REPORTER_CONFIG = ReporterConfig()


def default_report_output_root(
    runtime_paths: RuntimePathsLike,
    config: ReporterConfig = DEFAULT_REPORTER_CONFIG,
) -> Path:
    """Keep standalone Skill reports in the invoking project's result root.

    Release bootstrap already requires ``result_root`` to live outside the
    read-only Skill install.  It conventionally resolves to
    ``<project>/result``.  Adding the development-only
    ``output_report`` directory in release mode made the documented project
    destination inaccurate and obscured the delivered file from the user.
    """

    root = Path(runtime_paths.result_root).expanduser().resolve()
    if runtime_paths.mode == "release":
        return root
    return root / config.output_directory_name


__all__ = ["DEFAULT_REPORTER_CONFIG", "ReporterConfig", "default_report_output_root"]
