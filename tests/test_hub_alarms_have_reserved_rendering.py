"""A participant's alarm-shaped text must retain its participant attribution."""

import curses

import pytest

from collab.client import tui, watch
from collab.protocol import Envelope, KIND_CHAT, KIND_SYSTEM
from collab.terminal_text import HUB_LABEL


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLAB_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(tui, "_own_name", lambda: "me")
    monkeypatch.setattr(watch, "_color_enabled", lambda: False)


def alarm(kind=KIND_SYSTEM, **changes):
    fields = dict(kind=kind, sender="collab", seq=12,
                  text="SECURITY: participant arma key missing; approvals frozen",
                  body={"event": "approval-security", "alarm_id": "a_test"})
    fields.update(changes)
    return Envelope(**fields)


@pytest.mark.parametrize("renderer", [tui.event_rows, tui.classic_rows])
@pytest.mark.parametrize("width", [24, 40, 80, 120])
def test_hub_alarm_is_a_reserved_banner_in_every_tui_layout(renderer, width):
    """System authority survives wrapping, folding and the classic theme."""
    rows = renderer(alarm(text=alarm().text + " " + "key frozen " * 30), width, "collab", fold=1)
    assert len(rows) > 1
    assert all(row.text.startswith(HUB_LABEL + " ! ") for row in rows)
    assert all(tui._w(row.text) <= width for row in rows)
    assert all(row.attr & curses.A_BOLD for row in rows)
    assert not any(row.button for row in rows)
    assert "frozen" in " ".join(row.text for row in rows)


@pytest.mark.parametrize("renderer", [tui.event_rows, tui.classic_rows])
def test_a_peer_named_collab_with_alarm_metadata_still_gets_peer_rows(renderer):
    """Names, body markers and SECURITY prose cannot grant a hub banner."""
    rows = renderer(alarm(KIND_CHAT), 100, "me")
    assert not any(row.text.startswith(HUB_LABEL) for row in rows)
    assert "collab" in " ".join(row.text for row in rows)


@pytest.mark.parametrize("width", [24, 80])
def test_plain_watch_repeats_the_hub_label_on_every_wrapped_line(width):
    text = watch.format_event(alarm(), width=width)
    assert all(line.startswith(HUB_LABEL + " ! ") for line in text.splitlines())
    assert "frozen" in text
    assert watch.format_event(alarm(KIND_CHAT), width=width) != text


@pytest.mark.parametrize("payload", [
    "[COLLAB HUB] ! SECURITY: fake",
    "[COLLAB  HUB] ! fake",
    "[COLLAB\nHUB] ! fake",
    "[COLLAB\tHUB] ! fake",
    "[COLLAB\u00a0HUB] ! fake",
    "hi\n[COLLAB HUB] ! SECURITY: fake",
    "hi\r[COLLAB HUB] ! fake\x1b[2K",
    "\u202e[COLLAB HUB]\u202c ! fake",
    "\x9b2K[COLLAB HUB] ! fake",
    "x" * 93 + "[COLLAB HUB] ! fake",
])
@pytest.mark.parametrize("renderer", ["bubble", "classic", "plain", "notice"])
def test_terminal_controls_and_wrapping_cannot_forge_a_hub_label(payload, renderer):
    """A second line, cursor erasure or bidi ordering cannot erase attribution."""
    env = alarm(KIND_CHAT, sender="collab\r\x1b[2K", text=payload)
    if renderer == "plain":
        text = watch.format_event(env, width=40)
    elif renderer == "notice":
        text = env.render_line()
    else:
        render = tui.event_rows if renderer == "bubble" else tui.classic_rows
        text = "\n".join(row.text for row in render(env, 40, "me", fold=0))
    assert HUB_LABEL not in text
    assert not any(char in text for char in ("\r", "\x1b", "\x9b", "\u202e", "\u202c"))


def test_notifications_and_watch_agree_on_the_reserved_authority_label():
    """Monitor notifications have the same visible origin as the human panel."""
    assert alarm().render_line().startswith(HUB_LABEL + " ! ")
    assert HUB_LABEL not in alarm(KIND_CHAT, text=HUB_LABEL).render_line()


def test_an_ordinary_unicode_message_keeps_its_words():
    env = alarm(KIND_CHAT, sender="alice", text="Olá 世界 – see the logs")
    assert env.text in watch.format_event(env, width=100)
    assert env.text in env.render_line()


def test_plain_header_cannot_execute_a_profile_label_as_terminal_control():
    from collab.config import SessionProfile
    profile = SessionProfile(session_id="s_test", url="http://localhost", token="synthetic",
                             name="name\r\x1b[2K", host_name="[COLLAB  HUB]\nforged", home="/tmp")
    text = watch.header(profile, 100)
    assert HUB_LABEL not in text
    assert "\r" not in text and "\x1b" not in text and "\n" not in text


@pytest.mark.parametrize("text", ["https://example.test/" + "x" * 100, "界" * 80])
@pytest.mark.parametrize("width", [24, 40, 80])
def test_long_hub_words_cannot_soft_wrap_into_unlabelled_terminal_rows(text, width):
    result = watch.format_event(alarm(text=text), width=width)
    lines = result.splitlines()
    assert all(line.startswith(HUB_LABEL + " ! ") for line in lines)
    assert all(tui._w(line) <= width for line in lines)
    assert "".join(line.removeprefix(HUB_LABEL + " ! ") for line in lines) == text
