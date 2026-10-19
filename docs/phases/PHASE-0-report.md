# Phase 0 Report: Environment, Feasibility, and Repository Foundation

> **2026-10-19 note:** This is a historical record. Work continues under Stage 00 with a workflow that does not use SSH (macOS → Git → GitHub → Ubuntu in UTM). See [`STAGE-00-report.md`](STAGE-00-report.md).

## Outcome

Phase/Layer 0 is **BLOCKED** because SSH host-key trust and authentication to the running UTM target are not configured. Portable macOS repository setup and development-tool checks succeeded. No Layer 1 work was started.

## Actual commands

Environment and access discovery included:

```sh
pwd
ls -la
find . -maxdepth 3 -print | sort
uname -a
sw_vers
arch
system_profiler SPSoftwareDataType SPHardwareDataType
id
groups
umask
df -h .
mount
python3 --version
git --version
command -v <required tools>
cc --version
c++ --version
make --version
cmake --version
git status --short --branch
git rev-parse --show-toplevel
find /Applications "$HOME/Applications" -maxdepth 3 -iname '*UTM*' -print
mdfind 'kMDItemCFBundleIdentifier == "com.utmapp.UTM"'
ssh -V
```

Repository/tooling commands included:

```sh
git init -b main
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pip install 'mypy==1.20.2'
.venv/bin/python -c 'import napguard; print(napguard.__version__); print(napguard.__file__)'
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/mypy src tests
git status --short --branch
```

## Results

- Actual host: macOS 14.8.1, Darwin 23.6.0, arm64 Apple M1 Pro.
- Workspace writability: PASS.
- Git repository initialization: PASS after narrowly scoped approval.
- Editable package installation/import: PASS.
- pytest: PASS, 1 test.
- Ruff: PASS.
- mypy: PASS, 10 source files.
- Required repository paths: PASS after the test report was added.
- Ubuntu target access: BLOCKED.
- Ubuntu systemd, D-Bus, and kernel inspection: BLOCKED.

Continuation discovery on 2026-10-11 found UTM 4.7.5 and a started VM named `Linux`. Its UTM configuration is QEMU/aarch64 with shared networking, four virtual CPUs, 6144 MiB memory, and no port forwards. `utmctl ip-address` reported that the QEMU guest agent is unavailable. Initial TCP probes to port 22 returned `Connection refused` on two live addresses and timed out on a third address. After the user enabled OpenSSH and supplied `ubuntu@192.168.64.2`, the required batch-mode connection reached SSH but stopped with `Host key verification failed.` No authenticated SSH connection was established.

Detailed evidence and failure output are in `docs/test-reports/layer-0.md`.

## Dependencies installed

Only the local `.venv` was modified. Direct development tools:

- mypy 1.20.2
- pytest 9.1.1
- Ruff 0.17.0
- NapGuard 0.1.0 editable package

Resolved support packages include iniconfig 2.3.1, librt 0.16.0, mypy_extensions 1.1.0, packaging 26.3, pathspec 1.1.1, pluggy 1.6.0, Pygments 2.21.0, and typing_extensions 4.16.0. Hatchling was used in pip's isolated build environment. No PySide6, DMTCP, CRIU, or system package was installed.

## Known limitations

- The intended guest is running and SSH answers at `192.168.64.2:22`, but Ubuntu 24.04 and its runtime architecture remain unverified because host trust/authentication failed.
- All passing executable checks are host-side portable checks, not Linux integration proof.
- The project uses the host's Python 3.14.7; target Python compatibility still requires Ubuntu verification.
- Network access was intermittent during dependency download but succeeded on retry.

## Git diff summary

The repository had no commits when initialized. Expected uncommitted files comprise the two pre-existing rule documents plus the Layer 0 README, configuration, package skeleton, smoke test, architecture/decision documentation, phase plan, and reports. `.venv`, caches, bytecode, and `.DS_Store` are ignored.

## Gate table

| Criterion | Command/evidence | Result | Remaining risk |
|---|---|---|---|
| Host environment identified | `uname`, `sw_vers`, `arch`, tool discovery | PASS | Host is not the target. |
| Workspace writable | Temporary-file probe | PASS | Target permissions unknown. |
| Repository initialized | `git status --short --branch` | PASS | No initial commit was requested or made. |
| Package install/import | Editable install and import command | PASS | macOS/Python 3.14 only. |
| pytest smoke test | `.venv/bin/python -m pytest` | PASS | Unit smoke only. |
| Ruff | `.venv/bin/ruff check .` | PASS | Portable lint only. |
| mypy | `.venv/bin/mypy src tests` | PASS | Portable type check only. |
| Architecture and ADR | Required Markdown files | PASS | Decisions remain proposed. |
| Ubuntu target verified | Required batch SSH command exited 255: `Host key verification failed.` | BLOCKED | Verified host key and public-key authentication are not configured. |
| Linux facilities inspectable | Target commands not executable | BLOCKED | systemd/D-Bus/kernel unknown. |
| No progression beyond Layer 0 | File/content review | PASS | None. |

## Required next action

Verify the guest host key through the trusted UTM console:

```sh
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

On macOS, obtain the presented key without installing it and compare its fingerprint to the trusted console value:

```sh
ssh-keyscan -t ed25519 192.168.64.2 | ssh-keygen -lf -
```

Only after the fingerprints match, add that key to the macOS user's `~/.ssh/known_hosts` outside this repository. Configure an Ed25519 client key and its matching Ubuntu `authorized_keys` entry outside the repository; do not send a password or private key in chat. Then rerun or authorize retrying the required batch-mode SSH command.

## Continuation commands executed

```sh
/Applications/UTM.app/Contents/MacOS/utmctl list
/Applications/UTM.app/Contents/MacOS/utmctl status 76EF4ED8-1A57-4699-A01A-42194D555909
/Applications/UTM.app/Contents/MacOS/utmctl ip-address 76EF4ED8-1A57-4699-A01A-42194D555909
plutil -p "$HOME/Library/Containers/com.utmapp.UTM/Data/Documents/Linux.utm/config.plist"
arp -an
ndp -an
ifconfig
netstat -rn -f inet
nc -vz -G 3 <each exact discovered guest IPv6 address> 22
```

No Linux-side command, dependency installation, repository synchronization, or test was performed because authentication is blocked.

The final requested connectivity command was also executed:

```sh
ssh -o BatchMode=yes -o ConnectTimeout=10 ubuntu@192.168.64.2 'whoami && uname -m'
```

The sandboxed attempt exited 255 with `Operation not permitted`. The approved host-network retry exited 255 with `Host key verification failed.` Host-key checking remained enabled; no SSH state or credential was changed.

**STOP: do not start Layer 1.**
