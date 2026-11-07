"""Shared refusal rules for official pages and desktop Instagram batches."""
import re

SECURITY_RE = re.compile(
    r'verify you are human|suspicious login|login challenge|security check required|'
    r'checkpoint_required|\bcaptcha\b|just a moment|비정상적인 (?:로그인|접근)|보안\s*확인(?:이|을)?\s*필요',
    re.I)
POLICY_RE = re.compile(r'privacy|policy|terms|약관|개인정보', re.I)
MEDIA_KEYS = frozenset({
    'photo', 'photos', 'image', 'images', 'media', 'media_url', 'thumbnail',
    'screenshot', 'video', 'raw', 'caption_file', 'bytes',
})
ASSET_TYPES = ('image/', 'video/', 'audio/', 'application/pdf', 'application/octet-stream')


def asset_type(content_type, url):
    content_type = (content_type or '').lower()
    path = url.lower().split('?', 1)[0]
    return any(part in content_type for part in ASSET_TYPES) or path.endswith((
        '.png', '.jpg', '.jpeg', '.gif', '.webp', '.pdf', '.mp4', '.svg', '.ico'))


def looks_like_security(text):
    return bool(text) and bool(SECURITY_RE.search(text))
