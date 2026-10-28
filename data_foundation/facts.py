"""Immutable observations and explicit conflict intake."""
import datetime as dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

KST = dt.timezone(dt.timedelta(hours=9))
SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / 'schema/fact.schema.json').read_text()
)
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
JSON_COLUMNS = ('value', 'qualifiers', 'evidence_locator')


def digest(value):
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def now():
    return dt.datetime.now(KST).isoformat()


ABSENCE_ATTRIBUTES = {'price','full_price','scrub_fee','guest_discount','last_entry'}


def absence_clause(attribute, text):
    return (attribute in ABSENCE_ATTRIBUTES
            and not re.search(r'\b(included|free)\b', text, re.I)
            and bool(re.search(r'\bnot (?:listed|stated|shown|on (?:the )?(?:official )?page)\b', text, re.I)))


def validate_fact(fact: dict) -> None:
    VALIDATOR.validate(fact)
    if fact['valid_from'] and fact['valid_to']:
        start = dt.datetime.fromisoformat(fact['valid_from'])
        end = dt.datetime.fromisoformat(fact['valid_to'])
        if end < start:
            raise ValueError('valid_to precedes valid_from')
    unknown_prefixes = ('not confirmed', 'not stated', 'not listed', 'awaiting reply')
    wording = fact['value_text'].strip().lower()
    if fact['value_state'] == 'known' and ((wording.startswith(unknown_prefixes) and not (wording.startswith(('not listed','not stated')) and fact['attribute'] in ABSENCE_ATTRIBUTES and re.search(r'\d|\b(?:included|free)\b',wording))) or (absence_clause(fact['attribute'], wording) and not re.search(r'\d',wording))):
        raise ValueError('unknown wording requires unknown state')


def insert_fact(db: sqlite3.Connection, fact: dict) -> bool:
    """Insert once by deterministic identity; caller owns the transaction."""
    validate_fact(fact)
    encoded = {
        key: json.dumps(value, ensure_ascii=False, sort_keys=True)
        if key in JSON_COLUMNS and value is not None else value
        for key, value in fact.items()
    }
    exists = db.execute(
        'SELECT 1 FROM facts WHERE fact_id=?', (fact['fact_id'],)
    ).fetchone()
    if exists:
        return False
    columns = ','.join(encoded)
    parameters = ','.join('?' for _ in encoded)
    db.execute(
        f'INSERT INTO facts ({columns}) VALUES ({parameters})',
        tuple(encoded.values()),
    )
    return True


def record(
    subject_id, subject_type, attribute, text, *, evidence_locator, source_url,
    value=None, value_state='known', source_type='official_page',
    status='source_claim', collected_at=None, **extra,
):
    """Construct interchange data; evidence and source must be explicit."""
    stamp = collected_at or now()
    fact = dict(
        fact_id='', subject_id=subject_id, subject_type=subject_type,
        attribute=attribute, qualifiers={}, value=value, value_text=text,
        unit=None, value_state=value_state, source_url=source_url,
        source_type=source_type, evidence_locator=evidence_locator,
        collected_at=stamp, valid_from=None, valid_to=None, status=status,
        conflict_group=None, supersedes_fact_id=None, correction_of=None,
        collection_permission='public_page_factual',
        retention_policy='facts_keep_history', retention_until=None,
        retention_days=None, extractor_id='manual', run_id='manual',
        created_at=stamp, closed_reason=None, closed_by_fact_id=None,
    )
    fact.update(extra)
    fact['fact_id'] = extra.get('fact_id') or 'fact_' + digest(fact)
    return fact


def close_fact(db, old_id, new_id, effective_at, reason):
    db.execute(
        'UPDATE facts SET valid_to=?,closed_reason=?,closed_by_fact_id=? '
        'WHERE fact_id=?',
        (effective_at, reason, new_id, old_id),
    )


def group_conflict(db, incoming, different):
    """Append grouped replicas: immutable old rows cannot acquire a group."""
    group = next(
        (row['conflict_group'] for row in different if row['conflict_group']),
        None,
    ) or 'conflict_' + digest([
        incoming['subject_id'], incoming['attribute'], incoming['qualifiers'],
    ])
    incoming['conflict_group'] = group
    ids = []
    for row in different:
        if row['conflict_group'] == group:
            ids.append(row['fact_id'])
            continue
        replica = dict(row)
        for key in JSON_COLUMNS:
            replica[key] = json.loads(replica[key]) if replica[key] is not None else None
        replica.update(
            fact_id='fact_' + digest([row['fact_id'], group]),
            conflict_group=group, supersedes_fact_id=row['fact_id'],
            valid_from=incoming['collected_at'], created_at=incoming['created_at'],
        )
        insert_fact(db, replica)
        close_fact(
            db, row['fact_id'], replica['fact_id'],
            incoming['collected_at'], 'conflict_grouped',
        )
        ids.append(replica['fact_id'])
    ids.append(incoming['fact_id'])
    db.execute(
        'INSERT OR IGNORE INTO review_items VALUES (?,?,?,?,?,?,?)',
        ('review_' + digest(ids), 'conflict', group, json.dumps(ids),
         'open', '', incoming['created_at']),
    )


def observe(db: sqlite3.Connection, fact: dict) -> str:
    """Confirm, replace a source lineage, or retain conflicting source values.

    Source lineage is exact source_type + source_url; qualifiers scope comparison.
    A corrected URL needs an operator correction row, not fuzzy matching.
    The function owns its transaction. Unknowns never close known observations.
    """
    validate_fact(fact)
    with db:
        if not db.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        if db.execute(
            'SELECT 1 FROM facts WHERE fact_id=?', (fact['fact_id'],)
        ).fetchone():
            return 'duplicate'
        rows = db.execute(
            'SELECT * FROM current_facts '
            'WHERE subject_id=? AND attribute=? AND qualifiers=?',
            (fact['subject_id'], fact['attribute'],
             json.dumps(fact['qualifiers'], sort_keys=True, ensure_ascii=False)),
        ).fetchall()
        known = [row for row in rows if row['value_state'] == 'known']
        if fact['value_state'] != 'known' and known:
            insert_fact(db, fact)
            return 'unknown_retained'
        same = [
            row for row in rows
            if row['source_url'] == fact['source_url']
            and row['source_type'] == fact['source_type']
        ]

        def equivalent(row):
            if row['value_state'] != fact['value_state']:
                return False
            if row['value'] is not None:
                return json.dumps(json.loads(row['value']), sort_keys=True) == json.dumps(fact['value'], sort_keys=True)
            return fact['value'] is None and row['value_text'] == fact['value_text']

        equal = [row for row in rows if equivalent(row)]
        if equal and not fact['correction_of'] and not any(
            not equivalent(row) for row in same
        ):
            for row in equal:
                db.execute(
                    'INSERT OR IGNORE INTO fact_confirmations VALUES (?,?,?,?,?,?,?,?)',
                    ('confirm_' + digest([row['fact_id'], fact['fact_id']]),
                     row['fact_id'], fact['collected_at'],
                     json.dumps(fact['evidence_locator']), fact['run_id'],
                     fact['source_url'], fact['source_type'], fact['status']),
                )
            return 'confirmed'
        different = [
            row for row in known if not equivalent(row) and row not in same
        ] if fact['value_state'] == 'known' else []
        successor = dict(fact)
        if different:
            group_conflict(db, successor, different)
        if same:
            successor['valid_from'] = fact['valid_from'] or fact['collected_at']
            successor['supersedes_fact_id'] = same[-1]['fact_id']
        insert_fact(db, successor)
        for row in same:
            close_fact(
                db, row['fact_id'], successor['fact_id'],
                fact['valid_from'] or fact['collected_at'],
                'corrected' if fact['correction_of'] else 'changed',
            )
        if different:
            return 'conflict'
        return 'changed' if same else 'inserted'
