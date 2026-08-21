"""Runtime presentation policy owned by Designer.

Colours, type and component rules remain in :mod:`design_tokens`, the single
design-token source. Reporter and other modules use Designer's public Tool
entry and never need this internal model.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Mapping


_MODULE_ASSET_ROOT = Path(__file__).resolve().parents[1] / "assets"
_RELEASE_ASSET_ROOT = Path(__file__).resolve().parents[3] / "assets" / "designer"
_REQUIRED_TEMPLATE_NAMES = ("card.html", "quote.html", "report.html")
_REQUIRED_TEMPLATE_DEFINITIONS = (
    "card-standard.template.json",
    "quote-standard.template.json",
    "report-standard.template.json",
)
_RUNTIME_ASSET_FILES = {
    "templates": frozenset((*_REQUIRED_TEMPLATE_NAMES, *_REQUIRED_TEMPLATE_DEFINITIONS)),
    "themes": frozenset({"designer-theme.css", "designer-token-vars.css"}),
    "vendor": frozenset({"echarts.min.js"}),
}
_IGNORED_ASSET_NAMES = frozenset({".DS_Store"})
# 开发态的资源属于Designer模块；标准Skill发行包将其置于assets/designer。
# 不由Reporter推断或透传资源路径，避免目录调整后报告失效。
_DEFAULT_ASSET_ROOT = _MODULE_ASSET_ROOT if _MODULE_ASSET_ROOT.is_dir() else _RELEASE_ASSET_ROOT


class DesignerConfigurationError(RuntimeError):
    """Raised when the shipped Designer assets are unavailable or incomplete."""


@dataclass(frozen=True)
class DesignerConfig:
    """Validated runtime policy and checked-in offline asset locations.

    Values here decide only which already-supported presentation outputs are
    accepted.  They never duplicate the visual tokens in ``design_tokens``.
    """

    asset_root: Path = _DEFAULT_ASSET_ROOT
    default_asset_mode: str = "shared"
    allow_portable_assets: bool = True
    allow_pdf: bool = True

    def __post_init__(self) -> None:
        root = self.asset_root.expanduser().resolve()
        object.__setattr__(self, "asset_root", root)
        if not root.is_dir():
            raise DesignerConfigurationError(f"找不到Designer资源目录：{root}")
        root_names = {path.name for path in root.iterdir() if path.name not in _IGNORED_ASSET_NAMES}
        expected_root_names = set(_RUNTIME_ASSET_FILES)
        missing_root_names = expected_root_names.difference(root_names)
        if missing_root_names:
            raise DesignerConfigurationError(
                f"Designer资源目录不符合运行时清单：缺少{', '.join(sorted(missing_root_names))}。"
            )
        for directory, expected_files in _RUNTIME_ASSET_FILES.items():
            directory_path = root / directory
            if not directory_path.is_dir():
                raise DesignerConfigurationError(f"Designer资源目录必须包含{directory}目录。")
            actual_files = {
                path.name
                for path in directory_path.iterdir()
                if path.name not in _IGNORED_ASSET_NAMES
            }
            allowed_files = actual_files
            if directory == "templates":
                allowed_files = {
                    name for name in actual_files
                    if name in expected_files or name.endswith(".template.json")
                }
                invalid_templates = actual_files.difference(allowed_files)
                if invalid_templates:
                    raise DesignerConfigurationError(
                        "Designer模板目录仅允许HTML壳和.template.json定义："
                        f"{', '.join(sorted(invalid_templates))}。"
                    )
            if not expected_files.issubset(allowed_files):
                missing = sorted(expected_files.difference(actual_files))
                detail = []
                if missing:
                    detail.append(f"缺少{', '.join(missing)}")
                raise DesignerConfigurationError(f"Designer资源目录{directory}不符合交付清单：{'；'.join(detail)}。")
        if not self.report_theme_path.is_file():
            raise DesignerConfigurationError(f"找不到Designer视觉主题：{self.report_theme_path}")
        if not self.echarts_asset_path.is_file():
            raise DesignerConfigurationError(f"找不到离线ECharts文件：{self.echarts_asset_path}")
        missing_templates = [name for name in _REQUIRED_TEMPLATE_NAMES if not self.template_path(name).is_file()]
        if missing_templates:
            raise DesignerConfigurationError(f"找不到Designer模板：{', '.join(missing_templates)}")
        if self.default_asset_mode not in {"shared", "portable"}:
            raise DesignerConfigurationError("default_asset_mode只能是shared或portable。")
        for name in ("allow_portable_assets", "allow_pdf"):
            if not isinstance(getattr(self, name), bool):
                raise DesignerConfigurationError(f"{name}必须是布尔值。")
        if self.default_asset_mode == "portable" and not self.allow_portable_assets:
            raise DesignerConfigurationError("default_asset_mode为portable时必须允许portable资源模式。")

    @property
    def report_theme_path(self) -> Path:
        return self.asset_root / "themes" / "designer-theme.css"

    @property
    def echarts_asset_path(self) -> Path:
        return self.asset_root / "vendor" / "echarts.min.js"

    @property
    def template_root(self) -> Path:
        """Return the checked-in public delivery template directory."""

        return self.asset_root / "templates"

    def template_path(self, name: str) -> Path:
        """Resolve one named template without accepting a caller-controlled path."""

        if name not in _REQUIRED_TEMPLATE_NAMES:
            raise DesignerConfigurationError(f"不支持的Designer模板：{name}")
        return self.template_root / name

    def template_definition_path(self, template_id: str) -> Path:
        """Resolve one managed template definition without accepting a path."""

        name = f"{template_id}.template.json"
        path = (self.template_root / name).resolve()
        if path.parent != self.template_root.resolve() or not path.is_file() or path.is_symlink():
            raise DesignerConfigurationError(f"找不到Designer模板定义：{template_id}。")
        return path

    def read_template(self, name: str) -> str:
        """Read an immutable, shipped HTML shell for one public output type."""

        path = self.template_path(name)
        if not path.is_file():
            raise DesignerConfigurationError(f"找不到Designer模板：{path}")
        return path.read_text(encoding="utf-8")

    def read_report_theme(self) -> str:
        """Build runtime CSS variables from tokens, then load structural CSS.

        ``designer-theme.css`` owns selectors and layout only.  Token values are
        prepended here from the single machine-readable source, so checked-in
        CSS cannot silently drift into a second palette or type scale.
        """

        from .design_system_builder import build_design_system

        design_system = build_design_system()
        structural_css = self.report_theme_path.read_text(encoding="utf-8")
        structural_css = structural_css.replace(
            "__BREAKPOINT_NARROW__",
            f"{design_system.tokens['breakpoints']['narrow']}px",
        )
        return (
            f"/* designer-token-hash:{design_system.token_hash} */\n"
            f"{design_system.css_variables}\n\n{structural_css}"
        )

    def relative_echarts_path(self, destination_dir: Path) -> str:
        """Return a reference to the single checked-in chart runtime."""

        return os.path.relpath(self.echarts_asset_path, destination_dir.resolve())

    def validate_request(
        self,
        *,
        asset_mode: str,
        output_format: str,
    ) -> None:
        """Reject a requested render policy before content is rendered."""

        if asset_mode == "portable" and not self.allow_portable_assets:
            raise DesignerConfigurationError("当前Designer配置禁止portable资源模式。")
        if output_format == "pdf" and not self.allow_pdf:
            raise DesignerConfigurationError("当前Designer配置禁止PDF输出。")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None = None) -> "DesignerConfig":
        """Load a strict, explicit render-policy mapping for one Tool call."""

        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise DesignerConfigurationError("config必须是对象。")
        allowed = {
            "default_asset_mode",
            "allow_portable_assets",
            "allow_pdf",
        }
        unknown = set(value).difference(allowed)
        if unknown:
            raise DesignerConfigurationError(f"config包含不支持的字段：{', '.join(sorted(map(str, unknown)))}")
        return cls(**dict(value))


def load_designer_config(value: DesignerConfig | Mapping[str, Any] | None = None) -> DesignerConfig:
    """Load Designer's single explicit runtime policy model."""

    if isinstance(value, DesignerConfig):
        return value
    return DesignerConfig.from_mapping(value)


__all__ = ["DesignerConfig", "DesignerConfigurationError", "load_designer_config"]
