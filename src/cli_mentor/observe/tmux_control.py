"""tmux control-mode client.

Control mode (``tmux -C``) is tmux's text protocol for programs:

- We write tmux commands, one per line, on stdin.
- Each command produces one block: ``%begin <time> <num> <flags>``, output
  lines, then ``%end ...`` or ``%error ...`` with the same number.
- Between blocks tmux sends notifications such as ``%output %3 <data>``.
  In ``<data>``, bytes below 32 and the backslash are written as ``\\ooo``
  (three octal digits). Other bytes are raw, so UTF-8 characters can be split
  across two notifications; we decode per pane with an incremental decoder.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from ..tmuxutil import Tmux, clean_env

log = logging.getLogger(__name__)

_BLOCK = re.compile(rb"^%(begin|end|error) (\d+) (\d+) (\d+)$")
_INLINE_FLOW = re.compile(r"^%(pause|continue) (%\d+)$")


def decode_octal(value: bytes) -> bytes:
    """Undo tmux's ``\\ooo`` escaping."""
    if b"\\" not in value:
        return value
    out = bytearray()
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch == 0x5C and i + 3 < n + 1 and value[i + 1 : i + 4].isdigit():
            out.append(int(value[i + 1 : i + 4], 8) & 0xFF)
            i += 4
        else:
            out.append(ch)
            i += 1
    return bytes(out)


@dataclass(frozen=True)
class Notification:
    name: str  # e.g. "output", "layout-change", "exit"
    args: bytes  # raw remainder of the line


@dataclass
class Response:
    number: int
    ok: bool
    lines: list[str] = field(default_factory=list)


class ControlParser:
    """Turns control-mode lines into notifications and command responses."""

    def __init__(self) -> None:
        self._block: tuple[int, list[bytes]] | None = None

    def feed(self, line: bytes) -> Notification | Response | None:
        line = line.rstrip(b"\n").removesuffix(b"\r")
        if self._block is not None:
            match = _BLOCK.match(line)
            if match and match.group(1) in (b"end", b"error") and int(match.group(3)) == self._block[0]:
                number, lines = self._block
                self._block = None
                return Response(
                    number,
                    ok=match.group(1) == b"end",
                    lines=[raw.decode("utf-8", "replace") for raw in lines],
                )
            self._block[1].append(line)
            return None
        match = _BLOCK.match(line)
        if match and match.group(1) == b"begin":
            self._block = (int(match.group(3)), [])
            return None
        if line.startswith(b"%"):
            name, _, rest = line[1:].partition(b" ")
            return Notification(name.decode("ascii", "replace"), rest)
        return None


def quote_arg(arg: str) -> str:
    if "'" in arg or "\n" in arg:
        raise ValueError("tmux arguments with quotes or newlines are not allowed")
    return "'" + arg + "'"


OutputHandler = Callable[[str, str, int | None], None]  # pane_id, text, buffered_ms
NotifyHandler = Callable[[Notification], None]


class TmuxControlClient:
    """One control-mode connection to one session on our tmux server."""

    def __init__(
        self,
        tmux: Tmux,
        session: str,
        *,
        on_output: OutputHandler,
        on_notify: NotifyHandler,
        pause_after: int = 5,
        ignored_panes: set[str] | None = None,
    ) -> None:
        self.tmux = tmux
        self.session = session
        self.on_output = on_output
        self.on_notify = on_notify
        self.pause_after = pause_after
        self.ignored_panes = ignored_panes if ignored_panes is not None else set()
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: deque[tuple[asyncio.Future[Response], str]] = deque()
        self._parser = ControlParser()
        self._decoders: dict[str, codecs.IncrementalDecoder] = {}
        self._lock = asyncio.Lock()
        self.closed = asyncio.Event()
        self.exit_reason = ""

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and not self.closed.is_set()

    async def start(self) -> None:
        flags = f"ignore-size,pause-after={self.pause_after}" if self.pause_after else "ignore-size"
        argv = self.tmux.argv("-C", "attach-session", "-t", f"={self.session}", "-f", flags)
        loop = asyncio.get_running_loop()
        attach_done: asyncio.Future[Response] = loop.create_future()
        self._pending.append((attach_done, "attach-session"))  # the attach command's own block
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=clean_env(),
            limit=8 * 1024 * 1024,
        )
        self._reader = asyncio.create_task(self._read_loop(), name="tmux-control-reader")
        try:
            response = await asyncio.wait_for(attach_done, 5)
        except TimeoutError:
            await self.stop()
            raise RuntimeError("tmux control client did not attach") from None
        if not response.ok:
            await self.stop()
            raise RuntimeError(f"tmux control attach failed: {' '.join(response.lines)}")

    async def stop(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                assert proc.stdin is not None
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except TimeoutError:
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        self._fail_pending("control client stopped")
        self.closed.set()

    async def command(self, *args: str, timeout: float = 5.0) -> Response:
        if not self.running or self._proc is None or self._proc.stdin is None:
            raise RuntimeError("tmux control client is not running")
        line = " ".join(quote_arg(a) if i else a for i, a in enumerate(args)) + "\n"
        async with self._lock:
            future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
            self._pending.append((future, args[0]))
            self._proc.stdin.write(line.encode())
            await self._proc.stdin.drain()
        return await asyncio.wait_for(future, timeout)

    async def capture(self, pane_id: str, history: int = 200) -> list[str]:
        response = await self.command("capture-pane", "-p", "-J", "-t", pane_id, "-S", f"-{history}")
        if not response.ok:
            raise RuntimeError(f"capture-pane failed: {' '.join(response.lines)}")
        lines = list(response.lines)
        while lines and not lines[-1].strip():
            lines.pop()
        return lines

    async def continue_pane(self, pane_id: str) -> None:
        await self.command("refresh-client", "-A", f"{pane_id}:continue")

    # ------------------------------------------------------------ internals
    def _fail_pending(self, reason: str) -> None:
        while self._pending:
            fut, _name = self._pending.popleft()
            if not fut.done():
                fut.set_exception(RuntimeError(reason))

    def _complete(self, response: Response) -> None:
        if not self._pending:
            return
        fut, name = self._pending.popleft()
        if name == "refresh-client":
            # tmux emits %pause / %continue caused by ``refresh-client -A``
            # inside that command's reply block (observed with tmux 3.7).
            kept: list[str] = []
            for line in response.lines:
                match = _INLINE_FLOW.match(line)
                if match:
                    self._dispatch(Notification(match.group(1), match.group(2).encode()))
                else:
                    kept.append(line)
            response.lines = kept
        if not fut.done():
            fut.set_result(response)

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                item = self._parser.feed(line)
                if item is None:
                    continue
                if isinstance(item, Response):
                    self._complete(item)
                    continue
                self._dispatch(item)
        except Exception:  # pragma: no cover - defensive
            log.exception("tmux control reader crashed")
        finally:
            self.closed.set()
            self._fail_pending("control connection closed")

    def _dispatch(self, note: Notification) -> None:
        if note.name in ("output", "extended-output"):
            pane_raw, _, rest = note.args.partition(b" ")
            pane = pane_raw.decode("ascii", "replace")
            if pane in self.ignored_panes:
                return  # e.g. the tutor's own pane: never observed
            buffered: int | None = None
            if note.name == "extended-output":
                head, sep, data = rest.partition(b" : ")
                if not sep:
                    return
                with contextlib.suppress(ValueError):
                    buffered = int(head.split(b" ")[0])
                rest = data
            decoder = self._decoders.get(pane)
            if decoder is None:
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                self._decoders[pane] = decoder
            text = decoder.decode(decode_octal(rest))
            if text:
                self.on_output(pane, text, buffered)
            return
        if note.name == "exit":
            self.exit_reason = note.args.decode("utf-8", "replace")
        self.on_notify(note)
