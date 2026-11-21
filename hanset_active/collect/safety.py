"""Shared refusal rules for official pages and desktop Instagram batches."""
import re
import unicodedata

from hanset_active.util.pii import strip_pii

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


PROHIBITED_TERMS = (
    'MZ' + ' 사우나', '한' + '세트', '디톡스', '해독 효과', '면역력', '살 빠', '체중 감량',
    '체지방 감소', '질병 예방', '혈액순환 개선', 'detox', 'immunity', 'immune boost',
    'weight loss', 'burns fat', 'disease prevention', 'improves circulation',
)
_PROHIBITED = re.compile('|'.join(re.escape(term).replace(r'\ ', r'\s*')
                                  for term in PROHIBITED_TERMS), re.I)
_PERSONAL = re.compile(
    r'(?:\b(?:athlete|competitor|participant|winner|owner|representative|'
    r'coach|instructor|manager|employee|(?:full |legal )?name|email|phone|mobile)\s*[:=]|'
    r'(?:선수|참가자|우승자|대표|운영자|강사|코치|담당자|성명|이름|연락처|전화번호|휴대폰)\s*[:=]|'
    r'\b(?:Mr|Mrs|Ms|Dr)\.?\s+[A-Z]|\b[A-Z][a-z]+\s+[A-Z][a-z]+\s+'
    r'(?:won|placed|finished|ranked|owns|operates|manages|founded)\b|'
    r'[가-힣]{2,5}\s*(?:선수|님|씨)(?=\s|$|[,.])|'
    r'\b(?:leaderboard|podium|medalli?st|competition results?|race results?|'
    r'personal (?:history|record|best)|\d+(?:st|nd|rd|th) place)\b|'
    r'(?:대회|경기|선수|개인)\s*(?:결과|기록|순위|경력)|(?:입상|수상|완주)\s*(?:경력|기록)|'
    r'\b(?:champions?|contestants?|placed|ranked)\b|'
    r'(?:우승|준우승|입상|수상|\d+\s*위)(?=\s|$|[,.]))', re.I)
_CONTACT = re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\[(?:phone|email|token|redacted[^\]]*)\]', re.I)
_INJECTION = re.compile(
    r'ignore (?:all |the |any )?(?:previous|above|system) instructions?|system prompt|developer message|이전\s*지시.*무시',
    re.I)


def evidence_rejection(text, credentials=()):
    folded = unicodedata.normalize('NFKC', text)
    if _PROHIBITED.search(folded):
        return 'prohibited_wording'
    if _PERSONAL.search(folded):
        return 'personal_data'
    if _INJECTION.search(folded):
        return 'instruction_text'
    if (strip_pii(folded) != folded or _CONTACT.search(folded)
            or any(secret in text or secret in folded for secret in credentials)):
        return 'sensitive_evidence'
    return None


def remove_personal_text(text):
    """Blank personal clauses in the model payload while preserving offsets."""
    clauses = r'(?:[^\n.!?。;]|(?<=\d)\.(?=\d)|\.(?=[A-Za-z_-]))+'
    return re.sub(clauses, lambda match: ' ' * len(match[0]) if _PERSONAL.search(match[0]) else match[0], text)


def venue_identity_safe(venue_id, venue_key, credentials=()):
    # Active venue IDs are deterministic SHA-256 digests.
    if not all(isinstance(value, str) and value.strip() for value in (venue_id, venue_key)):
        return False
    if re.fullmatch(r'[0-9a-f]{64}', venue_id):
        if any(secret in venue_id for secret in credentials):
            return False
    elif evidence_rejection(venue_id, credentials):
        return False
    return not evidence_rejection(venue_key, credentials)
