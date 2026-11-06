"""Repair only links proven malformed by the Northglenn HTML-base incident.

Default: read-only plan. --apply archives each matching original row before a
compare-and-swap update. No deletes, AI calls, or Telegram sends.
"""
from __future__ import annotations
import argparse
import collections
import json
import os
from pathlib import Path
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'collector'))
from fetch import _LinkParser, EVENTISH, new_client, normalize_url, page_text

REPAIR = 'northglenn-html-base-20261107'
PAGES = [
    'https://www.northglenn.org/parks_rec/programs/index.php',
    'https://www.northglenn.org/parks_rec/programs/youth_teens/index.php',
]

def api(path, data=None):
    base = 'https://api.cloudflare.com/client/v4/accounts/' + os.environ['CLOUDFLARE_ACCOUNT_ID']
    req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
        headers={'Authorization': 'Bearer ' + os.environ['CLOUDFLARE_API_TOKEN'], 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as response:
        result = json.load(response)
    if not result.get('success'):
        raise RuntimeError('Cloudflare request failed')
    return result['result']

def link_map(client):
    mapping = {}
    for page in PAGES:
        response = client.get(page)
        response.raise_for_status()
        parser = _LinkParser()
        parser.feed(response.text)
        if parser.base_href is None:
            raise RuntimeError('Expected base href missing; stop without changing queue')
        base = normalize_url(parser.base_href, page)
        if urllib.parse.urlsplit(base).hostname != 'www.northglenn.org':
            raise RuntimeError('Unexpected base host')
        for href, title in parser.links:
            old, new = normalize_url(href, page), normalize_url(href, base)
            if old != new and urllib.parse.urlsplit(new).hostname == 'www.northglenn.org':
                if old in mapping and mapping[old] != new:
                    raise RuntimeError('Ambiguous repair mapping')
                mapping[old] = new
    return mapping

def plan(rows, mapping, client):
    changes, checked = [], {}
    for row in rows:
        new = mapping.get(row['url'])
        if not new:
            continue
        parts = urllib.parse.urlsplit(new)
        if not EVENTISH.search(parts.path + '?' + parts.query):
            action = 'excluded'
        else:
            if new not in checked:
                try:
                    status, text = page_text(client, new)
                    checked[new] = status is not None and 200 <= status < 300 and len(text) >= 80
                except Exception:
                    checked[new] = False
            if not checked[new]:
                continue
            action = 'queued'
        changes.append({'key': row['key'], 'hash': row['hash'], 'old_url': row['url'],
            'attempts': row['attempts'], 'last_error': row['last_error'], 'new_url': new, 'action': action})
    return changes

MATCH = "p.key=json_extract(j.value,'$.key') AND p.hash=json_extract(j.value,'$.hash') AND p.url=json_extract(j.value,'$.old_url') AND p.attempts=json_extract(j.value,'$.attempts') AND p.last_error=json_extract(j.value,'$.last_error') AND p.status='stuck' AND p.lease_id IS NULL"
ARCHIVE = "INSERT OR IGNORE INTO pending_repair_audit (repair_id, key, snapshot, new_url, action) SELECT ?, p.key, json_object('key',p.key,'hash',p.hash,'url',p.url,'source_id',p.source_id,'title',p.title,'body',p.body,'hint',p.hint,'priority',p.priority,'status',p.status,'attempts',p.attempts,'last_error',p.last_error,'result',p.result,'first_seen',p.first_seen,'updated_at',p.updated_at), json_extract(j.value,'$.new_url'), json_extract(j.value,'$.action') FROM pending p JOIN json_each(?) j ON " + MATCH
UPDATE = "UPDATE pending AS p SET url=CASE WHEN json_extract(j.value,'$.action')='queued' THEN json_extract(j.value,'$.new_url') ELSE p.url END, status=json_extract(j.value,'$.action'), attempts=CASE WHEN json_extract(j.value,'$.action')='queued' THEN 0 ELSE p.attempts END, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM json_each(?) j WHERE " + MATCH + " AND EXISTS (SELECT 1 FROM pending_repair_audit a WHERE a.repair_id=? AND a.key=p.key AND json_extract(a.snapshot,'$.hash')=p.hash AND json_extract(a.snapshot,'$.url')=p.url AND json_extract(a.snapshot,'$.attempts')=p.attempts AND json_extract(a.snapshot,'$.last_error')=p.last_error AND a.new_url=json_extract(j.value,'$.new_url') AND a.action=json_extract(j.value,'$.action')) RETURNING key,status"
CREATE = "CREATE TABLE IF NOT EXISTS pending_repair_audit (repair_id TEXT NOT NULL, key TEXT NOT NULL, snapshot TEXT NOT NULL, new_url TEXT NOT NULL, action TEXT NOT NULL, repaired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), PRIMARY KEY(repair_id,key))"

def main():
    args = argparse.ArgumentParser()
    args.add_argument('--apply', action='store_true')
    opts = args.parse_args()
    ids = [d['uuid'] for d in api('/d1/database?per_page=100') if d['name'] == 'us-alert-db']
    assert len(ids) == 1
    def query(sql, params=()):
        result = api('/d1/database/' + ids[0] + '/query', {'sql': sql, 'params': list(params)})
        if not result[0].get('success', True): raise RuntimeError('D1 statement failed')
        return result[0].get('results', [])
    rows = query("SELECT key,hash,url,attempts,last_error FROM pending WHERE status='stuck' AND last_error LIKE 'detail-unavailable:%' AND url LIKE 'https://www.northglenn.org/%' AND lease_id IS NULL")
    with new_client() as client:
        changes = plan(rows, link_map(client), client)
    counts = dict(collections.Counter(c['action'] for c in changes))
    print(json.dumps({'mode': 'apply' if opts.apply else 'plan', 'candidates': len(rows), 'planned': counts, 'unchanged': len(rows)-len(changes)}))
    if opts.apply and changes:
        query(CREATE)
        updated = []
        for i in range(0,len(changes),40):
            payload = json.dumps(changes[i:i+40])
            query(ARCHIVE, (REPAIR,payload))
            updated.extend(query(UPDATE, (payload,REPAIR)))
        print(json.dumps({'updated':dict(collections.Counter(r['status'] for r in updated)), 'total_updated':len(updated)}))
    print(json.dumps({'queue':query('SELECT status,COUNT(*) AS n FROM pending GROUP BY status')}))

if __name__ == '__main__':
    main()
