"""Public Designer input and artifact models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


OUTPUT_TYPES = {"card", "report"}
FORMATS = {"html", "pdf"}
REPORT_LAYOUTS = {"continuous"}
ASSET_MODES = {"shared", "portable"}


@dataclass(frozen=True)
class DesignerInput:
    """Immutable handoff from Reporter to Designer.

    ``payload`` is treated as frozen content. Designer copies it before
    rendering, so formatting cannot mutate a ReportUnit or ReportBundle.
    ``input_dir`` is only used to resolve explicitly supplied artifact refs.
    """

    payload: Mapping[str, Any]
    output_type: str = "report"
    format: str = "html"
    html_report_layout: str | None = None
    asset_mode: str = "shared"
    input_dir: Path | None = None
    output_dir: Path | None = None
    design_system_version: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        output_type = {"detailed": "report", "simple": "card"}.get(str(self.output_type).lower(), str(self.output_type).lower())
        output_format = str(self.format).lower()
        layout = str(self.html_report_layout).lower() if self.html_report_layout is not None else None
        asset_mode = str(self.asset_mode).lower()
        if output_type not in OUTPUT_TYPES:
            raise ValueError(f"output_type仅支持：{', '.join(sorted(OUTPUT_TYPES))}")
        if output_format not in FORMATS:
            raise ValueError(f"format仅支持：{', '.join(sorted(FORMATS))}")
        if layout is not None and layout not in REPORT_LAYOUTS:
            raise ValueError(f"html_report_layout仅支持：{', '.join(sorted(REPORT_LAYOUTS))}")
        if output_type == "report" and output_format == "html":
            if layout is None:
                object.__setattr__(self, "html_report_layout", "continuous")
        elif layout is not None:
            raise ValueError("Card和PDF不接受html_report_layout。")
        if asset_mode not in ASSET_MODES:
            raise ValueError(f"asset_mode仅支持：{', '.join(sorted(ASSET_MODES))}")
        if not isinstance(self.payload, Mapping):
            raise TypeError("DesignerInput.payload必须是Mapping。")

    @property
    def normalized_output_type(self) -> str:
        return {"detailed": "report", "simple": "card"}.get(str(self.output_type).lower(), str(self.output_type).lower())

    @property
    def normalized_layout(self) -> str:
        if self.normalized_output_type == "card" or self.normalized_format == "pdf":
            return "brief"
        # Designer内部以brief表示唯一的连续完整正文；HTML与PDF均无目录。
        # 该值不改变冻结输入事实。
        return "brief"

    @property
    def normalized_format(self) -> str:
        return str(self.format).lower()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DesignerInput":
        if value.get("layout") is not None:
            raise ValueError("layout已废弃；Report HTML固定为连续版。")
        payload = value.get("payload") or value.get("designer_payload") or value.get("report_unit")
        if not isinstance(payload, Mapping):
            raise ValueError("DesignerInput需要payload、designer_payload或report_unit。")
        output_type = str(value.get("output_type") or value.get("report_level") or "report").lower()
        output_type = {"detailed": "report", "simple": "card"}.get(output_type, output_type)
        return cls(
            payload=payload,
            output_type=output_type,
            format=str(value.get("format") or "html").lower(),
            html_report_layout=(str(value["html_report_layout"]).lower() if value.get("html_report_layout") is not None else None),
            asset_mode=str(value.get("asset_mode") or "shared").lower(),
            input_dir=Path(value["input_dir"]).resolve() if value.get("input_dir") else None,
            output_dir=Path(value["output_dir"]).resolve() if value.get("output_dir") else None,
            design_system_version=str(value["design_system_version"]) if value.get("design_system_version") else None,
            metadata=value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {},
        )


__all__ = ["ASSET_MODES", "DesignerInput", "FORMATS", "OUTPUT_TYPES", "REPORT_LAYOUTS"]
