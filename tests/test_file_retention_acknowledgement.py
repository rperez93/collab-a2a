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
