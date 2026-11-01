"""Portal/directory domains are never stored as a venue's own website (de-identified)."""
import json
from hanset_active import maintenance, venues
from hanset_active.discover import directories

PROFILE={'directories':[{'id':'portal_en','directory':True,'url':'https://english.portal.example/sitemap.xml'},{'id':'portal_ko','directory':True,'url':'https://korean.portal.example/sitemap.xml'}],'aggregator_domains':[]}

def page(links):
    anchors=''.join(f'<li><a href="{u}">{t}</a></li>' for u,t in links)
    return f'<html><head><meta property="og:title" content="Example Spa | Portal"></head><body><ul>{anchors}</ul><p>주소: 1 Example-ro</p></body></html>'.encode()

def test_detail_skips_portal_language_hosts():
    url='https://english.portal.example/spa/P1'
    got=directories.detail(page([('https://korean.portal.example/','한국어'),('https://spa.example/','Website')]),url,PROFILE)
    assert got['website'].startswith('https://spa.example')
    assert directories.detail(page([('https://korean.portal.example/','한국어')]),url,PROFILE)['website']==''

def test_upsert_and_scrub_drop_portal_domain(bot,monkeypatch):
    db=bot.db
    monkeypatch.setattr(venues,'portal_domains',lambda *a:{'portal.example'})
    monkeypatch.setattr(maintenance,'portal_domains',lambda *a:{'portal.example'})
    vid=venues.upsert_venue(db,'Example Spa',website='https://korean.portal.example/')
    row=db.execute('SELECT website,domains FROM venues WHERE venue_id=?',(vid,)).fetchone()
    assert row['website']=='' and json.loads(row['domains'])==[]
    # a pre-existing bad row (written before the fix) is repaired in place, audited, idempotently
    with db:
        db.execute('UPDATE venues SET website=?,domains=? WHERE venue_id=?',('https://korean.portal.example',json.dumps(['portal.example','spa.example']),vid))
        other=venues.upsert_venue(db,'Sample Sauna',website='https://sauna.example/')
    assert maintenance.scrub_portal_venue_domains(db)==[vid]
    row=db.execute('SELECT website,domains FROM venues WHERE venue_id=?',(vid,)).fetchone()
    assert row['website']=='' and json.loads(row['domains'])==['spa.example']
    assert db.execute("SELECT count(*) FROM review_queue WHERE kind='portal_domain_scrubbed' AND venue_id=?",(vid,)).fetchone()[0]==1
    assert json.loads(db.execute('SELECT domains FROM venues WHERE venue_id=?',(other,)).fetchone()[0])==['sauna.example']
    assert maintenance.scrub_portal_venue_domains(db)==[]
