"""What the tutor pane's header, hints, and status say in each situation (pure functions)."""

from __future__ import annotations

from cli_mentor.lifecycle import tmux_conf
from cli_mentor.tutor.ui_protocol import StatusInfo
from cli_mentor.ui.app import header_segments, input_hints, stage_segments, state_segments, tutor_label
from cli_mentor.ui.theme import terminal_is_light


def info(**kw: object) -> StatusInfo:
    base: dict[str, object] = dict(
        goal=None,
        exercise=None,
        stage=None,
        mode="guided",
        observation="on",
        sync="current",
        sources={"term": "current", "shell": "none", "nvim": "current"},
        provider="openai",
        model="deepseek-chat",
        mock=False,
        permissions=[],
        busy=False,
    )
    base.update(kw)
    return StatusInfo(**base)  # type: ignore[arg-type]


def words(segments: list[tuple[str, str]]) -> str:
    return "".join(text for text, _style in segments)


def test_header_follows_the_learning_flow() -> None:
    assert words(header_segments(None)[2]) == "Getting ready…"
    assert words(header_segments(info())[0]) == "New subject"
    review = header_segments(info(phase="plan_review", subject="Rust", steps=12))
    assert words(review[1]) == "proposed plan · 12 steps" and "Reply yes" in words(review[2])
    learning = info(phase="learning", subject="Rust", steps=12, step=3, done=2, step_title="Borrowing")
    subject, where, step, stages = header_segments(learning)
    assert (words(subject), words(where)) == ("Rust", "step 3 of 12")
    assert words(step) == "Borrowing  · not started" and words(stages) == "○ learn  ○ try  ○ check"
    attempt = header_segments(info(phase="learning", subject="Rust", steps=12, step=3, step_stage="attempt"))
    assert words(attempt[3]) == "✓ learn  ● try  ○ check"
    done = header_segments(info(phase="learning", subject="Rust", steps=12, done=12))
    assert words(done[2]) == "Every step is done." and words(done[3]) == "✓ complete"


def test_stage_marks_use_shapes_not_only_colour() -> None:
    assert [text for text, _ in stage_segments("explain") if text.strip()] == ["● learn", "○ try", "○ check"]


def test_input_hints_say_what_to_type_next() -> None:
    assert "What do you want to learn?" in input_hints(info())[0]
    assert input_hints(info(phase="plan_review", subject="x"))[0].startswith("Type yes")
    learning = {"phase": "learning", "subject": "x", "steps": 3, "step": 1}
    assert "prediction" in input_hints(info(**learning, step_stage="explain"))[0]
    assert "/check" in input_hints(info(**learning, step_stage="attempt"))[0]
    assert "/lesson" in input_hints(info(**learning))[0]


def test_state_words_and_sync_problems() -> None:
    assert words(state_segments(info(), busy=False, frame=0)) == "● observing · deepseek-chat"
    paused = words(state_segments(info(observation="paused", mock=True), busy=True, frame=3))
    assert "writing" in paused and "‖ paused" in paused and "offline mock" in paused
    broken = info(sources={"term": "current", "shell": "none", "nvim": "disconnected"}, mode="practice")
    assert (
        words(state_segments(broken, False, 0)) == "● observing · Neovim disconnected · practice mode · deepseek-chat"
    )


def test_tutor_label_names_the_source_honestly() -> None:
    assert tutor_label("session_start", "CLI Mentor").plain == "Mentor"
    assert tutor_label("hint", "openai · deepseek-chat").plain == "Mentor · hint · openai · deepseek-chat"
    mock = tutor_label("lesson_intro", "MOCK TUTOR (offline, deterministic, not an AI)", "streaming").plain
    assert mock == "Mentor · lesson · offline mock, not an AI · writing…"


def test_light_or_dark_terminal_guess() -> None:
    assert terminal_is_light({"COLORFGBG": "0;15"}) and not terminal_is_light({"COLORFGBG": "15;0"})
    assert not terminal_is_light({}) and terminal_is_light({"CLI_MENTOR_THEME": "light", "COLORFGBG": "15;0"})
    assert "fg=blue" in tmux_conf(light=True) and "fg=cyan" in tmux_conf(light=False)
    assert "@mentor_label" in tmux_conf(light=False) and "bg=default" in tmux_conf(light=False)
