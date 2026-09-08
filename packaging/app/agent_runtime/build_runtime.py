"""Fail-closed native packaging skeleton for the OptionHelper agent runtime.

The runtime source is JavaScript, but the App must ship a native executable
per target platform. This module builds a Node Single Executable Application
candidate only when the host, local tools, and source closure are all present.
Locked npm dependencies are restored into the disposable build directory after
source freezing; the repository source tree is never used as a dependency
cache. The module never overwrites an artifact or marks a platform as supported.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOURCE_ROOT = REPO_ROOT / "products" / "app" / "runtime" / "optionhelper_agent_runtime"
BUILD_TOOLS_ROOT = Path(__file__).resolve().parent
MIN_NODE_VERSION = (22, 19, 0)
SEA_SENTINEL = "NODE_SEA_FUSE_fce680ab2cc467b6e072b8b5df1996b2"
ARTIFACT_MANIFEST_SUFFIX = ".manifest.json"
RUNTIME_RESOURCE_DIR = "agent-runtime"
RUNTIME_RESOURCE_BASENAME = "optionhelper-agent-runtime"
NODE_LICENSE_RESOURCE = "Nodejs-LICENSE.txt"
RUNTIME_ARTIFACT_ENV = "OPTIONHELPER_AGENT_RUNTIME_ARTIFACT"
RUNTIME_PATH_ENV = "OPTIONHELPER_AGENT_RUNTIME_PATH"
RUNTIME_MODE_ENV = "OPTIONHELPER_AGENT_RUNTIME_MODE"
REQUIRE_NATIVE_RUNTIME_ENV = "OPTIONHELPER_REQUIRE_NATIVE_RUNTIME"
ALLOW_SOURCE_RUNTIME_FALLBACK_ENV = "OPTIONHELPER_ALLOW_SOURCE_RUNTIME_FALLBACK"
LOCAL_VERIFIED = "local_verified"
DEVELOPMENT_ONLY = "development_only"
EXTERNAL_COMMAND_TIMEOUT_SECONDS = 900


class RuntimeBuildError(RuntimeError):
    """A preflight or build failure that must stop packaging."""


@dataclass(frozen=True)
class RuntimeTarget:
    key: str
    artifact_stem: str
    system: str
    machine: str
    extension: str


TARGETS: dict[str, RuntimeTarget] = {
    "macos-arm64": RuntimeTarget(
        key="macos-arm64",
        artifact_stem="optionhelper-agent-runtime-macos-arm64",
        system="darwin",
        machine="arm64",
        extension="",
    ),
    "windows-x64": RuntimeTarget(
        key="windows-x64",
        artifact_stem="optionhelper-agent-runtime-windows-x64",
        system="win32",
        machine="x86_64",
        extension=".exe",
    ),
}


@dataclass(frozen=True)
class RuntimeBuildPlan:
    target: RuntimeTarget
    source_root: Path
    output_root: Path
    artifact: Path
    support_status: str = "unverified_local_candidate"

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.key,
            "artifact_stem": self.target.artifact_stem,
            "artifact": str(self.artifact),
            "source_root": str(self.source_root),
            "support_status": self.support_status,
        }


@dataclass(frozen=True)
class RuntimeBuildResult:
    plan: RuntimeBuildPlan
    artifact: Path
    sha256: str
    manifest: Path | None = None
    support_status: str = LOCAL_VERIFIED
    probe: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.plan.to_dict(),
            "artifact": str(self.artifact),
            "sha256": self.sha256,
            "manifest": str(self.manifest) if self.manifest is not None else None,
            "support_status": self.support_status,
            "probe": self.probe,
        }


def _target(value: str | RuntimeTarget) -> RuntimeTarget:
    if isinstance(value, RuntimeTarget):
        return value
    try:
        return TARGETS[value]
    except KeyError as error:
        raise RuntimeBuildError(f"不支持的Agent运行时目标：{value}") from error


def expected_artifact_name(value: str | RuntimeTarget, *, with_extension: bool = True) -> str:
    target = _target(value)
    return target.artifact_stem + (target.extension if with_extension else "")


def runtime_resource_name(value: str | RuntimeTarget) -> str:
    """Return the stable filename used inside an App's Resources directory."""

    target = _target(value)
    return RUNTIME_RESOURCE_BASENAME + target.extension


def artifact_manifest_path(artifact: Path) -> Path:
    return artifact.with_name(artifact.name + ARTIFACT_MANIFEST_SUFFIX)


def _normalized_machine(machine: str) -> str:
    return machine.casefold().replace("amd64", "x86_64").replace("aarch64", "arm64")


def make_build_plan(
    value: str | RuntimeTarget,
    *,
    source_root: Path = DEFAULT_SOURCE_ROOT,
    output_root: Path,
) -> RuntimeBuildPlan:
    target = _target(value)
    source = source_root.resolve()
    output = output_root.resolve()
    return RuntimeBuildPlan(
        target=target,
        source_root=source,
        output_root=output,
        artifact=output / expected_artifact_name(target),
    )


def _host_matches(target: RuntimeTarget, *, system: str, machine: str) -> bool:
    return system == target.system and _normalized_machine(machine) == target.machine


def _is_executable(path: Path, *, system: str) -> bool:
    if system == "win32" or path.suffix.casefold() == ".exe":
        return True
    return bool(path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def _read_json(path: Path, message: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeBuildError(message) from error
    if not isinstance(value, dict):
        raise RuntimeBuildError(message)
    return value


def _runtime_manifest(
    target: RuntimeTarget,
    artifact: Path,
    *,
    support_status: str = LOCAL_VERIFIED,
    host_system: str | None = None,
    host_machine: str | None = None,
    probe: Mapping[str, Any] | None = None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    system = sys.platform if host_system is None else host_system
    machine = platform.machine() if host_machine is None else host_machine
    digest = _sha256(artifact)
    size = artifact.stat().st_size
    if support_status != LOCAL_VERIFIED:
        raise RuntimeBuildError("原生Agent运行时清单只能记录local_verified")
    manifest = {
        "schema": "optionhelper.agent-runtime-manifest",
        "target": target.key,
        "artifact": artifact.name,
        "sha256": digest,
        "size": size,
        "hash": {
            "algorithm": "sha256",
            "value": digest,
            "size": size,
            "status": LOCAL_VERIFIED,
        },
        "host": {"system": system, "machine": _normalized_machine(machine)},
        "support_status": support_status,
        "verification": {
            "status": LOCAL_VERIFIED,
            "probe": dict(probe) if probe is not None else None,
        },
        "runtime_version": str((probe or {}).get("runtime_version", "unknown")),
        "protocol_version": str((probe or {}).get("protocol_version", "unknown")),
        "capabilities": dict((probe or {}).get("capabilities", {})),
    }
    if probe is not None and "node_version" in probe:
        manifest["node_version"] = probe["node_version"]
    if source_root is not None:
        source_hashes = _source_content_hashes(source_root)
        manifest.update({
            "package_lock_sha256": source_hashes["package-lock.json"],
            "source_content_hashes": source_hashes,
            "source_tree_hash": _source_tree_hash(source_hashes),
        })
    return manifest


def write_runtime_manifest(
    artifact: Path,
    value: str | RuntimeTarget,
    *,
    host_system: str | None = None,
    host_machine: str | None = None,
    probe: Mapping[str, Any] | None = None,
    source_root: Path | None = None,
) -> Path:
    """Write a manifest only after a locally built native artifact exists."""

    target = _target(value)
    manifest_path = artifact_manifest_path(artifact)
    manifest_path.write_text(
        json.dumps(
            _runtime_manifest(
                target,
                artifact,
                host_system=host_system,
                host_machine=host_machine,
                probe=probe,
                source_root=source_root,
            ),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="",
    )
    return manifest_path


def verify_runtime_candidate(
    artifact: Path,
    value: str | RuntimeTarget,
    *,
    manifest_path: Path | None = None,
    host_system: str | None = None,
    host_machine: str | None = None,
    source_root: Path | None = None,
    require_source_provenance: bool = False,
) -> dict[str, Any]:
    """Accept only a native, target-named artifact with a matching manifest."""

    target = _target(value)
    path = artifact.expanduser().resolve()
    system = sys.platform if host_system is None else host_system
    machine = platform.machine() if host_machine is None else host_machine
    if not _host_matches(target, system=system, machine=machine):
        raise RuntimeBuildError(f"运行时候选宿主{system}/{machine}不匹配{target.key}")
    if path.name != expected_artifact_name(target):
        raise RuntimeBuildError(f"运行时候选名称不匹配：需要{expected_artifact_name(target)}")
    if not path.is_file():
        raise RuntimeBuildError(f"运行时候选不存在：{path}")
    if path.stat().st_size <= 0 or not _is_executable(path, system=system):
        raise RuntimeBuildError(f"运行时候选不可执行：{path}")
    manifest_file = (manifest_path or artifact_manifest_path(path)).expanduser().resolve()
    if not manifest_file.is_file():
        raise RuntimeBuildError(f"运行时候选缺少清单：{manifest_file}")
    manifest = _read_json(manifest_file, "运行时候选清单不可解析")
    expected_hash = _sha256(path)
    expected_size = path.stat().st_size
    if manifest.get("schema") != "optionhelper.agent-runtime-manifest":
        raise RuntimeBuildError("运行时候选清单Schema不匹配")
    if manifest.get("target") != target.key or manifest.get("artifact") != path.name:
        raise RuntimeBuildError("运行时候选清单目标或文件名不匹配")
    if manifest.get("sha256") != expected_hash or manifest.get("size") != expected_size:
        raise RuntimeBuildError("运行时候选清单哈希或大小不匹配")
    hash_record = manifest.get("hash")
    if hash_record is not None and (
        not isinstance(hash_record, dict)
        or hash_record.get("algorithm") != "sha256"
        or hash_record.get("value") != expected_hash
        or hash_record.get("size") != expected_size
        or hash_record.get("status") != LOCAL_VERIFIED
    ):
        raise RuntimeBuildError("运行时候选清单哈希记录无效")
    if manifest.get("support_status") != LOCAL_VERIFIED:
        raise RuntimeBuildError("运行时候选未标记为本机已验证")
    verification = manifest.get("verification")
    if verification is not None and (
        not isinstance(verification, dict) or verification.get("status") != LOCAL_VERIFIED
    ):
        raise RuntimeBuildError("运行时候选清单验证记录无效")
    host = manifest.get("host")
    if not isinstance(host, dict) or host.get("system") != system or host.get("machine") != _normalized_machine(machine):
        raise RuntimeBuildError("运行时候选清单宿主不匹配")
    if require_source_provenance:
        if source_root is None:
            raise RuntimeBuildError("正式运行时候选验真缺少当前源码根目录")
        source_hashes = _source_content_hashes(source_root)
        if (
            manifest.get("package_lock_sha256") != source_hashes["package-lock.json"]
            or manifest.get("source_content_hashes") != source_hashes
            or manifest.get("source_tree_hash") != _source_tree_hash(source_hashes)
        ):
            raise RuntimeBuildError("运行时候选绑定的源码或锁文件已漂移")
    return manifest


def resolve_runtime_candidate(
    value: str | RuntimeTarget,
    *,
    explicit: Path | None = None,
    repository_root: Path | None = None,
    formal: bool = False,
) -> Path | None:
    """Resolve only an explicitly configured or locally staged candidate."""

    target = _target(value)
    configured = explicit
    if formal and configured is None:
        return None
    if configured is None:
        raw = os.environ.get(RUNTIME_ARTIFACT_ENV, "").strip()
        configured = Path(raw).expanduser() if raw else None
    if configured is not None:
        candidate = configured.resolve()
        if not candidate.exists():
            raise RuntimeBuildError(f"显式配置的运行时候选不存在：{candidate}")
        return candidate
    root = (repository_root or Path(__file__).resolve().parents[3]).resolve()
    candidate = root / "dist" / expected_artifact_name(target)
    return candidate if candidate.exists() else None


def stage_runtime_candidate(
    resources: Path,
    value: str | RuntimeTarget,
    candidate: Path | None,
    *,
    host_system: str | None = None,
    host_machine: str | None = None,
    source_root: Path | None = None,
    require_source_provenance: bool = False,
) -> dict[str, Any]:
    """Copy one verified candidate to the explicit App Resources location."""

    target = _target(value)
    target_root = resources / RUNTIME_RESOURCE_DIR
    if candidate is None:
        return {"status": "disabled", "resource_path": None, "manifest_path": None}
    manifest = verify_runtime_candidate(
        candidate,
        target,
        host_system=host_system,
        host_machine=host_machine,
        source_root=source_root,
        require_source_provenance=require_source_provenance,
    )
    node_version = manifest.get("node_version")
    if require_source_provenance and node_version is None:
        raise RuntimeBuildError("Agent Runtime缺少Node版本及对应许可证来源，请重新构建原生产物")
    node_license = _node_license_source(node_version) if node_version is not None else None
    target_root.mkdir(parents=True, exist_ok=True)
    executable = target_root / runtime_resource_name(target)
    shutil.copy2(candidate, executable)
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    if node_license is not None:
        shutil.copy2(node_license, target_root / NODE_LICENSE_RESOURCE)
    resource_manifest = {
        **manifest,
        "resource_path": f"{RUNTIME_RESOURCE_DIR}/{executable.name}",
        "resource_sha256": _sha256(executable),
        "resource_size": executable.stat().st_size,
        "resource_hash": {
            "algorithm": "sha256",
            "value": _sha256(executable),
            "size": executable.stat().st_size,
            "status": LOCAL_VERIFIED,
        },
    }
    manifest_target = target_root / f"{RUNTIME_RESOURCE_BASENAME}.manifest.json"
    manifest_target.write_text(
        json.dumps(
            {**resource_manifest, "summary_status": LOCAL_VERIFIED},
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n",
        encoding="utf-8",
        newline="",
    )
    return {
        "status": LOCAL_VERIFIED,
        "support_status": LOCAL_VERIFIED,
        "target": target.key,
        "resource_path": f"{RUNTIME_RESOURCE_DIR}/{executable.name}",
        "manifest_path": f"{RUNTIME_RESOURCE_DIR}/{manifest_target.name}",
        "sha256": resource_manifest["resource_sha256"],
        "size": resource_manifest["resource_size"],
        "hash": resource_manifest["resource_hash"],
        "manifest_sha256": _sha256(manifest_target),
    }


def _staged_resource(resources: Path, value: object, label: str) -> Path:
    relative = Path(str(value or ""))
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeBuildError(f"App Resources运行时{label}路径越界")
    root = resources.expanduser().resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise RuntimeBuildError(f"App Resources运行时{label}路径越界")
    return path


def verify_staged_runtime(
    resources: Path,
    value: str | RuntimeTarget,
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify the staged file and its in-Resources manifest."""

    status = str(summary.get("status", "disabled"))
    if status == "disabled":
        return dict(summary)
    if status != LOCAL_VERIFIED or summary.get("support_status", LOCAL_VERIFIED) != LOCAL_VERIFIED:
        raise RuntimeBuildError("App Manifest中的运行时状态不受支持")
    target = _target(value)
    resource_path = _staged_resource(resources, summary.get("resource_path"), "可执行文件")
    manifest_path = _staged_resource(resources, summary.get("manifest_path"), "清单")
    if resource_path.name != runtime_resource_name(target) or not resource_path.is_file():
        raise RuntimeBuildError("App Resources缺少运行时可执行文件")
    if not manifest_path.is_file():
        raise RuntimeBuildError("App Resources缺少运行时清单")
    manifest = _read_json(manifest_path, "App Resources运行时清单不可解析")
    if manifest.get("support_status") != LOCAL_VERIFIED or manifest.get("summary_status") != LOCAL_VERIFIED:
        raise RuntimeBuildError("App Resources运行时清单未记录local_verified")
    if manifest.get("target") != target.key:
        raise RuntimeBuildError("App Resources运行时清单目标不匹配")
    if manifest.get("artifact") != expected_artifact_name(target):
        raise RuntimeBuildError("App Resources运行时清单原始产物名称不匹配")
    if manifest.get("resource_path") != summary.get("resource_path"):
        raise RuntimeBuildError("App Resources运行时路径与Manifest不匹配")
    digest = _sha256(resource_path)
    size = resource_path.stat().st_size
    if manifest.get("sha256") != digest or manifest.get("size") != size:
        raise RuntimeBuildError("App Resources原生运行时清单哈希或大小与文件不匹配")
    artifact_hash = manifest.get("hash")
    if (
        not isinstance(artifact_hash, dict)
        or artifact_hash.get("algorithm") != "sha256"
        or artifact_hash.get("value") != digest
        or artifact_hash.get("size") != size
        or artifact_hash.get("status") != LOCAL_VERIFIED
    ):
        raise RuntimeBuildError("App Resources原生产物哈希记录无效")
    if manifest.get("resource_sha256") != digest or manifest.get("resource_size") != size:
        raise RuntimeBuildError("App Resources运行时哈希或大小不匹配")
    resource_hash = manifest.get("resource_hash")
    if (
        not isinstance(resource_hash, dict)
        or resource_hash.get("algorithm") != "sha256"
        or resource_hash.get("value") != digest
        or resource_hash.get("size") != size
        or resource_hash.get("status") != LOCAL_VERIFIED
    ):
        raise RuntimeBuildError("App Resources运行时哈希记录无效")
    if summary.get("sha256") != digest or summary.get("size") != size:
        raise RuntimeBuildError("App Manifest运行时哈希与Resources不匹配")
    if summary.get("manifest_sha256") is not None and summary.get("manifest_sha256") != _sha256(manifest_path):
        raise RuntimeBuildError("App Manifest运行时清单哈希不匹配")
    if not _is_executable(resource_path, system=_target(value).system):
        raise RuntimeBuildError("App Resources运行时不可执行")
    if "node_version" in manifest:
        source_license = _node_license_source(manifest["node_version"])
        packaged_license = resource_path.parent / NODE_LICENSE_RESOURCE
        if (
            packaged_license.is_symlink()
            or not packaged_license.is_file()
            or packaged_license.read_bytes() != source_license.read_bytes()
        ):
            raise RuntimeBuildError("App Resources的Node完整许可证缺失或与受控来源不一致")
    return {**dict(summary), "status": LOCAL_VERIFIED, "support_status": LOCAL_VERIFIED}


def prepare_staged_runtime(
    resources: Path,
    value: str | RuntimeTarget,
    candidate: Path | None,
    *,
    required: bool,
    allow_source_fallback: bool = False,
    host_system: str | None = None,
    host_machine: str | None = None,
    source_root: Path | None = None,
    require_source_provenance: bool = False,
) -> dict[str, Any]:
    """Stage, hash, probe, and classify one App runtime candidate.

    A formal App may only take the ``local_verified`` branch. The disabled
    branch is intentionally labelled development-only and can be selected
    only by a source-development caller explicitly opting into fallback.
    """

    target = _target(value)
    if candidate is None:
        if required:
            raise RuntimeBuildError(
                f"正式{target.key}App缺少已验证原生Agent运行时；"
                "请提供OPTIONHELPER_AGENT_RUNTIME_ARTIFACT或先构建本机runtime"
            )
        if not allow_source_fallback:
            raise RuntimeBuildError(
                "源码开发回退未显式开启；仅允许显式设置"
                f"{ALLOW_SOURCE_RUNTIME_FALLBACK_ENV}=1"
            )
        return {
            "status": "disabled",
            "support_status": DEVELOPMENT_ONLY,
            "fallback": "source_explicit",
            "resource_path": None,
            "manifest_path": None,
        }

    summary = stage_runtime_candidate(
        resources,
        target,
        candidate,
        host_system=host_system,
        host_machine=host_machine,
        source_root=source_root,
        require_source_provenance=require_source_provenance,
    )
    verified = verify_staged_runtime(resources, target, summary)
    resource_path = resources / str(verified["resource_path"])
    probe = probe_runtime_process(resource_path)
    if probe.get("status") != LOCAL_VERIFIED or probe.get("support_status") != LOCAL_VERIFIED:
        raise RuntimeBuildError("原生Agent运行时探测未记录local_verified")
    manifest_path = _staged_resource(resources, verified["manifest_path"], "清单")
    manifest = _read_json(manifest_path, "App Resources运行时清单不可解析")
    manifest["verification"] = {"status": LOCAL_VERIFIED, "probe": probe}
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )
    verified = verify_staged_runtime(
        resources,
        target,
        {**verified, "manifest_sha256": _sha256(manifest_path)},
    )
    return {
        **verified,
        "status": LOCAL_VERIFIED,
        "support_status": LOCAL_VERIFIED,
        "probe": probe,
    }


def probe_runtime_process(artifact: Path, *, timeout: float = 8.0) -> dict[str, Any]:
    """Start the staged executable directly and require clean shutdown."""

    path = artifact.expanduser().resolve()
    if not path.is_file():
        raise RuntimeBuildError(f"运行时进程探测文件不存在：{path}")
    root_session_id = "packaging-probe-root"
    with tempfile.TemporaryDirectory(prefix="optionhelper-agent-runtime-probe-") as session_root:
        requests = "\n".join(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "probe-init",
                        "method": "runtime.initialize",
                        "params": {"sessionRoot": session_root, "rootSessionId": root_session_id},
                    },
                    separators=(",", ":"),
                ),
                json.dumps(
                    {"jsonrpc": "2.0", "id": "probe-capabilities", "method": "runtime.capabilities", "params": {}},
                    separators=(",", ":"),
                ),
                json.dumps(
                    {"jsonrpc": "2.0", "id": "probe-shutdown", "method": "runtime.shutdown", "params": {}},
                    separators=(",", ":"),
                ),
            )
        ) + "\n"
        try:
            process = subprocess.Popen(
                [str(path)],
                cwd=str(path.parent),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=os.name != "nt",
            )
            stdout, stderr = process.communicate(requests, timeout=max(0.5, float(timeout)))
        except (OSError, subprocess.TimeoutExpired) as error:
            if "process" in locals() and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            raise RuntimeBuildError(f"运行时进程探测未能在期限内结束：{path}") from error
        if process.poll() is None:
            process.terminate()
            raise RuntimeBuildError("运行时进程探测结束后仍有残留进程")
        if process.returncode not in (0, None):
            raise RuntimeBuildError(f"运行时进程探测失败：{stderr[-500:]}")
    responses: dict[str, Any] = {}
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and message.get("id") in {"probe-init", "probe-capabilities", "probe-shutdown"}:
            responses[str(message["id"])] = message
    init = responses.get("probe-init")
    capabilities_response = responses.get("probe-capabilities")
    shutdown = responses.get("probe-shutdown")
    if not isinstance(init, dict) or isinstance(init.get("error"), dict):
        raise RuntimeBuildError("运行时进程探测初始化失败")
    if init.get("result", {}).get("rootSessionId") != root_session_id:
        raise RuntimeBuildError("运行时进程探测未返回指定Root Session")
    capabilities_result = capabilities_response.get("result", {}) if isinstance(capabilities_response, dict) else {}
    capabilities = capabilities_result.get("capabilities") if isinstance(capabilities_result, dict) else None
    required_capabilities = {"persistentSessions", "nativeToolLoop", "streaming", "oneShotSubagent", "continuableSubagent"}
    if not isinstance(capabilities, dict) or any(capabilities.get(name) is not True for name in required_capabilities):
        raise RuntimeBuildError("运行时进程探测未返回完整Agent能力")
    if not isinstance(shutdown, dict) or isinstance(shutdown.get("error"), dict):
        raise RuntimeBuildError("运行时进程探测关闭失败")
    digest = _sha256(path)
    size = path.stat().st_size
    return {
        "status": LOCAL_VERIFIED,
        "support_status": LOCAL_VERIFIED,
        "returncode": 0,
        "sha256": digest,
        "size": size,
        "hash": {
            "algorithm": "sha256",
            "value": digest,
            "size": size,
            "status": LOCAL_VERIFIED,
        },
        "responses": tuple(sorted(responses)),
        "runtime_version": str(init.get("result", {}).get("version", "unknown")),
        "protocol_version": str(capabilities_result.get("protocolVersion", init.get("result", {}).get("protocolVersion", "unknown"))),
        "capabilities": capabilities,
    }


def _node_license_source(version: object) -> Path:
    """Select version-matched, vendored Node and bundled dependency notices."""

    if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise RuntimeBuildError("Node许可证来源缺少有效Node版本")
    source = BUILD_TOOLS_ROOT / "licenses" / f"node-v{version}-LICENSE.txt"
    if source.is_symlink() or not source.is_file():
        raise RuntimeBuildError(f"当前Node v{version}未附带匹配许可证；请安装仓库支持的Node版本：" + ", ".join(path.name.removeprefix("node-v").removesuffix("-LICENSE.txt") for path in sorted((BUILD_TOOLS_ROOT / "licenses").glob("node-v*-LICENSE.txt"))))
    if not source.read_bytes().startswith(b"Node.js is licensed for use as follows:"):
        raise RuntimeBuildError(f"Node v{version}许可证来源内容无效")
    return source


def _node_version(node: Path) -> tuple[int, int, int]:
    try:
        result = subprocess.run(
            [str(node), "--version"],
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=EXTERNAL_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeBuildError(f"读取Node版本超时：{node}") from error
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeBuildError(f"无法读取Node版本：{node}") from error
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", result.stdout.strip())
    if match is None:
        raise RuntimeBuildError("Node版本输出无法解析，已停止打包")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _resolve_tool(explicit: Path | None, name: str) -> Path:
    candidate = explicit if explicit is not None else shutil.which(name)
    if candidate is None:
        raise RuntimeBuildError(f"缺少本地打包依赖{name}；不下载、不生成伪支持产物")
    path = Path(candidate)
    if not path.is_file():
        raise RuntimeBuildError(f"打包依赖{name}不存在：{path}")
    executable = path.suffix.casefold() in {".exe", ".cmd", ".bat"} or bool(path.stat().st_mode & stat.S_IXUSR)
    if not executable:
        raise RuntimeBuildError(f"打包依赖{name}不可执行：{path}")
    return path.resolve()


def _validate_source(source: Path) -> None:
    required = (
        source / "package.json",
        source / "package-lock.json",
        source / "tsconfig.json",
        source / "src" / "entrypoint" / "runtime.ts",
    )
    if not source.is_dir() or any(not path.is_file() for path in required):
        raise RuntimeBuildError("Agent运行时缺少锁定源码、配置或package-lock.json")
    package_file = source / "package.json"
    try:
        package = json.loads(package_file.read_text(encoding="utf-8"))
        lock = json.loads((source / "package-lock.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeBuildError("Agent运行时锁定依赖文件不可解析") from error
    if package.get("name") != "@optionhelper/agent-runtime" or lock.get("lockfileVersion") != 3:
        raise RuntimeBuildError("Agent运行时包名或锁文件版本无效")
    declared = package.get("devDependencies")
    locked = lock.get("packages", {}).get("", {}).get("devDependencies") if isinstance(lock.get("packages"), dict) else None
    if not isinstance(declared, dict) or declared != locked:
        raise RuntimeBuildError("Agent运行时package.json与package-lock.json依赖不一致")
    for version in declared.values():
        if not isinstance(version, str) or re.search(r"[~^*><=| ]", version):
            raise RuntimeBuildError("Agent运行时构建依赖必须使用精确锁定版本")
    if package.get("dependencies") or package.get("optionalDependencies") or package.get("peerDependencies"):
        raise RuntimeBuildError("Agent运行时成品闭包不得包含外部运行依赖")
    _source_content_hashes(source)
    for path in (source / "src").rglob("*"):
        if path.is_file() and path.suffix in {".js", ".cjs", ".mjs", ".ts"}:
            content = path.read_text(encoding="utf-8")
            for specifier in re.findall(
                r"(?:from\s+|require\s*\(\s*|import\s*\(\s*)['\"]([^'\"]+)['\"]",
                content,
            ):
                if not (
                    specifier.startswith("node:")
                    or specifier.startswith("./")
                    or specifier.startswith("../")
                ):
                    raise RuntimeBuildError(f"Agent运行时依赖外部Node模块：{specifier}")


def _validate_build_tools(source: Path) -> None:
    package_file = source / "package.json"
    lock_file = source / "package-lock.json"
    if any(not path.is_file() or path.is_symlink() for path in (package_file, lock_file)):
        raise RuntimeBuildError("Agent Runtime打包工具缺少package.json或package-lock.json")
    try:
        package = json.loads(package_file.read_text(encoding="utf-8"))
        lock = json.loads(lock_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeBuildError("Agent Runtime打包工具锁文件不可解析") from error
    declared = package.get("dependencies")
    locked = (
        lock.get("packages", {}).get("", {}).get("dependencies")
        if isinstance(lock.get("packages"), dict)
        else None
    )
    if (
        package.get("name") != "optionhelper-agent-runtime-build-tools"
        or lock.get("lockfileVersion") != 3
        or not isinstance(declared, dict)
        or declared != locked
        or "postject" not in declared
    ):
        raise RuntimeBuildError("Agent Runtime打包工具package.json与package-lock.json不一致")
    if any(
        not isinstance(version, str) or re.search(r"[~^*><=| ]", version)
        for version in declared.values()
    ):
        raise RuntimeBuildError("Agent Runtime打包工具依赖必须使用精确锁定版本")


def _copy_locked_runtime_source(source: Path, destination: Path) -> None:
    """Copy only provenance-bound runtime inputs, never local dependencies."""

    hashes = _source_content_hashes(source)
    destination.mkdir(parents=True, exist_ok=False)
    for relative in hashes:
        origin = source / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, target)


def _copy_locked_build_tools(source: Path, destination: Path) -> None:
    _validate_build_tools(source)
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("package.json", "package-lock.json"):
        shutil.copy2(source / name, destination / name)


def _npm_ci_command(npm: Path, root: Path) -> list[str]:
    arguments = ["ci", "--prefix", str(root), "--ignore-scripts"]
    if npm.suffix.casefold() in {".cmd", ".bat"}:
        # npm's Windows wrapper delegates to this CLI. Calling Node directly
        # preserves spaces, Unicode and shell metacharacters in checkout paths.
        cli = npm.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        node = npm.parent / "node.exe"
        if not cli.is_file() or not node.is_file():
            raise RuntimeBuildError("npm安装不完整：缺少同目录node.exe或node_modules/npm/bin/npm-cli.js")
        return [str(node), str(cli), *arguments]
    return [str(npm), *arguments]


def _restore_locked_project(npm: Path, root: Path, *, label: str) -> None:
    try:
        _run(_npm_ci_command(npm, root), cwd=root)
    except RuntimeBuildError as error:
        raise RuntimeBuildError(f"{label}在冻结临时目录执行npm ci失败：{error}") from error


def _prepare_locked_build_workspace(
    source: Path,
    destination: Path,
    *,
    npm: Path,
    build_tools_source: Path = BUILD_TOOLS_ROOT,
) -> tuple[Path, Path]:
    """Restore both lockfiles inside one disposable, writable build tree."""

    runtime_source = destination / "runtime-source"
    build_tools = destination / "build-tools"
    _copy_locked_runtime_source(source, runtime_source)
    _copy_locked_build_tools(build_tools_source, build_tools)
    _restore_locked_project(npm, runtime_source, label="Agent Runtime源码依赖")
    _restore_locked_project(npm, build_tools, label="Agent Runtime打包工具依赖")

    esbuild = runtime_source / "node_modules" / "esbuild" / "bin" / "esbuild"
    postject = build_tools / "node_modules" / "postject" / "dist" / "cli.js"
    if not esbuild.is_file():
        raise RuntimeBuildError("Agent Runtime临时构建目录npm ci后仍缺少锁定esbuild")
    if not postject.is_file():
        raise RuntimeBuildError("Agent Runtime临时构建目录npm ci后仍缺少锁定postject")
    return runtime_source, postject


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_content_hashes(source_root: Path) -> dict[str, str]:
    """Hash the exact build-source closure, excluding dependencies and tests."""

    root = source_root.expanduser().resolve()
    required = tuple(root / name for name in ("package.json", "package-lock.json", "tsconfig.json"))
    if any(not path.is_file() or path.is_symlink() for path in required) or not (root / "src").is_dir():
        raise RuntimeBuildError("Agent运行时源码证明缺少锁文件、配置或src目录")
    selected = [*required, *(path for path in (root / "src").rglob("*") if path.is_file())]
    hashes: dict[str, str] = {}
    for path in sorted(selected):
        if path.is_symlink():
            raise RuntimeBuildError(f"Agent运行时源码证明拒绝符号链接：{path}")
        relative = path.relative_to(root).as_posix()
        hashes[relative] = _sha256(path)
    return hashes


def _source_tree_hash(content_hashes: Mapping[str, str]) -> str:
    import hashlib

    payload = json.dumps(dict(sorted(content_hashes.items())), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _create_sea_entry(source: Path, destination: Path, *, node: Path | None = None) -> Path:
    """Bundle the locked TypeScript Runtime into one dependency-free SEA entry."""

    node_path = node or _resolve_tool(None, "node")
    esbuild = source / "node_modules" / "esbuild" / "bin" / "esbuild"
    if not esbuild.is_file():
        raise RuntimeBuildError("Agent Runtime临时构建目录缺少锁定esbuild")
    _run([
        str(node_path), str(esbuild), str(source / "src" / "entrypoint" / "runtime.ts"),
        "--bundle", "--platform=node", "--target=node22.19", "--format=cjs",
        f"--outfile={destination}",
    ], cwd=source)
    if not destination.is_file() or destination.stat().st_size < 1_000:
        raise RuntimeBuildError("Agent运行时TypeScript未生成有效SEA入口")
    return destination


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=EXTERNAL_COMMAND_TIMEOUT_SECONDS,
        )
        return completed.stdout
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        raise RuntimeBuildError(f"本地打包命令超时：{' '.join(command)}\n{output}") from error
    except (OSError, subprocess.CalledProcessError) as error:
        output = getattr(error, "stdout", "") or ""
        raise RuntimeBuildError(f"本地打包命令失败：{' '.join(command)}\n{output}") from error


def preflight_runtime(
    value: str | RuntimeTarget,
    *,
    source_root: Path = DEFAULT_SOURCE_ROOT,
    output_root: Path,
    host_system: str | None = None,
    host_machine: str | None = None,
    node_path: Path | None = None,
    npm_path: Path | None = None,
    codesign_path: Path | None = None,
) -> tuple[RuntimeBuildPlan, Path, Path, Path | None]:
    plan = make_build_plan(value, source_root=source_root, output_root=output_root)
    system = sys.platform if host_system is None else host_system
    machine = platform.machine() if host_machine is None else host_machine
    if not _host_matches(plan.target, system=system, machine=machine):
        raise RuntimeBuildError(
            f"当前宿主{system}/{machine}不匹配{plan.target.key}；禁止交叉构建或标记平台支持"
        )
    _validate_source(plan.source_root)
    node = _resolve_tool(node_path, "node")
    node_version = _node_version(node)
    if node_version < MIN_NODE_VERSION:
        raise RuntimeBuildError(f"Node版本低于{MIN_NODE_VERSION}，已停止打包")
    _node_license_source(".".join(str(part) for part in node_version))
    npm = _resolve_tool(npm_path, "npm")
    _validate_build_tools(BUILD_TOOLS_ROOT)
    codesign = None
    if plan.target.system == "darwin":
        codesign = _resolve_tool(codesign_path, "codesign")
    if plan.artifact.exists():
        raise RuntimeBuildError(f"目标产物已存在，不覆盖：{plan.artifact}")
    return plan, node, npm, codesign


def build_runtime(
    value: str | RuntimeTarget,
    *,
    output_root: Path,
    source_root: Path = DEFAULT_SOURCE_ROOT,
    host_system: str | None = None,
    host_machine: str | None = None,
    node_path: Path | None = None,
    npm_path: Path | None = None,
    postject_path: Path | None = None,
    codesign_path: Path | None = None,
) -> RuntimeBuildResult:
    plan, node, npm, codesign = preflight_runtime(
        value,
        source_root=source_root,
        output_root=output_root,
        host_system=host_system,
        host_machine=host_machine,
        node_path=node_path,
        npm_path=npm_path,
        codesign_path=codesign_path,
    )
    node_version = ".".join(str(part) for part in _node_version(node))
    plan.output_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="optionhelper-agent-runtime-", dir=plan.output_root) as staging_name:
        staging = Path(staging_name)
        runtime_source, locked_postject = _prepare_locked_build_workspace(
            plan.source_root,
            staging / "locked-build",
            npm=npm,
        )
        # Kept only for compatibility with callers that still pass the former
        # source-tree path. Native injection always uses the disposable copy.
        del postject_path
        postject = locked_postject.resolve()
        entry = _create_sea_entry(runtime_source, staging / "entry.cjs", node=node)
        blob = staging / "sea-prep.blob"
        config = staging / "sea-config.json"
        config.write_text(
            json.dumps(
                {
                    "main": str(entry),
                    "output": str(blob),
                    "disableExperimentalSEAWarning": True,
                    "useCodeCache": True,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        _run([str(node), "--experimental-sea-config", str(config)])
        candidate = staging / expected_artifact_name(plan.target)
        if plan.target.system == "darwin":
            # Homebrew may provide a universal Node binary. Injecting into the
            # fat file finds one SEA sentinel per architecture. A thin arm64
            # distribution is already ready; lipo -thin rejects thin inputs.
            lipo = _resolve_tool(None, "lipo")
            architectures = _run([str(lipo), "-archs", str(node)]).split()
            if "arm64" not in architectures:
                raise RuntimeBuildError("Node二进制缺少目标arm64架构，已停止打包")
            if len(architectures) == 1:
                shutil.copy2(node, candidate)
            else:
                _run([str(lipo), str(node), "-thin", "arm64", "-output", str(candidate)])
        else:
            shutil.copy2(node, candidate)
        postject_command = [
            str(node), str(postject), str(candidate), "NODE_SEA_BLOB", str(blob),
            "--sentinel-fuse", SEA_SENTINEL,
        ]
        if plan.target.system == "darwin":
            postject_command.extend(("--macho-segment-name", "NODE_SEA"))
        _run(postject_command)
        if codesign is not None:
            _run([str(codesign), "--force", "--sign", "-", str(candidate)])
            _run([str(codesign), "--verify", "--strict", "--verbose=2", str(candidate)])
        candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
        candidate.replace(plan.artifact)
    probe = {**probe_runtime_process(plan.artifact), "node_version": node_version}
    manifest = write_runtime_manifest(
        plan.artifact,
        plan.target,
        host_system=host_system,
        host_machine=host_machine,
        probe=probe,
        source_root=plan.source_root,
    )
    verify_runtime_candidate(
        plan.artifact,
        plan.target,
        manifest_path=manifest,
        host_system=host_system,
        host_machine=host_machine,
        source_root=plan.source_root,
        require_source_provenance=True,
    )
    return RuntimeBuildResult(
        plan=plan,
        artifact=plan.artifact,
        sha256=_sha256(plan.artifact),
        manifest=manifest,
        probe=probe,
    )


__all__ = [
    "RuntimeBuildError",
    "RuntimeBuildPlan",
    "RuntimeBuildResult",
    "RuntimeTarget",
    "ARTIFACT_MANIFEST_SUFFIX",
    "RUNTIME_ARTIFACT_ENV",
    "RUNTIME_MODE_ENV",
    "RUNTIME_PATH_ENV",
    "REQUIRE_NATIVE_RUNTIME_ENV",
    "ALLOW_SOURCE_RUNTIME_FALLBACK_ENV",
    "LOCAL_VERIFIED",
    "DEVELOPMENT_ONLY",
    "RUNTIME_RESOURCE_BASENAME",
    "RUNTIME_RESOURCE_DIR",
    "artifact_manifest_path",
    "TARGETS",
    "build_runtime",
    "expected_artifact_name",
    "make_build_plan",
    "probe_runtime_process",
    "preflight_runtime",
    "prepare_staged_runtime",
    "resolve_runtime_candidate",
    "runtime_resource_name",
    "stage_runtime_candidate",
    "verify_runtime_candidate",
    "verify_staged_runtime",
    "write_runtime_manifest",
]
