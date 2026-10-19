from __future__ import annotations

import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from cli_mentor.events.schema import (
    PAYLOAD_ADAPTER,
    EventEnvelope,
    ShellCommandFinished,
    SourceKind,
    SourceRef,
)
from cli_mentor.events.sequencing import Sequencer
from cli_mentor.observe.privacy import (
    is_password_prompt,
    looks_binary,
    redact,
    sensitive_reason,
)
from cli_mentor.observe.workspace import WorkspaceScope


def src(kind: SourceKind = SourceKind.SHELL, sid: str = "zsh:1", epoch: str = "e1") -> SourceRef:
    return SourceRef(kind=kind, id=sid, epoch=epoch)


# ---------- schema ----------
def test_payload_discriminator_parses_shell_finish() -> None:
    payload = PAYLOAD_ADAPTER.validate_python(
        {
            "type": "shell.command_finished",
            "command": "ls",
            "cwd": "/tmp",
            "exit_status": 0,
            "finished_at": 1.0,
        }
    )
    assert isinstance(payload, ShellCommandFinished)


def test_payload_rejects_unknown_type_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        PAYLOAD_ADAPTER.validate_python({"type": "shell.run_this", "command": "rm -rf /"})
    with pytest.raises(ValidationError):
        PAYLOAD_ADAPTER.validate_python(
            {
                "type": "shell.command_finished",
                "command": "ls",
                "cwd": "/",
                "exit_status": 0,
                "finished_at": 1.0,
                "trusted": True,
            }
        )


def test_envelope_roundtrip_json() -> None:
    env = EventEnvelope(
        session_id="s1",
        source=src(),
        seq=1,
        captured_at=time.time(),
        received_at=time.time(),
        payload=ShellCommandFinished(command="ls", cwd="/", exit_status=0, finished_at=1.0),
    )
    again = EventEnvelope.model_validate_json(env.model_dump_json())
    assert again == env
    assert again.type == "shell.command_finished"
    assert again.schema_version == 1


def test_source_id_pattern_blocks_injection() -> None:
    with pytest.raises(ValidationError):
        SourceRef(kind=SourceKind.SHELL, id="zsh 1; rm -rf", epoch="e")


def test_tmux_pane_id_must_look_like_pane() -> None:
    with pytest.raises(ValidationError):
        PAYLOAD_ADAPTER.validate_python({"type": "tmux.output", "pane_id": "1", "text": "x"})


# ---------- sequencing ----------
def test_sequencer_in_order_duplicate_gap_epoch() -> None:
    s = Sequencer()
    assert s.check(src(), 1).new_epoch
    assert s.check(src(), 2).accept
    dup = s.check(src(), 2)
    assert dup.duplicate and not dup.accept
    gap = s.check(src(), 5)
    assert gap.accept and gap.missing == 2
    old = s.check(src(), 4)
    assert not old.accept
    restarted = s.check(src(epoch="e2"), 1)
    assert restarted.new_epoch and restarted.accept and restarted.missing == 0


def test_sequencer_first_event_lost_is_gap() -> None:
    v = Sequencer().check(src(), 3)
    assert v.new_epoch and v.missing == 2


def test_sequencer_sources_are_independent() -> None:
    s = Sequencer()
    s.check(src(sid="a"), 1)
    assert s.check(src(sid="b"), 1).new_epoch
    assert s.check(src(sid="a"), 2).missing == 0


# ---------- privacy ----------
@pytest.mark.parametrize(
    "text,secret",
    [
        ("export GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abc"),
        ("export MY_API_KEY=abcd1234efgh5678", "abcd1234"),
        ('password = "hunter2hunter2"', "hunter2"),
        ("curl -H 'Authorization: Bearer abc.def.ghi123'", "abc.def"),
        ("git clone https://user:s3cretpass@example.com/repo", "s3cretpass"),
        ("AKIAABCDEFGHIJKLMNOP is my key", "AKIAABCDEFGHIJKLMNOP"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----", "MIIabc"),
        ("mysql --password=topsecret123 db", "topsecret123"),
        ("sk-ant-api03-ZZZZZZZZZZZZZZZZZZZZ", "ZZZZZZZZZZ"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N", "dozjgNry"),
    ],
)
def test_redaction_catches_common_secrets(text: str, secret: str) -> None:
    result = redact(text)
    assert secret not in result.text
    assert result.count >= 1


@pytest.mark.parametrize(
    "text",
    [
        "tokens = line.split()",
        "mkdir -p notes/archive",
        "password = input('Password: ')",
        "tokenizer = TokenizerFactory()",
        "grep ERROR server.log | wc -l > error_count.txt",
    ],
)
def test_redaction_leaves_ordinary_code(text: str) -> None:
    assert redact(text).text == text


def test_password_prompt_detection() -> None:
    assert is_password_prompt("Password:")
    assert is_password_prompt("[sudo] password for anup: ")
    assert is_password_prompt("Enter passphrase for key '/x/id_ed25519': ")
    assert not is_password_prompt("password reset done")


@pytest.mark.parametrize(
    "path,blocked",
    [
        (".env", True),
        (".env.local", True),
        ("keys/server.pem", True),
        ("id_ed25519", True),
        ("project/node_modules/x.js", True),
        (".git/config", True),
        ("notes/todo.txt", False),
        ("python/greet.py", False),
    ],
)
def test_sensitive_paths(path: str, blocked: bool) -> None:
    assert (sensitive_reason(path) is not None) == blocked


def test_binary_detection() -> None:
    assert looks_binary(b"abc\x00def")
    assert not looks_binary(b"plain text")


# ---------- workspace scope ----------
def test_scope_inside_outside_and_symlink(tmp_path: Path, workspace: Path) -> None:
    scope = WorkspaceScope([workspace])
    (workspace / "a.txt").write_text("hello")
    assert scope.check(workspace / "a.txt").allowed
    assert scope.check("a.txt", base=workspace).allowed
    assert not scope.check(tmp_path / "other.txt").allowed
    assert not scope.check(workspace / ".." / "escape.txt").allowed
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    (workspace / "link.txt").symlink_to(secret)
    assert not scope.check(workspace / "link.txt").allowed
    (workspace / "dirlink").symlink_to(tmp_path)
    assert not scope.check(workspace / "dirlink" / "secret.txt").allowed


def test_scope_read_rules(workspace: Path) -> None:
    scope = WorkspaceScope([workspace], max_file_bytes=1024)
    (workspace / "ok.txt").write_text("API_KEY=abcdefgh12345678\nhello\n")
    read = scope.read_text(workspace / "ok.txt")
    assert read.text is not None and "abcdefgh" not in read.text and read.redactions == 1
    (workspace / ".env").write_text("X=1")
    assert scope.read_text(workspace / ".env").text is None
    (workspace / "big.txt").write_text("x" * 2000)
    assert "too large" in scope.read_text(workspace / "big.txt").reason
    (workspace / "bin.dat").write_bytes(b"\x00\x01\x02")
    assert scope.read_text(workspace / "bin.dat").reason == "binary file"


def test_list_files_skips_excluded(workspace: Path) -> None:
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "x.js").write_text("x")
    (workspace / "src").mkdir()
    (workspace / "src" / "main.py").write_text("print(1)")
    (workspace / ".env").write_text("A=1")
    files = WorkspaceScope([workspace]).list_files(workspace)
    assert files == ["src/main.py"]
