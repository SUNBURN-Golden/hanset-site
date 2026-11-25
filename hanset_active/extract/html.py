"""Main text, structured evidence and classified links from static HTML."""
import json
import re
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from ..util.pii import strip_pii
from ..util.urls import is_denied, registrable_domain
NAME = 'html'
VERSION = '1.1.0'

def extract(html: str | bytes, url: str = '') -> dict:
    soup = BeautifulSoup(strip_pii(html), 'lxml')
    structured = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            structured.append(json.loads(node.string or node.get_text()))
        except (ValueError, TypeError):
            pass
    title = soup.title.get_text(' ',strip=True) if soup.title else ''
    links=[]
    for a in soup.select('a[href], link[rel], img[src]'):
        try:
            href = urljoin(url, a.get('href') or a.get('src',''))
            if urlsplit(href).scheme not in ('http','https'):
                continue
            urlsplit(href).hostname
        except ValueError:
            continue  # malformed href (e.g. bad IPv6 literal): skip the link, never fail the page
        anchor = a.get_text(' ',strip=True) or a.get('alt','')
        domain = registrable_domain(href)
        kind = 'internal' if urlsplit(href).hostname == urlsplit(url).hostname else 'external'
        if domain=='instagr.am':
            kind='sns'
        elif is_denied(href) and 'instagram' not in domain:
            kind='excluded_link'
        elif domain in ('instagram.com','facebook.com','youtube.com','youtu.be','kakao.com','linktr.ee'):
            kind='sns'
        elif domain in ('booking.com','agoda.com','catchtable.co.kr','tablecheck.com'):
            kind='booking'
        elif urlsplit(href).path.lower().endswith('.pdf'):
            kind='attachment'
        elif a.name == 'img':
            kind='image_notice' if re.search(r'price|요금|이용|시간표|timetable|menu|rate',href+' '+anchor,re.I) else 'image'
        elif 'next' in a.get('rel',[]) or re.search(r'^(next|다음)$',anchor,re.I):
            kind='next_page'
        elif 'sitemap' in href.lower():
            kind='sitemap'
        links.append(dict(url=strip_pii(href),kind=kind,text=anchor,hreflang=a.get('hreflang')))
    ugc = re.compile(r'tripadvisor|review|comment|reply|trip-lst|guestbook', re.I)
    for node in list(soup.find_all(True)):
        if node.attrs is not None and ugc.search(' '.join(node.get('class', [])) + ' ' + (node.get('id') or '')):
            node.decompose()
    tables = [[[c.get_text(' ',strip=True) for c in row.select('th,td')] for row in table.select('tr')] for table in soup.select('table')]
    headings=[h.get_text(' ',strip=True) for h in soup.select('h1,h2,h3,h4,h5,h6')]
    for node in soup.select('script,style,nav,footer,header,noscript,template'):
        node.decompose()
    main = soup.select_one('main,article,[role="main"]') or soup.body or soup
    text = main.get_text('\n',strip=True)
    dates = re.findall(r'\b\d{4}[-./]\d{1,2}[-./]\d{1,2}\b|\d{4}년\s*\d{1,2}월\s*\d{1,2}일',text)
    amounts = re.findall(r'(?:₩\s*[\d,]+|[\d,]+\s*(?:원|KRW)|[\d만천]+원)',text,re.I)
    return dict(extractor=NAME,extractor_version=VERSION,title=title,text=text,structured={'jsonld':structured,'tables':tables,'headings':headings},mentions=[{'name':o['name'],'kind':o.get('@type','entity')} for item in structured for o in (item if isinstance(item,list) else [item]) if isinstance(o,dict) and 'name' in o],links=links,dates=dates,amounts=amounts,lang='ko' if re.search('[가-힣]',text) else 'en')
