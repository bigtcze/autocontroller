"""GitHub-native release eligibility, human acceptance and publication.

No repository configuration or controller state is created by this module.
All authorizations are bound to an exact release tag and main commit SHA.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
import re
from typing import Any, Callable

SEMVER = re.compile(r'^v(\d+)\.(\d+)\.(\d+)$')
RELEASE_ISSUE = re.compile(r'(?i)\b(?:feature\s+)?release\s*[:\-–]?\s*(v\d+\.\d+\.\d+)\b')
RELEASE_ISSUE_REVERSED = re.compile(r'(?i)\b(v\d+\.\d+\.\d+)\s+(?:feature\s+)?release\b')
APPROVAL_REQUEST = 'AUTOCONTROLLER_FEATURE_RELEASE_REQUEST'
APPROVAL_RESPONSE = 'APPROVE FEATURE RELEASE'
# Verified identity of the GitHub.com Renovate App bot user. Logins, branch
# names and labels are attacker-chosen text: none of them is proof on its own.
RENOVATE_BOT_LOGIN = 'renovate[bot]'
RENOVATE_BOT_ID = 29139614  # GitHub.com Renovate App bot user (type Bot)
TECHNICAL_LABELS = {'dependencies','dependency','maintenance','chore','security','bugfix','bug','fix'}


def version(tag: str) -> tuple[int, int, int] | None:
    match = SEMVER.fullmatch(str(tag or ''))
    return tuple(map(int,match.groups())) if match else None


def release_issue_tag(title: str) -> str | None:
    match = RELEASE_ISSUE.search(title or '') or RELEASE_ISSUE_REVERSED.search(title or '')
    return match.group(1).lower() if match else None


def explicitly_feature_title(title: str) -> bool:
    return bool(re.search(r'(?i)\bfeature\s+release\b', title or ''))


def is_feature_release(tag: str, previous_tag: str | None) -> bool:
    current = version(tag)
    prior = version(previous_tag) if previous_tag else None
    if not current:
        return False
    if prior is None:
        return True  # Initial release always needs owner acceptance.
    return current[:2] > prior[:2]


def parse_date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone.utc)


def released_since(release: dict, now: datetime) -> float:
    return (now-parse_date(release['published_at'])).total_seconds()/86400


def latest_release(repo: str, pages: Callable, *, exclude_tag: str | None = None) -> dict | None:
    releases=pages(f'repos/{repo}/releases?per_page=100')
    valid=[x for x in releases if isinstance(x,dict) and not x.get('draft')
           and not x.get('prerelease') and version(x.get('tag_name')) and x.get('published_at')
           and x.get('tag_name') != exclude_tag]
    return max(valid,key=lambda x:(parse_date(x['published_at']),version(x['tag_name']))) if valid else None


def latest_main_sha(repo: str, base: str, gh_json: Callable) -> str:
    data=gh_json('api',f'repos/{repo}/branches/{base}')
    sha=((data.get('commit') or {}).get('sha') or '')
    if not re.fullmatch(r'[a-fA-F0-9]{40}',sha):
        raise ValueError('Unable to determine exact main commit SHA')
    return sha


def compare_tag_to_sha(repo: str, tag: str, sha: str, gh_json: Callable) -> bool:
    data=gh_json('api',f'repos/{repo}/compare/{tag}...{sha}')
    return data.get('status') == 'ahead' and int(data.get('ahead_by',0))>0


def merged_since(repo: str, base: str, published_at: str, gh_json: Callable, fetch_pr: Callable) -> list[dict]:
    # GitHub Search includes date-granular boundaries; check exact merged_at on each result.
    date=parse_date(published_at).strftime('%Y-%m-%d')
    query=f'repo:{repo} is:pr is:merged base:{base} merged:>={date}'
    data=gh_json('api','--method','GET','search/issues','-f',f'q={query}','-f','per_page=100')
    results=data.get('items') if isinstance(data,dict) else None
    if not isinstance(results,list) or int(data.get('total_count',0))>len(results):
        raise ValueError('Merged PR search truncated or unavailable; refusing maintenance release')
    out=[]
    for item in results:
        pr=fetch_pr(repo,int(item['number']))
        if not pr.get('merged_at') or parse_date(pr['merged_at'])<=parse_date(published_at):
            continue
        if (pr.get('base') or {}).get('ref')!=base:
            continue
        out.append(pr)
    return out


def is_renovate_identity(user: Any) -> bool:
    """Only the genuine GitHub.com Renovate App bot account (login + type + id)."""
    if not isinstance(user, dict):
        return False
    return (str(user.get('login') or '').lower() == RENOVATE_BOT_LOGIN
            and user.get('type') == 'Bot' and user.get('id') == RENOVATE_BOT_ID)


def same_repository_pr(pr: dict, repo: str) -> bool:
    """PR head must be a branch of `repo` itself; fork or missing head repo is False."""
    repo = str(repo or '').lower()
    head_repo = (pr.get('head') or {}).get('repo')
    if not repo or not isinstance(head_repo, dict) or str(head_repo.get('full_name') or '').lower() != repo:
        return False
    base_repo = (pr.get('base') or {}).get('repo')
    return base_repo is None or str((base_repo or {}).get('full_name') or '').lower() == repo


def renovate_pr(pr: dict, repo: str) -> bool:
    """Single verified predicate: genuine Renovate bot AND same-repository head.

    Branch prefix, logins and labels never establish identity. Missing head
    repository data fails closed (see same_repository_pr).
    """
    return is_renovate_identity(pr.get('user')) and same_repository_pr(pr, repo)


def technical_pr(pr: dict, repo: str) -> bool:
    labels={str(x.get('name') or '').lower() for x in (pr.get('labels') or [])}
    return renovate_pr(pr, repo) or bool(labels & TECHNICAL_LABELS)


def maintenance_due(release: dict | None, now: datetime, prs: list[dict], repo: str,
                    *, days=14, minimum=3, feature_active=False) -> bool:
    if release is None or feature_active or released_since(release,now)<days:
        return False
    # Avoid silently publishing a feature under a patch version.
    if any(not technical_pr(p, repo) for p in prs):
        return False
    return sum(renovate_pr(p, repo) for p in prs)>=minimum


def approve_request_body(tag: str, sha: str) -> str:
    return (f'{APPROVAL_REQUEST} tag={tag} sha={sha}\n\n'
            f'Feature release **{tag}** is ready at commit `{sha}`.\n'
            'Development and required CI are complete; publication is waiting for the owner.\n'
            'To approve this **exact candidate**, add a new GitHub Issue comment consisting of:\n\n'
            f'`{APPROVAL_RESPONSE} {tag} {sha}`\n\n'
            'Any changed candidate SHA requires a new approval. No other task requires approval.')


def approval_state(comments: list[dict],tag: str,sha: str,approver: str) -> tuple[bool,bool]:
    """Return (request exists, owner approved after request); never treat bot text as approval."""
    marker=f'{APPROVAL_REQUEST} tag={tag} sha={sha}'
    requests=[c for c in comments if str(c.get('body') or '').splitlines()[0:1]==[marker]]
    if not requests:
        return False,False
    latest=max(requests,key=lambda c:int(c.get('id') or 0))
    request_id=int(latest.get('id') or 0)
    expected=f'{APPROVAL_RESPONSE} {tag} {sha}'
    approved=any(int(c.get('id') or 0)>request_id and str(c.get('body') or '').strip()==expected
                 and str((c.get('user') or {}).get('login') or '').lower()==approver.lower()
                 for c in comments)
    return True,approved


def next_patch_tag(previous_tag: str) -> str:
    v=version(previous_tag)
    if not v:
        raise ValueError('No stable version for patch release')
    return f'v{v[0]}.{v[1]}.{v[2]+1}'



RELEASE_BLOCKER_LABEL = 'release-blocker'
LIVE_ACCEPTANCE = ('github', 'opencode_omo', 'engram')
VM_LIFECYCLE_WORKFLOW = '.github/workflows/vm-lifecycle.yml'
VM_JOBS = ('vm (debian-12)', 'vm (debian-13)', 'vm (ubuntu-24.04)', 'vm (ubuntu-26.04)')
EVIDENCE_PATH = 'docs/releases/{tag}.evidence.json'
RELEASE_DOCS_PREFIX = 'docs/releases/'
HEX40 = re.compile(r'[0-9a-fA-F]{40}')
HEX64 = re.compile(r'[0-9a-fA-F]{64}')


def open_release_blockers(repo: str, pages: Callable, label: str = RELEASE_BLOCKER_LABEL) -> list[dict]:
    """Open Issues (never PRs) carrying `label`. A non-list listing fails closed."""
    rows = pages(f'repos/{repo}/issues?state=open&labels={label}&per_page=100')
    if not isinstance(rows, list):
        raise ValueError('Release blocker listing unavailable; refusing release')
    return [x for x in rows if isinstance(x, dict) and not x.get('pull_request')
            and str(x.get('state') or '').lower() == 'open']


def _iso_date(value: Any) -> bool:
    try:
        datetime.strptime(str(value), '%Y-%m-%d')
        return True
    except ValueError:
        return False


def validate_evidence(doc: Any, tag: str) -> list[str]:
    """Schema 1 evidence record for one feature release. Returns errors; empty means valid."""
    if not isinstance(doc, dict):
        return ['evidence is not a JSON object']
    errors = []
    if set(doc) != {'schema', 'tag', 'candidate_sha', 'vm_lifecycle_run', 'live_acceptance'}:
        errors.append('evidence has missing or unexpected top-level keys')
    if doc.get('schema') != 1:
        errors.append('evidence schema must be 1')
    if doc.get('tag') != tag:
        errors.append(f'evidence tag must be {tag}')
    if not HEX40.fullmatch(str(doc.get('candidate_sha') or '')):
        errors.append('evidence candidate_sha must be 40 hex characters')
    run = doc.get('vm_lifecycle_run')
    if not isinstance(run, int) or isinstance(run, bool) or run <= 0:
        errors.append('evidence vm_lifecycle_run must be a positive GitHub Actions run id')
    live = doc.get('live_acceptance')
    if not isinstance(live, dict) or set(live) != set(LIVE_ACCEPTANCE):
        errors.append('evidence live_acceptance must contain exactly github, opencode_omo and engram')
        return errors
    for name in LIVE_ACCEPTANCE:
        entry = live.get(name)
        if not isinstance(entry, dict):
            errors.append(f'live_acceptance.{name} must be an object')
            continue
        if entry.get('result') != 'passed':
            errors.append(f'live_acceptance.{name}.result must be "passed"')
        if not HEX64.fullmatch(str(entry.get('report_sha256') or '')):
            errors.append(f'live_acceptance.{name}.report_sha256 must be 64 hex characters')
        if not _iso_date(entry.get('date')):
            errors.append(f'live_acceptance.{name}.date must be an ISO date (YYYY-MM-DD)')
        summary = entry.get('summary')
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 500:
            errors.append(f'live_acceptance.{name}.summary must be 1-500 characters')
    return errors


def qualify_release(repo: str, tag: str, sha: str, *, previous_tag: str | None, on_main: bool,
                    gh_json: Callable, pages: Callable,
                    is_ancestor: Callable[[str, str], bool],
                    changed_paths: Callable[[str, str], list[str]],
                    read_file: Callable[[str, str], str | None]) -> list[str]:
    """Pure release qualification. Returns errors; an empty list means qualified.

    Side effects are injected: `gh_json(*args)`, `pages(path)`, `is_ancestor(a, b)`,
    `changed_paths(a, b)` and `read_file(commit, path)` (None when absent). `on_main` is the
    caller's ancestry result for `sha` against origin/main. Any unreadable state is an error.
    """
    if not version(tag):
        return [f'{tag!r} is not a stable SemVer tag']
    if not HEX40.fullmatch(sha or ''):
        return ['candidate sha must be 40 hex characters']
    errors = []
    if not on_main:
        errors.append(f'{sha} is not on main')
    try:
        runs = gh_json('api', f'repos/{repo}/actions/workflows/required.yml/runs?head_sha={sha}&event=push&per_page=100')
    except Exception as exc:
        return errors + [f'cannot read required.yml push runs: {exc}']
    rows = runs.get('workflow_runs') if isinstance(runs, dict) else None
    if not isinstance(rows, list) or int(runs.get('total_count', len(rows)) or 0) > len(rows):
        return errors + ['required.yml push run listing unavailable or truncated']
    rows = [r for r in rows if isinstance(r, dict) and r.get('head_sha') == sha and r.get('event') == 'push']
    if not rows:
        errors.append(f'no required.yml push run for {sha}')
    else:
        newest = max(rows, key=lambda r: (int(r.get('run_number') or 0), int(r.get('run_attempt') or 0), int(r.get('id') or 0)))
        if newest.get('status') != 'completed' or newest.get('conclusion') != 'success':
            errors.append(f'newest required.yml push run for {sha} is not a completed success')
    try:
        blockers = open_release_blockers(repo, pages)
    except ValueError as exc:
        errors.append(str(exc))
        blockers = []
    if blockers:
        errors.append('open release blockers: ' + ', '.join(f'#{x.get("number")}' for x in blockers))
    if not is_feature_release(tag, previous_tag):
        return errors  # Patch releases need no evidence; every other rule above still applies.
    path = EVIDENCE_PATH.format(tag=tag)
    text = read_file(sha, path)
    if text is None:
        return errors + [f'missing {path} at the tag commit {sha}']
    try:
        doc = json.loads(text)
    except ValueError:
        return errors + [f'{path} is not valid JSON']
    schema_errors = validate_evidence(doc, tag)
    if schema_errors:
        return errors + schema_errors
    candidate = doc['candidate_sha']
    if not is_ancestor(candidate, sha):
        errors.append(f'evidence candidate {candidate} is not an ancestor of {sha}')
    elif candidate != sha:
        try:
            changed = changed_paths(candidate, sha)
        except Exception as exc:
            return errors + [f'cannot diff {candidate}..{sha}: {exc}']
        outside = [p for p in changed if not p.startswith(RELEASE_DOCS_PREFIX) or '..' in p.split('/')]
        if outside:
            errors.append(f'changes {candidate}..{sha} outside {RELEASE_DOCS_PREFIX}: {", ".join(outside[:5])}')
    run_id = doc['vm_lifecycle_run']
    try:
        run = gh_json('api', f'repos/{repo}/actions/runs/{run_id}')
        jobs = gh_json('api', f'repos/{repo}/actions/runs/{run_id}/jobs?per_page=100')
    except Exception as exc:
        return errors + [f'cannot read vm-lifecycle run {run_id}: {exc}']
    run = run if isinstance(run, dict) else {}
    if run.get('path') != VM_LIFECYCLE_WORKFLOW:
        errors.append(f'run {run_id} is not {VM_LIFECYCLE_WORKFLOW}')
    if run.get('head_sha') != candidate:
        errors.append(f'vm-lifecycle run {run_id} head_sha is not candidate {candidate}')
    if run.get('status') != 'completed' or run.get('conclusion') != 'success':
        errors.append(f'vm-lifecycle run {run_id} is not a completed success')
    job_rows = jobs.get('jobs') if isinstance(jobs, dict) else None
    if not isinstance(job_rows, list) or int(jobs.get('total_count', len(job_rows)) or 0) > len(job_rows):
        return errors + [f'vm-lifecycle run {run_id} job listing unavailable or truncated']
    for name in VM_JOBS:
        found = [j for j in job_rows if isinstance(j, dict) and j.get('name') == name]
        if len(found) != 1 or found[0].get('conclusion') != 'success':
            errors.append(f'vm-lifecycle run {run_id} job "{name}" is missing or not a success')
    return errors
