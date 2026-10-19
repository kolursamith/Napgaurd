# Layer 0 Test Report

> **2026-10-19 note:** This is a historical record. Stage 00 replaces the SSH-based Ubuntu check with `scripts/layer0-ubuntu-check.sh`, run inside the Ubuntu VM. See [`../phases/STAGE-00-report.md`](../phases/STAGE-00-report.md).

- Date: 2026-10-11
- Scope: Environment discovery, repository foundation, and portable development tooling only
- Verdict: **BLOCKED**

## Environment evidence

The commands were executed from `/Users/sogo/genai/TEMP/Projects/OS_project/project`.

| Property | Observed value |
|---|---|
| Operating system | macOS 14.8.1 (build 23J30) |
| Kernel | Darwin 23.6.0 |
| Architecture | arm64, Apple M1 Pro |
| Execution context | Physical Mac host evidence; `/.dockerenv` absent; not the Ubuntu target |
| Filesystem | APFS data volume; 234 GiB available when inspected |
| User | Unprivileged interactive user `sogo` (uid 501), member of local `admin`; no elevated command used |
| Python | CPython 3.14.7 from Homebrew |
| Git | Apple Git 2.39.5 |
| C/C++ compiler | Apple Clang 16.0.0 |
| Make | GNU Make 3.81 |
| CMake | Not found |
| systemd runtime | Not present on macOS |
| D-Bus system socket | Not present on macOS |
| DMTCP / CRIU | Commands not found on the host; Linux target availability not verified |
| Workspace | Writable |
| Network | PyPI access worked after approval but was intermittent during one wheel download |
| Ubuntu/UTM target | UTM 4.7.5 and a started VM named `Linux` were discovered; SSH now answers, but host trust/authentication is not configured |

Hardware serial numbers and platform identifiers were intentionally omitted from this report.

## Required tests

| Test | Command/evidence | Result | Remaining risk |
|---|---|---|---|
| 0.1 Actual execution environment | `uname -a`, `sw_vers`, `arch`, `system_profiler`, `id`, `mount`, `df -h .` | PASS | Confirms only the current macOS host. |
| 0.2 Workspace writable | Create, verify, and remove `.napguard-write-test-phase0` | PASS | Does not test permissions on the unavailable Ubuntu target. |
| 0.3 Package installation/import | `.venv/bin/python -m pip install -e '.[dev]'`; `.venv/bin/python -c 'import napguard; ...'` | PASS | Passed on Python 3.14/macOS only. |
| 0.4 pytest smoke test | `.venv/bin/python -m pytest` | PASS | One portable unit smoke test passed. |
| 0.5 Ruff | `.venv/bin/ruff check .` | PASS | Portable lint only. |
| 0.6 mypy | `.venv/bin/mypy src tests` | PASS | Portable type check only. |
| 0.7 Repository structure/config | Explicit `test -f`/`test -d` checks for all required paths | PASS | No functional implementation is present by design. |
| 0.8 Git status/unintended changes | `git status --short --branch`; `.gitignore` verification | PASS | Repository has no commits; all visible files are expected Layer 0/user-provided files. |
| 0.9 Intended Ubuntu target access | UTM discovery and requested batch-mode SSH attempt to `ubuntu@192.168.64.2` | BLOCKED | Connection reached SSH but stopped with `Host key verification failed.` |
| 0.10 Linux facilities inspection | `systemctl`, `busctl`, D-Bus socket, kernel and checkpoint-tool lookups | BLOCKED | macOS absence is not evidence about Ubuntu; commands must run on the intended target. |

## Executed check outputs

### Package import

```text
0.1.0
/Users/sogo/genai/TEMP/Projects/OS_project/project/src/napguard/__init__.py
```

### pytest

```text
platform darwin -- Python 3.14.7, pytest-9.1.1, pluggy-1.6.0
collected 1 item
tests/unit/test_import.py . [100%]
1 passed in 0.01s
```

### Ruff

```text
All checks passed!
```

### mypy

```text
Success: no issues found in 10 source files
```

## Failures encountered and fixes

1. `git init -b main` initially failed in the managed sandbox with:

   ```text
   /Users/sogo/genai/TEMP/Projects/OS_project/project/.git: Operation not permitted
   ```

   The same narrowly scoped command was rerun after user approval and initialized the repository successfully.

2. The first sandboxed editable installation failed because DNS access to PyPI was blocked. The complete terminal failure was:

   ```text
   WARNING: The directory '/Users/sogo/Library/Caches/pip' or its parent directory is not owned or is not writable by the current user. The cache has been disabled. Check the permissions and owner of that directory. If executing pip with sudo, you should use sudo's -H flag.
   Obtaining file:///Users/sogo/genai/TEMP/Projects/OS_project/project
     Installing build dependencies: started
     Installing build dependencies: finished with status 'error'
     error: subprocess-exited-with-error

     × installing build dependencies did not run successfully.
     │ exit code: 1
     ╰─> [8 lines of output]
         WARNING: The directory '/Users/sogo/Library/Caches/pip' or its parent directory is not owned or is not writable by the current user. The cache has been disabled. Check the permissions and owner of that directory. If executing pip with sudo, you should use sudo's -H flag.
         WARNING: Retrying (Retry(total=4, connect=None, read=None, redirect=None, status=None)) after connection broken by 'NameResolutionError("HTTPSConnection(host='pypi.org', port=443): Failed to resolve 'pypi.org' ([Errno 8] nodename nor servname provided, or not known)")': /simple/hatchling/
         WARNING: Retrying (Retry(total=3, connect=None, read=None, redirect=None, status=None)) after connection broken by 'NameResolutionError("HTTPSConnection(host='pypi.org', port=443): Failed to resolve 'pypi.org' ([Errno 8] nodename nor servname provided, or not known)")': /simple/hatchling/
         WARNING: Retrying (Retry(total=2, connect=None, read=None, redirect=None, status=None)) after connection broken by 'NameResolutionError("HTTPSConnection(host='pypi.org', port=443): Failed to resolve 'pypi.org' ([Errno 8] nodename nor servname provided, or not known)")': /simple/hatchling/
         WARNING: Retrying (Retry(total=1, connect=None, read=None, redirect=None, status=None)) after connection broken by 'NameResolutionError("HTTPSConnection(host='pypi.org', port=443): Failed to resolve 'pypi.org' ([Errno 8] nodename nor servname provided, or not known)")': /simple/hatchling/
         WARNING: Retrying (Retry(total=0, connect=None, read=None, redirect=None, status=None)) after connection broken by 'NameResolutionError("HTTPSConnection(host='pypi.org', port=443): Failed to resolve 'pypi.org' ([Errno 8] nodename nor servname provided, or not known)")': /simple/hatchling/
         ERROR: Could not find a version that satisfies the requirement hatchling<2,>=1.27 (from versions: none)
         ERROR: No matching distribution found for hatchling<2,>=1.27
         [end of output]

     note: This error originates from a subprocess, and is likely not a problem with pip.
   ERROR: Failed to build 'file:///Users/sogo/genai/TEMP/Projects/OS_project/project' when installing build dependencies
   ```

   After network approval, the install reached PyPI. A mypy wheel download was interrupted repeatedly and ended with `error: incomplete-download`. Retrying `mypy==1.20.2` separately completed the wheel, and rerunning the editable install succeeded. No global package was changed.

3. The first structure assertion exited 1 because `docs/test-reports/` had not yet been created. This report created the required path; the structure gate was rerun afterward.

## Limitations

- Ubuntu 24.04, its kernel, systemd, D-Bus, filesystem, permissions, and CPU architecture were not inspected.
- DMTCP and CRIU are absent on the macOS host, which says nothing about target availability or compatibility.
- No checkpoint/restart experiment was attempted; those belong to Layer 1.
- No battery, lid, suspend, cold-boot, daemon, IPC, SQLite, recovery, or UI functionality was implemented or simulated.

## Gate conclusion

Portable repository/tooling checks pass, but mandatory tests 0.9 and 0.10 are blocked. Under the strict completion gate, Layer 0 is **BLOCKED**, not passed.

## Ubuntu target continuation evidence — 2026-10-11

### UTM discovery

The installed application is UTM 4.7.5 (build 118). Its read-only CLI returned:

```text
UUID                                 Status   Name
76EF4ED8-1A57-4699-A01A-42194D555909 started  Linux
```

The existing VM configuration reports:

- QEMU backend with target `virt`.
- Configured guest architecture `aarch64`.
- Four virtual CPUs and 6144 MiB memory.
- One `virtio-net-pci` interface in UTM `Shared` mode.
- No configured port forwards.

These are hypervisor configuration facts, not proof that the installed guest operating system is Ubuntu or that its running kernel reports `aarch64`.

### Guest-address discovery

The VM's configured MAC address was present in the host neighbor table on `bridge100`. UTM could not report an address because its QEMU guest agent is not installed or running:

```text
started
Error from event: The operation couldn’t be completed. (OSStatus error -2700.)
The QEMU guest agent is not running or not installed on the guest.
```

The live neighbor entries mapped the configured MAC address to the link-local address `fe80::b81a:85ff:fefd:d7af%bridge100` and ULA addresses on the host's UTM network. Exact TCP/22 probes returned:

```text
nc: connectx to fe80::b81a:85ff:fefd:d7af%bridge100 port 22 (tcp) failed: Connection refused
nc: connectx to fd40:c018:47c5:4488:1515:91c8:7172:fbce port 22 (tcp) failed: Connection refused
nc: connectx to fd40:c018:47c5:4488:7ad1:3b30:d158:ca7f port 22 (tcp) failed: Operation timed out
```

Before approval, the same probes inside the managed sandbox failed with `Operation not permitted`; the approved host-level probes above provide the authoritative result.

### SSH/authentication status at the initial continuation

- At the time of the initial continuation, no SSH server was reachable on port 22 at the discovered active guest addresses.
- The macOS host has no `~/.ssh/config` and no SSH identity file; only `known_hosts` is present.
- No password, private key, or credential was created or stored in the repository.
- No UTM networking, guest package, firewall, or service configuration was changed.

Consequently, no authenticated connection method existed at that point and none of the required Ubuntu commands or Linux-side repository tests were executed.

### Required user action inside the Ubuntu console

After approving OpenSSH installation, execute inside the running VM:

```sh
sudo apt update
sudo apt install --yes openssh-server
sudo systemctl enable --now ssh
systemctl is-active ssh
whoami
hostname -I
ip -6 address show scope global
sudo ss -ltnp 'sport = :22'
```

If `sudo ufw status` reports an active firewall that blocks SSH, stop and request approval before changing firewall rules. Do not place credentials in this repository.

## Final SSH verification attempt — 2026-10-11

The user subsequently reported that OpenSSH is active for user `ubuntu` at `192.168.64.2:22` with UFW inactive. The required command was executed unchanged:

```sh
ssh -o BatchMode=yes -o ConnectTimeout=10 ubuntu@192.168.64.2 'whoami && uname -m'
```

Inside the managed sandbox it exited 255 with:

```text
ssh: connect to host 192.168.64.2 port 22: Operation not permitted
```

The approved host-network retry also exited 255 with:

```text
Host key verification failed.
```

Host-key verification was not disabled. No host key was added, no SSH configuration was modified, no password was requested, and no private key or credential was stored in the repository. Because SSH trust failed before authentication, no remote command ran and the guest's operating system, runtime architecture, facilities, repository, Python environment, tests, and backend package availability remain unverified. Tests 0.9 and 0.10 remain **BLOCKED**.

The user must verify the guest's SSH host-key fingerprint through the trusted UTM console, add the verified host key to the macOS user's `known_hosts` outside this repository, and configure public-key authentication outside the repository. The batch-mode connection can then be retried.

