import hashlib
import re
import unicodedata
from .pii import strip_pii

def normalize_text(text: str) -> str:
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', strip_pii(text))).strip()

def content_hash(content: str | bytes, content_type: str = 'text/plain') -> str:
    if 'html' in content_type:
        from ..extract.html import extract
        payload = normalize_text(extract(content)['text']).encode()
    elif isinstance(content, str):
        payload = normalize_text(content).encode()
    else:
        payload = content
    return hashlib.sha256(payload).hexdigest()
