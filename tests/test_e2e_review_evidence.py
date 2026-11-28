"""E2E acceptance for independent oracle review evidence (issue #48).

(i) and (ii) use the scripted World (the wrapper is a stand-in; the recorded oracle review is an
explicit scripted step option). (iii) runs through the REAL safe wrapper and the fake OpenCode V2
store: the coding session's own `oracle` descendant carries the snapshot that the controller reads.

Review outcome and content are never inspected by the controller; these tests check identity,
freshness and independence only.
"""
from test_e2e_controller import E2EBase, R, SHA_ISSUE
from test_e2e_real_wrapper import RealWrapperBase

import e2e_world as ew

SHA_NEW = 'd7' * 20  # a later head pushed by the agent (fixer correction)


def open_issue_pr(gh, head: str) -> dict:
    return next(p for p in gh.read()['repos'][R]['prs'].values()
                if p['state'] == 'open' and p['head']['sha'] == head)


class ScriptedReviewEvidenceTests(E2EBase):
    """Scripted World: the controller's gate decides; the scripted reviews are explicit per step."""

    def setUp(self):
        super().setUp()
        self.world.add_project('alpha', R, 1)

    def add_renovate_ready(self, head: str):
        pr = self.gh.add_pr(R, title='Ready', body='', head_sha=head, head_ref='renovate/ready',
                            user=ew.eg.RENOVATE_USER)
        return pr

    # (i) non-draft PR, green required CI, no oracle review recorded -> no merge, then a review -> merge.
    def test_green_ci_without_oracle_review_is_not_merged_until_a_review_is_recorded(self):
        pr = self.add_renovate_ready(SHA_ISSUE)
        self.gh.set_checks(R, pr, SHA_ISSUE, [{'name': 'required', 'bucket': 'pass'}])
        # The coding session is scripted WITHOUT any oracle review: the controller must not merge it.
        self.world.script('alpha', [{'output': 'renovate verified'}])
        self.step_until('ci')
        self.restart().one_iteration()
        self.assertEqual(self.checkpoint()['phase'], 'code', 'missing evidence returns the task to code')
        self.assertIn('evidence', self.checkpoint()['review_feedback'])
        self.assertEqual(self.merges(), [])
        self.assertEqual(self.gh.pr(R, pr)['state'], 'open')

        # A later scripted session records an oracle review of the SAME head; the merge is the exact SHA.
        self.expire_retries('alpha')
        self.world.script('alpha', [
            {'output': 'oracle reviewed the head; no further change',
             'oracle_review': {'kind': 'renovate', 'number': pr, 'repo': R, 'pr': pr,
                               'session': 'ses_oracle_late'}}])
        self.step_until('postmerge')
        merges = self.merges()
        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0][-1], SHA_ISSUE)
        self.assertIn('--match-head-commit', merges[0])
        self.assertEqual(self.gh.pr(R, pr)['state'], 'closed')

    # (ii) review recorded on an old head; the agent pushes a new commit -> no merge until a review of the new tree.
    def test_review_of_an_old_head_does_not_merge_after_the_agent_pushes_a_new_commit(self):
        pr = self.add_renovate_ready(SHA_ISSUE)
        self.gh.set_checks(R, pr, SHA_ISSUE, [{'name': 'required', 'bucket': 'pass'}])
        # Oracle reviewed SHA_ISSUE (tree of SHA_ISSUE), the controller records it and moves to ci.
        self.world.script('alpha', [self._reviewed_head('old head reviewed', pr)])
        self.step_until('ci')
        self.restart().one_iteration()  # ci gate: the stored evidence covers SHA_ISSUE
        self.assertEqual(self.checkpoint()['review_evidence']['sha'], SHA_ISSUE)

        # Before merge, the agent pushes a correction on a new head (a different tree).
        self.gh.push_pr_commit(R, pr, SHA_NEW)
        self.gh.set_checks(R, pr, SHA_NEW, [{'name': 'required', 'bucket': 'pass'}])
        self.expire_retries('alpha')
        self.world.script('alpha', [{'output': 'fixer pushed correction, no new review'}])
        self.step_until('code')
        self.assertEqual(self.merges(), [], 'the old review does not cover the new head')
        self.assertNotIn('review_evidence', self.checkpoint())

        # Only a review of the new tree satisfies the gate; then the merge uses the new SHA.
        self.expire_retries('alpha')
        self.world.script('alpha', [self._reviewed_head('new tree reviewed', pr)])
        self.step_until('postmerge')
        merges = self.merges()
        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0][-1], SHA_NEW)

    def _reviewed_head(self, output: str, pr: int) -> dict:
        return {'output': output,
                'oracle_review': {'kind': 'renovate', 'number': pr, 'repo': R, 'pr': pr,
                                  'session': f'ses_oracle_{pr}_{self.gh.pr(R, pr)["head"]["sha"][:6]}'}}


class RealWrapperReviewEvidenceTests(RealWrapperBase):
    """(iii) The real wrapper: the coding session's own oracle descendant decides whether merge may happen."""

    def setUp(self):
        super().setUp()
        self.world.add_project('alpha', R, 1)
        self.issue = self.gh.add_issue(R, title='Feature A', body='Implement A',
                                       project='example-owner/1', status='Backlog')

    def _script_with_oracle(self, descendant: dict):
        self.world.script('alpha', [
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % self.issue},
            {'actions': [{'gh': ['gh', 'pr', 'create', '--repo', R, '--title', 'Feature A',
                                 '--body', f'Closes #{self.issue}', '--head-sha', SHA_ISSUE]}],
             'descendants': [descendant],
             'output': 'fixer completed; oracle reviewed'},
        ])

    def _run_to_ci_then_green(self):
        self.drive('alpha', 1)  # selection
        self.drive('alpha', 1)  # coding through the real wrapper opens the PR
        pr = next(p for p in self.gh.read()['repos'][R]['prs'].values()
                  if p['state'] == 'open' and p['head']['sha'] == SHA_ISSUE)
        self.gh.set_checks(R, pr['number'], SHA_ISSUE, [{'name': 'required', 'bucket': 'pass'}])
        return pr['number']

    def test_real_wrapper_oracle_descendant_on_the_head_tree_allows_exact_sha_merge(self):
        self._script_with_oracle({'title': 'review', 'agent': 'oracle', 'outcome': 'succeeded',
                                  'snapshot_tree': 'head'})
        number = self._run_to_ci_then_green()
        self.drive('alpha', 3)
        merges = self.merges()
        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0][-1], SHA_ISSUE)
        self.assertIn('--match-head-commit', merges[0])
        self.assertEqual(self.gh.pr(R, number)['state'], 'closed')

    def test_real_wrapper_interrupted_oracle_descendant_never_allows_merge(self):
        self._script_with_oracle({'title': 'review', 'agent': 'oracle', 'outcome': 'interrupted',
                                  'snapshot_tree': 'head'})
        number = self._run_to_ci_then_green()
        self.drive('alpha', 3)
        self.assertEqual(self.merges(), [], 'an interrupted oracle session is not review evidence')
        self.assertEqual(self.gh.pr(R, number)['state'], 'open')
        self.assertEqual(self.checkpoint()['phase'], 'code')

    def test_real_wrapper_oracle_snapshot_on_a_different_tree_never_allows_merge(self):
        self._script_with_oracle({'title': 'review', 'agent': 'oracle', 'outcome': 'succeeded',
                                  'snapshot_tree': '5' * 40})
        number = self._run_to_ci_then_green()
        self.drive('alpha', 3)
        self.assertEqual(self.merges(), [], 'a snapshot of another tree is not review of this head')
        self.assertEqual(self.gh.pr(R, number)['state'], 'open')
        self.assertEqual(self.checkpoint()['phase'], 'code')


if __name__ == '__main__':
    import unittest
    unittest.main()
