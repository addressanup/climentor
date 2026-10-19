"""Private local IPC: newline-delimited JSON over a Unix socket.

Protection layers:
1. Socket lives in a 0700 directory and is chmod 0600.
2. Peer user id must match ours (where the OS lets us check).
3. First line must be a hello with the 32-byte session token
   (compared in constant time).
4. Every later line is size-limited, rate-limited, and schema-validated by the
   engine. The client's identity comes from the hello, not from each message.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import socket
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..events.schema import SourceKind, SourceRef

log = logging.getLogger(__name__)

CLIENT_KINDS = frozenset({SourceKind.SHELL, SourceKind.NVIM})
HELLO_TIMEOUT = 5.0
MAX_LINE = 2 * 1024 * 1024


@dataclass(frozen=True)
class ClientIdentity:
    kind: SourceKind
    id: str
    epoch: str
    conn_id: int
    peer_uid: int | None

    def source_ref(self) -> SourceRef:
        return SourceRef(kind=self.kind, id=self.id, epoch=self.epoch)


MessageHandler = Callable[[ClientIdentity, dict[str, Any]], None]
ConnectHandler = Callable[[ClientIdentity, bool], None]
HelloExtra = Callable[[ClientIdentity], dict[str, Any]]


def peer_uid(sock: Any) -> int | None:
    """Return the user id of the process on the other end, if the OS tells us."""
    try:
        if str(getattr(sys, "platform", "")) == "darwin":
            # SOL_LOCAL = 0, LOCAL_PEERCRED = 1 -> struct xucred
            data = sock.getsockopt(0, 1, 76)
            _version, uid = struct.unpack_from("=II", data, 0)
            return int(uid)
        if hasattr(socket, "SO_PEERCRED"):
            data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", data)
            return int(uid)
    except OSError:
        return None
    return None


class RateLimiter:
    def __init__(self, per_second: float, burst: float) -> None:
        self.rate = per_second
        self.capacity = burst
        self.tokens = burst
        self.last = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class IpcServer:
    def __init__(
        self,
        socket_path: Path,
        token: str,
        on_message: MessageHandler,
        *,
        on_connect: ConnectHandler | None = None,
        hello_extra: HelloExtra | None = None,
        rate_per_second: float = 400.0,
        rate_burst: float = 2000.0,
    ) -> None:
        if len(token) < 32:
            raise ValueError("token too short")
        self.socket_path = socket_path
        self._token = token.encode()
        self.on_message = on_message
        self.on_connect = on_connect
        self.hello_extra = hello_extra
        self.rate_per_second = rate_per_second
        self.rate_burst = rate_burst
        self._server: asyncio.base_events.Server | None = None
        self._inode: int | None = None
        self._next_conn = 0
        self._clients: dict[int, tuple[ClientIdentity, asyncio.StreamWriter]] = {}
        self.rejected = 0
        self.rate_limited = 0

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.socket_path.exists() or self.socket_path.is_symlink():
            if await _socket_alive(self.socket_path):
                raise RuntimeError(f"Another process is already listening on {self.socket_path}")
            log.info("removing stale socket %s", self.socket_path)
            self.socket_path.unlink()
        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle, path=str(self.socket_path), limit=MAX_LINE
            )
        finally:
            os.umask(old_umask)
        self.socket_path.chmod(0o600)
        self._inode = self.socket_path.stat().st_ino

    async def stop(self) -> None:
        for _ident, writer in list(self._clients.values()):
            writer.close()
        self._clients.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
            self._server = None
        # Only remove the socket if it is still the one we created.
        with contextlib.suppress(FileNotFoundError):
            if self._inode is not None and self.socket_path.stat().st_ino == self._inode:
                self.socket_path.unlink()

    # ------------------------------------------------------------ sending
    async def send(self, kind: SourceKind, message: dict[str, Any]) -> int:
        """Send a control message to every connected client of ``kind``."""
        data = (json.dumps(message) + "\n").encode()
        sent = 0
        for ident, writer in list(self._clients.values()):
            if ident.kind != kind:
                continue
            try:
                writer.write(data)
                await writer.drain()
                sent += 1
            except (ConnectionError, RuntimeError):
                self._clients.pop(ident.conn_id, None)
        return sent

    def connected(self, kind: SourceKind | None = None) -> list[ClientIdentity]:
        return [i for i, _w in self._clients.values() if kind is None or i.kind == kind]

    # ------------------------------------------------------------ handling
    def _reject(self, reason: str, writer: asyncio.StreamWriter) -> None:
        self.rejected += 1
        log.warning("IPC connection rejected: %s", reason)
        writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        uid = peer_uid(sock) if sock is not None else None
        if uid is not None and uid != os.getuid():
            self._reject("peer belongs to another user", writer)
            return
        try:
            line = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT)
        except (TimeoutError, ValueError, ConnectionError):
            self._reject("no hello", writer)
            return
        ident = self._parse_hello(line, uid)
        if ident is None:
            self._reject("bad hello or token", writer)
            return
        self._clients[ident.conn_id] = (ident, writer)
        if self.on_connect:
            self.on_connect(ident, True)
        try:
            if ident.kind == SourceKind.NVIM:
                reply: dict[str, Any] = {"ok": True}
                if self.hello_extra:
                    reply.update(self.hello_extra(ident))
                writer.write((json.dumps(reply) + "\n").encode())
                await writer.drain()
            limiter = RateLimiter(self.rate_per_second, self.rate_burst)
            while True:
                try:
                    raw = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    log.warning("IPC message too large from %s; closing", ident.kind.value)
                    break
                if not raw:
                    break
                if not limiter.allow():
                    self.rate_limited += 1
                    continue
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    log.warning("IPC: invalid JSON from %s", ident.kind.value)
                    continue
                if isinstance(msg, dict):
                    self.on_message(ident, msg)
        except ConnectionError:
            pass
        finally:
            self._clients.pop(ident.conn_id, None)
            if self.on_connect:
                self.on_connect(ident, False)
            writer.close()

    def _parse_hello(self, line: bytes, uid: int | None) -> ClientIdentity | None:
        try:
            hello = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(hello, dict) or hello.get("v") != 1:
            return None
        token = hello.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token.encode(), self._token):
            return None
        try:
            kind = SourceKind(str(hello.get("kind")))
            if kind not in CLIENT_KINDS:
                return None
            ref = SourceRef(kind=kind, id=str(hello.get("id")), epoch=str(hello.get("epoch")))
        except (ValueError, ValidationError):
            return None
        self._next_conn += 1
        return ClientIdentity(ref.kind, ref.id, ref.epoch, self._next_conn, uid)


async def _socket_alive(path: Path) -> bool:
    try:
        _r, w = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), 1.0)
    except (OSError, TimeoutError):
        return False
    w.close()
    return True


def new_token() -> str:
    import secrets as _secrets

    return _secrets.token_hex(32)
