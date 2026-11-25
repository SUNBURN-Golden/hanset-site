"""URL cooldowns across runs, using only fake HTTP and model responses."""
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from hanset_active.collect import CollectError, service
from hanset_active.collect.state import State
from hanset_active.collect.store import (
    connect, digest, prune_web_target_blocks, recent_web_target_block, remember_web_target_block,
)
from hanset_active.util.timeutil import FakeClock
from tests.active_bot.test_collect_subpages import (
    ROOT, capture_extraction, fake_pages, html, setup_run,
)

DAY = 86400


def fake_http(monkeypatch, routes):
    """Exercise the real fetcher, including robots and collateral domain stops."""
    requests = []
    evaluations = []
    original_init = service.Fetcher.__init__
    original_fetch = service.Fetcher.fetch

    class Session:
        def get(self, url, **kwargs):
            requests.append(url)
            status, body = routes[url]
            return SimpleNamespace(status_code=status, headers={'Content-Type': 'text/html'},
                                   iter_content=lambda size: iter([body]), close=lambda: None)

        def close(self):
            pass

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.session.close()
        self.session = Session()

    def fetch(self, url, **kwargs):
        result = original_fetch(self, url, **kwargs)
        evaluations.append((url, result.outcome, result.status))
        return result

    monkeypatch.setattr(service.Fetcher, '__init__', init)
    monkeypatch.setattr(service.Fetcher, 'fetch', fetch)
    return requests, evaluations


def blocks(settings):
    with sqlite3.connect(settings.data_dir / 'collector.sqlite3') as db:
        return db.execute('SELECT url_hash,disposition,blocked_at FROM web_target_blocks').fetchall()


@pytest.mark.parametrize('kind', ['robots_disallowed', 'login_wall', 'blocked', 'security'])
def test_genuine_holds_skip_before_credentials_and_proxy_then_expire(tmp_path, monkeypatch, kind):
    settings, path = setup_run(tmp_path, monkeypatch)
    clock = FakeClock()
    routes = {ROOT + '/robots.txt': (200, b'User-agent: *\nAllow: /\n'),
              ROOT: (200, html())}
    if kind == 'robots_disallowed':
        monkeypatch.setattr('hanset_active.fetcher.is_korean_news', lambda url: True)
        monkeypatch.setattr('hanset_active.collect.store.is_korean_news', lambda url: True)
        routes[ROOT + '/robots.txt'] = (200, b'User-agent: *\nDisallow: /\n')
    elif kind == 'login_wall':
        routes[ROOT] = (200, b'<main>Sign in<input type="password"></main>')
    elif kind == 'blocked':
        routes[ROOT] = (403, b'Access denied')
    else:
        # detect_outcome permits this; classify_page refuses the security text.
        routes[ROOT] = (200, html(text='security check required ' * 10))
    requests, evaluations = fake_http(monkeypatch, routes)
    first = service.run_web(settings, targets_path=path, clock=clock, testing=True)
    assert first['success'] and first['counts']['pages'] == 1
    saved = blocks(settings)
    assert len(saved) == 1 and saved[0][1] == ('blocked' if kind == 'security' else kind)
    clock.sleep(DAY)
    before = len(requests)
    with monkeypatch.context() as patch:
        patch.setattr(service, 'credentials_present', lambda: (_ for _ in ()).throw(AssertionError('credentials checked')))
        patch.setattr(service.web_proxy, 'configured', lambda: (_ for _ in ()).throw(AssertionError('proxy checked')))
        today = service.run_web(settings, targets_path=path, clock=clock, testing=True)
        assert today['reason'] == 'recent_block_skipped' and today['success'] and not today['collected']
        assert today['counts']['pages'] == 0 and today['budgets']['model_calls'] == 0
        assert today['rejections']['recent_block_skipped'] == 1
        assert State(settings.data_dir, clock).data['completed'] == {}
        assert len(requests) == before and blocks(settings) == saved
        clock.sleep(13 * DAY - 1)
        assert service.run_web(settings, targets_path=path, clock=clock, testing=True)['reason'] == 'recent_block_skipped'
        assert blocks(settings) == saved
    clock.sleep(1)
    retried = service.run_web(settings, targets_path=path, clock=clock, testing=True)
    assert retried['success'] and retried['counts']['pages'] == 1
    assert len(evaluations) == 2 and len(requests) > before
    assert blocks(settings)[0][2] != saved[0][2]


@pytest.mark.parametrize('first', ['robots', 'child_login'])
def test_collateral_domain_stop_never_creates_child_hold(tmp_path, monkeypatch, first):
    initial = [ROOT, ROOT + '/sauna'] if first == 'robots' else [ROOT]
    settings, path = setup_run(tmp_path, monkeypatch, initial)
    clock = FakeClock()
    if first == 'robots':
        monkeypatch.setattr('hanset_active.fetcher.is_korean_news', lambda url: True)
        monkeypatch.setattr('hanset_active.collect.store.is_korean_news', lambda url: True)
        routes = {ROOT + '/robots.txt': (200, b'User-agent: *\nDisallow: /\n'),
                  ROOT + '/sauna': (200, html())}
        blocked_url, collateral = ROOT, ROOT + '/sauna'
    else:
        routes = {ROOT + '/robots.txt': (200, b'User-agent: *\nAllow: /\n'),
                  ROOT: (200, html('<a href="/price">Price</a><a href="/about">About</a>')),
                  ROOT + '/price': (200, b'<main>Sign in<input type="password"></main>'),
                  ROOT + '/about': (200, html())}
        blocked_url, collateral = ROOT + '/price', ROOT + '/about'
    requests, evaluations = fake_http(monkeypatch, routes)
    capture_extraction(monkeypatch)
    result = service.run_web(settings, targets_path=path, clock=clock, testing=True)
    assert result['success']
    if first == 'robots':
        assert evaluations[-1] == (collateral, 'blocked', None)
        assert collateral not in requests
    else:
        assert evaluations[-1] == (collateral, 'ok', 200)
        assert collateral in requests
    assert [(row[0], row[1]) for row in blocks(settings)] == [
        (digest(blocked_url), 'robots_disallowed' if first == 'robots' else 'login_wall')]
    clock.sleep(DAY)
    routes[ROOT + '/robots.txt'] = (200, b'User-agent: *\nAllow: /\n')
    again = service.run_web(settings, targets_path=path, clock=clock, testing=True)
    assert again['success'] and collateral in requests
    with closing(connect(settings.data_dir)) as db:
        assert recent_web_target_block(db, blocked_url, clock=clock, testing=True)
    assert not any(row[0] == digest(collateral) for row in blocks(settings))


def test_subpage_robots_denial_does_not_request_body(tmp_path, monkeypatch):
    monkeypatch.setattr('hanset_active.fetcher.is_korean_news', lambda url: True)
    settings, path = setup_run(tmp_path, monkeypatch)
    routes = {ROOT + '/robots.txt': (200, b'User-agent: *\nDisallow: /price\n'),
              ROOT: (200, html('<a href="/price">Price</a><a href="/about">About</a>'))}
    requests, evaluations = fake_http(monkeypatch, routes)
    capture_extraction(monkeypatch)
    result = service.run_web(settings, targets_path=path, clock=FakeClock(), testing=True)
    assert result['success'] and result['counts']['pages'] == 3
    assert requests == [ROOT + '/robots.txt', ROOT]
    assert evaluations[1:] == [(ROOT + '/price', 'robots_disallowed', None), (ROOT + '/about', 'blocked', None)]
    assert [(row[0], row[1]) for row in blocks(settings)] == [(digest(ROOT + '/price'), 'robots_disallowed')]


def test_collection_bypasses_legacy_body_budget_for_subpages(tmp_path, monkeypatch):
    settings, path = setup_run(tmp_path, monkeypatch)
    original_settings = service.Settings
    monkeypatch.setattr(service, 'Settings', lambda **kwargs: original_settings(**kwargs, body_cap=2))
    requests, evaluations = fake_http(monkeypatch, {
        ROOT + '/robots.txt': (200, b'User-agent: *\nAllow: /\n'),
        ROOT: (200, html('<a href="/price">Price</a><a href="/about">About</a>')),
        ROOT + '/price': (200, html()),
        ROOT + '/about': (200, html()),
    })
    capture_extraction(monkeypatch)
    result = service.run_web(settings, targets_path=path, clock=FakeClock(), testing=True)
    assert result['success'] and result['counts']['pages'] == 3
    assert requests == [ROOT, ROOT + '/price', ROOT + '/about']
    assert not (settings.active_dir / 'active.sqlite3').exists()


def test_pruning_timezone_validation_and_additive_schema(tmp_path):
    settings_dir = tmp_path / 'collector'
    settings_dir.mkdir()
    # Simulate a pre-cooldown database; migration must preserve its rows.
    with sqlite3.connect(settings_dir / 'collector.sqlite3') as legacy:
        legacy.execute('CREATE TABLE legacy_evidence (label TEXT)')
        legacy.execute("INSERT INTO legacy_evidence VALUES ('unchanged')")
    db = connect(settings_dir)
    clock = FakeClock()
    remember_web_target_block(db, ROOT, 'blocked', status=403, clock=clock)
    clock.sleep(14 * DAY)
    assert not recent_web_target_block(db, ROOT, clock=clock)
    # Prune opportunistically on writing a different URL.
    remember_web_target_block(db, ROOT + '/price', 'login_wall', status=200, clock=clock)
    assert db.execute('SELECT count(*) FROM web_target_blocks').fetchone()[0] == 1
    assert db.execute('SELECT label FROM legacy_evidence').fetchone()[0] == 'unchanged'
    db.execute('UPDATE web_target_blocks SET blocked_at=?', (clock.now().replace(tzinfo=None).isoformat(),))
    db.commit()
    with pytest.raises(CollectError, match='state_corrupt'):
        prune_web_target_blocks(db, clock=clock)
    db.close()


def test_many_skips_do_not_hide_fetches_or_fill_checkpoint(tmp_path, monkeypatch):
    settings, path = setup_run(tmp_path, monkeypatch)
    data = {'schema_version': 1, 'targets': [{
        'venue_id': f'fixture-{n}', 'venue_key': f'fixture-{n}', 'name': f'Fixture {n}',
        'official_urls': [f'https://venue{n}.example/info'],
    } for n in range(31)]}
    path.write_text(json.dumps(data))
    clock = FakeClock()
    db = connect(settings.data_dir)
    for n in range(30):
        remember_web_target_block(db, f'https://venue{n}.example/info', 'blocked', status=403, clock=clock)
    db.close()
    calls = fake_pages(monkeypatch, {'https://venue30.example/info': html()})
    capture_extraction(monkeypatch)
    result = service.run_web(settings, targets_path=path, clock=clock, testing=True)
    assert result['success'] and calls == ['https://venue30.example/info']
    assert result['counts']['pages'] == 1 and result['rejections'] == {'recent_block_skipped': 30}
    assert any(page['disposition'] == 'empty_extraction' for page in result['pages'])
    with sqlite3.connect(settings.active_dir / 'collect-runtime.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM web_completions').fetchone()[0] == 1
    assert service.run_status(settings)['rejections'] == {'recent_block_skipped': 30}


def test_malformed_cooldown_fails_closed(tmp_path, monkeypatch):
    settings, path = setup_run(tmp_path, monkeypatch)
    db = connect(settings.data_dir)
    db.execute('INSERT INTO web_target_blocks(url_hash,disposition,blocked_at) VALUES (?,?,?)', (digest(ROOT), 'blocked', 'invalid'))
    db.commit()
    db.close()
    calls = fake_pages(monkeypatch, {})
    result = service.run_web(settings, targets_path=path, clock=FakeClock(), testing=True)
    assert result['reason'] == 'state_corrupt' and not result['success'] and not calls


def test_cooling_child_does_not_consume_subpage_slot(tmp_path, monkeypatch):
    settings, path = setup_run(tmp_path, monkeypatch)
    clock = FakeClock()
    db = connect(settings.data_dir)
    remember_web_target_block(db, ROOT + '/price', 'blocked', status=403, clock=clock)
    db.close()
    calls = fake_pages(monkeypatch, {
        ROOT: html('<a href="/price/">Price</a><a href="/about/">About</a><a href="/rates/">Rates</a>'),
        ROOT + '/about/': html(), ROOT + '/rates/': html(),
    })
    capture_extraction(monkeypatch)
    result = service.run_web(settings, targets_path=path, clock=clock, testing=True)
    assert result['success'] and result['counts']['pages'] == 3
    # The cooling top-scored child is noted, and the two slots go to live pages.
    assert calls == [ROOT, ROOT + '/rates/', ROOT + '/about/']
    assert result['rejections'] == {'recent_block_skipped': 1}
    assert any(page['url'] == ROOT + '/price' and page['disposition'] == 'recent_block_skipped'
               for page in result['pages'])
