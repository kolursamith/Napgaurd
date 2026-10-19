#!/usr/bin/env bash
# NapGuard Stage 00 (formerly Layer 0) — Ubuntu baseline verification.
#
# Run INSIDE Ubuntu (UTM console or desktop terminal) as an ordinary user,
# from a clone of the repository:
#
#   bash scripts/layer0-ubuntu-check.sh <EXPECTED_COMMIT_SHA>
#
# EXPECTED_COMMIT_SHA is the `git rev-parse HEAD` value from the Mac. Without
# it the commit-identity check is SKIPPED and the verdict cannot be PASS.
#
# Safety:
#   - No sudo, no package installation, no system configuration changes, no SSH.
#   - Does not install DMTCP or CRIU.
#   - The Python virtual environment, pip cache, and mypy cache live in a
#     temporary directory that is deleted on exit.
#   - The persistent-storage probe is a temporary directory under $HOME that is
#     deleted immediately after the probe.
#   - pytest/Ruff/mypy caches and bytecode are not written into the checkout;
#     the final check verifies the checkout is unchanged.
#   - pip downloads the declared build/dev dependencies from PyPI.
#
# Every check reports PASS, FAIL, or SKIP with a reason, and is MANDATORY or
# OPTIONAL. Exit status: 0 = PASS, 1 = a mandatory check FAILED,
# 2 = no mandatory failure but a mandatory check was SKIPPED (BLOCKED).
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)" || exit 1
cd "$REPO" || exit 1
EXPECTED="${1:-}"

MFAIL=0
MSKIP=0
SUMMARY=""

result() { # result <mandatory|optional> <PASS|FAIL|SKIP> <name> [reason]
  local line
  line="$(printf '%-4s  %-9s  %s' "$2" "$1" "$3")${4:+ — $4}"
  echo ">>> $line"
  echo
  SUMMARY="${SUMMARY}${line}"$'\n'
  if [ "$1" = mandatory ]; then
    [ "$2" = FAIL ] && MFAIL=$((MFAIL + 1))
    [ "$2" = SKIP ] && MSKIP=$((MSKIP + 1))
  fi
  return 0
}

run() { # run <command> [args...]: show the command, its output, its exit code
  echo "\$ $*"
  "$@" 2>&1
  local rc=$?
  echo "[exit $rc]"
  return $rc
}

finish() {
  echo "### Summary"
  printf '%s' "$SUMMARY"
  echo
  if [ "$MFAIL" -gt 0 ]; then
    echo "STAGE 00 UBUNTU VERDICT: FAIL ($MFAIL mandatory check(s) failed, $MSKIP skipped)"
    exit 1
  elif [ "$MSKIP" -gt 0 ]; then
    echo "STAGE 00 UBUNTU VERDICT: BLOCKED ($MSKIP mandatory check(s) skipped)"
    exit 2
  fi
  echo "STAGE 00 UBUNTU VERDICT: PASS (all mandatory checks passed)"
  exit 0
}

TMP_ROOT=""
PROBE_DIR=""
cleanup() {
  [ -n "$TMP_ROOT" ] && rm -rf -- "$TMP_ROOT"
  [ -n "$PROBE_DIR" ] && rm -rf -- "$PROBE_DIR"
}
trap cleanup EXIT

export PYTHONDONTWRITEBYTECODE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1

echo "### NapGuard Stage 00 Ubuntu baseline verification"
run date -Is
echo "repository: $REPO"
echo "expected commit: ${EXPECTED:-<not supplied>}"
echo

echo "### 1. Operating system"
if [ "$(uname -s)" != Linux ]; then
  result mandatory FAIL "Linux kernel" "uname -s reports $(uname -s); this script must run inside Ubuntu"
  finish
fi
run uname -a
result mandatory PASS "Linux kernel" "$(uname -r)"

if [ "$(id -u)" -eq 0 ]; then
  result mandatory FAIL "Ordinary (non-root) user" "running as root; rerun as your normal user"
else
  result mandatory PASS "Ordinary (non-root) user" "$(id -un), uid $(id -u)"
fi

if [ -r /etc/os-release ]; then
  run cat /etc/os-release
  os_id="$(. /etc/os-release && echo "${ID:-}")"
  os_ver="$(. /etc/os-release && echo "${VERSION_ID:-}")"
  os_name="$(. /etc/os-release && echo "${PRETTY_NAME:-}")"
  if [ "$os_id" = ubuntu ]; then
    result mandatory PASS "Ubuntu release recorded" "$os_name"
  else
    result mandatory FAIL "Ubuntu release recorded" "ID=$os_id, not ubuntu"
  fi
  if [ "$os_ver" = 24.04 ]; then
    result optional PASS "Ubuntu 24.04 (project target)" "VERSION_ID=$os_ver"
  else
    result optional FAIL "Ubuntu 24.04 (project target)" "VERSION_ID=$os_ver differs from the documented target"
  fi
else
  result mandatory FAIL "Ubuntu release recorded" "/etc/os-release is missing or unreadable"
fi

arch="$(uname -m)"
run dpkg --print-architecture
if [ -n "$arch" ]; then
  result mandatory PASS "CPU architecture recorded" "uname -m = $arch"
else
  result mandatory FAIL "CPU architecture recorded" "uname -m returned nothing"
fi
if [ "$arch" = aarch64 ]; then
  result optional PASS "ARM64 (first-release target)" "$arch"
else
  result optional FAIL "ARM64 (first-release target)" "$arch must be supported separately"
fi
run nproc
run free -h

echo "### 2. Git and commit identity"
if run git --version; then
  result mandatory PASS "Git available" "$(git --version)"
else
  result mandatory FAIL "Git available" "git not found (install it yourself: sudo apt install git)"
fi

head_sha=""
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  result mandatory PASS "Repository is a Git checkout" "$REPO"
  if head_sha="$(git rev-parse --verify HEAD 2>/dev/null)"; then
    run git log -1 --format='%H %an %ad %s'
    run git branch --show-current
    result mandatory PASS "HEAD commit recorded" "$head_sha"
  else
    head_sha=""
    result mandatory FAIL "HEAD commit recorded" "the checkout has no commits"
  fi
else
  result mandatory FAIL "Repository is a Git checkout" "$REPO is not inside a Git work tree"
fi

if [ -z "$head_sha" ]; then
  result mandatory SKIP "Commit matches Mac revision" "no HEAD commit to compare"
elif [ -z "$EXPECTED" ]; then
  result mandatory SKIP "Commit matches Mac revision" "no expected SHA supplied; rerun with the Mac's 'git rev-parse HEAD'"
else
  expected_full="$(git rev-parse --verify --quiet "${EXPECTED}^{commit}" || true)"
  if [ -z "$expected_full" ]; then
    result mandatory FAIL "Commit matches Mac revision" "$EXPECTED is not a commit in this clone (did you fetch?)"
  elif [ "$expected_full" = "$head_sha" ]; then
    result mandatory PASS "Commit matches Mac revision" "$head_sha"
  else
    result mandatory FAIL "Commit matches Mac revision" "HEAD $head_sha != expected $expected_full"
  fi
fi

dirty="$(git status --porcelain 2>/dev/null)"
if [ -n "$head_sha" ] && [ -z "$dirty" ]; then
  result mandatory PASS "Working tree clean" "tests run against the exact commit"
elif [ -n "$head_sha" ]; then
  run git status --short
  result mandatory FAIL "Working tree clean" "local modifications mean the tested code is not the commit"
else
  result mandatory SKIP "Working tree clean" "no commit to compare against"
fi
before_state="$(git status --porcelain --ignored 2>/dev/null)"

echo "### 3. Python"
if run python3 --version; then
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
    result mandatory PASS "Python >= 3.12" "$(python3 --version 2>&1)"
  else
    result mandatory FAIL "Python >= 3.12" "$(python3 --version 2>&1) is older than pyproject's requires-python"
  fi
else
  result mandatory FAIL "Python >= 3.12" "python3 not found"
fi

echo "### 4. systemd and D-Bus"
pid1="$(cat /proc/1/comm 2>/dev/null || true)"
run systemctl --version
if [ "$pid1" = systemd ]; then
  result mandatory PASS "systemd is PID 1" "/proc/1/comm = $pid1"
else
  result mandatory FAIL "systemd is PID 1" "/proc/1/comm = ${pid1:-<unreadable>}"
fi
state="$(systemctl is-system-running 2>/dev/null || true)"
if [ "$state" = running ]; then
  result optional PASS "systemd system state" "$state"
else
  run systemctl --failed --no-legend
  result optional FAIL "systemd system state" "${state:-<unknown>} (see failed units above)"
fi

run ls -l /run/dbus/system_bus_socket
if [ -S /run/dbus/system_bus_socket ] &&
  run busctl --system call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus GetId; then
  result mandatory PASS "System D-Bus responds" "org.freedesktop.DBus.GetId succeeded"
else
  result mandatory FAIL "System D-Bus responds" "system bus socket missing or the bus did not answer"
fi

if run systemctl --user is-system-running; then
  result optional PASS "systemd user manager" "needed later for the user-level daemon"
elif systemctl --user show-environment >/dev/null 2>&1; then
  result optional PASS "systemd user manager" "reachable (state not 'running')"
else
  result optional FAIL "systemd user manager" "not reachable from this session; needed in later stages"
fi

if run busctl --system call org.freedesktop.login1 /org/freedesktop/login1 \
  org.freedesktop.login1.Manager CanSuspend; then
  result optional PASS "systemd-logind reachable" "needed later for sleep integration"
else
  result optional FAIL "systemd-logind reachable" "CanSuspend call failed; needed in later stages"
fi

echo "### 5. Persistent storage"
if PROBE_DIR="$(mktemp -d "$HOME/.napguard-stage00-probe.XXXXXX" 2>/dev/null)"; then
  fstype="$(findmnt -n -o FSTYPE --target "$PROBE_DIR" 2>/dev/null || true)"
  run findmnt --target "$PROBE_DIR"
  run df -h "$PROBE_DIR"
  probe="$PROBE_DIR/write-test"
  if [ "$fstype" = tmpfs ] || [ "$fstype" = ramfs ]; then
    result mandatory FAIL "Writable persistent storage" "\$HOME is on $fstype, which does not survive reboot"
  elif echo napguard-ok >"$probe" && sync "$probe" && [ "$(cat "$probe")" = napguard-ok ]; then
    result mandatory PASS "Writable persistent storage" "\$HOME on ${fstype:-unknown fs}: write, sync, read-back ok"
  else
    result mandatory FAIL "Writable persistent storage" "write/sync/read-back failed under \$HOME"
  fi
  rm -rf -- "$PROBE_DIR" && PROBE_DIR=""
else
  result mandatory FAIL "Writable persistent storage" "cannot create a directory under \$HOME"
fi

echo "### 6. Linux virtual environment and package install"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/napguard-stage00.XXXXXX")" || exit 1
export PIP_CACHE_DIR="$TMP_ROOT/pip-cache"
VENV="$TMP_ROOT/venv"
venv_ok=0
install_ok=0
if run python3 -m venv "$VENV"; then
  venv_ok=1
  run "$VENV/bin/python" --version
  result mandatory PASS "Python virtual environment created" "$VENV (temporary)"
else
  result mandatory FAIL "Python virtual environment created" \
    "python3 -m venv failed; on Ubuntu install python3-venv yourself (sudo apt install python3-venv)"
fi

if [ "$venv_ok" -eq 1 ]; then
  if run "$VENV/bin/python" -m pip install "$REPO[dev]"; then
    install_ok=1
    run "$VENV/bin/python" -m pip list --format=freeze
    result mandatory PASS "Project installs from pyproject.toml" "pip install '.[dev]' into the temporary venv"
  else
    result mandatory FAIL "Project installs from pyproject.toml" "pip install failed (see output; network access to PyPI is required)"
  fi
else
  result mandatory SKIP "Project installs from pyproject.toml" "prerequisite failed: virtual environment"
fi

echo "### 7. Import, tests, and static checks"
if [ "$install_ok" -eq 1 ]; then
  import_cmd='import napguard; print(napguard.__version__); print(napguard.__file__)'
  echo "\$ $VENV/bin/python -c '$import_cmd'"
  if out="$("$VENV/bin/python" -c "$import_cmd" 2>&1)"; then
    echo "$out"
    case "$(printf '%s\n' "$out" | tail -n 1)" in
      "$VENV"/*) result mandatory PASS "Package imports" "installed napguard imported from the venv" ;;
      *) result mandatory FAIL "Package imports" "napguard was not imported from the installed venv copy" ;;
    esac
  else
    echo "$out"
    result mandatory FAIL "Package imports" "import napguard failed"
  fi

  if run "$VENV/bin/python" -m pytest -p no:cacheprovider; then
    result mandatory PASS "pytest suite"
  else
    result mandatory FAIL "pytest suite" "see pytest output above"
  fi

  run "$VENV/bin/ruff" --version
  if run "$VENV/bin/ruff" check --no-cache .; then
    result mandatory PASS "Ruff"
  else
    result mandatory FAIL "Ruff" "see Ruff output above"
  fi

  run "$VENV/bin/mypy" --version
  if run "$VENV/bin/mypy" --cache-dir "$TMP_ROOT/mypy-cache" src tests; then
    result mandatory PASS "mypy"
  else
    result mandatory FAIL "mypy" "see mypy output above"
  fi
else
  for check in "Package imports" "pytest suite" "Ruff" "mypy"; do
    result mandatory SKIP "$check" "prerequisite failed: project install"
  done
fi

echo "### 8. Repository structure"
missing=""
for p in README.md AGENTS.md pyproject.toml .gitignore src/napguard/__init__.py \
  src/napguard/py.typed tests/unit/test_import.py tests/integration tests/system \
  docs/architecture/overview.md docs/decisions/0001-technology-stack.md \
  docs/phases/PHASE-0-plan.md docs/phases/PHASE-0-report.md \
  docs/phases/STAGE-00-report.md docs/test-reports/layer-0.md \
  packaging/systemd scripts experiments; do
  [ -e "$p" ] || missing="$missing $p"
done
if [ -z "$missing" ]; then
  result mandatory PASS "Expected repository paths present"
else
  result mandatory FAIL "Expected repository paths present" "missing:$missing"
fi

echo "### 9. Later-stage facilities (inspection only; nothing is installed)"
run ls /sys/class/power_supply/
run cat /sys/power/state
for tool in dmtcp_launch criu; do
  if path="$(command -v "$tool")"; then
    result optional PASS "$tool present" "$path (not used in Stage 00)"
  else
    result optional SKIP "$tool present" "not installed; backend installation is out of scope for Stage 00"
  fi
done

echo "### 10. Checkout left unchanged"
after_state="$(git status --porcelain --ignored 2>/dev/null)"
if [ "$before_state" = "$after_state" ]; then
  result mandatory PASS "Checkout unchanged by this script"
else
  diff <(printf '%s\n' "$before_state") <(printf '%s\n' "$after_state")
  result mandatory FAIL "Checkout unchanged by this script" "the script created or modified files in the checkout"
fi

run date -Is
finish
