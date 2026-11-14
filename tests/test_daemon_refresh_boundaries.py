"""Awaited peer intake must not outlive its route authority or sharing consent."""
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from collab import routes, runtime_settings
from collab.client import daemon as module
from collab.config import SessionProfile


def instance(tmp_path):
    profile = SessionProfile('s_boundaries', 'http://old', 'viewer', 'host', 'old-token', home=str(tmp_path))
    daemon = module.Daemon(profile)
    daemon.paths.root.mkdir(parents=True, exist_ok=True)
    return daemon


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['url', 'token', 'pin'])
@pytest.mark.parametrize('status', [200, 304, 500])
async def test_an_obsolete_skill_response_cannot_mutate_cache_or_retry_state(tmp_path, monkeypatch, change, status):
    """Even equal counts and valid old ETags cannot authorize old-route cache writes."""
    daemon = instance(tmp_path)
    pins = {'value': 'old-pin'}
    monkeypatch.setattr(routes, 'generation', lambda p: (p.url, p.token, 'https://127.0.0.1', pins['value']))
    module.Daemon._skill_refresh_target(daemon)
    daemon._skills_etag = 'old-etag'
    daemon._skills_signature = ('old-signature',)
    daemon._skills_at, daemon._skills_retry_at = 100, 123
    daemon.paths.skills.write_text('{"existing":"keep"}')
    async def transport(request):
        if change == 'pin':
            pins['value'] = 'new-pin'
        else:
            setattr(daemon.profile, change, 'http://new' if change == 'url' else 'new-token')
        return httpx.Response(status, json={'participants': [{'name': 'old-route-peer'}]}, headers={'etag': 'bad-tag'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        await daemon._fetch_skills(client, ('new-signature',))
    assert daemon.paths.skills.read_text() == '{"existing":"keep"}'
    assert daemon._skills_etag == 'old-etag'
    assert daemon._skills_signature == ('old-signature',)
    assert daemon._skills_at == 100 and daemon._skills_retry_at == 123


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['url', 'token', 'pin'])
async def test_a_new_skill_authority_resets_unchanged_count_timer_backoff_and_validator(tmp_path, monkeypatch, change):
    """A new authenticated route needs a fresh GET, even within the ten-minute cache."""
    daemon = instance(tmp_path)
    pin = {'value': 'old'}
    monkeypatch.setattr(routes, 'generation', lambda p: (p.url, p.token, pin['value']))
    daemon.snapshot = {'participants': [{'id': 'p1', 'skills_count': 1}]}
    scheduled = []
    daemon._schedule_refresh = lambda name, op: scheduled.append(op)
    module.Daemon._skill_refresh_target(daemon)
    daemon._skills_signature = (('p1', 1),)
    daemon._skills_at = time.time()
    daemon._skills_retry_at = time.time() + 600
    daemon._skills_etag = 'old-etag'
    if change == 'pin':
        pin['value'] = 'new'
    else:
        setattr(daemon.profile, change, 'http://new' if change == 'url' else 'new-token')
    daemon._maybe_refresh_skills(None)
    assert len(scheduled) == 1
    seen = []
    async def transport(request):
        seen.append(request.headers.get('if-none-match'))
        return httpx.Response(200, json={'participants': []}, headers={'etag': 'new-tag'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        await daemon._fetch_skills(client, (('p1', 1),))
    assert seen == [None] and daemon._skills_etag == 'new-tag'
    assert daemon._skills_retry_at == 0


@pytest.mark.asyncio
async def test_successful_roster_304_still_services_the_skill_refresh_timer(tmp_path):
    """Conditional roster reads must not suppress the independent skill-cache timer."""
    daemon = instance(tmp_path)
    daemon.snapshot = {'participants': [{'id': 'p1', 'skills_count': 1}]}
    daemon._snapshot_target = (daemon.profile.url, daemon.profile.token)
    daemon._snapshot_etag = 'roster-etag'
    module.Daemon._skill_refresh_target(daemon)
    daemon._skills_signature = (('p1', 1),)
    daemon._skills_at = time.time() - module.SKILLS_REFRESH - 1
    scheduled = []
    daemon._schedule_refresh = lambda name, op: scheduled.append(name)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(304))) as client:
        await daemon._fetch_snapshot(client)
    assert scheduled == ['skills']


@pytest.mark.asyncio
@pytest.mark.parametrize('stats_at_entry', [True, False])
@pytest.mark.parametrize('revoke_skills', [True, False])
@pytest.mark.parametrize('response_status', [200, 500])
async def test_sharing_is_checked_after_the_skill_scan_before_publication(tmp_path, monkeypatch, stats_at_entry, revoke_skills, response_status):
    """An optout during a scan must suppress stats without revoking separate skill consent."""
    daemon = instance(tmp_path)
    consent = {'stats': stats_at_entry}
    monkeypatch.setattr(module, 'share_stats_enabled', lambda: consent['stats'])
    monkeypatch.setattr(module, 'read_stats', lambda p: {'model': 'private-model'})
    monkeypatch.setattr('collab.worker.metrics', lambda root: None)
    monkeypatch.setattr('collab.resources.collect', lambda *a: {'observed_at': time.time()})
    skills = [{'name': 'review', 'description': 'Review'}]
    async def scan():
        consent['stats'] = False
        if revoke_skills:
            runtime_settings.set_value('share_skill_list', False)
        await asyncio.sleep(0)
        return skills
    daemon._installed_skills = scan
    sent = []
    async def transport(request):
        sent.append(json.loads(request.content))
        return httpx.Response(response_status)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        await daemon._report_stats(client)
    assert sent == ([] if revoke_skills else [{'skills': skills}])
    assert daemon._last_stats == {} and daemon._stats_sent_at == 0
    assert getattr(daemon, '_skills_sent_alone', None) == (skills if not revoke_skills and response_status == 200 else None)
