"""`mentor doctor`: check dependencies and settings. Never installs anything."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .apikeys import ENV_VARS, get_api_key, keychain_backend_name
from .config import load_config
from .paths import MAX_SOCKET_PATH, AppPaths


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # "OK" | "WARN" | "FAIL" | "INFO"
    detail: str
    fix: str = ""


def _version(cmd: list[str], pattern: str) -> tuple[int, ...] | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(pattern, out)
    if not match:
        return None
    return tuple(int(x) for x in match.group(1).split("."))


def run_checks(paths: AppPaths | None = None) -> list[CheckResult]:
    paths = paths or AppPaths.from_env()
    results: list[CheckResult] = []
    add = results.append

    py = sys.version_info
    add(
        CheckResult(
            "Python",
            "OK" if py >= (3, 12) else "FAIL",
            f"{py.major}.{py.minor}.{py.micro} at {sys.executable}",
            "" if py >= (3, 12) else "Install Python 3.12+: `brew install python@3.12`, then `uv sync`.",
        )
    )

    tmux = shutil.which("tmux")
    tv = _version(["tmux", "-V"], r"tmux (\d+\.\d+)") if tmux else None
    if tmux is None:
        add(CheckResult("tmux", "FAIL", "not found", "Install it: `brew install tmux`"))
    else:
        ok = tv is not None and tv >= (3, 2)
        add(
            CheckResult(
                "tmux",
                "OK" if ok else "FAIL",
                f"{'.'.join(map(str, tv or ()))} at {tmux}",
                "" if ok else "tmux 3.2+ is needed for control-mode flow control: `brew upgrade tmux`",
            )
        )

    nvim = shutil.which("nvim")
    nv = _version(["nvim", "--version"], r"NVIM v(\d+\.\d+)") if nvim else None
    if nvim is None:
        add(CheckResult("Neovim", "FAIL", "not found", "Install it: `brew install neovim`"))
    else:
        ok = nv is not None and nv >= (0, 10)
        add(
            CheckResult(
                "Neovim",
                "OK" if ok else "FAIL",
                f"{'.'.join(map(str, nv or ()))} at {nvim}",
                "" if ok else "Neovim 0.10+ is needed: `brew upgrade neovim`",
            )
        )

    zsh = shutil.which("zsh")
    if zsh is None:
        add(CheckResult("zsh", "FAIL", "not found", "macOS ships zsh at /bin/zsh; check your PATH."))
    else:
        probe = subprocess.run(
            [zsh, "-f", "-c", "zmodload zsh/net/socket && zmodload zsh/datetime"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        add(
            CheckResult(
                "zsh hooks",
                "OK" if probe.returncode == 0 else "FAIL",
                f"{zsh} (zsh/net/socket {'available' if probe.returncode == 0 else 'missing'})",
                ""
                if probe.returncode == 0
                else "Your zsh lacks the zsh/net/socket module; shell commands cannot be observed.",
            )
        )
    shell = os.environ.get("SHELL", "")
    if shell and not shell.endswith("zsh"):
        add(
            CheckResult(
                "Login shell",
                "INFO",
                f"{shell}",
                "The session shell pane uses zsh (bash hooks are not implemented yet).",
            )
        )

    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        add(CheckResult("SQLite FTS5", "OK", f"SQLite {sqlite3.sqlite_version}"))
    except sqlite3.Error as exc:
        add(CheckResult("SQLite FTS5", "FAIL", str(exc), "Use a Python built with FTS5 (Homebrew Python has it)."))

    try:
        paths.ensure()
        add(CheckResult("Folders", "OK", f"data {paths.data_dir} · config {paths.config_dir} (private, 0700)"))
    except Exception as exc:
        add(CheckResult("Folders", "FAIL", str(exc), "Fix the folder ownership/permissions shown above."))
    probe_sock = paths.runtime_dir / ("x" * 12) / "ipc.sock"
    ok = len(str(probe_sock).encode()) <= MAX_SOCKET_PATH
    add(
        CheckResult(
            "Socket path",
            "OK" if ok else "FAIL",
            f"{len(str(probe_sock))} bytes ({paths.runtime_dir})",
            "" if ok else "Set CLI_MENTOR_RUNTIME_DIR to a short private folder, e.g. /tmp/cm-$USER.",
        )
    )

    cfg_ok = paths.config_file.exists()
    try:
        config = load_config(paths.config_file)
    except Exception as exc:
        add(
            CheckResult(
                "Config", "FAIL", f"{paths.config_file}: {exc}", "Fix the file or delete it and run `mentor init`."
            )
        )
        return results
    if not cfg_ok or not config.initialized:
        add(CheckResult("Setup", "WARN", "not set up yet", "Run `mentor init`."))
    else:
        add(CheckResult("Setup", "OK", f"config at {paths.config_file}"))
        root = Path(config.workspace_root).expanduser()
        add(
            CheckResult(
                "Practice folder",
                "OK" if root.is_dir() else "FAIL",
                str(root),
                "" if root.is_dir() else "Run `mentor init` to create it (it asks first).",
            )
        )

    provider = config.tutor.provider
    settings = config.provider_settings()
    if provider == "mock":
        add(
            CheckResult(
                "Model provider",
                "INFO",
                "mock (offline, deterministic, not an AI)",
                "For a real tutor: `mentor init` and choose anthropic or openai.",
            )
        )
    else:
        lookup = get_api_key(provider)
        add(
            CheckResult(
                "API key",
                "OK" if lookup.key else "FAIL",
                f"{provider}: {'found in ' + lookup.source if lookup.key else 'not found'} (value never shown)",
                "" if lookup.key else f"Run `mentor key set {provider}` or export {ENV_VARS[provider]}.",
            )
        )
        add(
            CheckResult(
                "Model",
                "OK" if settings.model else "FAIL",
                settings.model or "not set",
                "" if settings.model else "Run `mentor init` to choose a model from the provider's list.",
            )
        )
    add(CheckResult("Keychain backend", "INFO", keychain_backend_name()))

    cxx = shutil.which("c++")
    add(CheckResult("C/C++ compiler", "OK" if cxx else "INFO", cxx or "not found",
                    "" if cxx else "Needed only for C/C++ plans: `xcode-select --install`"))
    learner_py = shutil.which("python3")
    lv = _version(["python3", "--version"], r"Python (\d+\.\d+)") if learner_py else None
    add(
        CheckResult(
            "python3 for exercises",
            "OK" if learner_py else "WARN",
            f"{'.'.join(map(str, lv or ()))} at {learner_py}" if learner_py else "not on PATH",
            "" if learner_py else "Python exercises need `python3` on your PATH.",
        )
    )
    if os.environ.get("TMUX"):
        add(
            CheckResult(
                "Inside tmux",
                "INFO",
                "you are inside tmux now",
                "`mentor start` will print how to attach from a new terminal (or use --nested).",
            )
        )
    return results


def format_results(results: list[CheckResult]) -> str:
    lines = []
    for r in results:
        lines.append(f"[{r.status:4}] {r.name}: {r.detail}")
        if r.fix:
            lines.append(f"       fix: {r.fix}")
    failed = sum(r.status == "FAIL" for r in results)
    lines.append("")
    lines.append(
        "All required checks passed." if not failed else f"{failed} problem(s) need fixing before `mentor start`."
    )
    return "\n".join(lines)
