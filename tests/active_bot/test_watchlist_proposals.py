"""Only already-fetched official anchors may produce disabled proposals."""
import json
from types import SimpleNamespace

import pytest

from hanset_active.collect.service import run_web
from hanset_active.collect.targets import source_authorization
from hanset_active.collect.watchlist_proposals import (
    PROPOSAL_KEYS, classify, inspect_page, write_proposals,
)
from tests.active_bot.test_collect_deepseek import Session, _envelope, enable_qwen
from tests.active_bot.test_collect_plan import env, save_targets, venue, watch


STAMP = '2026-11-10T04:15:00+09:00'


def page(target, links, *, final=None, disposition='ready', outcome='ok'):
    html = '<html><main><p>' + 'Official venue sauna opening hours information. ' * 8 + '</p></main><footer>'
    html += ''.join(f'<a href="{link}">do not persist anchor text</a>' for link in links)
    html += '</footer><script>do not persist scripts</script></html>'
    fetched = SimpleNamespace(url=final or target['urls'][0], outcome=outcome, content=html.encode())
    job = {**target, 'url': target['urls'][0]}
    job['source_authorization'] = source_authorization(target, job['url'], fetched.url)
    return inspect_page(job, fetched, observed_at=STAMP, disposition=disposition)


def test_single_official_profile_in_footer_is_eligible(env):
    _, _, db = env
    target = venue(db, 1)
    entries = page(target, ['https://www.instagram.com/fixture.venue/'])
    assert len(entries) == 1 and entries[0]['status'] == 'eligible'
    assert entries[0]['handle'] == 'fixture.venue'
    assert entries[0]['official_domain'] == 'venue1.example'
    assert set(entries[0]) == set(PROPOSAL_KEYS.split())
    assert classify(entries)[0]['reason'] == 'ok'


@pytest.mark.parametrize('change', ['third_party', 'redirect', 'not_approved', 'different_approved_domain', 'blocked', 'robots'])
def test_off_domain_or_unguarded_page_is_not_inspected(env, change):
    _, _, db = env
    target = venue(db, 1)
    final, disposition, outcome = None, 'ready', 'ok'
    if change == 'third_party':
        target['urls'] = ['https://third-party.example/']
    elif change == 'redirect':
        final = 'https://third-party.example/'
    elif change == 'not_approved':
        target['domains'] = {'different.example'}
    elif change == 'different_approved_domain':
        final = 'https://different.example/'
        target['domains'].add('different.example')
    elif change == 'blocked':
        disposition, outcome = 'blocked', 'blocked'
    else:
        disposition, outcome = 'robots_disallowed', 'robots_disallowed'
    assert not page(target, ['https://www.instagram.com/fixture.venue/'], final=final,
                    disposition=disposition, outcome=outcome)


@pytest.mark.parametrize('link', [
    'https://www.instagram.com/p/fixture/', 'https://www.instagram.com/reel/fixture/',
    'https://www.instagram.com/accounts/', 'https://instagram.com.evil.example/fixture/',
    'https://fakeinstagram.com/fixture/', 'http://www.instagram.com/fixture/',
    'https://www.instagram.com/fixture/?query=1', 'https://www.instagram.com/fixture/#fragment',
    'https://' + 'user' + '@' + 'www.instagram.com/fixture/', 'https://www.instagram.com:443/fixture/',
    'https://www.instagram.com//fixture/', 'https://www.instagram.com/explore/',
    'https://www.instagram.com/fixture/other/', 'https://www.instagram.com/',
    '//www.instagram.com/fixture/', '/instagram/fixture/',
])
def test_invalid_profile_link_cannot_be_proposed(env, link):
    _, _, db = env
    assert not page(venue(db, 1), [link])


def test_shared_handle_and_multiple_handles_need_review(env):
    _, _, db = env
    one, two = venue(db, 1), venue(db, 2)
    shared = classify(page(one, ['https://instagram.com/shared/']) + page(two, ['https://instagram.com/SHARED/']))
    assert all(e['status'] == 'rejected' and e['reason'] == 'shared_handle' for e in shared)
    ambiguous = classify(page(one, ['https://instagram.com/first/', 'https://instagram.com/second/']))
    assert len(ambiguous) == 2
    assert all(e['status'] == 'needs_review' and e['reason'] == 'ambiguous_handle_count' for e in ambiguous)


def test_artifact_merges_today_without_changing_live_watchlist(env):
    settings, _, db = env
    one, two = venue(db, 1), venue(db, 2)
    watch(settings, [one])
    path = settings.data_dir / 'watchlist.json'
    before = path.read_bytes()
    write_proposals(settings, page(one, ['https://instagram.com/shared/']), day='2026-11-10')
    write_proposals(settings, page(two, ['https://instagram.com/shared/']), day='2026-11-10')
    artifact = settings.data_dir / 'watchlist-proposals.json'
    saved = json.loads(artifact.read_text())
    assert len(saved['proposals']) == 2 and all(e['reason'] == 'shared_handle' for e in saved['proposals'])
    assert path.read_bytes() == before
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert 'do not persist' not in artifact.read_text()
    assert not {'text', 'body', 'caption', 'photo', 'anchor_text', 'follow_allowed'} & set(saved['proposals'][0])


def test_web_proposals_do_not_request_instagram(env, server, monkeypatch):
    settings, clock, db = env
    target = venue(db, 1)
    target['urls'] = [server.base + '/official']
    target['domains'] = {'127.0.0.1'}
    html = ('<html><main>' + 'Official hotel sauna information. ' * 8 + '</main>'
            '<footer><a href="https://www.instagram.com/fixture.venue/">social</a></footer></html>')
    server.add('/official', html)
    enable_qwen(monkeypatch)
    save_targets(settings.data_dir / 'web_targets.json', [target])
    session = Session(_envelope({'items': [{'index': 0, 'claims': []}]}))
    result = run_web(settings, clock=clock, qwen_session=session, testing=True)
    assert result['state'] == 'done' and len(session.calls) == 1
    assert {r['path'] for r in server.log} == {'/official'}
    proposals = json.loads((settings.data_dir / 'watchlist-proposals.json').read_text())
    assert proposals['proposals'][0]['handle'] == 'fixture.venue'
    assert not (settings.data_dir / 'watchlist.json').exists()
    assert result['budgets']['model_calls'] == 1


def test_proposal_write_failure_does_not_abort_extraction(env, server, monkeypatch):
    settings, clock, db = env
    target = venue(db, 1)
    target['urls'] = [server.base + '/official']
    target['domains'] = {'127.0.0.1'}
    html = ('<html><main>' + 'Official hotel sauna information. ' * 8 + '</main>'
            '<footer><a href="https://www.instagram.com/fixture.venue/">social</a></footer></html>')
    server.add('/official', html)
    enable_qwen(monkeypatch)
    save_targets(settings.data_dir / 'web_targets.json', [target])
    session = Session(_envelope({'items': [{'index': 0, 'claims': []}]}))

    import hanset_active.collect.service as service

    def fail_write(*args, **kwargs):
        raise OSError('injected')

    monkeypatch.setattr(service, 'write_proposals', fail_write)

    result = run_web(settings, clock=clock, qwen_session=session, testing=True)
    assert result['state'] == 'done' and len(session.calls) == 1
    assert {'url': '', 'disposition': 'proposals_invalid'} in result['pages']


def test_overflow_never_hides_a_shared_association(env):
    settings, _, db = env
    one, two = venue(db, 1), venue(db, 2)
    # The shared link is beyond the old per-page bound; it must still count.
    many = page(one, [f'https://instagram.com/fixture{i}/' for i in range(101)])
    shared = page(two, ['https://instagram.com/fixture100/'])
    assert len(many) == 101
    assert next(e for e in classify(many + shared) if e['venue_id'] == two['venue_id'])['reason'] == 'shared_handle'
    overflowing = page(one, [f'https://instagram.com/fixture{i}/' for i in range(1001)])
    write_proposals(settings, overflowing, day='2026-11-10')
    path = settings.data_dir / 'watchlist-proposals.json'
    artifact = json.loads(path.read_text())
    assert len(artifact['proposals']) <= 1000 and path.stat().st_size <= 512000
    assert all(e['reason'] == 'snapshot_overflow' for e in artifact['proposals'])
    write_proposals(settings, shared, day='2026-11-10')
    assert not any(e['status'] == 'eligible' for e in json.loads(path.read_text())['proposals'])
