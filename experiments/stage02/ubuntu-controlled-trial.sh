#!/usr/bin/env bash
# NapGuard Stage 02: Ubuntu verification for the ADR 0004 controlled DMTCP trial.
#
#   bash experiments/stage02/ubuntu-controlled-trial.sh <EXPECTED_SHA>              # prerequisites only
#   bash experiments/stage02/ubuntu-controlled-trial.sh <EXPECTED_SHA> --run-trial  # + ONE c-single trial
#
# Order (stops at the first failure; the trial runs only after every prerequisite passes):
#   1. HEAD == EXPECTED_SHA, clean working tree
#   2. Stage 00 baseline (venv install, pytest, Ruff, mypy)
#   3. Linux-only Stage 02 security tests: all must run (zero skipped) and pass
#   4. Listener snapshot BEFORE
#   5. Fresh dmtcp-isolation-check (same boot)
#   6. [--run-trial only] trial --backend dmtcp --class c-single --adr-0004-controlled-trial
#   7. Listener snapshot AFTER: no new non-loopback listener
#   8. No DMTCP/workload process of this user left
#   9. Evidence bundle (checkpoint images excluded)
# No sudo, no installs, no system changes. Exit: 0 PASS, 1 FAIL, 2 BLOCKED.
set -u

usage() { echo "usage: $0 <EXPECTED_SHA (40 hex)> [--run-trial]" >&2; exit 2; }
EXPECTED=${1:-}
[[ "$EXPECTED" =~ ^[0-9a-f]{40}$ ]] || usage
RUN_TRIAL=0
case "${2:-}" in
  "") ;;
  --run-trial) RUN_TRIAL=1 ;;
  *) usage ;;
esac
[ $# -le 2 ] || usage

REPO="$(cd "$(dirname "$0")/../.." && pwd)" || exit 2
OUT="$HOME/napguard-stage02-evidence-$(date -u +%Y%m%dT%H%M%SZ)"
VERDICT=PASS

blocked() { echo "BLOCKED: $1" | tee -a "$OUT/verdict.txt"; finish 2; }
failed() { echo "FAIL: $1" | tee -a "$OUT/verdict.txt"; VERDICT=FAIL; }
step() { echo; echo "### $1" | tee -a "$OUT/steps.txt"; }
finish() {
  echo "evidence: $OUT"
  tar -C "$HOME/.local/share" --exclude='*/checkpoint/*' -czf "$OUT/runs.tgz" napguard-stage02 \
    2>/dev/null || true
  case "$1" in
    0) echo "STAGE 02 CONTROLLED TRIAL VERIFICATION: PASS" ;;
    1) echo "STAGE 02 CONTROLLED TRIAL VERIFICATION: FAIL" ;;
    *) echo "STAGE 02 CONTROLLED TRIAL VERIFICATION: BLOCKED" ;;
  esac | tee -a "$OUT/verdict.txt"
  exit "$1"
}
listeners() { ss -H -l -n -t -u -p 2>&1; }
non_loopback() { awk '{print $1" "$5}' "$1" | grep -vE ' (127\.|\[::1\]:|::1:)' | sort -u; }

[ "$(uname -s)" = Linux ] || { echo "run inside Ubuntu" >&2; exit 2; }
[ "$(id -u)" -ne 0 ] || { echo "do not run as root" >&2; exit 2; }
mkdir -p "$OUT" && chmod 700 "$OUT"
cd "$REPO" || exit 2
{ date -Is; uname -a; grep PRETTY_NAME /etc/os-release; python3 --version; } | tee "$OUT/host.txt"

step "1. Exact revision and clean tree"
HEAD_SHA=$(git rev-parse HEAD)
echo "HEAD=$HEAD_SHA expected=$EXPECTED" | tee -a "$OUT/steps.txt"
[ "$HEAD_SHA" = "$EXPECTED" ] || blocked "HEAD $HEAD_SHA is not $EXPECTED"
[ -z "$(git status --porcelain)" ] || blocked "working tree not clean: $(git status --porcelain | head -3)"

step "2. Stage 00 baseline (pytest, Ruff, mypy in a temporary venv)"
bash scripts/layer0-ubuntu-check.sh "$EXPECTED" > "$OUT/baseline.log" 2>&1
rc=$?; tail -3 "$OUT/baseline.log"
[ $rc -eq 0 ] || blocked "baseline exit $rc (see baseline.log)"

step "3. Linux-only Stage 02 security tests (none may be skipped)"
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -rs -p no:cacheprovider \
  tests/unit/test_stage02_safety.py tests/unit/test_stage02_launch_config.py > "$OUT/security-tests.log" 2>&1
rc=$?; tail -3 "$OUT/security-tests.log"
[ $rc -eq 0 ] || blocked "security tests exit $rc (see security-tests.log)"
! grep -qi skipped "$OUT/security-tests.log" || blocked "some security tests were skipped"

step "4. Listener snapshot BEFORE"
listeners > "$OUT/listeners-before.txt"; non_loopback "$OUT/listeners-before.txt" > "$OUT/nonloop-before.txt"
cat "$OUT/nonloop-before.txt"

step "5. Fresh DMTCP isolation check"
python3 -I experiments/stage02/run_stage02.py dmtcp-isolation-check > "$OUT/isolation.log" 2>&1
rc=$?; tail -8 "$OUT/isolation.log"
[ $rc -eq 0 ] || blocked "dmtcp-isolation-check exit $rc (no checkpoint attempted)"

if [ "$RUN_TRIAL" = 1 ]; then
  step "6. ONE controlled c-single checkpoint/restore trial (ADR 0004)"
  python3 -I experiments/stage02/run_stage02.py trial --backend dmtcp --class c-single \
    --adr-0004-controlled-trial > "$OUT/trial.log" 2>&1
  rc=$?; tail -20 "$OUT/trial.log"
  [ $rc -eq 0 ] || failed "trial exit $rc (see trial.log)"
else
  step "6. Trial NOT run (pass --run-trial to run it after reviewing steps 1-5)"
fi

step "7. Listener snapshot AFTER"
listeners > "$OUT/listeners-after.txt"; non_loopback "$OUT/listeners-after.txt" > "$OUT/nonloop-after.txt"
comm -13 "$OUT/nonloop-before.txt" "$OUT/nonloop-after.txt" > "$OUT/nonloop-new.txt"
if [ -s "$OUT/nonloop-new.txt" ]; then
  cat "$OUT/nonloop-new.txt"; failed "new non-loopback listener(s) after the run"
else
  echo "no new non-loopback listeners"
fi

step "8. Leftover DMTCP / workload processes of this user"
pgrep -u "$(id -u)" -a -f 'dmtcp_coordinator|dmtcp_launch|dmtcp_restart|mtcp_restart|napguard-stage02/runs/' \
  > "$OUT/leftover-processes.txt"
if [ -s "$OUT/leftover-processes.txt" ]; then
  cat "$OUT/leftover-processes.txt"; failed "experiment processes still running (reported, not killed)"
else
  echo "none"
fi

step "9. Evidence"
for d in $(ls -1dt "$HOME"/.local/share/napguard-stage02/runs/* 2>/dev/null | head -2); do
  echo "== $d" >> "$OUT/results.txt"
  python3 -I - "$d/result.json" >> "$OUT/results.txt" 2>&1 <<'PY'
import json, sys
result = json.load(open(sys.argv[1], encoding="utf-8"))
print(result["kind"], result["status"])
for check in result["checks"]:
    print(f"{check['status']:<7} {check['name']} - {check['detail']}")
PY
done
cat "$OUT/results.txt" 2>/dev/null

[ "$VERDICT" = PASS ] && finish 0 || finish 1
