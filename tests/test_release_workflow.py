"""Offline contract tests for .github/workflows/release.yml.

Text-level checks, in the style of test_repository_contracts.py: they cover the publish
gates (`needs:`), least-privilege job scopes, job timeouts, checkout credential handling
and the four distribution images. Job keys are read from the workflow, so a new job is
covered by the timeout and permission checks without editing this file.
"""
import re
import unittest
from pathlib import Path

from test_repository_contracts import job_block, workflow_text

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ('debian:12', 'debian:13', 'ubuntu:24.04', 'ubuntu:26.04')


def job_keys(text):
    return re.findall(r'(?m)^  ([a-z-]+):$', text.split('\njobs:\n', 1)[1])


class ReleaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = workflow_text('release.yml')
        cls.jobs = job_keys(cls.text)

    def test_every_job_has_a_timeout(self):
        self.assertTrue(self.jobs, 'no jobs parsed from release.yml')
        self.assertIn('publish', self.jobs)
        for job in self.jobs:
            with self.subTest(job=job):
                if job == 'qualify':
                    # Reusable workflow call: timeouts live on the called regression.yml jobs.
                    self.assertIn('uses: ./.github/workflows/regression.yml', job_block(self.text, job))
                    continue
                self.assertRegex(job_block(self.text, job), r'timeout-minutes: \d+')

    def test_only_publish_can_write_contents(self):
        self.assertIn('publish', self.jobs)
        for job in self.jobs:
            block = job_block(self.text, job)
            with self.subTest(job=job):
                if job == 'publish':
                    self.assertIn('contents: write', block)
                else:
                    self.assertNotIn('contents: write', block)

    def test_only_github_token_is_used_as_a_secret(self):
        self.assertEqual(set(re.findall(r'secrets\.([A-Za-z_]+)', self.text)), set())

    def test_checkout_does_not_persist_credentials(self):
        for job in ('candidate', 'artifacts', 'publish'):
            block = job_block(self.text, job)
            with self.subTest(job=job):
                self.assertIn('persist-credentials: false', block)

    def test_candidate_runs_release_qualification_after_qualify(self):
        block = job_block(self.text, 'candidate')
        self.assertRegex(block, r'needs: \[qualify\]')
        self.assertIn('release_artifacts.py qualify', block)
        self.assertIn('--sha "$SHA"', block)

    def test_candidate_refuses_tags_that_are_not_on_main(self):
        block = job_block(self.text, 'candidate')
        self.assertIn('git merge-base --is-ancestor "$GITHUB_SHA" origin/main', block)

    def test_artifacts_build_in_all_four_distribution_images_with_pass_through_env(self):
        block = job_block(self.text, 'artifacts')
        for image in IMAGES:
            with self.subTest(image=image):
                self.assertIn(image, block)
        self.assertIn('--env TAG --env COMMIT', block)
        self.assertIn('release_artifacts.py build', block)
        self.assertIn('release_artifacts.py verify', block)
        self.assertIn('cmp "$first/SHA256SUMS" "$dir/SHA256SUMS"', block)

    def test_artifacts_job_has_no_write_scope_and_exports_only_a_digest(self):
        block = job_block(self.text, 'artifacts')
        self.assertIn('contents: read', block)
        self.assertRegex(block, r'outputs:\n\s+sha256: \$\{\{ steps\.digest\.outputs\.sha256 \}\}')

    def test_publish_waits_for_qualify_and_artifacts(self):
        self.assertRegex(job_block(self.text, 'publish'), r'needs: \[qualify, artifacts\]')

    def test_publish_rebuilds_and_compares_with_the_artifacts_digest(self):
        block = job_block(self.text, 'publish')
        self.assertIn('needs.artifacts.outputs.sha256', block)
        self.assertIn('release_artifacts.py build', block)

    def test_publish_job_scopes_are_exactly_contents_write_and_issues_read(self):
        # N2: the blocker re-check reads Issues, so publish needs issues: read; nothing broader.
        block = job_block(self.text, 'publish')
        scopes = re.search(r'(?m)^    permissions:\n((?:      \S.*\n)+)', block)
        self.assertIsNotNone(scopes, 'publish job has no permissions block')
        self.assertEqual(sorted(l.strip() for l in scopes.group(1).splitlines()),
                         ['contents: write', 'issues: read'])

    def test_publish_job_delegates_to_the_python_publish_command(self):
        block = job_block(self.text, 'publish')
        self.assertIn('release_artifacts.py publish --repo "$GH_REPO"', block)
        self.assertNotIn('gh api', block)
        self.assertNotIn('--slurp', self.text)
        self.assertNotIn('gh release create', self.text)

    def test_release_is_never_created_as_prerelease(self):
        self.assertNotIn('--prerelease', self.text)
        self.assertNotIn('--prerelease', (ROOT / 'release_artifacts.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
