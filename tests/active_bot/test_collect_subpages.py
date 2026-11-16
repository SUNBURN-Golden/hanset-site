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

    def fetch(self, url):
        calls.append(url)
        return FetchResult(url=url, outcome='ok', status=200, content=documents[url], content_type='text/html')

    monkeypatch.setattr(service.Fetcher, 'fetch', fetch)
    return calls


def capture_extraction(monkeypatch):
    rows = []

    def extract(batch, **kwargs):
        rows.extend(batch)
        for row in batch:
            kwargs['state'].complete(row['url'], 'empty_extraction')
        return [{'url': row['url'], 'disposition': 'empty_extraction'} for row in batch], 0

    monkeypatch.setattr(service, '_extract', extract)
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
    links = '<a href="/price">Price</a><a href="/about">About</a>'
    calls = fake_pages(monkeypatch, {
        ROOT: html(links, '' if thin else TEXT),
        ROOT + '/price': html('<a href="/schedule">Schedule</a>'),
        ROOT + '/about': html(), ROOT + '/other': html('<a href="/rates">Rates</a>'),
    })
    rows = capture_extraction(monkeypatch)
    result = service.run_web(settings, targets_path=path, clock=FakeClock(), testing=True)
    assert result['success'] and result['counts']['pages'] == 4
    assert calls == [ROOT, ROOT + '/price', ROOT + '/about', ROOT + '/other']
    assert all(row['source_authorization'] is None for row in rows if row['url'].endswith(('/price', '/about')))


def test_promoted_explicit_job_keeps_authorization(tmp_path, monkeypatch):
    settings, path = setup_run(tmp_path, monkeypatch, [ROOT, ROOT + '/other', ROOT + '/price'])
    calls = fake_pages(monkeypatch, {ROOT: html('<a href="/price">Price</a>'), ROOT + '/price': html(), ROOT + '/other': html()})
    rows = capture_extraction(monkeypatch)
    assert service.run_web(settings, targets_path=path, clock=FakeClock(), testing=True)['success']
    assert calls == [ROOT, ROOT + '/price', ROOT + '/other']
    assert rows[1]['source_authorization']['source_identity'] == ROOT + '/price'


@pytest.mark.parametrize('kind', ['blocked', 'excluded', 'policy', 'off_domain', 'asset'])
def test_unguarded_pages_never_supply_children(tmp_path, monkeypatch, kind):
    settings, path = setup_run(tmp_path, monkeypatch)
    calls = []

    def fetch(self, url):
        calls.append(url)
        content = html('<a href="/price">Price</a>',
                       {'blocked': 'security check required ' * 10,
                        'excluded': 'runport ' * 20}.get(kind, TEXT))
        if kind == 'policy':
            content = b'<title>Privacy policy</title>' + content
        return FetchResult('https://other.example' if kind == 'off_domain' else url,
                           'ok', status=200, content=content,
                           content_type='application/pdf' if kind == 'asset' else 'text/html')

    monkeypatch.setattr(service.Fetcher, 'fetch', fetch)
    assert service.run_web(settings, targets_path=path, clock=FakeClock(), testing=True)['success']
    assert calls == [ROOT]


def test_actual_ten_and_twenty_page_caps_include_children(tmp_path, monkeypatch):
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
    assert first['reason'] == 'run_cap' and len(calls) == 10
    assert sum(url.endswith(('/price', '/about')) for url in calls) == 6
    second = service.run_web(settings, targets_path=path, clock=clock, testing=True)
    assert second['reason'] == 'daily_cap' and len(calls) == 20
    assert second['counts']['pages'] == 20
    assert len(set(calls)) == 20


@pytest.mark.parametrize('limit', ['run', 'day', 'active'])
def test_children_respect_caps(tmp_path, monkeypatch, limit):
    settings, path = setup_run(tmp_path, monkeypatch)
    calls = fake_pages(monkeypatch, {ROOT: html('<a href="/price">Price</a><a href="/about">About</a>'),
                                     ROOT + '/price': html(), ROOT + '/about': html()})
    capture_extraction(monkeypatch)
    clock = FakeClock()
    if limit == 'run':
        monkeypatch.setattr(service, 'PAGE_RUN_CAP', 2)
    elif limit == 'day':
        state = State(settings.data_dir, clock)
        state.data['counts']['pages'] = 19
        state.save()
    else:
        def capped(self, url):
            calls.append(url)
            return FetchResult(url, 'ok', status=200, content=html('<a href="/price">Price</a>'), content_type='text/html') if url == ROOT else FetchResult(url, 'cap_reached')
        monkeypatch.setattr(service.Fetcher, 'fetch', capped)
    result = service.run_web(settings, targets_path=path, clock=clock, testing=True)
    assert result['reason'] == {'run': 'run_cap', 'day': 'daily_cap', 'active': 'active_body_cap'}[limit]
    assert len(calls) == (1 if limit == 'day' else 2)
    assert result['counts']['pages'] == {'run': 2, 'day': 20, 'active': 1}[limit]


def test_real_fetcher_loopback_children_claims_and_duplicate(server, tmp_path, monkeypatch):
    from tests.active_bot.test_collect_deepseek import HOURS, KEY
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    settings, path = setup_run(tmp_path, monkeypatch, [server.base + '/root'])
    server.add('/root', html('<a href="/price">Price</a>'))
    server.add('/price', html(text=HOURS + ' ' + TEXT))
    clock = FakeClock()
    # Return empty extraction for the root and a grounded claim for the child.
    from tests.active_bot.test_collect_deepseek import Session, _envelope, _hours_value
    session = Session(_envelope({'items': [{'index': 0, 'claims': []}, {'index': 1, 'claims': [
        {'field': 'hours', 'value': _hours_value(), 'quote': HOURS}]}]}))
    result = service.run_web(settings, targets_path=path, clock=clock, session=session, testing=True)
    assert result['success'] and result['counts']['pages'] == 2 and result['collected']
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        claim = db.execute('SELECT source_url,status,rights,evidence_origin FROM claims').fetchone()
        assert claim == (server.base + '/price', 'source_claim', 'internal_review_only', 'web_verbatim')
        assert db.execute('SELECT count(*) FROM web_change_observations').fetchone()[0] == 1
    clock.sleep(86400)
    again = service.run_web(settings, targets_path=path, clock=clock, session=session, testing=True)
    assert again['success'] and again['counts']['pages'] == 2
    assert sum(page['disposition'] == 'duplicate' for page in again['pages']) == 2
    assert len(session.calls) == 1


def test_explicit_receipt_identity_survives_canonical_queue_dedup(server, tmp_path, monkeypatch):
    from tests.active_bot.test_collect_deepseek import HOURS, KEY
    from tests.active_bot.test_collect_web import _session
    monkeypatch.setenv('DEEPSEEK_API_KEY', KEY)
    url = server.base + '/hours/'
    settings, path = setup_run(tmp_path, monkeypatch, [url, url + '#info'])
    server.add('/hours', html(text=HOURS + ' ' + TEXT))
    clock, session = FakeClock(), _session()
    first = service.run_web(settings, targets_path=path, clock=clock, session=session, testing=True)
    assert first['success'] and first['counts']['pages'] == 1
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        assert db.execute('SELECT source_url FROM receipts').fetchone()[0] == url
    clock.sleep(86400)
    again = service.run_web(settings, targets_path=path, clock=clock, session=session, testing=True)
    assert again['success'] and len(session.calls) == 1
    assert any(page['url'] == url and page['disposition'] == 'duplicate' for page in again['pages'])


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


def test_redirect_final_url_with_session_param_still_extracts(tmp_path, monkeypatch):
    # The fetcher only canonicalizes redirect targets, so a ?sid= landing URL
    # reaches selection and extraction; it must not abort the run mid-fetch.
    settings, path = setup_run(tmp_path, monkeypatch)
    final = ROOT + '/main?sid=fixture'
    calls = []

    def fetch(self, url):
        calls.append(url)
        return FetchResult(url=final, outcome='ok', status=200,
                           content=html('<a href="/price">Price</a>'), content_type='text/html')

    monkeypatch.setattr(service.Fetcher, 'fetch', fetch)
    rows = capture_extraction(monkeypatch)
    result = service.run_web(settings, targets_path=path, clock=FakeClock(), testing=True)
    assert result['success'] and result['reason'] != 'state_corrupt'
    assert calls == [ROOT] and result['counts']['pages'] == 1
    assert rows[0]['url'] == ROOT and rows[0]['final_url'] == final
