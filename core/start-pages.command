#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
if [[ -n "${OPTIONHELPER_PYTHON:-}" ]]; then
  if [[ "$OPTIONHELPER_PYTHON" != /* || ! -x "$OPTIONHELPER_PYTHON" ]]; then
    echo "OPTIONHELPER_PYTHON必须是可执行的绝对Python路径。" >&2
    exit 1
  fi
  PYTHON_CMD=("$OPTIONHELPER_PYTHON")
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD=("$(command -v python3)")
elif command -v python >/dev/null 2>&1; then
  PYTHON_CMD=("$(command -v python)")
else
  echo "未找到可用Python。请设置OPTIONHELPER_PYTHON，或安装python3。" >&2
  exit 1
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
"${PYTHON_CMD[@]}" "$CHECKER" --requirements "$SCRIPT_DIR/requirements.lock" --check-dependencies

if [[ "${1:-}" == "--check-environment" ]]; then
  exec "${PYTHON_CMD[@]}" "$SCRIPT_DIR/module_host.py" --list
fi
MODULE_NAME="${1:-payoffer}"
exec "${PYTHON_CMD[@]}" "$SCRIPT_DIR/module_host.py" --module "$MODULE_NAME"
