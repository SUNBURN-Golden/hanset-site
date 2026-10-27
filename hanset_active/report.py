"""Acceptance metrics from persisted evidence, never inferred facts."""
import json
from pathlib import Path
from .config import REPO_ROOT
from .util.pii import strip_pii

NATURAL_KEYS = {
    'raw_documents':('canonical_url','content_hash'),
    'extractions':('raw_id','extractor','extractor_version'),
    'labels':('raw_id','kind','value','rule_id'),
    'observations':('venue_key','field','value_hash','raw_id'),
    'venues':('venue_key',),
    'venue_fields':('venue_id','field','value_hash','valid_from'),
    'field_changes':('venue_id','field','old_value_hash','new_value_hash'),
    'gaps':('venue_id','field'),
    'tasks':('dedupe_key',),
    'venue_links':('venue_id','url','kind'),
    'sources':('domain',),
    'gap_attempts':('gap_id','strategy','url'),
    'structure_alerts':('source_id','page_type','raw_id'),
}

def duplicate_check(db):
    """Count excess rows per natural key, including NULL key components."""
    return {table:db.execute(f'SELECT coalesce(sum(n-1),0) FROM (SELECT count(*) n FROM {table} GROUP BY '+','.join(keys)+' HAVING count(*)>1)').fetchone()[0] for table,keys in NATURAL_KEYS.items()}


def report(db):
    def rows(sql, params=()): return [dict(r) for r in db.execute(sql,params)]
    def count(sql): return db.execute(sql).fetchone()[0]
    duplicates = duplicate_check(db)
    sources = rows("SELECT s.url,s.domain,s.reason,s.quality,(SELECT t.discovered_via FROM raw_documents r JOIN tasks t ON t.task_id=r.task_id WHERE r.source_id=s.source_id ORDER BY r.fetched_at LIMIT 1) discovered_via FROM sources s WHERE state='registered' AND NOT EXISTS (SELECT 1 FROM seed_domains d JOIN run_checkpoints c ON c.checkpoint_id='seed_freeze' AND c.cursor=d.seed_id WHERE d.domain=s.domain)")
    new_venues = rows('SELECT name,website AS url,discovered_via FROM venues WHERE is_seed=0')
    filled = rows("SELECT v.name,g.field,g.evidence_url FROM gaps g JOIN venues v USING(venue_id) WHERE g.state='filled'")
    extracted = rows('SELECT extractor,structured FROM extractions')
    pages = [json.loads(r['structured'] or '{}').get('_active_page',{}) for r in extracted]
    next_urls = {link['url'] for page in pages for link in page.get('links',[]) if link.get('kind')=='next_page'}
    requested = {r['url'] for r in rows('SELECT DISTINCT url FROM request_log')}
    facts = count("SELECT count(*) FROM observations WHERE label_status='fact'")
    result = {
        'new_valid_sources':{'count':len(sources),'examples':sources[:10]},
        'new_venues':{'count':len(new_venues),'examples':new_venues[:10]},
        'multi_page_lists_followed':len(next_urls & requested),
        'pdfs_collected':sum(r['extractor']=='pdf' and bool(p.get('text')) and not p.get('unavailable') for r,p in zip(extracted,pages)),
        'images_ocred':sum(r['extractor']=='ocr' and bool(p.get('text')) and not p.get('unavailable') for r,p in zip(extracted,pages)),
        'gaps_filled':{'count':len(filled),'examples':filled[:10]},
        'gap_states':{r['state']:r['n'] for r in rows('SELECT state,count(*) n FROM gaps GROUP BY state')},
        'duplicate_check':{'total':sum(duplicates.values()),'tables':duplicates},
        'field_changes':{'count':count('SELECT count(*) FROM field_changes'),'examples':rows('SELECT field,old_value,new_value,detected_at FROM field_changes LIMIT 10')},
        'structure_alerts':{'count':count('SELECT count(*) FROM structure_alerts'),'examples':rows('SELECT page_type,reason,similarity FROM structure_alerts LIMIT 10')},
        'request_cap_usage':{'requests':count('SELECT count(*) FROM request_log'),'daily':rows('SELECT * FROM budgets_daily'),'caps':{'candidates':500,'bodies':200,'aux':800}},
        'provider_states':rows('SELECT * FROM provider_state'),
        'inaccessible_sources':rows("SELECT url,domain,reason FROM sources WHERE state='inaccessible'"),
        'cost_per_valid_fact':{'ai_cost':0,'valid_facts':facts,'ai_cost_per_fact':0 if facts else None,'requests_per_fact':count('SELECT count(*) FROM request_log')/facts if facts else None},
    }
    return json.loads(strip_pii(json.dumps(result,ensure_ascii=False)))


def status(db):
    return {'tasks':{r['state']:r['n'] for r in db.execute('SELECT state,count(*) n FROM tasks GROUP BY state')},
            'lease':[dict(r) for r in db.execute('SELECT * FROM run_lease')],
            'latest_run':dict(r) if (r:=db.execute('SELECT * FROM runs ORDER BY started_at DESC,rowid DESC LIMIT 1').fetchone()) else None}


def export_service_data(db, out):
    out = Path(out).resolve()
    if out == REPO_ROOT or REPO_ROOT in out.parents: raise ValueError('Export directory must be outside checkout')
    out.mkdir(parents=True,exist_ok=True)
    tables = ('venues','venue_aliases','venue_links','venue_fields','field_changes','gaps')
    for table in tables:
        payload = strip_pii(json.dumps([dict(r) for r in db.execute('SELECT * FROM '+table)],ensure_ascii=False,indent=2))
        temporary = out/(table+'.json.tmp')
        temporary.write_text(payload,encoding='utf-8')
        temporary.replace(out/(table+'.json'))
    return [str(out/(t+'.json')) for t in tables]
