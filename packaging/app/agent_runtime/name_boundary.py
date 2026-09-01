"""Name-boundary scanner for product sources and packaged artifacts.

The product may mention the provider brand in provider integration and in the
three runtime provenance notices. That narrow allowance does not apply to the
prohibited historical/tooling names, and it does not apply to arbitrary tests,
UI, archives, or generated artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
from pathlib import Path
import re
from typing import Iterable
import zipfile


_PROHIBITED_MARKERS = (
    "d" + "sh",
    "deep" + "seek" + "-" + "harness",
    "@" + "deep" + "seek-ai",
)
_PROVIDER_MARKER = "deep" + "seek"
_MARKERS = (*_PROHIBITED_MARKERS, _PROVIDER_MARKER)

LEGAL_FILES = frozenset(
    {
        Path("LICENSES/OptionHelper-Agent-Runtime-LICENSE.txt"),
        Path("LICENSES/OptionHelper-Agent-Runtime-NOTICES.txt"),
        Path("LICENSES/OptionHelper-Agent-Runtime-SOURCE-MAPPING.md"),
    }
)

# The provider name is permitted only in provider implementation/integration
# paths. Tests and generic UI are deliberately not included: they are either not
# shipped or are product surfaces where the provider name is not required.
LEGAL_PROVIDER_PREFIXES = (
    ("backend", "model_gateway"),
    ("backend", "settings", "model_catalog.py"),
    ("backend", "app_server.py"),
)

# Kept as an audit classification for callers that need to distinguish a
# pre-existing source finding from a newly generated artifact finding. It is
# never used as a clean-scan exemption.
PREEXISTING_PRODUCT_SURFACES = frozenset(
    {
        Path("products/app/backend/app_server.py"),
        Path("products/app/backend/model_gateway/provider_registry.py"),
        Path("products/app/backend/settings/model_catalog.py"),
        Path("products/app/frontend/settings/general-settings.css"),
        Path("products/app/frontend/settings/index.html"),
        Path("products/app/frontend/shared/styles.css"),
        Path("products/app/tests"),
    }
)


@dataclass(frozen=True)
class NameBoundaryFinding:
    path: Path
    marker: str
    location: str = "content"


def runtime_scan_roots(repo_root: Path) -> tuple[Path, ...]:
    """Return product source, packaging source, and final-artifact roots."""

    root = repo_root.resolve()
    roots: list[Path] = [
        root / "products" / "app" / "backend",
        root / "products" / "app" / "frontend",
        root / "products" / "app" / "runtime",
        root / "products" / "app" / "config",
        root / "products" / "app" / "backend" / "agent_runtime",
        root / "products" / "app" / "frontend" / "optchat",
        root / "products" / "app" / "config" / "app-defaults.yaml",
        root / "products" / "app" / "config" / "logging.yaml",
        root / "packaging" / "app",
    ]
    # These are the delivery locations owned by the packaging chain. A ZIP
    # is inspected member-by-member; a mounted App directory is inspected as
    # a normal tree. No external repository or node_modules root is added.
    for optional in (
        root / "dist",
        root / "build",
        root / "result" / "windows-candidate",
    ):
        if optional.exists():
            roots.append(optional)
    return tuple(roots)


def _files_under(paths: Iterable[Path]) -> Iterable[Path]:
    seen: set[Path] = set()
    for root in paths:
        if not root.exists():
            continue
        candidates = (root,) if root.is_file() else root.rglob("*")
        for path in candidates:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and ".pytest_cache" not in path.parts
                and "node_modules" not in path.parts
                and not path.name.endswith(".pyc")
            ):
                yield path


def _parts(value: str | Path) -> tuple[str, ...]:
    return tuple(part.casefold() for part in Path(value).parts if part not in {"", "."})


def _ends_with(parts: tuple[str, ...], suffix: tuple[str, ...]) -> bool:
    return len(parts) >= len(suffix) and parts[-len(suffix):] == suffix


def _is_legal_file(
    logical_name: str,
    legal_files: set[Path],
) -> bool:
    parts = _parts(logical_name)
    legal_names = {_parts(item) for item in legal_files}
    for legal in legal_names:
        if _ends_with(parts, legal):
            return True
        # Packaged Apps place the legal files below Resources/LICENSES/*.
        if (
            len(legal) == 2
            and legal[0] == "licenses"
            and parts
            and parts[-1] == legal[1]
            and "licenses" in parts
        ):
            return True
    return False


def _is_provider_position(logical_name: str) -> bool:
    parts = _parts(logical_name)
    # macOS CodeResources is a generated signature index. It necessarily
    # repeats the names of signed, permitted Provider adapter files, but it is
    # not a product-facing source or UI surface.
    if len(parts) >= 2 and parts[-2:] == ("_codesignature", "coderesources"):
        return True
    for prefix in LEGAL_PROVIDER_PREFIXES:
        normalized = tuple(item.casefold() for item in prefix)
        if any(
            parts[index:index + len(normalized)] == normalized
            for index in range(len(parts))
        ):
            return True
    return False


def _marker_allowed(marker: str, *, logical_name: str, legal_files: set[Path]) -> bool:
    if _is_legal_file(logical_name, legal_files):
        return True
    if marker != _PROVIDER_MARKER:
        return False
    return _is_provider_position(logical_name)


def _logical_name(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _scan_text(
    path: Path,
    content: bytes,
    *,
    logical_name: str,
    legal_files: set[Path],
    findings: list[NameBoundaryFinding],
) -> None:
    text = content.decode("utf-8", errors="ignore").casefold()
    name = logical_name.casefold()
    for marker in _MARKERS:
        present_in_name = (
            bool(re.search(r"(?<![a-z0-9])" + ("d" + "sh") + r"(?![a-z0-9])", name))
            if marker == _PROHIBITED_MARKERS[0]
            else marker in name
        )
        present_in_text = (
            bool(re.search(r"(?<![a-z0-9])" + ("d" + "sh") + r"(?![a-z0-9])", text))
            if marker == _PROHIBITED_MARKERS[0]
            else marker in text
        )
        if present_in_name and not _marker_allowed(marker, logical_name=logical_name, legal_files=legal_files):
            findings.append(NameBoundaryFinding(path, marker, "path"))
        if present_in_text and b"\x00" not in content and not _marker_allowed(marker, logical_name=logical_name, legal_files=legal_files):
            findings.append(NameBoundaryFinding(path, marker, "content"))


def _scan_zip_bytes(
    path: Path,
    content: bytes,
    *,
    logical_prefix: str,
    legal_files: set[Path],
    findings: list[NameBoundaryFinding],
) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (OSError, zipfile.BadZipFile):
        return
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            member = f"{logical_prefix}/{info.filename}" if logical_prefix else info.filename
            display = Path(f"{path}!/{info.filename}")
            try:
                member_content = archive.read(info)
            except (OSError, RuntimeError, KeyError, zipfile.BadZipFile):
                continue
            _scan_text(
                display,
                member_content,
                logical_name=member,
                legal_files=legal_files,
                findings=findings,
            )
            if info.filename.casefold().endswith(".zip"):
                _scan_zip_bytes(
                    display,
                    member_content,
                    logical_prefix=member,
                    legal_files=legal_files,
                    findings=findings,
                )


def scan_name_boundary(
    repo_root: Path,
    *,
    paths: Iterable[Path] | None = None,
    legal_files: Iterable[Path] = LEGAL_FILES,
) -> tuple[NameBoundaryFinding, ...]:
    """Find prohibited names in source trees and final artifacts.

    Provider-name content is accepted only in provider implementation paths or the
    exact runtime legal-file names. Prohibited historical/tooling names remain
    forbidden in provider paths; the exact legal files are the only provenance
    exception.
    """

    root = repo_root.resolve()
    legal = {Path(item) for item in legal_files}
    findings: list[NameBoundaryFinding] = []
    scan_paths = runtime_scan_roots(root) if paths is None else tuple(paths)
    for path in _files_under(scan_paths):
        logical_name = _logical_name(root, path)
        try:
            content = path.read_bytes()
        except OSError:
            continue
        _scan_text(
            path,
            content,
            logical_name=logical_name,
            legal_files=legal,
            findings=findings,
        )
        if path.suffix.casefold() == ".zip":
            _scan_zip_bytes(
                path,
                content,
                logical_prefix=logical_name,
                legal_files=legal,
                findings=findings,
            )
    return tuple(findings)


def is_preexisting_product_surface(repo_root: Path, path: Path) -> bool:
    """Classify an already-existing product finding without suppressing it."""

    try:
        relative = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    return any(
        relative == prefix or prefix in relative.parents
        for prefix in PREEXISTING_PRODUCT_SURFACES
    )


def assert_name_boundary_clean(
    repo_root: Path,
    *,
    paths: Iterable[Path] | None = None,
    legal_files: Iterable[Path] = LEGAL_FILES,
) -> None:
    findings = scan_name_boundary(repo_root, paths=paths, legal_files=legal_files)
    if findings:
        detail = ", ".join(
            f"{item.path}:{item.marker}[{item.location}]" for item in findings
        )
        raise AssertionError(f"runtime name boundary violation: {detail}")


def agent_runtime_delivery_paths(resources_root: Path) -> tuple[Path, ...]:
    """Return only the files owned by the packaged Agent Runtime."""

    resources = resources_root.resolve()
    return (
        resources / "agent-runtime",
        resources / "LICENSES" / "agent-runtime",
    )


def assert_agent_runtime_delivery_clean(resources_root: Path) -> None:
    """Apply runtime provenance rules without scanning unrelated App content."""

    resources = resources_root.resolve()
    assert_name_boundary_clean(
        resources,
        paths=agent_runtime_delivery_paths(resources),
    )


__all__ = [
    "LEGAL_FILES",
    "LEGAL_PROVIDER_PREFIXES",
    "NameBoundaryFinding",
    "PREEXISTING_PRODUCT_SURFACES",
    "agent_runtime_delivery_paths",
    "assert_agent_runtime_delivery_clean",
    "assert_name_boundary_clean",
    "is_preexisting_product_surface",
    "runtime_scan_roots",
    "scan_name_boundary",
]
