"""Venue ownership boundary shared by reverse discovery and queue repair."""
import json
import re
from urllib.parse import urlsplit, unquote
from .urls import canonicalize, registrable_domain

FOCUSED_STRATEGIES=('site_links','lang_counterpart','attachments','images')

def portal_domains(profile=None):
    """Directory and aggregator domains are never a venue's own site."""
    if profile is None:
        from ..config import load_profile
        profile=load_profile()
    hosts={urlsplit(d.get('url','')).hostname or '' for d in profile.get('directories',[])}
    hosts|={d for d in profile.get('aggregator_domains',[]) if '*' not in d}
    return {registrable_domain('https://'+h) for h in hosts if h}

def allows_followup(venue, url, strategy, portals=None):
    if venue is None: return False
    portals=portal_domains() if portals is None else portals
    venue=dict(venue)
    domains=venue.get('domains') or []
    if isinstance(domains,str):
        try: domains=json.loads(domains)
        except ValueError: domains=[]
    if not isinstance(domains,list): domains=[]
    own={registrable_domain(d) for d in domains if isinstance(d,str) and d}
    if venue.get('website'): own.add(registrable_domain(venue['website']))
    own-=set(portals)
    try: url=canonicalize(url)
    except (ValueError,UnicodeError): return False
    if registrable_domain(url) not in own: return False
    path=unquote(urlsplit(url).path).strip('/').casefold()
    home=r'(?:home|index|main)(?:\.(?:html?|php|aspx?))?'
    language=r'(?:[a-z]{2}(?:[-_][a-z]{2})?|english|korean|chinese|japanese)'
    return strategy!='lang_counterpart' or bool(path and not re.fullmatch(fr'(?:{home}|{language}(?:/{home})?)',path))
