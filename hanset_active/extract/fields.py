"""Explicit bilingual field rules. Offsets refer to sanitized input text."""
import re
from ..util.pii import strip_pii
VERSION = '1.0.0'
NAME = 'fields'

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
        def matches(pattern):
            return re.finditer(pattern,s,re.I)
        class Offset:
            def __init__(self,m): self.m=m
            def span(self): return (block.start()+self.m.start(),block.start()+self.m.end())
        def add(field,value,m,unit=None,ambiguous=False): emit(field,value,Offset(m),unit,ambiguous)
        money=list(matches(r'₩\s*[\d,]+|[\d,]+\s*KRW|(?:\d+만(?:\d+천|\d+)?|\d+천|만|[\d,]+)\s*원'))
        price_context=text[max(0,block.start()-60):block.end()]
        if re.search(r'요금|이용료|입장료|1회|일일|drop.?in|day pass|visit|price',price_context,re.I):
            for m in money:
                value={'amount':amount(m[0]),'scope':_scope(s),'ticket':'drop_in' if re.search(r'drop.?in|드롭인',s,re.I) else 'day_pass'}
                if re.search(r'성인|adult',s,re.I): value['audience']='adult'
                elif re.search(r'소인|어린이|child',s,re.I): value['audience']='child'
                duration=re.search(r'(\d+)\s*(?:시간|hours?)',s,re.I)
                if duration: value['duration_hours']=int(duration[1])
                add('price_day_pass',value,m,'KRW',len(money)>1)
        for m in matches(r'\b\d{1,2}:\d{2}\s*[~–—-]\s*\d{1,2}:\d{2}|24시간|24\s*hours|연중무휴'):
            times=re.findall(r'(\d{1,2}):(\d{2})',m[0])
            hours='-'.join(f'{int(h):02d}:{minute}' for h,minute in times) if times else '24_hours' if re.search(r'24',m[0]) else 'open_every_day'
            add('hours',{'hours':hours,'scope':_scope(s)},m)
        for m in matches(r'(?:매주|매월)[^;]{0,50}(?:휴무|휴관)|closed on [A-Za-z]+|\d{4}[-/]\d{2}[-/]\d{2}\s*[~–]\s*\d{4}[-/]\d{2}[-/]\d{2}[^;]{0,30}(?:closed|휴무|휴관)'):
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
        for m in matches(r'건식|습식|온탕|냉탕|열탕|스팀|한증막|아이스배스|dry sauna|steam|hot bath|cold bath|ice bath'):
            if not re.search(r'^\s*(?:없|미운영|not available)',s[m.end():],re.I) and not re.search(r'\bno\s*$',s[:m.start()],re.I): baths.append({'dry sauna':'건식','steam':'스팀','hot bath':'온탕','cold bath':'냉탕','ice bath':'아이스배스'}.get(m[0].lower(),m[0].lower()))
        if baths: add('bath_composition',sorted(set(baths)),next(matches(r'건식|습식|온탕|냉탕|열탕|스팀|한증막|아이스배스|dry sauna|steam|hot bath|cold bath|ice bath')))
        for m in matches(r'문신|tattoos?'):
            tail=s[m.end():m.end()+70]
            if re.search(r'커버.*(?:불필요|필요 없)|cover.*not required',tail,re.I): value='allowed'
            elif re.search(r'커버|스티커|must be covered|cover required',tail,re.I): value='cover_required'
            elif re.search(r'금지하지 않|불가하지 않|not prohibited|not forbidden',tail,re.I): value='allowed'
            elif re.search(r'허용하지 않|가능하지 않|불가|금지|not allowed|prohibited',tail,re.I): value='not_allowed'
            elif re.search(r'커버.*(?:불필요|필요 없)|cover.*not required',tail,re.I): value='allowed'
            elif re.search(r'가능|허용|allowed',tail,re.I): value='allowed'
            elif re.search(r'조건|conditional',tail,re.I): value='conditional'
            else: continue
            add('tattoo_policy',value,m)
        for m in matches(r'외국인|foreigners?|tourists?|English|passport|여권|영어|\bID\b'):
            if re.search(r'허용하지 않|가능하지 않|불가|금지|not allowed|not accepted',s,re.I): value='not_allowed'
            elif re.search(r'여권|passport|\bID\b',s,re.I) and re.search(r'필요|지참|required|bring',s,re.I): value='id_required'
            elif re.search(r'English|영어',m[0],re.I) and re.search(r'미제공|없|not available|no guide',s,re.I): value='english_unavailable'
            elif re.search(r'English|영어',m[0],re.I) and re.search(r'안내|guide|available',s,re.I): value='english_guide'
            elif re.search(r'가능|환영|허용|accepted|welcome|allowed',s,re.I): value='accepted'
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
            rows.extend(extract(' | '.join(cells),locator={'selector':f'table:nth-of-type({ti+1}) tr:nth-of-type({ri+1})'}))
    return rows
