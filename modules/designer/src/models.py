"""Public Designer input and artifact models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


OUTPUT_TYPES = {"card", "quote", "report"}
FORMATS = {"html", "pdf"}
ASSET_MODES = {"shared", "portable"}
DESIGNER_PAYLOAD_SCHEMA = "optionhelper.designer-payload"
DESIGNER_ARTIFACT_MANIFEST_SCHEMA = "optionhelper.designer-artifact-manifest"
_REQUEST_FIELDS = {
    "payload",
    "output_type",
    "format",
    "asset_mode",
    "input_dir",
    "output_dir",
    "design_system_id",
    "template_id",
    "metadata",
}


@dataclass(frozen=True)
class DesignerInput:
    """Immutable handoff from Reporter to Designer.

    ``payload`` is treated as frozen content. Designer copies it before
    rendering, so presentation formatting cannot mutate the handed-off facts.
    ``input_dir`` is only used to resolve explicitly supplied artifact refs.
    """

    payload: Mapping[str, Any]
    output_type: str = "report"
    format: str = "html"
    asset_mode: str = "shared"
    input_dir: Path | None = None
    output_dir: Path | None = None
    design_system_id: str | None = None
    template_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        output_type = str(self.output_type).lower()
        output_format = str(self.format).lower()
        asset_mode = str(self.asset_mode).lower()
        if output_type not in OUTPUT_TYPES:
            raise ValueError(f"output_type仅支持：{', '.join(sorted(OUTPUT_TYPES))}")
        if output_format not in FORMATS:
            raise ValueError(f"format仅支持：{', '.join(sorted(FORMATS))}")
        if asset_mode not in ASSET_MODES:
            raise ValueError(f"asset_mode仅支持：{', '.join(sorted(ASSET_MODES))}")
        if not isinstance(self.payload, Mapping):
            raise TypeError("DesignerInput.payload必须是Mapping。")
        if self.template_id is not None and not str(self.template_id).strip():
            raise ValueError("template_id不能为空字符串。")
        payload = dict(self.payload)
        declared_schema = payload.get("schema")
        if str(declared_schema) != DESIGNER_PAYLOAD_SCHEMA:
            raise ValueError(f"payload.schema必须为{DESIGNER_PAYLOAD_SCHEMA}。")
        object.__setattr__(self, "payload", payload)

    @property
    def normalized_output_type(self) -> str:
        return str(self.output_type).lower()

    @property
    def normalized_format(self) -> str:
        return str(self.format).lower()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DesignerInput":
        unknown = set(value).difference(_REQUEST_FIELDS)
        if unknown:
            raise ValueError("DesignerInput包含不支持字段。")
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("DesignerInput需要payload。")
        output_type = str(value.get("output_type") or "report").lower()
        return cls(
            payload=payload,
            output_type=output_type,
            format=str(value.get("format") or "html").lower(),
            asset_mode=str(value.get("asset_mode") or "shared").lower(),
            input_dir=Path(value["input_dir"]).resolve() if value.get("input_dir") else None,
            output_dir=Path(value["output_dir"]).resolve() if value.get("output_dir") else None,
            design_system_id=str(value["design_system_id"]) if value.get("design_system_id") else None,
            template_id=str(value["template_id"]) if value.get("template_id") else None,
            metadata=value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {},
        )


__all__ = [
    "ASSET_MODES",
    "DESIGNER_ARTIFACT_MANIFEST_SCHEMA",
    "DESIGNER_PAYLOAD_SCHEMA",
    "DesignerInput",
    "FORMATS",
    "OUTPUT_TYPES",
]
