import pytest
from hanset_active.extract.fields import extract, amount, VERSION

@pytest.mark.parametrize('text,field,value',[
 ('Day pass ₩25,000','price_day_pass',25000),('1회 이용료 2만5천원','price_day_pass',25000),('일일 요금 만원','price_day_pass',10000),
 ('사우나 80~100℃','sauna_temp_c',[80.,100.]),('냉탕 10°C','cold_bath_temp_c',10.),('건식 섭씨 90도','sauna_temp_c',90.),
 ('급냉탕 있음','rapid_cold_plunge',True),('급냉탕 없음','rapid_cold_plunge',False),('rapid cold plunge available','rapid_cold_plunge',True),
 ('문신 불가','tattoo_policy','not_allowed'),('tattoos not allowed','tattoo_policy','not_allowed'),('tattoos must be covered','tattoo_policy','cover_required'),('문신 허용','tattoo_policy','allowed'),
 ('외국인 이용 가능','foreigner_policy','accepted'),('foreigners not accepted','foreigner_policy','not_allowed'),('Passport required','foreigner_policy','id_required'),('English guide available','foreigner_policy','english_guide')])
def test_rules(text,field,value):
    rows=[r for r in extract(text) if r['field']==field]
    assert rows
    assert (rows[0]['value']['amount'] if field=='price_day_pass' else rows[0]['value'])==value
    assert rows[0]['quote'] and len(rows[0]['quote'])<=240 and rows[0]['locator']['char_offsets']
    assert rows[0]['rule_id'] and rows[0]['rule_version']==VERSION

@pytest.mark.parametrize('text,field',[('평일 09:00 ~ 22:00','hours'),('24시간','hours'),('연중무휴','hours'),('매주 화요일 휴무','closures'),('매월 첫째 주 월요일 휴무','closures'),('closed on Mondays','closures'),('2026-11-01 ~ 2026-11-03 closed','closures'),('건식 습식 온탕 냉탕 스팀','bath_composition')])
def test_scoped_rules(text,field):
    assert any(r['field']==field for r in extract(text))

def test_ambiguity_and_negation():
    assert not extract('Nothing is known')
    assert not any(r['field']=='rapid_cold_plunge' for r in extract('급냉탕 확인 중'))
    assert all(r['ambiguous'] for r in extract('Day pass 25000 KRW or 30000 KRW'))
    assert extract('tattoos allowed',ocr_confidence=50)[0]['ambiguous']
    assert not any(r['field']=='bath_composition' for r in extract('no steam'))
    assert [r for r in extract('사우나 90°C 냉탕 8°C') if r['field'].endswith('temp_c')][1]['field']=='cold_bath_temp_c'

def test_locator():
    assert extract('Day pass 25000 KRW',locator={'pdf_page':2})[0]['locator']=={'pdf_page':2}

def test_document_adapters_and_table_lines():
    from hanset_active.extract.fields import extract_document
    assert extract('일일\n25,000원')[0]['value']['amount']==25000
    assert extract('사우나 90.5°C')[0]['value']==90.5
    assert extract('tattoos not prohibited')[0]['value']=='allowed'
    assert extract('문신 커버 필요 없음')[0]['value']=='allowed'
    pdf=extract_document({'extractor':'pdf','pages':[{'page':3,'text':'Day pass 25000 KRW'}]})
    assert pdf[0]['locator']=={'pdf_page':3}
    ocr=extract_document({'extractor':'ocr','text':'Day pass 25000 KRW','mean_confidence':50,'boxes':[{'left':0,'top':0}]})
    assert ocr[0]['ambiguous'] and ocr[0]['locator']['ocr_box']
    table=extract_document({'text':'','structured':{'tables':[[['Day pass','25000 KRW']]]}})
    assert table[0]['locator']['selector'].startswith('table:')

def test_semantic_values_do_not_include_evidence_wording():
    ko=next(r for r in extract('일일 이용료 25,000원') if r['field']=='price_day_pass')
    en=extract('Day pass 25000 KRW')[0]
    assert ko['value']==en['value']
    assert extract('평일 9:00 ~ 22:00')[0]['value']==extract('Weekdays 09:00-22:00')[0]['value']

def test_negation_stays_with_its_subject():
    rows=extract('급냉탕 있음, 온탕 없음')
    assert next(r['value'] for r in rows if r['field']=='rapid_cold_plunge') is True
    rows=extract('no rapid cold plunge')
    assert next(r['value'] for r in rows if r['field']=='rapid_cold_plunge') is False
