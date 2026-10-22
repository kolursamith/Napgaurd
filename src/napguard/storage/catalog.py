"""Stage 04 (MVP): the persistent SQLite catalog.

Workloads, process instances, checkpoint history, operations, the activity log, and
settings. Durability follows ADR 0003 where it applies (WAL, synchronous=FULL, foreign
keys, one transaction per state change, startup reconciliation). Checkpoint images are
NOT stored here and NOT moved: they stay where DMTCP wrote them (Stage 03a decision);
the catalog stores the manifest's SHA-256, which is the root of trust for a restore
(SEC-007). See ADR 0008 for the MVP deviations.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
# Additive migrations (never drop or rewrite data): version -> [(table, column, type)].
MIGRATIONS = {2: [("workloads", "archived_utc", "TEXT"), ("checkpoints", "archived_utc", "TEXT")]}
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workloads(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, workload_class TEXT NOT NULL,
  argv TEXT NOT NULL, work_dir TEXT NOT NULL, report_path TEXT NOT NULL,
  created_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS instances(
  id TEXT PRIMARY KEY, workload_id TEXT NOT NULL REFERENCES workloads(id),
  origin TEXT NOT NULL CHECK(origin IN ('LAUNCH','RESTORE')),
  checkpoint_id TEXT, pid INTEGER, start_ticks INTEGER, uid INTEGER, boot_id TEXT,
  session_dir TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('STARTING','RUNNING','STOPPED','EXITED','LOST','FAILED')),
  started_utc TEXT NOT NULL, ended_utc TEXT, exit_info TEXT, kill_line INTEGER);
CREATE TABLE IF NOT EXISTS checkpoints(
  id TEXT PRIMARY KEY, workload_id TEXT NOT NULL REFERENCES workloads(id),
  instance_id TEXT NOT NULL REFERENCES instances(id), generation INTEGER NOT NULL,
  trigger TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('PUBLISHED','SUPERSEDED','CORRUPT','MISSING')),
  directory TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, total_bytes INTEGER NOT NULL,
  capture_ms INTEGER NOT NULL, backend TEXT NOT NULL, backend_version TEXT NOT NULL,
  created_utc TEXT NOT NULL, verified_utc TEXT, restore_verified_utc TEXT,
  marks TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, workload_id TEXT, checkpoint_id TEXT,
  trigger TEXT, params TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL CHECK(state IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','INTERRUPTED')),
  created_utc TEXT NOT NULL, started_utc TEXT, ended_utc TEXT, duration_ms INTEGER,
  error_code TEXT, message TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, kind TEXT NOT NULL,
  simulated INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
DEFAULT_SETTINGS = {"auto_checkpoint_enabled": False, "auto_checkpoint_interval_s": 300,
                    "battery_low_percent": 20, "battery_critical_percent": 10}


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Catalog:
    """One writer connection, serialized by a lock; every state change is one transaction."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            for stmt in SCHEMA.strip().split(";"):
                if stmt.strip():
                    self._db.execute(stmt)
            with self.tx() as db:
                db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', '1')")
            self.migrations_applied = self._migrate()
        path.chmod(0o600)

    def schema_version(self) -> int:
        row = self.one("SELECT value FROM meta WHERE key='schema_version'")
        return int(row["value"]) if row else 0

    def _migrate(self) -> list[int]:
        """Additive, idempotent, one transaction per version; existing rows are kept."""
        applied = []
        for version in sorted(MIGRATIONS):
            if self.schema_version() >= version:
                continue
            with self.tx() as db:
                for table, column, kind in MIGRATIONS[version]:
                    cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
                    if column not in cols:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
                db.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(version),))
            applied.append(version)
        return applied

    def pragmas(self) -> dict[str, Any]:
        with self._lock:
            return {name: self._db.execute(f"PRAGMA {name}").fetchone()[0]
                    for name in ("journal_mode", "synchronous", "foreign_keys")}

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def query(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---------------------------------------------------------------- events

    def event(self, kind: str, message: str, *, simulated: bool = False, **data: Any) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO events(ts_utc, kind, simulated, message, data) "
                       "VALUES (?,?,?,?,?)",
                       (utc_now(), kind, int(simulated), message,
                        json.dumps(data, sort_keys=True, default=str)))

    def events(self, limit: int = 100, after_seq: int = 0) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM events WHERE seq > ? ORDER BY seq DESC LIMIT ?",
                          (after_seq, max(1, min(limit, 1000))))
        for r in rows:
            r["simulated"] = bool(r["simulated"])
            r["data"] = json.loads(r["data"])
        return rows

    # -------------------------------------------------------------- settings

    def settings(self) -> dict[str, Any]:
        found = dict(DEFAULT_SETTINGS)
        for row in self.query("SELECT key, value FROM settings"):
            found[row["key"]] = json.loads(row["value"])
        return found

    def set_settings(self, **values: Any) -> dict[str, Any]:
        with self.tx() as db:
            for key, value in values.items():
                if key not in DEFAULT_SETTINGS:
                    raise KeyError(key)
                db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))
        return self.settings()

    # ------------------------------------------------------------ operations

    def add_operation(self, kind: str, workload_id: str | None = None,
                      checkpoint_id: str | None = None, trigger: str | None = None,
                      params: dict[str, Any] | None = None) -> str:
        op_id = new_id("op")
        with self.tx() as db:
            db.execute("INSERT INTO operations(id, kind, workload_id, checkpoint_id, trigger, "
                       "params, state, created_utc) VALUES (?,?,?,?,?,?, 'QUEUED', ?)",
                       (op_id, kind, workload_id, checkpoint_id, trigger,
                        json.dumps(params or {}), utc_now()))
        return op_id

    def start_operation(self, op_id: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE operations SET state='RUNNING', started_utc=? WHERE id=?",
                       (utc_now(), op_id))

    def finish_operation(self, op_id: str, ok: bool, duration_ms: int, *,
                         error_code: str | None = None, message: str = "",
                         result: dict[str, Any] | None = None) -> None:
        with self.tx() as db:
            db.execute("UPDATE operations SET state=?, ended_utc=?, duration_ms=?, error_code=?, "
                       "message=?, result=? WHERE id=?",
                       ("SUCCEEDED" if ok else "FAILED", utc_now(), duration_ms, error_code,
                        message, json.dumps(result or {}, default=str), op_id))

    def operation(self, op_id: str) -> dict[str, Any] | None:
        row = self.one("SELECT * FROM operations WHERE id=?", (op_id,))
        if row is not None:
            row["params"] = json.loads(row["params"])
            row["result"] = json.loads(row["result"]) if row["result"] else None
        return row

    def pending_operation(self, kind: str, workload_id: str | None) -> str | None:
        row = self.one("SELECT id FROM operations WHERE kind=? AND workload_id IS ? "
                       "AND state IN ('QUEUED','RUNNING') ORDER BY created_utc LIMIT 1",
                       (kind, workload_id))
        return str(row["id"]) if row else None

    # ------------------------------------------------------------ reconcile

    def reconcile(self) -> dict[str, int]:
        """Startup reconciliation (REL-005, REL-009): nothing stays RUNNING across a restart."""
        with self.tx() as db:
            ops = db.execute("UPDATE operations SET state='INTERRUPTED', ended_utc=? "
                             "WHERE state IN ('QUEUED','RUNNING')", (utc_now(),)).rowcount
            inst = db.execute("UPDATE instances SET state='LOST', ended_utc=? "
                              "WHERE state IN ('STARTING','RUNNING')", (utc_now(),)).rowcount
        missing = 0
        for row in self.query("SELECT id, directory FROM checkpoints WHERE state='PUBLISHED'"):
            if not (Path(row["directory"]) / "napguard-manifest.json").is_file():
                self.set_checkpoint_state(row["id"], "MISSING")
                missing += 1
        return {"operations_interrupted": ops, "instances_lost": inst, "checkpoints_missing": missing}

    def set_checkpoint_state(self, ckpt_id: str, state: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE checkpoints SET state=? WHERE id=?", (state, ckpt_id))
