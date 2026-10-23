#!/usr/bin/env bash
# NapGuard final Ubuntu verification (Stages 11-12): install, daemon, GUI, real checkpoint and
# restore, scheduling, simulated battery, archive, reset, clean shutdown, no exposure/leftovers.
#
#   bash scripts/final-ubuntu-check.sh <EXPECTED_SHA>              # prerequisites only
#   bash scripts/final-ubuntu-check.sh <EXPECTED_SHA> --run-demo   # + the full end-to-end run
#
# Run from the Ubuntu desktop session (the GUI smoke test needs a display) as the ordinary user.
# Uses its own data directory and socket, so your normal NapGuard data is not touched.
# Requires python3-tk for the GUI step. No sudo, no installs, no system changes.
# Exit: 0 PASS, 1 FAIL, 2 BLOCKED.
set -u

usage() { echo "usage: $0 <EXPECTED_SHA (40 hex)> [--run-demo]" >&2; exit 2; }
EXPECTED=${1:-}
[[ "$EXPECTED" =~ ^[0-9a-f]{40}$ ]] || usage
shift
RUN=no
while [ $# -gt 0 ]; do case "$1" in --run-demo) RUN=yes ;; *) usage ;; esac; shift; done

REPO="$(cd "$(dirname "$0")/.." && pwd)" || exit 2
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$HOME/napguard-final-evidence-$STAMP"
DATA="$HOME/.local/share/napguard-final-check/$STAMP"
SOCK="${XDG_RUNTIME_DIR:-/nonexistent}/napguard-final-check/daemon.sock"
VERDICT=PASS
export PYTHONDONTWRITEBYTECODE=1
# shellcheck source=lib/daemon-control.sh
. "$REPO/scripts/lib/daemon-control.sh"

finish() {
  if [ -n "$DAEMON_PID" ]; then
    stop_daemon "$SOCK" 60 napguard.main || \
      echo "FAIL: daemon (pid $DAEMON_PID) did not stop cleanly: status $DAEMON_RC" | tee -a "$OUT/verdict.txt"
  fi
  [ -d "$DATA" ] && tar -C "$(dirname "$DATA")" --exclude='*.dmtcp' --exclude='*.temp' \
    -czf "$OUT/final-data.tgz" "$(basename "$DATA")" 2>/dev/null
  echo "evidence: $OUT"
  case "$1" in
    0) echo "NAPGUARD FINAL UBUNTU VERIFICATION: PASS" ;;
    1) echo "NAPGUARD FINAL UBUNTU VERIFICATION: FAIL" ;;
    *) echo "NAPGUARD FINAL UBUNTU VERIFICATION: BLOCKED" ;;
  esac | tee -a "$OUT/verdict.txt"
  exit "$1"
}
blocked() { echo "BLOCKED: $1" | tee -a "$OUT/verdict.txt"; finish 2; }
failed() { echo "FAIL: $1" | tee -a "$OUT/verdict.txt"; VERDICT=FAIL; }
step() { echo; echo "### $1" | tee -a "$OUT/steps.txt"; }
ng() { env PYTHONPATH="$REPO/src" python3 -s -m napguard.main --socket "$SOCK" "$@"; }
py() { python3 -I -c "$1"; }
listeners() { ss -H -l -n -t -u -p 2>&1; }
non_loopback() { awk '{print $1" "$5}' "$1" | grep -vE ' (127\.|\[::1\]:|::1:)' | sort -u; }
# wait_op TRIGGER_PREFIX SECONDS: wait for a SUCCEEDED checkpoint with that trigger
wait_op() {
  local end=$((SECONDS + $2)) state
  while [ $SECONDS -lt $end ]; do
    state=$(ng --json operations --limit 50 | py "import json,sys
ops=[o for o in json.load(sys.stdin) if o['kind']=='checkpoint' and (o['trigger'] or '').startswith('$1')]
print(ops[0]['state'] if ops else 'NONE')")
    case "$state" in SUCCEEDED) return 0 ;; FAILED|INTERRUPTED) return 1 ;; esac
    sleep 3
  done
  return 1
}

[ "$(uname -s)" = Linux ] || { echo "run inside Ubuntu" >&2; exit 2; }
[ "$(id -u)" -ne 0 ] || { echo "do not run as root" >&2; exit 2; }
mkdir -p "$OUT" && chmod 700 "$OUT"
cd "$REPO" || exit 2
{ date -Is; uname -a; grep PRETTY_NAME /etc/os-release; python3 --version
  echo "boot_id=$(cat /proc/sys/kernel/random/boot_id) run_demo=$RUN display=${DISPLAY:-none}"; } \
  | tee "$OUT/host.txt"

step "1. Exact revision, clean tree, Ubuntu 24.04 aarch64"
HEAD_SHA=$(git rev-parse HEAD)
echo "HEAD=$HEAD_SHA expected=$EXPECTED" | tee -a "$OUT/steps.txt"
[ "$HEAD_SHA" = "$EXPECTED" ] || blocked "HEAD $HEAD_SHA is not $EXPECTED"
[ -z "$(git status --porcelain)" ] || blocked "working tree not clean"
[ "$(uname -m)" = aarch64 ] || blocked "architecture $(uname -m) is not aarch64"
. /etc/os-release
[ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 24.04 ] || blocked "not Ubuntu 24.04"
[ -n "${XDG_RUNTIME_DIR:-}" ] || blocked "XDG_RUNTIME_DIR is not set"
python3 -c 'import tkinter' 2>/dev/null || blocked "python3-tk missing (sudo apt install python3-tk)"
[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || blocked "no display: run from the Ubuntu desktop"

step "2. Stage 00 baseline (pytest, Ruff, mypy in a temporary venv)"
bash scripts/layer0-ubuntu-check.sh "$EXPECTED" > "$OUT/baseline.log" 2>&1
rc=$?; tail -3 "$OUT/baseline.log"
[ $rc -eq 0 ] || blocked "baseline exit $rc (see baseline.log)"

step "3. Every Linux test (Stage 03, MVP, final, GUI smoke) — none may be skipped"
python3 -m pytest --version > "$OUT/pytest-version.txt" 2>&1
rc=$?; cat "$OUT/pytest-version.txt"
[ $rc -eq 0 ] || blocked "system Python cannot run pytest (see pytest-version.txt)"
python3 -m pytest -q -rs -p no:cacheprovider tests/unit/test_stage03_*.py tests/unit/test_mvp_*.py \
  tests/unit/test_final_*.py > "$OUT/tests.log" 2>&1
rc=$?; tail -3 "$OUT/tests.log"
[ $rc -eq 0 ] || blocked "tests exit $rc (see tests.log)"
! grep -qi skipped "$OUT/tests.log" || blocked "some tests were skipped"

step "4. Installation (sandboxed prefix; your ~/.local is not touched)"
SANDBOX="$OUT/install-sandbox"
mkdir -p "$SANDBOX/home"
INSTALL_ENV=(env HOME="$SANDBOX/home" XDG_DATA_HOME="$SANDBOX/home/.local/share"
             XDG_CONFIG_HOME="$SANDBOX/home/.config")
"${INSTALL_ENV[@]}" bash scripts/install.sh --with-desktop --with-systemd > "$OUT/install.log" 2>&1 \
  || failed "install.sh failed (see install.log)"
"${INSTALL_ENV[@]}" "$SANDBOX/home/.local/bin/napguard" --help > /dev/null || failed "installed command fails"
"${INSTALL_ENV[@]}" bash scripts/uninstall.sh >> "$OUT/install.log" 2>&1 || failed "uninstall.sh failed"
[ -z "$(find "$SANDBOX/home" -type f -name 'napguard*' ! -path '*/napguard/*')" ] || \
  failed "uninstall left files behind"
tail -4 "$OUT/install.log"

step "5. Backend probe and listener snapshot BEFORE"
env PYTHONPATH="$REPO/src" python3 -s -m napguard.cli backend probe > "$OUT/probe.txt" 2>&1
rc=$?; cat "$OUT/probe.txt"
[ $rc -eq 0 ] && grep -q 'verified=True' "$OUT/probe.txt" || blocked "DMTCP 4.2.0 not available/verified"
listeners > "$OUT/listeners-before.txt"; non_loopback "$OUT/listeners-before.txt" > "$OUT/nonloop-before.txt"

if [ "$RUN" = yes ]; then
  step "6. Daemon startup (developer mode, this run only)"
  start_daemon "$OUT/daemon.log" env PYTHONPATH="$REPO/src" python3 -s -m napguard.main \
    --socket "$SOCK" daemon --i-accept-single-user-coordinator --data-dir "$DATA"
  ps -o args= -p "$DAEMON_PID" | grep -q 'napguard.main' || blocked "\$! is not the daemon"
  for _ in $(seq 1 50); do [ -S "$SOCK" ] && break; sleep 0.2; done
  [ -S "$SOCK" ] || { cat "$OUT/daemon.log"; blocked "daemon socket did not appear"; }
  [ "$(stat -c %a "$SOCK")" = 600 ] || failed "socket mode is not 600"
  ng status | tee "$OUT/status.txt"

  step "7. GUI startup (every screen rendered against the live daemon)"
  NAPGUARD_GUI_CONFIG="$OUT/gui.json" ng gui --smoke > "$OUT/gui-smoke.txt" 2>&1 || failed "GUI smoke failed"
  cat "$OUT/gui-smoke.txt"
  grep -q 'daemon connected' "$OUT/gui-smoke.txt" || failed "GUI did not connect to the daemon"

  step "8. Launch, checkpoint, stop, verify, restore (genuine DMTCP)"
  ng launch-demo --class c-single > "$OUT/launch.txt" 2>&1 || failed "launch failed"
  WID=$(ng --json workloads | py 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
  sleep 3
  ng checkpoint "$WID" > "$OUT/checkpoint.txt" 2>&1 || failed "checkpoint failed"
  sleep 3
  ng stop "$WID" > "$OUT/stop.txt" 2>&1 || failed "stop failed"
  CID=$(ng --json checkpoints "$WID" | py 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
  ng verify "$CID" > "$OUT/verify.txt" 2>&1 || failed "verify failed"
  ng --json restore "$CID" --yes > "$OUT/restore.json" 2> "$OUT/restore.err" || failed "restore failed (see restore.json)"
  py "import json; r=json.load(open('$OUT/restore.json'))['result']; print('nonce', r['original_nonce'], '->', r['restored_nonce']); print('counters: checkpoint', r['counter_at_checkpoint'], 'termination', r['counter_before_termination'], 'first restored', r['first_restored_counter'], 'later', r['last_restored_counter']); raise SystemExit(0 if r['continuity_verified'] and r['original_nonce']==r['restored_nonce'] else 1)" \
    | tee "$OUT/continuity.txt" || failed "continuity not verified"

  step "9. Schedule a checkpoint (real, periodic, 60 s interval)"
  ng launch-demo --class c-threads > "$OUT/launch2.txt" 2>&1 || failed "second launch failed"
  WID2=$(ng --json workloads | py 'import json,sys; print([w["id"] for w in json.load(sys.stdin) if w["workload_class"]=="c-threads"][0])')
  ng schedule --enable --interval 60 | tee "$OUT/schedule.txt"
  wait_op periodic 150 || failed "no successful periodic checkpoint within 150 s"
  ng schedule --disable >> "$OUT/schedule.txt"

  step "10. Simulated battery event (SIMULATED reading -> REAL checkpoint)"
  ng battery --simulate 15 | tee "$OUT/battery.txt"
  grep -q SIMULATED "$OUT/battery.txt" || failed "simulated reading not labelled"
  wait_op low_battery_simulated 90 || failed "low-battery simulation did not produce a real checkpoint"
  ng battery --clear-simulation >> "$OUT/battery.txt"

  step "11. Stop workloads, archive completed records"
  ng stop "$WID" > "$OUT/stop-restored.txt" 2>&1 || failed "stopping the restored instance failed"
  ng stop "$WID2" > "$OUT/stop2.txt" 2>&1 || failed "stopping the second workload failed"
  ng archive "$WID" > "$OUT/archive.txt" 2>&1 || failed "archive failed"
  [ "$(ng --json workloads | py 'import json,sys; print(len(json.load(sys.stdin)))')" = 1 ] || \
    failed "archived record still listed by default"

  step "12. Reset demo history (preview first)"
  ng history | tee "$OUT/history-preview.txt"
  ng reset-demo --yes > "$OUT/reset.txt" 2>&1 || failed "reset-demo failed"
  [ "$(ng --json workloads | py 'import json,sys; print(len(json.load(sys.stdin)))')" = 0 ] || \
    failed "workloads remain after reset"
  ng events --limit 300 > "$OUT/events.txt"
  ng diagnostics > "$OUT/diagnostics.txt" 2>&1

  step "13. Clean shutdown"
  pid=$DAEMON_PID
  if stop_daemon "$SOCK" 60 napguard.main; then
    echo "daemon pid $pid exited with status 0 and removed $SOCK"
  else
    failed "daemon did not stop cleanly: status $DAEMON_RC"
  fi
else
  step "6-13. Nothing run (pass --run-demo after reviewing 1-5)"
fi

step "14. Listener snapshot AFTER and leftover processes"
listeners > "$OUT/listeners-after.txt"; non_loopback "$OUT/listeners-after.txt" > "$OUT/nonloop-after.txt"
comm -13 "$OUT/nonloop-before.txt" "$OUT/nonloop-after.txt" > "$OUT/nonloop-new.txt"
[ -s "$OUT/nonloop-new.txt" ] && { cat "$OUT/nonloop-new.txt"; failed "new non-loopback listener(s)"; }
pgrep -u "$(id -u)" -a -f 'dmtcp_coordinator|dmtcp_launch|dmtcp_restart|mtcp_restart|napguard-final-check/' \
  > "$OUT/leftover-processes.txt"
[ -s "$OUT/leftover-processes.txt" ] && { cat "$OUT/leftover-processes.txt"; failed "processes left (reported, not killed)"; }

[ "$VERDICT" = PASS ] && finish 0 || finish 1
