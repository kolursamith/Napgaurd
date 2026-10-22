#!/usr/bin/env bash
# NapGuard user-level installation (Stage 11). No root, no network, no system changes.
#
#   bash scripts/install.sh                 # check dependencies, install the commands
#   bash scripts/install.sh --with-desktop  # + application-grid launcher and icon
#   bash scripts/install.sh --with-systemd  # + systemd USER unit (refused mode; see below)
#   bash scripts/install.sh --check-only    # only report dependencies
#   options: --prefix DIR (default ~/.local)
#
# Installs small wrapper commands `napguard` and `napguard-dev` into PREFIX/bin that run
# NapGuard from THIS repository checkout. Every created file is listed in
# $XDG_DATA_HOME/napguard/install-manifest.txt so scripts/uninstall.sh removes exactly those.
# It never installs DMTCP or system packages; it tells you what is missing.
set -u

usage() { echo "usage: $0 [--prefix DIR] [--with-desktop] [--with-systemd] [--check-only]" >&2; exit 2; }
PREFIX="$HOME/.local"
DESKTOP=no
SYSTEMD=no
CHECK_ONLY=no
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) [ $# -ge 2 ] || usage; PREFIX=$2; shift ;;
    --with-desktop) DESKTOP=yes ;;
    --with-systemd) SYSTEMD=yes ;;
    --check-only) CHECK_ONLY=yes ;;
    *) usage ;;
  esac
  shift
done

REPO="$(cd "$(dirname "$0")/.." && pwd)" || exit 2
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
MANIFEST="$DATA_HOME/napguard/install-manifest.txt"
MARKER="Installed by NapGuard scripts/install.sh"
FAIL=0
ok() { echo "  OK    $1"; }
warn() { echo "  WARN  $1"; }
bad() { echo "  FAIL  $1"; FAIL=1; }

[ "$(id -u)" -ne 0 ] || { echo "do not run as root: NapGuard installs per user" >&2; exit 2; }

echo "Dependency check"
PY=$(command -v python3 || true)
if [ -n "$PY" ] && "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
  ok "Python $("$PY" -c 'import platform; print(platform.python_version())') ($PY)"
else
  bad "Python 3.12 or newer is required (Ubuntu 24.04 ships 3.12)"
fi
if [ -n "$PY" ] && "$PY" -c 'import tkinter' 2>/dev/null; then
  ok "Tkinter (GUI)"
else
  warn "Tkinter missing: the GUI needs it. Install it yourself with: sudo apt install python3-tk"
fi
command -v cc >/dev/null && ok "C compiler (builds the reference workload)" || \
  warn "no C compiler: install build-essential to launch demo workloads"
for tool in ss ip; do command -v "$tool" >/dev/null && ok "$tool (network audit)" || \
  warn "$tool missing: coordinator audits will refuse to run"; done
if [ -n "$PY" ] && PYTHONPATH="$REPO/src" "$PY" -s -m napguard.cli backend probe >/dev/null 2>&1; then
  ok "DMTCP 4.2.0 (pinned build) found"
else
  warn "DMTCP 4.2.0 not found: build it with experiments/stage02/build-dmtcp.sh (no root)"
fi
[ -n "${XDG_RUNTIME_DIR:-}" ] && ok "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR" || \
  warn "XDG_RUNTIME_DIR not set: start NapGuard from a desktop or systemd user session"
[ $FAIL -eq 0 ] || { echo "Required dependency missing; nothing installed."; exit 2; }
[ "$CHECK_ONLY" = no ] || exit 0

echo
echo "Installing into $PREFIX (user-level)"
mkdir -p "$PREFIX/bin" "$(dirname "$MANIFEST")"
chmod 700 "$(dirname "$MANIFEST")"
: > "$MANIFEST.new"
put() {  # put DEST MODE: content from stdin; refuses to overwrite a file it did not create
  local dest=$1 mode=$2
  if [ -e "$dest" ] && ! grep -q "$MARKER" "$dest" 2>/dev/null && [ "${dest##*.}" != svg ]; then
    echo "refusing to overwrite $dest (not created by NapGuard)" >&2; exit 1
  fi
  mkdir -p "$(dirname "$dest")"
  cat > "$dest.tmp" && chmod "$mode" "$dest.tmp" && mv "$dest.tmp" "$dest"
  echo "$dest" >> "$MANIFEST.new"
  echo "  wrote $dest"
}
for name in napguard napguard-dev; do
  module=$([ "$name" = napguard ] && echo napguard.main || echo napguard.cli)
  put "$PREFIX/bin/$name" 755 <<WRAPPER
#!/bin/sh
# $MARKER. Runs NapGuard from $REPO.
PYTHONPATH="$REPO/src\${PYTHONPATH:+:\$PYTHONPATH}"
export PYTHONPATH
exec "$PY" -s -m $module "\$@"
WRAPPER
done
if [ "$DESKTOP" = yes ]; then
  ICON="$DATA_HOME/icons/hicolor/scalable/apps/napguard.svg"
  put "$ICON" 644 < "$REPO/packaging/icons/napguard.svg"
  put "$DATA_HOME/applications/napguard.desktop" 644 <<DESKTOPFILE
[Desktop Entry]
# $MARKER
Type=Application
Name=NapGuard
GenericName=Process Checkpoint & Recovery
Comment=Checkpoint and restore supported demo processes with DMTCP (developer build)
Exec=$PREFIX/bin/napguard gui
Icon=$ICON
Terminal=false
Categories=Utility;System;Development;
DESKTOPFILE
fi
if [ "$SYSTEMD" = yes ]; then
  put "$CONFIG_HOME/systemd/user/napguard.service" 644 <<UNIT
# $MARKER
# Runs the daemon WITHOUT --i-accept-single-user-coordinator: status, history and
# verification work, but launch/checkpoint/restore are refused. The developer
# acknowledgement is per run and never stored (ADR 0006/0008); for a demo, start the
# daemon from the GUI banner or with: napguard daemon --i-accept-single-user-coordinator
[Unit]
Description=NapGuard daemon (refused mode)

[Service]
ExecStart=$PREFIX/bin/napguard daemon
Restart=no

[Install]
WantedBy=default.target
UNIT
  echo "  (not enabled; to use it: systemctl --user daemon-reload && systemctl --user start napguard)"
fi
touch "$MANIFEST"   # keep entries from earlier runs (e.g. a launcher installed before)
sort -u "$MANIFEST.new" "$MANIFEST" > "$MANIFEST.tmp" || exit 1
rm -f "$MANIFEST.new"
mv "$MANIFEST.tmp" "$MANIFEST"
echo
echo "Installed. Make sure $PREFIX/bin is on PATH, then run:  napguard gui"
