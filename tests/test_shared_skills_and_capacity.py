"""Publication is explicit data sharing; delegation remains a conditional estimate."""
from __future__ import annotations

import hashlib
import os
import time

import pytest

from collab.capacity import estimate_capacity
from collab.protocol import EXT_PREFIX
from collab.skill_sharing import read_selected


def publication(name='example', text='---\nname: example\ndescription: Selected guidance\n---\nReview me.\n'):
    return {'name': name, 'description': 'Selected guidance', 'content': text,
            'sha256': hashlib.sha256(text.encode()).hexdigest()}


def test_only_the_selected_skill_entrypoint_is_read(tmp_path):
    selected = tmp_path / 'chosen'
    selected.mkdir()
    text = '---\nname: chosen\ndescription: >\n  A folded\n  description\n---\nSelected text.\n'
    (selected / 'SKILL.md').write_text(text)
    (selected / 'private-token').write_text('must never be uploaded')
    got = read_selected(selected)
    assert got == {'name': 'chosen', 'description': 'A folded description', 'content': text,
                   'size_bytes': len(text.encode()), 'sha256': hashlib.sha256(text.encode()).hexdigest()}
    assert 'path' not in got


def test_a_selected_fifo_is_refused_without_waiting_for_a_writer(tmp_path):
    path = tmp_path / 'SKILL.md'
    os.mkfifo(path)
    started = time.monotonic()
    with pytest.raises(ValueError, match='regular'):
        read_selected(path)
    assert time.monotonic() - started < 1


def test_shared_content_is_authenticated_owned_and_review_only(client, session, host_headers):
    path = EXT_PREFIX + '/shared-skills'
    assert client.get(path).status_code == 401
    assert client.post(path, json=publication()).status_code == 401
    made = client.post(path, headers=host_headers, json=publication())
    assert made.status_code == 200, made.text
    skill = made.json()['skill']
    assert 'content' not in skill
    found = client.get(path, headers=host_headers).json()
    assert found['untrusted'] is True
    assert 'content' not in found['skills'][0]
    shown = client.get(path + '/' + skill['id'], headers=host_headers).json()
    assert shown['skill']['content'] == publication()['content']
    assert shown['untrusted'] is True and shown['entrypoint_only'] is True
    assert 'review' in shown
    session['store'].add_participant('other', 'other-token', is_host=False, meta={})
    other = {'Collab-Protocol-Major': '2', 'Collab-Version': '2.0.0', 'Authorization': 'Bearer other-token'}
    own = client.post(path, headers=other, json={**publication(), 'id': skill['id'], 'owner_id': skill['owner_id']}).json()['skill']
    assert own['id'] != skill['id'] and own['owner_id'] != skill['owner_id']
    assert client.delete(path + '/' + skill['id'], headers=other).status_code == 404
    assert client.delete(path + '/' + skill['id'], headers=host_headers).status_code == 200
    assert client.get(path + '/' + skill['id'], headers=other).status_code == 404


def test_republishing_updates_only_the_publishers_named_skill(client, host_headers):
    path = EXT_PREFIX + '/shared-skills'
    first = client.post(path, headers=host_headers, json=publication()).json()['skill']
    replacement = publication(text='new content')
    second = client.post(path, headers=host_headers, json=replacement).json()['skill']
    assert first['id'] == second['id'] and first['sha256'] != second['sha256']
    assert len(client.get(path, headers=host_headers).json()['skills']) == 1


def test_a_bad_hash_or_oversized_skill_never_creates_a_record(client, host_headers):
    path = EXT_PREFIX + '/shared-skills'
    assert client.post(path, headers=host_headers, json={**publication(), 'sha256': 'forged'}).status_code == 400
    assert client.post(path, headers=host_headers, json=publication(text='é' * 32769)).status_code == 400
    assert client.get(path, headers=host_headers).json()['skills'] == []


def test_an_owner_cannot_exceed_the_inventory_count(client, host_headers):
    path = EXT_PREFIX + '/shared-skills'
    for index in range(20):
        response = client.post(path, headers=host_headers, json=publication(name=f'entry-{index}'))
        assert response.status_code == 200, response.text
    assert client.post(path, headers=host_headers, json=publication(name='one-too-many')).status_code == 400
    assert len(client.get(path, headers=host_headers).json()['skills']) == 20


def fresh(**kwargs):
    return {'observed_at': 1000, 'source': 'synthetic',
            'quotas': {'five_hour': {'used_pct': 40}, 'seven_day': {'used_pct': 60}}, **kwargs}


def test_capacity_binds_to_the_tightest_account_window_and_active_commitments():
    got = estimate_capacity(fresh(), concurrency_limit=10, active_children=1,
                            percent_per_child=10, reserve_percent=20, now=1001)
    assert got['status'] == 'estimate'
    assert got['maximum_additional_children'] == 1
    assert got['binding_window'] == 'seven_day'
    assert got['available_slots'] == 9
    assert [row['maximum_additional_children'] for row in got['windows']] == [3, 1]


@pytest.mark.parametrize('changes', [{}, {'observed_at': 0}, {'observed_at': 1100}, {'quotas': {}}])
def test_missing_calibration_or_stale_quota_is_never_positive_capacity(changes):
    got = estimate_capacity(fresh(**changes), concurrency_limit=5, active_children=0, now=1001)
    assert got['status'] == 'unknown' and got['maximum_additional_children'] is None


def test_quota_exhaustion_proves_zero_without_cost_calibration():
    got = estimate_capacity(fresh(quotas={'week': {'used_pct': 90}}), reserve_percent=20, now=1001)
    assert got['status'] == 'known-zero' and got['maximum_additional_children'] == 0
    assert got['binding_window'] == 'week'


def test_unknown_concurrency_and_active_counts_are_not_inferred():
    got = estimate_capacity(fresh(), percent_per_child=5, now=1001)
    assert got['maximum_additional_children'] is None
    assert got['concurrency_limit'] is None and got['active_children'] is None


def test_per_window_costs_do_not_conflate_unequal_account_budgets():
    got = estimate_capacity(fresh(), concurrency_limit=9, active_children=0,
                            task_budget_percent={'five_hour': 20, 'seven_day': 5}, now=1001)
    assert got['maximum_additional_children'] == 2
    assert got['binding_window'] == 'five_hour'
    assert got['budget_source'] == 'explicit task budget'


@pytest.mark.parametrize('cost', [-1, 0, float('nan'), float('inf'), True])
def test_invalid_cost_calibration_is_refused(cost):
    with pytest.raises(ValueError, match='child budgets'):
        estimate_capacity(fresh(), percent_per_child=cost, now=1001)


def test_capacity_defaults_are_reloaded_on_each_invocation():
    from collab.runtime_settings import set_value
    set_value('delegation_max_children', 3)
    set_value('delegation_cost_per_child_pct', 10)
    set_value('delegation_reserve_pct', 0)
    assert estimate_capacity(fresh(), active_children=0, now=1001)['maximum_additional_children'] == 3
    set_value('delegation_max_children', 1)
    assert estimate_capacity(fresh(), active_children=0, now=1001)['maximum_additional_children'] == 1


def test_skill_inventory_and_content_work_through_the_actual_cli(live_server, session, tmp_path, monkeypatch, capsys):
    """Exercise parser, HTTP client, server validation and reviewed JSON output."""
    import json
    from collab import cli
    from collab.config import SessionProfile
    home = tmp_path / 'cli-state'
    monkeypatch.setenv('COLLAB_HOME', str(home))
    host = session['store'].participant_for_token(session['host_token'])
    SessionProfile(session_id='s_test', url=live_server['base'], name='alice', host_name='alice',
                   participant_id=host.id, token=session['host_token'], home=str(home)).save()
    chosen = tmp_path / 'SKILL.md'
    chosen.write_text(publication()['content'])
    assert cli.main(['skills', 'publish', str(chosen)]) == 0
    record = json.loads(capsys.readouterr().out)['skill']
    assert cli.main(['skills', 'shared']) == 0
    assert json.loads(capsys.readouterr().out)['skills'][0]['id'] == record['id']
    assert cli.main(['skills', 'show', record['id']]) == 0
    assert json.loads(capsys.readouterr().out)['untrusted'] is True
    assert cli.main(['skills', 'withdraw', record['id']]) == 0
    assert json.loads(capsys.readouterr().out)['withdrawn'] == record['id']


def test_capacity_cli_uses_the_supplied_report_without_starting_children(capsys):
    import json
    from collab import cli
    report = fresh(observed_at=time.time())
    assert cli.main(['capacity', '--report', json.dumps(report), '--limit', '5', '--active', '1',
                     '--task-budget', '10', '--reserve', '20', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['maximum_additional_children'] == 1
    assert result['status'] == 'estimate'


def test_revocation_removes_unwithdrawable_publications(session):
    store = session['store']
    person = store.add_participant('departing', 'departing-token', is_host=False, meta={})
    skill = store.publish_skill(person.id, publication())
    assert store.revoke(person.id)
    assert store.shared_skill(skill['id']) is None
    assert store._db.execute('SELECT COUNT(*) FROM shared_skills').fetchone()[0] == 0


async def test_a_flooding_skill_upload_is_refused_before_parsing_or_writing(session):
    """An arbitrary streaming request has a finite byte, CPU and memory budget."""
    import resource
    from types import SimpleNamespace
    from fastapi import HTTPException
    endpoint = next(route.endpoint for route in session['app'].routes
                    if getattr(route, 'path', '') == EXT_PREFIX + '/shared-skills'
                    and 'POST' in getattr(route, 'methods', set()))
    chunks = []
    class Flood:
        user = SimpleNamespace(is_authenticated=True, id='unused')
        async def stream(self):
            while True:
                chunks.append(1)
                yield b'x' * 65536
    started, cpu = time.monotonic(), time.process_time()
    memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    with pytest.raises(HTTPException) as refused:
        await endpoint(Flood())
    assert refused.value.status_code == 413
    assert len(chunks) == 9
    assert time.monotonic() - started < 1
    assert time.process_time() - cpu < 0.5
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - memory < 8 * 1024
    assert session['store'].shared_skills() == []


def test_a_new_cost_report_cannot_make_old_quota_fresh():
    got = estimate_capacity(fresh(observed_at=1000, quota_observed_at=100), concurrency_limit=5,
                            active_children=0, percent_per_child=5, now=1001)
    assert got['status'] == 'unknown'
    assert got['maximum_additional_children'] is None
    assert any('stale' in reason for reason in got['reasons'])


def test_total_skill_bytes_are_bounded_even_before_the_entry_limit(session):
    store = session['store']
    person = store.participant_for_token(session['host_token'])
    for index in range(8):
        store.publish_skill(person.id, publication(name=f'entry-{index}', text='x' * 65536))
    with pytest.raises(ValueError, match='512 KiB'):
        store.publish_skill(person.id, publication(name='next', text='x'))
    assert len(store.shared_skills()) == 8


def test_concurrent_skill_publications_cannot_share_the_last_free_slot(session):
    from concurrent.futures import ThreadPoolExecutor
    store = session['store']
    person = store.participant_for_token(session['host_token'])
    for index in range(19):
        store.publish_skill(person.id, publication(name=f'entry-{index}'))
    def publish(name):
        try:
            store.publish_skill(person.id, publication(name=name))
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(publish, ['first', 'second'])) == [False, True]
    assert len(store.shared_skills()) == 20
