#!/bin/zsh

set -u

ROOT="$(cd "${0:A:h}/.." && pwd)"
RUNTIME_DIR="$ROOT/.runtime"

stop_pid_file() {
  local file="$1"
  [[ -f "$file" ]] || return 0
  local pid
  pid="$(<"$file")"
  if [[ "$pid" == <-> ]] && kill -0 "$pid" >/dev/null 2>&1; then
    kill "$pid" >/dev/null 2>&1 || true
    for _ in {1..20}; do
      kill -0 "$pid" >/dev/null 2>&1 || break
      /bin/sleep 0.1
    done
    if kill -0 "$pid" >/dev/null 2>&1; then
      kill -KILL "$pid" >/dev/null 2>&1 || true
    fi
  fi
  rm -f "$file"
}

stop_pid_file "$RUNTIME_DIR/frontend.pid"
stop_pid_file "$RUNTIME_DIR/backend.pid"
/usr/bin/osascript -e 'display notification "Literary Studio services stopped." with title "Literary Studio"' >/dev/null 2>&1 || true
