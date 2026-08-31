#!/bin/zsh
# One-click macOS local-candidate build. It never writes the formal archive.
set -euo pipefail

ROOT="${0:A:h}"
VERSION="${OPTIONHELPER_VERSION:-v1.0.0}"

setopt NULL_GLOB

LOCAL_PYTHON_FILE="$ROOT/.optionhelper/runtime/build-python-path"
python_selection_source=""
if [[ -n "${OPTIONHELPER_PYTHON:-}" ]]; then
  python_selection_source="environment"
elif [[ -f "$LOCAL_PYTHON_FILE" && ! -L "$LOCAL_PYTHON_FILE" ]]; then
  IFS= read -r OPTIONHELPER_PYTHON < "$LOCAL_PYTHON_FILE" || OPTIONHELPER_PYTHON=""
  if [[ "$OPTIONHELPER_PYTHON" = /* && -x "$OPTIONHELPER_PYTHON" ]]; then
    python_selection_source="local"
    print "已使用本机保存的Python解释器：$OPTIONHELPER_PYTHON"
  else
    print -u2 "本机保存的Python解释器已失效，将重新列出候选环境。"
    OPTIONHELPER_PYTHON=""
  fi
fi

if [[ -z "${OPTIONHELPER_PYTHON:-}" ]]; then
  print -u2 "请选择本次构建使用的Python解释器。"
  print -u2 "可选解释器仅供选择；枚举过程不会运行候选解释器。"
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
  selected_candidates=()
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
    selected_candidates+=("$candidate")
    print -u2 "  [${#selected_candidates}] 环境名称=$environment_name；Python版本=$python_version；解释器绝对路径=$candidate"
  done
  if (( ${#selected_candidates} == 0 )); then
    print -u2 "未找到可选择的Python解释器。请安装Python3.11或更高版本后重试。"
    exit 1
  fi

  while true; do
    if ! IFS= read -r "selection?请输入序号后按回车（直接回车取消）： "; then
      print -u2 "未选择Python解释器，构建已取消。"
      exit 1
    fi
    if [[ -z "$selection" ]]; then
      print -u2 "未选择Python解释器，构建已取消。"
      exit 1
    fi
    if [[ "$selection" == <-> ]] && (( selection >= 1 && selection <= ${#selected_candidates} )); then
      OPTIONHELPER_PYTHON="${selected_candidates[$selection]}"
      python_selection_source="interactive"
      print "已选择Python解释器：$OPTIONHELPER_PYTHON"
      break
    fi
    print -u2 "请输入1到${#selected_candidates}之间的序号。"
  done
fi
if [[ "$OPTIONHELPER_PYTHON" != /* || ! -x "$OPTIONHELPER_PYTHON" ]]; then
  print -u2 "OPTIONHELPER_PYTHON必须是可执行的绝对Python路径。"
  exit 1
fi
PYTHON_BIN="$OPTIONHELPER_PYTHON"
if [[ "$python_selection_source" == "environment" || "$python_selection_source" == "interactive" ]]; then
  local_python_dir="${LOCAL_PYTHON_FILE:h}"
  local_python_temp="${LOCAL_PYTHON_FILE}.tmp.$$"
  /bin/mkdir -p "$local_python_dir"
  /bin/chmod 700 "$local_python_dir"
  print -r -- "$PYTHON_BIN" > "$local_python_temp"
  /bin/chmod 600 "$local_python_temp"
  /bin/mv -f "$local_python_temp" "$LOCAL_PYTHON_FILE"
  print "已将本次选择保存为本机设置：$LOCAL_PYTHON_FILE"
fi

cd "$ROOT"

check_dependencies() {
  local output
  local check_exit_code

  print "[1/3] 正在检查运行环境和锁定依赖…"
  if output=$("$PYTHON_BIN" packaging/skill/environment_check.py \
      --requirements "$ROOT/core/requirements.lock" \
      --requirements "$ROOT/packaging/build-requirements.lock" \
      --project-root "$ROOT" --check-dependencies 2>&1); then
    if print -r -- "$output" | /usr/bin/grep -q '"status": "hit"'; then
      print "[1/3] 运行条件已就绪，复用上次检查。"
    else
      print "[1/3] 运行条件检查通过。"
    fi
    return 0
  fi
  check_exit_code=$?
  print -u2 "运行环境和锁定依赖检查未通过，构建已停止。"
  print -u2 "$output"
  print -u2 "处理方式：请使用同一Python环境安装或恢复锁定依赖："
  print -u2 "  \"$PYTHON_BIN\" -m pip install -r \"$ROOT/core/requirements.lock\" --index-url \"https://pypi.tuna.tsinghua.edu.cn/simple\""
  print -u2 "  \"$PYTHON_BIN\" -m pip install -r \"$ROOT/packaging/build-requirements.lock\" --index-url \"https://pypi.tuna.tsinghua.edu.cn/simple\""
  print -u2 "修复后重新运行本命令。"
  exit "$check_exit_code"
}

prepare_agent_runtime_tools() {
  if ! command -v npm >/dev/null 2>&1; then
    print -u2 "缺少Node.js/npm，无法构建原生Agent运行时。"
    exit 1
  fi
  print "正在恢复两套锁定的Agent运行时构建依赖…"
  "$PYTHON_BIN" "$ROOT/packaging/app/agent_runtime/restore_dependencies.py"
}

print "OptionHelper macOS候选构建"
check_dependencies
prepare_agent_runtime_tools
print "[2/3] 运行条件已就绪，准备构建Skill、App和DMG。"
print "[3/3] 正在构建Skill、App和DMG。此过程可能需要数分钟，下面会持续显示阶段进度。"
export OPTIONHELPER_REQUIRE_NATIVE_RUNTIME=1
if "$PYTHON_BIN" packaging/build_current.py --version "$VERSION" --platform macos; then
  exit 0
else
  build_exit_code=$?
  print -u2 "构建失败：未发布新的交付物。请根据上方失败阶段处理后重试。"
  exit "$build_exit_code"
fi
