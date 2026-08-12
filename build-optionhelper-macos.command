#!/bin/zsh
# One-click macOS v1.0 local-candidate build.  It never overwrites the
# existing formal archive under versions/v1.0.
set -euo pipefail

ROOT="${0:A:h}"
VERSION="${OPTIONHELPER_VERSION:-v1.0}"

setopt NULL_GLOB

if [[ -z "${OPTIONHELPER_PYTHON:-}" ]]; then
  print -u2 "必须先由用户显式选择Python。构建器不得自动选择候选环境。"
  print -u2 "可选解释器仅供选择；枚举过程不会运行候选解释器。设置OPTIONHELPER_PYTHON为其中一个绝对路径后重新运行："
  candidates=()
  [[ -n "${CONDA_PREFIX:-}" ]] && candidates+=("$CONDA_PREFIX/bin/python")
  candidates+=(
    "${commands[python3]:-}"
    "${commands[python]:-}"
    /opt/anaconda3/bin/python
    /opt/anaconda3/envs/*/bin/python
    "$HOME"/anaconda3/bin/python
    "$HOME"/anaconda3/envs/*/bin/python
    "$HOME"/miniconda3/bin/python
    "$HOME"/miniconda3/envs/*/bin/python
    "$HOME"/mambaforge/bin/python
    "$HOME"/mambaforge/envs/*/bin/python
  )
  typeset -A seen
  for candidate in "${candidates[@]}"; do
    [[ "$candidate" = /* && -x "$candidate" && -z "${seen[$candidate]:-}" ]] || continue
    seen[$candidate]=1
    environment_root="${candidate:h:h}"
    environment_name="独立解释器"
    python_version="选择后确认"
    if [[ -d "$environment_root/conda-meta" ]]; then
      if [[ "$environment_root" == */envs/* ]]; then
        environment_name="${environment_root:t}"
      else
        environment_name="base (${environment_root:t})"
      fi
      version_files=("$environment_root"/conda-meta/python-*.json(N))
      if (( ${#version_files} )); then
        metadata_version=$(/usr/bin/sed -n 's/^[[:space:]]*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$version_files[1]" | /usr/bin/head -n 1)
        [[ -n "$metadata_version" ]] && python_version="$metadata_version"
      fi
    elif [[ -f "$environment_root/pyvenv.cfg" ]]; then
      environment_name="${environment_root:t}"
      metadata_version=$(/usr/bin/sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*//p' "$environment_root/pyvenv.cfg" | /usr/bin/head -n 1)
      [[ -n "$metadata_version" ]] && python_version="$metadata_version"
    fi
    print -u2 "  环境名称=$environment_name；Python版本=$python_version；解释器绝对路径=$candidate"
  done
  exit 1
fi
if [[ "$OPTIONHELPER_PYTHON" != /* || ! -x "$OPTIONHELPER_PYTHON" ]]; then
  print -u2 "OPTIONHELPER_PYTHON必须是可执行的绝对Python路径。"
  exit 1
fi
PYTHON_BIN="$OPTIONHELPER_PYTHON"

cd "$ROOT"
print "OptionHelper macOS候选构建"
print "[1/3] 正在检查运行依赖…"
"$PYTHON_BIN" packaging/skill/environment_check.py --requirements core/requirements.lock --check-dependencies
print "[2/3] 正在检查打包工具…"
"$PYTHON_BIN" packaging/skill/environment_check.py --requirements packaging/build-requirements.lock --check-dependencies
print "[3/3] 正在构建Skill、App和DMG。此过程可能需要数分钟，下面会持续显示阶段进度。"
if "$PYTHON_BIN" packaging/build_current.py --version "$VERSION" --platform macos; then
  print "构建成功：最新Skill、App和DMG已发布到dist。"
  exit 0
else
  build_exit_code=$?
  print -u2 "构建失败：未发布新的交付物。请根据上方失败阶段处理后重试。"
  exit "$build_exit_code"
fi
