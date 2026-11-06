import importlib.util
import json
from pathlib import Path
import sqlite3

spec = importlib.util.spec_from_file_location('repair', Path(__file__).parents[1] / 'repair_northglenn_queue.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)

def test_archive_update_preserves_body_and_is_idempotent():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE pending (key TEXT PRIMARY KEY,hash TEXT,url TEXT,source_id TEXT,title TEXT,body TEXT,hint TEXT,priority INTEGER,status TEXT,attempts INTEGER,last_error TEXT,result TEXT,first_seen TEXT,updated_at TEXT,lease_id TEXT)')
    for key in ['recover','exclude','changed']:
        db.execute("INSERT INTO pending(key,hash,url,body,status,attempts,last_error) VALUES (?,?,?,?,?,?,?)",(key,'h','https://www.northglenn.org/wrong/'+key,'original body','stuck',6,'detail-unavailable: HTTP 404'))
    changes=[{'key':key,'hash':'h','old_url':'https://www.northglenn.org/wrong/'+key,'new_url':'https://www.northglenn.org/events/'+key,'attempts':6,'last_error':'detail-unavailable: HTTP 404','action':'excluded' if key=='exclude' else 'queued'} for key in ['recover','exclude','changed']]
    payload=json.dumps(changes)
    db.execute(repair.CREATE)
    db.execute(repair.ARCHIVE,(repair.REPAIR,payload))
    db.execute("UPDATE pending SET hash='new' WHERE key='changed'")
    assert sorted(db.execute(repair.UPDATE,(payload,repair.REPAIR)).fetchall())==[('exclude','excluded'),('recover','queued')]
    assert db.execute(repair.UPDATE,(payload,repair.REPAIR)).fetchall()==[]
    assert db.execute("SELECT body,attempts,url FROM pending WHERE key='recover'").fetchone()==('original body',0,'https://www.northglenn.org/events/recover')
    snapshot=json.loads(db.execute("SELECT snapshot FROM pending_repair_audit WHERE key='recover'").fetchone()[0])
    assert snapshot['status']=='stuck' and snapshot['attempts']==6 and snapshot['body']=='original body'
    assert db.execute("SELECT status,attempts,url FROM pending WHERE key='exclude'").fetchone()==('excluded',6,'https://www.northglenn.org/wrong/exclude')
    assert db.execute("SELECT status FROM pending WHERE key='changed'").fetchone()==('stuck',)

def test_plan_only_recovers_proven_links_with_readable_destinations(monkeypatch):
    monkeypatch.setattr(repair,'page_text',lambda client,url:(200,'x'*100) if url.endswith('valid') else (404,''))
    rows=[{'key':key,'hash':'h','url':key,'attempts':6,'last_error':'detail-unavailable: HTTP 404'} for key in ['valid','bad','nav','unknown']]
    mapping={'valid':'https://www.northglenn.org/events/valid','bad':'https://www.northglenn.org/events/bad','nav':'https://www.northglenn.org/government/board.php'}
    assert [(x['key'],x['action']) for x in repair.plan(rows,mapping,None)]==[('valid','queued'),('nav','excluded')]
