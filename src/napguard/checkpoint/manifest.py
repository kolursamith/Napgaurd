"""Checkpoint manifest: what was captured, how to check it, and what it does NOT restore.

A manifest is written next to the images after a verified checkpoint and validated
before every restore (SEC-007, SEC-011, REL-006, CMP-004, CMP-005). It is strict: unknown
or missing fields, unsafe artifact names, an unknown role, a missing image, a digest
mismatch, or a missing limitation statement make it `INVALID_MANIFEST`. A manifest can
never claim that files or other external state are rolled back (LIM-007, LIM-002).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from . import artifacts
from .base import ArtifactEntry, ArtifactRole, BackendError, CaptureRecord, ErrorCode

MANIFEST_NAME = "napguard-manifest.json"
SCHEMA = "napguard.checkpoint-manifest/v1"
MAX_BYTES = 1 << 20  # SEC-005-style bound on what is parsed
LIMITATIONS = (
    "LIM-007: files and other external state changed after the checkpoint are NOT rolled "
    "back by a restore; a restored process may see newer files than its memory expects",
    "LIM-002: external side effects (file writes, messages, database changes) are not undone",
    "LIM-003: network connections are not restored",
)
_FIELDS = {"schema", "created_utc", "backend", "backend_version", "tool_sha256", "arch",
           "kernel_release", "artifacts", "restore_hints", "limitations", "body_sha256"}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _invalid(why: str) -> BackendError:
    return BackendError(ErrorCode.INVALID_MANIFEST, f"invalid checkpoint manifest: {why}")


def _canonical(body: dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def to_body(record: CaptureRecord) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": SCHEMA,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "backend": record.backend, "backend_version": record.backend_version,
        "tool_sha256": dict(record.tool_sha256), "arch": record.arch,
        "kernel_release": record.kernel_release,
        "artifacts": [{"name": a.relative_path, "role": str(a.role), "size": a.size,
                       "sha256": a.sha256} for a in record.artifacts],
        "restore_hints": dict(record.restore_hints), "limitations": list(LIMITATIONS)}
    body["body_sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
    return body


def write_manifest(directory: Path, record: CaptureRecord) -> Path:
    """Write the manifest (0600, O_EXCL, no symlinks) and fsync it and the directory."""
    data = json.dumps(to_body(record), indent=2, sort_keys=True).encode()
    with artifacts.private_dir(directory) as dfd:
        try:
            fd = os.open(MANIFEST_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        except FileExistsError as exc:
            raise BackendError(ErrorCode.INVALID_ARGUMENT,
                               f"{directory} already holds a manifest") from exc
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(dfd)
    return directory / MANIFEST_NAME


def parse_body(body: object) -> CaptureRecord:
    """Validate a decoded manifest strictly and return the capture record."""
    if not isinstance(body, dict):
        raise _invalid("not a JSON object")
    keys = set(body)
    if keys != _FIELDS:
        raise _invalid(f"missing {sorted(_FIELDS - keys)}, unknown {sorted(keys - _FIELDS)}")
    if body["schema"] != SCHEMA:
        raise _invalid(f"unsupported schema {body['schema']!r}")
    claimed = body["body_sha256"]
    rest = {k: v for k, v in body.items() if k != "body_sha256"}
    if not isinstance(claimed, str) or hashlib.sha256(_canonical(rest)).hexdigest() != claimed:
        raise _invalid("body digest does not match (edited or corrupt)")
    for key in ("backend", "backend_version", "arch", "kernel_release", "created_utc"):
        if not isinstance(body[key], str) or not body[key]:
            raise _invalid(f"{key} must be a non-empty string")
    tools = body["tool_sha256"]
    if not isinstance(tools, dict) or not tools or not all(
            isinstance(k, str) and isinstance(v, str) and _HEX64.match(v)
            for k, v in tools.items()):
        raise _invalid("tool_sha256 must map tool names to SHA-256 digests")
    if not isinstance(body["restore_hints"], dict):
        raise _invalid("restore_hints must be an object")
    if body["limitations"] != list(LIMITATIONS):
        raise _invalid("limitations must state, unchanged, what a restore does NOT roll back")
    entries = []
    items = body["artifacts"]
    if not isinstance(items, list) or not items:
        raise _invalid("artifacts must be a non-empty list")
    for item in items:
        if not isinstance(item, dict) or set(item) != {"name", "role", "size", "sha256"}:
            raise _invalid("each artifact needs exactly name, role, size, sha256")
        try:
            name = artifacts.check_name(item["name"]) if isinstance(item["name"], str) else ""
            role = ArtifactRole(item["role"])
        except (BackendError, ValueError) as exc:
            raise _invalid(f"artifact {item.get('name')!r}: {exc}") from exc
        size, digest = item["size"], item["sha256"]
        if not name or not isinstance(size, int) or isinstance(size, bool) or size < 0 \
                or not isinstance(digest, str) or not _HEX64.match(digest):
            raise _invalid(f"artifact {item.get('name')!r} has an invalid name, size or digest")
        entries.append(ArtifactEntry(name, role, size, digest))
    if len({e.relative_path for e in entries}) != len(entries):
        raise _invalid("duplicate artifact names")
    if not any(e.role is ArtifactRole.IMAGE for e in entries):
        raise _invalid("no IMAGE artifact")
    return CaptureRecord(body["backend"], body["backend_version"], dict(tools), body["arch"],
                         body["kernel_release"], entries, dict(body["restore_hints"]))


def read_manifest(directory: Path) -> CaptureRecord:
    """Open (O_NOFOLLOW, inside the private directory), bound, parse, and validate."""
    with artifacts.private_dir(directory) as dfd:
        try:
            fd = os.open(MANIFEST_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
        except FileNotFoundError as exc:
            raise _invalid("missing") from exc
        except OSError as exc:
            raise BackendError(ErrorCode.INSECURE_ARTIFACT,
                               f"manifest unusable: {exc.strerror}") from exc
        try:
            st = os.fstat(fd)
            if st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise BackendError(ErrorCode.INSECURE_ARTIFACT,
                                   "manifest must be owned by this user with mode 0600")
            data = os.read(fd, MAX_BYTES + 1)
        finally:
            os.close(fd)
    if len(data) > MAX_BYTES:
        raise _invalid("too large")
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _invalid(f"not valid JSON ({exc})") from exc
    return parse_body(body)
