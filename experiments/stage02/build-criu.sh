#!/usr/bin/env bash
# NapGuard Stage 02: build CRIU from a pinned upstream commit, as an ordinary user.
#
#   bash experiments/stage02/build-criu.sh
#
# - No sudo. Missing -dev packages are reported with the apt command for YOU to run.
# - Builds only the `criu` binary and installs it to
#   $NAPGUARD_STAGE02_PREFIX (default ~/.local/opt/napguard-stage02)/criu-<ver>/sbin/criu (mode 0750).
# - This script does NOT grant capabilities. Without CAP_CHECKPOINT_RESTORE (+ CAP_SYS_PTRACE
#   under Yama ptrace_scope=1) criu cannot checkpoint as non-root; the harness reports BLOCKED.
#   Granting them is a security-relevant system change that needs your explicit approval
#   (see docs/phases/STAGE-02-report.md, section I).
# - Expected resources (estimate, not yet measured on the VM): ~300 MB of -dev packages,
#   ~500 MB build tree, 3-10 minutes with 4 vCPUs.
set -euo pipefail

VERSION=4.2.1
TAG=v4.2.1
COMMIT=9539417f3e3cfa4eb84c319cd71f4d52f1f08645   # github.com/checkpoint-restore/criu tag v4.2.1
URL=https://github.com/checkpoint-restore/criu.git
SRC_ROOT=${NAPGUARD_STAGE02_SRC:-$HOME/.cache/napguard-stage02/src}
PREFIX_ROOT=${NAPGUARD_STAGE02_PREFIX:-$HOME/.local/opt/napguard-stage02}
SRC=$SRC_ROOT/criu-$VERSION
PREFIX=$PREFIX_ROOT/criu-$VERSION
LOGS=$PREFIX_ROOT/build-logs/criu-$VERSION-$(date -u +%Y%m%dT%H%M%SZ)

# Build dependencies per criu.org/Installation (Debian/Ubuntu names).
PACKAGES=(build-essential git pkg-config libprotobuf-dev libprotobuf-c-dev protobuf-c-compiler
          protobuf-compiler python3-protobuf libcap-dev libnl-3-dev libnet1-dev libaio-dev
          libgnutls28-dev uuid-dev libbsd-dev)

die() { echo "ERROR: $*" >&2; exit "${2:-1}"; }
step() { echo; echo "### $*"; }

[ "$(uname -s)" = Linux ] || die "run this inside Ubuntu, not on $(uname -s)"
[ "$(id -u)" -ne 0 ] || die "do not run as root"

step "Prerequisites"
missing=()
for pkg in "${PACKAGES[@]}"; do
  dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || missing+=("$pkg")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing packages: ${missing[*]}"
  echo "Install them yourself (needs sudo, your approval):"
  echo "  sudo apt install ${missing[*]}"
  exit 2
fi
free_kib=$(df -Pk "$HOME" | awk 'NR==2 {print $4}')
[ "$free_kib" -ge $((1024 * 1024)) ] || die "need at least 1 GiB free under \$HOME"
mkdir -p "$SRC_ROOT" "$LOGS"
{ uname -a; cat /etc/os-release; gcc --version | head -1; nproc; } | tee "$LOGS/host.txt"

step "Source (pinned $TAG = $COMMIT)"
if [ ! -d "$SRC/.git" ]; then
  git clone --branch "$TAG" --depth 1 "$URL" "$SRC" 2>&1 | tee "$LOGS/clone.log"
fi
actual=$(git -C "$SRC" rev-parse HEAD)
[ "$actual" = "$COMMIT" ] || die "checkout is $actual, expected $COMMIT; delete $SRC and retry"
[ -z "$(git -C "$SRC" status --porcelain --untracked-files=no)" ] || \
  die "tracked files in $SRC were modified; delete $SRC and retry"
echo "verified commit $actual"

step "Build the criu binary"
cd "$SRC"
if ! make -j"$(nproc)" criu 2>&1 | tee "$LOGS/make.log"; then
  echo "'make criu' failed; trying the default target (see $LOGS/make.log)"
  make -j"$(nproc)" 2>&1 | tee "$LOGS/make-all.log"
fi
[ -x criu/criu ] || die "criu/criu was not produced"

step "Install into $PREFIX (binary only)"
install -d -m 0750 "$PREFIX/sbin"
install -m 0750 criu/criu "$PREFIX/sbin/criu"
"$PREFIX/sbin/criu" --version | tee "$LOGS/version.txt"
{ echo "version=$VERSION"; echo "commit=$actual"; echo "binary=$PREFIX/sbin/criu"; echo "logs=$LOGS"; } \
  | tee "$PREFIX/napguard-build-info.txt"

step "Not done by this script (decision required)"
cat <<EOF
CRIU is built but has NO capabilities. As non-root it will be reported BLOCKED.
Options, each needing your explicit approval (see STAGE-02-report.md, section I):
  A) Grant file capabilities to this one binary (security-sensitive: CAP_SYS_PTRACE lets
     whoever runs it inspect other users' processes):
       sudo setcap cap_checkpoint_restore,cap_sys_ptrace+ep $PREFIX/sbin/criu
  B) Run a diagnostic trial as root through sudo (does not match NapGuard's user-level model):
       sudo -v && python3 experiments/stage02/run_stage02.py --criu-mode sudo trial --backend criu
EOF
