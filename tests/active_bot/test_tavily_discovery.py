"""Future daemon contract: eligible gaps only, no DB mutation or fetch grant."""
import json
from unittest.mock import Mock

import pytest

from hanset_active.gaps import ensure_gaps
from hanset_active.search import discover_known_venue
from hanset_active.search import tavily
from hanset_active.util.urls import is_denied
from hanset_active.venues import upsert_venue


@pytest.fixture
def eligible(bot, tmp_path, monkeypatch):
    monkeypatch.setattr(tavily, '_KEY_PATH', tmp_path / 'absent-key')
    monkeypatch.setenv('TAVILY_API_KEY', 'sentinel-discovery-private-value')
    monkeypatch.setenv('HANSET_IG_DIR', str(tmp_path / 'ig'))
    vid = upsert_venue(bot.db, 'Silver Sauna Central', website='https://silver.example', city='Seoul')
    ensure_gaps(bot.db, vid, clock=bot.clock)
    return vid, 'price_day_pass'


def response(rows):
    result = Mock(status_code=200)
    result.iter_content.return_value = [json.dumps({'results': rows}).encode()]
    return result


def test_known_venue_query_is_single_bounded_and_db_read_only(bot, eligible):
    post = Mock(return_value=response([{'url': 'https://silver.example/official'}] * 10))
    before = '\n'.join(bot.db.iterdump())
    result = discover_known_venue(bot.db, *eligible, clock=bot.clock, transport=post)
    assert result.status == 'ok' and len(result.leads) == 1
    assert (result.venue_id, result.gap_field) == eligible
    assert post.call_count == 1
    query = post.call_args.kwargs['json']['query']
    assert query == 'Silver Sauna Central Seoul 공식 홈페이지'
    assert len(query) <= tavily.MAX_QUERY
    assert '\n'.join(bot.db.iterdump()) == before


@pytest.mark.parametrize('case,reason', [
    ('unknown', 'tavily_unknown_venue'), ('missing', 'tavily_gap_missing'),
    ('resolved', 'tavily_gap_resolved'), ('excluded', 'tavily_venue_excluded'),
    ('target', 'tavily_unsupported_target'), ('field', 'tavily_unsupported_gap'),
    ('long', 'tavily_invalid_identity'),
])
def test_ineligible_never_loads_key_or_spends_budget(bot, eligible, monkeypatch, tmp_path, case, reason):
    vid, field = eligible
    target = 'official_page'
    if case == 'unknown':
        vid = 'unknown-id'
    elif case == 'missing':
        bot.db.execute('DELETE FROM gaps WHERE venue_id=?', (vid,))
    elif case == 'resolved':
        bot.db.execute("UPDATE gaps SET state='filled' WHERE venue_id=? AND field=?", (vid, field))
    elif case == 'excluded':
        bot.db.execute("UPDATE venues SET website='https://instagram.com/' WHERE venue_id=?", (vid,))
    elif case == 'target':
        target = 'new_venues'
    elif case == 'field':
        field = 'invented_field'
    elif case == 'long':
        bot.db.execute('UPDATE venues SET name=? WHERE venue_id=?', ('Long' * 200, vid))
    key = Mock(side_effect=AssertionError('credentials accessed before validation'))
    monkeypatch.setattr(tavily, '_load_credential', key)
    post = Mock()
    result = discover_known_venue(bot.db, vid, field, target=target, transport=post)
    assert result.reason == reason
    key.assert_not_called()
    post.assert_not_called()
    assert not (tmp_path / 'ig').exists()


def test_ig_leads_leave_denylist_and_all_database_state_unchanged(bot, eligible):
    before = '\n'.join(bot.db.iterdump())
    post = Mock(return_value=response([{'url': 'https://www.instagram.com/silver_sauna/', 'title': 'Candidate'}]))
    result = discover_known_venue(bot.db, *eligible, target='instagram_handle', clock=bot.clock, transport=post)
    assert result.leads[0].handle == 'silver_sauna'
    assert result.leads[0].verification == 'unverified'
    assert is_denied(result.leads[0].url)
    assert '\n'.join(bot.db.iterdump()) == before


@pytest.mark.parametrize('condition,reason', [('missing', 'tavily_key_missing'), ('exhausted', 'tavily_provider_quota')])
def test_simulated_daemon_continues_other_work(bot, eligible, monkeypatch, condition, reason):
    if condition == 'missing':
        monkeypatch.delenv('TAVILY_API_KEY')
    post = Mock(return_value=Mock(status_code=432))
    statuses = []
    unrelated = []
    def daemon_tick():
        outcome = discover_known_venue(bot.db, *eligible, clock=bot.clock, transport=post)
        statuses.append(outcome.reason)
        unrelated.append('completed-unrelated-step')
    daemon_tick()
    assert statuses == [reason] and unrelated == ['completed-unrelated-step']
    assert bot.db.execute('SELECT state FROM gaps WHERE venue_id=? AND field=?', eligible).fetchone()[0] == 'open'
    if condition == 'missing':
        post.assert_not_called()
    else:
        post.assert_called_once()
