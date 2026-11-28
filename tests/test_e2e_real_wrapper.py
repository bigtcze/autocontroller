"""E2E acceptance through the REAL safe wrapper (`opencode-project-safe.py`).

The controller's `run_wrapper` starts tests/e2e_real_wrapper.py, which runs the real wrapper code
unchanged. The wrapper talks to the fake OpenCode V2 CLI (tests/e2e_opencode.py). Only settle
timing is shortened (see the launcher's docstring). Every subprocess has a timeout.

Every test here runs with the memory runtime on (a stand-in Engram), so the controller issues
learning grants exactly as in production.
"""
import json

from test_e2e_controller import E2EBase, R, SHA_ISSUE


class RealWrapperBase(E2EBase):
    real_wrapper = True

    def setUp(self):
        super().setUp()
        self.world.start_engram()
        self.addCleanup(self.world.stop_engram)

    def rows(self, kind: str) -> list[dict]:
        path = self.world.e2e / 'invocations.jsonl'
        if not path.exists():
            return []
        parsed = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]
        return [r for r in parsed if r.get('kind') == kind]

    def pending_marker(self):
        return self.world.state / 'projects' / 'alpha' / 'cycle.pending'


class RealWrapperHappyPathTests(RealWrapperBase):
    def test_backlog_issue_through_the_real_wrapper_resumes_once_and_merges_exact_sha(self):
        self.world.add_project('alpha', R, 1)
        issue = self.gh.add_issue(R, title='Feature A', body='Implement A', project='example-owner/1',
                                  status='Backlog')
        self.world.script('alpha', [
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue},
            # The coding session opens the PR, and its real `oracle` subagent reviews that exact head tree.
            {'actions': [{'gh': ['gh', 'pr', 'create', '--repo', R, '--title', 'Feature A',
                                 '--body', f'Closes #{issue}', '--head-sha', SHA_ISSUE]}],
             'descendants': [{'title': 'review', 'agent': 'oracle', 'outcome': 'succeeded',
                              'snapshot_tree': 'head'}],
             'output': 'fixer completed; oracle reviewed'},
        ])
        self.drive('alpha', 1)  # selection -> In Progress
        self.assertEqual(self.gh.project_status('example-owner/1', issue), 'In Progress')
        self.drive('alpha', 1)  # coding session through the real wrapper opens the PR
        pr = next(p for p in self.gh.read()['repos'][R]['prs'].values()
                  if p['state'] == 'open' and p['head']['sha'] == SHA_ISSUE)
        self.gh.set_checks(R, pr['number'], SHA_ISSUE, [{'name': 'required', 'bucket': 'pending'}])
        self.drive('alpha', 1)
        self.gh.set_checks(R, pr['number'], SHA_ISSUE, [{'name': 'required', 'bucket': 'pass'}])
        self.drive('alpha', 2)  # exact-head green -> merge
        merged = self.gh.pr(R, pr['number'])
        self.assertEqual(merged['state'], 'closed')
        self.gh.set_run(R, merged['merge_commit_sha'], 'completed', 'success')
        self.drive('alpha', 1)

        self.assertIsNone(self.checkpoint())
        self.assertFalse(self.pending_marker().exists(), 'the wrapper marker is gone after completion')

        # One real-wrapper coding session with a guard title; one recovery turn into THAT session.
        code = self.sessions('code')
        self.assertEqual(len(code), 1)
        self.assertIn('[guard:', code[0]['title'])
        # The wrapper's `run` always ends with one recovery turn into its own session (selection too).
        resumes = self.rows('resume')
        code_resumes = [r for r in resumes if r['session_id'] == code[0]['session_id']]
        self.assertEqual(len(code_resumes), 1, 'exactly one resume turn into the coding session')
        self.assertEqual(code_resumes[0]['agent'], code[0]['agent'])
        self.assertEqual(code_resumes[0]['agent'], 'orchestrator')
        self.assertEqual(len(resumes), len(self.sessions()), 'one recovery turn per real-wrapper session')

        merges = self.merges()
        self.assertEqual(len(merges), 1)
        self.assertIn('--match-head-commit', merges[0])
        self.assertEqual(merges[0][-1], SHA_ISSUE)

    def test_learning_instructions_reach_the_coding_env_and_recovery_prompt_but_never_selection(self):
        self.world.add_project('alpha', R, 1)
        issue = self.gh.add_issue(R, title='Feature L', body='l', project='example-owner/1', status='Backlog')
        self.world.script('alpha', [
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue},
            {'output': 'coding done'},
        ])
        self.drive('alpha', 2)
        selection, code = self.sessions()
        self.assertEqual(selection['phase'], 'selection')
        # `learning_instructions` is computed inside the fake OpenCode process from the env the
        # real wrapper passed on, so it proves the session env path, not only the controller's.
        self.assertFalse(selection['learning_instructions'], 'selection never gets a learning grant')
        self.assertTrue(code['learning_instructions'], 'the coding session env carries the grant')
        self.assertIn('Passive learning', code['prompt'], 'the coding prompt carries the learning instruction')
        self.assertEqual(selection['env']['AUTONOMY_PHASE'], 'selection')
        self.assertEqual(code['env']['AUTONOMY_PHASE'], 'code')
        # Each recovery prompt is built from the same env as its session: the coding recovery keeps
        # the grant, the selection recovery never gets one.
        by_id = {r['session_id']: r for r in self.rows('resume')}
        self.assertTrue(by_id[code['session_id']]['learning_instructions'],
                        'the coding recovery prompt carries the learning grant')
        self.assertFalse(by_id[selection['session_id']]['learning_instructions'],
                         'the selection recovery prompt never carries a learning grant')


class RealWrapperUnstartedQuotaTests(RealWrapperBase):
    def test_unstarted_quota_is_cleared_by_the_real_wrapper_and_the_same_task_resumes_once(self):
        self.world.add_project('alpha', R, 1)
        issue = self.gh.add_issue(R, title='Feature Q', body='q', project='example-owner/1', status='Backlog')
        self.world.script('alpha', [
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue},
            # Quota before any OpenCode session exists: exit 1, no session record, marker left "starting".
            {'unstarted_quota': True, 'output': 'HTTP 429: quota reached', 'rc': 1},
        ])
        self.drive('alpha', 2)
        # The real wrapper exited 78 and left the marker; the controller verified and cleared it.
        self.assertFalse(self.pending_marker().exists(), 'an unstarted quota marker must be cleared')
        self.assertEqual(self.sessions('code'), [], 'quota before a session created no session')
        checkpoint = self.checkpoint()
        self.assertEqual((checkpoint['kind'], checkpoint['number']), ('issue', issue))
        self.assertTrue(checkpoint.get('retry_same_task'))
        self.assertGreater(json.loads((self.world.state / 'ai-cooldown.json').read_text())['until'], 0)

        # After the cooldown the SAME task resumes exactly once and completes.
        cooldown = json.loads((self.world.state / 'ai-cooldown.json').read_text())
        cooldown['until'] = 0
        (self.world.state / 'ai-cooldown.json').write_text(json.dumps(cooldown), encoding='utf-8')
        self.expire_retries('alpha')
        self.world.script('alpha', [{'output': 'coding done'}])
        self.drive('alpha', 1)
        code = self.sessions('code')
        self.assertEqual(len(code), 1, 'no duplicate session for the task')
        self.assertIn('[guard:', code[0]['title'])
        self.assertEqual(len(self.sessions('selection')), 1, 'quota recovery never reselects work')
        self.assertEqual(self.gh.project_status('example-owner/1', issue), 'In Progress')
        # The real --clear-unstarted-quota writes no invocation row; the controller records the
        # verified clear as its notice (written only after the wrapper returned 0).
        notices = json.loads((self.world.state / 'projects' / 'alpha' / 'notices.json').read_text())
        self.assertEqual(notices['unstarted-cleared']['message'],
                         'verified no OpenCode session exists; retrying original task')
