"""Operating public bath and fitness licenses, projected before persistence."""
from collections import Counter
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlencode, unquote

from ..db import deterministic_id
from . import checkpoints
from .candidates import Observation, clean_text, public_url

DATASETS = {'public_baths': 'sauna', 'fitness_centers': 'gym'}
HOST = 'https://apis.data.go.kr/1741000'
GYM_RELEVANCE = re.compile(r'cross[\s-]*fit|크로스\s*핏|hyrox|하이(?:록|락)스|functional[\s-]*fitness|기능성\s*(?:운동|피트니스)', re.I)
_PAGE_SIZE = 100


def credential():
    path = Path(os.environ.get('HANSET_DATAGO_KEY_FILE', '/workspace/.secrets/datago_key'))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                return '', 'skipped:unsafe_key'
            value = unquote(stream.read(4097).decode('ascii').strip())
        if not re.fullmatch(r'[A-Za-z0-9+/=_~.-]{1,1024}', value):
            return '', 'skipped:invalid_key'
        return value, 'ok'
    except FileNotFoundError:
        return '', 'skipped:no_key'
    except (OSError, UnicodeError):
        return '', 'skipped:unsafe_key'


def _timestamp(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.fullmatch(r'\d{14}', value):
        try:
            value = datetime.strptime(value, '%Y%m%d%H%M%S').isoformat(sep=' ')
        except ValueError:
            return None
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?', value):
        return None
    try:
        return datetime.fromisoformat(value).isoformat(sep=' ')
    except ValueError:
        return None


def _page(body, expected_page):
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError
    result = result.get('response', result)
    if not isinstance(result, dict):
        raise ValueError
    header = result.get('header', {})
    if not isinstance(header, dict) or str(header.get('resultCode', '00')) not in ('00', '0', 'NORMAL_SERVICE'):
        raise ValueError
    payload = result.get('body', result)
    if not isinstance(payload, dict):
        raise ValueError
    if 'resultCode' in payload and str(payload['resultCode']) not in ('00', '0', 'NORMAL_SERVICE'):
        raise ValueError
    page = payload.get('pageNo', payload.get('page', expected_page))
    total = payload.get('totalCount', payload.get('matchCount', payload.get('total')))
    rows = payload.get('data', payload.get('items'))
    if isinstance(rows, dict):
        rows = rows.get('item')
        if isinstance(rows, dict):
            rows = [rows]
    if total is None or int(page) != expected_page or int(total) < 0:
        raise ValueError
    if rows in (None, '') and int(total) == 0:
        rows = []
    if not isinstance(rows, list) or len(rows) > _PAGE_SIZE or any(not isinstance(row, dict) for row in rows):
        raise ValueError
    if not rows and int(total) > (expected_page - 1) * _PAGE_SIZE:
        raise ValueError
    return rows, int(total)


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and abs(number) <= 1_000_000_000 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _project(row, dataset, key, endpoint, store, resolve_url):
    # Any echoed credential can occur in an ostensibly safe field. Remove it
    # before applying the per-field whitelist and before hashing revisions.
    row = {k: v for k, v in row.items()
           if not isinstance(v, str) or not (key in v or key in unquote(v))}
    raw_id = row.get('MNG_NO') or row.get('MGT_NO') or row.get('OPN_SVC_MGT_NO')
    status = row.get('SALS_STTS_CD')
    updated = _timestamp(row.get('DAT_UPDT_PNT'))
    if status is None:
        raise ValueError
    name = clean_text(row.get('BPLC_NM') or row.get('BPLCNM'))
    road = clean_text(row.get('ROAD_NM_ADDR') or row.get('RDN_WHL_ADDR') or row.get('RDNWHLADDR'))
    lot = clean_text(row.get('LOTNO_ADDR') or row.get('LCTN_ADDR') or row.get('SITE_WHL_ADDR') or row.get('SITEWHLADDR'))
    address = road or lot
    record = deterministic_id('licenses', dataset, str(raw_id) if raw_id else (name, address))
    if str(status).zfill(2) != '01' or not name:
        return record, updated, None
    fields = {'road_address': road, 'lot_address': lot, 'operating_status': '01'}
    stamp = row.get('LCPMT_YMD') or row.get('LCPMT_DT') or row.get('APV_PERM_YMD') or row.get('APVPERMYMD') or row.get('LICENSE_DATE')
    if isinstance(stamp, str):
        try:
            fields['license_date'] = datetime.strptime(stamp[:10].replace('-', ''), '%Y%m%d').date().isoformat()
        except ValueError:
            pass
    x = _number(row.get('X', row.get('CRD_INFO_X', row.get('X_CRD'))))
    y = _number(row.get('Y', row.get('CRD_INFO_Y', row.get('Y_CRD'))))
    if x is not None and y is not None:
        fields['coordinates'] = {'x': x, 'y': y, 'crs': 'EPSG:5174'}
    parts = address.split()
    sido = clean_text(row.get('CTPV_NM') or row.get('SIDO')) or (parts[0] if parts else '')
    sigungu = clean_text(row.get('SGG_NM') or row.get('SIGUNGU')) or (parts[1] if len(parts) > 1 else '')
    category = DATASETS[dataset]
    if dataset == 'fitness_centers':
        if re.search(r'cross[\s-]*fit|크로스\s*핏', name, re.I):
            category = 'crossfit_box'
        elif re.search(r'hyrox|하이(?:록|락)스', name, re.I):
            category = 'hyrox_box'
    observation = Observation(name, category, 'licenses', endpoint, address, sido, sigungu,
                              record_key=record, fields=fields)
    url = public_url(row.get('HMPG_ADDR') or row.get('HOMEPAGE') or row.get('URL'), testing=store.testing)
    if url:
        observation = resolve_url(observation, url)
    if dataset == 'fitness_centers' and not (GYM_RELEVANCE.search(name)
            or observation.fetch_url or store.known_public_presence(name, address)):
        observation = None
    return record, updated, observation


def discover(store, getter, resolve_url):
    key, status = credential()
    if not key:
        return Counter(), status
    total_counts, results = Counter(), []
    for dataset in DATASETS:
        counts, status = _dataset(store, getter, resolve_url, dataset, key)
        total_counts.update(counts)
        results.append(status)
    return total_counts, 'ok' if all(status == 'ok' for status in results) else next(
        status for status in results if status != 'ok')


def _dataset(store, getter, resolve_url, dataset, key):
    db, clock = store.db, store.clock
    endpoint = f'{HOST}/{dataset}/info'
    progress = checkpoints.load(db, 'licenses', dataset)
    committed = progress.get('watermark')
    page = progress.get('page', 1)
    highest = progress.get('highest', committed)
    seen = set(progress.get('pages', []))
    counts = Counter()
    while True:
        params = {'returnType': 'json', 'pageNo': page, 'numOfRows': _PAGE_SIZE, 'cond[SALS_STTS_CD::EQ]': '01'}
        if committed:
            params['cond[DAT_UPDT_PNT::GTE]'] = committed
        result = getter(endpoint + '?' + urlencode(params), kind='api', service_key=key)
        if not result.ok:
            return counts, 'partial:license_request'
        try:
            rows, total = _page(result.content, page)
            projected = [_project(row, dataset, key, endpoint, store, resolve_url) for row in rows]
        except (ValueError, TypeError, RecursionError, OverflowError):
            return counts, 'partial:license_response'
        fingerprint = deterministic_id(sorted((record, updated or '') for record, updated, _ in projected))
        if fingerprint in seen:
            return counts, 'partial:repeated_page'
        if page * _PAGE_SIZE < total and len(rows) != _PAGE_SIZE:
            return counts, 'partial:license_response'
        seen.add(fingerprint)
        highest = max([highest or '', *(updated or '' for _, updated, _ in projected)]) or None
        finished = page * _PAGE_SIZE >= total
        cursor = {'watermark': highest, 'pass': 'incremental' if highest else 'full'} if finished else {
            'watermark': committed, 'pass': 'incremental' if committed else 'full',
            'highest': highest, 'page': page + 1, 'pages': sorted(seen)}
        with db:
            for _, _, observation in projected:
                if observation:
                    counts.update(store.record(observation))
            checkpoints.save(db, 'licenses', dataset, cursor, clock)
        if finished:
            return counts, 'ok'
        page += 1
