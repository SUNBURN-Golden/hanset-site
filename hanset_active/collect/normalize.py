"""Pure comparison rules. Original evidence is never rewritten."""
import json
import math
import re

LEGACY_VERSION = 'source-claim-v1'
VERSION = 'source-claim-v2'
AVAILABILITY_FIELDS = frozenset({'drop_in_available', 'parking', 'showers'})


def stable_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def normalize_value(field, value):
    """Return comparison value and exact qualifiers, or a diagnostic reason.

    Absent qualifiers stay absent; no default scope, audience or facility is
    inferred. Only bath_composition is a set. Free text uses exact equality.
    """
    qualifiers = {}
    reason = 'unsupported_or_ambiguous_value'
    normalized = None
    if field in ('hours', 'price_day_pass'):
        primary = 'hours' if field == 'hours' else 'amount'
        allowed = ({'hours', 'scope', 'facility'} if field == 'hours' else
                   {'amount', 'currency', 'ticket', 'scope', 'audience', 'duration_hours', 'facility'})
        if not isinstance(value, dict) or primary not in value or set(value) - allowed:
            return {'reason': reason, 'version': VERSION}
        qualifiers = {key: item for key, item in value.items() if key not in (primary, 'currency')}
        for key, item in qualifiers.items():
            if key == 'duration_hours':
                if not _number(item) or item <= 0:
                    return {'reason': reason, 'version': VERSION}
            elif not isinstance(item, str) or not item.strip():
                return {'reason': reason, 'version': VERSION}
        if field == 'hours':
            raw = value['hours']
            if raw == '24_hours':
                normalized = raw
            elif isinstance(raw, str):
                match = re.fullmatch(r'\s*(\d{1,2})\s*:\s*(\d{2})\s*[-~–—]\s*(\d{1,2})\s*:\s*(\d{2})\s*', raw)
                if match:
                    h1, m1, h2, m2 = map(int, match.groups())
                    if h1 < 24 and m1 < 60 and m2 < 60 and (h2 < 24 or (h2 == 24 and m2 == 0)):
                        if (h1, m1) != (h2 % 24, m2):
                            normalized = f'{h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}'
                        else:
                            reason = 'ambiguous_midnight_range'
        else:
            amount = value['amount']
            if value.get('currency', 'KRW') != 'KRW' or value.get('ticket') not in ('day_pass', 'drop_in'):
                return {'reason': reason, 'version': VERSION}
            # Integer amount is KRW in the established collector shape. Strings
            # must explicitly identify won; a bare legacy numeral is ambiguous.
            if type(amount) is int and 0 <= amount <= 100_000_000:
                normalized = amount
            elif isinstance(amount, str):
                match = re.fullmatch(r'\s*(?:KRW\s*([0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)|([0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)\s*(?:원|KRW))\s*', amount)
                if match:
                    parsed = int((match[1] or match[2]).replace(',', ''))
                    if parsed <= 100_000_000:
                        normalized = parsed
    elif (field == 'rapid_cold_plunge' or field in AVAILABILITY_FIELDS) and type(value) is bool:
        normalized = value
    elif field == 'tattoo_policy' and isinstance(value, str) and value in ('allowed', 'not_allowed', 'cover_required', 'conditional'):
        normalized = value
    elif field == 'foreigner_policy' and isinstance(value, str) and value in ('accepted', 'not_allowed', 'id_required', 'english_guide', 'english_unavailable'):
        normalized = value
    elif field == 'closures' and isinstance(value, str) and 0 < len(value.strip()) <= 240:
        normalized = value
    elif field in ('cold_bath_temp_c', 'sauna_temp_c'):
        if _number(value):
            normalized = value
        elif isinstance(value, list) and len(value) == 2 and all(_number(item) for item in value) and value[0] <= value[1]:
            normalized = value
    elif field == 'bath_composition' and isinstance(value, list) and value and all(isinstance(item, str) and 0 < len(item.strip()) <= 80 for item in value):
        normalized = sorted(set(value))
    if normalized is None:
        return {'reason': reason, 'version': VERSION}
    return {'reason': None, 'version': VERSION, 'qualifiers': qualifiers,
            'normalized_value': normalized, 'comparison_key': stable_json(normalized)}
