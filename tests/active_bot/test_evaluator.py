from hanset_active.evaluator import evaluate, jaccard, trial, trial_task, is_aggregator
from hanset_active.discover import Discovery

def document():
    return {'title':'Silver Sauna Seoul','text':'Silver Sauna Seoul sauna spa '+('Open daily welcome to our venue. '*5)+'\nDay pass 25000 KRW','dates':['2026-10-25'],'mentions':[{'name':'Silver Sauna'}],'links':[]}

def test_decisions_and_copy():
    doc=document()
    result=evaluate('https://silver.example',[doc],venue_name='Silver Sauna')
    assert result['decision']=='registered'
    assert all(0<=result[k]<=1 for k in ('relevance','novelty','extractability','original'))
    assert evaluate('https://medium.com/sauna',[doc])['decision']=='deprioritized'
    assert evaluate('https://silver.example',[doc],blocked=True)['decision']=='inaccessible'
    copied=evaluate('https://copy.example',[doc],existing_texts=[('other.example',doc['title']+'\n'+doc['text'])])
    assert copied['decision']=='deprioritized' and copied['reason']=='copy'
    assert jaccard(doc['text'],doc['text'])==1
    assert jaccard('short','short')==0
    assert is_aggregator('https://blog.example/sauna')

def test_trial_offline_and_idempotent(bot,server):
    server.add('/venue','<title>Silver Sauna Seoul</title><main>'+document()['text']+'<a href="/price">Price</a><a hreflang="en" href="/en/">English</a></main>')
    server.add('/price','<main>'+document()['text']+'</main>')
    server.add('/en/','<main>'+document()['text']+'</main>')
    context=Discovery(bot,budget=3)
    assert trial_task(bot.db,server.base+'/venue')
    decision=trial(context,server.base+'/venue',venue_name='Silver Sauna')
    assert decision=='registered'
    assert trial(context,server.base+'/venue')==decision
    assert bot.db.execute('SELECT count(*) FROM source_trials').fetchone()[0]==1
    assert len([r for r in server.log if r['path']!='/robots.txt'])==3

def test_taxonomy_is_review_only(bot):
    from hanset_active.evaluator import record_doc_kind
    assert record_doc_kind(bot.db,'venue_page') is None
    first=record_doc_kind(bot.db,'new_document_shape')
    assert first==record_doc_kind(bot.db,'new_document_shape')
    assert bot.db.execute('SELECT count(*) FROM taxonomy_candidates').fetchone()[0]==1
