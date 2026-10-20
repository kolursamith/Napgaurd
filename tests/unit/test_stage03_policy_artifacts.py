"""Stage 03a: coordinator policy (default deny, developer mode) and artifact validation."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from napguard.checkpoint import artifacts, policy
from napguard.checkpoint.base import ArtifactEntry, ArtifactRole, BackendError, ErrorCode

# ------------------------------------------------------------------ policy


def test_default_policy_refuses_every_coordinator() -> None:
    for pol in (policy.CoordinatorPolicy(), policy.CoordinatorPolicy.deny(),
                policy.CoordinatorPolicy.experimental_single_user(False)):
        with pytest.raises(BackendError) as err:
            pol.authorize(os.getuid(), lambda uid: [])  # even on a single-user host
        assert err.value.code is ErrorCode.PERMISSION_DENIED
        assert policy.SINGLE_USER_FLAG in err.value.message


def test_developer_mode_needs_a_single_user_host() -> None:
    dev = policy.CoordinatorPolicy.experimental_single_user(True)
    dev.authorize(os.getuid(), lambda uid: [])  # allowed: acknowledged and no other user
    with pytest.raises(BackendError) as err:
        dev.authorize(os.getuid(), lambda uid: ["logind sessions of other users: uids [1001]"])
    assert err.value.code is ErrorCode.PERMISSION_DENIED
    assert "does not look single-user" in err.value.message


def test_host_check_flags_other_users_and_fails_closed_without_logind() -> None:
    assert policy.single_user_host_findings(1000, lambda: [("2", 1000)], lambda: []) == []
    greeter = [("c1", 120), ("c2", 65534)]  # gdm and nobody are system accounts
    assert policy.single_user_host_findings(1000, lambda: greeter, lambda: []) == []
    other = policy.single_user_host_findings(1000, lambda: [("2", 1000), ("5", 1001)],
                                             lambda: [])
    assert other == ["logind sessions of other users: uids [1001]"]
    procs = policy.single_user_host_findings(1000, lambda: [], lambda: [1002])
    assert procs == ["processes of other regular users: uids [1002]"]

    def broken() -> list[tuple[str, int]]:
        raise RuntimeError("loginctl not found")

    findings = policy.single_user_host_findings(1000, broken, lambda: [])
    assert findings and "refusing" in findings[0]


def test_parse_loginctl_sessions() -> None:
    out = "     2 1000 sogo seat0 1234 user tty2 no -\n c1 120 gdm seat0 99 greeter - no -\n"
    assert policy.parse_loginctl_sessions(out) == [("2", 1000), ("c1", 120)]


# --------------------------------------------------------------- artifacts


def private(tmp_path: Path) -> Path:
    d = tmp_path / "ckpt"
    d.mkdir(mode=0o700)
    return d


def write(d: Path, name: str, data: bytes = b"image-bytes", mode: int = 0o600) -> Path:
    path = d / name
    path.write_bytes(data)
    path.chmod(mode)
    return path


def test_valid_artifacts_are_hashed_from_the_opened_file(tmp_path: Path) -> None:
    d = private(tmp_path)
    write(d, "ckpt_a.dmtcp")
    write(d, "dmtcp_restart_script_x.sh", b"#!/bin/sh\n", 0o700)
    found = artifacts.inspect_all(d, [("ckpt_a.dmtcp", ArtifactRole.IMAGE),
                                      ("dmtcp_restart_script_x.sh", ArtifactRole.RESTART_SCRIPT)])
    assert found[0].sha256 == hashlib.sha256(b"image-bytes").hexdigest()
    assert artifacts.verify(d, found) == found


def test_symlinked_artifact_is_rejected(tmp_path: Path) -> None:
    d = private(tmp_path)
    target = write(tmp_path, "elsewhere")
    (d / "ckpt_a.dmtcp").symlink_to(target)
    with pytest.raises(BackendError) as err:
        artifacts.inspect_all(d, [("ckpt_a.dmtcp", ArtifactRole.IMAGE)])
    assert err.value.code is ErrorCode.INSECURE_ARTIFACT


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o666])
def test_group_or_other_permissions_are_rejected(tmp_path: Path, mode: int) -> None:
    d = private(tmp_path)
    write(d, "ckpt_a.dmtcp", mode=mode)
    with pytest.raises(BackendError) as err:
        artifacts.inspect_all(d, [("ckpt_a.dmtcp", ArtifactRole.IMAGE)])
    assert err.value.code is ErrorCode.INSECURE_ARTIFACT


def test_non_private_directory_is_rejected(tmp_path: Path) -> None:
    d = private(tmp_path)
    write(d, "ckpt_a.dmtcp")
    d.chmod(0o755)
    with pytest.raises(BackendError) as err:
        artifacts.inspect_all(d, [("ckpt_a.dmtcp", ArtifactRole.IMAGE)])
    assert err.value.code is ErrorCode.INSECURE_ARTIFACT


def test_tampered_artifact_is_corrupt(tmp_path: Path) -> None:
    d = private(tmp_path)
    write(d, "ckpt_a.dmtcp")
    recorded = artifacts.inspect_all(d, [("ckpt_a.dmtcp", ArtifactRole.IMAGE)])
    write(d, "ckpt_a.dmtcp", b"image-bytez")  # same size, one byte flipped
    with pytest.raises(BackendError) as err:
        artifacts.verify(d, recorded)
    assert err.value.code is ErrorCode.CORRUPT


@pytest.mark.parametrize("name", ["", ".", "..", "../x", "a/b", "-rf", "x\x00y"])
def test_unsafe_artifact_names_are_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(BackendError) as err:
        artifacts.check_name(name)
    assert err.value.code is ErrorCode.INVALID_ARTIFACTS


def test_empty_image_and_missing_image_role_are_invalid(tmp_path: Path) -> None:
    d = private(tmp_path)
    write(d, "ckpt_a.dmtcp", b"")
    with pytest.raises(BackendError) as err:
        artifacts.inspect_all(d, [("ckpt_a.dmtcp", ArtifactRole.IMAGE)])
    assert err.value.code is ErrorCode.INVALID_ARTIFACTS
    script = ArtifactEntry("s.sh", ArtifactRole.RESTART_SCRIPT, 1, "0" * 64)
    with pytest.raises(BackendError) as err:
        artifacts.verify(d, [script])
    assert "no image" in err.value.message
