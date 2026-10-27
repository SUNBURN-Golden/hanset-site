"""Sanitize before persistence, including URL parameters and raw text."""
import re
from urllib.parse import unquote

_EMAIL = re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}')
_SECRET = re.compile(r'(?i)(\b(?:api[_-]?key|token|secret|session|sid|auth)[\"\']?\s*[=:]\s*[\"\']?)([^\s&<>"\']+)')
_PHONE = re.compile(r'(?<!\w)(?:\+\d{1,3}[ .-]?)?(?:\(?0\d{1,2}\)?[ .-]?\d{3,4}[ .-]?\d{4}|1\d{3}[ .-]\d{4}|\+\d[\d .()-]{7,}\d)(?!\w)')

def strip_pii(value: str | bytes) -> str | bytes:
    binary = isinstance(value, bytes)
    text = value.decode('utf-8', errors='replace') if binary else value
    text = unquote(text)
    text = _EMAIL.sub(lambda m: m[0] if m[0].lower() == 'hanset@soulbound.co.kr' else '[email]', text)
    text = re.sub(r'(?i)Bearer\s+[^\s<>"\']+', 'Bearer [token]', text)
    text = _SECRET.sub(lambda m: m[1] + '[token]', text)
    text = re.sub(r'\beyJ[\w-]+\.[\w-]+\.[\w-]+\b', '[token]', text)
    text = re.sub(r'(?<![\w])(?:[A-Za-z0-9_+/=-]{32,})(?![\w])', '[token]', text)
    text = _PHONE.sub('[phone]', text)
    return text.encode() if binary else text
