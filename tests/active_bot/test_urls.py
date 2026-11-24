from datetime import datetime, timezone
from urllib.parse import unquote
import pytest
from hanset_active import __version__
from hanset_active.config import Settings, REPO_ROOT, load_profile
from hanset_active.util.urls import canonicalize, registrable_domain, is_denied, is_excluded, literal_private, same_fetch_target
from hanset_active.util.timeutil import FakeClock, kst_now, kst_day

def test_url_identity():
    assert canonicalize('HTTPS://Venue-A.test:443/a/?utm_source=x&b=2&a=1#fragment')=='https://venue-a.test/a?a=1&b=2'
    assert canonicalize('/b/','https://venue-a.test/a')=='https://venue-a.test/b'
    assert registrable_domain('https://www.venue-a.co.kr/a')=='venue-a.co.kr'
    assert registrable_domain('https://www.venue-a.test')=='venue-a.test'
    for url in ('file:///a','https://user:pass'+'@'+'venue-a.test'):
        with pytest.raises(ValueError): canonicalize(url)

@pytest.mark.parametrize('path', ['/facilities/', '/a%2Fb//', '/%EC%82%AC%EC%9A%B0%EB%82%98/', '/'])
def test_opt_in_fetch_path_preserves_encoding_and_slashes(path):
    url = 'HTTPS://Venue-A.test:443' + path + '?utm_source=x&b=2&a=1#fragment'
    assert canonicalize(url, preserve_path=True) == 'https://venue-a.test' + path + '?a=1&b=2'
    assert canonicalize(url) == 'https://venue-a.test' + unquote(path.rstrip('/')) + '?a=1&b=2'
    assert canonicalize(path, 'https://venue-a.test/root', preserve_path=True) == 'https://venue-a.test' + path

@pytest.mark.parametrize('suffix', ['?token=fixture', '?%74oken=fixture', '/Bearer%20fixture/'])
def test_path_preservation_never_repairs_private_urls(suffix):
    with pytest.raises(ValueError):
        canonicalize('https://venue-a.test' + suffix, preserve_path=True)

@pytest.mark.parametrize(('a', 'b', 'expected'), [
    ('https://venue.example/a/보', 'https://venue.example/a/%EB%B3%B4', True),
    ('https://venue.example/a/%EB%B3%B4', 'https://venue.example/a/%eb%b3%b4', True),
    ('https://venue.example/a/보', 'https://venue.example/a/%eb%b3%b4/', False),
    ('https://venue.example/a%2Fb', 'https://venue.example/a/b', False),
    ('https://venue.example', 'https://venue.example/', True),
    ('https://venue.example/?x=1', 'https://venue.example/?x=2', False),
    ('https://venue.example/?x=&x=1', 'https://venue.example/?x=1&x=', False),
    ('http://venue.example/a', 'https://venue.example/a', False),
    ('https://VENUE.example:443/a', 'https://venue.example/a', True),
    ('https://venue.example:443/a', 'https://venue.example:444/a', False),
    ('https://venue.example/a', 'https://other.example/a', False),
])
def test_same_fetch_target(a, b, expected):
    assert same_fetch_target(a, b) is expected

def test_denial_and_exclusion():
    for host in ('naver.com','booking.naver.com','naver.me','a.naver.co.kr','pstatic.net','instagram.com','www.instagram.com'):
        assert is_denied('https://'+host)
    assert not is_denied('https://venue-a.test')
    assert is_excluded(load_profile()['exclusion_terms'][0])
    assert literal_private('http://127.0.0.1')
    assert literal_private('http://localhost')
    assert not literal_private('http://198.18.0.1')
    assert not literal_private('https://venue-a.test')

def test_settings(tmp_path,monkeypatch):
    assert __version__
    settings=Settings()
    assert settings.min_interval==5 and settings.global_gap==1
    assert (settings.candidate_cap,settings.body_cap,settings.aux_cap)==(500,200,800)
    for kwargs in ({'min_interval':4},{'global_gap':0},{'body_cap':201},{'candidate_cap':501},{'aux_cap':801},{'allow_private_hosts':True},{'data_dir':REPO_ROOT/'raw'}):
        with pytest.raises(ValueError): Settings(**kwargs)
    link=tmp_path/'repo-link'; link.symlink_to(REPO_ROOT,target_is_directory=True)
    with pytest.raises(ValueError): Settings(data_dir=link/'raw')
    monkeypatch.setenv('HANSET_ACTIVE_DIR',str(tmp_path/'external'))
    assert Settings().data_dir==tmp_path/'external'
    assert len(settings.profile['venue_types'])==6 and len(settings.profile['fields'])==9
    assert all('naver' not in d['url'] for d in settings.profile['directories'])

def test_kst_fake_clock():
    clock=FakeClock(datetime(2026,1,1,15,tzinfo=timezone.utc))
    assert kst_day(clock)=='2026-01-02'
    assert kst_now(clock).hour==0
    clock.sleep(5)
    assert clock.monotonic()==5
