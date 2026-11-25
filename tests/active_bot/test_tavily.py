"""No live service or real credential access: all transports and paths are fake."""
import json
import os
import traceback
from urllib.parse import quote
from unittest.mock import Mock

import pytest
import requests

from hanset_active.search import tavily
from hanset_active.search import budget
from hanset_active.util.timeutil import FakeClock

SENTINEL = 'sentinel-tavily-private-value'


class Response:
    def __init__(self, data=None, *, status=200, headers=None, body=None):
        self.status_code = status
        self.headers = headers or {}
        self.body = body if body is not None else json.dumps(data if data is not None else {'results': []}).encode()
        self.closed = False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def credentials(tmp_path, monkeypatch, isolate_tavily_credentials):
    monkeypatch.setattr(tavily, '_KEY_PATH', tmp_path / 'fake_key')
    monkeypatch.setenv('TAVILY_API_KEY', SENTINEL)
    monkeypatch.setenv('HANSET_IG_DIR', str(tmp_path / 'ig'))


def search(transport, clock=None, target='official_page'):
    return tavily._search('Silver Sauna Seoul 공식 홈페이지', target, transport=transport, clock=clock or FakeClock())


def test_file_precedence_and_private_repr(monkeypatch):
    tavily._KEY_PATH.write_text('  file-only-sentinel\n')
    tavily._KEY_PATH.chmod(0o600)
    credential = tavily._load_credential()
    assert credential.value == os.environ['TAVILY_API_KEY'] == 'file-only-sentinel'
    assert 'file-only-sentinel' not in repr(credential)


@pytest.mark.parametrize('kind', ['permissions', 'symlink', 'empty', 'multiline', 'directory', 'unreadable'])
def test_existing_invalid_file_never_falls_back(kind, tmp_path, monkeypatch):
    key = tavily._KEY_PATH
    if kind == 'symlink':
        key.symlink_to(tmp_path / 'absent')
    elif kind == 'directory':
        key.mkdir()
    else:
        key.write_text('' if kind == 'empty' else 'line one\nline two' if kind == 'multiline' else SENTINEL)
        key.chmod(0o644 if kind == 'permissions' else 0o600)
    if kind == 'unreadable':
        original = os.open
        def fail(path, *args, **kwargs):
            if path == key:
                raise PermissionError(SENTINEL)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(os, 'open', fail)
    post = Mock()
    outcome = search(post)
    assert outcome.reason == 'tavily_key_invalid'
    post.assert_not_called()
    assert not (tmp_path / 'ig').exists()


def test_missing_key_skips_without_budget(monkeypatch, tmp_path):
    monkeypatch.delenv('TAVILY_API_KEY')
    post = Mock()
    outcome = search(post)
    assert outcome.reason == 'tavily_key_missing'
    post.assert_not_called()
    assert not (tmp_path / 'ig').exists()


def test_exact_bounded_post_and_no_raw_persistence(tmp_path):
    from tests.active_bot.test_collect_retry4 import assert_host_locked

    response = Response({'results': [{'url': 'https://silver.example/', 'title': 'Silver Sauna', 'content': 'Official candidate'}]})
    def dispatch(url, **kwargs):
        assert_host_locked(tmp_path, url)
        assert kwargs['headers']['User-Agent'] == tavily.USER_AGENT
        return response
    post = Mock(side_effect=dispatch)
    outcome = tavily._search('Silver Sauna Seoul 공식 홈페이지', 'official_page',
                             transport=post, clock=FakeClock(), host_dir=tmp_path)
    assert outcome.reason == 'ok' and len(outcome.leads) == 1
    assert outcome.leads[0].verification == 'unverified'
    assert outcome.budget.used == 1
    args, options = post.call_args
    assert args == (tavily.ENDPOINT,)
    assert options['headers']['Authorization'] == 'Bearer ' + SENTINEL
    assert options['timeout'] == (5, 10) and options['stream']
    assert options['allow_redirects'] is False
    assert options['json'] == {
        'query': 'Silver Sauna Seoul 공식 홈페이지', 'search_depth': 'basic',
        'auto_parameters': False, 'topic': 'general', 'max_results': 3,
        'include_answer': False, 'include_raw_content': False, 'include_images': False,
    }
    assert response.closed
    assert {p.name for p in (tmp_path / 'ig').iterdir()} == {'tavily_usage.json', 'tavily_usage.lock'}


def test_private_session_settings(monkeypatch):
    session = Mock()
    session.post.return_value = Response()
    monkeypatch.setattr(requests, 'Session', lambda: session)
    assert tavily._search('Silver Sauna', 'official_page').reason == 'ok'
    assert session.trust_env is False
    assert session.mount.call_args.args[1].max_retries.total == 0
    assert session.close.called and session.post.call_count == 1


@pytest.mark.parametrize('status,body,expected', [
    (302, b'', 'tavily_http_error'), (503, b'', 'tavily_http_error'),
    (200, b'not json', 'tavily_invalid_response'), (200, b'[]', 'tavily_invalid_response'),
    (200, b'{"results": null}', 'tavily_invalid_response'),
    (200, b'x' * (tavily.MAX_RESPONSE + 1), 'tavily_response_too_large'),
])
def test_bounded_errors_count_attempts(status, body, expected):
    post = Mock(return_value=Response(status=status, body=body))
    outcome = search(post)
    assert outcome.reason == expected and outcome.budget.used == 1
    assert post.call_count == 1


@pytest.mark.parametrize('error,reason', [(requests.Timeout, 'tavily_timeout'), (RuntimeError, 'tavily_transport_error')])
def test_secret_echo_errors_are_safe(error, reason, tmp_path, caplog, capsys):
    post = Mock(side_effect=error(SENTINEL))
    outcome = search(post)
    assert outcome.reason == reason
    assert SENTINEL not in repr(outcome) + caplog.text + str(capsys.readouterr())
    assert SENTINEL not in (tmp_path / 'ig' / 'tavily_usage.json').read_text()


def test_echo_response_is_redacted_before_pii_and_url_filtering(tmp_path, caplog, capsys):
    encoded = quote(SENTINEL, safe='')  # hyphens are unreserved; encode every byte too.
    encoded = ''.join('%%%02X' % b for b in encoded.encode())
    post = Mock(return_value=Response({'results': [
        {'url': 'https://silver.example', 'title': SENTINEL, 'content': encoded},
        {'url': 'https://silver.example/' + encoded},
        {'url': 'https://silver.example/?api_key=other-value'},
    ]}))
    outcome = search(post)
    assert len(outcome.leads) == 1 and outcome.leads[0].title == '[redacted]'
    assert outcome.leads[0].snippet == '[redacted]'
    assert SENTINEL not in repr(outcome) + caplog.text + str(capsys.readouterr())
    assert SENTINEL not in (tmp_path / 'ig' / 'tavily_usage.json').read_text()


@pytest.mark.parametrize('url', [
    'https://instagram.com/example/', 'https://localhost/',
    'https://user:password' + chr(64) + 'silver.example/', 'https://silver.example/?token=private',
    'https://silver.example/?contact=hidden%40example.test', 'https://silver.example/\\bad',
    'javascript:alert(1)', 'https://silver.example:invalid/',
])
def test_official_candidate_url_rejections(url):
    outcome = search(Mock(return_value=Response({'results': [{'url': url}]})))
    assert outcome.reason == 'ok' and not outcome.leads


@pytest.mark.parametrize('url,handle', [
    ('https://www.instagram.com/silver.sauna/', 'silver.sauna'),
    ('https://instagram.com/Silver_Sauna', 'silver_sauna'),
    ('https://instagram.com/p/', None), ('https://instagram.com/reels/', None),
    ('https://instagram.com/login/', None), ('https://instagram.com/accounts/login/', None),
    ('https://instagram.com/silver.sauna/reel/abc', None),
    ('https://instagram.com.evil.test/silver/', None), ('https://evil.instagram.com/silver/', None),
    ('https://instagram.com/%70/', None), ('https://instagram.com/silver/?token=private', None),
    ('http://instagram.com/silver/', None), ('https://instagram.com/a..b/', None),
])
def test_instagram_profile_only(url, handle):
    outcome = search(Mock(return_value=Response({'results': [{'url': url}]})), target='instagram_handle')
    assert [lead.handle for lead in outcome.leads] == ([handle] if handle else [])


def test_rate_limit_cooldown_is_persisted_and_bounded():
    clock = FakeClock()
    post = Mock(return_value=Response(status=429, headers={'Retry-After': '999999'}))
    assert search(post, clock).reason == 'tavily_rate_limited'
    assert search(post, clock).reason == 'tavily_cooldown'
    assert post.call_count == 1
    clock.sleep(3600)
    post.return_value = Response()
    assert search(post, clock).budget.used == 2


def test_budget_exception_has_only_fixed_code(monkeypatch):
    def fail():
        raise OSError(SENTINEL)
    monkeypatch.setattr(budget, '_directory', fail)
    try:
        with budget.locked_budget():
            pytest.fail('must not enter')
    except budget.BudgetUnavailable as exc:
        rendered = ''.join(traceback.format_exception(exc))
        assert str(exc) == 'tavily_budget_unavailable'
        assert SENTINEL not in rendered and exc.__context__ is None
    else:
        pytest.fail('must fail closed')


def test_date_rechecked_after_session_setup_before_dispatch(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    clock = FakeClock(datetime(2026, 11, 5, 14, 59, 59, tzinfo=timezone.utc))
    session = Mock()
    session.post.return_value = Response()
    def setup():
        clock.sleep(1)  # Session setup crosses KST midnight after the first fsync.
        return session
    monkeypatch.setattr(requests, 'Session', setup)
    result = tavily._search('Silver Sauna', 'official_page', clock=clock)
    assert result.reason == 'ok' and result.budget.day == '2026-11-06'
    days = json.loads((tmp_path / 'ig' / 'tavily_usage.json').read_text())['budgets']['tavily']['days']
    assert days == {'2026-11-05': {'used': 1}, '2026-11-06': {'used': 1}}
    assert session.post.call_count == 1


def test_rollover_reservation_write_failure_never_dispatches(monkeypatch):
    from datetime import datetime, timezone
    clock = FakeClock(datetime(2026, 11, 5, 14, 59, 59, tzinfo=timezone.utc))
    session = Mock()
    original_write = budget._write
    writes = []
    def write(path, state):
        writes.append(path)
        if len(writes) == 2:
            raise OSError(SENTINEL)
        original_write(path, state)
    def setup():
        clock.sleep(1)
        return session
    monkeypatch.setattr(requests, 'Session', setup)
    monkeypatch.setattr(budget, '_write', write)
    result = tavily._search('Silver Sauna', 'official_page', clock=clock)
    assert result.reason == 'tavily_budget_unavailable' and SENTINEL not in repr(result)
    session.post.assert_not_called()


@pytest.mark.parametrize('header,seconds', [
    ('Thu, 01 Jan 2026 00:02:00 GMT', 120), ('garbage', 60), ('-12', 1),
])
def test_retry_after_http_date_and_fallback(header, seconds):
    assert tavily._retry_after(header, FakeClock()) == seconds


def test_markup_and_url_normalization_cannot_reintroduce_key():
    marked_up = SENTINEL[:8] + '<b></b>' + SENTINEL[8:]
    post = Mock(return_value=Response({'results': [
        {'url': 'https://silver.example', 'title': marked_up, 'content': marked_up},
        {'url': 'https://' + SENTINEL.upper() + '.example/'},
    ]}))
    result = search(post)
    assert len(result.leads) == 1
    assert result.leads[0].title == result.leads[0].snippet == '[redacted]'
    assert SENTINEL not in repr(result)
