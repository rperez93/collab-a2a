"""A local recovery acknowledgement must never masquerade as remote delivery."""
import argparse
import json
from types import SimpleNamespace

import pytest

from collab import cli, worker


@pytest.mark.parametrize('verbatim', [False, True])
def test_a_full_queue_recovery_is_local_and_keeps_the_original_private_answer(tmp_path, monkeypatch, capsys, verbatim):
    """The held record doubles as its notice, with no capacity growth or model answer."""
    monkeypatch.setattr(worker, 'MAX_PENDING', 1)
    state=worker.Store(tmp_path)
    state.configure({'agent':'command','command':['never-executed'],'scope':'Synthetic only'})
    assert state.commit_turn(expected_cursor=0,cursor=1,summary='',replies=[],
        escalations=[{'seq':1,'reason':'decision','question':'Synthetic question'}],
        decision_sources={1:{'sender_id':'p_original','sender':'old','room':'','broadcast':False}})
    record=state.pending()[0]['id']
    secret='PRIVATE-RECOVERY-CLI-MARKER'
    state.answer(record,secret,verbatim=False)
    state.quarantine_answers([record],reason='ambiguous identity')
    assert len(state.pending())==1 and state.pending()[0]['local_only']
    assert secret not in json.dumps(state.pending())
    profile=SimpleNamespace(dir=tmp_path,name='fixture',session_id='s_fixture')
    monkeypatch.setattr(cli,'_require_own_profile',lambda _:profile)
    args=argparse.Namespace(worker_action='reply',decision_id=record,text='Reviewed locally',
                            awaiting_human=False,verbatim=verbatim,instruct=not verbatim)
    result=cli.cmd_worker(args)
    printed=capsys.readouterr()
    assert secret not in printed.out+printed.err
    assert not state.outbox() and not state.snapshot()['answers']
    with state._db() as db:
        row=db.execute("SELECT kind,data FROM records WHERE id=?",(record,)).fetchone()
        assert row['kind']=='quarantine'
        held=json.loads(row['data'])
        assert held['text']==secret
        assert db.execute('SELECT count(*) FROM records').fetchone()[0]==1
    if verbatim:
        assert result != 0 and state.pending()
        assert not held.get('recovery_resolved')
    else:
        assert result==0 and not state.pending()
        assert held['recovery_resolved']
        assert 'recovery noted locally' in printed.out
        assert 'decision queued' not in printed.out



def test_waiting_for_human_recovery_never_starts_a_daemon_or_exposes_the_note(tmp_path, monkeypatch, capsys):
    """The generic waiting command must keep an unknown-recipient review local."""
    monkeypatch.setattr(worker,'MAX_PENDING',1)
    state=worker.Store(tmp_path)
    state.configure({'agent':'command','command':['never-executed'],'scope':'Synthetic only'})
    state.commit_turn(expected_cursor=0,cursor=1,summary='',replies=[],
        escalations=[{'seq':1,'reason':'decision','question':'Synthetic'}])
    record=state.pending()[0]['id']
    state.answer(record,'PRIVATE-HELD-ANSWER',verbatim=False)
    state.quarantine_answers([record],reason='ambiguous recipient')
    profile=SimpleNamespace(dir=tmp_path,name='fixture',session_id='s_fixture')
    monkeypatch.setattr(cli,'_require_own_profile',lambda _:profile)
    def forbidden(*args,**kwargs):raise AssertionError('local recovery started transport')
    monkeypatch.setattr(cli.onboard,'ensure_daemon',forbidden)
    args=argparse.Namespace(worker_action='reply',decision_id=record,text='PRIVATE-WAIT-NOTE',
                            awaiting_human=True,verbatim=False,instruct=False)
    assert cli.cmd_worker(args)==0
    printed=capsys.readouterr()
    assert 'no peer or model was notified' in printed.out
    assert 'PRIVATE-' not in printed.out+printed.err
    assert state.pending()[0]['awaiting_human']
    assert worker.pending_count_readonly(tmp_path)==0
    assert not state.outbox() and not state.snapshot()['answers']
    assert 'PRIVATE-' not in json.dumps(state.pending())
