"""Independent oracle review evidence gate (issue #48) over the real ProjectWorker.

Only the GitHub and OpenCode seams are replaced: the git-tree lookup, the wrapper listing call
(`--oracle-reviews`) and the gh/CI reads. The controller's own gate, routing and merge code run
unmodified. Review outcome and content are never inspected; the listing candidates below are
native OpenCode metadata only.
"""
import importlib.util
import json
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('review_gate_controller', str(Path(__file__).resolve().parents[1] / 'autonomy-controller.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

SHA = 'a' * 40          # reviewed PR head
NEW = 'b' * 40          # later head (fixer correction)
TREE1 = '1' * 40
TREE2 = '2' * 40
URL = 'https://github.com/org/demo/issues/68'
REAL_RUN = subprocess.run


def cand(**overrides):
    base = {'root': 'ses_root', 'session': 'ses_oracle', 'created': 1, 'idle': 2, 'outcome': 'succeeded',
            'active': False, 'trees': [TREE1], 'snapshot_complete': True}
    base.update(overrides)
    return base


class ReviewGate(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.w = m.ProjectWorker({'name': 'demo', 'repo': 'org/demo', 'directory': str(root / 'repo'),
                                  'project_owner': 'org', 'project_number': 2},
                                 {'state_dir': str(root / 'state'), 'opencode': 'oc', 'wrapper': 'wrapper'},
                                 threading.Event(), threading.Lock())
        self.w.pdir.mkdir(parents=True)
        self.pr = {'number': 94, 'state': 'open', 'base': {'ref': 'main'}, 'head': {'sha': SHA},
                   'body': 'original', 'draft': False}
        self.trees = {SHA: TREE1, NEW: TREE2}
        self.candidates = []
        self.merges = []
        self.lookups = []
        self.prompts = []
        self.on_agent = lambda: None
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(self.w, 'related_pr', side_effect=lambda *a: dict(self.pr)))
        stack.enter_context(patch.object(m, 'pr_data', side_effect=lambda *a: dict(self.pr)))
        stack.enter_context(patch.object(self.w, 'checkout_state', return_value=('main', False)))
        stack.enter_context(patch.object(m, 'command', side_effect=self._command))
        stack.enter_context(patch.object(m, 'pr_ci_state', return_value='passed'))
        stack.enter_context(patch.object(m, 'issue_state', return_value='closed'))
        stack.enter_context(patch.object(m, 'main_ci_state', return_value='passed'))
        stack.enter_context(patch.object(self.w.project, 'items', return_value=[]))
        stack.enter_context(patch.object(m, 'gh_json', side_effect=self._gh_json))
        stack.enter_context(patch.object(m.subprocess, 'run', side_effect=self._run))
        stack.enter_context(patch.object(self.w, 'run_wrapper', side_effect=self._agent))

    # ----- seams ---------------------------------------------------------------
    def _command(self, argv, **kwargs):
        if argv[:3] == ['gh', 'pr', 'merge']:
            self.merges.append(argv)
            self.pr.update(state='closed', merged_at='2026-11-26T12:00:00Z', merge_commit_sha='m' * 40)
        return ''

    def _gh_json(self, *args, timeout=180):
        path = args[1]
        head = path.rsplit('/', 1)[-1]
        if path.startswith('repos/org/demo/git/commits/') and head in self.trees:
            return {'tree': {'sha': self.trees[head]}}
        raise AssertionError(f'unmodelled gh_json call: {args}')

    def _run(self, argv, *args, **kwargs):
        if '--oracle-reviews' in argv:
            self.lookups.append(list(argv[2:]))
            payload = {'kind': 'issue', 'number': 68, 'candidates': list(self.candidates)}
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), '')
        return REAL_RUN(argv, *args, **kwargs)

    def _agent(self, prompt, *args, **kwargs):
        self.prompts.append(prompt)
        self.on_agent()
        return 0, 'agent pass'

    # ----- helpers -------------------------------------------------------------
    def checkpoint(self, phase, **extra):
        cp = {'kind': 'issue', 'number': 68, 'url': URL, 'phase': phase, 'pr_number': 94,
              'retry_same_task': True, **extra}
        self.w.save_checkpoint(cp)
        return cp

    def cp(self):
        return m.json_read(self.w.cycle_state, None)

    def cycle(self):
        self.w._cycle('issue', 68, URL, None)

    def finish(self):
        for _ in range(8):
            if not self.w.cycle_state.exists():
                return
            self.cycle()
        self.fail('checkpoint did not complete')

    def stored(self, sha=SHA, tree=TREE1):
        return {'sha': sha, 'tree': tree, 'session': 'ses_oracle', 'root': 'ses_root'}

    # ----- (a) green CI without evidence ----------------------------------------
    def test_a_green_ci_without_review_evidence_never_reaches_ready_or_merge(self):
        self.candidates = []
        self.checkpoint('ci')
        self.cycle()
        cp = self.cp()
        self.assertEqual(cp['phase'], 'code')
        self.assertNotIn('ready_sha', cp)
        self.assertNotIn('review_evidence', cp)
        self.assertEqual(self.merges, [])
        self.assertIn('oracle', cp['review_feedback'])
        self.assertIn('evidence', cp['review_feedback'])
        self.assertIn('issue:68', self.w.retry_map())
        self.assertEqual(len(self.lookups), 1)

    # ----- (b) stale evidence for an old head ------------------------------------
    def test_b_evidence_for_old_head_is_not_reused_after_head_change(self):
        self.candidates = [cand()]  # oracle reviewed TREE1 only
        self.checkpoint('ci', review_evidence=self.stored(SHA, TREE1))
        self.pr['head']['sha'] = NEW  # tree TREE2
        self.cycle()
        cp = self.cp()
        self.assertEqual(cp['phase'], 'code')
        self.assertEqual(len(self.lookups), 1, 'a new head requires a fresh lookup')
        self.assertNotIn('review_evidence', cp)
        self.assertEqual(self.merges, [])

    # ----- (c) rejected candidates ------------------------------------------------
    def test_c_non_successful_incomplete_or_foreign_tree_candidates_are_rejected(self):
        variants = {
            'interrupted': dict(outcome='interrupted'),
            'failed': dict(outcome='failed'),
            'no_outcome_while_running': dict(outcome=None),
            'other_tree': dict(trees=[TREE2]),
            'mixed_trees': dict(trees=sorted([TREE1, TREE2])),
            'incomplete_snapshot': dict(snapshot_complete=False),
            'still_active': dict(active=True),
            'not_idle': dict(idle=None),
        }
        for name, override in variants.items():
            with self.subTest(variant=name):
                self.pr['head']['sha'] = SHA
                self.candidates = [cand(**override)]
                self.checkpoint('ci')
                self.cycle()
                self.assertEqual(self.cp()['phase'], 'code')
                self.assertNotIn('review_evidence', self.cp())
                self.assertEqual(self.merges, [])

    def test_c_wrapper_listing_failure_keeps_checkpoint_and_does_not_route_to_code(self):
        self.checkpoint('ci')
        with patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 2, '', 'api down')):
            with self.assertRaises(m.ControllerError):
                self.cycle()
        self.assertEqual(self.cp()['phase'], 'ci')
        self.assertEqual(self.merges, [])

    # ----- (d) successful reviewed candidate ---------------------------------------
    def test_d_successful_reviewed_candidate_reaches_merge_with_exact_head(self):
        self.candidates = [cand(session='ses_old', idle=2), cand(session='ses_new', idle=9)]
        self.checkpoint('ci')
        self.cycle()
        cp = self.cp()
        self.assertEqual(cp['phase'], 'merge')
        self.assertEqual(cp['ready_sha'], SHA)
        self.assertEqual(cp['review_evidence']['session'], 'ses_new', 'latest completed candidate wins')
        self.assertEqual(cp['review_evidence']['sha'], SHA)
        self.finish()
        self.assertEqual(len(self.merges), 1)
        self.assertIn('--match-head-commit', self.merges[0])
        self.assertEqual(self.merges[0][self.merges[0].index('--match-head-commit') + 1], SHA)
        self.assertEqual(self.lookups, [['--oracle-reviews', 'issue', '68']])

    # ----- (e) description-only and identical-tree commits -------------------------
    def test_e_description_only_edit_at_same_head_needs_no_new_lookup(self):
        self.candidates = []  # no oracle at all: the stored evidence alone must suffice
        self.checkpoint('ci', review_evidence=self.stored(SHA, TREE1))
        self.pr['body'] = 'corrected wording only'
        self.finish()
        self.assertEqual(self.lookups, [])
        self.assertEqual(len(self.merges), 1)

    def test_e_new_commit_with_identical_tree_is_accepted_by_existing_review(self):
        self.trees[NEW] = TREE1  # same git tree, new commit
        self.pr['head']['sha'] = NEW
        self.candidates = [cand()]
        self.checkpoint('ci', review_evidence=self.stored(SHA, TREE1))
        self.finish()
        self.assertEqual(len(self.lookups), 1)
        self.assertEqual(self.merges[0][-1], NEW)

    # ----- (f) restart and legacy checkpoints -------------------------------------
    def test_f_restart_with_stored_evidence_for_current_head_skips_lookup(self):
        self.candidates = []  # sessions cleaned up: stored evidence is the only proof
        self.checkpoint('ci', review_evidence=self.stored(SHA, TREE1))
        self.finish()
        self.assertEqual(self.lookups, [])
        self.assertEqual(len(self.merges), 1)

    def test_f_legacy_merge_checkpoint_without_evidence_fails_closed_without_ai_or_lookup(self):
        self.candidates = [cand()]  # an accepted candidate exists but is not consulted at merge
        self.checkpoint('merge', ready_sha=SHA)
        self.cycle()
        cp = self.cp()
        self.assertEqual(cp['phase'], 'code')
        self.assertNotIn('ready_sha', cp)
        self.assertEqual(self.merges, [])
        self.assertEqual(self.lookups, [])
        self.assertIn('evidence', cp['review_feedback'])

    # ----- (g) repeated missing evidence -----------------------------------------
    def test_g_repeated_missing_evidence_at_same_head_is_blocker_with_hourly_retry(self):
        self.candidates = []
        self.checkpoint('ci')
        self.cycle()
        self.assertEqual(self.cp().get('review_missing_head'), SHA)
        first = self.w.retry_map()['issue:68']['until'] - m.time.time()
        self.assertLess(first, 3000, 'first miss uses the short retry')
        cp = self.cp()
        cp['phase'] = 'ci'
        self.w.save_checkpoint(cp)
        self.cycle()
        retry = self.w.retry_map()['issue:68']
        self.assertGreater(retry['until'] - m.time.time(), 3000, 'repeat at the same head waits an hour')
        notices = json.loads(self.w.notices_file.read_text())
        self.assertIn('BLOCKER', notices['review-missing:issue:68']['message'])
        self.assertEqual(self.merges, [])
        self.assertEqual(len(self.lookups), 2)

    # ----- (h) negative completed review, fixer correction, new tree ---------------
    def test_h_review_of_old_tree_is_invalid_after_fixer_changes_the_tree(self):
        # The old review is recorded for TREE1 (its outcome is never judged here).
        self.candidates = [cand(session='ses_on_tree1', trees=[TREE1])]
        self.checkpoint('ci')
        self.cycle()
        self.assertEqual(self.cp()['phase'], 'merge')
        # The fixer pushes a correction: the head moves to TREE2 while the merge is pending.
        self.pr['head']['sha'] = NEW
        self.cycle()
        cp = self.cp()
        self.assertEqual(cp['phase'], 'code')
        self.assertNotIn('review_evidence', cp)
        self.assertEqual(self.merges, [])
        # Agent pass for the correction; then the ci gate finds no review of TREE2 and returns to code.
        self.cycle()
        self.assertEqual(self.cp()['phase'], 'ci')
        self.cycle()
        self.assertEqual(self.cp()['phase'], 'code')
        self.assertEqual(self.merges, [])
        self.assertEqual(self.lookups[-1], ['--oracle-reviews', 'issue', '68'])
        # Only a review of the new tree satisfies the gate.
        self.candidates = [cand(session='ses_on_tree2', trees=[TREE2])]
        self.finish()
        self.assertEqual(len(self.merges), 1)
        self.assertEqual(self.merges[0][-1], NEW)

    # ----- invalidation points -----------------------------------------------------
    def test_divergence_routing_drops_stored_evidence(self):
        self.candidates = []
        self.checkpoint('ci', review_evidence=self.stored(SHA, TREE1))
        self.pr['mergeable_state'] = 'behind'
        self.cycle()
        cp = self.cp()
        self.assertEqual(cp['phase'], 'code')
        self.assertNotIn('review_evidence', cp)
        self.assertEqual(self.lookups, [])

    def test_head_change_at_merge_drops_evidence_and_ready_sha(self):
        self.checkpoint('merge', ready_sha=SHA, review_evidence=self.stored(SHA, TREE1))
        self.pr['head']['sha'] = NEW
        self.cycle()
        cp = self.cp()
        self.assertEqual(cp['phase'], 'code')
        self.assertNotIn('review_evidence', cp)
        self.assertNotIn('ready_sha', cp)
        self.assertEqual(self.merges, [])

    def test_coding_prompt_asks_for_final_oracle_review_on_the_exact_head_tree(self):
        self.candidates = []
        self.checkpoint('code')
        self.cycle()
        self.assertTrue(self.prompts)
        self.assertIn('exact final PR head', self.prompts[0].replace('\n', ' '))
        self.assertIn('`oracle`', self.prompts[0])


if __name__ == '__main__':
    unittest.main()
