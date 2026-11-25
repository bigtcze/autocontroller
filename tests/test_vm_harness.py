"""Offline tests for the disposable-VM harness (vmtest/) and its guest stand-ins.

Nothing here starts qemu, opens SSH or touches the network. Guest stand-ins are run
by absolute path with a temporary HOME; the blocking shims on PATH are never used.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vmtest import cloudinit, fetch, harness, manifest, qemu, runner, source, ssh
from vmtest.runner import ScenarioAbort

ROOT = Path(__file__).resolve().parents[1]
GUEST = ROOT / 'vmtest' / 'guest'
IMAGES = ROOT / 'vmtest' / 'images.json'
SAFE_PATH = '/usr/bin:/bin'
PUB_CLIENT = 'ssh-ed25519 AAAAC3TESTCLIENT vmtest-client'
PUB_HOST = 'ssh-ed25519 AAAAC3TESTHOST vmtest-host'
PRIV_HOST = '-----BEGIN TEST PRIVATE KEY-----\nAAAATESTHOST\n-----END TEST PRIVATE KEY-----\n'


class TempDirTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='vmtest-unit-')
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)


class ManifestTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.data = json.loads(IMAGES.read_text(encoding='utf-8'))

    def _mutate(self, distro, key, value):
        data = copy.deepcopy(self.data)
        data['images'][distro][key] = value
        return data

    def test_shipped_manifest_is_valid(self):
        self.assertEqual(manifest.validate(copy.deepcopy(self.data)), self.data)
        self.assertEqual(set(manifest.load(IMAGES)['images']), set(manifest.SUPPORTED))

    def test_rejects_http_url(self):
        url = self.data['images']['debian-13']['url'].replace('https://', 'http://', 1)
        with self.assertRaisesRegex(manifest.ManifestError, 'https'):
            manifest.validate(self._mutate('debian-13', 'url', url))

    def test_rejects_foreign_host(self):
        url = 'https://downloads.example.invalid/debian-13.qcow2'
        with self.assertRaisesRegex(manifest.ManifestError, 'allowlist'):
            manifest.validate(self._mutate('debian-13', 'url', url))

    def test_rejects_userinfo_and_port_on_allowed_host(self):
        for url in ('https://user@cloud.debian.org/x.qcow2', 'https://cloud.debian.org:8443/x.qcow2'):
            with self.subTest(url=url), self.assertRaises(manifest.ManifestError):
                manifest.validate(self._mutate('debian-13', 'url', url))

    def test_rejects_wrong_digest_length_and_non_hex(self):
        with self.assertRaisesRegex(manifest.ManifestError, 'digest'):
            manifest.validate(self._mutate('ubuntu-24.04', 'digest', 'a' * 63))
        with self.assertRaisesRegex(manifest.ManifestError, 'digest'):
            manifest.validate(self._mutate('ubuntu-24.04', 'digest', 'g' * 64))
        with self.assertRaisesRegex(manifest.ManifestError, 'digest'):
            manifest.validate(self._mutate('debian-13', 'digest', 'A' * 128))  # uppercase is not pinned form

    def test_rejects_unknown_algorithm_and_format(self):
        with self.assertRaisesRegex(manifest.ManifestError, 'algorithm'):
            manifest.validate(self._mutate('debian-12', 'algorithm', 'md5'))
        with self.assertRaisesRegex(manifest.ManifestError, 'format'):
            manifest.validate(self._mutate('debian-12', 'format', 'vmdk'))

    def test_rejects_missing_distro(self):
        data = copy.deepcopy(self.data)
        del data['images']['debian-12']
        with self.assertRaisesRegex(manifest.ManifestError, 'exactly'):
            manifest.validate(data)

    def test_rejects_extra_distro(self):
        data = copy.deepcopy(self.data)
        data['images']['debian-11'] = copy.deepcopy(data['images']['debian-12'])
        with self.assertRaisesRegex(manifest.ManifestError, 'exactly'):
            manifest.validate(data)

    def test_unknown_distro_and_unreadable_file_fail_closed(self):
        with self.assertRaises(manifest.ManifestError):
            manifest.image('debian-99', self.data)
        bad = self.dir / 'images.json'
        bad.write_text('{not json', encoding='utf-8')
        with self.assertRaises(manifest.ManifestError):
            manifest.load(bad)


class DigestAndFetchTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.payload = b'pinned image bytes\n' * 1000
        self.file = self.dir / 'image.img'
        self.file.write_bytes(self.payload)
        self.sha256 = hashlib.sha256(self.payload).hexdigest()

    def test_good_digest_passes_for_sha256_and_sha512(self):
        manifest.verify_digest(self.file, 'sha256', self.sha256)
        manifest.verify_digest(self.file, 'sha512', hashlib.sha512(self.payload).hexdigest())
        manifest.verify_digest(self.file, 'sha256', self.sha256.upper())

    def test_bad_digest_fails_closed(self):
        with self.assertRaisesRegex(manifest.ManifestError, 'mismatch'):
            manifest.verify_digest(self.file, 'sha256', '0' * 64)

    def test_one_flipped_byte_fails(self):
        tampered = self.dir / 'tampered.img'
        tampered.write_bytes(self.payload[:-1] + b'X')
        with self.assertRaises(manifest.ManifestError):
            manifest.verify_digest(tampered, 'sha256', self.sha256)

    def test_unsupported_algorithm_rejected(self):
        with self.assertRaises(manifest.ManifestError):
            manifest.verify_digest(self.file, 'md5', 'a' * 32)

    def _entry(self, digest):
        return {'url': 'https://cloud.debian.org/images/test.qcow2', 'algorithm': 'sha256',
                'digest': digest, 'format': 'qcow2', 'os_release': {'ID': 'debian', 'VERSION_ID': '13'}}

    def _ensure(self, cache, entry):
        return fetch.ensure_base_image('debian-13', cache, {'images': {'debian-13': entry}})

    def test_cache_hit_with_good_digest_needs_no_download(self):
        cache = self.dir / 'cache'
        entry = self._entry(self.sha256)
        target = fetch.cache_path(cache, entry)
        target.parent.mkdir(parents=True)
        target.write_bytes(self.payload)
        with mock.patch.object(fetch, 'download', side_effect=AssertionError('network used')):
            self.assertEqual(self._ensure(cache, entry), target)

    def test_cache_key_is_the_digest(self):
        entry = self._entry(self.sha256)
        self.assertEqual(fetch.cache_path(self.dir, entry).name, f'sha256-{self.sha256}.img')

    def test_corrupt_cache_entry_is_replaced_by_verified_download(self):
        cache = self.dir / 'cache'
        entry = self._entry(self.sha256)
        target = fetch.cache_path(cache, entry)
        target.parent.mkdir(parents=True)
        target.write_bytes(b'corrupt')

        def good_download(url, destination, **_):
            Path(destination).write_bytes(self.payload)

        with mock.patch.object(fetch, 'download', side_effect=good_download) as download:
            got = self._ensure(cache, entry)
        download.assert_called_once()
        self.assertEqual(got.read_bytes(), self.payload)

    def test_download_that_fails_verification_leaves_no_file(self):
        cache = self.dir / 'cache'
        entry = self._entry(self.sha256)

        def tampered_download(url, destination, **_):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b'tampered')

        with mock.patch.object(fetch, 'download', side_effect=tampered_download):
            with self.assertRaises(manifest.ManifestError):
                self._ensure(cache, entry)
        self.assertFalse(fetch.cache_path(cache, entry).exists())

    def test_download_refuses_non_https_before_any_network(self):
        with self.assertRaises(manifest.ManifestError):
            fetch.download('http://cloud.debian.org/x.qcow2', self.dir / 'x.img')


def _keys(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _keys(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _keys(value)


class CloudInitTests(unittest.TestCase):
    def setUp(self):
        self.raw = cloudinit.user_data(client_pubkey=PUB_CLIENT, host_private=PRIV_HOST, host_public=PUB_HOST)
        self.assertTrue(self.raw.startswith('#cloud-config\n'))
        self.doc = json.loads(self.raw.split('\n', 1)[1])

    def test_tester_and_vmadmin_with_public_keys_only(self):
        users = {u['name']: u for u in self.doc['users']}
        self.assertEqual(set(users), {'tester', 'vmadmin'})
        for name, user in users.items():
            with self.subTest(user=name):
                self.assertEqual(user['ssh_authorized_keys'], [PUB_CLIENT])
                self.assertTrue(all(k.startswith('ssh-') for k in user['ssh_authorized_keys']))
                self.assertTrue(user['lock_passwd'])
                self.assertEqual(user['sudo'], 'ALL=(ALL) NOPASSWD:ALL')

    def test_no_password_material_anywhere(self):
        forbidden = {'passwd', 'plain_text_passwd', 'password', 'chpasswd'}
        self.assertFalse(forbidden & set(_keys(self.doc)))
        self.assertIs(self.doc['ssh_pwauth'], False)
        self.assertNotIn('password', self.raw.lower())

    def test_host_key_injected_and_no_client_private_key(self):
        keys = self.doc['ssh_keys']
        self.assertEqual(keys['ed25519_private'], PRIV_HOST)
        self.assertEqual(keys['ed25519_public'], PUB_HOST)
        # One PEM block (BEGIN + END marker = 2 hits) is the injected host key; the client private
        # key never enters user-data, so no second block may appear.
        self.assertEqual(self.raw.count('PRIVATE KEY'), 2)

    def test_git_is_purged_so_the_scenario_starts_clean(self):
        commands = [' '.join(c) for c in self.doc['runcmd']]
        self.assertTrue(any('apt-get purge -y git' in c for c in commands))

    def test_rejects_malformed_key_material(self):
        with self.assertRaises(ValueError):
            cloudinit.user_data(client_pubkey='not a key', host_private=PRIV_HOST, host_public=PUB_HOST)
        with self.assertRaises(ValueError):
            cloudinit.user_data(client_pubkey=PUB_CLIENT, host_private=PRIV_HOST, host_public='bogus')
        with self.assertRaises(ValueError):
            cloudinit.user_data(client_pubkey=PUB_CLIENT, host_private='just text', host_public=PUB_HOST)

    def test_meta_data_and_seed_location(self):
        self.assertEqual(json.loads(cloudinit.meta_data('vmtest-abc123'))['instance-id'], 'vmtest-abc123')
        with self.assertRaises(ValueError):
            cloudinit.meta_data('bad id;rm')
        # nocloud-net is required by cloud-init 22.4.x (debian-12); the bare `nocloud` name is not.
        self.assertEqual(cloudinit.smbios_argument(8080), 'type=1,serial=ds=nocloud-net;s=http://10.0.2.2:8080/')
        with self.assertRaises(ValueError):
            cloudinit.smbios_argument(80)


class QemuArgvTests(TempDirTest):
    def _argv(self, accel, **overrides):
        kwargs = dict(disk=self.dir / 'd.qcow2', seed_port=8080, ssh_port=2222, accel=accel,
                      pidfile=self.dir / 'qemu.pid', serial_log=self.dir / 'serial.log',
                      vars_file=self.dir / 'OVMF_VARS.fd', firmware=self.dir / 'OVMF_CODE.fd')
        kwargs.update(overrides)
        return qemu.build_argv(**kwargs)

    @staticmethod
    def _value(argv, flag):
        return argv[argv.index(flag) + 1]

    def test_kvm_argv_uses_host_cpu_and_kvm_accel(self):
        argv = self._argv('kvm')
        self.assertIn('q35,accel=kvm', argv)
        self.assertEqual(self._value(argv, '-cpu'), 'host')

    def test_tcg_argv_never_requests_kvm(self):
        argv = self._argv('tcg')
        self.assertIn('q35,accel=tcg', argv)
        self.assertEqual(self._value(argv, '-cpu'), 'max')
        self.assertFalse(any('accel=kvm' in part for part in argv))

    def test_ssh_forward_is_bound_to_loopback_only(self):
        for accel in ('kvm', 'tcg'):
            with self.subTest(accel=accel):
                netdev = self._value(self._argv(accel), '-netdev')
                self.assertIn('hostfwd=tcp:127.0.0.1:2222-:22', netdev)
                self.assertNotRegex(netdev, r'hostfwd=tcp:(?!127\.0\.0\.1:)')

    def test_no_display_or_remote_graphics(self):
        argv = self._argv('tcg')
        self.assertEqual(self._value(argv, '-display'), 'none')
        self.assertEqual(self._value(argv, '-monitor'), 'none')
        self.assertFalse({'-vnc', '-spice'} & set(argv))

    def test_uefi_pflash_with_per_run_vars_and_seed_alias(self):
        argv = self._argv('kvm')
        pflash = [argv[i + 1] for i, a in enumerate(argv) if a == '-drive' and 'pflash' in argv[i + 1]]
        self.assertEqual(len(pflash), 2)
        self.assertIn('readonly=on', pflash[0])
        self.assertIn('OVMF_VARS.fd', pflash[1])
        self.assertIn('10.0.2.2:8080', self._value(argv, '-smbios'))

    def test_invalid_accel_and_ssh_port_rejected(self):
        with self.assertRaises(ValueError):
            self._argv('xen')
        with self.assertRaises(ValueError):
            self._argv('kvm', ssh_port=80)

    def test_resolve_accel_auto_uses_kvm_only_when_device_usable(self):
        device = self.dir / 'kvm'
        device.write_text('', encoding='utf-8')  # readable and writable by this user
        self.assertEqual(qemu.resolve_accel('auto', device), 'kvm')
        self.assertEqual(qemu.resolve_accel('auto', self.dir / 'absent'), 'tcg')

    def test_resolve_accel_forced_modes(self):
        device = self.dir / 'kvm'
        device.write_text('', encoding='utf-8')
        self.assertEqual(qemu.resolve_accel('kvm', device), 'kvm')
        self.assertEqual(qemu.resolve_accel('tcg', device), 'tcg')
        with self.assertRaises(RuntimeError):
            qemu.resolve_accel('kvm', self.dir / 'absent')
        with self.assertRaises(ValueError):
            qemu.resolve_accel('xen', device)


class SourceArchiveTests(TempDirTest):
    def test_excluded_paths_are_recognised(self):
        for rel in ('.git/HEAD', '__pycache__/m.pyc', 'state/memory/db', 'pkg/state/x', 'config.json',
                    'controller.lock', 'onboarding.lock', 'x.log', '.env', '.env.local', 'vmtest-artifacts/a'):
            with self.subTest(path=rel):
                self.assertTrue(source.is_excluded(rel))
        for rel in ('install.sh', 'vmtest/harness.py', 'vmtest/guest/gh', 'docs/VM_TESTING.md'):
            with self.subTest(path=rel):
                self.assertFalse(source.is_excluded(rel))

    def test_archive_omits_secrets_state_and_caches_and_is_deterministic(self):
        root = self.dir / 'src'
        files = {'install.sh': 'echo ok\n', 'vmtest/harness.py': 'x = 1\n', '.git/HEAD': 'ref\n',
                 'config.json': '{"token": "x"}\n', 'state/memory/db': 'db\n', 'controller.lock': '1\n',
                 '__pycache__/m.cpython-311.pyc': 'bytecode', 'run.log': 'log\n', '.env': 'SECRET=1\n',
                 'vmtest-artifacts/report.json': '{}\n'}
        for rel, body in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding='utf-8')
        archive = source.build_archive(root)
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
            members = tar.getmembers()
        names = {m.name for m in members}
        self.assertEqual(names, {'install.sh', 'vmtest/harness.py'})
        self.assertTrue(all(m.mtime == 0 and m.uid == 0 and m.gid == 0 for m in members))
        self.assertEqual(source.build_archive(root), archive)  # byte-identical on rebuild

    def test_archive_is_identical_across_a_clock_change(self):
        # Rebuild after the file mtimes move and after time passes, so a clock-based gzip
        # header or file mtime would change the bytes. Compare the gzip header mtime too.
        root = self.dir / 'src'
        (root / 'vmtest').mkdir(parents=True)
        (root / 'install.sh').write_text('echo ok\n', encoding='utf-8')
        (root / 'vmtest' / 'harness.py').write_text('x = 1\n', encoding='utf-8')
        first = source.build_archive(root)
        os.utime(root / 'install.sh', (1_900_000_000, 1_900_000_000))
        second = source.build_archive(root)
        self.assertEqual(first, second)
        self.assertEqual(first[4:8], b'\x00\x00\x00\x00')  # gzip header MTIME field is zero


class RunnerSemanticsTests(TempDirTest):
    def _run(self, steps):
        art = self.dir / 'art'
        ctx = runner.Context(distro='debian-13', repo_root=ROOT, artifacts=art)
        report = runner.Report(distro='debian-13')
        path = art / 'report.json'
        ok = runner.execute(steps, ctx, report, path)
        return ok, json.loads(path.read_text(encoding='utf-8'))

    def test_failing_step_skips_later_steps_but_collect_still_runs(self):
        order = []

        def record(name):
            def step(ctx):
                order.append(name)
            return step

        def boom(ctx):
            order.append('b')
            raise ScenarioAbort('injected failure')

        steps = [runner.Step('a', record('a')), runner.Step('b', boom),
                 runner.Step('c', record('c')), runner.Step('j collect', record('j'), collect=True)]
        ok, data = self._run(steps)
        self.assertFalse(ok)
        self.assertEqual({s['name']: s['status'] for s in data['steps']},
                         {'a': 'passed', 'b': 'failed', 'c': 'skipped', 'j collect': 'passed'})
        self.assertEqual(order, ['a', 'b', 'j'])  # c never executed
        self.assertEqual(data['overall'], 'failed')

    def test_overall_passes_only_when_every_step_passes(self):
        ok, data = self._run([runner.Step('a', lambda ctx: 'fine'), runner.Step('j', lambda ctx: None, collect=True)])
        self.assertTrue(ok)
        self.assertEqual(data['overall'], 'passed')
        self.assertEqual(data['steps'][0]['detail'], 'fine')

    def test_collect_step_failure_is_recorded_and_fails_overall(self):
        def broken(ctx):
            raise ScenarioAbort('collection incomplete: x')
        ok, data = self._run([runner.Step('j', broken, collect=True)])
        self.assertFalse(ok)
        self.assertEqual(data['overall'], 'failed')

    def test_failure_writes_traceback_artifact(self):
        def broken(ctx):
            raise ScenarioAbort('boom detail')
        _, _ = self._run([runner.Step('x step', broken)])
        failures = list((self.dir / 'art' / 'failures').glob('*.txt'))
        self.assertEqual(len(failures), 1)
        self.assertIn('boom detail', failures[0].read_text(encoding='utf-8'))

    def test_empty_report_is_failed(self):
        self.assertEqual(runner.Report(distro='debian-13').to_dict()['overall'], 'failed')

    def test_report_records_accelerator_qemu_and_integration_split(self):
        data = runner.Report(distro='debian-13', accel='tcg',
                             qemu_version='QEMU emulator version 9.2.0').to_dict()
        self.assertEqual(data['accel'], 'tcg')
        self.assertEqual(data['qemu_version'], 'QEMU emulator version 9.2.0')
        self.assertTrue(data['integration']['real'])
        self.assertTrue(data['integration']['stand_in'])

    def test_mise_wording_names_the_vendor_repository_not_distribution(self):
        real = runner.INTEGRATION['real']
        mise = [item for item in real if 'mise' in item]
        self.assertEqual(len(mise), 1)
        self.assertIn('vendor APT repository (mise.jdx.dev)', mise[0])
        self.assertNotIn('distribution repository', mise[0])


class HarnessWiringTests(unittest.TestCase):
    def test_only_the_final_collect_step_is_flagged_and_it_is_last(self):
        steps = harness.steps()
        self.assertEqual(sum(1 for s in steps if s.collect), 1)
        self.assertTrue(steps[-1].collect)
        self.assertTrue(steps[-1].name.startswith('j '))

    def test_steps_cover_a_to_j_including_the_marker_check(self):
        names = [s.name for s in harness.steps()]
        for letter in 'abcdefghij':
            with self.subTest(letter=letter):
                self.assertTrue(any(n.startswith(letter + ' ') for n in names))
        self.assertIn('d Engram failure marker cleared after recovery', names)

    def test_qemu_version_failure_is_recorded_not_raised(self):
        with mock.patch.object(harness.ssh, 'run_checked', side_effect=ssh.SshError('qemu missing')):
            value = harness._qemu_version()
        self.assertTrue(value.startswith('unavailable'))
        self.assertIn('qemu missing', value)

    def test_tcg_boot_budget_fits_the_job_limit(self):
        self.assertLessEqual(harness.BOOT_TIMEOUT['tcg'], 1800)
        self.assertLess(harness.BOOT_TIMEOUT['kvm'], harness.BOOT_TIMEOUT['tcg'])

    def test_tcg_scales_fixed_waits_and_kvm_stays_at_baseline(self):
        self.assertEqual(harness.WAIT_FACTOR['kvm'], 1)
        self.assertGreater(harness.WAIT_FACTOR['tcg'], 1)

    def test_tcg_worst_case_fits_the_vm_job_timeout_parsed_from_workflow(self):
        workflow = (ROOT / '.github' / 'workflows' / 'vm-lifecycle.yml').read_text(encoding='utf-8')
        vm_job = workflow.split('\n  vm:\n', 1)[1]
        body = re.split(r'(?m)^  [A-Za-z0-9_-]+:\n', vm_job, maxsplit=1)[0]  # stop at the next job
        match = re.search(r'(?m)^    timeout-minutes: (\d+)$', body)
        self.assertIsNotNone(match, 'vm job has no timeout-minutes')
        minutes = int(match.group(1))
        # Worst case: boot budget plus the scaled reboot/readiness waits plus the fixed sleeps.
        scale = harness.WAIT_FACTOR['tcg']
        worst_tcg_seconds = harness.BOOT_TIMEOUT['tcg'] + (600 + 300 + 900 + 300) * scale
        self.assertLessEqual(worst_tcg_seconds, minutes * 60,
                             f'TCG worst case {worst_tcg_seconds}s exceeds vm job {minutes} min')


class StandInTestBase(TempDirTest):
    """Run guest stand-ins by absolute path with a temporary HOME and a clean environment."""

    def setUp(self):
        super().setUp()
        self.home = self.dir / 'home'
        self.home.mkdir(mode=0o700)
        self.log_dir = self.home / '.local' / 'state' / 'vmtest'

    def _env(self, invocation=False):
        env = {'PATH': SAFE_PATH, 'HOME': str(self.home), 'LANG': 'C.UTF-8'}
        if invocation:
            env['INVOCATION_ID'] = 'a1b2c3d4e5f6'
        return env

    def _call(self, script, *args, invocation=False):
        return subprocess.run(['bash', str(GUEST / script), *args], env=self._env(invocation),
                              capture_output=True, text=True, timeout=30, check=False)


class FakeOpencodeTests(StandInTestBase):
    def test_api_info_and_three_agents_are_answered(self):
        info = self._call('opencode', 'api', 'GET', '/api/info')
        self.assertEqual(info.returncode, 0, info.stderr)
        self.assertIn('2.0.0-vmtest-standin', json.loads(info.stdout)['data']['version'])
        for role in ('orchestrator', 'fixer', 'oracle'):
            with self.subTest(role=role):
                proc = self._call('opencode', 'api', 'GET', f'/api/agent/{role}')
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(json.loads(proc.stdout)['data']['name'], role)

    def test_unknown_agent_and_run_are_refused_nonzero(self):
        self.assertNotEqual(self._call('opencode', 'api', 'GET', '/api/agent/intruder').returncode, 0)
        proc = self._call('opencode', 'run', 'do something')
        self.assertEqual(proc.returncode, 3)
        self.assertIn('refusing', proc.stderr)

    def test_every_invocation_is_logged_including_refusals(self):
        self._call('opencode', 'run', 'x')
        log = (self.log_dir / 'opencode-calls.log').read_text(encoding='utf-8')
        self.assertIn('run x', log)


class FakeGhTests(StandInTestBase):
    def _log(self):
        return (self.log_dir / 'gh-calls.log').read_text(encoding='utf-8').splitlines()

    def test_both_installer_capability_probes_succeed_and_mention_json(self):
        link = self._call('gh', 'project', 'link', '--help')
        self.assertEqual(link.returncode, 0, link.stderr)
        checks = self._call('gh', 'pr', 'checks', '--help')
        self.assertEqual(checks.returncode, 0, checks.stderr)
        self.assertIn('--json', checks.stdout)  # install.sh greps for "--json"

    def test_sequence_numbers_are_monotonic(self):
        self._call('gh', 'project', 'view', '7', '--owner', 'example-owner')
        self._call('gh', 'project', 'view', '7', '--owner', 'example-owner')
        seqs = [int(line.split()[0]) for line in self._log()]
        self.assertEqual(seqs, [1, 2])

    def test_origin_is_svc_only_when_invocation_id_is_set(self):
        self._call('gh', 'project', 'view', '7', invocation=True)
        self._call('gh', 'project', 'view', '7')
        fields = [line.split() for line in self._log()]
        self.assertEqual([f[2] for f in fields], ['svc', 'cli'])

    def test_controller_read_calls_answer_with_json_and_log_as_svc(self):
        proc = self._call('gh', 'api', '--method', 'GET',
                          'repos/example-owner/example-repo/pulls?state=open&per_page=100',
                          '--paginate', '--slurp', invocation=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [[]])
        self.assertEqual(self._log()[-1].split()[2], 'svc')

    def test_unknown_call_is_refused_with_exit_4_and_logged(self):
        proc = self._call('gh', 'project', 'link', invocation=True)  # no --help: not an allowed call
        self.assertEqual(proc.returncode, 4)
        lines = self._log()
        self.assertTrue(any(' REFUSED ' in line for line in lines), lines)

    def test_write_api_methods_are_refused(self):
        allowed = 'repos/example-owner/example-repo/pulls?state=open&per_page=100'
        for argv in (['--method', 'POST', allowed], ['-X', 'DELETE', allowed],
                     ['--method', 'GET', '-f', 'state=closed', allowed],
                     ['--method', 'GET', '--field', 'title=x', allowed]):
            with self.subTest(argv=argv):
                proc = self._call('gh', 'api', *argv, invocation=True)
                self.assertEqual(proc.returncode, 4, proc.stdout)

    def test_probe_usage_documents_the_json_flag_as_a_flag_line(self):
        out = self._call('gh', 'pr', 'checks', '--help').stdout.splitlines()
        self.assertTrue(any(line.lstrip().startswith('--json') for line in out), out)

    def test_unrecognised_top_level_command_is_refused(self):
        proc = self._call('gh', 'auth', 'token', invocation=True)
        self.assertEqual(proc.returncode, 4)
        self.assertTrue(any(' REFUSED ' in line for line in self._log()))

    def test_no_network_tools_are_invoked_by_the_stand_in(self):
        text = (GUEST / 'gh').read_text(encoding='utf-8')
        for banned in ('curl', 'wget', 'https://api.github.com', 'nc '):
            self.assertNotIn(banned, text)


class SetActiveVersionTests(TempDirTest):
    def _run(self, path, version):
        return subprocess.run([sys.executable, str(GUEST / 'set_active_version.py'), str(path), version],
                              capture_output=True, text=True, timeout=30, check=False)

    def test_rewrites_only_version_and_keeps_generation(self):
        active = self.dir / 'active.json'
        active.write_text(json.dumps({'generation': 'gen-1', 'version': 'v3.7.2'}), encoding='utf-8')
        proc = self._run(active, 'v0.0.1')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(active.read_text(encoding='utf-8')),
                         {'generation': 'gen-1', 'version': 'v0.0.1'})
        self.assertFalse(active.with_suffix('.tmp').exists())  # atomic replace leaves no temp file

    def test_refuses_file_without_generation_and_leaves_it_untouched(self):
        active = self.dir / 'active.json'
        original = json.dumps({'version': 'v1'})
        active.write_text(original, encoding='utf-8')
        proc = self._run(active, 'v2')
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(active.read_text(encoding='utf-8'), original)


if __name__ == '__main__':
    unittest.main()
