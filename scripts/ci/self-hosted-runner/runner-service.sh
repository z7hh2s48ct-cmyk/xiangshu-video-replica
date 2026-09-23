#!/usr/bin/env bash
# Run the self-hosted runner as a background service on a dev VM / WSL2 host.
#
# The runner must be ONLINE while a PR's Linux quality gate is dispatched here
# (ci.yml select-runner falls back to GitHub-hosted ubuntu-24.04 when no
# `video-replica` runner is online, so stopping this is always safe — the gate
# simply runs on hosted runners again).
#
# Subcommands:
#   start    Launch ./run.sh in the background (nohup), record PID + log.
#   stop     Gracefully stop (SIGINT: finish current job, then exit); escalate.
#   status   Show whether the runner listener is alive.
#   logs     Tail the runner log.
#   install-systemd  Optional: register a boot-persistent service via the
#                    runner's own svc.sh (needs systemd; present on OrbStack VMs
#                    and WSL2 with systemd enabled).
#
# Env: RUNNER_DIR (default ~/actions-runner).

set -euo pipefail

log() { printf '\033[1;34m==> %s\033[0m\n' "$*"; }

RUNNER_DIR="${RUNNER_DIR:-${HOME}/actions-runner}"
PID_FILE="${RUNNER_DIR}/runner.nohup.pid"
LOG_FILE="${RUNNER_DIR}/runner.nohup.log"

cd "${RUNNER_DIR}" 2>/dev/null || {
  echo "ERROR: RUNNER_DIR '${RUNNER_DIR}' not found. Run register-runner.sh first." >&2
  exit 2
}
[ -f ./run.sh ] || { echo "ERROR: ./run.sh missing in ${RUNNER_DIR} — not a runner dir." >&2; exit 2; }

is_alive() {
  [ -f "${PID_FILE}" ] || return 1
  local pid
  pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
  [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null
}

case "${1:-}" in
  start)
    if is_alive; then
      log "Runner already running (PID $(cat "${PID_FILE}"))."
      exit 0
    fi
    log "Starting runner in background (log: ${LOG_FILE})"
    # setsid detaches into its own process group so `stop` can signal cleanly;
    # fall back to plain nohup when setsid is unavailable (e.g. minimal images).
    if command -v setsid >/dev/null 2>&1; then
      setsid nohup ./run.sh > "${LOG_FILE}" 2>&1 < /dev/null &
    else
      nohup ./run.sh > "${LOG_FILE}" 2>&1 < /dev/null &
    fi
    echo $! > "${PID_FILE}"
    sleep 3
    if is_alive; then
      log "Runner started (PID $(cat "${PID_FILE}")). It is ONLINE for PR jobs."
      tail -n 5 "${LOG_FILE}" 2>/dev/null || true
    else
      echo "ERROR: runner exited immediately; see ${LOG_FILE}" >&2
      tail -n 20 "${LOG_FILE}" 2>/dev/null || true
      exit 1
    fi
    ;;

  stop)
    if ! is_alive; then
      log "Runner not running."
      rm -f "${PID_FILE}"
      exit 0
    fi
    pid="$(cat "${PID_FILE}")"
    log "Stopping runner (PID ${pid}) gracefully — SIGINT lets it finish the current job..."
    kill -INT "${pid}" 2>/dev/null || true
    for _ in $(seq 1 30); do
      is_alive || break
      sleep 1
    done
    if is_alive; then
      log "Still alive after 30s; sending SIGTERM."
      kill -TERM "${pid}" 2>/dev/null || true
      sleep 3
    fi
    if is_alive; then
      log "Escalating to SIGKILL."
      kill -KILL "${pid}" 2>/dev/null || true
    fi
    rm -f "${PID_FILE}"
    log "Runner stopped. New PRs fall back to GitHub-hosted ubuntu-24.04."
    ;;

  status)
    if is_alive; then
      log "ONLINE: runner listener alive (PID $(cat "${PID_FILE}"))."
    else
      log "OFFLINE: no runner listener running (Linux gate uses hosted runners)."
    fi
    ;;

  logs)
    [ -f "${LOG_FILE}" ] || { echo "No log yet at ${LOG_FILE}" >&2; exit 1; }
    tail -n "${2:-50}" -f "${LOG_FILE}"
    ;;

  install-systemd)
    if ! command -v systemctl >/dev/null 2>&1; then
      echo "ERROR: systemd not available on this host; use 'start' (nohup) instead." >&2
      exit 1
    fi
    log "Installing boot-persistent service via the runner's svc.sh (needs sudo)"
    sudo ./svc.sh install
    sudo ./svc.sh start
    log "Service installed + started. Manage with: sudo ./svc.sh {status|stop|start|uninstall}"
    ;;

  *)
    echo "Usage: $0 {start|stop|status|logs [N]|install-systemd}" >&2
    exit 2
    ;;
esac
