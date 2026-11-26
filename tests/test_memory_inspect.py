"""Offline tests for `memory inspect`: read-only, one GET /search, no POST on the wire.

A recording Unix-socket HTTP server in a thread stands in for Engram. It records every request
line, so the no-write property is checked on the wire, not inferred from code. Nothing touches
the real Engram socket, /run/user, HOME or the network.
"""
from __future__ import annotations

import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import memory as learning  # noqa: E402

REPO = 'example-owner/demo'


class RecordingEngram:
    """Serves canned answers and records (method, path) for every request."""

    def __init__(self, sock: Path, search_rows):
        self.sock = sock
        self.rows = search_rows
        self.requests: list[tuple[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'

            def log_message(self, *args):
                pass

            def _reply(self, obj):
                raw = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                outer.requests.append(('GET', self.path))
                if self.path.startswith('/search'):
                    return self._reply(outer.rows)
                self._reply({'status': 'ok', 'service': 'engram'})

            def do_POST(self):
                outer.requests.append(('POST', self.path))
                self._reply({'id': 1})

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.server = Server(str(sock), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def writes(self):
        return [r for r in self.requests if r[0] != 'GET']


def row(title, key, status, project):
    return {'id': 7, 'project': project, 'scope': 'project', 'type': learning.TYPE, 'topic_key': key,
            'title': title, 'content': json.dumps({'status': status, 'secret_lesson': 'do not print'})}


class InspectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='memory-inspect-'))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.state = self.tmp / 'state'
        self.root = self.state / 'memory'
        self.root.mkdir(parents=True)
        (self.root / 'active.json').write_text(json.dumps({'generation': 'gen-0123456789abcdef', 'version': 'v3.2.0'}))
        (self.root / 'metrics.json').write_text(json.dumps({'search_total': 4, 'record_total': 1}))
        # AF_UNIX paths are short; keep the socket under the temp root directly.
        self.sock = self.tmp / 'e.sock'

    def standin(self, rows):
        return RecordingEngram(self.sock, rows)

    def backend(self):
        return learning.Backend(self.sock)

    def test_summary_without_query_makes_no_request(self):
        with self.standin([]) as engram:
            lines = learning.inspect(self.state, REPO, backend=self.backend())
            self.assertEqual(engram.requests, [])
        self.assertIn('engram version: v3.2.0', lines)
        self.assertIn('search_total: 4', lines)

    def test_query_performs_one_get_search_and_no_write(self):
        rows = [row('Retry on 503', 'k1', 'confirmed', learning.scope(REPO))]
        with self.standin(rows) as engram:
            lines = learning.inspect(self.state, REPO, query='retry 503', backend=self.backend())
            self.assertEqual(len(engram.requests), 1)
            self.assertEqual(engram.requests[0][0], 'GET')
            self.assertTrue(engram.requests[0][1].startswith('/search?'))
            self.assertEqual(engram.writes(), [], 'memory inspect must never send a write')
        self.assertIn('- Retry on 503 | topic_key=k1 | status=confirmed', lines)

    def test_output_has_title_topic_and_status_only(self):
        rows = [row('Retry on 503', 'k1', 'candidate', learning.scope(REPO))]
        with self.standin(rows):
            text = '\n'.join(learning.inspect(self.state, REPO, query='retry', backend=self.backend()))
        self.assertNotIn('do not print', text)
        self.assertNotIn('secret_lesson', text)

    def test_shared_scope_queries_the_shared_project(self):
        rows = [row('Shared lesson', 'k2', 'shared', learning.SHARED)]
        with self.standin(rows) as engram:
            learning.inspect(self.state, REPO, query='lesson', shared=True, backend=self.backend())
            self.assertIn(f'project={learning.SHARED}', engram.requests[0][1])

    def test_limit_is_at_most_twenty_on_the_wire(self):
        with self.standin([]) as engram:
            learning.inspect(self.state, REPO, query='x', backend=self.backend())
            self.assertIn('limit=10', engram.requests[0][1])
        self.assertLessEqual(learning.INSPECT_LIMIT, 20)

    def test_result_from_another_project_is_refused(self):
        rows = [row('Foreign', 'k3', 'confirmed', 'autocontroller-repo-someone-else')]
        with self.standin(rows):
            with self.assertRaisesRegex(learning.MemoryError, 'unexpected observation scope'):
                learning.inspect(self.state, REPO, query='x', backend=self.backend())

    def test_non_list_search_response_is_refused(self):
        with self.standin({'not': 'a list'}):
            with self.assertRaisesRegex(learning.MemoryError, 'Invalid Engram search response'):
                learning.inspect(self.state, REPO, query='x', backend=self.backend())

    def test_unreachable_engram_raises_for_nonzero_exit(self):
        with self.assertRaises(learning.MemoryError):
            learning.inspect(self.state, REPO, query='x', backend=learning.Backend(self.tmp / 'absent.sock'))

    def test_unreadable_metrics_are_reported_not_fatal(self):
        (self.root / 'metrics.json').write_text('{not json')
        lines = learning.inspect(self.state, REPO, backend=self.backend())
        self.assertIn('operational counters: unreadable', lines)

    def test_missing_active_version_reports_unknown(self):
        (self.root / 'active.json').unlink()
        self.assertIn('engram version: unknown', learning.inspect(self.state, REPO, backend=self.backend()))

    def test_empty_query_is_refused_before_any_request(self):
        with self.standin([]) as engram:
            with self.assertRaises(ValueError):
                learning.inspect(self.state, REPO, query='', backend=self.backend())
            self.assertEqual(engram.requests, [])


class InspectCliTest(unittest.TestCase):
    """The CLI wiring: remove/inspect parse and refuse cleanly. Runs the real onboarding.py via subprocess."""

    def test_memory_subcommand_other_than_inspect_is_refused(self):
        import subprocess
        proc = subprocess.run([sys.executable, str(ROOT / 'onboarding.py'), 'memory', 'record'],
                              capture_output=True, text=True, timeout=120,
                              env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': tempfile.mkdtemp()})
        self.assertEqual(proc.returncode, 1)
        self.assertIn('must be "inspect"', proc.stderr)


if __name__ == '__main__':
    unittest.main()
