"""Offline guard tests for scripts/live-smoke.py.

The live smoke is never run by CI. These tests prove only that every guard
refuses BEFORE any external call: a fake `gh` and `opencode` shim on PATH
records any invocation, and the test asserts that the log stays empty.
No network, no real credentials, no real HOME.
"""
import importlib.util
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('live_smoke', str(ROOT / 'scripts' / 'live-smoke.py'))
live = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live)

OWNER = 'example-owner'
RUN = 'abc123xyz'
GOOD_REPO = f'autocontroller-smoke-{RUN}'
GOOD_ARGS = ['--owner', OWNER, '--allow-owner', OWNER, '--repo', GOOD_REPO, '--run-id', RUN,
             '--model', 'example-provider/example-model', '--max-ai-sessions', '1',
             '--timeout-seconds', '300', live.ACK_FLAG]


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='live-guard-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, True))
        self.tmp.chmod(0o700)
        self.gh_dir = self.tmp / 'gh'
        self.gh_dir.mkdir(mode=0o700)
        self.log = self.tmp / 'shim-calls.log'
        shims = self.tmp / 'bin'
        shims.mkdir(mode=0o700)
        for name in ('gh', 'opencode', 'git'):
            shim = shims / name
            shim.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.log}"\nexit 97\n', encoding='utf-8')
            shim.chmod(stat.S_IRWXU)
        self.shims = shims

    def env(self, **overrides):
        base = {'PATH': f'{self.shims}:{os.environ.get("PATH", "/usr/bin:/bin")}',
                'HOME': str(self.tmp / 'home'), 'GH_CONFIG_DIR': str(self.gh_dir),
                live.ENV_OPT_IN: '1'}
        base.update(overrides)
        return {k: v for k, v in base.items() if v is not None}

    def run_main(self, args, env, production_root=None):
        """Run main() with the given env; returns (rc, stderr). Asserts no shim was reached.

        The production root is injected (absent by default) so the host's real
        ~/scripts/autocontroller is never looked at.
        """
        import io
        from contextlib import redirect_stderr, redirect_stdout
        err, out = io.StringIO(), io.StringIO()
        root = self.tmp / 'absent-production-root' if production_root is None else production_root
        with redirect_stderr(err), redirect_stdout(out):
            rc = live.main(args, env=env, production_root=root)
        self.assertFalse(self.log.exists(), f'an external shim was invoked: {self.log.read_text() if self.log.exists() else ""}')
        return rc, err.getvalue()

    def assertRefused(self, args, env, needle):
        rc, err = self.run_main(args, env)
        self.assertEqual(rc, 2, err)
        self.assertIn(needle, err)

    def test_good_configuration_passes_guards_without_any_external_call(self):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            rc = live.main(GOOD_ARGS, env=self.env(), production_root=self.tmp / 'absent-production-root')
        self.assertEqual(rc, 0)
        self.assertIn('no external call made', out.getvalue())
        self.assertFalse(self.log.exists(), 'guards alone must not invoke gh, git or opencode')

    def test_missing_opt_in_env_refuses(self):
        self.assertRefused(GOOD_ARGS, self.env(**{live.ENV_OPT_IN: None}), 'AUTOCONTROLLER_LIVE_SMOKE=1')

    def test_opt_in_value_other_than_one_refuses(self):
        self.assertRefused(GOOD_ARGS, self.env(**{live.ENV_OPT_IN: 'true'}), 'AUTOCONTROLLER_LIVE_SMOKE=1')

    def test_missing_token_acknowledgement_refuses(self):
        args = [a for a in GOOD_ARGS if a != live.ACK_FLAG]
        self.assertRefused(args, self.env(), live.ACK_FLAG)

    def test_owner_not_on_allowlist_refuses(self):
        args = [*GOOD_ARGS]
        args[args.index('--allow-owner') + 1] = 'someone-else'
        self.assertRefused(args, self.env(), '--allow-owner')

    def test_invalid_owner_characters_refuse(self):
        args = [*GOOD_ARGS]
        args[args.index('--owner') + 1] = 'bad owner'
        args[args.index('--allow-owner') + 1] = 'bad owner'
        self.assertRefused(args, self.env(), 'invalid --owner')

    def test_missing_gh_config_dir_refuses(self):
        self.assertRefused(GOOD_ARGS, self.env(GH_CONFIG_DIR=None), 'GH_CONFIG_DIR must point')

    def test_default_gh_config_dir_refuses(self):
        default = Path.home() / '.config' / 'gh'
        self.assertRefused(GOOD_ARGS, self.env(GH_CONFIG_DIR=str(default)), 'default gh configuration')

    def test_nonexistent_gh_config_dir_refuses(self):
        self.assertRefused(GOOD_ARGS, self.env(GH_CONFIG_DIR=str(self.tmp / 'missing')), 'does not exist')

    def test_ambient_github_tokens_refuse(self):
        for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
            with self.subTest(name=name):
                self.assertRefused(GOOD_ARGS, self.env(**{name: 'not-a-real-token'}), f'{name} must be unset')

    def test_ci_environment_refuses(self):
        for name in ('GITHUB_ACTIONS', 'CI'):
            with self.subTest(name=name):
                self.assertRefused(GOOD_ARGS, self.env(**{name: 'true'}), 'refusing to run under CI')

    def test_pull_request_events_refuse_even_outside_ci(self):
        for event in ('pull_request', 'pull_request_target'):
            with self.subTest(event=event):
                self.assertRefused(GOOD_ARGS, self.env(GITHUB_EVENT_NAME=event), 'pull_request event')

    def test_repo_without_disposable_prefix_refuses(self):
        args = [*GOOD_ARGS]
        args[args.index('--repo') + 1] = f'production-{RUN}'
        self.assertRefused(args, self.env(), '--repo must fullmatch')

    def test_repo_name_must_fullmatch_the_disposable_pattern(self):
        for name in (f'{GOOD_REPO}.extra', f'{GOOD_REPO}_x', f'AUTOCONTROLLER-SMOKE-{RUN}'):
            with self.subTest(repo=name):
                args = [*GOOD_ARGS]
                args[args.index('--repo') + 1] = name
                self.assertRefused(args, self.env(), '--repo must fullmatch')

    def test_model_with_leading_dash_or_bad_characters_refuses(self):
        for model in ('-x/y', 'provider/model with space', 'provider/mo;del'):
            with self.subTest(model=model):
                # `--model=VALUE` form so argparse hands a leading dash to the guard.
                args = [*GOOD_ARGS]
                cut = args.index('--model')
                del args[cut:cut + 2]
                args.append(f'--model={model}')
                self.assertRefused(args, self.env(), '--model must be an explicit provider/model id')

    def test_production_root_present_refuses_before_any_external_call(self):
        present = self.tmp / 'present-production-root'
        present.mkdir()
        rc, err = self.run_main(GOOD_ARGS, self.env(), production_root=present)
        self.assertEqual(rc, 2, err)
        self.assertIn('production controller root exists', err)

    def test_allowlist_file_in_gh_config_dir_permits_the_owner(self):
        (self.gh_dir / live.ALLOWLIST_FILE).write_text(f'# sandbox owners\n{OWNER}\n', encoding='utf-8')
        args = [*GOOD_ARGS]
        cut = args.index('--allow-owner')
        del args[cut:cut + 2]  # only the --allow-owner pair; --owner stays
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            rc = live.main(args, env=self.env(), production_root=self.tmp / 'absent-production-root')
        self.assertEqual(rc, 0)
        self.assertFalse(self.log.exists(), 'the allowlist file is read without any external call')

    def test_allowlist_file_with_an_invalid_login_refuses(self):
        (self.gh_dir / live.ALLOWLIST_FILE).write_text('bad login\n', encoding='utf-8')
        args = [*GOOD_ARGS]
        cut = args.index('--allow-owner')
        del args[cut:cut + 2]
        self.assertRefused(args, self.env(), f'invalid login in {live.ALLOWLIST_FILE}')

    def test_execute_refuses_allow_owner_without_the_allowlist_file(self):
        # m3: --allow-owner is self-asserted. Without the file in GH_CONFIG_DIR, --execute refuses.
        self.assertFalse((self.gh_dir / live.ALLOWLIST_FILE).exists())
        fake_opencode = self.tmp / 'opencode-bin'
        fake_opencode.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        fake_opencode.chmod(stat.S_IRWXU)
        report = self.tmp / 'report-m3b.json'
        args = [*GOOD_ARGS, '--execute', '--opencode', str(fake_opencode), '--report', str(report)]
        rc, err = self.run_main(args, self.env())
        self.assertEqual(rc, 2, err)
        self.assertIn(f'{live.ALLOWLIST_FILE} inside GH_CONFIG_DIR', err)
        self.assertFalse(report.exists())

    def test_non_utf8_allowlist_is_a_refusal_with_exit_two(self):
        # m4: a non-UTF-8 allowlist refuses (exit 2) rather than raising an unexpected error.
        (self.gh_dir / live.ALLOWLIST_FILE).write_bytes(b'\xff\xfe\x00bad\n')
        args = [*GOOD_ARGS]
        cut = args.index('--allow-owner')
        del args[cut:cut + 2]
        self.assertRefused(args, self.env(), 'not valid UTF-8')

    def test_unreadable_allowlist_directory_in_place_of_file_is_a_refusal_with_exit_two(self):
        # NIT: an allowlist that exists but cannot be read refuses (exit 2). A directory in place
        # of the file fails the read for every user, including root, so no file mode is relied on.
        (self.gh_dir / live.ALLOWLIST_FILE).mkdir()
        args = [*GOOD_ARGS]
        cut = args.index('--allow-owner')
        del args[cut:cut + 2]
        self.assertRefused(args, self.env(), 'is not readable')

    def test_permission_error_reading_the_allowlist_is_a_refusal_with_exit_two(self):
        # NIT: the PermissionError branch, forced by patching the read (deterministic as root too).
        (self.gh_dir / live.ALLOWLIST_FILE).write_text(f'{OWNER}\n', encoding='utf-8')
        args = [*GOOD_ARGS]
        cut = args.index('--allow-owner')
        del args[cut:cut + 2]
        with patch.object(live.Path, 'read_text', side_effect=PermissionError('denied')):
            self.assertRefused(args, self.env(), 'is not readable')

    def test_multithreaded_process_refuses_execute_before_any_report_or_call(self):
        # m3: the mask assumes one thread. More threads refuse --execute (exit 2), no report file.
        (self.gh_dir / live.ALLOWLIST_FILE).write_text(f'{OWNER}\n', encoding='utf-8')
        fake_opencode = self.tmp / 'opencode-bin'
        fake_opencode.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        fake_opencode.chmod(stat.S_IRWXU)
        report = self.tmp / 'report-m3-threads.json'
        args = [*GOOD_ARGS, '--execute', '--opencode', str(fake_opencode), '--report', str(report)]
        with patch.object(live.threading, 'active_count', return_value=2):
            rc, err = self.run_main(args, self.env())
        self.assertEqual(rc, 2, err)
        self.assertIn('single-threaded', err)
        self.assertFalse(report.exists(), 'no report file is created when the refusal is made')

    def test_refusal_removes_the_empty_exclusive_report(self):
        # m4: a refusal after the report was created (here: the login check) leaves no empty file.
        fake_opencode = self.tmp / 'opencode-bin'
        fake_opencode.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        fake_opencode.chmod(stat.S_IRWXU)
        (self.gh_dir / live.ALLOWLIST_FILE).write_text(f'{OWNER}\n', encoding='utf-8')
        report = self.tmp / 'report-m4.json'
        args = [*GOOD_ARGS, '--execute', '--opencode', str(fake_opencode), '--report', str(report)]
        with patch.object(live, 'gh', return_value='{"login": "someone-else"}'):
            rc, err = self.run_main(args, self.env())
        self.assertEqual(rc, 2, err)
        self.assertFalse(report.exists(), 'an empty report from a pre-run refusal is removed')

    def test_opencode_must_be_absolute_and_not_the_controller_wrapper(self):
        wrapper = self.tmp / 'opencode-project-safe.py'
        wrapper.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        wrapper.chmod(stat.S_IRWXU)
        report = self.tmp / 'report.json'
        args = [*GOOD_ARGS, '--execute', '--opencode', str(wrapper), '--report', str(report)]
        rc, err = self.run_main(args, self.env())
        self.assertEqual(rc, 2, err)
        self.assertIn('never the controller wrapper', err)
        self.assertFalse(report.exists(), 'refused before the report file is created')
        args = [*GOOD_ARGS, '--execute', '--opencode', 'opencode', '--report', str(report)]
        rc, err = self.run_main(args, self.env())
        self.assertEqual(rc, 2, err)
        self.assertIn('absolute path', err)

    def test_opencode_inside_the_production_root_refuses(self):
        # check_guards refuses first whenever the production root exists, so this guard is
        # exercised directly: the binary lives under the (injected) production root.
        prod = self.tmp / 'prod-root'
        prod.mkdir()
        binary = prod / 'opencode'
        binary.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        binary.chmod(stat.S_IRWXU)
        args = live.parse_args([*GOOD_ARGS, '--execute', '--opencode', str(binary),
                                '--report', str(self.tmp / 'report.json')])
        with self.assertRaises(live.Refused) as caught:
            live.check_execute_inputs(args, self.tmp / 'some-checkout', production_root=prod)
        self.assertIn('installed controller', str(caught.exception))
        self.assertFalse((self.tmp / 'report.json').exists())

    def test_repo_missing_run_id_refuses(self):
        args = [*GOOD_ARGS]
        args[args.index('--repo') + 1] = 'autocontroller-smoke-fixed'
        self.assertRefused(args, self.env(), '--repo must contain --run-id')

    def test_run_id_format_refuses(self):
        args = [*GOOD_ARGS]
        args[args.index('--run-id') + 1] = 'ABC/..'
        self.assertRefused(args, self.env(), '--run-id must be')

    def test_model_must_be_explicit_provider_model(self):
        args = [*GOOD_ARGS]
        args[args.index('--model') + 1] = 'default'
        self.assertRefused(args, self.env(), '--model must be an explicit provider/model id')

    def test_ai_session_cap_is_exactly_one(self):
        for value in ('0', '2', '99'):
            with self.subTest(value=value):
                args = [*GOOD_ARGS]
                args[args.index('--max-ai-sessions') + 1] = value
                self.assertRefused(args, self.env(), '--max-ai-sessions must be exactly 1')

    def test_timeout_bounds_enforced(self):
        for value in ('5', '901'):
            with self.subTest(value=value):
                args = [*GOOD_ARGS]
                args[args.index('--timeout-seconds') + 1] = value
                self.assertRefused(args, self.env(), '--timeout-seconds must be between 30 and 900')

    def test_prompt_size_cap_enforced(self):
        args = [*GOOD_ARGS, '--max-prompt-bytes', str(live.MAX_PROMPT_BYTES + 1)]
        self.assertRefused(args, self.env(), '--max-prompt-bytes must be between')

    def test_execute_without_opencode_or_report_refuses_before_any_external_call(self):
        # --execute needs an explicit opencode binary and a report path; neither is guessed.
        rc, err = self.run_main([*GOOD_ARGS, '--execute'], self.env())
        self.assertEqual(rc, 2, err)
        self.assertIn('--opencode PATH is required', err)

    def test_execute_with_missing_report_path_refuses(self):
        fake_opencode = self.tmp / 'opencode'
        fake_opencode.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        fake_opencode.chmod(stat.S_IRWXU)
        args = [*GOOD_ARGS, '--execute', '--opencode', str(fake_opencode)]
        rc, err = self.run_main(args, self.env())
        self.assertEqual(rc, 2, err)
        self.assertIn('--report PATH is required', err)

    def test_login_mismatch_refuses_before_any_repo_call(self):
        (self.gh_dir / live.ALLOWLIST_FILE).write_text(f'{OWNER}\n', encoding='utf-8')  # m3: required by --execute
        fake_opencode = self.tmp / 'opencode'
        fake_opencode.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        fake_opencode.chmod(stat.S_IRWXU)
        report = self.tmp / 'report.json'
        args = [*GOOD_ARGS, '--execute', '--opencode', str(fake_opencode), '--report', str(report)]
        with patch.object(live, 'gh', return_value='{"login": "someone-else"}'):
            rc, err = self.run_main(args, self.env())
        # The login check is the first external call; it is a read-only `gh api user`.
        self.assertEqual(rc, 2, err)
        self.assertIn('does not match --owner', err)
        self.assertFalse(report.exists(), 'm4: the empty exclusive report is removed on a pre-run refusal')


class RepositoryContractTests(unittest.TestCase):
    """Workflows must never reference the live smoke or expose secrets to it."""

    def workflow_text(self, name):
        return (ROOT / '.github' / 'workflows' / name).read_text(encoding='utf-8')

    def test_no_workflow_references_live_smoke(self):
        for path in sorted((ROOT / '.github' / 'workflows').glob('*.yml')):
            with self.subTest(workflow=path.name):
                text = path.read_text(encoding='utf-8')
                self.assertNotIn('live-smoke', text)
                self.assertNotIn(live.ENV_OPT_IN, text)
                self.assertNotIn('i-understand-this-spends-tokens', text)

    def test_required_and_regression_workflows_remain_secret_free(self):
        for name in ('required.yml', 'regression.yml'):
            with self.subTest(workflow=name):
                self.assertNotIn('secrets.', self.workflow_text(name))


if __name__ == '__main__':
    unittest.main()
