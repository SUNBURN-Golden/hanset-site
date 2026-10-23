"""Fail-closed minimization before persistence and AI. No raw quarantine or logging."""
import re
import unicodedata
import ipaddress
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, unquote, quote
from .config import Rejected

EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
PHONE = re.compile(r'(?<!\d)(?:\+\d{1,3}[ .-]?)?(?:0\d{1,2})[ .()-]*\d{3,4}[ .-]*\d{4}(?!\d)')
INTERNATIONAL_PHONE = re.compile(r'(?<!\w)(?:\+\d{1,3}[ .-]?(?:\d[ .()-]*){7,12}\d|\(\d{3}\)[ .-]*\d{3}[ .-]*\d{4})(?!\d)')
FINANCIAL_NUMBER = re.compile(r'(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)')
RRN = re.compile(r'(?<!\d)\d{6}[ -]?[1-8]\d{6}(?!\d)')
SECRET = re.compile(r'(?i)(?:bearer\s+\S+|(?:password|passwd|cookie|token|secret|api[_-]?key|비밀번호)\s*[:=]\s*[^\s,;]+|eyJ[\w-]+\.[\w-]+\.[\w-]+)')
PRIVATE = re.compile(r'(?i)(?:direct message|private message|members?[-\s]+only\s+(?:conversation|chat|discussion|message|forum)|inbox|개인\s*프로필|쪽지|회원\s*전용\s*(?:대화|채팅|메시지|쪽지|게시판)|주거\s*주소|집\s*주소|자택|주민등록|계좌\s*번호|신용카드\s*번호|진료\s*기록|건강\s*기록|나의\s*진단|아동\s*개인|아이의\s*(?:이름|학교)|my\s*(?:diagnosis|bank account|home address)|child.{0,20}(?:name|school)|\bDM\b)')
PRIVATE_DETAILS = re.compile(r'(?i)(?:patient\s*(?:name|record|diagnosis)|medical\s*record|my\s*(?:medication|income|salary|balance)|credit\s*card\s*(?:number|details)|(?:제|저의)\s*(?:병명|진단|복용약|계좌|잔고)|(?:자녀|아동|미성년자).{0,16}(?:이름|학교|생년월일)|\b(?:son|daughter).{0,16}(?:name|school|born))')
ADDRESS = re.compile(r'(?:[가-힣]+(?:로|길)\s*\d+(?:-\d+)?\s*(?:번지|동|호)|\d+\s+\w+\s+(?:Street|Road|Avenue)\b)', re.I)
INJECTION = re.compile(r'(?i)(?:ignore\s+(?:all\s+)?(?:previous|prior|system)\s+instructions|이전\s*지시.*무시|이\s*명령을?\s*실행|system\s*prompt|execute\s+(?:this|the following)\s+command)')
SENSITIVE_KEY = re.compile(r'(?i)(?:token|auth|password|passwd|secret|cookie|session|email|phone|address|input|textarea|contenteditable|profile|private|full_text|html)')


def safe_url(value):
    if not isinstance(value, str) or len(value) > 2048 or re.search(r'[\s\\\x00-\x1f]', value):
        raise Rejected('unsafe_url')
    decoded = unicodedata.normalize('NFKC', unquote(unquote(value)))
    if re.search(r'[\\\x00-\x1f\x7f]', decoded):
        raise Rejected('unsafe_url')
    try:
        u = urlsplit(decoded)
        port = u.port
    except ValueError:
        raise Rejected('unsafe_url') from None
    host = (u.hostname or '').lower().rstrip('.')
    try:
        host = host.encode('idna').decode('ascii')
    except UnicodeError:
        raise Rejected('unsafe_url') from None
    if ':' not in host and (len(host) > 253 or any(
            not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
            for label in host.split('.'))):
        raise Rejected('unsafe_url')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (not address.is_global or address.is_multicast or address.is_reserved or
                    getattr(address, 'ipv4_mapped', None)):
        raise Rejected('non_public_destination')
    if (u.scheme != 'https' or not host or u.username or u.password or port not in (None, 443)
            or EMAIL.search(decoded) or SECRET.search(decoded) or PHONE.search(decoded)
            or host == 'naver.com' or host.endswith('.naver.com') or host == 'naver.net'
            or host.endswith('.naver.net') or any(x in u.path.lower().split('/') for x in
                    ('login', 'signin', 'inbox', 'messages', 'account', 'profile'))):
        raise Rejected('unsafe_url')
    query = parse_qsl(u.query, keep_blank_values=True)
    if any(SENSITIVE_KEY.search(k) or len(v) > 200 for k, v in query):
        raise Rejected('sensitive_url')
    query = [(k, v) for k, v in query if not k.lower().startswith(('utm_', 'fbclid', 'gclid'))]
    path = u.path or '/'
    if '..' in path.split('/') or '%' in decoded or re.search(r'(?i)(calendar|/search|[?&](?:q|search|page|offset)=)', path + '?' + urlencode(query)):
        raise Rejected('url_trap')
    if text(decoded) != decoded:
        raise Rejected('sensitive_url')
    # Validation uses decoded text; transport and identity always use ASCII URLs.
    path = quote(path, safe="/:@!$&'()*+,;=-._~")
    return urlunsplit(('https', '[' + host + ']' if ':' in host else host, path, urlencode(sorted(query)), ''))


def text(value, contacts=()):
    if not isinstance(value, str):
        raise Rejected('invalid_text')
    value = unicodedata.normalize('NFKC', value)
    if PRIVATE.search(value) or PRIVATE_DETAILS.search(value) or ADDRESS.search(value):
        raise Rejected('private_context')
    allowed = {'hanset@soulbound.co.kr', *contacts}
    value = EMAIL.sub(lambda m: m[0] if m[0] in allowed else '[redacted]', value)
    for pattern in (RRN, PHONE, INTERNATIONAL_PHONE, FINANCIAL_NUMBER, SECRET):
        value = pattern.sub(lambda m: m[0] if m[0] in allowed and pattern in
                            (PHONE, INTERNATIONAL_PHONE) else '[redacted]', value)
    # Suspect instructions are not retained, even in quarantine.
    value = '\n'.join('[untrusted instruction removed]' if INJECTION.search(line) else line
                      for line in value.splitlines())
    return value.strip()


def clean(value, contacts=(), depth=0):
    if depth > 12:
        raise Rejected('payload_depth')
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise Rejected('invalid_key')
            if SENSITIVE_KEY.search(k):
                continue
            key = text(k, contacts)
            result[key] = safe_url(v) if k in ('url', 'source_url', 'booking_url') else clean(v, contacts, depth + 1)
        return result
    if isinstance(value, list):
        if len(value) > 200:
            raise Rejected('payload_size')
        return [clean(v, contacts, depth + 1) for v in value]
    if isinstance(value, str):
        if len(value) > 24000:
            raise Rejected('payload_size')
        return text(value, contacts)
    if value is None or type(value) in (bool, int, float):
        return value
    raise Rejected('invalid_payload')


def ai_payload(record):
    if record.get('privacy_status') != 'passed':
        raise Rejected('privacy_not_passed')
    return {'untrusted_data': clean({k: record[k] for k in ('title', 'extracted_facts', 'evidence_locator')}),
            'tools': [], 'can_verify': False, 'can_publish': False}
