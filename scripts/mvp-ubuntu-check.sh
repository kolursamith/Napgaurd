#!/usr/bin/env bash
# NapGuard MVP (Stages 04-10): Ubuntu verification of the daemon, CLI, catalog, and recovery.
#
#   bash scripts/mvp-ubuntu-check.sh <EXPECTED_SHA>              # prerequisites only
#   bash scripts/mvp-ubuntu-check.sh <EXPECTED_SHA> --run-demo   # + ONE real end-to-end demo
#
# Developer mode only (ADR 0006, ADR 0008 proposed), on the single-user Ubuntu 24.04 ARM64 VM.
# The demo starts the user-level daemon with --i-accept-single-user-coordinator and drives it
# through the CLI: launch a verified reference workload, checkpoint it with DMTCP, stop it,
# verify the checkpoint, restore it, and check continuity. Then it stops the daemon.
# No sudo, no installs, no system changes. Exit: 0 PASS, 1 FAIL, 2 BLOCKED.
set -u

usage() { echo "usage: $0 <EXPECTED_SHA (40 hex)> [--run-demo]" >&2; exit 2; }
EXPECTED=${1:-}
[[ "$EXPECTED" =~ ^[0-9a-f]{40}$ ]] || usage
shift
RUN=no
while [ $# -gt 0 ]; do
  case "$1" in --run-demo) RUN=yes ;; *) usage ;; esac
  shift
done

REPO="$(cd "$(dirname "$0")/.." && pwd)" || exit 2
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$HOME/napguard-mvp-evidence-$STAMP"
DATA="$HOME/.local/share/napguard-mvp-check/$STAMP"
SOCK="${XDG_RUNTIME_DIR:-/nonexistent}/napguard-mvp-check/daemon.sock"
VERDICT=PASS
DAEMON_PID=
export PYTHONDONTWRITEBYTECODE=1

finish() {
  if [ -n "$DAEMON_PID" ] && kill -0 "$DAEMON_PID" 2>/dev/null; then
    kill -TERM "$DAEMON_PID"; wait "$DAEMON_PID" 2>/dev/null
  fi
  [ -d "$DATA" ] && tar -C "$(dirname "$DATA")" --exclude='*.dmtcp' --exclude='*.temp' \
    -czf "$OUT/mvp-data.tgz" "$(basename "$DATA")" 2>/dev/null
  echo "evidence: $OUT"
  case "$1" in
    0) echo "NAPGUARD MVP UBUNTU VERIFICATION: PASS" ;;
    1) echo "NAPGUARD MVP UBUNTU VERIFICATION: FAIL" ;;
    *) echo "NAPGUARD MVP UBUNTU VERIFICATION: BLOCKED" ;;
  esac | tee -a "$OUT/verdict.txt"
  exit "$1"
}
blocked() { echo "BLOCKED: $1" | tee -a "$OUT/verdict.txt"; finish 2; }
failed() { echo "FAIL: $1" | tee -a "$OUT/verdict.txt"; VERDICT=FAIL; }
step() { echo; echo "### $1" | tee -a "$OUT/steps.txt"; }
ng() { env PYTHONPATH="$REPO/src" python3 -s -m napguard.main --socket "$SOCK" "$@"; }
first_id() { python3 -I -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])'; }
first_restore_verified() { python3 -I -c 'import json,sys; print(json.load(sys.stdin)[0]["restore_verified_utc"] or "")'; }
listeners() { ss -H -l -n -t -u -p 2>&1; }
non_loopback() { awk '{print $1" "$5}' "$1" | grep -vE ' (127\.|\[::1\]:|::1:)' | sort -u; }

[ "$(uname -s)" = Linux ] || { echo "run inside Ubuntu" >&2; exit 2; }
[ "$(id -u)" -ne 0 ] || { echo "do not run as root" >&2; exit 2; }
mkdir -p "$OUT" && chmod 700 "$OUT"
cd "$REPO" || exit 2
{ date -Is; uname -a; grep PRETTY_NAME /etc/os-release; python3 --version
  echo "boot_id=$(cat /proc/sys/kernel/random/boot_id) run_demo=$RUN"; } | tee "$OUT/host.txt"

step "1. Exact revision, clean tree, Ubuntu 24.04 aarch64"
HEAD_SHA=$(git rev-parse HEAD)
echo "HEAD=$HEAD_SHA expected=$EXPECTED" | tee -a "$OUT/steps.txt"
[ "$HEAD_SHA" = "$EXPECTED" ] || blocked "HEAD $HEAD_SHA is not $EXPECTED"
[ -z "$(git status --porcelain)" ] || blocked "working tree not clean"
[ "$(uname -m)" = aarch64 ] || blocked "architecture $(uname -m) is not aarch64"
. /etc/os-release
[ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 24.04 ] || blocked "not Ubuntu 24.04"
[ -n "${XDG_RUNTIME_DIR:-}" ] || blocked "XDG_RUNTIME_DIR is not set"

step "2. Stage 00 baseline (pytest, Ruff, mypy in a temporary venv)"
bash scripts/layer0-ubuntu-check.sh "$EXPECTED" > "$OUT/baseline.log" 2>&1
rc=$?; tail -3 "$OUT/baseline.log"
[ $rc -eq 0 ] || blocked "baseline exit $rc (see baseline.log)"

step "3. Stage 03 and MVP tests on Linux (none may be skipped)"
python3 -m pytest -q -rs -p no:cacheprovider tests/unit/test_stage03_*.py tests/unit/test_mvp_*.py \
  > "$OUT/mvp-tests.log" 2>&1
rc=$?; tail -3 "$OUT/mvp-tests.log"
[ $rc -eq 0 ] || blocked "tests exit $rc (see mvp-tests.log)"
! grep -qi skipped "$OUT/mvp-tests.log" || blocked "some tests were skipped"

step "4. Backend probe and listener snapshot BEFORE"
env PYTHONPATH="$REPO/src" python3 -s -m napguard.cli backend probe > "$OUT/probe.txt" 2>&1
rc=$?; cat "$OUT/probe.txt"
[ $rc -eq 0 ] && grep -q 'verified=True' "$OUT/probe.txt" || blocked "DMTCP 4.2.0 not available/verified"
listeners > "$OUT/listeners-before.txt"; non_loopback "$OUT/listeners-before.txt" > "$OUT/nonloop-before.txt"

if [ "$RUN" = yes ]; then
  step "5. Start the user-level daemon (developer mode)"
  ng daemon --i-accept-single-user-coordinator --data-dir "$DATA" > "$OUT/daemon.log" 2>&1 &
  DAEMON_PID=$!
  for _ in $(seq 1 50); do [ -S "$SOCK" ] && break; sleep 0.2; done
  [ -S "$SOCK" ] || { cat "$OUT/daemon.log"; blocked "daemon socket did not appear"; }
  [ "$(stat -c %a "$SOCK")" = 600 ] || failed "socket mode is $(stat -c %a "$SOCK"), not 600"
  ng status | tee "$OUT/status.txt"

  step "6. Launch, checkpoint, stop, verify, restore (genuine DMTCP)"
  ng launch-demo --class c-single > "$OUT/launch.txt" 2>&1 || failed "launch-demo failed"
  cat "$OUT/launch.txt"
  WID=$(ng --json workloads | first_id) || blocked "no workload"
  sleep 3
  ng checkpoint "$WID" > "$OUT/checkpoint.txt" 2>&1 || failed "checkpoint failed"
  cat "$OUT/checkpoint.txt"
  sleep 3
  ng stop "$WID" > "$OUT/stop.txt" 2>&1 || failed "stop failed"
  CID=$(ng --json checkpoints "$WID" | first_id) || blocked "no checkpoint"
  ng verify "$CID" > "$OUT/verify.txt" 2>&1 || failed "verify failed"
  cat "$OUT/verify.txt"
  ng restore "$CID" --yes > "$OUT/restore.txt" 2>&1 || failed "restore failed (see restore.txt)"
  cat "$OUT/restore.txt"
  ng --json checkpoints "$WID" > "$OUT/checkpoints.json"
  first_restore_verified < "$OUT/checkpoints.json" | grep -q . || \
    failed "checkpoint not marked restore-verified"
  ng --json workloads > "$OUT/workloads.json"
  ng events --limit 200 > "$OUT/events.txt"
  sleep 2
  ng stop "$WID" > "$OUT/stop-restored.txt" 2>&1 || failed "stopping the restored instance failed"

  step "7. Stop the daemon"
  kill -TERM "$DAEMON_PID"; wait "$DAEMON_PID"; rc=$?; DAEMON_PID=
  tail -3 "$OUT/daemon.log"
  [ $rc -eq 0 ] || failed "daemon exit $rc"
  [ ! -e "$SOCK" ] || failed "socket left behind"
else
  step "5-7. Nothing run (pass --run-demo after reviewing 1-4)"
fi

step "8. Listener snapshot AFTER and leftover processes"
listeners > "$OUT/listeners-after.txt"; non_loopback "$OUT/listeners-after.txt" > "$OUT/nonloop-after.txt"
comm -13 "$OUT/nonloop-before.txt" "$OUT/nonloop-after.txt" > "$OUT/nonloop-new.txt"
[ -s "$OUT/nonloop-new.txt" ] && { cat "$OUT/nonloop-new.txt"; failed "new non-loopback listener(s)"; }
pgrep -u "$(id -u)" -a -f 'dmtcp_coordinator|dmtcp_launch|dmtcp_restart|mtcp_restart|napguard-mvp-check/' \
  > "$OUT/leftover-processes.txt"
[ -s "$OUT/leftover-processes.txt" ] && { cat "$OUT/leftover-processes.txt"; failed "processes left (reported, not killed)"; }

[ "$VERDICT" = PASS ] && finish 0 || finish 1
