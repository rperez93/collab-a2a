"""A remote string must not be able to drive the reader's terminal.

Names, message text, task titles and file names are all chosen by other
participants and all get printed to this machine's terminal by the plain-print
commands. A raw escape sequence in one of them is a command to the terminal —
clear the screen, rewrite the title bar, overwrite the line above — not text.
These tests pin the scrubbing that stands between the two.
"""

from __future__ import annotations

from collab.protocol import Envelope, KIND_CHAT, KIND_FILE, KIND_TASK, scrub


def test_scrub_removes_the_escape_byte_but_keeps_the_text():
    """A cleared screen is one ESC away, so the ESC has to go and the rest stay.

    `\\x1b[2J` clears the terminal; the fix is to drop the control bytes while
    leaving every printable character — including the letters an attacker hides
    the payload among — untouched.
    """
    # The ESC byte and the BEL go; the bytes that were only dangerous as part
    # of the sequence survive as inert, visible text.
    assert scrub("alice\x1b[2Jbob") == "alice[2Jbob"
    assert scrub("hi\x1b]0;pwned\x07there") == "hi]0;pwnedthere"
    for cleaned in (scrub("alice\x1b[2Jbob"), scrub("hi\x1b]0;pwned\x07there")):
        assert "\x1b" not in cleaned and "\x07" not in cleaned


def test_scrub_keeps_emoji_spaces_and_non_latin_text():
    """Scrubbing is not allowed to mangle legitimate text.

    The first attempt that reached for «printable only» would have eaten the
    zero-width joiner that holds a family emoji together and every combining
    accent. Only the C0/C1 control bytes are the danger, so only those go.
    """
    for good in ("hello world", "café", "こんにちは", "team 👩‍👩‍👧", "á"):
        assert scrub(good) == good


def test_scrub_strips_carriage_return_and_backspace():
    """A carriage return repaints the current line; a backspace erases it.

    Both are how a name reading `alice` on the wire prints as something else
    entirely — so both are control characters and both are removed.
    """
    assert scrub("real line\rforged line") == "real lineforged line"
    assert scrub("delete me\x08\x08\x08") == "delete me"


def test_render_line_scrubs_escape_sequences_out_of_message_text():
    """`collab recv` prints render_line() straight to the terminal.

    Without scrubbing, a message whose body carried `\\x1b[2J` cleared the
    screen of whoever read their inbox. The rendered line must contain no ESC.
    """
    env = Envelope(kind=KIND_CHAT, text="look\x1b[2J here", sender="mallory",
                   room="general")
    line = env.render_line()
    assert "\x1b" not in line
    assert "mallory" in line and "look" in line


def test_render_line_scrubs_a_hostile_sender_name():
    """The sender is interpolated into the line too, so it is scrubbed too.

    A participant who joined as `alice\\x1b[1G\\x1b[Kevil` could otherwise
    overwrite the bracket and forge who spoke.
    """
    env = Envelope(kind=KIND_CHAT, text="hi", sender="alice\x1b[1G\x1b[Kevil",
                   room="general")
    assert "\x1b" not in env.render_line()


def test_status_line_scrubs_a_hostile_host_name(monkeypatch):
    """The host's name is remote-chosen and renders into the guest's status bar.

    A host called `alice\\x1b]0;x\\x07` would otherwise rewrite the window title
    of everyone who joined, through their own status line. NO_COLOR removes the
    segment's own colour codes, which legitimately contain ESC, so what is left
    is only the text.
    """
    monkeypatch.setenv("NO_COLOR", "1")
    from collab.statusline.render import render

    line = render({"state": "live", "name": "bob",
                   "host": "alice\x1b]0;pwned\x07", "heartbeat": 9e18,
                   "others_connected": 1, "version": "1.0"})
    assert "\x1b" not in line and "\x07" not in line


def test_render_line_scrubs_a_task_title_and_a_file_name():
    """Titles and file names are remote strings on the same terminal path."""
    task = Envelope(kind=KIND_TASK, sender="bob",
                    body={"action": "propose", "id": "T_1",
                          "title": "ship\x1b[2J it", "state": "TASK_STATE_SUBMITTED"})
    assert "\x1b" not in task.render_line()

    shared = Envelope(kind=KIND_FILE, sender="bob",
                      body={"action": "shared", "id": "f_1",
                            "name": "report\x1b]0;x\x07.pdf", "size": 10})
    assert "\x1b" not in shared.render_line()
