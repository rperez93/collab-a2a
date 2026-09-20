"""The settings editor stages edits and uses the production registry to save them."""
from __future__ import annotations

import curses

import pytest

from collab import config
from collab.client.settings_tui import SettingsEditor, edit_lines, run


def editor_for(name):
    editor = SettingsEditor()
    editor.selected = name
    return editor


def test_typing_and_canceling_never_writes_a_setting():
    editor = editor_for("worker_codex_model")
    before = config.setting(editor.selected).read()
    editor.begin()
    editor.handle("\x15")
    for letter in "another-model":
        editor.handle(letter)
    assert config.setting(editor.selected).read() == before
    editor.handle("\x1b")
    assert config.setting(editor.selected).read() == before
    assert not editor.changed


def test_save_uses_the_same_parser_and_preserves_invalid_drafts():
    editor = editor_for("worker_timeout")
    editor.begin()
    editor.draft = "forever"
    assert editor.save() is False
    assert editor.mode == "edit" and editor.draft == "forever"
    assert config.setting("worker_timeout").read() == 60
    editor.draft = "120"
    assert editor.save() is True
    assert config.setting("worker_timeout").read() == 120
    assert editor.changed == {"worker_timeout"}


def test_an_external_change_to_the_same_setting_blocks_a_stale_draft():
    editor = editor_for("worker_timeout")
    editor.begin()
    editor.draft = "120"
    config.setting("worker_timeout").write(90)
    editor.refresh()
    assert "Changed elsewhere" in editor.notice
    assert not editor.save()
    assert config.setting("worker_timeout").read() == 90
    assert editor.draft == "120"


def test_an_unrelated_external_change_survives_saving():
    editor = editor_for("worker_timeout")
    editor.begin()
    editor.draft = "120"
    config.setting("worker_turn_gap").write(9)
    assert editor.save()
    assert config.setting("worker_turn_gap").read() == 9


def test_reset_needs_confirmation_and_returns_to_the_registry_default():
    config.setting("worker_timeout").write(90)
    editor = editor_for("worker_timeout")
    editor.handle("r")
    assert editor.mode == "reset"
    assert config.setting("worker_timeout").read() == 90
    editor.handle("\x1b")
    assert config.setting("worker_timeout").read() == 90
    editor.handle("r")
    editor.handle("\n")
    assert config.setting("worker_timeout").read() == 60


def test_boolean_toggle_is_a_draft_until_saved():
    editor = editor_for("watch_participant_details")
    editor.begin()
    editor.handle(" ")
    assert editor.draft == "true"
    assert config.setting(editor.selected).read() is False
    editor.handle("\n")
    assert config.setting(editor.selected).read() is True


def test_multiline_guidance_and_prices_round_trip_without_shell_execution():
    editor = editor_for("worker_instructions")
    editor.begin()
    for key in [*"Read the task.", "\x0e", *"Ask before sending $(anything)."]:
        editor.handle(key)
    assert editor.save()
    assert config.setting("worker_instructions").read() == "Read the task.\nAsk before sending $(anything)."
    editor = editor_for("stats_prices")
    editor.begin()
    editor.draft = '{"example-model":{"input":1,"output":2}}'
    assert editor.save()
    editor.begin()
    assert '"input": 1' in editor.draft


def test_search_updates_selection_and_can_restore_its_previous_filter():
    editor = SettingsEditor()
    editor.handle("/")
    for letter in "worker_":
        editor.handle(letter)
    assert editor.filtered and all("worker" in item.name or "worker" in item.about for item in editor.filtered)
    editor.handle("\n")
    editor.move(1)
    assert editor.selected == editor.filtered[1].name
    editor.handle("/")
    editor.handle("z")
    assert not editor.filtered
    editor.handle("\x1b")
    assert editor.query == "worker_" and editor.filtered


class Screen:
    def __init__(self, height=24, width=80):
        self.height, self.width = height, width
        self.writes = []
        self.cursor = None
    def getmaxyx(self):
        return self.height, self.width
    def erase(self):
        self.writes = []
    def addstr(self, y, x, text, *_):
        from collab.columns import width
        assert 0 <= y < self.height
        assert x + width(text) < self.width
        self.writes.append((y, x, text))
    def move(self, y, x):
        self.cursor = y, x
    def refresh(self):
        pass


@pytest.mark.parametrize("width,height", [(28,10), (38,20), (80,24), (120,40)])
def test_all_editor_modes_fit_the_terminal(width, height):
    editor = editor_for("worker_instructions")
    screen = Screen(height, width)
    for mode in ("browse", "search", "edit", "reset", "help"):
        editor.mode = mode
        editor.draft = "機能追加 " * 12 + "\nsecond line"
        editor.cursor = len(editor.draft)
        editor.draw(screen)
        assert screen.writes
        if mode == "edit":
            assert screen.cursor[0] < height - 2


def test_mouse_selection_edit_save_and_wheel_follow_visible_controls(monkeypatch):
    editor = SettingsEditor()
    screen = Screen()
    editor.draw(screen)
    monkeypatch.setattr(curses, "getmouse", lambda: (0, 4, 4, 0, curses.BUTTON1_CLICKED))
    editor.handle(curses.KEY_MOUSE)
    assert editor.selected == editor.filtered[1].name
    monkeypatch.setattr(curses, "getmouse", lambda: (0, 4, 4, 0, curses.BUTTON5_PRESSED))
    editor.handle(curses.KEY_MOUSE)
    assert editor.selected == editor.filtered[4].name
    editor.selected = "worker_timeout"
    editor.draw(screen)
    _, start, _, _ = next(button for button in editor._buttons if button[-1] == "edit")
    monkeypatch.setattr(curses, "getmouse", lambda: (0, start, 1, 0, curses.BUTTON1_CLICKED))
    editor.handle(curses.KEY_MOUSE)
    assert editor.mode == "edit"
    editor.draft = "75"
    editor.draw(screen)
    _, start, _, _ = next(button for button in editor._buttons if button[-1] == "save")
    monkeypatch.setattr(curses, "getmouse", lambda: (0, start, 1, 0, curses.BUTTON1_CLICKED))
    editor.handle(curses.KEY_MOUSE)
    assert config.setting("worker_timeout").read() == 75


def test_caret_follows_unicode_columns_and_explicit_newlines():
    lines, row, col = edit_lines("ab機能\ncd", 4, 5)
    assert lines == ["ab機", "能", "cd"]
    assert (row, col) == (1, 2)


def test_nonterminal_call_returns_guidance_instead_of_starting_curses(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert run() == 2
    assert "needs a terminal" in capsys.readouterr().err


def test_narrow_editor_retains_every_mouse_action():
    """A small terminal abbreviates labels instead of losing mouse controls."""
    editor = editor_for("watch_participant_details")
    screen = Screen(20, 28)
    editor.draw(screen)
    assert {button[-1] for button in editor._buttons} == {"search", "edit", "reset", "help", "quit"}
    editor.begin()
    editor.draw(screen)
    assert {button[-1] for button in editor._buttons} == {"save", "cancel", "toggle"}
