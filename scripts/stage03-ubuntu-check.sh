#!/usr/bin/env bash
# NapGuard Stage 03a: Ubuntu verification of the DMTCP backend integration (ADR 0006).
#
#   bash scripts/stage03-ubuntu-check.sh <EXPECTED_SHA>                   # prerequisites only
#   bash scripts/stage03-ubuntu-check.sh <EXPECTED_SHA> --run-selftest    # + ONE c-single self-test
#   ... --run-selftest --class c-threads                                  # the other allowed class
#
# Developer mode only, on the isolated single-user Ubuntu 24.04 ARM64 VM. The self-test
# starts a DMTCP 4.2.0 coordinator ONLY through NapGuard's API with
# --i-accept-single-user-coordinator (fail-closed loopback hand-off, listener audits).
#
# Order (stops at the first failure; the self-test runs only after every prerequisite):
#   1. HEAD == EXPECTED_SHA, clean working tree
#   2. Stage 00 baseline (venv, pytest, Ruff, mypy)
#   3. Linux-only Stage 03 tests: all must run (zero skipped) and pass
#   4. Listener snapshot BEFORE
#   5. napguard-dev backend probe: DMTCP 4.2.0 available on the verified platform
#   6. only with --run-selftest: napguard-dev selftest (checkpoint, terminate, restore, verify)
#   7. Listener snapshot AFTER: no new non-loopback listener
#   8. No DMTCP / reference-workload process of this user left
#   9. Evidence bundle (checkpoint images excluded)
# No sudo, no installs, no system changes. Exit: 0 PASS, 1 FAIL, 2 BLOCKED.
set -u

usage() {
  echo "usage: $0 <EXPECTED_SHA (40 hex)> [--run-selftest] [--class c-single|c-threads]" >&2
  exit 2
}
EXPECTED=${1:-}
[[ "$EXPECTED" =~ ^[0-9a-f]{40}$ ]] || usage
shift
RUN=no
CLASS=c-single
while [ $# -gt 0 ]; do
  case "$1" in
    --run-selftest) RUN=yes ;;
    --class)
      [ $# -ge 2 ] || usage
      case "$2" in  # strict allowlist: Stage 02-verified single-process reference classes
        c-single|c-threads) CLASS=$2 ;;
        *) usage ;;
      esac
      shift ;;
    *) usage ;;
  esac
  shift
done

REPO="$(cd "$(dirname "$0")/.." && pwd)" || exit 2
OUT="$HOME/napguard-stage03-evidence-$(date -u +%Y%m%dT%H%M%SZ)"
WORK_ROOT="$HOME/.local/share/napguard-dev/selftest"
VERDICT=PASS
export PYTHONDONTWRITEBYTECODE=1

finish() {
  echo "evidence: $OUT"
  latest=$(ls -1dt "$WORK_ROOT"/* 2>/dev/null | head -1)
  if [ "$RUN" = yes ] && [ -n "$latest" ]; then
    tar -C "$(dirname "$latest")" --exclude='*.dmtcp' --exclude='*.temp' \
      -czf "$OUT/selftest-run.tgz" "$(basename "$latest")" 2>/dev/null || true
  fi
  case "$1" in
    0) echo "STAGE 03A UBUNTU VERIFICATION: PASS" ;;
    1) echo "STAGE 03A UBUNTU VERIFICATION: FAIL" ;;
    *) echo "STAGE 03A UBUNTU VERIFICATION: BLOCKED" ;;
  esac | tee -a "$OUT/verdict.txt"
  exit "$1"
}
blocked() { echo "BLOCKED: $1" | tee -a "$OUT/verdict.txt"; finish 2; }
failed() { echo "FAIL: $1" | tee -a "$OUT/verdict.txt"; VERDICT=FAIL; }
step() { echo; echo "### $1" | tee -a "$OUT/steps.txt"; }
listeners() { ss -H -l -n -t -u -p 2>&1; }
non_loopback() { awk '{print $1" "$5}' "$1" | grep -vE ' (127\.|\[::1\]:|::1:)' | sort -u; }
napguard_dev() { env PYTHONPATH="$REPO/src" python3 -s -m napguard.cli "$@"; }

[ "$(uname -s)" = Linux ] || { echo "run inside Ubuntu" >&2; exit 2; }
[ "$(id -u)" -ne 0 ] || { echo "do not run as root" >&2; exit 2; }
mkdir -p "$OUT" && chmod 700 "$OUT"
cd "$REPO" || exit 2
{ date -Is; uname -a; grep PRETTY_NAME /etc/os-release; python3 --version
  echo "boot_id=$(cat /proc/sys/kernel/random/boot_id) run_selftest=$RUN class=$CLASS"; } \
  | tee "$OUT/host.txt"

step "1. Exact revision and clean tree"
HEAD_SHA=$(git rev-parse HEAD)
echo "HEAD=$HEAD_SHA expected=$EXPECTED" | tee -a "$OUT/steps.txt"
[ "$HEAD_SHA" = "$EXPECTED" ] || blocked "HEAD $HEAD_SHA is not $EXPECTED"
[ -z "$(git status --porcelain)" ] || blocked "working tree not clean: $(git status --porcelain | head -3)"

step "2. Stage 00 baseline (pytest, Ruff, mypy in a temporary venv)"
bash scripts/layer0-ubuntu-check.sh "$EXPECTED" > "$OUT/baseline.log" 2>&1
rc=$?; tail -3 "$OUT/baseline.log"
[ $rc -eq 0 ] || blocked "baseline exit $rc (see baseline.log)"

step "3. Linux-only Stage 03 tests (none may be skipped)"
python3 -m pytest -q -rs -p no:cacheprovider tests/unit/test_stage03_*.py \
  > "$OUT/stage03-tests.log" 2>&1
rc=$?; tail -3 "$OUT/stage03-tests.log"
[ $rc -eq 0 ] || blocked "Stage 03 tests exit $rc (see stage03-tests.log)"
! grep -qi skipped "$OUT/stage03-tests.log" || blocked "some Stage 03 tests were skipped"

step "4. Listener snapshot BEFORE"
listeners > "$OUT/listeners-before.txt"; non_loopback "$OUT/listeners-before.txt" > "$OUT/nonloop-before.txt"
cat "$OUT/nonloop-before.txt"

step "5. Backend probe (starts nothing)"
napguard_dev backend probe > "$OUT/probe.txt" 2>&1
rc=$?; cat "$OUT/probe.txt"
napguard_dev backend probe --json > "$OUT/probe.json" 2>/dev/null
[ $rc -eq 0 ] || blocked "DMTCP 4.2.0 not available (probe exit $rc)"
grep -q 'verified=True' "$OUT/probe.txt" || blocked "platform is not the Stage 02-verified one"

if [ "$RUN" = yes ]; then
  step "6. ONE developer-mode self-test ($CLASS) through NapGuard's API"
  napguard_dev selftest --i-accept-single-user-coordinator --class "$CLASS" \
    --work-root "$WORK_ROOT" > "$OUT/selftest.log" 2>&1
  rc=$?; cat "$OUT/selftest.log"
  case $rc in
    0) ;;
    2) blocked "self-test BLOCKED (see selftest.log)" ;;
    *) failed "self-test exit $rc (see selftest.log)" ;;
  esac
else
  step "6. Nothing run (pass --run-selftest after reviewing 1-5)"
fi

step "7. Listener snapshot AFTER"
listeners > "$OUT/listeners-after.txt"; non_loopback "$OUT/listeners-after.txt" > "$OUT/nonloop-after.txt"
comm -13 "$OUT/nonloop-before.txt" "$OUT/nonloop-after.txt" > "$OUT/nonloop-new.txt"
if [ -s "$OUT/nonloop-new.txt" ]; then
  cat "$OUT/nonloop-new.txt"; failed "new non-loopback listener(s) after the run"
else
  echo "no new non-loopback listeners"
fi

step "8. Leftover DMTCP / reference-workload processes of this user"
pgrep -u "$(id -u)" -a -f 'dmtcp_coordinator|dmtcp_launch|dmtcp_restart|mtcp_restart|napguard-dev/selftest/' \
  > "$OUT/leftover-processes.txt"
if [ -s "$OUT/leftover-processes.txt" ]; then
  cat "$OUT/leftover-processes.txt"; failed "processes still running (reported, not killed)"
else
  echo "none"
fi

step "9. Evidence"
[ "$VERDICT" = PASS ] && finish 0 || finish 1
