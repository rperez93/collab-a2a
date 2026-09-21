"""A keyboard and mouse editor for the same registry used by ``collab config``.

Edits are drafts until saved. The file is reread before each write, and a
changed value blocks that draft instead of overwriting another terminal's work.
The setting's own parser and writer remain the only source of validation.
"""
from __future__ import annotations

import copy
import curses
import sys
from typing import Any, Callable

from .. import config
from ..columns import clip, width as columns
from ..setting_edit import MAX_DRAFT, edit_text, editor_draft, shown


def _safe(text: Any) -> str:
    # Config strings can be shell snippets. Their control bytes must never
    # become terminal controls or make the displayed value disagree with it.
    return "".join(char if char.isprintable() else " " for char in str(text))


class SettingsEditor:
    def __init__(self) -> None:
        self.entries = config.settings()
        self.values: dict[str, Any] = {}
        self.selected = self.entries[0].name if self.entries else ""
        self.query = ""
        self.mode = "browse"
        self.draft = ""
        self.cursor = 0
        self.baseline: Any = None
        self.notice = "Values reload when changed in another terminal."
        self.offset = 0
        self.page = 8
        self.changed: set[str] = set()
        self._stamp: Any = object()
        self._search_before = ""
        self._buttons: list[tuple[int, int, int, str]] = []
        self._list_top = 3
        self.help_offset = 0
        self.refresh()

    @property
    def filtered(self):
        query = self.query.casefold()
        return [item for item in self.entries if query in item.name.casefold()
                or query in item.about.casefold()]

    @property
    def item(self):
        return next((item for item in self.entries if item.name == self.selected), None)

    def refresh(self, *, force: bool = False) -> None:
        try:
            stat = config.global_config_path().stat()
            stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        if not force and stamp == self._stamp:
            return
        self._stamp = stamp
        for item in self.entries:
            self.values[item.name] = copy.deepcopy(item.read())
        if self.mode in ("edit", "reset", "choose", "external") and self.values.get(self.selected) != self.baseline:
            self.notice = "Changed elsewhere. Cancel and reopen before saving."

    def move(self, step: int) -> None:
        items = self.filtered
        if not items:
            return
        index = next((i for i, item in enumerate(items) if item.name == self.selected), 0)
        self.selected = items[max(0, min(index + step, len(items) - 1))].name
        self._settle()

    def _settle(self) -> None:
        items = self.filtered
        if not items:
            self.selected, self.offset = "", 0
            return
        index = next((i for i, item in enumerate(items) if item.name == self.selected), 0)
        self.selected = items[index].name
        if index < self.offset:
            self.offset = index
        elif index >= self.offset + self.page:
            self.offset = max(0, index - self.page + 1)
        self.offset = min(self.offset, max(len(items) - self.page, 0))

    def begin(self, mode: str = "edit") -> None:
        if self.item is None:
            return
        self.refresh(force=True)
        self.baseline = copy.deepcopy(self.values[self.selected])
        self.draft = shown(self.baseline)
        self.cursor = len(self.draft)
        self.mode = mode
        self.notice = "Draft only – Enter saves; Esc cancels."

    def request_edit(self) -> None:
        self.begin()
        if self.item and (isinstance(self.baseline, str) or self.item.parse is str):
            self.mode = "choose"
            self.notice = "Choose an external terminal editor or inline editing."

    def external_edit(self) -> None:
        """Return an editor's content to the draft; Enter still commits it."""
        try:
            draft = editor_draft(self.item, edit_text(self.draft))
        except (ValueError, OSError) as exc:
            self.notice = str(exc)
        except KeyboardInterrupt:
            self.notice = "External edit cancelled; draft unchanged."
        else:
            self.draft, self.cursor = draft, len(draft)
            self.notice = "Editor closed – Enter saves; Esc discards."
        self.mode = "edit"
        self.refresh(force=True)

    def save(self, *, reset: bool = False) -> bool:
        item = self.item
        if item is None:
            return False
        self.refresh(force=True)
        if self.values[item.name] != self.baseline:
            self.notice = "Changed elsewhere. Cancel and reopen before saving."
            return False
        try:
            if reset:
                config.unset_setting(item.name)
            else:
                item.write(item.parse(self.draft))
        except (ValueError, TypeError, OSError) as exc:
            self.notice = str(exc)
            return False
        self.changed.add(item.name)
        self.mode = "browse"
        self.refresh(force=True)
        self.notice = f"Saved {item.name}" + (" – using its default." if reset else ".")
        return True

    def _insert(self, text: str) -> None:
        if len(self.draft) + len(text) > MAX_DRAFT:
            self.notice = f"Draft limit: {MAX_DRAFT:,} characters."
            return
        self.draft = self.draft[:self.cursor] + text + self.draft[self.cursor:]
        self.cursor += len(text)

    def action(self, action: str) -> bool:
        if action == "quit":
            return False
        if action == "edit":
            self.request_edit()
        elif action == "external":
            self.mode = "external"
        elif action == "inline":
            self.mode = "edit"
            self.notice = "Draft only – Enter saves; Esc cancels."
        elif action == "reset":
            self.begin("reset")
        elif action == "save":
            self.save(reset=self.mode == "reset")
        elif action == "cancel":
            self.mode = "browse"
            self.notice = "Draft discarded."
        elif action == "search":
            self._search_before, self.mode = self.query, "search"
        elif action == "help":
            self.mode = "help"
        elif action == "toggle":
            self.draft = "false" if self.draft == "true" else "true"
            self.cursor = len(self.draft)
        return True

    def handle(self, key: int | str) -> bool:
        if key == curses.KEY_MOUSE:
            return self.mouse()
        enter = key in ("\n", "\r", 10, 13, curses.KEY_ENTER)
        escape = key in ("\x1b", 27)
        if self.mode == "help":
            if key in (curses.KEY_DOWN, "j"):
                self.help_offset += 1
            elif key in (curses.KEY_UP, "k"):
                self.help_offset = max(0, self.help_offset - 1)
            if escape or enter or key in ("?", "q"):
                self.mode = "browse"
            return True
        if self.mode == "choose":
            if enter or key in ("y", "Y"):
                self.action("external")
            elif key in ("n", "N"):
                self.action("inline")
            elif escape:
                self.action("cancel")
            return True
        if self.mode == "reset":
            if enter:
                self.save(reset=True)
            elif escape:
                self.action("cancel")
            return True
        if self.mode == "search":
            if enter:
                self.mode = "browse"
            elif escape:
                self.query, self.mode = self._search_before, "browse"
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                self.query = self.query[:-1]
            elif isinstance(key, str) and key.isprintable() and len(self.query) < 200:
                self.query += key
            self._settle()
            return True
        if self.mode == "edit":
            if enter:
                self.save()
            elif escape:
                self.action("cancel")
            elif key == " " and isinstance(self.baseline, bool):
                self.action("toggle")
            elif key in (curses.KEY_LEFT,):
                self.cursor = max(0, self.cursor - 1)
            elif key in (curses.KEY_RIGHT,):
                self.cursor = min(len(self.draft), self.cursor + 1)
            elif key in (curses.KEY_HOME, "\x01"):
                self.cursor = 0
            elif key in (curses.KEY_END, "\x05"):
                self.cursor = len(self.draft)
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b") and self.cursor:
                self.draft = self.draft[:self.cursor - 1] + self.draft[self.cursor:]
                self.cursor -= 1
            elif key == curses.KEY_DC:
                self.draft = self.draft[:self.cursor] + self.draft[self.cursor + 1:]
            elif key == "\x15":
                self.draft, self.cursor = "", 0
            elif key == "\x0e":
                self._insert("\n")
            elif isinstance(key, str) and key.isprintable():
                self._insert(key)
            return True
        if key in ("q", "Q"):
            return False
        if key in (curses.KEY_UP, "k"):
            self.move(-1)
        elif key in (curses.KEY_DOWN, "j"):
            self.move(1)
        elif key == curses.KEY_PPAGE:
            self.move(-self.page)
        elif key == curses.KEY_NPAGE:
            self.move(self.page)
        elif key in (curses.KEY_HOME, "g"):
            self.move(-len(self.filtered))
        elif key in (curses.KEY_END, "G"):
            self.move(len(self.filtered))
        elif enter or key == "e":
            self.request_edit()
        elif key == "r":
            self.begin("reset")
        elif key == "/":
            self.action("search")
        elif key == "?":
            self.action("help")
        return True

    def mouse(self) -> bool:
        try:
            _, x, y, _, state = curses.getmouse()
        except curses.error:
            return True
        if self.mode == "help":
            if state & getattr(curses, "BUTTON4_PRESSED", 0):
                self.help_offset = max(0, self.help_offset - 3)
            elif state & getattr(curses, "BUTTON5_PRESSED", 0):
                self.help_offset += 3
        if self.mode == "browse":
            if state & getattr(curses, "BUTTON4_PRESSED", 0):
                self.move(-3)
                return True
            if state & getattr(curses, "BUTTON5_PRESSED", 0):
                self.move(3)
                return True
        click = (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED
                 | curses.BUTTON1_DOUBLE_CLICKED | curses.BUTTON1_TRIPLE_CLICKED)
        if not state & click:
            return True
        for row, start, end, action in self._buttons:
            if y == row and start <= x < end:
                return self.action(action)
        if self.mode == "browse" and self._list_top <= y < self._list_top + self.page:
            index = self.offset + y - self._list_top
            if index < len(self.filtered):
                self.selected = self.filtered[index].name
        return True

    def draw(self, win) -> None:
        from . import tui
        self.refresh()
        tui._apply_theme_palette(win)
        win.erase()
        height, width = win.getmaxyx()
        self._buttons = []
        caret = None

        def put(y, text, attr=0, x=0):
            if 0 <= y < height and x < width - 1:
                try:
                    win.addstr(y, x, clip(_safe(text), max(width - x - 1, 0)), attr)
                except curses.error:
                    pass

        def buttons(items, y=1):
            x = 0
            for label, action in items:
                if width < 76:
                    label = {"search": "/", "edit": "e", "reset": "r", "help": "?", "quit": "q",
                             "save": "Confirm" if self.mode == "reset" else "Save",
                             "cancel": "Cancel", "toggle": "Toggle",
                             "external": "Yes", "inline": "No"}.get(action, label)
                text = f"[{label}]"
                if x + len(text) >= width:
                    break
                put(y, text, curses.A_BOLD | curses.color_pair(tui.C_ACCENT), x)
                self._buttons.append((y, x, x + len(text), action))
                x += len(text) + 1

        if height < 10 or width < 28:
            put(0, "Enlarge to 28 columns / 10 rows")
            put(1, "q quits; Esc cancels an edit")
            win.refresh()
            return
        put(0, "collab settings".ljust(width - 1), curses.A_BOLD | curses.color_pair(tui.C_TITLE))
        if self.mode == "help":
            buttons([("Back", "cancel")])
            help_text = (
                "Up/Down or j/k: select; wheel: move three settings.",
                "Enter or Edit: draft the selected value.",
                "Text asks whether to use an external terminal editor (y/n).",
                "Set editor to vim, nvim, nano or a command with arguments.",
                "Empty editor uses VISUAL, EDITOR, then vi.",
                "/ or Search: filter names and descriptions.",
                "r or Reset: confirm returning to the default.",
                "In an edit: Enter saves; Esc/Cancel discards.",
                "Ctrl-U clears; Home/End moves; Ctrl-N inserts a newline.",
                "Space or Toggle changes a boolean draft.",
                "Validation matches collab config; commands stay text until saved.",
                "Changes elsewhere reload; conflicting drafts never overwrite them.",
                "Name/colour session updates run after closing the editor.",
                "q quits; ? or Esc closes this help.",
            )
            help_lines = [line for text in help_text for line in edit_lines(text, 0, width - 2)[0]]
            self.help_offset = min(self.help_offset, max(len(help_lines) - height + 6, 0))
            for i, line in enumerate(help_lines[self.help_offset:self.help_offset + height - 6], 3):
                put(i, line)
        elif self.mode == "choose":
            buttons([("Yes Enter", "external"), ("No n", "inline"), ("Cancel Esc", "cancel")])
            put(3, self.selected, curses.A_BOLD)
            for row, line in enumerate(edit_lines(
                    "Edit in an external terminal editor?\n"
                    "Yes opens a temporary file; No edits here.\n"
                    "Choose vim, nvim or nano using the editor setting.", 0, width - 2)[0][:max(height - 7, 1)], 4):
                put(row, line)
        elif self.mode in ("edit", "reset"):
            buttons([("Save Enter" if self.mode == "edit" else "Confirm Enter", "save"),
                     ("Cancel Esc", "cancel")]
                    + ([("Toggle Space", "toggle")] if isinstance(self.baseline, bool) and self.mode == "edit" else []))
            put(2, self.selected, curses.A_BOLD)
            if self.item:
                put(3, self.item.about)
            if self.mode == "reset":
                put(5, "Reset to default: " + shown(self.item.default))
                put(6, "Press Enter or click Confirm to apply.")
            else:
                lines, cursor_row, cursor_col = edit_lines(self.draft, self.cursor, width - 2)
                available = max(height - 8, 1)
                start = max(cursor_row - available + 1, 0)
                for i, line in enumerate(lines[start:start + available]):
                    put(5 + i, line)
                try:
                    caret = (5 + cursor_row - start, min(cursor_col, width - 2))
                    curses.curs_set(1)
                except curses.error:
                    pass
        else:
            buttons([("Search /", "search"), ("Edit Enter", "edit"),
                     ("Reset to default r", "reset"), ("Help ?", "help"), ("Quit q", "quit")])
            put(2, f"{'Search' if self.mode == 'search' else 'Filter'}: {self.query}  ({len(self.filtered)} settings)")
            self.page = max(height - 9, 1)
            self._settle()
            key_width = min(36, max(16, width // 2))
            for row, item in enumerate(self.filtered[self.offset:self.offset + self.page], 3):
                value = shown(self.values.get(item.name)) or "(empty)"
                modified = "*" if self.values.get(item.name) != item.default else " "
                line = modified + clip(item.name, key_width - 1).ljust(key_width) + " " + _safe(value)
                put(row, line.ljust(width - 1), curses.color_pair(tui.C_SELECTION) | curses.A_BOLD
                    if item.name == self.selected else curses.color_pair(tui.C_TEXT))
            if self.item:
                put(height - 6, self.selected, curses.A_BOLD)
                about = edit_lines(self.item.about, 0, width - 2)[0]
                for i, line in enumerate(about[:2], height - 5):
                    put(i, line)
                put(height - 3, "r: Reset to default: " + (shown(self.item.default) or "(empty)"))
            if self.mode == "search":
                try:
                    caret = (2, min(columns("Search: " + self.query), width - 2))
                    curses.curs_set(1)
                except curses.error:
                    pass
        put(height - 2, self.notice)
        put(height - 1, "Enter save · Esc cancel · Ctrl-N newline" if self.mode == "edit"
            else "y/Enter external · n inline · Esc cancel" if self.mode == "choose"
            else "↑↓ select · / search · Enter edit · r reset · q quit")
        if self.mode not in ("edit", "search"):
            try:
                curses.curs_set(0)
            except curses.error:
                pass
        if caret is not None:
            try:
                win.move(*caret)
            except curses.error:
                pass
        win.refresh()


def edit_lines(text: str, cursor: int, width: int) -> tuple[list[str], int, int]:
    """Wrap draft text and locate its caret using terminal columns."""
    lines = [""]
    caret = (0, 0)
    for index, char in enumerate(text):
        if char != "\n" and columns(lines[-1]) + columns(char) > width:
            lines.append("")
        if index == cursor:
            caret = (len(lines) - 1, columns(lines[-1]))
        if char == "\n":
            lines.append("")
        else:
            lines[-1] += char
    if cursor == len(text):
        if columns(lines[-1]) >= width:
            lines.append("")
        caret = (len(lines) - 1, columns(lines[-1]))
    return lines, *caret


def run(*, on_change: Callable[[str], Any] | None = None) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("The settings editor needs a terminal; use collab config <key> <value>.", file=sys.stderr)
        return 2
    editor = SettingsEditor()

    def loop(win):
        from . import tui
        tui._init_colors()
        win.keypad(True)
        win.timeout(250)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        # See the participant panel's real-host regression: zero swallowed
        # repeated mouse reports on ncurses; 100 ms preserves click decoding.
        curses.mouseinterval(100)
        try:
            curses.set_escdelay(25)
        except (AttributeError, curses.error):
            pass
        while True:
            editor.draw(win)
            try:
                key = win.get_wch()
            except curses.error:
                continue
            if not editor.handle(key):
                return
            if editor.mode == "external":
                # Hand the real terminal back to vim/nano, then restore curses
                # even when launching or reading the editor fails. Keeping raw
                # mode active makes a working editor look unresponsive.
                curses.def_prog_mode()
                curses.endwin()
                try:
                    editor.external_edit()
                finally:
                    curses.reset_prog_mode()
                    win.keypad(True)
                    win.timeout(250)
                    win.clearok(True)

    from . import tui
    tui._THEME_POLLING[0] = True
    try:
        curses.wrapper(loop)
    except KeyboardInterrupt:
        pass
    finally:
        tui._THEME_POLLING[0] = False
    if on_change:
        for name in sorted(editor.changed):
            on_change(name)
    return 0
