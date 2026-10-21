# shellcheck shell=bash
# Start and stop a background daemon from a verification script (sourced, not executed).
#
# Root cause of the first Ubuntu MVP run (43832c3): the daemon was started as
# `ng daemon ... &`, where `ng` is a shell FUNCTION. Backgrounding a function makes bash
# fork a subshell that runs the function and starts python as ITS child, so `$!` was the
# wrapper subshell, not the daemon. `kill -TERM $!` killed only the wrapper (exit 143) and
# left the daemon and its socket behind.
#
# start_daemon backgrounds the command itself, so DAEMON_PID is the daemon's own PID.
# stop_daemon succeeds only if that process exits by itself with status 0 AND its socket
# is gone. Anything else (timeout, signal, non-zero status, socket left) is a failure.

DAEMON_PID=
DAEMON_RC=

# start_daemon LOGFILE COMMAND [ARGS...]
start_daemon() {
  local log=$1
  shift
  "$@" > "$log" 2>&1 &   # a simple command: bash forks and execs it, so $! IS the daemon
  DAEMON_PID=$!
}

# daemon_running: true while DAEMON_PID exists and is not a zombie (an exited child that
# has not been reaped yet still answers `kill -0`, so its state is checked instead).
daemon_running() {
  local state
  [ -n "${DAEMON_PID:-}" ] || return 1
  state=$(ps -o stat= -p "$DAEMON_PID" 2>/dev/null | tr -d ' ')
  [ -n "$state" ] && [ "${state#Z}" = "$state" ]
}

# stop_daemon SOCKET TIMEOUT_SECONDS IDENTITY_PATTERN
# Signals DAEMON_PID only if its command line still contains IDENTITY_PATTERN (never a
# reused PID). Sets DAEMON_RC to the daemon's exit status ("timeout" or "not-ours" if it
# could not be collected). Returns 0 only for exit status 0 with the socket removed.
stop_daemon() {
  local sock=$1 timeout=$2 pattern=$3 args i
  DAEMON_RC=
  [ -n "${DAEMON_PID:-}" ] || { DAEMON_RC=not-started; return 1; }
  if daemon_running; then
    args=$(ps -o args= -p "$DAEMON_PID" 2>/dev/null)
    case "$args" in
      *"$pattern"*) kill -TERM "$DAEMON_PID" 2>/dev/null ;;
      *) DAEMON_RC=not-ours; return 1 ;;
    esac
  fi
  for ((i = 0; i < timeout * 10; i++)); do
    daemon_running || break
    sleep 0.1
  done
  if daemon_running; then
    DAEMON_RC=timeout
    return 1
  fi
  wait "$DAEMON_PID"
  DAEMON_RC=$?
  DAEMON_PID=
  for ((i = 0; i < 50; i++)); do [ -e "$sock" ] || break; sleep 0.1; done
  [ "$DAEMON_RC" -eq 0 ] && [ ! -e "$sock" ]
}
