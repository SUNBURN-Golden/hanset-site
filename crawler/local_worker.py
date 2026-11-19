"""Shared intake pipeline. No site imports, content writes, or publication hooks."""
import datetime as dt
import json
import os
import re
import time
import uuid
from functools import wraps
from urllib.parse import urlsplit, unquote
from .config import Rejected, load, policy_for, seed_policy
from . import evidence
from .dedup import venue_for, group_for
from .leases import Supervisor, current, process_start
from .privacy_filter import safe_url, text
from .rate_limiter import charge, day_and_expiry
from .schema import CHANNELS, STAGES, digest, payload
from .store import Store

_RECORD_EXTENSION = object()


def uid():
    return str(uuid.uuid4())


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
        raise Rejected('invalid_identifier')
    return value


def audited(operation):
    def decorate(fn):
        @wraps(fn)
        def call(self, *args, **kwargs):
            try:
                return fn(self, *args, **kwargs)
            except Rejected as exc:
                self.store.db.execute('INSERT INTO audit(operation,reason,at) VALUES (?,?,?)',
                                      (operation, str(exc), self.clock()))
                raise
        return call
    return decorate


class Worker:
    def __init__(self, store=None, cfg=None, clock=time.time):
        self.store, self.cfg, self.clock = store or Store(), cfg or load(), clock
        self.instance = f'{os.getpid()}:{process_start(os.getpid())}:{uid()}'
        self.supervisor = Supervisor(self)

    def candidate(self, url, title='', channel='auto_discovery', depth=0, db=None, *, seed=False):
        if channel not in CHANNELS or type(depth) is not int or not 0 <= depth <= self.cfg['max_depth']:
            raise Rejected('invalid_channel_or_depth')
        url, title = safe_url(url), text(title)[:240]
        if any(term.casefold() in (unquote(url) + ' ' + title).casefold() for term in self.cfg['excluded_terms']):
            raise Rejected('excluded_venue')
        if any(v['venue_id'] in self.cfg['excluded_venues'] and url in (safe_url(value) for value in v.get('urls', [])) for v in self.cfg['venues']):
            raise Rejected('excluded_venue')
        configured_policy = seed_policy(self.cfg, url) if seed else None
        def insert(conn):
            old = conn.execute('SELECT id, channel FROM records WHERE brand=? AND url=?', ('HANSET', url)).fetchone()
            if old:
                if seed and old['channel'] == 'manual_clip':
                    # Explicit seed configuration schedules this URL even if it was clipped first.
                    row = conn.execute('SELECT data FROM records WHERE id=?', (old['id'],)).fetchone()
                    record = json.loads(row[0])
                    record['channel'] = 'auto_discovery'
                    self._save(conn, record)
                return old[0]
            now = self.clock()
            day, _ = day_and_expiry(now, self.cfg['lease_seconds'])
            charge(conn, self.cfg, day, 'HANSET', candidate=True)
            rid = uid()
            known_venue = venue_for(self.cfg, url, [])
            record = dict(record_id=rid, brand='HANSET', channel=channel, item_type='candidate',
                          url=url, title=title, source=urlsplit(url).hostname,
                          published_at=None, published_at_reason='unknown', collected_at=None,
                          venue_id=known_venue, venue_id_reason=None if known_venue else 'identity_unconfirmed', extracted_facts=[], evidence_locator=[],
                          dedup_group_id=None, verification_status='candidate', pipeline_stage=STAGES[0],
                          has_conflict=False, conflicts=[], privacy_status='passed', schema_version=1,
                          depth=depth, selected_evidence_ids=[])
            policy = configured_policy if seed else policy_for(self.cfg, url, now=now, required=False)
            if policy.get('adapter'):
                record['item_type'] = 'discovery_index'
            conn.execute('INSERT INTO records VALUES (?,?,?,?,?,?)',
                         (rid, 'HANSET', url, channel, STAGES[0], json.dumps(record, ensure_ascii=False)))
            self._history(conn, rid, STAGES[0], 'discovered')
            return rid
        if db is not None:
            return insert(db)
        with self.store.transaction() as conn:
            return insert(conn)

    @audited('reserve')
    def reserve(self, clip_id, url, document_id, channel='manual_clip', recheck=False, *,
                expected_extension=_RECORD_EXTENSION):
        clip_id, document_id = identifier(clip_id), identifier(document_id)
        url = safe_url(url)
        if channel not in CHANNELS:
            raise Rejected('invalid_channel')
        now = self.clock()
        policy_for(self.cfg, url, browser=channel == 'browser_extract', now=now,
                   required=channel != 'manual_clip')
        with self.store.transaction() as db:
            old = db.execute('SELECT * FROM reservations WHERE clip_id=?', (clip_id,)).fetchone()
            if old:
                if (old['url'], old['document_id'], old['channel']) != (url, document_id, channel):
                    raise Rejected('intent_mismatch')
                return dict(old)
            day, expiry = day_and_expiry(now, self.cfg['lease_seconds'])
            host = urlsplit(url).hostname
            db.execute('INSERT OR IGNORE INTO domains(host) VALUES (?)', (host,))
            dom = db.execute('SELECT * FROM domains WHERE host=?', (host,)).fetchone()
            if dom['owner'] or dom['stopped_until'] > now:
                raise Rejected('domain_held')
            rid = self.candidate(url, channel=channel, db=db)
            record = json.loads(db.execute('SELECT data FROM records WHERE id=?', (rid,)).fetchone()[0])
            extension_origin = record.get('extension_origin') if channel != 'manual_clip' else None
            if expected_extension is not _RECORD_EXTENSION and expected_extension != extension_origin:
                raise Rejected('permission_not_granted')
            if extension_origin:
                origin = 'https://' + urlsplit(url).netloc
                if not self.browser_grant_enabled(db, origin, extension_origin):
                    raise Rejected('permission_not_granted')
            charge(db, self.cfg, day, 'HANSET', body=True, new_body=not record.get('collected_at'))
            gen = db.execute('SELECT COALESCE(MAX(generation),0)+1 FROM reservations WHERE record_id=?', (rid,)).fetchone()[0]
            res_id = uid()
            db.execute('UPDATE domains SET owner=?,epoch=epoch+1 WHERE host=?', (res_id, host))
            db.execute('''INSERT INTO reservations(id,clip_id,record_id,brand,day,url,document_id,channel,generation,
                       domain_epoch,domain,state,issued,expires,worker,extension_origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (res_id, clip_id, rid, 'HANSET', day, url, document_id, channel, gen, dom['epoch'] + 1,
                        host, 'reserved', now, expiry, self.instance, extension_origin))
            return dict(db.execute('SELECT * FROM reservations WHERE id=?', (res_id,)).fetchone())

    @audited('begin')
    def begin(self, reservation):
        with self.store.transaction() as db:
            row = current(db, reservation, self.clock())
            if row['state'] == 'reserved':
                self._history(db, row['record_id'], STAGES[0], 'extraction_started')
            db.execute("UPDATE reservations SET state='running' WHERE id=?", (row['id'],))
            return dict(db.execute('SELECT * FROM reservations WHERE id=?', (row['id'],)).fetchone())

    def heartbeat(self, reservation):
        with self.store.transaction() as db:
            row = current(db, reservation, self.clock(), running=True)
            day, expiry = day_and_expiry(self.clock(), self.cfg['lease_seconds'])
            if day != row['day']:
                raise Rejected('wrong_budget_day')
            # Bounded total job duration: heartbeat never grants an unlimited browser run.
            expiry = min(expiry, row['issued'] + self.cfg['lease_seconds'])
            db.execute('UPDATE reservations SET expires=? WHERE id=?', (expiry, row['id']))
            return expiry

    def status(self, reservation):
        row = self.store.db.execute('SELECT * FROM reservations WHERE id=? AND generation=?',
                                    (reservation['id'], reservation['generation'])).fetchone()
        if not row:
            raise Rejected('stale_generation')
        return dict(row)

    def accept(self, reservation, raw, archive=None):
        try:
            return self._accept(reservation, raw, archive)
        except Rejected as exc:
            self.store.db.execute('INSERT INTO audit(operation,reason,at) VALUES (?,?,?)',
                                  ('accept', str(exc), self.clock()))
            raise

    def _accept(self, reservation, raw, archive=None):
        # Hash for exact replay, but never persist unfiltered payload or its strings.
        raw_hash = digest(raw)
        with self.store.transaction() as db:
            saved = db.execute('SELECT * FROM reservations WHERE id=?', (reservation['id'],)).fetchone()
            if saved and saved['generation'] == reservation['generation'] and saved['ack']:
                if raw_hash != saved['payload_hash']:
                    raise Rejected('payload_mismatch')
                return json.loads(saved['ack'])
            row = current(db, reservation, self.clock(), running=True)
            if raw.get('document_key') != row['document_id']:
                raise Rejected('document_mismatch')
            data = payload(raw, self.cfg['public_contacts'])
            if any(term.casefold() in (data['title'] + ' ' + data['excerpt']).casefold()
                   for term in self.cfg['excluded_terms']):
                raise Rejected('excluded_venue')
            now = self.clock()
            record = json.loads(db.execute('SELECT data FROM records WHERE id=?', (row['record_id'],)).fetchone()[0])
            source_url = safe_url(data.get('final_url') or row['url'])
            if row['channel'] == 'manual_clip' and source_url != row['url']:
                raise Rejected('document_mismatch')
            policy = policy_for(self.cfg, source_url, now=now, required=row['channel'] != 'manual_clip')
            venue = venue_for(self.cfg, source_url, data['facts'])
            from .extraction import facts_from_excerpt
            data['facts'] = facts_from_excerpt(self.cfg, source_url, data, now=now,
                                              require_access=row['channel'] != 'manual_clip')
            data = payload(data, self.cfg['public_contacts'])
            venue = venue_for(self.cfg, source_url, data['facts'])
            facts = evidence.observations(self.cfg, source_url, venue, data['facts'], now, row['id'])
            group, reason = group_for(self.cfg, row['url'], data['excerpt'], data['document_key'])
            others = [json.loads(r[0]) for r in db.execute('SELECT data FROM records WHERE id!=?', (record['record_id'],))]
            representative = next((r for r in others if r['dedup_group_id'] == group and r['pipeline_stage'] != STAGES[2]), None)
            stage = STAGES[2] if representative else STAGES[1]
            previous = dict(record)
            record.update(final_url=source_url, redirects=data.get('redirects') or [], title=data['title'], item_type='research' if policy.get('research') else 'venue',
                          published_at=data['published_at'], published_at_reason=None if data['published_at'] else 'unknown',
                          collected_at=dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
                          venue_id=venue, venue_id_reason=None if venue else 'identity_unconfirmed',
                          extracted_facts=facts, evidence_locator=[{'locator': data['locator'], 'quote': data['excerpt'][:400]}],
                          dedup_group_id=group, dedup_reason=reason,
                          representative_record_id=representative['record_id'] if representative else None,
                          verification_status='candidate', pipeline_stage=stage, selected_evidence_ids=[],
                          privacy_status='passed', recheck_requested=False)
            reason = 'source_accepted'
            if not representative and previous['pipeline_stage'] in STAGES[3:]:
                reason = evidence.retain_review(previous, record, now, self.cfg)
            if archive is not None:
                p = policy_for(self.cfg, source_url, now=now)
                permission = p.get('full_text_permissions', {}).get(source_url)
                if not permission or not p.get('archive_enabled'):
                    raise Rejected('archive_not_approved')
                sanitized = text(archive, self.cfg['public_contacts'])
                if len(sanitized) > 24000:
                    raise Rejected('archive_size')
                db.execute('INSERT INTO archives VALUES (?,?,?)', (row['id'], text(permission)[:240], sanitized))
            self._save(db, record)
            db.execute('INSERT INTO observations(record_id,reservation_id,data,observed) VALUES (?,?,?,?)',
                       (record['record_id'], row['id'], json.dumps(record, ensure_ascii=False), now))
            self._history(db, record['record_id'], record['pipeline_stage'], reason)
            self._refresh(db)
            ack = {'ack': row['id'], 'record_id': record['record_id']}
            db.execute("UPDATE reservations SET state='completed',payload_hash=?,ack=? WHERE id=?",
                       (raw_hash, json.dumps(ack), row['id']))
        self.supervisor.drain(dict(row))
        return ack

    def accept_discovery(self, reservation, source_url, redirects):
        """A feed checkpoint is operational progress, never checked venue evidence."""
        with self.store.transaction() as db:
            row = current(db, reservation, self.clock(), running=True)
            policy = policy_for(self.cfg, source_url, now=self.clock())
            if row['channel'] != 'auto_discovery' or not policy.get('adapter'):
                raise Rejected('not_discovery_index')
            record = json.loads(db.execute('SELECT data FROM records WHERE id=?', (row['record_id'],)).fetchone()[0])
            record.update(item_type='discovery_index', final_url=safe_url(source_url), redirects=redirects,
                          collected_at=dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).isoformat())
            self._save(db, record)
            self._history(db, record['record_id'], STAGES[0], 'discovery_checked')
            ack = {'ack': row['id'], 'record_id': record['record_id']}
            db.execute("UPDATE reservations SET state='completed',ack=? WHERE id=?", (json.dumps(ack), row['id']))
        self.supervisor.drain(dict(row))
        return ack

    def revoke(self, reservation):
        with self.store.transaction() as db:
            db.execute("UPDATE reservations SET state='revoking' WHERE id=? AND generation=? AND state IN ('reserved','running')",
                       (reservation['id'], reservation['generation']))
        return self.supervisor.drain(reservation)

    @staticmethod
    def browser_grant_enabled(db, origin, extension_origin):
        grant = db.execute('SELECT enabled FROM grants WHERE origin=? AND extension_origin=?',
                           (origin, extension_origin)).fetchone()
        return bool(grant and grant[0])

    def revoke_browser_grant(self, origin, extension_origin):
        with self.store.transaction() as db:
            grant = db.execute('UPDATE grants SET enabled=0 WHERE origin=? AND extension_origin=?',
                               (origin, extension_origin))
            if not grant.rowcount:
                return
            # Canonical HTTPS source origins use the default port. The domain is the reservation's source host.
            jobs = db.execute("""UPDATE reservations SET state='revoking'
                               WHERE channel='browser_extract' AND domain=? AND extension_origin=?
                               AND state IN ('reserved','running') RETURNING *""",
                              (urlsplit(origin).hostname, extension_origin)).fetchall()
        for job in jobs:
            self.supervisor.drain(dict(job))

    def _save(self, db, r):
        db.execute('UPDATE records SET stage=?,data=?,channel=? WHERE id=?',
                   (r['pipeline_stage'], json.dumps(r, ensure_ascii=False), r['channel'], r['record_id']))

    def _history(self, db, rid, stage, reason):
        db.execute('INSERT INTO history(record_id,stage,reason,at) VALUES (?,?,?,?)', (rid, stage, reason, self.clock()))

    def _refresh(self, db):
        records = [json.loads(r[0]) for r in db.execute('SELECT data FROM records')]
        issues = evidence.conflicts(records)
        for issue in issues:
            resolution = db.execute('SELECT evidence_id,reason,at FROM conflict_resolutions WHERE id=?', (issue['conflict_id'],)).fetchone()
            issue['resolved'] = bool(resolution)
            if resolution:
                issue['decision'] = dict(resolution)
        for r in records:
            r['conflicts'] = [x for x in issues if r['record_id'] in x['records']]
            r['has_conflict'] = any(not c['resolved'] for c in r['conflicts'])
            selected = [f for f in r['extracted_facts'] if f['evidence_id'] in r['selected_evidence_ids']]
            stale = any(evidence.fact_status(f, self.clock()) == '오래됨' for f in selected)
            r['stale'] = stale
            if (r['has_conflict'] or stale) and r['pipeline_stage'] in STAGES[3:]:
                r['pipeline_stage'], r['verification_status'] = STAGES[1], 'review_required'
                self._history(db, r['record_id'], STAGES[1], 'conflict_or_stale')
            self._save(db, r)

    def resolve_conflict(self, conflict_id, evidence_id, reason):
        identifier(reason)
        with self.store.transaction() as db:
            records = [json.loads(r[0]) for r in db.execute('SELECT data FROM records')]
            issue = next((x for x in evidence.conflicts(records) if x['conflict_id'] == conflict_id), None)
            if not issue or evidence_id not in issue['evidence_ids']:
                raise Rejected('unknown_conflict_or_evidence')
            db.execute('INSERT OR REPLACE INTO conflict_resolutions VALUES (?,?,?,?)',
                       (conflict_id, evidence_id, reason, self.clock()))
            db.execute('INSERT INTO reviews(record_id,data,at) VALUES (?,?,?)',
                       (issue['records'][0], json.dumps({'conflict_id': conflict_id, 'selected_evidence_id': evidence_id, 'reason': reason}), self.clock()))
            self._refresh(db)

    def verify(self, record_id, evidence_ids):
        with self.store.transaction() as db:
            self._refresh(db)
            r = json.loads(db.execute('SELECT data FROM records WHERE id=?', (record_id,)).fetchone()[0])
            if r['pipeline_stage'] not in (STAGES[1], STAGES[3]):
                raise Rejected('invalid_stage')
            for issue in r['conflicts']:
                if issue.get('resolved') and any(e in evidence_ids and e != issue['decision']['evidence_id'] for e in issue['evidence_ids']):
                    raise Rejected('conflict_decision_excludes_fact')
            evidence.review(r, evidence_ids, self.clock(), self.cfg)
            r['pipeline_stage'] = STAGES[3]
            self._save(db, r)
            db.execute('INSERT INTO reviews(record_id,data,at) VALUES (?,?,?)', (record_id, json.dumps(evidence_ids), self.clock()))
            self._history(db, record_id, STAGES[3], 'operator_verified')

    def publish_pending(self, record_id):
        with self.store.transaction() as db:
            self._refresh(db)
            r = json.loads(db.execute('SELECT data FROM records WHERE id=?', (record_id,)).fetchone()[0])
            if r['pipeline_stage'] != STAGES[3]:
                raise Rejected('not_verified')
            evidence.review(r, r['selected_evidence_ids'], self.clock(), self.cfg)
            r['pipeline_stage'] = STAGES[4]
            self._save(db, r)
            self._history(db, record_id, STAGES[4], 'operator_publish_pending')

    def _reconcile_access(self):
        eligible = []
        with self.store.transaction() as db:
            now = self.clock()
            rows = db.execute("SELECT data FROM records WHERE channel!='manual_clip'").fetchall()
            for row in rows:
                record = json.loads(row[0])
                try:
                    policy = policy_for(self.cfg, record['url'], browser=record['channel'] == 'browser_extract', now=now)
                except Rejected as exc:
                    reason = str(exc)
                    if record.get('access_hold') != reason:
                        record['access_hold'] = reason
                        self._save(db, record)
                        db.execute('INSERT INTO audit(operation,reason,at) VALUES (?,?,?)',
                                   ('access_policy', reason, now))
                    continue
                if record.pop('access_hold', None):
                    self._save(db, record)
                eligible.append((record, policy))
        return eligible

    def once(self):
        self.supervisor.recover()
        self.supervisor.expire()
        with self.store.transaction() as db:
            self._refresh(db)
        for seed in self.cfg['seeds']:
            try:
                self.candidate(seed, seed=True)
            except Rejected:
                continue
        eligible = self._reconcile_access()
        # Recheck volatile fields before new discovery; prior observations remain append-only.
        def priority(r):
            volatile = any(f['field'] in ('rates', 'hours', 'closures') for f in r['extracted_facts'])
            return (0 if volatile else 1, r.get('collected_at') or '', r['url'])
        for r, policy in sorted(eligible, key=lambda entry: priority(entry[0])):
            res = None
            try:
                last_ts = r.get('collected_at') or r.get('last_attempt_at')
                if last_ts and not r.get('recheck_requested'):
                    age = self.clock() - dt.datetime.fromisoformat(last_ts).timestamp()
                    interval = policy.get('recheck_seconds', 86400 if policy.get('adapter') or any(f['field'] in ('rates', 'hours', 'closures') for f in r['extracted_facts']) else 30 * 86400)
                    if age < max(300, interval):
                        continue
                extension_origin = r.get('extension_origin')
                if extension_origin:
                    origin = 'https://' + urlsplit(r['url']).netloc
                    if not self.browser_grant_enabled(self.store.db, origin, extension_origin):
                        continue
                    from .consent import require
                    require(self, r, origin)
                res = self.reserve(uid(), r['url'], uid(), r['channel'], expected_extension=extension_origin)
                self.begin(res)
                from .fetcher import collect
                collect(self, res)
                return True
            except Rejected as exc:
                if res:
                    self.revoke(res)
                    attempted_at = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).isoformat()
                    self.store.db.execute("UPDATE records SET data=json_set(data, '$.last_attempt_at', ?, '$.last_error', ?) WHERE id=?",
                                          (attempted_at, str(exc), r['record_id']))
                continue
        return False
