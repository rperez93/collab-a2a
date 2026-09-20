"""CLI regressions reproduced by the independent Claude participant."""
import contextlib
import json
from types import SimpleNamespace

import pytest
from collab import cli


@pytest.mark.parametrize('command', ['task', 'project'])
def test_a_comment_after_its_id_is_not_rejected(command):
    args = cli.build_parser().parse_args([command, 'comment', '--id', 'test-id', 'hola 世界'])
    assert args.title == 'hola 世界' and args.id == 'test-id'
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([command, 'comment', '--id', 'test-id', '--typo'])


def test_file_send_json_is_one_machine_readable_record(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'small.txt'
    path.write_text('hello')
    record = {'id': 'f_test', 'name': path.name, 'size': 5}
    client = SimpleNamespace(upload_file=lambda *a, **k: record)
    monkeypatch.setattr(cli, '_require_own_profile', lambda args: SimpleNamespace(room='main'))
    monkeypatch.setattr(cli, '_client', lambda profile: contextlib.nullcontext(client))
    args = cli.build_parser().parse_args(['file', 'send', str(path), '--json'])
    assert cli.cmd_file(args) == 0
    assert json.loads(capsys.readouterr().out) == record
    assert cli._file_size(5) == '5 B'
