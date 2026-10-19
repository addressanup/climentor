"""Run learner code on the host, with limits.

THIS IS NOT A SANDBOX. Learner code runs as the learner, on this Mac, and can
do anything the learner's account can do. We reduce accidents with:

- an explicit permission ("run learner code") before anything runs;
- ``python -I`` (ignore PYTHON* env vars and user site-packages);
- a minimal environment (no API keys, no tokens, HOME = exercise folder);
- CPU-time, file-size, and open-file limits (``resource.setrlimit``);
- a wall-clock timeout, after which the whole process group is killed;
- output truncated to a fixed size.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT = 64 * 1024

# Applied in the child before exec. Runs with the same trusted interpreter.
_LIMITS_LAUNCHER = """
import os, resource, sys
cpu, fsize, nofile = (int(x) for x in sys.argv[1:4])
resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
os.execv(sys.argv[4], sys.argv[4:])
"""


@dataclass(frozen=True)
class RunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def find_python() -> str:
    return shutil.which("python3") or sys.executable


class HostRunner:
    label = "host execution (not a sandbox)"

    def __init__(
        self,
        python: str | None = None,
        timeout: float = 10.0,
        cpu_seconds: int = 5,
        max_file_bytes: int = 5 * 1024 * 1024,
        max_open_files: int = 64,
    ) -> None:
        self.python = python or find_python()
        self.timeout = timeout
        self.cpu_seconds = cpu_seconds
        self.max_file_bytes = max_file_bytes
        self.max_open_files = max_open_files

    def environment(self, cwd: Path) -> dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:" + str(Path(self.python).parent),
            "HOME": str(cwd),
            "LANG": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
        }

    async def run_python(
        self, args: list[str], cwd: Path, stdin: str | None = None, *, isolated: bool = True
    ) -> RunResult:
        # -I: fully isolated (script folder not on sys.path). -E -s: ignore PYTHON*
        # variables and user site-packages, but let a test file import its neighbour.
        flags = ["-I"] if isolated else ["-E", "-s"]
        argv = [
            sys.executable,
            "-I",
            "-c",
            _LIMITS_LAUNCHER,
            str(self.cpu_seconds),
            str(self.max_file_bytes),
            str(self.max_open_files),
            self.python,
            *flags,
            *args,
        ]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=self.environment(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin.encode() if stdin is not None else b""), self.timeout
            )
        except TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            out, err = await proc.communicate()
        finally:
            # Clean up anything the learner's code left running in its group.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        return RunResult(
            exit_code=None if timed_out else proc.returncode,
            stdout=out[:MAX_OUTPUT].decode("utf-8", "replace"),
            stderr=err[:MAX_OUTPUT].decode("utf-8", "replace"),
            timed_out=timed_out,
        )
