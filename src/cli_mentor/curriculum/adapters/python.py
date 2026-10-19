"""Trusted verifiers for the Python fundamentals track.

These run learner code (host execution, not a sandbox). The engine only calls
them after the "run learner code" permission is granted.
"""

from __future__ import annotations

import ast
import json
import secrets
from typing import Any, ClassVar

from ..verify import Check, VerificationResult, VerifyContext, register

# The probe reads a one-time marker from stdin *before* running learner code,
# so learner output cannot fake the result line.
_PROBE = r"""
import json, runpy, sys
marker = sys.stdin.readline().strip()
path, names = sys.argv[1], sys.argv[2:]
sys.path.insert(0, ".")
result = {}
try:
    ns = runpy.run_path(path, run_name="__mentor_check__")
    for n in names:
        if n in ns:
            v = ns[n]
            result[n] = {"type": type(v).__name__, "repr": repr(v)[:300]}
    if "greet" in ns and callable(ns["greet"]):
        calls = {}
        for arg in ("Ada", ""):
            try:
                calls[arg] = {"ok": True, "value": repr(ns["greet"](arg))[:300]}
            except Exception as exc:
                calls[arg] = {"ok": False, "value": type(exc).__name__ + ": " + str(exc)[:200]}
        result["__greet_calls__"] = calls
except BaseException as exc:
    result["__error__"] = type(exc).__name__ + ": " + str(exc)[:300]
sys.stdout.write("\n" + marker + json.dumps(result) + "\n")
"""


def _parse(source: str | None) -> ast.Module | None:
    if source is None:
        return None
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


async def probe(ctx: VerifyContext, filename: str, names: list[str]) -> dict[str, Any]:
    assert ctx.runner is not None
    marker = "@@" + secrets.token_hex(12) + "@@"
    result = await ctx.runner.run_python(["-c", _PROBE, filename, *names], ctx.workdir, stdin=marker + "\n")
    if result.timed_out:
        return {"__error__": "timed out (infinite loop?)"}
    for line in reversed(result.stdout.splitlines()):
        if line.startswith(marker):
            data: dict[str, Any] = json.loads(line[len(marker) :])
            return data
    return {"__error__": (result.stderr.strip().splitlines() or ["no result"])[-1]}


@register
class ExpressionsVerifier:
    id: ClassVar[str] = "python.expressions.v1"
    executes_learner_code: ClassVar[bool] = True

    async def verify(self, ctx: VerifyContext) -> VerificationResult:
        rel = ctx.params.get("file", "expressions.py")
        expected: list[str] = ctx.params["expected_output"]
        source = ctx.read(rel)
        tree = _parse(source)
        checks: list[Check] = []
        if tree is None:
            checks.append(
                Check(
                    "File parses as Python",
                    False,
                    "missing file or syntax error — run it with python3 to see the error",
                    "static_analysis",
                )
            )
            return VerificationResult(self.id, False, checks)
        ops = {type(n.op).__name__ for n in ast.walk(tree) if isinstance(n, ast.BinOp)}
        for op, symbol in (("FloorDiv", "//"), ("Mod", "%"), ("Pow", "**")):
            checks.append(
                Check(
                    f"Uses {symbol}",
                    op in ops,
                    "found" if op in ops else f"no `{symbol}` operator in your code",
                    "static_analysis",
                )
            )
        assert ctx.runner is not None
        run = await ctx.runner.run_python([rel], ctx.workdir)
        got = run.stdout.strip().splitlines()
        ok = run.ok and got == expected
        detail = (
            "output matches"
            if ok
            else (
                "timed out"
                if run.timed_out
                else f"exit {run.exit_code}: {(run.stderr.strip().splitlines() or [''])[-1]}"
                if run.exit_code
                else f"printed {got!r}"
            )
        )
        checks.append(Check("Prints the expected four values", ok, detail, "executed_code"))
        return VerificationResult(self.id, all(c.passed for c in checks), checks, executed_learner_code=True)


@register
class TypesVerifier:
    id: ClassVar[str] = "python.types.v1"
    executes_learner_code: ClassVar[bool] = True

    async def verify(self, ctx: VerifyContext) -> VerificationResult:
        rel = ctx.params.get("file", "types_practice.py")
        wanted: dict[str, str] = ctx.params["variables"]
        data = await probe(ctx, rel, [*wanted, "summary"])
        checks: list[Check] = []
        if "__error__" in data:
            checks.append(Check("Your file runs", False, data["__error__"], "executed_code"))
            return VerificationResult(self.id, False, checks, executed_learner_code=True)
        for name, type_name in wanted.items():
            got = data.get(name, {}).get("type")
            checks.append(
                Check(
                    f"`{name}` is a {type_name}",
                    got == type_name,
                    f"it is a {got}" if got else "not defined",
                    "executed_code",
                )
            )
        summary = data.get("summary", {})
        name_repr = data.get("name", {}).get("repr", "''").strip("'\"")
        age_repr = data.get("age", {}).get("repr", "")
        ok = (
            summary.get("type") == "str"
            and name_repr in summary.get("repr", "")
            and age_repr in summary.get("repr", "")
        )
        checks.append(
            Check(
                "`summary` is a string containing name and age",
                ok,
                summary.get("repr", "not defined"),
                "executed_code",
            )
        )
        return VerificationResult(self.id, all(c.passed for c in checks), checks, executed_learner_code=True)


@register
class FunctionsVerifier:
    id: ClassVar[str] = "python.functions.v1"
    executes_learner_code: ClassVar[bool] = True

    async def verify(self, ctx: VerifyContext) -> VerificationResult:
        checks: list[Check] = []
        tree = _parse(ctx.read("greet.py"))
        func = (
            next(
                (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "greet"),
                None,
            )
            if tree
            else None
        )
        checks.append(
            Check(
                "greet.py defines greet(name)",
                func is not None and len(func.args.args) == 1,
                "found" if func else "no `def greet(...)` found",
                "static_analysis",
            )
        )
        test_tree = _parse(ctx.read("test_greet.py"))
        asserts = [n for n in ast.walk(test_tree) if isinstance(n, ast.Assert)] if test_tree else []
        calling = [
            a
            for a in asserts
            if any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "greet" for c in ast.walk(a))
        ]
        checks.append(
            Check(
                "test_greet.py has 2+ asserts that call greet",
                len(calling) >= 2,
                f"found {len(calling)}",
                "static_analysis",
            )
        )
        data = await probe(ctx, "greet.py", [])
        calls = data.get("__greet_calls__", {})
        for arg, want in (("Ada", "'Hello, Ada!'"), ("", "'Hello, !'")):
            got = calls.get(arg, {})
            ok = got.get("ok") and got.get("value") == want
            checks.append(
                Check(
                    f"greet({arg!r}) returns {want}",
                    bool(ok),
                    str(got.get("value", data.get("__error__", "not run"))),
                    "executed_code",
                )
            )
        assert ctx.runner is not None
        run = await ctx.runner.run_python(["test_greet.py"], ctx.workdir, isolated=False)
        checks.append(
            Check(
                "python3 test_greet.py exits with 0",
                run.ok,
                "your tests pass"
                if run.ok
                else ("timed out" if run.timed_out else (run.stderr.strip().splitlines() or ["failed"])[-1]),
                "executed_code",
            )
        )
        return VerificationResult(self.id, all(c.passed for c in checks), checks, executed_learner_code=True)
