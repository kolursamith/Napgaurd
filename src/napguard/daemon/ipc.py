"""Stage 06 (MVP): local IPC over a Unix-domain socket (ADR 0002; SEC-001, SEC-002, SEC-005).

* `$XDG_RUNTIME_DIR/napguard/daemon.sock`: the directory is 0700 and the socket 0600,
  bound with umask 077, guarded by `flock(daemon.lock)`. There is never a TCP listener.
* Every connection is authenticated with the kernel's SO_PEERCRED: only this user's UID
  is served; anything else gets one UNAUTHENTICATED frame and is closed.
* Frames: a 4-byte big-endian length, then one UTF-8 JSON object, at most 1 MiB.
* Envelopes: {"v": 1, "id", "method", "params"} -> {"v": 1, "id", "ok", "result" | "error"}.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import stat
import struct
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

VERSION = 1
MAX_FRAME = 1 << 20
MAX_CONNECTIONS = 16
IDLE_TIMEOUT_S = 300.0


class IpcError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None if not buf else bytes(buf)
        buf += chunk
    return bytes(buf)


def send_frame(sock: socket.socket, obj: dict[str, Any]) -> None:
    data = json.dumps(obj, default=str).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise IpcError("FRAME_TOO_LARGE", f"frame of {len(data)} bytes")
    sock.sendall(struct.pack(">I", len(data)) + data)


def recv_frame(sock: socket.socket) -> dict[str, Any] | None:
    head = _recv_exact(sock, 4)
    if head is None:
        return None
    if len(head) < 4:
        raise IpcError("INVALID_ARGUMENT", "truncated frame header")
    (length,) = struct.unpack(">I", head)
    if length > MAX_FRAME:
        raise IpcError("FRAME_TOO_LARGE", f"frame of {length} bytes exceeds {MAX_FRAME}")
    body = _recv_exact(sock, length) if length else b""
    if body is None or len(body) < length:
        raise IpcError("INVALID_ARGUMENT", "truncated frame")
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise IpcError("INVALID_ARGUMENT", f"frame is not JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise IpcError("INVALID_ARGUMENT", "frame must be a JSON object")
    return obj


def peer_uid(sock: socket.socket) -> int:
    """The connecting process's UID from the kernel (SO_PEERCRED; Linux only)."""
    opt = getattr(socket, "SO_PEERCRED", None)
    if opt is None:
        raise IpcError("UNAUTHENTICATED", "SO_PEERCRED is not available on this platform")
    _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, opt,
                                                          struct.calcsize("3i")))
    return int(uid)


def default_socket_path() -> Path:
    """`$XDG_RUNTIME_DIR/napguard/daemon.sock`, after checking the runtime directory."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        raise IpcError("INSECURE_PATH", "XDG_RUNTIME_DIR is not set (no systemd user session?)")
    st = os.stat(base)
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise IpcError("INSECURE_PATH", f"{base} must be owned by this user with mode 0700")
    return Path(base) / "napguard" / "daemon.sock"


class Server:
    def __init__(self, sock_path: Path, handler: Callable[[str, dict[str, Any]], Any],
                 audit: Callable[[str, dict[str, Any]], None] = lambda kind, data: None,
                 peer: Callable[[socket.socket], int] = peer_uid) -> None:
        self.sock_path = sock_path
        self.handler = handler
        self.audit = audit
        self.peer = peer
        self._sock: socket.socket | None = None
        self._lock_fd: int | None = None
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def bind(self) -> None:
        directory = self.sock_path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = os.lstat(directory)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise IpcError("INSECURE_PATH", f"{directory} must be a 0700 directory of this user")
        self._lock_fd = os.open(directory / "daemon.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                                0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise IpcError("ALREADY_RUNNING", "another NapGuard daemon holds the lock") from exc
        if self.sock_path.exists() or self.sock_path.is_symlink():
            self.sock_path.unlink()  # stale: we hold the lock
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o077)
        try:
            sock.bind(str(self.sock_path))
        finally:
            os.umask(old)
        os.chmod(self.sock_path, 0o600)
        sock.listen(MAX_CONNECTIONS)
        sock.settimeout(0.5)
        self._sock = sock

    def serve_forever(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                raise
            if not self._slots.acquire(blocking=False):
                self._reject(conn, "BUSY", "too many connections")
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _reject(self, conn: socket.socket, code: str, message: str) -> None:
        try:
            send_frame(conn, {"v": VERSION, "id": None, "ok": False,
                              "error": {"code": code, "message": message}})
        except OSError:
            pass
        conn.close()

    def _serve(self, conn: socket.socket) -> None:
        try:
            try:
                uid = self.peer(conn)
            except (OSError, IpcError) as exc:
                self.audit("auth.rejected", {"reason": str(exc)})
                self._reject(conn, "UNAUTHENTICATED", "peer credentials unavailable")
                return
            if uid != os.getuid():
                self.audit("auth.rejected", {"peer_uid": uid})
                self._reject(conn, "UNAUTHENTICATED", "this daemon serves only its own user")
                return
            conn.settimeout(IDLE_TIMEOUT_S)
            while not self._stop.is_set():
                try:
                    request = recv_frame(conn)
                except IpcError as exc:
                    self._reject(conn, exc.code, exc.message)
                    return
                except (TimeoutError, OSError):
                    return
                if request is None:
                    return
                send_frame(conn, self._respond(request))
        finally:
            conn.close()
            self._slots.release()

    def _respond(self, request: dict[str, Any]) -> dict[str, Any]:
        rid = request.get("id")
        try:
            if request.get("v") != VERSION:
                raise IpcError("UNSUPPORTED_VERSION", f"expected v={VERSION}")
            method, params = request.get("method"), request.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise IpcError("INVALID_ARGUMENT", "method must be a string, params an object")
            return {"v": VERSION, "id": rid, "ok": True, "result": self.handler(method, params)}
        except IpcError as exc:
            error = {"code": exc.code, "message": exc.message, "details": exc.details}
        except Exception as exc:  # mapped by the service; anything else is INTERNAL
            code = str(getattr(exc, "code", "INTERNAL"))
            error = {"code": code, "message": str(getattr(exc, "message", repr(exc))),
                     "details": getattr(exc, "detail", {})}
        return {"v": VERSION, "id": rid, "ok": False, "error": error}

    def close(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()
            self._sock = None
            try:
                self.sock_path.unlink()
            except FileNotFoundError:
                pass
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None


class Client:
    def __init__(self, sock_path: Path, timeout: float = 30.0) -> None:
        self.sock_path = sock_path
        self.timeout = timeout
        self._next = 0

    def call(self, method: str, **params: Any) -> Any:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            try:
                sock.connect(str(self.sock_path))
            except OSError as exc:
                raise IpcError("DAEMON_UNAVAILABLE",
                               f"cannot reach the NapGuard daemon at {self.sock_path}: "
                               f"{exc.strerror or exc}") from exc
            self._next += 1
            send_frame(sock, {"v": VERSION, "id": self._next, "method": method, "params": params})
            reply = recv_frame(sock)
        finally:
            sock.close()
        if reply is None:
            raise IpcError("DAEMON_UNAVAILABLE", "the daemon closed the connection")
        if not reply.get("ok"):
            err = reply.get("error") or {}
            raise IpcError(str(err.get("code", "INTERNAL")), str(err.get("message", "")),
                           err.get("details") or {})
        return reply.get("result")
