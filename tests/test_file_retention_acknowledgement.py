"""A host must explicitly acknowledge the requested retention policy."""
import contextlib
from types import SimpleNamespace

import pytest
from collab import cli


@pytest.mark.parametrize('value',[None,'false',0,1,{},[]])
def test_malformed_retention_metadata_never_claims_durable_availability(value):
    """Truthiness is not an acknowledgement that a host retained the artifact."""
    assert cli._file_retention_status({'kept':value,'state':'available'})=='retention unconfirmed by host'
    assert cli._file_retention_status({'kept':True,'state':'available'})=='kept until withdrawal'


@pytest.mark.parametrize('ack',[True,'false'])
def test_actual_temporary_upload_warns_when_ack_is_wrong_or_malformed(tmp_path,monkeypatch,capsys,ack):
    """A temporary request must not silently accept durable or truthy-string metadata."""
    path=tmp_path/'review.md';path.write_text('review')
    record={'id':'f_synthetic','name':'review.md','size':6,'sender':'alice','kept':ack,'state':'available'}
    monkeypatch.setattr(cli,'_require_own_profile',lambda args:SimpleNamespace(room='general'))
    monkeypatch.setattr(cli,'_client',lambda p:contextlib.nullcontext(SimpleNamespace(upload_file=lambda *a,**k:record)))
    assert cli.cmd_file(cli.build_parser().parse_args(['file','send',str(path),'--temporary']))==0
    output=capsys.readouterr()
    assert 'did not confirm requested retention/source' in output.err
    assert 'Keep your original' in output.err
    if ack=='false':
        assert 'retention unconfirmed' in output.out
        assert 'kept until withdrawal' not in output.out
        assert 'withdraw explicitly' not in output.out


@pytest.mark.parametrize('flag,warned', [(['--temporary'], False), (['--keep'], True), ([], False)])
def test_an_older_hub_that_never_answers_kept_is_flagged_only_when_keeping_was_asked(tmp_path, monkeypatch, capsys, flag, warned):
    """Lead gate on #214: a pre-2.3.0 hub answers without 'kept'. Only an
    explicit --keep (or a source) can then go unconfirmed; a temporary file,
    or one left to the default, is not flagged."""
    path = tmp_path / 'notes.txt'; path.write_text('x')
    record = {'id': 'f_old', 'name': 'notes.txt', 'size': 1, 'sender': 'alice', 'state': 'available'}
    monkeypatch.setattr(cli, '_require_own_profile', lambda args: SimpleNamespace(room='general'))
    monkeypatch.setattr(cli, '_client', lambda p: contextlib.nullcontext(SimpleNamespace(upload_file=lambda *a, **k: record)))
    assert cli.cmd_file(cli.build_parser().parse_args(['file', 'send', str(path), *flag])) == 0
    assert ('did not confirm requested retention/source' in capsys.readouterr().err) is warned
