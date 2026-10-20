#!/usr/bin/env bash
# NapGuard Stage 02: Ubuntu verification for the ADR 0004 controlled DMTCP trial.
#
#   bash experiments/stage02/ubuntu-controlled-trial.sh <EXPECTED_SHA>              # prerequisites only
#   bash experiments/stage02/ubuntu-controlled-trial.sh <EXPECTED_SHA> --run-trial  # + ONE c-single trial
#   bash experiments/stage02/ubuntu-controlled-trial.sh <EXPECTED_SHA> --reboot-prepare
#       (then YOU power the VM off and on; nothing here reboots)
#   bash experiments/stage02/ubuntu-controlled-trial.sh <EXPECTED_SHA> --reboot-resume
#   ... --run-trial --class <c-single|c-threads|py|c-file-static|c-file-grow>
#       (non-c-single classes run under ADR 0004 Amendment 2, approved 2026-10-20 for one
#        same-boot trial each; reboot modes are c-single only; c-child/tcp/gui are refused)
#
# Order (stops at the first failure; the trial runs only after every prerequisite passes):
#   1. HEAD == EXPECTED_SHA, clean working tree
#   2. Stage 00 baseline (venv install, pytest, Ruff, mypy)
#   3. Linux-only Stage 02 security tests: all must run (zero skipped) and pass
#   4. Listener snapshot BEFORE
#   5. Fresh dmtcp-isolation-check (same boot)
#   6. only with a mode flag, ONE of (all --adr-0004-controlled-trial, dmtcp, c-single):
#        --run-trial       same-boot trial
#        --reboot-prepare  launch, verified checkpoint, terminate, stop coordinators, manifest
#        --reboot-resume   (new boot) verify boot id/artifacts/engine, fresh isolation, restore
#   7. Listener snapshot AFTER: no new non-loopback listener
#   8. No DMTCP/workload process of this user left
#   9. Evidence bundle (checkpoint images excluded)
# No sudo, no installs, no system changes. Exit: 0 PASS, 1 FAIL, 2 BLOCKED.
set -u

usage() {
  echo "usage: $0 <EXPECTED_SHA (40 hex)> [--run-trial | --reboot-prepare | --reboot-resume]" \
       "[--class c-single|c-threads|py|c-file-static|c-file-grow]" >&2
  exit 2
}
EXPECTED=${1:-}
[[ "$EXPECTED" =~ ^[0-9a-f]{40}$ ]] || usage
MODE=none
CLASS=c-single
shift
while [ $# -gt 0 ]; do
  case "$1" in
    --run-trial) [ "$MODE" = none ] || usage; MODE=trial ;;
    --reboot-prepare) [ "$MODE" = none ] || usage; MODE=reboot-prepare ;;
    --reboot-resume) [ "$MODE" = none ] || usage; MODE=reboot-resume ;;
    --class)
      [ $# -ge 2 ] || usage
      case "$2" in  # strict allowlist; anything else (incl. c-child, tcp, gui) is refused
        c-single|c-threads|py|c-file-static|c-file-grow) CLASS=$2 ;;
        *) usage ;;
      esac
      shift ;;
    *) usage ;;
  esac
  shift
done
case "$MODE" in reboot-*) [ "$CLASS" = c-single ] || usage ;; esac

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
{ date -Is; uname -a; grep PRETTY_NAME /etc/os-release; python3 --version
  echo "boot_id=$(cat /proc/sys/kernel/random/boot_id) mode=$MODE class=$CLASS"; } | tee "$OUT/host.txt"

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
  tests/unit/test_stage02_safety.py tests/unit/test_stage02_launch_config.py \
  tests/unit/test_stage02_classes.py tests/unit/test_stage02_reboot.py \
  tests/unit/test_stage02_ckptwait.py > "$OUT/security-tests.log" 2>&1
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

case "$MODE" in
  trial)
    step "6. ONE controlled $CLASS checkpoint/restore trial (ADR 0004)"
    python3 -I experiments/stage02/run_stage02.py trial --backend dmtcp --class "$CLASS" \
      --adr-0004-controlled-trial > "$OUT/trial.log" 2>&1
    rc=$?; tail -20 "$OUT/trial.log"
    [ $rc -ne 2 ] || blocked "harness refused $CLASS (BLOCKED; e.g. ADR 0004 Amendment 2 not approved)"
    [ $rc -eq 0 ] || failed "trial exit $rc (see trial.log)" ;;
  reboot-prepare)
    step "6. Cross-reboot PREPARE: checkpoint, terminate, stop coordinators, write manifest"
    python3 -I experiments/stage02/run_stage02.py reboot-prepare --backend dmtcp --class c-single \
      --adr-0004-controlled-trial > "$OUT/trial.log" 2>&1
    rc=$?; tail -20 "$OUT/trial.log"
    [ $rc -eq 0 ] || failed "reboot-prepare exit $rc (see trial.log); do NOT reboot for this test" ;;
  reboot-resume)
    step "6. Cross-reboot RESUME (new boot): verify, fresh isolation, restore, continuity"
    python3 -I experiments/stage02/run_stage02.py reboot-resume --backend dmtcp --class c-single \
      --adr-0004-controlled-trial > "$OUT/trial.log" 2>&1
    rc=$?; tail -25 "$OUT/trial.log"
    [ $rc -eq 0 ] || failed "reboot-resume exit $rc (see trial.log)" ;;
  *)
    step "6. Nothing run (pass --run-trial, --reboot-prepare or --reboot-resume after reviewing 1-5)" ;;
esac

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
