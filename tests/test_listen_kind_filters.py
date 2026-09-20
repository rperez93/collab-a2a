from collab import cli
from collab.client.inbox import Inbox
from collab.protocol import Envelope


def test_explicit_full_listing_filter(profile, monkeypatch, capsys):
    box = Inbox(profile.dir)
    for seq, kind in enumerate(('chat', 'activity', 'task'), 1):
        box.record(Envelope(seq=seq, kind=kind, sender='other', text='synthetic-'+kind))
    monkeypatch.setattr(cli, '_require_profile', lambda args: profile)
    assert cli.main(['listen', '--no-activity']) == 0
    output = capsys.readouterr().out
    assert 'synthetic-chat' in output and 'synthetic-activity' not in output
    assert cli.main(['listen', '--kind', 'chat']) == 0
    output = capsys.readouterr().out
    assert 'synthetic-chat' in output and 'synthetic-task' not in output
    assert box.unread_count() == 3
    box.close()


def test_notice_filter_is_before_page_limit(tmp_path):
    box = Inbox(tmp_path)
    for seq in range(1, 103):
        box.record(Envelope(seq=seq, kind='task' if seq < 102 else 'chat', text='x'))
    assert [e.seq for e in box.notice_events(limit=1, kinds=('chat',))] == [102]
    assert box.unread_count() == 102
    box.close()
