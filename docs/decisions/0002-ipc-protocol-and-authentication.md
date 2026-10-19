# ADR 0002: Local IPC Protocol and Authentication

- Status: Proposed (Stage 01). To be confirmed in Stage 06 on Ubuntu.
- Date: 2026-10-19
- Requirements: SEC-001, SEC-002, SEC-005, FR-032, NFR-005

## Context

The GUI and CLI must control a user-level daemon. ADR 0001 already chose a Unix-domain socket. Still to be decided: how messages are framed, how requests and responses are shaped, and how clients authenticate without adding secrets or dependencies.

## Decision

- **Transport:** `AF_UNIX` `SOCK_STREAM` at `$XDG_RUNTIME_DIR/napguard/daemon.sock`. The directory is 0700 and the socket 0600.
- **Framing:** a 4-byte big-endian length prefix followed by one UTF-8 JSON object. The maximum is 1 MiB.
- **Envelopes:** versioned (`"v": 1`). Requests and responses are matched by a client-chosen `id`, and errors use stable codes ([IPC and security design](../architecture/ipc-security.md)).
- **Authentication:** kernel peer credentials (`SO_PEERCRED`). Only the daemon's own UID is accepted. There are no tokens or passwords.
- **Long-running operations:** they return `ACCEPTED` with an operation ID, and progress arrives as events on a subscription.

## Alternatives considered

- **Newline-delimited JSON:** simpler, but a newline inside a payload breaks a frame and the maximum size is awkward to enforce. Rejected.
- **D-Bus session bus API:** standard on desktops, but it adds a dependency and couples the daemon to a session bus that may be missing at boot for a user service. Rejected for v1.
- **HTTP over a Unix socket:** heavier, with no benefit for a local, single-user API. Rejected.
- **Token file authentication:** redundant given `SO_PEERCRED` with a 0700 directory, and it adds secret handling. Rejected.

## Consequences

- No runtime dependency is needed. The standard library's `socket`, `asyncio`, and `json` are enough.
- Only Linux semantics are supported (`SO_PEERCRED`). macOS can run unit tests of framing and validation only.
- Same-UID processes can use the API. This is accepted because they already hold equivalent authority over the user's processes.
