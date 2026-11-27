"""Repository contract tests: text-level, stdlib only, no network, no git.

Deliberately simple: workflows are checked by text patterns rather than a YAML
parser so the suite stays dependency-free.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import unittest

import bootstrap_policy

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {'.git', '__pycache__', '.pytest_cache'}
WORKFLOWS = ROOT / '.github' / 'workflows'
PERSONAL_PATH = re.compile(r'/home/(?!example(?![A-Za-z0-9._-]))[A-Za-z0-9._-]+')
TOKEN = re.compile(r'\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}')
USES = re.compile(r'^\s*(?:-\s*)?uses:\s*(\S+)', re.M)
SHA_PIN = re.compile(r'@[0-9a-f]{40}$')
IMAGES = ('debian:12', 'debian:13', 'ubuntu:24.04', 'ubuntu:26.04')


def tracked_like_files():
    """Repository content: git-tracked paths when available, else a filesystem walk.

    git may be missing, the checkout may not be a repository, or it may refuse
    (e.g. ownership checks in a copied CI checkout). Any failure falls back.
    """
    try:
        proc = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                              capture_output=True, timeout=60, check=False)
        if proc.returncode == 0:
            names = [n for n in proc.stdout.decode('utf-8', 'surrogateescape').split('\0') if n]
            paths = [ROOT / n for n in names if (ROOT / n).is_file()]
            if paths:
                yield from paths
                return
    except (OSError, subprocess.SubprocessError):
        pass
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix in ('.pyc',):
                continue
            yield path


def relative(path):
    return path.relative_to(ROOT).as_posix()


def read_text(path):
    try:
        return path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        return None


def workflow_text(name):
    return (WORKFLOWS / name).read_text(encoding='utf-8')


def job_block(text, job):
    """Return the lines of one job under `jobs:` (two-space indent), up to the next job."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line == f'  {job}:'), None)
    if start is None:
        return None
    block = []
    for line in lines[start + 1:]:
        if line.startswith('  ') and not line.startswith('   ') and line.strip():
            break  # next sibling job key
        if line and not line.startswith(' '):
            break  # next top-level key
        block.append(line)
    return '\n'.join(block)


class RepositoryContractTests(unittest.TestCase):
    def test_bootstrap_contract_is_satisfied_by_this_repository(self):
        paths = {relative(p) for p in tracked_like_files()}
        self.assertEqual(bootstrap_policy.missing(paths), [])

    def test_standard_version_marker_matches_policy(self):
        marker = ROOT / '.agents' / 'STANDARD_VERSION'
        self.assertTrue(marker.is_file(), 'missing .agents/STANDARD_VERSION')
        self.assertEqual(marker.read_text(encoding='utf-8').strip(), bootstrap_policy.VERSION)

    def test_no_pull_request_target_anywhere(self):
        for path in WORKFLOWS.glob('*.yml'):
            with self.subTest(workflow=path.name):
                self.assertNotIn('pull_request_target', path.read_text(encoding='utf-8'))

    def test_required_and_regression_workflows_have_no_secrets(self):
        for name in ('required.yml', 'regression.yml'):
            with self.subTest(workflow=name):
                self.assertNotIn('secrets.', workflow_text(name))

    def test_required_workflow_contract(self):
        text = workflow_text('required.yml')
        self.assertRegex(text, r'(?m)^name: required$')
        self.assertIn('pull_request:', text)
        self.assertRegex(text, r'push:\s*\n\s*branches: \[main\]')
        block = job_block(text, 'required')
        self.assertIsNotNone(block, 'required job missing')
        self.assertIn('if: always()', block)
        self.assertIn('needs:', block)
        self.assertIn('needs.regression.result', block)

    def test_required_concurrency_group_is_not_shared_for_push(self):
        text = workflow_text('required.yml')
        group = re.search(r'(?m)^  group: (.+)$', text)
        self.assertIsNotNone(group, 'concurrency group missing')
        # Push runs (including pushes to main) must each get a unique group.
        self.assertIn('github.run_id', group.group(1))
        self.assertIn("github.event_name == 'pull_request'", group.group(1))
        self.assertRegex(text, r"(?m)^  cancel-in-progress: \$\{\{ github\.event_name == 'pull_request' \}\}$")

    def test_no_continue_on_error(self):
        for path in WORKFLOWS.glob('*.yml'):
            with self.subTest(workflow=path.name):
                self.assertNotIn('continue-on-error', path.read_text(encoding='utf-8'))

    def test_third_party_actions_are_pinned_to_full_sha(self):
        for path in WORKFLOWS.glob('*.yml'):
            for ref in USES.findall(path.read_text(encoding='utf-8')):
                with self.subTest(workflow=path.name, uses=ref):
                    if ref.startswith('./'):
                        continue
                    self.assertRegex(ref, SHA_PIN)

    def test_regression_covers_all_support_images(self):
        text = workflow_text('regression.yml')
        for image in IMAGES:
            with self.subTest(image=image):
                self.assertIn(image, text)

    def test_top_level_permissions_are_read_only(self):
        for path in WORKFLOWS.glob('*.yml'):
            with self.subTest(workflow=path.name):
                self.assertRegex(path.read_text(encoding='utf-8'),
                                 r'(?m)^permissions:\n  contents: read$')

    def test_vm_lifecycle_triggers_are_schedule_and_manual_only(self):
        text = workflow_text('vm-lifecycle.yml')
        self.assertRegex(text, r'(?m)^on:\n  schedule:\n    - cron: ')
        self.assertIn('  workflow_dispatch:', text)
        for trigger in ('pull_request', 'push:', 'pull_request_target', 'workflow_call', 'release'):
            with self.subTest(trigger=trigger):
                self.assertNotIn(trigger, text.split('permissions:', 1)[0].split('on:', 1)[1])

    def test_vm_lifecycle_has_no_secrets(self):
        self.assertNotIn('secrets.', workflow_text('vm-lifecycle.yml'))

    def test_vm_lifecycle_every_job_has_a_timeout(self):
        text = workflow_text('vm-lifecycle.yml')
        jobs = re.findall(r'(?m)^  ([A-Za-z0-9_-]+):\n(?=    )', text.split('\njobs:\n', 1)[1])
        self.assertTrue(jobs, 'no jobs parsed from vm-lifecycle.yml')
        for job in jobs:
            with self.subTest(job=job):
                block = job_block(text, job)
                self.assertRegex(block, r'(?m)^    timeout-minutes: (\d+)$')
                minutes = int(re.search(r'(?m)^    timeout-minutes: (\d+)$', block).group(1))
                self.assertLessEqual(minutes, 150)

    def test_vm_lifecycle_matrix_covers_all_four_distros(self):
        text = workflow_text('vm-lifecycle.yml')
        block = job_block(text, 'vm')
        self.assertIn('fail-fast: false', block)
        self.assertIn('fromJSON(needs.plan.outputs.matrix)', block)
        plan = job_block(text, 'plan')
        listed = re.search(r"matrix='\[([^\]]+)\]'", plan)
        self.assertIsNotNone(listed, 'plan job does not list the distro matrix')
        self.assertEqual(sorted(x.strip().strip('"') for x in listed.group(1).split(',')),
                         ['debian-12', 'debian-13', 'ubuntu-24.04', 'ubuntu-26.04'])

    def test_vm_lifecycle_run_step_passes_matrix_and_paths_through_env(self):
        # Interpolating ${{ }} inside run: is a script-injection shape; values go through env:.
        block = job_block(workflow_text('vm-lifecycle.yml'), 'vm')
        run_step = re.search(r'(?ms)^      - name: Run lifecycle scenario\n(.*?)(?=^      - name: |\Z)', block)
        self.assertIsNotNone(run_step, 'run lifecycle scenario step missing')
        body = run_step.group(1)
        env_part, _, run_part = body.partition('        run: |')
        self.assertIn('DISTRO: ${{ matrix.distro }}', env_part)
        self.assertNotIn('${{', run_part, 'run: must not interpolate expressions directly')
        self.assertIn('"$DISTRO"', run_part)

    def test_vm_lifecycle_checkout_does_not_persist_credentials(self):
        block = job_block(workflow_text('vm-lifecycle.yml'), 'vm')
        self.assertIn('persist-credentials: false', block)

    def test_vm_lifecycle_is_not_a_required_gate(self):
        for name in ('required.yml', 'regression.yml', 'release.yml'):
            with self.subTest(workflow=name):
                self.assertNotIn('vm-lifecycle', workflow_text(name))

    def test_no_personal_paths_in_tracked_text(self):
        for path in tracked_like_files():
            text = read_text(path)
            if text is None:
                continue
            for match in PERSONAL_PATH.finditer(text):
                with self.subTest(path=relative(path), match=match.group(0)):
                    self.fail('personal path in repository text')

    def test_no_token_patterns_in_tracked_text(self):
        for path in tracked_like_files():
            text = read_text(path)
            if text is None:
                continue
            with self.subTest(path=relative(path)):
                self.assertIsNone(TOKEN.search(text), 'token-like string in repository text')

    def test_renovate_config_honours_golden_standard(self):
        config = json.loads((ROOT / 'renovate.json').read_text(encoding='utf-8'))
        extends = config.get('extends', [])
        self.assertIn('config:recommended', extends)
        self.assertIn('helpers:pinGitHubActionDigests', extends)  # SECURITY.md: SHA-pinned actions
        dashboard = config.get('dependencyDashboard')
        if isinstance(dashboard, dict):  # migrated object form {"enabled": true}
            dashboard = dashboard.get('enabled')
        self.assertIs(dashboard, True, 'dependency dashboard must be enabled')
        self.assertIsNot(config.get('automerge'), True, 'blanket automerge bypasses controller review')


if __name__ == '__main__':
    unittest.main()
