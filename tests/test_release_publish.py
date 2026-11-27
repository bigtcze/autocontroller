"""Offline tests for `release_artifacts.py publish`, driven through a fake `gh` on PATH.

The fake gh records every argv, refuses `--slurp` together with `--jq` (the shape that broke
the old workflow), and models releases with ids, drafts, assets and digests in a JSON state
file. No network, no real gh, no real GitHub.
"""
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import release_artifacts as ra  # noqa: E402

REPO = 'example-owner/autocontroller'
TAG = 'v1.2.3'
ARCHIVE = ra.ARCHIVE_NAME.format(tag=TAG)

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
state_path = os.environ['FAKE_GH_STATE']
log_path = os.environ['FAKE_GH_LOG']
argv = sys.argv[1:]
with open(log_path, 'a', encoding='utf-8') as log:
    log.write(json.dumps(argv) + '\n')
if '--slurp' in argv and '--jq' in argv:
    sys.stderr.write('gh: --slurp not supported with --jq\n')
    sys.exit(1)
state = json.load(open(state_path, encoding='utf-8'))
def save():
    json.dump(state, open(state_path, 'w', encoding='utf-8'))
def fail(msg, code=1):
    sys.stderr.write(msg + '\n'); sys.exit(code)
def release_by_id(rid):
    for r in state['releases']:
        if r['id'] == rid:
            return r
    fail('not found', 1)
def hook(name):
    # Test hook: run a named side effect before a given call, e.g. open a blocker.
    if name in state.get('hooks', {}):
        action = state['hooks'].pop(name)
        if action == 'open_blocker':
            state['issues'].append({'number': 99, 'state': 'open', 'labels': [{'name': 'release-blocker'}]})
        if action == 'add_extra_asset':
            for r in state['releases']:
                if r.get('draft'):
                    r['assets'].append({'name': 'extra.txt', 'digest': 'sha256:' + '1' * 64})
        save()
def path_arg():
    return [a for a in argv if a.startswith('repos/')][0]
if argv[:1] == ['api'] and '--paginate' in argv and '--slurp' in argv and '--jq' not in argv:
    # The Python side reads pagination itself: --slurp yields an array of pages.
    if state.get('listing_error'):
        fail('HTTP 500', 1)
    if '/labels?' in path_arg():
        pages = [[{'name': x} for x in state['labels']]]
    elif '/issues?' in path_arg():
        hook('before_issues')
        rows = [i for i in state['issues'] if i['state'] == 'open'
                and any(x['name'] == 'release-blocker' for x in i.get('labels', []))]
        # Pagination fixture: the first page is empty and the blocker sits on the second one.
        pages = [[], rows] if state.get('split_issue_pages') else [rows]
    else:
        rows = state['releases']
        if state.get('list_lag', 0) > 0 and state.get('created_draft'):
            # N5: the listing lags the create: after a draft exists, the drafts are hidden for the next N
            # listings. The pre-create listing is not affected, so the lag applies only to the re-read.
            state['list_lag'] -= 1
            save()
            rows = [r for r in rows if r.get('draft') is not True]
        pages = [rows]
    print(json.dumps(pages))
elif argv[:1] == ['api'] and '--paginate' in argv and '--slurp' in argv:
    fail('gh: --slurp not supported with --jq', 1)
elif argv[:1] == ['release'] and argv[1:2] == ['create']:
    hook('before_create')
    if '--draft' not in argv:
        fail('create without --draft', 1)
    rid = state['next_id']; state['next_id'] += 1
    assets = []
    for path in argv[-2:]:
        name = os.path.basename(path)
        digest = state['digests'].get(name)
        assets.append({'name': name, 'digest': digest})
    release = {'id': rid, 'tag_name': argv[2], 'draft': True, 'assets': assets}
    state['created_draft'] = True
    pending = state.pop('pending_patch_failure', None)
    if pending == 'fail':
        release['patch_fail'] = True
    elif pending == 'land':
        release['patch_land_then_fail'] = True
    state['releases'].append(release)
    save()
elif argv[:1] == ['api'] and '-X' in argv and 'PATCH' in argv:
    path = [a for a in argv if a.startswith('repos/')][0]
    rid = int(path.rsplit('/', 1)[1])
    hook('before_patch')
    release = release_by_id(rid)
    release['undraft_attempted'] = True
    save()
    if release.get('patch_fail'):
        fail('patch failed', 1)
    if release.get('patch_land_then_fail'):
        # N5: GitHub applied the undraft, but the response was lost: the caller sees a failure.
        release['draft'] = False
        save()
        fail('patch response lost after GitHub applied it', 1)
    release['draft'] = False
    save()
elif argv[:1] == ['api'] and '--method' in argv and 'DELETE' in argv:
    rid = int([a for a in argv if a.startswith('repos/')][0].rsplit('/', 1)[1])
    state['releases'] = [r for r in state['releases'] if r['id'] != rid]
    save()
elif argv[:1] == ['api'] and '--method' not in argv:
    rid = int([a for a in argv if a.startswith('repos/')][0].rsplit('/', 1)[1])
    hook('before_detail')
    if state.get('refetch_fail') and any(r['id'] == rid and r.get('undraft_attempted') for r in state['releases']):
        # N5: the single-release re-read AFTER a failed undraft fails. Verification reads are not affected.
        fail('HTTP 503 on re-read', 1)
    print(json.dumps(release_by_id(rid)))
else:
    fail('unmodelled gh call: ' + ' '.join(argv), 2)
'''


class PublishFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.bin = base / 'bin'
        self.bin.mkdir()
        gh = self.bin / 'gh'
        gh.write_text(FAKE_GH, encoding='utf-8')
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.log = base / 'gh.log'
        self.state_path = base / 'state.json'
        self.dist = base / 'dist'
        self.dist.mkdir()
        self.archive_bytes = b'archive bytes'
        self.sums_bytes = None
        (self.dist / ARCHIVE).write_bytes(self.archive_bytes)
        self.sums_bytes = f'{hashlib.sha256(self.archive_bytes).hexdigest()}  {ARCHIVE}\n'.encode()
        (self.dist / 'SHA256SUMS').write_bytes(self.sums_bytes)
        self.digests = {
            ARCHIVE: 'sha256:' + hashlib.sha256(self.archive_bytes).hexdigest(),
            'SHA256SUMS': 'sha256:' + hashlib.sha256(self.sums_bytes).hexdigest(),
        }
        self.notes = base / 'notes.md'
        self.notes.write_text('notes\n', encoding='utf-8')
        self.state = {'releases': [], 'issues': [], 'labels': ['release-blocker'], 'next_id': 100,
                      'digests': dict(self.digests), 'hooks': {}}
        self.save()
        self.env = {'PATH': f'{self.bin}{os.pathsep}{os.environ.get("PATH", "/usr/bin:/bin")}',
                    'FAKE_GH_STATE': str(self.state_path), 'FAKE_GH_LOG': str(self.log),
                    'GH_TOKEN': '', 'HOME': str(base)}

    def save(self):
        self.state_path.write_text(json.dumps(self.state), encoding='utf-8')

    def load(self):
        return json.loads(self.state_path.read_text(encoding='utf-8'))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]

    def publish_cli(self):
        """Run the real CLI in a subprocess so PATH and the fake gh are the only tools seen."""
        return subprocess.run([sys.executable, str(ROOT / 'release_artifacts.py'), 'publish',
                               '--repo', REPO, '--tag', TAG, '--dist', str(self.dist),
                               '--notes-file', str(self.notes)],
                              capture_output=True, text=True, env=self.env, timeout=120, check=False)

    def publish_inproc(self):
        """In-process call with the fake gh on PATH; used when a test needs to inspect state."""
        old_path = os.environ.get('PATH')
        saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        try:
            return ra.publish(REPO, TAG, self.dist, notes_file=str(self.notes))
        finally:
            os.environ['PATH'] = old_path
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class PublishHappyPathTests(PublishFixture):
    def test_happy_path_creates_verifies_then_publishes_by_id_in_order(self):
        proc = self.publish_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv_list = self.calls()
        self.assertFalse(any('--slurp' in a and '--jq' in a for a in argv_list),
                         'gh must never be called with --slurp together with --jq')
        create = next(i for i, a in enumerate(argv_list) if a[:2] == ['release', 'create'])
        detail = next(i for i, a in enumerate(argv_list) if a[:1] == ['api'] and a[-1:] == [f'repos/{REPO}/releases/100'])
        patch = next(i for i, a in enumerate(argv_list) if '-X' in a and 'PATCH' in a)
        self.assertLess(create, detail)
        self.assertLess(detail, patch)
        self.assertIn('--draft', argv_list[create])
        self.assertIn(['api', '-X', 'PATCH', f'repos/{REPO}/releases/100', '-F', 'draft=false'], argv_list)
        self.assertFalse(self.load()['releases'][0]['draft'])

    def test_publish_is_by_id_not_by_tag_name(self):
        self.publish_cli()
        for a in self.calls():
            if 'PATCH' in a:
                self.assertIn('releases/100', ' '.join(a))
                self.assertNotIn('edit', a)

    def test_draft_is_created_with_both_assets_and_notes(self):
        self.publish_cli()
        create = next(a for a in self.calls() if a[:2] == ['release', 'create'])
        self.assertIn('--notes-file', create)
        self.assertIn('--verify-tag', create)
        self.assertNotIn('--prerelease', create)
        self.assertEqual(create[-2:], [str(self.dist / ARCHIVE), str(self.dist / 'SHA256SUMS')])

    def test_stale_draft_for_the_tag_is_deleted_before_create(self):
        self.state['releases'].append({'id': 7, 'tag_name': TAG, 'draft': True, 'assets': []})
        self.save()
        self.publish_cli()
        names = [a for a in self.calls() if '--method' in a and 'DELETE' in a]
        self.assertEqual(len(names), 1)
        create_at = next(i for i, a in enumerate(self.calls()) if a[:2] == ['release', 'create'])
        delete_at = next(i for i, a in enumerate(self.calls()) if '--method' in a and 'DELETE' in a)
        self.assertLess(delete_at, create_at)
        self.assertEqual(self.load()['releases'][0]['id'], 100)


class PublishRefusalTests(PublishFixture):
    def test_existing_published_release_is_refused_without_any_mutation(self):
        self.state['releases'].append({'id': 5, 'tag_name': TAG, 'draft': False, 'assets': []})
        self.save()
        proc = self.publish_cli()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('already exists', proc.stderr)
        self.assertFalse(any(a[:2] == ['release', 'create'] for a in self.calls()))
        self.assertFalse(any('PATCH' in a or 'DELETE' in a for a in self.calls()))

    def test_digest_mismatch_deletes_the_draft_and_never_publishes(self):
        self.state['digests'][ARCHIVE] = 'sha256:' + '0' * 64
        self.save()
        proc = self.publish_cli()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('does not match the local SHA256', proc.stderr)
        self.assertFalse(any('PATCH' in a for a in self.calls()))
        self.assertEqual(self.load()['releases'], [], 'the failed draft must be deleted')

    def test_missing_digest_fails_closed_and_deletes_the_draft(self):
        self.state['digests']['SHA256SUMS'] = None
        self.save()
        proc = self.publish_cli()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('no GitHub digest', proc.stderr)
        self.assertFalse(any('PATCH' in a for a in self.calls()))
        self.assertEqual(self.load()['releases'], [])

    def test_extra_asset_on_the_draft_is_refused_and_deleted(self):
        self.state['hooks']['before_detail'] = 'add_extra_asset'
        self.save()
        proc = self.publish_cli()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('exactly the two release assets', proc.stderr)
        self.assertFalse(any('PATCH' in a for a in self.calls()))
        self.assertEqual(self.load()['releases'], [])

    def test_blocker_opened_before_publish_deletes_the_draft(self):
        # Opened after the draft is verified and during the immediate pre-publish check.
        self.state['hooks']['before_issues'] = 'open_blocker'
        self.save()
        proc = self.publish_cli()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('release-blocker', proc.stderr)
        self.assertFalse(any('PATCH' in a for a in self.calls()),
                         'blocker re-check must happen before un-drafting')
        self.assertEqual(self.load()['releases'], [], 'the draft must be deleted when a blocker is open')

    def test_missing_blocker_label_refuses_and_deletes_the_draft(self):
        # GitHub lists no Issues for a missing label, so the absence must be detected, not read as "clear".
        self.state['labels'] = []
        self.save()
        proc = self.publish_cli()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("label 'release-blocker' does not exist", proc.stderr)
        self.assertFalse(any('PATCH' in a for a in self.calls()))
        self.assertEqual(self.load()['releases'], [], 'the draft must be deleted when the label is missing')

    def test_blocker_on_a_later_listing_page_is_refused(self):
        self.state['issues'].append({'number': 98, 'state': 'open', 'labels': [{'name': 'release-blocker'}]})
        self.state['split_issue_pages'] = True
        self.save()
        proc = self.publish_cli()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('open release-blocker Issues: #98', proc.stderr)
        self.assertFalse(any('PATCH' in a for a in self.calls()))
        self.assertEqual(self.load()['releases'], [])

    def test_failed_undraft_on_a_still_draft_release_deletes_it(self):
        # N5: the PATCH failed and the release is still a draft: it is ours to delete.
        self._arm_patch_failure(land=False)
        proc = self.publish_cli()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('patch failed', proc.stderr)
        self.assertEqual(self.load()['releases'], [], 'a still-draft release must be deleted')

    def test_undraft_that_landed_before_the_response_failed_is_never_deleted(self):
        # N5: GitHub made the release public, then the response was lost. Deleting it would destroy a
        # published release, so the code re-reads it, sees draft=false, and reports it instead.
        self._arm_patch_failure(land=True)
        proc = self.publish_cli()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('ALREADY PUBLIC', proc.stderr)
        remaining = self.load()['releases']
        self.assertEqual(len(remaining), 1, 'the public release must not be deleted')
        self.assertFalse(remaining[0]['draft'])
        self.assertFalse(any(a[:1] == ['api'] and 'DELETE' in a for a in self.calls()),
                         'no DELETE may be issued for a release that is public')

    def test_unreadable_state_after_a_failed_undraft_does_not_delete_blind(self):
        # N5: when the re-read fails the state is unknown. The code reports it and does not delete.
        self._arm_patch_failure(land=False)
        self.state['refetch_fail'] = True
        self.save()
        proc = self.publish_cli()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('could not confirm the state', proc.stderr)
        self.assertFalse(any(a[:1] == ['api'] and 'DELETE' in a for a in self.calls()),
                         'an unknown state must not be deleted blind')

    def test_drafts_listing_that_lags_the_create_is_retried_and_publishes(self):
        # N5: the first listing after `gh release create` misses the new draft; a retry sees it.
        self.state['list_lag'] = 1
        self.save()
        proc = self.publish_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.load()['releases'][0]['draft'])

    def test_drafts_listing_still_empty_after_retries_refuses_without_deleting(self):
        self.state['list_lag'] = 99
        self.save()
        proc = self.publish_cli()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('expected exactly one draft', proc.stderr)
        self.assertFalse(any('PATCH' in a for a in self.calls()))

    def _arm_patch_failure(self, *, land: bool):
        """Arm the fake so the first undraft PATCH on the created draft fails (optionally after landing)."""
        self.state['pending_patch_failure'] = 'land' if land else 'fail'
        self.save()

    def test_failed_listing_refuses_before_any_creation(self):
        self.state['listing_error'] = True
        self.save()
        proc = self.publish_cli()
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(any(a[:2] == ['release', 'create'] for a in self.calls()))

    def test_invalid_tag_and_both_notes_modes_are_refused(self):
        with self.assertRaises(ra.ArtifactError):
            ra.publish(REPO, 'v1.2', self.dist, generate_notes=True)
        with self.assertRaises(ra.ArtifactError):
            ra.publish(REPO, TAG, self.dist)


if __name__ == '__main__':
    unittest.main()
