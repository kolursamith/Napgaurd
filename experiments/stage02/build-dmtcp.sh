#!/usr/bin/env bash
# NapGuard Stage 02: build DMTCP from a pinned upstream commit, as an ordinary user.
#
#   bash experiments/stage02/build-dmtcp.sh            # build + install (starts no DMTCP process)
#   NAPGUARD_STAGE02_BUILD_JOBS=3 bash experiments/stage02/build-dmtcp.sh   # parallel make jobs
#   bash experiments/stage02/build-dmtcp.sh --check-config   # validate settings, print them, exit
#
# DMTCP's own test suite (`make check`) is deliberately NOT supported here: it starts DMTCP
# coordinators, which listen on all network interfaces without authentication (finding I-1).
#
# - No sudo. Missing compilers are reported with the apt command for YOU to run.
# - Source:  $NAPGUARD_STAGE02_SRC (default ~/.cache/napguard-stage02/src)/dmtcp-<ver>
# - Install: $NAPGUARD_STAGE02_PREFIX (default ~/.local/opt/napguard-stage02)/dmtcp-<ver>
# - The clone must match the pinned commit exactly, or the build stops.
# - Build jobs: $NAPGUARD_STAGE02_BUILD_JOBS, a positive integer, default 2 (not nproc), so that
#   parallel C++ compiles fit a small VM (e.g. 3.8 GiB RAM); invalid values are rejected.
# - Expected resources (estimate, not yet measured on the VM): ~1 GiB free disk,
#   roughly 10-30 minutes with 2 build jobs.
set -euo pipefail

VERSION=4.2.0
TAG=v4.2.0
COMMIT=f8009ce7b4ad211311ca2f72a929b975e4aa1155   # github.com/dmtcp/dmtcp tag v4.2.0
URL=https://github.com/dmtcp/dmtcp.git
SRC_ROOT=${NAPGUARD_STAGE02_SRC:-$HOME/.cache/napguard-stage02/src}
PREFIX_ROOT=${NAPGUARD_STAGE02_PREFIX:-$HOME/.local/opt/napguard-stage02}
SRC=$SRC_ROOT/dmtcp-$VERSION
PREFIX=$PREFIX_ROOT/dmtcp-$VERSION
LOGS=$PREFIX_ROOT/build-logs/dmtcp-$VERSION-$(date -u +%Y%m%dT%H%M%SZ)
JOBS=${NAPGUARD_STAGE02_BUILD_JOBS:-2}

die() { echo "ERROR: $1" >&2; exit "${2:-1}"; }  # die MESSAGE [EXIT_CODE]
step() { echo; echo "### $*"; }

# Validated before anything else runs (no clone, no build, no host changes on rejection).
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || \
  die "NAPGUARD_STAGE02_BUILD_JOBS must be a positive integer, got '$JOBS'" 2
case "${1:-}" in
  "") ;;
  --check-config)
    echo "version=$VERSION commit=$COMMIT jobs=$JOBS src=$SRC prefix=$PREFIX"
    exit 0 ;;
  *) die "unknown argument '$1' (only --check-config is accepted)" 2 ;;
esac

[ "$(uname -s)" = Linux ] || die "run this inside Ubuntu, not on $(uname -s)"
[ "$(id -u)" -ne 0 ] || die "do not run as root; DMTCP builds and runs as an ordinary user"
[ "${RUN_MAKE_CHECK:-0}" = 0 ] || \
  die "RUN_MAKE_CHECK is refused: make check starts network-exposed DMTCP coordinators (I-1, OD-13)"

step "Prerequisites"
missing=()
for tool in git gcc g++ make python3; do command -v "$tool" >/dev/null || missing+=("$tool"); done
if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing: ${missing[*]}"
  echo "Install them yourself (needs sudo, your approval):"
  echo "  sudo apt install git gcc g++ make python3"
  exit 2
fi
free_kib=$(df -Pk "$HOME" | awk 'NR==2 {print $4}')
[ "$free_kib" -ge $((1024 * 1024)) ] || die "need at least 1 GiB free under \$HOME"
mkdir -p "$SRC_ROOT" "$LOGS"
{ uname -a; cat /etc/os-release; gcc --version | head -1; g++ --version | head -1; nproc
  echo "build_jobs=$JOBS"; } \
  | tee "$LOGS/host.txt"

step "Source (pinned $TAG = $COMMIT)"
if [ ! -d "$SRC/.git" ]; then
  git clone --branch "$TAG" --depth 1 "$URL" "$SRC" 2>&1 | tee "$LOGS/clone.log"
fi
actual=$(git -C "$SRC" rev-parse HEAD)
[ "$actual" = "$COMMIT" ] || die "checkout is $actual, expected $COMMIT; delete $SRC and retry"
[ -z "$(git -C "$SRC" status --porcelain --untracked-files=no)" ] || \
  die "tracked files in $SRC were modified; delete $SRC and retry"
echo "verified commit $actual"

step "Configure, build, install into $PREFIX"
cd "$SRC"
./configure --prefix="$PREFIX" 2>&1 | tee "$LOGS/configure.log"
make -j"$JOBS" 2>&1 | tee "$LOGS/make.log"
make install 2>&1 | tee "$LOGS/install.log"

step "Result"
"$PREFIX/bin/dmtcp_launch" --version | tee "$LOGS/version.txt"
{ echo "version=$VERSION"; echo "commit=$actual"; echo "prefix=$PREFIX"; echo "logs=$LOGS"
  echo "build_jobs=$JOBS"; } \
  | tee "$PREFIX/napguard-build-info.txt"
echo
echo "DMTCP installed. Building it starts no DMTCP process. The Stage 02 harness finds it under"
echo "$PREFIX_ROOT, but refuses to start a coordinator until OD-13 is resolved (finding I-1)."
