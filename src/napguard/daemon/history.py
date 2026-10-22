"""History management: archive, preview, delete, and reset demo state safely.

Policy (documented in docs/USER_GUIDE.md):

* **Archive** hides a workload (or a checkpoint) from the default lists. Nothing is
  deleted; it can be un-archived, and a PUBLISHED archived checkpoint stays restorable.
* **Delete history** permanently removes a finished workload: its catalog rows (instances,
  checkpoints, operations) and its own directory `<data>/workloads/<id>/` (reports,
  binaries, session logs, checkpoint images). It needs an explicit confirmation.
* **Reset demo** deletes the history of every finished workload, restores the default
  settings, and clears a battery simulation. It refuses while anything is live or busy.
* **Audit records are kept**: the activity log is never deleted; every cleanup adds an
  event naming what was removed.

Safety rules:

* never touch a workload with a live instance, or one with a queued or running operation;
* never delete outside `<data>/workloads/<wl-id>/`; the directory must be a real
  directory of this user (not a symlink), and its name must be a NapGuard workload ID;
* files are removed with `os.fwalk(..., follow_symlinks=False)` and dir-fd-relative
  unlink and rmdir, so a symlink inside is removed, never followed;
* order: checkpoints are first marked MISSING in one transaction (so a half-deleted
  checkpoint can never look restorable), then files are removed, then rows are deleted
  in one transaction. A file-system failure leaves the rows, with their checkpoints
  MISSING, so the deletion can be retried.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

from napguard.checkpoint import artifacts
from napguard.checkpoint.manifest import MANIFEST_NAME, read_manifest
from napguard.storage.catalog import DEFAULT_SETTINGS, Catalog, utc_now

WORKLOAD_ID = re.compile(r"^wl-[0-9a-f]{12}$")
LIVE_STATES = ("STARTING", "RUNNING")


class HistoryError(Exception):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}


def _tree_size(path: Path) -> tuple[int, int]:
    files = size = 0
    for _root, _dirs, names, dfd in os.fwalk(path, follow_symlinks=False):
        for name in names:
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                continue
            files += 1
            size += st.st_size if stat.S_ISREG(st.st_mode) else 0
    return files, size


def safe_remove_tree(path: Path) -> None:
    """Remove a directory tree without ever following a symlink."""
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise HistoryError("INSECURE_PATH", f"{path} is not a directory owned by this user")
    for _root, dirs, names, dfd in os.fwalk(path, topdown=False, follow_symlinks=False):
        for name in names:
            os.unlink(name, dir_fd=dfd)
        for name in dirs:
            if stat.S_ISLNK(os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mode):
                os.unlink(name, dir_fd=dfd)  # a symlink to a directory: removed, not followed
            else:
                os.rmdir(name, dir_fd=dfd)
    os.rmdir(path)


class History:
    def __init__(self, catalog: Catalog, data_dir: Path, is_live: Any) -> None:
        self.catalog = catalog
        self.workloads_dir = data_dir / "workloads"
        self.is_live = is_live  # callable(workload_id) -> bool, from the manager

    # ---------------------------------------------------------- eligibility

    def blocked_reason(self, workload_id: str) -> str | None:
        row = self.catalog.one("SELECT id FROM workloads WHERE id=?", (workload_id,))
        if row is None:
            return "unknown workload"
        if self.is_live(workload_id):
            return "it has a live instance: stop it first"
        live = self.catalog.one("SELECT id FROM instances WHERE workload_id=? AND state IN "
                                "('STARTING','RUNNING')", (workload_id,))
        if live is not None:
            return "an instance is still recorded as starting or running"
        # Process and checkpoint operations block cleanup. Cleanup operations themselves run on
        # the same single worker, so they never overlap (and must not block themselves).
        busy = self.catalog.one(
            "SELECT kind FROM operations WHERE state IN ('QUEUED','RUNNING') AND kind IN "
            "('launch','stop','checkpoint','verify','restore') AND (workload_id=? OR "
            "checkpoint_id IN (SELECT id FROM checkpoints WHERE workload_id=?))",
            (workload_id, workload_id))
        if busy is not None:
            return f"a {busy['kind']} operation is queued or running for it"
        return None

    def _directory(self, workload_id: str) -> Path | None:
        if not WORKLOAD_ID.match(workload_id):
            raise HistoryError("INVALID_ARGUMENT", f"{workload_id!r} is not a workload ID")
        path = self.workloads_dir / workload_id
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise HistoryError("INSECURE_PATH", f"{path} is not a directory owned by this user")
        return path

    # -------------------------------------------------------------- archive

    def archive(self, workload_id: str, archived: bool = True) -> dict[str, Any]:
        reason = self.blocked_reason(workload_id)
        if archived and reason is not None:
            raise HistoryError("CONFLICT", f"cannot archive {workload_id}: {reason}")
        with self.catalog.tx() as db:
            db.execute("UPDATE workloads SET archived_utc=? WHERE id=?",
                       (utc_now() if archived else None, workload_id))
        verb = "archived" if archived else "unarchived"
        self.catalog.event(f"history.{verb}", f"{verb} workload {workload_id}",
                           workload_id=workload_id)
        return {"workload_id": workload_id, "archived": archived}

    def archive_checkpoint(self, checkpoint_id: str, archived: bool = True) -> dict[str, Any]:
        row = self.catalog.one("SELECT id FROM checkpoints WHERE id=?", (checkpoint_id,))
        if row is None:
            raise HistoryError("NOT_FOUND", f"unknown checkpoint {checkpoint_id}")
        with self.catalog.tx() as db:
            db.execute("UPDATE checkpoints SET archived_utc=? WHERE id=?",
                       (utc_now() if archived else None, checkpoint_id))
        self.catalog.event("history.checkpoint_archived" if archived else
                           "history.checkpoint_unarchived",
                           f"{'archived' if archived else 'unarchived'} checkpoint {checkpoint_id}",
                           checkpoint_id=checkpoint_id)
        return {"checkpoint_id": checkpoint_id, "archived": archived}

    # -------------------------------------------------------------- preview

    def finished_workloads(self) -> list[str]:
        rows = self.catalog.query("SELECT id FROM workloads ORDER BY created_utc")
        return [str(r["id"]) for r in rows if self.blocked_reason(str(r["id"])) is None]

    def preview(self, workload_ids: list[str] | None = None) -> dict[str, Any]:
        """Exactly what a deletion would remove, and what is blocked and why. Read-only."""
        ids = workload_ids if workload_ids is not None else [
            str(r["id"]) for r in self.catalog.query("SELECT id FROM workloads")]
        eligible, blocked = [], []
        total_files = total_bytes = 0
        for wid in ids:
            reason = self.blocked_reason(wid)
            if reason is not None:
                blocked.append({"workload_id": wid, "reason": reason})
                continue
            directory = self._directory(wid)
            files, size = _tree_size(directory) if directory else (0, 0)
            total_files += files
            total_bytes += size
            count = {t: self.catalog.one(f"SELECT COUNT(*) AS n FROM {t} WHERE workload_id=?",
                                         (wid,))["n"]  # type: ignore[index]
                     for t in ("instances", "checkpoints", "operations")}
            eligible.append({"workload_id": wid, "directory": str(directory) if directory else None,
                             "files": files, "bytes": size, **count})
        return {"eligible": eligible, "blocked": blocked, "total_files": total_files,
                "total_bytes": total_bytes,
                "kept": "the activity log (audit) is kept; nothing outside each workload's own "
                        "directory is touched"}

    # --------------------------------------------------------------- delete

    def delete(self, workload_ids: list[str]) -> dict[str, Any]:
        deleted, blocked, failed = [], [], []
        for wid in workload_ids:
            reason = self.blocked_reason(wid)
            if reason is not None:
                blocked.append({"workload_id": wid, "reason": reason})
                continue
            directory = self._directory(wid)
            with self.catalog.tx() as db:  # 1. a half-deleted checkpoint is never restorable
                db.execute("UPDATE checkpoints SET state='MISSING' WHERE workload_id=?", (wid,))
            try:
                if directory is not None:
                    safe_remove_tree(directory)  # 2. files, never following symlinks
            except (OSError, HistoryError) as exc:
                failed.append({"workload_id": wid, "error": str(exc)})
                self.catalog.event("history.delete_failed", f"could not delete files of {wid}: "
                                   f"{exc}; its checkpoints are now MISSING", workload_id=wid)
                continue
            with self.catalog.tx() as db:  # 3. rows, children first (foreign keys)
                db.execute("DELETE FROM operations WHERE workload_id=? OR checkpoint_id IN "
                           "(SELECT id FROM checkpoints WHERE workload_id=?)", (wid, wid))
                db.execute("DELETE FROM checkpoints WHERE workload_id=?", (wid,))
                db.execute("DELETE FROM instances WHERE workload_id=?", (wid,))
                db.execute("DELETE FROM workloads WHERE id=?", (wid,))
            deleted.append(wid)
        self.catalog.event("history.deleted", f"deleted the history of {len(deleted)} workload(s)",
                           deleted=deleted, blocked=blocked, failed=failed)
        return {"deleted": deleted, "blocked": blocked, "failed": failed}

    def delete_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        """Delete one checkpoint record; its image files only if it owns the newest image."""
        row = self.catalog.one("SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,))
        if row is None:
            raise HistoryError("NOT_FOUND", f"unknown checkpoint {checkpoint_id}")
        reason = self.blocked_reason(str(row["workload_id"]))
        if reason is not None:
            raise HistoryError("CONFLICT", f"cannot delete {checkpoint_id}: {reason}")
        newer = self.catalog.one("SELECT id FROM checkpoints WHERE instance_id=? AND generation>?",
                                 (row["instance_id"], row["generation"]))
        removed: list[str] = []
        if row["state"] in ("PUBLISHED", "CORRUPT") and newer is None:
            directory = Path(row["directory"])
            with self.catalog.tx() as db:
                db.execute("UPDATE checkpoints SET state='MISSING' WHERE id=?", (checkpoint_id,))
            names = [MANIFEST_NAME]
            try:
                names += [a.relative_path for a in read_manifest(directory).artifacts]
            except Exception:  # unreadable manifest: only remove what we can name safely
                pass
            with artifacts.private_dir(directory) as dfd:
                for name in dict.fromkeys(names):
                    artifacts.check_name(name)
                    try:
                        os.unlink(name, dir_fd=dfd)
                        removed.append(name)
                    except FileNotFoundError:
                        pass
        with self.catalog.tx() as db:
            db.execute("DELETE FROM checkpoints WHERE id=?", (checkpoint_id,))
        self.catalog.event("history.checkpoint_deleted",
                           f"deleted checkpoint {checkpoint_id} ({row['state']}); files removed: "
                           f"{len(removed)}", checkpoint_id=checkpoint_id, files=removed)
        return {"checkpoint_id": checkpoint_id, "files_removed": removed}

    # ---------------------------------------------------------------- reset

    def reset_demo(self) -> dict[str, Any]:
        ids = [str(r["id"]) for r in self.catalog.query("SELECT id FROM workloads")]
        blocked = [{"workload_id": w, "reason": r} for w in ids
                   if (r := self.blocked_reason(w)) is not None]
        if blocked:
            raise HistoryError("CONFLICT", "reset refused: stop every workload first",
                               {"blocked": blocked})
        result = self.delete(ids)
        self.catalog.set_settings(**DEFAULT_SETTINGS)
        self.catalog.event("demo.reset", f"demo reset: {len(result['deleted'])} workload(s) "
                           "removed, settings restored to defaults", **result)
        return result
