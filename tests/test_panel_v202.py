"""Cards stay compact and scrolling never turns participant updates into motion."""
import curses
import time
from types import SimpleNamespace

from collab.client import tui
from collab.config import SessionProfile


def test_collapsed_card_is_one_padded_coloured_line_at_every_width():
    """No activity, metrics or theme spacing may add collapsed rows."""
    person = {"name": "alice", "activity": {"state": "working", "since": time.time()-180, "updated_at": time.time()},
              "stats": {"model": "model", "worker": {"model": "worker-model"}, "context_pct": 70,
              "quotas": {"5h": {"used_pct": 20}}, "quota_observed_at": time.time()}}
    for width in (12, 38, 49, 100):
        rows = tui._participant_card(person, width, who="▸○ alice (you)", state="online",
            online=True, selected=False, fields=list(tui.participant_metrics.FIELDS),
            detailed=False, doing="a long activity that previously added another row")
        assert len(rows) == 1
        assert rows[0].text.startswith(" ")
        assert tui._w(rows[0].text) <= width
        assert rows[0].edge and rows[0].head == len(rows[0].text)
    assert "m:model" in rows[0].text and "w◌:worker-model" in rows[0].text
    assert "working 3m" in rows[0].text


def test_roster_bottom_never_starts_following_heartbeats():
    """Wheel, arrow and End must not pin participant cards to a moving tail."""
    panel = tui.Tui(SimpleNamespace(events=[]), view="roster")
    panel.roster.rows, panel.roster.total = 4, 10
    for key in (curses.KEY_DOWN, curses.KEY_END, ord("]")):
        panel.roster.offset = 6
        panel.handle(key)
        assert not panel.roster.follow
        panel.roster.total = 20
        panel.roster.settle()
        assert panel.roster.offset == 6
        panel.roster.total = 10


def test_solo_local_stats_survive_a_missing_hub_roster():
    """Quota must be accessible before peers join or a snapshot comes back."""
    profile = SessionProfile(session_id="s", url="u", name="me", host_name="me", token="t", home="/tmp")
    model = tui.Model(profile)
    model.own_stats = {"quotas": {"7d": {"used_pct": 42}}, "context_pct": 50}
    person, = model.participants()
    assert person["name"] == "me" and person["stats"] == model.own_stats
    assert not person["connected"]


def test_rewrapping_preserves_the_row_inside_a_message(monkeypatch):
    """A redraw should not jump from a message body back to its heading."""
    panel = tui.Tui(SimpleNamespace(events=[], profile=SimpleNamespace(name="me")))
    rows = [tui.Row(str(i), tui.C_TEXT, seq=1) for i in range(15)]
    monkeypatch.setattr(tui, "conversation_rows", lambda *args: rows)
    panel._conversation(60)
    panel.chat.follow = False
    panel.chat.offset = 7
    panel._conversation(40)
    assert panel.chat.offset == 7


def test_automatic_colours_remain_readable_on_light_and_dark_backgrounds(monkeypatch):
    """Bright random identity colours must not disappear on a light theme."""
    import curses
    monkeypatch.setattr(curses, 'COLORS', 256, raising=False)
    levels = (0, 95, 135, 175, 215, 255)
    def rgb(index):
        if index == 0:
            return (0, 0, 0)
        if index == 15:
            return (1, 1, 1)
        index -= 16
        return tuple(levels[n] / 255 for n in (index // 36, index // 6 % 6, index % 6))
    monkeypatch.setattr(curses, 'color_content', lambda index: tuple(round(v * 1000) for v in rgb(index)))
    def lum(values):
        return sum((v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4) * w
                   for v, w in zip(values, (.2126, .7152, .0722)))
    for background in (0, 15):
        for colour in tui.SPEAKER_COLORS_256:
            adjusted = tui._readable_colour(colour, background)
            a, b = lum(rgb(adjusted)), lum(rgb(background))
            assert (max(a, b) + .05) / (min(a, b) + .05) >= 4.5


def test_keyboard_can_switch_panes_without_moving_either_viewport():
    panel = tui.Tui(SimpleNamespace(events=[]), view='both')
    panel.chat.offset, panel.roster.offset = 7, 3
    for key, wanted in [(ord('1'), 'roster'), (ord('2'), 'chat'), (9, 'roster'), (curses.KEY_BTAB, 'chat')]:
        panel.handle(key)
        assert panel.focus == wanted
        assert (panel.chat.offset, panel.roster.offset) == (7, 3)


def test_expanded_content_has_padding_and_packs_related_short_facts():
    person = {'name': 'alice', 'stats': {'model': 'm', 'context_pct': 20, 'context_tokens': 200,
              'context_limit': 1000, 'model_observed_at': time.time(), 'worker': {'model': 'w'}}}
    rows = tui._participant_card(person, 80, who='▾○ alice', state='online', online=True,
        selected=False, fields=['model', 'context', 'worker'], detailed=True)
    assert 'm:m' in rows[0].text and 'w◌:w' in rows[0].text
    assert all(row.text.startswith('   ') for row in rows[1:] if row.text.strip())
    assert any('model m · model ' in row.text for row in rows)
