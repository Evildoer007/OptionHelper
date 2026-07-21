#!/bin/zsh

set -u

editor_dir="$(cd "$(dirname "$0")" && pwd -P)"
project_dir="$(cd "$editor_dir/../.." && pwd -P)"
editor_port=4179

fail() {
  local message="$1"
  print -u2 -- "Payoff Editor启动失败：${message}"
  osascript -e "display alert \"无法启动Payoff Editor\" message \"${message}\"" >/dev/null 2>&1 || true
  exit 1
}

for required_path in \
  "$editor_dir/app/server.mjs" \
  "$project_dir/references/optionlist.md" \
  "$project_dir/references/optionlib.md" \
  "$project_dir/assets/payoff"; do
  [[ -e "$required_path" ]] || fail "项目目录不完整。请保留完整OptionHelper项目及其原有目录结构。"
done

node_bin="$(command -v node 2>/dev/null || true)"
[[ -n "$node_bin" ]] || fail "未找到Node.js。请安装Node.js 18或更高版本后重试。"

node_version="$($node_bin --version 2>/dev/null || true)"
node_major="${node_version#v}"
node_major="${node_major%%.*}"
[[ "$node_major" =~ '^[0-9]+$' ]] || fail "无法识别Node.js版本。请安装Node.js 18或更高版本后重试。"
(( node_major >= 18 )) || fail "当前Node.js版本为${node_version}。请升级至18或更高版本后重试。"

if lsof -nP -iTCP:"$editor_port" -sTCP:LISTEN >/dev/null 2>&1; then
  fail "${editor_port}端口已被占用。请先关闭已有Payoff Editor或释放该端口。"
fi

cd "$project_dir" || fail "无法进入OptionHelper项目目录。"
"$node_bin" "$editor_dir/app/server.mjs" &
server_pid=$!

cleanup() {
  kill "$server_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for attempt in {1..50}; do
  if curl --silent --fail "http://127.0.0.1:${editor_port}/api/products" >/dev/null 2>&1; then
    open "http://127.0.0.1:${editor_port}"
    print -- "Payoff Editor已启动。关闭此终端窗口将停止服务。"
    wait "$server_pid"
    exit $?
  fi
  if ! kill -0 "$server_pid" 2>/dev/null; then
    wait "$server_pid"
    fail "服务未能启动。请查看终端中的错误信息。"
  fi
  sleep 0.1
done

fail "服务启动超时。请检查${editor_port}端口和项目文件。"
