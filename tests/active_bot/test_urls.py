from datetime import datetime, timezone
import pytest
from hanset_active import __version__
from hanset_active.config import Settings, REPO_ROOT, load_profile
from hanset_active.util.urls import canonicalize, registrable_domain, is_denied, is_excluded, literal_private
from hanset_active.util.timeutil import FakeClock, kst_now, kst_day

def test_url_identity():
    assert canonicalize('HTTPS://Venue-A.test:443/a/?utm_source=x&b=2&a=1#fragment')=='https://venue-a.test/a?a=1&b=2'
    assert canonicalize('/b/','https://venue-a.test/a')=='https://venue-a.test/b'
    assert registrable_domain('https://www.venue-a.co.kr/a')=='venue-a.co.kr'
    assert registrable_domain('https://www.venue-a.test')=='venue-a.test'
    for url in ('file:///a','https://user:pass'+'@'+'venue-a.test'):
        with pytest.raises(ValueError): canonicalize(url)

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
