from collab.cli import dashboard_rows


def test_dashboard_resources_use_their_own_freshness(monkeypatch):
    monkeypatch.setattr('collab.resources.time.time',lambda:100)
    snapshot={'participants':[{'id':'p_test','name':'tester','stats':{'resources':{
        'observed_at':99,'roles':{'daemon':{'complete':True,'rss_bytes':10*1024**3}},'machine':{}}}}]}
    row=dashboard_rows(snapshot,None)[0]
    assert any('daemon RSS exceeds 1 GiB' in line for line in row['resources'])
    snapshot['participants'][0]['stats']['resources']['observed_at']=1
    assert dashboard_rows(snapshot,None)[0]['resources']==['resources unknown (missing or stale sample)']


def test_dashboard_offline_does_not_render_old_resources(monkeypatch):
    monkeypatch.setattr('collab.resources.time.time',lambda:100)
    snapshot={'participants':[{'id':'p_test','name':'tester','stats':{'resources':{
        'observed_at':99,'roles':{'daemon':{'complete':True,'rss_bytes':10*1024**3}},'machine':{}}}}]}
    assert dashboard_rows(snapshot,None,live=False)[0]['resources']==['resources unknown (missing or stale sample)']


def test_real_process_sample_survives_daemon_http_hub_and_dashboard(live_server, tmp_path, monkeypatch):
    import asyncio
    import sys
    import httpx
    import pytest
    from collab import resources
    from collab.client import daemon
    from collab.client.exclusive import stamp_for
    from collab.compatibility import request_headers
    from collab.config import SessionProfile
    if not sys.platform.startswith('linux'):
        pytest.skip('actual procfs sampling requires Linux')
    profile = SessionProfile('s_test', live_server['base'], 'alice', 'guest',
                             live_server['host_token'], home=str(tmp_path))
    instance = daemon.Daemon(profile)
    # Sample only this explicitly owned process, not a user's other services.
    monkeypatch.setattr(resources, 'own_roots', lambda profile: {'daemon': [stamp_for()]})
    monkeypatch.setattr(daemon, 'share_stats_enabled', lambda: True)
    monkeypatch.setattr(daemon.peers, 'identity', lambda: {})
    async def no_skills():
        return []
    monkeypatch.setattr(instance, '_installed_skills', no_skills)
    async def publish_and_read():
        async with httpx.AsyncClient() as client:
            await instance._report_stats(client)
            reply = await client.get(live_server['base'] + '/ext/collab/v1/participants',
                                     headers=request_headers(live_server['host_token']))
            assert reply.status_code == 200
            return reply.json()
    snapshot = asyncio.run(publish_and_read())
    person = next(p for p in snapshot['participants'] if p['name'] == 'alice')
    observed = person['stats']['resources']
    assert observed['roles']['daemon']['rss_bytes'] > 0
    assert observed['roles']['daemon']['complete'] is True
    assert 'pid' not in observed['roles']['daemon']
    row = dashboard_rows({'participants': [person]}, None)[0]
    assert any('daemon' in line and 'RSS' in line for line in row['resources'])
