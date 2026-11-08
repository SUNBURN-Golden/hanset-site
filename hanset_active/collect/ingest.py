"""Desktop Instagram batch boundary. This module never opens a browser or a session file."""
import json
from datetime import datetime, timedelta

from hanset_active.util.timeutil import KST, kst_now

from . import CollectError
from .config import IG_READ_MAX, canonical_instagram, handle_valid
from .ground import bounded_text
from .safety import MEDIA_KEYS, looks_like_security
from .state import HALT_REASONS

LINE_KEYS = frozenset({
    'venue_id', 'venue_key', 'account_handle', 'post_url', 'seen_at',
    'operator_summary', 'provenance', 'security_warning', 'action',
})
SUMMARY_LIMIT = 480
WATCH_KEYS = frozenset({'enabled', 'venue_id', 'venue_key', 'handle', 'name'})


def within_window(clock=None):
    return kst_now(clock).hour == 13


def load_watchlist(path):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 512_000:
            raise ValueError
        data = json.loads(path.read_bytes().decode())
        rows = data.get('venues')
        if data.get('schema_version') != 1 or not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        raise CollectError('watchlist_invalid') from None
    entries, seen, handles = [], set(), set()
    for entry in rows:
        if not isinstance(entry, dict) or type(entry.get('enabled', True)) is not bool or set(entry) - WATCH_KEYS:
            raise CollectError('watchlist_invalid')
        if not entry.get('enabled', True):
            continue
        try:
            if not all(isinstance(entry.get(key), str) and entry[key] and len(entry[key]) <= 200 for key in ('venue_id', 'venue_key', 'name', 'handle')):
                raise ValueError
            if not handle_valid(entry['handle']):
                raise ValueError
        except (TypeError, ValueError):
            raise CollectError('watchlist_invalid') from None
        identity = (entry['venue_id'], entry['venue_key'], entry['handle'].lower())
        folded = entry['handle'].casefold()
        if identity in seen or folded in handles:
            raise CollectError('watchlist_invalid')
        seen.add(identity)
        handles.add(folded)
        entries.append(entry)
    return entries


def security_flag(path):
    """Return a halt reason and delete the flag. The flag body is not returned."""
    if not path.exists():
        return None
    reason = 'security_check'
    try:
        if path.is_symlink() or path.stat().st_size > 4096:
            raise ValueError
        data = json.loads(path.read_bytes().decode())
        if isinstance(data, dict) and data.get('halt') is True and data.get('reason') in HALT_REASONS:
            reason = data['reason']
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        reason = 'security_check'
    finally:
        if path.exists() and not path.is_symlink():
            path.unlink()
    return reason


def _stamp_ok(value, clock):
    try:
        seen = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    if seen.utcoffset() is None:
        return False
    seen = seen.astimezone(KST)
    now = kst_now(clock)
    if seen.date() != now.date() or seen.hour != 13:
        return False
    return seen <= now + timedelta(minutes=5)


def parse_batch(lines, entries, clock=None):
    """Parse JSONL in memory. A security signal halts the day and returns no documents."""
    documents, rejected = [], {}

    def reject(reason):
        rejected[reason] = rejected.get(reason, 0) + 1

    for line in lines:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            reject('input_invalid')
            continue
        if not isinstance(raw, dict):
            reject('input_invalid')
            continue
        strings = [value for value in raw.values() if isinstance(value, str)]
        if raw.get('security_warning') is True or any(looks_like_security(value) for value in strings):
            return [], rejected, 'security_check'
        if set(raw) & MEDIA_KEYS or any(isinstance(item, str) and item.startswith('data:image') for item in raw.values()):
            reject('photo_rejected')
            continue
        if 'action' in raw:
            reject('action_not_automated')
            continue
        if 'text' in raw:
            reject('provenance_invalid')
            continue
        if set(raw) - LINE_KEYS:
            reject('input_invalid')
            continue
        if 'provenance' not in raw or 'operator_summary' not in raw:
            reject('provenance_required')
            continue
        if raw.get('provenance') != 'operator_summary':
            reject('provenance_invalid')
            continue
        text = raw.get('operator_summary')
        if not isinstance(text, str) or not text.strip() or len(text) > SUMMARY_LIMIT:
            reject('provenance_invalid')
            continue
        if not _stamp_ok(raw.get('seen_at'), clock):
            reject('outside_window')
            continue
        identity = ('venue_id', 'venue_key', 'account_handle', 'post_url', 'seen_at')
        if not all(isinstance(raw.get(key), str) and raw[key] for key in identity):
            reject('input_invalid')
            continue
        matches = [entry for entry in entries if entry['venue_id'] == raw['venue_id'] and entry['venue_key'] == raw['venue_key'] and entry['handle'].lower() == raw['account_handle'].lower()]
        if len(matches) != 1:
            reject('unresolved_venue')
            continue
        try:
            url, _kind = canonical_instagram(raw['post_url'], handle=matches[0]['handle'])
        except CollectError:
            reject('invalid_source_url')
            continue
        clean = bounded_text(text)
        if not clean.strip():
            reject('provenance_invalid')
            continue
        documents.append({
            'venue_id': matches[0]['venue_id'],
            'venue_key': matches[0]['venue_key'],
            'url': url,
            'text': clean,
            'seen_at': datetime.fromisoformat(raw['seen_at']).astimezone(KST).isoformat(),
        })
    if len(documents) > IG_READ_MAX:
        return [], {'ig_quota': len(documents)}, None
    return documents, rejected, None
