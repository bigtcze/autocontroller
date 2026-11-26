"""Controller-level follow-up scenarios (spec item 6a-6d, 6f), driven through ProjectWorker.one_iteration.

Offline. The FakeGitHub from test_followup_admission backs every gh call, so the #15/#6 story and
the idle wake-up run against one stateful board. run_wrapper is replaced by a recording double
that performs the CLI call itself, which is the only way the agent can create a follow-up.
"""
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import followup as f  # noqa: E402
from test_followup_admission import (  # noqa: E402
    FakeGitHub, FollowupBase, load_controller, make_worker, REPO, OWNER, PROJECT, PARENT, url_for,
)

m = load_controller()


class ControllerFollowupBase(FollowupBase):
    """Stateful board: #6 Todo, #12 active (In Progress) with checkpoint, #15 outside the Project."""

    def setUp(self):
        self._patchers = []
        super().setUp()
        self.gh.add_issue(REPO, 6, status='Todo', title='six')
        self.gh.add_issue(REPO, 12, status='In Progress', title='twelve')
        self.gh.add_issue(REPO, 15, status=None, in_project=False, title='outsider')
        self.gh.next_number = 200
        self.w = make_worker(self)
        self.patches = []
        self.wrapper_calls = []
        self.p(self.w, 'ensure_unsafe_group_gone')
        self.p(self.w, 'checkout_neutral', return_value=True)
        self.p(self.w, 'checkout_state', return_value=('main', False))
        self.p(self.w, 'quota_until', return_value=0)
        self.p(self.w, 'check_maintenance', return_value=None)
        self.p(self.w, 'bootstrap_gate', return_value=False)
        self.p(self.w, 'memory_gate')
        self.p(self.w, 'release_enabled', return_value=False)
        self.p(m, 'renovate_prs', return_value=[])
        self.p(m, 'issue_state', return_value='open')
        self.p(m, 'gh_json', side_effect=self.gh_json_fake)
        self.p(self.w.project, 'set_status', side_effect=self.set_status_fake)
        self.set_wrapper_result((0, 'AUTOCONTROLLER_NO_ACTION'))
        # Bootstrap is #5 for this repository; its completion proof makes ordinary work reachable.
        self.w.bootstrap_issue = 5
        self.w.standard_version = '1'
        self.w.bootstrap_done.parent.mkdir(parents=True, exist_ok=True)
        self.w.bootstrap_done.write_text(json.dumps({'issue': 5, 'version': '1', 'commit': 'b' * 40}))

    def p(self, obj, name, **kwargs):
        patcher = patch.object(obj, name, **kwargs)
        self._patchers.append((name, patcher))
        self.addCleanup(self._stop_patch, name)
        return patcher.start()

    def _stop_patch(self, name):
        for index, (key, patcher) in enumerate(self._patchers):
            if key == name:
                patcher.stop()
                del self._patchers[index]
                return

    def gh_json_fake(self, *args, **kwargs):
        """Controller-side gh_json(*args): routes the same argv the controller builds into FakeGitHub."""
        argv = list(args)
        if argv[:2] == ['api', 'user']:
            return {'login': self.gh.viewer_login, 'id': self.gh.viewer_id}
        if argv[:1] == ['project'] and argv[1] in ('item-list', 'view', 'field-list'):
            return json.loads(self.gh.command(['project', *argv[1:]]))
        if argv[0] == 'api' and len(argv) > 1 and str(argv[1]).startswith('repos/'):
            return json.loads(self.gh.command(['api', *argv[1:]]))
        if argv[:2] == ['api', 'graphql']:
            # Linked-PR lookup for an Issue task: this fake has no pull requests linked to any Issue.
            return {'data': {'repository': {'issue': {'closedByPullRequestsReferences': {'nodes': []}}}}}
        raise AssertionError(f'unfixtured GitHub read in controller test: {argv[:3]}')

    def renovate_prs_real(self, repo):
        """A verified Renovate PR, shaped exactly like the admission suite's renovate_pr()."""
        return [{'number': 31, 'state': 'open', 'merged_at': None, 'draft': False,
                 'user': {'login': 'renovate[bot]', 'type': 'Bot', 'id': 29139614},
                 'head': {'ref': 'renovate/pkg-31', 'sha': 'a' * 40, 'repo': {'full_name': REPO}},
                 'base': {'ref': 'main', 'repo': {'full_name': REPO}},
                 'html_url': url_for(REPO, 31), 'created_at': '2026-11-25T00:00:00Z'}]

    def set_status_fake(self, item, status):
        """Status moves land on the stateful board, the way GitHubProject.set_status would write them."""
        row = next(r for rows in self.gh.projects.values() for r in rows if r['url'] == item['url'])
        row['status'] = status

    def set_wrapper_result(self, result):
        self.agent = self.p(self.w, 'run_wrapper', side_effect=self.wrapper_effect(result))

    def wrapper_effect(self, result):
        def effect(prompt, kind, number, phase='code', timeout_seconds=None):
            self.wrapper_calls.append({'kind': kind, 'number': number, 'phase': phase, 'prompt': prompt})
            return result
        return effect

    def board_items(self):
        return self.w.project.items()


class FollowupAgentScenario(ControllerFollowupBase):
    """6a: #15/#6 story. Selection offers #6 only, the CLI creates a follow-up, then it is selectable."""

    def setUp(self):
        super().setUp()
        # Start from an idle board: no active task, no pending marker, no checkpoint.
        (self.pdir / 'cycle.pending').unlink(missing_ok=True)
        (self.pdir / 'cycle.json').unlink(missing_ok=True)
        (self.pdir / f.GRANT_FILE).unlink(missing_ok=True)

    def test_selection_offers_todo_six_and_never_outsider_fifteen(self):
        self.set_wrapper_result((0, 'AUTOCONTROLLER_NO_ACTION'))
        self.w.one_iteration()
        selection = [c for c in self.wrapper_calls if c['kind'] == 'selection']
        self.assertEqual(len(selection), 1)
        self.assertIn(url_for(REPO, 6), selection[0]['prompt'])
        self.assertNotIn(url_for(REPO, 15), selection[0]['prompt'])

    def test_active_twelve_creates_follow_up_blocking_six_then_both_are_offered(self):
        # Active task #12: pending marker, checkpoint, grant (as run_wrapper would have written them).
        (self.pdir / 'cycle.pending').write_text('{}', encoding='utf-8')
        (self.pdir / 'cycle.json').write_text(json.dumps(
            {'kind': 'issue', 'number': 12, 'url': url_for(REPO, 12), 'phase': 'code'}), encoding='utf-8')
        self.write_grant(parent=12)
        self.gh.repos[REPO][12]['state'] = 'open'
        result = self.create(self.payload(key='prereq', title='Prerequisite', reason='Blocks six',
                                          blocks=[6]))
        self.assertEqual(result['state'], 'done')
        # Exactly one new Issue, Todo in the configured Project, provenance footer, native link on #6.
        self.assertEqual(len(self.gh.created), 1)
        new_number = self.gh.created[0][1]
        body = self.gh.repos[REPO][new_number]['body']
        self.assertIn('Follow-up of #12: Blocks six', body)
        self.assertEqual(self.project_status(new_number), 'Todo')
        self.assertIn(new_number, self.gh.deps[(REPO, 6)])

        # Task finishes: remove checkpoint, pending marker and grant. Next iteration offers both.
        (self.pdir / 'cycle.pending').unlink()
        (self.pdir / 'cycle.json').unlink()
        (self.pdir / f.GRANT_FILE).unlink(missing_ok=True)
        self.set_wrapper_result((0, 'AUTOCONTROLLER_SELECT_ISSUE #%d' % new_number))
        self.w.one_iteration()
        selection = [c for c in self.wrapper_calls if c['kind'] == 'selection']
        self.assertTrue(selection)
        self.assertIn(url_for(REPO, 6), selection[-1]['prompt'])
        self.assertIn(url_for(REPO, new_number), selection[-1]['prompt'])
        self.assertNotIn(url_for(REPO, 15), selection[-1]['prompt'])
        # The selection of the follow-up dispatches it: In Progress, issue checkpoint for that number.
        self.assertEqual(self.project_status(new_number), 'In Progress')
        checkpoint = json.loads(self.w.cycle_state.read_text(encoding='utf-8'))
        self.assertEqual((checkpoint['kind'], checkpoint['number']), ('issue', new_number))
        # The next iteration runs the coding session for that Issue, not the blocked #6.
        self.w.one_iteration()
        issue_calls = [c for c in self.wrapper_calls if c['kind'] == 'issue']
        self.assertEqual([c['number'] for c in issue_calls], [new_number])

    def test_interrupted_item_add_completes_without_run_wrapper_or_second_create(self):
        (self.pdir / 'cycle.pending').write_text('{}', encoding='utf-8')
        (self.pdir / 'cycle.json').write_text(json.dumps(
            {'kind': 'issue', 'number': 12, 'url': url_for(REPO, 12), 'phase': 'code'}), encoding='utf-8')
        self.write_grant(parent=12)
        self.gh.fail['item-add'] = 1
        with self.assertRaises(f.FollowupError):
            self.create(self.payload(key='cut', title='Cut', reason='Interrupted'))
        self.assertEqual(self.journal()[0]['state'], 'created')
        created = self.gh.created[0][1]
        # Pending marker still present, no controller replay of the agent: the controller must not
        # call run_wrapper to complete admission.
        calls_before = len(self.wrapper_calls)
        (self.pdir / 'cycle.pending').unlink()
        (self.pdir / 'cycle.json').unlink()
        (self.pdir / f.GRANT_FILE).unlink(missing_ok=True)
        self.set_wrapper_result((0, 'AUTOCONTROLLER_NO_ACTION'))
        self.w.one_iteration()
        self.assertEqual(self.project_status(created), 'Todo')
        self.assertEqual(len(self.gh.created), 1, 'no second issue create')
        self.assertEqual([c['kind'] for c in self.wrapper_calls[calls_before:]], ['selection'],
                         'only the selection call; admission ran without a session of its own')

    def test_failed_follow_up_defers_selection_and_renovate_still_runs_first(self):
        self.gh.fail['item-add'] = 99
        (self.pdir / 'cycle.pending').write_text('{}', encoding='utf-8')
        (self.pdir / 'cycle.json').write_text(json.dumps(
            {'kind': 'issue', 'number': 12, 'url': url_for(REPO, 12), 'phase': 'code'}), encoding='utf-8')
        self.write_grant(parent=12)
        with self.assertRaises(f.FollowupError):
            self.create(self.payload(key='stuck'))
        (self.pdir / 'cycle.pending').unlink()
        (self.pdir / 'cycle.json').unlink()
        (self.pdir / f.GRANT_FILE).unlink(missing_ok=True)
        # A verified Renovate PR is handled before the follow-up gate and before selection.
        self.p(m, 'renovate_prs', side_effect=self.renovate_prs_real)
        self.set_wrapper_result((0, 'AUTOCONTROLLER_NO_ACTION'))
        self.w.one_iteration()
        self.assertEqual([c['kind'] for c in self.wrapper_calls], ['renovate'])
        # Renovate returns before the follow-up gate, so no retry is written yet; the entry is kept.
        self.assertEqual([e['key'] for e in self.journal() if e['state'] != 'done'], ['stuck'])

    def test_blank_status_bootstrap_still_runs_through_bootstrap_gate(self):
        # Bootstrap is an explicit exception: the gate dispatches it even when its Project status is blank.
        self.w.bootstrap_done.unlink(missing_ok=True)
        self.stack_bootstrap_stub_off()  # undo setUp's stub so the real bootstrap_gate runs
        self.gh.add_issue(REPO, 5, status='', title='bootstrap')
        dispatched = []
        self.p(self.w, '_cycle', side_effect=lambda kind, number, url, item: dispatched.append((kind, number)))
        self.w.bootstrap_gate()
        self.assertEqual(dispatched, [('issue', 5)])
        self.assertEqual(self.wrapper_calls, [], 'the gate dispatches a cycle; it does not start selection')

    def test_idle_board_change_wakes_one_selection_and_outsider_comment_does_not(self):
        # After NO_ACTION the board is idle until a new admitted follow-up appears.
        self.set_wrapper_result((0, 'AUTOCONTROLLER_NO_ACTION'))
        self.w.one_iteration()
        self.assertEqual([c['kind'] for c in self.wrapper_calls], ['selection'])
        self.w.one_iteration()  # idle: review window, unchanged signature, no second selection
        self.assertEqual([c['kind'] for c in self.wrapper_calls], ['selection'])
        # A new admitted follow-up is a board change and wakes exactly one new selection.
        (self.pdir / 'cycle.pending').write_text('{}', encoding='utf-8')
        (self.pdir / 'cycle.json').write_text(json.dumps(
            {'kind': 'issue', 'number': 12, 'url': url_for(REPO, 12), 'phase': 'code'}), encoding='utf-8')
        self.write_grant(parent=12)
        self.create(self.payload(key='wake', title='Wake', reason='Board change'))
        (self.pdir / 'cycle.pending').unlink()
        (self.pdir / 'cycle.json').unlink()
        (self.pdir / f.GRANT_FILE).unlink(missing_ok=True)
        self.w.one_iteration()
        self.assertEqual([c['kind'] for c in self.wrapper_calls].count('selection'), 2)
        self.assertEqual(self.wrapper_calls[-1]['kind'], 'selection')

    def test_reconciliation_never_calls_run_wrapper(self):
        (self.pdir / 'cycle.pending').write_text('{}', encoding='utf-8')
        (self.pdir / 'cycle.json').write_text(json.dumps(
            {'kind': 'issue', 'number': 12, 'url': url_for(REPO, 12), 'phase': 'code'}), encoding='utf-8')
        self.write_grant(parent=12)
        self.gh.fail['item-add'] = 1
        with self.assertRaises(f.FollowupError):
            self.create(self.payload(key='noai'))
        (self.pdir / 'cycle.pending').unlink()
        (self.pdir / 'cycle.json').unlink()
        (self.pdir / f.GRANT_FILE).unlink(missing_ok=True)
        self.set_wrapper_result((0, 'AUTOCONTROLLER_NO_ACTION'))
        self.w.followup_gate()
        self.assertEqual(self.wrapper_calls, [], 'reconciliation makes no AI call')

    def test_normal_cycle_without_journal_performs_no_admission_mutation(self):
        self.set_wrapper_result((0, 'AUTOCONTROLLER_NO_ACTION'))
        self.w.one_iteration()
        mutations = [c for c in self.gh.calls if c[:2] == ['project', 'item-add']
                     or c[:2] == ['issue', 'create']
                     or (('--method' in c) and c[c.index('--method') + 1] == 'POST')]
        self.assertEqual(mutations, [])
        self.assertFalse(self.journal())

    def stack_bootstrap_stub_off(self):
        # setUp patched bootstrap_gate to False for the ordinary-work tests; restore the real method.
        self._stop_patch('bootstrap_gate')

    def write_grant(self, **overrides):
        obj = {'token': 'a' * 48, 'repo': REPO, 'project_owner': OWNER, 'project_number': PROJECT,
               'parent': PARENT, 'kind': 'issue', 'project_dir': 'demo'}
        obj.update(overrides)
        (self.pdir / f.GRANT_FILE).write_text(json.dumps(obj), encoding='utf-8')

    def journal(self):
        path = self.pdir / f.JOURNAL_FILE
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding='utf-8'))['entries']

    def project_status(self, number):
        return self.gh.status_of(number)

    def payload(self, **overrides):
        obj = {'key': 'followup-a', 'title': 'Follow-up A', 'body': 'Details.', 'reason': 'Needed for X'}
        obj.update(overrides)
        return json.dumps(obj)

    def create(self, raw):
        return f.create(self.pdir / f.GRANT_FILE, 'a' * 48, raw, self.state)


class MaintenanceSuppressionTests(ControllerFollowupBase):
    """Non-blocking 7: a non-schedulable release Issue for the next tag must not suppress maintenance."""

    def due(self, items):
        w = self.w
        w.release_cfg = {'enabled': True, 'maintenance_days': 14, 'maintenance_min_renovate_prs': 3}
        w.release_cache = self.pdir / 'release-cache.json'
        self.p(w, 'release_enabled', return_value=True)
        self.p(w, 'release_latest', return_value={'tag_name': 'v1.2.4', 'published_at': '2026-10-01T00:00:00Z'})
        self.p(m.releases, 'released_since', return_value=60)
        self.p(m.releases, 'merged_since', return_value=[])
        self.p(m.releases, 'maintenance_due', return_value=True)
        self.p(m.releases, 'next_patch_tag', return_value='v1.2.5')
        # The base setUp stubs the instance's check_maintenance to None; call the real method
        # on this worker, and prove the release_enabled re-patch is the one in effect.
        self.assertTrue(w.release_enabled(), 'the release_enabled re-patch must be in effect')
        return m.ProjectWorker.check_maintenance(w, items)

    def release_item(self, status):
        return {'number': 40, 'title': 'v1.2.5 Release', 'status': status, 'item_id': 'x',
                'url': url_for(REPO, 40)}

    def test_backlog_release_issue_for_next_tag_does_not_suppress_maintenance(self):
        decision = self.due([self.release_item('Backlog')])
        self.assertIsNotNone(decision)
        self.assertEqual(decision['tag'], 'v1.2.5')

    def test_blank_release_issue_for_next_tag_does_not_suppress_maintenance(self):
        self.assertIsNotNone(self.due([self.release_item('')]))

    def test_todo_release_issue_for_next_tag_still_owns_the_version(self):
        self.assertIsNone(self.due([self.release_item('Todo')]))

    def test_in_progress_feature_release_still_suppresses_maintenance(self):
        self.assertIsNone(self.due([self.release_item('In Progress')]))


if __name__ == '__main__':
    unittest.main()
