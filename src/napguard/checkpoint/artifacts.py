"""Checkpoint artifact validation (SEC-006, SEC-011, REL-006; ADR 0006 §4).

Artifacts live flat in one private directory (mode 0700, owned by the user). Each is
opened relative to that directory with O_NOFOLLOW, must be a regular file owned by
the user with no group or other permissions, and is hashed from the opened
descriptor, so a path swapped after the check is never the one that was hashed.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from .base import ArtifactEntry, ArtifactRole, BackendError, ErrorCode


def check_name(name: str) -> str:
    """A single, plain file name: no directories, '..', NUL, or hidden-path tricks."""
    if (not name or name in (".", "..") or "/" in name or "\x00" in name
            or name.startswith("-")):
        raise BackendError(ErrorCode.INVALID_ARTIFACTS, f"invalid artifact name {name!r}")
    return name


@contextmanager
def private_dir(path: Path) -> Iterator[int]:
    """Open `path` as a directory fd after checking it is ours and private."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise BackendError(ErrorCode.INSECURE_ARTIFACT,
                           f"artifact directory {path} unusable: {exc.strerror}") from exc
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise BackendError(ErrorCode.INSECURE_ARTIFACT,
                               f"artifact directory {path} must be owned by uid {os.getuid()} "
                               f"with mode 0700 (found uid {st.st_uid}, mode "
                               f"{st.st_mode & 0o777:o})")
        yield fd
    finally:
        os.close(fd)


def inspect(dir_fd: int, name: str, role: ArtifactRole) -> ArtifactEntry:
    check_name(name)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     dir_fd=dir_fd)
    except OSError as exc:
        raise BackendError(ErrorCode.INSECURE_ARTIFACT if exc.errno == errno.ELOOP  # a symlink
                           else ErrorCode.INVALID_ARTIFACTS,
                           f"artifact {name}: {exc.strerror}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise BackendError(ErrorCode.INVALID_ARTIFACTS, f"artifact {name} is not a regular file")
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise BackendError(ErrorCode.INSECURE_ARTIFACT,
                               f"artifact {name} must be owned by uid {os.getuid()} with no group "
                               f"or other permissions (found uid {st.st_uid}, mode "
                               f"{st.st_mode & 0o777:o})")
        if role is ArtifactRole.IMAGE and st.st_size == 0:
            raise BackendError(ErrorCode.INVALID_ARTIFACTS, f"image {name} is empty")
        digest = hashlib.sha256()
        with os.fdopen(os.dup(fd), "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
        return ArtifactEntry(name, role, st.st_size, digest.hexdigest())
    finally:
        os.close(fd)


def inspect_all(directory: Path, items: Sequence[tuple[str, ArtifactRole]]) -> list[ArtifactEntry]:
    with private_dir(directory) as dfd:
        return [inspect(dfd, name, role) for name, role in items]


def verify(directory: Path, expected: Sequence[ArtifactEntry]) -> list[ArtifactEntry]:
    """Re-check every recorded artifact before a restore; CORRUPT on any hash change."""
    if not expected:
        raise BackendError(ErrorCode.INVALID_ARTIFACTS, "capture lists no artifacts")
    if not any(e.role is ArtifactRole.IMAGE for e in expected):
        raise BackendError(ErrorCode.INVALID_ARTIFACTS, "capture lists no image")
    found = inspect_all(directory, [(e.relative_path, e.role) for e in expected])
    for want, got in zip(expected, found, strict=True):
        if (want.size, want.sha256) != (got.size, got.sha256):
            raise BackendError(ErrorCode.CORRUPT,
                               f"artifact {want.relative_path} changed since capture "
                               f"(size {want.size} -> {got.size})",
                               detail={"expected_sha256": want.sha256, "found_sha256": got.sha256})
    return found
