from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_macos_first_use_lists_metadata_without_running_a_candidate() -> None:
    script = (ROOT / "build-optionhelper-macos.command").read_text(encoding="utf-8")

    assert "环境名称=$environment_name" in script
    assert "Python版本=$python_version" in script
    assert "解释器绝对路径=$candidate" in script
    assert "枚举过程不会运行候选解释器" in script
    selection_block = script.split('if [[ -z "${OPTIONHELPER_PYTHON:-}" ]]', 1)[1].split("exit 1", 1)[0]
    assert '"$candidate" --version' not in selection_block


def test_windows_first_use_uses_read_only_metadata_listing() -> None:
    script = (ROOT / "build-optionhelper-windows.bat").read_text(encoding="utf-8")
    helper = (ROOT / "packaging" / "list_python_environments.ps1").read_text(encoding="utf-8")

    assert "list_python_environments.ps1" in script
    assert "枚举过程不会运行候选解释器" in script
    assert "环境名称=$environmentName" in helper
    assert "Python版本=$pythonVersion" in helper
    assert "解释器绝对路径=$absolutePath" in helper
    assert "conda-meta" in helper
    assert "pyvenv.cfg" in helper
    assert "& $absolutePath" not in helper
    assert "Start-Process" not in helper
