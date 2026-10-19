from __future__ import annotations

import pytest

from cli_mentor.observe.tmux_control import (
    ControlParser,
    Notification,
    Response,
    decode_octal,
    quote_arg,
)


def test_decode_octal_escapes() -> None:
    assert decode_octal(rb"hello\015\012") == b"hello\r\n"
    assert decode_octal(rb"back\134slash") == b"back\\slash"
    assert decode_octal(b"plain") == b"plain"
    assert decode_octal("café".encode()) == "café".encode()


def test_parser_blocks_and_notifications() -> None:
    p = ControlParser()
    assert p.feed(b"%begin 1700000000 12 1\n") is None
    assert p.feed(b"line one\n") is None
    # A line that looks like a notification inside a block is block content.
    assert p.feed(b"%output %1 not-a-notification\n") is None
    resp = p.feed(b"%end 1700000000 12 1\n")
    assert isinstance(resp, Response)
    assert resp.ok and resp.number == 12
    assert resp.lines == ["line one", "%output %1 not-a-notification"]
    note = p.feed(b"%output %3 hi\\012\n")
    assert note == Notification("output", b"%3 hi\\012")


def test_parser_error_block_and_mismatched_end() -> None:
    p = ControlParser()
    p.feed(b"%begin 1 5 0")
    # An %end with another command number is content, not the end.
    assert p.feed(b"%end 1 4 0") is None
    resp = p.feed(b"%error 1 5 0")
    assert isinstance(resp, Response)
    assert not resp.ok and resp.lines == ["%end 1 4 0"]


def test_quote_arg_rejects_quotes() -> None:
    assert quote_arg("#{pane_id}") == "'#{pane_id}'"
    with pytest.raises(ValueError):
        quote_arg("it's")
