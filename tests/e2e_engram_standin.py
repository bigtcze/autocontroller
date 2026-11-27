#!/usr/bin/env python3
"""STAND-IN for the `engram` binary, used only by the offline E2E acceptance suite.

This is NOT upstream Engram. It implements exactly the HTTP-over-Unix-socket
endpoints that memory.Backend uses, persisted in a real SQLite file at
$ENGRAM_DATA_DIR/engram.db so restart persistence and integrity checks exercise
real storage. Search semantics are a simplified approximation (see
docs/E2E_TESTING.md). Launched by the real memory.Runtime.start_process via
`<folder>/engram serve` with ENGRAM_DATA_DIR and ENGRAM_SOCKET set.
"""
import json
import os
from pathlib import Path
import re
import socketserver
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

STANDIN_VERSION = '3.2.0'
MAX_BODY = 1_000_000
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, project TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL, scope TEXT NOT NULL, type TEXT NOT NULL,
  topic_key TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
  session_id TEXT NOT NULL,
  UNIQUE(project, topic_key));
"""
HEX64 = re.compile(r'[0-9a-f]{64}')
LOCK = threading.Lock()


def row_dict(row):
    return {'id': row[0], 'project': row[1], 'scope': row[2], 'type': row[3],
            'topic_key': row[4], 'title': row[5], 'content': row[6]}


class Handler(BaseHTTPRequestHandler):
    server_version = 'EngramStandIn/1'
    protocol_version = 'HTTP/1.0'

    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, status, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _json_body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length > MAX_BODY:
            raise ValueError('body too large')
        return json.loads(self.rfile.read(length) or b'{}')

    def do_GET(self):
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        db = self.server.db
        try:
            if url.path == '/health':
                return self._send(200, {'status': 'ok', 'service': 'engram', 'version': STANDIN_VERSION,
                                        'capabilities': {'isolated_session_registration': True}})
            if url.path == '/search':
                return self._send(200, self._search(db, query))
            match = re.fullmatch(r'/observations/(\d+)', url.path)
            if match:
                with LOCK:
                    row = db.execute('SELECT id,project,scope,type,topic_key,title,content FROM observations WHERE id=?',
                                     (int(match.group(1)),)).fetchone()
                if row is None:
                    return self._send(404, {'error': 'not found'})
                return self._send(200, row_dict(row))
            return self._send(404, {'error': 'not found'})
        except (ValueError, sqlite3.Error) as exc:
            return self._send(400, {'error': str(exc)})

    def do_POST(self):
        db = self.server.db
        try:
            body = self._json_body()
            if self.path == '/sessions':
                sid, project = str(body['id']), str(body['project'])
                if body.get('isolated') is not True or body.get('ownership_mode') != 'project_owned':
                    return self._send(400, {'error': 'session must be isolated and project_owned'})
                with LOCK:
                    db.execute('INSERT INTO sessions(id,project) VALUES(?,?) '
                               'ON CONFLICT(id) DO NOTHING', (sid, project))
                    db.commit()
                return self._send(200, {'id': sid})
            if self.path == '/observations':
                return self._send(200, self._save(db, body))
            return self._send(404, {'error': 'not found'})
        except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
            return self._send(400, {'error': str(exc)})

    @staticmethod
    def _save(db, body):
        project, scope, kind = str(body['project']), str(body['scope']), str(body['type'])
        key, title, content = str(body['topic_key']), str(body['title']), str(body['content'])
        session = str(body['session_id'])
        with LOCK:
            if not db.execute('SELECT 1 FROM sessions WHERE id=? AND project=?', (session, project)).fetchone():
                raise ValueError('unknown session for project')
            existing = db.execute('SELECT id FROM observations WHERE project=? AND topic_key=?',
                                  (project, key)).fetchone()
            if existing:  # topic_key upsert keeps the identity of the first observation
                db.execute('UPDATE observations SET scope=?,type=?,title=?,content=?,session_id=? WHERE id=?',
                           (scope, kind, title, content, session, existing[0]))
                ident = existing[0]
            else:
                cur = db.execute('INSERT INTO observations(project,scope,type,topic_key,title,content,session_id) '
                                 'VALUES(?,?,?,?,?,?,?)', (project, scope, kind, key, title, content, session))
                ident = cur.lastrowid
            db.commit()
        return {'id': ident}

    @staticmethod
    def _search(db, query):
        project, scope, kind = query.get('project', ''), query.get('scope', ''), query.get('type', '')
        limit = max(1, min(50, int(query.get('limit', 12))))
        text = query.get('q', '').strip().casefold()
        if not text:
            raise ValueError('empty query')
        with LOCK:
            rows = db.execute('SELECT id,project,scope,type,topic_key,title,content FROM observations '
                              'WHERE project=? AND scope=? AND type=? ORDER BY id',
                              (project, scope, kind)).fetchall()
        if HEX64.fullmatch(text):  # exact topic-key lookup used for dedupe
            hits = [r for r in rows if r[4] == text]
        else:  # match_mode=any over title, topic key and content
            terms = [t for t in re.split(r'\s+', text) if t]
            hits = [r for r in rows if any(t in f"{r[5]} {r[4]} {r[6]}".casefold() for t in terms)]
        return [row_dict(r) for r in hits[:limit]]


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main():
    data = Path(os.environ['ENGRAM_DATA_DIR'])
    sock = os.environ['ENGRAM_SOCKET']
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(str(data / 'engram.db'), check_same_thread=False, timeout=10)
    db.executescript(SCHEMA)
    db.commit()
    if os.path.exists(sock):
        os.unlink(sock)
    server = Server(sock, Handler)
    server.db = db
    try:
        server.serve_forever()
    finally:
        server.server_close()
        db.close()


if __name__ == '__main__':
    if sys.argv[1:] != ['serve']:
        sys.exit('usage: engram serve (stand-in)')
    main()
