#!/usr/bin/env bash
# Remove what scripts/install.sh created (PKG-004). User data is KEPT unless --purge.
#
#   bash scripts/uninstall.sh            # remove commands, launcher, icon, unit
#   bash scripts/uninstall.sh --purge    # ALSO delete $XDG_DATA_HOME/napguard and
#                                        # $XDG_CONFIG_HOME/napguard (asks to confirm;
#                                        # --yes skips the question)
set -u
PURGE=no
YES=no
for arg in "$@"; do
  case "$arg" in --purge) PURGE=yes ;; --yes) YES=yes ;; *) echo "usage: $0 [--purge [--yes]]" >&2; exit 2 ;; esac
done
REPO="$(cd "$(dirname "$0")/.." && pwd)" || exit 2
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
MANIFEST="$DATA_HOME/napguard/install-manifest.txt"
MARKER="Installed by NapGuard scripts/install.sh"
[ "$(id -u)" -ne 0 ] || { echo "do not run as root" >&2; exit 2; }

if [ -f "$CONFIG_HOME/systemd/user/napguard.service" ] && command -v systemctl >/dev/null; then
  systemctl --user stop napguard 2>/dev/null
  systemctl --user disable napguard 2>/dev/null
fi
if [ -f "$MANIFEST" ]; then
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    if [ -L "$path" ]; then echo "  skip $path (symlink)"; continue; fi
    if [ -f "$path" ] && { grep -q "$MARKER" "$path" 2>/dev/null || [ "${path##*.}" = svg ]; }; then
      rm -f -- "$path" && echo "  removed $path"
    elif [ -e "$path" ]; then
      echo "  kept $path (not created by NapGuard)"
    fi
  done < "$MANIFEST"
  rm -f -- "$MANIFEST"
else
  echo "no install manifest at $MANIFEST: nothing to remove"
fi
command -v systemctl >/dev/null && systemctl --user daemon-reload 2>/dev/null

if [ "$PURGE" = yes ]; then
  if [ -S "${XDG_RUNTIME_DIR:-/nonexistent}/napguard/daemon.sock" ]; then
    echo "a NapGuard daemon is running: stop it first (Settings & Help > Stop Daemon & Exit)" >&2
    exit 1
  fi
  if [ "$YES" != yes ]; then
    printf 'Type PURGE to delete %s and %s permanently: ' "$DATA_HOME/napguard" "$CONFIG_HOME/napguard"
    read -r answer
    [ "$answer" = PURGE ] || { echo "not purged"; exit 1; }
  fi
  PYTHONPATH="$REPO/src" python3 -s - "$DATA_HOME/napguard" "$CONFIG_HOME/napguard" <<'PY'
import sys
from pathlib import Path
from napguard.daemon.history import safe_remove_tree
for target in map(Path, sys.argv[1:]):
    if target.name == "napguard" and target.is_dir() and not target.is_symlink():
        safe_remove_tree(target)  # never follows symlinks
        print(f"  purged {target}")
PY
fi
echo "Uninstalled. The repository checkout itself was not touched."
