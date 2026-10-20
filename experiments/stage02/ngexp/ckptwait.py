"""Deciding when a DMTCP 4.2.0 checkpoint is really complete.

Root cause of the first Ubuntu failure (commit a088cd9): `dmtcp_command --bcheckpoint`
does NOT block in DMTCP 4.2.0. dmtcp_command sends the blocking prefix as a separate
one-character command 'b' (dmtcp_command.cpp: `case 'b': connectAndSendUserCommand(cmdChar)`
then `connectAndSendUserCommand('c')`). The coordinator turns it into the string "b"
(`string cmd(1, hello_remote.coordCmd)`), but `handleUserCommand` only sets
`blockUntilDone` for "bc", so "b" is an "unhandled user command". The following 'c'
starts the checkpoint and is answered immediately, and dmtcp_command exits 0 as soon as
the checkpoint has *started*.

Completion evidence used instead (all from the pinned 4.2.0 source):

* each worker writes its image to `<image>.temp` (processinfo.h:114), fsyncs it, and
  renames it over `ckpt_*.dmtcp` (ckptserializer.cpp:400-411), so a final-name image is a
  whole image;
* the coordinator writes `dmtcp_restart_script_<compId>.sh` only after every worker has
  reported its image filename (dmtcp_coordinator.cpp:575-595), and the script names the
  coordinator port and the image files;
* the image must also stay unchanged between two polls, no temporary file may remain, and
  `dmtcp_command --status` must afterwards report the expected peers RUNNING.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_GLOB = "ckpt_*.dmtcp"
SCRIPT_GLOB = "dmtcp_restart_script_*.sh"


@dataclass(frozen=True)
class FileState:
    name: str
    size: int
    mtime_ns: int


@dataclass
class Evidence:
    images: list[FileState] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    temp_files: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    complete: bool = False
    reason: str = "not inspected"

    def snapshot(self) -> tuple[FileState, ...]:
        return tuple(sorted(self.images, key=lambda f: f.name))


def checkpoint_evidence(ckpt_dir: Path, port: int, since_ns: int, expected_images: int) -> Evidence:
    """Inspect `ckpt_dir` for a complete checkpoint of THIS experiment taken after `since_ns`.

    Only regular files are considered. Files older than `since_ns` (wall clock, ns) are
    reported as stale and never counted.
    """
    ev = Evidence()
    if not ckpt_dir.is_dir():
        ev.reason = f"checkpoint directory {ckpt_dir} missing"
        return ev
    for path in sorted(ckpt_dir.iterdir()):
        if path.is_symlink() or not path.is_file():
            continue
        st = path.stat()
        # DMTCP writes `<image>.temp` and renames it when complete (processinfo.h:114).
        if path.name.endswith((".temp", ".tmp")):
            ev.temp_files.append(path.name)
            continue
        if st.st_mtime_ns < since_ns:
            ev.stale.append(path.name)
            continue
        if path.match(IMAGE_GLOB):
            ev.images.append(FileState(path.name, st.st_size, st.st_mtime_ns))
        elif path.match(SCRIPT_GLOB):
            ev.scripts.append(path.name)
    if ev.temp_files:
        ev.reason = f"temporary checkpoint file(s) still present: {ev.temp_files}"
    elif not ev.images:
        ev.reason = "no ckpt_*.dmtcp image written yet"
    elif any(img.size == 0 for img in ev.images):
        ev.reason = "an image has size 0"
    elif len(ev.images) != expected_images:
        ev.reason = f"expected {expected_images} image(s), found {len(ev.images)}"
    elif not ev.scripts:
        ev.reason = "coordinator has not written the restart script (checkpoint not complete)"
    else:
        script_text = "\n".join((ckpt_dir / s).read_text(encoding="utf-8", errors="replace")
                                for s in ev.scripts)
        missing = [img.name for img in ev.images if img.name not in script_text]
        if missing:
            ev.reason = f"restart script does not reference image(s) {missing}"
        elif str(port) not in script_text:
            ev.reason = f"restart script does not reference this run's coordinator port {port}"
        else:
            ev.complete = True
            ev.reason = "restart script written; images referenced; no temporary files"
    return ev


@dataclass
class WaitResult:
    ok: bool
    reason: str
    evidence: Evidence
    waited_s: float
    polls: int


def wait_for_checkpoint(
    ckpt_dir: Path,
    port: int,
    since_ns: int,
    expected_images: int,
    *,
    timeout: float,
    poll: float = 0.25,
    stable_for: float = 0.5,
    liveness: Callable[[], str | None] = lambda: None,
    audit: Callable[[], None] = lambda: None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> WaitResult:
    """Poll until the checkpoint is complete AND unchanged for `stable_for` seconds.

    Every poll first runs `audit()` (listener audit; it raises to abort) and `liveness()`
    (returns a reason if the workload or coordinator died). Bounded by `timeout`.
    """
    started = clock()
    polls = 0
    stable_since: float | None = None
    last: tuple[FileState, ...] | None = None
    ev = Evidence()
    while True:
        polls += 1
        audit()
        dead = liveness()
        if dead is not None:
            return WaitResult(False, dead, ev, clock() - started, polls)
        ev = checkpoint_evidence(ckpt_dir, port, since_ns, expected_images)
        now = clock()
        if ev.complete:
            snap = ev.snapshot()
            if snap != last:
                last, stable_since = snap, now
            elif stable_since is not None and now - stable_since >= stable_for:
                return WaitResult(True, ev.reason, ev, now - started, polls)
        else:
            last, stable_since = None, None
        if now - started >= timeout:
            return WaitResult(False, f"timed out after {timeout:.0f}s: {ev.reason}", ev,
                              now - started, polls)
        sleep(poll)


def parse_dmtcp_status(text: str) -> dict[str, str]:
    """KEY=VALUE lines of `dmtcp_command --status` (e.g. NUM_PEERS=1, RUNNING=yes)."""
    found = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.isupper():
            found[key] = value.strip()
    return found
