"""Explicit bilingual field rules. Offsets refer to sanitized input text."""
import re
from ..util.pii import strip_pii
VERSION = '1.2.0'
NAME = 'fields'

# A sentence that explicitly says a fact is unconfirmed/unlisted yields nothing at all:
# never a value, never an ambiguous review item. Softer hedges stay per-match 'ambiguous'.
_UNCONFIRMED = re.compile(
    r'not confirmed|unconfirmed|\bunknown\b|미확인|확인\s*안\s*됨|확인안됨|확인\s*되지\s*않|확인되지\s*않|알\s*수\s*없'
    r'|확인\s*중|확인\s*불가|확인\s*안\s*된|not\s+yet\s+confirmed|미정|미공개|미표시|\bnot listed\b|\bnot shown\b|\bnot stated\b|\bnot announced\b|\bnot found\b'
    r'|\bnot public\b|\bnot applicable\b|\bnot read\b|\bno price\b|\bprice not\b', re.I)

# Canonical (Korean) tokens so ontology mapping works for English mentions too.
_BATH_TOKEN = {'dry sauna': '건식', 'wet sauna': '습식', 'steam': '스팀', 'hot bath': '온탕',
               'cold bath': '냉탕', 'ice bath': '아이스배스', 'cold plunge': '냉탕'}

def amount(text):
    text = re.sub(r'[₩,\s]|KRW|원', '', text, flags=re.I)
    total = 0
    for unit, scale in [('만',10000), ('천',1000)]:
        if unit in text:
            prefix, text = text.split(unit,1)
            total += int(prefix or 1)*scale
    return total + int(text or 0)

def _scope(text):
    if re.search(r'주말|weekends?',text,re.I): return 'weekend'
    if re.search(r'평일|weekdays?',text,re.I): return 'weekday'
    return 'unspecified'

def extract(text: str, *, locator=None, ocr_confidence=None) -> list[dict]:
    text = strip_pii(text)
    results = []
    def emit(field, value, match, unit=None, ambiguous=False):
        start,end = match.span()
        line_start=text.rfind('\n',0,start)+1
        line_end=text.find('\n',end)
        evidence_start=max(line_start,start-50)
        if field=='price_day_pass': evidence_start=max(0,start-60)
        evidence = text[evidence_start:min(line_end if line_end>=0 else len(text),end+80)]
        uncertain = bool(re.search(r'확인 중|예정|미정|아마|maybe|might|uncertain|not confirmed', evidence, re.I))
        results.append(dict(field=field,value=value,unit=unit,quote=evidence[:240],
            locator=locator or {'char_offsets':[start,end]},rule_id=f'{field}.explicit.v1',
            rule_version=VERSION,extractor_version=VERSION,
            ambiguous=ambiguous or uncertain or (ocr_confidence is not None and ocr_confidence<60)))
    # Work per line/sentence to keep scopes and negations local.
    for block in re.finditer(r'(?:[^\n.!?]|(?<=\d)\.(?=\d))+',text):
        s=block[0]
        if _UNCONFIRMED.search(s):
            continue  # "Not confirmed"/"미확인" text must never produce a value
        def matches(pattern):
            return re.finditer(pattern,s,re.I)
        class Offset:
            def __init__(self,m): self.m=m
            def span(self): return (block.start()+self.m.start(),block.start()+self.m.end())
        def add(field,value,m,unit=None,ambiguous=False): emit(field,value,Offset(m),unit,ambiguous)
        money=list(matches(r'₩\s*[\d,]+|[\d,]+\s*KRW|(?:\d+만(?:\d+천|\d+)?|\d+천|만|[\d,]+)\s*원'))
        price_context=text[max(0,block.start()-60):block.end()]
        if re.search(r'요금|이용료|입장료|1회|일일|이용권|drop.?in|day pass|single entry|admission|visit|price',price_context,re.I):
            for m in money:
                value={'amount':amount(m[0]),'scope':_scope(s),'ticket':'drop_in' if re.search(r'drop.?in|드롭인',s,re.I) else 'day_pass'}
                if re.search(r'성인|adult',s,re.I): value['audience']='adult'
                elif re.search(r'소인|어린이|child',s,re.I): value['audience']='child'
                duration=re.search(r'(\d+)\s*(?:시간|hours?)',s,re.I)
                if duration: value['duration_hours']=int(duration[1])
                add('price_day_pass',value,m,'KRW',len(money)>1)
        for m in matches(r'(?:이용시간|운영시간|영업시간)\s*[:：]?\s*(\d{1,2})시(?:\s*(\d{1,2})분)?\s*[~–-]\s*(\d{1,2})시(?:\s*(\d{1,2})분)?'):
            if int(m[1])<24 and int(m[3])<=24 and int(m[2] or 0)<60 and int(m[4] or 0)<60:
                add('hours',{'hours':f'{int(m[1]):02d}:{int(m[2] or 0):02d}-{int(m[3]):02d}:{int(m[4] or 0):02d}','scope':_scope(s)},m)
        for m in matches(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*(?:[~–-]|to)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b'):
            if 1<=int(m[1])<=12 and 1<=int(m[4])<=12 and int(m[2] or 0)<60 and int(m[5] or 0)<60:
                start=int(m[1])%12+(12 if m[3].lower()=='pm' else 0)
                end=int(m[4])%12+(12 if m[6].lower()=='pm' else 0)
                add('hours',{'hours':f'{start:02d}:{int(m[2] or 0):02d}-{end:02d}:{int(m[5] or 0):02d}','scope':_scope(s)},m)
        for m in matches(r'\b\d{1,2}:\d{2}\s*[~–—-]\s*\d{1,2}:\d{2}|24시간|24\s*hours|연중무휴'):
            times=re.findall(r'(\d{1,2}):(\d{2})',m[0])
            hours='-'.join(f'{int(h):02d}:{minute}' for h,minute in times) if times else '24_hours' if re.search(r'24',m[0]) else 'open_every_day'
            add('hours',{'hours':hours,'scope':_scope(s)},m)
        for m in matches(r'(?:휴무|휴관)\s*[:：]\s*(?:매주|매월)[^;]{0,40}|(?:매주|매월)[^;]{0,50}(?:휴무|휴관)|closed (?:on |every )?(?:Mondays?|Tuesdays?|Wednesdays?|Thursdays?|Fridays?|Saturdays?|Sundays?)|\d{4}[-/]\d{2}[-/]\d{2}\s*[~–]\s*\d{4}[-/]\d{2}[-/]\d{2}[^;]{0,30}(?:closed|휴무|휴관)'):
            add('closures',m[0].strip(),m)
        for m in matches(r'(?:섭씨\s*)?\d+(?:\.\d+)?(?:\s*[~–-]\s*\d+(?:\.\d+)?)?\s*(?:℃|°\s*C|도)'):
            before=s[:m.start()]
            cues=list(re.finditer(r'냉탕|냉수|cold plunge|ice bath|아이스|사우나|핀란드식|건식|sauna',before,re.I))
            if cues:
                cold=bool(re.search(r'냉탕|냉수|cold plunge|ice bath|아이스',cues[-1][0],re.I))
                nums=[float(n) for n in re.findall(r'\d+(?:\.\d+)?',m[0])]
                add('cold_bath_temp_c' if cold else 'sauna_temp_c',nums[0] if len(nums)==1 else nums,m,'C',bool(re.search(r'아님|아니|not confirmed|not actually',s,re.I)))
        for m in matches(r'급냉탕|rapid cold plunge'):
            tail=re.split(r'[,;]',s[m.end():m.end()+30])[0]
            prefix=s[max(0,m.start()-4):m.start()]
            if re.search(r'없|불가|미운영|not available|absent',tail,re.I) or re.search(r'\bno\s*$',prefix,re.I): value=False
            elif re.search(r'있|운영|제공|구비|available|present|yes',tail,re.I): value=True
            else: continue
            add('rapid_cold_plunge',value,m)
        baths=[]
        def bath(tok,m):
            if not re.search(r'^\s*(?:없|미운영|not available)',s[m.end():],re.I) and not re.search(r'\bno\s*$',s[:m.start()],re.I):
                baths.append((tok,m))
        for m in matches(r'건식|습식|온탕|냉탕|열탕|스팀|한증막|아이스배스|dry sauna|wet sauna|steam|hot bath|cold bath|ice bath|cold plunge'):
            bath(_BATH_TOKEN.get(m[0].lower(),m[0].lower()),m)
        for m in matches(r'\b(?:dry|wet)\s*(?:,|and|&|/|\+|·)\s*(?:dry|wet)\s*saunas?\b'):  # "dry and wet saunas"
            bath('건식',m); bath('습식',m)
        for m in matches(r'\b(?:(?:very[- ])?hot|cold)\s*(?:/|&|,|or|and)\s*(?:(?:very[- ])?hot|cold)(?:\s*(?:/|&|,|or|and)\s*(?:(?:very[- ])?hot|cold))*\s*baths?\b'):
            if re.search(r'cold',m[0],re.I): bath('냉탕',m)  # "hot/cold baths": the cold side is explicit
        if re.search(r'\b(?:baths?|tubs?|pools?)\b|탕',s,re.I):  # "Four hot baths, one cold" -- cold named next to a bath noun
            for m in matches(r'\b(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+cold\b(?!-?\s*showers?)'):
                bath('냉탕',m)
        if baths: add('bath_composition',sorted({t for t,_ in baths}),baths[0][1])
        for m in matches(r'문신|tattoos?'):
            tail=re.split(r'[,;]',s[m.end():m.end()+160])[0]
            if re.search(r'커버.*(?:불필요|필요 없)|cover.*not required',tail,re.I): value='allowed'
            elif re.search(r'금지하지 않|불가하지 않|not prohibited|not forbidden|not banned',tail,re.I): value='allowed'
            elif re.search(r'not allowed|not admitted|not permitted|prohibited|forbidden|banned|출입금지|입장불가',tail,re.I) and not re.search(r'unless|except|제외|경우|하면',tail,re.I): value='not_allowed'
            elif re.search(r'커버|스티커|패치|patch|rash.?guard|래쉬가드|레이시가드|covered',tail,re.I) and re.search(r'필요|필수|착용|부착|가려|가린|must|needs?|required|only when|unless|covered',tail,re.I): value='cover_required'
            elif re.search(r'may be restrict|restrictions?\s+may\s+apply|제한될\s*수|거절될\s*수',tail,re.I): value='conditional'
            elif re.search(r'허용하지 않|가능하지 않|불가|금지|제한|거절|not allowed|not admitted|not permitted|prohibited|forbidden|banned|restricted',tail,re.I): value='not_allowed'
            elif re.search(r'가능|허용|allowed|permitted|admitted|welcome',tail,re.I): value='allowed'
            elif re.search(r'조건|conditional',tail,re.I): value='conditional'
            else: continue
            add('tattoo_policy',value,m)
        for m in matches(r'외국인|foreign visitors?|foreigners?|tourists?|English|passport|여권|영어|\bID\b'):
            if re.search(r'허용하지 않|가능하지 않|불가|금지|not allowed|not accepted',s,re.I): value='not_allowed'
            elif re.search(r'여권|passport|\bID\b',s,re.I) and re.search(r'필요|지참|required|bring',s,re.I): value='id_required'
            elif re.search(r'English|영어',m[0],re.I) and re.search(r'미제공|없|not available|no guide',s,re.I): value='english_unavailable'
            elif re.search(r'English|영어',m[0],re.I) and re.search(r'안내|guide|available',s,re.I): value='english_guide'
            elif re.search(r'가능|환영|허용|accepted|welcome|allowed|permitted',s,re.I): value='accepted'
            else: continue
            add('foreigner_policy',value,m)
    unique={ (r['field'],repr(r['value']),repr(r['locator'])):r for r in results }
    return list(unique.values())

def extract_document(document: dict) -> list[dict]:
    """Adapt Stage A HTML/PDF/OCR dictionaries without losing evidence locations."""
    if document.get('extractor')=='pdf':
        return [row for page in document.get('pages',[]) for row in extract(page['text'],locator={'pdf_page':page['page']})]
    if document.get('extractor')=='ocr':
        return extract(document.get('text',''),locator={'ocr_box':document.get('boxes',[])},ocr_confidence=document.get('mean_confidence',0))
    rows=extract(document.get('text',''))
    for ti,table in enumerate(document.get('structured',{}).get('tables',[])):
        for ri,cells in enumerate(table):
            header=' | '.join(table[0]) if ri and not re.search(r'\d', ' '.join(table[0])) else ''
            rows.extend(extract(header+'\n'+' | '.join(cells),locator={'selector':f'table:nth-of-type({ti+1}) tr:nth-of-type({ri+1})'}))
    return rows
