"""Offline information-link selection and bounded web followups."""
import json
import sqlite3

import pytest

from hanset_active.collect import service
from hanset_active.collect.state import State
from hanset_active.collect.web import select_subpages
from hanset_active.fetcher import FetchResult
from hanset_active.search.tavily import SearchLead, SearchOutcome
from hanset_active.util.timeutil import FakeClock
from tests.active_bot.test_collect_deepseek import enable_qwen
from tests.active_bot.test_collect_web import _settings, _targets

ROOT = 'https://venue.example'
TEXT = 'Official venue information for guests. ' * 6


def html(links='', text=TEXT):
    return f'<html><nav>{links}</nav><main>{text}</main></html>'.encode()


def setup_run(tmp_path, monkeypatch, urls=None):
    settings = _settings(tmp_path)
    settings.data_dir.mkdir()
    path = settings.data_dir / 'web_targets.json'
    _targets(path, ROOT)
    if urls:
        data = json.loads(path.read_text())
        data['targets'][0]['official_urls'] = urls
        path.write_text(json.dumps(data))
    monkeypatch.setattr(service, 'credentials_present', lambda: True)
    return settings, path


def fake_pages(monkeypatch, documents):
    calls = []

    def fetch(self, url, *, subpage=False, url_allowed=None):
        assert url_allowed is None or url_allowed(url)
        calls.append(url)
        return FetchResult(url=url, outcome='ok', status=200, content=documents[url], content_type='text/html')

    monkeypatch.setattr(service.Fetcher, 'fetch', fetch)
    return calls


def capture_extraction(monkeypatch):
    rows = []

    offer = service.WebExtraction.offer

    def capture(self, row):
        rows.append(row.copy())
        offer(self, row)

    def empty(self, documents, **kwargs):
        return {'items': [{'index': item['index'], 'claims': []} for item in documents]}

    monkeypatch.setattr(service.WebExtraction, 'offer', capture)
    monkeypatch.setattr(service.DeepSeek, 'ask', empty)
    return rows


def test_rank_and_deduplicate_links():
    links = '''<a href="/about">About</a><a href="/price?utm_source=nav#one">Pricing</a>
      <a href="/price#two">Pricing</a><a href="https://other.example/price">Price</a>
      <a href="https://naver.com/price">Price</a><a href="/rates.pdf">Rates</a>
      <a href="/rates.zip">Rates</a><a download href="/download-price">Price</a>
      <a href="/privacy/info">Info</a><a href="mailto:invalid">Info</a>
      <a href="http://venue.example/info">Info</a><a href="https://[bad/price">Price</a>
      <a href="/price?token=fixture">Price</a><a href="https://user:pass@venue.example/price">Price</a>'''
    assert select_subpages(html(links), ROOT, {'venue.example'}) == [ROOT + '/price', ROOT + '/about']
    assert select_subpages(html(links), ROOT, {'other.example'}) == []


def test_fetch_paths_deduplicate_by_identity_with_max_score_and_first_url():
    links = '''<a href="/facilities/?utm_source=nav#first">Details</a>
      <a href="/facilities#second">Sauna price</a><a href="/sauna/">Sauna</a>
      <a href="/visit/?day=2&utm_source=nav">Details</a>
      <a href="/visit?day=2#other">Guide</a><a href="/visit/?day=1">Guide</a>'''
    assert select_subpages(html(links), ROOT, {'venue.example'}, limit=None) == [
        ROOT + '/facilities/', ROOT + '/sauna/', ROOT + '/visit/?day=1', ROOT + '/visit/?day=2']


def test_sauna_pages_outrank_about_and_location():
    links = '''<a href="/about">About</a><a href="/location/">Info</a>
      <a href="/sauna/">Sauna</a><a href="/jjimjilbang/">Jjimjilbang</a>'''
    assert select_subpages(html(links), ROOT, {'venue.example'}) == [ROOT + '/jjimjilbang/', ROOT + '/sauna/']


@pytest.mark.parametrize('label, competitor', [
    ('사우나', 'Facilities'), ('찜질', 'Facilities'), ('찜질방', 'Facilities'),
    ('이용가이드', 'About'), ('Guide', 'About'), ('floor-guide', 'Facilities'), ('층별', 'About'),
])
def test_sauna_and_guide_keyword_priorities(label, competitor):
    links = f'<a href="/a">{competitor}</a><a href="/z/">{label}</a>'
    assert select_subpages(html(links), ROOT, {'venue.example'})[0] == ROOT + '/z/'


def test_query_variants_subdomains_and_portals(monkeypatch):
    monkeypatch.setattr('hanset_active.util.followups.portal_domains', lambda profile=None: {'portal.example'})
    assert select_subpages(html('<a href="/price">Price</a>'), 'https://portal.example', {'portal.example'}) == []
    links = '''<a href="/info?id=2&utm_source=nav">Info</a><a href="/info?id=1#x">Info</a>
               <a href="/info?id=1&fbclid=fixture">Info</a>'''
    assert select_subpages(html(links), ROOT, {'venue.example'}) == [ROOT + '/info?id=1', ROOT + '/info?id=2']
    assert select_subpages(html('<a href="https://book.venue.example/price">Price</a>'), ROOT,
                           {'venue.example'}) == ['https://book.venue.example/price']
    loop = 'http://127.0.0.1:12345'
    assert select_subpages(html('<a href="/price">Price</a>'), loop, {'127.0.0.1'}, testing=True) == [loop + '/price']
    # Unsafe parent identities (production loopback, redirect credential params)
    # yield no children instead of aborting the run that fetched the page.
    assert select_subpages(html(), loop, {'127.0.0.1'}) == []
    assert select_subpages(html('<a href="/price">Price</a>'), ROOT + '/main?sid=fixture',
                           {'venue.example'}) == []


@pytest.mark.parametrize('thin', [False, True])
def test_follow_before_new_roots_no_recursion_shared_allowance(tmp_path, monkeypatch, thin):
    settings, path = setup_run(tmp_path, monkeypatch, [ROOT, ROOT + '/other'])
    links = '<a href="/price/">Price</a><a href="/about/">About</a>'
    calls = fake_pages(monkeypatch, {
        ROOT: html(links, '' if thin else TEXT),
        ROOT + '/price/': html('<a href="/schedule">Schedule</a>'),
        ROOT + '/about/': html(), ROOT + '/other': html('<a href="/rates">Rates</a>'),
    })
    rows = capture_extraction(monkeypatch)
    result = service.run_web(settings, targets_path=path, clock=FakeClock(), testing=True)
    assert result['success'] and result['counts']['pages'] == 4
    assert calls == [ROOT, ROOT + '/price/', ROOT + '/about/', ROOT + '/other']
    assert all(row['source_authorization'] is None for row in rows if row['url'].endswith(('/price/', '/about/')))


@pytest.mark.parametrize('explicit_path, anchor_path', [('/price/', '/price'), ('/price', '/price/')])
def test_promoted_explicit_job_keeps_authorization(tmp_path, monkeypatch, explicit_path, anchor_path):
    settings, path = setup_run(tmp_path, monkeypatch, [ROOT, ROOT + '/other', ROOT + explicit_path])
    calls, modes = [], []

    def fetch(self, url, *, subpage=False, url_allowed=None):
        calls.append(url)
        modes.append(subpage)
        return FetchResult(url, 'ok', status=200, content=html(f'<a href="{anchor_path}">Price</a>'), content_type='text/html')

    monkeypatch.setattr(service.Fetcher, 'fetch', fetch)
    rows = capture_extraction(monkeypatch)
    assert service.run_web(settings, targets_path=path, clock=FakeClock(), testing=True)['success']
    assert calls == [ROOT, ROOT + explicit_path, ROOT + '/other']
    assert modes == [False, False, False]
    assert rows[1]['source_authorization']['source_identity'] == ROOT + '/price'


@pytest.mark.parametrize('kind', ['blocked', 'policy', 'off_domain', 'asset'])
def test_unguarded_pages_never_supply_children(tmp_path, monkeypatch, kind):
    settings, path = setup_run(tmp_path, monkeypatch)
    calls = []

    def fetch(self, url, **kwargs):
        calls.append(url)
        content = html('<a href="/price">Price</a>', 'security check required ' * 10 if kind == 'blocked' else TEXT)
        if kind == 'policy':
            content = b'<title>Privacy policy</title>' + content
        return FetchResult('https://other.example' if kind == 'off_domain' else url,
                           'ok', status=200, content=content,
                           content_type='application/pdf' if kind == 'asset' else 'text/html')

    monkeypatch.setattr(service.Fetcher, 'fetch', fetch)
    assert service.run_web(settings, targets_path=path, clock=FakeClock(), testing=True)['success']
    assert calls == [ROOT]


def test_all_authorized_parents_and_children_run_in_one_pass(tmp_path, monkeypatch):
    settings, path = setup_run(tmp_path, monkeypatch)
    data, documents = [], {}
    for n in range(8):
        root = f'https://venue{n}.example'
        data.append({'venue_id': f'fixture-{n}', 'venue_key': f'fixture-{n}', 'name': f'Fixture {n}',
                     'official_urls': [root]})
        documents.update({root: html('<a href="/price">Price</a><a href="/about">About</a>'),
                          root + '/price': html(), root + '/about': html()})
    path.write_text(json.dumps({'schema_version': 1, 'targets': data}))
    calls = fake_pages(monkeypatch, documents)
    capture_extraction(monkeypatch)
    clock = FakeClock()
    first = service.run_web(settings, targets_path=path, clock=clock, testing=True)
    assert first['success'] and len(calls) == 24
    assert sum(url.endswith(('/price', '/about')) for url in calls) == 16
    second = service.run_web(settings, targets_path=path, clock=clock, testing=True)
    assert second['reason'] == 'already_complete' and len(calls) == 24
    assert second['counts']['pages'] == 24
    assert len(set(calls)) == 24


@pytest.mark.parametrize('completed_path', ['/facilities', '/facilities/'])
def test_slash_children_use_canonical_checkpoint_and_attempted_keys(tmp_path, monkeypatch, completed_path):
    settings, path = setup_run(tmp_path, monkeypatch, [ROOT, ROOT + '/other'])
    clock = FakeClock()
    state = State(settings.data_dir, clock)
    state.complete(ROOT + completed_path, 'empty_extraction')
    calls = fake_pages(monkeypatch, {
        ROOT: html('<a href="/facilities/">Facilities</a><a href="/sauna/">Sauna</a>'),
        ROOT + '/sauna/': html(),
        ROOT + '/other': html('<a href="/facilities">Facilities</a><a href="/sauna">Sauna</a>'),
    })
    capture_extraction(monkeypatch)
    result = service.run_web(settings, targets_path=path, clock=clock, testing=True)
    assert result['success'] and result['counts']['pages'] == 3
    assert calls == [ROOT, ROOT + '/sauna/', ROOT + '/other']


def test_fake_http_slash_children_produce_grounded_sauna_claim(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tests.active_bot.test_collect_deepseek import HOURS, Session, _envelope, _hours_value

    enable_qwen(monkeypatch)
    settings, path = setup_run(tmp_path, monkeypatch)
    calls = []
    routes = {
        ROOT + '/robots.txt': (200, b'User-agent: *\nAllow: /'),
        ROOT: (200, html('<a href="/facilities/">Facilities</a><a href="/sauna/">Sauna</a>')),
        ROOT + '/facilities': (307, b''),
        ROOT + '/facilities/': (200, html()),
        ROOT + '/sauna/': (200, html(text=HOURS + ' ' + TEXT)),
    }

    def get(self, url, **kwargs):
        calls.append(url)
        assert kwargs['allow_redirects'] is False
        status, body = routes[url]
        headers = {'Content-Type': 'text/html; charset=utf-8'}
        if status == 307:
            headers['Location'] = '/facilities/'
        return SimpleNamespace(status_code=status, headers=headers,
                               iter_content=lambda size: iter([body]), close=lambda: None)

    monkeypatch.setattr('requests.sessions.Session.get', get)
    session = Session(_envelope({'items': [
        {'index': 0, 'claims': []},
        {'index': 1, 'claims': [{'field': 'hours', 'value': _hours_value(), 'quote': HOURS}]},
        {'index': 2, 'claims': []},
    ]}))
    clock = FakeClock()
    first = service.run_web(settings, targets_path=path, clock=clock, qwen_session=session, testing=True)
    assert first['success'] and first['collected'] and first['counts']['pages'] == 3
    assert calls == [ROOT, ROOT + '/sauna/', ROOT + '/facilities/']
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        assert db.execute('SELECT source_url,status,rights,evidence_origin FROM claims').fetchone() == (
            ROOT + '/sauna/', 'source_claim', 'internal_review_only', 'web_verbatim')
    again = service.run_web(settings, targets_path=path, clock=clock, qwen_session=session, testing=True)
    assert again['reason'] == 'already_complete' and len(calls) == 3
    clock.sleep(86400)
    tomorrow = service.run_web(settings, targets_path=path, clock=clock, qwen_session=session, testing=True)
    assert tomorrow['success'] and tomorrow['counts']['pages'] == 3
    assert sum(page['disposition'] == 'unchanged' for page in tomorrow['pages']) == 3
    assert len(session.calls) == 1


def test_real_fetcher_loopback_children_claims_and_duplicate(server, tmp_path, monkeypatch):
    from tests.active_bot.test_collect_deepseek import HOURS
    enable_qwen(monkeypatch)
    settings, path = setup_run(tmp_path, monkeypatch, [server.base + '/root'])
    server.add('/root', html('<a href="/price">Price</a>'))
    server.add('/price', html(text=HOURS + ' ' + TEXT))
    clock = FakeClock()
    # Return empty extraction for the root and a grounded claim for the child.
    from tests.active_bot.test_collect_deepseek import Session, _envelope, _hours_value
    session = Session(_envelope({'items': [{'index': 0, 'claims': []}, {'index': 1, 'claims': [
        {'field': 'hours', 'value': _hours_value(), 'quote': HOURS}]}]}))
    result = service.run_web(settings, targets_path=path, clock=clock, qwen_session=session, testing=True)
    assert result['success'] and result['counts']['pages'] == 2 and result['collected']
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        claim = db.execute('SELECT source_url,status,rights,evidence_origin FROM claims').fetchone()
        assert claim == (server.base + '/price', 'source_claim', 'internal_review_only', 'web_verbatim')
        assert db.execute('SELECT count(*) FROM web_change_observations').fetchone()[0] == 1
    clock.sleep(86400)
    again = service.run_web(settings, targets_path=path, clock=clock, qwen_session=session, testing=True)
    assert again['success'] and again['counts']['pages'] == 2
    assert sum(page['disposition'] == 'unchanged' for page in again['pages']) == 2
    assert len(session.calls) == 1


def test_explicit_receipt_identity_survives_canonical_queue_dedup(server, tmp_path, monkeypatch):
    from tests.active_bot.test_collect_deepseek import HOURS
    from tests.active_bot.test_collect_web import _session
    enable_qwen(monkeypatch)
    url = server.base + '/hours/'
    settings, path = setup_run(tmp_path, monkeypatch, [url, url + '#info'])
    server.add('/hours', html(text=HOURS + ' ' + TEXT))
    clock, session = FakeClock(), _session()
    first = service.run_web(settings, targets_path=path, clock=clock, qwen_session=session, testing=True)
    assert first['success'] and first['counts']['pages'] == 1
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        assert db.execute('SELECT source_url FROM receipts').fetchone()[0] == url
    clock.sleep(86400)
    again = service.run_web(settings, targets_path=path, clock=clock, qwen_session=session, testing=True)
    assert again['success'] and len(session.calls) == 1
    assert any(page['url'] == url and page['disposition'] == 'unchanged' for page in again['pages'])


def _lead_target(path):
    data = json.loads(path.read_text())
    data['targets'].append({
        'venue_id': 'venue-lead', 'venue_key': 'lead-bbs', 'name': 'Lead Bbs',
        'official_urls': [], 'official_domains': ['lead.example'],
        'tavily': {'venue_id': 'venue-lead', 'field': 'hours'},
    })
    path.write_text(json.dumps(data))


def _lead_search(lead):
    def search(db, venue_id, field, *, target='official_page', clock=None):
        return SearchOutcome('ok', 'ok', leads=(SearchLead(lead, '', ''),))
    return search


@pytest.mark.parametrize('lead', [
    'https://lead.example/bbs?sid=fixture',
    'https://lead.example/bbs?key=fixture',
    'https://lead.example/' + 'a' * 500,
])
def test_unsafe_search_lead_skips_job_not_run(tmp_path, monkeypatch, lead):
    # Leads only pass scheme/domain checks upstream; legacy BBS session/key
    # params must skip the one job, never corrupt the whole run.
    settings, path = setup_run(tmp_path, monkeypatch)
    _lead_target(path)
    calls = fake_pages(monkeypatch, {ROOT: html()})
    capture_extraction(monkeypatch)
    result = service.run_web(settings, targets_path=path, clock=FakeClock(),
                             search=_lead_search(lead), testing=True)
    assert result['success'] and result['reason'] != 'state_corrupt'
    assert calls == [ROOT] and result['counts']['pages'] == 1
    assert result['rejections'] == {'unsafe_url_skipped': 1}
    assert {'url': '', 'disposition': 'unsafe_url_skipped'} in result['pages']
    assert lead not in json.dumps(result)


def test_all_unsafe_leads_stop_before_credentials_and_fetch(tmp_path, monkeypatch):
    settings, path = setup_run(tmp_path, monkeypatch)
    _lead_target(path)
    data = json.loads(path.read_text())
    data['targets'] = [data['targets'][-1]]
    path.write_text(json.dumps(data))
    monkeypatch.setattr(service, 'credentials_present',
                        lambda: (_ for _ in ()).throw(AssertionError('credentials checked')))
    calls = fake_pages(monkeypatch, {})
    result = service.run_web(settings, targets_path=path, clock=FakeClock(),
                             search=_lead_search('https://lead.example/bbs?token=fixture'), testing=True)
    assert result['reason'] == 'unsafe_url_skipped' and not result['success'] and not calls
    assert result['rejections'] == {'unsafe_url_skipped': 1}


def test_redirect_with_session_param_skips_destination(tmp_path, monkeypatch):
    from types import SimpleNamespace

    settings, path = setup_run(tmp_path, monkeypatch)
    calls = []

    def get(self, url, **kwargs):
        calls.append(url)
        if url == ROOT + '/robots.txt':
            return SimpleNamespace(status_code=404, headers={}, iter_content=lambda size: (), close=lambda: None)
        assert url == ROOT
        return SimpleNamespace(status_code=302, headers={'Location': '/main?sid=fixture'},
                               iter_content=lambda size: (), close=lambda: None)

    monkeypatch.setattr('requests.sessions.Session.get', get)
    rows = capture_extraction(monkeypatch)
    result = service.run_web(settings, targets_path=path, clock=FakeClock(), testing=True)
    assert result['success'] and result['pages'][-1] == {'url': ROOT, 'disposition': 'denied'}
    assert calls == [ROOT] and not rows


def _defer_children(settings, clock, paths):
    from hanset_active.collect.store import connect, digest, remember_web_target_observation
    db = connect(settings.data_dir)
    try:
        for path in paths:
            remember_web_target_observation(
                db, ROOT + path, 'empty_extraction', content_hash=digest(TEXT),
                observed_at=clock.now().isoformat())
    finally:
        db.close()
    clock.sleep(86400)


def test_deferred_children_use_slots_and_yield_to_fresh_roots(tmp_path, monkeypatch):
    settings, path = setup_run(tmp_path, monkeypatch, [ROOT, ROOT + '/manual'])
    clock = FakeClock()
    _defer_children(settings, clock, ['/price', '/rates'])
    calls = fake_pages(monkeypatch, {
        ROOT: html('<a href="/price">Price</a><a href="/rates">Rates</a>'
                   '<a href="/about">About</a>'),
        ROOT + '/about': html(), ROOT + '/manual': html(), ROOT + '/price': html(),
    })
    rows = capture_extraction(monkeypatch)
    result = service.run_web(settings, targets_path=path, clock=clock, testing=True)
    # The fresh third-ranked child gets a slot, then the first deferred child.
    # No third child is queued, and fresh explicit jobs run before deferrals.
    expected = [ROOT, ROOT + '/about', ROOT + '/manual', ROOT + '/price']
    assert result['success'] and calls == expected
    assert ROOT + '/rates' not in calls
    assert result['rejections'] == {'recent_unchanged_deferred': 1}
    children = [row for row in rows if row['depth'] == 1]
    assert len(children) == 2
    assert all(row['source_authorization'] is None for row in children)


@pytest.mark.parametrize('explicit_path,anchor_path', [('/price/', '/price'), ('/price', '/price/')])
def test_deferred_explicit_child_promotion_preserves_tier_and_authorization(
        tmp_path, monkeypatch, explicit_path, anchor_path):
    later = ROOT + '/hours'
    settings, path = setup_run(tmp_path, monkeypatch, [ROOT, ROOT + explicit_path, later])
    data = json.loads(path.read_text())
    data['targets'].append({'venue_id': 'venue-2', 'venue_key': 'fixture-2',
                            'name': 'Fixture Other', 'official_urls': ['https://other.example']})
    path.write_text(json.dumps(data))
    clock = FakeClock()
    _defer_children(settings, clock, [anchor_path, '/hours'])
    calls, modes = [], []

    def fetch(self, url, *, subpage=False, url_allowed=None):
        calls.append(url)
        modes.append(subpage)
        links = f'<a href="{anchor_path}">Price</a>' if url == ROOT else ''
        return FetchResult(url, 'ok', status=200,
                           content=html(links), content_type='text/html')

    monkeypatch.setattr(service.Fetcher, 'fetch', fetch)
    rows = capture_extraction(monkeypatch)
    result = service.run_web(settings, targets_path=path, clock=clock, testing=True)
    assert result['success'] and calls == [ROOT, 'https://other.example', ROOT + explicit_path, later]
    assert modes == [False, False, False, False]
    promoted = rows[2]
    assert promoted['url'] == ROOT + explicit_path and promoted['depth'] == 1
    assert promoted['source_authorization']['source_identity'] == ROOT + '/price'
    assert result['rejections'] == {'recent_unchanged_deferred': 2}


def test_deferred_child_runs_after_unseen_job_from_another_target(tmp_path, monkeypatch):
    settings, path = setup_run(tmp_path, monkeypatch)
    data = json.loads(path.read_text())
    other = 'https://other.example'
    data['targets'][0].update(venue_id='fixture-01', venue_key='fixture-01', name='Fixture One')
    data['targets'].append({'venue_id': 'fixture-02', 'venue_key': 'fixture-02',
                            'name': 'Fixture Two', 'official_urls': [other]})
    path.write_text(json.dumps(data))
    clock = FakeClock()
    _defer_children(settings, clock, ['/price'])
    calls = fake_pages(monkeypatch, {
        ROOT: html('<a href="/price">Price</a>'), ROOT + '/price': html(), other: html(),
    })
    capture_extraction(monkeypatch)
    result = service.run_web(settings, targets_path=path, clock=clock, testing=True)
    assert result['success'] and calls == [ROOT, other, ROOT + '/price']
    assert result['rejections'] == {'recent_unchanged_deferred': 1}
    assert result['counts']['committed'] == 3


def test_discovered_child_retries_with_original_slash_after_parent_commits(tmp_path, monkeypatch):
    from tests.active_bot.test_collect_deepseek import _envelope
    from tests.active_bot.test_collect_web import WebModel

    enable_qwen(monkeypatch)
    parent = ROOT + '/home'
    child = ROOT + '/price/'
    settings, path = setup_run(tmp_path, monkeypatch, [parent])
    calls = fake_pages(monkeypatch, {
        parent: html('<a href="/price/">Price</a>'),
        child: html(),
    })
    clock = FakeClock()
    model = WebModel(lambda items, attempt: _envelope({'items': [
        {'index': item['index'], 'claims': []} for item in items
    ] if attempt in (2, 4) else []}))
    first = service.run_web(settings, targets_path=path, clock=clock, qwen_session=model, testing=True)
    assert first['counts']['committed'] == first['counts']['pending'] == 1
    assert calls == [parent, child]
    second = service.run_web(settings, targets_path=path, clock=clock, qwen_session=model, testing=True)
    assert second['success'] and second['counts']['committed'] == 1
    assert calls == [parent, child, child]
