"""Fresh Chrome permission challenges. Missing/offline extension fails closed.

The intake services challenge replies while a separate --once worker executes.
An old stored grant is never sufficient authority to start a browser request.
"""
import time
import uuid
from .config import Rejected


def require(worker, record, origin, timeout=5):
    extension = record.get('extension_origin')
    if not extension:
        return
    challenge = str(uuid.uuid4())
    worker.store.db.execute('INSERT INTO consent_checks VALUES (?,?,?,?,NULL)',
                            (challenge, extension, origin, worker.clock() + timeout))
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            row = worker.store.db.execute('SELECT * FROM consent_checks WHERE id=?', (challenge,)).fetchone()
            if not row or row['expires'] <= worker.clock():
                break
            if row['allowed'] is not None:
                if row['allowed']:
                    return
                raise Rejected('permission_revoked')
            time.sleep(.05)
        raise Rejected('permission_check_unavailable')
    finally:
        worker.store.db.execute('DELETE FROM consent_checks WHERE id=?', (challenge,))


def exchange(worker, extension, answers):
    if not isinstance(answers, list) or len(answers) > 40:
        raise Rejected('invalid_consent_answers')
    with worker.store.transaction() as db:
        for answer in answers:
            if not isinstance(answer, dict) or type(answer.get('allowed')) is not bool:
                raise Rejected('invalid_consent_answer')
            db.execute('UPDATE consent_checks SET allowed=? WHERE id=? AND extension_origin=? AND expires>? AND allowed IS NULL',
                       (int(answer['allowed']), answer.get('id'), extension, worker.clock()))
        db.execute('DELETE FROM consent_checks WHERE expires<=?', (worker.clock(),))
        return {'checks': [dict(r) for r in db.execute('SELECT id,origin FROM consent_checks WHERE extension_origin=? AND allowed IS NULL LIMIT 40', (extension,))]}
