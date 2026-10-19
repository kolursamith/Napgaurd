"""Stage 02 backend command construction (no backend is executed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ngexp import backends

TOOLS = backends.DmtcpTools("/p/dmtcp_launch", "/p/dmtcp_command", "/p/dmtcp_coordinator",
                            "/p/dmtcp_restart")


def test_dmtcp_commands_use_private_loopback_coordinator() -> None:
    coord = backends.dmtcp_coordinator_argv(TOOLS, Path("/r/c.port"), Path("/r/ckpt"))
    assert coord[:3] == ["/p/dmtcp_coordinator", "--coord-port", "0"]
    launch = backends.dmtcp_launch_argv(TOOLS, 7779, Path("/r/ckpt"), Path("/r/tmp"),
                                        ["/r/bin/ngcounter", "--report", "/r/x"])
    assert launch[launch.index("--coord-host") + 1] == "127.0.0.1"
    assert launch[launch.index("--coord-port") + 1] == "7779"
    assert "--join-coordinator" in launch  # never silently start or join a default coordinator
    assert launch[-3:] == ["/r/bin/ngcounter", "--report", "/r/x"]
    cmd = backends.dmtcp_command_argv(TOOLS, 7779, "--bcheckpoint")
    assert cmd[-1] == "--bcheckpoint" and "7779" in cmd
    restart = backends.dmtcp_restart_argv(TOOLS, 7780, Path("/r/tmp"), [Path("/r/ckpt/a.dmtcp")])
    assert restart[-1] == "/r/ckpt/a.dmtcp" and "7780" in restart


def test_criu_dump_targets_only_the_given_tree_and_leaves_it_running() -> None:
    argv = backends.criu_dump_argv("/x/criu", "unprivileged", 4242, Path("/r/img"),
                                   Path("/r/d.log"))
    assert argv[:3] == ["/x/criu", "dump", "--unprivileged"]
    assert argv[argv.index("--tree") + 1] == "4242"
    assert "--leave-running" in argv
    assert "--shell-job" not in argv and "--tcp-established" not in argv


def test_criu_privileged_mode_is_explicit() -> None:
    argv = backends.criu_restore_argv("/x/criu", "sudo", Path("/r/img"), Path("/r/l"),
                                      Path("/r/p"))
    assert argv[:3] == ["sudo", "-n", "/x/criu"]
    assert "--unprivileged" not in argv
    assert "--restore-detached" in argv and argv[argv.index("--pidfile") + 1] == "/r/p"
    with pytest.raises(ValueError):
        backends.criu_prefix("root")


@pytest.mark.parametrize(
    ("output", "caps"),
    [
        ("/x/criu cap_checkpoint_restore,cap_sys_ptrace=ep\n",
         {"cap_checkpoint_restore", "cap_sys_ptrace"}),
        ("/x/criu cap_checkpoint_restore+eip\n", {"cap_checkpoint_restore"}),
        ("", set()),
    ],
)
def test_parse_getcap(output: str, caps: set[str]) -> None:
    assert backends.parse_getcap(output) == caps
