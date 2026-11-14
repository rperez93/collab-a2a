"""The participant card renders cached resource facts without acquiring them."""
from __future__ import annotations

import curses
import time
from types import SimpleNamespace

import pytest

from collab import config, resources
from collab.client import participant_metrics, tui


def sample(now):
    return {'observed_at': now, 'source': 'linux-procfs', 'roles': {
        role: {'complete': True, 'rss_bytes': (i + 1) * 256 * 1024**2,
               'cpu_pct': (i + 1) * 25, 'read_bps': (i + 1) * 2 * 1024**2,
               'write_bps': 1024, 'processes': 1}
        for i, role in enumerate(resources.ROLES)},
        'machine': {'memory_available_bytes': 1024**3, 'load_per_cpu': 2,
                    'warnings': ['low_memory', 'high_load']}}


def person(value):
    return {'id': 'p_owner', 'name': 'Alexandra-platform-owner', 'connected': True,
            'stats': {'resources': value, 'worker': {'pending': 2}}}


def card(data, width, fields=participant_metrics.FIELDS, detailed=True):
    return tui._participant_card(data, width, who='▾● Alexandra-platform-owner',
        state='online', online=True, selected=False, fields=fields, detailed=detailed)


@pytest.mark.parametrize('width', [38, 54, 80])
def test_expanded_resources_keep_all_formatter_facts_and_warning_styles(width):
    """Wrapping must preserve every role and caveat, including warning continuations."""
    value = sample(time.time())
    rows = card(person(value), width)
    start = next(i for i, row in enumerate(rows) if row.text.strip() == 'RESOURCES')
    details = rows[start + 1:]
    text = ' '.join(' '.join(row.text.split()) for row in details)
    for line in resources.format_lines(value, detailed=True):
        assert ' '.join(line.split()) in text
    assert all(tui._w(row.text) <= width for row in rows)
    assert all(row.participant == 'p_owner' for row in rows)
    assert rows[0].participant_header and '2 decisions pending' in rows[1].text
    warning_rows = [row for row in details if row.pair == tui.C_WARN]
    assert warning_rows and all(row.attr & curses.A_BOLD for row in warning_rows)
    assert sum(row.text.strip().startswith('WARNING:') for row in warning_rows) >= 5
    # All wrapped fragments of each warning retain the warning style.
    cursor = 0
    detail_rows = [row for row in details if row.text.strip()]
    for line in resources.format_lines(value, detailed=True):
        parts = tui._wrap(line, width - min(tui._current_theme()['roster_indent'], width - 4))
        for _ in parts:
            assert (detail_rows[cursor].pair == tui.C_WARN) == line.startswith('WARNING:')
            cursor += 1


@pytest.mark.parametrize('width', [38, 54, 80])
@pytest.mark.parametrize('value', [None, {}, [], 'malformed', {'observed_at': True},
                                    {'observed_at': 1}, {'observed_at': float('nan')}])
def test_missing_stale_or_malformed_samples_remain_unknown(width, value):
    """A heartbeat or expansion cannot turn absent resource measurements into zero."""
    rows = card(person(value), width)
    text = ' '.join(' '.join(row.text.split()) for row in rows)
    assert 'resources unknown (missing or stale sample)' in text
    assert 'RSS 0' not in text
    assert all(tui._w(row.text) <= width for row in rows)


@pytest.mark.parametrize('fields', [(), participant_metrics.IDENTITY_FIELDS])
def test_identity_or_hidden_fields_do_not_reveal_resource_telemetry(fields):
    """Expanding identity is not permission to re-enable hidden usage fields."""
    assert not any('RESOURCES' in row.text for row in card(person(sample(time.time())), 54, fields))
    collapsed = card(person(sample(time.time())), 54, detailed=False)
    assert len(collapsed) == 2 and '2 decisions pending' in collapsed[1].text


def test_resource_changes_and_staleness_use_the_actual_roster_cache(monkeypatch):
    """The existing cache clock ages samples while repeated draws do no collection."""
    clock = {'wall': 100.0, 'mono': 100.0}
    monkeypatch.setattr(tui.time, 'time', lambda: clock['wall'])
    monkeypatch.setattr(tui.time, 'monotonic', lambda: clock['mono'])
    monkeypatch.setattr(config, 'watch_participant_settings', lambda: {'fields': ['worker'], 'details': True})
    def forbidden(*args, **kwargs):
        raise AssertionError('rendering must not collect or poll')
    monkeypatch.setattr(resources, 'collect', forbidden)
    monkeypatch.setattr(resources, 'own_roots', forbidden)
    data = person(sample(56))  # age 44; stale after45 seconds
    model = SimpleNamespace(profile=SimpleNamespace(name='viewer'), events=[],
        participants=lambda: [data], roster_is_current=lambda: True, snapshot_age=lambda: '')
    panel = tui.Tui(model, view='roster')
    first = panel._roster(54)
    assert any('daemon: RSS' in row.text for row in first)
    clock.update(wall=102.0, mono=104.9)
    assert panel._roster(54) is first  # at most the existing five-second cache delay
    clock['mono'] = 105.0
    aged = panel._roster(54)
    assert aged is not first
    assert 'resources unknown (missing or stale sample)' in ' '.join(row.text.strip() for row in aged)
    data['stats']['resources'] = sample(102)
    fresh = panel._roster(54)
    assert fresh is not aged and any('daemon: RSS' in row.text for row in fresh)
    panel.participant_field_mode = 2
    assert not any('RESOURCES' in row.text for row in panel._roster(54))
