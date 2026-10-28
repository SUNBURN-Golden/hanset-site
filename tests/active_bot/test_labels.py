import pytest
from hanset_active.labeling import claim_status, labels, ModelHook, store_observation, store_labels
from hanset_active.extract.fields import extract

@pytest.mark.parametrize('source,status',[('own_site','fact'),('official_directory','fact'),('third_party','source_claim'),('aggregator','source_claim'),('sns','source_claim'),('attachment','source_claim')])
def test_status(source,status):
    assert claim_status(source)==status
    assert claim_status(source,ambiguous=True)=='unverified'
    assert all(r['status']!='ai_inference' for r in labels('Day pass price change',source_type=source))

def test_attachment_hook_and_labels():
    assert claim_status('attachment',hosted_on_own_domain=True)=='fact'
    assert claim_status('own_site',ocr_confidence=59)=='unverified'
    assert ModelHook().infer('anything') is None
    assert labels('요금 변경')[1]['value']=='price_change'
    assert labels('',content_type='application/pdf')[0]['value']=='pdf_attachment'
    assert labels('운영시간')[0]['value']=='hours_page'

def test_observation_idempotence(bot):
    row=extract('Day pass 25000 KRW')[0]
    first=store_observation(bot.db,'test',row,source_url='https://venue.example/price',source_type='own_site',clock=bot.clock)
    assert first==store_observation(bot.db,'test',row,source_url='https://venue.example/price',source_type='own_site',clock=bot.clock)
    assert bot.db.execute('SELECT count(*) FROM observations').fetchone()[0]==1
    store_labels(bot.db,None,None,labels('Day pass price'),bot.clock)
    store_labels(bot.db,None,None,labels('Day pass price'),bot.clock)
    assert bot.db.execute('SELECT count(*) FROM labels').fetchone()[0]==1

def test_claim_labels_preserve_rule_ambiguity():
    claims=[r for r in labels('Day pass 25000 KRW or 27000 KRW',source_type='own_site') if r['kind']=='claim']
    assert claims and claims[0]['status']=='unverified'
    claims=[r for r in labels('Day pass 25000 KRW',source_type='third_party') if r['kind']=='claim']
    assert claims[0]['status']=='source_claim'
