from __future__ import annotations

import copy
from hashlib import sha256
from importlib import metadata
import json
from pathlib import Path
import re
from runpy import run_path
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile


SKILL_PACKAGING = Path(__file__).resolve().parents[1]
if str(SKILL_PACKAGING) not in sys.path:
    sys.path.insert(0, str(SKILL_PACKAGING))

from build_skill import SOURCE_MAP, SkillBuildError, _copy_file, _excluded, _validate_source_map, build_skill, load_source_map, verify_source_snapshot, write_zip
from environment_check import check_dependencies, check_external_stores
from verify_skill import HASH_SPEC_VERSION, MODULES, PAGE_MODULES, SkillVerificationError, _code_errors, _module_hashes, content_tree_entries, probe_runtime, tree_hash, verify_skill, verify_zip


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CORE_SRC = PROJECT_ROOT / "core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))
from runtime.knowledger.versioning import SNAPSHOT_FILES, build_candidate


REAL_OPTIONLIST = (PROJECT_ROOT / "references" / "optionlist.md").read_text(encoding="utf-8")
REAL_PRODUCT_IDS = re.findall(r"^\|\s*\d+\s*\|.*?\|\s*(\d+\.\d+)\s*\|", REAL_OPTIONLIST, re.MULTILINE)
_LOCAL_ENVIRONMENT_MARKER = "Machine" + "Learning"


def _formal_catalog_payload() -> dict[str, object]:
    return {
        "manifest_type": "CatalogVersion", "publication_status": "published",
        "formal_release": True, "executable": True, "catalog_version": "v1.0",
        "published_by": "packaging-test", "published_at": "2026-08-07T12:00:00+08:00",
        "products": {product_id: "v1.0" for product_id in REAL_PRODUCT_IDS},
        "ordered_product_version_map": {product_id: "v1.0" for product_id in REAL_PRODUCT_IDS},
        "product_manifest_refs": {product_id: f"knowledger/products/{product_id}/product-version.json" for product_id in REAL_PRODUCT_IDS},
        "product_manifest_sha256": {product_id: "0" * 64 for product_id in REAL_PRODUCT_IDS},
    }


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _install_published_history(repo: Path, scratch: Path) -> None:
    technical = scratch / "technical-v1.0"
    build_candidate(
        PROJECT_ROOT, technical, version="v1.0", built_at="2026-08-07T11:00:00+08:00",
    )
    candidate = json.loads((technical / "snapshot.json").read_text(encoding="utf-8"))
    drift = candidate["integrity_evidence"]["default_asset_registry_drift"]
    if drift not in ({}, {"7.3": ["terms"]}):
        raise AssertionError("测试只接受无资产漂移或已知的7.3技术候选资产漂移")
    manifest_refs: dict[str, str] = {}
    manifest_hashes: dict[str, str] = {}
    for product_id in REAL_PRODUCT_IDS:
        source = technical / "products" / product_id
        target = repo / "versions" / "v1.0" / "knowledger" / "products" / product_id
        target.mkdir(parents=True)
        product_snapshot = json.loads((source / "product-snapshot.json").read_text(encoding="utf-8"))
        for filename in SNAPSHOT_FILES.values():
            shutil.copy2(source / filename, target / filename)
        if product_id == "7.3" and product_id in drift:
            product = run_path(str(target / SNAPSHOT_FILES["optionreg"]))["PRODUCT"]
            payoff_path = target / SNAPSHOT_FILES["default_json"]
            payoff = json.loads(payoff_path.read_text(encoding="utf-8"))
            payoff.update({"name_zh": product["identity"]["name_zh"], "terms": product["terms"], "paths": product["paths"]})
            _write(payoff_path, json.dumps(payoff, ensure_ascii=False))
            product_snapshot["snapshot_sha256"]["default_json_sha256"] = _sha256(payoff_path)
        manifest = {
            "manifest_type": "ProductVersion", "publication_status": "published",
            "formal_release": True, "executable": True, "product_id": product_id,
            "product_version": "v1.0", "published_by": "packaging-test",
            "published_at": "2026-08-07T12:00:00+08:00",
            "snapshot_files": SNAPSHOT_FILES,
            "snapshot_sha256": product_snapshot["snapshot_sha256"],
        }
        manifest_path = target / "product-version.json"
        _write(manifest_path, json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        manifest_refs[product_id] = f"knowledger/products/{product_id}/product-version.json"
        manifest_hashes[product_id] = _sha256(manifest_path)
    catalog = _formal_catalog_payload()
    catalog["product_manifest_refs"] = manifest_refs
    catalog["product_manifest_sha256"] = manifest_hashes
    catalog["source_file_sha256"] = {
        name: _sha256(repo / "references" / name)
        for name in ("optionlist.md", "optionlib.md", "optionreg.py")
    }
    _write(
        repo / "versions" / "v1.0" / "knowledger" / "catalog-version.json",
        json.dumps(catalog, ensure_ascii=False),
    )


def _write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _valid_skill(root: Path) -> Path:
    skill = root / "option-helper"
    skill.mkdir()
    _write(
        skill / "SKILL.md",
        "---\nname: option-helper\ndescription: test skill\n---\n"
        "运行`scripts/environment_check.py`，并设置OPTIONHELPER_RUNTIME_ROOT、OPTIONHELPER_DATA_ROOT和OPTIONHELPER_RESULT_ROOT。\n",
    )
    _write(skill / "README.md", "# OptionHelper Skill\n\n项目级安装说明。\n")
    for relative in ("references/context.md", "references/optionlist.md", "references/optionlib.md", "references/knowledger-manager.md"):
        _write(skill / relative)
    _write(skill / "references" / "optionlist.md", REAL_OPTIONLIST)
    for module in MODULES:
        _write(skill / "references" / "module-guides" / f"{module}.md")
        for name in ("__init__.py", "service.py", "config.py", "models.py"):
            _write(skill / "scripts" / "modules" / module / name)
    for module in PAGE_MODULES:
        for suffix in ("html", "css", "js"):
            _write(skill / "assets" / "pages" / module / f"{module}.{suffix}")
    _write(
        skill / "assets/pages/module-host-bridge.js",
        "// ModuleHostContext X-OptionHelper-Request-Id dataAssetDownloadId optionhelper.module-download-error\n",
    )
    _write(skill / "assets/pages/datafetcher/datafetcher.js", "fetch('/api/assets/' + id + '/download')\n")
    _write(skill / "assets/designer/vendor/echarts.min.js", "window.echarts = {};\n")
    _write(skill / "scripts/runtime/protocol/models.py", "class CallerContext: pass\n")
    _write(skill / "scripts/runtime/protocol/module_host.py", "class ModuleHostContext: pass\n")
    _write(skill / "scripts/runtime/adapters/local_store.py", "class LocalResultStore: pass\n")
    _write(
        skill / "scripts/runtime/protocol/schemas/caller-context.schema.json",
        '{"title":"CallerContext","properties":{"request_id":{"type":"string"}}}\n',
    )
    _write(
        skill / "scripts/runtime/protocol/schemas/module-host-context.schema.json",
        '{"title":"ModuleHostContext","properties":{"capability_token":{"pattern":"^v2\\\\."}}}\n',
    )
    _write(
        skill / "scripts/runtime/protocol/schemas/run-ref.schema.json",
        '{"title":"RunRef","oneOf":[{"required":["module","tenant_id","task_id","run_id","expected_semantic_result_hash","expected_artifact_manifest_hash"],"properties":{"module":{"type":"string"}}}]}\n',
    )
    _write(skill / "scripts/runtime/ports/__init__.py", "class ResultSelectionPort: pass\n")
    _write(skill / "scripts/runtime/adapters/local_host.py", "class LocalHostAuthority: pass\n")
    _write(skill / "scripts/modules/pricer/engines/pricing_core/optionhelper_core.py", "pass\n")
    _write(
        skill / "scripts/modules/datafetcher/market_conventions.py",
        'def china_market_convention(adjustment: str = "auto"):\n    return "forward_adjusted_for_adj_fields"\n',
    )
    _write(skill / "scripts/modules/reporter/service.py", "from runtime.ports import ResultSelectionPort\n")
    _write(skill / "scripts/modules/reporter/artifact_validator.py", "portable_assets = []\n")
    _write(skill / "scripts/modules/reporter/export_service.py", "portable_assets = []\n")
    _write(skill / "scripts/modules/reporter/evidence_resolver.py", "expected_artifact_manifest_hash = True\n")
    _write(skill / "scripts/modules/reporter/models.py", "expected_artifact_manifest_hash = True\n")
    _write(skill / "scripts/modules/reporter/report_unit_builder.py", "expected_artifact_manifest_hash = True\n")
    for relative in (
        "scripts/module_host.py", "scripts/environment_check.py",
        "scripts/start-pages.command", "scripts/start-pages.bat", "scripts/requirements.lock",
        "scripts/knowledger/__init__.py", "scripts/knowledger/optionreg.py",
        "scripts/runtime/contracts/contract_engine.py", "scripts/runtime/protocol/tool_catalog.py",
        "assets/icons/icon.svg", "assets/designer/templates/.keep", "assets/designer/themes/.keep",
        "assets/designer/vendor/.keep", "assets/designer/fonts/.keep", "LICENSES/NOTICE",
    ):
        _write(skill / relative)
    _write(
        skill / "scripts/tool_entry.py",
        'def run_project_request():\n    layout = value or "continuous"\n\ndef public_project_result():\n    pass\n\n# --project-request\n',
    )
    _write(skill / "scripts/runtime/protocol/tool_catalog.py", 'TOOL = {"protocol_version": "v1.2"}\n')
    _write(skill / "scripts" / "requirements.lock", "example==1\n")
    _write(skill / "scripts/start-pages.command", "#!/bin/zsh\nOPTIONHELPER_PYTHON=${OPTIONHELPER_PYTHON:-}\npython3 environment_check.py --check-dependencies\npython3 \"$SCRIPT_DIR/module_host.py\"\n")
    _write(skill / "scripts/start-pages.bat", "@echo off\nif defined OPTIONHELPER_PYTHON set PYTHON_BIN=%OPTIONHELPER_PYTHON%\nwhere python3\npython3 environment_check.py --check-dependencies\npython3 \"%SCRIPT_DIR%module_host.py\"\n")
    _write(
        skill / "scripts" / "runtime" / "bootstrap.py",
        "import os\nRUNTIME = os.environ.get('OPTIONHELPER_RUNTIME_ROOT')\nDATA = os.environ.get('OPTIONHELPER_DATA_ROOT')\nRESULT = os.environ.get('OPTIONHELPER_RESULT_ROOT')\n",
    )
    formal = root / "formal"
    for name in ("optionlist.md", "optionlib.md", "optionreg.py"):
        destination = formal / "references" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / "references" / name, destination)
    _install_published_history(formal, root)
    catalog_source = formal / "versions" / "v1.0" / "knowledger" / "catalog-version.json"
    shutil.copy2(catalog_source, skill / "scripts" / "knowledger" / "catalog-version.json")
    products_source = formal / "versions" / "v1.0" / "knowledger" / "products"
    shutil.copytree(products_source, skill / "scripts" / "knowledger" / "products")
    for product_id in REAL_PRODUCT_IDS:
        product_dir = products_source / product_id
        payload = json.loads((product_dir / SNAPSHOT_FILES["default_json"]).read_text(encoding="utf-8"))
        name = payload["name_zh"]
        json_target = skill / "assets" / "payoffer" / "figures" / "json" / f"{name}.json"
        svg_target = skill / "assets" / "payoffer" / "figures" / "svg" / f"{name}.svg"
        json_target.parent.mkdir(parents=True, exist_ok=True)
        svg_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(product_dir / SNAPSHOT_FILES["default_json"], json_target)
        shutil.copy2(product_dir / SNAPSHOT_FILES["default_svg"], svg_target)
    entries = content_tree_entries(skill)
    hashes = {item["path"]: item["sha256"] for item in entries}
    contract_entries = [item for item in entries if str(item["path"]).startswith("scripts/runtime/contracts/")]
    manifest = {
        "manifest_schema_version": "1.0",
        "package_status": "candidate",
        "capability_version": "candidate",
        "release_status": "candidate_from_published_catalog",
        "formal_release": False,
        "execution_scope": "candidate_verification",
        "catalog_version": "v1.0",
        "catalog_source": "versions/v1.0/knowledger/catalog-version.json",
        "protocol_version": "v1.2",
        "design_system_version": "candidate",
        "hash_spec_version": HASH_SPEC_VERSION,
        "modules": list(MODULES),
        "page_modules": list(PAGE_MODULES),
        "contract_core_hash": tree_hash(contract_entries),
        "tool_catalog_hash": hashes["scripts/runtime/protocol/tool_catalog.py"],
        "content_tree_hash": tree_hash(entries),
        "content_hashes": hashes,
        "content_tree_entries": entries,
        "module_content_hashes": _module_hashes(entries),
        "source_map_hash": sha256(SOURCE_MAP.read_bytes()).hexdigest(),
        "source_tree_hash": tree_hash(entries),
        "source_content_hashes": hashes,
    }
    _write(skill / "capability-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return skill


def _valid_source_repo(root: Path) -> tuple[Path, Path]:
    repo = root / "source"
    source_map = repo / "source-map.json"
    guides = "\n".join(f"[{module}](modules/{module}/module-guide.md)" for module in MODULES)
    _write(
        repo / "SKILL.md",
        "---\nname: option-helper\ndescription: test source skill\n---\n"
        "[context](CONTEXT.md)\n"
        f"{guides}\n"
        "运行`scripts/environment_check.py`并设置OPTIONHELPER_RUNTIME_ROOT、OPTIONHELPER_DATA_ROOT和OPTIONHELPER_RESULT_ROOT。\n",
    )
    _write(repo / "packaging" / "skill" / "SKILL_README.md", "# OptionHelper Skill\n\n项目级安装说明。\n")
    _write(source_map, SOURCE_MAP.read_text(encoding="utf-8"))
    for relative in (
        "CONTEXT.md", "references/optionlist.md", "references/optionlib.md", "references/knowledger-manager.md",
        "references/optionreg.py", "core/tool_entry.py", "core/module_host.py", "core/start-pages.command",
        "core/start-pages.bat", "core/src/runtime/browser/module_host_bridge.js", "core/src/runtime/knowledger/__init__.py", "core/src/runtime/contracts/contract_engine.py",
        "modules/reporter/assets/disclaimer.md", "LICENSES/NOTICE", "assets/icons/icon.svg",
        "packaging/skill/environment_check.py",
    ):
        _write(repo / relative, "pass\n")
    _write(
        repo / "core/module_host.py",
        "import json\nprint(json.dumps({name: {'page': name, 'exists': True} for name in " + repr(MODULES[:1] + PAGE_MODULES) + "}))\n",
    )
    _write(repo / "core/start-pages.command", "#!/bin/zsh\nOPTIONHELPER_PYTHON=${OPTIONHELPER_PYTHON:-}\npython3 environment_check.py --check-dependencies\npython3 \"$SCRIPT_DIR/module_host.py\"\n")
    _write(repo / "core/start-pages.bat", "@echo off\nif defined OPTIONHELPER_PYTHON set PYTHON_BIN=%OPTIONHELPER_PYTHON%\nwhere python3\npython3 environment_check.py --check-dependencies\npython3 \"%SCRIPT_DIR%module_host.py\"\n")
    shutil.copy2(SKILL_PACKAGING / "environment_check.py", repo / "packaging/skill/environment_check.py")
    for name in ("optionlist.md", "optionlib.md", "optionreg.py"):
        shutil.copy2(PROJECT_ROOT / "references" / name, repo / "references" / name)
    _write(repo / "core/requirements.lock", "example==1\n")
    _write(
        repo / "core/src/runtime/bootstrap.py",
        "import os\nRUNTIME = os.environ.get('OPTIONHELPER_RUNTIME_ROOT')\nDATA = os.environ.get('OPTIONHELPER_DATA_ROOT')\nRESULT = os.environ.get('OPTIONHELPER_RESULT_ROOT')\n",
    )
    _write(repo / "core/src/runtime/protocol/tool_catalog.py", 'TOOL = {"protocol_version": "v1.2"}\n')
    _write(repo / "core/src/runtime/protocol/models.py", "class CallerContext: pass\n")
    _write(repo / "core/src/runtime/protocol/module_host.py", "class ModuleHostContext: pass\n")
    _write(repo / "core/src/runtime/adapters/local_host.py", "class LocalHostAuthority: pass\n")
    _write(repo / "core/src/runtime/adapters/local_store.py", "class LocalResultStore: pass\n")
    _write(
        repo / "core/tool_entry.py",
        'def run_project_request():\n    layout = value or "continuous"\n\ndef public_project_result():\n    pass\n\n# --project-request\n',
    )
    _write(
        repo / "core/src/runtime/protocol/schemas/caller-context.schema.json",
        '{"title":"CallerContext","properties":{"request_id":{"type":"string"}}}\n',
    )
    _write(
        repo / "core/src/runtime/protocol/schemas/module-host-context.schema.json",
        '{"title":"ModuleHostContext","properties":{"capability_token":{"pattern":"^v2\\\\."}}}\n',
    )
    _write(
        repo / "core/src/runtime/protocol/schemas/run-ref.schema.json",
        '{"title":"RunRef","oneOf":[{"required":["module","tenant_id","task_id","run_id","expected_semantic_result_hash","expected_artifact_manifest_hash"],"properties":{"module":{"type":"string"}}}]}\n',
    )
    _write(
        repo / "core/src/runtime/browser/module_host_bridge.js",
        "// ModuleHostContext X-OptionHelper-Request-Id dataAssetDownloadId optionhelper.module-download-error\n",
    )
    _write(repo / "core/src/runtime/ports/__init__.py", "class ResultSelectionPort: pass\n")
    for module in MODULES:
        _write(repo / "modules" / module / "module-guide.md", "# Guide\n")
        for name in ("__init__.py", "service.py", "config.py", "models.py"):
            _write(repo / "modules" / module / "src" / name, "pass\n")
    for module in PAGE_MODULES:
        for suffix in ("html", "css", "js"):
            _write(repo / "modules" / module / "page" / f"{module}.{suffix}")
    _write(repo / "modules/pricer/src/engines/pricing_core/optionhelper_core.py", "pass\n")
    _write(
        repo / "modules/datafetcher/src/market_conventions.py",
        'def china_market_convention(adjustment: str = "auto"):\n    return "forward_adjusted_for_adj_fields"\n',
    )
    _write(repo / "modules/datafetcher/page/datafetcher.js", "fetch('/api/assets/' + id + '/download')\n")
    _write(repo / "modules/reporter/src/service.py", "from runtime.ports import ResultSelectionPort\n")
    _write(repo / "modules/reporter/src/artifact_validator.py", "portable_assets = []\n")
    _write(repo / "modules/reporter/src/export_service.py", "portable_assets = []\n")
    _write(repo / "modules/reporter/src/evidence_resolver.py", "expected_artifact_manifest_hash = True\n")
    _write(repo / "modules/reporter/src/models.py", "expected_artifact_manifest_hash = True\n")
    _write(repo / "modules/reporter/src/report_unit_builder.py", "expected_artifact_manifest_hash = True\n")
    _write(repo / "modules/designer/assets/vendor/echarts.min.js", "window.echarts = {};\n")
    for kind in ("json", "svg"):
        source = PROJECT_ROOT / "modules" / "payoffer" / "figures" / kind
        target = repo / "modules" / "payoffer" / "figures" / kind
        target.mkdir(parents=True, exist_ok=True)
        for asset in source.glob(f"*.{kind}"):
            shutil.copy2(asset, target / asset.name)
    for name in ("templates", "themes", "vendor", "fonts"):
        _write(repo / "modules/designer/assets" / name / ".keep")
    _install_published_history(repo, root)
    return repo, source_map


class SkillPackagingTest(unittest.TestCase):
    def test_source_map_is_explicit_and_rejects_path_traversal(self) -> None:
        source_map = load_source_map()
        self.assertEqual(tuple(source_map["modules"]), MODULES)
        self.assertEqual(tuple(source_map["page_modules"]), PAGE_MODULES)
        bad = copy.deepcopy(source_map)
        bad["files"][0]["target"] = "../escape.md"
        with self.assertRaises(SkillBuildError):
            _validate_source_map(bad)

    def test_source_map_rejects_development_evals(self) -> None:
        source_map = copy.deepcopy(load_source_map())
        source_map["files"].append({
            "source": "evals/runner.py",
            "target": "references/evals-runner.py",
        })
        with self.assertRaisesRegex(SkillBuildError, "不得读取"):
            _validate_source_map(source_map)

    def test_source_map_contains_no_development_evals(self) -> None:
        source_map = load_source_map()
        sources = [entry["source"] for kind in ("files", "trees") for entry in source_map[kind]]
        self.assertFalse(
            any("evals" in Path(source).parts for source in sources),
            "正式Skill Source Map不得读取开发Eval目录",
        )

    def test_development_directories_are_always_excluded_and_rejected(self) -> None:
        development_parts = ("dev", "development", "docs", "eval", "evals", "example", "examples", "test", "tests")
        for part in development_parts:
            with self.subTest(part=part):
                relative = Path("nested") / part / "probe.py"
                self.assertTrue(_excluded(relative, []))
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    _write(root / relative)
                    self.assertIn(f"候选包包含开发内容：{relative.as_posix()}", _code_errors(root))

        for filename in ("development_score.py", "devtools.js", "testament.md", "examples_data.py"):
            with self.subTest(filename=filename):
                relative = Path("scripts") / filename
                self.assertFalse(_excluded(relative, []))
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    _write(root / relative)
                    self.assertFalse(any("开发内容" in error for error in _code_errors(root)))

    def test_source_map_rejects_nested_development_content(self) -> None:
        source_map = copy.deepcopy(load_source_map())
        source_map["files"].append({
            "source": "modules/pricer/evals/probe.py",
            "target": "references/probe.py",
        })
        with self.assertRaisesRegex(SkillBuildError, "不得包含开发内容"):
            _validate_source_map(source_map)

    def test_current_candidate_skill_zip_contains_no_development_artifacts(self) -> None:
        """Validate the artifact the current build actually emits.

        The one-click build deliberately produces a fresh candidate from the
        working source and does not depend on a checked-in ``versions`` ZIP.
        Keeping this assertion tied to a historical archive made a clean
        checkout fail before the build could start.
        """

        with tempfile.TemporaryDirectory(prefix="optionhelper-current-skill-") as temporary:
            with patch("build_skill.probe_runtime", return_value=[]):
                skill = build_skill(
                    Path(temporary) / "option-helper",
                    candidate=True,
                    repo_root=PROJECT_ROOT,
                )
            archive = write_zip(skill)
            with zipfile.ZipFile(archive) as package:
                members = package.namelist()
        banned_parts = {"evals", "tests", "test", "dev", "development", ".pytest_cache", "__pycache__"}
        polluted = [
            member for member in members
            if banned_parts.intersection(Path(member).parts) or member.endswith((".pyc", ".pyo"))
        ]
        self.assertEqual(polluted, [], f"当前Skill ZIP包含开发文件：{polluted}")

    def test_ephemeral_capability_contains_no_development_evals(self) -> None:
        with tempfile.TemporaryDirectory(prefix="optionhelper-skill-test-") as temporary:
            capability = _valid_skill(Path(temporary))
            banned_parts = {"evals", "tests", "test", "dev", "development", ".pytest_cache", "__pycache__"}
            polluted = [
                str(path.relative_to(capability))
                for path in capability.rglob("*")
                if (
                    banned_parts.intersection(path.relative_to(capability).parts)
                    or path.name.endswith((".pyc", ".pyo"))
                )
            ]
        self.assertEqual(polluted, [], f"临时Capability包含开发文件：{polluted}")

    def test_app_capability_contains_no_development_artifacts(self) -> None:
        capability = PROJECT_ROOT / "products" / "app" / "capability" / "option-helper"
        self.assertTrue(capability.is_dir(), f"缺少App Capability：{capability}")
        banned_parts = {"evals", "tests", "test", "dev", "development", ".pytest_cache", "__pycache__"}
        polluted = [
            str(path.relative_to(capability))
            for path in capability.rglob("*")
            if (
                banned_parts.intersection(path.relative_to(capability).parts)
                or path.name.endswith((".pyc", ".pyo"))
            )
        ]
        self.assertEqual(polluted, [], f"App Capability包含开发文件：{polluted}")

    def test_content_tree_hash_includes_mode_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "option-helper"
            root.mkdir()
            file = root / "run.command"
            _write(file, "echo ok\n")
            first = tree_hash(content_tree_entries(root))
            file.chmod(file.stat().st_mode | stat.S_IXUSR)
            second = tree_hash(content_tree_entries(root))
            _write(file, "echo changed\n")
            third = tree_hash(content_tree_entries(root))
        self.assertNotEqual(first, second)
        self.assertNotEqual(second, third)

    def test_content_tree_rejects_symbolic_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "option-helper"
            root.mkdir()
            _write(root / "target.txt")
            (root / "link.txt").symlink_to(root / "target.txt")
            with self.assertRaises(SkillVerificationError):
                content_tree_entries(root)

    def test_environment_check_reports_missing_package_and_installation_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "option-helper"
            root.mkdir()
            lock = root / "requirements.lock"
            _write(lock, "missing-package==1.0\n")

            def missing(_: str) -> str:
                raise metadata.PackageNotFoundError("missing-package")

            dependencies = check_dependencies(lock, version_lookup=missing)
            stores = check_external_stores(root, str(root / "data"), str(Path(temporary) / "result"))
        self.assertFalse(dependencies["ok"])
        self.assertEqual(dependencies["requirements"][0]["status"], "missing")
        self.assertFalse(stores["ok"])
        self.assertEqual(stores["stores"]["data_root"]["status"], "inside_installation")

    def test_verifier_rejects_local_environment_name_in_all_text_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill = _valid_skill(Path(temporary))
            _write(skill / "references" / "local-environment.md", _LOCAL_ENVIRONMENT_MARKER)
            _write(skill / "scripts" / "local_environment.py", _LOCAL_ENVIRONMENT_MARKER)
            errors = verify_skill(skill)
        self.assertTrue(any("本机Python环境名" in error for error in errors), errors)

    def test_root_skill_links_are_transformed_for_extracted_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "SKILL.md"
            target = Path(temporary) / "package" / "SKILL.md"
            _write(source, "[guide](modules/payoffer/module-guide.md)\n[context](CONTEXT.md)\n")
            _copy_file(source, target, transform="skill_links")
            rendered = target.read_text(encoding="utf-8")
        self.assertIn("references/module-guides/payoffer.md", rendered)
        self.assertIn("references/context.md", rendered)

    def test_candidate_zip_is_deterministic_and_revalidates_after_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill = _valid_skill(Path(temporary))
            self.assertEqual(verify_skill(skill), [])
            first = write_zip(skill).read_bytes()
            second = write_zip(skill).read_bytes()
            errors = verify_zip(skill.parent / "option-helper.zip")
        self.assertEqual(first, second)
        self.assertEqual(errors, [])

    def test_verifier_rejects_pre_v12_capability_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill = _valid_skill(Path(temporary))
            manifest_path = skill / "capability-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["protocol_version"] = "v1.1"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertIn("Capability协议版本必须为v1.2", verify_skill(skill))

    def test_build_requires_existing_catalog_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            isolated_repo = Path(temporary) / "repo"
            isolated_repo.mkdir()
            with self.assertRaises(SkillBuildError):
                build_skill(
                    Path(temporary) / "candidate",
                    catalog_version="v1.0",
                    repo_root=isolated_repo,
                )

    def test_build_rejects_technical_catalog_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, source_map = _valid_source_repo(Path(temporary))
            catalog = repo / "versions/v1.0/knowledger/catalog-version.json"
            catalog.write_text(json.dumps({
                "manifest_type": "CatalogVersionCandidate",
                "candidate_status": "technical_candidate_not_executable",
                "formal_release": False,
                "executable": False,
                "catalog_version": "v1.0",
                "products": {f"p{index}": "v1.0" for index in range(65)},
            }), encoding="utf-8")
            with patch("build_skill.SOURCE_MAP", source_map):
                with self.assertRaisesRegex(SkillBuildError, "技术候选"):
                    build_skill(Path(temporary) / "candidate", catalog_version="v1.0", repo_root=repo)

    def test_builds_development_candidate_without_catalog_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, source_map = _valid_source_repo(Path(temporary))
            shutil.rmtree(repo / "versions")
            with patch("build_skill.SOURCE_MAP", source_map):
                with patch("build_skill.probe_runtime", return_value=[]):
                    skill = build_skill(Path(temporary) / "candidate", candidate=True, repo_root=repo)
                self.assertEqual(verify_skill(skill), [])
                self.assertEqual(verify_source_snapshot(skill, repo_root=repo), [])
                manifest = json.loads((skill / "capability-manifest.json").read_text(encoding="utf-8"))
                catalog = json.loads((skill / "scripts/knowledger/catalog-version.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["catalog_version"], "unreleased")
            self.assertEqual(manifest["release_status"], "technical_candidate")
            self.assertFalse(manifest["formal_release"])
            self.assertEqual(manifest["execution_scope"], "development_only")
            self.assertTrue(catalog["executable"])
            self.assertEqual(catalog["execution_scope"], "development_only")
            with self.assertRaises(SkillBuildError):
                build_skill(Path(temporary) / "formal", catalog_version="v1.0", repo_root=repo)

    def test_build_rejects_missing_or_tampered_published_payoff_snapshot(self) -> None:
        for action in ("missing", "tampered"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temporary:
                repo, source_map = _valid_source_repo(Path(temporary))
                asset = repo / "versions" / "v1.0" / "knowledger" / "products" / "2.1" / SNAPSHOT_FILES["default_json"]
                if action == "missing":
                    asset.unlink()
                else:
                    asset.write_text("{}\n", encoding="utf-8")
                with patch("build_skill.SOURCE_MAP", source_map):
                    with self.assertRaisesRegex(SkillBuildError, "正式版本链校验失败"):
                        build_skill(Path(temporary) / "candidate", catalog_version="v1.0", repo_root=repo)

    def test_builds_complete_candidate_from_whitelist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, source_map = _valid_source_repo(Path(temporary))
            for relative in ("data/cache.json", ".pytest_cache/state", "credentials.json"):
                _write(repo / "modules/payoffer/src" / relative)
            with patch("build_skill.SOURCE_MAP", source_map):
                with patch("build_skill.probe_runtime", return_value=[]):
                    skill = build_skill(Path(temporary) / "candidate", catalog_version="v1.0", repo_root=repo)
                self.assertEqual(verify_skill(skill), [])
                self.assertEqual(verify_source_snapshot(skill, repo_root=repo), [])
                self.assertEqual(verify_zip(write_zip(skill)), [])
                for relative in ("scripts/modules/payoffer/data/cache.json", "scripts/modules/payoffer/.pytest_cache/state", "scripts/modules/payoffer/credentials.json"):
                    self.assertFalse((skill / relative).exists())
                _write(skill / "assets" / "unmapped.txt")
                self.assertIn("候选文件集合已与当前白名单映射漂移", verify_source_snapshot(skill, repo_root=repo))

    def test_runtime_probe_requires_all_page_resources_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, source_map = _valid_source_repo(Path(temporary))
            with patch("build_skill.SOURCE_MAP", source_map):
                with patch("build_skill.probe_runtime", return_value=[]):
                    skill = build_skill(Path(temporary) / "candidate", catalog_version="v1.0", repo_root=repo)
            page = skill / "assets/pages/pricer/pricer.html"
            page.unlink()
            self.assertTrue(any(
                "页面资源HTTP不可用" in error
                for error in probe_runtime(
                    skill,
                    page_probe=lambda root: ["页面资源HTTP不可用：assets/pages/pricer/pricer.html"]
                    if not (root / "assets/pages/pricer/pricer.html").is_file() else [],
                )
            ))

    def test_runtime_probe_defaults_to_real_http_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill = _valid_skill(Path(temporary))
            def completed(command: list[str], **_: object) -> SimpleNamespace:
                stdout = json.dumps({module: {"exists": True} for module in PAGE_MODULES}) if "module_host.py" in command[1] else "inside_installation"
                return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

            with patch("verify_skill.subprocess.run", side_effect=completed), patch(
                "verify_skill._page_http_errors", return_value=["真实HTTP门禁已调用"]
            ):
                self.assertIn("真实HTTP门禁已调用", probe_runtime(skill))

    def test_source_snapshot_detects_source_map_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill = _valid_skill(Path(temporary))
            entries = content_tree_entries(skill)
            with patch("build_skill._resolve_catalog", return_value=(Path(temporary) / "catalog-version.json", {})), patch(
                "build_skill._source_records", return_value=entries
            ), patch("build_skill._source_output_paths", return_value={str(item["path"]) for item in entries}):
                self.assertEqual(verify_source_snapshot(skill), [])
                manifest_path = skill / "capability-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["source_map_hash"] = "0" * 64
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                self.assertIn("候选Source Map已与当前白名单漂移", verify_source_snapshot(skill))


if __name__ == "__main__":
    unittest.main()
