#!/usr/bin/env bash
set -Eeuo pipefail

ACTION="${1:-status}"
PROJECT_ROOT="${PROJECT_ROOT:-/root/geng-agent-task-driven}"
CASES_ROOT="${GENG_CASES_ROOT:-/root/geng-agent-cases}"
WEB_HOST="${GENG_WEB_HOST:-127.0.0.1}"
WEB_PORT="${GENG_WEB_PORT:-8765}"
STATE_ROOT="$CASES_ROOT/.web"
PID_FILE="$STATE_ROOT/server.pid"
LOG_FILE="$STATE_ROOT/server.log"

[[ -f /root/.config/geng-agent/env.sh ]] && source /root/.config/geng-agent/env.sh
mkdir -p "$STATE_ROOT"

running_pid() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q 'geng-agent-web' || return 1
  printf '%s' "$pid"
}

health() {
  curl -fsS "http://$WEB_HOST:$WEB_PORT/api/v1/health"
}

case "$ACTION" in
  start)
    if pid="$(running_pid)"; then
      echo "web already running: pid=$pid"
      health
      exit 0
    fi
    rm -f "$PID_FILE"
    cd "$PROJECT_ROOT"
    nohup geng-agent-web --host "$WEB_HOST" --port "$WEB_PORT" >> "$LOG_FILE" 2>&1 &
    pid=$!
    printf '%s\n' "$pid" > "$PID_FILE"
    for _ in $(seq 1 40); do
      if health; then
        printf '\nweb started: pid=%s url=http://%s:%s\n' "$pid" "$WEB_HOST" "$WEB_PORT"
        exit 0
      fi
      sleep 0.25
    done
    echo "web failed to become healthy; last log lines:" >&2
    tail -n 80 "$LOG_FILE" >&2 || true
    kill -TERM "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    exit 1
    ;;
  stop)
    if ! pid="$(running_pid)"; then
      rm -f "$PID_FILE"
      echo 'web is not running'
      exit 0
    fi
    kill -TERM "$pid"
    for _ in $(seq 1 40); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.25
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "web did not stop after 10 seconds: pid=$pid" >&2
      exit 1
    fi
    rm -f "$PID_FILE"
    echo "web stopped: pid=$pid"
    ;;
  status)
    if pid="$(running_pid)"; then
      echo "web running: pid=$pid url=http://$WEB_HOST:$WEB_PORT"
      health
    else
      echo 'web is not running'
      exit 1
    fi
    ;;
  log)
    tail -n 100 "$LOG_FILE"
    ;;
  *)
    echo 'usage: remote_web.sh {start|stop|status|log}' >&2
    exit 2
    ;;
esac
