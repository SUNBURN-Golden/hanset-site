"""Sanitize before persistence, including URL parameters and raw text."""
import re
from urllib.parse import unquote

_EMAIL = re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}')
_SECRET = re.compile(r'(?i)(\b(?:api[_-]?key|service[_-]?key|token|secret|session|sid|auth)[\"\']?\s*[=:]\s*[\"\']?)([^\s&<>"\']+)')
_UUID = re.compile(r'\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b', re.I)
# Do not start a match on the minute digits of a time such as 22:00.
_PHONE = re.compile(r'(?<!\w)(?<!\d:)(?:\+\d{1,3}[ .-]?)?(?:\(?0\d{1,2}\)?[ .-]?\d{3,4}[ .-]?\d{4}|1\d{3}[ .-]\d{4}|\+\d[\d .()-]{7,}\d)(?!\w)')

def strip_pii(value: str | bytes) -> str | bytes:
    if value is None: return ''
    binary = isinstance(value, bytes)
    text = value.decode('utf-8', errors='replace') if binary else value
    text = unquote(text)
    text = _EMAIL.sub(lambda m: m[0] if m[0].lower() == 'hanset@soulbound.co.kr' else '[email]', text)
    text = re.sub(r'(?i)Bearer\s+[^\s<>"\']+', 'Bearer [token]', text)
    text = _SECRET.sub(lambda m: m[1] + '[token]', text)
    text = re.sub(r'\beyJ[\w-]+\.[\w-]+\.[\w-]+\b', '[token]', text)
    text = re.sub(r'(?<![\w])(?!(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?![\w]))(?=[A-Za-z0-9_+=-]*\d)(?=[A-Za-z0-9_+=-]*[A-Za-z])[A-Za-z0-9_+=-]{32,}(?![\w])', '[token]', text)  # long mixed alnum runs; '/' excluded so URL paths survive, UUID ids kept
    # UUIDs can contain consecutive numeric segments shaped like a phone.
    parts, start = [], 0
    for uuid in _UUID.finditer(text):
        parts.extend((_PHONE.sub('[phone]', text[start:uuid.start()]), uuid[0]))
        start = uuid.end()
    text = ''.join([*parts, _PHONE.sub('[phone]', text[start:])])
    return text.encode() if binary else text
