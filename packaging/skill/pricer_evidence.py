"""Packaging gates for the Pricer fair-parameter evidence manifest.

The manifest is runtime data, but its physical source paths are a packaging
contract.  Keep the contract here so Skill and App validate the same bytes
without copying the rules into two verifiers.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Mapping


MANIFEST_SCHEMA_ID = "optionhelper.pricer-fair-parameter-evidence"
MANIFEST_VERSION = "1"
DEVELOPMENT_MANIFEST_RELATIVE_PATH = "modules/pricer/src/fair_parameter_evidence_manifest.json"
PACKAGE_MANIFEST_RELATIVE_PATH = "scripts/modules/pricer/fair_parameter_evidence_manifest.json"

EXPECTED_EVIDENCE_IDS = frozenset(
    {
        "optionreg.registry",
        "pricer.model_router.capabilities",
        "pricer.product_acceptance_matrix",
        "pricer.fair_parameter.cashflow_evidence",
    }
)
_ENTRY_FIELDS = frozenset(
    {
        "development_source_path",
        "release_source_path",
        "source_sha256",
        "release_source_sha256",
    }
)
_MANIFEST_FIELDS = frozenset({"schema_id", "manifest_version", "entries", "manifest_hash"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# A logical evidence id is deliberately bound to one development source and,
# where releasable, one shared payload path.  This prevents a re-signed
# manifest from silently remapping evidence to another file.
EXPECTED_DEVELOPMENT_PATHS = {
    "optionreg.registry": "references/optionreg.py",
    "pricer.model_router.capabilities": "modules/pricer/src/model_router.py",
    "pricer.product_acceptance_matrix": "modules/pricer/tests/fixtures/product_acceptance_matrix.json",
    "pricer.fair_parameter.cashflow_evidence": "modules/pricer/tests/test_fair_parameter_cashflow_evidence.py",
}
EXPECTED_RELEASE_PATHS = {
    "optionreg.registry": "scripts/knowledger/optionreg.py",
    "pricer.model_router.capabilities": "scripts/modules/pricer/model_router.py",
    "pricer.product_acceptance_matrix": None,
    "pricer.fair_parameter.cashflow_evidence": None,
}


class PricerEvidenceManifestError(ValueError):
    """Raised when a fair-parameter evidence manifest is not trustworthy."""


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def _canonical_hash(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _safe_relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise PricerEvidenceManifestError(f"{label}必须是非空POSIX相对路径")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or re.match(r"^[A-Za-z]:", value)
    ):
        raise PricerEvidenceManifestError(f"{label}包含不安全路径：{value}")
    return path.as_posix()


def _regular_file(root: Path, relative: str, label: str) -> Path:
    root = root.expanduser()
    if root.is_symlink() or not root.is_dir():
        raise PricerEvidenceManifestError(f"{label}根目录无效：{root}")
    root = root.resolve()
    path = root.joinpath(*PurePosixPath(relative).parts)

    # Reject symlinked parent directories too.  A lexical relative path is not
    # sufficient protection when a directory in the path can be replaced.
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as error:
            raise PricerEvidenceManifestError(f"{label}不存在：{relative}") from error
        if stat.S_ISLNK(mode):
            raise PricerEvidenceManifestError(f"{label}不得为符号链接：{relative}")

    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise PricerEvidenceManifestError(f"{label}不存在：{relative}") from error
    if not stat.S_ISREG(mode):
        raise PricerEvidenceManifestError(f"{label}必须是普通文件：{relative}")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise PricerEvidenceManifestError(f"{label}越出根目录：{relative}") from error
    return path


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_path(value: Path) -> Path:
    candidate = value.expanduser()
    if candidate.is_dir():
        development = candidate / DEVELOPMENT_MANIFEST_RELATIVE_PATH
        packaged = candidate / PACKAGE_MANIFEST_RELATIVE_PATH
        if development.is_file() or development.is_symlink():
            return development
        return packaged
    return candidate


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PricerEvidenceManifestError(f"Pricer公平参数证据manifest不可读取：{path}") from error
    if not isinstance(raw, dict):
        raise PricerEvidenceManifestError("Pricer公平参数证据manifest必须为JSON对象")
    if set(raw) != _MANIFEST_FIELDS:
        raise PricerEvidenceManifestError(
            f"Pricer公平参数证据manifest字段必须精确为{sorted(_MANIFEST_FIELDS)}"
        )
    if raw.get("schema_id") != MANIFEST_SCHEMA_ID or raw.get("manifest_version") != MANIFEST_VERSION:
        raise PricerEvidenceManifestError("Pricer公平参数证据manifest schema或版本无效")
    manifest_hash = raw.get("manifest_hash")
    if not _is_sha256(manifest_hash):
        raise PricerEvidenceManifestError("Pricer公平参数证据manifest_hash格式无效")
    unsigned = {key: value for key, value in raw.items() if key != "manifest_hash"}
    if _canonical_hash(unsigned) != manifest_hash:
        raise PricerEvidenceManifestError("Pricer公平参数证据manifest_hash校验失败")

    entries = raw.get("entries")
    if not isinstance(entries, dict) or set(entries) != EXPECTED_EVIDENCE_IDS:
        raise PricerEvidenceManifestError("Pricer公平参数证据manifest必须唯一覆盖四个logical id")
    for evidence_id in EXPECTED_EVIDENCE_IDS:
        entry = entries[evidence_id]
        if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
            raise PricerEvidenceManifestError(f"Pricer证据{evidence_id}字段不符合严格schema")
        development_path = _safe_relative(
            entry.get("development_source_path"),
            f"Pricer证据{evidence_id}.development_source_path",
        )
        release_value = entry.get("release_source_path")
        release_path = None if release_value is None else _safe_relative(
            release_value,
            f"Pricer证据{evidence_id}.release_source_path",
        )
        if development_path != EXPECTED_DEVELOPMENT_PATHS[evidence_id]:
            raise PricerEvidenceManifestError(f"Pricer证据{evidence_id}开发源映射无效")
        if release_path != EXPECTED_RELEASE_PATHS[evidence_id]:
            raise PricerEvidenceManifestError(f"Pricer证据{evidence_id}发行源映射无效")
        source_hash = entry.get("source_sha256")
        release_hash = entry.get("release_source_sha256")
        if not _is_sha256(source_hash):
            raise PricerEvidenceManifestError(f"Pricer证据{evidence_id}.source_sha256格式无效")
        if release_path is None:
            if release_hash is not None:
                raise PricerEvidenceManifestError(f"Pricer证据{evidence_id}无发行路径却登记release hash")
        elif not _is_sha256(release_hash) or release_hash != source_hash:
            raise PricerEvidenceManifestError(f"Pricer证据{evidence_id}发行hash无效或与开发hash不一致")
        # Store normalized values only after all path checks have succeeded.
        entry["development_source_path"] = development_path
        entry["release_source_path"] = release_path
    return raw


def validate_pricer_evidence_manifest(
    manifest: Path,
    *,
    development_root: Path | None = None,
    release_root: Path | None = None,
) -> dict[str, object]:
    """Validate structure, source bytes and optional staged release bytes.

    ``development_root`` is required for build-time source verification.  A
    package verifier passes only ``release_root``; test evidence therefore
    remains a development-only check and is never required inside a package.
    """

    path = _manifest_path(manifest)
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise PricerEvidenceManifestError(f"Pricer公平参数证据manifest不存在：{path}") from error
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise PricerEvidenceManifestError("Pricer公平参数证据manifest必须是普通文件且不得为符号链接")
    payload = _read_manifest(path)
    entries = payload["entries"]
    assert isinstance(entries, dict)

    if development_root is not None:
        development_root = development_root.expanduser().resolve()
        development_manifest = _regular_file(
            development_root,
            DEVELOPMENT_MANIFEST_RELATIVE_PATH,
            "Pricer公平参数开发manifest",
        )
        if path.resolve() != development_manifest.resolve() and path.read_bytes() != development_manifest.read_bytes():
            raise PricerEvidenceManifestError("Pricer公平参数发行manifest与开发冻结源字节不一致")
        for evidence_id, entry in entries.items():
            assert isinstance(entry, dict)
            relative = str(entry["development_source_path"])
            source = _regular_file(development_root, relative, f"Pricer证据{evidence_id}开发源")
            actual = _file_sha256(source)
            if actual != entry["source_sha256"]:
                raise PricerEvidenceManifestError(
                    f"Pricer证据{evidence_id}开发源字节Hash不一致：{relative}"
                )

    if release_root is not None:
        release_root = release_root.expanduser().resolve()
        for evidence_id, entry in entries.items():
            assert isinstance(entry, dict)
            relative = entry["release_source_path"]
            if relative is None:
                continue
            release = _regular_file(release_root, str(relative), f"Pricer证据{evidence_id}发行源")
            actual = _file_sha256(release)
            if actual != entry["release_source_sha256"]:
                raise PricerEvidenceManifestError(
                    f"Pricer证据{evidence_id}发行源字节Hash不一致：{relative}"
                )

    return payload


def pricer_evidence_is_present(root: Path, *, packaged: bool) -> bool:
    """Whether a root contains the fair-parameter capability being gated."""

    manifest = root / (PACKAGE_MANIFEST_RELATIVE_PATH if packaged else DEVELOPMENT_MANIFEST_RELATIVE_PATH)
    implementation = root / (
        "scripts/modules/pricer/fair_parameter.py"
        if packaged
        else "modules/pricer/src/fair_parameter.py"
    )
    return manifest.exists() or implementation.exists()


__all__ = (
    "DEVELOPMENT_MANIFEST_RELATIVE_PATH",
    "EXPECTED_DEVELOPMENT_PATHS",
    "EXPECTED_EVIDENCE_IDS",
    "EXPECTED_RELEASE_PATHS",
    "MANIFEST_SCHEMA_ID",
    "PACKAGE_MANIFEST_RELATIVE_PATH",
    "PricerEvidenceManifestError",
    "pricer_evidence_is_present",
    "validate_pricer_evidence_manifest",
)
