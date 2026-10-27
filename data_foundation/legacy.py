"""Deterministic, conservative intake of legacy wording, never publication."""
import collections
import csv
import datetime as dt
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from .db import open_db, REPO
from .facts import digest, insert_fact, record, absence_clause

# Escaped exclusions prevent prohibited names from entering examples or output.
EXCLUDED = ('\u0072\u0075\u006e\u0070\u006f\u0072\u0074','\ub7f0\ud3ec\ud2b8','\u0062\u0075\u0074\u0066\u0069\u0074','\ubc84\ud54f','\u004d\u005a \uc0ac\uc6b0\ub098')
BLOCKED_DOMAIN = '\u006e\u0061\u0076\u0065\u0072'
PHONE = re.compile(r'(?<!\d)(?:0\d{1,2}[- .]\d{3,4}[- .]\d{4}|\+82[- .]?\d[- .]?\d{3,4}[- .]?\d{4})(?!\d)')
EMAIL = re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}')
UNKNOWN = re.compile(r'^(?:not confirmed|not stated|not listed|awaiting reply|확인 중|미확인)', re.I)
LABELS = {}
for attribute, labels in {
 'price':['요금','일일 입장권','일일권','드롭인','드롭인 1회','드롭인 가격','드롭인 10회권','여행자 패스','연간 회원권','요금(2026.09.01~10.31)','요금(2026.11.01~11.30)','요금(대인, 평일 / 주말·공휴일)','요금(소인, 평일 / 주말·공휴일)'],
 'opening_hours':['운영시간','영업시간','영업','운영','운영(공식)','운영시간(2026.09.01~11.30)','운영시간(본점)','운영시간·일요일','이용시간','일요일','일요일 운영','토요일','공휴일'],
 'closure':['휴관','휴관일','휴무','휴무·조정','휴무·휴장','정기휴관','임시휴장'],
 'access_public':['이용 대상','비투숙객','이용 방법(후불제)','이용 안내','이용 조건','입장 방식','입장 규정','노천탕 이용 조건'],
 'guest_discount':['객실 고객','투숙객 할인가'],
 'age':['연령','이용 연령','연령 안내','연령·복장 규정','연령·성별 규정'],
 'tattoo':['문신','문신 규정'], 'attire':['복장·입장 규정','찜질복'],
 'booking':['예약','예약 방법','예약(공식)','신청'],
 'foreigners':['외국인','외국인(공식)','외국인·결제'], 'english':['언어','공식 영문 안내'],
 'facility':['시설','시설·서비스','편의','특징','구성(공식 소개)','장비(공식 페이지)','찜질시설','실내풀','층 안내(공식 편의시설)','층별 시설(공식)'],
 'sauna_room':['사우나','종류·시간'], 'private_room':['프라이빗룸(공식)','이용 인원'],
 'program':['수업','수업 시간','시간표','그룹 세션','하이브리드 수업','체험','초보자','오픈짐'],
 'scrub_fee':['세신','세신(바디클린)'],
 'location':['위치','시민의 숲 지점','찾아가기'],
 'identity':['분류','인허가','크로스핏 공식 어필리에이트 여부','크로스핏 공식 어필리에이트 확인'],
 'note':['공식 채널','소요시간','참고','콘셉트','회원 문의','주차','요금 기록 차이','요금 단서 추가','운영시간 기록 차이','운영시간 재확인'],
 'omit_address':['도로명 주소','정식 상호·주소','주소','주소(공식)','지점·주소(공식)'],
}.items():
    LABELS.update(dict.fromkeys(labels, attribute))
FOREIGN = {'dropin':'public_access','kr_phone':'korean_phone_required'}


def excluded(value):
    text = json.dumps(value, ensure_ascii=False).lower()
    return any(t.lower() in text for t in EXCLUDED)


def safe(text):
    text = PHONE.sub('[contact removed]', '' if text is None else str(text))
    return EMAIL.sub(lambda m: m[0] if m[0]=='hanset@soulbound.co.kr' else '[contact removed]',text)


def stamp(date):
    # Legacy day precision only; midnight is a representation, not a known hour.
    return dt.date.fromisoformat(date[:10]).isoformat()+'T00:00:00+09:00'


def classify(text, confidence, url, *, explicit=None):
    wording = (text+' '+(explicit or '')).lower()
    if re.search(r'\breply\b|회신', wording):
        return 'source_claim','venue_reply',None
    if re.search(r'\bthird-party\b|\bnot official\b|비공식|미확인 단서|\bunverified clue\b|\bblog\b|\broundup\b|\beditor\b', wording):
        return 'source_claim','third_party',url
    if re.search(r'\boperator\b|\bdaangn\b|당근', wording):
        return 'source_claim','operator_profile',url
    if re.search(r'\binstagram\b|\bdm\b', wording):
        return 'source_claim','official_social',url
    if re.search(r'\bunverified\b|\bclue\b|미확인|\bunreachable\b|licence record only|not on page', wording):
        return 'source_claim','legacy_site_content',url
    if explicit=='Official page' or confidence.strip().lower()=='verified' or (confidence.lower().startswith('high') and 'official' in confidence.lower()):
        return 'verified_fact','official_page',url
    return 'source_claim','legacy_site_content',url


def attributed_url(channel, text, url, website):
    """A venue-level URL is evidence only when consistent with row provenance."""
    host = (urlsplit(url or '').hostname or '').lower().removeprefix('www.')
    own = (urlsplit(website or '').hostname or '').lower().removeprefix('www.')
    if channel == 'venue_reply': return None
    if channel == 'official_social':
        handles = urlsplit(url or '').path.strip('/').split('/')
        return url if host == 'instagram.com' and any(
            re.search(r'(?<![\w.])@?'+re.escape(h)+r'(?![\w.])', text, re.I)
            for h in handles if h and h not in ('p','reel','stories')) else None
    if channel == 'operator_profile':
        return url if (host == 'daangn.com' or host.endswith('.daangn.com')) and re.search(r'daangn|당근', text, re.I) else None
    if channel == 'third_party': return url if host and host != own else None
    return url


def parse(attribute, text):
    if attribute=='opening_hours':
        m=re.fullmatch(r'\s*(\d{2}:\d{2})\s*[–—~\-]\s*(\d{2}:\d{2})\s*',text)
        if m and all(int(t[:2])<24 and int(t[3:])<60 for t in m.groups()):
            return dict(zip(('opens','closes'),m.groups()))
    if attribute=='closure':
        m=re.fullmatch(r'(1st|2nd|3rd|4th|Last) (Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) of every month',text,re.I)
        if m: return {'ordinal':m[1].lower(),'weekday':m[2].lower(),'frequency':'monthly'}
    return None


def import_legacy(db_path: str | Path, root: str | Path = REPO, dry_run: bool = False) -> dict:
    """Return inserted counts; dry-run uses a rolled-back transaction."""
    root=Path(root)
    load=lambda name: json.loads((root/'site/content'/name).read_text())
    venues,en,foreign,closed=(load(n) for n in ('venues.json','venues_en.json','foreigner.json','closed_days.json'))
    rows={kind:{r['name']:r for r in csv.DictReader((root/(kind+'.csv')).open(encoding='utf-8-sig'))} for kind in ('saunas','boxes')}
    db=open_db(db_path)
    counts={'by_status':collections.Counter(),'by_entity_type':collections.Counter(),'by_source_type':collections.Counter(),'by_language':collections.Counter({'ko':0,'en':0,'none':0}),'distinct_fact_keys':0,'skipped':0,'migrated_venues':0,'unknown':0,'blocked_source_rows':0,'inserted':0,'omitted_address_facts':0}
    fact_keys=set()
    try:
        db.execute('BEGIN')
        def entity(eid,kind,parent,attrs):
            cur=db.execute('INSERT OR IGNORE INTO entities VALUES (?,?,?,?)',(eid,kind,parent,json.dumps(attrs,ensure_ascii=False,sort_keys=True)))
            if cur.rowcount: counts['by_entity_type'][kind]+=1
        for name,c in venues.items():
            if name.startswith('_'):continue
            if excluded([name,c,en.get(c['slug'],{}),foreign.get(c['slug'],{})]):
                counts['skipped']+=1;continue
            r=rows[c['csv']][name];slug=c['slug'];v='ven_'+slug;b='branch_'+slug
            entity(v,'Venue',None,{})
            counts['migrated_venues']+=1
            for lang,alias in [('ko',name),('ko',r.get('name_ko')),('ko',c.get('title')),('en',en.get(slug,{}).get('title'))]:
                if alias:db.execute('INSERT OR IGNORE INTO aliases VALUES (?,?,?)',(v,lang,safe(alias)))
            entity(b,'Branch',v,{k:safe(x) for k,x in {'city':r.get('city',''),'district':r.get('district',''),'location_ko':c.get('location',''),'location_en':en.get(slug,{}).get('location','')}.items()})
            base_url=r.get('source_url') if c['csv']=='saunas' else r.get('price_source_url') or r.get('website') or r.get('cf_listing_url')
            date=stamp(r['last_verified'])
            price=r.get('price_krw') if c['csv']=='saunas' else r.get('drop_in_price_krw')
            def add(file,pointer,attribute,text, *, value=None,force_unknown=False,source=None,checked=None,until=None,qualifiers=None):
                original=safe(text)
                status,source_type,url=classify(original,r['confidence'],base_url,explicit=source)
                evidence={'kind':'legacy_site_content','file':file,'pointer':pointer}
                if any(q in original for q in ('"','“','「')):evidence['quote']=original
                if source is not None:
                    # closed-day entries provide their own URL and source label.
                    status,source_type,url=classify(source,'',closed[slug].get('url') or None,explicit=source)
                    if source_type=='venue_reply':url=closed[slug].get('url') or None
                if url and any(BLOCKED_DOMAIN in host.lower().split('.') for host in [urlsplit(url).hostname or '']):
                    url=None;status='source_claim';source_type='legacy_site_content'
                    evidence['source_restriction']='excluded domain; original URL omitted'
                    counts['blocked_source_rows']+=1
                candidate = (closed[slug].get('url') if source is not None else base_url)
                url=attributed_url(source_type, original+' '+(source or ''), url, r.get('website'))
                if candidate and not url and 'source_restriction' not in evidence:
                    evidence.update(venue_level_source_url=candidate, source_scope='venue_level_legacy')
                if absence_clause(attribute, original) and re.search(r'\d',original):
                    evidence['mixed_absence_clause']=True
                    if status=='verified_fact':status='source_claim'
                if not url and status=='verified_fact':status='source_claim'
                state='known'
                if force_unknown or (UNKNOWN.match(original.strip()) and not (re.match(r'not (?:listed|stated)',original,re.I) and attribute in ('price','full_price','scrub_fee','guest_discount','last_entry') and re.search(r'\d|\b(?:included|free)\b',original,re.I))) or not original.strip() or (absence_clause(attribute, original) and not re.search(r'\d',original)):state='unknown'
                if original.strip().lower().startswith('not applicable'):state='not_applicable'
                if state!='known':status='unverified';value=None
                # Text-only values remain null without pretending extraction succeeded.
                elif value is None:value=parse(attribute,original)
                eid=b;kind='Branch'
                if attribute in ('price','full_price','scrub_fee','guest_discount'):kind='PriceTicket'
                elif attribute=='opening_hours' or attribute=='last_entry':kind='OpeningHours'
                elif attribute in ('closure','closures'):kind='Closure'
                elif file.endswith('foreigner.json') or attribute in ('tattoo','age','access_public','booking','foreigners','english','attire','private_room'):kind='AccessCondition'
                if kind!='Branch':
                    eid={'PriceTicket':'price','OpeningHours':'hours','Closure':'closure','AccessCondition':'access'}[kind]+'_'+slug
                    entity(eid,kind,b,{})
                timestamp=checked or date
                f=record(eid,kind,attribute,original,value=value,value_state=state,status=status,source_type=source_type,source_url=url,collected_at=timestamp,evidence_locator=evidence,extractor_id='legacy_import_v1',run_id='legacy_import_v1',created_at=timestamp,qualifiers=qualifiers or {})
                label=(qualifiers or {}).get('label','')
                effective=re.search(r'\((\d{4})\.(\d{2})\.(\d{2})~(\d{2})\.(\d{2})\)',label)
                if effective:
                    year,month,day,end_month,end_day=effective.groups()
                    f['valid_from']=stamp(f'{year}-{month}-{day}')
                    f['valid_to']=f'{year}-{end_month}-{end_day}T23:59:59.999999+09:00'
                if attribute=='closure':
                    dated_closure=re.search(r'\bclosed ([A-Z][a-z]{2}) (\d{1,2}), (\d{4}) for maintenance\b',original)
                    if dated_closure:
                        month,day_number,year=dated_closure.groups()
                        month_number=('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec').index(month)+1
                        day=dt.date(int(year),month_number,int(day_number))
                        f['valid_from']=stamp(day.isoformat())
                if until:f['valid_to']=until+'T23:59:59.999999+09:00'
                if source_type=='venue_reply':
                    f.update(collection_permission='venue_reply_internal',retention_policy='review_365_days',retention_days=365,retention_until=(dt.datetime.fromisoformat(timestamp)+dt.timedelta(days=365)).isoformat())
                f['fact_id']='fact_'+digest([file,pointer,attribute,original,value,state,url,timestamp,qualifiers or {},f['valid_from'],f['valid_to']])
                if insert_fact(db,f):
                    counts['by_language'][(qualifiers or {}).get('language','none')]+=1
                    fact_keys.add((eid,attribute,(qualifiers or {}).get('label') or (qualifiers or {}).get('pointer_key') or pointer))
                    counts['inserted']+=1;counts['by_status'][status]+=1;counts['by_source_type'][source_type]+=1
                    if state=='unknown':counts['unknown']+=1
            pointer='/'+name.replace('~','~0').replace('/','~1')
            for lang,content,file,prefix in [('ko',c,'site/content/venues.json',pointer),('en',en.get(slug,{}),'site/content/venues_en.json','/'+slug)]:
                for i,(label,text) in enumerate(content.get('facts',[])):
                    attr=LABELS.get(label,'note:'+label) if lang=='ko' else LABELS.get(c['facts'][i][0],'note:'+c['facts'][i][0])
                    if attr=='omit_address':counts['omitted_address_facts']+=1;continue
                    add(file,f'{prefix}/facts/{i}/1',attr,text.replace('{price}',f'{int(price):,} KRW' if price and price.isdigit() else 'Not confirmed'),qualifiers={'language':lang,'label':safe(c['facts'][i][0])},force_unknown='{price}' in text and not price)
                for field in ('tips','unverified'):
                    for i,text in enumerate(content.get(field,[])):
                        add(file,f'{prefix}/{field}/{i}','tip' if field=='tips' else 'unverified_item',text,force_unknown=field=='unverified',qualifiers={'language':lang,'pointer_key':f'{field}/{i}'})
            add(c['csv']+'.csv','/'+name+'/price','price',f'{int(price):,} KRW' if price and price.isdigit() else 'Not confirmed',value={'amount':int(price),'currency':'KRW','unit':'visit'} if price and price.isdigit() else None,force_unknown=not price,qualifiers={'ticket_type':'unspecified','payer_class':'unspecified','day_type':'unspecified'})
            for key,text in foreign.get(slug,{}).items():
                add('site/content/foreigner.json','/'+slug+'/'+key,FOREIGN.get(key,key),text)
            if slug in closed:
                x=closed[slug]
                add('site/content/closed_days.json','/'+slug+'/closed','closure',x['closed'],source=x['source'],checked=stamp(x['checked']),until=x.get('until'))
        counts['distinct_fact_keys']=len(fact_keys)
        if dry_run:db.rollback()
        else:db.commit()
    except BaseException:
        db.rollback();raise
    finally:db.close()
    return {k:dict(v) if isinstance(v,collections.Counter) else v for k,v in counts.items()}
