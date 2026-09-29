#!/usr/bin/env python3
"""SyncHarbor: reviewable REST-to-SQLite synchronization. Python 3.11+."""
import csv
from contextlib import contextmanager
import io
import ipaddress
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qs
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parent
PORT = int(os.getenv('PORT', '8111'))
DB = Path(os.getenv('SYNCHARBOR_DB', str(ROOT / 'data' / 'syncharbor.sqlite3')))
LOCK = threading.RLock()
PREVIEWS = {}
MAX_PAGES = 20
MAX_RECORDS = 5000
DEFAULT_MAPPING = {'external_id': 'id', 'name': 'company', 'email': 'contact.email', 'status': 'stage'}


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


@contextmanager
def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=5000')
    try:
        with con:
            yield con
    finally:
        con.close()


def initialize():
    with connect() as con:
        con.executescript('''CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, label TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
        INSERT OR IGNORE INTO meta VALUES('revision', '0');''')
        if not con.execute('SELECT 1 FROM snapshots').fetchone():
            con.execute('INSERT INTO snapshots(created,label,payload) VALUES(?,?,?)', (stamp(), 'Initial empty dataset', '{}'))


def dataset(con):
    return {r['id']: json.loads(r['payload']) for r in con.execute('SELECT id,payload FROM records ORDER BY id')}


def revision(con):
    return int(con.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])


def state():
    with LOCK, connect() as con:
        rows = dataset(con)
        return {'records': list(rows.values()), 'revision': revision(con), 'snapshots': [dict(r) for r in con.execute('SELECT id,created,label FROM snapshots ORDER BY id DESC LIMIT 30')], 'history': [dict(r) for r in con.execute('SELECT * FROM history ORDER BY id DESC LIMIT 12')], 'remote_enabled': os.getenv('ALLOW_REMOTE_FETCH') == '1', 'default_url': f'http://127.0.0.1:{PORT}/api/fixture?page=1&edition=1', 'default_mapping': DEFAULT_MAPPING}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Redirects are disabled. Enter the final endpoint URL explicitly.')


def validate_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError('Use an HTTP(S) URL without credentials or fragments.')
    try:
        local = ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        local = parts.hostname == 'localhost'
    if not local and os.getenv('ALLOW_REMOTE_FETCH') != '1':
        raise ValueError('Remote fetching is disabled. Set ALLOW_REMOTE_FETCH=1 explicitly to enable it.')
    if os.getenv('ALLOW_INSECURE_HTTP') != '1' and not local and parts.scheme != 'https':
        raise ValueError('Remote endpoints must use HTTPS; opt in with ALLOW_INSECURE_HTTP=1 if required.')
    return parts


def fetch_pages(url):
    first = validate_url(url)
    origin = (first.scheme, first.hostname, first.port)
    visited, raw, requests = set(), [], 0
    deadline = time.monotonic() + 25
    opener = build_opener(NoRedirect())
    while url:
        if len(visited) >= MAX_PAGES:
            raise ValueError(f'Pagination exceeds the {MAX_PAGES}-page limit; no partial preview was retained.')
        if url in visited:
            raise ValueError('Pagination loop detected; no partial preview was retained.')
        parts = validate_url(url)
        if (parts.scheme, parts.hostname, parts.port) != origin:
            raise ValueError('Pagination must remain on the configured origin.')
        visited.add(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('The total 25-second fetch budget was exhausted.')
        req = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'SyncHarbor/1.0'})
        with opener.open(req, timeout=min(6, remaining)) as res:
            data = res.read(2_000_001)
            if len(data) > 2_000_000:
                raise ValueError('Response exceeds the 2 MB per-page limit.')
        requests += 1
        body = json.loads(data)
        if not isinstance(body, dict) or not isinstance(body.get('items'), list):
            raise ValueError('Each response must contain an items array and an optional next URL.')
        raw.extend(body['items'])
        if len(raw) > MAX_RECORDS:
            raise ValueError(f'Response exceeds {MAX_RECORDS} records.')
        nxt = body.get('next')
        if nxt is not None and not isinstance(nxt, str):
            raise ValueError('next must be a URL string or null.')
        url = urljoin(url, nxt) if nxt else None
    return raw, requests


def value_at(row, path):
    value = row
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        raise ValueError(f'{path} is not a scalar field.')
    return str(value)


def make_preview(url, mapping):
    if not isinstance(mapping, dict) or set(mapping) != set(DEFAULT_MAPPING) or any(not isinstance(v, str) or not v.strip() for v in mapping.values()):
        raise ValueError('Map exactly external_id, name, email and status to nonempty dot-path strings.')
    start = time.monotonic()
    raw, pages = fetch_pages(url)
    incoming, conflicts = {}, []
    for index, row in enumerate(raw, 1):
        if not isinstance(row, dict):
            conflicts.append({'row': index, 'reason': 'Record is not an object.'})
            continue
        try:
            normalized = {key: value_at(row, path) for key, path in mapping.items()}
            if any(v is None for v in normalized.values()):
                raise ValueError('One or more mapped paths are missing. Correct the mapping before committing.')
            if not normalized['external_id'].strip() or not normalized['name'].strip():
                raise ValueError('external_id and name must be nonempty.')
            if len(normalized['external_id']) > 200 or any(len(v) > 10000 for v in normalized.values()):
                raise ValueError('A mapped value exceeds the supported length.')
            key = normalized['external_id']
            if key in incoming and incoming[key] != normalized:
                raise ValueError(f'Conflicting duplicate ID: {key}.')
            incoming[key] = normalized
        except ValueError as error:
            conflicts.append({'row': index, 'reason': str(error)})
    with LOCK, connect() as con:
        current, base = dataset(con), revision(con)
        changes = [{'kind': 'add' if key not in current else 'update' if current[key] != row else 'unchanged', 'record': row, 'before': current.get(key)} for key, row in incoming.items()]
        token = str(uuid.uuid4())
        result = {'token': token, 'created': stamp(), 'source_url': url, 'base_revision': base, 'pages': pages, 'input_rows': len(raw), 'elapsed_ms': round((time.monotonic() - start) * 1000), 'changes': changes, 'conflicts': conflicts, 'additions': sum(c['kind'] == 'add' for c in changes), 'updates': sum(c['kind'] == 'update' for c in changes), 'unchanged': sum(c['kind'] == 'unchanged' for c in changes)}
        cutoff = time.monotonic() - 1800
        for old in [key for key, (_, created) in PREVIEWS.items() if created < cutoff]:
            del PREVIEWS[old]
        if len(PREVIEWS) >= 30:
            PREVIEWS.pop(next(iter(PREVIEWS)))
        PREVIEWS[token] = (result, time.monotonic())
        return result


def save_snapshot(con, label):
    con.execute('INSERT INTO snapshots(created,label,payload) VALUES(?,?,?)', (stamp(), label, json.dumps(dataset(con))))


def commit(token, allow_updates=False):
    with LOCK, connect() as con:
        if token not in PREVIEWS or time.monotonic() - PREVIEWS[token][1] > 1800:
            raise ValueError('Preview expired or unavailable. Fetch a fresh preview.')
        preview = PREVIEWS[token][0]
        if preview['conflicts']:
            raise ValueError('Resolve every rejected row or conflicting duplicate before committing.')
        con.execute('BEGIN IMMEDIATE')
        if revision(con) != preview['base_revision']:
            raise ValueError('The dataset changed after this preview. Fetch a fresh preview before committing.')
        if preview['updates'] and allow_updates is not True:
            raise ValueError('Explicitly approve the changed existing records before committing.')
        save_snapshot(con, f"Before import at revision {revision(con)}")
        for change in preview['changes']:
            if change['kind'] != 'unchanged':
                row = change['record']
                con.execute('INSERT OR REPLACE INTO records VALUES(?,?)', (row['external_id'], json.dumps(row)))
        new_revision = revision(con) + 1
        con.execute("UPDATE meta SET value=? WHERE key='revision'", (str(new_revision),))
        save_snapshot(con, f'Imported dataset · revision {new_revision}')
        detail = f"{preview['additions']} added, {preview['updates']} updated; {preview['pages']} HTTP pages; {preview['source_url']}"
        con.execute('INSERT INTO history(created,action,detail) VALUES(?,?,?)', (stamp(), 'Import committed', detail))
        con.commit()
        PREVIEWS.pop(token, None)
        return {'revision': new_revision}


def restore(snapshot_id, expected_revision):
    with LOCK, connect() as con:
        con.execute('BEGIN IMMEDIATE')
        if revision(con) != expected_revision:
            raise ValueError('Dataset changed. Refresh before restoring a snapshot.')
        row = con.execute('SELECT * FROM snapshots WHERE id=?', (snapshot_id,)).fetchone()
        if row is None:
            raise ValueError('Snapshot does not exist.')
        save_snapshot(con, f'Before restoring snapshot {snapshot_id}')
        con.execute('DELETE FROM records')
        for key, value in json.loads(row['payload']).items():
            con.execute('INSERT INTO records VALUES(?,?)', (key, json.dumps(value)))
        rev = revision(con) + 1
        con.execute("UPDATE meta SET value=? WHERE key='revision'", (str(rev),))
        save_snapshot(con, f'Restored snapshot {snapshot_id} · revision {rev}')
        con.execute('INSERT INTO history(created,action,detail) VALUES(?,?,?)', (stamp(), 'Snapshot restored', f"Restored {row['label']}. Previous state retained."))
        return {'revision': rev}


FIXTURE = [
    {'id': 'C-1001', 'company': 'Northline Architecture', 'contact': {'email': 'studio@northline.example'}, 'stage': 'Active'},
    {'id': 'C-1002', 'company': 'Moss & Timber', 'contact': {'email': 'hello@moss.example'}, 'stage': 'Onboarding'},
    {'id': 'C-1003', 'company': 'Harbor Coffee', 'contact': {'email': 'team@harbor.example'}, 'stage': 'Active'},
    {'id': 'C-1004', 'company': 'Forma Studio', 'contact': {'email': 'work@forma.example'}, 'stage': 'Review'},
    {'id': 'C-1005', 'company': 'Fieldwork Collective', 'contact': {'email': 'desk@fieldwork.example'}, 'stage': 'Active'},
    {'id': 'C-1006', 'company': 'Aster Logistics', 'contact': {'email': 'ops@aster.example'}, 'stage': 'Onboarding'},
]


class Handler(BaseHTTPRequestHandler):
    def valid_request(self):
        allowed = {f'127.0.0.1:{PORT}', f'localhost:{PORT}'}
        if self.headers.get('Host') not in allowed:
            self.reply(403, {'error': 'Invalid Host header.'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin not in {f'http://{v}' for v in allowed}:
            self.reply(403, {'error': 'Cross-origin requests are rejected.'})
            return False
        return True

    def reply(self, code, payload, content_type='application/json', filename=None):
        raw = (json.dumps(payload) if content_type == 'application/json' else payload).encode() if not isinstance(payload, bytes) else payload
        self.send_response(code)
        self.send_header('Content-Type', content_type + ('; charset=utf-8' if not content_type.startswith('image/') else ''))
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.valid_request():
            return
        path = urlsplit(self.path).path
        try:
            if path == '/':
                self.reply(200, (ROOT / 'index.html').read_text(), 'text/html')
            elif path == '/api/state':
                self.reply(200, state())
            elif path == '/api/fixture':
                query = parse_qs(urlsplit(self.path).query)
                page, edition = int(query.get('page', ['1'])[0]), int(query.get('edition', ['1'])[0])
                if page not in (1, 2) or edition not in (1, 2):
                    raise ValueError('Fixture supports page 1–2 and edition 1–2.')
                rows = json.loads(json.dumps(FIXTURE))
                if edition == 2:
                    rows[1]['stage'] = 'Active'
                    rows[3]['company'] = 'Forma Design Studio'
                    rows.append({'id': 'C-1007', 'company': 'Juniper Labs', 'contact': {'email': 'hello@juniper.example'}, 'stage': 'New'})
                cut = 3
                self.reply(200, {'fixture': True, 'items': rows[:cut] if page == 1 else rows[cut:], 'next': f'/api/fixture?page=2&edition={edition}' if page == 1 else None})
            elif path == '/api/export.json':
                self.reply(200, state()['records'], filename='syncharbor-records.json')
            elif path == '/api/export.csv':
                out = io.StringIO()
                writer = csv.DictWriter(out, fieldnames=list(DEFAULT_MAPPING))
                writer.writeheader()
                for row in state()['records']:
                    writer.writerow({k: "'" + v if v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for k, v in row.items()})
                self.reply(200, out.getvalue(), 'text/csv', 'syncharbor-records.csv')
            else:
                self.reply(404, {'error': 'Not found.'})
        except (ValueError, OSError) as error:
            self.reply(400, {'error': str(error)})

    def do_POST(self):
        if not self.valid_request():
            return
        try:
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('JSON requests are required.')
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > 30000:
                raise ValueError('Request must contain 1–30000 bytes.')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('Request body must be an object.')
            if self.path == '/api/preview':
                result = make_preview(body.get('url', ''), body.get('mapping'))
            elif self.path == '/api/commit':
                result = commit(body.get('token'), body.get('allow_updates', False))
            elif self.path == '/api/restore':
                result = restore(int(body.get('snapshot_id', 0)), int(body.get('expected_revision', -1)))
            else:
                self.reply(404, {'error': 'Not found.'})
                return
            self.reply(200, result)
        except (ValueError, OSError, KeyError, TypeError) as error:
            self.reply(400, {'error': str(error)})

    def log_message(self, fmt, *args):
        print(f'[{stamp()}] {fmt % args}', flush=True)


if __name__ == '__main__':
    initialize()
    print(f'SyncHarbor · http://127.0.0.1:{PORT} · remote fetch {"enabled" if os.getenv("ALLOW_REMOTE_FETCH") == "1" else "disabled"}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
