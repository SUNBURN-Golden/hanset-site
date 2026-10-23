"""Venue identity and document identity are separate, explicit decisions."""
from .schema import digest
from .privacy_filter import safe_url
import re
from urllib.parse import urlsplit, unquote


def venue_for(cfg, url, facts):
    names = {str(f['value']).casefold() for f in facts if f['field'] in ('place_name_kr', 'place_name_en')}
    branches = {str(f['value']).casefold() for f in facts if f['field'] == 'branch'}
    for v in cfg['venues']:
        if v['venue_id'] in cfg['excluded_venues']:
            continue
        if url in (safe_url(value) for value in v.get('urls', [])):
            return v['venue_id']
        if names.intersection(a.casefold() for a in v.get('aliases', [])) and v.get('branch', '').casefold() in branches:
            return v['venue_id']
    return None


def group_for(cfg, url, excerpt, document_key=None):
    # Caller-supplied DOI/PMID or translation assertions alone never merge records.
    for relation in cfg['document_relations']:
        if url in (safe_url(value) for value in relation['urls']):
            return 'doc-' + digest(relation['id'])[:24], 'approved_relation'
    u = urlsplit(url)
    if u.hostname in ('doi.org', 'api.crossref.org'):
        doi = unquote(u.path).removeprefix('/works/').lstrip('/').casefold()
        if re.fullmatch(r'10\.\d{4,9}/\S+', doi):
            return 'doc-' + digest(['doi', doi])[:24], 'doi'
    if u.hostname == 'pubmed.ncbi.nlm.nih.gov' and re.fullmatch(r'/\d+/', u.path):
        return 'doc-' + digest(['pmid', u.path.strip('/')])[:24], 'pmid'
    normalized = ' '.join(excerpt.split()).casefold()
    # Generic index labels and short common facility lines are not enough to
    # establish syndication. Keep independent sources separate for review.
    if len(normalized) < 160 or normalized == '[untrusted instruction removed]':
        return 'doc-' + digest(url)[:24], 'url_only_insufficient_fingerprint'
    return 'doc-' + digest(normalized)[:24], 'content_fingerprint'
