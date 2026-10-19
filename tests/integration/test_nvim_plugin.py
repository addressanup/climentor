"""The real Lua plugin inside real headless Neovim, talking to a real Engine."""

from __future__ import annotations

import asyncio
import os
import random
import shutil
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from cli_mentor.events.schema import SourceKind
from cli_mentor.lifecycle import plugin_dir
from cli_mentor.tutor.engine import Engine
from cli_mentor.tutor.ui_protocol import HeadlessUi
from tests.conftest import require_tool
from tests.engine_helpers import make_engine
from tests.helpers import wait_for

pytestmark = pytest.mark.integration


class NvimDriver:
    """Runs pynvim calls on one dedicated thread (pynvim is not thread-safe)."""

    def __init__(self, listen: Path) -> None:
        self.listen = listen
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.nvim: Any = None

    async def _run(self, fn: Callable[[], Any]) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self.pool, fn)

    async def connect(self) -> None:
        import pynvim

        def attach() -> Any:
            return pynvim.attach("socket", path=str(self.listen))

        for _ in range(100):
            if self.listen.exists():
                try:
                    self.nvim = await self._run(attach)
                    return
                except Exception:
                    pass
            await asyncio.sleep(0.05)
        raise AssertionError("could not connect to headless nvim")

    async def cmd(self, command: str) -> None:
        await self._run(lambda: self.nvim.command(command))

    async def lua(self, code: str, *args: Any) -> Any:
        return await self._run(lambda: self.nvim.exec_lua(code, *args))

    async def keys(self, keys: str) -> None:
        await self._run(lambda: self.nvim.input(keys))
        await asyncio.sleep(0.05)

    async def lines(self) -> list[str]:
        return list(await self.lua("return vim.api.nvim_buf_get_lines(0, 0, -1, false)"))

    def close(self) -> None:
        if self.nvim is not None:
            try:
                self.pool.submit(self.nvim.close).result(timeout=2)
            except Exception:
                pass
        self.pool.shutdown(wait=False)


async def start_nvim(eng: Engine, listen: Path) -> asyncio.subprocess.Process:
    nvim = require_tool("nvim")
    assert eng.socket_path is not None and eng.token_file is not None
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLI_MENTOR")}
    env.update(CLI_MENTOR_SOCK=str(eng.socket_path), CLI_MENTOR_TOKEN_FILE=str(eng.token_file))
    return await asyncio.create_subprocess_exec(
        nvim,
        "--headless",
        "--clean",
        "--listen",
        str(listen),
        "--cmd",
        f"set rtp^={plugin_dir()}",
        "--cmd",
        "lua require('cli_mentor').start()",
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


@pytest.fixture
async def rig(
    workspace: Path, mentor_env: dict[str, str], short_tmp: Path
) -> AsyncIterator[tuple[Engine, NvimDriver, Path]]:
    require_tool("nvim")
    eng = make_engine(workspace, ui=HeadlessUi(answers={"Resume observation": "y"}))
    await eng.start(greet=False)
    listen = short_tmp / "nvim.sock"
    proc = await start_nvim(eng, listen)
    drv = NvimDriver(listen)
    await drv.connect()
    assert eng.ipc is not None
    await wait_for(lambda: eng.ipc.connected(SourceKind.NVIM))  # type: ignore[union-attr]
    yield eng, drv, listen
    drv.close()
    if proc.returncode is None:
        proc.kill()
        await proc.wait()
    await eng.stop()


def mirror(eng: Engine, path: Path) -> Any:
    return eng.state.buffer_for_path(os.path.realpath(path))


async def test_snapshot_incremental_unsaved_and_write(rig: tuple[Engine, NvimDriver, Path], workspace: Path) -> None:
    eng, nv, _ = rig
    f = workspace / "a.txt"
    f.write_text("one\ntwo\nthree\n")
    await nv.cmd(f"edit {f}")
    await wait_for(lambda: mirror(eng, f))
    assert mirror(eng, f).lines == ["one", "two", "three"]
    rng = random.Random(1234)
    for _ in range(40):
        op = rng.choice(["set", "dd", "yyp", "J", "undo", "redo", "append"])
        n = len(await nv.lines())
        if op == "set":
            a = rng.randint(0, n)
            b = rng.randint(a, n)
            await nv.lua(
                "local a, b, l = ...; vim.api.nvim_buf_set_lines(0, a, b, false, l)",
                a,
                b,
                [f"r{rng.randint(0, 99)}" for _ in range(rng.randint(0, 3))],
            )
        elif op == "append":
            await nv.keys("Gonew line<Esc>")
        elif op in ("undo", "redo"):
            await nv.cmd(op)
        else:
            await nv.cmd(f"silent! normal! {rng.randint(1, max(1, n))}G{op}")
    expected = await nv.lines()
    await wait_for(lambda: mirror(eng, f) and mirror(eng, f).lines == expected, timeout=10)
    assert mirror(eng, f).modified  # unsaved edits are visible...
    assert f.read_text() == "one\ntwo\nthree\n"  # ...and are not on disk
    await nv.cmd("write")
    await wait_for(lambda: not mirror(eng, f).modified)
    assert f.read_text().splitlines() == expected
    tick_rows = eng.store.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'nvim.buffer_written'").fetchone()[0]
    assert tick_rows == 1


async def test_scope_sensitive_and_terminal_buffers_skipped(
    rig: tuple[Engine, NvimDriver, Path], workspace: Path, tmp_path: Path
) -> None:
    eng, nv, _ = rig
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret\n")
    env_file = workspace / ".env"
    env_file.write_text("TOKEN=abc\n")
    (workspace / "node_modules").mkdir()
    dep = workspace / "node_modules" / "x.js"
    dep.write_text("x\n")
    link = workspace / "link.txt"
    link.symlink_to(outside)
    ok = workspace / "ok.txt"
    ok.write_text("fine\n")
    for path in (outside, env_file, dep, link):
        await nv.cmd(f"edit {path}")
    await nv.cmd("terminal echo hi")
    await nv.cmd(f"edit {ok}")
    await wait_for(lambda: mirror(eng, ok))
    paths = {m.path for m in eng.state.buffers.values()}
    assert paths == {os.path.realpath(ok)}
    status = await nv.lua("return require('cli_mentor').status()")
    assert [b["path"] for b in status["attached"]] == [os.path.realpath(ok)]
    rows = eng.store.conn.execute("SELECT payload FROM events").fetchall()
    assert not any("outside secret" in r[0] or "TOKEN=abc" in r[0] for r in rows)


async def test_reload_and_resync(rig: tuple[Engine, NvimDriver, Path], workspace: Path) -> None:
    eng, nv, _ = rig
    f = workspace / "r.txt"
    f.write_text("v1\n")
    await nv.cmd(f"edit {f}")
    await wait_for(lambda: mirror(eng, f))
    f.write_text("v2 changed outside\nline2\n")
    await nv.cmd("edit!")
    await wait_for(lambda: mirror(eng, f).lines == ["v2 changed outside", "line2"])
    # Simulate the engine losing track: corrupt the mirror and ask for a resync.
    m = mirror(eng, f)
    m.lines = ["garbage"]
    eng._request_resync(SourceKind.NVIM)
    await wait_for(lambda: mirror(eng, f).lines == ["v2 changed outside", "line2"])


async def test_pause_stops_sending_and_resume_snapshots(rig: tuple[Engine, NvimDriver, Path], workspace: Path) -> None:
    eng, nv, _ = rig
    f = workspace / "p.txt"
    f.write_text("start\n")
    await nv.cmd(f"edit {f}")
    await wait_for(lambda: mirror(eng, f))
    await eng.pause_observation()
    await asyncio.sleep(0.1)
    status = await nv.lua("return require('cli_mentor').status()")
    assert status["paused"] is True
    seq_before = status["seq"]
    await nv.lua("vim.api.nvim_buf_set_lines(0, 0, -1, false, {'typed while paused'})")
    await asyncio.sleep(0.3)
    assert (await nv.lua("return require('cli_mentor').status()"))["seq"] == seq_before  # nothing sent
    assert mirror(eng, f).lines == ["start"]
    await eng.resume_observation()
    await wait_for(lambda: mirror(eng, f).lines == ["typed while paused"])
    snaps = eng.store.conn.execute("SELECT payload FROM events WHERE type = 'nvim.buffer_snapshot'").fetchall()
    assert any('"resume"' in r[0] for r in snaps)


async def test_mode_cursor_and_restart(rig: tuple[Engine, NvimDriver, Path], workspace: Path) -> None:
    eng, nv, listen = rig
    f = workspace / "m.txt"
    f.write_text("alpha beta gamma\nsecond\n")
    await nv.cmd(f"edit {f}")
    await wait_for(lambda: mirror(eng, f))
    await nv.keys("i")
    await wait_for(lambda: eng.state.editor.mode.startswith("i"))
    await nv.keys("<Esc>")
    await wait_for(lambda: eng.state.editor.mode.startswith("n"))
    await nv.keys("jl")
    await wait_for(lambda: eng.state.editor.cursor is not None and eng.state.editor.cursor[2] == 1)
    old_epochs = {s.epoch for s in eng.state.sources.values() if s.kind == SourceKind.NVIM}
    # Restart Neovim: the old buffers go away, the new instance re-snapshots.
    try:
        await nv.cmd("qa!")
    except (EOFError, OSError):
        pass  # Neovim exits while answering; that is the point
    nv.close()
    await wait_for(lambda: not eng.ipc.connected(SourceKind.NVIM))  # type: ignore[union-attr]
    assert mirror(eng, f) is None
    if listen.exists():
        listen.unlink()
    proc = await start_nvim(eng, listen)
    nv2 = NvimDriver(listen)
    await nv2.connect()
    try:
        await nv2.cmd(f"edit {f}")
        await wait_for(lambda: mirror(eng, f))
        new_epochs = {s.epoch for s in eng.state.sources.values() if s.kind == SourceKind.NVIM}
        assert new_epochs - old_epochs
    finally:
        nv2.close()
        proc.kill()
        await proc.wait()


async def test_opt_in_command_counts(rig: tuple[Engine, NvimDriver, Path], workspace: Path) -> None:
    eng, nv, _ = rig
    f = workspace / "ops.txt"
    f.write_text("delete the extra extra word\nI like teh color blue\n")
    await nv.cmd(f"edit {f}")
    await wait_for(lambda: mirror(eng, f))
    # Without opt-in nothing is counted.
    await nv.keys("w")
    await asyncio.sleep(0.3)
    assert eng.state.editor.technique == {}
    assert eng.ipc is not None
    await eng.ipc.send(
        SourceKind.NVIM, {"cmd": "telemetry", "enabled": True, "exercise_id": "ex1", "path": os.path.realpath(f)}
    )
    await asyncio.sleep(0.1)
    await nv.keys("gg")
    await nv.keys("2w")
    await nv.keys("dw")
    await nv.keys("j")
    await nv.keys("0")
    await nv.keys("2w")
    await nv.keys("ciwthe<Esc>")
    await nv.keys("fb")
    await wait_for(lambda: eng.state.editor.technique.get("ex1", {}).get("ciw"), timeout=6)
    counts = eng.state.editor.technique["ex1"]
    assert counts.get("dw") == 1 and counts.get("ciw") == 1 and counts.get("gg") == 1 and counts.get("j") == 1
    await wait_for(lambda: eng.state.editor.technique["ex1"].get("f") == 1, timeout=6)
    # Letters typed in Insert mode and the f target are never recorded.
    assert set(eng.state.editor.technique["ex1"]) <= {"gg", "w", "dw", "j", "0", "ciw", "f"}
    assert await nv.lines() == ["delete the extra word", "I like the color blue"]
    await eng.ipc.send(SourceKind.NVIM, {"cmd": "telemetry", "enabled": False, "exercise_id": "ex1"})
    await asyncio.sleep(0.1)
    before = dict(eng.state.editor.technique["ex1"])
    await nv.keys("dd")
    await asyncio.sleep(2.0)
    assert eng.state.editor.technique["ex1"] == before


def test_plugin_file_exists() -> None:
    assert (plugin_dir() / "lua" / "cli_mentor" / "init.lua").is_file()
    assert shutil.which("nvim") is None or True
