from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import os
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
MODULES = ("datafetcher", "recommender", "payoffer", "pricer", "backtester", "reporter", "designer")
PAGE_MODULES = ("datafetcher", "payoffer", "pricer", "backtester", "reporter")
NON_PAGE_MODULES = ("recommender", "designer")


def _missing(paths: list[Path]) -> list[str]:
    return [str(path.relative_to(ROOT)) for path in paths if not path.exists()]


def _python_files(root: Path):
    if root.is_dir():
        yield from sorted(root.rglob("*.py"))


def _imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
    return found


class Architecture121Test(unittest.TestCase):
    maxDiff = None

    def assert_paths_exist(self, label: str, relative_paths: list[str]) -> None:
        missing = _missing([ROOT / relative for relative in relative_paths])
        self.assertFalse(missing, f"{label}缺失：{missing}")

    def test_root_and_knowledger_source_layout(self) -> None:
        self.assert_paths_exist(
            "12.1根文件",
            [
                "SKILL.md", "CONTEXT.md", ".gitignore", "general-manager.md",
                "references/optionlist.md", "references/optionlib.md",
                "references/optionreg.py", "references/knowledger-manager.md",
                "tests", "LICENSES", "data", "result", "versions", "dist", "blueprint",
            ],
        )
        optionregs = [
            path for path in ROOT.rglob("optionreg.py")
            if not any(part in {"history", "dist", "versions", "capability", "result"} for part in path.parts)
        ]
        self.assertEqual(
            optionregs,
            [ROOT / "references" / "optionreg.py"],
            f"OptionReg手工开发源必须唯一，实际为{[str(path.relative_to(ROOT)) for path in optionregs]}",
        )

    def test_core_layout_and_unique_registry_loader(self) -> None:
        self.assert_paths_exist(
            "共享核心",
            [
                "core/tool_entry.py", "core/module_host.py", "core/start-pages.command",
                "core/start-pages.bat", "core/requirements.lock", "core/src/runtime/__init__.py",
                "core/src/runtime/bootstrap.py", "core/src/runtime/contracts",
                "core/src/runtime/protocol/schemas", "core/src/runtime/knowledger/registry_loader.py",
                "core/src/runtime/ports", "core/src/runtime/adapters", "core/tests",
            ],
        )
        loader = ROOT / "core" / "src" / "runtime" / "knowledger" / "registry_loader.py"
        if loader.is_file():
            tree = ast.parse(loader.read_text(encoding="utf-8"), filename=str(loader))
            functions = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            self.assertIn("load_registry", functions, "Registry Loader必须公开load_registry()")

        offenders: list[str] = []
        target_roots = [ROOT / "core" / "src", *(ROOT / "modules" / module / "src" for module in MODULES)]
        for source_root in target_roots:
            for path in _python_files(source_root):
                relative = path.relative_to(ROOT)
                text = path.read_text(encoding="utf-8")
                if path not in {loader, ROOT / "core" / "src" / "runtime" / "bootstrap.py"}:
                    tree = ast.parse(text, filename=str(path))
                    hardcoded_paths = [
                        node.value for node in ast.walk(tree)
                        if isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and ("/optionreg.py" in node.value or "\\optionreg.py" in node.value)
                    ]
                    if hardcoded_paths:
                        offenders.append(f"{relative}:硬编码OptionReg路径{hardcoded_paths}")
                for line, module_name in _imports(path):
                    if module_name == "references.optionreg" or module_name.startswith("references.optionreg."):
                        offenders.append(f"{relative}:{line}:直接Import {module_name}")
        self.assertFalse(offenders, "Registry唯一Loader被绕过：\n" + "\n".join(offenders))

    def test_runtime_and_module_import_boundaries(self) -> None:
        violations: list[str] = []
        core_root = ROOT / "core" / "src" / "runtime"
        for path in _python_files(core_root):
            for line, module_name in _imports(path):
                if module_name == "modules" or module_name.startswith("modules."):
                    violations.append(f"{path.relative_to(ROOT)}:{line}:共享核心反向依赖{module_name}")
                if module_name.split(".", 1)[0] in {"assets", "core", "products"}:
                    violations.append(f"{path.relative_to(ROOT)}:{line}:旧或物理目录Import {module_name}")

        for module in MODULES:
            source_root = ROOT / "modules" / module / "src"
            for path in _python_files(source_root):
                for line, module_name in _imports(path):
                    top_level = module_name.split(".", 1)[0]
                    if top_level in {"assets", "core", "products", "references"}:
                        violations.append(f"{path.relative_to(ROOT)}:{line}:禁止Import {module_name}")
                    if top_level == "modules" and not (
                        module_name == f"modules.{module}" or module_name.startswith(f"modules.{module}.")
                    ):
                        violations.append(f"{path.relative_to(ROOT)}:{line}:直接读取其他模块内部{module_name}")
        self.assertFalse(violations, "内部Import边界不合格：\n" + "\n".join(violations))

    def test_seven_module_skeletons_and_specialized_files(self) -> None:
        missing: list[str] = []
        for module in MODULES:
            base = ROOT / "modules" / module
            paths = [
                base / "module-guide.md",
                base / f"{module}-manager.md",
                base / "src" / "__init__.py",
                base / "src" / "service.py",
                base / "src" / "config.py",
                base / "src" / "models.py",
                base / "tests",
            ]
            missing.extend(_missing(paths))
            self.assertFalse((base / "SKILL.md").exists(), f"{module}不得建立嵌套SKILL.md")
            self.assertFalse((base / "agents").exists(), f"{module}不得建立模型厂商agents目录")

        specialized = {
            "datafetcher": [
                "request_validator.py", "cache_resolver.py", "data_normalizer.py", "quality_validator.py",
                "providers/base.py", "providers/local.py", "providers/ifind_http.py",
                "providers/ifind_sdk.py", "providers/wind.py",
            ],
            "recommender": [
                "intent_router.py", "evidence_retriever.py", "candidate_builder.py",
                "candidate_critic.py", "executor.py", "agent_steps.py",
            ],
            "payoffer": ["path_sampler.py", "svg_renderer.py", "asset_resolver.py"],
            "pricer": [
                "observed_state.py", "market_resolver.py", "model_router.py", "valuation_solver.py",
                "greeks.py", "risk_engine.py", "diagnostics.py", "engines",
            ],
            "backtester": [
                "entry_generator.py", "path_replay.py", "trade_ledger.py",
                "common_metrics.py", "metric_profiles.py",
            ],
            "reporter": [
                "evidence_resolver.py", "report_unit_builder.py", "designer_handoff.py",
                "artifact_validator.py", "export_service.py",
            ],
            "designer": [
                "design_system_builder.py", "design_renderer.py", "echarts_renderer.py", "svg_theme.py",
            ],
        }
        for module, relatives in specialized.items():
            package = ROOT / "modules" / module / "src"
            missing.extend(_missing([package / relative for relative in relatives]))
        missing.extend(_missing([
            ROOT / "modules" / "payoffer" / "figures" / "json",
            ROOT / "modules" / "payoffer" / "figures" / "svg",
            ROOT / "modules" / "payoffer" / "maintenance" / "default_asset_manager.py",
            ROOT / "modules" / "designer" / "assets" / "templates",
            ROOT / "modules" / "designer" / "assets" / "themes",
            ROOT / "modules" / "designer" / "assets" / "vendor",
            ROOT / "modules" / "designer" / "assets" / "fonts",
        ]))
        self.assertFalse(missing, f"七模块目标文件不完整：{missing}")

    def test_exactly_five_modules_own_operation_pages(self) -> None:
        missing: list[str] = []
        for module in PAGE_MODULES:
            page = ROOT / "modules" / module / "page"
            missing.extend(_missing([
                page / f"{module}.html", page / f"{module}.css", page / f"{module}.js"
            ]))
        self.assertFalse(missing, f"五个操作页面不完整：{missing}")
        for module in NON_PAGE_MODULES:
            self.assertFalse(
                (ROOT / "modules" / module / "page").exists(),
                f"{module}不应拥有独立操作页面",
            )

        app_frontend = ROOT / "products" / "app" / "frontend"
        duplicates = []
        if app_frontend.is_dir():
            for module in PAGE_MODULES:
                duplicates.extend(app_frontend.rglob(f"{module}.html"))
                if (app_frontend / module).exists():
                    duplicates.append(app_frontend / module)
        self.assertFalse(
            duplicates,
            "App frontend不得复制模块页面：" + str([str(path.relative_to(ROOT)) for path in duplicates]),
        )

    def test_built_skill_resolves_all_five_operation_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = ROOT / "versions" / "v1.0" / "option-helper.zip"
            self.assertTrue(archive.is_file(), "必须先签发v1.0 Skill ZIP再验收发行态页面")
            with zipfile.ZipFile(archive) as package:
                package.extractall(temporary)
            host = Path(temporary) / "option-helper" / "scripts" / "module_host.py"
            environment = os.environ | {
                "OPTIONHELPER_DATA_ROOT": str(Path(temporary) / "data"),
                "OPTIONHELPER_RESULT_ROOT": str(Path(temporary) / "result"),
            }
            completed = subprocess.run(
            [sys.executable, str(host), "--list"],
            cwd=temporary,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            )
        catalog = json.loads(completed.stdout)
        self.assertEqual(set(catalog), set(PAGE_MODULES))
        self.assertTrue(all(item.get("exists") for item in catalog.values()), catalog)

    def test_app_source_and_cross_platform_layout(self) -> None:
        self.assert_paths_exist(
            "OptionHelper App开发源",
            [
                "products/app/app-manager.md", "products/app/pyproject.toml",
                "products/app/locks/macos-arm64.lock", "products/app/locks/windows-x86_64.lock",
                "products/app/config/app-defaults.yaml", "products/app/config/logging.yaml",
                "products/app/backend/app_server.py", "products/app/backend/tool_gateway.py",
                "products/app/backend/page_registry.py",
                "products/app/backend/settings/settings_service.py",
                "products/app/backend/settings/connection_tester.py",
                "products/app/backend/settings/settings_models.py",
                "products/app/backend/secrets/secret_provider.py", "products/app/backend/secrets/secret_ref.py",
                "products/app/backend/identity/identity_provider.py", "products/app/backend/identity/session_identity.py",
                "products/app/backend/authorization/policy.py", "products/app/backend/authorization/roles.py",
                "products/app/backend/agent_runtime/conversation_service.py",
                "products/app/backend/agent_runtime/tool_dispatcher.py",
                "products/app/backend/task_runtime/task_service.py", "products/app/backend/task_runtime/job_runner.py",
                "products/app/backend/model_gateway/gateway.py",
                "products/app/backend/model_gateway/provider_registry.py",
                "products/app/backend/stores/data_store.py", "products/app/backend/stores/result_store.py",
                "products/app/backend/stores/session_store.py", "products/app/backend/stores/settings_store.py",
                "products/app/backend/audit/audit_service.py", "products/app/backend/audit/audit_models.py",
                "products/app/frontend/shared", "products/app/frontend/login", "products/app/frontend/optchat",
                "products/app/frontend/optdesk", "products/app/frontend/settings",
                "products/app/desktop/common/webview_app.py", "products/app/desktop/common/paths.py",
                "products/app/desktop/macos/app_window.py", "products/app/desktop/macos/keychain.py",
                "products/app/desktop/windows/app_window.py",
                "products/app/desktop/windows/credential_manager.py",
                "products/app/tests", "products/app/packaging/macos", "products/app/packaging/windows",
            ],
        )

    def test_packaging_and_icon_sources(self) -> None:
        self.assert_paths_exist(
            "Skill与App构建源",
            [
                "assets/icons", "packaging/build_all.py", "packaging/verify_release.py",
                "packaging/skill/package-source-map.json", "packaging/skill/build_skill.py",
                "packaging/skill/verify_skill.py", "packaging/app/build_app.py",
                "packaging/app/verify_app.py", "packaging/app/run_development_app.py", "packaging/app/macos/build_macos.py",
                "packaging/app/windows/build_windows.py",
            ],
        )

        source_map_path = ROOT / "packaging" / "skill" / "package-source-map.json"
        if source_map_path.is_file():
            source_map = json.loads(source_map_path.read_text(encoding="utf-8"))
            self.assertEqual(source_map.get("modules"), list(MODULES), "Skill Source Map七模块顺序或集合不正确")
            self.assertEqual(source_map.get("page_modules"), list(PAGE_MODULES), "Skill Source Map必须只登记五个页面模块")
            serialized = json.dumps(source_map, ensure_ascii=False)
            for generated in ("data/", "result/", "dist/", "evals/", "products/app/capability"):
                self.assertNotIn(generated, serialized, f"Source Map不得把生成目录{generated}作为Skill源码")

    def test_development_evals_exist_and_are_excluded_from_distribution(self) -> None:
        self.assert_paths_exist(
            "开发评测体系",
            [
                "evals/README.md", "evals/case.schema.json", "evals/runner.py",
                "evals/cases/knowledger.json", "evals/cases/tools.json",
                "evals/cases/delivery.json", "evals/cases/permissions.json",
                "evals/cases/workflows.json", "evals/fixtures/market_history.csv",
                "evals/support.py", "evals/tests/test_evals.py",
            ],
        )
        source_map = json.loads((ROOT / "packaging" / "skill" / "package-source-map.json").read_text(encoding="utf-8"))
        self.assertNotIn("evals/", json.dumps(source_map, ensure_ascii=False))
        archive = ROOT / "versions" / "v1.0" / "option-helper.zip"
        self.assertTrue(archive.is_file(), "必须存在当前标准Skill ZIP")
        with zipfile.ZipFile(archive) as package:
            members = package.namelist()
        self.assertFalse(any("/evals/" in f"/{name}" for name in members), "标准Skill ZIP不得包含evals/")

    def test_gitignore_covers_generated_and_secret_state(self) -> None:
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        required_markers = (
            "data/", "result/", "dist/", "products/app/capability/",
            "*.secret-ref", "*.credentials.json", "products/app/build/",
        )
        missing = [marker for marker in required_markers if marker not in text]
        self.assertFalse(missing, f".gitignore未覆盖12.1生成物或Secret状态：{missing}")

    def test_no_active_legacy_business_code_under_assets(self) -> None:
        forbidden = [
            "assets/contract_engine.py", "assets/data", "assets/payoffer", "assets/pricer",
            "assets/backtester", "assets/reporter", "assets/designer", "assets/vendor",
        ]
        remaining = [relative for relative in forbidden if (ROOT / relative).exists()]
        self.assertFalse(
            remaining,
            "旧assets业务实现尚未满足退役门禁：" + str(remaining)
            + "。必须先通过金融基线、目标模块测试、Import切换、页面同源和历史快照验证。",
        )
        frozen_web = ROOT / "assets" / "web-design"
        if frozen_web.exists():
            self.assertTrue(
                (frozen_web / "FROZEN.md").is_file(),
                "assets/web-design保留时必须有FROZEN.md，明确其不是正式源码且不得被运行入口加载。",
            )

    def test_pricer_target_keeps_executable_golden(self) -> None:
        compact = ROOT / "modules" / "pricer" / "tests"
        embedded = (
            ROOT / "modules" / "pricer" / "src"
            / "engines" / "pricing_core"
        )
        golden_candidates = [
            compact / "fixtures" / "option_pricing_golden.json",
            embedded / "tests" / "golden_standard_results.json",
        ]
        random_candidates = [compact / "fixtures" / "rand_normal.npy", embedded / "data" / "rand_normal.npy"]
        test_candidates = [compact / "test_option_pricing_golden.py", embedded / "tests" / "test_golden_results.py"]
        self.assertTrue(any(path.is_file() for path in golden_candidates), "Pricer目标目录缺少迁移前Golden数值")
        self.assertTrue(any(path.is_file() for path in random_candidates), "Pricer目标目录缺少固定随机源")
        self.assertTrue(any(path.is_file() for path in test_candidates), "Pricer目标目录缺少可执行Golden测试")

    def test_root_skill_is_generic_and_modules_have_no_vendor_manifests(self) -> None:
        skill = ROOT / "SKILL.md"
        text = skill.read_text(encoding="utf-8") if skill.is_file() else ""
        self.assertRegex(text, r"(?m)^name:\s*option-helper\s*$", "根SKILL.md名称必须是option-helper")
        for formal_name in ("DataFetcher", "Recommender", "Payoffer", "Pricer", "Backtester", "Reporter", "Designer"):
            self.assertIn(formal_name, text, f"根SKILL.md未声明内部能力{formal_name}")
        vendor_manifests = [
            path for path in ROOT.rglob("openai.yaml")
            if not any(part in {"history", "dist", "versions", "capability", "result"} for part in path.parts)
        ]
        self.assertFalse(
            vendor_manifests,
            "通用Skill源码不得包含厂商专用Manifest："
            + str([str(path.relative_to(ROOT)) for path in vendor_manifests]),
        )

    def test_no_high_confidence_plaintext_secrets(self) -> None:
        excluded_parts = {
            ".git", ".idea", "history", "材料", "一页通图片", "data", "result", "dist", "versions",
            "__pycache__", "baselines",
        }
        text_extensions = {
            ".py", ".md", ".html", ".css", ".js", ".mjs", ".json", ".yaml", ".yml",
            ".toml", ".ini", ".cfg", ".txt", ".command", ".bat", ".plist", ".swift",
        }
        patterns = [
            re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|client[_-]?secret)\s*[:=]\s*['\"][^'\"\r\n]{8,}['\"]"),
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{32,}"),
            re.compile(r"[0-9a-f]{40,}\.signs_[A-Za-z0-9_-]+"),
        ]
        findings: list[str] = []
        # Prune generated and Cloud-backed trees before traversal.  Filtering
        # after Path.rglob() still asks macOS FileProvider to enumerate those
        # directories and can block indefinitely on a dataless conflict item.
        for directory, dirnames, filenames in os.walk(ROOT, topdown=True):
            dirnames[:] = [name for name in dirnames if name not in excluded_parts]
            parent = Path(directory)
            for filename in filenames:
                path = parent / filename
                if path.suffix.lower() not in text_extensions:
                    continue
                relative = path.relative_to(ROOT)
                text = path.read_text(encoding="utf-8", errors="ignore")
                for pattern in patterns:
                    if match := pattern.search(text):
                        line = text.count("\n", 0, match.start()) + 1
                        findings.append(f"{relative}:{line}")
        self.assertFalse(findings, "发现高置信明文Secret：" + str(findings))


if __name__ == "__main__":
    unittest.main()
