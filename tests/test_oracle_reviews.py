"""Read-only `--oracle-reviews` evidence listing in the safe wrapper (stubbed OpenCode API, no network)."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

WRAPPER = Path(__file__).resolve().parents[1] / 'opencode-project-safe.py'
PROJECT = 'demo'
ROOT_TITLE = 'demo kind #5 [guard:1-2]'


def load_wrapper(tmp):
    env = {'AUTONOMY_PROJECT_ROOT': tmp, 'AUTONOMY_PROJECT_NAME': PROJECT,
           'AUTONOMY_PENDING_MARKER': tmp + '/pending.json', 'AUTONOMY_OPENCODE': '/bin/true'}
    with patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location('oracle_wrapper', str(WRAPPER))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class FakeApi:
    """Routes the wrapper's OpenCode calls to in-memory pages; unknown paths fail like the real CLI."""

    def __init__(self, root, sessions, messages=None, pages=None, fail=None, active=()):
        self.root = root
        self.active = set(active)
        self.sessions = sessions      # id -> session dict
        self.messages = messages or {}  # session id -> list of message pages (list of lists)
        self.pages = pages or {}      # query fragment -> list of session pages
        self.fail = fail or set()
        self.calls = []

    @staticmethod
    def page_index(cursor):
        return 0 if cursor is None else int(cursor.lstrip('c'))

    def __call__(self, method, path, *, envelope=False):
        self.calls.append(path)
        if any(f in path for f in self.fail):
            raise RuntimeError(f'OpenCode API failed: {method} {path}: unavailable')
        if path == '/api/session/active':
            return {sid: {'status': 'busy'} for sid in self.active}
        if path.startswith('/api/session?'):
            for key, page_list in self.pages.items():
                if key in path:
                    # Pages are served in order; a page's `cursor.next` is the next request's cursor.
                    cursor = path.split('&cursor=')[1] if '&cursor=' in path else None
                    data = page_list[self.page_index(cursor)]
                    return data if envelope else data['data']
            return {'data': [], 'cursor': {'next': None}} if envelope else []
        if path.endswith('/message') or '/message?' in path:
            sid = path.split('/')[3]
            cursor = path.split('?cursor=')[1] if '?cursor=' in path else None
            page = self.messages[sid][self.page_index(cursor)]
            return page if envelope else page['data']
        sid = path.split('/')[3]
        return self.sessions[sid]


def root(sid, title=ROOT_TITLE, directory=None, parent=None):
    return {'id': sid, 'title': title, 'parentID': parent,
            'location': {'directory': directory or '/tmp/unused'}}


def child(sid, agent='oracle', outcome='succeeded', created=1, idle=2):
    return {'id': sid, 'parentID': 'ses_root', 'agent': agent, 'outcome': outcome,
            'time': {'created': created, 'idle': idle}}


def assistant(start, end):
    return {'type': 'assistant', 'agent': 'oracle', 'snapshot': {'start': start, 'end': end, 'files': []}}


class OracleReviewsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.mod = load_wrapper(self.tmp)

    def run_listing(self, api):
        with patch.object(self.mod, 'opencode_api', side_effect=api), \
                patch.object(self.mod, 'verify_v2_service', return_value=None):
            return self.mod.oracle_reviews('kind', 5)

    def base_sessions(self, **extra):
        sessions = {'ses_root': root('ses_root', directory=self.tmp)}
        sessions.update(extra)
        return sessions

    def test_exact_oracle_child_reports_trees_and_outcome(self):
        sessions = self.base_sessions(ses_o=child('ses_o'))
        sessions['ses_o'] = dict(sessions['ses_o'], location={'directory': self.tmp})
        api = FakeApi(self.tmp, sessions,
                      messages={'ses_o': [{'data': [assistant('T1', 'T1')]}]},
                      pages={'parentID=null': [{'data': [root('ses_root', directory=self.tmp)], 'cursor': {'next': None}}],
                             'parentID=ses_root': [{'data': [child('ses_o')], 'cursor': {'next': None}}]})
        result = self.run_listing(api)
        self.assertEqual(len(result['candidates']), 1)
        candidate = result['candidates'][0]
        self.assertEqual(candidate['session'], 'ses_o')
        self.assertEqual(candidate['outcome'], 'succeeded')
        self.assertEqual(candidate['trees'], ['T1'])
        self.assertTrue(candidate['snapshot_complete'])

    def test_title_and_directory_mismatch_roots_are_excluded(self):
        other_dir = str(Path(self.tmp).parent / 'other')
        subdir = self.tmp + '/sub'
        sessions = {'ses_bad_title': root('ses_bad_title', title='demo kind #5 extra [guard:1-2]', directory=self.tmp),
                    'ses_bad_dir': root('ses_bad_dir', directory=other_dir),
                    'ses_subdir': root('ses_subdir', directory=subdir)}
        api = FakeApi(self.tmp, sessions,
                      pages={'parentID=null': [{'data': [root('ses_bad_title', title='demo kind #5 extra [guard:1-2]', directory=self.tmp),
                                                         root('ses_bad_dir', directory=other_dir),
                                                         root('ses_subdir', directory=subdir)],
                                                'cursor': {'next': None}}]})
        result = self.run_listing(api)
        self.assertEqual(result['candidates'], [])
        self.assertFalse(any('parentID=ses' in p for p in api.calls))

    def test_nested_and_non_oracle_children_are_excluded(self):
        sessions = self.base_sessions(ses_fixer=child('ses_fixer', agent='fixer'),
                                      ses_o=child('ses_o'))
        api = FakeApi(self.tmp, sessions,
                      messages={'ses_o': [{'data': [assistant('T1', 'T1')]}]},
                      pages={'parentID=null': [{'data': [root('ses_root', directory=self.tmp)], 'cursor': {'next': None}}],
                             'parentID=ses_root': [{'data': [child('ses_fixer', agent='fixer'), child('ses_o')],
                                                    'cursor': {'next': None}}]})
        result = self.run_listing(api)
        self.assertEqual([c['session'] for c in result['candidates']], ['ses_o'])

    def test_running_oracle_is_reported_active(self):
        sessions = self.base_sessions(ses_o=child('ses_o'))
        api = FakeApi(self.tmp, sessions, active={'ses_o'},
                      messages={'ses_o': [{'data': [assistant('T1', 'T1')]}]},
                      pages={'parentID=null': [{'data': [root('ses_root', directory=self.tmp)], 'cursor': {'next': None}}],
                             'parentID=ses_root': [{'data': [child('ses_o')], 'cursor': {'next': None}}]})
        self.assertTrue(self.run_listing(api)['candidates'][0]['active'])

    def test_missing_snapshot_marks_candidate_incomplete(self):
        sessions = self.base_sessions(ses_o=child('ses_o'))
        api = FakeApi(self.tmp, sessions,
                      messages={'ses_o': [{'data': [{'type': 'assistant', 'agent': 'oracle'}]}]},
                      pages={'parentID=null': [{'data': [root('ses_root', directory=self.tmp)], 'cursor': {'next': None}}],
                             'parentID=ses_root': [{'data': [child('ses_o')], 'cursor': {'next': None}}]})
        result = self.run_listing(api)
        self.assertFalse(result['candidates'][0]['snapshot_complete'])

    def test_mixed_snapshot_trees_are_all_reported(self):
        sessions = self.base_sessions(ses_o=child('ses_o'))
        api = FakeApi(self.tmp, sessions,
                      messages={'ses_o': [{'data': [assistant('T1', 'T1'), assistant('T2', 'T2')]}]},
                      pages={'parentID=null': [{'data': [root('ses_root', directory=self.tmp)], 'cursor': {'next': None}}],
                             'parentID=ses_root': [{'data': [child('ses_o')], 'cursor': {'next': None}}]})
        result = self.run_listing(api)
        self.assertEqual(result['candidates'][0]['trees'], ['T1', 'T2'])

    def test_root_pagination_follows_cursor(self):
        sessions = self.base_sessions(ses_o=child('ses_o'))
        api = FakeApi(self.tmp, sessions,
                      messages={'ses_o': [{'data': [assistant('T1', 'T1')]}]},
                      pages={'parentID=null': [
                                 {'data': [], 'cursor': {'next': 'c1'}},
                                 {'data': [root('ses_root', directory=self.tmp)], 'cursor': {'next': None}}],
                             'parentID=ses_root': [{'data': [child('ses_o')], 'cursor': {'next': None}}]})
        result = self.run_listing(api)
        self.assertEqual(len(result['candidates']), 1)
        self.assertTrue(any('&cursor=c1' in p for p in api.calls))

    def test_page_cap_fails_closed(self):
        pages = [{'data': [], 'cursor': {'next': f'c{i}'}} for i in range(1, 30)]
        api = FakeApi(self.tmp, self.base_sessions(), pages={'parentID=null': pages})
        with self.assertRaisesRegex(RuntimeError, 'page cap'):
            self.run_listing(api)

    def test_api_error_fails_closed(self):
        api = FakeApi(self.tmp, self.base_sessions(), fail={'parentID=null'})
        with self.assertRaises(RuntimeError):
            self.run_listing(api)

    def test_cli_api_error_exits_nonzero_with_message(self):
        import subprocess
        import sys
        env = {**os.environ, 'AUTONOMY_PROJECT_ROOT': self.tmp, 'AUTONOMY_PROJECT_NAME': PROJECT,
               'AUTONOMY_PENDING_MARKER': self.tmp + '/pending.json', 'AUTONOMY_OPENCODE': '/bin/false'}
        proc = subprocess.run([sys.executable, str(WRAPPER), '--oracle-reviews', 'kind', '5'],
                              env=env, cwd=self.tmp, text=True, capture_output=True, timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('ORACLE REVIEWS UNAVAILABLE', proc.stderr)
        self.assertEqual(proc.stdout, '')


if __name__ == '__main__':
    unittest.main()
