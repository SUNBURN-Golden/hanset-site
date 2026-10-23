"""Optional operator-approved field rules; absent fields remain unknown.

Rules operate only on the privacy-filtered minimal excerpt, never on a full page.
No model output can configure these rules or verify facts.
"""
import re
from .config import policy_for, Rejected


def facts_from_excerpt(cfg, url, data, now=None, require_access=True):
    p = policy_for(cfg, url, now=now, required=require_access)
    facts = list(data.get('facts') or [])
    for rule in p.get('field_rules', [])[:60]:
        try:
            match = re.search(rule['pattern'], data['excerpt'])
        except (re.error, KeyError):
            raise Rejected('invalid_extraction_rule') from None
        if match:
            value = match.groupdict().get('value', match[0])
            facts.append({'field': rule['field'], 'value': value, 'quote': match[0][:400],
                          'locator': data['locator'], 'scope': rule.get('scope', {}),
                          **({'key': rule['key']} if 'key' in rule else {})})
    return facts
