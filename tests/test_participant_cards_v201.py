"""Participant layout and resize controls keep measurement groups reachable."""
import curses
import pytest
from types import SimpleNamespace

from collab import config
from collab.client import tui


def test_full_width_selection_keeps_state_and_separates_worker_measurements(monkeypatch):
    person = {'id': 'a', 'name': '機能追加' * 20, 'stats': {
        'model': 'coding-model', 'context_pct': 30,
        'worker': {'model': 'bridge-model', 'enabled': True, 'context_pct': 10}}}
    rows = tui._participant_card(person, 110, who='▾○ ' + person['name'],
        state='online', online=True, selected=True,
        fields=['model', 'context', 'worker'], detailed=True)
    assert tui._w(rows[0].text) == 110
    assert rows[0].text.endswith('online')
    assert rows[0].pair == tui.C_SELECTION and rows[0].edge == 0
    heading = next(r for r in rows if 'MAIN AGENT' in r.text)
    assert 'WORKER' in heading.text
    assert all(tui._w(row.text) <= 110 for row in rows)
    narrow = tui._participant_card(person, 38, who='▾○ Alice', state='online',
        online=True, selected=False, fields=['model', 'context', 'worker'], detailed=True)
    text = '\n'.join(r.text for r in narrow)
    assert 'MAIN AGENT\n' in text and 'WORKER\n' in text
    assert 'coding-model' in text and 'bridge-model' in text


def test_drag_is_memory_only_until_release_and_keyboard_saves_the_same_setting(monkeypatch):
    model = SimpleNamespace(events=[])
    panel = tui.Tui(model)
    panel._split_body_height = 40
    panel._chat_top = 15
    saved, masks = [], []
    monkeypatch.setattr(config, 'save_watch_settings', lambda **kw: saved.append(kw))
    monkeypatch.setattr(tui, 'roster_share', lambda: .30)
    monkeypatch.setattr(curses, 'mousemask', lambda mask: masks.append(mask))
    def event(y, flags):
        monkeypatch.setattr(curses, 'getmouse', lambda: (0, 6, y, 0, flags))
        panel.handle_mouse()
    event(14, curses.BUTTON1_PRESSED)
    event(25, getattr(curses, 'REPORT_MOUSE_POSITION', 0))
    assert saved == [] and panel._split_preview == 23 / 40
    event(26, curses.BUTTON1_RELEASED)
    assert saved == [{'roster_size': 60}]
    assert panel._split_preview is None and not panel._split_dragging
    assert masks[-1] == curses.ALL_MOUSE_EVENTS
    panel.handle(ord('+'))
    assert saved[-1] == {'roster_size': 35}


def test_roster_geometry_clamps_and_survives_read_only_settings(monkeypatch):
    panel = tui.Tui(SimpleNamespace(events=[]))
    def denied(**kw):
        raise PermissionError('readonly')
    monkeypatch.setattr(config, 'save_watch_settings', denied)
    panel._resize_roster(2)
    assert panel._split_override == .90
    panel._resize_roster(-1)
    assert panel._split_override == .05


@pytest.mark.parametrize('view,size', [('roster', (30,100)), ('both', (6,100)), ('both', (30,20))])
@pytest.mark.parametrize('dragging', [True, False])
def test_hiding_a_dragged_divider_stops_motion_reports_without_saving(monkeypatch, view, size, dragging):
    panel = tui.Tui(SimpleNamespace(events=[]), view=view)
    panel._split_dragging = dragging
    panel._split_preview = .6 if dragging else None
    panel._split_body_height = 40
    saved, masks = [], []
    monkeypatch.setattr(config, 'save_watch_settings', lambda **kw: saved.append(kw))
    monkeypatch.setattr(curses, 'mousemask', lambda mask: masks.append(mask))
    monkeypatch.setattr(tui, '_apply_theme_palette', lambda win: None)
    monkeypatch.setattr(panel, '_draw_single', lambda *args: None)
    win = SimpleNamespace(erase=lambda: None, getmaxyx=lambda: size, refresh=lambda: None, addnstr=lambda *args: None)
    panel._draw(win)
    assert not panel._split_dragging and panel._split_preview is None
    assert panel._split_body_height == 0
    assert masks == ([curses.ALL_MOUSE_EVENTS] if dragging else []) and saved == []
