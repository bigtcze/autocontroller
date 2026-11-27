"""Offline release qualification tests: pure release_policy logic with injected fakes only."""
import json
import unittest

import release_policy as rp

REPO = 'example-owner/alpha'
SHA = 'a1' * 20
CANDIDATE = 'b2' * 20
RUN = 4242
HEX64 = 'c3' * 32
VM_JOBS = ['vm (debian-12)', 'vm (debian-13)', 'vm (ubuntu-24.04)', 'vm (ubuntu-26.04)']


def evidence(tag='v1.1.0', candidate=CANDIDATE, run=RUN, **overrides):
    live = {name: {'result': 'passed', 'report_sha256': HEX64, 'date': '2026-11-20', 'summary': 'ok'}
            for name in ('github', 'opencode_omo', 'engram')}
    doc = {'schema': 1, 'tag': tag, 'candidate_sha': candidate, 'vm_lifecycle_run': run, 'live_acceptance': live}
    doc.update(overrides)
    return json.dumps(doc)


class Fakes:
    """A complete, green GitHub/git world; tests break exactly one thing at a time."""

    def __init__(self):
        self.runs = {'workflow_runs': [{'head_sha': SHA, 'event': 'push', 'status': 'completed',
                                        'conclusion': 'success', 'run_number': 7, 'run_attempt': 1, 'id': 1}],
                     'total_count': 1}
        self.blockers = []
        self.labels = [{'name': 'release-blocker'}]
        self.vm_run = {'path': rp.VM_LIFECYCLE_WORKFLOW, 'head_sha': CANDIDATE,
                       'status': 'completed', 'conclusion': 'success'}
        self.jobs = {'total_count': 4, 'jobs': [{'name': n, 'conclusion': 'success'} for n in VM_JOBS]}
        self.evidence = evidence()
        self.ancestors = {(CANDIDATE, SHA), (SHA, 'origin/main')}
        self.changed = ['docs/releases/v1.1.0.evidence.json', 'docs/releases/v1.1.0.md']
        self.main_ok = True

    def gh_json(self, *args):
        path = args[-1]
        if '/actions/workflows/required.yml/runs' in path:
            return self.runs
        if path.endswith('/jobs?per_page=100'):
            return self.jobs
        if '/actions/runs/' in path:
            return self.vm_run
        raise AssertionError(f'unexpected gh_json {path}')

    def pages(self, path):
        if path == f'repos/{REPO}/labels?per_page=100':
            return self.labels
        assert path.startswith(f'repos/{REPO}/issues?state=open&labels=release-blocker'), path
        return self.blockers

    def qualify(self, tag='v1.1.0', sha=SHA, previous='v1.0.0', **overrides):
        kwargs = dict(previous_tag=previous, on_main=self.main_ok, gh_json=self.gh_json, pages=self.pages,
                      is_ancestor=lambda a, b: (a, b) in self.ancestors or a == b,
                      changed_paths=lambda a, b: self.changed,
                      read_file=lambda commit, path: self.evidence if path.endswith('.evidence.json') else None)
        kwargs.update(overrides)
        return rp.qualify_release(REPO, tag, sha, **kwargs)


def listing_pages(issues, labels=('release-blocker',), seen=None):
    """Fake `pages`: the labels listing and the issues listing, dispatched by path."""
    def pages(path):
        if seen is not None:
            seen.append(path)
        if path == f'repos/{REPO}/labels?per_page=100':
            return [{'name': x} for x in labels] if isinstance(labels, tuple) else labels
        return issues
    return pages


class BlockerListingTests(unittest.TestCase):
    def test_issues_are_listed_by_label_and_pull_requests_are_excluded(self):
        rows = [{'number': 1, 'state': 'open'}, {'number': 2, 'state': 'open', 'pull_request': {'url': 'x'}}]
        got = rp.open_release_blockers(REPO, listing_pages(rows), 'release-blocker')
        self.assertEqual([x['number'] for x in got], [1])

    def test_closed_blockers_are_ignored_and_only_open_issues_are_queried(self):
        seen = []
        rows = [{'number': 1, 'state': 'closed'}, {'number': 2, 'state': 'open'}]
        got = rp.open_release_blockers(REPO, listing_pages(rows, seen=seen), 'release-blocker')
        self.assertEqual([x['number'] for x in got], [2])
        self.assertIn('state=open&', seen[-1])

    def test_missing_label_is_refused_even_when_no_issue_carries_it(self):
        with self.assertRaisesRegex(ValueError, "label 'release-blocker' does not exist"):
            rp.open_release_blockers(REPO, listing_pages([], labels=()), 'release-blocker')

    def test_label_match_is_exact(self):
        for name in ('Release-Blocker', 'release-blocker ', 'release-blockers'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                rp.open_release_blockers(REPO, listing_pages([], labels=(name,)), 'release-blocker')

    def test_non_list_label_listing_fails_closed(self):
        with self.assertRaises(ValueError):
            rp.open_release_blockers(REPO, listing_pages([], labels={'message': 'Not Found'}))

    def test_non_list_issue_listing_fails_closed(self):
        with self.assertRaises(ValueError):
            rp.open_release_blockers(REPO, lambda path: {'message': 'Not Found'} if 'issues' in path else [{'name': 'release-blocker'}])

    # NIT: the blocker label is URL-encoded; the controller accepts spaces and ':' in it.
    def test_label_with_spaces_is_url_encoded_in_the_query(self):
        seen = []
        rp.open_release_blockers(REPO, listing_pages([], labels=('release blocker: critical',), seen=seen),
                                 'release blocker: critical')
        issue_queries = [p for p in seen if '/issues?' in p]
        self.assertEqual(len(issue_queries), 1)
        self.assertIn('labels=release%20blocker%3A%20critical&', issue_queries[0])
        self.assertNotIn(' ', issue_queries[0])


class EvidenceSchemaTests(unittest.TestCase):
    def test_valid_record_has_no_errors(self):
        self.assertEqual(rp.validate_evidence(json.loads(evidence()), 'v1.1.0'), [])

    def test_each_schema_violation_is_reported(self):
        cases = {
            'wrong schema': dict(schema=2),
            'wrong tag': dict(tag='v1.2.0'),
            'short candidate': dict(candidate_sha='abc'),
            'bool run id': dict(vm_lifecycle_run=True),
            'string run id': dict(vm_lifecycle_run='4242'),
            'extra key': dict(extra=1),
        }
        for label, change in cases.items():
            with self.subTest(label=label):
                doc = json.loads(evidence())
                doc.update(change)
                self.assertTrue(rp.validate_evidence(doc, 'v1.1.0'))

    def test_live_entries_must_pass_with_digest_date_and_summary(self):
        base = json.loads(evidence())
        broken = {
            'result': lambda e: e.update(result='failed'),
            'digest': lambda e: e.update(report_sha256='xyz'),
            'date': lambda e: e.update(date='26-11-20'),
            'summary': lambda e: e.update(summary=''),
            'long summary': lambda e: e.update(summary='x' * 501),
        }
        for label, mutate in broken.items():
            with self.subTest(label=label):
                doc = json.loads(json.dumps(base))
                mutate(doc['live_acceptance']['engram'])
                self.assertTrue(rp.validate_evidence(doc, 'v1.1.0'))

    def test_missing_live_suite_is_rejected(self):
        doc = json.loads(evidence())
        del doc['live_acceptance']['opencode_omo']
        self.assertTrue(rp.validate_evidence(doc, 'v1.1.0'))

    # NIT: evidence hex is lowercase only; an uppercase digest is refused, not normalised.
    def test_uppercase_candidate_sha_is_rejected(self):
        doc = json.loads(evidence())
        doc['candidate_sha'] = doc['candidate_sha'].upper()
        self.assertTrue(any('candidate_sha' in e for e in rp.validate_evidence(doc, 'v1.1.0')))

    def test_uppercase_report_digest_is_rejected(self):
        doc = json.loads(evidence())
        doc['live_acceptance']['github']['report_sha256'] = doc['live_acceptance']['github']['report_sha256'].upper()
        self.assertTrue(any('report_sha256' in e for e in rp.validate_evidence(doc, 'v1.1.0')))


class QualifyReleaseTests(unittest.TestCase):
    def assertBlocked(self, fakes, fragment, **kwargs):
        errors = fakes.qualify(**kwargs)
        self.assertTrue(any(fragment in e for e in errors), errors)

    def test_complete_feature_release_qualifies(self):
        self.assertEqual(Fakes().qualify(), [])

    def test_patch_release_needs_no_evidence_but_all_other_rules(self):
        fakes = Fakes()
        fakes.evidence = None
        self.assertEqual(fakes.qualify(tag='v1.0.1', previous='v1.0.0'), [])
        fakes.runs['workflow_runs'][0]['conclusion'] = 'failure'
        self.assertBlocked(fakes, 'not a completed success', tag='v1.0.1', previous='v1.0.0')

    def test_non_semver_tag_and_bad_sha_are_refused(self):
        self.assertBlocked(Fakes(), 'not a stable SemVer', tag='v1.1.0-rc1')
        self.assertBlocked(Fakes(), '40 hex', sha='abc')

    def test_candidate_not_on_main_is_refused(self):
        self.assertBlocked(Fakes(), 'not on main', on_main=False)

    def test_missing_push_run_is_refused(self):
        fakes = Fakes()
        fakes.runs = {'workflow_runs': [], 'total_count': 0}
        self.assertBlocked(fakes, 'no required.yml push run')

    def test_truncated_run_listing_fails_closed(self):
        fakes = Fakes()
        fakes.runs['total_count'] = 150
        self.assertBlocked(fakes, 'unavailable or truncated')

    def test_newest_run_must_be_the_successful_one(self):
        fakes = Fakes()
        fakes.runs['workflow_runs'].append({'head_sha': SHA, 'event': 'push', 'status': 'completed',
                                            'conclusion': 'failure', 'run_number': 8, 'run_attempt': 1, 'id': 2})
        self.assertBlocked(fakes, 'newest required.yml push run')

    def test_open_release_blocker_is_refused(self):
        fakes = Fakes()
        fakes.blockers = [{'number': 99, 'state': 'open'}]
        self.assertBlocked(fakes, 'open release blockers: #99')

    def test_blocker_listing_failure_is_refused(self):
        fakes = Fakes()
        def broken(path):
            raise ValueError('unavailable')
        self.assertBlocked(fakes, 'unavailable', pages=broken)

    def test_missing_blocker_label_is_refused_not_read_as_no_blockers(self):
        fakes = Fakes()
        fakes.labels = []
        self.assertBlocked(fakes, "label 'release-blocker' does not exist")

    def test_non_list_label_listing_is_refused(self):
        fakes = Fakes()
        fakes.labels = {'message': 'Not Found'}
        self.assertBlocked(fakes, 'label listing unavailable')

    def test_feature_release_without_evidence_is_refused(self):
        fakes = Fakes()
        fakes.evidence = None
        self.assertBlocked(fakes, 'missing docs/releases/v1.1.0.evidence.json')

    def test_evidence_candidate_must_be_an_ancestor_of_the_release_sha(self):
        fakes = Fakes()
        fakes.ancestors = {(SHA, 'origin/main')}
        self.assertBlocked(fakes, 'is not an ancestor')

    def test_changes_after_candidate_must_be_release_documentation_only(self):
        fakes = Fakes()
        fakes.changed = ['docs/releases/v1.1.0.md', 'autonomy-controller.py']
        fakes.ancestors.add((CANDIDATE, SHA))
        self.assertBlocked(fakes, 'outside docs/releases/')

    def test_documentation_only_delta_after_candidate_is_accepted(self):
        fakes = Fakes()
        fakes.ancestors.add((CANDIDATE, SHA))
        self.assertEqual(fakes.qualify(), [])

    def test_vm_run_on_a_different_commit_is_refused(self):
        fakes = Fakes()
        fakes.vm_run = dict(fakes.vm_run, head_sha='d4' * 20)
        self.assertBlocked(fakes, 'head_sha is not candidate')

    def test_vm_run_from_the_wrong_workflow_is_refused(self):
        fakes = Fakes()
        fakes.vm_run = dict(fakes.vm_run, path='.github/workflows/regression.yml')
        self.assertBlocked(fakes, 'is not .github/workflows/vm-lifecycle.yml')

    def test_each_distribution_vm_job_must_succeed(self):
        for name in VM_JOBS:
            with self.subTest(job=name):
                fakes = Fakes()
                fakes.jobs['jobs'] = [dict(j, conclusion='failure') if j['name'] == name else j
                                      for j in fakes.jobs['jobs']]
                self.assertBlocked(fakes, f'job "{name}"')

    def test_missing_vm_job_is_refused(self):
        fakes = Fakes()
        fakes.jobs['jobs'] = fakes.jobs['jobs'][:3]
        fakes.jobs['total_count'] = 3
        self.assertBlocked(fakes, 'job "vm (ubuntu-26.04)"')

    def test_truncated_job_listing_fails_closed(self):
        fakes = Fakes()
        fakes.jobs['total_count'] = 40
        self.assertBlocked(fakes, 'job listing unavailable or truncated')

    def test_unreadable_run_fails_closed(self):
        fakes = Fakes()
        def broken(*args):
            if '/actions/runs/' in args[-1]:
                raise RuntimeError('HTTP 500')
            return fakes.gh_json(*args)
        self.assertBlocked(fakes, 'cannot read vm-lifecycle run', gh_json=broken)

    def test_previous_tag_is_excluded_from_latest_release_lookup(self):
        releases = [{'tag_name': 'v1.1.0', 'published_at': '2026-11-25T00:00:00Z'},
                    {'tag_name': 'v1.0.0', 'published_at': '2026-11-01T00:00:00Z'}]
        latest = rp.latest_release(REPO, lambda path: releases, exclude_tag='v1.1.0')
        self.assertEqual(latest['tag_name'], 'v1.0.0')


if __name__ == '__main__':
    unittest.main()
