"""R4: each participant's skill list is visible; a peer may ask, the human consents."""
import argparse
from types import SimpleNamespace

from collab import cli
from collab.client.worker_service import skill_requests
from collab.protocol import Envelope
from collab.skill_sharing import MAX_LISTED, sanitise_list


def test_the_list_is_names_and_short_descriptions_only_and_bounded():
    raw = [{'name': 'ok-skill', 'description': 'line one\nline two ' + 'x' * 400, 'content': 'secret'},
           {'name': 'Bad Name', 'description': 'dropped'}, 'junk', {'name': 'x' * 70}]
    clean = sanitise_list(raw * 1)
    assert clean == [{'name': 'ok-skill', 'description': ('line one line two ' + 'x' * 400)[:160]}]
    assert len(sanitise_list([{'name': f's{i}', 'description': ''} for i in range(MAX_LISTED + 50)])) == MAX_LISTED
    assert sanitise_list({'name': 'not-a-list'}) == []


def test_the_hub_keeps_the_list_off_the_roster_and_serves_it_on_demand(client, host_headers):
    skills = [{'name': 'collab-join', 'description': 'Join a session'},
              {'name': 'review', 'description': 'Review a diff'}]
    assert client.post('/ext/collab/v1/stats', headers=host_headers, json={'skills': skills}).status_code == 200
    roster = client.get('/ext/collab/v1/participants', headers=host_headers).json()['participants']
    assert roster[0]['skills_count'] == 2 and 'skills' not in roster[0]
    lists = client.get('/ext/collab/v1/participant-skills', headers=host_headers).json()
    assert lists['untrusted'] and lists['participants'][0]['skills'] == skills
    client.post('/ext/collab/v1/stats', headers=host_headers, json={'skills': []})
    assert client.get('/ext/collab/v1/participants', headers=host_headers).json()['participants'][0]['skills_count'] == 0


def test_only_the_human_can_consent_a_worker_always_escalates_a_request():
    ask = {'skill_request': {'id': 'sr_1', 'skill': 'review', 'args': 'PR 7'}}
    events = [SimpleNamespace(seq=4, sender='bob', to='alice', body=ask),
              SimpleNamespace(seq=5, sender='bob', to='carol', body=ask),
              SimpleNamespace(seq=6, sender='bob', to='alice', body={})]
    added = skill_requests(events, escalations=[], me='alice')
    assert [(e['seq'], e['reason']) for e in added] == [(4, 'scope')]
    assert 'Only your human can consent' in added[0]['question']
    assert skill_requests(events, escalations=[{'seq': 4}], me='alice') == []


class Hub:
    def __init__(self, history):
        self.history_events, self.sent = history, []
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def history(self, limit=50): return self.history_events
    def send(self, env):
        env.seq = 100 + len(self.sent)
        env.sender = self.as_name
        self.sent.append(env)
        self.history_events.append(env)


def run(monkeypatch, hub, action, target=None, rest=(), to=None, name='alice'):
    profile = SimpleNamespace(name=name, participant_id='')
    monkeypatch.setattr(cli, '_require_profile', lambda a: profile)
    monkeypatch.setattr(cli, '_require_own_profile', lambda a: profile)
    monkeypatch.setattr(cli, '_client', lambda p: hub)
    hub.as_name = name
    text = ' '.join(rest) or None
    return cli.cmd_skills(argparse.Namespace(action=action, target=target, args=text, note=text,
                                             to=to, json=True))


def test_ask_accept_done_is_a_consented_round_trip(monkeypatch):
    hub = Hub([])
    assert run(monkeypatch, hub, 'ask', 'review', ['PR', '7'], to='bob', name='alice') == 0
    request = hub.sent[-1]
    assert request.to == 'bob' and request.body['skill_request']['skill'] == 'review'
    request_id = request.body['skill_request']['id']
    assert run(monkeypatch, hub, 'done', request_id, ['ran'], name='bob') == 1, 'done before consent'
    assert run(monkeypatch, hub, 'accept', request_id, name='bob') == 0
    assert hub.sent[-1].to == 'alice' and hub.sent[-1].body['skill_response']['state'] == 'accepted'
    assert run(monkeypatch, hub, 'decline', request_id, name='bob') == 1, 'already accepted'
    assert run(monkeypatch, hub, 'done', request_id, ['no', 'findings'], name='bob') == 0
    assert hub.sent[-1].body['skill_response'] == {'id': request_id, 'state': 'done', 'text': 'no findings'}
    assert run(monkeypatch, hub, 'accept', request_id, name='carol') == 1, 'not addressed to carol'


def test_ask_refuses_a_malformed_skill_or_missing_recipient(monkeypatch):
    hub = Hub([])
    assert run(monkeypatch, hub, 'ask', 'Not A Skill', to='bob') == 1
    assert run(monkeypatch, hub, 'ask', 'review') == 1
    assert hub.sent == []


def test_a_reused_request_id_does_not_inherit_consent(monkeypatch):
    hub = Hub([])
    run(monkeypatch, hub, 'ask', 'review', to='bob', name='alice')
    request_id = hub.sent[-1].body['skill_request']['id']
    run(monkeypatch, hub, 'accept', request_id, name='bob')
    # alice replays the id for a different skill, straight through /messages
    hub.history_events.append(Envelope(kind='chat', sender='alice', to='bob', seq=999, text='x',
                                       body={'skill_request': {'id': request_id, 'skill': 'deploy'}}))
    carol = Envelope(kind='chat', sender='carol', to='bob', seq=1000, text='x',
                     body={'skill_request': {'id': request_id, 'skill': 'deploy'}})
    hub.history_events.append(carol)
    found = cli._skill_requests(hub, SimpleNamespace(name='bob', participant_id=''))
    assert [(r['from'], r['skill'], r['state']) for r in found] == [
        ('alice', 'review', 'accepted'), ('carol', 'deploy', 'pending')]
    assert run(monkeypatch, hub, 'accept', request_id, name='bob') == 1, 'ambiguous id refused'


def test_asking_yourself_is_refused(monkeypatch):
    hub = Hub([])
    assert run(monkeypatch, hub, 'ask', 'review', to='alice', name='alice') == 1
    assert hub.sent == []


def test_a_flood_of_requests_cannot_break_the_worker_turn():
    ask = {'skill_request': {'id': 'sr', 'skill': 'review'}}
    events = [SimpleNamespace(seq=i, sender='bob', to='alice', to_id='p_a', body=ask) for i in range(1, 60)]
    added = skill_requests(events, escalations=[{'seq': 0}] * 5, me='renamed', me_id='p_a')
    assert len(added) == 27 and 'More skill requests' in added[-1]['question']


def test_the_list_is_shared_by_default_and_host_join_say_so(capsys, monkeypatch):
    from collab import runtime_settings
    assert runtime_settings.get('share_skill_list') is True
    cli._skill_list_notice()
    assert 'share_skill_list false' in capsys.readouterr().out
    runtime_settings.set_value('share_skill_list', False)
    cli._skill_list_notice()
    assert capsys.readouterr().out == ''
