#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${OPTIONHELPER_PROJECT_ROOT:-}"
if [[ -z "$PROJECT_ROOT" || ! -d "$PROJECT_ROOT" ]]; then
  echo "请设置OPTIONHELPER_PROJECT_ROOT为Skill安装目录外的项目运行目录。" >&2
  exit 1
fi
PROJECT_ROOT="${PROJECT_ROOT:A}"
if [[ "$PROJECT_ROOT" == "$SCRIPT_DIR" || "$PROJECT_ROOT" == "$SCRIPT_DIR"/* ]]; then
  echo "OPTIONHELPER_PROJECT_ROOT不得位于Skill安装目录内。" >&2
  exit 1
fi

# A released Skill is read-only.  Keep all mutable state in the project
# selected by the user, then pass those exact roots to the module Host after
# the readiness check.  The check and the actual run must not resolve
# different Store locations.
RUNTIME_ROOT="$PROJECT_ROOT/.optionhelper/runtime"
DATA_ROOT="$PROJECT_ROOT/data"
RESULT_ROOT="$PROJECT_ROOT/result"
mkdir -p "$RUNTIME_ROOT" "$DATA_ROOT" "$RESULT_ROOT"
export OPTIONHELPER_RUNTIME_ROOT="$RUNTIME_ROOT"
export OPTIONHELPER_DATA_ROOT="$DATA_ROOT"
export OPTIONHELPER_RESULT_ROOT="$RESULT_ROOT"

STATE_FILE="$RUNTIME_ROOT/python-path"
typeset -a candidates
candidates=()

add_candidate() {
  local candidate="$1"
  [[ "$candidate" == /* && -x "$candidate" && ! -d "$candidate" ]] || return 0
  local existing
  for existing in "${candidates[@]}"; do
    [[ "$existing" == "$candidate" ]] && return 0
  done
  candidates+=("$candidate")
}

if [[ -n "${OPTIONHELPER_PYTHON:-}" ]]; then
  add_candidate "$OPTIONHELPER_PYTHON"
  if (( ${#candidates[@]} != 1 )); then
    echo "OPTIONHELPER_PYTHON必须是可执行的绝对Python路径。" >&2
    exit 1
  fi
  PYTHON_BIN="$OPTIONHELPER_PYTHON"
else
  [[ -f "$STATE_FILE" ]] && add_candidate "$(<"$STATE_FILE")"
  [[ -n "${CONDA_PREFIX:-}" ]] && add_candidate "$CONDA_PREFIX/bin/python"
  if [[ -n "${CONDA_EXE:-}" && -x "$CONDA_EXE" ]]; then
    CONDA_BASE="${CONDA_EXE:A:h:h}"
    for candidate in "$CONDA_BASE"/envs/*/bin/python(N); do
      add_candidate "$candidate"
    done
  fi
  for command_name in python3 python; do
    command_path="$(command -v "$command_name" 2>/dev/null || true)"
    [[ -n "$command_path" ]] && add_candidate "${command_path:A}"
  done
  if (( ${#candidates[@]} == 0 )); then
    echo "未发现可用Python。请先激活所需conda环境，或设置OPTIONHELPER_PYTHON。" >&2
    exit 1
  fi
  saved_python=""
  [[ -f "$STATE_FILE" ]] && saved_python="$(<"$STATE_FILE")"
  if [[ "$saved_python" == /* && -x "$saved_python" && ! -d "$saved_python" ]]; then
    PYTHON_BIN="$saved_python"
  else
    echo "请选择本次项目工作流使用的Python解释器："
    integer index=1
    for candidate in "${candidates[@]}"; do
      echo "  $index) $candidate"
      (( index++ ))
    done
    print -n "输入编号："
    read -r selection
    if [[ ! "$selection" =~ '^[0-9]+$' || "$selection" -lt 1 || "$selection" -gt ${#candidates[@]} ]]; then
      echo "Python选择无效。" >&2
      exit 1
    fi
    PYTHON_BIN="$candidates[$selection]"
    PERSIST_SELECTION=1
  fi
fi

if [[ -f "$SCRIPT_DIR/environment_check.py" ]]; then
  CHECKER="$SCRIPT_DIR/environment_check.py"
else
  CHECKER="$SCRIPT_DIR/../packaging/skill/environment_check.py"
fi
if [[ ! -f "$CHECKER" ]]; then
  echo "缺少环境检查器：$CHECKER" >&2
  exit 1
fi
if [[ -n "${PERSIST_SELECTION:-}" ]]; then
  mkdir -p "${STATE_FILE:h}"
  chmod 700 "${STATE_FILE:h}"
  print -r -- "$PYTHON_BIN" > "$STATE_FILE"
  chmod 600 "$STATE_FILE"
fi
"$PYTHON_BIN" "$CHECKER" --requirements "$SCRIPT_DIR/requirements.lock" --check-readiness \
  --skill-root "$SCRIPT_DIR" --project-root "$PROJECT_ROOT" \
  --data-root "$DATA_ROOT" --result-root "$RESULT_ROOT" \
  --runtime-root "$RUNTIME_ROOT"

if [[ "${1:-}" == "--check-environment" ]]; then
  exec "$PYTHON_BIN" "$SCRIPT_DIR/module_host.py" --list
fi
MODULE_NAME="${1:-payoffer}"
exec "$PYTHON_BIN" "$SCRIPT_DIR/module_host.py" --module "$MODULE_NAME" --project-root "$PROJECT_ROOT"
