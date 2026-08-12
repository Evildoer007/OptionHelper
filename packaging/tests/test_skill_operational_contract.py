"""Operational contract for the portable Skill and its packaging entrypoints."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
SKILL_PACKAGING = ROOT / "packaging" / "skill"
if str(SKILL_PACKAGING) not in sys.path:
    sys.path.insert(0, str(SKILL_PACKAGING))

from environment_check import check_data_api, check_external_stores


GUIDES = tuple(sorted((ROOT / "modules").glob("*/module-guide.md")))


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_external_store_preflight_defaults_to_project_roots_but_rejects_skill_installation(tmp_path: Path) -> None:
    project = tmp_path / "research-project"
    skill = project / ".claude" / "skills" / "option-helper"
    skill.mkdir(parents=True)
    outside = tmp_path / "optionhelper-store"

    defaults = check_external_stores(skill, None, None, project_root=project)
    assert defaults["ok"]
    assert defaults["stores"]["data_root"]["path"] == str((project / "data").resolve())
    assert defaults["stores"]["result_root"]["path"] == str((project / "result").resolve())
    assert defaults["stores"]["runtime_root"]["path"] == str((project / ".optionhelper" / "runtime").resolve())

    project_scoped = check_external_stores(
        skill, str(project / "data"), str(outside / "result"), project_root=project
    )
    assert project_scoped["ok"]
    assert project_scoped["stores"]["data_root"]["status"] == "external"

    accepted = check_external_stores(
        skill,
        str(outside / "data"),
        str(outside / "result"),
        runtime_root=str(outside / "runtime"),
        project_root=project,
    )
    assert accepted["ok"]
    assert {item["status"] for item in accepted["stores"].values()} == {"external"}

    runtime_inside_install = check_external_stores(
        skill,
        str(outside / "data"),
        str(outside / "result"),
        runtime_root=str(skill / ".optionhelper" / "runtime"),
        project_root=project,
    )
    assert not runtime_inside_install["ok"]
    assert runtime_inside_install["stores"]["runtime_root"]["status"] == "inside_installation"

    linked_inside_skill = tmp_path / "linked-store"
    linked_inside_skill.symlink_to(skill / "state", target_is_directory=True)
    resolved_inside_skill = check_external_stores(
        skill,
        str(linked_inside_skill),
        str(outside / "result"),
        runtime_root=str(outside / "runtime"),
        project_root=project,
    )
    assert not resolved_inside_skill["ok"]
    assert resolved_inside_skill["stores"]["data_root"]["status"] == "inside_installation"


def test_data_api_preflight_is_status_only_and_never_calls_the_provider(monkeypatch) -> None:
    monkeypatch.delenv("IFIND_REFRESH_TOKEN", raising=False)
    missing = check_data_api()
    assert missing == {
        "ok": False,
        "data_provider": {"name": "iFind API", "credential": "IFIND_REFRESH_TOKEN", "status": "missing"},
        "note": "预检只检查凭据是否由Host配置，不发起网络或数据请求。",
    }

    monkeypatch.setenv("IFIND_REFRESH_TOKEN", "configured-by-host")
    configured = check_data_api()
    assert configured["ok"]
    assert configured["data_provider"]["status"] == "configured"
    assert "configured-by-host" not in str(configured)


def test_operational_docs_have_one_three_workflow_external_store_and_reporter_boundary() -> None:
    skill = _read("SKILL.md")
    readme = _read("packaging/skill/SKILL_README.md")

    for text in (skill, readme):
        assert "安装目录之外的项目Store" in text
        assert "--check-data-api" in text
        assert "不发起网络或数据请求" in text
        assert "不得自行选择" in text
        assert '"$OPTIONHELPER_PYTHON"' in text
    assert skill.count("### 1.咨询与结构解释") == 1
    assert skill.count("### 2.指定模块与参数重算") == 1
    assert skill.count("### 3.结构推荐与正式交付") == 1
    assert "不得以“快速版本”或“先做示例”为由创建临时Python、HTML、SVG或PDF" in skill
    assert "Reporter→Designer" in readme
    assert all("安装目录之外的项目Store" in guide.read_text(encoding="utf-8") for guide in GUIDES)


def test_build_entrypoints_require_the_user_selected_python_and_show_progress() -> None:
    macos = _read("build-optionhelper-macos.command")
    windows = _read("build-optionhelper-windows.bat")

    for script in (macos, windows):
        assert "OPTIONHELPER_PYTHON" in script
        assert "不得自动选择" in script
        assert "[1/3]" in script
        assert "[3/3]" in script
        assert "build_current.py" in script
    assert 'PYTHON_BIN="$candidate"' not in macos
    assert 'set "PYTHON_BIN=%%P"' not in windows


def test_repository_docs_and_release_verifier_distinguish_replaceable_dist_from_immutable_versions() -> None:
    readme = _read("README.md")
    verifier = _read("packaging/verify_release.py")

    assert "可替换的当前候选交付目录" in readme
    assert "不可覆盖的正式签发归档" in readme
    assert 'default=ROOT / "dist" / "candidates"' not in verifier
    assert 'required=True' in verifier


def test_windows_build_rejects_installed_but_mismatched_report_runtime_dependencies(monkeypatch, tmp_path: Path) -> None:
    import importlib.util
    import types

    module_path = ROOT / "packaging" / "app" / "windows" / "build_windows.py"
    spec = importlib.util.spec_from_file_location("test_windows_prerequisites", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    capability = tmp_path / "capability"
    capability.mkdir()
    (capability / "capability-manifest.json").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module.sys, "version_info", (3, 12))
    monkeypatch.setattr(module.shutil, "which", lambda name: f"C:/{name}.exe")
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: type("Completed", (), {"returncode": 0, "stdout": "8.0.100"})())

    monkeypatch.setattr(module.metadata, "version", lambda name: {"PyInstaller": module.PYINSTALLER_VERSION, "reportlab": "0.0.0"}[name])
    try:
        module.check_prerequisites(capability)
    except module.WindowsBuildError as error:
        assert "ReportLab必须固定" in str(error)
    else:
        raise AssertionError("ReportLab版本漂移必须拒绝Windows构建")

    monkeypatch.setattr(
        module.metadata,
        "version",
        lambda name: {"PyInstaller": module.PYINSTALLER_VERSION, "reportlab": module.PDF_RUNTIME_VERSION}[name],
    )
    monkeypatch.setitem(sys.modules, "PIL", types.SimpleNamespace(__version__="0.0.0"))
    try:
        module.check_prerequisites(capability)
    except module.WindowsBuildError as error:
        assert "Pillow必须固定" in str(error)
    else:
        raise AssertionError("Pillow版本漂移必须拒绝Windows构建")
