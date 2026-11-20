"""Deterministic, private review artifacts. This module cannot publish the site."""
import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from .changes import DETECTOR_VERSION, FIELDS, HEX, closure_state, has_schema, put_meta, review_holds, safe_text, source_conflicts
from .config import official_site
from .normalize import stable_json
from .targets import canonical_requested_url

COMMON = {'schema_version': 1, 'rights': 'internal_review_only', 'auto_publish_to_site': False}
ARTIFACTS = ('closed_days.proposals.json', 'rebuild_candidates.json', 'viral_topics.jsonl')


def canonical_bytes(value):
    return (stable_json(value) + '\n').encode('utf-8')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def _no_symlinks(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('unsafe_output_path')
    return path


def _root(directory, *, create=False):
    root = _no_symlinks(Path(directory) / 'official-web-changes')
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
    if root.exists():
        for name in (*ARTIFACTS, 'manifest.json'):
            _no_symlinks(root / name)
    return root


def _mapping(directory):
    """Explicit external mapping only: venue_id -> {venue_key, site_slug}."""
    path = _no_symlinks(Path(directory) / 'site_slug_map.json')
    if not path.exists():
        return {}
    if path.stat().st_size > 100_000:
        raise ValueError('mapping_invalid')
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict):
        raise ValueError('mapping_invalid')
    out = {}
    for venue_id, entry in data.items():
        if (not isinstance(entry, dict) or set(entry) != {'venue_key', 'site_slug'}
                or not isinstance(entry['venue_key'], str)
                or not isinstance(entry['site_slug'], str)
                or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', entry['site_slug'])):
            raise ValueError('mapping_invalid')
        out[(venue_id, entry['venue_key'])] = entry['site_slug']
    return out


def _until(statement):
    """Conservative fully specified end dates; no recurrence or stray dates."""
    if re.search(r'매주|매월|every|weekly|monthly|월요일|화요일|수요일|목요일|금요일|토요일|일요일', statement, re.I):
        return None
    pattern = r'(?<!\d)(\d{4})[-/](\d{2})[-/](\d{2})(?!\d)'
    matches = list(re.finditer(pattern, statement))
    remaining = re.sub(pattern, '', statement)
    if re.search(r'\d', remaining):
        return None
    try:
        dates = [date(*map(int, m.groups())) for m in matches]
    except ValueError:
        return None
    if len(matches) == 1 and re.search(r'until|through|까지|종료', statement, re.I):
        return dates[0].isoformat()
    if len(matches) == 2 and re.fullmatch(r'\s*(?:[~–—-]|to|through)\s*', statement[matches[0].end():matches[1].start()], re.I):
        return dates[1].isoformat() if dates[0] <= dates[1] else None
    return None


def _scope(event):
    return tuple(event[k] for k in ('venue_id', 'venue_key', 'field', 'qualifier_key'))


def _fresh(checked, as_of):
    return '오래됨' if date.fromisoformat(checked) < date.fromisoformat(as_of) - timedelta(days=90) else 'fresh'


def _safe_event(event):
    if not HEX.fullmatch(event['change_id']):
        return False
    try:
        for key in ('requested_url', 'final_url'):
            if canonical_requested_url(event[key]) != event[key] or not official_site(event[key]):
                return False
        for key in ('venue_id', 'venue_key', 'qualifier_key'):
            if not safe_text(event[key]):
                return False
        for key in ('old_value_json', 'new_value_json'):
            value = json.loads(event[key])
            if isinstance(value, str) and not safe_text(value):
                return False
    except (ValueError, TypeError):
        return False
    return True


def build_artifacts(db, directory, *, as_of=None):
    events = [dict(r) for r in db.execute('SELECT * FROM web_change_events ORDER BY event_seq')]
    watermark = max((e['event_seq'] for e in events), default=0)
    as_of = as_of or max((e['checked'] for e in events), default='1970-01-01')
    date.fromisoformat(as_of)
    mapping = _mapping(directory)
    conflicts = source_conflicts(db)
    holds = review_holds(db)
    latest = {}
    for event in events:
        if event['event_kind'] in FIELDS and event['new_comparison_key'] is not None:
            latest[(*_scope(event), event['source_identity'])] = event
    # Old transitions remain in the immutable log; a rebaseline retires the
    # current divergences without deleting history.
    active = {tuple(r[k] for k in ('venue_id','venue_key','field','qualifier_key','source_identity')): r['last_event_id']
              for r in db.execute("SELECT * FROM web_change_baselines WHERE state='active'")}
    authorized = {tuple(r[k] for k in ('venue_id','venue_key','source_identity','target_fingerprint'))
                  for r in db.execute("SELECT * FROM web_change_baselines WHERE state='active'")}
    current = [e for scope, e in latest.items() if active.get(scope) == e['change_id']]
    venues = defaultdict(list)
    for event in current:
        venues[(event['venue_id'], event['venue_key'])].append(event)
    proposals, candidates = [], []
    for venue, entries in sorted(venues.items()):
        safe_entries = [e for e in entries if _safe_event(e)]
        if not safe_entries:
            continue
        slug = mapping.get(venue)
        reasons = sorted({e['review_reason'] for e in entries if e['review_reason']}
                         | {holds[_scope(e)] for e in entries if _scope(e) in holds}
                         | ({'source_conflict'} if any(_scope(e) in conflicts for e in entries) else set()))
        checked = max(e['checked'] for e in entries)
        candidates.append({
            'venue_id': venue[0], 'venue_key': venue[1],
            'fields': sorted({e['field'] for e in entries}),
            'latest_change_ids': sorted(e['change_id'] for e in safe_entries),
            'source_urls': sorted({e['requested_url'] for e in safe_entries}),
            'checked': checked, 'requires_human_review': True,
            'freshness': _fresh(checked, as_of), 'review_reasons': reasons,
            'suggested_routes': [f'/finder/{slug}/'] if slug else [],
        })
        closures = [e for e in safe_entries if e['field'] == 'closures']
        if not closures:
            continue
        checked = max(e['checked'] for e in closures)
        statements = sorted({json.loads(e['new_value_json']) for e in closures})
        conflict = any(_scope(e) in conflicts for e in closures)
        states = {closure_state(s) for s in statements}
        reason = ('source_conflict' if conflict else
                  next((holds[_scope(e)] for e in closures if _scope(e) in holds), None) or
                  next((e['review_reason'] for e in closures if e['review_reason']), None))
        state = ('review_only' if conflict or states == {'review_only'} else
                 'no_closure' if states == {'no_closure'} else
                 'closure_notice' if states == {'closure_notice'} and not reason else 'review_only')
        update = state == 'closure_notice'
        row = {
            'venue_id': venue[0], 'venue_key': venue[1], 'site_slug': slug,
            'mapping_status': 'mapped' if slug else '확인 중',
            'action': 'update' if update else 'review_only', 'state': state,
            'until_status': '확인 중', 'url': closures[-1]['requested_url'],
            'resolved_url': closures[-1]['final_url'], 'source': 'Official page',
            'checked': checked, 'change_ids': sorted(e['change_id'] for e in closures),
            'review_reason': reason, 'freshness': _fresh(checked, as_of),
        }
        if update:
            row['closed'] = ' · '.join(statements)
            until = _until(row['closed'])
            if until:
                row['until'], row['until_status'] = until, 'explicit'
        proposals.append(row)
    topics = []
    labels = {'hours': '운영시간', 'price_day_pass': '일일 이용요금', 'closures': '휴무 안내'}
    for event in events:
        if (event['event_kind'] not in FIELDS or not event['usable_for_viral'] or event['review_reason']
                or tuple(event[k] for k in ('venue_id','venue_key','source_identity','target_fingerprint')) not in authorized
                or _scope(event) in conflicts or _scope(event) in holds
                or _fresh(event['checked'], as_of) != 'fresh' or not _safe_event(event)):
            continue
        field = event['field']
        old, new = json.loads(event['old_value_json']), json.loads(event['new_value_json'])
        if field == 'closures' and closure_state(new) != 'closure_notice':
            continue
        if field == 'price_day_pass':
            old, new = f'{old}원', f'{new}원'
        qualifiers = json.loads(event['qualifier_key'])
        scope_text = ' '.join(str(value) for _, value in sorted(qualifiers.items()))
        blurb = (f"{event['venue_key']}: 공식 웹페이지의 {scope_text + ' ' if scope_text else ''}{labels[field]} "
                 f"표기가 {old}에서 {new}으로 변경됨. 확인일 {event['checked']}.")
        if not safe_text(blurb):
            continue
        topics.append({**COMMON, 'change_id': event['change_id'], 'venue_id': event['venue_id'],
                       'venue_key': event['venue_key'], 'field': field, 'qualifiers': qualifiers,
                       'blurb': blurb, 'source_url': event['requested_url'], 'checked': event['checked'],
                       'freshness': 'fresh', 'requires_review': True})
    base = {**COMMON, 'event_watermark': watermark, 'as_of': as_of}
    artifacts = {
        ARTIFACTS[0]: canonical_bytes({**base, 'proposals': proposals}),
        ARTIFACTS[1]: canonical_bytes({**base, 'candidates': candidates}),
        ARTIFACTS[2]: b''.join(canonical_bytes(t) for t in topics),
    }
    manifest = {**base, 'generator_version': DETECTOR_VERSION,
                'event_digest': sha('\n'.join(sorted(e['change_id'] for e in events)).encode()),
                'artifacts': [{'name': name, 'sha256': sha(data), 'bytes': len(data)}
                              for name, data in sorted(artifacts.items())]}
    return artifacts, manifest


def _stage(root, name, data):
    fd, temporary = tempfile.mkstemp(prefix='.' + name + '.', dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return Path(temporary)
    except BaseException:
        os.unlink(temporary)
        raise


def _sync_dir(root):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def export_changes(db, directory, *, as_of=None):
    """Stage all files first; manifest last. Roll back replacement failures.

    Consumers must verify hashes and retry if reading during replacement. Events
    commit separately and survive every export failure.
    """
    if not has_schema(db) or not db.execute('SELECT 1 FROM web_change_events LIMIT 1').fetchone():
        return {'reason': 'no_events', 'event_watermark': 0}
    staged, backups, replaced = {}, {}, []
    try:
        root = _root(directory, create=True)
        artifacts, manifest = build_artifacts(db, directory, as_of=as_of)
        artifacts['manifest.json'] = canonical_bytes(manifest)
        for name, data in artifacts.items():
            path = _no_symlinks(root / name)
            if path.exists():
                backups[name] = _stage(root, name + '.previous', path.read_bytes())
            staged[name] = _stage(root, name, data)
        for name in (*ARTIFACTS, 'manifest.json'):
            os.replace(staged[name], root / name)
            replaced.append(name)
            _sync_dir(root)
        put_meta(db, 'last_export_event_seq', manifest['event_watermark'])
        put_meta(db, 'last_export_as_of', manifest['as_of'])
        put_meta(db, 'export_diagnostic', '')
        db.commit()
        return {'reason': 'ok', 'event_watermark': manifest['event_watermark'],
                'manifest_sha256': sha(artifacts['manifest.json'])}
    except Exception:
        for name in reversed(replaced):
            if name in backups:
                os.replace(backups[name], root / name)
            else:
                (root / name).unlink(missing_ok=True)
        if replaced:
            try:
                _sync_dir(root)
            except OSError:
                pass
        put_meta(db, 'export_diagnostic', 'export_failed')
        db.commit()
        return {'reason': 'export_failed'}
    finally:
        for path in (*staged.values(), *backups.values()):
            path.unlink(missing_ok=True)


def manifest_status(directory, *, expected_watermark=None):
    """Read-only hash verification; no directory creation or model dependency."""
    try:
        root = _root(directory)
        path = root / 'manifest.json'
        if not path.exists():
            return {'state': 'missing'}
        manifest_bytes = path.read_bytes()
        manifest = json.loads(manifest_bytes)
        if (manifest.get('schema_version') != 1 or manifest.get('generator_version') != DETECTOR_VERSION
                or manifest.get('rights') != 'internal_review_only' or manifest.get('auto_publish_to_site') is not False
                or type(manifest['event_watermark']) is not int or not HEX.fullmatch(manifest['event_digest'])
                or sorted(a['name'] for a in manifest['artifacts']) != sorted(ARTIFACTS)):
            raise ValueError('manifest_invalid')
        for artifact in manifest['artifacts']:
            data = (root / artifact['name']).read_bytes()
            if (not HEX.fullmatch(artifact['sha256']) or sha(data) != artifact['sha256']
                    or len(data) != artifact['bytes']):
                raise ValueError('artifact_invalid')
        if path.read_bytes() != manifest_bytes:
            raise ValueError('manifest_changed')
        state = 'ok' if expected_watermark is None or expected_watermark == manifest['event_watermark'] else 'behind'
        return {'state': state, 'event_watermark': manifest['event_watermark'], 'manifest_sha256': sha(manifest_bytes)}
    except (OSError, ValueError, TypeError, KeyError):
        return {'state': 'invalid'}
