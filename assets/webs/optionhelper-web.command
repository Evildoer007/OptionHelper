#!/bin/zsh
set -eu
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${OPTIONHELPER_DESK_PORT:-4180}"
URL="http://127.0.0.1:${PORT}/"
HEALTH_URL="${URL}api/health"
SERVICE_VERSION="2026.07.30.6"

if ! command -v node >/dev/null 2>&1; then
  osascript -e 'display alert "OptionHelper无法启动" message "未找到Node.js运行环境。请使用App版本，或安装受支持的Node.js运行环境。"'
  exit 1
fi

is_current_service() {
  curl --silent --fail --max-time 1 "$HEALTH_URL" 2>/dev/null | grep -Fq "\"version\":\"${SERVICE_VERSION}\""
}

stop_outdated_optionhelper_service() {
  local listener_pid listener_command
  local listener_pids=("${(@f)$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)}")
  for listener_pid in "${listener_pids[@]}"; do
    [[ -z "$listener_pid" ]] && continue
    listener_command="$(ps -p "$listener_pid" -o command= 2>/dev/null || true)"
    # It may have been started with either an absolute path or the project's
    # relative assets/web-design path. Port 4180 is reserved for this launcher.
    if [[ "$listener_command" == *"$SCRIPT_DIR/desk-server.mjs"* || "$listener_command" == *"assets/web-design/desk-server.mjs"* || "$listener_command" == *"desk-server.mjs"* ]]; then
      kill "$listener_pid" 2>/dev/null || true
    else
      osascript -e 'display alert "OptionHelper无法启动" message "端口4180正被其他程序占用。请先关闭该程序，或设置OPTIONHELPER_DESK_PORT后重试。"'
      exit 1
    fi
  done
}

if ! is_current_service; then
  stop_outdated_optionhelper_service
  node "$SCRIPT_DIR/desk-server.mjs" >/tmp/optionhelper-web.log 2>&1 &
  SERVER_PID=$!
  trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT INT TERM
  for _ in {1..20}; do
    if curl --silent --fail --max-time 1 "$URL" >/dev/null 2>&1; then break; fi
    sleep 0.25
  done
fi

open "$URL"
wait
