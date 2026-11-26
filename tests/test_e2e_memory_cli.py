"""E2E: the REAL `memory.cli()` runs in a subprocess (tests/e2e_memory_cli.py).

Only `memory.ROOT` (the world tmp dir) and `memory.socket_path()` (the private stand-in socket) are
redirected by the launcher. Everything else is product code: the grant path shape, the token
check, the selection refusal, the `failure` marker, the 4096-byte stdin cap, the JSON-object
check, the `operation()` budgets, the `metrics.json` counters and the exit-code mapping.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import unittest.mock

import e2e_world as ew
from test_e2e_controller import E2EBase, R

LAUNCHER = ew.TESTS / 'e2e_memory_cli.py'


class MemoryCliBase(E2EBase):
    def setUp(self):
        super().setUp()
        self.world.start_engram()
        self.addCleanup(self.world.stop_engram)
        self.pdir = self.world.state / 'projects' / 'alpha'
        self.pdir.mkdir(parents=True, exist_ok=True)
        self.token = 'tok-' + 'b' * 24
        self.grant_path = self.pdir / 'memory-grant.json'
        self._write_grant('issue', 1)

    def _write_grant(self, kind: str, number: int, path=None):
        grant = {'token': self.token, 'repo': R, 'kind': kind, 'number': number, 'used': 0,
                 'queries': [], 'records': 0, 'feedback': 0, 'returned': []}
        target = path or self.grant_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(grant), encoding='utf-8')
        return grant

    def cli(self, *args, stdin: str | None = None, grant=None, token=None):
        argv = [sys.executable, str(LAUNCHER), '--grant', str(grant or self.grant_path),
                '--token', token or self.token, *args]
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=60,
                              env=os.environ.copy(), check=False)

    def record_payload(self, **overrides) -> str:
        entry = {'title': 'Verify cwd before edits', 'trigger': 'working directory mismatch',
                 'lesson': 'Verify the working directory before modifying files.',
                 'proof': 'Reproduced the wrong cwd and verified the corrected path.',
                 'evidence': f'https://github.com/{R}/pull/9', 'transferable': False}
        entry.update(overrides)
        return json.dumps(entry)


class MemoryCliRefusalTests(MemoryCliBase):
    def test_a_search_through_the_real_cli_succeeds_and_prints_a_json_list(self):
        proc = self.cli('search', 'working directory')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [])

    def test_failure_marker_refuses_with_memory_blocked(self):
        (self.world.state / 'memory' / 'failure').touch()
        proc = self.cli('search', 'working directory')
        self.assertEqual(proc.returncode, 1, (proc.stdout, proc.stderr))
        self.assertIn('Memory operation refused: Memory blocked', proc.stderr)
        self.assertEqual(proc.stdout, '')

    def test_stdin_over_4096_bytes_is_refused_as_input_too_large(self):
        payload = self.record_payload(lesson='x' * 5000)
        self.assertGreater(len(payload), 4096)
        proc = self.cli('record', stdin=payload)
        self.assertEqual(proc.returncode, 1, (proc.stdout, proc.stderr))
        self.assertIn('Input too large', proc.stderr)
        self.assertEqual(proc.stdout, '', 'an oversized record never prints a result')
        self.assertEqual(json.loads((self.grant_path).read_text(encoding='utf-8'))['records'], 0,
                         'an oversized record never counts against the grant')

    def test_non_object_json_on_stdin_is_refused(self):
        proc = self.cli('record', stdin='["not", "an", "object"]')
        self.assertEqual(proc.returncode, 1, (proc.stdout, proc.stderr))
        self.assertIn('JSON object required', proc.stderr)

    def test_grant_outside_the_projects_layout_is_refused(self):
        misplaced = self.world.state / 'other' / 'alpha' / 'memory-grant.json'
        self._write_grant('issue', 1, path=misplaced)
        proc = self.cli('search', 'x', grant=misplaced)
        self.assertEqual(proc.returncode, 1, (proc.stdout, proc.stderr))
        self.assertIn('Invalid controller grant path', proc.stderr)

    def test_wrong_token_is_refused_and_the_grant_is_untouched(self):
        before = self.grant_path.read_text(encoding='utf-8')
        proc = self.cli('search', 'x', token='tok-wrong')
        self.assertEqual(proc.returncode, 1, (proc.stdout, proc.stderr))
        self.assertIn('Invalid session grant', proc.stderr)
        self.assertEqual(self.grant_path.read_text(encoding='utf-8'), before)

    def test_selection_grant_cannot_search_or_record(self):
        self._write_grant('selection', 0)
        proc = self.cli('search', 'x')
        self.assertEqual(proc.returncode, 1, (proc.stdout, proc.stderr))
        self.assertIn('No learning during selection', proc.stderr)


class MemoryCliFailureTests(MemoryCliBase):
    def test_a_dead_backend_records_the_failure_marker(self):
        """A MemoryError from the backend is the adapter's failure: memory.cli() touches the marker."""
        runtime = self.world.runtime
        runtime.proc.kill()
        runtime.proc.wait(timeout=15)
        self.assertFalse(runtime.failed.exists())
        proc = self.cli('search', 'working directory')
        self.assertEqual(proc.returncode, 1, (proc.stdout, proc.stderr))
        self.assertIn('Memory operation refused:', proc.stderr)
        self.assertTrue(runtime.failed.exists(), 'a backend MemoryError creates <state>/memory/failure')


class EngramBackendIdentityTests(E2EBase):
    """Which Engram answered: the stand-in by default, the real upstream binary when opted in.

    No skip. With `E2E_ENGRAM_BINARY` set, the probe must NOT report the stand-in's version, and
    with `E2E_ENGRAM_VERSION` set it must report exactly that version.
    """

    def setUp(self):
        super().setUp()
        self.world.start_engram()
        self.addCleanup(self.world.stop_engram)

    def test_probe_identifies_the_engram_backend_and_its_version(self):
        health = self.world.runtime.api.health()
        self.assertEqual(health.get('service'), 'engram')
        self.assertEqual(health.get('status'), 'ok')
        recorded = self.world.engram_backend
        self.assertEqual(recorded['service'], 'engram')
        self.assertEqual(recorded['status'], 'ok')
        self.assertEqual(recorded['version'], health.get('version'))

    def test_backend_is_the_stand_in_or_the_real_binary_the_environment_selected(self):
        source = os.environ.get(ew.ENGRAM_BINARY_ENV, '')
        version = self.world.engram_backend['version']
        if not source:
            self.assertEqual(self.world.engram_backend['backend'], 'stand-in')
            self.assertEqual(version, ew.STANDIN_VERSION)
            return
        self.assertEqual(self.world.engram_backend['backend'], 'upstream-binary')
        self.assertNotEqual(version, ew.STANDIN_VERSION, 'the real binary must not report the stand-in version')
        expected = os.environ.get(ew.ENGRAM_VERSION_ENV, '')
        if not expected:
            self.fail(f'{ew.ENGRAM_BINARY_ENV} is set but {ew.ENGRAM_VERSION_ENV} is not: '
                      f'set E2E_ENGRAM_VERSION to the binary\'s version (the real binary reported {version!r})')
        self.assertEqual(version, expected)

    def test_an_invalid_binary_path_fails_loudly_instead_of_falling_back(self):
        folder = self.tmp / 'gen'
        folder.mkdir()
        bad = str(self.tmp / 'not-an-engram')
        with unittest.mock.patch.dict(os.environ, {ew.ENGRAM_BINARY_ENV: bad}):
            with self.assertRaises(AssertionError):
                ew.install_engram_backend(folder)
        self.assertFalse((folder / 'engram').exists(), 'no stand-in is silently installed')

    def test_unsafe_binary_paths_are_refused_before_anything_is_copied(self):
        """MINOR 7: production controller dir, /run/user/, and non-regular or non-executable files."""
        fake_home = self.tmp / 'fakehome'
        production = fake_home / 'scripts' / 'autocontroller'
        production.mkdir(parents=True)
        inside = production / 'engram'
        inside.write_bytes(b'\x7fELF-not-really')
        inside.chmod(0o700)
        plain = self.tmp / 'plain-engram'
        plain.write_bytes(b'x')
        plain.chmod(0o700)
        not_executable = self.tmp / 'not-exec'
        not_executable.write_bytes(b'x')
        not_executable.chmod(0o600)
        directory = self.tmp / 'a-dir'
        directory.mkdir()
        link_into_production = self.tmp / 'link-to-production'
        link_into_production.symlink_to(inside)
        cases = {
            'inside ~/scripts/autocontroller': str(inside),
            'symlink into ~/scripts/autocontroller': str(link_into_production),
            'the production directory itself': str(production),
            'a regular file outside but not executable': str(not_executable),
            'a directory': str(directory),
            'missing': str(self.tmp / 'missing-engram'),
        }
        with unittest.mock.patch.object(Path, 'home', return_value=fake_home):
            for label, source in cases.items():
                folder = self.tmp / ('gen-' + label.replace(' ', '-').replace('~', 'home').replace('/', '_'))
                folder.mkdir()
                with unittest.mock.patch.dict(os.environ, {ew.ENGRAM_BINARY_ENV: source}):
                    with self.assertRaises(AssertionError, msg=label):
                        ew.install_engram_backend(folder)
                self.assertFalse((folder / 'engram').exists(), f'{label}: nothing installed')
                self.assertFalse((folder / 'engram-real').exists(), f'{label}: no copy made')
        # A /run/user/ path is refused by its prefix alone, before any file check. The path does not
        # exist, so the test checks the message names the /run/user/ rule and not "not a file".
        with unittest.mock.patch.object(Path, 'home', return_value=fake_home):
            with self.assertRaisesRegex(AssertionError, r'under /run/user/'):
                ew.check_engram_binary_path('/run/user/%d/autocontroller-memory/engram' % os.getuid())

        # The supported entry point sets HOME to a temp dir, so Path.home() no longer points at the
        # real user's controller. The passwd home (pw_dir) must still be refused. Both homes here
        # are temp dirs; the real home is never touched.
        temp_home = self.tmp / 'scripted-home'  # what offline-tests.sh would set HOME to
        temp_home.mkdir()
        passwd_home = self.tmp / 'passwd-home'  # stands in for the real user's pw_dir
        real_controller = passwd_home / 'scripts' / 'autocontroller'
        real_controller.mkdir(parents=True)
        installed = real_controller / 'engram'
        installed.write_bytes(b'\x7fELF-not-really')
        installed.chmod(0o700)
        fake_passwd = unittest.mock.Mock(pw_dir=str(passwd_home))
        with unittest.mock.patch.dict(os.environ, {'HOME': str(temp_home)}):
            with unittest.mock.patch('pwd.getpwuid', return_value=fake_passwd):
                self.assertEqual(Path.home(), temp_home, 'HOME is the temp dir in this case')
                with self.assertRaisesRegex(AssertionError, r'production controller'):
                    ew.check_engram_binary_path(str(installed))
                with self.assertRaisesRegex(AssertionError, r'production controller'):
                    ew.check_engram_binary_path(str(real_controller))
                # Both candidate homes are guarded: the temp HOME's own controller dir is refused too.
                own = temp_home / 'scripts' / 'autocontroller' / 'engram'
                own.parent.mkdir(parents=True)
                own.write_bytes(b'x')
                own.chmod(0o700)
                with self.assertRaisesRegex(AssertionError, r'production controller'):
                    ew.check_engram_binary_path(str(own))
                # Outside every candidate home, an ordinary executable is accepted.
                ordinary = self.tmp / 'ordinary-engram'
                ordinary.write_bytes(b'x')
                ordinary.chmod(0o700)
                self.assertEqual(ew.check_engram_binary_path(str(ordinary)), ordinary.resolve())


class MemoryCliMetricsTests(MemoryCliBase):
    def test_scripted_learning_session_updates_every_operational_counter(self):
        """Search, record, then a shared confirmation from another repository: the counters move."""
        beta = 'example-owner/beta'
        self.world.add_project('alpha', R, 1)
        self.world.add_project('beta', beta, 2)
        issue_a = self.gh.add_issue(R, title='Alpha bug', body='x', project='example-owner/1', status='Backlog')
        issue_b = self.gh.add_issue(beta, title='Beta bug', body='y', project='example-owner/2', status='Backlog')
        lesson = {'title': 'Verify cwd before edits', 'trigger': 'working directory mismatch before edit',
                  'lesson': 'Verify the working directory before modifying files.',
                  'proof': 'Reproduced the wrong cwd and verified the corrected path.',
                  'evidence': f'https://github.com/{R}/pull/9', 'transferable': True}
        self.world.script('alpha', [
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue_a},
            {'memory': [{'action': 'search', 'arg': 'working directory mismatch'},
                        {'action': 'record', 'arg': lesson}], 'output': 'recorded'},
        ])
        self.world.script('beta', [
            {'output': 'AUTOCONTROLLER_SELECT_ISSUE #%d' % issue_b},
            {'memory': [{'action': 'search', 'arg': 'working directory mismatch'},
                        {'action': 'feedback', 'arg': {'id': '$first_returned', 'scope': 'shared',
                                                       'result': 'confirmed',
                                                       'evidence': f'https://github.com/{beta}/pull/3',
                                                       'reason': 'Reproduced the same cwd mismatch and verified the fix.'}}],
             'output': 'confirmed in beta'},
        ])
        self.drive('alpha', 2)
        self.drive('beta', 2)
        metrics = json.loads((self.world.state / 'memory' / 'metrics.json').read_text(encoding='utf-8'))
        for key in ('search_total', 'record_total', 'feedback_total', 'context_bytes_total', 'reuse_total'):
            self.assertGreaterEqual(metrics.get(key, 0), 1, f'{key} must be counted: {metrics}')
        self.assertEqual(metrics['record_total'], 1)
        self.assertEqual(metrics['feedback_total'], 1)
        self.assertEqual(metrics['reuse_total'], 1)
        self.assertGreater(metrics['context_bytes_total'], 0)
