#!/bin/zsh

# Launch Literary Studio without requiring an IDE or an interactive terminal.
# The checked-in source tree, backend/.env, backend/venv, and frontend/
# node_modules remain the runtime authority; this script only starts services.

set -u

ROOT="$(cd "${0:A:h}/.." && pwd)"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
RUNTIME_DIR="$ROOT/.runtime"
BACKEND_PID_FILE="$RUNTIME_DIR/backend.pid"
FRONTEND_PID_FILE="$RUNTIME_DIR/frontend.pid"
BACKEND_LOG="$RUNTIME_DIR/backend.log"
FRONTEND_LOG="$RUNTIME_DIR/frontend.log"

mkdir -p "$RUNTIME_DIR"

notify() {
  /usr/bin/osascript -e "display notification \"$1\" with title \"Literary Studio\"" >/dev/null 2>&1 || true
}

pid_is_alive() {
  [[ -n "$1" ]] && kill -0 "$1" >/dev/null 2>&1
}

existing_pid() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  local pid
  pid="$(<"$file")"
  pid_is_alive "$pid" || return 1
  print -r -- "$pid"
}

if backend_pid="$(existing_pid "$BACKEND_PID_FILE")"; then
  if frontend_pid="$(existing_pid "$FRONTEND_PID_FILE")"; then
    /usr/bin/open "http://127.0.0.1:3000/"
    notify "Already running — opening the Literary Studio desk."
    exit 0
  fi
fi

PYTHON="$BACKEND_DIR/venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3 || true)"
NEXT="$FRONTEND_DIR/node_modules/.bin/next"

if [[ ! -x "$PYTHON" ]]; then
  notify "Python runtime missing. Check backend/venv."
  exit 1
fi
if [[ ! -x "$NEXT" ]]; then
  notify "Frontend dependencies missing. Run npm install in frontend."
  exit 1
fi

port_owner() {
  local owner
  owner="$(/usr/sbin/lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -1)"
  if [[ -n "$owner" ]]; then
    print -r -- "$owner"
    return 0
  fi
  return 1
}

if owner="$(port_owner 8000)"; then
  notify "Port 8000 is already in use; Literary Studio was not started."
  exit 1
fi
if owner="$(port_owner 3000)"; then
  notify "Port 3000 is already in use; Literary Studio was not started."
  exit 1
fi

: >| "$BACKEND_LOG"
: >| "$FRONTEND_LOG"

if "$PYTHON" -c 'import uvicorn' >/dev/null 2>&1; then
  (cd "$BACKEND_DIR" && nohup "$PYTHON" -m uvicorn main:app --reload --host 127.0.0.1 --port 8000 >>"$BACKEND_LOG" 2>&1 </dev/null) &
else
  UVICORN="$(command -v uvicorn || true)"
  if [[ -z "$UVICORN" ]]; then
    notify "Uvicorn is missing. Check backend/venv."
    exit 1
  fi
  (cd "$BACKEND_DIR" && nohup "$UVICORN" main:app --reload --host 127.0.0.1 --port 8000 >>"$BACKEND_LOG" 2>&1 </dev/null) &
fi
backend_pid=$!
print -r -- "$backend_pid" >| "$BACKEND_PID_FILE"

(cd "$FRONTEND_DIR" && nohup "$NEXT" dev --hostname 127.0.0.1 --port 3000 >>"$FRONTEND_LOG" 2>&1 </dev/null) &
frontend_pid=$!
print -r -- "$frontend_pid" >| "$FRONTEND_PID_FILE"

wait_for_url() {
  local url="$1"
  local attempt
  for attempt in {1..60}; do
    if /usr/bin/curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      return 0
    fi
    /bin/sleep 0.5
  done
  return 1
}

if ! wait_for_url "http://127.0.0.1:8000/health" || ! wait_for_url "http://127.0.0.1:3000/"; then
  notify "Literary Studio failed to become healthy. Check .runtime/*.log."
  exit 1
fi

/usr/bin/open "http://127.0.0.1:3000/"
notify "Literary Studio is live."
