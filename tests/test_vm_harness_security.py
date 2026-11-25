"""Offline regression tests for guest-facing and host-facing security controls of vmtest/.

Nothing here starts qemu, opens a real socket to the internet or runs ssh. Each test fails
if the control it covers is removed.
"""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from vmtest import fetch, manifest, ssh, vm

PUB = 'ssh-ed25519 AAAAC3TESTCLIENT vmtest-client'


class SeedServerBindingTests(unittest.TestCase):
    def test_binds_loopback_only_and_serves_only_known_paths(self):
        import urllib.request
        import urllib.error
        payload = b'#cloud-config\n'
        with vm.SeedServer({'user-data': payload}) as server:
            self.assertEqual(server.server.server_address[0], '127.0.0.1')
            url = f'http://127.0.0.1:{server.port}/user-data'
            with urllib.request.urlopen(url, timeout=10) as resp:
                self.assertEqual(resp.read(), payload)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(f'http://127.0.0.1:{server.port}/secret', timeout=10)
            self.assertEqual(caught.exception.code, 404)


class SshArgvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='vmtest-sec-')
        self.addCleanup(self.tmp.cleanup)
        self.known = Path(self.tmp.name) / 'known_hosts'
        self.guest = ssh.GuestSsh(port=2222, key=Path('/nonexistent/key'), known_hosts=self.known)

    def _options(self):
        argv = self.guest._argv('vmadmin', ['true'])
        return [argv[i + 1] for i, a in enumerate(argv) if a == '-o']

    def test_strict_host_key_checking_and_pinned_known_hosts_present(self):
        options = self._options()
        self.assertIn('StrictHostKeyChecking=yes', options)
        self.assertIn(f'UserKnownHostsFile={self.known}', options)
        self.assertIn('GlobalKnownHostsFile=/dev/null', options)

    def test_password_auth_is_disabled(self):
        options = self._options()
        self.assertIn('PasswordAuthentication=no', options)
        self.assertIn('BatchMode=yes', options)


class DownloadSizeCapTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='vmtest-dl-')
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def _fake_response(self, body: bytes):
        resp = mock.MagicMock()
        stream = io.BytesIO(body)
        resp.read.side_effect = stream.read
        resp.__enter__.return_value = resp
        return resp

    def test_oversize_body_is_refused_and_no_file_is_left(self):
        destination = self.dir / 'image.img'
        body = b'x' * (64 * 1024)
        with mock.patch.object(fetch.urllib.request, 'urlopen', return_value=self._fake_response(body)):
            with self.assertRaisesRegex(manifest.ManifestError, 'size cap'):
                fetch.download('https://cloud.debian.org/x.qcow2', destination, max_bytes=1024)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.dir.glob('.download-*')), [])

    def test_body_within_cap_is_written(self):
        destination = self.dir / 'image.img'
        body = b'y' * 2048
        with mock.patch.object(fetch.urllib.request, 'urlopen', return_value=self._fake_response(body)):
            fetch.download('https://cloud.debian.org/x.qcow2', destination, max_bytes=4096)
        self.assertEqual(destination.read_bytes(), body)

    def test_network_error_is_not_swallowed(self):
        with mock.patch.object(fetch.urllib.request, 'urlopen', side_effect=URLError('down')):
            with self.assertRaises(URLError):
                fetch.download('https://cloud.debian.org/x.qcow2', self.dir / 'x.img')


class KeypairOverwriteTests(unittest.TestCase):
    def test_existing_private_key_is_never_overwritten(self):
        tmp = tempfile.TemporaryDirectory(prefix='vmtest-key-')
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        (directory / 'client').write_text('keep me', encoding='utf-8')
        with mock.patch.object(ssh, 'run_checked', side_effect=AssertionError('ssh-keygen must not run')):
            with self.assertRaises(ssh.SshError):
                ssh.generate_keypair(directory, 'client')
        self.assertEqual((directory / 'client').read_text(encoding='utf-8'), 'keep me')

    def test_existing_public_key_alone_is_also_refused(self):
        tmp = tempfile.TemporaryDirectory(prefix='vmtest-key-')
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        (directory / 'client.pub').write_text(PUB + '\n', encoding='utf-8')
        with mock.patch.object(ssh, 'run_checked', side_effect=AssertionError('ssh-keygen must not run')):
            with self.assertRaises(ssh.SshError):
                ssh.generate_keypair(directory, 'client')


if __name__ == '__main__':
    unittest.main()
