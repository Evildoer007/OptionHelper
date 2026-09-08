"""本机DataStore与ResultStore的受控文件实现。"""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Any, Mapping

from runtime.contracts.contract_types import canonical_json, semantic_hash
from runtime.contracts.contract_api import ResolvedContract
from runtime.protocol.models import DataAssetRef, ModuleRunRef


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_MODULE_DIR = {"payoffer": "output_payoff", "pricer": "output_pricing", "backtester": "output_backtest"}
_FINAL_RUN_STATES = {"succeeded", "partial", "failed", "unsupported", "cancelled", "timed_out"}


class StoreError(ValueError):
    pass


class StoreIntegrityError(StoreError):
    """已提交Store内容与其不可变引用或清单不一致。"""


def _safe_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or value in {".", ".."}:
        raise StoreError(f"{label}含非法路径字符")
    return value


def _safe_relative(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise StoreError("Store文件名必须为受控相对路径")
    if any(not _ID.fullmatch(part) for part in path.parts):
        raise StoreError("Store文件名含非法路径字符")
    return Path(*path.parts)


def _inside(root: Path, target: Path) -> Path:
    root = root.expanduser().resolve()
    target = target.resolve(strict=False)
    if target != root and root not in target.parents:
        raise StoreError("Store路径越界")
    return target


def _store_root(path: str | Path, label: str) -> tuple[Path, tuple[int, int]]:
    requested = Path(os.path.abspath(Path(path).expanduser()))
    if requested.exists() and stat.S_ISLNK(os.lstat(requested).st_mode):
        raise StoreError(f"{label}不得为符号链接")
    # macOS normally exposes /var as a symlink to /private/var.  Canonicalise
    # that platform alias before pinning the configured store root; Local Host
    # separately verifies its project/data and project/result children.
    root = requested.resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    entry = os.lstat(root)
    if not stat.S_ISDIR(entry.st_mode):
        raise StoreError(f"{label}必须是目录")
    return root, (entry.st_dev, entry.st_ino)


def _tenant_root(root: Path, tenant: str) -> Path:
    """单机默认保持蓝图result/output_*路径；非本机租户显式隔离。"""
    return root if tenant == "local" else _inside(root, root / "tenants" / tenant)


def _encode_value(value: bytes | str | Mapping[str, Any]) -> bytes:
    if isinstance(value, bytes):
        return value
    elif isinstance(value, str):
        return value.encode("utf-8")
    elif isinstance(value, Mapping):
        return (canonical_json(value) + "\n").encode("utf-8")
    else:
        raise StoreError("Store只接受bytes、str或JSON对象")


def _write_value(path: Path, value: bytes | str | Mapping[str, Any]) -> bytes:
    payload = _encode_value(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _read_regular_bytes(path: Path, label: str) -> bytes:
    """Read a leaf file once without following a replacement symlink."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise FileNotFoundError(f"{label}不存在") from error
    except OSError as error:
        raise StoreIntegrityError(f"{label}不可安全读取") from error
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise StoreIntegrityError(f"{label}必须是普通文件")
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _json_mapping(value: bytes | str | Mapping[str, Any], label: str) -> Mapping[str, Any]:
    try:
        parsed = value if isinstance(value, Mapping) else json.loads(
            value.decode("utf-8") if isinstance(value, bytes) else value,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        raise StoreError(f"{label}必须是有效JSON对象") from error
    if not isinstance(parsed, Mapping):
        raise StoreError(f"{label}必须是JSON对象")
    return parsed


def _validate_success_contract(
    manifest: Mapping[str, Any],
    resolved_contract: bytes | str | Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    """Reject a successful Run before it can anchor a display-only contract."""

    try:
        contract = ResolvedContract.from_controlled_snapshot(
            _json_mapping(resolved_contract, "resolved_contract.json"),
        )
    except Exception as error:
        raise StoreError("成功ModuleRun必须包含严格受控ResolvedContract快照") from error
    candidate_id = manifest.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise StoreError("成功ModuleRun缺少manifest.json.candidate_id")
    if result.get("candidate_id") != candidate_id:
        raise StoreError("result.json.candidate_id与manifest.json不一致")
    product_id = contract.identity.get("product_id")
    rule_revision = contract.identity.get("rule_revision")
    if not isinstance(product_id, str) or not product_id:
        raise StoreError("成功ModuleRun的ResolvedContract缺少product_id")
    if isinstance(rule_revision, bool) or not isinstance(rule_revision, int) or rule_revision <= 0:
        raise StoreError("成功ModuleRun的ResolvedContract缺少rule_revision")
    for filename, payload in (("manifest.json", manifest), ("result.json", result)):
        revision = payload.get("rule_revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise StoreError(f"成功ModuleRun的{filename}.rule_revision必须为正整数")
    for field, expected in (("product_id", product_id), ("rule_revision", rule_revision)):
        if manifest.get(field) != expected or result.get(field) != expected:
            raise StoreError(f"成功ModuleRun的{field}与ResolvedContract不一致")


def _publish_directory(stage: Path, final: Path, label: str) -> None:
    """原子发布目录，并把并发输家稳定归类为不可变对象冲突。"""
    try:
        os.replace(stage, final)
    except OSError as error:
        if final.exists():
            raise FileExistsError(f"{label}已存在") from error
        raise


def _data_ref_metadata(ref: DataAssetRef) -> dict[str, Any]:
    """返回DataAssetRef中必须随内容一并认证的逻辑元数据。"""
    return {
        "data_asset_id": ref.data_asset_id,
        "media_type": ref.media_type,
        "schema_id": ref.schema_id,
        "asset_ids": ref.asset_ids,
        "normalized_fields": ref.normalized_fields,
        "coverage": ref.coverage,
        "row_count": ref.row_count,
        "price_convention": ref.price_convention,
        "content_hash": ref.content_hash,
        "lineage": ref.lineage,
        "tenant_id": ref.tenant_id,
        "created_by": ref.created_by,
        "access_scope": ref.access_scope,
        "partition_spec": ref.partition_spec,
    }


class LocalDataStore:
    def __init__(self, root: str | Path) -> None:
        self.root, self._root_identity = _store_root(root, "DataStore根目录")

    def _assert_root_unchanged(self) -> None:
        root, identity = _store_root(self.root, "DataStore根目录")
        if root != self.root or identity != self._root_identity:
            raise StoreError("DataStore根目录在运行期间发生变化")

    def put_bytes(
        self, *, tenant_id: str, data_asset_id: str, payload: bytes, media_type: str, schema_id: str,
        asset_ids: tuple[str, ...] = (), normalized_fields: tuple[str, ...] = (), coverage: Mapping[str, Any] | None = None,
        row_count: int = 0, partition_spec: Mapping[str, Any] | None = None, price_convention: Mapping[str, Any] | None = None, lineage: Mapping[str, Any] | None = None,
        created_by: str = "local", access_scope: tuple[str, ...] = ("read",),
    ) -> DataAssetRef:
        self._assert_root_unchanged()
        tenant = _safe_id(tenant_id, "tenant_id")
        asset = _safe_id(data_asset_id, "data_asset_id")
        content_hash = sha256(payload).hexdigest()
        provisional = DataAssetRef(
            # ``storage_ref`` is part of the public Ref shape but intentionally
            # excluded from the metadata commitment below.  Use a harmless
            # opaque placeholder while deriving that commitment; an empty
            # string would be a second, invalid Ref state.
            data_asset_id=asset, storage_ref=f"pending:{tenant}:{asset}", media_type=media_type, schema_id=schema_id,
            asset_ids=asset_ids, normalized_fields=normalized_fields, coverage=dict(coverage or {}),
            row_count=row_count, price_convention=dict(price_convention or {}), content_hash=content_hash,
            lineage=dict(lineage or {}), tenant_id=tenant, created_by=created_by,
            access_scope=access_scope, partition_spec=dict(partition_spec or {}),
        )
        metadata_hash = semantic_hash(_data_ref_metadata(provisional))
        ref = replace(provisional, storage_ref=f"data:{tenant}:{asset}:{content_hash}:{metadata_hash}")
        tenant_root = _tenant_root(self.root, tenant)
        final = _inside(self.root, tenant_root / "assets" / asset)
        if final.exists():
            raise FileExistsError(f"DataAsset已存在：{data_asset_id}")
        staging_root = _inside(self.root, tenant_root / ".staging")
        staging_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="data-", dir=staging_root) as temporary:
            stage = Path(temporary)
            (stage / "payload.bin").write_bytes(payload)
            (stage / "manifest.json").write_text(
                canonical_json({"storage_ref": ref.storage_ref, "metadata": _data_ref_metadata(ref)}) + "\n",
                encoding="utf-8",
                newline="",
            )
            final.parent.mkdir(parents=True, exist_ok=True)
            _publish_directory(stage, final, f"DataAsset：{data_asset_id}")
        return ref

    def get_ref(self, *, tenant_id: str, data_asset_id: str) -> DataAssetRef:
        """Read one committed immutable asset reference and verify it in full."""

        self._assert_root_unchanged()
        tenant = _safe_id(tenant_id, "tenant_id")
        asset = _safe_id(data_asset_id, "data_asset_id")
        asset_dir = _inside(self.root, _tenant_root(self.root, tenant) / "assets" / asset)
        manifest_path = _inside(asset_dir, asset_dir / "manifest.json")
        try:
            manifest = _json_mapping(_read_regular_bytes(manifest_path, "DataAsset清单"), "DataAsset清单")
            storage_ref = manifest["storage_ref"]
            metadata = manifest["metadata"]
            if not isinstance(storage_ref, str) or not isinstance(metadata, Mapping):
                raise StoreIntegrityError("DataAsset清单缺少受控引用或元数据")
            ref = DataAssetRef(**{**dict(metadata), "storage_ref": storage_ref})
        except (KeyError, TypeError, ValueError, StoreError) as error:
            raise StoreIntegrityError("DataAsset清单无效") from error
        if ref.tenant_id != tenant or ref.data_asset_id != asset:
            raise StoreIntegrityError("DataAsset清单身份与存储位置不一致")
        self.resolve(ref, tenant_id=tenant)
        return ref

    def _verified_payload(self, ref: DataAssetRef, *, tenant_id: str) -> tuple[Path, bytes]:
        self._assert_root_unchanged()
        tenant = _safe_id(tenant_id, "tenant_id")
        if ref.tenant_id != tenant:
            raise PermissionError("DataAssetRef跨租户访问被拒绝")
        metadata = _data_ref_metadata(ref)
        expected_storage_ref = f"data:{tenant}:{ref.data_asset_id}:{ref.content_hash}:{semantic_hash(metadata)}"
        if ref.storage_ref != expected_storage_ref:
            raise StoreError("DataAssetRef.storage_ref无效")
        asset_dir = _inside(self.root, _tenant_root(self.root, tenant) / "assets" / _safe_id(ref.data_asset_id, "data_asset_id"))
        manifest_path = _inside(asset_dir, asset_dir / "manifest.json")
        payload = _inside(asset_dir, asset_dir / "payload.bin")
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise FileNotFoundError("DataAsset清单不存在")
        try:
            manifest = json.loads(_read_regular_bytes(manifest_path, "DataAsset清单"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StoreIntegrityError("DataAsset清单无效") from error
        if manifest != {"storage_ref": ref.storage_ref, "metadata": json.loads(canonical_json(metadata))}:
            raise StoreIntegrityError("DataAssetRef元数据与受控清单不一致")
        if not payload.is_file() or payload.is_symlink():
            raise FileNotFoundError("DataAsset不存在")
        payload_bytes = _read_regular_bytes(payload, "DataAsset")
        if sha256(payload_bytes).hexdigest() != ref.content_hash:
            raise StoreIntegrityError("DataAsset内容哈希不一致")
        return payload, payload_bytes

    def resolve(self, ref: DataAssetRef, *, tenant_id: str) -> Path:
        payload, _ = self._verified_payload(ref, tenant_id=tenant_id)
        return payload

    def read_bytes(self, ref: DataAssetRef, *, tenant_id: str) -> bytes:
        _, payload = self._verified_payload(ref, tenant_id=tenant_id)
        return payload


class LocalResultStore:
    def __init__(self, root: str | Path) -> None:
        self.root, self._root_identity = _store_root(root, "ResultStore根目录")

    def _assert_root_unchanged(self) -> None:
        root, identity = _store_root(self.root, "ResultStore根目录")
        if root != self.root or identity != self._root_identity:
            raise StoreError("ResultStore根目录在运行期间发生变化")

    def predict_module_run_ref(self, *, module: str, tenant_id: str, task_id: str, run_id: str, files: Mapping[str, bytes | str | Mapping[str, Any]]) -> ModuleRunRef:
        """Compute the immutable external anchor before publishing any bytes."""

        _, _, _, _, artifact_manifest, reference = self._prepare_module_run(
            module=module, tenant_id=tenant_id, task_id=task_id, run_id=run_id, files=files,
        )
        if sha256(artifact_manifest).hexdigest() != reference.expected_artifact_manifest_hash:
            raise StoreIntegrityError("ModuleRun预提交产物清单哈希不一致")
        return reference

    def _prepare_module_run(self, *, module: str, tenant_id: str, task_id: str, run_id: str, files: Mapping[str, bytes | str | Mapping[str, Any]]) -> tuple[str, str, str, dict[str, bytes], bytes, ModuleRunRef]:
        if module not in _MODULE_DIR:
            raise StoreError("ResultStore仅接收三个并列计算模块")
        tenant, task, run = _safe_id(tenant_id, "tenant_id"), _safe_id(task_id, "task_id"), _safe_id(run_id, "run_id")
        required = {"manifest.json", "input_snapshot.json", "resolved_contract.json", "data_refs.json", "limitations.json"}
        missing = required - set(files)
        if missing:
            raise StoreError(f"ModuleRun提交缺少正式文件：{','.join(sorted(missing))}")
        if "artifacts/artifact_manifest.json" in files or "commit_marker.json" in files:
            raise StoreError("产物清单与提交标记由ResultStore生成")
        manifest_value = _json_mapping(files["manifest.json"], "manifest.json")
        if manifest_value.get("status") not in _FINAL_RUN_STATES:
            raise StoreError("ModuleRun只提交合法终态")
        status = str(manifest_value["status"])
        for field, expected in {"module": module, "tenant_id": tenant, "task_id": task, "run_id": run}.items():
            if field in manifest_value and manifest_value[field] != expected:
                raise StoreError(f"manifest.json.{field}与ResultStore提交身份不一致")
        has_result, has_error = "result.json" in files, "error.json" in files
        if status in {"succeeded", "partial"} and (not has_result or has_error):
            raise StoreError(f"{status}必须且只能包含真实result.json")
        if status in {"failed", "unsupported", "cancelled", "timed_out"} and (not has_error or has_result):
            raise StoreError(f"{status}必须且只能包含error.json")
        result_name = "result.json" if has_result else "error.json"
        raw_result = files.get("result.json")
        result_value = {} if raw_result is None else _json_mapping(raw_result, "result.json")
        if status in {"succeeded", "partial"}:
            _validate_success_contract(manifest_value, files["resolved_contract.json"], result_value)
        result_hash = sha256(_encode_value(files[result_name])).hexdigest()
        declared_result_hash = manifest_value.get("result_file_hash")
        if declared_result_hash is not None and declared_result_hash != result_hash:
            raise StoreError("manifest.json.result_file_hash与最终结果文件不一致")
        values = dict(files)
        values["manifest.json"] = {**dict(manifest_value), "result_file_hash": result_hash}
        encoded = {name: _encode_value(value) for name, value in values.items()}
        hashes = {name: sha256(payload).hexdigest() for name, payload in encoded.items()}
        artifact_manifest = _encode_value({
            "module": module, "tenant_id": tenant, "task_id": task, "run_id": run,
            "result_file": result_name, "result_file_hash": result_hash, "file_hashes": hashes,
        })
        reference = ModuleRunRef(
            module=module, tenant_id=tenant, task_id=task, run_id=run,
            expected_result_file_hash=result_hash,
            expected_artifact_manifest_hash=sha256(artifact_manifest).hexdigest(),
        )
        return tenant, task, run, encoded, artifact_manifest, reference

    def commit_module_run(self, *, module: str, tenant_id: str, task_id: str, run_id: str, files: Mapping[str, bytes | str | Mapping[str, Any]]) -> ModuleRunRef:
        self._assert_root_unchanged()
        tenant, task, run, encoded, artifact_manifest, reference = self._prepare_module_run(
            module=module, tenant_id=tenant_id, task_id=task_id, run_id=run_id, files=files,
        )
        tenant_root = _tenant_root(self.root, tenant)
        final = _inside(self.root, tenant_root / _MODULE_DIR[module] / task / run)
        if final.exists():
            raise FileExistsError(f"ModuleRun已存在：{run_id}")
        staging_root = _inside(self.root, tenant_root / ".staging")
        staging_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=staging_root) as temporary:
            stage = Path(temporary)
            for name, payload in encoded.items():
                _write_value(stage / _safe_relative(name), payload)
            _write_value(stage / "artifacts" / "artifact_manifest.json", artifact_manifest)
            _write_value(stage / "commit_marker.json", {
                "committed": True,
                "result_file_hash": reference.expected_result_file_hash,
                "artifact_manifest_hash": reference.expected_artifact_manifest_hash,
            })
            final.parent.mkdir(parents=True, exist_ok=True)
            _publish_directory(stage, final, f"ModuleRun：{run_id}")
        committed_manifest = _inside(final, final / "artifacts" / "artifact_manifest.json")
        if sha256(_read_regular_bytes(committed_manifest, "ModuleRun产物清单")).hexdigest() != reference.expected_artifact_manifest_hash:
            raise StoreIntegrityError("ModuleRun发布后的产物清单与提交快照不一致")
        return reference

    def resolve_module_run(self, ref: ModuleRunRef, *, tenant_id: str) -> Path:
        self._assert_root_unchanged()
        tenant = _safe_id(tenant_id, "tenant_id")
        if ref.tenant_id != tenant:
            raise PermissionError("ModuleRunRef跨租户访问被拒绝")
        if ref.module not in _MODULE_DIR:
            raise StoreError("ModuleRunRef.module无效")
        run_dir = _inside(
            self.root,
            _tenant_root(self.root, tenant) / _MODULE_DIR[ref.module]
            / _safe_id(ref.task_id, "task_id") / _safe_id(ref.run_id, "run_id"),
        )
        if not run_dir.is_dir() or run_dir.is_symlink():
            raise FileNotFoundError("ModuleRunRef不存在")
        artifact_manifest_path = run_dir / "artifacts" / "artifact_manifest.json"
        marker_path = run_dir / "commit_marker.json"
        if not artifact_manifest_path.is_file() or artifact_manifest_path.is_symlink() or not marker_path.is_file() or marker_path.is_symlink():
            raise StoreIntegrityError("ModuleRun提交文件缺失")
        artifact_manifest_payload = _read_regular_bytes(artifact_manifest_path, "ModuleRun产物清单")
        artifact_manifest_hash = sha256(artifact_manifest_payload).hexdigest()
        if artifact_manifest_hash != ref.expected_artifact_manifest_hash:
            raise StoreIntegrityError("ModuleRun产物清单与外部RunRef锚点不一致")
        try:
            manifest = json.loads(artifact_manifest_payload)
            marker = json.loads(_read_regular_bytes(marker_path, "ModuleRun提交标记"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StoreIntegrityError("ModuleRun提交文件无效") from error
        if not isinstance(manifest, Mapping):
            raise StoreIntegrityError("ModuleRun产物清单无效")
        if not isinstance(marker, Mapping) or set(marker) != {
            "committed", "result_file_hash", "artifact_manifest_hash",
        }:
            raise StoreIntegrityError("ModuleRun提交标记无效")
        if marker.get("committed") is not True or marker.get("result_file_hash") != manifest.get("result_file_hash"):
            raise StoreIntegrityError("ModuleRun提交标记无效")
        if marker.get("artifact_manifest_hash") != artifact_manifest_hash:
            raise StoreIntegrityError("ModuleRun产物清单哈希不一致")
        if not isinstance(manifest, Mapping) or set(manifest) != {
            "module", "tenant_id", "task_id", "run_id", "result_file", "result_file_hash", "file_hashes",
        }:
            raise StoreIntegrityError("ModuleRun产物清单无效")
        identity = {
            "module": ref.module,
            "tenant_id": tenant,
            "task_id": ref.task_id,
            "run_id": ref.run_id,
        }
        if any(manifest.get(key) != value for key, value in identity.items()):
            raise StoreIntegrityError("ModuleRun产物清单与RunRef不一致")
        if manifest.get("result_file_hash") != ref.expected_result_file_hash:
            raise StoreIntegrityError("ModuleRun结果文件哈希不一致")
        file_hashes = manifest.get("file_hashes")
        if not isinstance(file_hashes, Mapping):
            raise StoreIntegrityError("ModuleRun产物清单无效")
        actual_files: set[str] = set()
        for path in run_dir.rglob("*"):
            if path.is_symlink():
                raise StoreIntegrityError("ModuleRun不得包含符号链接")
            if path.is_file():
                actual_files.add(path.relative_to(run_dir).as_posix())
        if actual_files != set(file_hashes) | {"artifacts/artifact_manifest.json", "commit_marker.json"}:
            raise StoreIntegrityError("ModuleRun文件集合与不可变提交不一致")
        for name, expected in file_hashes.items():
            path = _inside(run_dir, run_dir / _safe_relative(name))
            if not path.is_file() or path.is_symlink() or sha256(_read_regular_bytes(path, f"ModuleRun文件：{name}")).hexdigest() != expected:
                raise StoreIntegrityError(f"ModuleRun文件哈希不一致：{name}")
        result_name = manifest.get("result_file")
        if result_name not in {"result.json", "error.json"}:
            raise StoreIntegrityError("ModuleRun结果文件引用无效")
        result_bytes = _read_regular_bytes(run_dir / result_name, "ModuleRun结果文件")
        if sha256(result_bytes).hexdigest() != ref.expected_result_file_hash:
            raise StoreIntegrityError("ModuleRun结果文件哈希与正式结果不一致")
        return run_dir

    def verify_module_run(self, ref: ModuleRunRef, *, tenant_id: str) -> None:
        """Verify a RunRef without exposing its physical directory to callers."""

        self.resolve_module_run(ref, tenant_id=tenant_id)

    def read_module_run_file(self, ref: ModuleRunRef, name: str, *, tenant_id: str) -> bytes:
        """Return one RunRef-bound artifact without exposing an unchecked read."""

        return self.read_module_run_files(ref, (name,), tenant_id=tenant_id)[name]

    def read_module_run_files(
        self,
        ref: ModuleRunRef,
        names: tuple[str, ...],
        *,
        tenant_id: str,
    ) -> dict[str, bytes]:
        """Read selected files after one complete immutable-run verification."""

        run_dir = self.resolve_module_run(ref, tenant_id=tenant_id)
        artifact_manifest = _read_regular_bytes(run_dir / "artifacts" / "artifact_manifest.json", "ModuleRun产物清单")
        if sha256(artifact_manifest).hexdigest() != ref.expected_artifact_manifest_hash:
            raise StoreIntegrityError("ModuleRun产物清单与外部RunRef锚点不一致")
        try:
            file_hashes = json.loads(artifact_manifest)["file_hashes"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise StoreIntegrityError("ModuleRun产物清单无效") from error
        if not isinstance(file_hashes, Mapping):
            raise StoreIntegrityError("ModuleRun产物清单无效")
        requested: dict[str, Path] = {}
        for name in names:
            relative = _safe_relative(name)
            key = relative.as_posix()
            if key in requested:
                raise StoreError("ModuleRun请求文件重复")
            expected = file_hashes.get(key)
            if not isinstance(expected, str):
                raise FileNotFoundError("ModuleRun未声明请求文件")
            requested[key] = relative
        payloads: dict[str, bytes] = {}
        for key, relative in requested.items():
            payload = _read_regular_bytes(_inside(run_dir, run_dir / relative), f"ModuleRun文件：{key}")
            if sha256(payload).hexdigest() != file_hashes[key]:
                raise StoreIntegrityError(f"ModuleRun文件哈希不一致：{key}")
            payloads[key] = payload
        return payloads

    def read_module_run_bundle(self, ref: ModuleRunRef, *, tenant_id: str) -> dict[str, bytes]:
        """Return the complete verified immutable run as bytes, never a path."""

        run_dir = self.resolve_module_run(ref, tenant_id=tenant_id)
        artifact = _read_regular_bytes(run_dir / "artifacts" / "artifact_manifest.json", "ModuleRun产物清单")
        try:
            file_hashes = json.loads(artifact)["file_hashes"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise StoreIntegrityError("ModuleRun产物清单无效") from error
        if not isinstance(file_hashes, Mapping):
            raise StoreIntegrityError("ModuleRun产物清单无效")
        bundle: dict[str, bytes] = {}
        for name, expected in file_hashes.items():
            relative = _safe_relative(str(name))
            key = relative.as_posix()
            payload = _read_regular_bytes(_inside(run_dir, run_dir / relative), f"ModuleRun文件：{key}")
            if sha256(payload).hexdigest() != expected:
                raise StoreIntegrityError(f"ModuleRun文件哈希不一致：{key}")
            bundle[key] = payload
        bundle["artifacts/artifact_manifest.json"] = artifact
        bundle["commit_marker.json"] = _read_regular_bytes(run_dir / "commit_marker.json", "ModuleRun提交标记")
        return bundle


__all__ = ("LocalDataStore", "LocalResultStore", "StoreError", "StoreIntegrityError")
